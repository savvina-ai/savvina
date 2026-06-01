# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Formats a SemanticModel into human-readable text for LLM prompt injection."""

from __future__ import annotations

import re

from .models import (
    CardinalityClass,
    ConversionMetric,
    CumulativeMetric,
    MetricType,
    RatioMetric,
    RelationshipType,
    SemanticModel,
    SemanticType,
    SimpleMetric,
)

# SemanticType groups that drive per-table column summary blocks
_AGGREGATE_TYPES = frozenset(
    {SemanticType.MONETARY, SemanticType.COUNT, SemanticType.MEASUREMENT, SemanticType.PERCENTAGE}
)
_GROUPBY_TYPES = frozenset(
    {SemanticType.CATEGORICAL, SemanticType.STATUS_FLAG, SemanticType.BOOLEAN_FLAG}
)
_DATE_TYPES = frozenset({SemanticType.TIMESTAMP, SemanticType.DATE})
_IDENTIFIER_TYPES = frozenset({SemanticType.IDENTIFIER, SemanticType.FOREIGN_KEY})


def _truncate_description(text: str, max_chars: int = 120) -> str:
    """Return the first sentence of text, hard-capped at max_chars characters."""
    if not text:
        return text
    # Split on sentence-ending punctuation
    m = re.search(r"[.!?]", text)
    sentence = text[: m.end()].strip() if m else text.strip()
    if len(sentence) <= max_chars:
        return sentence
    return sentence[:max_chars].rstrip() + "..."


