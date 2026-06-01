# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for semantic/models.py — Pydantic v2 model construction and validation."""

from __future__ import annotations

from app.semantic.models import (
    AggregationType,
    BusinessMetric,
    ColumnSemantic,
    ConversionMetric,
    DerivedColumn,
    FormatHint,
    GlobalSectionsResponse,
    RatioMetric,
    RelationshipEdge,
    RelationshipType,
    SemanticModel,
    SemanticType,
    SimpleMetric,
    TablesBatchResponse,
    TableSemantic,
    TimeGranularity,
)

# ── ColumnSemantic validators ──────────────────────────────────────────────────


def test_column_semantic_unknown_semantic_type_coerced():
    col = ColumnSemantic(display_name="x", semantic_type="totally_unknown_value")
    assert col.semantic_type == SemanticType.UNKNOWN


def test_column_semantic_valid_semantic_type_preserved():
    col = ColumnSemantic(display_name="x", semantic_type="monetary")
    assert col.semantic_type == SemanticType.MONETARY


def test_column_semantic_unknown_aggregation_coerced_to_none():
    col = ColumnSemantic(display_name="x", default_aggregation="avg_not_real")
    assert col.default_aggregation is None


def test_column_semantic_valid_aggregation_preserved():
    col = ColumnSemantic(display_name="x", default_aggregation="sum")
    assert col.default_aggregation == AggregationType.SUM


# ── TableSemantic validators ───────────────────────────────────────────────────


def test_table_semantic_filters_non_dict_columns():
    tbl = TableSemantic(
        display_name="t",
        columns={"good": {"display_name": "G"}, "bad": "not_a_dict"},
    )
    assert "good" in tbl.columns
    assert "bad" not in tbl.columns


def test_table_semantic_coerces_non_string_filters():
    tbl = TableSemantic(
        display_name="t",
        default_filters=["status = 'A'", {"key": "value"}],
    )
    assert tbl.default_filters[0] == "status = 'A'"
    assert '"key"' in tbl.default_filters[1]


def test_table_semantic_filters_invalid_hierarchies():
    tbl = TableSemantic(
        display_name="t",
        hierarchies=[
            {"name": "Date", "levels": ["year", "month"]},
            {"levels": ["a"]},  # no name → dropped
            "not_a_dict",
        ],
    )
    assert len(tbl.hierarchies) == 1
    assert tbl.hierarchies[0].name == "Date"


# ── BusinessMetric discriminated union ────────────────────────────────────────


def test_simple_metric_round_trip():
    m = SimpleMetric(
        name="Revenue",
        description="Total revenue",
        definition="SUM(orders.total)",
        aggregation=AggregationType.SUM,
    )
    assert m.metric_type.value == "simple"
    dumped = m.model_dump(mode="json")
    restored = SimpleMetric.model_validate(dumped)
    assert restored.name == "Revenue"


def test_ratio_metric_round_trip():
    m = RatioMetric(
        name="CVR",
        description="Conversion rate",
        numerator_expr="COUNT(DISTINCT orders.id)",
        denominator_expr="COUNT(DISTINCT sessions.id)",
    )
    data = m.model_dump(mode="json")
    assert data["metric_type"] == "ratio"
    r2 = RatioMetric.model_validate(data)
    assert r2.numerator_expr == m.numerator_expr


def test_business_metric_discriminated_union_simple():
    raw = {
        "metric_type": "simple",
        "name": "Count",
        "description": "Row count",
        "definition": "COUNT(*)",
        "aggregation": "count",
    }
    m: BusinessMetric = SimpleMetric.model_validate(raw)  # type: ignore[assignment]
    assert isinstance(m, SimpleMetric)


def test_business_metric_discriminated_union_ratio():
    raw = {
        "metric_type": "ratio",
        "name": "Rate",
        "description": "A ratio",
        "numerator_expr": "a",
        "denominator_expr": "b",
    }
    from pydantic import TypeAdapter

    ta = TypeAdapter(BusinessMetric)
    m = ta.validate_python(raw)
    assert isinstance(m, RatioMetric)


# ── RelationshipEdge.is_required round-trip ───────────────────────────────────


def test_is_required_true_round_trip():
    edge = RelationshipEdge(
        from_table="orders",
        from_column="customer_id",
        to_table="customers",
        to_column="id",
        join_sql="orders.customer_id = customers.id",
        relationship_type=RelationshipType.MANY_TO_ONE,
        is_required=True,
    )
    data = edge.model_dump(mode="json")
    assert data["is_required"] is True
    r2 = RelationshipEdge.model_validate(data)
    assert r2.is_required is True


