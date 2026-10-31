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
    RelationshipInfo,
    SchemaInfo,
    TableInfo,
)
from app.semantic.drift import SchemaDrift, compute_drift
from app.semantic.generator import (
    _TABLE_REFERENCE_FIELDS,
    SemanticModelGenerator,
    _map_reference_value,
    _map_table_references,
    _reference_names,
    drop_dangling_edits,
    edges_in_schema,
    names_any_table,
    policy_hides_table,
    prune_to_schema,
    rename_columns,
)
from app.semantic.models import (
    AggregationType,
    ColumnsBatchResponse,
    ColumnSemantic,
    Hierarchy,
    RelationshipEdge,
    SemanticModel,
    SimpleMetric,
    TablesBatchResponse,
    TableSemantic,
    ValueMapping,
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
    privacy: PrivacySettings | None = None,
) -> dict[str, TableSemantic]:
    return await SemanticModelGenerator().generate_tables(
        schema=schema,
        provider=provider,
        model=None,
        table_keys=keys,
        relationship_edges=edges or [],
        privacy=privacy,
    )


def _columns_provider(tables: dict[str, dict[str, ColumnSemantic]]) -> MagicMock:
    # Round-trip through dicts like a real LLM response.
    response = ColumnsBatchResponse.model_validate(
        {
            "tables": {
                key: {"columns": {c: col.model_dump(mode="json") for c, col in cols.items()}}
                for key, cols in tables.items()
            }
        }
    )
    provider = MagicMock()
    provider.max_output_tokens = 8000
    provider.generate_structured = AsyncMock(return_value=response)
    return provider


