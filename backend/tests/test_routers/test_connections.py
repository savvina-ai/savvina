# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for /api/v1/connections endpoints."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import get_settings
from app.database import get_db
from app.datasources.errors import ErrorCategory, ExplainError
from app.datasources.models import ConnectionResult
from app.main import app
from app.services.row_filter import RowFilterRejectedError

from .conftest import MockResult, _make_conn, _mock_db


@pytest.fixture
def no_verification():
    """Row-filter saves verify every new or changed pair against the live database; these
    tests are about the rest of the handler. A PUT decrypts the stored config before it
    verifies, so the fake connection's config is stubbed too."""
    with (
        patch("app.routers.connections._verify_row_filters", new=AsyncMock()) as verify,
        patch("app.routers.connections.decrypt_value", return_value="{}"),
    ):
        yield verify


# ── POST /api/v1/connections/test ────────────────────────────────────────────────


class TestTestNewConnection:
    async def test_success_returns_200(self, http_client):
        adapter = MagicMock()
        adapter.test_connection = AsyncMock(
            return_value=ConnectionResult(success=True, message="Connected")
        )
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            resp = await http_client.post(
                "/api/v1/connections/test",
                json={"source_type": "postgresql", "config": {"host": "localhost"}},
            )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    async def test_unknown_source_type_returns_400(self, http_client):
        # Use a license-allowed source_type so the tier check passes;
        # the mocked create_datasource still raises ValueError → 400.
        with patch(
            "app.routers.connections.create_datasource",
            side_effect=ValueError("Unknown source_type"),
        ):
            resp = await http_client.post(
                "/api/v1/connections/test",
                json={"source_type": "postgresql", "config": {}},
            )
        assert resp.status_code == 400

    async def test_connection_failure_returns_400(self, http_client):
        adapter = MagicMock()
        adapter.test_connection = AsyncMock(
            return_value=ConnectionResult(success=False, message="Connection refused")
        )
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            resp = await http_client.post(
                "/api/v1/connections/test",
                json={"source_type": "postgresql", "config": {"host": "bad-host"}},
            )
        assert resp.status_code == 400


# ── POST /api/v1/connections ──────────────────────────────────────────────────────


class TestCreateConnection:
    async def test_returns_201(self, http_client):
        db = _mock_db()
        db.refresh = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/connections",
            json={
                "name": "My DB",
                "source_type": "postgresql",
                "config": {"host": "localhost", "port": 5432},
            },
        )
        assert resp.status_code == 201

    async def test_add_and_commit_called(self, http_client):
        db = _mock_db()
        db.refresh = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db
        await http_client.post(
            "/api/v1/connections",
            json={
                "name": "My DB",
                "source_type": "postgresql",
                "config": {"host": "localhost"},
            },
        )
        db.add.assert_called_once()
        db.commit.assert_called_once()

    async def test_missing_name_returns_422(self, http_client):
        resp = await http_client.post(
            "/api/v1/connections",
            json={"source_type": "postgresql", "config": {}},
        )
        assert resp.status_code == 422

    async def test_invalid_row_filters_returns_422_and_creates_nothing(self, http_client):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/connections",
            json={
                "name": "My DB",
                "source_type": "postgresql",
                "config": {"host": "localhost"},
                "privacy_settings": {"row_filters": {"public.t": "tenant_id = 1; DROP TABLE t"}},
            },
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            "row_filters[\"public.t\"] must be a single condition without ';'"
        )
        db.add.assert_not_called()

    async def test_row_filters_are_stored_clean_and_row_filter_sql_is_dropped(
        self, http_client, no_verification
    ):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/connections",
            json={
                "name": "My DB",
                "source_type": "postgresql",
                "config": {"host": "localhost"},
                "privacy_settings": {
                    "include_sample_values": False,
                    "row_filters": {" public.t ": "  tenant_id = 1 "},
                    "row_filter_sql": "tenant_id = 1",
                },
            },
        )
        assert resp.status_code == 201
        assert db.add.call_args.args[0].privacy_settings == {
            "include_sample_values": False,
            "row_filters": {"public.t": "tenant_id = 1"},
        }

    @pytest.mark.parametrize("value", [False, 0, ["x"], "tenant_id = 1"])
    async def test_non_object_row_filters_returns_422_and_creates_nothing(self, http_client, value):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/connections",
            json={
                "name": "My DB",
                "source_type": "postgresql",
                "config": {"host": "localhost"},
                "privacy_settings": {"row_filters": value},
            },
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            "row_filters must be an object mapping schema.table to a condition"
        )
        db.add.assert_not_called()

    async def test_null_row_filters_returns_422_and_creates_nothing(self, http_client):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/connections",
            json={
                "name": "My DB",
                "source_type": "postgresql",
                "config": {"host": "localhost"},
                "privacy_settings": {"row_filters": None},
            },
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            "row_filters must be an object mapping schema.table to a condition"
        )
        db.add.assert_not_called()

    async def test_unregistered_source_type_refuses_row_filters(self, http_client):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/connections",
            json={
                "name": "My DB",
                "source_type": "nosuch",
                "config": {"host": "localhost"},
                "privacy_settings": {"row_filters": {"s.t": "tag = '#1'"}},
            },
        )
        assert resp.status_code == 422
        # The rewriter knows no dialect for an unregistered source type (Task 5b).
        assert resp.json()["detail"] == "row_filters are not supported for this database"
        db.add.assert_not_called()


# ── GET /api/v1/connections ───────────────────────────────────────────────────────


