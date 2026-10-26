# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Auto-generates a SemanticModel from a DataSourceSchema using an LLM."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from ..datasources.models import (
    ColumnInfo,
    DataSourceSchema,
    PrivacySettings,
    TableInfo,
    table_key,
)
from .drift import (
    SchemaDrift,
    compute_drift,
    policy_hides_column,
    policy_hides_table,
    resolve_real_key,
    strip_ident_quotes,
)
from .models import (
    GENERATION_BATCH_SIZE,
    AggregationType,
    BusinessMetric,
    CardinalityClass,
    ColumnsBatchResponse,
    ColumnSemantic,
    CommonJoin,
    DerivedColumn,
    EntityType,
    GlobalSectionsResponse,
    RelationshipEdge,
    RelationshipType,
    Segment,
    SemanticModel,
    SemanticType,
    TablesBatchResponse,
    TableSemantic,
    TimeGranularity,
)

if TYPE_CHECKING:
    from ..datasources.base import BaseDataSource
    from ..providers.base import BaseLLMProvider

logger = logging.getLogger(__name__)

# The per-column instructions of both annotation prompts (items 1-8; the table-level
# items 9-11 stay in ``_BATCH_TABLES_PROMPT``). One constant: a new column is
# annotated to the same standard as one from the full generation.
_COLUMN_FIELD_ITEMS = """\
1. A human-friendly display name
2. A business description explaining what this data represents
3. semantic_type: One of: identifier, status_flag, monetary, percentage, \
timestamp, date, free_text, categorical, boolean_flag, foreign_key, \
url, email, phone, measurement, count, unknown
4. For columns with coded/enum values (based on sample values), explain the likely \
meaning of each value
5. Identify columns that likely contain sensitive personal data (PII)
6. currency: ISO code if monetary (EUR, USD, GBP...), null otherwise
7. unit: Physical unit if measurement (kg, km, seconds, items...), null otherwise
8. default_aggregation: Prescribed aggregation for numeric/count columns — one of: \
sum, count, count_distinct, average, max, min, median. Omit for non-numeric columns."""

# Prompt for a single batch of tables (tables section only, no global metrics/joins).
_BATCH_TABLES_PROMPT = (
    """\
You are a data analyst examining a database schema. Analyze the tables below and \
generate semantic annotations.

DATABASE SCHEMA:
{schema_ddl}

{column_stats_summary}
For each table and column, provide:
"""
    + _COLUMN_FIELD_ITEMS
    + """
9. Suggest default filters that should typically be applied
10. grain: One sentence describing the row-level granularity, \
e.g. "one row per order" or "one row per customer per day".
11. hierarchies: If columns imply a natural drill-down path (date parts, geography, \
org structure), list them as ordered levels.

IMPORTANT: Omit any JSON field whose value is null, false, or empty ([] / {{}}).
Do NOT emit: "currency": null, "unit": null, "is_sensitive": false, "value_mappings": [].
Only emit fields that carry meaningful values.

Respond ONLY with valid JSON:
{{
  "tables": {{
    "schema.table_name": {{
      "display_name": "Human-Friendly Name",
      "description": "What this table contains",
      "grain": "one row per order",
      "default_filters": [],
      "hierarchies": [
        {{"name": "Date Hierarchy", "levels": ["order_year", "order_month", "order_date"]}}
      ],
      "columns": {{
        "column_name": {{
          "display_name": "Human Name",
          "description": "What this column means",
          "semantic_type": "monetary",
          "default_aggregation": "sum",
          "value_mappings": [{{"raw_value": "A", "display_value": "Active"}}],
          "is_sensitive": true
        }}
      }}
    }}
  }}
}}"""
)

# Prompt for the columns added to tables the model already annotates. The tables are
# context; only the listed columns are annotated, so nothing already stored is rewritten.
_NEW_COLUMNS_PROMPT = (
    """\
You are a data analyst examining a database schema. The tables below already have \
semantic annotations; new columns were added to them. Annotate ONLY the columns listed \
under NEW COLUMNS. The rest of each table is context.

DATABASE SCHEMA:
{schema_ddl}

NEW COLUMNS (annotate only these):
{targets}

For each new column, provide:
"""
    + _COLUMN_FIELD_ITEMS
    + """

IMPORTANT: Omit any JSON field whose value is null, false, or empty ([] / {{}}).
Do NOT emit: "currency": null, "unit": null, "is_sensitive": false, "value_mappings": [].
Only emit fields that carry meaningful values.

Respond ONLY with valid JSON:
{{
  "tables": {{
    "schema.table_name": {{
      "columns": {{
        "new_column_name": {{
          "display_name": "Human Name",
          "description": "What this column means",
          "semantic_type": "monetary",
          "default_aggregation": "sum",
          "value_mappings": [{{"raw_value": "A", "display_value": "Active"}}],
          "is_sensitive": true
        }}
      }}
    }}
  }}
}}"""
)

# Prompt for cross-table business metrics, joins, derived columns (global sections only).
_GLOBAL_SECTIONS_PROMPT = """\
You are a data analyst examining a database schema. Based on the table list below, \
identify cross-table business KPIs, derived columns, and common join patterns.

TABLES IN DATABASE (format: schema.table (column:type, ...) [primary_date=col] [primary_ts=col]):
{tables_summary}

DETECTED RELATIONSHIPS (formal foreign keys):
{relationships_summary}

Provide:
- business_metrics: Named KPIs with exact SQL definitions and format hints \
(format_hint: 'currency_eur'|'currency_usd'|'percentage'|'integer'|null). \
For each metric also provide: \
metric_type ('simple'|'derived'|'ratio'|'cumulative'|'conversion'), \
aggregation (sum/count/count_distinct/average/max/min — omit for derived/ratio/conversion), \
numerator_expr and denominator_expr (for ratio metrics only), \
base_measure and conversion_measure (for conversion metrics — funnel/event tables), \
entity (join column linking the two event measures, e.g. 'user_id'), \
window (optional time window for conversion, e.g. '7 days'), \
calculation ('conversion_rate' or 'conversions', default 'conversion_rate'), \
is_non_additive (true if this cannot be SUM-ed across dimensions), \
non_additive_dimension (which dimension makes it non-additive, if applicable). \
Use 'conversion' metric_type when two tables represent funnel steps \
(e.g. sessions → signups, visits → purchases).
- segments: Named, reusable WHERE-clause fragments for common filter patterns. \
Each has: name (snake_case), sql_expression, description, applicable_tables.
- derived_columns: Calculated fields (margins, rates, durations) with SQL expressions
- common_joins: Common join patterns between tables

Respond ONLY with valid JSON:
{{
  "business_metrics": [
    {{
      "name": "Total Revenue",
      "definition": "SUM(orders.total_amount)",
      "description": "Sum of all completed order amounts",
      "metric_type": "simple",
      "aggregation": "sum",
      "filters": ["orders.status = 'completed'"],
      "related_tables": ["orders"],
      "format_hint": "currency_usd"
    }},
    {{
      "name": "Conversion Rate",
      "definition": "COUNT(DISTINCT orders.customer_id) / NULLIF(COUNT(sessions.session_id), 0)",
      "description": "Percentage of sessions that result in an order",
      "metric_type": "ratio",
      "numerator_expr": "COUNT(DISTINCT orders.customer_id)",
      "denominator_expr": "COUNT(DISTINCT sessions.session_id)",
      "related_tables": ["orders", "sessions"],
      "format_hint": "percentage"
    }},
    {{
      "name": "Signup Conversion Rate",
      "description": "Fraction of sessions that convert to a signup within 7 days",
      "metric_type": "conversion",
      "base_measure": "sessions",
      "conversion_measure": "signups",
      "entity": "user_id",
      "window": "7 days",
      "calculation": "conversion_rate",
      "related_tables": ["sessions", "signups"],
      "format_hint": "percentage"
    }}
  ],
  "segments": [
    {{
      "name": "active_customers",
      "sql_expression": "status = 'active' AND deleted_at IS NULL",
      "description": "Customers who are active and not soft-deleted",
      "applicable_tables": ["schema.customers"]
    }}
  ],
  "derived_columns": [
    {{
      "name": "Gross Margin %",
      "sql_expression": "(base_price - cost_price) / NULLIF(base_price, 0) * 100",
      "base_tables": ["products"],
      "description": "Product gross margin as a percentage",
      "format_hint": "percentage"
    }}
  ],
  "common_joins": [
    {{
      "description": "Customer orders",
      "tables": ["customers", "orders"],
      "join_pattern": "customers.id = orders.customer_id"
    }}
  ]
}}"""

_RATIO_NUMERATOR_KEYWORDS: tuple[str, ...] = (
    "spent",
    "used",
    "actual",
    "consumed",
    "sold",
    "cost",
    "paid",
)
_RATIO_DENOMINATOR_KEYWORDS: tuple[str, ...] = (
    "budget",
    "limit",
    "target",
    "allocated",
    "capacity",
    "quota",
)

# Dialect-specific time expressions injected into the LLM prompt for temporal questions.
# PostgreSQL time expression syntax.
_PG_TIME_EXPRESSIONS: dict[str, str] = {
    "today": "CURRENT_DATE",
    "yesterday": "CURRENT_DATE - INTERVAL '1 day'",
    "this_week": "DATE_TRUNC('week', CURRENT_DATE)",
    "last_week": "DATE_TRUNC('week', CURRENT_DATE) - INTERVAL '1 week'",
    "this_month": "DATE_TRUNC('month', CURRENT_DATE)",
    "last_month": "DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '1 month'",
    "this_quarter": "DATE_TRUNC('quarter', CURRENT_DATE)",
    "last_quarter": "DATE_TRUNC('quarter', CURRENT_DATE) - INTERVAL '3 months'",
    "this_year": "DATE_TRUNC('year', CURRENT_DATE)",
    "last_year": "DATE_TRUNC('year', CURRENT_DATE) - INTERVAL '1 year'",
    "ytd": "DATE_TRUNC('year', CURRENT_DATE)",
    "rolling_7": "CURRENT_DATE - INTERVAL '7 days'",
    "rolling_30": "CURRENT_DATE - INTERVAL '30 days'",
    "rolling_90": "CURRENT_DATE - INTERVAL '90 days'",
    "rolling_12m": "CURRENT_DATE - INTERVAL '12 months'",
}