def test_is_required_false_default():
    edge = RelationshipEdge(
        from_table="a",
        from_column="b_id",
        to_table="b",
        to_column="id",
        join_sql="a.b_id = b.id",
    )
    assert edge.is_required is False


# ── TablesBatchResponse ────────────────────────────────────────────────────────


def test_tables_batch_response_filters_non_dict_tables():
    r = TablesBatchResponse.model_validate(
        {
            "tables": {
                "public.orders": {"display_name": "Orders"},
                "public.bad": "not_a_dict",
            }
        }
    )
    assert "public.orders" in r.tables
    assert "public.bad" not in r.tables


def test_tables_batch_response_empty_on_non_dict_input():
    r = TablesBatchResponse.model_validate({"tables": "oops"})
    assert r.tables == {}


# ── GlobalSectionsResponse ────────────────────────────────────────────────────


def test_global_sections_filters_metric_missing_definition():
    raw = {
        "business_metrics": [
            # valid simple metric
            {
                "metric_type": "simple",
                "name": "Revenue",
                "description": "Total",
                "definition": "SUM(x)",
                "aggregation": "sum",
            },
            # missing definition → dropped
            {
                "metric_type": "simple",
                "name": "Bad",
                "description": "Missing def",
                "aggregation": "sum",
            },
        ],
        "segments": [],
        "derived_columns": [],
        "common_joins": [],
    }
    r = GlobalSectionsResponse.model_validate(raw)
    assert len(r.business_metrics) == 1
    assert r.business_metrics[0].name == "Revenue"


def test_global_sections_coerces_unknown_aggregation():
    raw = {
        "business_metrics": [
            {
                "metric_type": "simple",
                "name": "M",
                "description": "D",
                "definition": "SUM(x)",
                "aggregation": "mean",  # invalid → coerced to "count"
            }
        ],
        "segments": [],
        "derived_columns": [],
        "common_joins": [],
    }
    r = GlobalSectionsResponse.model_validate(raw)
    assert len(r.business_metrics) == 1
    m = r.business_metrics[0]
    assert isinstance(m, SimpleMetric)
    assert m.aggregation == AggregationType.COUNT


def test_global_sections_filters_ratio_missing_exprs():
    raw = {
        "business_metrics": [
            {
                "metric_type": "ratio",
                "name": "Rate",
                "description": "D",
                # missing numerator_expr / denominator_expr → dropped
            }
        ],
        "segments": [],
        "derived_columns": [],
        "common_joins": [],
    }
    r = GlobalSectionsResponse.model_validate(raw)
    assert r.business_metrics == []


def test_global_sections_filters_invalid_segments():
    raw = {
        "business_metrics": [],
        "segments": [
            {"name": "active", "sql_expression": "status = 'A'"},
            {"name": "no_expr"},  # missing sql_expression → dropped
            "not_a_dict",
        ],
        "derived_columns": [],
        "common_joins": [],
    }
    r = GlobalSectionsResponse.model_validate(raw)
    assert len(r.segments) == 1
    assert r.segments[0].name == "active"


def test_global_sections_filters_derived_missing_sql():
    raw = {
        "business_metrics": [],
        "segments": [],
        "derived_columns": [
            {"name": "Margin", "sql_expression": "(a-b)/b"},
            {"name": "NoSQL"},  # missing sql_expression → dropped
        ],
        "common_joins": [],
    }
    r = GlobalSectionsResponse.model_validate(raw)
    assert len(r.derived_columns) == 1


def test_global_sections_filters_joins_missing_join_pattern():
    raw = {
        "business_metrics": [],
        "segments": [],
        "derived_columns": [],
        "common_joins": [
            {"description": "Orders→Customers", "join_pattern": "a.id = b.id"},
            {"description": "Missing pattern"},  # dropped
        ],
    }
    r = GlobalSectionsResponse.model_validate(raw)
    assert len(r.common_joins) == 1


# ── SemanticModel round-trip ──────────────────────────────────────────────────


def test_semantic_model_round_trip():
    m = SemanticModel(
        tables={
            "public.orders": TableSemantic(
                display_name="Orders",
                columns={
                    "id": ColumnSemantic(display_name="ID", semantic_type=SemanticType.IDENTIFIER)
                },
            )
        },
        business_metrics=[
            SimpleMetric(
                name="Revenue",
                description="Total",
                definition="SUM(total)",
                aggregation=AggregationType.SUM,
            )
        ],
    )
    dumped = m.model_dump(mode="json")
    restored = SemanticModel.model_validate(dumped)
    assert "public.orders" in restored.tables
    assert len(restored.business_metrics) == 1
    assert isinstance(restored.business_metrics[0], SimpleMetric)