class TestListConnections:
    async def test_returns_200(self, http_client):
        conn = _make_conn()
        db = _mock_db(MockResult(rows=[conn]))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections")
        assert resp.status_code == 200

    async def test_response_is_paginated(self, http_client):
        db = _mock_db(MockResult(rows=[]))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections")
        body = resp.json()
        assert "items" in body
        assert isinstance(body["items"], list)

    async def test_returns_connection_fields(self, http_client):
        conn = _make_conn(id="abc-123", name="Sales DB")
        db = _mock_db(MockResult(rows=[conn]))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections")
        data = resp.json()["items"]
        assert len(data) == 1
        assert data[0]["id"] == "abc-123"
        assert data[0]["name"] == "Sales DB"
        assert "semantic_model_updated_at" not in data[0]


# ── GET /api/v1/connections/{id} ─────────────────────────────────────────────────


class TestGetConnection:
    async def test_returns_200_for_known_connection(self, http_client):
        conn = _make_conn()
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/conn-123")
        assert resp.status_code == 200

    async def test_returns_connection_detail(self, http_client):
        conn = _make_conn(id="conn-123", name="My DB")
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/conn-123")
        body = resp.json()
        assert body["id"] == "conn-123"
        assert body["name"] == "My DB"
        assert "semantic_model_updated_at" not in body

    async def test_returns_404_for_unknown_connection(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/missing")
        assert resp.status_code == 404


# ── POST /api/v1/connections/{id}/test ───────────────────────────────────────────


class TestTestExistingConnection:
    async def test_success_returns_200(self, http_client):
        conn = _make_conn()
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        adapter = MagicMock()
        adapter.test_connection = AsyncMock(
            return_value=ConnectionResult(success=True, message="Connected")
        )
        with (
            patch("app.routers.connections.decrypt_value", return_value='{"host":"localhost"}'),
            patch("app.routers.connections.create_datasource", return_value=adapter),
        ):
            resp = await http_client.post("/api/v1/connections/conn-123/test")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    async def test_not_found_returns_404(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post("/api/v1/connections/missing/test")
        assert resp.status_code == 404

    async def test_connection_error_returns_400(self, http_client):
        conn = _make_conn()
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        adapter = MagicMock()
        adapter.test_connection = AsyncMock(
            return_value=ConnectionResult(success=False, message="timeout")
        )
        with (
            patch("app.routers.connections.decrypt_value", return_value='{"host":"x"}'),
            patch("app.routers.connections.create_datasource", return_value=adapter),
        ):
            resp = await http_client.post("/api/v1/connections/conn-123/test")
        assert resp.status_code == 400


# ── DELETE /api/v1/connections/{id} ──────────────────────────────────────────────


class TestDeleteConnection:
    @pytest.fixture(autouse=True)
    def stub_deletes(self):
        # delete_model has its own coverage in tests/test_semantic; here it is a seam,
        # and ``yield``ing the mock lets tests assert the route awaits it.
        with (
            patch("app.routers.connections._evict_pg_pool", new=AsyncMock()),
            patch("app.routers.connections.decrypt_value", return_value='{"host": "h"}'),
            patch(
                "app.routers.connections.semantic_store.delete_model", new=AsyncMock()
            ) as delete_model,
        ):
            yield delete_model

    async def test_success_returns_204(self, http_client, stub_deletes):
        conn = _make_conn()
        # execute calls: (1) get conn, (2) get session ids, (3) del cache, (4) del examples,
        # (5) del UserSchemaCache, (6) del TableEmbeddingCache, (7) del SemanticSuggestion
        db = _mock_db(
            MockResult(single=conn),
            MockResult(rows=[]),  # no chat sessions → skip message/session deletes
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete VerifiedExample
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
            MockResult(),  # delete SemanticSuggestion
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.delete("/api/v1/connections/conn-123")
        assert resp.status_code == 204
        assert db.execute.call_count == 7
        # The legacy blob is cleared by store.delete_model, patched out here.
        # Soft-delete never fires ON DELETE CASCADE, so the semantic rows go explicitly.
        stub_deletes.assert_awaited_once()
        args, kwargs = stub_deletes.await_args
        assert args[1] == "conn-123"
        assert kwargs["changed_by"] == "admin-1"

    async def test_not_found_returns_404(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.delete("/api/v1/connections/missing")
        assert resp.status_code == 404

    async def test_cascades_session_messages(self, http_client):
        conn = _make_conn()
        # select(ChatSession.id) returns rows as plain tuples: [("sess-1",)]
        # execute: get conn, get session ids, del messages, del sessions, del cache, del examples,
        # del UserSchemaCache, del TableEmbeddingCache, del SemanticSuggestion
        db = _mock_db(
            MockResult(single=conn),
            MockResult(rows=[("sess-1",)]),
            MockResult(),  # delete ChatMessage
            MockResult(),  # delete ChatSession
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete VerifiedExample
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
            MockResult(),  # delete SemanticSuggestion
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.delete("/api/v1/connections/conn-123")
        assert resp.status_code == 204
        assert db.execute.call_count == 9


# ── GET /api/v1/connections/{id}/schema ─────────────────────────────────────────


class TestGetSchema:
    async def test_returns_cached_schema(self, http_client):
        schema = {"tables": {"users": {}}}
        conn = _make_conn()
        usc = MagicMock()
        usc.schema_cache = schema
        db = _mock_db(
            MockResult(single=conn),  # get_connection_or_404
            MockResult(single=usc),  # _get_user_schema_cache
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/conn-123/schema")
        assert resp.status_code == 200
        assert resp.json() == schema
        assert db.execute.call_count == 2

    async def test_no_cache_returns_404(self, http_client):
        conn = _make_conn()
        db = _mock_db(
            MockResult(single=conn),  # get_connection_or_404
            MockResult(single=None),  # _get_user_schema_cache → no cache
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/conn-123/schema")
        assert resp.status_code == 404
        assert db.execute.call_count == 2

    async def test_connection_not_found_returns_404(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/missing/schema")
        assert resp.status_code == 404


# ── POST /api/v1/connections/{id}/schema/refresh ─────────────────────────────────


class TestRefreshSchema:
    async def test_success_returns_schema(self, http_client):
        from dataclasses import dataclass

        @dataclass
        class FakeSchema:
            tables: dict

        conn = _make_conn()
        fake_schema = FakeSchema(tables={})
        adapter = MagicMock()
        adapter.connect = AsyncMock()
        adapter.introspect = AsyncMock(return_value=fake_schema)
        adapter.disconnect = AsyncMock()
        db = _mock_db(
            MockResult(single=conn),  # get_connection_or_404
            MockResult(single=None),  # _get_user_schema_cache → no existing cache (db.add called)
            MockResult(),  # delete TableEmbeddingCache
            MockResult(),  # delete QueryCacheEntry
        )
        app.dependency_overrides[get_db] = lambda: db
        with (
            patch("app.services.schema_utils.decrypt_value", return_value='{"host":"localhost"}'),
            patch("app.services.schema_utils.create_datasource", return_value=adapter),
        ):
            resp = await http_client.post("/api/v1/connections/conn-123/schema/refresh")
        assert resp.status_code == 200
        assert db.execute.call_count == 4

    async def test_an_unchanged_schema_still_flushes_the_derived_caches(self, http_client):
        """The explicit Refresh always clears embeddings and cached answers, even when
        the schema hash says nothing changed (``flush="always"``)."""
        from app.datasources.models import ColumnInfo, DataSourceSchema, TableInfo
        from app.models.cache import QueryCacheEntry
        from app.models.table_embedding_cache import TableEmbeddingCache
        from app.semantic.generator import SemanticModelGenerator

        schema = DataSourceSchema(
            source_type="postgresql",
            schemas=[],
            tables=[
                TableInfo(
                    catalog=None,
                    schema_name="public",
                    name="orders",
                    table_type="table",
                    columns=[ColumnInfo(name="id", data_type="integer", native_type="int4")],
                )
            ],
        )
        cache = MagicMock()
        cache.schema_hash = SemanticModelGenerator().compute_schema_hash(schema)
        adapter = MagicMock()
        adapter.connect = AsyncMock()
        adapter.introspect = AsyncMock(return_value=schema)
        adapter.disconnect = AsyncMock()
        db = _mock_db(
            MockResult(single=_make_conn()),  # get_connection_or_404
            MockResult(single=cache),  # the user's cache, same hash
            MockResult(),  # delete TableEmbeddingCache
            MockResult(),  # delete QueryCacheEntry
        )
        app.dependency_overrides[get_db] = lambda: db
        with (
            patch("app.services.schema_utils.decrypt_value", return_value='{"host":"localhost"}'),
            patch("app.services.schema_utils.create_datasource", return_value=adapter),
        ):
            resp = await http_client.post("/api/v1/connections/conn-123/schema/refresh")
        assert resp.status_code == 200
        deleted = [call.args[0].table.name for call in db.execute.await_args_list[2:]]
        assert deleted == [TableEmbeddingCache.__tablename__, QueryCacheEntry.__tablename__]
        db.commit.assert_awaited_once()

    async def test_introspection_error_returns_400(self, http_client):
        conn = _make_conn()
        adapter = MagicMock()
        adapter.connect = AsyncMock()
        adapter.introspect = AsyncMock(side_effect=Exception("timeout"))
        adapter.disconnect = AsyncMock()
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        with (
            patch("app.services.schema_utils.decrypt_value", return_value='{"host":"localhost"}'),
            patch("app.services.schema_utils.create_datasource", return_value=adapter),
        ):
            resp = await http_client.post("/api/v1/connections/conn-123/schema/refresh")
        assert resp.status_code == 400


# ── PUT /api/v1/connections/{id}/privacy ─────────────────────────────────────────


class TestUpdatePrivacy:
    async def test_success_returns_200(self, http_client):
        conn = _make_conn(privacy_settings={"include_sample_values": False})
        db = _mock_db(
            MockResult(single=conn),  # _get_or_404
            MockResult(),  # update
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete TableEmbeddingCache
        )
        db.refresh = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy",
            json={"include_sample_values": True},
        )
        assert resp.status_code == 200
        assert db.execute.call_count == 5

    async def test_not_found_returns_404(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/connections/missing/privacy",
            json={"include_sample_values": True},
        )
        assert resp.status_code == 404

    @staticmethod
    def _stored_privacy(db) -> dict:
        # execute call 0 is the lookup, call 1 the UPDATE.
        update_stmt = db.execute.call_args_list[1].args[0]
        return update_stmt.compile().params["privacy_settings"]

    @staticmethod
    def _db_for_update(conn):
        db = _mock_db(
            MockResult(single=conn),  # _get_or_404
            MockResult(),  # update
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete TableEmbeddingCache
        )

        async def _refresh(obj) -> None:
            # Like the real refresh: the object shows what the UPDATE stored.
            obj.privacy_settings = TestUpdatePrivacy._stored_privacy(db)

        db.refresh = AsyncMock(side_effect=_refresh)
        return db

    @staticmethod
    def _use_db(db) -> None:
        # A factory call binds ``db`` (no B023); a ``db=db`` default would be read by FastAPI
        # as a dependency parameter.
        app.dependency_overrides[get_db] = lambda: db

    @staticmethod
    def _executes(db) -> int:
        return db.execute.call_count

    @staticmethod
    def _touches_schema_cache(db) -> bool:
        return any("user_schema_caches" in str(c.args[0]) for c in db.execute.call_args_list)

    async def test_sets_the_map_and_keeps_the_schema_cache(self, http_client, no_verification):
        conn = _make_conn(privacy_settings={"include_sample_values": False})
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy",
            json={"row_filters": {" public.t ": " tenant_id = 42 "}},
        )
        assert resp.status_code == 200
        assert self._stored_privacy(db) == {
            "include_sample_values": False,
            "row_filters": {"public.t": "tenant_id = 42"},
        }
        # lookup + update + delete TableEmbeddingCache + delete QueryCacheEntry: the
        # UserSchemaCache row stays.
        assert self._executes(db) == 4
        assert not self._touches_schema_cache(db)

    async def test_any_other_key_still_flushes_the_schema_cache(self, http_client, no_verification):
        conn = _make_conn(privacy_settings={})
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy",
            json={"row_filters": {"public.t": "tenant_id = 42"}, "excluded_tables": ["x"]},
        )
        assert resp.status_code == 200
        assert self._executes(db) == 5

    async def test_whole_form_with_only_a_condition_changed_keeps_the_schema_cache(
        self, http_client, no_verification
    ):
        stored = {
            "include_sample_values": False,
            "excluded_tables": ["a"],
            "row_filters": {"public.t": "tenant_id = 42"},
        }
        conn = _make_conn(privacy_settings=dict(stored))
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy",
            json={**stored, "row_filters": {"public.t": "tenant_id = 43"}},
        )
        assert resp.status_code == 200
        assert self._stored_privacy(db)["row_filters"] == {"public.t": "tenant_id = 43"}
        assert self._executes(db) == 4
        assert not self._touches_schema_cache(db)

    async def test_padded_key_is_stored_clean_and_keeps_the_schema_cache(self, http_client):
        stored = {
            "include_sample_values": False,
            "row_filters": {"public.t": "tenant_id = 42"},
        }
        conn = _make_conn(privacy_settings=dict(stored))
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy",
            json={**stored, "row_filters": {" public.t ": " tenant_id = 42 "}},
        )
        assert resp.status_code == 200
        assert self._stored_privacy(db) == stored
        assert self._executes(db) == 4
        assert not self._touches_schema_cache(db)

    async def test_whole_form_with_another_key_changed_flushes_the_schema_cache(
        self, http_client, no_verification
    ):
        stored = {
            "include_sample_values": False,
            "excluded_tables": ["a"],
            "row_filters": {"public.t": "tenant_id = 42"},
        }
        conn = _make_conn(privacy_settings=dict(stored))
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy",
            json={**stored, "excluded_tables": ["a", "b"]},
        )
        assert resp.status_code == 200
        assert self._executes(db) == 5
        assert self._touches_schema_cache(db)

    async def test_the_lookup_takes_the_row_lock(self, http_client):
        from sqlalchemy.dialects import postgresql

        conn = _make_conn(privacy_settings={})
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy", json={"include_sample_values": True}
        )
        assert resp.status_code == 200
        lookup = db.execute.call_args_list[0].args[0]
        sql = str(lookup.compile(dialect=postgresql.dialect()))
        assert "FOR NO KEY UPDATE" in sql

    async def test_empty_map_clears_when_there_is_no_legacy_filter(self, http_client):
        conn = _make_conn(privacy_settings={"row_filters": {"public.t": "tenant_id = 42"}})
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy", json={"row_filters": {}}
        )
        assert resp.status_code == 200
        assert self._stored_privacy(db) == {"row_filters": {}}

    async def test_empty_map_on_a_legacy_filter_is_422_and_writes_nothing(self, http_client):
        conn = _make_conn(privacy_settings={"row_filter_sql": "tenant_id = 42"})
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy",
            json={"row_filters": {}, "include_sample_values": True},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            "row_filters: this connection still has the old whole-query filter. Define"
            " per-table filters, or send clear_legacy_row_filter to remove it."
        )
        assert self._executes(db) == 1
        db.commit.assert_not_called()

    async def test_non_empty_map_ends_the_migration_state(self, http_client, no_verification):
        conn = _make_conn(privacy_settings={"row_filter_sql": "tenant_id = 42"})
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy",
            json={"row_filters": {"public.t": "tenant_id = 42"}},
        )
        assert resp.status_code == 200
        assert self._stored_privacy(db) == {"row_filters": {"public.t": "tenant_id = 42"}}
        assert resp.json()["row_filter_needs_migration"] is False

    @pytest.mark.parametrize(
        "body",
        [
            {"row_filters": {}, "clear_legacy_row_filter": True},
            {"clear_legacy_row_filter": True},
        ],
    )
    async def test_clear_flag_removes_the_legacy_filter(self, http_client, body):
        conn = _make_conn(privacy_settings={"row_filter_sql": "tenant_id = 42"})
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put("/api/v1/connections/conn-123/privacy", json=body)
        assert resp.status_code == 200
        assert self._stored_privacy(db) == {"row_filters": {}}
        assert self._executes(db) == 4
        assert not self._touches_schema_cache(db)
        assert resp.json()["row_filter_needs_migration"] is False

    async def test_clear_flag_keeps_an_existing_map(self, http_client):
        conn = _make_conn(privacy_settings={"row_filters": {"public.t": "tenant_id = 42"}})
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy", json={"clear_legacy_row_filter": True}
        )
        assert resp.status_code == 200
        assert self._stored_privacy(db) == {"row_filters": {"public.t": "tenant_id = 42"}}

    async def test_unrelated_keys_leave_a_legacy_connection_in_the_migration_state(
        self, http_client
    ):
        conn = _make_conn(privacy_settings={"row_filter_sql": "tenant_id = 42"})
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy", json={"include_sample_values": True}
        )
        assert resp.status_code == 200
        stored = self._stored_privacy(db)
        assert stored == {"row_filter_sql": "tenant_id = 42", "include_sample_values": True}
        assert "row_filters" not in stored
        assert resp.json()["row_filter_needs_migration"] is True

    async def test_row_filter_sql_in_the_body_is_ignored(self, http_client):
        conn = _make_conn(privacy_settings={"row_filter_sql": "tenant_id = 42"})
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy", json={"row_filter_sql": "x = 1"}
        )
        assert resp.status_code == 200
        assert self._stored_privacy(db) == {"row_filter_sql": "tenant_id = 42"}

    async def test_absent_or_null_leaves_the_map_unchanged(self, http_client):
        for body in ({"include_sample_values": True}, {"row_filters": None}):
            conn = _make_conn(privacy_settings={"row_filters": {"public.t": "tenant_id = 42"}})
            db = self._db_for_update(conn)
            self._use_db(db)
            resp = await http_client.put("/api/v1/connections/conn-123/privacy", json=body)
            assert resp.status_code == 200
            assert self._stored_privacy(db)["row_filters"] == {"public.t": "tenant_id = 42"}

    async def test_invalid_map_returns_422_and_writes_nothing(self, http_client):
        conn = _make_conn(privacy_settings={})
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy",
            json={"row_filters": {"public.t": "WHERE tenant_id = 1"}},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            'row_filters["public.t"] is a condition: write it without the leading WHERE'
        )
        assert self._executes(db) == 1
        db.commit.assert_not_called()

    async def test_validation_uses_the_connections_dialect(self, http_client, no_verification):
        # "#" starts a comment on MySQL only.
        for source_type, expected in (("postgresql", 200), ("mysql", 422)):
            conn = _make_conn(source_type=source_type, privacy_settings={})
            db = self._db_for_update(conn)
            self._use_db(db)
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={"row_filters": {"s.t": "tag = '#1'"}},
            )
            assert resp.status_code == expected, source_type

    async def test_row_filters_on_missing_connection_returns_404_before_validation(
        self, http_client
    ):
        db = _mock_db(MockResult(single=None))
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/missing/privacy", json={"row_filters": {"s.t": "WHERE x = 1"}}
        )
        assert resp.status_code == 404

    async def test_non_object_row_filters_is_rejected_by_the_schema(self, http_client):
        conn = _make_conn(privacy_settings={})
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy", json={"row_filters": "tenant_id = 1"}
        )
        assert resp.status_code == 422
        assert self._executes(db) == 0
        db.commit.assert_not_called()


