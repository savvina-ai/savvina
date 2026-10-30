# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for /api/v1/connections endpoints."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from app.config import get_settings
from app.database import get_db
from app.datasources.errors import ErrorCategory, ExplainError
from app.datasources.models import ConnectionResult, PrivilegeFacts, TablePrivilegeFacts
from app.main import app
from app.routers.connections import _audit_privileges, _audit_tables, _verify_row_filters
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


@pytest.fixture
def no_audit():
    """Every create, test, privacy save and config save audits the connection role's
    privileges on the live database; these tests are about the rest of the handler."""
    with patch(
        "app.routers.connections._audit_privileges",
        new=AsyncMock(return_value={"status": "ok", "stub": True}),
    ) as audit:
        yield audit


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


@pytest.mark.usefixtures("no_audit")
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

    async def test_sensitive_columns_are_stored_clean_and_deduplicated(self, http_client):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/connections",
            json={
                "name": "My DB",
                "source_type": "postgresql",
                "config": {"host": "localhost"},
                "privacy_settings": {"sensitive_columns": ["Name", " name ", "", "  ", "notes"]},
            },
        )
        assert resp.status_code == 201
        assert db.add.call_args.args[0].privacy_settings == {"sensitive_columns": ["Name", "notes"]}

    async def test_sensitive_columns_differing_only_in_case_keep_the_first(self, http_client):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/connections",
            json={
                "name": "My DB",
                "source_type": "postgresql",
                "config": {"host": "localhost"},
                "privacy_settings": {"sensitive_columns": ["notes", "Notes"]},
            },
        )
        assert resp.status_code == 201
        assert db.add.call_args.args[0].privacy_settings == {"sensitive_columns": ["notes"]}

    @pytest.mark.parametrize("value", ["name", None, {"a": 1}, ["a", 7], [["a"]], 5])
    async def test_malformed_sensitive_columns_returns_422_and_creates_nothing(
        self, http_client, value
    ):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/connections",
            json={
                "name": "My DB",
                "source_type": "postgresql",
                "config": {"host": "localhost"},
                "privacy_settings": {"sensitive_columns": value},
            },
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == "sensitive_columns must be a list of strings"
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

    async def test_serves_the_stored_privilege_audit(self, http_client):
        stored = {
            "checked_at": "2026-10-29T10:00:00+00:00",
            "role": "savvina",
            "status": "ok",
            "error": None,
            "findings": [
                {
                    "code": "superuser",
                    "tables": [],
                    "functions": [],
                    "remediation": 'ALTER ROLE "savvina" NOSUPERUSER;',
                },
                {"code": "from_the_future", "remediation": "x"},
            ],
            "rls_in_use": False,
            "rls_effective": {"public.orders": False},
        }
        conn = _make_conn(privilege_audit=stored)
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        body = (await http_client.get("/api/v1/connections/conn-123")).json()
        audit = body["privilege_audit"]
        assert audit["status"] == "ok"
        assert audit["role"] == "savvina"
        assert [f["code"] for f in audit["findings"]] == ["superuser"]
        assert audit["findings"][0]["remediation"] == 'ALTER ROLE "savvina" NOSUPERUSER;'
        assert audit["rls_in_use"] is False
        assert audit["rls_effective"] == {"public.orders": False}
        assert audit["checked_at"].startswith("2026-10-29T10:00:00")

    async def test_serves_more_tables_and_reads_an_old_audit_without_it_as_zero(self, http_client):
        stored = {
            "checked_at": "2026-10-29T10:00:00+00:00",
            "role": "savvina",
            "status": "ok",
            "error": None,
            "findings": [
                {"code": "write_privileges", "tables": ["public.t"], "more_tables": 3},
                {"code": "reads_excluded_tables", "tables": ["hr.salaries"]},
            ],
            "rls_in_use": False,
            "rls_effective": {},
        }
        conn = _make_conn(privilege_audit=stored)
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        body = (await http_client.get("/api/v1/connections/conn-123")).json()
        findings = body["privilege_audit"]["findings"]
        assert [f["more_tables"] for f in findings] == [3, 0]

    async def test_a_never_audited_connection_serves_null(self, http_client):
        conn = _make_conn()
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        body = (await http_client.get("/api/v1/connections/conn-123")).json()
        assert body["privilege_audit"] is None

    @pytest.mark.parametrize(
        "stored", ["garbage", {"status": "ok"}, {"checked_at": "x", "status": "ok"}, []]
    )
    async def test_a_malformed_stored_audit_serves_null_not_500(self, http_client, stored):
        conn = _make_conn(privilege_audit=stored)
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/conn-123")
        assert resp.status_code == 200
        assert resp.json()["privilege_audit"] is None

    async def test_an_unavailable_audit_is_served(self, http_client):
        stored = {
            "checked_at": "2026-10-29T10:00:00+00:00",
            "role": "",
            "status": "unavailable",
            "error": "connection refused",
            "findings": [],
            "rls_in_use": False,
            "rls_effective": {},
        }
        conn = _make_conn(privilege_audit=stored)
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/conn-123")
        assert resp.status_code == 200
        audit = resp.json()["privilege_audit"]
        assert audit["status"] == "unavailable"
        assert audit["error"] == "connection refused"
        assert audit["findings"] == []

    @pytest.mark.parametrize("findings", [5, True, "x"])
    async def test_a_non_list_findings_serves_an_audit_with_no_findings(
        self, http_client, findings
    ):
        stored = {
            "checked_at": "2026-10-29T10:00:00+00:00",
            "role": "savvina",
            "status": "ok",
            "error": None,
            "findings": findings,
            "rls_in_use": False,
            "rls_effective": {},
        }
        conn = _make_conn(privilege_audit=stored)
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/conn-123")
        assert resp.status_code == 200
        assert resp.json()["privilege_audit"]["findings"] == []

    async def test_the_list_shape_does_not_carry_it(self, http_client):
        conn = _make_conn(
            privilege_audit={"checked_at": "2026-10-29T10:00:00+00:00", "status": "ok"}
        )
        db = _mock_db(MockResult(rows=[conn]))  # the count goes through db.scalar
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections")
        assert resp.status_code == 200
        assert "privilege_audit" not in resp.json()["items"][0]