# Column names that are row-lifecycle audit fields, not business time axes.
_AUDIT_TIMESTAMP_NAMES: frozenset[str] = frozenset(
    {
        "created_at",
        "updated_at",
        "modified_at",
        "changed_at",
        "deleted_at",
        "inserted_at",
        "last_modified",
        "last_updated",
        "created_time",
        "updated_time",
    }
)

# Suffix/prefix patterns that identify integer fiscal/period dimension columns.
# Used by _infer_semantic_type (→ CATEGORICAL) and _infer_temporal_columns
# (→ suppress audit timestamps as primary_timestamp_column).
_FISCAL_DIMENSION_SUFFIXES: tuple[str, ...] = ("_year", "_quarter", "_period")
_FISCAL_DIMENSION_PREFIXES: tuple[str, ...] = ("fiscal_", "budget_", "calendar_", "tax_")


def _warn_near_miss(kind: str, name: str, target: str | None, where: str) -> None:
    """Warn that a dropped stored ``name`` was a near-miss spelling of live ``target``.

    No remap: a migrated row's user-edit marks are keyed by the old spelling, so a
    remap would keep the value but silently lose the marks, and the next regenerate
    would overwrite the edit anyway.
    """
    if target is None:
        return
    logger.warning(
        "prune_to_schema: near-miss %s key %r%s dropped; its annotations, incl. any hand "
        "edits, are not carried to %r",
        kind,
        name,
        where,
        target,
    )


def _prune_table(
    key: str,
    table: TableSemantic,
    real_columns: list[str],
    privacy: PrivacySettings | None,
    *,
    log: bool,
) -> TableSemantic:
    """``table`` bounded to ``real_columns`` (plus the ones ``privacy`` hides)."""
    live = set(real_columns)

    def kept(name: str) -> bool:
        # A column the policy hides is not gone: keep it, and references to it, as is.
        return name in live or policy_hides_column(key, name, privacy)

    columns = {name: col for name, col in table.columns.items() if kept(name)}
    if log:
        for name in table.columns:
            if not kept(name):
                target = SemanticModelGenerator._resolve_real_key(name, real_columns, bare_ok=False)
                _warn_near_miss("column", name, target, f" on {key}")

    # References are checked against the live columns, not the annotated ones: the
    # generator never backfills a column the LLM omitted, and a reference to a real
    # but unannotated column is not dangling.
    hierarchies = []
    for hierarchy in table.hierarchies:
        levels = [level for level in hierarchy.levels if kept(level)]
        if levels:
            hierarchies.append(hierarchy.model_copy(update={"levels": levels}))
    timestamp, date = table.primary_timestamp_column, table.primary_date_column
    return table.model_copy(
        update={
            "columns": columns,
            "primary_timestamp_column": timestamp if timestamp and kept(timestamp) else None,
            "primary_date_column": date if date and kept(date) else None,
            "partition_columns": [c for c in table.partition_columns if kept(c)],
            "cluster_columns": [c for c in table.cluster_columns if kept(c)],
            "hierarchies": hierarchies,
        }
    )


def prune_to_schema(
    tables: dict[str, TableSemantic],
    schema: DataSourceSchema,
    privacy: PrivacySettings | None = None,
    *,
    log: bool = True,
    keep_unreadable: bool = False,
) -> dict[str, TableSemantic]:
    """Bound a model's tables to what ``schema`` still has. Pure; never mutates ``tables``.

    A regenerate reconciles onto the stored rows and never blanks them, so
    ``/generate/globals`` and ``/apply-drift`` both call this to stop tables and
    columns that left the database from living in the model forever:

    - a table whose key (``schema.name``) is not in ``schema`` is dropped, and
      within a kept table every column that is not a real column of it;
    - references to a column the table no longer has are cleared:
      ``primary_timestamp_column`` / ``primary_date_column`` become ``None``,
      ``partition_columns`` / ``cluster_columns`` and each hierarchy's ``levels``
      are filtered, and a hierarchy left with no levels is dropped.

    **An absence the policy explains is not drift.** With ``privacy``, a table
    whose schema or table is excluded, and a column whose ``schema.table.column``
    is in ``excluded_columns``, is absent from ``schema`` because it is hidden,
    not because it is gone: it is carried through unchanged, references to it
    included.

    **So is one the role cannot read.** With ``keep_unreadable``, a table in
    ``schema.unreadable_tables`` (it exists, but the connection's role holds no
    privilege on it) is carried through unchanged too. Whatever persists the model
    passes it; a call that builds an LLM prompt leaves it off, since nothing over
    such a table could run.

    **Near-miss spellings are dropped, not remapped.** Rows backfilled from an old
    blob can carry an LLM spelling (``public.Orders``, ``"OrderID"``) that the
    generator later grounds to the real key. Such a row is dropped like any other
    absent key — a remap would keep its value but lose its user-edit marks, which
    are keyed by the old spelling — and, when ``log``, a warning names the key it
    would have matched (``_resolve_real_key``'s rules). ``log=False`` is for a
    second call over the same model, so the warnings are not duplicated.

    ``default_filters`` are free SQL and are NOT rewritten: a filter naming a
    dropped column stays as written.

    Membership is by schema, not by generation-run stamp, so finishing a partial
    run keeps every real table it annotated.
    """
    real_columns = {table_key(t): [c.name for c in t.columns] for t in schema.tables}
    real_keys = list(real_columns)
    unreadable = set(schema.unreadable_tables) if keep_unreadable else set()
    out: dict[str, TableSemantic] = {}
    for key, table in tables.items():
        if key in real_columns:
            out[key] = _prune_table(key, table, real_columns[key], privacy, log=log)
        elif policy_hides_table(key, privacy) or key in unreadable:
            out[key] = table.model_copy()
        elif log:
            target = SemanticModelGenerator._resolve_real_key(key, real_keys, bare_ok=True)
            _warn_near_miss("table", key, target, "")
    return out


def edges_in_schema(
    edges: Sequence[RelationshipEdge],
    schema: DataSourceSchema,
    privacy: PrivacySettings | None = None,
    *,
    keep_unreadable: bool = False,
) -> list[RelationshipEdge]:
    """The edges whose both endpoints are real columns of real tables in ``schema``.

    Bounded by the live schema, not by the annotated tables: a user-authored edge
    to a real but unannotated table must survive "Finish with what's generated".
    An endpoint ``privacy`` hides (its table, or its column) counts as live, and so,
    with ``keep_unreadable``, does one on a table the role cannot read
    (``prune_to_schema``'s rule).
    """
    live = {table_key(t): {c.name for c in t.columns} for t in schema.tables}
    unreadable = set(schema.unreadable_tables) if keep_unreadable else set()

    def alive(table: str, column: str) -> bool:
        if table in live:
            return column in live[table] or policy_hides_column(table, column, privacy)
        return policy_hides_table(table, privacy) or table in unreadable

    return [
        e for e in edges if alive(e.from_table, e.from_column) and alive(e.to_table, e.to_column)
    ]


def names_any_table(refs: Iterable[str], targets: set[str], real_keys: list[str]) -> bool:
    """Whether any of ``refs`` (an LLM item's table list) names one of ``targets``.

    Each ref is resolved against ``real_keys`` by ``_resolve_real_key``'s rules —
    quote-stripped, case-insensitive, or a bare table name that is unique among
    ``real_keys`` — so ``events`` counts for ``public.events`` only when no other
    real table is called ``events``. Pass every table the model can name as
    ``real_keys``, not just ``targets``, so a bare name is not claimed by the wrong one.
    """
    return any(
        SemanticModelGenerator._resolve_real_key(ref, real_keys, bare_ok=True) in targets
        for ref in refs
    )


_TABLE_REFERENCE_FIELDS = (
    "primary_timestamp_column",
    "primary_date_column",
    "partition_columns",
    "cluster_columns",
    "hierarchies",
)


def _unique(names: Iterable[str]) -> list[str]:
    """``names`` without repeats, in first-seen order."""
    return list(dict.fromkeys(names))


