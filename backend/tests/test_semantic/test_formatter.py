# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for semantic/formatter.py — SemanticFormatter output correctness."""

from __future__ import annotations

from app.semantic.formatter import SemanticFormatter
from app.semantic.models import (
    AggregationType,
    ColumnSemantic,
    ConversionMetric,
    CumulativeMetric,
    DerivedColumn,
    FormatHint,
    RatioMetric,
    RelationshipEdge,
    Segment,
    SemanticModel,
    SemanticType,
    SimpleMetric,
    TableSemantic,
    TimeGranularity,
)

_fmt = SemanticFormatter()


# ── Empty model ────────────────────────────────────────────────────────────────


def test_empty_model_returns_empty_string():
    assert _fmt.format_for_prompt(SemanticModel()) == ""


# ── JOIN type selection ────────────────────────────────────────────────────────


def test_inner_join_when_is_required_true():
    model = SemanticModel(
        relationships=[
            RelationshipEdge(
                from_table="public.orders",
                from_column="customer_id",
                to_table="public.customers",
                to_column="id",
                join_sql="public.orders.customer_id = public.customers.id",
                is_required=True,
            )
        ]
    )
    output = _fmt.format_for_prompt(model)
    assert "INNER JOIN" in output
    assert "LEFT JOIN" not in output


def test_left_join_when_is_required_false():
    model = SemanticModel(
        relationships=[
            RelationshipEdge(
                from_table="public.orders",
                from_column="customer_id",
                to_table="public.customers",
                to_column="id",
                join_sql="public.orders.customer_id = public.customers.id",
                is_required=False,
            )
        ]
    )
    output = _fmt.format_for_prompt(model)
    assert "LEFT JOIN" in output
    assert "INNER JOIN" not in output


# ── Metric rendering ───────────────────────────────────────────────────────────


def test_simple_metric_renders_definition():
    model = SemanticModel(
        business_metrics=[
            SimpleMetric(
                name="Revenue",
                description="Total revenue",
                definition="SUM(orders.total_amount)",
                aggregation=AggregationType.SUM,
            )
        ]
    )
    output = _fmt.format_for_prompt(model)
    assert "Revenue" in output
    assert "SUM(orders.total_amount)" in output


def test_ratio_metric_renders_numerator_denominator():
    model = SemanticModel(
        business_metrics=[
            RatioMetric(
                name="CVR",
                description="Conversion rate",
                numerator_expr="COUNT(DISTINCT orders.id)",
                denominator_expr="COUNT(DISTINCT sessions.id)",
            )
        ]
    )
    output = _fmt.format_for_prompt(model)
    assert "CVR" in output
    assert "COUNT(DISTINCT orders.id)" in output
    assert "NULLIF(COUNT(DISTINCT sessions.id), 0)" in output


def test_metric_format_hint_appears_in_output():
    model = SemanticModel(
        business_metrics=[
            SimpleMetric(
                name="GMV",
                description="Gross merch value",
                definition="SUM(revenue)",
                aggregation=AggregationType.SUM,
                format_hint=FormatHint.CURRENCY_USD,
            )
        ]
    )
    output = _fmt.format_for_prompt(model)
    assert "currency_usd" in output


# ── Sensitive column handling ──────────────────────────────────────────────────


def test_sensitive_column_omitted_from_output():
    model = SemanticModel(
        tables={
            "public.users": TableSemantic(
                display_name="Users",
                columns={
                    "email": ColumnSemantic(
                        display_name="Email",
                        semantic_type=SemanticType.EMAIL,
                        is_sensitive=True,
                    ),
                    "status": ColumnSemantic(
                        display_name="Status",
                        semantic_type=SemanticType.STATUS_FLAG,
                        is_sensitive=False,
                    ),
                },
            )
        }
    )
    output = _fmt.format_for_prompt(model)
    assert "email" not in output
    assert "status" in output


# ── Segments and derived columns ──────────────────────────────────────────────


def test_segment_renders_sql_expression():
    model = SemanticModel(
        segments=[
            Segment(
                name="active_customers",
                sql_expression="status = 'active' AND deleted_at IS NULL",
                description="Active non-deleted customers",
            )
        ]
    )
    output = _fmt.format_for_prompt(model)
    assert "active_customers" in output
    assert "status = 'active'" in output


