# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for _filter_semantic_by_relevance — query-aware semantic model pruning."""

from __future__ import annotations

from app.semantic.models import (
    ColumnSemantic,
    RelationshipEdge,
    RelationshipType,
    SemanticModel,
    TableSemantic,
)
from app.services.schema_pruning import _filter_semantic_by_relevance


def _make_model(*table_keys: str) -> SemanticModel:
    """Build a SemanticModel with the given table keys and no columns."""
    return SemanticModel(
        tables={k: TableSemantic(display_name=k.split(".")[-1].title()) for k in table_keys}
    )


def _make_model_with_columns(**tables: dict) -> SemanticModel:
    """Build a model where each key maps to a dict of col_name → display_name."""
    tbl_map = {}
    for tbl_key, cols in tables.items():
        tbl_map[tbl_key] = TableSemantic(
            display_name=tbl_key.split(".")[-1].title(),
            columns={
                col_name: ColumnSemantic(display_name=display_name)
                for col_name, display_name in cols.items()
            },
        )
    return SemanticModel(tables=tbl_map)


# ── Small schema passthrough ───────────────────────────────────────────────────


class TestSmallSchemaPassthrough:
    def test_model_unchanged_when_table_count_le_max(self):
        model = _make_model("public.orders", "public.customers")
        result = _filter_semantic_by_relevance(model, "show me orders", max_tables=10)
        assert set(result.tables.keys()) == {"public.orders", "public.customers"}

    def test_model_unchanged_on_empty_question(self):
        model = _make_model("public.orders", "public.customers", "public.products")
        result = _filter_semantic_by_relevance(model, "", max_tables=2)
        assert set(result.tables.keys()) == {"public.orders", "public.customers", "public.products"}


# ── Table name matching ────────────────────────────────────────────────────────


class TestTableNameMatching:
    def test_exact_table_name_in_question_included(self):
        model = _make_model(
            "public.orders", "public.customers", "public.products", "public.shipments"
        )
        result = _filter_semantic_by_relevance(model, "show me orders", max_tables=1)
        assert "public.orders" in result.tables

    def test_unrelated_tables_excluded(self):
        model = _make_model(
            "public.orders", "public.customers", "public.products", "public.shipments"
        )
        result = _filter_semantic_by_relevance(model, "show me orders", max_tables=1)
        assert "public.customers" not in result.tables

    def test_compound_table_name_parts_matched(self):
        # "order" appears in "order_items"
        model = _make_model(
            "public.order_items", "public.categories", "public.suppliers", "public.warehouses"
        )
        result = _filter_semantic_by_relevance(model, "show me order items", max_tables=1)
        assert "public.order_items" in result.tables


# ── Column name matching ───────────────────────────────────────────────────────


class TestColumnNameMatching:
    def test_column_name_in_question_boosts_table_score(self):
        model = _make_model_with_columns(
            **{
                "public.orders": {"revenue": "Revenue", "id": "ID"},
                "public.customers": {"name": "Name", "id": "ID"},
                "public.products": {"sku": "SKU", "id": "ID"},
                "public.shipments": {"tracking": "Tracking", "id": "ID"},
            }
        )
        result = _filter_semantic_by_relevance(model, "what is the total revenue", max_tables=1)
        assert "public.orders" in result.tables


# ── Relationship hop expansion ─────────────────────────────────────────────────


class TestRelationshipHopExpansion:
    def test_fk_partner_included_after_hop(self):
        model = SemanticModel(
            tables={
                "public.orders": TableSemantic(display_name="Orders"),
                "public.customers": TableSemantic(display_name="Customers"),
                "public.products": TableSemantic(display_name="Products"),
                "public.shipments": TableSemantic(display_name="Shipments"),
                "public.categories": TableSemantic(display_name="Categories"),
            },
            relationships=[
                RelationshipEdge(
                    from_table="public.orders",
                    from_column="customer_id",
                    to_table="public.customers",
                    to_column="id",
                    relationship_type=RelationshipType.MANY_TO_ONE,
                    join_sql="public.orders.customer_id = public.customers.id",
                )
            ],
        )
        # Direct question about orders — customers should be pulled in via FK hop
        result = _filter_semantic_by_relevance(model, "show me orders", max_tables=1)
        assert "public.orders" in result.tables
        assert "public.customers" in result.tables  # FK partner included

    def test_unrelated_table_not_included_after_hop(self):
        model = SemanticModel(
            tables={
                "public.orders": TableSemantic(display_name="Orders"),
                "public.customers": TableSemantic(display_name="Customers"),
                "public.products": TableSemantic(display_name="Products"),
                "public.shipments": TableSemantic(display_name="Shipments"),
                "public.categories": TableSemantic(display_name="Categories"),
            },
            relationships=[
                RelationshipEdge(
                    from_table="public.orders",
                    from_column="customer_id",
                    to_table="public.customers",
                    to_column="id",
                    relationship_type=RelationshipType.MANY_TO_ONE,
                    join_sql="public.orders.customer_id = public.customers.id",
                )
            ],
        )
        result = _filter_semantic_by_relevance(model, "show me orders", max_tables=1)
        assert "public.products" not in result.tables
        assert "public.categories" not in result.tables

    def test_relationships_filtered_to_included_tables(self):
        model = SemanticModel(
            tables={
                "public.orders": TableSemantic(display_name="Orders"),
                "public.customers": TableSemantic(display_name="Customers"),
                "public.products": TableSemantic(display_name="Products"),
                "public.categories": TableSemantic(display_name="Categories"),
            },
            relationships=[
                RelationshipEdge(
                    from_table="public.orders",
                    from_column="customer_id",
                    to_table="public.customers",
                    to_column="id",
                    relationship_type=RelationshipType.MANY_TO_ONE,
                    join_sql="public.orders.customer_id = public.customers.id",
                ),
                RelationshipEdge(
                    from_table="public.products",
                    from_column="category_id",
                    to_table="public.categories",
                    to_column="id",
                    relationship_type=RelationshipType.MANY_TO_ONE,
                    join_sql="public.products.category_id = public.categories.id",
                ),
            ],
        )
        result = _filter_semantic_by_relevance(model, "show me orders", max_tables=1)
        # Only the orders→customers relationship should survive
        assert len(result.relationships) == 1
        assert result.relationships[0].from_table == "public.orders"