def drop_dangling_edits(
    marks: dict[str, dict[str, list[str]]],
    protected: dict[str, dict[str, dict[str, Any]]],
    schema: DataSourceSchema,
    privacy: PrivacySettings | None = None,
    *,
    log: bool = True,
) -> tuple[dict[str, dict[str, list[str]]], dict[str, dict[str, dict[str, Any]]], list[str]]:
    """``marks`` and ``protected`` with each table edit's references to gone columns dropped.

    A full save writes every protected value back over the rebuilt row — after
    ``prune_to_schema`` cleared the same references in the incoming model. A
    hand-set ``primary_timestamp_column`` on a column that left the database
    would therefore come back as a dangling reference, and its mark would stop
    every later regenerate from choosing another.

    The rule is ``_prune_table``'s, applied to the protected values:

    - ``primary_timestamp_column`` / ``primary_date_column`` naming a gone column
      is voided: the field leaves the table's marks and its protected values, so
      the pruned generated value wins.
    - ``partition_columns`` / ``cluster_columns`` lose only their gone entries.
      If any entry remains the mark stays and the filtered list is protected;
      if none does, the field is voided.
    - ``hierarchies``: each hierarchy keeps only its live levels, and one left
      with no levels is dropped. If any hierarchy remains the mark stays with
      the filtered list; if none does, the field is voided. A field is only
      rewritten when one of its names is gone.

    A column the policy hides is not gone (``policy_hides_column``, the rule
    ``prune_to_schema`` applies), and a table absent from ``schema`` is left
    alone: whether its row survives is ``prune_to_schema``'s decision. A table
    left with no marks or no protected values loses that entry.

    Also returns one human-readable note per table field changed, and logs it
    unless ``log=False`` (a dry run, which only previews the trim). Pure: the
    returned dicts and mark lists are new and every rewritten value is a new list
    (of new hierarchy dicts); an unchanged protected value is the input's object.
    """
    real_columns = {table_key(t): {c.name for c in t.columns} for t in schema.tables}
    out_marks = {kind: {k: list(v) for k, v in entries.items()} for kind, entries in marks.items()}
    out_protected = {
        kind: {k: dict(v) for k, v in entries.items()} for kind, entries in protected.items()
    }
    out_marks.setdefault("table", {})
    notes: list[str] = []
    for key, values in protected.get("table", {}).items():
        live = real_columns.get(key)
        if live is None:
            continue

        def kept(name: str, _key: str = key, _live: set[str] = live) -> bool:
            return name in _live or policy_hides_column(_key, name, privacy)

        for field in _TABLE_REFERENCE_FIELDS:
            value = values.get(field)
            if not value:
                continue
            remaining: list[Any]
            if field == "hierarchies":
                dropped = _unique(
                    level for h in value for level in h.get("levels") or [] if not kept(level)
                )
                filtered = (
                    {**h, "levels": [level for level in h.get("levels") or [] if kept(level)]}
                    for h in value
                )
                remaining = [h for h in filtered if h["levels"]]
            elif isinstance(value, list):
                dropped = _unique(c for c in value if not kept(c))
                remaining = [c for c in value if kept(c)]
            else:
                dropped = [] if kept(value) else [value]
                remaining = []
            if not dropped:
                continue
            names = ", ".join(dropped)
            if remaining:
                out_protected["table"][key][field] = remaining
                note = f"{key}.{field}: dropped {names} (no longer in the table)"
            else:
                out_protected["table"][key].pop(field, None)
                out_marks["table"][key] = [f for f in out_marks["table"].get(key, []) if f != field]
                verb = "is" if len(dropped) == 1 else "are"
                note = f"{key}.{field}: hand edit cleared — {names} {verb} no longer in the table"
            if log:
                logger.warning("Dangling hand edit: %s", note)
            notes.append(note)
        if not out_protected["table"][key]:
            del out_protected["table"][key]
        if key in out_marks["table"] and not out_marks["table"][key]:
            del out_marks["table"][key]
    return out_marks, out_protected, notes