def test_derived_column_renders():
    model = SemanticModel(
        derived_columns=[
            DerivedColumn(
                name="Gross Margin %",
                sql_expression="(base_price - cost) / NULLIF(base_price, 0) * 100",
                format_hint=FormatHint.PERCENTAGE,
            )
        ]
    )
    output = _fmt.format_for_prompt(model)
    assert "Gross Margin %" in output
    assert "NULLIF(base_price, 0)" in output


# ── Table section ─────────────────────────────────────────────────────────────


def test_table_grain_appears():
    model = SemanticModel(
        tables={
            "public.orders": TableSemantic(
                display_name="Orders",
                grain="one row per order",
            )
        }
    )
    output = _fmt.format_for_prompt(model)
    assert "one row per order" in output


def test_default_filters_appear():
    model = SemanticModel(
        tables={
            "public.orders": TableSemantic(
                display_name="Orders",
                default_filters=["status != 'deleted'"],
            )
        }
    )
    output = _fmt.format_for_prompt(model)
    assert "status != 'deleted'" in output


def test_aggregate_column_summary_emitted():
    model = SemanticModel(
        tables={
            "public.orders": TableSemantic(
                display_name="Orders",
                columns={
                    "total": ColumnSemantic(
                        display_name="Total",
                        semantic_type=SemanticType.MONETARY,
                    )
                },
            )
        }
    )
    output = _fmt.format_for_prompt(model)
    assert "Aggregate with" in output


# ── Time expressions ──────────────────────────────────────────────────────────


def test_time_expressions_included_when_flag_true():
    model = SemanticModel(time_expressions={"today": "CURRENT_DATE"})
    output = _fmt.format_for_prompt(model, include_time_exprs=True)
    assert "today" in output


def test_time_expressions_omitted_when_flag_false():
    model = SemanticModel(time_expressions={"today": "CURRENT_DATE"})
    output = _fmt.format_for_prompt(model, include_time_exprs=False)
    assert "today" not in output


# ── Notes ─────────────────────────────────────────────────────────────────────


def test_notes_appear_first():
    model = SemanticModel(
        notes=["finance.budgets is a period table — filter by fiscal_year"],
        tables={"finance.budgets": TableSemantic(display_name="Budgets")},
    )
    output = _fmt.format_for_prompt(model)
    notes_pos = output.find("NOTES")
    table_pos = output.find("finance.budgets")
    assert notes_pos < table_pos


# ── ConversionMetric rendering ────────────────────────────────────────────────


def test_conversion_metric_renders_base_and_conversion_measure():
    model = SemanticModel(
        business_metrics=[
            ConversionMetric(
                name="Signup CVR",
                description="",
                base_measure="sessions",
                conversion_measure="signups",
                entity="user_id",
                window="7 days",
            )
        ]
    )
    output = _fmt.format_for_prompt(model)
    assert "Signup CVR" in output
    assert "signups" in output
    assert "sessions" in output
    assert "conversion_rate" in output


def test_conversion_metric_renders_window_and_entity():
    model = SemanticModel(
        business_metrics=[
            ConversionMetric(
                name="Purchase CVR",
                description="",
                base_measure="visits",
                conversion_measure="purchases",
                entity="visitor_id",
                window="14 days",
            )
        ]
    )
    output = _fmt.format_for_prompt(model)
    assert "14 days" in output
    assert "visitor_id" in output


def test_conversion_metric_without_optional_fields():
    model = SemanticModel(
        business_metrics=[
            ConversionMetric(
                name="Basic CVR",
                description="",
                base_measure="impressions",
                conversion_measure="clicks",
            )
        ]
    )
    output = _fmt.format_for_prompt(model)
    assert "clicks" in output
    assert "impressions" in output


# ── measure_filters rendering ─────────────────────────────────────────────────


