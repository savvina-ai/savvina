# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Every read that serves a stored result re-masks it with the connection's current
privacy settings, and serves the stored SQL without the row-filter wrap.

Messages stored before results were masked on the streaming path hold sensitive values
in the clear; the old non-streaming path stored the wrapped SQL.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
import io
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import openpyxl
import pytest

from app.auth.limiter import limiter
from app.database import get_db
from app.main import app

from .conftest import MockResult, _mock_db

if TYPE_CHECKING:
    from httpx import AsyncClient

_R = "[REDACTED]"
_CLEAN = "SELECT id, email FROM users"
_WRAPPED = "SELECT * FROM (SELECT id, email FROM users) AS _sift_q WHERE tenant_id = 7"


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


def _results() -> dict:
    return {
        "columns": ["id", "email"],
        "column_types": ["integer", "text"],
        "rows": [[1, "jane@example.com"], [2, "joe@example.com"]],
        "row_count": 2,
        "truncated": False,
        "execution_time_ms": 3.5,
        "bytes_scanned": None,
    }


def _message(**kwargs) -> MagicMock:
    now = datetime.now(UTC)
    m = MagicMock()
    m.id = kwargs.get("id", "msg-12345678")
    m.session_id = kwargs.get("session_id", "sess-1")
    m.role = "assistant"
    m.content = "Here are the users"
    m.query_generated = kwargs.get("query_generated", _WRAPPED)
    m.query_dialect = "PostgreSQL"
    m.results_json = kwargs.get("results_json", _results())
    m.execution_time_ms = 3.5
    m.bytes_scanned = None
    m.token_count = None
    m.input_tokens = None
    m.output_tokens = None
    m.status = "executed"
    m.cache_hit = False
    m.error = None
    m.feedback = None
    m.share_expires_at = None
    m.created_at = now
    return m


def _session(**kwargs) -> MagicMock:
    s = MagicMock()
    s.id = kwargs.get("id", "sess-1")
    s.connection_id = "conn-1"
    s.title = "Users"
    s.share_expires_at = None
    return s


def _privacy_rows(privacy_settings: dict | None = None, source_type: str | None = "postgresql"):
    """What ``load_result_masks`` reads: (session id, privacy settings, source type)."""
    return MockResult(rows=[("sess-1", privacy_settings, source_type)])


def _csv_rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text.lstrip("﻿"))))


def _xlsx_rows(content: bytes) -> list[list]:
    ws = openpyxl.load_workbook(io.BytesIO(content)).active
    return [list(r) for r in ws.iter_rows(values_only=True)]


def _assert_email_masked_in_csv(text: str) -> None:
    rows = _csv_rows(text)
    assert rows[0] == ["id", "email"]
    assert [r[1] for r in rows[1:]] == [_R, _R]
    assert "jane@example.com" not in text


def _assert_email_masked_in_xlsx(content: bytes) -> None:
    rows = _xlsx_rows(content)
    assert rows[0][:2] == ["id", "email"]
    assert [r[1] for r in rows[1:]] == [_R, _R]


# ── Session history ───────────────────────────────────────────────────────────


class TestSessionHistory:
    async def test_stored_email_is_masked_and_sql_unwrapped(self, http_client: AsyncClient):
        db = _mock_db(MockResult(rows=[_message()]), _privacy_rows())
        db.scalar = AsyncMock(return_value=1)
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/chat/sessions/sess-1/history")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["results_json"]["rows"] == [[1, _R], [2, _R]]
        assert item["results_json"]["columns"] == ["id", "email"]
        assert item["query_generated"] == _CLEAN
        assert db.execute.await_count == 2  # messages + one privacy load for the page

    async def test_current_settings_apply(self, http_client: AsyncClient):
        results = _results()
        results["columns"] = ["id", "nickname"]
        msg = _message(results_json=results, query_generated="SELECT id, nickname FROM users")
        db = _mock_db(
            MockResult(rows=[msg]), _privacy_rows({"sensitive_column_patterns": ["nickname"]})
        )
        db.scalar = AsyncMock(return_value=1)
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/chat/sessions/sess-1/history")
        assert resp.json()["items"][0]["results_json"]["rows"] == [[1, _R], [2, _R]]

    async def test_message_without_results_is_unchanged(self, http_client: AsyncClient):
        msg = _message(results_json=None, query_generated=_CLEAN)
        db = _mock_db(MockResult(rows=[msg]), _privacy_rows())
        db.scalar = AsyncMock(return_value=1)
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/chat/sessions/sess-1/history")
        item = resp.json()["items"][0]
        assert item["results_json"] is None
        assert item["query_generated"] == _CLEAN


