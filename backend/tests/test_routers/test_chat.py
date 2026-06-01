# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for /api/v1/chat endpoints."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
import json as _json
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from httpx import AsyncClient

from app.database import get_db
from app.main import app

from .conftest import MockResult, _mock_db

# ── Helpers ───────────────────────────────────────────────────────────────────


def _sse_events(response_text: str) -> list[dict]:
    """Parse SSE wire format into a list of event dicts."""
    events: list[dict] = []
    for block in response_text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                with contextlib.suppress(_json.JSONDecodeError):
                    events.append(_json.loads(line[6:]))
    return events


async def _stream_gen(*events):
    """Yield events from an iterable — simulates ChatService.stream_message()."""
    for e in events:
        yield e


def _mock_streaming_service(*events) -> MagicMock:
    """Build a mock ChatService whose stream_message() yields the given events."""
    svc = MagicMock()
    svc.stream_message = MagicMock(return_value=_stream_gen(*events))
    return svc


def _make_chat_response(**kwargs) -> dict:
    """Minimal valid ChatResponse dict (used by other endpoint tests)."""
    return {
        "session_id": kwargs.get("session_id", "sess-1"),
        "message_id": kwargs.get("message_id", "msg-1"),
        "query": kwargs.get("query", "SELECT 1"),
        "query_dialect": kwargs.get("query_dialect", "postgresql"),
        "explanation": kwargs.get("explanation", ""),
        "results": None,
        "execution_time_ms": None,
        "status": kwargs.get("status", "executed"),
        "cache_hit": False,
        "error": kwargs.get("error"),
    }


def _make_session(**kwargs) -> MagicMock:
    """Raw ChatSession ORM mock — used by scalar_one_or_none() in get/delete tests."""
    now = datetime.now(UTC)
    m = MagicMock()
    m.id = kwargs.get("id", "sess-1")
    m.connection_id = kwargs.get("connection_id", "conn-123")
    m.title = kwargs.get("title", "My Session")
    m.provider = kwargs.get("provider", "claude")
    m.created_at = kwargs.get("created_at", now)
    m.updated_at = kwargs.get("updated_at", now)
    return m


def _make_session_row(**kwargs) -> MagicMock:
    """Joined row from list_sessions query (ChatSession + cache_hit_count label)."""
    session = _make_session(**{k: v for k, v in kwargs.items() if k != "cache_hit_count"})
    row = MagicMock()
    row.ChatSession = session
    row.cache_hit_count = kwargs.get("cache_hit_count", 0)
    return row


def _make_message(**kwargs) -> MagicMock:
    now = datetime.now(UTC)
    m = MagicMock()
    m.id = kwargs.get("id", "msg-1")
    m.session_id = kwargs.get("session_id", "sess-1")
    m.role = kwargs.get("role", "assistant")
    m.content = kwargs.get("content", "Here is your query")
    m.query_generated = kwargs.get("query_generated", "SELECT 1")
    m.query_dialect = kwargs.get("query_dialect", "postgresql")
    m.results_json = kwargs.get("results_json")
    m.execution_time_ms = kwargs.get("execution_time_ms")
    m.bytes_scanned = kwargs.get("bytes_scanned")
    m.status = kwargs.get("status", "executed")
    m.cache_hit = kwargs.get("cache_hit", False)
    m.error = kwargs.get("error")
    m.feedback = kwargs.get("feedback")
    m.created_at = kwargs.get("created_at", now)
    return m


def _mock_service(**methods) -> MagicMock:
    """Build a mock ChatService with AsyncMock methods."""
    svc = MagicMock()
    for name, return_value in methods.items():
        setattr(svc, name, AsyncMock(return_value=return_value))
    return svc


# ── POST /api/v1/chat (SSE stream) ────────────────────────────────────────────


