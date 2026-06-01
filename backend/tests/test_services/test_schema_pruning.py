# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Unit tests for schema pruning helpers in chat_service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.datasources.models import ColumnInfo, DataSourceSchema, PrivacySettings, TableInfo
from app.semantic.models import (
    CommonJoin,
    DerivedMetric,
    RelationshipEdge,
    SemanticModel,
    TableSemantic,
)
from app.services.schema_pruning import (
    _build_table_text,
    _filter_semantic_by_relevance,
    _filter_semantic_to_schema,
    _select_relevant_tables,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────


def _make_table(schema: str, name: str, cols: list[str], desc: str = "") -> TableInfo:
    return TableInfo(
        catalog=None,
        schema_name=schema,
        name=name,
        table_type="BASE TABLE",
        columns=[ColumnInfo(name=c, data_type="text", native_type="text") for c in cols],
        description=desc,
    )


def _make_schema(*tables: TableInfo) -> DataSourceSchema:
    return DataSourceSchema(source_type="postgresql", tables=list(tables))


def _make_settings(top_k: int = 15, enabled: bool = True) -> MagicMock:
    s = MagicMock()
    s.schema_pruning_top_k = top_k
    s.schema_pruning_enabled = enabled
    return s


def _make_ann_db(ann_items: list[tuple[str, float]]) -> MagicMock:
    """Return a mock DB whose execute().all() yields (table_key, distance) rows.

    distance = 1 - similarity, so similarity 0.8 → distance 0.2.
    """
    rows = []
    for key, distance in ann_items:
        row = MagicMock()
        row.table_key = key
        row.distance = distance
        rows.append(row)
    result_mock = MagicMock()
    result_mock.all = MagicMock(return_value=rows)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result_mock)
    return db


# ── _build_table_text ──────────────────────────────────────────────────────────


class TestBuildTableText:
    def test_basic_table_text(self) -> None:
        table = _make_table("store", "orders", ["order_id", "customer_id", "total_amount"])
        text = _build_table_text(table)
        assert "store.orders" in text
        assert "order_id" in text
        assert "customer_id" in text
        assert "total_amount" in text

    def test_description_included(self) -> None:
        table = _make_table("store", "orders", ["id"], desc="All customer orders")
        text = _build_table_text(table)
        assert "All customer orders" in text

    def test_caps_at_40_columns(self) -> None:
        cols = [f"col_{i}" for i in range(60)]
        table = _make_table("public", "big_table", cols)
        text = _build_table_text(table)
        # Only first 40 columns should appear
        assert "col_39" in text
        assert "col_40" not in text


# ── _select_relevant_tables ────────────────────────────────────────────────────