class SemanticModelGenerator:
    """Generates an initial SemanticModel by asking an LLM to analyse the schema."""

    async def generate(
        self,
        schema: DataSourceSchema,
        provider: BaseLLMProvider,
        model: str | None = None,
        schema_dialect: str = "sql",
        datasource: BaseDataSource | None = None,
    ) -> SemanticModel:
        """
        Auto-generate a semantic model from *schema* using *provider*.

        Steps:
        1. Build a compact DDL representation of the schema.
        2. Build relationship summary from formal FK constraints.
        3. Fingerprint columns via catalog statistics (best-effort, requires datasource).
        4. Ask the LLM to return a JSON semantic model.
        5. Parse and validate the JSON response.
        6. Enrich with relationship edges, temporal columns, and time expressions.
        7. Return a populated :class:`SemanticModel`.

        :param schema_dialect: datasource ``source_type`` (e.g. ``'postgresql'``, ``'mysql'``)
            or the legacy ``'sql'`` sentinel.
        :param datasource: Connected datasource for catalog fingerprinting.
            Optional — fingerprinting is skipped when ``None``.
        """
        schema_ddl = self._schema_to_ddl(schema)

        if not schema_ddl.strip():
            logger.warning("SemanticModelGenerator: schema is empty, returning empty model")
            return SemanticModel()

        # Build relationship edges from FK catalog (zero DB overhead)
        relationship_edges = self._build_relationship_graph(schema)
        relationships_summary = self._format_relationships_for_prompt(relationship_edges)

        # Column fingerprinting (catalog-read; best-effort — failures are silently skipped).
        # Each call is capped at 10 s so a single slow column cannot stall the whole generation.
        column_stats: dict[str, dict] = {}
        if datasource is not None:
            for tbl in schema.tables:
                for col in tbl.columns:
                    key = f"{tbl.name}.{col.name}"
                    try:
                        stats = await asyncio.wait_for(
                            self._fingerprint_column(datasource, tbl.schema_name, tbl.name, col),
                            timeout=10.0,
                        )
                        if stats:
                            column_stats[key] = stats
                    except Exception:  # noqa: S110
                        pass

        # Always batch — the single-shot path was deleted in favour of a single
        # uniform code path. Even small schemas now batch (one batch when ≤ GENERATION_BATCH_SIZE).
        parsed = await self._generate_batched(
            schema=schema,
            provider=provider,
            model=model,
            column_stats=column_stats,
            relationships_summary=relationships_summary,
            relationship_edges=relationship_edges,
        )

        # Enrich with catalog-derived data (zero overhead — already in schema)
        parsed.relationships = relationship_edges

        # Append auto-detected ratio pairs not already covered by LLM-generated derived columns
        auto_pairs = self._detect_ratio_pairs(schema)
        existing_covered: set[tuple[str, str]] = set()
        for existing in parsed.derived_columns:
            expr = existing.sql_expression.lower()
            for num_kw in _RATIO_NUMERATOR_KEYWORDS:
                for den_kw in _RATIO_DENOMINATOR_KEYWORDS:
                    if num_kw in expr and den_kw in expr:
                        existing_covered.add((num_kw, den_kw))
        parsed.derived_columns.extend(
            dc for dc in auto_pairs if not self._is_already_covered(dc, existing_covered)
        )

        parsed.time_expressions = self.build_time_expressions(schema_dialect)
        parsed.schema_hash = self.compute_schema_hash(schema)
        parsed.source_dialect = schema_dialect

        # Auto-detect temporal columns per table (name/type matching — no DB calls)
        for table in schema.tables:
            tbl_key = table_key(table)
            if tbl_key in parsed.tables:
                temporal = self._infer_temporal_columns(tbl_key, table.columns)
                if temporal["primary_timestamp_column"]:
                    parsed.tables[tbl_key].primary_timestamp_column = temporal[
                        "primary_timestamp_column"
                    ]
                if temporal["primary_date_column"]:
                    parsed.tables[tbl_key].primary_date_column = temporal["primary_date_column"]

        # Backfill fingerprint-derived cardinality onto ColumnSemantic objects.
        # column_stats keys are "table_name.col_name" (no schema prefix).
        if column_stats:
            for table in schema.tables:
                tbl_key = table_key(table)
                if tbl_key not in parsed.tables:
                    continue
                for col in table.columns:
                    stat_key = f"{table.name}.{col.name}"
                    if stat_key not in column_stats:
                        continue
                    if col.name in parsed.tables[tbl_key].columns:
                        parsed.tables[tbl_key].columns[col.name].cardinality = column_stats[
                            stat_key
                        ]["cardinality"]

        # Populate partition_columns, cluster_columns, and base_sql from introspection metadata.
        # Also suggest COUNT_DISTINCT_APPROX for high-cardinality ID/FK columns.
        self._populate_schema_metadata(parsed.tables, schema)

        # Post-process: stamp audit timestamp descriptions, validate metric time columns,
        # and auto-generate cross-table domain notes for fiscal-period tables.
        self._enrich_fiscal_table_annotations(parsed.tables, schema)
        parsed.generation_warnings.extend(
            self._validate_metrics_time_columns(parsed.business_metrics, schema)
        )
        parsed.notes = self._auto_generate_notes(schema)

        return parsed

    # ── Batched generation (for low-output-token providers) ────────────────────

    async def _generate_batched(
        self,
        schema: DataSourceSchema,
        provider: BaseLLMProvider,
        model: str | None,
        column_stats: dict[str, dict],
        relationships_summary: str,
        relationship_edges: list[RelationshipEdge],
    ) -> SemanticModel:
        """
        Generate semantic model in multiple LLM calls when the provider cannot produce
        the full JSON in one shot (max_output_tokens < _BATCH_TOKEN_THRESHOLD).

        Strategy:
        1. Split schema.tables into batches of GENERATION_BATCH_SIZE.
        2. Call the LLM once per batch, returning only {"tables": {...}}.
        3. Merge all table results.
        4. Call the LLM once more for {"business_metrics", "derived_columns", "common_joins"}.
        5. Assemble a SemanticModel from the merged tables + global sections.
        """
        sys_prompt = (
            "You are a data modelling expert. You ALWAYS respond with valid JSON and nothing else."
        )
        gen_model = model or provider.provider_name

        # ── Step 1-3: one call per batch of tables ────────────────────────────
        all_tables: dict[str, TableSemantic] = {}
        tables = schema.tables
        batches = [
            tables[i : i + GENERATION_BATCH_SIZE]
            for i in range(0, len(tables), GENERATION_BATCH_SIZE)
        ]
        logger.debug(
            "SemanticModelGenerator batched: %d tables → %d batches",
            len(tables),
            len(batches),
        )

        for batch_idx, batch in enumerate(batches):
            # Build DDL for this batch only
            batch_schema_obj = DataSourceSchema(
                source_type=schema.source_type,
                schemas=schema.schemas,
                tables=list(batch),
                relationships=schema.relationships,
                metadata=schema.metadata,
            )
            batch_ddl = self._schema_to_ddl(batch_schema_obj)

            # Build column stats subset for this batch
            batch_table_names = {t.name for t in batch}
            stat_lines: list[str] = []
            for col_key, st in column_stats.items():
                # col_key = "table_name.col_name"
                tbl_name = col_key.split(".")[0] if "." in col_key else ""
                if tbl_name not in batch_table_names:
                    continue
                parts = [f"  {col_key}: cardinality={st.get('cardinality', '?')}"]
                if st.get("n_distinct") is not None:
                    parts.append(f"n_distinct={st['n_distinct']}")
                if st.get("common_vals"):
                    vals = ", ".join(str(v) for v in st["common_vals"][:5])
                    parts.append(f"sample_values=[{vals}]")
                stat_lines.append(", ".join(parts))
            batch_stats_summary = (
                "COLUMN STATISTICS (from catalog — no user data rows):\n" + "\n".join(stat_lines)
                if stat_lines
                else ""
            )

            prompt = _BATCH_TABLES_PROMPT.format(
                schema_ddl=batch_ddl,
                column_stats_summary=batch_stats_summary,
            )

            logger.debug(
                "SemanticModelGenerator batched: calling LLM for batch %d/%d (%d tables)",
                batch_idx + 1,
                len(batches),
                len(batch),
            )
            try:
                result = await provider.generate_structured(
                    system_prompt=sys_prompt,
                    user_message=prompt,
                    schema_type=TablesBatchResponse,
                    model=model,
                    temperature=0.0,
                    max_tokens=min(65536, provider.max_output_tokens),
                )
                all_tables.update(result.tables)
            except Exception as exc:
                logger.warning(
                    "SemanticModelGenerator batched: batch %d failed: %s — skipping",
                    batch_idx + 1,
                    exc,
                )

        # ── Step 4: one call for global sections ─────────────────────────────
        tables_summary = self._build_tables_summary(schema)

        global_prompt = _GLOBAL_SECTIONS_PROMPT.format(
            tables_summary=tables_summary,
            relationships_summary=relationships_summary or "(none detected)",
        )
        business_metrics: list[BusinessMetric] = []
        common_joins: list[CommonJoin] = []
        derived_columns: list[DerivedColumn] = []
        segments: list[Segment] = []
        try:
            global_result = await provider.generate_structured(
                system_prompt=sys_prompt,
                user_message=global_prompt,
                schema_type=GlobalSectionsResponse,
                model=model,
                temperature=0.0,
                max_tokens=min(65536, provider.max_output_tokens),
            )
            business_metrics = global_result.business_metrics
            common_joins = global_result.common_joins
            derived_columns = global_result.derived_columns
            segments = global_result.segments
        except Exception as exc:
            logger.warning(
                "SemanticModelGenerator batched: global sections call failed: %s — "
                "skipping metrics/joins",
                exc,
            )

        logger.info(
            "SemanticModelGenerator batched: completed — %d/%d tables, %d metrics, %d joins",
            len(all_tables),
            len(tables),
            len(business_metrics),
            len(common_joins),
        )

        # Prune to only include edges and derived columns that reference tables
        # actually present in the model — the FK catalog covers all DB schemas
        # but only a subset may have been generated.
        generated_tables = set(all_tables.keys())
        pruned_relationships = [
            e
            for e in relationship_edges
            if e.from_table in generated_tables and e.to_table in generated_tables
        ]
        pruned_derived = [
            d for d in derived_columns if all(t in generated_tables for t in d.base_tables)
        ]
        if len(pruned_relationships) < len(relationship_edges):
            logger.info(
                "SemanticModelGenerator: pruned %d dangling relationships (tables not in model)",
                len(relationship_edges) - len(pruned_relationships),
            )
        if len(pruned_derived) < len(derived_columns):
            logger.info(
                "SemanticModelGenerator: pruned %d derived columns with missing base_tables",
                len(derived_columns) - len(pruned_derived),
            )

        result = SemanticModel(
            tables=all_tables,
            business_metrics=business_metrics,
            common_joins=common_joins,
            derived_columns=pruned_derived,
            relationships=pruned_relationships,
            segments=segments,
            generated_at=datetime.now(UTC).isoformat(),
            is_user_reviewed=False,
            generation_model=gen_model,
        )
        self._enrich_fiscal_table_annotations(result.tables, schema)
        result.generation_warnings.extend(
            self._validate_metrics_time_columns(result.business_metrics, schema)
        )
        result.notes = self._auto_generate_notes(schema)
        return result

    # ── Phase methods (called independently by phased generation endpoints) ────

    async def generate_tables(
        self,
        schema: DataSourceSchema,
        provider: BaseLLMProvider,
        model: str | None,
        table_keys: Sequence[str],
        relationship_edges: list[RelationshipEdge],
    ) -> dict[str, TableSemantic]:
        """Generate semantic annotations for the named tables (no adapter required).

        Keys not present in ``schema`` are ignored; an empty resolved set makes no
        LLM call. Returns ``schema.table → TableSemantic`` for the resolved tables.
        Near-miss table/column names are remapped to the batch's real keys and invented
        ones are dropped; columns the LLM omits are left absent (so the store keeps any
        earlier annotation for them). Raises ``ValueError`` on LLM failure so the router
        can map it to HTTP.
        """
        wanted = set(table_keys)
        batch = [t for t in schema.tables if table_key(t) in wanted]
        if not batch:
            return {}

        batch_schema_obj = DataSourceSchema(
            source_type=schema.source_type,
            schemas=schema.schemas,
            tables=list(batch),
            relationships=schema.relationships,
            metadata=schema.metadata,
        )
        batch_ddl = self._schema_to_ddl(batch_schema_obj)

        # Filter FK edges to only those relevant to this batch
        batch_table_keys = {table_key(t) for t in batch}
        batch_edges = [
            e
            for e in relationship_edges
            if e.from_table in batch_table_keys or e.to_table in batch_table_keys
        ]
        relationships_summary = self._format_relationships_for_prompt(batch_edges)

        sys_prompt = (
            "You are a data modelling expert. You ALWAYS respond with valid JSON and nothing else."
        )
        prompt = _BATCH_TABLES_PROMPT.format(
            schema_ddl=batch_ddl,
            column_stats_summary="",
        )
        if relationships_summary:
            prompt = (
                f"DETECTED RELATIONSHIPS (formal foreign keys):\n{relationships_summary}\n\n"
                + prompt
            )

        try:
            result = await provider.generate_structured(
                system_prompt=sys_prompt,
                user_message=prompt,
                schema_type=TablesBatchResponse,
                model=model,
                temperature=0.0,
                max_tokens=min(65536, provider.max_output_tokens),
            )
        except Exception as e:
            raise ValueError(f"LLM call failed in generate_tables: {e}") from e
        # Ground first so fiscal enrichment sees the remapped (real) table keys.
        self._ground_tables_to_schema(result.tables, batch_schema_obj)
        self._enrich_fiscal_table_annotations(result.tables, schema)
        return result.tables

    async def generate_new_columns(
        self,
        schema: DataSourceSchema,
        provider: BaseLLMProvider,
        model: str | None,
        columns_by_table: Mapping[str, Sequence[str]],
        table_context: Mapping[str, TableSemantic],
        relationship_edges: list[RelationshipEdge],
    ) -> dict[str, dict[str, ColumnSemantic]]:
        """Annotate only the named new columns of tables the model already holds.

        ``columns_by_table`` maps a table key to the column names to annotate;
        ``table_context`` carries each table's stored annotation (its description is shown
        to the LLM after the new column names, collapsed to one line). Tables missing from
        ``schema`` and names the table does not have are dropped before the call; if nothing
        is left no LLM call is made. The prompt shows the whole table as context but asks
        only for the named columns, and the result is grounded to the schema (near-miss
        names remapped, invented ones dropped), given the fiscal audit-timestamp rule
        ``generate_tables`` applies, and filtered to the requested names, so a column the
        LLM re-describes is ignored. The caller packs a batch so its DDL fits
        ``_MAX_DDL_CHARS`` (``table_ddl_sizes``).

        Returns ``table key → {column name → ColumnSemantic}``, holding only the tables
        with at least one requested column annotated: a table the LLM answered nothing
        usable for is absent. Raises ``ValueError`` on LLM failure; its message keeps the
        provider's, error mark included, so the router can tell a throttle or an outage
        from any other failure.
        """
        batch = [t for t in schema.tables if table_key(t) in columns_by_table]
        live = {table_key(t): {c.name for c in t.columns} for t in batch}
        wanted: dict[str, set[str]] = {}
        for key, cols in columns_by_table.items():
            if names := set(cols) & live.get(key, set()):
                wanted[key] = names
        if not wanted:
            return {}
        batch = [t for t in batch if table_key(t) in wanted]

        batch_schema_obj = DataSourceSchema(
            source_type=schema.source_type,
            schemas=schema.schemas,
            tables=batch,
            relationships=schema.relationships,
            metadata=schema.metadata,
        )
        targets: list[str] = []
        for key in sorted(wanted):
            line = f"- {key}: {', '.join(sorted(wanted[key]))}"
            # A stored description is user-editable and may hold newlines, colons or
            # parentheses: collapsed to one line after the columns, it cannot break the
            # list or be read as a column name.
            context = table_context.get(key)
            if context and (about := " ".join((context.description or "").split())):
                line += f" (table: {about})"
            targets.append(line)

        # Filter FK edges to only those relevant to this batch
        batch_edges = [
            e for e in relationship_edges if e.from_table in wanted or e.to_table in wanted
        ]
        relationships_summary = self._format_relationships_for_prompt(batch_edges)

        sys_prompt = (
            "You are a data modelling expert. You ALWAYS respond with valid JSON and nothing else."
        )
        prompt = _NEW_COLUMNS_PROMPT.format(
            schema_ddl=self._schema_to_ddl(batch_schema_obj),
            targets="\n".join(targets),
        )
        if relationships_summary:
            prompt = (
                f"DETECTED RELATIONSHIPS (formal foreign keys):\n{relationships_summary}\n\n"
                + prompt
            )

        try:
            result = await provider.generate_structured(
                system_prompt=sys_prompt,
                user_message=prompt,
                schema_type=ColumnsBatchResponse,
                model=model,
                temperature=0.0,
                max_tokens=min(65536, provider.max_output_tokens),
            )
        except Exception as e:
            raise ValueError(f"LLM call failed in generate_new_columns: {e}") from e

        # Grounding and the fiscal rule work on tables: wrap each reply's columns in a
        # throwaway table. Ground first so fiscal enrichment sees the remapped keys.
        replies = {
            key: TableSemantic(display_name=key, columns=entry.columns)
            for key, entry in result.tables.items()
        }
        self._ground_tables_to_schema(replies, batch_schema_obj)
        # The rule ``generate_tables`` applies, so a new ``created_at`` on a fiscal-period
        # table is not described as a business date.
        self._enrich_fiscal_table_annotations(replies, schema)
        out: dict[str, dict[str, ColumnSemantic]] = {}
        for key, names in wanted.items():
            reply = replies.get(key)
            if reply is None:
                continue
            if kept := {name: col for name, col in reply.columns.items() if name in names}:
                out[key] = kept
        return out

    @staticmethod
    def _strip_quotes(name: str) -> str:
        """``drift.strip_ident_quotes`` — the one identifier normaliser; kept under this
        name for its callers."""
        return strip_ident_quotes(name)

    @staticmethod
    def _resolve_real_key(name: str, real_keys: list[str], *, bare_ok: bool) -> str | None:
        """``drift.resolve_real_key`` — the one near-miss resolver; kept under this name
        for its callers."""
        return resolve_real_key(name, real_keys, bare_ok=bare_ok)

    def _ground_tables_to_schema(
        self,
        tables: dict[str, TableSemantic],
        schema: DataSourceSchema,
    ) -> tuple[int, int]:
        """Keep the generated model grounded in the introspected schema, in place.

        The store persists whatever it is given, so an invented table or column would
        be stored permanently. Near-miss names are mapped back before anything is
        dropped (a dropped table is never stamped, so every resume would re-pay for it):

        - a table key not in ``schema`` is remapped to the one real key it matches by
          quote-stripping, then case-insensitively, then as a bare table name (see
          ``_resolve_real_key``) — provided the match is unique and that real key is not
          already in the output; otherwise it is dropped as invented;
        - within each real table, a column key not in the real column set is remapped the
          same way (quote-strip, then case-insensitive), else dropped.

        Real columns the LLM omitted are deliberately NOT backfilled. The store's
        ``reconcile_tables`` overwrites every unedited field of each column a batch
        names, so a bare-default column for one the LLM merely skipped would wipe the
        description, semantic type and value mappings from the previous run (plus bump
        the version and write a history row). An absent column is left untouched.

        Returns ``(dropped_tables, dropped_columns)``.
        """
        real_cols_by_key = {table_key(t): [c.name for c in t.columns] for t in schema.tables}
        real_keys = list(real_cols_by_key)
        dropped_tables = 0
        dropped_columns = 0

        # Real keys already present win over any near-miss that resolves to them.
        for tkey in [k for k in tables if k not in real_cols_by_key]:
            entry = tables.pop(tkey)
            target = self._resolve_real_key(tkey, real_keys, bare_ok=True)
            if target is None or target in tables:
                dropped_tables += 1
                continue
            tables[target] = entry
            logger.info("SemanticModelGenerator: remapped table key %r to %r", tkey, target)

        for tkey, real_cols in real_cols_by_key.items():
            sem_table = tables.get(tkey)
            if sem_table is None:
                continue
            columns = sem_table.columns
            for col_name in [c for c in columns if c not in real_cols]:
                col = columns.pop(col_name)
                target = self._resolve_real_key(col_name, real_cols, bare_ok=False)
                if target is None or target in columns:
                    dropped_columns += 1
                    continue
                columns[target] = col
                logger.info(
                    "SemanticModelGenerator: remapped column %r to %r on %s",
                    col_name,
                    target,
                    tkey,
                )

        if dropped_tables or dropped_columns:
            logger.info(
                "SemanticModelGenerator: grounded to schema — dropped %d invented "
                "table(s), %d invented column(s)",
                dropped_tables,
                dropped_columns,
            )
        return dropped_tables, dropped_columns

    async def generate_globals(
        self,
        schema: DataSourceSchema,
        provider: BaseLLMProvider,
        model: str | None,
        all_tables: dict[str, TableSemantic],
        relationship_edges: list[RelationshipEdge],
    ) -> tuple[
        list[BusinessMetric],
        list[CommonJoin],
        list[DerivedColumn],
        list[str],
        list[Segment],
    ]:
        """
        Generate cross-table business metrics, common joins, derived columns, and segments.

        Returns ``(metrics, joins, derived_columns, generation_warnings, segments)``.
        Derived columns also get the auto-detected ratio-pair supplement from
        ``_detect_ratio_pairs``. ``generation_warnings`` contains any metric
        time-column validation issues detected post-generation.

        Raises ``ValueError`` on LLM failure so the router can map it to HTTP.
        """
        tables_summary = self._build_tables_summary(schema, all_tables=all_tables)

        # Prune edges to generated tables only
        generated_tables = set(all_tables.keys())
        pruned_edges = [
            e
            for e in relationship_edges
            if e.from_table in generated_tables and e.to_table in generated_tables
        ]
        relationships_summary = self._format_relationships_for_prompt(pruned_edges)

        sys_prompt = (
            "You are a data modelling expert. You ALWAYS respond with valid JSON and nothing else."
        )
        global_prompt = _GLOBAL_SECTIONS_PROMPT.format(
            tables_summary=tables_summary,
            relationships_summary=relationships_summary or "(none detected)",
        )

        try:
            global_result = await provider.generate_structured(
                system_prompt=sys_prompt,
                user_message=global_prompt,
                schema_type=GlobalSectionsResponse,
                model=model,
                temperature=0.0,
                max_tokens=min(65536, provider.max_output_tokens),
            )
            metrics: list[BusinessMetric] = global_result.business_metrics
            joins: list[CommonJoin] = global_result.common_joins
            derived_columns: list[DerivedColumn] = global_result.derived_columns
            segments: list[Segment] = global_result.segments
        except Exception as exc:
            # Same contract as generate_tables: the route maps a throttle to 429 (so the
            # page retries) and anything else to 422, and the run stays ``tables_partial``
            # with Finish available — rather than completing with no metrics and no word.
            raise ValueError(f"LLM call failed in generate_globals: {exc}") from exc

        # Supplement with auto-detected ratio pairs
        auto_pairs = self._detect_ratio_pairs(schema)
        existing_covered: set[tuple[str, str]] = set()
        for existing in derived_columns:
            expr = existing.sql_expression.lower()
            for num_kw in _RATIO_NUMERATOR_KEYWORDS:
                for den_kw in _RATIO_DENOMINATOR_KEYWORDS:
                    if num_kw in expr and den_kw in expr:
                        existing_covered.add((num_kw, den_kw))
        derived_columns.extend(
            dc for dc in auto_pairs if not self._is_already_covered(dc, existing_covered)
        )

        # Prune derived columns to tables that were actually generated
        derived_columns = [
            d for d in derived_columns if all(t in generated_tables for t in d.base_tables)
        ]

        validation_warnings = self._validate_metrics_time_columns(metrics, schema)
        return metrics, joins, derived_columns, validation_warnings, segments

    # ── Relationship graph ─────────────────────────────────────────────────────

    # OVERHEAD: catalog-read — reads from DataSourceSchema.relationships
    #           which was already populated during introspect() from information_schema
    def _build_relationship_graph(self, schema: DataSourceSchema) -> list[RelationshipEdge]:
        """
        Build relationship edges from formal FK constraints already in the schema.
        No additional database queries. Zero overhead.
        """
        nullable_map: dict[str, dict[str, bool]] = {
            table_key(t): {c.name: c.nullable for c in t.columns} for t in schema.tables
        }
        edges: list[RelationshipEdge] = []
        for rel in schema.relationships:
            from_table = f"{rel.from_schema}.{rel.from_table}"
            to_table = f"{rel.to_schema}.{rel.to_table}"
            nullable = nullable_map.get(from_table, {}).get(rel.from_column, True)
            is_required = not nullable
            if is_required:
                description = (
                    f"Required FK — every {from_table} row has a matching {to_table} row; "
                    f"INNER JOIN is safe in either direction"
                )
            else:
                description = (
                    f"Optional FK — {from_table}.{rel.from_column} may be NULL; "
                    f"use LEFT JOIN in both directions"
                )
            edges.append(
                RelationshipEdge(
                    from_table=from_table,
                    from_column=rel.from_column,
                    to_table=to_table,
                    to_column=rel.to_column,
                    relationship_type=RelationshipType.MANY_TO_ONE,
                    join_sql=(f"{from_table}.{rel.from_column} = {to_table}.{rel.to_column}"),
                    is_required=is_required,
                    description=description,
                    entity_type=EntityType.FOREIGN,
                )
            )
        return edges

    @staticmethod
    def _format_relationships_for_prompt(edges: list[RelationshipEdge]) -> str:
        if not edges:
            return ""
        lines = []
        for e in edges:
            lines.append(
                f"  {e.from_table}.{e.from_column} → {e.to_table}.{e.to_column}"
                f" (JOIN: {e.join_sql})"
            )
        return "\n".join(lines)

    # ── Column fingerprinting ──────────────────────────────────────────────────

    # OVERHEAD: catalog-read — uses pg_stats populated by autovacuum
    async def _fingerprint_column(
        self,
        datasource: BaseDataSource,
        schema: str,
        table: str,
        column: ColumnInfo,
    ) -> dict:
        """
        Derive semantic type and cardinality from pg_stats and column metadata.
        Never scans user data tables.
        """
        stats: dict = {}
        if hasattr(datasource, "get_column_statistics"):
            stats = await datasource.get_column_statistics(schema, table, column.name)

        n_distinct = stats.get("n_distinct", 0)
        null_frac = stats.get("null_frac", 0.0)
        common_vals = stats.get("most_common_vals", [])

        cardinality = self._classify_cardinality(n_distinct)
        semantic_type = self._infer_semantic_type(
            column.name,
            column.data_type,
            n_distinct,
            common_vals,
            column.is_primary_key,
            null_frac,
        )

        return {
            "semantic_type": semantic_type,
            "cardinality": cardinality,
            "n_distinct": n_distinct,
            "common_vals": common_vals,
        }

    def _classify_cardinality(self, n_distinct: float) -> CardinalityClass:
        if n_distinct == -1:
            return CardinalityClass.UNIQUE
        if n_distinct < 0:
            fraction = abs(n_distinct)
            if fraction > 0.9:
                return CardinalityClass.UNIQUE
            if fraction > 0.3:
                return CardinalityClass.HIGH
            if fraction > 0.05:
                return CardinalityClass.MEDIUM
            return CardinalityClass.LOW
        if n_distinct > 1000:
            return CardinalityClass.HIGH
        if n_distinct > 50:
            return CardinalityClass.MEDIUM
        return CardinalityClass.LOW

    def _infer_semantic_type(
        self,
        name: str,
        data_type: str,
        n_distinct: float,
        common_vals: list,
        is_pk: bool,
        null_frac: float,
    ) -> SemanticType:
        """
        Pure local inference — no database calls.
        Priority: PK check → name patterns → type patterns → cardinality.
        """
        name_lower = name.lower()

        if is_pk:
            return SemanticType.IDENTIFIER

        if any(p in name_lower for p in ("email", "e_mail")):
            return SemanticType.EMAIL
        if any(p in name_lower for p in ("phone", "mobile", "tel")):
            return SemanticType.PHONE
        if any(p in name_lower for p in ("url", "link", "href", "website")):
            return SemanticType.URL
        if any(p in name_lower for p in ("password", "passwd", "secret", "token")):
            return SemanticType.IDENTIFIER
        if name_lower.endswith("_id") or name_lower.endswith("_fk"):
            return SemanticType.FOREIGN_KEY
        if any(p in name_lower for p in ("rate", "ratio", "pct", "percent", "fraction")):
            return SemanticType.PERCENTAGE
        if any(
            p in name_lower
            for p in (
                "amount",
                "price",
                "revenue",
                "cost",
                "salary",
                "_total",
                "subtotal",
                "tax",
                "discount",
                "wage",
                "fee",
            )
        ):
            return SemanticType.MONETARY
        if any(
            p in name_lower
            for p in (
                "status",
                "state",
                "type",
                "kind",
                "category",
                "level",
                "tier",
                "stage",
                "phase",
                "mode",
            )
        ):
            return SemanticType.STATUS_FLAG
        if any(
            p in name_lower for p in ("count", "qty", "quantity", "num_", "number_of", "total_")
        ):
            return SemanticType.COUNT
        if any(
            p in name_lower
            for p in (
                "weight",
                "height",
                "width",
                "length",
                "size",
                "distance",
                "speed",
                "duration",
                "age",
            )
        ):
            return SemanticType.MEASUREMENT

        # Fiscal/period integer dimensions — always categorical regardless of cardinality
        if name_lower.endswith(_FISCAL_DIMENSION_SUFFIXES) or name_lower.startswith(
            _FISCAL_DIMENSION_PREFIXES
        ):
            return SemanticType.CATEGORICAL

        if data_type in (
            "timestamp",
            "timestamptz",
            "datetime",
            "timestamp with time zone",
            "timestamp without time zone",
        ):
            return SemanticType.TIMESTAMP
        if data_type == "date":
            return SemanticType.DATE
        if data_type in ("boolean", "bool"):
            return SemanticType.BOOLEAN_FLAG
        if data_type in ("text", "varchar") and n_distinct == -1:
            return SemanticType.FREE_TEXT

        if 0 < n_distinct < 20:
            return SemanticType.CATEGORICAL

        return SemanticType.UNKNOWN

    # ── Temporal column detection ──────────────────────────────────────────────

    # OVERHEAD: app-only — pure name/type pattern matching
    def _infer_temporal_columns(
        self,
        tbl_key: str,
        columns: list[ColumnInfo],
    ) -> dict:
        """
        Identify primary timestamp and date columns from column names and types.
        No database queries.
        """
        timestamp_priority = [
            "created_at",
            "ordered_at",
            "placed_at",
            "submitted_at",
            "occurred_at",
            "event_at",
            "timestamp",
            "created_time",
            "updated_at",
            "modified_at",
            "changed_at",
            "deleted_at",
            "closed_at",
            "resolved_at",
        ]
        date_priority = [
            "order_date",
            "invoice_date",
            "due_date",
            "ship_date",
            "hire_date",
            "birth_date",
            "start_date",
            "end_date",
            "effective_date",
            "expiry_date",
            "report_date",
        ]

        timestamp_candidates: list[tuple[int, str]] = []
        date_candidates: list[tuple[int, str]] = []

        for col in columns:
            col_lower = col.name.lower()
            if col.data_type in (
                "timestamp",
                "timestamptz",
                "timestamp with time zone",
                "datetime",
                "timestamp without time zone",
            ):
                priority = next(
                    (i for i, p in enumerate(timestamp_priority) if p in col_lower), 999
                )
                timestamp_candidates.append((priority, col.name))
            elif col.data_type == "date":
                priority = next((i for i, p in enumerate(date_priority) if p in col_lower), 999)
                date_candidates.append((priority, col.name))

        # If the table has fiscal dimension columns (fiscal_year, fiscal_quarter, etc.)
        # then audit timestamps (created_at, updated_at, ...) are row lifecycle metadata,
        # not business time axes. Strip them so the table correctly reports no primary
        # timestamp and the formatter doesn't emit a misleading "Primary timestamp" hint.
        col_names_lower = {c.name.lower() for c in columns}
        has_fiscal_dims = any(
            n.endswith(_FISCAL_DIMENSION_SUFFIXES) or n.startswith(_FISCAL_DIMENSION_PREFIXES)
            for n in col_names_lower
        )
        if has_fiscal_dims:
            timestamp_candidates = [
                (pri, name)
                for pri, name in timestamp_candidates
                if name.lower() not in _AUDIT_TIMESTAMP_NAMES
            ]

        primary_ts = sorted(timestamp_candidates)[0][1] if timestamp_candidates else None
        primary_dt = sorted(date_candidates)[0][1] if date_candidates else None

        return {
            "primary_timestamp_column": primary_ts,
            "primary_date_column": primary_dt,
        }

    # ── Fiscal-period post-processing ──────────────────────────────────────────

    @staticmethod
    def _fiscal_table_keys(schema: DataSourceSchema) -> set[str]:
        """Return the schema.table keys of tables that have fiscal dimension columns.

        Requires at least two matching columns (e.g. fiscal_year + fiscal_quarter)
        so that tables with a single incidental column like order_year or trial_period
        are not misclassified as fiscal-period tables.
        """
        keys: set[str] = set()
        for tbl in schema.tables:
            col_names_lower = [c.name.lower() for c in tbl.columns]
            fiscal_col_count = sum(
                1
                for n in col_names_lower
                if n.endswith(_FISCAL_DIMENSION_SUFFIXES)
                or n.startswith(_FISCAL_DIMENSION_PREFIXES)
            )
            if fiscal_col_count >= 2:
                keys.add(table_key(tbl))
        return keys

    def _enrich_fiscal_table_annotations(
        self,
        tables: dict[str, TableSemantic],
        schema: DataSourceSchema,
    ) -> None:
        """Override audit timestamp descriptions on fiscal-period tables in-place.

        When a table's time axis is fiscal_year + fiscal_quarter (or similar integer
        dimensions), its created_at/updated_at columns are row lifecycle metadata.
        We set an explicit description so the query generator sees a clear warning
        rather than relying on whatever the LLM produced.
        """
        fiscal_keys = self._fiscal_table_keys(schema)
        for tbl in schema.tables:
            tbl_key = table_key(tbl)
            if tbl_key not in fiscal_keys or tbl_key not in tables:
                continue
            fiscal_cols = [
                c.name
                for c in tbl.columns
                if c.name.lower().endswith(_FISCAL_DIMENSION_SUFFIXES)
                or c.name.lower().startswith(_FISCAL_DIMENSION_PREFIXES)
            ]
            sem_table = tables[tbl_key]
            for col_name, col_sem in sem_table.columns.items():
                if col_name.lower() not in _AUDIT_TIMESTAMP_NAMES:
                    continue
                col_sem.description = (
                    f"Row insert timestamp only — NOT a business period date. "
                    f"Use {', '.join(fiscal_cols)} for time-based filtering."
                )
                col_sem.semantic_type = SemanticType.TIMESTAMP

    def _validate_metrics_time_columns(
        self,
        metrics: list[BusinessMetric],
        schema: DataSourceSchema,
    ) -> list[str]:
        """Scan metrics for time-series SQL patterns applied to fiscal-period tables.

        Returns generation_warning strings for any metric whose definition or filters
        reference an audit timestamp column on a table whose time axis is
        fiscal_year/fiscal_quarter rather than a date column.
        """
        fiscal_keys = self._fiscal_table_keys(schema)
        if not fiscal_keys:
            return []

        time_patterns = ("created_at", "date_trunc", "interval", "current_date", "lag(")
        warnings: list[str] = []
        for metric in metrics:
            definition = getattr(metric, "definition", "")
            combined = (definition + " " + " ".join(metric.filters)).lower()
            if not any(p in combined for p in time_patterns):
                continue
            for related in metric.related_tables:
                # Accept both qualified ("finance.budgets") and bare ("budgets") references
                bare = related.rpartition(".")[2]
                matched = next(
                    (k for k in fiscal_keys if k == related or k.endswith(f".{bare}")),
                    None,
                )
                if matched:
                    warnings.append(
                        f'Metric "{metric.name}" uses a time-series pattern on '
                        f'"{matched}" which is a fiscal-period table — '
                        f"verify it filters by fiscal_year/fiscal_quarter, "
                        f"not a timestamp column."
                    )
                    break
        return warnings

    def _auto_generate_notes(self, schema: DataSourceSchema) -> list[str]:
        """Produce cross-table guidance notes for fiscal-period tables.

        Purely structural — no LLM, no DB queries. For each table whose time axis
        is integer fiscal dimensions (fiscal_year, fiscal_quarter, …) rather than
        a date column, emits one note that:
          - names the correct filter dimensions
          - warns against using audit timestamps
          - points to any companion table that shares an FK target with the fiscal
            table AND has a real DATE column (e.g. finance.transactions)
        """
        fiscal_keys = self._fiscal_table_keys(schema)
        if not fiscal_keys:
            return []

        # FK targets per table: table_key → set of target table_keys
        fk_targets: dict[str, set[str]] = {table_key(t): set() for t in schema.tables}
        for rel in schema.relationships:
            from_key = f"{rel.from_schema}.{rel.from_table}"
            to_key = f"{rel.to_schema}.{rel.to_table}"
            if from_key in fk_targets:
                fk_targets[from_key].add(to_key)

        # DATE columns per table (excludes TIMESTAMP — those are not period axes)
        date_col_map: dict[str, list[str]] = {
            table_key(t): [c.name for c in t.columns if c.data_type == "date"]
            for t in schema.tables
        }

        notes: list[str] = []
        for tbl in schema.tables:
            tbl_key = table_key(tbl)
            if tbl_key not in fiscal_keys:
                continue

            fiscal_dims = [
                c.name
                for c in tbl.columns
                if c.name.lower().endswith(_FISCAL_DIMENSION_SUFFIXES)
                or c.name.lower().startswith(_FISCAL_DIMENSION_PREFIXES)
            ]
            audit_cols = [c.name for c in tbl.columns if c.name.lower() in _AUDIT_TIMESTAMP_NAMES]

            # Find companion tables: share ≥1 FK target with this fiscal table
            # AND have at least one DATE column AND are not themselves fiscal tables
            my_targets = fk_targets.get(tbl_key, set())
            companions: list[str] = []
            for other_key, other_date_cols in date_col_map.items():
                if other_key == tbl_key or other_key in fiscal_keys:
                    continue
                if not other_date_cols:
                    continue
                if my_targets & fk_targets.get(other_key, set()):
                    companions.append(f"{other_key} ({', '.join(other_date_cols[:2])})")

            note = (
                f"{tbl_key} is a period table — filter by "
                f"{', '.join(fiscal_dims)}, not by timestamp columns"
            )
            if audit_cols:
                note += f" ({', '.join(audit_cols)} are row insert timestamps, not period dates)"
            note += "."
            if companions:
                note += f" For time-series analysis use: {'; '.join(companions)}."
            notes.append(note)

        return notes

    # ── Schema metadata enrichment ─────────────────────────────────────────────

    def _populate_schema_metadata(
        self,
        tables: dict[str, TableSemantic],
        schema: DataSourceSchema,
    ) -> None:
        """Enrich TableSemantic objects with data derivable from introspection metadata.

        - partition_columns: columns where ColumnInfo.is_partition_key is True
        - base_sql: view definition when adapter stored it in TableInfo.metadata
        - COUNT_DISTINCT_APPROX: suggested for high-cardinality IDENTIFIER/FOREIGN_KEY columns
        """
        _high_card = (CardinalityClass.HIGH, CardinalityClass.UNIQUE)
        _id_types = (SemanticType.IDENTIFIER, SemanticType.FOREIGN_KEY)

        for tbl in schema.tables:
            tbl_key = table_key(tbl)
            if tbl_key not in tables:
                continue
            sem = tables[tbl_key]

            # Partition columns — sourced from adapter introspect() where supported.
            partition_cols = [c.name for c in tbl.columns if c.is_partition_key]
            if partition_cols:
                sem.partition_columns = partition_cols

            # View definition — adapters that support it store it in TableInfo.metadata
            if tbl.table_type in ("view", "materialized_view") and tbl.metadata:
                view_def = tbl.metadata.get("view_definition")
                if view_def and isinstance(view_def, str):
                    sem.base_sql = view_def[:2000]  # cap to avoid bloating prompt

            # Suggest COUNT_DISTINCT_APPROX for high-cardinality ID/FK columns that
            # the LLM left without an aggregation hint.
            # Also set time_granularity=DAY for columns the adapter typed as "date".
            col_type_map = {c.name: c.data_type for c in tbl.columns}
            for col_name, col_sem in sem.columns.items():
                if (
                    col_sem.semantic_type in _id_types
                    and col_sem.cardinality in _high_card
                    and col_sem.default_aggregation is None
                ):
                    col_sem.default_aggregation = AggregationType.COUNT_DISTINCT_APPROX
                if col_sem.time_granularity is None and col_type_map.get(col_name) == "date":
                    col_sem.time_granularity = TimeGranularity.DAY

    # ── Ratio pair detection ────────────────────────────────────────────────────

    def _detect_ratio_pairs(self, schema: DataSourceSchema) -> list[DerivedColumn]:
        """Scan tables for (numerator, denominator) column pairs and return DerivedColumn hints."""
        results: list[DerivedColumn] = []
        for table in schema.tables:
            tbl_key = table_key(table)
            col_names = [c.name for c in table.columns]
            num_cols = [
                n for n in col_names if any(kw in n.lower() for kw in _RATIO_NUMERATOR_KEYWORDS)
            ]
            den_cols = [
                n for n in col_names if any(kw in n.lower() for kw in _RATIO_DENOMINATOR_KEYWORDS)
            ]
            for num_col in num_cols:
                for den_col in den_cols:
                    if num_col == den_col:
                        continue
                    results.append(
                        DerivedColumn(
                            name=f"{table.name} {num_col} utilization",
                            sql_expression=(
                                f"ROUND({tbl_key}.{num_col}"
                                f" / NULLIF({tbl_key}.{den_col}, 0) * 100, 2)"
                            ),
                            base_tables=[tbl_key],
                            description=f"Percentage of {den_col} consumed by {num_col}",
                            format_hint="percentage",
                        )
                    )
        return results

    @staticmethod
    def _is_already_covered(dc: DerivedColumn, covered: set[tuple[str, str]]) -> bool:
        """Return True if any (num_kw, den_kw) pair from covered appears in dc's expression."""
        expr = dc.sql_expression.lower()
        return any(num_kw in expr and den_kw in expr for num_kw, den_kw in covered)

    # ── Drift detection ────────────────────────────────────────────────────────

    # OVERHEAD: catalog-read only — compares structure, never re-runs statistics
    def detect_drift(
        self,
        current_schema: DataSourceSchema,
        stored_model: SemanticModel,
        privacy: PrivacySettings | None = None,
        *,
        drift: SchemaDrift | None = None,
    ) -> list[str]:
        """
        Compare current schema structure against stored semantic model.
        Returns list of human-readable drift warnings.

        IMPORTANT: This method ONLY compares structural changes
        (tables/columns added, removed, renamed). It NEVER re-runs
        statistical queries.

        The table/column half is ``semantic/drift.compute_drift`` — the same diff
        ``apply_drift`` acts on, so the check cannot report what the apply would not
        remove. ``drift`` is that diff when the caller already computed it (the drift
        route does, for ``has_actionable_changes``, over the known-columns baseline);
        otherwise it is computed here without a baseline, so no column is reported added
        and no removed key counts as a drop.

        Each removed column key is worded by what the apply does to it. A key in the
        diff's ``dropped_columns`` (the baseline knew it as a live column) no longer
        exists, whatever it resembles, and is named verbatim: it is a real column name.
        Any other is resolved against the table's live columns with ``resolve_real_key``
        (the rule ``compute_drift`` uses): a near-miss spelling of a column in
        ``respelled_columns`` is a respelling (the apply removes the key and annotates
        the column afresh), so that column is not also listed as new; a near-miss
        spelling of a column the model also stores is a duplicate the apply removes;
        any other key no longer exists, named quote-stripped unless the stripped name is
        still a live column.

        With ``privacy``, a stored table or column the policy hides is not reported
        as removed: the store keeps hidden annotations, and ``current_schema`` (with
        the policy applied) lacks them because they are hidden, not gone — the same
        rule as ``prune_to_schema``.

        Nor is a stored table in ``current_schema.unreadable_tables``: it exists, but
        the connection's role cannot read it. One warning names those instead, since
        neither a regenerate nor apply-drift can fix a missing grant.
        """
        warnings: list[str] = []
        if drift is None:
            drift = compute_drift(current_schema, stored_model, privacy)

        live_columns = {
            table_key(t): sorted(c.name for c in t.columns) for t in current_schema.tables
        }
        for tbl_key, table in stored_model.tables.items():
            if tbl_key in drift.removed_table_keys:
                warnings.append(
                    f'Table "{tbl_key}" referenced in semantic model '
                    f"no longer exists in the database"
                )
                continue
            live = live_columns.get(tbl_key, [])
            respelled = drift.respelled_columns.get(tbl_key, set())
            dropped = drift.dropped_columns.get(tbl_key, set())
            missing: set[str] = set()
            duplicates: dict[str, str] = {}
            respellings: dict[str, str] = {}
            for col in drift.removed_columns.get(tbl_key, ()):
                if col in dropped:
                    # A key the baseline knew is a real column the database dropped: not a
                    # near-miss spelling of anything, even of a column still stored, and
                    # named as the database named it, quotes included.
                    missing.add(col)
                    continue
                target = resolve_real_key(col, live, bare_ok=False)
                if target is not None and target in respelled:
                    respellings[col] = target
                elif target is not None and target in table.columns:
                    duplicates[col] = target
                else:
                    # Only for a key that is not a known drop (a dropped key is a real name,
                    # so real columns ``"a"`` and ``a`` both dropped are two warnings):
                    # normalised then deduplicated, so a blob-era ``"a"`` and ``a`` stored
                    # side by side, both gone, are one missing column to a reader. A
                    # stripped name that is still live keeps its stored spelling, so the
                    # warning never says a live column is gone.
                    stripped = strip_ident_quotes(col)
                    missing.add(col if stripped in live else stripped)
            for col in sorted(missing):
                warnings.append(
                    f'Column "{tbl_key}.{col}" referenced in semantic model no longer exists'
                )
            # Near-miss keys (quoted, or differing only in letter case) are named under
            # their stored spelling: the duplicate's twin keeps its own annotation; a
            # respelling loses its annotation to a fresh one.
            for col in sorted(duplicates):
                warnings.append(
                    f'Column key "{tbl_key}.{col}" in the semantic model is a near-miss '
                    f'spelling of "{duplicates[col]}"; Apply Drift removes the duplicate'
                )
            for col in sorted(respellings):
                target = respellings[col]
                warnings.append(
                    f'Column key "{tbl_key}.{col}" in the semantic model is a near-miss '
                    f'spelling of "{target}"; Apply Drift removes the key and annotates '
                    f'"{target}" afresh (the key\'s annotation is not kept)'
                )

        if drift.unreadable_table_keys:
            names = sorted(drift.unreadable_table_keys)
            sample = ", ".join(names[:5]) + (", …" if len(names) > 5 else "")
            warnings.append(
                f"{len(names)} table(s) in the semantic model exist in the database but this "
                f"connection's database role cannot read them: {sample}. Their annotations "
                "are kept; grant the role access to use them."
            )

        if drift.new_table_keys:
            sample = ", ".join(sorted(drift.new_table_keys)[:5])
            warnings.append(
                f"{len(drift.new_table_keys)} new table(s) added since semantic model "
                f"was generated: {sample}"
            )

        new_columns = [
            f"{tbl}.{col}"
            for tbl in sorted(drift.added_columns)
            for col in sorted(drift.added_columns[tbl] - drift.respelled_columns.get(tbl, set()))
        ]
        if new_columns:
            sample = ", ".join(new_columns[:5]) + (", …" if len(new_columns) > 5 else "")
            warnings.append(
                f"{len(new_columns)} new column(s) added to tables in the semantic model "
                f"since it was generated: {sample}"
            )

        current_tables = {table_key(t) for t in current_schema.tables}
        # Bare names (without schema prefix) for metrics generated by LLMs that omit schema
        current_tables_bare = {t.name for t in current_schema.tables}
        # A metric may name a table bare, so match an unreadable one either way.
        unreadable = set(current_schema.unreadable_tables)
        unreadable_names = unreadable | {key.rpartition(".")[2] for key in unreadable}
        # A bare metric table under an excluded schema: the policy cannot be asked
        # about a bare name, but the stored model holds the qualified key it came from.
        stored_hidden_bare = {
            key.rpartition(".")[2]
            for key in stored_model.tables
            if key not in current_tables and policy_hides_table(key, privacy)
        }
        for metric in stored_model.business_metrics:
            for t in metric.related_tables:
                norm_t = strip_ident_quotes(t)
                # Accept both "public.orders" (qualified) and "orders" (bare)
                if (
                    norm_t not in current_tables
                    and norm_t not in current_tables_bare
                    and norm_t not in stored_hidden_bare
                    and norm_t not in unreadable_names
                    and not policy_hides_table(norm_t, privacy, bare_ok=True)
                ):
                    warnings.append(
                        f'Business metric "{metric.name}" references '
                        f'table "{t}" which no longer exists'
                    )

        return warnings

    # OVERHEAD: app-only — hash of already-loaded schema structure
    def compute_schema_hash(self, schema: DataSourceSchema) -> str:
        """
        Hash the structural elements of a schema (names and types only).
        Ignores statistics, sample values, and row counts.
        Used for cheap drift detection without re-querying the database.
        """
        structure = []
        for table in sorted(schema.tables, key=table_key):
            table_entry = {
                "key": table_key(table),
                "type": table.table_type,
                "columns": [
                    {"name": c.name, "type": c.data_type, "pk": c.is_primary_key}
                    for c in sorted(table.columns, key=lambda c: c.name)
                ],
            }
            structure.append(table_entry)

        return hashlib.sha256(json.dumps(structure, sort_keys=True).encode()).hexdigest()

    # ── Time expressions ───────────────────────────────────────────────────────

    # OVERHEAD: app-only — pure string computation
    def build_time_expressions(self, dialect: str) -> dict[str, str]:
        """
        Returns dialect-specific SQL expressions for common time periods.
        No database calls. Stored in SemanticModel for prompt injection.

        *dialect* is the datasource ``source_type`` (``"postgresql"`` or ``"mysql"``)
        or the legacy ``"sql"`` sentinel.
        """
        if dialect in ("sql", "postgresql"):
            return dict(_PG_TIME_EXPRESSIONS)

        if dialect == "mysql":
            return {
                "today": "CURDATE()",
                "yesterday": "DATE_SUB(CURDATE(), INTERVAL 1 DAY)",
                "this_week": "DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)",
                "last_week": "DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) + 7 DAY)",
                "this_month": "DATE_FORMAT(CURDATE(), '%Y-%m-01')",
                "last_month": "DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 MONTH), '%Y-%m-01')",
                "this_quarter": (
                    "MAKEDATE(YEAR(CURDATE()), 1) + INTERVAL (QUARTER(CURDATE())-1) QUARTER"
                ),
                "last_quarter": (
                    "MAKEDATE(YEAR(CURDATE()), 1) + INTERVAL (QUARTER(CURDATE())-2) QUARTER"
                ),
                "this_year": "DATE_FORMAT(CURDATE(), '%Y-01-01')",
                "last_year": "DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 YEAR), '%Y-01-01')",
                "ytd": "DATE_FORMAT(CURDATE(), '%Y-01-01')",
                "rolling_7": "DATE_SUB(CURDATE(), INTERVAL 7 DAY)",
                "rolling_30": "DATE_SUB(CURDATE(), INTERVAL 30 DAY)",
                "rolling_90": "DATE_SUB(CURDATE(), INTERVAL 90 DAY)",
                "rolling_12m": "DATE_SUB(CURDATE(), INTERVAL 12 MONTH)",
            }

        return {}

    # ── Private helpers ────────────────────────────────────────────────────────

    def _build_tables_summary(
        self,
        schema: DataSourceSchema,
        all_tables: dict[str, TableSemantic] | None = None,
        max_cols: int = 20,
    ) -> str:
        """Compact per-table summary injected into the global sections prompt.

        Each line is ``schema.table (col:type, ...) [primary_date=col] [primary_ts=col]``.
        Including data types lets the LLM distinguish date/int/numeric time dimensions
        (e.g. fiscal_year:int + fiscal_quarter:int) from timestamp audit columns
        (created_at:timestamp). When ``all_tables`` is provided the already-generated
        TableSemantic objects supply the primary date/timestamp hints.
        """
        lines: list[str] = []
        for tbl in schema.tables:
            tbl_key = table_key(tbl)
            col_parts = [f"{c.name}:{c.data_type}" for c in tbl.columns[:max_cols]]
            line = f"  {tbl_key} ({', '.join(col_parts)})"
            if all_tables and tbl_key in all_tables:
                sem = all_tables[tbl_key]
                hints: list[str] = []
                if sem.primary_date_column:
                    hints.append(f"primary_date={sem.primary_date_column}")
                if sem.primary_timestamp_column:
                    hints.append(f"primary_ts={sem.primary_timestamp_column}")
                if hints:
                    line += f" [{', '.join(hints)}]"
            lines.append(line)
        return "\n".join(lines)

    # Maximum characters for the generated DDL string.  Large schemas (e.g. Airflow)
    # can produce 100 k+ chars with sample values, which exceeds provider payload
    # limits (Groq 413, etc.).  We truncate gracefully when the limit is reached.
    _MAX_DDL_CHARS: int = 20_000

    def _schema_to_ddl(self, schema: DataSourceSchema) -> str:
        """Produce a compact CREATE TABLE DDL string from a DataSourceSchema.

        If the total DDL would exceed ``_MAX_DDL_CHARS``, tables are included in order
        until the limit is reached and a truncation notice is appended.
        """
        table_blocks = [self._table_ddl_block(table) for table in schema.tables]

        parts: list[str] = []
        total = 0
        truncated = False
        for block in table_blocks:
            if total + len(block) > self._MAX_DDL_CHARS:
                truncated = True
                break
            parts.append(block)
            total += len(block)

        ddl = "\n".join(parts)
        if truncated:
            omitted = len(table_blocks) - len(parts)
            ddl += (
                f"\n-- NOTE: {omitted} additional table(s) omitted — schema exceeded "
                f"the {self._MAX_DDL_CHARS:,}-character limit for LLM generation."
            )
        return ddl

    @staticmethod
    def _table_ddl_block(table: TableInfo) -> str:
        """One table's ``CREATE TABLE`` block, as ``_schema_to_ddl`` renders it."""
        fq = table_key(table)
        block_lines: list[str] = [f"-- Table: {fq}"]
        if table.description:
            block_lines.append(f"-- {table.description}")
        block_lines.append(f"CREATE TABLE {fq} (")
        col_lines: list[str] = []
        for col in table.columns:
            parts = [f"  {col.name} {col.native_type}"]
            if not col.nullable:
                parts.append("NOT NULL")
            if col.is_primary_key:
                parts.append("PRIMARY KEY")
            annotations: list[str] = []
            if col.description:
                annotations.append(col.description)
            col_line = " ".join(parts)
            if annotations:
                col_line += f"  -- {'; '.join(annotations)}"
            col_lines.append(col_line)
        block_lines.append(",\n".join(col_lines))
        block_lines.append(");")
        block_lines.append("")
        return "\n".join(block_lines)

    def table_ddl_sizes(self, schema: DataSourceSchema, keys: Iterable[str]) -> dict[str, int]:
        """Table key → the characters ``_schema_to_ddl`` counts for that table against
        ``_MAX_DDL_CHARS`` (its block; the joining newlines are not counted), for the keys
        the schema holds. ``_schema_to_ddl`` drops every table past the limit, so a batch
        whose sizes sum to at most ``_MAX_DDL_CHARS`` is rendered whole. A table whose
        size alone exceeds the limit fits no batch: wherever it is sent its DDL is
        omitted (as it would be for ``generate_tables``)."""
        wanted = set(keys)
        return {
            table_key(t): len(self._table_ddl_block(t))
            for t in schema.tables
            if table_key(t) in wanted
        }
