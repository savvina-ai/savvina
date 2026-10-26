# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Phased generation driven through the real routes and the real semantic store.

Only the LLM (``generate_tables`` / ``generate_globals``), provider resolution and
schema loading are mocked; everything else — the routes, the row lock, the store's
reconcile, run stamping, provenance and the full save — runs against an in-memory
SQLite database. SQLite ignores ``FOR NO KEY UPDATE`` / ``FOR UPDATE``, so this
proves the flow, not the locking (that is covered against PostgreSQL by review, and
by the route unit tests asserting the lock is taken).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select

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
    SemanticSuggestion,
    # Emptied by _invalidate_connection_caches on every finalising write.
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
