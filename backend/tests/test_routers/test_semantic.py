# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for /api/v1/connections/{id}/semantic endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.database import get_db
from app.main import app
from app.semantic import store as semantic_store
from app.semantic.models import SemanticModel

from .conftest import MockResult, _make_conn, _mock_db

# Minimal dict that validates as SemanticModelResponse
_EMPTY_MODEL = {
    "tables": {},
    "business_metrics": [],
    "common_joins": [],
    "is_user_reviewed": False,
}

_STORE = "app.routers.semantic.semantic_store"


def _populated() -> SemanticModel:
    return SemanticModel.model_validate(
        {
            **_EMPTY_MODEL,
            "tables": {
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"status": {"display_name": "Status"}},
                }
            },
        }
    )


# ── GET /api/v1/connections/{id}/semantic ───────────────────────────────────────


class TestGetSemanticModel:
    async def test_returns_200_when_model_exists(self, http_client):
        db = _mock_db(MockResult(single=_make_conn()))
        app.dependency_overrides[get_db] = lambda: db
        with patch(f"{_STORE}.load_model", new=AsyncMock(return_value=_populated())):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic")
        assert resp.status_code == 200
        assert "public.orders" in resp.json()["tables"]

    async def test_returns_404_when_no_model(self, http_client):
        db = _mock_db(MockResult(single=_make_conn()))
        app.dependency_overrides[get_db] = lambda: db
        with patch(f"{_STORE}.load_model", new=AsyncMock(return_value=None)):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic")
        assert resp.status_code == 404

    async def test_returns_404_when_connection_missing(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/missing/semantic")
        assert resp.status_code == 404


# ── PUT /api/v1/connections/{id}/semantic ───────────────────────────────────────


class TestUpdateSemanticModel:
    async def test_patch_goes_through_the_store_and_returns_its_model(self, http_client):
        db = _mock_db(
            MockResult(single=_make_conn()),  # lock_and_reread_connection
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
        )
        app.dependency_overrides[get_db] = lambda: db
        apply = AsyncMock(return_value=_populated())
        with patch(f"{_STORE}.apply_user_patch", new=apply):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/semantic",
                json={"tables": {"public.orders": {"description": "hand written"}}},
            )
        assert resp.status_code == 200
        assert "public.orders" in resp.json()["tables"]
        patch_arg = apply.await_args.args[2]
        assert patch_arg.tables["public.orders"].description == "hand written"
        assert apply.await_args.kwargs["updated_by"]
        db.commit.assert_awaited_once()

    async def test_invalidates_caches(self, http_client):
        """PUT semantic must invalidate all three cache tables before commit."""
        db = _mock_db(
            MockResult(single=_make_conn()),  # lock_and_reread_connection
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
        )
        app.dependency_overrides[get_db] = lambda: db
        with patch(f"{_STORE}.apply_user_patch", new=AsyncMock(return_value=_populated())):
            await http_client.put("/api/v1/connections/conn-123/semantic", json={})
        # 1 select (lock_and_reread) + 3 deletes; the store write is patched out.
        assert db.execute.await_count == 4

    async def test_store_conflict_is_409(self, http_client):
        db = _mock_db(MockResult(single=_make_conn()))
        app.dependency_overrides[get_db] = lambda: db
        with patch(
            f"{_STORE}.apply_user_patch",
            new=AsyncMock(side_effect=semantic_store.SemanticStoreConflict("moved on")),
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/semantic", json={"notes": ["x"]}
            )
        assert resp.status_code == 409
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_validation_error_is_400(self, http_client):
        db = _mock_db(MockResult(single=_make_conn()))
        app.dependency_overrides[get_db] = lambda: db
        with patch(
            f"{_STORE}.apply_user_patch", new=AsyncMock(side_effect=ValueError("bad shape"))
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/semantic", json={"notes": ["x"]}
            )
        assert resp.status_code == 400
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_returns_404_when_connection_missing(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put("/api/v1/connections/missing/semantic", json={})
        assert resp.status_code == 404


# ── DELETE /api/v1/connections/{id}/semantic ────────────────────────────────────


class TestDeleteSemanticModel:
    async def test_deletes_through_the_store(self, http_client):
        db = _mock_db(
            MockResult(single=_make_conn()),  # lock_and_reread_connection
            MockResult(),
            MockResult(),
            MockResult(),  # cache deletes
        )
        app.dependency_overrides[get_db] = lambda: db
        delete = AsyncMock()
        with patch(f"{_STORE}.delete_model", new=delete):
            resp = await http_client.delete("/api/v1/connections/conn-123/semantic")
        assert resp.status_code == 204
        delete.assert_awaited_once()
        assert delete.await_args.args[1] == "conn-123"
        assert delete.await_args.kwargs["changed_by"]
        db.commit.assert_awaited_once()

    async def test_invalidates_caches(self, http_client):
        """DELETE semantic must invalidate all three cache tables before commit."""
        db = _mock_db(
            MockResult(single=_make_conn()),  # lock_and_reread_connection
            MockResult(),
            MockResult(),
            MockResult(),  # cache deletes
        )
        app.dependency_overrides[get_db] = lambda: db
        with patch(f"{_STORE}.delete_model", new=AsyncMock()):
            await http_client.delete("/api/v1/connections/conn-123/semantic")
        # 1 select (lock_and_reread) + 3 deletes; the store delete is patched out.
        assert db.execute.await_count == 4

    async def test_returns_404_when_connection_missing(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.delete("/api/v1/connections/missing/semantic")
        assert resp.status_code == 404


# ── GET /api/v1/connections/{id}/semantic/drift ─────────────────────────────────


class TestCheckDrift:
    async def test_returns_200_no_warnings(self, http_client):
        usc = MagicMock()
        usc.schema_cache = {"tables": {}}
        db = _mock_db(
            MockResult(single=_make_conn()),  # get_connection_or_404
            MockResult(single=usc),  # user schema cache lookup
        )
        app.dependency_overrides[get_db] = lambda: db
        with (
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=_populated())),
            patch("app.routers.semantic._schema_from_dict", return_value=MagicMock()),
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
        ):
            mock_gen.return_value.detect_drift = MagicMock(return_value=[])
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 200
        assert resp.json()["warnings"] == []

    async def test_returns_404_no_semantic_model(self, http_client):
        db = _mock_db(MockResult(single=_make_conn()))
        app.dependency_overrides[get_db] = lambda: db
        with patch(f"{_STORE}.load_model", new=AsyncMock(return_value=None)):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 404

    async def test_returns_404_no_schema_cache(self, http_client):
        db = _mock_db(
            MockResult(single=_make_conn()),  # get_connection_or_404
            MockResult(single=None),  # no user schema cache for this user
        )
        app.dependency_overrides[get_db] = lambda: db
        with patch(f"{_STORE}.load_model", new=AsyncMock(return_value=_populated())):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 404