async def _generate_new_columns(
    schema: DataSourceSchema,
    provider: MagicMock,
    columns_by_table: dict[str, list[str]],
    context: dict[str, TableSemantic] | None = None,
    edges: list[RelationshipEdge] | None = None,
    privacy: PrivacySettings | None = None,
) -> dict[str, dict[str, ColumnSemantic]]:
    return await SemanticModelGenerator().generate_new_columns(
        schema=schema,
        provider=provider,
        model=None,
        columns_by_table=columns_by_table,
        table_context=context or {},
        relationship_edges=edges or [],
        privacy=privacy,
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

    async def test_generate_globals_returns_metrics_derived_columns_and_segments(self) -> None:
        """A 3-tuple: no joins (never generated) and no warnings (the routes run the
        metric checks over the metrics they keep)."""
        from app.semantic.models import GlobalSectionsResponse

        response = GlobalSectionsResponse.model_validate(
            {
                "business_metrics": [
                    {
                        "name": "Revenue",
                        "description": "Completed order revenue",
                        "definition": "orders.total",
                        "metric_type": "simple",
                        "aggregation": "sum",
                        "filters": ["orders.status = 'completed'"],
                        "related_tables": ["public.orders"],
                    }
                ],
                "segments": [
                    {"name": "big", "sql_expression": "total > 100", "description": "Big"}
                ],
            }
        )
        provider = MagicMock()
        provider.max_output_tokens = 8000
        provider.generate_structured = AsyncMock(return_value=response)
        metrics, derived, segments = await SemanticModelGenerator().generate_globals(
            schema=_schema_with_tables(["orders"], ["status"]),
            provider=provider,
            model=None,
            all_tables={"public.orders": _tbl("id", "status", name="Orders")},
            relationship_edges=[],
        )
        assert [m.name for m in metrics] == ["Revenue"]
        assert derived == []
        assert [s.name for s in segments] == ["big"]


class TestGenerateNewColumns:
    async def test_returns_only_the_requested_new_columns(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["loyalty_tier"])
        provider = _columns_provider(
            {
                "public.orders": {
                    "loyalty_tier": ColumnSemantic(display_name="Tier"),
                    # The LLM re-describes an existing column: ignored.
                    "id": ColumnSemantic(display_name="Rewritten"),
                }
            }
        )
        out = await _generate_new_columns(schema, provider, {"public.orders": ["loyalty_tier"]})
        assert set(out) == {"public.orders"}
        assert set(out["public.orders"]) == {"loyalty_tier"}
        assert out["public.orders"]["loyalty_tier"].display_name == "Tier"

    async def test_a_table_with_no_requested_column_answered_is_left_out(self) -> None:
        schema = _schema_with_tables(["orders", "customers"], extra_columns=["loyalty_tier"])
        provider = _columns_provider(
            {
                "public.orders": {"loyalty_tier": ColumnSemantic(display_name="Tier")},
                "public.customers": {"id": ColumnSemantic(display_name="Rewritten")},
            }
        )
        out = await _generate_new_columns(
            schema,
            provider,
            {"public.orders": ["loyalty_tier"], "public.customers": ["loyalty_tier"]},
        )
        assert set(out) == {"public.orders"}

    async def test_the_prompt_names_the_new_columns_and_carries_the_table_context(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["loyalty_tier"])
        provider = _columns_provider({})
        context = {
            "public.orders": TableSemantic(display_name="Orders", description="Customer purchases")
        }
        await _generate_new_columns(schema, provider, {"public.orders": ["loyalty_tier"]}, context)
        kwargs = provider.generate_structured.await_args.kwargs
        assert kwargs["schema_type"] is ColumnsBatchResponse
        prompt = kwargs["user_message"]
        assert "- public.orders: loyalty_tier (table: Customer purchases)\n" in prompt

    async def test_the_prompt_ddl_holds_only_the_requested_tables(self) -> None:
        schema = _schema_with_tables(["orders", "customers"], extra_columns=["loyalty_tier"])
        provider = _columns_provider({})
        await _generate_new_columns(schema, provider, {"public.orders": ["loyalty_tier"]})
        prompt = provider.generate_structured.await_args.kwargs["user_message"]
        assert "CREATE TABLE public.orders (" in prompt
        assert "CREATE TABLE public.customers" not in prompt

    async def test_a_description_with_a_newline_and_a_colon_stays_on_its_target_line(
        self,
    ) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["loyalty_tier", "region"])
        provider = _columns_provider({})
        context = {
            "public.orders": TableSemantic(
                display_name="Orders", description="Purchases:\n  one row per order (EU)"
            )
        }
        await _generate_new_columns(
            schema, provider, {"public.orders": ["loyalty_tier", "region"]}, context
        )
        prompt = provider.generate_structured.await_args.kwargs["user_message"]
        targets = prompt.split("NEW COLUMNS (annotate only these):\n", 1)[1]
        targets = targets.split("\n\nFor each new column", 1)[0]
        assert targets == (
            "- public.orders: loyalty_tier, region (table: Purchases: one row per order (EU))"
        )

    async def test_a_whitespace_only_description_is_left_off_the_target_line(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["loyalty_tier"])
        provider = _columns_provider({})
        context = {"public.orders": TableSemantic(display_name="Orders", description="  \n ")}
        await _generate_new_columns(schema, provider, {"public.orders": ["loyalty_tier"]}, context)
        prompt = provider.generate_structured.await_args.kwargs["user_message"]
        assert "- public.orders: loyalty_tier\n" in prompt
        assert "(table:" not in prompt

    async def test_a_name_the_table_does_not_have_is_left_out_of_the_targets(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["loyalty_tier"])
        provider = _columns_provider({})
        await _generate_new_columns(
            schema, provider, {"public.orders": ["loyalty_tier", "not_a_column"]}
        )
        prompt = provider.generate_structured.await_args.kwargs["user_message"]
        assert "- public.orders: loyalty_tier\n" in prompt
        assert "not_a_column" not in prompt

    async def test_only_edges_touching_the_batch_reach_the_prompt(self) -> None:
        schema = _schema_with_tables(
            ["orders", "customers", "items", "tags"], extra_columns=["loyalty_tier"]
        )

        def edge(a: str, b: str) -> RelationshipEdge:
            return RelationshipEdge(
                from_table=f"public.{a}",
                from_column="id",
                to_table=f"public.{b}",
                to_column="id",
                join_sql=f"public.{a}.id = public.{b}.id",
            )

        provider = _columns_provider({})
        await _generate_new_columns(
            schema,
            provider,
            {"public.orders": ["loyalty_tier"]},
            edges=[edge("orders", "customers"), edge("items", "tags")],
        )
        prompt = provider.generate_structured.await_args.kwargs["user_message"]
        relationships = prompt.split("\n\n", 1)[0]
        assert relationships.startswith("DETECTED RELATIONSHIPS")
        assert "public.orders.id → public.customers.id" in relationships
        assert "public.items.id" not in prompt

    async def test_a_bare_table_key_in_the_reply_is_remapped_to_the_real_one(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["loyalty_tier"])
        provider = _columns_provider(
            {"orders": {"loyalty_tier": ColumnSemantic(display_name="Tier")}}
        )
        out = await _generate_new_columns(schema, provider, {"public.orders": ["loyalty_tier"]})
        assert set(out) == {"public.orders"}
        assert out["public.orders"]["loyalty_tier"].display_name == "Tier"

    async def test_a_reply_for_a_table_outside_the_batch_is_dropped(self) -> None:
        schema = _schema_with_tables(["orders", "customers"], extra_columns=["loyalty_tier"])
        provider = _columns_provider(
            {
                "public.orders": {"loyalty_tier": ColumnSemantic(display_name="Tier")},
                "public.customers": {"loyalty_tier": ColumnSemantic(display_name="Other")},
            }
        )
        out = await _generate_new_columns(schema, provider, {"public.orders": ["loyalty_tier"]})
        assert set(out) == {"public.orders"}

    async def test_a_table_outside_the_schema_makes_no_llm_call(self) -> None:
        provider = MagicMock()
        provider.generate_structured = AsyncMock()
        out = await _generate_new_columns(
            _schema_with_tables(["orders"]), provider, {"public.ghost": ["x"]}
        )
        assert out == {}
        provider.generate_structured.assert_not_awaited()

    async def test_a_column_the_table_does_not_have_is_not_asked_for(self) -> None:
        provider = MagicMock()
        provider.generate_structured = AsyncMock()
        out = await _generate_new_columns(
            _schema_with_tables(["orders"]), provider, {"public.orders": ["not_a_column"]}
        )
        assert out == {}
        provider.generate_structured.assert_not_awaited()

    async def test_a_near_miss_column_name_is_remapped_to_the_real_one(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["loyalty_tier"])
        provider = _columns_provider(
            {"public.orders": {"Loyalty_Tier": ColumnSemantic(display_name="Tier")}}
        )
        out = await _generate_new_columns(schema, provider, {"public.orders": ["loyalty_tier"]})
        assert set(out["public.orders"]) == {"loyalty_tier"}

    async def test_an_llm_failure_raises_value_error(self) -> None:
        provider = MagicMock()
        provider.max_output_tokens = 8000
        provider.generate_structured = AsyncMock(side_effect=RuntimeError("[RATE_LIMIT] slow"))
        schema = _schema_with_tables(["orders"], extra_columns=["loyalty_tier"])
        with pytest.raises(ValueError, match="generate_new_columns") as excinfo:
            await _generate_new_columns(schema, provider, {"public.orders": ["loyalty_tier"]})
        assert "[RATE_LIMIT]" in str(excinfo.value)

    async def test_the_fiscal_audit_timestamp_rule_applies_to_a_new_created_at(self) -> None:
        schema = _schema_with_tables(
            ["Facts"], extra_columns=["fiscal_year", "fiscal_quarter", "created_at"]
        )
        provider = _columns_provider(
            {"public.Facts": {"created_at": ColumnSemantic(display_name="Created")}}
        )
        out = await _generate_new_columns(schema, provider, {"public.Facts": ["created_at"]})
        description = out["public.Facts"]["created_at"].description
        assert description is not None and "NOT a business period date" in description

    def test_the_new_and_table_prompts_share_the_column_field_instructions(self) -> None:
        from app.semantic.generator import (
            _BATCH_TABLES_PROMPT,
            _COLUMN_FIELD_ITEMS,
            _NEW_COLUMNS_PROMPT,
        )

        assert _COLUMN_FIELD_ITEMS in _BATCH_TABLES_PROMPT
        assert _COLUMN_FIELD_ITEMS in _NEW_COLUMNS_PROMPT

    def test_table_ddl_sizes_count_each_requested_table_as_the_ddl_limit_does(self) -> None:
        schema = _schema_with_tables(["orders", "customers"], extra_columns=["a_long_column_name"])
        gen = SemanticModelGenerator()
        sizes = gen.table_ddl_sizes(schema, ["public.orders", "public.ghost"])
        assert set(sizes) == {"public.orders"}
        assert sizes["public.orders"] == len(gen._table_ddl_block(schema.tables[0]))
        # A batch whose sizes sum to the limit renders whole; one character less and
        # ``_schema_to_ddl`` drops the last table.
        both = gen.table_ddl_sizes(schema, ["public.orders", "public.customers"])
        gen._MAX_DDL_CHARS = sum(both.values())
        assert "omitted" not in gen._schema_to_ddl(schema)
        gen._MAX_DDL_CHARS -= 1
        assert "1 additional table(s) omitted" in gen._schema_to_ddl(schema)


# ── Value mappings are kept only for columns whose values may reach the LLM ────


_PENDING = ValueMapping(raw_value="P", display_value="Pending")


def _status_table(column: str = "status") -> dict[str, TableSemantic]:
    return {
        "public.orders": TableSemantic(
            display_name="Orders",
            columns={
                "id": ColumnSemantic(display_name="ID"),
                column: ColumnSemantic(display_name="Status", value_mappings=[_PENDING]),
            },
        )
    }