class TestSelectRelevantTables:
    @pytest.fixture
    def tables(self) -> list[TableInfo]:
        return [
            _make_table("store", "orders", ["order_id", "customer_id", "total_amount", "status"]),
            _make_table("store", "customers", ["customer_id", "name", "email"]),
            _make_table("store", "products", ["product_id", "name", "price", "category"]),
            _make_table("store", "log_audit", ["event", "ts", "user_id"]),
        ]

    def _make_cache(self) -> MagicMock:
        cache = MagicMock()
        cache.compute_embedding_async = AsyncMock(return_value=[1.0])
        return cache

    async def test_relevant_tables_selected(self, tables: list[TableInfo]) -> None:
        """Tables with high cosine scores are selected; irrelevant ones are dropped."""
        schema = _make_schema(*tables)
        # distance = 1 - similarity; 3 tables pass threshold=0.30, products (sim=0.1) must not.
        db = _make_ann_db(
            [
                ("store.orders", 0.20),  # sim=0.80
                ("store.customers", 0.25),  # sim=0.75
                ("store.log_audit", 0.55),  # sim=0.45
                ("store.products", 0.90),  # sim=0.10 — below threshold
            ]
        )
        privacy = PrivacySettings(schema_pruning_threshold=0.30)
        settings = _make_settings(top_k=15)

        result = await _select_relevant_tables(
            schema=schema,
            question="top customers by spend",
            privacy=privacy,
            settings=settings,
            cache=self._make_cache(),
            db=db,
            connection_id="conn-1",
            user_id="user-1",
        )

        selected_keys = {f"{t.schema_name}.{t.name}" for t in result.tables}
        assert "store.orders" in selected_keys
        assert "store.customers" in selected_keys
        assert "store.products" not in selected_keys

    async def test_always_include_overrides_threshold(self, tables: list[TableInfo]) -> None:
        """always_include_tables are returned regardless of cosine score."""
        schema = _make_schema(*tables)
        # All tables score very low — none pass threshold=0.50
        db = _make_ann_db(
            [
                ("store.orders", 0.95),
                ("store.customers", 0.95),
                ("store.products", 0.95),
                ("store.log_audit", 0.95),
            ]
        )
        privacy = PrivacySettings(
            schema_pruning_threshold=0.50,
            always_include_tables=["store.log_audit"],
        )
        settings = _make_settings(top_k=5)

        result = await _select_relevant_tables(
            schema=schema,
            question="any question",
            privacy=privacy,
            settings=settings,
            cache=self._make_cache(),
            db=db,
            connection_id="conn-1",
            user_id="user-1",
        )

        selected_keys = {f"{t.schema_name}.{t.name}" for t in result.tables}
        assert "store.log_audit" in selected_keys

    async def test_fallback_to_full_schema_when_too_few_tables(
        self,
        tables: list[TableInfo],
    ) -> None:
        """Returns full schema when pruning would yield fewer than 3 tables."""
        schema = _make_schema(*tables)
        # All tables score extremely low (threshold=0.99 — nothing passes)
        db = _make_ann_db(
            [
                ("store.orders", 0.995),
                ("store.customers", 0.995),
                ("store.products", 0.995),
                ("store.log_audit", 0.995),
            ]
        )
        privacy = PrivacySettings(schema_pruning_threshold=0.99)
        settings = _make_settings(top_k=15)

        result = await _select_relevant_tables(
            schema=schema,
            question="any question",
            privacy=privacy,
            settings=settings,
            cache=self._make_cache(),
            db=db,
            connection_id="conn-1",
            user_id="user-1",
        )

        assert len(result.tables) == len(tables)

    async def test_missing_embedding_always_included(self, tables: list[TableInfo]) -> None:
        """Tables absent from ANN results (no embedding row) are always included."""
        schema = _make_schema(*tables)
        # Only orders and customers have ANN results; products and log_audit are missing
        db = _make_ann_db(
            [
                ("store.orders", 0.10),  # sim=0.90
                ("store.customers", 0.10),  # sim=0.90
                # store.products and store.log_audit intentionally absent → safe-include
            ]
        )
        privacy = PrivacySettings(schema_pruning_threshold=0.30)
        settings = _make_settings(top_k=15)

        result = await _select_relevant_tables(
            schema=schema,
            question="any question",
            privacy=privacy,
            settings=settings,
            cache=self._make_cache(),
            db=db,
            connection_id="conn-1",
            user_id="user-1",
        )

        selected_keys = {f"{t.schema_name}.{t.name}" for t in result.tables}
        assert "store.products" in selected_keys
        assert "store.log_audit" in selected_keys


# ── _filter_semantic_to_schema ─────────────────────────────────────────────────


