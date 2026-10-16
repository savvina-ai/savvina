# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for SemanticModelGenerator — focused on build_time_expressions()."""

import logging
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.datasources.models import (
    ColumnInfo,
    DataSourceSchema,
    PrivacySettings,
    SchemaInfo,
    TableInfo,
)
from app.semantic.drift import SchemaDrift
from app.semantic.generator import (
    SemanticModelGenerator,
    drop_dangling_edits,
    edges_in_schema,
    names_any_table,
    policy_hides_table,
    prune_to_schema,
)
from app.semantic.models import (
    AggregationType,
    ColumnSemantic,
    Hierarchy,
    RelationshipEdge,
    SemanticModel,
    SimpleMetric,
    TablesBatchResponse,
    TableSemantic,
)


@pytest.fixture
def gen() -> SemanticModelGenerator:
    return SemanticModelGenerator()


# ── PostgreSQL ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("dialect", ["sql", "postgresql"])
def test_pg_compatible_dialects(gen: SemanticModelGenerator, dialect: str) -> None:
    exprs = gen.build_time_expressions(dialect)
    assert exprs["today"] == "CURRENT_DATE"
    assert "DATE_TRUNC" in exprs["this_month"]
    assert "INTERVAL '1 month'" in exprs["last_month"]
    assert len(exprs) == 15


def test_pg_returns_copy(gen: SemanticModelGenerator) -> None:
    # Mutations to the returned dict must not affect the module-level constant
    a = gen.build_time_expressions("postgresql")
    a["today"] = "mutated"
    b = gen.build_time_expressions("postgresql")
    assert b["today"] == "CURRENT_DATE"


# ── MySQL ──────────────────────────────────────────────────────────────────────


def test_mysql(gen: SemanticModelGenerator) -> None:
    exprs = gen.build_time_expressions("mysql")
    assert exprs["today"] == "CURDATE()"
    assert "DATE_SUB" in exprs["yesterday"]
    assert "DATE_FORMAT" in exprs["this_month"]
    assert len(exprs) == 15


# ── Unknown dialect ────────────────────────────────────────────────────────────


def test_unknown_dialect_returns_empty(gen: SemanticModelGenerator) -> None:
    assert gen.build_time_expressions("unknown_db") == {}
    assert gen.build_time_expressions("") == {}


# ── Key-addressed table generation ─────────────────────────────────────────────


def _schema_with_tables(
    names: list[str], extra_columns: list[str] | None = None
) -> DataSourceSchema:
    """A ``public`` schema with an ``id`` column (plus any extras) per named table."""
    cols = ["id", *(extra_columns or [])]
    return DataSourceSchema(
        source_type="postgresql",
        schemas=[SchemaInfo(name="public")],
        tables=[
            TableInfo(
                catalog=None,
                schema_name="public",
                name=name,
                table_type="table",
                columns=[
                    ColumnInfo(
                        name=col,
                        data_type="integer",
                        native_type="int4",
                        is_primary_key=(col == "id"),
                    )
                    for col in cols
                ],
            )
            for name in names
        ],
    )


def _provider_returning(tables: dict[str, TableSemantic]) -> MagicMock:
    # Round-trip through dicts like a real LLM response: ``TablesBatchResponse`` drops
    # any ``tables`` value that is not a raw dict.
    response = TablesBatchResponse.model_validate(
        {"tables": {k: v.model_dump(mode="json") for k, v in tables.items()}}
    )
    provider = MagicMock()
    provider.max_output_tokens = 8000
    provider.generate_structured = AsyncMock(return_value=response)
    return provider


def _tbl(*cols: str, name: str = "T") -> TableSemantic:
    return TableSemantic(
        display_name=name, columns={c: ColumnSemantic(display_name=c.upper()) for c in cols}
    )


async def _generate(
    schema: DataSourceSchema,
    provider: MagicMock,
    keys: list[str],
    edges: list[RelationshipEdge] | None = None,
) -> dict[str, TableSemantic]:
    return await SemanticModelGenerator().generate_tables(
        schema=schema,
        provider=provider,
        model=None,
        table_keys=keys,
        relationship_edges=edges or [],
    )