# ── Public share ─────────────────────────────────────────────────────────────


class TestShare:
    async def test_shared_result_is_masked(self, http_client: AsyncClient):
        db = _mock_db(_privacy_rows())
        db.scalar = AsyncMock(return_value=_message())
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/public/share/tok")
        assert resp.status_code == 200
        body = resp.json()
        assert body["results"]["rows"] == [[1, _R], [2, _R]]
        assert body["query_generated"] == _CLEAN

    async def test_shared_csv_is_masked(self, http_client: AsyncClient):
        db = _mock_db(_privacy_rows())
        db.scalar = AsyncMock(return_value=_message())
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/public/share/tok/csv")
        assert resp.status_code == 200
        _assert_email_masked_in_csv(resp.text)

    async def test_shared_xlsx_is_masked(self, http_client: AsyncClient):
        db = _mock_db(_privacy_rows())
        db.scalar = AsyncMock(return_value=_message())
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/public/share/tok/xlsx")
        assert resp.status_code == 200
        _assert_email_masked_in_xlsx(resp.content)

    async def test_shared_session_is_masked(self, http_client: AsyncClient):
        db = _mock_db(MockResult(rows=[_message(), _message(id="msg-2")]), _privacy_rows())
        db.scalar = AsyncMock(return_value=_session())
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/public/share/session/tok")
        assert resp.status_code == 200
        messages = resp.json()["messages"]
        assert [m["results_json"]["rows"] for m in messages] == [[[1, _R], [2, _R]]] * 2
        assert [m["query_generated"] for m in messages] == [_CLEAN, _CLEAN]
        assert db.execute.await_count == 2  # messages + one privacy load

    async def test_deleted_connection_masks_with_defaults(self, http_client: AsyncClient):
        db = _mock_db(_privacy_rows(None, None))
        db.scalar = AsyncMock(return_value=_message())
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/public/share/tok")
        assert resp.json()["results"]["rows"] == [[1, _R], [2, _R]]


# ── Authenticated export ─────────────────────────────────────────────────────


class TestExport:
    async def test_csv_is_masked(self, http_client: AsyncClient):
        db = _mock_db(_privacy_rows())
        db.scalar = AsyncMock(return_value=_message())
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/export/messages/msg-12345678/csv")
        assert resp.status_code == 200
        _assert_email_masked_in_csv(resp.text)

    async def test_xlsx_is_masked(self, http_client: AsyncClient):
        db = _mock_db(_privacy_rows())
        db.scalar = AsyncMock(return_value=_message())
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/export/messages/msg-12345678/xlsx")
        assert resp.status_code == 200
        _assert_email_masked_in_xlsx(resp.content)

    async def test_pdf_report_sections_are_masked(self, http_client: AsyncClient):
        db = _mock_db(
            MockResult(rows=[_message(), _message(id="msg-2")]),
            _privacy_rows(),
        )
        app.dependency_overrides[get_db] = lambda: db
        captured: dict = {}

        def fake_report(title: str, sections: list[dict]) -> bytes:
            captured["sections"] = sections
            return b"%PDF-1.4"

        with patch("app.routers.export.generate_report", side_effect=fake_report):
            resp = await http_client.post(
                "/api/v1/export/report",
                json={
                    "title": "Users",
                    "sections": [{"message_id": "msg-12345678"}, {"message_id": "msg-2"}],
                },
            )
        assert resp.status_code == 200
        sections = captured["sections"]
        assert [s["results_json"]["rows"] for s in sections] == [[[1, _R], [2, _R]]] * 2
        assert [s["sql"] for s in sections] == [_CLEAN, _CLEAN]
        assert db.execute.await_count == 2  # messages + one privacy load