class TestProcessMessage:
    """POST /api/v1/chat now returns text/event-stream SSE events."""

    _payload: ClassVar[dict[str, str]] = {
        "connection_id": "conn-123",
        "message": "How many users?",
        "provider": "claude",
    }

    async def test_success_returns_200_and_sse_content_type(self, http_client):
        # process_message no longer uses get_db — no override needed (PERF-5)
        svc = _mock_streaming_service(
            {
                "type": "done",
                "session_id": "s-1",
                "message_id": "m-1",
                "execution_time_ms": None,
                "cache_hit": False,
                "status": "executed",
            },
        )
        with patch("app.routers.chat._make_chat_service", return_value=svc):
            resp = await http_client.post("/api/v1/chat", json=self._payload)
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

    async def test_done_event_carries_session_and_message_ids(self, http_client):
        svc = _mock_streaming_service(
            {
                "type": "done",
                "session_id": "s-1",
                "message_id": "m-1",
                "execution_time_ms": None,
                "cache_hit": False,
                "status": "executed",
            },
        )
        with patch("app.routers.chat._make_chat_service", return_value=svc):
            resp = await http_client.post("/api/v1/chat", json=self._payload)
        events = _sse_events(resp.text)
        done = next(e for e in events if e.get("type") == "done")
        assert done["session_id"] == "s-1"
        assert done["message_id"] == "m-1"

    async def test_status_events_are_emitted(self, http_client):
        svc = _mock_streaming_service(
            {"type": "status", "message": "Loading connection\u2026"},
            {
                "type": "done",
                "session_id": "s-1",
                "message_id": "m-1",
                "execution_time_ms": None,
                "cache_hit": False,
                "status": "executed",
            },
        )
        with patch("app.routers.chat._make_chat_service", return_value=svc):
            resp = await http_client.post("/api/v1/chat", json=self._payload)
        events = _sse_events(resp.text)
        assert any(e.get("type") == "status" for e in events)

    async def test_sql_event_appears_before_done(self, http_client):
        svc = _mock_streaming_service(
            {"type": "sql", "query": "SELECT count(*) FROM users", "dialect": "postgresql"},
            {
                "type": "done",
                "session_id": "s-1",
                "message_id": "m-1",
                "execution_time_ms": 42.0,
                "cache_hit": False,
                "status": "executed",
            },
        )
        with patch("app.routers.chat._make_chat_service", return_value=svc):
            resp = await http_client.post("/api/v1/chat", json=self._payload)
        events = _sse_events(resp.text)
        types = [e["type"] for e in events]
        assert "sql" in types
        assert types.index("sql") < types.index("done")

    async def test_pipeline_error_emits_error_and_done(self, http_client):
        """Pipeline errors arrive as SSE events, not HTTP 4xx."""
        svc = _mock_streaming_service(
            {"type": "error", "message": "Connection 'bad-conn' not found"},
            {
                "type": "done",
                "session_id": "",
                "message_id": "",
                "execution_time_ms": None,
                "cache_hit": False,
                "status": "error",
            },
        )
        with patch("app.routers.chat._make_chat_service", return_value=svc):
            resp = await http_client.post(
                "/api/v1/chat",
                json={"connection_id": "bad-conn", "message": "test", "provider": "claude"},
            )
        # SSE always returns 200 — error information is in the event stream
        assert resp.status_code == 200
        events = _sse_events(resp.text)
        assert any(e.get("type") == "error" for e in events)
        done = next(e for e in events if e.get("type") == "done")
        assert done["status"] == "error"

    async def test_missing_message_returns_422(self, http_client):
        """Request validation (missing required field) still returns 422."""
        resp = await http_client.post(
            "/api/v1/chat",
            json={"connection_id": "conn-123", "provider": "claude"},
        )
        assert resp.status_code == 422

    async def test_disconnect_cancels_stream(self, http_client):
        """Client disconnect causes aclose() on the stream generator (PROD-6).

        Verifies that when is_disconnected() returns True after the first event,
        the event_generator breaks out of the loop and the finally block calls
        stream.aclose(), which triggers GeneratorExit inside stream_message.
        """
        import asyncio

        aclose_called = False

        async def _slow_stream(*_args, **_kwargs):
            nonlocal aclose_called
            try:
                yield {"type": "status", "message": "Loading…"}
                await asyncio.sleep(0)
                yield {
                    "type": "done",
                    "session_id": "s-1",
                    "message_id": "m-1",
                    "execution_time_ms": None,
                    "cache_hit": False,
                    "status": "executed",
                }
            except GeneratorExit:
                aclose_called = True

        svc = MagicMock()
        svc.stream_message = MagicMock(return_value=_slow_stream())

        # Simulate a disconnect reported after the first event is checked.
        disconnect_calls = {"n": 0}

        async def _is_disconnected_after_first(self_req):
            disconnect_calls["n"] += 1
            # Return True from the second call onwards (after first event yields)
            return disconnect_calls["n"] > 1

        with (
            patch("app.routers.chat._make_chat_service", return_value=svc),
            patch(
                "starlette.requests.Request.is_disconnected",
                _is_disconnected_after_first,
            ),
        ):
            resp = await http_client.post("/api/v1/chat", json=self._payload)

        assert resp.status_code == 200
        assert aclose_called, "stream.aclose() must be called when client disconnects"