# ── POST /api/v1/connections/{id}/test ───────────────────────────────────────────


@pytest.mark.usefixtures("no_audit")
class TestTestExistingConnection:
    async def test_success_returns_200(self, http_client):
        conn = _make_conn()
        db = _mock_db(MockResult(single=conn), MockResult())
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

    async def test_unreadable_stored_config_returns_the_normal_failure(self, http_client, no_audit):
        from cryptography.fernet import InvalidToken

        conn = _make_conn()
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        with (
            patch("app.routers.connections.decrypt_value", side_effect=InvalidToken()),
            patch("app.routers.connections.create_datasource") as create,
        ):
            resp = await http_client.post("/api/v1/connections/conn-123/test")
        assert resp.status_code == 400
        assert resp.json()["detail"] == (
            "The stored connection settings could not be read; edit the connection and enter"
            " them again."
        )
        create.assert_not_called()
        no_audit.assert_not_awaited()
        db.commit.assert_not_called()


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

    async def test_readable_postgresql_config_evicts_the_pool(self, http_client):
        conn = _make_conn(source_type="postgresql")
        db = _mock_db(
            MockResult(single=conn),
            MockResult(rows=[]),
            MockResult(),
            MockResult(),
            MockResult(),
            MockResult(),
            MockResult(),
        )
        app.dependency_overrides[get_db] = lambda: db
        with patch("app.routers.connections._evict_pg_pool", new=AsyncMock()) as evict:
            resp = await http_client.delete("/api/v1/connections/conn-123")
        assert resp.status_code == 204
        evict.assert_awaited_once_with({"host": "h"})

    async def test_unreadable_config_skips_pool_eviction_and_deletes(self, http_client, caplog):
        from cryptography.fernet import InvalidToken

        conn = _make_conn(source_type="postgresql")
        db = _mock_db(
            MockResult(single=conn),
            MockResult(rows=[]),
            MockResult(),
            MockResult(),
            MockResult(),
            MockResult(),
            MockResult(),
        )
        app.dependency_overrides[get_db] = lambda: db
        with (
            patch("app.routers.connections.decrypt_value", side_effect=InvalidToken()),
            patch("app.routers.connections._evict_pg_pool", new=AsyncMock()) as evict,
        ):
            resp = await http_client.delete("/api/v1/connections/conn-123")
        assert resp.status_code == 204
        evict.assert_not_awaited()
        db.commit.assert_awaited_once()
        assert conn.is_active is False
        assert "InvalidToken" in caplog.text


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


@pytest.mark.usefixtures("no_audit")
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

    async def test_privacy_save_dedupes_sensitive_columns_case_insensitively(self, http_client):
        # They match case-insensitively (PrivacySettings.is_column_sensitive), so two
        # spellings are one entry; the first spelling is kept.
        conn = _make_conn(privacy_settings={})
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy",
            json={"sensitive_columns": ["Name", "name", " NAME ", "notes"]},
        )
        assert resp.status_code == 200
        assert self._stored_privacy(db)["sensitive_columns"] == ["Name", "notes"]

    async def test_privacy_save_stores_sensitive_columns_trimmed(self, http_client):
        conn = _make_conn(privacy_settings={"include_sample_values": False})
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy",
            json={"sensitive_columns": [" name ", "notes", "", "  ", "name"]},
        )
        assert resp.status_code == 200
        assert self._stored_privacy(db)["sensitive_columns"] == ["name", "notes"]
        assert resp.json()["privacy_settings"]["sensitive_columns"] == ["name", "notes"]
        # The [SENSITIVE] annotation lives in the formatted schema, so the schema cache goes.
        assert self._touches_schema_cache(db)

    async def test_privacy_save_stores_the_strict_toggle(self, http_client):
        conn = _make_conn(privacy_settings={"include_sample_values": False})
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy", json={"strict_sensitive_columns": True}
        )
        assert resp.status_code == 200
        assert self._stored_privacy(db)["strict_sensitive_columns"] is True
        assert resp.json()["privacy_settings"]["strict_sensitive_columns"] is True
        # The toggle changes no grant, so the stored privilege audit is left alone.
        assert "privilege_audit" not in db.execute.call_args_list[1].args[0].compile().params

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

    async def test_keys_missing_from_storage_sent_at_their_defaults_keep_the_schema_cache(
        self, http_client, no_audit, no_verification
    ):
        # The map changes, so the audit runs: stubbed, or the real helper would try to
        # connect with the stubbed "{}" config.
        # A connection stored before sensitive_columns / strict_sensitive_columns existed:
        # the whole form sends them at their defaults, which changes nothing.
        stored = {"include_sample_values": False, "row_filters": {"public.t": "tenant_id = 42"}}
        conn = _make_conn(privacy_settings=dict(stored))
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy",
            json={
                **stored,
                "sensitive_columns": [],
                "strict_sensitive_columns": False,
                "excluded_tables": [],
                "row_filters": {"public.t": "tenant_id = 43"},
            },
        )
        assert resp.status_code == 200
        assert self._executes(db) == 4
        assert not self._touches_schema_cache(db)

    async def test_a_key_missing_from_storage_set_off_its_default_flushes_the_schema_cache(
        self, http_client, no_verification
    ):
        conn = _make_conn(privacy_settings={"row_filters": {"public.t": "tenant_id = 42"}})
        db = self._db_for_update(conn)
        self._use_db(db)
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy",
            json={"row_filters": {"public.t": "tenant_id = 42"}, "sensitive_columns": ["name"]},
        )
        assert resp.status_code == 200
        assert self._executes(db) == 5
        assert self._touches_schema_cache(db)

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

    async def test_empty_map_clears_when_there_is_no_legacy_filter(
        self, http_client, no_verification
    ):
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
    async def test_clear_flag_removes_the_legacy_filter(self, http_client, body, no_verification):
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


