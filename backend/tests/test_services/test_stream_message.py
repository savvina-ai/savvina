# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for ChatService.stream_message() — the SSE streaming pipeline."""

from __future__ import annotations

import contextlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.datasources.models import QueryResult, ValidationResult
from app.providers.base import LLMResponse
from app.services.chat_service import ChatService

# ── Test module ───────────────────────────────────────────────────────────────

_MODULE = "app.services.chat_service"


def _mock_session_factory(mock_db):
    """Return a callable that acts like ``async_session_maker`` but yields *mock_db*.

    Used to patch ``_create_session`` in ``chat_service`` so that the self-managed
    ``async with _create_session() as db:`` blocks receive the pre-configured mock
    session instead of trying to open a real database connection.
    """

    @contextlib.asynccontextmanager
    async def _factory():
        yield mock_db

    return _factory


_SCHEMA_CACHE_DICT = {
    "source_type": "postgresql",
    "schemas": [{"name": "public", "description": None}],
    "tables": [],
    "relationships": [],
    "metadata": {},
}


async def _collect(gen) -> list[dict]:
    """Drain an async generator into a list of event dicts."""
    return [e async for e in gen]


def _make_settings(**overrides) -> MagicMock:
    s = MagicMock()
    s.encryption_key = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
    s.cache_enabled = False  # disable cache by default in these tests
    s.default_query_timeout = 30
    s.default_row_limit = 1000
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _make_connection(
    *,
    execution_mode: str = "auto_execute",
    privacy_settings: dict | None = None,
    semantic_model: dict | None = None,
) -> MagicMock:
    conn = MagicMock()
    conn.id = "conn-1"
    conn.source_type = "postgresql"
    conn.config_encrypted = b"encrypted-bytes"
    conn.privacy_settings = privacy_settings
    conn.execution_mode = execution_mode
    conn.semantic_model = semantic_model
    return conn


def _make_db_with_conn(conn: MagicMock) -> MagicMock:
    """AsyncSession that returns the connection on first execute, None thereafter."""
    db = MagicMock()

    class _Result:
        def __init__(self, val):
            self._val = val

        def scalar_one_or_none(self):
            return self._val

    # Connection lookup, then UserSchemaCache lookup (returns None → introspect path),
    # then ChatSession lookup (get_or_create_session inner queries)
    conn_result = _Result(conn)
    usc_result = _Result(None)  # no cached schema → will introspect

    # For get_or_create_session: it tries to find existing session (returns None)
    # then creates one — we let it fall through to the MagicMock
    session_result = _Result(None)

    db.execute = AsyncMock(
        side_effect=[conn_result, usc_result, session_result, session_result, session_result]
    )
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _make_adapter(batch_rows=None, batch_cols=None) -> MagicMock:
    adapter = MagicMock()
    adapter.source_type = "postgresql"
    adapter.query_dialect = "postgresql"
    adapter.get_system_prompt_additions = MagicMock(return_value="")
    adapter.format_schema_for_llm = MagicMock(return_value="-- DDL --")
    adapter.validate_query = MagicMock(
        return_value=ValidationResult(is_valid=True, error_message=None)
    )
    cols = batch_cols or ["count"]
    rows_data = batch_rows or [[1]]

    async def _stream_gen(query, timeout=30, batch_size=50, max_rows=1000):  # noqa: ASYNC109
        yield QueryResult(
            columns=cols,
            column_types=["integer"] * len(cols),
            rows=rows_data,
            row_count=len(rows_data),
            truncated=False,
            execution_time_ms=10.0,
        )

    adapter.execute_query_stream = _stream_gen
    adapter.connect = AsyncMock()
    adapter.disconnect = AsyncMock()
    adapter.introspect = AsyncMock(
        return_value=MagicMock(
            source_type="postgresql", schemas=[], tables=[], relationships=[], metadata={}
        )
    )
    return adapter


def _make_llm_response(
    query: str = "SELECT count(*) FROM users",
    explanation: str = "Count users",
) -> LLMResponse:
    return LLMResponse(
        query=query,
        explanation=explanation,
        raw_response=f"```sql\n{query}\n```\n\n{explanation}",
        model="test-model",
        tokens_used=10,
    )


def _make_provider(llm_response: LLMResponse | None = None) -> MagicMock:
    p = MagicMock()
    p.generate_response = AsyncMock(return_value=llm_response or _make_llm_response())
    p.max_output_tokens = 8192
    p.context_window = None
    return p


# ── Happy-path end-to-end ──────────────────────────────────────────────────────