class TestValueMappingsGuard:
    """A proposed mapping survives only when ``PrivacySettings.column_values_allowed``
    admits the column: no sample values, no generated mappings."""

    async def test_no_privacy_settings_drops_mappings(self) -> None:
        schema = _schema_with_tables(["orders"], ["status"])
        out = await _generate(schema, _provider_returning(_status_table()), ["public.orders"])
        status = out["public.orders"].columns["status"]
        assert status.value_mappings == []
        assert status.display_name == "Status"  # the rest of the annotation stays

    async def test_sample_values_off_drops_mappings(self) -> None:
        schema = _schema_with_tables(["orders"], ["status"])
        out = await _generate(
            schema,
            _provider_returning(_status_table()),
            ["public.orders"],
            privacy=PrivacySettings(include_sample_values=False),
        )
        assert out["public.orders"].columns["status"].value_mappings == []

    async def test_sample_values_on_keeps_mappings(self) -> None:
        schema = _schema_with_tables(["orders"], ["status"])
        out = await _generate(
            schema,
            _provider_returning(_status_table()),
            ["public.orders"],
            privacy=PrivacySettings(include_sample_values=True),
        )
        assert out["public.orders"].columns["status"].value_mappings == [_PENDING]

    async def test_sensitive_pattern_drops_mappings_despite_sample_values(self) -> None:
        # "email_status" matches the default sensitive pattern "email": its values may
        # not reach the LLM, so no generated mapping is kept for it.
        schema = _schema_with_tables(["orders"], ["email_status"])
        out = await _generate(
            schema,
            _provider_returning(_status_table("email_status")),
            ["public.orders"],
            privacy=PrivacySettings(include_sample_values=True),
        )
        assert out["public.orders"].columns["email_status"].value_mappings == []

    async def test_new_columns_pass_applies_the_same_rule(self) -> None:
        schema = _schema_with_tables(["orders"], ["status"])
        reply = {
            "public.orders": {"status": ColumnSemantic(display_name="S", value_mappings=[_PENDING])}
        }
        off = await _generate_new_columns(
            schema, _columns_provider(reply), {"public.orders": ["status"]}
        )
        assert off["public.orders"]["status"].value_mappings == []
        on = await _generate_new_columns(
            schema,
            _columns_provider(reply),
            {"public.orders": ["status"]},
            privacy=PrivacySettings(include_sample_values=True),
        )
        assert on["public.orders"]["status"].value_mappings == [_PENDING]

    def test_drop_disallowed_value_mappings_counts_cleared_columns(self) -> None:
        tables = _status_table()
        cleared = SemanticModelGenerator._drop_disallowed_value_mappings(tables, None)
        assert cleared == 1
        assert tables["public.orders"].columns["status"].value_mappings == []
        # Idempotent: nothing left to clear.
        assert SemanticModelGenerator._drop_disallowed_value_mappings(tables, None) == 0


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


def _drop_b(name: str) -> str | None:
    return None if name == "b" else name


def _upper_b(name: str) -> str:
    return "B" if name == "b" else name


