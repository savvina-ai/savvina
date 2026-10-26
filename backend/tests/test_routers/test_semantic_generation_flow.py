# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Phased generation, and the drift check and apply after it, driven through the real
routes and the real semantic store.

Only the LLM (``generate_tables`` / ``generate_globals`` / ``generate_new_columns``),
provider resolution and schema loading (the drift routes' live introspection too) are
mocked; everything else — the routes, the row lock, the store's
reconcile, run stamping, provenance and the full save — runs against an in-memory
SQLite database. SQLite ignores ``FOR NO KEY UPDATE`` / ``FOR UPDATE``, so this
proves the flow, not the locking (that is covered against PostgreSQL by review, and
by the route unit tests asserting the lock is taken).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update

from app.database import get_db
from app.datasources.models import (
    ColumnInfo,
    DataSourceSchema,
    RelationshipInfo,
    SchemaInfo,
    TableInfo,
)
from app.main import app
from app.models.cache import QueryCacheEntry
from app.models.connection import Connection
from app.models.semantic_store import (
    SemanticColumnRow,
    SemanticGlobalItemRow,
    SemanticHistoryRow,
    SemanticKnownColumnsRow,
    SemanticMetricRow,
    SemanticModelRow,
    SemanticRelationshipRow,
    SemanticTableRow,
)
from app.models.semantic_suggestion import SemanticSuggestion
from app.models.table_embedding_cache import TableEmbeddingCache
from app.models.user import User
from app.models.user_schema_cache import UserSchemaCache
from app.semantic import store
from app.semantic.generator import SemanticModelGenerator
from app.semantic.models import (
    AggregationType,
    ColumnSemantic,
    GenerationStatus,
    SimpleMetric,
    TableSemantic,
)
from tests.test_semantic.conftest import semantic_db, semantic_tables  # noqa: F401

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_CID = "22222222-2222-2222-2222-222222222222"
_BASE = f"/api/v1/connections/{_CID}/semantic"
_TABLES = (
    User,  # parent of the cache tables' user_id FKs, which SQLite checks on delete
    Connection,
    SemanticModelRow,
    SemanticTableRow,
    SemanticColumnRow,
    SemanticMetricRow,
    SemanticRelationshipRow,
    SemanticGlobalItemRow,
    SemanticHistoryRow,
    SemanticKnownColumnsRow,
    SemanticSuggestion,
    # Emptied by _invalidate_connection_caches on the writes that change the model.
    QueryCacheEntry,
    UserSchemaCache,
    TableEmbeddingCache,
)


def _schema(
    tables: dict[str, list[str]],
    fks: list[RelationshipInfo] | None = None,
    unreadable: list[str] | None = None,
) -> DataSourceSchema:
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
                    ColumnInfo(name=c, data_type="integer", native_type="int4") for c in columns
                ],
            )
            for name, columns in tables.items()
        ],
        relationships=fks or [],
        unreadable_tables=unreadable or [],
    )


@pytest.fixture
def semantic_tables() -> tuple[type, ...]:  # noqa: F811 - overrides the shared default
    return _TABLES


@pytest_asyncio.fixture
async def db(semantic_db: AsyncSession) -> AsyncIterator[AsyncSession]:  # noqa: F811
    semantic_db.add(
        Connection(id=_CID, name="flow", source_type="postgresql", config_encrypted=b"")
    )
    await semantic_db.commit()

    async def _get_db() -> AsyncIterator[AsyncSession]:
        yield semantic_db

    app.dependency_overrides[get_db] = _get_db
    yield semantic_db


class _Llm:
    """The mocked LLM: annotates whatever it is asked for, and records the asks."""

    def __init__(self) -> None:
        self.description = "generated"
        self.batches: list[list[str]] = []
        self.during_batch = None  # optional async hook, run inside the "LLM call"
        self.during_globals = None  # likewise, inside the globals "LLM call"
        self.globals_tables: dict[str, TableSemantic] = {}
        self.omit_columns: set[str] = set()  # "schema.table.column" names left unannotated
        self.column_batches: list[dict[str, list[str]]] = []  # new-column asks, in order
        # 1-based index of a new-column call → the error message it raises.
        self.column_failures: dict[int, str] = {}

    async def generate_tables(self, *, schema, table_keys, **_kw) -> dict[str, TableSemantic]:
        self.batches.append(list(table_keys))
        if self.during_batch is not None:
            await self.during_batch()
        wanted = set(table_keys)
        return {
            f"{t.schema_name}.{t.name}": TableSemantic(
                display_name=t.name.title(),
                description=f"{self.description} {t.name}",
                columns={
                    c.name: ColumnSemantic(display_name=c.name.upper())
                    for c in t.columns
                    if f"{t.schema_name}.{t.name}.{c.name}" not in self.omit_columns
                },
            )
            for t in schema.tables
            if f"{t.schema_name}.{t.name}" in wanted
        }

    async def generate_new_columns(
        self, *, columns_by_table, **_kw
    ) -> dict[str, dict[str, ColumnSemantic]]:
        self.column_batches.append({key: list(names) for key, names in columns_by_table.items()})
        if (failure := self.column_failures.get(len(self.column_batches))) is not None:
            # What the generator raises for a provider error, the provider's mark kept.
            raise ValueError(f"LLM call failed in generate_new_columns: {failure}")
        return {
            key: {name: ColumnSemantic(display_name=name.upper()) for name in names}
            for key, names in columns_by_table.items()
        }

    async def generate_globals(self, **kw):
        self.globals_tables = kw["all_tables"]
        if self.during_globals is not None:
            await self.during_globals()
        metric = SimpleMetric(
            name="Row count", definition="COUNT(*)", aggregation=AggregationType.COUNT
        )
        return [metric], [], [], [], []