class TestGenerateTablesByKey:
    async def test_generates_only_the_named_tables(self) -> None:
        schema = _schema_with_tables(["orders", "customers", "items"])
        provider = _provider_returning({"public.customers": _tbl("id", name="Customers")})
        out = await _generate(schema, provider, ["public.customers"])
        assert set(out) == {"public.customers"}
        ddl = provider.generate_structured.call_args.kwargs["user_message"]
        assert "CREATE TABLE public.customers (" in ddl
        assert "CREATE TABLE public.orders (" not in ddl
        assert "CREATE TABLE public.items (" not in ddl

    async def test_keys_in_a_different_order_than_the_schema_resolve(self) -> None:
        schema = _schema_with_tables(["a", "b", "c"])
        provider = _provider_returning({"public.c": _tbl("id"), "public.a": _tbl("id")})
        out = await _generate(schema, provider, ["public.c", "public.a"])
        assert set(out) == {"public.a", "public.c"}
        ddl = provider.generate_structured.call_args.kwargs["user_message"]
        assert "CREATE TABLE public.a (" in ddl and "CREATE TABLE public.c (" in ddl
        assert "CREATE TABLE public.b (" not in ddl

    async def test_unknown_keys_make_no_llm_call(self) -> None:
        provider = MagicMock()
        provider.generate_structured = AsyncMock()
        out = await _generate(_schema_with_tables(["orders"]), provider, ["public.ghost"])
        assert out == {}
        provider.generate_structured.assert_not_called()

    async def test_llm_failure_raises_value_error(self) -> None:
        provider = MagicMock()
        provider.max_output_tokens = 8000
        provider.generate_structured = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(ValueError, match="generate_tables"):
            await _generate(_schema_with_tables(["orders"]), provider, ["public.orders"])

    async def test_only_edges_touching_the_batch_reach_the_prompt(self) -> None:
        schema = _schema_with_tables(["orders", "customers", "items", "tags"])

        def edge(a: str, b: str) -> RelationshipEdge:
            return RelationshipEdge(
                from_table=f"public.{a}",
                from_column="id",
                to_table=f"public.{b}",
                to_column="id",
                join_sql=f"public.{a}.id = public.{b}.id",
            )

        provider = _provider_returning({"public.orders": _tbl("id")})
        await _generate(
            schema,
            provider,
            ["public.orders"],
            edges=[edge("orders", "customers"), edge("items", "tags")],
        )
        prompt = provider.generate_structured.call_args.kwargs["user_message"]
        assert "public.orders.id → public.customers.id" in prompt
        assert "public.items.id" not in prompt

    async def test_real_table_outside_the_batch_is_dropped(self) -> None:
        schema = _schema_with_tables(["orders", "customers"])
        provider = _provider_returning(
            {"public.orders": _tbl("id"), "public.customers": _tbl("id")}
        )
        out = await _generate(schema, provider, ["public.orders"])
        assert set(out) == {"public.orders"}

    async def test_output_is_grounded_in_the_schema(self) -> None:
        """Invented tables/columns are dropped; an omitted real column stays absent."""
        schema = _schema_with_tables(["orders"], extra_columns=["order_total"])
        provider = _provider_returning(
            {
                "public.orders": _tbl("id", "invented_col", name="Orders"),
                "public.ghost": _tbl("id", name="Ghost"),
            }
        )
        out = await _generate(schema, provider, ["public.orders"])
        assert set(out) == {"public.orders"}
        cols = out["public.orders"].columns
        assert set(cols) == {"id"}  # invented dropped, omitted order_total NOT backfilled
        assert cols["id"].display_name == "ID"

    async def test_fiscal_enrichment_sees_remapped_keys(self) -> None:
        schema = _schema_with_tables(
            ["Facts"], extra_columns=["fiscal_year", "fiscal_quarter", "created_at"]
        )
        provider = _provider_returning(
            {"public.facts": _tbl("id", "fiscal_year", "fiscal_quarter", "created_at")}
        )
        out = await _generate(schema, provider, ["public.Facts"])
        description = out["public.Facts"].columns["created_at"].description
        assert description is not None and "NOT a business period date" in description

    async def test_generate_globals_llm_failure_raises_value_error(self) -> None:
        """Same contract as generate_tables: the router maps the error to HTTP."""
        provider = MagicMock()
        provider.max_output_tokens = 8000
        provider.generate_structured = AsyncMock(side_effect=RuntimeError("[RATE_LIMIT] slow"))
        with pytest.raises(ValueError, match="generate_globals") as excinfo:
            await SemanticModelGenerator().generate_globals(
                schema=_schema_with_tables(["orders"]),
                provider=provider,
                model=None,
                all_tables={"public.orders": _tbl("id", name="Orders")},
                relationship_edges=[],
            )
        assert "[RATE_LIMIT]" in str(excinfo.value)


