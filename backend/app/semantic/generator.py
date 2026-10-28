# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Auto-generates a SemanticModel from a DataSourceSchema using an LLM."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
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
    AggregationType,
    BusinessMetric,
    CardinalityClass,
    ColumnsBatchResponse,
    ColumnSemantic,
    CommonJoin,
    DerivedColumn,
    EntityType,
    GlobalSectionsResponse,
    Hierarchy,
    RelationshipEdge,
    RelationshipType,
    Segment,
    SemanticModel,
    SemanticType,
    TablesBatchResponse,
    TableSemantic,
    TimeGranularity,
    relationship_key,
)

if TYPE_CHECKING:
    from ..providers.base import BaseLLMProvider

logger = logging.getLogger(__name__)

# The per-column instructions of both annotation prompts (items 1-8; the table-level
# items 9-11 stay in ``_BATCH_TABLES_PROMPT``). One constant: a new column is
# annotated to the same standard as one from ``generate_tables``.
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
# Used by the fiscal-period helpers and by _infer_temporal_columns (→ suppress audit
# timestamps as primary_timestamp_column).
_FISCAL_DIMENSION_SUFFIXES: tuple[str, ...] = ("_year", "_quarter", "_period")
_FISCAL_DIMENSION_PREFIXES: tuple[str, ...] = ("fiscal_", "budget_", "calendar_", "tax_")


def _warn_near_miss(kind: str, name: str, target: str | None, where: str) -> None:
    """Warn that a dropped stored ``name`` was a near-miss spelling of live ``target``.

    No remap here: a migrated row's user-edit marks are keyed by the old spelling, so a
    remap would keep the value but silently lose the marks, and the next regenerate
    would overwrite the edit anyway. The saves that prune (``/apply-drift``,
    ``/generate/globals``) first rename the column keys the drift renames
    (``SchemaDrift.renamed_columns``), marks included (``rename_columns``), so a key
    their prune drops is one the drift does not rename: a duplicate of a stored column,
    say.
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
    return table.model_copy(
        update={
            "columns": columns,
            **_map_table_references(table, lambda name: name if kept(name) else None),
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
    absent key — a remap here would keep its value but lose its user-edit marks, which
    are keyed by the old spelling — and, when ``log``, a warning names the key it
    would have matched (``_resolve_real_key``'s rules). ``log=False`` is for a
    second call over the same model, so the warnings are not duplicated. The saves that
    prune (``/apply-drift``, ``/generate/globals``) first rename the column keys the
    drift renames, marks included (``rename_columns``), so this sees such a column
    under its real name and keeps it.

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


def _reference_names(field: str, value: Any) -> list[str]:
    """Every column name a table's reference *field* (one of ``_TABLE_REFERENCE_FIELDS``)
    holds in its stored shape, in order: each hierarchy's ``levels`` for ``hierarchies``,
    a list's entries, else the one name."""
    if field == "hierarchies":
        return [level for h in value for level in h.get("levels") or []]
    if isinstance(value, list):
        return list(value)
    return [value]


def _map_reference_value(
    field: str, value: Any, fn: Callable[[str], str | None], *, drop_empty: bool = True
) -> Any:
    """*value*, a table's reference *field* (one of ``_TABLE_REFERENCE_FIELDS``) in its
    stored shape — a name, a list of names, or for ``hierarchies`` a list of dicts with
    ``levels`` — with each column name replaced by ``fn(name)``. A name ``fn`` maps to
    ``None`` leaves: a list loses the entry, a hierarchy loses the level and (unless
    ``drop_empty=False``) is dropped when none remains, a scalar becomes ``None``. The one
    shape rule for the prune, the dangling-edit trim and the column renames. A rename
    passes ``drop_empty=False``: it keeps a hierarchy with no levels, as it always did.
    Pure: new lists and dicts; *value* is not touched."""
    if field == "hierarchies":
        out = []
        for h in value:
            levels = [new for level in h.get("levels") or [] if (new := fn(level)) is not None]
            if levels or not drop_empty:
                out.append({**h, "levels": levels})
        return out
    if isinstance(value, list):
        return [new for name in value if (new := fn(name)) is not None]
    return fn(value)