class SemanticFormatter:
    """Converts a :class:`SemanticModel` to a compact prompt-ready string."""

    def format_for_prompt(
        self,
        model: SemanticModel,
        include_time_exprs: bool = True,
    ) -> str:
        """
        Render the semantic model as structured text for injection into a system prompt.

        ``include_time_exprs`` should be set to ``False`` for non-temporal questions
        to avoid emitting all 15 time-expression snippets on every turn.

        Output format::

            SEMANTIC CONTEXT:

            Table "public.orders" → Orders
              Description: Completed customer purchase records.
              Default filters: status != 'deleted'
              Columns:
                - status (Order Status): A=Active, D=Deleted  (filter: status = 'A')
                - total_amount (Total Amount) (currency: EUR): Revenue amount
              Aggregate with: total_amount (EUR)
              Group by:       status
              Date columns:   created_at [primary timestamp]
              Identifiers:    id, customer_id

            RELATIONSHIPS:
              FROM orders LEFT JOIN customers ON orders.customer_id = customers.id

            Business Metrics:
              - Revenue = SUM(public.orders.total_amount)  [currency_eur]

            DERIVED CALCULATIONS (always use these exact expressions):
              "Gross Margin %" = (base_price - cost_price) / NULLIF(base_price, 0) * 100

            TIME EXPRESSIONS (use these for date-based questions):
              "this_month" = DATE_TRUNC('month', CURRENT_DATE)
        """
        if not (
            model.tables
            or model.business_metrics
            or model.common_joins
            or model.notes
            or model.segments
            or model.relationships
            or model.derived_columns
            or model.time_expressions
        ):
            return ""

        parts: list[str] = ["SEMANTIC CONTEXT:"]

        # ── Domain notes — injected first so they act as global context ───────
        if model.notes:
            parts.append("\nNOTES (apply to all queries against this database):")
            for note in model.notes:
                parts.append(f"  • {note}")

        # ── Tables ────────────────────────────────────────────────────────────
        for table_key, tbl in model.tables.items():
            parts.append(f'\nTable "{table_key}" → {tbl.display_name}')

            if tbl.description:
                parts.append(f"  Description: {_truncate_description(tbl.description)}")

            if tbl.grain:
                parts.append(f"  Grain: {tbl.grain}")

            if tbl.base_sql:
                # Emit only the first line to keep the prompt compact
                first_line = tbl.base_sql.split("\n")[0].strip()
                parts.append(f"  Virtual table (defined as SQL): {first_line}")

            if tbl.partition_columns:
                cols = ", ".join(tbl.partition_columns)
                parts.append(
                    f"  Performance: ALWAYS filter on {cols} — "
                    "queries without this filter will full-scan and are expensive."
                )
            if tbl.cluster_columns:
                cols = ", ".join(tbl.cluster_columns)
                parts.append(f"  Cluster columns (filter-friendly): {cols}")

            if tbl.default_filters:
                parts.append(f"  Default filters: {'; '.join(tbl.default_filters)}")

            if tbl.primary_timestamp_column:
                parts.append(f"  Primary timestamp: {tbl.primary_timestamp_column}")

            if tbl.columns:
                parts.append("  Columns:")
                for col_name, col in tbl.columns.items():
                    # 2c: skip sensitive columns entirely
                    if col.is_sensitive:
                        continue

                    currency_tag = f" (currency: {col.currency})" if col.currency else ""
                    unit_tag = f" (unit: {col.unit})" if col.unit else ""
                    agg_tag = (
                        f" [aggregate: {col.default_aggregation}]"
                        if col.default_aggregation
                        else ""
                    )
                    non_additive_tag = " [NON-ADDITIVE]" if col.is_non_additive else ""
                    granularity_tag = (
                        f" [grain: {col.time_granularity}]" if col.time_granularity else ""
                    )

                    if col.value_mappings:
                        # Format as "label='raw'" so the LLM reads left→right as
                        # "user concept → SQL value to use in WHERE clauses".
                        mappings_str = ", ".join(
                            f"{vm.display_value}='{vm.raw_value}'" for vm in col.value_mappings
                        )
                        raw_vals = " | ".join(f"'{vm.raw_value}'" for vm in col.value_mappings)
                        col_line = (
                            f"    - {col_name} ({col.display_name})"
                            f"{currency_tag}{unit_tag}{agg_tag}"
                            f"{non_additive_tag}{granularity_tag}: {mappings_str}"
                            f"  [SQL values: {raw_vals}]"
                        )
                    else:
                        desc = (
                            f": {_truncate_description(col.description)}" if col.description else ""
                        )
                        col_line = (
                            f"    - {col_name} ({col.display_name})"
                            f"{currency_tag}{unit_tag}{agg_tag}"
                            f"{non_additive_tag}{granularity_tag}{desc}"
                        )
                    parts.append(col_line)

                # 2e: grouped column summary block
                self._append_column_summary(parts, tbl, table_key)

        # ── Relationship graph — compact single-line format ───────────────────
        # 2a: common_joins intentionally omitted (relationships covers this)
        if model.relationships:
            parts.append("\nRELATIONSHIPS:")
            for rel in model.relationships:
                # INNER JOIN when the FK column is non-nullable (every child row has
                # a matching parent); LEFT JOIN otherwise — the safe default that
                # preserves child rows whose FK is NULL.
                join_type = "INNER JOIN" if rel.is_required else "LEFT JOIN"
                # Extract bare table names for the JOIN template
                from_bare = rel.from_table.rpartition(".")[2]
                to_bare = rel.to_table.rpartition(".")[2]
                entity_hint = f" [{rel.entity_type} key]" if rel.entity_type else ""
                if rel.relationship_type == RelationshipType.ONE_TO_MANY:
                    # Reverse: from_table is on the "one" side, join to the "many" side
                    parts.append(
                        f"  FROM {from_bare} {join_type} {to_bare}"
                        f" ON {rel.from_table}.{rel.from_column}"
                        f" = {rel.to_table}.{rel.to_column}{entity_hint}"
                    )
                else:
                    parts.append(
                        f"  FROM {from_bare} {join_type} {to_bare} ON {rel.join_sql}{entity_hint}"
                    )

        # ── Business Metrics ──────────────────────────────────────────────────
        if model.business_metrics:
            parts.append("\nBusiness Metrics:")
            for metric in model.business_metrics:
                filter_clause = ""
                if metric.filters:
                    filter_clause = " [filter: " + "; ".join(metric.filters) + "]"
                fmt_tag = f" [{metric.format_hint}]" if metric.format_hint else ""
                type_tag = (
                    f" [type: {metric.metric_type}]"
                    if metric.metric_type != MetricType.SIMPLE
                    else ""
                )
                # Only Simple/Cumulative metrics expose a discrete aggregation
                # function; Ratio renders num/den, Derived embeds aggregation
                # inside the definition. Conversion renders base→conversion.
                aggregation = getattr(metric, "aggregation", None)
                agg_tag = f" [agg: {aggregation}]" if aggregation else ""
                if isinstance(metric, ConversionMetric):
                    window_hint = f" within {metric.window}" if metric.window else ""
                    entity_hint = f" per {metric.entity}" if metric.entity else ""
                    definition = (
                        f"{metric.conversion_measure} / {metric.base_measure}"
                        f"{entity_hint}{window_hint} [{metric.calculation}]"
                    )
                elif isinstance(metric, RatioMetric):
                    definition = f"({metric.numerator_expr}) / NULLIF({metric.denominator_expr}, 0)"
                else:
                    # SIMPLE/DERIVED/CUMULATIVE all carry ``definition``.
                    definition = metric.definition
                if isinstance(metric, (SimpleMetric, CumulativeMetric)) and metric.measure_filters:
                    filter_str = " AND ".join(metric.measure_filters)
                    definition = f"{definition} FILTER (WHERE {filter_str})"
                parts.append(
                    f"  - {metric.name} = {definition}{filter_clause}{fmt_tag}{type_tag}{agg_tag}"
                )
                if metric.description:
                    short_desc = _truncate_description(metric.description)
                    parts.append(f"      {short_desc}")
                if metric.is_non_additive:
                    dim_hint = (
                        f" over '{metric.non_additive_dimension}'"
                        if metric.non_additive_dimension
                        else ""
                    )
                    parts.append(
                        f"      WARNING: non-additive{dim_hint} — "
                        f"do NOT SUM across dimensions; use LAST_VALUE or snapshot approach."
                    )

        # ── Derived columns ───────────────────────────────────────────────────
        if model.derived_columns:
            parts.append("\nDERIVED CALCULATIONS (always use these exact expressions):")
            for dc in model.derived_columns:
                parts.append(f'  "{dc.name}" = {dc.sql_expression}')
                if dc.format_hint:
                    parts.append(f"    Format as: {dc.format_hint}")
                if dc.description:
                    parts.append(f"    {_truncate_description(dc.description)}")

        # ── Named segments (reusable WHERE-clause fragments) ─────────────────
        if model.segments:
            parts.append("\nNAMED FILTERS (use these exact WHERE clauses — do not rewrite them):")
            for seg in model.segments:
                table_scope = (
                    f" [applies to: {', '.join(seg.applicable_tables)}]"
                    if seg.applicable_tables
                    else ""
                )
                parts.append(f'  "{seg.name}": {seg.sql_expression}{table_scope}')
                if seg.description:
                    parts.append(f"    {_truncate_description(seg.description)}")

        # ── Time expressions — only when caller requests them ─────────────────
        if include_time_exprs and model.time_expressions:
            parts.append("\nTIME EXPRESSIONS (use these for date-based questions):")
            for label, expr in model.time_expressions.items():
                parts.append(f'  "{label}" = {expr}')
            if model.db_timezone:
                parts.append(f"  Database timezone: {model.db_timezone}")

        return "\n".join(parts)

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _append_column_summary(
        parts: list[str],
        tbl,  # TableSemantic
        table_key: str,
    ) -> None:
        """Append grouped column summary lines after the column list (2e)."""
        aggregate: list[str] = []
        groupby: list[str] = []
        dates: list[str] = []
        identifiers: list[str] = []

        for col_name, col in tbl.columns.items():
            if col.is_sensitive:
                continue
            st = col.semantic_type
            card = col.cardinality

            if st in _AGGREGATE_TYPES:
                label = col_name
                if col.currency:
                    label += f" ({col.currency})"
                elif col.unit:
                    label += f" ({col.unit})"
                aggregate.append(label)
            elif st in _DATE_TYPES:
                label = col_name
                if col_name == tbl.primary_timestamp_column:
                    label += " [primary timestamp]"
                elif col_name == tbl.primary_date_column:
                    label += " [primary date]"
                if col.time_granularity:
                    label += f" [grain: {col.time_granularity}]"
                dates.append(label)
            elif st in _IDENTIFIER_TYPES:
                identifiers.append(col_name)
            elif st in _GROUPBY_TYPES or card == CardinalityClass.LOW:
                groupby.append(col_name)

        if aggregate:
            parts.append(f"  Aggregate with: {', '.join(aggregate)}")
        if groupby:
            parts.append(f"  Group by:       {', '.join(groupby)}")
        if dates:
            parts.append(f"  Date columns:   {', '.join(dates)}")
        if identifiers:
            parts.append(f"  Identifiers:    {', '.join(identifiers)}  [do not aggregate]")
        non_additive = [
            n for n, c in tbl.columns.items() if c.is_non_additive and not c.is_sensitive
        ]
        if non_additive:
            parts.append(f"  Non-additive (never SUM across dims): {', '.join(non_additive)}")
        for h in tbl.hierarchies:
            parts.append(f"  Drill path ({h.name}): {' → '.join(h.levels)}")