@pytest.mark.usefixtures("no_audit")
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


@pytest.mark.usefixtures("no_audit")
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

    async def test_get_config_unreadable_returns_422(self, http_client) -> None:
        from cryptography.fernet import InvalidToken

        conn = _make_conn()
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        with patch("app.routers.connections.decrypt_value", side_effect=InvalidToken()):
            resp = await http_client.get("/api/v1/connections/conn-123/config")
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            "The stored connection settings could not be read; enter every field again."
        )

    async def test_put_full_config_saves_without_reading_the_old_one(self, http_client) -> None:
        from cryptography.fernet import InvalidToken

        conn = _make_conn(source_type="mysql")
        db = _mock_db(
            MockResult(single=conn),
            MockResult(),  # update Connection
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete TableEmbeddingCache
        )
        db.refresh = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db
        captured: dict = {}

        def fake_encrypt(data: str, key: bytes) -> bytes:
            captured.update(json.loads(data))
            return b"encrypted"

        with (
            patch("app.routers.connections.decrypt_value", side_effect=InvalidToken()) as dec,
            patch("app.routers.connections.encrypt_value", side_effect=fake_encrypt),
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/config",
                json={"config": {"host": "new-host", "password": "new-pass"}},
            )
        assert resp.status_code == 200
        dec.assert_not_called()
        assert captured == {"host": "new-host", "password": "new-pass"}

    async def test_put_sentinel_on_postgresql_evicts_the_old_unmasked_config(
        self, http_client
    ) -> None:
        conn = _make_conn(source_type="postgresql")
        db = _mock_db(
            MockResult(single=conn), MockResult(), MockResult(), MockResult(), MockResult()
        )
        db.refresh = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db
        old = {"host": "old-host", "password": "old-pass"}
        with (
            patch("app.routers.connections.decrypt_value", return_value=json.dumps(old)),
            patch("app.routers.connections.encrypt_value", return_value=b"enc"),
            patch("app.routers.connections._evict_pg_pool", new=AsyncMock()) as evict,
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/config",
                json={"config": {"host": "new-host", "password": "**redacted**"}},
            )
        assert resp.status_code == 200
        evict.assert_awaited_once_with(old)

    async def test_put_full_config_on_postgresql_evicts_the_old_pool(self, http_client) -> None:
        conn = _make_conn(source_type="postgresql")
        db = _mock_db(
            MockResult(single=conn), MockResult(), MockResult(), MockResult(), MockResult()
        )
        db.refresh = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db
        old = {"host": "old-host", "password": "old-pass"}
        with (
            patch("app.routers.connections.decrypt_value", return_value=json.dumps(old)),
            patch("app.routers.connections.encrypt_value", return_value=b"enc"),
            patch("app.routers.connections._evict_pg_pool", new=AsyncMock()) as evict,
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/config",
                json={"config": {"host": "new-host", "password": "new-pass"}},
            )
        assert resp.status_code == 200
        evict.assert_awaited_once_with(old)

    async def test_put_full_config_on_unreadable_postgresql_skips_pool_eviction(
        self, http_client
    ) -> None:
        from cryptography.fernet import InvalidToken

        conn = _make_conn(source_type="postgresql")
        db = _mock_db(
            MockResult(single=conn), MockResult(), MockResult(), MockResult(), MockResult()
        )
        db.refresh = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db
        with (
            patch("app.routers.connections.decrypt_value", side_effect=InvalidToken()),
            patch("app.routers.connections.encrypt_value", return_value=b"enc"),
            patch("app.routers.connections._evict_pg_pool", new=AsyncMock()) as evict,
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/config",
                json={"config": {"host": "new-host", "password": "new-pass"}},
            )
        assert resp.status_code == 200
        evict.assert_not_awaited()

    async def test_put_sentinel_with_unreadable_old_config_returns_422(self, http_client) -> None:
        from cryptography.fernet import InvalidToken

        conn = _make_conn()
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        with patch("app.routers.connections.decrypt_value", side_effect=InvalidToken()):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/config",
                json={"config": {"host": "new-host", "password": "**redacted**"}},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            "The stored connection settings could not be read; enter every field again."
        )
        db.commit.assert_not_called()
        assert db.execute.call_count == 1


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