def _map_table_references(
    table: TableSemantic, fn: Callable[[str], str | None], *, drop_empty: bool = True
) -> dict[str, Any]:
    """A ``model_copy`` update for *table* with each column name of its
    ``_TABLE_REFERENCE_FIELDS`` replaced by ``fn(name)`` under ``_map_reference_value``'s
    rule: the fields are dumped to that shape, mapped, and the hierarchies validated back
    to ``Hierarchy``. A ``None`` field is returned as it is. ``drop_empty`` is passed through:
    a rename keeps a hierarchy with no levels, as it always did, and the prune drops it.
    Adding a reference field to the tuple is enough for the prune and the
    renames to cover it."""
    update = {
        field: _map_reference_value(field, value, fn, drop_empty=drop_empty)
        if value is not None
        else value
        for field, value in table.model_dump(include=set(_TABLE_REFERENCE_FIELDS)).items()
    }
    update["hierarchies"] = [Hierarchy.model_validate(h) for h in update["hierarchies"]]
    return update


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

        def kept_or_none(name: str, _kept: Callable[[str], bool] = kept) -> str | None:
            return name if _kept(name) else None

        for field in _TABLE_REFERENCE_FIELDS:
            value = values.get(field)
            if not value:
                continue
            dropped = _unique(name for name in _reference_names(field, value) if not kept(name))
            if not dropped:
                continue
            remaining = _map_reference_value(field, value, kept_or_none)
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


def _renamed_table(table: TableSemantic, mapping: Mapping[str, str]) -> TableSemantic:
    """``table`` with each column key of ``mapping`` renamed, its references included
    (``_map_table_references``, so every field of ``_TABLE_REFERENCE_FIELDS`` follows)."""

    def new(name: str) -> str:
        return mapping.get(name, name)

    return table.model_copy(
        update={
            "columns": {new(name): col for name, col in table.columns.items()},
            **_map_table_references(table, new, drop_empty=False),
        }
    )


def rename_columns(
    tables: Mapping[str, TableSemantic],
    relationships: Sequence[RelationshipEdge],
    marks: dict[str, dict[str, list[str]]],
    protected: dict[str, dict[str, dict[str, Any]]],
    renames: Mapping[str, Mapping[str, str]],
) -> tuple[
    dict[str, TableSemantic],
    list[RelationshipEdge],
    dict[str, dict[str, list[str]]],
    dict[str, dict[str, dict[str, Any]]],
]:
    """``tables``, ``relationships``, ``marks`` and ``protected`` with each column key of
    ``renames`` rekeyed to its new name.

    ``renames`` is ``SchemaDrift.renamed_columns`` of a diff against ``tables``: table key
    → old key → new name, where the old key is a column of that table and the new name
    is not. A renamed column is the column the live schema has, stored under a stale
    spelling, so everything that names it by key follows the rename, and it keeps its
    annotation and its user edits:

    - the ``ColumnSemantic`` moves to the new key (in place, so the column order holds);
    - the table's reference fields (``_TABLE_REFERENCE_FIELDS``: the primary timestamp and
      date columns, the partition and cluster columns, each hierarchy's ``levels``). A
      hierarchy with no levels is kept as it was; dropping it is ``prune_to_schema``'s;
    - each relationship endpoint on that table, with the edge's marks moved to its new
      ``relationship_key``. A renamed edge whose new key is one another edge already has
      is a duplicate and is dropped with its marks: the edge stored under the real names
      wins, as a column stored under its real name does;
    - the column's marks and protected values (keyed ``"table.column"``), and a table's
      protected values for the reference fields above (its marks name fields, not
      columns, and stay as they are).

    Free SQL (``default_filters``, metric, segment and derived-column expressions, join
    patterns, a relationship's ``join_sql``) is not rewritten, as ``prune_to_schema`` does
    not rewrite it either. Nor are a metric's ``non_additive_dimension`` and ``entity``:
    they name a column without its table, so a rename cannot be scoped to them.

    Runs before ``drop_dangling_edits`` and ``prune_to_schema``, so both see a renamed
    column under its live name and leave it alone. Pure: the returned containers are new
    and the inputs are never mutated; a table with no rename and an edge with no renamed
    endpoint are the input's objects.
    """
    out_tables = dict(tables)
    out_marks = {kind: {k: list(v) for k, v in entries.items()} for kind, entries in marks.items()}
    out_protected = {
        kind: {k: dict(v) for k, v in entries.items()} for kind, entries in protected.items()
    }
    for key, mapping in renames.items():
        out_tables[key] = _renamed_table(tables[key], mapping)
        for old, new in mapping.items():
            for entries in (out_marks.get("column"), out_protected.get("column")):
                if entries is not None and f"{key}.{old}" in entries:
                    entries[f"{key}.{new}"] = entries.pop(f"{key}.{old}")
        values = out_protected.get("table", {}).get(key, {})
        for field in _TABLE_REFERENCE_FIELDS:
            if value := values.get(field):
                values[field] = _map_reference_value(
                    field, value, lambda name, _m=mapping: _m.get(name, name), drop_empty=False
                )

    def end(table: str, column: str) -> str:
        return renames.get(table, {}).get(column, column)

    held = {relationship_key(e) for e in relationships}
    out_relationships: list[RelationshipEdge] = []
    for edge in relationships:
        moved = edge.model_copy(
            update={
                "from_column": end(edge.from_table, edge.from_column),
                "to_column": end(edge.to_table, edge.to_column),
            }
        )
        if moved == edge:
            out_relationships.append(edge)
            continue
        old_key, new_key = relationship_key(edge), relationship_key(moved)
        edge_marks = out_marks.get("relationship", {}).pop(old_key, None)
        if new_key in held:
            continue
        held.add(new_key)
        if edge_marks is not None:
            out_marks["relationship"][new_key] = edge_marks
        out_relationships.append(moved)
    return out_tables, out_relationships, out_marks, out_protected


