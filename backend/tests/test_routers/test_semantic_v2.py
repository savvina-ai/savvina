# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for Stage C semantic v2 router endpoints: drift, suggestions, apply."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from app.database import get_db
from app.main import app
from app.semantic import store as semantic_store
from app.semantic.models import SemanticModel

from .conftest import MockResult, _make_conn, _mock_db

# ── Shared fixtures ────────────────────────────────────────────────────────────

_STORE = "app.routers.semantic.semantic_store"

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


def _populated() -> SemanticModel:
    return SemanticModel.model_validate(_POPULATED_MODEL)


# ── GET /api/v1/connections/{id}/semantic/drift ──────────────────────────────────


class TestCheckDrift:
    async def test_no_semantic_model_returns_404(self, http_client) -> None:
        conn = _make_conn()
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        with patch(f"{_STORE}.load_model", new=AsyncMock(return_value=None)):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 404

    async def test_no_schema_cache_returns_404(self, http_client) -> None:
        conn = _make_conn()
        db = _mock_db(MockResult(single=conn), MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        with patch(f"{_STORE}.load_model", new=AsyncMock(return_value=_populated())):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 404

    async def test_connection_not_found_returns_404(self, http_client) -> None:
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/missing/semantic/drift")
        assert resp.status_code == 404

    async def test_clean_schema_returns_empty_warnings(self, http_client) -> None:
        conn = _make_conn()
        _usc = MagicMock()
        _usc.schema_cache = _SCHEMA_CACHE
        db = _mock_db(MockResult(single=conn), MockResult(single=_usc))
        app.dependency_overrides[get_db] = lambda: db

        with (
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=_populated())),
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
        conn = _make_conn()
        _usc = MagicMock()
        _usc.schema_cache = _SCHEMA_CACHE
        db = _mock_db(MockResult(single=conn), MockResult(single=_usc))
        app.dependency_overrides[get_db] = lambda: db

        warning = 'Table "public.deleted_table" referenced in semantic model no longer exists'
        with (
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=_populated())),
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
        conn = _make_conn()
        _usc = MagicMock()
        _usc.schema_cache = _SCHEMA_CACHE
        db = _mock_db(MockResult(single=conn), MockResult(single=_usc))
        app.dependency_overrides[get_db] = lambda: db

        with (
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=_populated())),
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
    @staticmethod
    def _apply_db(conn, sug) -> MagicMock:
        return _mock_db(
            MockResult(single=conn),  # lock_and_reread_connection (404s a missing one)
            MockResult(single=sug),  # select suggestion by id, under the lock
            MockResult(),  # update SemanticSuggestion
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
        )

    async def _post_apply(self, http_client, db, *, current=None, apply=None):
        """POST the apply route with the store patched; returns (response, apply mock)."""
        app.dependency_overrides[get_db] = lambda: db
        current = current if current is not None else _populated()
        apply = apply if apply is not None else AsyncMock(return_value=current)
        with (
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=current)),
            patch(f"{_STORE}.apply_user_patch", new=apply),
        ):
            resp = await http_client.post(
                "/api/v1/connections/conn-123/semantic/suggestions/sug-001/apply"
            )
        return resp, apply

    async def test_apply_add_value_mapping(self, http_client) -> None:
        sug = _make_suggestion(
            id="sug-001",
            connection_id="conn-123",
            table_key="public.orders",
            field="status",
            correction_type="add_value_mapping",
            value={"raw_value": "P", "display_value": "Pending"},
        )
        db = self._apply_db(_make_conn(), sug)
        resp, apply = await self._post_apply(http_client, db)
        assert resp.status_code == 200
        built = apply.await_args.args[2]
        mappings = built.tables["public.orders"].columns["status"].value_mappings
        assert [(m.raw_value, m.display_value, m.description) for m in mappings] == [
            ("P", "Pending", None)
        ]
        assert apply.await_args.kwargs["updated_by"]

    async def test_apply_add_value_mapping_keeps_existing_and_skips_duplicates(
        self, http_client
    ) -> None:
        model = {
            **_POPULATED_MODEL,
            "tables": {
                "public.orders": {
                    **_POPULATED_MODEL["tables"]["public.orders"],
                    "columns": {
                        "status": {
                            "display_name": "Status",
                            "value_mappings": [
                                {"raw_value": "A", "display_value": "Active", "description": "d"}
                            ],
                        }
                    },
                }
            },
        }
        current = SemanticModel.model_validate(model)
        sug = _make_suggestion(
            correction_type="add_value_mapping",
            value={"raw_value": "A", "display_value": "Other"},
        )
        db = self._apply_db(_make_conn(), sug)
        resp, apply = await self._post_apply(http_client, db, current=current)
        assert resp.status_code == 200
        built = apply.await_args.args[2]
        mappings = built.tables["public.orders"].columns["status"].value_mappings
        assert [(m.raw_value, m.display_value, m.description) for m in mappings] == [
            ("A", "Active", "d")
        ]

    async def test_apply_malformed_value_mapping_is_400(self, http_client) -> None:
        sug = _make_suggestion(
            correction_type="add_value_mapping",
            value={"raw_value": "P"},  # no display_value
        )
        db = self._apply_db(_make_conn(), sug)
        resp, apply = await self._post_apply(http_client, db)
        assert resp.status_code == 400
        apply.assert_not_awaited()
        db.commit.assert_not_awaited()

    async def test_apply_value_mapping_on_unknown_column_is_404(self, http_client) -> None:
        sug = _make_suggestion(
            field="ghost",
            correction_type="add_value_mapping",
            value={"raw_value": "P", "display_value": "Pending"},
        )
        db = self._apply_db(_make_conn(), sug)
        resp, apply = await self._post_apply(http_client, db)
        assert resp.status_code == 404
        assert "ghost" in resp.json()["detail"]
        apply.assert_not_awaited()
        db.commit.assert_not_awaited()

    async def test_apply_column_description_on_unknown_column_is_404(self, http_client) -> None:
        sug = _make_suggestion(
            field="ghost",
            correction_type="update_description",
            value={"target": "column", "description": "boo"},
        )
        db = self._apply_db(_make_conn(), sug)
        resp, apply = await self._post_apply(http_client, db)
        assert resp.status_code == 404
        apply.assert_not_awaited()

    async def test_apply_table_description_ignores_unknown_field(self, http_client) -> None:
        sug = _make_suggestion(
            field="ghost",
            correction_type="update_description",
            value={"target": "table", "description": "fine"},
        )
        db = self._apply_db(_make_conn(), sug)
        resp, apply = await self._post_apply(http_client, db)
        assert resp.status_code == 200
        apply.assert_awaited_once()

    async def test_apply_wrong_connection_returns_404(self, http_client) -> None:
        conn = _make_conn(id="conn-123")
        # The suggestion query filters by connection_id in the WHERE clause;
        # when it doesn't match the DB returns None → 404 (no existence leak via 403).
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

    async def test_apply_table_missing_from_model_returns_404(self, http_client) -> None:
        sug = _make_suggestion(table_key="public.orders")
        db = self._apply_db(_make_conn(), sug)
        current = SemanticModel.model_validate(_EMPTY_MODEL)
        resp, apply = await self._post_apply(http_client, db, current=current)
        assert resp.status_code == 404
        apply.assert_not_awaited()

    async def test_apply_no_semantic_model_returns_404(self, http_client) -> None:
        sug = _make_suggestion()
        db = self._apply_db(_make_conn(), sug)
        app.dependency_overrides[get_db] = lambda: db
        apply = AsyncMock()
        with (
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=None)),
            patch(f"{_STORE}.apply_user_patch", new=apply),
        ):
            resp = await http_client.post(
                "/api/v1/connections/conn-123/semantic/suggestions/sug-001/apply"
            )
        assert resp.status_code == 404
        apply.assert_not_awaited()

    async def test_apply_unknown_correction_type_is_400(self, http_client) -> None:
        sug = _make_suggestion(correction_type="rename_everything")
        db = self._apply_db(_make_conn(), sug)
        resp, apply = await self._post_apply(http_client, db)
        assert resp.status_code == 400
        apply.assert_not_awaited()

    async def test_apply_update_filter(self, http_client) -> None:
        sug = _make_suggestion(
            correction_type="update_filter",
            value={"filter": "status != 'deleted'"},
        )
        db = self._apply_db(_make_conn(), sug)
        resp, apply = await self._post_apply(http_client, db)
        assert resp.status_code == 200
        built = apply.await_args.args[2]
        assert built.tables["public.orders"].default_filters == ["status != 'deleted'"]

    async def test_apply_update_description_table(self, http_client) -> None:
        sug = _make_suggestion(
            correction_type="update_description",
            value={"target": "table", "description": "Updated description"},
        )
        db = self._apply_db(_make_conn(), sug)
        resp, apply = await self._post_apply(http_client, db)
        assert resp.status_code == 200
        built = apply.await_args.args[2]
        assert built.tables["public.orders"].description == "Updated description"

    async def test_apply_update_description_column(self, http_client) -> None:
        sug = _make_suggestion(
            field="status",
            correction_type="update_description",
            value={"target": "column", "description": "Order state"},
        )
        db = self._apply_db(_make_conn(), sug)
        resp, apply = await self._post_apply(http_client, db)
        assert resp.status_code == 200
        built = apply.await_args.args[2]
        assert built.tables["public.orders"].columns["status"].description == "Order state"

    async def test_store_conflict_is_409_and_rolls_back(self, http_client) -> None:
        sug = _make_suggestion()
        db = self._apply_db(_make_conn(), sug)
        apply = AsyncMock(side_effect=semantic_store.SemanticStoreConflict("moved on"))
        resp, _ = await self._post_apply(http_client, db, apply=apply)
        assert resp.status_code == 409
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_store_value_error_is_400_and_rolls_back(self, http_client) -> None:
        sug = _make_suggestion()
        db = self._apply_db(_make_conn(), sug)
        apply = AsyncMock(side_effect=ValueError("column name too long"))
        resp, _ = await self._post_apply(http_client, db, apply=apply)
        assert resp.status_code == 400
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_commit_called_on_apply(self, http_client) -> None:
        sug = _make_suggestion(connection_id="conn-123")
        db = self._apply_db(_make_conn(), sug)
        await self._post_apply(http_client, db)
        db.commit.assert_called_once()

    async def test_invalidates_caches_on_apply(self, http_client) -> None:
        """POST apply must invalidate all three cache tables before commit."""
        sug = _make_suggestion(connection_id="conn-123")
        db = self._apply_db(_make_conn(), sug)
        await self._post_apply(http_client, db)
        # 1 select (lock_and_reread) + 1 select (suggestion) + 1 update (suggestion)
        # + 3 deletes = 6; the store write is patched out.
        assert db.execute.await_count == 6

    async def test_an_applied_suggestion_is_409_and_nothing_is_written(self, http_client) -> None:
        sug = _make_suggestion(is_applied=True)
        db = self._apply_db(_make_conn(), sug)
        resp, apply = await self._post_apply(http_client, db)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Suggestion already applied"
        apply.assert_not_awaited()
        db.commit.assert_not_awaited()
        assert db.execute.await_count == 2  # the lock and the suggestion read only

    async def test_the_suggestion_is_read_under_the_lock(self, http_client) -> None:
        """Read before the lock, a concurrent apply that commits in between would go
        unseen and the suggestion would be applied twice."""
        sug = _make_suggestion()
        db = self._apply_db(_make_conn(), sug)
        await self._post_apply(http_client, db)
        lock_stmt, sug_stmt = (c.args[0] for c in db.execute.await_args_list[:2])
        assert lock_stmt._for_update_arg is not None
        assert sug_stmt.get_execution_options().get("populate_existing") is True