@pytest.fixture
def flow(db):
    """Patch the LLM, provider and schema loading; yields ``(llm, set_schema)``."""
    from app.auth.limiter import limiter

    limiter._storage.reset()
    llm = _Llm()
    current = {"schema": _schema({"orders": ["id", "total"], "customers": ["id"]})}

    async def load_schema(connection_id, _user, session):
        return current["schema"], await session.get(Connection, connection_id)

    def set_schema(
        tables: dict[str, list[str]],
        fks: list[RelationshipInfo] | None = None,
        unreadable: list[str] | None = None,
    ) -> None:
        current["schema"] = _schema(tables, fks, unreadable)

    with (
        patch("app.routers.semantic._load_schema_for_connection", new=load_schema),
        patch(
            "app.routers.semantic._resolve_provider",
            new=AsyncMock(return_value=(MagicMock(), "model-x", "claude")),
        ),
        patch.object(SemanticModelGenerator, "generate_tables", new=llm.generate_tables),
        patch.object(SemanticModelGenerator, "generate_globals", new=llm.generate_globals),
        patch.object(SemanticModelGenerator, "generate_new_columns", new=llm.generate_new_columns),
    ):
        yield llm, set_schema


async def _init(client, *, resume: bool = False) -> dict:
    resp = await client.post(f"{_BASE}/generate/init" + ("?resume=true" if resume else ""))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _batches(client, keys: list[str], size: int) -> None:
    for i in range(0, len(keys), size):
        resp = await client.post(f"{_BASE}/generate/batch", json={"table_keys": keys[i : i + size]})
        assert resp.status_code == 200, resp.text


async def _globals(client) -> dict:
    resp = await client.post(f"{_BASE}/generate/globals")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _generate(client) -> dict:
    plan = await _init(client)
    await _batches(client, plan["pending_table_keys"], plan["batch_size"])
    return await _globals(client)