class TestGroundTablesToSchema:
    @staticmethod
    def _ground(tables: dict[str, TableSemantic], schema: DataSourceSchema) -> tuple[int, int]:
        return SemanticModelGenerator()._ground_tables_to_schema(tables, schema)

    def test_returns_drop_counts_and_does_not_backfill(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["order_total"])
        tables = {"public.orders": _tbl("x"), "public.ghost": _tbl("id")}
        assert self._ground(tables, schema) == (1, 1)
        assert tables["public.orders"].columns == {}

    def test_table_key_quotes_are_stripped(self) -> None:
        schema = _schema_with_tables(["Orders"])
        entry = _tbl("id")
        tables = {'"public"."Orders"': entry}
        assert self._ground(tables, schema) == (0, 0)
        assert tables == {"public.Orders": entry}

    def test_table_key_backticks_are_stripped(self) -> None:
        schema = _schema_with_tables(["orders"])
        schema.tables[0].schema_name = "sales"
        entry = _tbl("id")
        tables = {"`sales`.`orders`": entry}
        assert self._ground(tables, schema) == (0, 0)
        assert tables == {"sales.orders": entry}

    def test_table_key_surrounding_whitespace_is_stripped(self) -> None:
        schema = _schema_with_tables(["orders"])
        entry = _tbl("id")
        tables = {" public.orders ": entry}
        assert self._ground(tables, schema) == (0, 0)
        assert tables == {"public.orders": entry}

    def test_table_key_case_insensitive(self) -> None:
        schema = _schema_with_tables(["Orders"])
        entry = _tbl("id")
        tables = {"public.orders": entry}
        assert self._ground(tables, schema) == (0, 0)
        assert tables == {"public.Orders": entry}

    def test_bare_table_name_maps_when_unique(self) -> None:
        schema = _schema_with_tables(["orders", "customers"])
        entry = _tbl("id")
        tables = {"Orders": entry}
        assert self._ground(tables, schema) == (0, 0)
        assert tables == {"public.orders": entry}

    def test_ambiguous_bare_name_is_dropped(self) -> None:
        schema = _schema_with_tables(["orders"])
        schema.schemas.append(SchemaInfo(name="archive"))
        schema.tables.append(
            TableInfo(
                catalog=None,
                schema_name="archive",
                name="orders",
                table_type="table",
                columns=[ColumnInfo(name="id", data_type="integer", native_type="int4")],
            )
        )
        tables = {"orders": _tbl("id")}
        assert self._ground(tables, schema) == (1, 0)
        assert tables == {}

    def test_ambiguous_case_insensitive_match_is_dropped(self) -> None:
        schema = _schema_with_tables(["Orders", "orders"])
        tables = {"public.ORDERS": _tbl("id")}
        assert self._ground(tables, schema) == (1, 0)
        assert tables == {}

    def test_remap_does_not_overwrite_an_existing_real_key(self) -> None:
        schema = _schema_with_tables(["Orders"])
        real = _tbl("id", name="Real")
        tables = {"public.orders": _tbl("id", name="Near"), "public.Orders": real}
        assert self._ground(tables, schema) == (1, 0)
        assert tables == {"public.Orders": real}

    def test_column_quote_strip_and_case_insensitive_remap(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["OrderTotal", "Status"])
        tables = {"public.orders": _tbl('"OrderTotal"', "status", "id")}
        assert self._ground(tables, schema) == (0, 0)
        assert set(tables["public.orders"].columns) == {"OrderTotal", "Status", "id"}

    def test_column_remap_ambiguous_or_duplicate_is_dropped(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["Status", "STATUS"])
        # "status" matches both Status and STATUS -> ambiguous -> dropped;
        # "ID" maps to "id", which is already present -> dropped.
        tables = {"public.orders": _tbl("status", "id", "ID")}
        assert self._ground(tables, schema) == (0, 2)
        assert set(tables["public.orders"].columns) == {"id"}