@pytest.mark.usefixtures("no_audit")
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

    async def test_an_unreadable_config_is_422_and_opens_nothing(self, http_client):
        from cryptography.fernet import InvalidToken

        conn = _make_conn(privacy_settings={})
        db = self._db(conn)
        TestUpdatePrivacy._use_db(db)
        with (
            patch("app.routers.connections.decrypt_value", side_effect=InvalidToken),
            patch("app.routers.connections.create_datasource") as create,
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={"row_filters": {"public.orders": "tenant_id = 1"}},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            "row filters could not be verified against the database: the connection settings"
            " could not be read"
        )
        create.assert_not_called()
        # Only the locked lookup ran: no UPDATE, no cache flush, no commit.
        assert TestUpdatePrivacy._executes(db) == 1
        db.commit.assert_not_called()

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


def _auditing_adapter(facts: PrivilegeFacts | None = None, *, connect=None) -> MagicMock:
    adapter = MagicMock()
    adapter.connect = connect or AsyncMock(
        return_value=ConnectionResult(success=True, message="ok")
    )
    adapter.disconnect = AsyncMock()
    adapter.audit_privileges = AsyncMock(return_value=facts or PrivilegeFacts(role="savvina"))
    adapter.classify_error = MagicMock(return_value=ErrorCategory.OTHER)
    adapter.describe_connection_error = MagicMock(return_value=None)
    return adapter


class TestAuditTables:
    def test_row_filter_keys_split_at_the_first_dot(self):
        assert _audit_tables({"row_filters": {"public.orders": "x", "s.a.b": "y"}}) == [
            ("public", "orders"),
            ("s", "a.b"),
        ]

    def test_excluded_entries_as_is_table_excluded_reads_them(self):
        privacy = {"excluded_tables": ["public.Audit_Log", "sessions", "", 7, "a.b.c"]}
        assert _audit_tables(privacy) == [
            ("public", "Audit_Log"),
            (None, "sessions"),
            ("a", "b.c"),
        ]

    def test_duplicates_read_once_and_excluded_schemas_ignored(self):
        privacy = {
            "row_filters": {"public.orders": "x"},
            "excluded_tables": ["public.orders"],
            "excluded_schemas": ["hr"],
        }
        assert _audit_tables(privacy) == [("public", "orders")]

    def test_empty(self):
        assert _audit_tables({}) == []
        assert _audit_tables(None) == []

    def test_a_row_filter_key_without_a_dot_is_skipped(self):
        assert _audit_tables({"row_filters": {"orders": "x"}}) == []


