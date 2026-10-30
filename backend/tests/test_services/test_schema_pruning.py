# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Unit tests for schema pruning helpers in chat_service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.datasources.models import ColumnInfo, DataSourceSchema, PrivacySettings, TableInfo
from app.semantic.formatter import SemanticFormatter
from app.semantic.models import (
    ColumnSemantic,
    CommonJoin,
    DerivedMetric,
    RelationshipEdge,
    SemanticModel,
    TableSemantic,
)
from app.services import schema_pruning
from app.services.schema_pruning import (
    _build_table_text,
    _filter_semantic_by_relevance,
    _filter_semantic_to_schema,
    _resolve_schema,
    _select_relevant_tables,
    clear_semantic_filter_caches,
)
from app.services.schema_utils import _apply_privacy_to_schema, _schema_to_dict

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
            _make_table("store", "orders", ["id", "customer_id"]),
            _make_table("store", "customers", ["id", "customer_id"]),
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


class TestFilterSemanticToSchemaPrivacy:
    """The stored model may annotate columns the privacy settings hide.

    ``/generate/globals`` keeps those annotations so un-hiding loses no work, so the
    chat path must narrow columns, not only tables, to the privacy-filtered schema.
    """

    def _model(self) -> SemanticModel:
        return SemanticModel(
            tables={
                "store.orders": TableSemantic(
                    display_name="Orders",
                    primary_timestamp_column="secret_ts",
                    partition_columns=["secret_ts"],
                    columns={
                        "id": ColumnSemantic(display_name="Order ID", description="Order key"),
                        "amount": ColumnSemantic(
                            display_name="Amount", description="Order value in euros"
                        ),
                        "secret_ts": ColumnSemantic(
                            display_name="Hidden Stamp",
                            description="Zorblax confidential moment",
                        ),
                    },
                ),
                "store.customers": TableSemantic(
                    display_name="Customers",
                    columns={"id": ColumnSemantic(display_name="Customer ID")},
                ),
            },
            relationships=[
                RelationshipEdge(
                    from_table="store.orders",
                    from_column="secret_ts",
                    to_table="store.customers",
                    to_column="id",
                    join_sql="store.orders.secret_ts = store.customers.id",
                ),
                RelationshipEdge(
                    from_table="store.orders",
                    from_column="id",
                    to_table="store.customers",
                    to_column="id",
                    join_sql="store.orders.id = store.customers.id",
                ),
            ],
        )

    def _schema_without_secret(self) -> DataSourceSchema:
        return _make_schema(
            _make_table("store", "orders", ["id", "amount"]),
            _make_table("store", "customers", ["id"]),
        )

    def test_excluded_column_never_reaches_the_prompt(self) -> None:
        result = _filter_semantic_to_schema(self._model(), self._schema_without_secret())

        prompt = SemanticFormatter().format_for_prompt(result)

        # Sanity: the visible parts of the same table are rendered.
        assert "Order value in euros" in prompt
        for leaked in ("secret_ts", "Hidden Stamp", "Zorblax"):
            assert leaked not in prompt

    def test_excluded_column_is_dropped_with_its_references_and_edges(self) -> None:
        result = _filter_semantic_to_schema(self._model(), self._schema_without_secret())

        orders = result.tables["store.orders"]
        assert set(orders.columns) == {"id", "amount"}
        assert orders.primary_timestamp_column is None
        assert orders.partition_columns == []
        assert [(r.from_column, r.to_column) for r in result.relationships] == [("id", "id")]

    def test_column_present_in_schema_is_kept(self) -> None:
        schema = _make_schema(
            _make_table("store", "orders", ["id", "amount", "secret_ts"]),
            _make_table("store", "customers", ["id"]),
        )

        result = _filter_semantic_to_schema(self._model(), schema)

        orders = result.tables["store.orders"]
        assert set(orders.columns) == {"id", "amount", "secret_ts"}
        assert orders.primary_timestamp_column == "secret_ts"
        assert len(result.relationships) == 2
        prompt = SemanticFormatter().format_for_prompt(result)
        assert "Zorblax confidential moment" in prompt

    def test_privacy_excluded_column_is_dropped_even_from_a_stale_schema(self) -> None:
        """A stale schema cache can still hold a newly excluded column. The chat path
        narrows the schema with ``_apply_privacy_to_schema`` before this filter sees
        it, so the narrowed schema is what the filter — and column validation — get."""
        stale_schema = _make_schema(
            _make_table("store", "orders", ["id", "amount", "secret_ts"]),
            _make_table("store", "customers", ["id"]),
        )
        privacy = PrivacySettings(excluded_columns=["store.orders.secret_ts"])
        narrowed = _apply_privacy_to_schema(stale_schema, privacy)

        result = _filter_semantic_to_schema(self._model(), narrowed)

        orders = result.tables["store.orders"]
        assert set(orders.columns) == {"id", "amount"}
        assert orders.primary_timestamp_column is None
        assert orders.partition_columns == []
        assert [(r.from_column, r.to_column) for r in result.relationships] == [("id", "id")]
        prompt = SemanticFormatter().format_for_prompt(result)
        assert "Order value in euros" in prompt
        for leaked in ("secret_ts", "Hidden Stamp", "Zorblax"):
            assert leaked not in prompt
        # The caller's schema is not narrowed in place.
        assert [c.name for c in stale_schema.tables[0].columns] == ["id", "amount", "secret_ts"]

    def test_a_column_the_schema_still_has_is_kept(self) -> None:
        """Exactness lives in ``_apply_privacy_to_schema``; this filter trusts the
        schema it is given, so a column present there (``secret`` is only a
        sensitive-name pattern, not an exclusion) stays."""
        schema = _make_schema(
            _make_table("store", "orders", ["id", "amount", "secret_ts"]),
            _make_table("store", "customers", ["id"]),
        )
        result = _filter_semantic_to_schema(self._model(), schema)

        assert "secret_ts" in result.tables["store.orders"].columns

    def test_edge_dropped_on_a_column_mismatch_is_logged(self, caplog) -> None:
        clear_semantic_filter_caches()
        with caplog.at_level("WARNING", logger="app.services.schema_pruning"):
            _filter_semantic_to_schema(self._model(), self._schema_without_secret())

        messages = [r.getMessage() for r in caplog.records]
        assert len(messages) == 1
        assert "store.orders.secret_ts -> store.customers.id" in messages[0]

    def test_edge_dropped_for_an_invisible_table_is_not_logged(self, caplog) -> None:
        clear_semantic_filter_caches()
        schema = _make_schema(_make_table("store", "orders", ["id", "amount", "secret_ts"]))

        with caplog.at_level("WARNING", logger="app.services.schema_pruning"):
            result = _filter_semantic_to_schema(self._model(), schema)

        assert result.relationships == []
        assert caplog.records == []

    def test_input_model_is_not_mutated(self) -> None:
        model = self._model()

        _filter_semantic_to_schema(model, self._schema_without_secret())

        assert "secret_ts" in model.tables["store.orders"].columns
        assert model.tables["store.orders"].primary_timestamp_column == "secret_ts"
        assert len(model.relationships) == 2

    def test_narrowing_is_memoised_on_the_model_instance_and_schema(self) -> None:
        clear_semantic_filter_caches()
        model = self._model()
        schema = self._schema_without_secret()
        first = _filter_semantic_to_schema(model, schema)
        assert _filter_semantic_to_schema(model, self._schema_without_secret()) is first
        other = _make_schema(_make_table("store", "orders", ["id"]))
        assert _filter_semantic_to_schema(model, other) is not first
        assert _filter_semantic_to_schema(self._model(), schema) is not first

    def test_a_dropped_edge_is_warned_about_once(self, caplog) -> None:
        clear_semantic_filter_caches()
        with caplog.at_level("WARNING", logger="app.services.schema_pruning"):
            _filter_semantic_to_schema(self._model(), self._schema_without_secret())
            clear_semantic_filter_caches(narrowed_only=True)
            _filter_semantic_to_schema(self._model(), self._schema_without_secret())
        assert len([r for r in caplog.records if "Dropped relationship" in r.getMessage()]) == 1

    def test_the_narrowed_cache_evicts_the_least_recently_used_entry(self, monkeypatch) -> None:
        monkeypatch.setattr(schema_pruning, "_NARROWED_CACHE_SIZE", 2)
        model = self._model()
        one = _make_schema(_make_table("store", "orders", ["id"]))
        two = _make_schema(_make_table("store", "orders", ["amount"]))
        three = _make_schema(_make_table("store", "customers", ["id"]))
        first = _filter_semantic_to_schema(model, one)
        _filter_semantic_to_schema(model, two)
        assert _filter_semantic_to_schema(model, one) is first  # now most recent
        _filter_semantic_to_schema(model, three)  # evicts ``two``
        assert len(schema_pruning._narrowed_cache) == 2
        assert _filter_semantic_to_schema(model, one) is first
        assert all(
            key[1] != schema_pruning._schema_signature(two)
            for key in schema_pruning._narrowed_cache
        )

    def test_the_warned_edge_set_resets_at_its_cap(self, monkeypatch, caplog) -> None:
        """At the cap the set is cleared, so an edge warned about before is warned again."""
        monkeypatch.setattr(schema_pruning, "_WARNED_EDGE_DROPS_CAP", 1)
        model = self._model().model_copy(
            update={
                "relationships": [
                    *self._model().relationships,
                    RelationshipEdge(
                        from_table="store.orders",
                        from_column="gone",
                        to_table="store.customers",
                        to_column="id",
                        join_sql="store.orders.gone = store.customers.id",
                    ),
                ]
            }
        )
        with caplog.at_level("WARNING", logger="app.services.schema_pruning"):
            _filter_semantic_to_schema(model, self._schema_without_secret())
            clear_semantic_filter_caches(narrowed_only=True)
            _filter_semantic_to_schema(model, self._schema_without_secret())
        warned = [r for r in caplog.records if "Dropped relationship" in r.getMessage()]
        assert len(warned) == 4
        assert len(schema_pruning._WARNED_EDGE_DROPS) == 1