def test_simple_metric_measure_filters_appended_as_filter_clause():
    model = SemanticModel(
        business_metrics=[
            SimpleMetric(
                name="Completed Revenue",
                description="",
                definition="SUM(orders.total)",
                aggregation=AggregationType.SUM,
                measure_filters=["status = 'completed'"],
            )
        ]
    )
    output = _fmt.format_for_prompt(model)
    assert "FILTER (WHERE status = 'completed')" in output


def test_cumulative_metric_measure_filters_appended():
    model = SemanticModel(
        business_metrics=[
            CumulativeMetric(
                name="Cumulative Revenue",
                description="",
                definition="SUM(orders.total)",
                aggregation=AggregationType.SUM,
                measure_filters=["region = 'EU'", "status != 'cancelled'"],
            )
        ]
    )
    output = _fmt.format_for_prompt(model)
    assert "FILTER (WHERE region = 'EU' AND status != 'cancelled')" in output


def test_simple_metric_no_measure_filters_no_filter_clause():
    model = SemanticModel(
        business_metrics=[
            SimpleMetric(
                name="Revenue",
                description="",
                definition="SUM(orders.total)",
                aggregation=AggregationType.SUM,
            )
        ]
    )
    output = _fmt.format_for_prompt(model)
    assert "FILTER" not in output


# ── is_non_additive rendering ─────────────────────────────────────────────────


def test_non_additive_column_renders_tag_in_column_list():
    model = SemanticModel(
        tables={
            "public.inventory": TableSemantic(
                display_name="Inventory",
                columns={
                    "stock_level": ColumnSemantic(
                        display_name="Stock Level",
                        semantic_type=SemanticType.MEASUREMENT,
                        is_non_additive=True,
                    )
                },
            )
        }
    )
    output = _fmt.format_for_prompt(model)
    assert "[NON-ADDITIVE]" in output


def test_non_additive_column_renders_summary_warning():
    model = SemanticModel(
        tables={
            "public.inventory": TableSemantic(
                display_name="Inventory",
                columns={
                    "stock_level": ColumnSemantic(
                        display_name="Stock Level",
                        semantic_type=SemanticType.MEASUREMENT,
                        is_non_additive=True,
                    )
                },
            )
        }
    )
    output = _fmt.format_for_prompt(model)
    assert "Non-additive" in output
    assert "stock_level" in output


def test_additive_column_no_non_additive_warning():
    model = SemanticModel(
        tables={
            "public.orders": TableSemantic(
                display_name="Orders",
                columns={
                    "total": ColumnSemantic(
                        display_name="Total",
                        semantic_type=SemanticType.MONETARY,
                        is_non_additive=False,
                    )
                },
            )
        }
    )
    output = _fmt.format_for_prompt(model)
    assert "NON-ADDITIVE" not in output
    assert "Non-additive" not in output


# ── time_granularity rendering ────────────────────────────────────────────────


def test_time_granularity_appears_in_column_list():
    model = SemanticModel(
        tables={
            "public.events": TableSemantic(
                display_name="Events",
                columns={
                    "event_date": ColumnSemantic(
                        display_name="Event Date",
                        semantic_type=SemanticType.DATE,
                        time_granularity=TimeGranularity.DAY,
                    )
                },
            )
        }
    )
    output = _fmt.format_for_prompt(model)
    assert "[grain: day]" in output


def test_time_granularity_appears_in_date_column_summary():
    model = SemanticModel(
        tables={
            "public.events": TableSemantic(
                display_name="Events",
                columns={
                    "event_month": ColumnSemantic(
                        display_name="Event Month",
                        semantic_type=SemanticType.DATE,
                        time_granularity=TimeGranularity.MONTH,
                    )
                },
            )
        }
    )
    output = _fmt.format_for_prompt(model)
    # Appears in both column list AND date column summary block
    assert output.count("[grain: month]") >= 1


def test_no_time_granularity_no_grain_tag():
    model = SemanticModel(
        tables={
            "public.events": TableSemantic(
                display_name="Events",
                columns={
                    "created_at": ColumnSemantic(
                        display_name="Created At",
                        semantic_type=SemanticType.TIMESTAMP,
                    )
                },
            )
        }
    )
    output = _fmt.format_for_prompt(model)
    assert "grain" not in output