class TestAuditPrivilegesHelper:
    async def test_audits_the_tables_under_the_budget_and_disconnects(self):
        facts = PrivilegeFacts(
            role="savvina",
            is_superuser=True,
            tables=[TablePrivilegeFacts("public", "orders", True, can_select=True)],
        )
        adapter = _auditing_adapter(facts)
        privacy = {"row_filters": {"public.orders": "tenant_id = 1"}, "excluded_tables": ["log"]}
        with patch("app.routers.connections.create_datasource", return_value=adapter) as create:
            stored = await _audit_privileges(
                "postgresql", {"host": "db", "database": "app"}, privacy
            )
        create.assert_called_once_with("postgresql")
        adapter.connect.assert_awaited_once_with({"host": "db", "database": "app"})
        adapter.audit_privileges.assert_awaited_once_with(
            [("public", "orders"), (None, "log")], timeout=get_settings().default_query_timeout
        )
        adapter.disconnect.assert_awaited_once()
        assert stored["status"] == "ok"
        assert stored["role"] == "savvina"
        assert [f["code"] for f in stored["findings"]] == ["superuser"]
        assert stored["rls_effective"] == {"public.orders": False}

    async def test_a_refused_connection_is_described_by_the_adapter_before_redaction(self):
        message = "(1045, \"Access denied for user 'u'@'h'\")"
        adapter = MagicMock()
        adapter.connect = AsyncMock(return_value=ConnectionResult(success=False, message=message))
        adapter.describe_connection_error = MagicMock(
            return_value="the server refused the user name or password"
        )
        adapter.disconnect = AsyncMock()
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            stored = await _audit_privileges("mysql", {"database": "shop"}, None)
        assert stored["status"] == "unavailable"
        assert stored["error"] == "the server refused the user name or password"
        adapter.describe_connection_error.assert_called_once_with(message)

    async def test_a_driver_exception_is_redacted_when_the_adapter_has_no_sentence(self):
        adapter = MagicMock()
        adapter.connect = AsyncMock(return_value=None)
        adapter.audit_privileges = AsyncMock(
            side_effect=RuntimeError("(1142, \"SELECT command denied to user 'u'@'h'\")")
        )
        adapter.classify_error = MagicMock(return_value=ErrorCategory.OTHER)
        adapter.describe_connection_error = MagicMock(return_value=None)
        adapter.disconnect = AsyncMock()
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            stored = await _audit_privileges("mysql", {}, None)
        assert stored["error"] == "SELECT command denied to user [REDACTED]@[REDACTED]"

    async def test_the_database_is_passed_to_evaluate_for_every_engine(self):
        adapter = MagicMock()
        adapter.connect = AsyncMock(return_value=None)
        adapter.audit_privileges = AsyncMock(
            return_value=PrivilegeFacts(role="r", is_superuser=True, schemas=["public"])
        )
        adapter.disconnect = AsyncMock()
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            stored = await _audit_privileges("postgresql", {"database": "app"}, None)
        remediation = stored["findings"][0]["remediation"]
        assert 'GRANT CONNECT ON DATABASE "app" TO savvina_reader;' in remediation

    async def test_mysql_passes_the_database_to_the_superuser_remediation(self):
        adapter = _auditing_adapter(PrivilegeFacts(role="root@localhost", is_superuser=True))
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            stored = await _audit_privileges("mysql", {"database": "shop"}, None)
        assert stored["findings"][0]["remediation"].splitlines() == [
            "-- Create a read-only account for Savvina and point the connection at it.",
            "CREATE USER `savvina_reader`@`%` IDENTIFIED BY '<choose a password>';",
            "GRANT SELECT ON `shop`.* TO `savvina_reader`@`%`;",
        ]

    async def test_a_refused_connection_is_unavailable(self):
        adapter = _auditing_adapter(
            connect=AsyncMock(
                return_value=ConnectionResult(
                    success=False, message="password authentication failed for user 'x'"
                )
            )
        )
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            stored = await _audit_privileges("postgresql", {}, None)
        assert stored["status"] == "unavailable"
        assert stored["role"] == ""
        assert stored["findings"] == [] and stored["rls_effective"] == {}
        assert "[REDACTED]" in stored["error"] and "'x'" not in stored["error"]
        adapter.audit_privileges.assert_not_awaited()
        adapter.disconnect.assert_awaited_once()

    async def test_a_catalog_exception_is_unavailable_with_the_text_redacted(self):
        adapter = _auditing_adapter()
        adapter.audit_privileges = AsyncMock(
            side_effect=RuntimeError("relation 'secret_table' does not exist")
        )
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            stored = await _audit_privileges("postgresql", {}, None)
        assert stored["status"] == "unavailable"
        assert stored["error"] == "relation [REDACTED] does not exist"
        adapter.classify_error.assert_called_once()
        adapter.disconnect.assert_awaited_once()

    async def test_the_budget_timeout_is_unavailable(self):
        adapter = _auditing_adapter()

        async def slow(*_a, **_k):
            await asyncio.sleep(60)

        adapter.audit_privileges = AsyncMock(side_effect=slow)
        with (
            patch("app.routers.connections.create_datasource", return_value=adapter),
            patch("app.routers.connections._explain_budget", return_value=0.01),
        ):
            stored = await _audit_privileges("postgresql", {}, None)
        assert stored["status"] == "unavailable"
        assert stored["error"] == "the privilege check did not finish within 0.01 seconds"
        adapter.disconnect.assert_awaited_once()

    async def test_an_unknown_source_type_is_unavailable(self):
        with patch(
            "app.routers.connections.create_datasource",
            side_effect=ValueError("Unknown source_type 'x'"),
        ):
            stored = await _audit_privileges("x", {}, None)
        assert stored["status"] == "unavailable"
        assert stored["error"] == "Unknown source_type [REDACTED]"

    async def test_a_failing_disconnect_does_not_change_the_verdict(self):
        adapter = _auditing_adapter()
        adapter.disconnect = AsyncMock(side_effect=RuntimeError("pool closed"))
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            stored = await _audit_privileges("postgresql", {}, None)
        assert stored["status"] == "ok"

    async def test_a_cancelled_audit_propagates_and_disconnects(self):
        adapter = _auditing_adapter()
        adapter.audit_privileges = AsyncMock(side_effect=asyncio.CancelledError())
        with (
            patch("app.routers.connections.create_datasource", return_value=adapter),
            pytest.raises(asyncio.CancelledError),
        ):
            await _audit_privileges("postgresql", {}, None)
        adapter.disconnect.assert_awaited_once()

    async def test_an_evaluate_failure_after_the_facts_is_unavailable(self):
        adapter = _auditing_adapter()
        with (
            patch("app.routers.connections.create_datasource", return_value=adapter),
            patch(
                "app.routers.connections.evaluate_privileges",
                side_effect=RuntimeError("boom"),
            ),
        ):
            stored = await _audit_privileges("postgresql", {}, None)
        assert stored["status"] == "unavailable"
        assert stored["error"] == "boom"
        adapter.disconnect.assert_awaited_once()


# ── The four call sites of the privilege audit ───────────────────────────────────