class TestResolveSchemaNarrowsStaleCache:
    """The schema the chat stages receive comes from ``_resolve_schema``; a per-user
    cache written before a privacy change must not carry the newly excluded column."""

    async def test_cached_schema_loses_a_newly_excluded_column(self) -> None:
        stale = _make_schema(
            _make_table("store", "orders", ["id", "amount", "secret_ts"]),
            _make_table("store", "customers", ["id"]),
        )
        usc = MagicMock()
        usc.schema_cache = _schema_to_dict(stale)
        db = MagicMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=usc))
        )
        privacy = PrivacySettings(excluded_columns=["store.orders.secret_ts"])

        result = await _resolve_schema(
            conn=MagicMock(id="conn-1"),
            adapter=MagicMock(),
            config_dict={},
            privacy=privacy,
            connected=False,
            db=db,
            user_id="u1",
        )

        orders = next(t for t in result.schema.tables if t.name == "orders")
        assert [c.name for c in orders.columns] == ["id", "amount"]

    async def test_no_privacy_settings_means_the_defaults_not_no_filter(self) -> None:
        """``privacy=None`` is "never configured": sample values and ENUM/SET member
        lists stay withheld, as under ``PrivacySettings()``."""
        table = _make_table("store", "orders", ["id", "status"])
        table.columns[1] = ColumnInfo(
            name="status",
            data_type="enum",
            native_type="enum('a','b')",
            sample_values=["a", "b"],
        )
        usc = MagicMock()
        usc.schema_cache = _schema_to_dict(_make_schema(table))
        db = MagicMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=usc))
        )

        result = await _resolve_schema(
            conn=MagicMock(id="conn-1"),
            adapter=MagicMock(),
            config_dict={},
            privacy=None,
            connected=False,
            db=db,
            user_id="u1",
        )

        status = next(c for c in result.schema.tables[0].columns if c.name == "status")
        assert status.sample_values is None
        assert status.native_type == "enum(…)"


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