class TestStreamMessageHappyPath:
    async def test_emits_status_events_then_sql_then_row_batch_then_done(self):
        conn = _make_connection()
        db = _make_db_with_conn(conn)
        adapter = _make_adapter()
        provider = _make_provider()
        settings = _make_settings()

        svc = ChatService(MagicMock(), MagicMock())

        with (
            patch(f"{_MODULE}.get_settings", return_value=settings),
            patch(f"{_MODULE}.decrypt_value", return_value=json.dumps({"host": "db"})),
            patch(f"{_MODULE}.create_datasource", return_value=adapter),
            patch(f"{_MODULE}._create_session", _mock_session_factory(db)),
            patch(f"{_MODULE}._resolve_schema") as mock_schema,
            patch(f"{_MODULE}._generate_query") as mock_gen,
            patch(f"{_MODULE}._validate_and_correct_query") as mock_val,
            patch(f"{_MODULE}._get_or_create_session") as mock_session,
            patch(f"{_MODULE}._save_user_message", new=AsyncMock()),
            patch(f"{_MODULE}._save_assistant_message") as mock_save,
        ):
            schema = MagicMock()
            schema_result = MagicMock(schema=schema, connected=False, embeddings_available=False)
            mock_schema.return_value = schema_result

            gen_result = MagicMock()
            gen_result.generated_query = "SELECT count(*) FROM users"
            gen_result.explanation = "Count users"
            gen_result.error = None
            gen_result.status = "executed"
            gen_result.cache_hit = False
            gen_result.cache_embedding = None
            gen_result.provider = provider
            gen_result.configured_model = "claude-3"
            gen_result.system_prompt = "sys"
            gen_result.history = []
            mock_gen.return_value = gen_result

            val_result = MagicMock()
            val_result.generated_query = "SELECT count(*) FROM users"
            val_result.explanation = "Count users"
            val_result.error = None
            val_result.status = "executed"
            mock_val.return_value = val_result

            session = MagicMock(id="sess-1")
            mock_session.return_value = session

            saved_msg = MagicMock(id="msg-1")
            mock_save.return_value = saved_msg

            events = await _collect(
                svc.stream_message(
                    connection_id="conn-1",
                    session_id=None,
                    message="How many users?",
                    provider_name="claude",
                    options={"max_rows": 100},
                )
            )

        types = [e["type"] for e in events]
        assert "status" in types
        assert "sql" in types
        assert "row_batch" in types
        assert "done" in types

        # Order: at least one status before sql; sql before row_batch; row_batch before done
        assert types.index("sql") > types.index("status")
        assert types.index("row_batch") > types.index("sql")
        assert types.index("done") > types.index("row_batch")

        done = next(e for e in events if e["type"] == "done")
        assert done["session_id"] == "sess-1"
        assert done["message_id"] == "msg-1"
        assert done["cache_hit"] is False
        assert done["status"] == "executed"

    async def test_row_batch_event_has_correct_fields(self):
        conn = _make_connection()
        db = _make_db_with_conn(conn)
        adapter = _make_adapter(batch_rows=[[5], [10]], batch_cols=["total"])
        provider = _make_provider()
        settings = _make_settings()

        svc = ChatService(MagicMock(), MagicMock())

        with (
            patch(f"{_MODULE}.get_settings", return_value=settings),
            patch(f"{_MODULE}.decrypt_value", return_value=json.dumps({"host": "db"})),
            patch(f"{_MODULE}.create_datasource", return_value=adapter),
            patch(f"{_MODULE}._create_session", _mock_session_factory(db)),
            patch(f"{_MODULE}._resolve_schema") as mock_schema,
            patch(f"{_MODULE}._generate_query") as mock_gen,
            patch(f"{_MODULE}._validate_and_correct_query") as mock_val,
            patch(f"{_MODULE}._get_or_create_session") as mock_session,
            patch(f"{_MODULE}._save_user_message", new=AsyncMock()),
            patch(
                f"{_MODULE}._save_assistant_message",
                new=AsyncMock(return_value=MagicMock(id="m")),
            ),
        ):
            mock_schema.return_value = MagicMock(
                schema=MagicMock(),
                connected=False,
                embeddings_available=False,
            )

            gen_result = MagicMock(
                generated_query="SELECT total FROM t",
                explanation="Total",
                error=None,
                status="executed",
                cache_hit=False,
                provider=provider,
                configured_model="m",
                system_prompt="s",
                history=[],
            )
            mock_gen.return_value = gen_result
            mock_val.return_value = MagicMock(
                generated_query="SELECT total FROM t",
                explanation="Total",
                error=None,
                status="executed",
            )
            mock_session.return_value = MagicMock(id="sess-1")

            events = await _collect(
                svc.stream_message(
                    connection_id="conn-1",
                    session_id=None,
                    message="query",
                    provider_name="claude",
                    options={},
                )
            )

        batch = next(e for e in events if e["type"] == "row_batch")
        assert batch["columns"] == ["total"]
        assert batch["rows"] == [[5], [10]]
        assert "truncated" in batch
        assert "batch_index" in batch


# ── Connection not found ────────────────────────────────────────────────────────