@pytest.mark.usefixtures("no_audit")
class TestAuditCallSites:
    """The tests assert on ``str(stmt)`` of the UPDATE the handler issued: SQLAlchemy renders
    it as ``UPDATE connections SET privilege_audit=:privilege_audit ...``."""

    async def test_create_stores_the_audit_on_the_new_row(self, http_client, no_audit):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/connections",
            json={
                "name": "My DB",
                "source_type": "postgresql",
                "config": {"host": "localhost", "database": "app"},
                "privacy_settings": {"excluded_tables": ["log"]},
            },
        )
        assert resp.status_code == 201
        no_audit.assert_awaited_once_with(
            "postgresql",
            {"host": "localhost", "database": "app"},
            {"excluded_tables": ["log"]},
            session=ANY,
        )
        row = db.add.call_args.args[0]
        assert row.privilege_audit == {"status": "ok", "stub": True}

    async def test_an_empty_map_sent_to_a_connection_without_one_does_not_audit(
        self, http_client, no_audit, no_verification
    ):
        conn = _make_conn(privacy_settings={"include_sample_values": False})
        db = TestUpdatePrivacy._db_for_update(conn)
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy",
            json={"include_sample_values": False, "row_filters": {}, "excluded_tables": []},
        )
        assert resp.status_code == 200
        no_audit.assert_not_awaited()

    async def test_create_runs_the_audit_after_the_row_filter_verification(
        self, http_client, no_audit, no_verification
    ):
        order: list[str] = []
        no_verification.side_effect = lambda *a, **k: order.append("verify")
        no_audit.side_effect = lambda *a, **k: order.append("audit") or {"status": "ok"}
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
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
        assert order == ["verify", "audit"]

    async def test_a_successful_test_writes_only_the_audit_column(self, http_client, no_audit):
        conn = _make_conn(privacy_settings={"excluded_tables": ["log"]})
        db = _mock_db(MockResult(single=conn), MockResult())
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
        no_audit.assert_awaited_once_with(
            "postgresql", {"host": "localhost"}, {"excluded_tables": ["log"]}
        )
        assert db.execute.call_count == 2
        stmt = db.execute.call_args_list[1].args[0]
        assert str(stmt).startswith("UPDATE connections SET privilege_audit=")
        assert "privacy_settings" not in str(stmt)
        assert "WHERE connections.id" in str(stmt)
        assert stmt.compile().params["privilege_audit"] == {"status": "ok", "stub": True}
        db.commit.assert_awaited_once()
        db.add.assert_not_called()

    async def test_a_failed_test_writes_nothing(self, http_client, no_audit):
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
        no_audit.assert_not_awaited()
        assert db.execute.call_count == 1
        db.commit.assert_not_awaited()

    async def test_privacy_save_that_changes_excluded_tables_stores_the_audit(
        self, http_client, no_audit, no_verification
    ):
        conn = _make_conn(privacy_settings={"excluded_tables": ["a"]})
        db = TestUpdatePrivacy._db_for_update(conn)
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy", json={"excluded_tables": ["a", "b"]}
        )
        assert resp.status_code == 200
        no_audit.assert_awaited_once()
        assert no_audit.await_args.args[0] == "postgresql"
        assert no_audit.await_args.args[2]["excluded_tables"] == ["a", "b"]
        stmt = db.execute.call_args_list[1].args[0]
        assert "privilege_audit" in str(stmt) and "privacy_settings" in str(stmt)
        assert stmt.compile().params["privilege_audit"] == {"status": "ok", "stub": True}

    async def test_privacy_save_that_changes_row_filters_stores_the_audit(
        self, http_client, no_audit, no_verification
    ):
        conn = _make_conn(privacy_settings={})
        db = TestUpdatePrivacy._db_for_update(conn)
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy",
            json={"row_filters": {"public.orders": "tenant_id = 1"}},
        )
        assert resp.status_code == 200
        no_audit.assert_awaited_once()
        assert no_audit.await_args.args[2]["row_filters"] == {"public.orders": "tenant_id = 1"}

    @pytest.mark.parametrize(
        "body",
        [{"include_sample_values": True}, {"excluded_tables": ["a"]}],
    )
    async def test_privacy_save_without_a_table_change_does_not_audit(
        self, http_client, no_audit, no_verification, body
    ):
        conn = _make_conn(privacy_settings={"excluded_tables": ["a"], "row_filters": {}})
        db = TestUpdatePrivacy._db_for_update(conn)
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put("/api/v1/connections/conn-123/privacy", json=body)
        assert resp.status_code == 200
        no_audit.assert_not_awaited()
        assert "privilege_audit" not in str(db.execute.call_args_list[1].args[0])

    async def test_privacy_save_that_changes_excluded_schemas_stores_the_audit(
        self, http_client, no_audit, no_verification
    ):
        conn = _make_conn(privacy_settings={"excluded_schemas": ["a"]})
        db = TestUpdatePrivacy._db_for_update(conn)
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy", json={"excluded_schemas": ["a", "hr"]}
        )
        assert resp.status_code == 200
        no_audit.assert_awaited_once()
        assert no_audit.await_args.args[2]["excluded_schemas"] == ["a", "hr"]
        stmt = db.execute.call_args_list[1].args[0]
        assert stmt.compile().params["privilege_audit"] == {"status": "ok", "stub": True}

    async def test_a_422_on_the_row_filter_leaves_the_stored_audit(
        self, http_client, no_audit, no_verification
    ):
        from fastapi import HTTPException

        no_verification.side_effect = HTTPException(status_code=422, detail="nope")
        conn = _make_conn(privacy_settings={})
        db = TestUpdatePrivacy._db_for_update(conn)
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy",
            json={"row_filters": {"public.orders": "tenant_id = 1"}},
        )
        assert resp.status_code == 422
        no_audit.assert_not_awaited()
        assert db.execute.call_count == 1

    async def test_config_save_audits_the_merged_config(self, http_client, no_audit):
        conn = _make_conn(privacy_settings={"excluded_tables": ["a"]})
        db = _mock_db(
            MockResult(single=conn), MockResult(), MockResult(), MockResult(), MockResult()
        )
        app.dependency_overrides[get_db] = lambda: db
        with (
            patch(
                "app.routers.connections.decrypt_value",
                return_value='{"host":"h","password":"old"}',
            ),
            patch("app.routers.connections.encrypt_value", return_value=b"enc"),
            patch("app.routers.connections._evict_pg_pool", new=AsyncMock()),
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/config",
                json={"config": {"host": "new", "password": "**redacted**"}},
            )
        assert resp.status_code == 200
        no_audit.assert_awaited_once_with(
            "postgresql", {"host": "new", "password": "old"}, {"excluded_tables": ["a"]}
        )
        stmt = db.execute.call_args_list[1].args[0]
        assert "privilege_audit" in str(stmt) and "config_encrypted" in str(stmt)
        assert stmt.compile().params["privilege_audit"] == {"status": "ok", "stub": True}

    async def test_privacy_audit_gets_the_decrypted_config(
        self, http_client, no_audit, no_verification
    ):
        # no_verification stubs decrypt_value to "{}".
        conn = _make_conn(privacy_settings={"excluded_tables": ["a"]})
        db = TestUpdatePrivacy._db_for_update(conn)
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy", json={"excluded_tables": ["a", "b"]}
        )
        assert resp.status_code == 200
        no_audit.assert_awaited_once()
        assert no_audit.await_args.args[1] == {}

    async def test_clearing_the_legacy_filter_on_a_row_without_row_filters_does_not_audit(
        self, http_client, no_audit, no_verification
    ):
        # A missing row_filters key that becomes {} is no change: it reads as {} either way.
        conn = _make_conn(privacy_settings={"row_filter_sql": "tenant_id = 1"})
        db = TestUpdatePrivacy._db_for_update(conn)
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/connections/conn-123/privacy", json={"clear_legacy_row_filter": True}
        )
        assert resp.status_code == 200
        no_audit.assert_not_awaited()

    async def test_an_undecryptable_config_on_an_excluded_tables_save_stores_unavailable(
        self, http_client, no_audit
    ):
        from cryptography.fernet import InvalidToken

        conn = _make_conn(privacy_settings={"excluded_tables": ["a"]})
        db = TestUpdatePrivacy._db_for_update(conn)
        app.dependency_overrides[get_db] = lambda: db
        with patch("app.routers.connections.decrypt_value", side_effect=InvalidToken()):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy", json={"excluded_tables": ["a", "b"]}
            )
        assert resp.status_code == 200
        no_audit.assert_not_awaited()
        stored = db.execute.call_args_list[1].args[0].compile().params["privilege_audit"]
        assert stored["status"] == "unavailable"
        assert stored["error"] == "the connection settings could not be read"

    async def test_a_config_that_is_not_json_on_an_excluded_tables_save_stores_unavailable(
        self, http_client, no_audit, caplog
    ):
        conn = _make_conn(privacy_settings={"excluded_tables": ["a"]})
        db = TestUpdatePrivacy._db_for_update(conn)
        app.dependency_overrides[get_db] = lambda: db
        with patch("app.routers.connections.decrypt_value", return_value="secret-pw-not-json"):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy", json={"excluded_tables": ["a", "b"]}
            )
        assert resp.status_code == 200
        no_audit.assert_not_awaited()
        stored = db.execute.call_args_list[1].args[0].compile().params["privilege_audit"]
        assert stored["status"] == "unavailable"
        assert "JSONDecodeError" in caplog.text
        assert "secret-pw-not-json" not in caplog.text

    async def test_a_row_filter_save_decrypts_the_config_once(
        self, http_client, no_audit, no_verification
    ):
        conn = _make_conn(privacy_settings={})
        db = TestUpdatePrivacy._db_for_update(conn)
        app.dependency_overrides[get_db] = lambda: db
        with patch("app.routers.connections.decrypt_value", return_value='{"host":"h"}') as dec:
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={"row_filters": {"public.orders": "tenant_id = 1"}},
            )
        assert resp.status_code == 200
        assert dec.call_count == 1
        no_verification.assert_awaited_once()
        no_audit.assert_awaited_once()
        assert no_audit.await_args.args[1] == {"host": "h"}

    async def test_a_name_only_config_save_does_not_audit(self, http_client, no_audit):
        conn = _make_conn()
        db = _mock_db(MockResult(single=conn), MockResult())
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/connections/conn-123/config", json={"name": "Renamed"}
        )
        assert resp.status_code == 200
        no_audit.assert_not_awaited()