async def _check_drift(client, live: DataSourceSchema) -> dict:
    """``GET .../drift`` against *live* as the introspection; the drift report."""
    from app.auth.limiter import limiter

    limiter._storage.reset()  # 5/minute, and a flow checks more often
    with patch("app.routers.semantic.refresh_user_schema_cache", new=AsyncMock(return_value=live)):
        resp = await client.get(f"{_BASE}/drift")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _apply_drift(client, live: DataSourceSchema, *, dry_run: bool = False) -> dict:
    """``POST .../apply-drift`` against *live* as the introspection; the response."""
    from app.auth.limiter import limiter

    limiter._storage.reset()
    with patch("app.routers.semantic.refresh_user_schema_cache", new=AsyncMock(return_value=live)):
        resp = await client.post(f"{_BASE}/apply-drift" + ("?dry_run=true" if dry_run else ""))
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestGenerationFlow:
    async def test_fresh_generate_reaches_complete(self, http_client, db, flow) -> None:
        body = await _generate(http_client)

        assert body["generation_status"] == "complete"
        assert set(body["tables"]) == {"public.orders", "public.customers"}
        assert [m["name"] for m in body["business_metrics"]] == ["Row count"]
        stored = await store.load_model(db, _CID)
        assert stored.generation_status == GenerationStatus.COMPLETE
        assert stored.generation_progress is None
        assert stored.tables["public.orders"].description == "generated orders"
        assert set(stored.tables["public.orders"].columns) == {"id", "total"}

    async def test_generation_records_the_live_columns_as_the_baseline(
        self, http_client, db, flow
    ) -> None:
        llm, _ = flow
        llm.omit_columns = {"public.orders.total"}
        await _generate(http_client)

        # The LLM left ``total`` out, but the model has seen it: it is not a new column.
        stored = await store.load_model(db, _CID)
        assert set(stored.tables["public.orders"].columns) == {"id"}
        assert await store.load_known_columns(db, _CID) == {
            "public.orders": frozenset({"id", "total"}),
            "public.customers": frozenset({"id"}),
        }

    async def test_a_regenerate_trims_the_baseline_but_keeps_that_of_an_unreadable_table(
        self, http_client, db, flow
    ) -> None:
        _, set_schema = flow
        set_schema({"orders": ["id", "total"], "customers": ["id", "email"], "legacy": ["id"]})
        await _generate(http_client)

        # ``total`` and ``legacy`` left the database; ``customers`` is only unreadable.
        set_schema({"orders": ["id"]}, unreadable=["public.customers"])
        await _generate(http_client)

        assert await store.load_known_columns(db, _CID) == {
            "public.orders": frozenset({"id"}),
            "public.customers": frozenset({"id", "email"}),
        }

    async def test_check_drift_baselines_only_the_table_without_an_entry_then_reports_a_new_column(
        self, http_client, db, flow
    ) -> None:
        await _generate(http_client)
        # A legacy table has no entry; ``orders`` keeps one with a stale name that only an
        # untouched row can still hold.
        await db.execute(
            delete(SemanticKnownColumnsRow).where(
                SemanticKnownColumnsRow.table_key == "public.customers"
            )
        )
        await db.execute(
            update(SemanticKnownColumnsRow)
            .where(SemanticKnownColumnsRow.table_key == "public.orders")
            .values(columns=["id", "total", "ghost"])
        )
        await db.commit()

        live = _schema({"orders": ["id", "total"], "customers": ["id"]})
        with patch(
            "app.routers.semantic.refresh_user_schema_cache", new=AsyncMock(return_value=live)
        ):
            resp = await http_client.get(f"{_BASE}/drift")
        assert resp.status_code == 200, resp.text
        assert resp.json()["has_actionable_changes"] is False
        assert await store.load_known_columns(db, _CID) == {
            "public.orders": frozenset({"id", "total", "ghost"}),
            "public.customers": frozenset({"id"}),
        }

        live = _schema({"orders": ["id", "total", "region"], "customers": ["id"]})
        with patch(
            "app.routers.semantic.refresh_user_schema_cache", new=AsyncMock(return_value=live)
        ):
            resp = await http_client.get(f"{_BASE}/drift")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert any("public.orders.region" in w for w in body["warnings"])
        assert body["has_actionable_changes"] is True

    async def test_finishing_a_run_renames_a_column_held_only_under_a_near_miss_key(
        self, http_client, db, flow
    ) -> None:
        llm, set_schema = flow
        set_schema({"orders": ["id"], "customers": ["id", "status"]})
        llm.omit_columns = {"public.customers.status"}
        await _generate(http_client)
        # A near-miss key as a blob-era backfill (migration 0003) left it: one plain
        # column row, no provenance, never grounded to the live name.
        table_id = await db.scalar(
            select(SemanticTableRow.id).where(SemanticTableRow.table_key == "public.customers")
        )
        db.add(SemanticColumnRow(table_id=table_id, column_name="Status", display_name="Legacy"))
        await db.commit()
        assert "Status" in (await store.load_model(db, _CID)).tables["public.customers"].columns

        # "Finish with what's generated": no batch reaches ``customers``.
        await _init(http_client)
        await _globals(http_client)

        stored = await store.load_model(db, _CID)
        # Renamed to the live name, its annotation carried, before the prune could drop it.
        assert set(stored.tables["public.customers"].columns) == {"id", "status"}
        assert stored.tables["public.customers"].columns["status"].display_name == "Legacy"
        assert (await store.load_known_columns(db, _CID))["public.customers"] == {"id", "status"}

    async def test_regenerate_keeps_a_hand_edit_and_prunes_a_dropped_column(
        self, http_client, db, flow
    ) -> None:
        llm, set_schema = flow
        await _generate(http_client)
        resp = await http_client.put(
            _BASE, json={"tables": {"public.orders": {"description": "hand written"}}}
        )
        assert resp.status_code == 200, resp.text

        set_schema({"orders": ["id"], "customers": ["id"]})  # orders.total was dropped
        llm.description = "regenerated"
        body = await _generate(http_client)

        orders = body["tables"]["public.orders"]
        assert orders["description"] == "hand written"
        assert set(orders["columns"]) == {"id"}
        assert body["tables"]["public.customers"]["description"] == "regenerated customers"
        stored = await store.load_model(db, _CID)
        assert set(stored.tables["public.orders"].columns) == {"id"}
        assert stored.tables["public.orders"].description == "hand written"

    async def test_an_interrupted_run_resumes_only_the_pending_keys(
        self, http_client, db, flow
    ) -> None:
        llm, set_schema = flow
        names = ["a", "b", "c", "d", "e"]
        set_schema({n: ["id"] for n in names})
        plan = await _init(http_client)
        assert plan["batch_count"] == 2
        first = plan["pending_table_keys"][: plan["batch_size"]]
        await _batches(http_client, first, plan["batch_size"])  # then the tab closes

        resumed = await _init(http_client, resume=True)
        assert resumed["resumed"] is True
        assert resumed["pending_table_keys"] == ["public.e"]
        await _batches(http_client, resumed["pending_table_keys"], resumed["batch_size"])
        body = await _globals(http_client)

        assert llm.batches == [first, ["public.e"]]
        assert body["generation_status"] == "complete"
        assert set(body["tables"]) == {f"public.{n}" for n in names}

    async def test_finish_with_what_is_generated(self, http_client, db, flow) -> None:
        _llm, set_schema = flow
        set_schema({n: ["id"] for n in ["a", "b", "c", "d", "e"]})
        plan = await _init(http_client)
        first = plan["pending_table_keys"][: plan["batch_size"]]
        await _batches(http_client, first, plan["batch_size"])

        body = await _globals(http_client)

        assert body["generation_status"] == "complete"
        assert set(body["tables"]) == set(first)

    async def test_a_batch_purges_the_cached_answers_and_embeddings_but_keeps_the_schema_cache(
        self, http_client, db, flow
    ) -> None:
        plan = await _init(http_client)
        # An answer generated against the model as it was before this run, a table
        # embedding, and the user's schema cache (what a real batch reads).
        db.add(User(id="admin-1", email="admin@test.com", password_hash="x"))
        # Flushed on its own: no ORM relationship links the cache rows to it, so the
        # unit of work does not order its insert before theirs, and SQLite checks the FK.
        await db.flush()
        db.add(
            QueryCacheEntry(
                connection_id=_CID,
                question_normalized="how many orders",
                question_raw="How many orders?",
                generated_query="SELECT COUNT(*) FROM public.orders",
                query_dialect="postgresql",
            )
        )
        db.add(
            TableEmbeddingCache(connection_id=_CID, user_id="admin-1", table_key="public.orders")
        )
        db.add(UserSchemaCache(connection_id=_CID, user_id="admin-1", schema_cache={}))
        await db.commit()

        resp = await http_client.post(
            f"{_BASE}/generate/batch", json={"table_keys": plan["pending_table_keys"][:1]}
        )

        assert resp.status_code == 200, resp.text
        db.expunge_all()
        assert (await db.scalars(select(QueryCacheEntry))).all() == []
        assert (await db.scalars(select(TableEmbeddingCache))).all() == []
        [kept] = (await db.scalars(select(UserSchemaCache))).all()
        assert kept.user_id == "admin-1"

    async def test_a_table_the_llm_omits_stays_pending_and_is_reported(
        self, http_client, db, flow
    ) -> None:
        llm, set_schema = flow
        set_schema({"a": ["id"], "b": ["id"]})
        original = llm.generate_tables

        # Patched onto the class as a plain function, so it is bound: ``_self`` is the
        # generator instance (the fixture's ``llm.generate_tables`` is a bound method
        # and needs no such parameter).
        async def omit_b(_self, *, schema, table_keys, **kw):
            out = await original(schema=schema, table_keys=table_keys, **kw)
            out.pop("public.b", None)
            return out

        with patch.object(SemanticModelGenerator, "generate_tables", new=omit_b):
            plan = await _init(http_client)
            resp = await http_client.post(
                f"{_BASE}/generate/batch", json={"table_keys": plan["pending_table_keys"]}
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["omitted_table_keys"] == ["public.b"]
            assert await store.pending_table_keys(db, _CID, ["public.a", "public.b"]) == [
                "public.b"
            ]
            body = await _globals(http_client)
        assert body["generation_status"] == "complete"
        assert set(body["tables"]) == {"public.a"}
        assert any("public.b" in w and "no annotation" in w for w in body["generation_warnings"])

    async def test_the_globals_response_is_what_a_later_get_returns(
        self, http_client, db, flow
    ) -> None:
        """/generate/globals answers with the model as stored, not the one it built:
        a GET right after returns the same JSON, ordering included. The names are
        deliberately not in sorted order, so a reorder by the store would show."""
        llm, set_schema = flow
        set_schema({"zeta": ["zz", "id", "amount"], "alpha": ["yy", "bb"], "mid": ["k"]})
        await _generate(http_client)
        resp = await http_client.put(
            _BASE,
            json={
                "tables": {
                    "public.zeta": {
                        "description": "hand written",
                        "columns": {"amount": {"description": "hand column"}},
                    }
                }
            },
        )
        assert resp.status_code == 200, resp.text

        llm.description = "regenerated"
        original = llm.generate_globals

        # A NUL the store strips on write: answering with the built model instead of
        # the stored one would show it.
        async def nul_in_a_metric(_self, **kw):
            metrics, *rest = await original(**kw)
            metrics[0].description = "rows\x00 counted"
            return (metrics, *rest)

        with patch.object(SemanticModelGenerator, "generate_globals", new=nul_in_a_metric):
            body = await _generate(http_client)
        got = await http_client.get(_BASE)
        assert got.status_code == 200, got.text
        assert body == got.json()
        assert body["business_metrics"][0]["description"] == "rows counted"
        assert body["tables"]["public.zeta"]["description"] == "hand written"
        assert body["tables"]["public.zeta"]["columns"]["amount"]["description"] == "hand column"
        assert body["tables"]["public.alpha"]["description"] == "regenerated alpha"

    async def test_an_unmarked_fk_edge_to_a_hidden_table_returns_once_unhidden(
        self, http_client, db, flow
    ) -> None:
        """Documented rule (semantic-model-generation.md, Finalisation): while a table
        is hidden, the generated FK edge to it is not regenerated and is not carried;
        the hidden table's own row and hand edits are. Un-hiding brings the edge back
        on the next globals run, from the FK it is built from. Nothing hand-written
        is involved, so nothing is lost."""
        _llm, set_schema = flow
        fk = RelationshipInfo(
            from_schema="public",
            from_table="orders",
            from_column="customer_id",
            to_schema="public",
            to_table="customers",
            to_column="id",
        )
        both = {"orders": ["id", "customer_id"], "customers": ["id"]}

        def edges_to_customers() -> list[str]:
            return [r.from_column for r in stored.relationships if r.to_table == "public.customers"]

        set_schema(both, fks=[fk])
        await _generate(http_client)
        resp = await http_client.put(
            _BASE, json={"tables": {"public.customers": {"description": "hand table"}}}
        )
        assert resp.status_code == 200, resp.text
        stored = await store.load_model(db, _CID)
        assert edges_to_customers() == ["customer_id"]  # generated from the FK, unmarked

        conn = await db.get(Connection, _CID)
        conn.privacy_settings = {"excluded_tables": ["public.customers"]}
        await db.commit()
        # What the real loader returns under that policy: customers and its FK gone.
        set_schema({"orders": ["id", "customer_id"]})
        await _generate(http_client)
        stored = await store.load_model(db, _CID)
        assert stored.tables["public.customers"].description == "hand table"
        assert edges_to_customers() == []

        conn = await db.get(Connection, _CID)
        conn.privacy_settings = {}
        await db.commit()
        set_schema(both, fks=[fk])
        await _generate(http_client)
        stored = await store.load_model(db, _CID)
        assert stored.tables["public.customers"].description == "hand table"
        assert edges_to_customers() == ["customer_id"]

    async def test_a_batch_from_a_restarted_run_is_409_and_stamps_nothing(
        self, http_client, db, flow
    ) -> None:
        llm, _set_schema = flow
        plan = await _init(http_client)
        old_run = (await store.load_header(db, _CID)).generation_run_id

        async def restart() -> None:
            # A fresh /generate/init commits while this batch awaits the LLM.
            await store.begin_generation(
                db,
                _CID,
                schema_hash=(await store.load_header(db, _CID)).schema_hash,
                tables_total=plan["tables_total"],
                tables_done=0,
                batch_size=plan["batch_size"],
                source_dialect="postgresql",
            )
            await db.commit()

        llm.during_batch = restart
        resp = await http_client.post(
            f"{_BASE}/generate/batch", json={"table_keys": ["public.orders"]}
        )

        assert resp.status_code == 409
        assert "restarted" in resp.json()["detail"]
        new_run = (await store.load_header(db, _CID)).generation_run_id
        assert new_run != old_run
        assert (await db.scalars(select(SemanticTableRow))).all() == []
        assert await store.pending_table_keys(db, _CID, ["public.orders", "public.customers"]) == [
            "public.orders",
            "public.customers",
        ]

    async def test_globals_from_a_restarted_run_is_409_and_the_new_run_finishes(
        self, http_client, db, flow
    ) -> None:
        llm, _set_schema = flow
        plan = await _init(http_client)
        keys = plan["pending_table_keys"]
        await _batches(http_client, keys, plan["batch_size"])
        old_run = (await store.load_header(db, _CID)).generation_run_id

        async def restart() -> None:
            # A fresh /generate/init commits while globals awaits the LLM.
            llm.during_globals = None
            await store.begin_generation(
                db,
                _CID,
                schema_hash=(await store.load_header(db, _CID)).schema_hash,
                tables_total=plan["tables_total"],
                tables_done=0,
                batch_size=plan["batch_size"],
                source_dialect="postgresql",
            )
            await db.commit()

        llm.during_globals = restart
        resp = await http_client.post(f"{_BASE}/generate/globals")

        assert resp.status_code == 409
        assert "restarted" in resp.json()["detail"]
        header = await store.load_header(db, _CID)
        assert header.generation_status == GenerationStatus.TABLES_PARTIAL
        assert header.generation_run_id != old_run
        # The new run is untouched: every key is still pending for it, and its
        # batches and globals go through.
        assert await store.pending_table_keys(db, _CID, keys) == keys
        await _batches(http_client, keys, plan["batch_size"])
        body = await _globals(http_client)
        assert body["generation_status"] == "complete"
        assert set(body["tables"]) == set(keys)

    async def test_a_suggestion_applied_twice_is_409_and_writes_nothing(
        self, http_client, db, flow
    ) -> None:
        await _generate(http_client)
        db.add(
            SemanticSuggestion(
                id="sug-1",
                connection_id=_CID,
                table_key="public.orders",
                field="total",
                correction_type="update_description",
                value={"target": "column", "description": "Order total"},
            )
        )
        await db.commit()
        url = f"{_BASE}/suggestions/sug-1/apply"

        first = await http_client.post(url)
        assert first.status_code == 200, first.text
        assert first.json()["tables"]["public.orders"]["columns"]["total"]["description"] == (
            "Order total"
        )
        # The user edits the description after applying the suggestion.
        edited = await http_client.put(
            _BASE,
            json={"tables": {"public.orders": {"columns": {"total": {"description": "mine"}}}}},
        )
        assert edited.status_code == 200, edited.text
        history_before = len((await db.scalars(select(SemanticHistoryRow))).all())
        version_before = (await db.scalar(select(SemanticModelRow))).version

        second = await http_client.post(url)

        assert second.status_code == 409
        assert second.json()["detail"] == "Suggestion already applied"
        db.expunge_all()
        assert len((await db.scalars(select(SemanticHistoryRow))).all()) == history_before
        assert (await db.scalar(select(SemanticModelRow))).version == version_before
        model = await store.load_model(db, _CID)
        assert model.tables["public.orders"].columns["total"].description == "mine"

    async def test_what_the_privacy_policy_hides_survives_a_regenerate(
        self, http_client, db, flow
    ) -> None:
        """Hiding a table and a column is not dropping them: their hand edits, marks
        and a user edge to the hidden table survive a full regenerate."""
        llm, set_schema = flow
        set_schema({"orders": ["id", "customer_id", "ssn"], "customers": ["id"]})
        await _generate(http_client)
        edge = {
            "from_table": "public.orders",
            "from_column": "customer_id",
            "to_table": "public.customers",
            "to_column": "id",
            "join_sql": "public.orders.customer_id = public.customers.id",
        }
        resp = await http_client.put(
            _BASE,
            json={
                "tables": {
                    "public.customers": {"description": "hand table"},
                    "public.orders": {"columns": {"ssn": {"description": "hand column"}}},
                },
                "relationships": [edge],
            },
        )
        assert resp.status_code == 200, resp.text

        conn = await db.get(Connection, _CID)
        conn.privacy_settings = {
            "excluded_tables": ["public.customers"],
            "excluded_columns": ["public.orders.ssn"],
        }
        await db.commit()
        # What the real loader returns under that policy: the table filtered out and
        # the column never introspected.
        set_schema({"orders": ["id", "customer_id"]})
        llm.description = "regenerated"
        await _generate(http_client)

        stored = await store.load_model(db, _CID)
        assert stored.generation_status == GenerationStatus.COMPLETE
        assert stored.tables["public.customers"].description == "hand table"
        assert stored.tables["public.orders"].columns["ssn"].description == "hand column"
        assert stored.tables["public.orders"].description == "regenerated orders"
        assert [store.relationship_key(r) for r in stored.relationships] == [
            store.relationship_key(edge)
        ]
        marks = (await store.provenance_snapshot(db, _CID)).marks
        assert "description" in marks["table"]["public.customers"]
        assert "description" in marks["column"]["public.orders.ssn"]
        assert store.relationship_key(edge) in marks["relationship"]
        # Kept in the store, never shown to the LLM.
        assert set(llm.globals_tables) == {"public.orders"}
        assert set(llm.globals_tables["public.orders"].columns) == {"id", "customer_id"}

    async def test_a_table_the_role_cannot_read_survives_a_regenerate(
        self, http_client, db, flow
    ) -> None:
        """A table that exists but that the connection's role cannot read is not dropped:
        its row, its hand edit and a user edge to it survive a full regenerate, and it
        never reaches the LLM. A table that really left the database is still pruned."""
        llm, set_schema = flow
        set_schema({"orders": ["id", "customer_id"], "customers": ["id"], "legacy": ["id"]})
        await _generate(http_client)
        edge = {
            "from_table": "public.orders",
            "from_column": "customer_id",
            "to_table": "public.customers",
            "to_column": "id",
            "join_sql": "public.orders.customer_id = public.customers.id",
        }
        resp = await http_client.put(
            _BASE,
            json={
                "tables": {"public.customers": {"description": "hand table"}},
                "relationships": [edge],
            },
        )
        assert resp.status_code == 200, resp.text

        # What a narrower role's introspection returns: customers is unreadable, not
        # gone; legacy was dropped.
        set_schema({"orders": ["id", "customer_id"]}, unreadable=["public.customers"])
        await _generate(http_client)

        stored = await store.load_model(db, _CID)
        assert stored.generation_status == GenerationStatus.COMPLETE
        assert stored.tables["public.customers"].description == "hand table"
        assert "public.legacy" not in stored.tables
        assert [store.relationship_key(r) for r in stored.relationships] == [
            store.relationship_key(edge)
        ]
        marks = (await store.provenance_snapshot(db, _CID)).marks
        assert "description" in marks["table"]["public.customers"]
        assert set(llm.globals_tables) == {"public.orders"}


class TestApplyDriftFlow:
    """Check Drift and Apply Drift over a generated model, the baseline included: what
    the check reports, the apply acts on, and the check after it is clean."""

    async def test_a_legacy_model_is_baselined_then_a_new_column_is_reported_and_applied(
        self, http_client, db, flow
    ) -> None:
        llm, _ = flow
        await _generate(http_client)
        # A model from before the baseline existed: no row at all.
        await db.execute(delete(SemanticKnownColumnsRow))
        await db.commit()

        report = await _check_drift(
            http_client, _schema({"orders": ["id", "total"], "customers": ["id"]})
        )
        assert report["has_actionable_changes"] is False
        assert set(await store.load_known_columns(db, _CID)) == {
            "public.orders",
            "public.customers",
        }

        live = _schema({"orders": ["id", "total", "region"], "customers": ["id"]})
        report = await _check_drift(http_client, live)
        assert report["has_actionable_changes"] is True
        assert any("public.orders.region" in w for w in report["warnings"])

        preview = await _apply_drift(http_client, live, dry_run=True)
        assert preview["summary"]["columns_added"] == {"public.orders": ["region"]}
        assert llm.column_batches == []

        applied = await _apply_drift(http_client, live)
        assert applied["summary"]["columns_added"] == {"public.orders": ["region"]}
        assert llm.column_batches == [{"public.orders": ["region"]}]
        stored = await store.load_model(db, _CID)
        assert stored.tables["public.orders"].columns["region"].display_name == "REGION"
        assert (await store.load_known_columns(db, _CID))["public.orders"] == {
            "id",
            "total",
            "region",
        }

        report = await _check_drift(http_client, live)
        assert report["has_actionable_changes"] is False
        assert report["warnings"] == []

    async def test_a_near_miss_key_is_reported_as_a_rename_and_renamed_with_no_llm_call(
        self, http_client, db, flow
    ) -> None:
        """The baseline knows ``status``, held only under a quoted blob-era key with a hand
        edit. Check Drift words it as a rename; Apply Drift renames it, annotation and
        mark carried, without asking the LLM; the next check is clean."""
        llm, set_schema = flow
        set_schema({"orders": ["id"], "customers": ["id", "status"]})
        llm.omit_columns = {"public.customers.status"}
        await _generate(http_client)
        table_id = await db.scalar(
            select(SemanticTableRow.id).where(SemanticTableRow.table_key == "public.customers")
        )
        db.add(
            SemanticColumnRow(
                table_id=table_id,
                column_name='"status"',
                display_name="Legacy",
                user_edited_fields=["display_name"],
            )
        )
        await db.commit()
        live = _schema({"orders": ["id"], "customers": ["id", "status"]})
        report = await _check_drift(http_client, live)
        assert report["has_actionable_changes"] is True
        assert report["warnings"] == [
            'Column key "public.customers."status"" in the semantic model is a near-miss '
            'spelling of "status"; Apply Drift renames it to "status", keeping its annotation'
        ]

        applied = await _apply_drift(http_client, live)
        assert applied["summary"]["columns_renamed"] == {"public.customers": {'"status"': "status"}}
        assert applied["summary"]["columns_removed"] == {}
        assert applied["summary"]["columns_added"] == {}
        assert llm.column_batches == []
        stored = await store.load_model(db, _CID)
        assert set(stored.tables["public.customers"].columns) == {"id", "status"}
        assert stored.tables["public.customers"].columns["status"].display_name == "Legacy"
        snapshot = await store.provenance_snapshot(db, _CID)
        assert snapshot.marks["column"] == {"public.customers.status": ["display_name"]}
        assert (await store.load_known_columns(db, _CID))["public.customers"] == {"id", "status"}

        report = await _check_drift(http_client, live)
        assert report["has_actionable_changes"] is False
        assert report["warnings"] == []

    async def test_a_hidden_near_miss_key_is_renamed_once_its_column_is_unhidden(
        self, http_client, db, flow
    ) -> None:
        """The model holds ``ssn`` only under a quoted blob-era key with a hand edit, and
        the policy hides ``ssn``. A Regenerate keeps the hidden row, and the baseline keeps
        ``ssn``. Once un-hidden, Check Drift reports a rename, not a new column, and Apply
        Drift renames the key with its mark and no LLM call."""
        llm, set_schema = flow
        set_schema({"orders": ["id"], "customers": ["id", "ssn"]})
        llm.omit_columns = {"public.customers.ssn"}
        await _generate(http_client)
        table_id = await db.scalar(
            select(SemanticTableRow.id).where(SemanticTableRow.table_key == "public.customers")
        )
        db.add(
            SemanticColumnRow(
                table_id=table_id,
                column_name='"ssn"',
                display_name="Legacy",
                user_edited_fields=["display_name"],
            )
        )
        conn = await db.get(Connection, _CID)
        conn.privacy_settings = {"excluded_columns": ["public.customers.ssn"]}
        await db.commit()
        set_schema({"orders": ["id"], "customers": ["id"]})  # ``ssn`` is not introspected
        await _generate(http_client)
        assert '"ssn"' in (await store.load_model(db, _CID)).tables["public.customers"].columns
        assert (await store.load_known_columns(db, _CID))["public.customers"] == {"id", "ssn"}

        conn.privacy_settings = {}
        await db.commit()
        live = _schema({"orders": ["id"], "customers": ["id", "ssn"]})
        report = await _check_drift(http_client, live)
        assert report["warnings"] == [
            'Column key "public.customers."ssn"" in the semantic model is a near-miss '
            'spelling of "ssn"; Apply Drift renames it to "ssn", keeping its annotation'
        ]

        llm.column_batches = []
        applied = await _apply_drift(http_client, live)
        assert applied["summary"]["columns_renamed"] == {"public.customers": {'"ssn"': "ssn"}}
        assert applied["summary"]["columns_added"] == {}
        assert llm.column_batches == []
        stored = await store.load_model(db, _CID)
        assert stored.tables["public.customers"].columns["ssn"].display_name == "Legacy"
        marks = (await store.provenance_snapshot(db, _CID)).marks
        assert marks["column"] == {"public.customers.ssn": ["display_name"]}
        assert (await _check_drift(http_client, live))["warnings"] == []

    async def test_a_hidden_column_the_model_never_annotated_is_new_once_unhidden(
        self, http_client, db, flow
    ) -> None:
        """Nothing stored keeps it seen: the Regenerate under the policy drops it from the
        baseline, and Check Drift reports it as new once the policy shows it again."""
        llm, set_schema = flow
        set_schema({"orders": ["id"], "customers": ["id", "ssn"]})
        llm.omit_columns = {"public.customers.ssn"}
        await _generate(http_client)
        conn = await db.get(Connection, _CID)
        conn.privacy_settings = {"excluded_columns": ["public.customers.ssn"]}
        await db.commit()
        set_schema({"orders": ["id"], "customers": ["id"]})
        await _generate(http_client)
        assert (await store.load_known_columns(db, _CID))["public.customers"] == {"id"}

        conn.privacy_settings = {}
        await db.commit()
        report = await _check_drift(
            http_client, _schema({"orders": ["id"], "customers": ["id", "ssn"]})
        )
        assert report["warnings"] == [
            "1 new column(s) added to tables in the semantic model since it was generated: "
            "public.customers.ssn"
        ]

    async def test_a_throttle_on_the_second_batch_keeps_the_first_and_the_next_apply_sends_the_rest(
        self, http_client, db, flow
    ) -> None:
        """Nothing but new columns, over more tables than one batch holds: the throttled
        second batch is a warning, not a 429, because the first one answered."""
        llm, set_schema = flow
        names = ["a", "b", "c", "d", "e"]  # one more than GENERATION_BATCH_SIZE
        set_schema({n: ["id"] for n in names})
        await _generate(http_client)
        live = _schema({n: ["id", "x"] for n in names})

        llm.column_failures = {2: "[RATE_LIMIT] slow down"}
        summary = (await _apply_drift(http_client, live))["summary"]
        assert summary["columns_added"] == {f"public.{n}": ["x"] for n in names[:4]}
        assert summary["column_tables_failed"] == ["public.e"]
        stored = await store.load_model(db, _CID)
        assert all("x" in stored.tables[f"public.{n}"].columns for n in names[:4])
        assert "x" not in stored.tables["public.e"].columns
        report = await _check_drift(http_client, live)
        assert report["has_actionable_changes"] is True

        llm.column_batches, llm.column_failures = [], {}
        summary = (await _apply_drift(http_client, live))["summary"]
        assert llm.column_batches == [{"public.e": ["x"]}]
        assert summary["columns_added"] == {"public.e": ["x"]}
        assert summary["column_tables_failed"] == []
        assert (await _check_drift(http_client, live))["has_actionable_changes"] is False

    async def test_without_a_provider_the_removals_are_applied_and_the_new_column_stays_reported(
        self, http_client, db, flow
    ) -> None:
        llm, set_schema = flow
        set_schema({"orders": ["id", "total"], "customers": ["id"]})
        await _generate(http_client)
        live = _schema({"orders": ["id", "region"], "customers": ["id"]})  # total dropped

        no_provider = HTTPException(status_code=400, detail="No API key configured for claude")
        with patch(
            "app.routers.semantic._resolve_provider", new=AsyncMock(side_effect=no_provider)
        ):
            summary = (await _apply_drift(http_client, live))["summary"]
        assert summary["columns_removed"] == {"public.orders": ["total"]}
        assert summary["columns_added"] == {}
        assert llm.column_batches == []
        assert set((await store.load_model(db, _CID)).tables["public.orders"].columns) == {"id"}

        report = await _check_drift(http_client, live)
        assert report["has_actionable_changes"] is True
        assert any("public.orders.region" in w for w in report["warnings"])