class TestPruneToSchema:
    def test_drops_tables_the_schema_no_longer_has(self) -> None:
        schema = _schema_with_tables(["orders"])
        out = prune_to_schema({"public.orders": _tbl("id"), "public.gone": _tbl("id")}, schema)
        assert set(out) == {"public.orders"}

    def test_a_bare_key_is_dropped_even_when_the_policy_hides_that_table(self) -> None:
        # A bare stored key is an old-blob/LLM anomaly, not a hidden "public.payroll".
        out = prune_to_schema(
            {"payroll": _tbl("id")},
            _schema_with_tables(["orders"]),
            PrivacySettings(excluded_tables=["public.payroll"]),
        )
        assert out == {}

    def test_drops_columns_the_table_no_longer_has(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["total"])
        out = prune_to_schema({"public.orders": _tbl("id", "total", "dropped_col")}, schema)
        assert set(out["public.orders"].columns) == {"id", "total"}

    def test_clears_time_columns_that_left_the_table(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["order_date"])
        table = _tbl("id", "order_date").model_copy(
            update={"primary_timestamp_column": "created_at", "primary_date_column": "order_date"}
        )
        out = prune_to_schema({"public.orders": table}, schema)["public.orders"]
        assert out.primary_timestamp_column is None
        assert out.primary_date_column == "order_date"

    def test_a_reference_to_a_real_unannotated_column_is_kept(self) -> None:
        # The generator never backfills a column the LLM omitted; that column is
        # still real, so a reference to it is not dangling.
        schema = _schema_with_tables(["orders"], extra_columns=["created_at"])
        table = _tbl("id").model_copy(
            update={"primary_timestamp_column": "created_at", "partition_columns": ["created_at"]}
        )
        out = prune_to_schema({"public.orders": table}, schema)["public.orders"]
        assert out.primary_timestamp_column == "created_at"
        assert out.partition_columns == ["created_at"]

    def test_filters_partition_and_cluster_columns(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["region", "day"])
        table = _tbl("id").model_copy(
            update={"partition_columns": ["day", "gone"], "cluster_columns": ["gone", "region"]}
        )
        out = prune_to_schema({"public.orders": table}, schema)["public.orders"]
        assert out.partition_columns == ["day"]
        assert out.cluster_columns == ["region"]

    def test_filters_hierarchy_levels_and_drops_empty_hierarchies(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["year", "month"])
        table = _tbl("id").model_copy(
            update={
                "hierarchies": [
                    Hierarchy(name="Date", levels=["year", "quarter", "month"]),
                    Hierarchy(name="Geo", levels=["country", "city"]),
                ]
            }
        )
        out = prune_to_schema({"public.orders": table}, schema)["public.orders"]
        assert [(h.name, h.levels) for h in out.hierarchies] == [("Date", ["year", "month"])]

    def test_default_filters_are_not_rewritten(self) -> None:
        schema = _schema_with_tables(["orders"])
        table = _tbl("id").model_copy(update={"default_filters": ["gone_col IS NULL"]})
        out = prune_to_schema({"public.orders": table}, schema)["public.orders"]
        assert out.default_filters == ["gone_col IS NULL"]

    def test_does_not_mutate_its_input(self) -> None:
        schema = _schema_with_tables(["orders"])
        table = _tbl("id", "gone").model_copy(
            update={
                "primary_date_column": "gone",
                "partition_columns": ["gone"],
                "hierarchies": [Hierarchy(name="H", levels=["gone"])],
            }
        )
        tables = {"public.orders": table, "public.ghost": _tbl("id")}
        before = {k: v.model_dump() for k, v in tables.items()}
        out = prune_to_schema(tables, schema)
        assert {k: v.model_dump() for k, v in tables.items()} == before
        assert out["public.orders"] is not table

    # ── An absence the privacy policy explains is not drift ──────────────────

    def test_a_table_the_policy_hides_is_kept_unchanged(self) -> None:
        schema = _schema_with_tables(["orders"])  # public.secret was filtered out
        secret = _tbl("id", "x", name="Secret").model_copy(
            update={"primary_timestamp_column": "x", "description": "hand written"}
        )
        privacy = PrivacySettings(excluded_tables=["public.secret"])
        out = prune_to_schema(
            {"public.orders": _tbl("id"), "public.secret": secret}, schema, privacy
        )
        assert set(out) == {"public.orders", "public.secret"}
        assert out["public.secret"].model_dump() == secret.model_dump()

    def test_a_table_in_an_excluded_schema_is_kept(self) -> None:
        schema = _schema_with_tables(["orders"])
        privacy = PrivacySettings(excluded_schemas=["hr"])
        out = prune_to_schema({"hr.people": _tbl("id"), "public.gone": _tbl("id")}, schema, privacy)
        assert set(out) == {"hr.people"}

    def test_a_column_the_policy_hides_is_kept(self) -> None:
        schema = _schema_with_tables(["orders"])  # ssn skipped at introspection
        privacy = PrivacySettings(excluded_columns=["public.orders.ssn"])
        out = prune_to_schema({"public.orders": _tbl("id", "ssn", "gone")}, schema, privacy)
        assert set(out["public.orders"].columns) == {"id", "ssn"}

    def test_references_to_a_hidden_column_are_kept(self) -> None:
        schema = _schema_with_tables(["orders"])
        privacy = PrivacySettings(excluded_columns=["public.orders.signed_up_at"])
        table = _tbl("id").model_copy(
            update={
                "primary_timestamp_column": "signed_up_at",
                "partition_columns": ["signed_up_at", "gone"],
                "hierarchies": [Hierarchy(name="H", levels=["signed_up_at"])],
            }
        )
        out = prune_to_schema({"public.orders": table}, schema, privacy)["public.orders"]
        assert out.primary_timestamp_column == "signed_up_at"
        assert out.partition_columns == ["signed_up_at"]
        assert [h.levels for h in out.hierarchies] == [["signed_up_at"]]

    def test_a_sensitive_name_alone_does_not_hide_a_column(self) -> None:
        # The adapters still introspect sensitive-pattern columns, so an absent
        # ``email`` really is gone: only exact excluded_columns membership hides.
        schema = _schema_with_tables(["orders"])
        out = prune_to_schema({"public.orders": _tbl("id", "email")}, schema, PrivacySettings())
        assert set(out["public.orders"].columns) == {"id"}

    # ── Near-miss stored spellings are dropped, with a warning ──────────────

    @staticmethod
    def _near_miss_warnings(caplog) -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "near-miss" in r.getMessage()
        ]

    def test_a_near_miss_table_key_is_dropped_with_a_warning(self, caplog) -> None:
        schema = _schema_with_tables(["orders"])
        with caplog.at_level(logging.WARNING, logger="app.semantic.generator"):
            out = prune_to_schema({"public.Orders": _tbl("id", name="Hand-edited")}, schema)
        assert out == {}
        [message] = self._near_miss_warnings(caplog)
        assert "'public.Orders'" in message and "'public.orders'" in message
        assert "hand edits" in message

    def test_a_near_miss_column_is_dropped_and_its_references_cleared(self, caplog) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["OrderID", "OrderDate"])
        table = _tbl("id", '"OrderID"', "orderdate").model_copy(
            update={"primary_date_column": "orderdate", "cluster_columns": ['"OrderID"']}
        )
        with caplog.at_level(logging.WARNING, logger="app.semantic.generator"):
            out = prune_to_schema({"public.orders": table}, schema)["public.orders"]
        assert set(out.columns) == {"id"}
        assert out.primary_date_column is None
        assert out.cluster_columns == []
        messages = self._near_miss_warnings(caplog)
        assert len(messages) == 2
        assert any("'orderdate'" in m and "'OrderDate'" in m for m in messages)
        assert any("'\"OrderID\"'" in m and "'OrderID'" in m for m in messages)

    def test_a_key_that_is_simply_gone_is_dropped_without_a_warning(self, caplog) -> None:
        schema = _schema_with_tables(["orders"])
        with caplog.at_level(logging.WARNING, logger="app.semantic.generator"):
            out = prune_to_schema(
                {"public.ghost": _tbl("id"), "public.orders": _tbl("id", "gone")}, schema
            )
        assert set(out) == {"public.orders"}
        assert self._near_miss_warnings(caplog) == []

    def test_log_false_silences_the_near_miss_warnings(self, caplog) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["OrderID"])
        tables = {"public.Orders": _tbl("id"), "public.orders": _tbl("id", "orderid")}
        with caplog.at_level(logging.WARNING, logger="app.semantic.generator"):
            out = prune_to_schema(tables, schema, log=False)
        assert set(out) == {"public.orders"}
        assert set(out["public.orders"].columns) == {"id"}
        assert self._near_miss_warnings(caplog) == []


