# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for Stage C semantic v2 router endpoints: drift, suggestions, apply."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from app.database import get_db
from app.main import app

from .conftest import MockResult, _make_conn, _mock_db

# ── Shared fixtures ────────────────────────────────────────────────────────────

_EMPTY_MODEL = {
    "tables": {},
    "business_metrics": [],
    "common_joins": [],
    "is_user_reviewed": False,
}

_POPULATED_MODEL = {
    "tables": {
        "public.orders": {
            "display_name": "Orders",
            "description": "Sales orders",
            "default_filters": [],
            "columns": {
                "status": {
                    "display_name": "Status",
                    "description": None,
                    "value_mappings": [],
                    "is_sensitive": False,
                }
            },
        }
    },
    "business_metrics": [],
    "common_joins": [],
    "is_user_reviewed": False,
}

_SCHEMA_CACHE = {
    "source_type": "postgresql",
    "tables": [
        {
            "catalog": None,
            "schema_name": "public",
            "name": "orders",
            "table_type": "table",
            "columns": [
                {
                    "name": "id",
                    "data_type": "integer",
                    "native_type": "int4",
                    "nullable": False,
                    "is_primary_key": True,
                    "is_partition_key": False,
                    "description": None,
                    "sample_values": None,
                }
            ],
            "row_count_approx": None,
            "description": None,
        }
    ],
    "schemas": [{"name": "public", "description": None}],
    "relationships": [],
    "metadata": {},
}


def _make_suggestion(**kwargs) -> MagicMock:
    """Return a mock shaped like a SemanticSuggestion ORM row."""
    now = datetime.now(UTC)
    s = MagicMock()
    s.id = kwargs.get("id", "sug-001")
    s.connection_id = kwargs.get("connection_id", "conn-123")
    s.table_key = kwargs.get("table_key", "public.orders")
    s.field = kwargs.get("field", "status")
    s.correction_type = kwargs.get("correction_type", "add_value_mapping")
    s.value = kwargs.get("value", {"raw_value": "X", "display_value": "Unknown"})
    s.is_applied = kwargs.get("is_applied", False)
    s.source_message_id = kwargs.get("source_message_id")
    s.created_at = kwargs.get("created_at", now)
    return s


# ── GET /api/v1/connections/{id}/semantic/drift ──────────────────────────────────