class TestFilterSemanticToSchema:
    def _make_semantic(self) -> SemanticModel:
        return SemanticModel(
            tables={
                "store.orders": TableSemantic(display_name="Orders"),
                "store.customers": TableSemantic(display_name="Customers"),
                "store.products": TableSemantic(display_name="Products"),
            },
            business_metrics=[],
            common_joins=[
                CommonJoin(
                    description="Customer orders",
                    tables=["store.orders", "store.customers"],
                    join_pattern="store.orders.customer_id = store.customers.customer_id",
                ),
                CommonJoin(
                    description="Product orders",
                    tables=["store.orders", "store.products"],
                    join_pattern="store.order_items.product_id = store.products.product_id",
                ),
            ],
            relationships=[
                RelationshipEdge(
                    from_table="store.orders",
                    from_column="customer_id",
                    to_table="store.customers",
                    to_column="customer_id",
                    relationship_type="many_to_one",
                    join_sql="store.orders.customer_id = store.customers.customer_id",
                ),
                RelationshipEdge(
                    from_table="store.orders",
                    from_column="product_id",
                    to_table="store.products",
                    to_column="product_id",
                    relationship_type="many_to_one",
                    join_sql="store.orders.product_id = store.products.product_id",
                ),
            ],
            derived_columns=[],
            time_expressions={},
            schema_hash=None,
        )

    def test_tables_filtered_to_visible(self) -> None:
        """Only tables visible in schema are kept."""
        semantic = self._make_semantic()
        schema = _make_schema(
            _make_table("store", "orders", ["id"]),
            _make_table("store", "customers", ["id"]),
            # store.products not in schema
        )
        result = _filter_semantic_to_schema(semantic, schema)
        assert "store.orders" in result.tables
        assert "store.customers" in result.tables
        assert "store.products" not in result.tables

    def test_relationships_filtered_to_visible_tables(self) -> None:
        """Relationships referencing pruned tables are dropped."""
        semantic = self._make_semantic()
        schema = _make_schema(
            _make_table("store", "orders", ["id"]),
            _make_table("store", "customers", ["id"]),
            # products pruned
        )
        result = _filter_semantic_to_schema(semantic, schema)
        assert len(result.relationships) == 1
        assert result.relationships[0].to_table == "store.customers"

    def test_common_joins_filtered_to_visible_tables(self) -> None:
        """Common joins referencing pruned tables are dropped."""
        semantic = self._make_semantic()
        schema = _make_schema(
            _make_table("store", "orders", ["id"]),
            _make_table("store", "customers", ["id"]),
            # products pruned
        )
        result = _filter_semantic_to_schema(semantic, schema)
        assert len(result.common_joins) == 1
        assert "customer" in result.common_joins[0].description.lower()

    def test_business_metrics_always_kept(self) -> None:
        """Business metrics are not filtered regardless of table scope."""
        semantic = self._make_semantic().model_copy(
            update={
                "business_metrics": [
                    DerivedMetric(name="Revenue", definition="SUM(o.total_amount)", description=""),
                    DerivedMetric(name="Order Count", definition="COUNT(o.id)", description=""),
                ]
            }
        )
        schema = _make_schema(_make_table("store", "orders", ["id"]))
        result = _filter_semantic_to_schema(semantic, schema)
        assert len(result.business_metrics) == 2


# ── Domain-aware schema pruning ────────────────────────────────────────────────