def _edge(from_table: str, from_column: str, to_table: str, to_column: str) -> RelationshipEdge:
    return RelationshipEdge(
        from_table=from_table,
        from_column=from_column,
        to_table=to_table,
        to_column=to_column,
        join_sql=f"{from_table}.{from_column} = {to_table}.{to_column}",
    )


class TestEdgesInSchema:
    def test_keeps_live_edges_and_drops_gone_ones(self) -> None:
        schema = _schema_with_tables(["orders", "customers"], extra_columns=["customer_id"])
        live = _edge("public.orders", "customer_id", "public.customers", "id")
        gone_table = _edge("public.orders", "customer_id", "public.ghosts", "id")
        gone_column = _edge("public.orders", "region_id", "public.customers", "id")
        assert edges_in_schema([live, gone_table, gone_column], schema) == [live]

    def test_an_endpoint_the_policy_hides_counts_as_live(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["customer_id"])
        privacy = PrivacySettings(
            excluded_tables=["public.customers"], excluded_columns=["public.orders.ssn"]
        )
        to_hidden_table = _edge("public.orders", "customer_id", "public.customers", "id")
        from_hidden_column = _edge("public.orders", "ssn", "public.orders", "id")
        gone = _edge("public.orders", "customer_id", "public.ghosts", "id")
        edges = [to_hidden_table, from_hidden_column, gone]
        assert edges_in_schema(edges, schema, privacy) == [to_hidden_table, from_hidden_column]
        assert edges_in_schema(edges, schema) == []


class TestDetectDriftPrivacy:
    """The store keeps what the policy hides; Check Drift must not call it removed."""

    _PRIVACY = PrivacySettings(
        excluded_tables=["public.payroll"], excluded_columns=["public.orders.ssn"]
    )

    def test_a_metric_naming_a_hidden_table_bare_is_not_drift(self) -> None:
        """related_tables may be bare ("payroll"); the policy lists "public.payroll"."""
        schema = _schema_with_tables(["orders"])
        stored = SemanticModel(
            tables={"public.orders": _tbl("id", name="Orders")},
            business_metrics=[
                SimpleMetric(
                    name="Payroll total",
                    definition="SUM(x)",
                    aggregation=AggregationType.SUM,
                    related_tables=["payroll"],
                ),
                SimpleMetric(
                    name="Ghost total",
                    definition="SUM(x)",
                    aggregation=AggregationType.SUM,
                    related_tables=["ghosts"],
                ),
            ],
        )
        warnings = SemanticModelGenerator().detect_drift(schema, stored, self._PRIVACY)
        assert not any("Payroll total" in w for w in warnings)
        assert any("Ghost total" in w for w in warnings)

    def test_policy_hides_table_bare_keys_only_with_bare_ok(self) -> None:
        qualified = PrivacySettings(excluded_tables=["public.payroll"])
        bare = PrivacySettings(excluded_tables=["payroll"])
        assert policy_hides_table("payroll", qualified, bare_ok=True)
        assert not policy_hides_table("payroll", qualified)
        assert policy_hides_table("payroll", bare, bare_ok=True)
        assert policy_hides_table("public.payroll", bare)
        assert policy_hides_table("public.payroll", qualified)
        assert not policy_hides_table("orders", qualified, bare_ok=True)
        assert not policy_hides_table("public.orders", bare)
        assert not policy_hides_table("payroll", None)
        assert policy_hides_table("hr.people", PrivacySettings(excluded_schemas=["hr"]))

    def _stored(self) -> SemanticModel:
        return SemanticModel(
            tables={
                "public.orders": _tbl("id", "ssn", "gone_col", name="Orders"),
                "public.payroll": _tbl("id", name="Payroll"),
                "public.ghosts": _tbl("id", name="Ghosts"),
            }
        )

    def test_hidden_table_and_column_are_not_drift(self) -> None:
        # The policy-applied schema: payroll removed, orders.ssn never introspected.
        schema = _schema_with_tables(["orders"])
        warnings = SemanticModelGenerator().detect_drift(schema, self._stored(), self._PRIVACY)
        assert not any("payroll" in w for w in warnings)
        assert not any("ssn" in w for w in warnings)

    def test_genuinely_missing_table_and_column_are_still_drift(self) -> None:
        schema = _schema_with_tables(["orders"])
        warnings = SemanticModelGenerator().detect_drift(schema, self._stored(), self._PRIVACY)
        assert any('"public.ghosts"' in w and "no longer exists" in w for w in warnings)
        assert any('"public.orders.gone_col"' in w for w in warnings)

    def test_without_privacy_a_hidden_item_reads_as_removed(self) -> None:
        schema = _schema_with_tables(["orders"])
        warnings = SemanticModelGenerator().detect_drift(schema, self._stored())
        assert any('"public.payroll"' in w for w in warnings)
        assert any('"public.orders.ssn"' in w for w in warnings)