class TestRowFilterNeedsMigration:
    @pytest.mark.parametrize(
        ("privacy", "expected"),
        [
            (None, False),
            ({}, False),
            ({"row_filter_sql": "tenant_id = 42"}, True),
            # A stored {} next to a legacy value is still the migration state: with False,
            # _executable would see an empty map and run unfiltered. The router never stores
            # this combination (422 or the legacy value is popped); the property stays safe.
            ({"row_filter_sql": "tenant_id = 42", "row_filters": {}}, True),
            ({"row_filter_sql": "tenant_id = 42", "row_filters": {"s.t": "x = 1"}}, False),
            ({"row_filter_sql": None}, False),
        ],
    )
    async def test_detail_reports_the_state(self, http_client, privacy, expected):
        conn = _make_conn(privacy_settings=privacy)
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/conn-123")
        assert resp.status_code == 200
        body = resp.json()
        assert body["row_filter_needs_migration"] is expected
        assert body["privacy_settings"] == privacy


# ── PUT /api/v1/connections/{id}/execution-mode ──────────────────────────────────


class TestUpdateExecutionMode:
    async def test_success_returns_200(self, http_client):
        conn = _make_conn()
        db = _mock_db(
            MockResult(single=conn),  # _get_or_404
            MockResult(),  # update
        )
        db.refresh = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/connections/conn-123/execution-mode",
            json={"execution_mode": "review_first"},
        )
        assert resp.status_code == 200
        assert db.execute.call_count == 2

    async def test_invalid_mode_returns_422(self, http_client):
        resp = await http_client.put(
            "/api/v1/connections/conn-123/execution-mode",
            json={"execution_mode": "invalid_mode"},
        )
        assert resp.status_code == 422

    async def test_not_found_returns_404(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/connections/missing/execution-mode",
            json={"execution_mode": "auto_execute"},
        )
        assert resp.status_code == 404