class TestAuditEndToEnd:
    """No ``no_audit``: the real ``_audit_privileges`` runs against a mocked adapter."""

    async def test_create_with_a_config_that_does_not_connect_stores_unavailable_and_creates_row(
        self, http_client
    ):
        adapter = _auditing_adapter(
            connect=AsyncMock(
                return_value=ConnectionResult(success=False, message="could not connect to server")
            )
        )
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        with (
            patch("app.routers.connections.create_datasource", return_value=adapter),
            patch("app.routers.connections.encrypt_value", return_value=b"enc"),
        ):
            resp = await http_client.post(
                "/api/v1/connections",
                json={
                    "name": "My DB",
                    "source_type": "postgresql",
                    "config": {"host": "nowhere", "database": "app"},
                },
            )
        assert resp.status_code == 201
        row = db.add.call_args.args[0]
        assert row.privilege_audit["status"] == "unavailable"
        adapter.audit_privileges.assert_not_awaited()

    async def test_a_catalog_exception_on_test_stores_unavailable_and_returns_200(
        self, http_client
    ):
        conn = _make_conn()
        db = _mock_db(MockResult(single=conn), MockResult())
        app.dependency_overrides[get_db] = lambda: db
        adapter = _auditing_adapter()
        adapter.test_connection = AsyncMock(
            return_value=ConnectionResult(success=True, message="Connected")
        )
        adapter.audit_privileges = AsyncMock(
            side_effect=RuntimeError("permission denied for table pg_roles")
        )
        with (
            patch("app.routers.connections.decrypt_value", return_value='{"host":"localhost"}'),
            patch("app.routers.connections.create_datasource", return_value=adapter),
        ):
            resp = await http_client.post("/api/v1/connections/conn-123/test")
        assert resp.status_code == 200
        stmt = db.execute.call_args_list[1].args[0]
        assert stmt.compile().params["privilege_audit"]["status"] == "unavailable"
        db.commit.assert_awaited_once()