class TestUnreadableTables:
    """A table the role cannot read exists: persisting keeps it, drift names it once."""

    def _schema(self) -> DataSourceSchema:
        schema = _schema_with_tables(["orders"], extra_columns=["customer_id"])
        schema.unreadable_tables = ["public.customers"]
        return schema

    def test_prune_keeps_an_unreadable_table_only_when_asked(self) -> None:
        tables = {
            "public.orders": _tbl("id"),
            "public.customers": _tbl("id", name="Customers"),
            "public.gone": _tbl("id"),
        }
        kept = prune_to_schema(tables, self._schema(), keep_unreadable=True)
        assert set(kept) == {"public.orders", "public.customers"}
        assert kept["public.customers"] == tables["public.customers"]
        # The default is strict: a prompt must not offer a table nothing can query.
        assert set(prune_to_schema(tables, self._schema())) == {"public.orders"}

    def test_an_edge_to_an_unreadable_table_is_live_only_when_asked(self) -> None:
        to_unreadable = _edge("public.orders", "customer_id", "public.customers", "id")
        gone = _edge("public.orders", "customer_id", "public.ghosts", "id")
        edges = [to_unreadable, gone]
        assert edges_in_schema(edges, self._schema(), keep_unreadable=True) == [to_unreadable]
        assert edges_in_schema(edges, self._schema()) == []

    def test_drift_names_unreadable_tables_once_instead_of_calling_them_removed(self) -> None:
        stored = SemanticModel(
            tables={
                "public.orders": _tbl("id", name="Orders"),
                "public.customers": _tbl("id", name="Customers"),
                "public.ghosts": _tbl("id", name="Ghosts"),
            },
            business_metrics=[
                SimpleMetric(
                    name="Customer count",
                    definition="COUNT(*)",
                    aggregation=AggregationType.COUNT,
                    related_tables=["customers"],
                ),
            ],
        )
        warnings = SemanticModelGenerator().detect_drift(self._schema(), stored)
        assert not any('"public.customers"' in w and "no longer exists" in w for w in warnings)
        assert not any("Customer count" in w for w in warnings)
        unreadable = [w for w in warnings if "cannot read" in w]
        assert len(unreadable) == 1 and "public.customers" in unreadable[0]
        assert any('"public.ghosts"' in w and "no longer exists" in w for w in warnings)


class TestTableKeyAndHidesColumn:
    def test_table_key_is_schema_dot_name(self) -> None:
        from app.datasources.models import ColumnInfo, TableInfo, table_key

        t = TableInfo(
            catalog=None,
            schema_name="sales",
            name="orders",
            table_type="table",
            columns=[ColumnInfo(name="id", data_type="integer", native_type="int4")],
        )
        assert table_key(t) == "sales.orders"

    def test_hides_column_is_exact_membership_only(self) -> None:
        privacy = PrivacySettings(excluded_columns=["public.orders.ssn"])
        assert privacy.hides_column("public.orders", "ssn")
        assert not privacy.hides_column("public.orders", "SSN")
        assert not privacy.hides_column("public.customers", "ssn")
        # Unlike is_column_excluded, a sensitive-name pattern does not hide a column here.
        assert privacy.is_column_excluded("public", "orders", "email")
        assert not privacy.hides_column("public.orders", "email")


def _marks(table: dict[str, list[str]]) -> dict[str, dict[str, list[str]]]:
    return {
        "model": {},
        "table": table,
        "column": {"public.orders.status": ["display_name"]},
        "metric": {},
        "global_item": {},
        "relationship": {},
    }


