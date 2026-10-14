# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for SemanticModelGenerator — focused on build_time_expressions()."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.datasources.models import ColumnInfo, DataSourceSchema, SchemaInfo, TableInfo
from app.semantic.generator import SemanticModelGenerator
from app.semantic.models import (
    GENERATION_BATCH_SIZE,
    ColumnSemantic,
    RelationshipEdge,
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

    def test_batch_size_constant_is_shared(self) -> None:
        from app.semantic.generator import _BATCH_SIZE

        assert _BATCH_SIZE == GENERATION_BATCH_SIZE == 4


class TestGenerateTableBatchWrapper:
    async def test_batch_idx_delegates_by_key(self) -> None:
        schema = _schema_with_tables(["a", "b", "c", "d", "e"])
        provider = _provider_returning({"public.e": _tbl("id")})
        out = await SemanticModelGenerator().generate_table_batch(
            schema=schema, provider=provider, model=None, batch_idx=1, relationship_edges=[]
        )
        assert set(out) == {"public.e"}
        ddl = provider.generate_structured.call_args.kwargs["user_message"]
        assert "CREATE TABLE public.e (" in ddl
        assert "CREATE TABLE public.a (" not in ddl

    async def test_batch_idx_past_the_end_makes_no_llm_call(self) -> None:
        provider = MagicMock()
        provider.generate_structured = AsyncMock()
        out = await SemanticModelGenerator().generate_table_batch(
            schema=_schema_with_tables(["a", "b"]),
            provider=provider,
            model=None,
            batch_idx=5,
            relationship_edges=[],
        )
        assert out == {}
        provider.generate_structured.assert_not_called()


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