def test_derived_column_invalid_format_hint_coerced_to_none():
    dc = DerivedColumn(name="X", sql_expression="a+b", format_hint="bad_hint")
    assert dc.format_hint is None


def test_derived_column_valid_format_hint_preserved():
    dc = DerivedColumn(name="X", sql_expression="a+b", format_hint="percentage")
    assert dc.format_hint == FormatHint.PERCENTAGE


# ── ColumnSemantic — new fields ───────────────────────────────────────────────


def test_column_is_non_additive_defaults_false():
    col = ColumnSemantic(display_name="x")
    assert col.is_non_additive is False


def test_column_is_non_additive_true_round_trips():
    col = ColumnSemantic(display_name="x", is_non_additive=True)
    dumped = col.model_dump(mode="json")
    assert dumped["is_non_additive"] is True
    restored = ColumnSemantic.model_validate(dumped)
    assert restored.is_non_additive is True


def test_column_time_granularity_valid_string_coerced():
    col = ColumnSemantic(display_name="x", time_granularity="day")
    assert col.time_granularity == TimeGranularity.DAY


def test_column_time_granularity_invalid_string_coerced_to_none():
    col = ColumnSemantic(display_name="x", time_granularity="fortnight")
    assert col.time_granularity is None


def test_column_time_granularity_none_stays_none():
    col = ColumnSemantic(display_name="x")
    assert col.time_granularity is None


# ── ConversionMetric ──────────────────────────────────────────────────────────


def test_conversion_metric_valid_round_trip():
    m = ConversionMetric(
        name="Signup CVR",
        description="Sessions that convert to signups",
        base_measure="sessions",
        conversion_measure="signups",
        entity="user_id",
        window="7 days",
    )
    assert m.metric_type.value == "conversion"
    dumped = m.model_dump(mode="json")
    restored = ConversionMetric.model_validate(dumped)
    assert restored.base_measure == "sessions"
    assert restored.conversion_measure == "signups"
    assert restored.calculation == "conversion_rate"


def test_conversion_metric_discriminated_union():
    from pydantic import TypeAdapter

    raw = {
        "metric_type": "conversion",
        "name": "Purchase CVR",
        "description": "",
        "base_measure": "visits",
        "conversion_measure": "purchases",
    }
    ta = TypeAdapter(BusinessMetric)
    m = ta.validate_python(raw)
    assert isinstance(m, ConversionMetric)


def test_global_sections_filters_conversion_missing_base_measure():
    raw = {
        "business_metrics": [
            {
                "metric_type": "conversion",
                "name": "Bad CVR",
                "description": "",
                # missing base_measure → dropped
                "conversion_measure": "signups",
            }
        ],
        "segments": [],
        "derived_columns": [],
        "common_joins": [],
    }
    r = GlobalSectionsResponse.model_validate(raw)
    assert r.business_metrics == []


def test_global_sections_filters_conversion_missing_conversion_measure():
    raw = {
        "business_metrics": [
            {
                "metric_type": "conversion",
                "name": "Bad CVR",
                "description": "",
                "base_measure": "sessions",
                # missing conversion_measure → dropped
            }
        ],
        "segments": [],
        "derived_columns": [],
        "common_joins": [],
    }
    r = GlobalSectionsResponse.model_validate(raw)
    assert r.business_metrics == []


def test_global_sections_valid_conversion_metric_passes():
    raw = {
        "business_metrics": [
            {
                "metric_type": "conversion",
                "name": "Signup CVR",
                "description": "Sessions that convert to a signup",
                "base_measure": "sessions",
                "conversion_measure": "signups",
            }
        ],
        "segments": [],
        "derived_columns": [],
        "common_joins": [],
    }
    r = GlobalSectionsResponse.model_validate(raw)
    assert len(r.business_metrics) == 1
    assert isinstance(r.business_metrics[0], ConversionMetric)


# ── SimpleMetric / CumulativeMetric measure_filters ───────────────────────────


def test_simple_metric_measure_filters_default_empty():
    m = SimpleMetric(
        name="Revenue",
        description="",
        definition="SUM(orders.total)",
        aggregation=AggregationType.SUM,
    )
    assert m.measure_filters == []


def test_simple_metric_measure_filters_round_trip():
    m = SimpleMetric(
        name="Revenue",
        description="",
        definition="SUM(orders.total)",
        aggregation=AggregationType.SUM,
        measure_filters=["status = 'completed'"],
    )
    dumped = m.model_dump(mode="json")
    restored = SimpleMetric.model_validate(dumped)
    assert restored.measure_filters == ["status = 'completed'"]