# ── PUT /api/v1/connections/{id}/config ──────────────────────────────────────────


class TestUpdateConfig:
    @pytest.fixture(autouse=True)
    def patch_evict(self):
        with patch("app.routers.connections._evict_pg_pool", new=AsyncMock()):
            yield

    async def test_name_change_returns_200(self, http_client):
        conn = _make_conn()
        db = _mock_db(
            MockResult(single=conn),  # get_connection_or_404
            MockResult(),  # update Connection
        )
        db.refresh = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/connections/conn-123/config",
            json={"name": "Renamed DB"},
        )
        assert resp.status_code == 200
        # name-only: no config change, so UserSchemaCache is NOT deleted
        assert db.execute.call_count == 2

    async def test_name_change_sets_updated_at(self, http_client):
        from datetime import UTC
        from datetime import datetime as real_dt

        conn = _make_conn()
        db = _mock_db(
            MockResult(single=conn),  # get_connection_or_404
            MockResult(),  # update Connection
        )
        db.refresh = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db
        fixed_time = real_dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        with patch("app.routers.connections.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_time
            resp = await http_client.put(
                "/api/v1/connections/conn-123/config",
                json={"name": "Renamed DB"},
            )
        assert resp.status_code == 200
        mock_dt.now.assert_called_once_with(UTC)

    async def test_config_change_invalidates_user_schema_caches(self, http_client):
        conn = _make_conn()
        db = _mock_db(
            MockResult(single=conn),  # get_connection_or_404
            MockResult(),  # update Connection
            MockResult(),  # delete UserSchemaCache rows
            MockResult(),  # delete QueryCacheEntry rows
            MockResult(),  # delete TableEmbeddingCache rows
        )
        db.refresh = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db
        with (
            patch("app.routers.connections.decrypt_value", return_value='{"host":"h"}'),
            patch("app.routers.connections.encrypt_value", return_value=b"enc"),
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/config",
                json={"config": {"host": "newhost", "port": 5432, "database": "mydb"}},
            )
        assert resp.status_code == 200
        # get_connection + update + delete UserSchemaCache + delete QueryCacheEntry
        # + delete TableEmbeddingCache = 5
        assert db.execute.call_count == 5


# ── GET /api/v1/connections/{id}/config — SEC-2 credential masking ────────────


class TestGetConnectionConfig:
    async def test_returns_masked_password(self, http_client) -> None:
        """GET /connections/{id}/config must mask the password field (SEC-2)."""
        conn = _make_conn()
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        with patch(
            "app.routers.connections.decrypt_value",
            return_value='{"host":"localhost","password":"s3cr3t"}',
        ):
            resp = await http_client.get("/api/v1/connections/conn-123/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["config"]["host"] == "localhost"
        assert body["config"]["password"] == "**redacted**"

    async def test_non_sensitive_fields_not_masked(self, http_client) -> None:
        """Non-sensitive fields must pass through the config response unchanged."""
        conn = _make_conn()
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        with patch(
            "app.routers.connections.decrypt_value",
            return_value='{"host":"db.example.com","port":5432,"database":"prod"}',
        ):
            resp = await http_client.get("/api/v1/connections/conn-123/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["config"]["host"] == "db.example.com"
        assert body["config"]["port"] == 5432
        assert body["config"]["database"] == "prod"

    async def test_put_config_sentinel_preserves_existing_password(self, http_client) -> None:
        """PUT /connections/{id}/config must preserve stored password when sentinel is sent."""
        conn = _make_conn()
        db = _mock_db(
            MockResult(single=conn),
            MockResult(),  # update Connection
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete TableEmbeddingCache
        )
        db.refresh = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db

        captured_config: dict = {}

        def fake_encrypt(data: str, key: bytes) -> bytes:
            captured_config.update(json.loads(data))
            return b"encrypted"

        with (
            patch(
                "app.routers.connections.decrypt_value",
                return_value='{"host":"db.example.com","password":"original-pass"}',
            ),
            patch("app.routers.connections.encrypt_value", side_effect=fake_encrypt),
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/config",
                json={"config": {"host": "new-host", "password": "**redacted**"}},
            )
        assert resp.status_code == 200
        assert captured_config["password"] == "original-pass"
        assert captured_config["host"] == "new-host"

    async def test_put_config_new_password_overwrites(self, http_client) -> None:
        """PUT /connections/{id}/config with a real new password must update it."""
        conn = _make_conn()
        db = _mock_db(
            MockResult(single=conn),
            MockResult(),  # update Connection
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete TableEmbeddingCache
        )
        db.refresh = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db

        captured_config: dict = {}

        def fake_encrypt(data: str, key: bytes) -> bytes:
            captured_config.update(json.loads(data))
            return b"encrypted"

        with (
            patch(
                "app.routers.connections.decrypt_value",
                return_value='{"host":"db.example.com","password":"old-pass"}',
            ),
            patch("app.routers.connections.encrypt_value", side_effect=fake_encrypt),
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/config",
                json={"config": {"host": "db.example.com", "password": "new-pass"}},
            )
        assert resp.status_code == 200
        assert captured_config["password"] == "new-pass"


# ── Response shape ──────────────────────────────────────────────────────────────


class TestConnectionResponseShape:
    def test_semantic_model_updated_at_is_not_served(self) -> None:
        """Nothing writes Connection.semantic_model_updated_at since the semantic store;
        the column goes with the legacy blob, so the API stops serving a stale value."""
        from app.schemas.connection import ConnectionDetail, ConnectionResponse

        assert "semantic_model_updated_at" not in ConnectionResponse.model_fields
        assert "semantic_model_updated_at" not in ConnectionDetail.model_fields


# ── Row-filter verification against the live database ────────────────────────────


def _verifying_adapter(explain=None) -> MagicMock:
    adapter = MagicMock()
    adapter.connect = AsyncMock(return_value=MagicMock(success=True))
    adapter.disconnect = AsyncMock()
    adapter.explain = explain or AsyncMock()
    adapter.classify_error = MagicMock(return_value=ErrorCategory.UNKNOWN_NAME)
    return adapter


class TestRowFilterVerification:
    @pytest.fixture(autouse=True)
    def _config(self):
        with patch("app.routers.connections.decrypt_value", return_value='{"host": "db"}'):
            yield

    def _db(self, conn):
        return TestUpdatePrivacy._db_for_update(conn)

    async def test_new_pair_is_planned_as_the_rewriter_writes_it(self, http_client):
        """The statement planned is ``SELECT * FROM "s"."t"`` after ``apply_row_filters``:
        the condition as sqlglot writes it back, inside the fenced derived table."""
        conn = _make_conn(privacy_settings={})
        adapter = _verifying_adapter()
        app.dependency_overrides[get_db] = lambda: self._db(conn)
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={"row_filters": {"public.orders": "tenant_id = 42"}},
            )
        assert resp.status_code == 200
        adapter.explain.assert_awaited_once_with(
            'SELECT * FROM (SELECT * FROM "public"."orders" WHERE (tenant_id = 42) OFFSET 0)'
            ' AS "orders"',
            timeout=get_settings().default_query_timeout,
        )
        adapter.disconnect.assert_awaited_once()

    async def test_a_mysql_pair_is_planned_with_backticks_and_the_limit_fence(self, http_client):
        conn = _make_conn(source_type="mysql", privacy_settings={})
        adapter = _verifying_adapter()
        app.dependency_overrides[get_db] = lambda: self._db(conn)
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={"row_filters": {"shop.orders": "tenant_id = 42"}},
            )
        assert resp.status_code == 200
        adapter.explain.assert_awaited_once_with(
            "SELECT * FROM (SELECT * FROM `shop`.`orders` WHERE (tenant_id = 42)"
            " LIMIT 18446744073709551615) AS `orders`",
            timeout=get_settings().default_query_timeout,
        )

    async def test_a_quoted_key_is_planned_with_its_case(self, http_client):
        conn = _make_conn(privacy_settings={})
        adapter = _verifying_adapter()
        app.dependency_overrides[get_db] = lambda: self._db(conn)
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={"row_filters": {'public."Orders"': "tenant_id = 42"}},
            )
        assert resp.status_code == 200
        planned = adapter.explain.await_args.args[0]
        assert '"public"."Orders" WHERE (tenant_id = 42) OFFSET 0) AS "Orders"' in planned

    async def test_a_pair_the_rewriter_refuses_is_422_and_nothing_is_planned(self, http_client):
        conn = _make_conn(privacy_settings={})
        adapter = _verifying_adapter()
        db = self._db(conn)
        app.dependency_overrides[get_db] = lambda: db
        with (
            patch("app.routers.connections.create_datasource", return_value=adapter),
            patch(
                "app.routers.connections.apply_row_filters",
                side_effect=RowFilterRejectedError(
                    "The query uses a construct the row-filter rewriter cannot write back"
                    " for this dialect, so it was not run."
                ),
            ),
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={"row_filters": {"public.orders": "tenant_id = 42"}},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            'row_filters["public.orders"]: The query uses a construct the row-filter rewriter'
            " cannot write back for this dialect, so it was not run."
        )
        adapter.connect.assert_not_awaited()  # the statements are built before connecting
        adapter.explain.assert_not_awaited()
        assert db.execute.call_count == 1  # the lookup only; no UPDATE

    async def test_a_pair_the_rewriter_does_not_match_is_422_and_nothing_is_planned(
        self, http_client
    ):
        conn = _make_conn(privacy_settings={})
        adapter = _verifying_adapter()
        db = self._db(conn)
        app.dependency_overrides[get_db] = lambda: db
        with (
            patch("app.routers.connections.create_datasource", return_value=adapter),
            patch(
                "app.routers.connections.apply_row_filters",
                side_effect=lambda sql, filters, dialect: sql,
            ),
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={"row_filters": {"public.orders": "tenant_id = 42"}},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            'row_filters["public.orders"]: the filter could not be applied to the table,'
            " so it was not saved."
        )
        adapter.connect.assert_not_awaited()
        adapter.explain.assert_not_awaited()
        assert db.execute.call_count == 1  # the lookup only; no UPDATE

    async def test_only_new_or_changed_pairs_are_explained(self, http_client):
        stored = {"public.a": "x = 1", "public.b": "y = 2"}
        conn = _make_conn(privacy_settings={"row_filters": dict(stored)})
        adapter = _verifying_adapter()
        app.dependency_overrides[get_db] = lambda: self._db(conn)
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={
                    "row_filters": {"public.a": "x = 1", "public.b": "y = 3", "public.c": "z = 1"}
                },
            )
        assert resp.status_code == 200
        explained = [c.args[0] for c in adapter.explain.await_args_list]
        assert len(explained) == 2
        assert any('"b" WHERE (y = 3)' in s for s in explained)
        assert any('"c" WHERE (z = 1)' in s for s in explained)

    async def test_unchanged_map_opens_no_connection(self, http_client):
        stored = {"public.a": "x = 1"}
        conn = _make_conn(privacy_settings={"row_filters": dict(stored)})
        app.dependency_overrides[get_db] = lambda: self._db(conn)
        with patch("app.routers.connections.create_datasource") as create:
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={"row_filters": dict(stored), "include_sample_values": True},
            )
        assert resp.status_code == 200
        create.assert_not_called()

    async def test_put_without_row_filters_opens_no_connection(self, http_client):
        conn = _make_conn(privacy_settings={"row_filters": {"public.a": "x = 1"}})
        app.dependency_overrides[get_db] = lambda: self._db(conn)
        with patch("app.routers.connections.create_datasource") as create:
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy", json={"include_sample_values": True}
            )
        assert resp.status_code == 200
        create.assert_not_called()

    async def test_engine_error_is_422_with_the_redacted_text_and_nothing_stored(self, http_client):
        conn = _make_conn(privacy_settings={})
        cause = RuntimeError('column "nosuch" does not exist')
        explain = AsyncMock(side_effect=ExplainError(str(cause)))
        explain.side_effect.__cause__ = cause
        adapter = _verifying_adapter(explain)
        db = self._db(conn)
        app.dependency_overrides[get_db] = lambda: db
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={"row_filters": {"public.orders": "nosuch = 1"}},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            'row_filters["public.orders"]: rejected by the database: column "nosuch" does not exist'
        )
        assert db.execute.call_count == 1  # the lookup only; no UPDATE
        adapter.disconnect.assert_awaited_once()

    async def test_a_value_quoting_category_is_redacted(self, http_client):
        conn = _make_conn(privacy_settings={})
        explain = AsyncMock(
            side_effect=ExplainError('invalid input syntax for type integer: "abc"')
        )
        adapter = _verifying_adapter(explain)
        adapter.classify_error = MagicMock(return_value=ErrorCategory.DATA)
        app.dependency_overrides[get_db] = lambda: self._db(conn)
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={"row_filters": {"public.orders": "id = 'abc'"}},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            'row_filters["public.orders"]: rejected by the database:'
            " invalid input syntax for type integer: [REDACTED]"
        )

    async def test_a_timeout_is_unverified_not_rejected(self, http_client):
        conn = _make_conn(privacy_settings={})
        adapter = _verifying_adapter(AsyncMock(side_effect=ExplainError("canceling statement")))
        adapter.classify_error = MagicMock(return_value=ErrorCategory.TIMEOUT)
        app.dependency_overrides[get_db] = lambda: self._db(conn)
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={"row_filters": {"public.orders": "tenant_id = 42"}},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            "row filters could not be verified against the database: canceling statement"
        )

    async def test_a_plan_that_does_not_return_is_unverified_within_the_budget(self, http_client):
        """MySQL applies no timeout to an EXPLAIN, so the router bounds the wait itself."""
        conn = _make_conn(privacy_settings={})

        cancelled: list[bool] = []

        async def hang(sql: str, **kwargs: int) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        adapter = _verifying_adapter(AsyncMock(side_effect=hang))
        db = self._db(conn)
        app.dependency_overrides[get_db] = lambda: db
        # The outer timeout makes a missing wait_for fail in seconds instead of hanging.
        with (
            patch("app.routers.connections.create_datasource", return_value=adapter),
            patch("app.routers.connections._explain_budget", return_value=0.01),
        ):
            async with asyncio.timeout(5):
                resp = await http_client.put(
                    "/api/v1/connections/conn-123/privacy",
                    json={"row_filters": {"public.orders": "tenant_id = 42"}},
                )
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            "row filters could not be verified against the database:"
            " the planning check did not finish within 0.01 seconds"
        )
        assert cancelled == [True]
        assert db.execute.call_count == 1
        adapter.disconnect.assert_awaited_once()

    def test_the_budget_is_the_query_timeout_plus_the_grace(self):
        from app.routers.connections import _EXPLAIN_GRACE_SECONDS, _explain_budget

        assert _explain_budget() == get_settings().default_query_timeout + _EXPLAIN_GRACE_SECONDS
        assert _EXPLAIN_GRACE_SECONDS == 5

    async def test_unreachable_database_is_422_and_nothing_stored(self, http_client):
        conn = _make_conn(privacy_settings={})
        adapter = _verifying_adapter()
        adapter.connect = AsyncMock(return_value=MagicMock(success=False, message="refused"))
        db = self._db(conn)
        app.dependency_overrides[get_db] = lambda: db
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={"row_filters": {"public.orders": "tenant_id = 42"}},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            "row filters could not be verified against the database: refused"
        )
        assert db.execute.call_count == 1
        adapter.disconnect.assert_awaited_once()

    async def test_connect_raising_is_422(self, http_client):
        conn = _make_conn(privacy_settings={})
        adapter = _verifying_adapter()
        adapter.connect = AsyncMock(side_effect=OSError("no route to host"))
        app.dependency_overrides[get_db] = lambda: self._db(conn)
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={"row_filters": {"public.orders": "tenant_id = 42"}},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            "row filters could not be verified against the database: no route to host"
        )
        adapter.disconnect.assert_awaited_once()

    async def test_a_driver_error_during_explain_is_unverified(self, http_client):
        conn = _make_conn(privacy_settings={})
        adapter = _verifying_adapter(AsyncMock(side_effect=RuntimeError("driver boom")))
        db = self._db(conn)
        app.dependency_overrides[get_db] = lambda: db
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={"row_filters": {"public.orders": "tenant_id = 42"}},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            "row filters could not be verified against the database: driver boom"
        )
        assert db.execute.call_count == 1
        adapter.disconnect.assert_awaited_once()

    async def test_post_verifies_before_the_connection_is_created(self, http_client):
        db = _mock_db()
        db.refresh = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db
        adapter = _verifying_adapter(
            AsyncMock(side_effect=ExplainError('relation "public.ordres" does not exist'))
        )
        with (
            patch("app.routers.connections.create_datasource", return_value=adapter),
            patch("app.routers.connections.encrypt_value", return_value=b"enc"),
        ):
            resp = await http_client.post(
                "/api/v1/connections",
                json={
                    "name": "My DB",
                    "source_type": "postgresql",
                    "config": {"host": "localhost"},
                    "privacy_settings": {"row_filters": {"public.ordres": "tenant_id = 1"}},
                },
            )
        assert resp.status_code == 422
        assert resp.json()["detail"].startswith(
            'row_filters["public.ordres"]: rejected by the database:'
        )
        db.add.assert_not_called()
        db.commit.assert_not_called()

    async def test_post_with_verified_filters_is_created(self, http_client):
        db = _mock_db()
        db.refresh = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db
        adapter = _verifying_adapter()
        with (
            patch("app.routers.connections.create_datasource", return_value=adapter),
            patch("app.routers.connections.encrypt_value", return_value=b"enc"),
        ):
            resp = await http_client.post(
                "/api/v1/connections",
                json={
                    "name": "My DB",
                    "source_type": "postgresql",
                    "config": {"host": "localhost"},
                    "privacy_settings": {"row_filters": {"public.orders": "tenant_id = 1"}},
                },
            )
        assert resp.status_code == 201
        adapter.explain.assert_awaited_once()
        db.add.assert_called_once()

    async def test_a_failed_disconnect_keeps_the_422(self, http_client, caplog):
        conn = _make_conn(privacy_settings={})
        cause = RuntimeError('column "nosuch" does not exist')
        explain = AsyncMock(side_effect=ExplainError(str(cause)))
        explain.side_effect.__cause__ = cause
        adapter = _verifying_adapter(explain)
        adapter.disconnect = AsyncMock(side_effect=RuntimeError("pool already closed"))
        app.dependency_overrides[get_db] = lambda: self._db(conn)
        with (
            patch("app.routers.connections.create_datasource", return_value=adapter),
            caplog.at_level("WARNING", logger="app.routers.connections"),
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={"row_filters": {"public.orders": "nosuch = 1"}},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"].startswith('row_filters["public.orders"]: rejected')
        assert any("verify row filters" in r.getMessage() for r in caplog.records)
        adapter.disconnect.assert_awaited_once()

    async def test_a_failed_disconnect_keeps_the_200(self, http_client):
        conn = _make_conn(privacy_settings={})
        adapter = _verifying_adapter()
        adapter.disconnect = AsyncMock(side_effect=RuntimeError("pool already closed"))
        app.dependency_overrides[get_db] = lambda: self._db(conn)
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={"row_filters": {"public.orders": "tenant_id = 42"}},
            )
        assert resp.status_code == 200
        adapter.disconnect.assert_awaited_once()