class TestReferenceMappers:
    """The one shape rule for a table's reference fields, shared by the prune, the
    dangling-edit trim and the renames: a scalar name, a list of names, or hierarchies."""

    def test_the_names_of_each_shape(self) -> None:
        assert _reference_names("primary_timestamp_column", "ts") == ["ts"]
        assert _reference_names("partition_columns", ["a", "b"]) == ["a", "b"]
        assert _reference_names(
            "hierarchies", [{"name": "H", "levels": ["y", "m"]}, {"name": "E"}]
        ) == ["y", "m"]

    def test_a_name_mapped_to_none_leaves_each_shape(self) -> None:
        assert _map_reference_value("primary_date_column", "b", _drop_b) is None
        assert _map_reference_value("primary_date_column", "a", _drop_b) == "a"
        assert _map_reference_value("cluster_columns", ["a", "b"], _drop_b) == ["a"]
        hierarchies = [{"name": "H", "levels": ["a", "b"]}, {"name": "E", "levels": ["b"]}]
        assert _map_reference_value("hierarchies", hierarchies, _drop_b) == [
            {"name": "H", "levels": ["a"]}
        ]

    def test_a_rename_keeps_every_entry_in_place(self) -> None:
        assert _map_reference_value("partition_columns", ["b", "a"], _upper_b) == ["B", "a"]
        assert _map_reference_value(
            "hierarchies", [{"name": "H", "levels": ["b", "a"], "note": 1}], _upper_b
        ) == [{"name": "H", "levels": ["B", "a"], "note": 1}]

    def test_the_input_is_not_mutated(self) -> None:
        hierarchies = [{"name": "H", "levels": ["a", "b"]}]
        names = ["a", "b"]
        _map_reference_value("hierarchies", hierarchies, _drop_b)
        _map_reference_value("partition_columns", names, _drop_b)
        assert hierarchies == [{"name": "H", "levels": ["a", "b"]}]
        assert names == ["a", "b"]

    def test_a_table_update_covers_every_reference_field(self) -> None:
        table = TableSemantic(
            display_name="T",
            columns={"a": ColumnSemantic(display_name="A")},
            primary_timestamp_column="b",
            primary_date_column="a",
            partition_columns=["a", "b"],
            cluster_columns=["b"],
            hierarchies=[Hierarchy(name="H", levels=["a", "b"]), Hierarchy(name="E", levels=["b"])],
        )
        update = _map_table_references(table, _drop_b)
        assert set(update) == set(_TABLE_REFERENCE_FIELDS)
        assert update == {
            "primary_timestamp_column": None,
            "primary_date_column": "a",
            "partition_columns": ["a"],
            "cluster_columns": [],
            "hierarchies": [Hierarchy(name="H", levels=["a"])],
        }
        renamed = table.model_copy(update=_map_table_references(table, _upper_b))
        assert renamed.primary_timestamp_column == "B"
        assert renamed.partition_columns == ["a", "B"]
        assert renamed.hierarchies == [
            Hierarchy(name="H", levels=["a", "B"]),
            Hierarchy(name="E", levels=["B"]),
        ]
        assert table.partition_columns == ["a", "b"]  # pure

    def test_an_empty_string_name_is_not_a_kept_name(self) -> None:
        table = TableSemantic(display_name="T", primary_timestamp_column="")
        # the prune's rule: a name that is not a live column leaves, and "" is not one
        only_a = _map_table_references(table, lambda name: name if name == "a" else None)
        assert only_a["primary_timestamp_column"] is None
        # a rename maps "" to itself
        assert _map_table_references(table, _upper_b)["primary_timestamp_column"] == ""

    def test_a_rename_keeps_a_hierarchy_without_levels_and_the_prune_drops_it(self) -> None:
        table = TableSemantic(display_name="T", hierarchies=[Hierarchy(name="E", levels=[])])
        kept = _map_table_references(table, _upper_b, drop_empty=False)
        assert kept["hierarchies"] == [Hierarchy(name="E", levels=[])]
        assert _map_table_references(table, _upper_b)["hierarchies"] == []
        assert _map_reference_value("hierarchies", [{"name": "E"}], _upper_b, drop_empty=False) == [
            {"name": "E", "levels": []}
        ]
        assert _map_reference_value("hierarchies", [{"name": "E"}], _upper_b) == []

    def test_an_empty_field_is_left_as_it_is(self) -> None:
        table = TableSemantic(display_name="T")
        assert _map_table_references(table, _drop_b) == {
            "primary_timestamp_column": None,
            "primary_date_column": None,
            "partition_columns": [],
            "cluster_columns": [],
            "hierarchies": [],
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


class TestRenameColumns:
    """The drift's renames, applied: each near-miss key becomes its live column's name,
    and everything that names the column by key follows it."""

    _RENAMES: ClassVar[dict[str, dict[str, str]]] = {"public.orders": {'"Status"': "status"}}

    @staticmethod
    def _orders() -> TableSemantic:
        return TableSemantic(
            display_name="Orders",
            columns={
                "id": ColumnSemantic(display_name="Id"),
                '"Status"': ColumnSemantic(display_name="Hand-written", description="kept"),
            },
            primary_timestamp_column='"Status"',
            primary_date_column='"Status"',
            partition_columns=["id", '"Status"'],
            cluster_columns=['"Status"'],
            hierarchies=[Hierarchy(name="H", levels=["id", '"Status"'])],
            default_filters=['"Status" = 1'],
        )

    @staticmethod
    def _snapshot() -> tuple[dict[str, dict[str, list[str]]], dict[str, dict[str, Any]]]:
        marks = {
            "model": {},
            "table": {"public.orders": ["primary_timestamp_column", "hierarchies"]},
            "column": {
                'public.orders."Status"': ["display_name", "description"],
                "public.orders.id": ["display_name"],
            },
            "metric": {},
            "global_item": {},
            "relationship": {'public.orders."Status"->public.statuses.code': ["description"]},
        }
        protected = {
            "table": {
                "public.orders": {
                    "primary_timestamp_column": '"Status"',
                    "hierarchies": [{"name": "H", "levels": ["id", '"Status"']}],
                }
            },
            "column": {
                'public.orders."Status"': {"display_name": "Hand-written", "description": "kept"},
                "public.orders.id": {"display_name": "Id"},
            },
        }
        return marks, protected

    def _rename(self, relationships: list[RelationshipEdge] | None = None):
        marks, protected = self._snapshot()
        return rename_columns(
            {"public.orders": self._orders()}, relationships or [], marks, protected, self._RENAMES
        )

    def test_the_column_is_rekeyed_with_its_annotation(self) -> None:
        tables, _, _, _ = self._rename()
        columns = tables["public.orders"].columns
        assert list(columns) == ["id", "status"]
        assert columns["status"] == ColumnSemantic(display_name="Hand-written", description="kept")

    def test_every_reference_field_follows_the_rename(self) -> None:
        tables, _, _, _ = self._rename()
        orders = tables["public.orders"]
        assert orders.primary_timestamp_column == "status"
        assert orders.primary_date_column == "status"
        assert orders.partition_columns == ["id", "status"]
        assert orders.cluster_columns == ["status"]
        assert orders.hierarchies == [Hierarchy(name="H", levels=["id", "status"])]

    def test_free_sql_is_not_rewritten(self) -> None:
        tables, _, _, _ = self._rename()
        assert tables["public.orders"].default_filters == ['"Status" = 1']

    def test_relationship_endpoints_on_the_table_follow_the_rename(self) -> None:
        out_of = _edge("public.orders", '"Status"', "public.statuses", "code")
        into = _edge("public.statuses", "code", "public.orders", '"Status"')
        other = _edge("public.orders", "id", "public.statuses", "code")
        _, relationships, _, _ = self._rename([out_of, into, other])
        assert [(e.from_column, e.to_column) for e in relationships] == [
            ("status", "code"),
            ("code", "status"),
            ("id", "code"),
        ]
        # The join SQL is free SQL, written as stored.
        assert relationships[0].join_sql == out_of.join_sql
        assert relationships[2] is other

    def test_an_endpoint_of_the_same_name_on_another_table_is_not_renamed(self) -> None:
        edge = _edge("public.statuses", '"Status"', "public.orders", "id")
        _, relationships, _, _ = self._rename([edge])
        assert relationships == [edge]

    def test_a_renamed_edge_that_duplicates_a_held_one_is_dropped(self) -> None:
        """The edge already stored under the real names wins, as the column stored under
        its real name does; the near-miss edge's marks go with it."""
        stale = _edge("public.orders", '"Status"', "public.statuses", "code")
        real = _edge("public.orders", "status", "public.statuses", "code")
        for edges in ([stale, real], [real, stale]):
            _, relationships, marks, _ = self._rename(edges)
            assert relationships == [real]
            assert marks["relationship"] == {}

    def test_column_marks_and_protected_values_move_to_the_new_key(self) -> None:
        _, _, marks, protected = self._rename()
        assert marks["column"] == {
            "public.orders.status": ["display_name", "description"],
            "public.orders.id": ["display_name"],
        }
        assert protected["column"] == {
            "public.orders.status": {"display_name": "Hand-written", "description": "kept"},
            "public.orders.id": {"display_name": "Id"},
        }

    def test_a_table_edit_naming_the_column_is_rewritten(self) -> None:
        _, _, marks, protected = self._rename()
        assert marks["table"] == {"public.orders": ["primary_timestamp_column", "hierarchies"]}
        assert protected["table"] == {
            "public.orders": {
                "primary_timestamp_column": "status",
                "hierarchies": [{"name": "H", "levels": ["id", "status"]}],
            }
        }

    def test_a_relationship_mark_follows_its_edge(self) -> None:
        edge = _edge("public.orders", '"Status"', "public.statuses", "code")
        _, _, marks, _ = self._rename([edge])
        assert marks["relationship"] == {
            "public.orders.status->public.statuses.code": ["description"]
        }

    def test_the_inputs_are_not_mutated(self) -> None:
        tables = {"public.orders": self._orders()}
        edges = [_edge("public.orders", '"Status"', "public.statuses", "code")]
        marks, protected = self._snapshot()
        before = (
            {k: t.model_copy(deep=True) for k, t in tables.items()},
            [e.model_copy() for e in edges],
            self._snapshot(),
        )
        rename_columns(tables, edges, marks, protected, self._RENAMES)
        assert (tables, edges, (marks, protected)) == before

    def test_no_renames_change_nothing(self) -> None:
        marks, protected = self._snapshot()
        tables = {"public.orders": self._orders()}
        edges = [_edge("public.orders", '"Status"', "public.statuses", "code")]
        assert rename_columns(tables, edges, marks, protected, {}) == (
            tables,
            edges,
            marks,
            protected,
        )


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
            'Column key "public.orders."status"" in the semantic model is a near-miss '
            'spelling of "status"; Apply Drift removes the duplicate'
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


class TestDetectDriftNewColumns:
    """With a baseline (``compute_drift``'s ``known_columns``), Check Drift names the live
    columns the model has never seen, and words each removed near-miss key by what the
    apply does to it."""

    _KNOWN: ClassVar[dict[str, set[str]]] = {"public.orders": {"id"}}
    # What every baseline writer records for a table with live ``id`` and ``status``.
    _KNOWN_STATUS: ClassVar[dict[str, set[str]]] = {"public.orders": {"id", "status"}}

    @staticmethod
    def _warnings(
        schema: DataSourceSchema, model: SemanticModel, known: dict[str, set[str]]
    ) -> list[str]:
        """``detect_drift`` over the diff against *known*, as the drift route passes it."""
        drift = compute_drift(schema, model, None, known)
        return SemanticModelGenerator().detect_drift(schema, model, drift=drift)

    @staticmethod
    def _rename(key: str, target: str) -> str:
        return (
            f'Column key "public.orders.{key}" in the semantic model is a near-miss spelling '
            f'of "{target}"; Apply Drift renames it to "{target}", keeping its annotation'
        )

    def test_a_new_column_on_a_known_table_is_reported(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["loyalty_tier", "region"])
        model = SemanticModel(tables={"public.orders": _tbl("id")})
        warnings = self._warnings(schema, model, self._KNOWN)
        assert warnings == [
            "2 new column(s) added to tables in the semantic model since it was generated: "
            "public.orders.loyalty_tier, public.orders.region"
        ]

    def test_a_column_the_baseline_knows_is_not_reported(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["notes"])
        model = SemanticModel(tables={"public.orders": _tbl("id")})
        known = {"public.orders": {"id", "notes"}}
        assert self._warnings(schema, model, known) == []

    def test_no_new_column_no_warning(self) -> None:
        schema = _schema_with_tables(["orders"])
        model = SemanticModel(tables={"public.orders": _tbl("id")})
        assert self._warnings(schema, model, self._KNOWN) == []

    def test_without_a_baseline_no_column_is_reported_new(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["loyalty_tier"])
        model = SemanticModel(tables={"public.orders": _tbl("id")})
        assert SemanticModelGenerator().detect_drift(schema, model) == []

    def test_a_passed_drift_is_the_one_worded(self) -> None:
        """A diff of its own has no baseline and would report no new column: the caller's
        drift, which names one, is what the warnings word."""
        schema = _schema_with_tables(["orders"], extra_columns=["loyalty_tier"])
        model = SemanticModel(tables={"public.orders": _tbl("id")})
        drift = SchemaDrift(added_columns={"public.orders": {"loyalty_tier"}})
        assert SemanticModelGenerator().detect_drift(schema, model, drift=drift) == [
            "1 new column(s) added to tables in the semantic model since it was generated: "
            "public.orders.loyalty_tier"
        ]

    def test_the_warning_lists_at_most_five_names(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=[f"c{i}" for i in range(7)])
        model = SemanticModel(tables={"public.orders": _tbl("id")})
        [warning] = self._warnings(schema, model, self._KNOWN)
        assert "7 new column(s)" in warning
        assert warning.endswith("…")
        assert warning.count("public.orders.c") == 5

    def test_a_quoted_key_held_without_its_bare_twin_is_a_rename(self) -> None:
        """The apply renames ``'"status"'`` to ``status``, annotation and all: one warning
        says so. The column is neither gone nor new since the model was generated."""
        schema = _schema_with_tables(["orders"], extra_columns=["status"])
        model = SemanticModel(tables={"public.orders": _tbl("id", '"status"')})
        warnings = self._warnings(schema, model, self._KNOWN_STATUS)
        assert warnings == [self._rename('"status"', "status")]

    def test_a_case_only_key_held_without_its_live_twin_is_a_rename(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["status"])
        model = SemanticModel(tables={"public.orders": _tbl("id", "Status")})
        warnings = self._warnings(schema, model, self._KNOWN_STATUS)
        assert warnings == [self._rename("Status", "status")]

    def test_a_renamed_column_is_left_out_of_the_new_columns_warning(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["status", "region"])
        model = SemanticModel(tables={"public.orders": _tbl("id", '"status"')})
        warnings = self._warnings(schema, model, self._KNOWN_STATUS)
        assert warnings == [
            self._rename('"status"', "status"),
            "1 new column(s) added to tables in the semantic model since it was generated: "
            "public.orders.region",
        ]

    def test_a_second_near_miss_key_of_a_renamed_column_is_a_duplicate(self) -> None:
        """Two keys spell ``status``: the first in sort order is renamed, and the other is
        a duplicate of the column it becomes."""
        schema = _schema_with_tables(["orders"], extra_columns=["status"])
        model = SemanticModel(tables={"public.orders": _tbl("id", "Status", '"status"')})
        warnings = self._warnings(schema, model, self._KNOWN_STATUS)
        assert warnings == [
            'Column key "public.orders.Status" in the semantic model is a near-miss spelling '
            'of "status"; Apply Drift removes the duplicate',
            self._rename('"status"', "status"),
        ]

    def test_a_near_miss_key_beside_its_stored_twin_is_a_duplicate(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["status"])
        model = SemanticModel(tables={"public.orders": _tbl("id", "status", "Status")})
        warnings = self._warnings(schema, model, self._KNOWN_STATUS)
        assert warnings == [
            'Column key "public.orders.Status" in the semantic model is a near-miss spelling '
            'of "status"; Apply Drift removes the duplicate'
        ]

    def test_a_dropped_column_the_baseline_knew_is_gone_not_renamed(self) -> None:
        """The database had ``Status`` and ``status`` and dropped ``Status``: a drop, so
        ``status`` (seen, and not held) is not new either."""
        schema = _schema_with_tables(["orders"], extra_columns=["status"])
        model = SemanticModel(tables={"public.orders": _tbl("id", "Status")})
        known = {"public.orders": {"id", "Status", "status"}}
        warnings = self._warnings(schema, model, known)
        assert warnings == [
            'Column "public.orders.Status" referenced in semantic model no longer exists'
        ]

    def test_a_case_only_rename_in_the_database_is_a_drop_and_a_new_column(self) -> None:
        """The baseline saw ``Status`` but never ``status``: the database renamed it, so
        ``Status`` is gone and ``status`` is new, not a rename target."""
        schema = _schema_with_tables(["orders"], extra_columns=["status"])
        model = SemanticModel(tables={"public.orders": _tbl("id", "Status")})
        known = {"public.orders": {"id", "Status"}}
        warnings = self._warnings(schema, model, known)
        assert warnings == [
            'Column "public.orders.Status" referenced in semantic model no longer exists',
            "1 new column(s) added to tables in the semantic model since it was generated: "
            "public.orders.status",
        ]

    def test_a_dropped_column_beside_its_stored_case_twin_is_gone_not_a_duplicate(
        self,
    ) -> None:
        """The table had ``Status`` and ``status`` and the model held both. The database
        dropped ``Status``: the baseline knew it as a real column, so it is gone, not a
        near-miss spelling of ``status``."""
        schema = _schema_with_tables(["orders"], extra_columns=["status"])
        model = SemanticModel(tables={"public.orders": _tbl("id", "Status", "status")})
        known = {"public.orders": {"id", "Status", "status"}}
        warnings = self._warnings(schema, model, known)
        assert warnings == [
            'Column "public.orders.Status" referenced in semantic model no longer exists'
        ]

    def test_a_dropped_quoted_column_is_named_verbatim(self) -> None:
        """The baseline recorded ``'"weird"'`` as a live column, so that is its real name:
        not quote-stripped like a blob-era key."""
        schema = _schema_with_tables(["orders"])
        model = SemanticModel(tables={"public.orders": _tbl("id", '"weird"')})
        known = {"public.orders": {"id", '"weird"'}}
        warnings = self._warnings(schema, model, known)
        assert warnings == [
            'Column "public.orders."weird"" referenced in semantic model no longer exists'
        ]

    def test_two_dropped_columns_that_differ_only_by_quotes_are_two_warnings(self) -> None:
        schema = _schema_with_tables(["orders"])
        model = SemanticModel(tables={"public.orders": _tbl("id", '"a"', "a")})
        known = {"public.orders": {"id", '"a"', "a"}}
        warnings = self._warnings(schema, model, known)
        assert warnings == [
            'Column "public.orders."a"" referenced in semantic model no longer exists',
            'Column "public.orders.a" referenced in semantic model no longer exists',
        ]

    def test_an_ambiguous_case_only_key_is_gone(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["Status", "status"])
        model = SemanticModel(tables={"public.orders": _tbl("id", "STATUS")})
        known = {"public.orders": {"id", "Status", "status"}}
        warnings = self._warnings(schema, model, known)
        assert warnings == [
            'Column "public.orders.STATUS" referenced in semantic model no longer exists'
        ]

    @staticmethod
    def _unseen(key: str, target: str) -> str:
        return (
            f'Column key "public.orders.{key}" in the semantic model is a near-miss spelling '
            f'of "{target}", a column the model has not seen; Apply Drift removes the key'
        )

    def test_a_near_miss_key_of_a_column_the_baseline_never_saw_is_removed_beside_it(
        self,
    ) -> None:
        """The baseline lacks ``status`` (the column arrived after the entry was written,
        or the entry predates the recording of hidden names and the column was just
        un-hidden): the key is not renamed, so the apply removes it and annotates
        ``status`` afresh. Worded as that, never as a column that no longer exists."""
        schema = _schema_with_tables(["orders"], extra_columns=["status"])
        model = SemanticModel(tables={"public.orders": _tbl("id", '"status"')})
        warnings = self._warnings(schema, model, self._KNOWN)
        assert warnings == [
            self._unseen('"status"', "status"),
            "1 new column(s) added to tables in the semantic model since it was generated: "
            "public.orders.status",
        ]

    def test_a_case_only_key_of_a_column_the_baseline_never_saw_is_unseen(self) -> None:
        schema = _schema_with_tables(["orders"], extra_columns=["status"])
        model = SemanticModel(tables={"public.orders": _tbl("id", "Status")})
        warnings = self._warnings(schema, model, self._KNOWN)
        assert warnings == [
            self._unseen("Status", "status"),
            "1 new column(s) added to tables in the semantic model since it was generated: "
            "public.orders.status",
        ]

    def test_without_a_baseline_a_quoted_key_of_an_unheld_live_column_is_unseen(self) -> None:
        """No baseline, so nothing is reported new and the apply drops the key: still a
        near-miss key of a column the model has not seen, not a gone column."""
        schema = _schema_with_tables(["orders"], extra_columns=["status"])
        model = SemanticModel(tables={"public.orders": _tbl("id", '"status"')})
        warnings = SemanticModelGenerator().detect_drift(schema, model)
        assert warnings == [self._unseen('"status"', "status")]

    def test_an_unseen_near_miss_key_is_worded_after_the_duplicates(self) -> None:
        """Gone columns, then duplicates, then unseen near-miss keys, then renames, then
        new columns. ``_column_renames`` walks the gone keys in sort order: ``'"region"'``
        (its column is unseen, so no rename), then ``'"status"'`` (renamed to ``status``),
        then ``Status`` (``status`` is taken: a duplicate); ``gone`` is a baseline drop."""
        schema = _schema_with_tables(["orders"], extra_columns=["status", "region"])
        model = SemanticModel(
            tables={"public.orders": _tbl("id", "Status", '"status"', '"region"', "gone")}
        )
        known = {"public.orders": {"id", "status", "gone"}}
        warnings = self._warnings(schema, model, known)
        assert warnings == [
            'Column "public.orders.gone" referenced in semantic model no longer exists',
            'Column key "public.orders.Status" in the semantic model is a near-miss spelling '
            'of "status"; Apply Drift removes the duplicate',
            self._unseen('"region"', "region"),
            self._rename('"status"', "status"),
            "1 new column(s) added to tables in the semantic model since it was generated: "
            "public.orders.region",
        ]


# ── Metric filter literals against value mappings ─────────────────────────────


def _metric(
    *filters: str, related: list[str] | None = None, measure: list[str] | None = None
) -> SimpleMetric:
    return SimpleMetric(
        name="Revenue",
        definition="orders.total",
        aggregation=AggregationType.SUM,
        filters=list(filters),
        measure_filters=measure or [],
        related_tables=related or [],
    )


def _with_invoices() -> dict[str, TableSemantic]:
    """``orders.status`` (P) next to ``invoices.status`` (paid, open)."""
    return {
        **_status_table(),
        "public.invoices": TableSemantic(
            display_name="Invoices",
            columns={
                "status": ColumnSemantic(
                    display_name="S",
                    value_mappings=[
                        ValueMapping(raw_value="paid", display_value="Paid"),
                        ValueMapping(raw_value="open", display_value="Open"),
                    ],
                )
            },
        ),
    }


class TestValidateMetricFilterValues:
    def test_unmapped_literal_is_reported(self, gen: SemanticModelGenerator) -> None:
        out = gen._validate_metric_filter_values(
            [_metric("orders.status = 'completed'")], _status_table()
        )
        assert out == [
            "Metric \"Revenue\" compares public.orders.status with 'completed', but the value "
            "mappings of public.orders.status list only 'P'. Check the value against the "
            "database and correct the filter."
        ]

    def test_mapped_literal_is_silent(self, gen: SemanticModelGenerator) -> None:
        assert (
            gen._validate_metric_filter_values(
                [_metric("status = 'P'", related=["public.orders"])], _status_table()
            )
            == []
        )

    def test_in_list_reports_only_the_unmapped_values(self, gen: SemanticModelGenerator) -> None:
        out = gen._validate_metric_filter_values(
            [_metric("o.status NOT IN ('P', 'done')", related=["public.orders"])], _status_table()
        )
        assert len(out) == 1 and "'done'" in out[0] and "'P', " not in out[0].split("list only")[0]

    def test_not_equal_on_an_unqualified_column(self, gen: SemanticModelGenerator) -> None:
        out = gen._validate_metric_filter_values(
            [_metric("status <> 'void'", "status != 'gone'", related=["orders"])], _status_table()
        )
        assert len(out) == 2 and "'void'" in out[0] and "'gone'" in out[1]

    def test_measure_filters_are_checked_too(self, gen: SemanticModelGenerator) -> None:
        out = gen._validate_metric_filter_values(
            [_metric(measure=["status = 'shipped'"], related=["public.orders"])], _status_table()
        )
        assert len(out) == 1 and "'shipped'" in out[0]

    def test_column_without_mappings_is_skipped(self, gen: SemanticModelGenerator) -> None:
        tables = {"public.orders": _tbl("id", "status")}
        assert (
            gen._validate_metric_filter_values(
                [_metric("status = 'x'", related=["public.orders"])], tables
            )
            == []
        )

    def test_related_tables_scope_the_lookup(self, gen: SemanticModelGenerator) -> None:
        # ``status`` on invoices has other mappings; the metric is about orders only.
        tables = _with_invoices()
        assert (
            gen._validate_metric_filter_values(
                [_metric("status = 'paid'", related=["invoices"])], tables
            )
            == []
        )
        out = gen._validate_metric_filter_values(
            [_metric("status = 'paid'", related=["public.orders"])], tables
        )
        assert len(out) == 1 and "public.orders.status" in out[0]

    def test_an_alias_falls_back_to_the_related_tables(self, gen: SemanticModelGenerator) -> None:
        out = gen._validate_metric_filter_values(
            [_metric("o.status = 'paid'", related=["public.orders"])], _with_invoices()
        )
        assert len(out) == 1 and "public.orders.status" in out[0] and "'paid'" in out[0]

    def test_a_table_qualifier_beats_related_tables(self, gen: SemanticModelGenerator) -> None:
        tables = _with_invoices()
        silent = _metric("invoices.status = 'paid'", related=["public.orders"])
        assert gen._validate_metric_filter_values([silent], tables) == []
        loud = _metric('public."Invoices"."status" = \'void\'', related=["public.orders"])
        out = gen._validate_metric_filter_values([loud], tables)
        assert len(out) == 1
        assert "public.invoices.status" in out[0] and "'open', 'paid'" in out[0]

    def test_ambiguous_column_is_skipped(self, gen: SemanticModelGenerator) -> None:
        # Both related tables map ``status`` differently: the check does not pick one.
        assert (
            gen._validate_metric_filter_values(
                [_metric("status = 'x'", related=["orders", "invoices"])], _with_invoices()
            )
            == []
        )

    def test_quoted_and_qualified_columns_resolve(self, gen: SemanticModelGenerator) -> None:
        out = gen._validate_metric_filter_values(
            [_metric("public.orders.\"status\" <> 'void'")], _status_table()
        )
        assert len(out) == 1 and "'void'" in out[0]

    def test_the_column_keeps_its_stored_case(self, gen: SemanticModelGenerator) -> None:
        out = gen._validate_metric_filter_values(
            [_metric("status = 'x'", related=["public.orders"])], _status_table(column="Status")
        )
        assert len(out) == 1 and "public.orders.Status" in out[0]

    def test_repeats_are_reported_once(self, gen: SemanticModelGenerator) -> None:
        out = gen._validate_metric_filter_values(
            [
                _metric(
                    "status IN ('x', 'x', 'P')",
                    "status = 'x'",
                    measure=["status IN ('x')"],
                    related=["public.orders"],
                )
            ],
            _status_table(),
        )
        assert len(out) == 1 and out[0].count("'x'") == 1

    def test_a_long_allowed_list_is_cut_at_five(self, gen: SemanticModelGenerator) -> None:
        tables = {
            "public.orders": TableSemantic(
                display_name="Orders",
                columns={
                    "status": ColumnSemantic(
                        display_name="S",
                        value_mappings=[
                            ValueMapping(raw_value=v, display_value=v.upper()) for v in "gfedcba"
                        ],
                    )
                },
            )
        }
        (out,) = gen._validate_metric_filter_values(
            [_metric("status = 'z'", related=["public.orders"])], tables
        )
        assert "list only 'a', 'b', 'c', 'd', 'e', …. Check" in out

    def test_a_column_with_no_table_in_scope_is_not_checked(
        self, gen: SemanticModelGenerator
    ) -> None:
        """No qualifier (or an alias) and no annotated related table: a same-named
        column on an unrelated table must not be read as the metric's."""
        tables = {**_status_table(), "public.payments": _tbl("id", "status")}
        assert gen._validate_metric_filter_values([_metric("status = 'refund'")], tables) == []
        assert gen._validate_metric_filter_values([_metric("p.status = 'refund'")], tables) == []
        # ``events`` is related but has no annotation: nothing to fall back to either.
        assert (
            gen._validate_metric_filter_values(
                [_metric("status = 'refund'", related=["events"])], _status_table()
            )
            == []
        )

    def test_conditions_joined_by_and_or_are_each_checked(
        self, gen: SemanticModelGenerator
    ) -> None:
        out = gen._validate_metric_filter_values(
            [
                _metric(
                    "o.total > 10 AND o.status = 'x' OR o.status IN ('P', 'y')", related=["orders"]
                )
            ],
            _status_table(),
        )
        assert len(out) == 2
        assert "'x'" in out[0] and "'y'" in out[1] and "'P'" not in out[1].split("list only")[0]

    def test_operators_are_case_insensitive(self, gen: SemanticModelGenerator) -> None:
        out = gen._validate_metric_filter_values(
            [_metric("status not in ('P', 'z')", related=["orders"])], _status_table()
        )
        assert len(out) == 1 and "'z'" in out[0]

    def test_only_string_literals_against_a_column_are_judged(
        self, gen: SemanticModelGenerator
    ) -> None:
        silent = [
            "status IS NULL",
            "status IS NOT NULL",
            "status LIKE 'pend%'",
            "status = other_col",
            "status = 3",
            "status IN (1, 2)",
            "status IN (SELECT 'x')",
            "lower(status) = 'x'",
            "'x' = status",
            "total > 100",
            "",
        ]
        for clause in silent:
            assert (
                gen._validate_metric_filter_values(
                    [_metric(clause, related=["orders"])], _status_table()
                )
                == []
            ), clause

    def test_literals_compare_exactly_and_keep_inner_spaces(
        self, gen: SemanticModelGenerator
    ) -> None:
        out = gen._validate_metric_filter_values(
            [_metric("status = 'p'", "status = ' P'", related=["orders"])], _status_table()
        )
        assert len(out) == 2 and "'p'" in out[0] and "' P'" in out[1]

    def test_two_metrics_with_the_same_mistake_each_get_a_warning(
        self, gen: SemanticModelGenerator
    ) -> None:
        a = _metric("status = 'x'", related=["orders"]).model_copy(update={"name": "A"})
        b = _metric("status = 'x'", related=["orders"]).model_copy(update={"name": "B"})
        out = gen._validate_metric_filter_values([a, b], _status_table())
        assert [w.split('"')[1] for w in out] == ["A", "B"]

    def test_no_metrics_no_tables_and_no_filters_are_silent(
        self, gen: SemanticModelGenerator
    ) -> None:
        assert gen._validate_metric_filter_values([], _status_table()) == []
        assert gen._validate_metric_filter_values([_metric("status = 'x'")], {}) == []
        assert gen._validate_metric_filter_values([_metric()], _status_table()) == []

    def test_a_derived_metric_without_filters_is_skipped(self, gen: SemanticModelGenerator) -> None:
        from app.semantic.models import DerivedMetric

        derived = DerivedMetric(name="Margin", definition="revenue - cost")
        assert gen._validate_metric_filter_values([derived], _status_table()) == []

    def test_same_value_set_on_two_related_tables_is_not_ambiguous(
        self, gen: SemanticModelGenerator
    ) -> None:
        """Two tables listing the same raw values give one candidate: the check judges."""
        tables = {
            **_status_table(),
            "public.invoices": TableSemantic(
                display_name="Invoices",
                columns={"status": ColumnSemantic(display_name="S", value_mappings=[_PENDING])},
            ),
        }
        out = gen._validate_metric_filter_values(
            [_metric("status = 'x'", related=["orders", "invoices"])], tables
        )
        assert len(out) == 1 and "public.orders.status" in out[0]


class TestRelationshipGraphDescriptions:
    """The edge text is what the LLM reads next to the RELATIONSHIPS header: a required FK no
    longer promises that INNER JOIN is safe in either direction."""

    @staticmethod
    def _edges(*, nullable: bool) -> list[RelationshipEdge]:
        def table(name: str, cols: list[ColumnInfo]) -> TableInfo:
            return TableInfo(
                catalog=None, schema_name="public", name=name, table_type="table", columns=cols
            )

        schema = DataSourceSchema(
            source_type="postgresql",
            schemas=[SchemaInfo(name="public")],
            tables=[
                table(
                    "orders",
                    [
                        ColumnInfo(name="id", data_type="integer", native_type="int4"),
                        ColumnInfo(
                            name="customer_id",
                            data_type="integer",
                            native_type="int4",
                            nullable=nullable,
                        ),
                    ],
                ),
                table(
                    "customers", [ColumnInfo(name="id", data_type="integer", native_type="int4")]
                ),
            ],
            relationships=[
                RelationshipInfo(
                    from_schema="public",
                    from_table="orders",
                    from_column="customer_id",
                    to_schema="public",
                    to_table="customers",
                    to_column="id",
                )
            ],
        )
        return SemanticModelGenerator()._build_relationship_graph(schema)

    def test_a_not_null_fk_is_required_and_warns_about_a_filtered_parent(self) -> None:
        (edge,) = self._edges(nullable=False)
        assert edge.is_required is True
        assert edge.description == (
            "Required FK — every public.orders row has a public.customers key; "
            "INNER JOIN keeps every public.orders row unless the query also "
            "filters public.customers"
        )
        assert "safe in either direction" not in edge.description

    def test_a_nullable_fk_stays_optional(self) -> None:
        (edge,) = self._edges(nullable=True)
        assert edge.is_required is False
        assert edge.description.startswith("Optional FK")
        assert "LEFT JOIN" in edge.description

    def test_a_missing_column_in_the_schema_is_optional(self) -> None:
        schema = DataSourceSchema(
            source_type="postgresql",
            schemas=[SchemaInfo(name="public")],
            tables=[],
            relationships=[
                RelationshipInfo(
                    from_schema="public",
                    from_table="orders",
                    from_column="customer_id",
                    to_schema="public",
                    to_table="customers",
                    to_column="id",
                )
            ],
        )
        (edge,) = SemanticModelGenerator()._build_relationship_graph(schema)
        assert edge.is_required is False


class TestGlobalSectionsPrompt:
    """Joins are not generated: the prompt neither asks for them nor shows an example."""

    async def test_the_prompt_sent_to_the_llm_does_not_ask_for_common_joins(self) -> None:
        from app.semantic.models import GlobalSectionsResponse

        provider = MagicMock()
        provider.max_output_tokens = 8000
        provider.generate_structured = AsyncMock(return_value=GlobalSectionsResponse())
        await SemanticModelGenerator().generate_globals(
            schema=_schema_with_tables(["orders"], ["status"]),
            provider=provider,
            model=None,
            all_tables={"public.orders": _tbl("id", "status", name="Orders")},
            relationship_edges=[],
        )
        kwargs = provider.generate_structured.await_args.kwargs
        prompt = kwargs["user_message"]
        assert kwargs["schema_type"] is GlobalSectionsResponse
        assert "common_joins" not in prompt
        assert "join pattern" not in prompt.lower()
        assert "join_pattern" not in prompt
        assert "business_metrics" in prompt and "segments" in prompt
        assert "derived_columns" in prompt

    async def test_a_reply_that_still_carries_common_joins_is_accepted_and_they_are_ignored(
        self,
    ) -> None:
        """An older model may still answer with the field; validation must not fail on it."""
        from app.semantic.models import GlobalSectionsResponse

        reply = GlobalSectionsResponse.model_validate(
            {
                "business_metrics": [],
                "common_joins": [
                    {"description": "x", "tables": ["a"], "join_pattern": "a.id = b.id"}
                ],
            }
        )
        provider = MagicMock()
        provider.max_output_tokens = 8000
        provider.generate_structured = AsyncMock(return_value=reply)
        result = await SemanticModelGenerator().generate_globals(
            schema=_schema_with_tables(["orders"]),
            provider=provider,
            model=None,
            all_tables={"public.orders": _tbl("id", name="Orders")},
            relationship_edges=[],
        )
        assert len(result) == 3
