# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for /api/v1/connections endpoints."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database import get_db
from app.datasources.models import ConnectionResult
from app.main import app

from .conftest import MockResult, _make_conn, _mock_db

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
    def patch_evict(self):
        with (
            patch("app.routers.connections._evict_pg_pool", new=AsyncMock()),
            patch("app.routers.connections.decrypt_value", return_value='{"host": "h"}'),
        ):
            yield

    async def test_success_returns_204(self, http_client):
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
            patch("app.routers.connections.decrypt_value", return_value='{"host":"localhost"}'),
            patch("app.routers.connections.create_datasource", return_value=adapter),
        ):
            resp = await http_client.post("/api/v1/connections/conn-123/schema/refresh")
        assert resp.status_code == 200
        assert db.execute.call_count == 4

    async def test_introspection_error_returns_400(self, http_client):
        conn = _make_conn()
        adapter = MagicMock()
        adapter.connect = AsyncMock()
        adapter.introspect = AsyncMock(side_effect=Exception("timeout"))
        adapter.disconnect = AsyncMock()
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        with (
            patch("app.routers.connections.decrypt_value", return_value='{"host":"localhost"}'),
            patch("app.routers.connections.create_datasource", return_value=adapter),
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
