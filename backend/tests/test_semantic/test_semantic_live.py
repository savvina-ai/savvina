# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""The semantic layer and the prompt, checked against the real sample databases.

The unit tests prove the logic on hand-built schemas; these prove it on what the adapters
introspect and what the databases answer:

- the value-mapping guard on a real schema (PostgreSQL text column, MySQL ENUM);
- the metric-filter lint agrees with the database: a literal it reports matches no row, one it
  accepts does;
- the relationship edges follow the live nullability, the prompt renders them with the new
  header, and the join advice in that header holds on the real tables;
- ``_compress_prompt`` keeps, compacts and drops the semantic model on the real prompt, and
  the chat warning follows.

Opt-in: ``SAVVINA_LIVE_TEST_DBS=1`` with the ``test-dbs`` compose profile up
(``docker compose --profile test-dbs up -d`` from the repo root: ``sample-postgres`` on
5435, ``sample-mysql`` on 3307). Skipped otherwise. Nothing is written to the databases.
"""

# ruff: noqa: S608 — test queries against the sample databases.

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.datasources.adapters.postgresql import close_all_pools
from app.datasources.models import DataSourceSchema, PrivacySettings, table_key
from app.datasources.registry import create_datasource
from app.semantic.generator import SemanticModelGenerator
from app.semantic.models import (
    AggregationType,
    ColumnSemantic,
    SemanticModel,
    SimpleMetric,
    TablesBatchResponse,
    TableSemantic,
    ValueMapping,
)
from app.services.pipeline import (
    _SEMANTIC_COMPACTED_WARNING,
    _SEMANTIC_DROPPED_WARNING,
    _compact_semantic_model,
    _compress_prompt,
    _semantic_context_warning,
)
from app.services.prompt_builder import PromptBuilder

pytestmark = pytest.mark.skipif(
    os.environ.get("SAVVINA_LIVE_TEST_DBS") != "1",
    reason="set SAVVINA_LIVE_TEST_DBS=1 with the test-dbs compose profile running",
)

_PG = {
    "host": "localhost",
    "port": 5435,
    "database": "savvina_test",
    "username": "savvina",
    "password": os.environ.get("SAMPLE_POSTGRES_PASSWORD", "savvina_demo"),
    "ssl_mode": "disable",
}
_MYSQL = {
    "host": "localhost",
    "port": 3307,
    "database": "sample_delivery",
    "username": "savvina",
    "password": os.environ.get("SAMPLE_MYSQL_PASSWORD", "savvina_demo"),
}

# (source_type, config, orders table key) — each orders table has a ``status`` column and a
# ``customer_id`` foreign key to ``customers``.
_CASES = {
    "PostgreSQL": ("postgresql", _PG, "ecommerce.orders"),
    "MySQL": ("mysql", _MYSQL, "sample_delivery.orders"),
}
_SAMPLE_VALUES = PrivacySettings(include_sample_values=True)
_BOGUS = "__no_such_status__"


@pytest.fixture(autouse=True)
async def _drop_cached_pools():
    """Each test runs on its own event loop; close the PostgreSQL pool before it goes."""
    yield
    await close_all_pools()


class _Live:
    """A connected adapter and its introspected schema for one dialect."""

    def __init__(self, adapter, schema: DataSourceSchema, orders_key: str) -> None:
        self.adapter = adapter
        self.schema = schema
        self.orders_key = orders_key

    async def scalar(self, sql: str):
        result = await self.adapter.execute_query(sql, timeout=30, max_rows=10)
        return result.rows[0][0]

    async def column(self, sql: str) -> list:
        result = await self.adapter.execute_query(sql, timeout=30, max_rows=1000)
        return [row[0] for row in result.rows]


@pytest.fixture(params=list(_CASES))
async def live(request):
    source_type, config, orders_key = _CASES[request.param]
    adapter = create_datasource(source_type)
    connected = await adapter.connect(config)
    assert connected is None or connected.success, getattr(connected, "message", "")
    try:
        schema = await adapter.introspect(_SAMPLE_VALUES)
        assert orders_key in {table_key(t) for t in schema.tables}
        yield _Live(adapter, schema, orders_key)
    finally:
        await adapter.disconnect()


async def _real_statuses(live: _Live) -> list[str]:
    values = await live.column(f"SELECT DISTINCT status FROM {live.orders_key} ORDER BY status")
    assert len(values) >= 2, "the sample data must hold several statuses"
    return values


def _status_reply(values: list[str]) -> TablesBatchResponse:
    """What an LLM would answer for the orders table: a mapping per real status."""
    table = TableSemantic(
        display_name="Orders",
        columns={
            "status": ColumnSemantic(
                display_name="Status",
                value_mappings=[ValueMapping(raw_value=v, display_value=v.title()) for v in values],
            )
        },
    )
    return TablesBatchResponse.model_validate({"tables": {"_": table.model_dump(mode="json")}})


async def _generate_orders(
    live: _Live, values: list[str], privacy: PrivacySettings | None
) -> TableSemantic:
    reply = _status_reply(values)
    reply = TablesBatchResponse.model_validate(
        {"tables": {live.orders_key: reply.tables["_"].model_dump(mode="json")}}
    )
    provider = MagicMock()
    provider.max_output_tokens = 8000
    provider.generate_structured = AsyncMock(return_value=reply)
    out = await SemanticModelGenerator().generate_tables(
        schema=live.schema,
        provider=provider,
        model=None,
        table_keys=[live.orders_key],
        relationship_edges=SemanticModelGenerator()._build_relationship_graph(live.schema),
        privacy=privacy,
    )
    return out[live.orders_key]


# ── value-mapping guard ────────────────────────────────────────────────────────


async def test_value_mappings_are_kept_only_where_sample_values_are_allowed(live: _Live) -> None:
    values = await _real_statuses(live)

    kept = await _generate_orders(live, values, _SAMPLE_VALUES)
    assert [vm.raw_value for vm in kept.columns["status"].value_mappings] == values

    for privacy in (None, PrivacySettings(include_sample_values=False)):
        dropped = await _generate_orders(live, values, privacy)
        assert dropped.columns["status"].value_mappings == []
        assert dropped.columns["status"].display_name == "Status"  # the rest of the reply stays


async def test_a_sensitive_pattern_drops_the_mappings_though_sample_values_are_on(
    live: _Live,
) -> None:
    values = await _real_statuses(live)
    privacy = PrivacySettings(include_sample_values=True, sensitive_column_patterns=["status"])
    dropped = await _generate_orders(live, values, privacy)
    assert dropped.columns["status"].value_mappings == []


# ── metric-filter lint against the database ────────────────────────────────────


def _revenue(*filters: str, key: str) -> SimpleMetric:
    return SimpleMetric(
        name="Revenue",
        definition="orders.total",
        aggregation=AggregationType.SUM,
        filters=list(filters),
        related_tables=[key],
    )


async def test_the_filter_lint_reports_what_the_database_cannot_match(live: _Live) -> None:
    values = await _real_statuses(live)
    kept = await _generate_orders(live, values, _SAMPLE_VALUES)
    tables = {live.orders_key: kept}
    gen = SemanticModelGenerator()
    # ``completed`` is what an LLM guesses for a finished order; neither sample database has it.
    guesses = ["completed", _BOGUS, *values[:1]]

    for literal in guesses:
        matching = await live.scalar(
            f"SELECT COUNT(*) FROM {live.orders_key} WHERE status = '{literal}'"
        )
        warnings = gen._validate_metric_filter_values(
            [_revenue(f"status = '{literal}'", key=live.orders_key)], tables
        )
        assert bool(warnings) is (int(matching) == 0), (literal, matching, warnings)
        if warnings:
            assert f"'{literal}'" in warnings[0] and live.orders_key + ".status" in warnings[0]


async def test_the_filter_lint_reads_in_lists_and_negations_against_the_database(
    live: _Live,
) -> None:
    values = await _real_statuses(live)
    tables = {live.orders_key: await _generate_orders(live, values, _SAMPLE_VALUES)}
    gen = SemanticModelGenerator()
    listed = ", ".join(f"'{v}'" for v in values)

    silent = gen._validate_metric_filter_values(
        [
            _revenue(
                f"o.status IN ({listed})", f"status NOT IN ('{values[0]}')", key=live.orders_key
            )
        ],
        tables,
    )
    assert silent == []

    (warning,) = gen._validate_metric_filter_values(
        [
            _revenue(
                f"o.status NOT IN ('{values[0]}', '{_BOGUS}', 'completed')", key=live.orders_key
            )
        ],
        tables,
    )
    assert (
        f"'{_BOGUS}'" in warning
        and "'completed'" in warning
        and f"'{values[0]}'" not in (warning.split("list only")[0])
    )
    # The database agrees the reported values select nothing.
    for literal in (_BOGUS, "completed"):
        assert (
            int(
                await live.scalar(
                    f"SELECT COUNT(*) FROM {live.orders_key} WHERE status = '{literal}'"
                )
            )
            == 0
        )


# ── relationships and the prompt ───────────────────────────────────────────────


async def _is_nullable(live: _Live, key: str, column: str) -> bool:
    schema_name, _, table = key.partition(".")
    answer = await live.scalar(
        "SELECT is_nullable FROM information_schema.columns"
        f" WHERE table_schema = '{schema_name}' AND table_name = '{table}'"
        f" AND column_name = '{column}'"
    )
    return str(answer).upper() == "YES"


async def test_relationship_edges_follow_the_live_nullability(live: _Live) -> None:
    edges = SemanticModelGenerator()._build_relationship_graph(live.schema)
    assert edges, "the sample schema has foreign keys"
    for edge in edges:
        nullable = await _is_nullable(live, edge.from_table, edge.from_column)
        assert edge.is_required is (not nullable), edge.join_sql
        if edge.is_required:
            assert "unless the query also filters" in edge.description
            assert "safe in either direction" not in edge.description
        else:
            assert edge.description.startswith("Optional FK")


async def test_the_prompt_renders_the_new_header_and_each_edge_by_its_nullability(
    live: _Live,
) -> None:
    edges = SemanticModelGenerator()._build_relationship_graph(live.schema)
    model = SemanticModel(
        tables={
            key: TableSemantic(display_name=key)
            for key in {k for e in edges for k in (e.from_table, e.to_table)}
        },
        relationships=edges,
    )
    prompt = PromptBuilder().build_system_prompt(
        datasource=live.adapter,
        schema=live.schema,
        privacy=_SAMPLE_VALUES,
        semantic_model=model,
        few_shot_examples=None,
        user_question="orders per customer",
    )
    assert "RELATIONSHIPS (INNER JOIN marks a NOT NULL foreign key" in prompt
    assert "put that filter in the ON clause" in prompt
    assert "safe in either direction" not in prompt
    for edge in edges:
        kind = "INNER JOIN" if edge.is_required else "LEFT JOIN"
        bare_from, bare_to = edge.from_table.rpartition(".")[2], edge.to_table.rpartition(".")[2]
        assert f"FROM {bare_from} {kind} {bare_to} ON" in prompt, edge.join_sql


async def test_the_join_advice_in_the_header_holds_on_the_real_tables(live: _Live) -> None:
    """A filter on the parent in WHERE drops child rows even over a NOT NULL foreign key; the
    same filter in the ON clause of a LEFT JOIN keeps every child row."""
    edge = next(
        e
        for e in SemanticModelGenerator()._build_relationship_graph(live.schema)
        if e.from_table == live.orders_key and e.from_column == "customer_id"
    )
    child, parent = edge.from_table, edge.to_table
    fk, pk = edge.from_column, edge.to_column
    first_parent = f"(SELECT MIN({pk}) FROM {parent})"

    total = int(await live.scalar(f"SELECT COUNT(*) FROM {child}"))
    unfiltered_inner = int(
        await live.scalar(
            f"SELECT COUNT(*) FROM {child} c INNER JOIN {parent} p ON c.{fk} = p.{pk}"
        )
    )
    inner_where = int(
        await live.scalar(
            f"SELECT COUNT(*) FROM {child} c INNER JOIN {parent} p ON c.{fk} = p.{pk}"
            f" WHERE p.{pk} = {first_parent}"
        )
    )
    left_on = int(
        await live.scalar(
            f"SELECT COUNT(*) FROM {child} c LEFT JOIN {parent} p"
            f" ON c.{fk} = p.{pk} AND p.{pk} = {first_parent}"
        )
    )

    if edge.is_required:
        # The claim in the edge text: a required FK loses no child row on its own...
        assert unfiltered_inner == total
    assert inner_where < total  # ...but a filter on the parent does
    assert left_on == total  # and the filter in the ON clause of a LEFT JOIN does not


# ── compression: what the chat warns about ─────────────────────────────────────


async def test_compression_keeps_compacts_and_drops_the_semantic_model_and_warns(
    live: _Live,
) -> None:
    values = await _real_statuses(live)
    orders = await _generate_orders(live, values, _SAMPLE_VALUES)
    orders = orders.model_copy(update={"description": "Customer orders " * 20})
    edges = SemanticModelGenerator()._build_relationship_graph(live.schema)
    model = SemanticModel(
        tables={live.orders_key: orders},
        business_metrics=[_revenue("status = 'confirmed'", key=live.orders_key)],
        relationships=edges,
    )
    builder = PromptBuilder()
    question = "how many orders per status?"

    def length(semantic: SemanticModel | None) -> int:
        return len(
            builder.build_system_prompt(
                datasource=live.adapter,
                schema=live.schema,
                privacy=_SAMPLE_VALUES,
                semantic_model=semantic,
                few_shot_examples=None,
                user_question=question,
            )
        )

    full, compact, none = length(model), length(_compact_semantic_model(model)), length(None)
    assert none < compact < full, (none, compact, full)

    def compress(budget_chars: int) -> str:
        _, fit = _compress_prompt(
            builder=builder,
            datasource=live.adapter,
            schema=live.schema,
            privacy=_SAMPLE_VALUES,
            semantic_model=model,
            few_shot_examples=None,
            user_question=question,
            user_message=question,
            budget_chars=budget_chars,
        )
        return fit

    room = len(question)
    assert compress(full + room) == "full"
    assert compress(compact + room) == "compact"
    assert compress(none + room) == "dropped"
    assert compress(1) == "dropped"  # nothing fits: the smallest prompt, semantic model out

    assert _semantic_context_warning("full", model) is None
    assert _semantic_context_warning("compact", model) == _SEMANTIC_COMPACTED_WARNING
    assert _semantic_context_warning("dropped", model) == _SEMANTIC_DROPPED_WARNING