# ── POST /api/v1/chat/execute/{message_id} ───────────────────────────────────────


class TestExecutePending:
    async def test_success_returns_200(self, http_client):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        svc = _mock_service(execute_pending=_make_chat_response())
        with patch("app.routers.chat._make_chat_service", return_value=svc):
            resp = await http_client.post("/api/v1/chat/execute/msg-1")
        assert resp.status_code == 200

    async def test_value_error_returns_400(self, http_client):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        svc = MagicMock()
        svc.execute_pending = AsyncMock(side_effect=ValueError("Message not found"))
        with patch("app.routers.chat._make_chat_service", return_value=svc):
            resp = await http_client.post("/api/v1/chat/execute/bad-msg")
        assert resp.status_code == 400

    async def test_resource_not_found_returns_404(self, http_client):
        from app.services.chat_service import ResourceNotFoundError

        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        svc = MagicMock()
        svc.execute_pending = AsyncMock(
            side_effect=ResourceNotFoundError("Message 'bad-msg' not found")
        )
        with patch("app.routers.chat._make_chat_service", return_value=svc):
            resp = await http_client.post("/api/v1/chat/execute/bad-msg")
        assert resp.status_code == 404


# ── POST /api/v1/chat/edit/{message_id} ──────────────────────────────────────────