class TestDropDanglingEdits:
    _MARKS: ClassVar[dict[str, dict[str, list[str]]]] = _marks(
        {"public.orders": ["description", "primary_timestamp_column", "partition_columns"]}
    )
    _PROTECTED: ClassVar[dict[str, dict[str, dict[str, Any]]]] = {
        "table": {
            "public.orders": {
                "description": "hand",
                "primary_timestamp_column": "created_ts",
                "partition_columns": ["created_ts"],
            }
        },
        "column": {"public.orders.status": {"display_name": "Status"}},
    }

    def test_an_edit_naming_a_dropped_column_is_voided_mark_and_value(self) -> None:
        schema = _schema_with_tables(["orders"])  # orders has only ``id``
        marks, protected, notes = drop_dangling_edits(self._MARKS, self._PROTECTED, schema)
        assert marks["table"]["public.orders"] == ["description"]
        assert protected["table"]["public.orders"] == {"description": "hand"}
        assert marks["column"] == self._MARKS["column"]  # column marks are not references
        assert notes == [
            "public.orders.primary_timestamp_column: hand edit cleared — created_ts is no "
            "longer in the table",
            "public.orders.partition_columns: hand edit cleared — created_ts is no longer "
            "in the table",
        ]
        # Pure: the inputs are untouched.
        assert self._PROTECTED["table"]["public.orders"]["primary_timestamp_column"] == "created_ts"
        assert self._MARKS["table"]["public.orders"] == [
            "description",
            "primary_timestamp_column",
            "partition_columns",
        ]

    def test_log_false_returns_the_notes_without_logging_them(self, caplog) -> None:
        """A dry run previews the notes; it must not log a trim that never happened."""
        schema = _schema_with_tables(["orders"])
        with caplog.at_level(logging.WARNING, logger="app.semantic.generator"):
            _, _, notes = drop_dangling_edits(self._MARKS, self._PROTECTED, schema, log=False)
        assert len(notes) == 2
        assert "Dangling hand edit" not in caplog.text

    def test_a_reference_to_a_hidden_column_is_kept(self) -> None:
        schema = _schema_with_tables(["orders"])
        privacy = PrivacySettings(excluded_columns=["public.orders.created_ts"])
        marks, protected, notes = drop_dangling_edits(self._MARKS, self._PROTECTED, schema, privacy)
        assert marks == self._MARKS and protected == self._PROTECTED
        assert notes == []

    def test_a_table_not_in_the_schema_is_left_to_prune_to_schema(self) -> None:
        marks, protected, notes = drop_dangling_edits(
            self._MARKS, self._PROTECTED, _schema_with_tables([])
        )
        assert marks == self._MARKS and protected == self._PROTECTED
        assert notes == []

    def test_a_list_field_loses_only_its_dangling_entries(self) -> None:
        marks, protected, notes = drop_dangling_edits(
            _marks({"public.orders": ["partition_columns"]}),
            {"table": {"public.orders": {"partition_columns": ["id", "gone"]}}, "column": {}},
            _schema_with_tables(["orders"]),
        )
        assert marks["table"]["public.orders"] == ["partition_columns"]
        assert protected["table"]["public.orders"] == {"partition_columns": ["id"]}
        assert notes == ["public.orders.partition_columns: dropped gone (no longer in the table)"]

    def test_a_hierarchy_with_one_dangling_level_is_filtered_and_keeps_its_mark(self) -> None:
        hierarchies = [
            {"name": "Date", "levels": ["year", "gone", "day"]},
            {"name": "Geo", "levels": ["country"]},
        ]
        given = {"table": {"public.orders": {"hierarchies": hierarchies}}, "column": {}}
        marks, protected, notes = drop_dangling_edits(
            _marks({"public.orders": ["hierarchies"]}),
            given,
            _schema_with_tables(["orders"], extra_columns=["year", "day", "country"]),
        )
        assert marks["table"]["public.orders"] == ["hierarchies"]
        assert protected["table"]["public.orders"]["hierarchies"] == [
            {"name": "Date", "levels": ["year", "day"]},
            {"name": "Geo", "levels": ["country"]},
        ]
        assert notes == ["public.orders.hierarchies: dropped gone (no longer in the table)"]
        assert hierarchies[0]["levels"] == ["year", "gone", "day"]  # input untouched

    def test_a_hierarchy_whose_levels_all_dangle_is_dropped(self) -> None:
        given = {
            "table": {
                "public.orders": {
                    "hierarchies": [
                        {"name": "Date", "levels": ["gone", "lost"]},
                        {"name": "Ids", "levels": ["id"]},
                    ]
                }
            },
            "column": {},
        }
        marks, protected, notes = drop_dangling_edits(
            _marks({"public.orders": ["hierarchies"]}), given, _schema_with_tables(["orders"])
        )
        assert marks["table"]["public.orders"] == ["hierarchies"]
        assert protected["table"]["public.orders"]["hierarchies"] == [
            {"name": "Ids", "levels": ["id"]}
        ]
        assert notes == ["public.orders.hierarchies: dropped gone, lost (no longer in the table)"]

    def test_the_only_hierarchy_dropped_voids_the_field_and_the_table_entry(self) -> None:
        """A table whose only marked field is voided leaves marks and protected."""
        given = {
            "table": {"public.orders": {"hierarchies": [{"name": "D", "levels": ["gone"]}]}},
            "column": {},
        }
        marks, protected, notes = drop_dangling_edits(
            _marks({"public.orders": ["hierarchies"]}), given, _schema_with_tables(["orders"])
        )
        assert "public.orders" not in marks["table"]
        assert "public.orders" not in protected["table"]
        assert notes == [
            "public.orders.hierarchies: hand edit cleared — gone is no longer in the table"
        ]

    def test_one_note_per_changed_field(self) -> None:
        fields = {
            "primary_timestamp_column": "gone_ts",
            "primary_date_column": "id",  # live: unchanged, no note
            "partition_columns": ["id", "gone_p"],
            "cluster_columns": ["gone_a", "gone_b"],
            "hierarchies": [{"name": "H", "levels": ["id", "gone_h"]}],
        }
        marks, protected, notes = drop_dangling_edits(
            _marks({"public.orders": list(fields)}),
            {"table": {"public.orders": fields}, "column": {}},
            _schema_with_tables(["orders"]),
        )
        assert marks["table"]["public.orders"] == [
            "primary_date_column",
            "partition_columns",
            "hierarchies",
        ]
        assert protected["table"]["public.orders"] == {
            "primary_date_column": "id",
            "partition_columns": ["id"],
            "hierarchies": [{"name": "H", "levels": ["id"]}],
        }
        assert len(notes) == 4
        assert [n.split(":")[0] for n in notes] == [
            "public.orders.primary_timestamp_column",
            "public.orders.partition_columns",
            "public.orders.cluster_columns",
            "public.orders.hierarchies",
        ]
        assert "gone_a, gone_b are no longer in the table" in notes[2]


class TestDetectDriftBareNamesUnderAnExcludedSchema:
    def test_a_bare_metric_table_under_an_excluded_schema_is_not_drift(self) -> None:
        privacy = PrivacySettings(excluded_schemas=["hr"])
        schema = _schema_with_tables(["orders"])
        stored = SemanticModel(
            tables={
                "public.orders": _tbl("id", name="Orders"),
                "hr.salaries": _tbl("id", name="S"),
            },
            business_metrics=[
                SimpleMetric(
                    name="Payroll",
                    definition="SUM(x)",
                    aggregation=AggregationType.SUM,
                    related_tables=["salaries"],
                ),
                SimpleMetric(
                    name="Ghost",
                    definition="SUM(x)",
                    aggregation=AggregationType.SUM,
                    related_tables=["ghosts"],
                ),
            ],
        )
        warnings = SemanticModelGenerator().detect_drift(schema, stored, privacy)
        assert not any("Payroll" in w for w in warnings)
        assert any("Ghost" in w for w in warnings)
        assert not any("hr.salaries" in w for w in warnings)


