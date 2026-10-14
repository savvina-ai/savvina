# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Phased generation driven through the real routes and the real semantic store.

Only the LLM (``generate_tables`` / ``generate_globals``), provider resolution and
schema loading are mocked; everything else — the routes, the row lock, the store's
reconcile, run stamping, provenance and the full save — runs against an in-memory
SQLite database. SQLite ignores ``FOR UPDATE``, so this proves the flow, not the
locking (that is covered against PostgreSQL by review, and by the route unit tests
asserting the lock is taken).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import get_db
from app.datasources.models import ColumnInfo, DataSourceSchema, SchemaInfo, TableInfo
from app.main import app
from app.models.cache import QueryCacheEntry
from app.models.connection import Connection
from app.models.semantic_store import (
    SemanticColumnRow,
    SemanticGlobalItemRow,
    SemanticHistoryRow,
    SemanticMetricRow,
    SemanticModelRow,
    SemanticRelationshipRow,
    SemanticTableRow,
)
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
    # Emptied by _invalidate_connection_caches on every finalising write.
    QueryCacheEntry,
    UserSchemaCache,
    TableEmbeddingCache,
)


def _schema(tables: dict[str, list[str]]) -> DataSourceSchema:
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
    )


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        for model in _TABLES:
            await conn.run_sync(model.__table__.create)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        session.add(
            Connection(id=_CID, name="flow", source_type="postgresql", config_encrypted=b"")
        )
        await session.commit()

        async def _get_db() -> AsyncIterator[AsyncSession]:
            yield session

        app.dependency_overrides[get_db] = _get_db
        yield session
    await engine.dispose()


class _Llm:
    """The mocked LLM: annotates whatever it is asked for, and records the asks."""

    def __init__(self) -> None:
        self.description = "generated"
        self.batches: list[list[str]] = []
        self.during_batch = None  # optional async hook, run inside the "LLM call"
        self.globals_tables: dict[str, TableSemantic] = {}

    async def generate_tables(self, *, schema, table_keys, **_kw) -> dict[str, TableSemantic]:
        self.batches.append(list(table_keys))
        if self.during_batch is not None:
            await self.during_batch()
        wanted = set(table_keys)
        return {
            f"{t.schema_name}.{t.name}": TableSemantic(
                display_name=t.name.title(),
                description=f"{self.description} {t.name}",
                columns={c.name: ColumnSemantic(display_name=c.name.upper()) for c in t.columns},
            )
            for t in schema.tables
            if f"{t.schema_name}.{t.name}" in wanted
        }

    async def generate_globals(self, **kw):
        self.globals_tables = kw["all_tables"]
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

    def set_schema(tables: dict[str, list[str]]) -> None:
        current["schema"] = _schema(tables)

    with (
        patch("app.routers.semantic._load_schema_for_connection", new=load_schema),
        patch(
            "app.routers.semantic._resolve_provider",
            new=AsyncMock(return_value=(MagicMock(), "model-x", "claude")),
        ),
        patch.object(SemanticModelGenerator, "generate_tables", new=llm.generate_tables),
        patch.object(SemanticModelGenerator, "generate_globals", new=llm.generate_globals),
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
        marks = await store.provenance_map(db, _CID)
        assert "description" in marks["table"]["public.customers"]
        assert "description" in marks["column"]["public.orders.ssn"]
        assert store.relationship_key(edge) in marks["relationship"]
        # Kept in the store, never shown to the LLM.
        assert set(llm.globals_tables) == {"public.orders"}
        assert set(llm.globals_tables["public.orders"].columns) == {"id", "customer_id"}