class TestEditAndExecute:
    async def test_success_returns_200(self, http_client):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        svc = _mock_service(edit_and_execute=_make_chat_response())
        with patch("app.routers.chat._make_chat_service", return_value=svc):
            resp = await http_client.post(
                "/api/v1/chat/edit/msg-1",
                json={"message_id": "msg-1", "edited_query": "SELECT count(*) FROM users"},
            )
        assert resp.status_code == 200

    async def test_missing_edited_query_returns_422(self, http_client):
        resp = await http_client.post(
            "/api/v1/chat/edit/msg-1",
            json={"message_id": "msg-1"},
        )
        assert resp.status_code == 422

    async def test_value_error_returns_400(self, http_client):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        svc = MagicMock()
        svc.edit_and_execute = AsyncMock(side_effect=ValueError("not found"))
        with patch("app.routers.chat._make_chat_service", return_value=svc):
            resp = await http_client.post(
                "/api/v1/chat/edit/msg-1",
                json={"message_id": "msg-1", "edited_query": "SELECT 1"},
            )
        assert resp.status_code == 400

    async def test_execution_error_returns_200_with_error_status(self, http_client):
        """Service returns status='error' (e.g. DB exec failed) → router passes it through as 200.

        The service is responsible for persisting status='error' to the DB before returning;
        the router contract is simply to surface whatever ChatResponse the service produces.
        """
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        error_response = _make_chat_response(status="error", error="column 'x' does not exist")
        svc = _mock_service(edit_and_execute=error_response)
        with patch("app.routers.chat._make_chat_service", return_value=svc):
            resp = await http_client.post(
                "/api/v1/chat/edit/msg-1",
                json={"message_id": "msg-1", "edited_query": "SELECT x FROM t"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "error"
        assert body["error"] is not None
        assert "x" in body["error"]


# ── POST /api/v1/chat/feedback/{message_id} ──────────────────────────────────────


class TestSubmitFeedback:
    async def test_thumbs_up_returns_204(self, http_client):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        svc = _mock_service(submit_feedback="conn-123")
        with patch("app.routers.chat._make_chat_service", return_value=svc):
            resp = await http_client.post(
                "/api/v1/chat/feedback/msg-1",
                json={"message_id": "msg-1", "feedback": "thumbs_up"},
            )
        assert resp.status_code == 204

    async def test_thumbs_down_returns_204(self, http_client):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        svc = _mock_service(submit_feedback="conn-123")
        with patch("app.routers.chat._make_chat_service", return_value=svc):
            resp = await http_client.post(
                "/api/v1/chat/feedback/msg-1",
                json={"message_id": "msg-1", "feedback": "thumbs_down"},
            )
        assert resp.status_code == 204

    async def test_invalid_feedback_returns_422(self, http_client):
        resp = await http_client.post(
            "/api/v1/chat/feedback/msg-1",
            json={"message_id": "msg-1", "feedback": "meh"},
        )
        assert resp.status_code == 422

    async def test_value_error_returns_400(self, http_client):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        svc = MagicMock()
        svc.submit_feedback = AsyncMock(side_effect=ValueError("not found"))
        with patch("app.routers.chat._make_chat_service", return_value=svc):
            resp = await http_client.post(
                "/api/v1/chat/feedback/msg-1",
                json={"message_id": "msg-1", "feedback": "thumbs_up"},
            )
        assert resp.status_code == 400

    async def test_resource_not_found_returns_404(self, http_client):
        from app.services.chat_service import ResourceNotFoundError

        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        svc = MagicMock()
        svc.submit_feedback = AsyncMock(
            side_effect=ResourceNotFoundError("Message 'msg-1' not found")
        )
        with patch("app.routers.chat._make_chat_service", return_value=svc):
            resp = await http_client.post(
                "/api/v1/chat/feedback/msg-1",
                json={"message_id": "msg-1", "feedback": "thumbs_up"},
            )
        assert resp.status_code == 404


# ── GET /api/v1/chat/sessions ────────────────────────────────────────────────────


class TestListSessions:
    async def test_returns_200(self, http_client):
        db = _mock_db(MockResult(rows=[]))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/chat/sessions")
        assert resp.status_code == 200

    async def test_returns_paginated(self, http_client):
        db = _mock_db(MockResult(rows=[]))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/chat/sessions")
        body = resp.json()
        assert "items" in body
        assert isinstance(body["items"], list)

    async def test_returns_session_fields(self, http_client):
        row = _make_session_row(id="sess-1", title="My Chat")
        db = _mock_db(MockResult(rows=[row]))  # single JOIN query
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/chat/sessions")
        data = resp.json()["items"]
        assert len(data) == 1
        assert data[0]["id"] == "sess-1"


# ── GET /api/v1/chat/sessions/{session_id} ───────────────────────────────────────


class TestGetSession:
    async def test_returns_200_for_known_session(self, http_client):
        row = _make_session_row(id="sess-1")
        db = _mock_db(MockResult(row=row))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/chat/sessions/sess-1")
        assert resp.status_code == 200

    async def test_not_found_returns_404(self, http_client):
        db = _mock_db(MockResult(row=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/chat/sessions/missing")
        assert resp.status_code == 404


# ── GET /api/v1/chat/sessions/{session_id}/history ───────────────────────────────


class TestGetSessionHistory:
    async def test_returns_200(self, http_client):
        msg = _make_message()
        # scalar: count=1 (non-zero → skip exists check); execute: messages
        db = _mock_db(MockResult(rows=[msg]))
        db.scalar = AsyncMock(return_value=1)
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/chat/sessions/sess-1/history")
        assert resp.status_code == 200

    async def test_returns_paginated(self, http_client):
        # scalar: count=0 (empty session), then exists=1 (owned session → not 404)
        db = _mock_db(MockResult(rows=[]))
        db.scalar = AsyncMock(side_effect=[0, 1])
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/chat/sessions/sess-1/history")
        body = resp.json()
        assert "items" in body
        assert isinstance(body["items"], list)

    async def test_cannot_read_foreign_org_session_history(self, http_client: AsyncClient) -> None:
        """Session history for session belonging to different org → 404."""
        # scalar: count=0 (no messages for this user), exists=0 (not owned) → 404
        db = _mock_db()
        db.scalar = AsyncMock(side_effect=[0, 0])
        app.dependency_overrides[get_db] = lambda: db
        response = await http_client.get("/api/v1/chat/sessions/foreign-session/history")
        assert response.status_code == 404


# ── DELETE /api/v1/chat/sessions/{session_id} ────────────────────────────────────


class TestDeleteSession:
    async def test_success_returns_204(self, http_client):
        session = _make_session()
        db = _mock_db(
            MockResult(single=session),  # get session
            MockResult(),  # delete messages
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.delete("/api/v1/chat/sessions/sess-1")
        assert resp.status_code == 204

    async def test_not_found_returns_404(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.delete("/api/v1/chat/sessions/missing")
        assert resp.status_code == 404


# ── GET /api/v1/chat/cache/stats ─────────────────────────────────────────────────


class TestGetCacheStats:
    async def test_returns_200(self, http_client):
        agg_row = MagicMock()
        agg_row.total = 3
        agg_row.total_hits = 7
        agg_row.total_misses = 0  # now embedded in the agg query via scalar subquery
        top_entry = MagicMock()
        top_entry.question_raw = "How many users?"
        top_entry.hit_count = 5
        db = _mock_db(
            MockResult(row=agg_row),  # aggregate query (total, hits, misses)
            MockResult(rows=[top_entry]),  # top entries
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/chat/cache/stats")
        assert resp.status_code == 200

    async def test_response_has_required_fields(self, http_client):
        agg_row = MagicMock()
        agg_row.total = 0
        agg_row.total_hits = 0
        agg_row.total_misses = 0
        db = _mock_db(
            MockResult(row=agg_row),
            MockResult(rows=[]),
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/chat/cache/stats")
        body = resp.json()
        assert "total_entries" in body
        assert "hit_count" in body
        assert "top_cached_queries" in body

    async def test_total_entries_matches_db(self, http_client):
        agg_row = MagicMock()
        agg_row.total = 42
        agg_row.total_hits = 100
        agg_row.total_misses = 0
        db = _mock_db(
            MockResult(row=agg_row),
            MockResult(rows=[]),
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/chat/cache/stats")
        assert resp.json()["total_entries"] == 42

    async def test_miss_count_comes_from_db(self, http_client):
        agg_row = MagicMock()
        agg_row.total = 5
        agg_row.total_hits = 10
        agg_row.total_misses = 42  # 42 misses now embedded in the agg row
        db = _mock_db(
            MockResult(row=agg_row),
            MockResult(rows=[]),
        )
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/chat/cache/stats")
        assert resp.json()["miss_count"] == 42


# ── DELETE /api/v1/chat/cache/{connection_id} ────────────────────────────────────


class TestClearCache:
    async def test_returns_204(self, http_client):
        db = _mock_db(MockResult(single=MagicMock()))
        app.dependency_overrides[get_db] = lambda: db
        mock_cache = MagicMock()
        mock_cache.invalidate = AsyncMock()
        with patch("app.routers.chat._get_shared_cache", return_value=mock_cache):
            resp = await http_client.delete("/api/v1/chat/cache/conn-123")
        assert resp.status_code == 204

    async def test_calls_invalidate_with_connection_id(self, http_client):
        db = _mock_db(MockResult(single=MagicMock()))
        app.dependency_overrides[get_db] = lambda: db
        mock_cache = MagicMock()
        mock_cache.invalidate = AsyncMock()
        with patch("app.routers.chat._get_shared_cache", return_value=mock_cache):
            await http_client.delete("/api/v1/chat/cache/conn-456")
        mock_cache.invalidate.assert_called_once()
        call_args = mock_cache.invalidate.call_args[0]
        assert call_args[0] == "conn-456"

    async def test_invalid_connection_returns_404(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.delete("/api/v1/chat/cache/missing-conn")
        assert resp.status_code == 404


# ── DELETE /api/v1/chat/cache/entries/{entry_id} ────────────────────────────────


class TestDeleteCacheEntry:
    def _make_entry(self, connection_id: str = "conn-123") -> MagicMock:
        m = MagicMock()
        m.id = "entry-1"
        m.connection_id = connection_id
        return m

    async def test_returns_204(self, http_client):
        entry = self._make_entry()
        conn = MagicMock()
        # Two execute calls: entry lookup, then connection ownership check
        db = _mock_db(MockResult(single=entry), MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.delete("/api/v1/chat/cache/entries/entry-1")
        assert resp.status_code == 204

    async def test_missing_entry_returns_404(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.delete("/api/v1/chat/cache/entries/missing-entry")
        assert resp.status_code == 404

    async def test_inactive_connection_returns_404(self, http_client):
        """Entry exists but its connection is inactive — ownership check must block deletion."""
        entry = self._make_entry(connection_id="deactivated-conn")
        # Entry found, but connection lookup returns None (inactive/missing)
        db = _mock_db(MockResult(single=entry), MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.delete("/api/v1/chat/cache/entries/entry-1")
        assert resp.status_code == 404


# ── GET /api/v1/chat/examples/{connection_id} ────────────────────────────────────


class TestListExamples:
    async def test_returns_200(self, http_client):
        db = _mock_db(MockResult(rows=[]))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/chat/examples/conn-123")
        assert resp.status_code == 200

    async def test_response_has_items_and_total(self, http_client):
        db = _mock_db(MockResult(rows=[]))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/chat/examples/conn-123")
        body = resp.json()
        assert "items" in body
        assert "total" in body
        assert body["total"] == 0


# ── POST /api/v1/chat/examples/{connection_id} ───────────────────────────────────


class TestAddExample:
    async def test_returns_201(self, http_client):
        from datetime import datetime

        example_row = MagicMock()
        example_row.id = "ex-1"
        example_row.question = "How many users?"
        example_row.query = "SELECT count(*) FROM users"
        example_row.query_dialect = ""
        example_row.created_at = datetime.now(UTC)
        # Two execute calls: connection check + reload example after insert
        db = _mock_db(MockResult(single=MagicMock()), MockResult(single=example_row))
        app.dependency_overrides[get_db] = lambda: db
        mock_cache = MagicMock()
        mock_cache.compute_embedding_async = AsyncMock(return_value=[0.1, 0.2])
        mock_cache.evict_similar = AsyncMock()
        mock_library = MagicMock()
        mock_library.add_example = AsyncMock(return_value=MagicMock(id="ex-1"))
        with (
            patch("app.routers.chat._get_shared_cache", return_value=mock_cache),
            patch("app.routers.chat.ExampleLibrary", return_value=mock_library),
            patch("asyncio.to_thread", new_callable=AsyncMock, return_value=[0.1, 0.2]),
            patch(
                "app.routers.chat.create_datasource",
                return_value=MagicMock(query_dialect="postgresql"),
            ),
        ):
            resp = await http_client.post(
                "/api/v1/chat/examples/conn-123",
                json={"question": "How many users?", "query": "SELECT count(*) FROM users"},
            )
        assert resp.status_code == 201

    async def test_invalid_connection_returns_404(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/chat/examples/missing-conn",
            json={"question": "How many?", "query": "SELECT count(*) FROM t"},
        )
        assert resp.status_code == 404


# ── DELETE /api/v1/chat/examples/{example_id} ───────────────────────────────────


class TestDeleteExample:
    async def test_returns_204(self, http_client):
        db = _mock_db(MockResult(single=MagicMock()))
        app.dependency_overrides[get_db] = lambda: db
        mock_library = MagicMock()
        mock_library.remove_example = AsyncMock()
        with patch("app.routers.chat.ExampleLibrary", return_value=mock_library):
            resp = await http_client.delete("/api/v1/chat/examples/ex-1")
        assert resp.status_code == 204

    async def test_remove_example_called_with_id(self, http_client):
        db = _mock_db(MockResult(single=MagicMock()))
        app.dependency_overrides[get_db] = lambda: db
        mock_library = MagicMock()
        mock_library.remove_example = AsyncMock()
        with patch("app.routers.chat.ExampleLibrary", return_value=mock_library):
            await http_client.delete("/api/v1/chat/examples/ex-999")
        mock_library.remove_example.assert_called_once_with("ex-999", db)

    async def test_not_found_returns_404(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.delete("/api/v1/chat/examples/missing-ex")
        assert resp.status_code == 404


# ── Cross-org IDOR tests ─────────────────────────────────────────────────────


class TestSessionHistoryIDOR:
    async def test_cannot_read_foreign_org_session_history(self, http_client) -> None:
        """Session belonging to different org returns 404."""
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/chat/sessions/foreign-session/history")
        assert resp.status_code == 404