class TestDetectDriftTakesAPrecomputedDiff:
    """``detect_drift(drift=...)`` reports from the caller's diff instead of recomputing."""

    def test_a_passed_drift_is_used_instead_of_recomputed(self) -> None:
        schema = _schema_with_tables(["orders"])
        stored = SemanticModel(tables={"public.orders": TableSemantic(display_name="Orders")})
        # The schema and model agree, but the caller's diff says a table is gone: the
        # warnings follow the diff, so the route can compute it once and reuse it.
        drift = SchemaDrift(removed_table_keys={"public.orders"})
        warnings = SemanticModelGenerator().detect_drift(schema, stored, drift=drift)
        assert warnings == [
            'Table "public.orders" referenced in semantic model no longer exists in the database'
        ]


class TestDetectDriftNamesAQuotedDuplicate:
    """A stored ``'"status"'`` beside a live ``status`` is reported as what it is — a
    duplicate key the apply removes — not as a column that no longer exists."""

    def test_a_quoted_spelling_of_a_live_column_gets_its_own_warning(self) -> None:
        schema = _schema_with_tables(["orders"], ["status"])
        stored = SemanticModel(
            tables={"public.orders": _tbl("id", "status", '"status"', name="Orders")}
        )
        warnings = SemanticModelGenerator().detect_drift(schema, stored)
        assert warnings == [
            'Column key "public.orders."status"" in the semantic model is a quoted spelling '
            'of "status"; Apply Drift removes the duplicate'
        ]

    def test_a_quoted_key_of_a_gone_column_is_still_reported_gone(self) -> None:
        schema = _schema_with_tables(["orders"])
        stored = SemanticModel(tables={"public.orders": _tbl("id", '"legacy"', name="Orders")})
        warnings = SemanticModelGenerator().detect_drift(schema, stored)
        assert warnings == [
            'Column "public.orders.legacy" referenced in semantic model no longer exists'
        ]


class TestAQuotedKeyOfAHiddenColumnIsHidden:
    """A blob-era key ``'"ssn"'`` for a column the policy hides is hidden, not gone —
    the rule ``compute_drift`` applies, so the prune never drops (and the summary
    never names) what the check says is hidden."""

    _PRIVACY = PrivacySettings(excluded_columns=["public.orders.ssn"])

    def test_prune_keeps_it(self) -> None:
        out = prune_to_schema(
            {"public.orders": _tbl("id", '"ssn"')}, _schema_with_tables(["orders"]), self._PRIVACY
        )
        assert set(out["public.orders"].columns) == {"id", '"ssn"'}

    def test_a_reference_to_it_is_kept(self) -> None:
        table = _tbl("id", '"ssn"').model_copy(update={"partition_columns": ['"ssn"']})
        out = prune_to_schema(
            {"public.orders": table}, _schema_with_tables(["orders"]), self._PRIVACY
        )
        assert out["public.orders"].partition_columns == ['"ssn"']

    def test_a_hand_edit_naming_it_is_kept(self) -> None:
        marks = _marks({"public.orders": ["partition_columns"]})
        protected = {"table": {"public.orders": {"partition_columns": ['"ssn"']}}, "column": {}}
        out_marks, out_protected, notes = drop_dangling_edits(
            marks, protected, _schema_with_tables(["orders"]), self._PRIVACY
        )
        assert out_protected == protected and out_marks["table"] == marks["table"]
        assert notes == []

    def test_an_edge_on_it_is_alive(self) -> None:
        edge = _edge("public.orders", '"ssn"', "public.orders", "id")
        assert edges_in_schema([edge], _schema_with_tables(["orders"]), self._PRIVACY) == [edge]

    def test_a_quoted_key_of_a_visible_column_is_still_a_near_miss(self) -> None:
        """Only the policy check strips quotes: ``'"id"'`` beside a live ``id`` is a
        near-miss spelling and is dropped, as before."""
        out = prune_to_schema(
            {"public.orders": _tbl("id", '"id"')}, _schema_with_tables(["orders"]), self._PRIVACY
        )
        assert set(out["public.orders"].columns) == {"id"}


class TestNamesAnyTable:
    """Whether an LLM item's table list names one of the target tables, by the
    generator's near-miss rules (quotes, case, a unique bare name)."""

    _KEYS: ClassVar[list[str]] = ["public.orders", "public.events", "sales.events"]

    @pytest.mark.parametrize(
        ("refs", "expected"),
        [
            (["public.events"], True),
            (['"public"."events"'], True),
            (["PUBLIC.EVENTS"], True),
            (["public.orders"], False),
            ([], False),
            # Ambiguous bare name: public.events or sales.events — not evidence.
            (["events"], False),
        ],
    )
    def test_qualified_and_near_miss_refs(self, refs: list[str], expected: bool) -> None:
        assert names_any_table(refs, {"public.events"}, self._KEYS) is expected

    def test_a_unique_bare_name_counts(self) -> None:
        assert names_any_table(["orders"], {"public.orders"}, self._KEYS) is True
