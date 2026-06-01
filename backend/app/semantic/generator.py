# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Auto-generates a SemanticModel from a DataSourceSchema using an LLM."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
import logging
from typing import TYPE_CHECKING

from ..datasources.models import ColumnInfo, DataSourceSchema
from .models import (
    AggregationType,
    BusinessMetric,
    CardinalityClass,
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

# Tables per LLM call in batched generation. Schemas with up to _BATCH_SIZE tables
# generate in a single batch; larger schemas are split into multiple batches.
_BATCH_SIZE: int = 4

# Prompt for a single batch of tables (tables section only, no global metrics/joins).
_BATCH_TABLES_PROMPT = """\
You are a data analyst examining a database schema. Analyze the tables below and \
generate semantic annotations.

DATABASE SCHEMA:
{schema_ddl}

{column_stats_summary}
For each table and column, provide:
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
sum, count, count_distinct, average, max, min, median. Omit for non-numeric columns.
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
        # uniform code path. Even small schemas now batch (one batch when ≤ _BATCH_SIZE).
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
            table_key = f"{table.schema_name}.{table.name}"
            if table_key in parsed.tables:
                temporal = self._infer_temporal_columns(table_key, table.columns)
                if temporal["primary_timestamp_column"]:
                    parsed.tables[table_key].primary_timestamp_column = temporal[
                        "primary_timestamp_column"
                    ]
                if temporal["primary_date_column"]:
                    parsed.tables[table_key].primary_date_column = temporal["primary_date_column"]

        # Backfill fingerprint-derived cardinality onto ColumnSemantic objects.
        # column_stats keys are "table_name.col_name" (no schema prefix).
        if column_stats:
            for table in schema.tables:
                table_key = f"{table.schema_name}.{table.name}"
                if table_key not in parsed.tables:
                    continue
                for col in table.columns:
                    stat_key = f"{table.name}.{col.name}"
                    if stat_key not in column_stats:
                        continue
                    if col.name in parsed.tables[table_key].columns:
                        parsed.tables[table_key].columns[col.name].cardinality = column_stats[
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
        1. Split schema.tables into batches of _BATCH_SIZE.
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
        batches = [tables[i : i + _BATCH_SIZE] for i in range(0, len(tables), _BATCH_SIZE)]
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

    def prepare_generation(
        self,
        schema: DataSourceSchema,
    ) -> dict:
        """
        Compute everything needed to orchestrate phased generation — no LLM, no adapter.

        Returns a plain dict suitable for JSON serialisation:
        {
            "tables_total": int,
            "batch_count": int,
            "batch_size": int,
            "relationship_edges": list[RelationshipEdge],  # kept as dataclasses
        }
        """
        relationship_edges = self._build_relationship_graph(schema)
        tables_total = len(schema.tables)
        batch_count = max(1, -(-tables_total // _BATCH_SIZE))  # ceil division
        return {
            "tables_total": tables_total,
            "batch_count": batch_count,
            "batch_size": _BATCH_SIZE,
            "relationship_edges": relationship_edges,
        }

    async def generate_table_batch(
        self,
        schema: DataSourceSchema,
        provider: BaseLLMProvider,
        model: str | None,
        batch_idx: int,
        relationship_edges: list[RelationshipEdge],
    ) -> dict[str, TableSemantic]:
        """
        Generate semantic annotations for one batch of tables (no adapter required).

        Returns a dict of ``schema.table → TableSemantic`` for the batch tables only.
        Raises ``ValueError`` on LLM failure so the router can return HTTP 500.
        """
        tables = schema.tables
        start = batch_idx * _BATCH_SIZE
        batch = tables[start : start + _BATCH_SIZE]
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
        batch_table_keys = {f"{t.schema_name}.{t.name}" for t in batch}
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

        result = await provider.generate_structured(
            system_prompt=sys_prompt,
            user_message=prompt,
            schema_type=TablesBatchResponse,
            model=model,
            temperature=0.0,
            max_tokens=min(65536, provider.max_output_tokens),
        )
        self._enrich_fiscal_table_annotations(result.tables, schema)
        return result.tables

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

        metrics: list[BusinessMetric] = []
        joins: list[CommonJoin] = []
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
            metrics = global_result.business_metrics
            joins = global_result.common_joins
            derived_columns = global_result.derived_columns
            segments = global_result.segments
        except Exception as exc:
            logger.warning(
                "SemanticModelGenerator.generate_globals: call failed: %s — returning empty",
                exc,
            )

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
            f"{t.schema_name}.{t.name}": {c.name: c.nullable for c in t.columns}
            for t in schema.tables
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
        table_key: str,
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
                keys.add(f"{tbl.schema_name}.{tbl.name}")
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
            table_key = f"{tbl.schema_name}.{tbl.name}"
            if table_key not in fiscal_keys or table_key not in tables:
                continue
            fiscal_cols = [
                c.name
                for c in tbl.columns
                if c.name.lower().endswith(_FISCAL_DIMENSION_SUFFIXES)
                or c.name.lower().startswith(_FISCAL_DIMENSION_PREFIXES)
            ]
            sem_table = tables[table_key]
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
        fk_targets: dict[str, set[str]] = {
            f"{t.schema_name}.{t.name}": set() for t in schema.tables
        }
        for rel in schema.relationships:
            from_key = f"{rel.from_schema}.{rel.from_table}"
            to_key = f"{rel.to_schema}.{rel.to_table}"
            if from_key in fk_targets:
                fk_targets[from_key].add(to_key)

        # DATE columns per table (excludes TIMESTAMP — those are not period axes)
        date_col_map: dict[str, list[str]] = {
            f"{t.schema_name}.{t.name}": [c.name for c in t.columns if c.data_type == "date"]
            for t in schema.tables
        }

        notes: list[str] = []
        for tbl in schema.tables:
            table_key = f"{tbl.schema_name}.{tbl.name}"
            if table_key not in fiscal_keys:
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
            my_targets = fk_targets.get(table_key, set())
            companions: list[str] = []
            for other_key, other_date_cols in date_col_map.items():
                if other_key == table_key or other_key in fiscal_keys:
                    continue
                if not other_date_cols:
                    continue
                if my_targets & fk_targets.get(other_key, set()):
                    companions.append(f"{other_key} ({', '.join(other_date_cols[:2])})")

            note = (
                f"{table_key} is a period table — filter by "
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
            table_key = f"{tbl.schema_name}.{tbl.name}"
            if table_key not in tables:
                continue
            sem = tables[table_key]

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
            table_key = f"{table.schema_name}.{table.name}"
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
                                f"ROUND({table_key}.{num_col}"
                                f" / NULLIF({table_key}.{den_col}, 0) * 100, 2)"
                            ),
                            base_tables=[table_key],
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
    ) -> list[str]:
        """
        Compare current schema structure against stored semantic model.
        Returns list of human-readable drift warnings.

        IMPORTANT: This method ONLY compares structural changes
        (tables/columns added, removed, renamed). It NEVER re-runs
        statistical queries.
        """
        warnings: list[str] = []

        def _norm(name: str) -> str:
            """Strip SQL quoting characters that LLMs may emit around identifiers."""
            return name.replace("`", "").replace('"', "").replace("'", "")

        current_tables = {f"{t.schema_name}.{t.name}" for t in current_schema.tables}
        # Bare names (without schema prefix) for metrics generated by LLMs that omit schema
        current_tables_bare = {t.name for t in current_schema.tables}
        current_columns: dict[str, set[str]] = {}
        for t in current_schema.tables:
            key = f"{t.schema_name}.{t.name}"
            current_columns[key] = {c.name for c in t.columns}

        for table_key in stored_model.tables:
            if table_key not in current_tables:
                warnings.append(
                    f'Table "{table_key}" referenced in semantic model '
                    f"no longer exists in the database"
                )
            else:
                stored_cols = {_norm(c) for c in stored_model.tables[table_key].columns}
                live_cols = current_columns.get(table_key, set())
                for col in stored_cols - live_cols:
                    warnings.append(
                        f'Column "{table_key}.{col}" referenced in semantic model no longer exists'
                    )

        semantic_tables = set(stored_model.tables.keys())
        new_tables = current_tables - semantic_tables
        if new_tables:
            sample = ", ".join(sorted(new_tables)[:5])
            warnings.append(
                f"{len(new_tables)} new table(s) added since semantic model was generated: {sample}"
            )

        for metric in stored_model.business_metrics:
            for t in metric.related_tables:
                norm_t = _norm(t)
                # Accept both "public.orders" (qualified) and "orders" (bare)
                if norm_t not in current_tables and norm_t not in current_tables_bare:
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
        for table in sorted(schema.tables, key=lambda t: f"{t.schema_name}.{t.name}"):
            table_entry = {
                "key": f"{table.schema_name}.{table.name}",
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
            table_key = f"{tbl.schema_name}.{tbl.name}"
            col_parts = [f"{c.name}:{c.data_type}" for c in tbl.columns[:max_cols]]
            line = f"  {table_key} ({', '.join(col_parts)})"
            if all_tables and table_key in all_tables:
                sem = all_tables[table_key]
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

        Sample values per column are capped at 3 and truncated to 60 characters
        each to keep the payload manageable.  If the total DDL would exceed
        ``_MAX_DDL_CHARS``, tables are included in order until the limit is
        reached and a truncation notice is appended.
        """
        table_blocks: list[str] = []
        for table in schema.tables:
            fq = f"{table.schema_name}.{table.name}"
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
            table_blocks.append("\n".join(block_lines))

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
