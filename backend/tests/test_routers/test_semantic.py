# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for /api/v1/connections/{id}/semantic endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.database import get_db
from app.main import app

from .conftest import MockResult, _make_conn, _mock_db

# Minimal dict that validates as SemanticModelResponse
_EMPTY_MODEL = {
    "tables": {},
    "business_metrics": [],
    "common_joins": [],
    "is_user_reviewed": False,
}


# ── GET /api/v1/connections/{id}/semantic ───────────────────────────────────────


class TestGetSemanticModel:
    async def test_returns_200_when_model_exists(self, http_client):
        conn = _make_conn(semantic_model=_EMPTY_MODEL)
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/conn-123/semantic")
        assert resp.status_code == 200

    async def test_response_has_tables_key(self, http_client):
        conn = _make_conn(semantic_model=_EMPTY_MODEL)
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/conn-123/semantic")
        assert "tables" in resp.json()

    async def test_returns_404_when_no_model(self, http_client):
        conn = _make_conn(semantic_model=None)
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/conn-123/semantic")
        assert resp.status_code == 404

    async def test_returns_404_when_connection_missing(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/missing/semantic")
        assert resp.status_code == 404


# ── PUT /api/v1/connections/{id}/semantic ───────────────────────────────────────


class TestUpdateSemanticModel:
    async def test_returns_200_with_merged_model(self, http_client):
        existing = dict(_EMPTY_MODEL)
        conn = _make_conn(semantic_model=existing)
        db = _mock_db(
            MockResult(single=conn),  # _get_or_404
            MockResult(),  # update Connection
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/connections/conn-123/semantic",
            json={
                "tables": {
                    "public.users": {
                        "display_name": "Users",
                        "description": "User accounts",
                    }
                }
            },
        )
        assert resp.status_code == 200

    async def test_response_tables_include_update(self, http_client):
        conn = _make_conn(semantic_model=dict(_EMPTY_MODEL))
        db = _mock_db(
            MockResult(single=conn),
            MockResult(),  # update Connection
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/connections/conn-123/semantic",
            json={"tables": {"public.orders": {"display_name": "Orders"}}},
        )
        body = resp.json()
        assert "public.orders" in body["tables"]

    async def test_sets_is_user_reviewed(self, http_client):
        conn = _make_conn(semantic_model=dict(_EMPTY_MODEL))
        db = _mock_db(
            MockResult(single=conn),
            MockResult(),  # update Connection
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/connections/conn-123/semantic",
            json={"is_user_reviewed": True},
        )
        assert resp.status_code == 200
        assert resp.json()["is_user_reviewed"] is True

    async def test_not_found_returns_404(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/connections/missing/semantic",
            json={},
        )
        assert resp.status_code == 404

    async def test_invalidates_caches(self, http_client):
        """PUT semantic must invalidate all three cache tables before commit."""
        conn = _make_conn(semantic_model=dict(_EMPTY_MODEL))
        db = _mock_db(
            MockResult(single=conn),  # get_connection_or_404
            MockResult(),  # update Connection
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
        )
        app.dependency_overrides[get_db] = lambda: db
        await http_client.put("/api/v1/connections/conn-123/semantic", json={})
        # 1 select (get_or_404) + 1 update + 3 deletes = 5
        assert db.execute.await_count == 5


# ── DELETE /api/v1/connections/{id}/semantic ────────────────────────────────────


class TestDeleteSemanticModel:
    async def test_returns_204(self, http_client):
        conn = _make_conn(semantic_model=_EMPTY_MODEL)
        db = _mock_db(
            MockResult(single=conn),  # _get_or_404
            MockResult(),  # update Connection (set model to None)
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.delete("/api/v1/connections/conn-123/semantic")
        assert resp.status_code == 204

    async def test_not_found_returns_404(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.delete("/api/v1/connections/missing/semantic")
        assert resp.status_code == 404

    async def test_commit_called(self, http_client):
        conn = _make_conn(semantic_model=_EMPTY_MODEL)
        db = _mock_db(
            MockResult(single=conn),
            MockResult(),  # update Connection
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
        )
        app.dependency_overrides[get_db] = lambda: db
        await http_client.delete("/api/v1/connections/conn-123/semantic")
        db.commit.assert_called_once()

    async def test_invalidates_caches(self, http_client):
        """DELETE semantic must invalidate all three cache tables before commit."""
        conn = _make_conn(semantic_model=_EMPTY_MODEL)
        db = _mock_db(
            MockResult(single=conn),  # get_connection_or_404
            MockResult(),  # update Connection
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
        )
        app.dependency_overrides[get_db] = lambda: db
        await http_client.delete("/api/v1/connections/conn-123/semantic")
        # 1 select (get_or_404) + 1 update + 3 deletes = 5
        assert db.execute.await_count == 5


# ── GET /api/v1/connections/{id}/semantic/drift ─────────────────────────────────


class TestCheckDrift:
    async def test_returns_200_no_warnings(self, http_client):
        conn = _make_conn(semantic_model=_EMPTY_MODEL)
        usc = MagicMock()
        usc.schema_cache = {"tables": {}}
        db = _mock_db(
            MockResult(single=conn),  # get_connection_or_404
            MockResult(single=usc),  # user schema cache lookup
        )
        app.dependency_overrides[get_db] = lambda: db
        with (
            patch("app.routers.semantic._schema_from_dict", return_value=MagicMock()),
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
        ):
            mock_gen.return_value.detect_drift = MagicMock(return_value=[])
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 200
        assert resp.json()["warnings"] == []

    async def test_returns_404_no_semantic_model(self, http_client):
        conn = _make_conn(semantic_model=None)
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 404

    async def test_returns_404_no_schema_cache(self, http_client):
        conn = _make_conn(semantic_model=_EMPTY_MODEL)
        db = _mock_db(
            MockResult(single=conn),  # get_connection_or_404
            MockResult(single=None),  # no user schema cache for this user
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 404


# ── Admin guard (403 for non-admin) ─────────────────────────────────────────────