class SemanticModelGenerator:
    """Generates an initial SemanticModel by asking an LLM to analyse the schema."""

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
        prompt = _BATCH_TABLES_PROMPT.format(schema_ddl=batch_ddl)
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

        Not called on the generation path yet; see
        docs/architecture/semantic-model-generation.md, "Enrichment not yet on the
        generation path".
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
        - time_granularity = DAY: for columns the adapter typed as "date" that have none
        - COUNT_DISTINCT_APPROX: suggested for high-cardinality IDENTIFIER/FOREIGN_KEY columns
          without an aggregation; needs ``cardinality``, which no generator sets

        Not called on the generation path yet; see
        docs/architecture/semantic-model-generation.md, "Enrichment not yet on the
        generation path".
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

        Each removed or renamed column key is worded by what the apply does to it. A key
        in the diff's ``renamed_columns`` is a near-miss spelling the apply renames to its
        live column, keeping its annotation; that column is not listed as new. A key in
        ``dropped_columns`` (the baseline knew it as a live column) no longer exists,
        whatever it resembles, and is named verbatim: it is a real column name. Any other
        is resolved against the table's live columns with ``resolve_real_key`` (the rule
        ``compute_drift`` uses): a near-miss spelling of a column the model also stores,
        or of one another key is renamed to, is a duplicate the apply removes; one of a live
        column the model does not store and no key is renamed to is a column the model has
        not seen (with a baseline it is in ``added_columns`` too), and the apply removes
        the key; any other key no longer exists, named quote-stripped.

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
            renamed = drift.renamed_columns.get(tbl_key, {})
            dropped = drift.dropped_columns.get(tbl_key, set())
            missing: set[str] = set()
            duplicates: dict[str, str] = {}
            unseen: dict[str, str] = {}
            for col in drift.removed_columns.get(tbl_key, ()):
                if col in dropped:
                    # A key the baseline knew is a real column the database dropped: not a
                    # near-miss spelling of anything, even of a column still stored, and
                    # named as the database named it, quotes included.
                    missing.add(col)
                    continue
                target = resolve_real_key(col, live, bare_ok=False)
                if target is None:
                    # Only for a key that is not a known drop (a dropped key is a real name,
                    # so real columns ``"a"`` and ``a`` both dropped are two warnings):
                    # normalised then deduplicated, so a blob-era ``"a"`` and ``a`` stored
                    # side by side, both gone, are one missing column to a reader. (The
                    # stripped name is not live, or it would have resolved.)
                    missing.add(strip_ident_quotes(col))
                elif target in table.columns or target in renamed.values():
                    duplicates[col] = target
                else:
                    # A near-miss spelling of a live column the table does not store and no
                    # key is renamed to: with a baseline, one it never saw (otherwise
                    # ``_column_renames`` renamed this key), so the column is new
                    # (``added_columns``, worded below) and the apply removes the key;
                    # without one, nothing is new and the apply still removes the key.
                    unseen[col] = target
            for col in sorted(missing):
                warnings.append(
                    f'Column "{tbl_key}.{col}" referenced in semantic model no longer exists'
                )
            # Near-miss keys (quoted, or differing only in letter case) are named under
            # their stored spelling: the duplicate's twin keeps its own annotation; an
            # unseen key's column is annotated afresh; a renamed key carries its
            # annotation to its live name.
            for col in sorted(duplicates):
                warnings.append(
                    f'Column key "{tbl_key}.{col}" in the semantic model is a near-miss '
                    f'spelling of "{duplicates[col]}"; Apply Drift removes the duplicate'
                )
            for col in sorted(unseen):
                warnings.append(
                    f'Column key "{tbl_key}.{col}" in the semantic model is a near-miss '
                    f'spelling of "{unseen[col]}", a column the model has not seen; Apply '
                    "Drift removes the key"
                )
            for col in sorted(renamed):
                target = renamed[col]
                warnings.append(
                    f'Column key "{tbl_key}.{col}" in the semantic model is a near-miss '
                    f'spelling of "{target}"; Apply Drift renames it to "{target}", keeping '
                    "its annotation"
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
            for col in sorted(drift.added_columns[tbl])
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