# ── Admin guard (403 for non-admin) ─────────────────────────────────────────────


# ── POST /api/v1/connections/{id}/semantic/generate/init ────────────────────────


class TestPhasedGenerationInit:
    async def test_generate_init_persists_the_partial_model_it_plans(self, http_client) -> None:
        """The response only echoes the batch plan; the *write* is the contract.

        ``/generate/init`` is the one destructive endpoint in this router — it blanks
        ``tables`` — and nothing asserted what it stored, so a change to the persisted
        payload was invisible to this suite.
        """
        conn = _make_conn(semantic_model=None)
        schema = MagicMock()
        schema.tables = []

        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
        ):
            mock_load.return_value = (schema, conn)
            gen = mock_gen.return_value
            gen.prepare_generation.return_value = {
                "tables_total": 7,
                "batch_count": 2,
                "batch_size": 4,
            }
            gen.compute_schema_hash.return_value = "hash-abc"
            db = _mock_db(MockResult())  # update Connection
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post("/api/v1/connections/conn-123/semantic/generate/init")

        assert resp.status_code == 200
        stored = db.execute.await_args.args[0].compile().params["semantic_model"]
        assert stored["tables"] == {}
        assert stored["generation_status"] == "tables_partial"
        assert stored["generation_progress"] == {
            "tables_done": 0,
            "tables_total": 7,
            # _BATCH_SIZE, not prepare_generation's batch_size — the route passes its own.
            "batch_size": 4,
        }
        # 12.5b: the resume precondition. Without it a resume cannot tell that the
        # cached schema was refreshed and reordered underneath it.
        assert stored["schema_hash"] == "hash-abc"

    async def test_generate_init_restamps_a_stale_schema_hash(self, http_client) -> None:
        """Re-initialising must not carry the previous run's hash forward.

        ``model_copy(update=…)`` copies every unlisted field, so before 12.5b a
        regenerate kept the old ``schema_hash`` — worse than the ``None`` a fresh
        connection gets, because a resume would read it as authoritative.
        """
        conn = _make_conn(semantic_model={**_EMPTY_MODEL, "schema_hash": "stale-hash"})
        schema = MagicMock()
        schema.tables = []

        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
        ):
            mock_load.return_value = (schema, conn)
            gen = mock_gen.return_value
            gen.prepare_generation.return_value = {
                "tables_total": 1,
                "batch_count": 1,
                "batch_size": 4,
            }
            gen.compute_schema_hash.return_value = "fresh-hash"
            db = _mock_db(MockResult())  # update Connection
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post("/api/v1/connections/conn-123/semantic/generate/init")

        assert resp.status_code == 200
        stored = db.execute.await_args.args[0].compile().params["semantic_model"]
        assert stored["schema_hash"] == "fresh-hash"