class TestStreamMessageConnectionNotFound:
    async def test_yields_error_then_done_when_connection_missing(self):
        db = MagicMock()

        class _NullResult:
            def scalar_one_or_none(self):
                return None

        db.execute = AsyncMock(return_value=_NullResult())
        svc = ChatService(MagicMock(), MagicMock())

        with (
            patch(f"{_MODULE}.get_settings", return_value=_make_settings()),
            patch(f"{_MODULE}._create_session", _mock_session_factory(db)),
        ):
            events = await _collect(
                svc.stream_message(
                    connection_id="missing",
                    session_id=None,
                    message="test",
                    provider_name="claude",
                    options={},
                )
            )

        types = [e["type"] for e in events]
        assert "error" in types
        assert "done" in types
        assert types.index("error") < types.index("done")
        done = next(e for e in events if e["type"] == "done")
        assert done["status"] == "error"


# ── Generation error ────────────────────────────────────────────────────────────


class TestStreamMessageGenerationError:
    async def test_yields_error_then_done_when_generation_fails(self):
        conn = _make_connection()
        db = _make_db_with_conn(conn)
        adapter = _make_adapter()
        settings = _make_settings()

        svc = ChatService(MagicMock(), MagicMock())

        with (
            patch(f"{_MODULE}.get_settings", return_value=settings),
            patch(f"{_MODULE}.decrypt_value", return_value=json.dumps({"host": "db"})),
            patch(f"{_MODULE}.create_datasource", return_value=adapter),
            patch(f"{_MODULE}._create_session", _mock_session_factory(db)),
            patch(f"{_MODULE}._resolve_schema") as mock_schema,
            patch(f"{_MODULE}._generate_query", side_effect=RuntimeError("LLM timeout")),
        ):
            mock_schema.return_value = MagicMock(
                schema=MagicMock(),
                connected=False,
                embeddings_available=False,
            )
            events = await _collect(
                svc.stream_message(
                    connection_id="conn-1",
                    session_id=None,
                    message="fail",
                    provider_name="claude",
                    options={},
                )
            )

        types = [e["type"] for e in events]
        assert "error" in types
        assert "done" in types
        done = next(e for e in events if e["type"] == "done")
        assert done["status"] == "error"


# ── Execution mode: generate_only ─────────────────────────────────────────────


class TestStreamMessageExecutionMode:
    async def _run_mode(self, execution_mode: str) -> list[dict]:
        conn = _make_connection(execution_mode=execution_mode)
        db = _make_db_with_conn(conn)
        adapter = _make_adapter()
        provider = _make_provider()
        settings = _make_settings()

        svc = ChatService(MagicMock(), MagicMock())

        with (
            patch(f"{_MODULE}.get_settings", return_value=settings),
            patch(f"{_MODULE}.decrypt_value", return_value=json.dumps({"host": "db"})),
            patch(f"{_MODULE}.create_datasource", return_value=adapter),
            patch(f"{_MODULE}._create_session", _mock_session_factory(db)),
            patch(f"{_MODULE}._resolve_schema") as mock_schema,
            patch(f"{_MODULE}._generate_query") as mock_gen,
            patch(f"{_MODULE}._validate_and_correct_query") as mock_val,
            patch(f"{_MODULE}._get_or_create_session") as mock_session,
            patch(f"{_MODULE}._save_user_message", new=AsyncMock()),
            patch(
                f"{_MODULE}._save_assistant_message",
                new=AsyncMock(return_value=MagicMock(id="m")),
            ),
        ):
            mock_schema.return_value = MagicMock(
                schema=MagicMock(),
                connected=False,
                embeddings_available=False,
            )
            gen_result = MagicMock(
                generated_query="SELECT 1",
                explanation="one",
                error=None,
                status="executed",
                cache_hit=False,
                provider=provider,
                configured_model="m",
                system_prompt="s",
                history=[],
            )
            mock_gen.return_value = gen_result
            mock_val.return_value = MagicMock(
                generated_query="SELECT 1",
                explanation="one",
                error=None,
                status="executed",
            )
            mock_session.return_value = MagicMock(id="sess-1")

            return await _collect(
                svc.stream_message(
                    connection_id="conn-1",
                    session_id=None,
                    message="test",
                    provider_name="claude",
                    options={},
                )
            )

    async def test_generate_only_has_no_row_batch(self):
        events = await self._run_mode("generate_only")
        types = [e["type"] for e in events]
        assert "row_batch" not in types
        done = next(e for e in events if e["type"] == "done")
        assert done["status"] == "query_only"

    async def test_review_first_has_no_row_batch(self):
        events = await self._run_mode("review_first")
        types = [e["type"] for e in events]
        assert "row_batch" not in types
        done = next(e for e in events if e["type"] == "done")
        assert done["status"] == "pending_approval"