class TestOneConnectionPerSave:
    @staticmethod
    def _adapter() -> MagicMock:
        adapter = _verifying_adapter()
        adapter.audit_privileges = AsyncMock(return_value=PrivilegeFacts(role="savvina"))
        adapter.describe_connection_error = MagicMock(return_value=None)
        return adapter

    async def test_create_with_filters_connects_once_for_the_check_and_the_audit(self, http_client):
        db = _mock_db()
        db.refresh = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db
        adapter = self._adapter()
        with (
            patch("app.routers.connections.create_datasource", return_value=adapter) as create,
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
        create.assert_called_once_with("postgresql")
        adapter.connect.assert_awaited_once()
        adapter.explain.assert_awaited_once()
        adapter.audit_privileges.assert_awaited_once()
        adapter.disconnect.assert_awaited_once()
        assert db.add.call_args.args[0].privilege_audit["status"] == "ok"

    async def test_privacy_save_with_a_new_filter_connects_once(self, http_client):
        conn = _make_conn(privacy_settings={})
        app.dependency_overrides[get_db] = lambda: TestUpdatePrivacy._db_for_update(conn)
        adapter = self._adapter()
        with (
            patch("app.routers.connections.decrypt_value", return_value='{"host": "db"}'),
            patch("app.routers.connections.create_datasource", return_value=adapter) as create,
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={"row_filters": {"public.orders": "tenant_id = 42"}},
            )
        assert resp.status_code == 200
        create.assert_called_once_with("postgresql")
        adapter.connect.assert_awaited_once()
        adapter.explain.assert_awaited_once()
        adapter.audit_privileges.assert_awaited_once()
        adapter.disconnect.assert_awaited_once()

    async def test_a_refused_connection_is_422_and_the_audit_never_runs(self, http_client):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        adapter = self._adapter()
        adapter.connect = AsyncMock(return_value=ConnectionResult(success=False, message="no"))
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            resp = await http_client.post(
                "/api/v1/connections",
                json={
                    "name": "My DB",
                    "source_type": "postgresql",
                    "config": {"host": "localhost"},
                    "privacy_settings": {"row_filters": {"public.orders": "tenant_id = 1"}},
                },
            )
        assert resp.status_code == 422
        adapter.connect.assert_awaited_once()
        adapter.audit_privileges.assert_not_awaited()
        adapter.disconnect.assert_awaited_once()
        db.add.assert_not_called()

    async def test_a_rejected_filter_on_a_privacy_save_never_audits(self, http_client):
        conn = _make_conn(privacy_settings={})
        app.dependency_overrides[get_db] = lambda: TestUpdatePrivacy._db_for_update(conn)
        adapter = self._adapter()
        adapter.explain = AsyncMock(side_effect=ExplainError("x"))
        with (
            patch("app.routers.connections.decrypt_value", return_value='{"host": "db"}'),
            patch("app.routers.connections.create_datasource", return_value=adapter),
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/privacy",
                json={"row_filters": {"public.orders": "tenant_id = 42"}},
            )
        assert resp.status_code == 422
        adapter.audit_privileges.assert_not_awaited()
        adapter.disconnect.assert_awaited_once()

    async def test_the_helpers_still_open_their_own_connection_when_called_alone(self):
        adapter = self._adapter()
        with patch("app.routers.connections.create_datasource", return_value=adapter):
            await _verify_row_filters("postgresql", {"host": "db"}, {"public.t": "x = 1"})
            stored = await _audit_privileges("postgresql", {"host": "db"}, None)
        assert stored["status"] == "ok"
        assert adapter.connect.await_count == 2
        assert adapter.disconnect.await_count == 2