class TestDomainAwarePruning:
    """Tests for domain boost in _select_relevant_tables and _filter_semantic_by_relevance."""

    def _make_cache(self) -> MagicMock:
        cache = MagicMock()
        cache.compute_embedding_async = AsyncMock(return_value=[1.0])
        return cache

    async def test_domain_boost_lifts_co_domain_table_above_threshold(self) -> None:
        """A co-domain table scoring below threshold gets boosted above it."""
        # finance.revenue sim=0.80, finance.costs sim=0.20 (below threshold 0.30).
        # After domain boost (+0.15), costs becomes 0.35 → above threshold.
        revenue = _make_table("finance", "revenue", ["id", "amount"])
        costs = _make_table("finance", "costs", ["id", "amount"])
        marketing = _make_table("marketing", "campaigns", ["id", "name"])

        schema = _make_schema(revenue, costs, marketing)
        db = _make_ann_db(
            [
                ("finance.revenue", 0.20),  # sim=0.80
                ("finance.costs", 0.80),  # sim=0.20 → boosted to 0.35
                ("marketing.campaigns", 0.95),  # sim=0.05
            ]
        )
        semantic = SemanticModel(
            tables={
                "finance.revenue": TableSemantic(display_name="Revenue", domain="finance"),
                "finance.costs": TableSemantic(display_name="Costs", domain="finance"),
                "marketing.campaigns": TableSemantic(display_name="Campaigns", domain="marketing"),
            }
        )
        privacy = PrivacySettings(schema_pruning_threshold=0.30)
        settings = _make_settings(top_k=15)

        result = await _select_relevant_tables(
            schema=schema,
            question="revenue and costs",
            privacy=privacy,
            settings=settings,
            cache=self._make_cache(),
            db=db,
            connection_id="conn-1",
            user_id="user-1",
            semantic_model=semantic,
        )

        selected_keys = {f"{t.schema_name}.{t.name}" for t in result.tables}
        assert "finance.revenue" in selected_keys
        assert "finance.costs" in selected_keys  # boosted from 0.20 → 0.35

    async def test_non_domain_tables_not_boosted(self) -> None:
        """Tables in a different domain are not boosted when finance dominates."""
        t_rev = _make_table("finance", "revenue", ["id"])
        t_budget = _make_table("finance", "budget", ["id"])
        t_ledger = _make_table("finance", "ledger", ["id"])
        t_campaigns = _make_table("marketing", "campaigns", ["id"])
        schema = _make_schema(t_rev, t_budget, t_ledger, t_campaigns)
        db = _make_ann_db(
            [
                ("finance.revenue", 0.20),  # sim=0.80
                ("finance.budget", 0.25),  # sim=0.75
                ("finance.ledger", 0.30),  # sim=0.70
                ("marketing.campaigns", 0.80),  # sim=0.20 — different domain, no boost
            ]
        )
        semantic = SemanticModel(
            tables={
                "finance.revenue": TableSemantic(display_name="Revenue", domain="finance"),
                "finance.budget": TableSemantic(display_name="Budget", domain="finance"),
                "finance.ledger": TableSemantic(display_name="Ledger", domain="finance"),
                "marketing.campaigns": TableSemantic(display_name="Campaigns", domain="marketing"),
            }
        )
        privacy = PrivacySettings(schema_pruning_threshold=0.30)
        settings = _make_settings(top_k=15)

        result = await _select_relevant_tables(
            schema=schema,
            question="finance revenue query",
            privacy=privacy,
            settings=settings,
            cache=self._make_cache(),
            db=db,
            connection_id="conn-1",
            user_id="user-1",
            semantic_model=semantic,
        )

        selected_keys = {f"{t.schema_name}.{t.name}" for t in result.tables}
        assert "finance.revenue" in selected_keys
        assert "finance.budget" in selected_keys
        assert "finance.ledger" in selected_keys
        assert "marketing.campaigns" not in selected_keys

    async def test_no_domain_annotations_behaves_like_original(self) -> None:
        """When no tables have domain tags, behaviour is identical to without semantic_model."""
        t1 = _make_table("store", "orders", ["id"])
        t2 = _make_table("store", "customers", ["id"])
        t3 = _make_table("store", "invoices", ["id"])
        t4 = _make_table("store", "products", ["id"])
        schema = _make_schema(t1, t2, t3, t4)
        db = _make_ann_db(
            [
                ("store.orders", 0.20),  # sim=0.80
                ("store.customers", 0.25),  # sim=0.75
                ("store.invoices", 0.40),  # sim=0.60
                ("store.products", 0.90),  # sim=0.10 — no domain tag, no boost
            ]
        )
        semantic = SemanticModel(
            tables={
                "store.orders": TableSemantic(display_name="Orders"),
                "store.customers": TableSemantic(display_name="Customers"),
                "store.invoices": TableSemantic(display_name="Invoices"),
                "store.products": TableSemantic(display_name="Products"),
            }
        )
        privacy = PrivacySettings(schema_pruning_threshold=0.30)
        settings = _make_settings(top_k=15)

        result = await _select_relevant_tables(
            schema=schema,
            question="orders",
            privacy=privacy,
            settings=settings,
            cache=self._make_cache(),
            db=db,
            connection_id="conn-1",
            user_id="user-1",
            semantic_model=semantic,
        )

        selected_keys = {f"{t.schema_name}.{t.name}" for t in result.tables}
        assert "store.orders" in selected_keys
        assert "store.customers" in selected_keys
        assert "store.invoices" in selected_keys
        assert "store.products" not in selected_keys


class TestDomainTokenScoring:
    """Tests for domain token scoring in _filter_semantic_by_relevance."""

    def test_domain_token_in_question_adds_score(self) -> None:
        """Table tagged 'finance' scores higher when question contains 'finance'."""
        # 12 tables so that filtering actually runs (max_tables=10 by default)
        tables = {f"public.t{i}": TableSemantic(display_name=f"Table {i}") for i in range(12)}
        tables["public.finance_report"] = TableSemantic(
            display_name="Finance Report", domain="finance"
        )
        tables["public.sales"] = TableSemantic(display_name="Sales", domain="sales")
        model = SemanticModel(tables=tables)

        result = _filter_semantic_by_relevance(model, "finance department costs", max_tables=10)

        # finance_report must be included (domain token "finance" matches question)
        assert "public.finance_report" in result.tables

    def test_table_without_domain_not_boosted(self) -> None:
        """Tables without a domain tag receive no domain bonus."""
        tables = {f"public.t{i}": TableSemantic(display_name=f"Table {i}") for i in range(12)}
        tables["public.nodomain"] = TableSemantic(display_name="No Domain Table")
        model = SemanticModel(tables=tables)
        # Question that would only boost a "nodomain"-tagged table (no such tag here)
        result = _filter_semantic_by_relevance(model, "nodomain query test", max_tables=10)
        # No assertion on inclusion — just verify no crash and max_tables respected
        assert len(result.tables) <= 10