class TestCheckDrift:
    async def test_no_semantic_model_returns_404(self, http_client) -> None:
        conn = _make_conn(semantic_model=None, schema_cache=_SCHEMA_CACHE)
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 404

    async def test_no_schema_cache_returns_404(self, http_client) -> None:
        conn = _make_conn(semantic_model=_POPULATED_MODEL, schema_cache=None)
        db = _mock_db(MockResult(single=conn), MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 404

    async def test_connection_not_found_returns_404(self, http_client) -> None:
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/missing/semantic/drift")
        assert resp.status_code == 404

    async def test_clean_schema_returns_empty_warnings(self, http_client) -> None:
        conn = _make_conn(semantic_model=_POPULATED_MODEL, schema_cache=_SCHEMA_CACHE)
        _usc = MagicMock()
        _usc.schema_cache = _SCHEMA_CACHE
        db = _mock_db(MockResult(single=conn), MockResult(single=_usc))
        app.dependency_overrides[get_db] = lambda: db

        with (
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch("app.routers.semantic._schema_from_dict"),
        ):
            mock_gen.return_value.detect_drift.return_value = []
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")

        assert resp.status_code == 200
        body = resp.json()
        assert body["warnings"] == []
        assert body["warning_count"] == 0
        assert body["connection_id"] == "conn-123"

    async def test_drift_with_removed_table_returns_warnings(self, http_client) -> None:
        conn = _make_conn(semantic_model=_POPULATED_MODEL, schema_cache=_SCHEMA_CACHE)
        _usc = MagicMock()
        _usc.schema_cache = _SCHEMA_CACHE
        db = _mock_db(MockResult(single=conn), MockResult(single=_usc))
        app.dependency_overrides[get_db] = lambda: db

        warning = 'Table "public.deleted_table" referenced in semantic model no longer exists'
        with (
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch("app.routers.semantic._schema_from_dict"),
        ):
            mock_gen.return_value.detect_drift.return_value = [warning]
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")

        assert resp.status_code == 200
        body = resp.json()
        assert body["warning_count"] == 1
        assert body["warnings"][0] == warning

    async def test_drift_response_has_checked_at(self, http_client) -> None:
        conn = _make_conn(semantic_model=_POPULATED_MODEL, schema_cache=_SCHEMA_CACHE)
        _usc = MagicMock()
        _usc.schema_cache = _SCHEMA_CACHE
        db = _mock_db(MockResult(single=conn), MockResult(single=_usc))
        app.dependency_overrides[get_db] = lambda: db

        with (
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch("app.routers.semantic._schema_from_dict"),
        ):
            mock_gen.return_value.detect_drift.return_value = []
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")

        assert "checked_at" in resp.json()


# ── GET /api/v1/connections/{id}/semantic/suggestions ────────────────────────────


class TestListSuggestions:
    async def test_connection_not_found_returns_404(self, http_client) -> None:
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/missing/semantic/suggestions")
        assert resp.status_code == 404

    async def test_empty_list_returns_200(self, http_client) -> None:
        conn = _make_conn()
        db = _mock_db(
            MockResult(single=conn),  # _get_or_404
            MockResult(rows=[]),  # select suggestions
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/conn-123/semantic/suggestions")
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    async def test_returns_pending_only(self, http_client) -> None:
        conn = _make_conn()
        sug = _make_suggestion(is_applied=False)
        db = _mock_db(
            MockResult(single=conn),
            MockResult(rows=[sug]),
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/conn-123/semantic/suggestions")
        assert resp.status_code == 200
        body = resp.json()["items"]
        assert len(body) == 1
        assert body[0]["id"] == "sug-001"
        assert body[0]["is_applied"] is False

    async def test_suggestion_has_expected_fields(self, http_client) -> None:
        conn = _make_conn()
        sug = _make_suggestion(
            correction_type="add_value_mapping",
            value={"raw_value": "A", "display_value": "Active"},
        )
        db = _mock_db(MockResult(single=conn), MockResult(rows=[sug]))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/conn-123/semantic/suggestions")
        item = resp.json()["items"][0]
        assert "table_key" in item
        assert "field" in item
        assert "correction_type" in item
        assert "value" in item


# ── POST /api/v1/connections/{id}/semantic/suggestions/{id}/apply ────────────────


class TestApplySuggestion:
    async def test_apply_add_value_mapping(self, http_client) -> None:
        conn = _make_conn(semantic_model=_POPULATED_MODEL)
        sug = _make_suggestion(
            id="sug-001",
            connection_id="conn-123",
            table_key="public.orders",
            field="status",
            correction_type="add_value_mapping",
            value={"raw_value": "P", "display_value": "Pending"},
        )

        db = _mock_db(
            MockResult(single=conn),  # get_connection_or_404
            MockResult(single=sug),  # select suggestion by id
            MockResult(single=conn),  # lock_and_reread_connection
            MockResult(),  # update Connection
            MockResult(),  # update SemanticSuggestion
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/connections/conn-123/semantic/suggestions/sug-001/apply"
        )
        assert resp.status_code == 200
        body = resp.json()
        mappings = body["tables"]["public.orders"]["columns"]["status"]["value_mappings"]
        assert any(m["raw_value"] == "P" for m in mappings)

    async def test_apply_wrong_connection_returns_404(self, http_client) -> None:
        conn = _make_conn(id="conn-123")
        # The suggestion query filters by org_id AND connection_id in the WHERE clause;
        # when those don't match the DB returns None → 404 (no existence leak via 403).
        db = _mock_db(
            MockResult(single=conn),
            MockResult(single=None),
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/connections/conn-123/semantic/suggestions/sug-001/apply"
        )
        assert resp.status_code == 404

    async def test_suggestion_not_found_returns_404(self, http_client) -> None:
        conn = _make_conn()
        db = _mock_db(
            MockResult(single=conn),
            MockResult(single=None),  # suggestion not found
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/connections/conn-123/semantic/suggestions/no-such-id/apply"
        )
        assert resp.status_code == 404

    async def test_connection_not_found_returns_404(self, http_client) -> None:
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/connections/missing/semantic/suggestions/sug-001/apply"
        )
        assert resp.status_code == 404

    async def test_apply_update_filter(self, http_client) -> None:
        conn = _make_conn(semantic_model=_POPULATED_MODEL)
        sug = _make_suggestion(
            correction_type="update_filter",
            value={"filter": "status != 'deleted'"},
        )
        db = _mock_db(
            MockResult(single=conn),  # get_connection_or_404
            MockResult(single=sug),  # select suggestion
            MockResult(single=conn),  # lock_and_reread_connection
            MockResult(),  # update Connection
            MockResult(),  # update SemanticSuggestion
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/connections/conn-123/semantic/suggestions/sug-001/apply"
        )
        assert resp.status_code == 200
        body = resp.json()
        filters = body["tables"]["public.orders"]["default_filters"]
        assert "status != 'deleted'" in filters

    async def test_apply_update_description_table(self, http_client) -> None:
        conn = _make_conn(semantic_model=_POPULATED_MODEL)
        sug = _make_suggestion(
            correction_type="update_description",
            value={"target": "table", "description": "Updated description"},
        )
        db = _mock_db(
            MockResult(single=conn),  # get_connection_or_404
            MockResult(single=sug),  # select suggestion
            MockResult(single=conn),  # lock_and_reread_connection
            MockResult(),  # update Connection
            MockResult(),  # update SemanticSuggestion
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/connections/conn-123/semantic/suggestions/sug-001/apply"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["tables"]["public.orders"]["description"] == "Updated description"

    async def test_commit_called_on_apply(self, http_client) -> None:
        conn = _make_conn(semantic_model=_POPULATED_MODEL)
        sug = _make_suggestion(connection_id="conn-123")
        db = _mock_db(
            MockResult(single=conn),  # get_connection_or_404
            MockResult(single=sug),  # select suggestion
            MockResult(single=conn),  # lock_and_reread_connection
            MockResult(),  # update Connection
            MockResult(),  # update SemanticSuggestion
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
        )
        app.dependency_overrides[get_db] = lambda: db
        await http_client.post("/api/v1/connections/conn-123/semantic/suggestions/sug-001/apply")
        db.commit.assert_called_once()

    async def test_invalidates_caches_on_apply(self, http_client) -> None:
        """POST apply must invalidate all three cache tables before commit."""
        conn = _make_conn(semantic_model=_POPULATED_MODEL)
        sug = _make_suggestion(connection_id="conn-123")
        db = _mock_db(
            MockResult(single=conn),  # get_connection_or_404
            MockResult(single=sug),  # select suggestion
            MockResult(single=conn),  # lock_and_reread_connection
            MockResult(),  # update Connection
            MockResult(),  # update SemanticSuggestion
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
        )
        app.dependency_overrides[get_db] = lambda: db
        await http_client.post("/api/v1/connections/conn-123/semantic/suggestions/sug-001/apply")
        # 1 select (get_or_404) + 1 select (suggestion) + 1 select (lock_and_reread)
        # + 2 updates + 3 deletes = 8
        assert db.execute.await_count == 8
