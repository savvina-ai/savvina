# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for ChatService — the core NL-to-SQL orchestration service.

Covers all 7 scenarios from the implementation plan:
  1. Full pipeline works end-to-end
  2. Cache hit skips LLM call
  3. review_first mode returns pending_approval status
  4. execute_pending runs the query
  5. edit_and_execute validates and runs edited query
  6. generate_only mode never executes
  7. Feedback creates / removes examples
"""

from __future__ import annotations

import contextlib
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cache.query_cache import CacheHit
from app.datasources.models import (
    DataSourceSchema,
    PrivacySettings,
    QueryResult,
    TableInfo,
    ValidationResult,
)
from app.providers.base import LLMResponse
from app.schemas.chat import ChatResponse, QueryResultsResponse
from app.semantic.models import SemanticModel
from app.services.chat_service import ChatService
from app.services.execution import _mask_sensitive_result_columns, _results_to_response
from app.services.schema_utils import (
    LARGE_TABLE_ROW_THRESHOLD,
    _check_query_complexity,
    _schema_from_dict,
    _schema_to_dict,
)

# ── Shared mock helpers ────────────────────────────────────────────────────────


class MockResult:
    """Simulates the object returned by AsyncSession.execute()."""

    def __init__(self, single=None, multiple=None, row=None):
        self._single = single
        self._multiple = multiple if multiple is not None else []
        self._row = row

    def scalar_one_or_none(self):
        return self._single

    def scalars(self):
        return self

    def all(self):
        return self._multiple

    def __iter__(self):
        return iter(self._multiple)

    def first(self):
        return self._single

    def one_or_none(self):
        return self._row

    def one(self):
        return self._row


def _make_db(*execute_returns) -> MagicMock:
    """Return an AsyncSession mock with a pre-configured execute() side_effect."""
    db = MagicMock()
    db.execute = AsyncMock(side_effect=list(execute_returns))
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    return db


def _make_settings(**overrides) -> MagicMock:
    s = MagicMock()
    s.encryption_key = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
    s.cache_enabled = True
    s.anthropic_api_key = "test-anthropic-key"
    s.openai_api_key = "test-openai-key"
    s.ollama_base_url = "http://localhost:11434"
    s.default_query_timeout = 30
    s.default_row_limit = 1000
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


_SCHEMA_CACHE_DICT = {
    "source_type": "postgresql",
    "schemas": [{"name": "public", "description": None}],
    "tables": [],
    "relationships": [],
    "metadata": {},
}


def _make_usc(schema_cache: dict | None = None) -> MagicMock:
    """Return a mock UserSchemaCache row with schema_cache populated."""
    usc = MagicMock()
    usc.schema_cache = schema_cache if schema_cache is not None else _SCHEMA_CACHE_DICT
    return usc


def _make_connection(
    *,
    execution_mode: str = "auto_execute",
    schema_cache: dict | None = None,
    semantic_model: dict | None = None,
    privacy_settings: dict | None = None,
) -> MagicMock:
    conn = MagicMock()
    conn.id = "conn-1"
    conn.source_type = "postgresql"
    conn.config_encrypted = b"encrypted-bytes"
    conn.privacy_settings = privacy_settings
    conn.execution_mode = execution_mode
    # Provide a pre-populated schema cache so tests don't need to introspect
    conn.schema_cache = schema_cache if schema_cache is not None else _SCHEMA_CACHE_DICT
    conn.semantic_model = semantic_model
    return conn


def _make_query_result(
    query: str = "SELECT COUNT(*) FROM users",
    row_count: int = 1,
) -> QueryResult:
    return QueryResult(
        columns=["count"],
        column_types=["integer"],
        rows=[[row_count]],
        row_count=row_count,
        truncated=False,
        execution_time_ms=5.0,
        bytes_scanned=None,
    )


def _make_adapter(*, execute_result: QueryResult | None = None, valid: bool = True) -> MagicMock:
    adapter = MagicMock()
    adapter.source_type = "postgresql"
    adapter.display_name = "PostgreSQL"
    adapter.query_dialect = "postgresql"
    adapter.get_system_prompt_additions = MagicMock(return_value="")
    adapter.format_schema_for_llm = MagicMock(return_value="-- DDL --")
    adapter.validate_query = MagicMock(
        return_value=ValidationResult(
            is_valid=valid,
            error_message=None if valid else "Unsafe query",
        )
    )
    adapter.execute_query = AsyncMock(return_value=execute_result or _make_query_result())
    adapter.connect = AsyncMock()
    adapter.disconnect = AsyncMock()
    adapter.introspect = AsyncMock()
    return adapter


def _make_llm_response(
    query: str = "SELECT COUNT(*) FROM users",
    explanation: str = "Count all users",
) -> LLMResponse:
    return LLMResponse(
        query=query,
        explanation=explanation,
        raw_response=f"QUERY:\n```sql\n{query}\n```\n\nEXPLANATION:\n{explanation}",
        model="test-model",
        tokens_used=42,
    )


def _make_llm_provider(llm_response: LLMResponse | None = None) -> MagicMock:
    provider = MagicMock()
    provider.generate_response = AsyncMock(return_value=llm_response or _make_llm_response())
    provider.max_output_tokens = 8192
    provider.context_window = None
    return provider


def _make_cache_hit(query: str = "SELECT COUNT(*) FROM users") -> CacheHit:
    return CacheHit(
        cached_question="How many users?",
        generated_query=query,
        query_dialect="postgresql",
        similarity_score=1.0,
        cache_type="exact",
    )


def _make_msg(
    *,
    id: str = "msg-1",
    session_id: str = "sess-1",
    role: str = "assistant",
    content: str = "Count all users",
    query_generated: str = "SELECT COUNT(*) FROM users",
    query_dialect: str = "postgresql",
    status: str = "pending_approval",
    cache_hit: bool = False,
    created_at: datetime | None = None,
) -> MagicMock:
    msg = MagicMock()
    msg.id = id
    msg.session_id = session_id
    msg.role = role
    msg.content = content
    msg.query_generated = query_generated
    msg.query_dialect = query_dialect
    msg.status = status
    msg.cache_hit = cache_hit
    msg.created_at = created_at or datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    return msg


def _make_session(*, id: str = "sess-1", connection_id: str = "conn-1") -> MagicMock:
    s = MagicMock()
    s.id = id
    s.connection_id = connection_id
    return s


# ── Context manager helpers for patching ───────────────────────────────────────

_MODULE = "app.services.chat_service"
_PIPELINE_MODULE = "app.services.pipeline"
_FACTORY_MODULE = "app.providers._factory"


def _make_null_db() -> MagicMock:
    """Return a mock AsyncSession where all execute() calls return empty results.

    Used to patch ``_create_session`` in ``pipeline`` so that ``_generate_query``'s
    internal DB reads fall through to ``None`` without hitting a real database.
    Also suitable for Session B in ``process_message`` (write-path): all execute
    calls return a null result and the commit/flush/add stubs are async-safe.
    """
    null_result = MagicMock()
    null_result.scalar_one_or_none = MagicMock(return_value=None)
    scalars_mock = MagicMock()
    scalars_mock.first = MagicMock(return_value=None)
    null_result.scalars = MagicMock(return_value=scalars_mock)
    db = MagicMock()
    db.execute = AsyncMock(return_value=null_result)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _mock_session_factory(mock_db):
    """Return a callable that acts like ``async_session_maker`` but yields *mock_db*."""

    @contextlib.asynccontextmanager
    async def _factory():
        yield mock_db

    return _factory


def _mock_two_sessions(first_db, second_db):
    """Return a factory that yields *first_db* for Session A, *second_db* for Session B.

    Used to patch ``_create_session`` in ``process_message`` tests: Session A
    (inside ``_run_shared_pipeline``) needs the pre-configured *first_db* with
    specific execute side-effects, while Session B (cache + message persistence)
    can use the null-stub *second_db*.
    """
    dbs = iter([first_db, second_db])

    @contextlib.asynccontextmanager
    async def _factory():
        yield next(dbs)

    return _factory


@contextmanager
def _patch_settings(settings):
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_MODULE}.get_settings", return_value=settings))
        stack.enter_context(patch(f"{_FACTORY_MODULE}.get_settings", return_value=settings))
        yield


def _patch_decrypt(config_dict: dict):
    return patch(f"{_MODULE}.decrypt_value", return_value=json.dumps(config_dict))


def _patch_create_datasource(adapter):
    return patch(f"{_MODULE}.create_datasource", return_value=adapter)


def _patch_create_provider(provider):
    return patch(f"{_FACTORY_MODULE}.create_provider", return_value=provider)


def _patch_pipeline_session():
    """Patch ``_create_session`` in the pipeline module so ``_generate_query``'s
    internal DB reads use a mock session instead of a real PostgreSQL connection.

    Required for tests that reach the LLM-inference path (cache miss).
    """
    return patch(
        f"{_PIPELINE_MODULE}._create_session",
        _mock_session_factory(_make_null_db()),
    )


# ── Module-level helpers ───────────────────────────────────────────────────────


class TestSchemaSerialisation:
    """Round-trip: DataSourceSchema → dict → DataSourceSchema."""

    def test_roundtrip_empty_schema(self):
        from app.datasources.models import DataSourceSchema

        schema = DataSourceSchema(source_type="postgresql")
        restored = _schema_from_dict(_schema_to_dict(schema))
        assert restored.source_type == "postgresql"
        assert restored.tables == []

    def test_roundtrip_preserves_source_type(self):
        from app.datasources.models import DataSourceSchema

        schema = DataSourceSchema(source_type="mysql")
        assert _schema_from_dict(_schema_to_dict(schema)).source_type == "mysql"


class TestSemanticModelFromDict:
    def test_empty_dict_returns_empty_model(self):
        model = SemanticModel.model_validate({})
        assert model.tables == {}
        assert model.business_metrics == []
        assert model.common_joins == []

    def test_parses_table(self):
        d = {
            "tables": {
                "public.orders": {
                    "display_name": "Orders",
                    "description": "Sales orders",
                    "default_filters": ["status != 'D'"],
                    "columns": {},
                }
            },
            "business_metrics": [],
            "common_joins": [],
        }
        model = SemanticModel.model_validate(d)
        assert "public.orders" in model.tables
        assert model.tables["public.orders"].display_name == "Orders"


class TestResultsToResponse:
    def test_maps_all_fields(self):
        qr = _make_query_result(row_count=5)
        resp = _results_to_response(qr)
        assert resp.columns == ["count"]
        assert resp.row_count == 5
        assert resp.execution_time_ms == 5.0

    def test_truncated_flag_preserved(self):
        qr = _make_query_result()
        qr.truncated = True
        assert _results_to_response(qr).truncated is True


# ── ChatService.process_message ────────────────────────────────────────────────


class TestProcessMessageFullPipeline:
    """Scenario 1: full pipeline — auto_execute, no cache hit."""

    def _make_full_db(self, conn):
        """Build the Session-A mock db for full-pipeline (LLM-path) tests."""
        return _make_db(
            MockResult(single=conn),  # select(Connection)
            MockResult(single=_make_usc()),  # select(UserSchemaCache) in _resolve_schema
            MockResult(),  # select(TableEmbeddingCache) existing keys
            MockResult(),  # ANN query in _select_relevant_tables
            MockResult(single=None),  # select(ChatSession) in _get_or_create_session
        )

    async def test_returns_chat_response(self):
        conn = _make_connection()
        adapter = _make_adapter()
        provider = _make_llm_provider()
        db = self._make_full_db(conn)
        service = ChatService(MagicMock(), MagicMock())
        service.cache.lookup = AsyncMock(return_value=None)
        service.cache.store = AsyncMock()
        service.cache.compute_embedding_async = AsyncMock(return_value=[0.1, 0.2])
        service.examples.find_similar_examples = AsyncMock(return_value=[])

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            _patch_create_provider(provider),
            _patch_pipeline_session(),
            patch(f"{_MODULE}._create_session", _mock_two_sessions(db, _make_null_db())),
        ):
            resp = await service.process_message(
                connection_id="conn-1",
                session_id=None,
                message="How many users?",
                provider_name="claude",
                options={},
            )

        assert isinstance(resp, ChatResponse)

    async def test_status_is_executed(self):
        conn = _make_connection()
        adapter = _make_adapter()
        provider = _make_llm_provider()
        db = self._make_full_db(conn)
        service = ChatService(MagicMock(), MagicMock())
        service.cache.lookup = AsyncMock(return_value=None)
        service.cache.store = AsyncMock()
        service.cache.compute_embedding_async = AsyncMock(return_value=[0.1])
        service.examples.find_similar_examples = AsyncMock(return_value=[])

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            _patch_create_provider(provider),
            _patch_pipeline_session(),
            patch(f"{_MODULE}._create_session", _mock_two_sessions(db, _make_null_db())),
        ):
            resp = await service.process_message("conn-1", None, "q?", "claude", {})

        assert resp.status == "executed"

    async def test_cache_hit_is_false(self):
        conn = _make_connection()
        adapter = _make_adapter()
        provider = _make_llm_provider()
        db = self._make_full_db(conn)
        service = ChatService(MagicMock(), MagicMock())
        service.cache.lookup = AsyncMock(return_value=None)
        service.cache.store = AsyncMock()
        service.cache.compute_embedding_async = AsyncMock(return_value=[0.1])
        service.examples.find_similar_examples = AsyncMock(return_value=[])

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            _patch_create_provider(provider),
            _patch_pipeline_session(),
            patch(f"{_MODULE}._create_session", _mock_two_sessions(db, _make_null_db())),
        ):
            resp = await service.process_message("conn-1", None, "q?", "claude", {})

        assert resp.cache_hit is False

    async def test_llm_provider_generate_response_called(self):
        conn = _make_connection()
        adapter = _make_adapter()
        provider = _make_llm_provider()
        db = self._make_full_db(conn)
        service = ChatService(MagicMock(), MagicMock())
        service.cache.lookup = AsyncMock(return_value=None)
        service.cache.store = AsyncMock()
        service.cache.compute_embedding_async = AsyncMock(return_value=[0.1])
        service.examples.find_similar_examples = AsyncMock(return_value=[])

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            _patch_create_provider(provider),
            _patch_pipeline_session(),
            patch(f"{_MODULE}._create_session", _mock_two_sessions(db, _make_null_db())),
        ):
            await service.process_message("conn-1", None, "q?", "claude", {})

        provider.generate_response.assert_called_once()

    async def test_adapter_execute_query_called_for_auto_execute(self):
        conn = _make_connection(execution_mode="auto_execute")
        adapter = _make_adapter()
        provider = _make_llm_provider()
        db = self._make_full_db(conn)
        service = ChatService(MagicMock(), MagicMock())
        service.cache.lookup = AsyncMock(return_value=None)
        service.cache.store = AsyncMock()
        service.cache.compute_embedding_async = AsyncMock(return_value=[0.1])
        service.examples.find_similar_examples = AsyncMock(return_value=[])

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            _patch_create_provider(provider),
            _patch_pipeline_session(),
            patch(f"{_MODULE}._create_session", _mock_two_sessions(db, _make_null_db())),
        ):
            await service.process_message("conn-1", None, "q?", "claude", {})

        adapter.execute_query.assert_called_once()

    async def test_response_includes_results(self):
        conn = _make_connection()
        adapter = _make_adapter(execute_result=_make_query_result(row_count=7))
        provider = _make_llm_provider()
        db = self._make_full_db(conn)
        service = ChatService(MagicMock(), MagicMock())
        service.cache.lookup = AsyncMock(return_value=None)
        service.cache.store = AsyncMock()
        service.cache.compute_embedding_async = AsyncMock(return_value=[0.1])
        service.examples.find_similar_examples = AsyncMock(return_value=[])

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            _patch_create_provider(provider),
            _patch_pipeline_session(),
            patch(f"{_MODULE}._create_session", _mock_two_sessions(db, _make_null_db())),
        ):
            resp = await service.process_message("conn-1", None, "q?", "claude", {})

        assert resp.results is not None
        assert resp.results.row_count == 7

    async def test_query_stored_in_cache_after_llm_call(self):
        conn = _make_connection()
        adapter = _make_adapter()
        provider = _make_llm_provider()
        db = self._make_full_db(conn)
        service = ChatService(MagicMock(), MagicMock())
        service.cache.lookup = AsyncMock(return_value=None)
        service.cache.store = AsyncMock()
        service.cache.compute_embedding_async = AsyncMock(return_value=[0.1])
        service.examples.find_similar_examples = AsyncMock(return_value=[])

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            _patch_create_provider(provider),
            _patch_pipeline_session(),
            patch(f"{_MODULE}._create_session", _mock_two_sessions(db, _make_null_db())),
        ):
            await service.process_message("conn-1", None, "q?", "claude", {})

        service.cache.store.assert_called_once()

    async def test_invalid_query_returns_error_status(self):
        conn = _make_connection()
        adapter = _make_adapter(valid=False)
        provider = _make_llm_provider()
        db = self._make_full_db(conn)
        service = ChatService(MagicMock(), MagicMock())
        service.cache.lookup = AsyncMock(return_value=None)
        service.cache.store = AsyncMock()
        service.cache.compute_embedding_async = AsyncMock(return_value=[0.1])
        service.examples.find_similar_examples = AsyncMock(return_value=[])

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            _patch_create_provider(provider),
            _patch_pipeline_session(),
            patch(f"{_MODULE}._create_session", _mock_two_sessions(db, _make_null_db())),
        ):
            resp = await service.process_message("conn-1", None, "q?", "claude", {})

        assert resp.status == "error"
        assert resp.error is not None
        adapter.execute_query.assert_not_called()


# ── Scenario 2: Cache hit ──────────────────────────────────────────────────────


class TestProcessMessageCacheHit:
    """Scenario 2: cache hit skips LLM call entirely."""

    async def test_cache_hit_flag_is_true(self):
        conn = _make_connection()
        adapter = _make_adapter()
        db = _make_db(
            MockResult(single=conn),
            MockResult(single=_make_usc()),
            MockResult(),
            MockResult(),
        )
        service = ChatService(MagicMock(), MagicMock())
        service.cache.lookup = AsyncMock(return_value=_make_cache_hit())
        service.cache.compute_embedding_async = AsyncMock(return_value=[0.1])

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            patch(f"{_MODULE}._create_session", _mock_two_sessions(db, _make_null_db())),
        ):
            resp = await service.process_message("conn-1", None, "q?", "claude", {})

        assert resp.cache_hit is True

    async def test_llm_provider_not_called_on_cache_hit(self):
        conn = _make_connection()
        adapter = _make_adapter()
        provider = _make_llm_provider()
        db = _make_db(
            MockResult(single=conn),
            MockResult(single=_make_usc()),
            MockResult(),
            MockResult(),
        )
        service = ChatService(MagicMock(), MagicMock())
        service.cache.lookup = AsyncMock(return_value=_make_cache_hit())
        service.cache.compute_embedding_async = AsyncMock(return_value=[0.1])

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            _patch_create_provider(provider),
            patch(f"{_MODULE}._create_session", _mock_two_sessions(db, _make_null_db())),
        ):
            await service.process_message("conn-1", None, "q?", "claude", {})

        provider.generate_response.assert_not_called()

    async def test_cached_query_returned(self):
        conn = _make_connection()
        adapter = _make_adapter()
        cached_query = "SELECT COUNT(*) FROM archived_users"
        db = _make_db(
            MockResult(single=conn),
            MockResult(single=_make_usc()),
            MockResult(),
            MockResult(),
        )
        service = ChatService(MagicMock(), MagicMock())
        service.cache.lookup = AsyncMock(return_value=_make_cache_hit(query=cached_query))
        service.cache.compute_embedding_async = AsyncMock(return_value=[0.1])

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            patch(f"{_MODULE}._create_session", _mock_two_sessions(db, _make_null_db())),
        ):
            resp = await service.process_message("conn-1", None, "q?", "claude", {})

        assert resp.query == cached_query

    async def test_status_is_cached_in_auto_execute_mode(self):
        conn = _make_connection(execution_mode="auto_execute")
        adapter = _make_adapter()
        db = _make_db(
            MockResult(single=conn),
            MockResult(single=_make_usc()),
            MockResult(),
            MockResult(),
        )
        service = ChatService(MagicMock(), MagicMock())
        service.cache.lookup = AsyncMock(return_value=_make_cache_hit())
        service.cache.compute_embedding_async = AsyncMock(return_value=[0.1])

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            patch(f"{_MODULE}._create_session", _mock_two_sessions(db, _make_null_db())),
        ):
            resp = await service.process_message("conn-1", None, "q?", "claude", {})

        assert resp.status == "cached"

    async def test_cache_disabled_still_calls_llm(self):
        conn = _make_connection()
        adapter = _make_adapter()
        provider = _make_llm_provider()
        db = _make_db(
            MockResult(single=conn),
            MockResult(single=_make_usc()),
            MockResult(),
            MockResult(),
            MockResult(single=None),
        )
        service = ChatService(MagicMock(), MagicMock())
        service.cache.lookup = AsyncMock(return_value=None)
        service.cache.store = AsyncMock()
        service.cache.compute_embedding_async = AsyncMock(return_value=[0.1])
        service.examples.find_similar_examples = AsyncMock(return_value=[])

        with (
            _patch_settings(_make_settings(cache_enabled=False)),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            _patch_create_provider(provider),
            _patch_pipeline_session(),
            patch(f"{_MODULE}._create_session", _mock_two_sessions(db, _make_null_db())),
        ):
            await service.process_message("conn-1", None, "q?", "claude", {})

        # With cache disabled, lookup is never called
        service.cache.lookup.assert_not_called()
        provider.generate_response.assert_called_once()


# ── Scenario 3: review_first mode ─────────────────────────────────────────────


class TestProcessMessageReviewFirst:
    """Scenario 3: review_first mode returns pending_approval without executing."""

    async def _run(self, execution_mode: str = "review_first") -> tuple[ChatResponse, MagicMock]:
        conn = _make_connection(execution_mode=execution_mode)
        adapter = _make_adapter()
        provider = _make_llm_provider()
        db = _make_db(
            MockResult(single=conn),
            MockResult(single=_make_usc()),
            MockResult(),
            MockResult(),
            MockResult(single=None),
        )
        service = ChatService(MagicMock(), MagicMock())
        service.cache.lookup = AsyncMock(return_value=None)
        service.cache.store = AsyncMock()
        service.cache.compute_embedding_async = AsyncMock(return_value=[0.1])
        service.examples.find_similar_examples = AsyncMock(return_value=[])

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            _patch_create_provider(provider),
            _patch_pipeline_session(),
            patch(f"{_MODULE}._create_session", _mock_two_sessions(db, _make_null_db())),
        ):
            resp = await service.process_message("conn-1", None, "q?", "claude", {})

        return resp, adapter

    async def test_status_is_pending_approval(self):
        resp, _ = await self._run()
        assert resp.status == "pending_approval"

    async def test_execute_query_not_called(self):
        _, adapter = await self._run()
        adapter.execute_query.assert_not_called()

    async def test_results_are_none(self):
        resp, _ = await self._run()
        assert resp.results is None

    async def test_query_is_returned(self):
        resp, _ = await self._run()
        assert resp.query is not None


# ── Scenario 6: generate_only mode ────────────────────────────────────────────


class TestProcessMessageGenerateOnly:
    """Scenario 6: generate_only mode never executes the query."""

    async def _run(self) -> tuple[ChatResponse, MagicMock]:
        conn = _make_connection(execution_mode="generate_only")
        adapter = _make_adapter()
        provider = _make_llm_provider()
        db = _make_db(
            MockResult(single=conn),
            MockResult(single=_make_usc()),
            MockResult(),
            MockResult(),
            MockResult(single=None),
        )
        service = ChatService(MagicMock(), MagicMock())
        service.cache.lookup = AsyncMock(return_value=None)
        service.cache.store = AsyncMock()
        service.cache.compute_embedding_async = AsyncMock(return_value=[0.1])
        service.examples.find_similar_examples = AsyncMock(return_value=[])

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            _patch_create_provider(provider),
            _patch_pipeline_session(),
            patch(f"{_MODULE}._create_session", _mock_two_sessions(db, _make_null_db())),
        ):
            resp = await service.process_message("conn-1", None, "q?", "claude", {})

        return resp, adapter

    async def test_status_is_query_only(self):
        resp, _ = await self._run()
        assert resp.status == "query_only"

    async def test_execute_query_never_called(self):
        _, adapter = await self._run()
        adapter.execute_query.assert_not_called()

    async def test_results_are_none(self):
        resp, _ = await self._run()
        assert resp.results is None


# ── Scenario 4: execute_pending ───────────────────────────────────────────────


class TestExecutePending:
    """Scenario 4: execute_pending runs the query for a pending_approval message."""

    async def _run(self, *, msg_status="pending_approval") -> tuple[ChatResponse, MagicMock]:
        msg = _make_msg(status=msg_status)
        session = _make_session()
        conn = _make_connection()
        adapter = _make_adapter(execute_result=_make_query_result(row_count=3))
        db = _make_db(
            MockResult(row=(msg, session, conn)),  # JOIN: ChatMessage + ChatSession + Connection
            MockResult(),  # update(ChatMessage)
        )
        service = ChatService(MagicMock(), MagicMock())

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
        ):
            resp = await service.execute_pending(message_id="msg-1", db=db)

        return resp, adapter

    async def test_returns_executed_status(self):
        resp, _ = await self._run()
        assert resp.status == "executed"

    async def test_execute_query_called(self):
        _, adapter = await self._run()
        adapter.execute_query.assert_called_once()

    async def test_results_populated(self):
        resp, _ = await self._run()
        assert resp.results is not None
        assert resp.results.row_count == 3

    async def test_raises_if_not_pending_approval(self):
        service = ChatService(MagicMock(), MagicMock())
        msg = _make_msg(status="executed")
        session = _make_session()
        conn = _make_connection()
        db = _make_db(MockResult(row=(msg, session, conn)))
        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(_make_adapter()),
            pytest.raises(ValueError, match="not pending approval"),
        ):
            await service.execute_pending(message_id="msg-1", db=db)

    async def test_raises_if_message_not_found(self):
        service = ChatService(MagicMock(), MagicMock())
        db = _make_db(MockResult(row=None))

        with pytest.raises(ValueError, match="not found"):
            await service.execute_pending(message_id="ghost", db=db)


# ── Scenario 5: edit_and_execute ──────────────────────────────────────────────


class TestEditAndExecute:
    """Scenario 5: edit_and_execute validates and runs a user-edited query."""

    async def _run(self, *, valid: bool = True) -> tuple[ChatResponse, MagicMock]:
        msg = _make_msg(status="pending_approval")
        session = _make_session()
        conn = _make_connection()
        adapter = _make_adapter(valid=valid)
        db = _make_db(
            MockResult(row=(msg, session, conn)),  # JOIN: ChatMessage + ChatSession + Connection
            MockResult(),  # update(ChatMessage) — only reached if valid
        )
        service = ChatService(MagicMock(), MagicMock())

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
        ):
            resp = await service.edit_and_execute(
                message_id="msg-1",
                edited_query="SELECT * FROM users LIMIT 10",
                db=db,
            )

        return resp, adapter

    async def test_valid_query_returns_executed_status(self):
        resp, _ = await self._run(valid=True)
        assert resp.status == "executed"

    async def test_valid_query_calls_execute(self):
        _, adapter = await self._run(valid=True)
        adapter.execute_query.assert_called_once_with(
            "SELECT * FROM users LIMIT 10",
            timeout=30,
            max_rows=1000,
        )

    async def test_invalid_query_returns_error_status(self):
        resp, _ = await self._run(valid=False)
        assert resp.status == "error"
        assert resp.error is not None

    async def test_invalid_query_does_not_call_execute(self):
        _, adapter = await self._run(valid=False)
        adapter.execute_query.assert_not_called()

    async def test_raises_if_message_not_found(self):
        service = ChatService(MagicMock(), MagicMock())
        db = _make_db(MockResult(row=None))
        with pytest.raises(ValueError, match="not found"):
            await service.edit_and_execute("ghost", "SELECT 1", db)


# ── Scenario 7: submit_feedback ───────────────────────────────────────────────


class TestSubmitFeedback:
    """Scenario 7: feedback adds examples (thumbs_up) or purges cache (thumbs_down)."""

    def _make_user_msg(self) -> MagicMock:
        return _make_msg(
            id="user-msg-1",
            role="user",
            content="How many users?",
            query_generated=None,
            status="executed",
        )

    async def test_thumbs_up_adds_to_example_library(self):
        msg = _make_msg()
        session = _make_session()
        user_msg = self._make_user_msg()
        db = _make_db(
            MockResult(row=(msg, session)),  # JOIN: ChatMessage + ChatSession
            MockResult(single=user_msg),  # select(ChatMessage) — preceding user msg
        )
        service = ChatService(MagicMock(), MagicMock())
        service.cache.compute_embedding_async = AsyncMock(return_value=[0.1, 0.2])
        service.examples.add_example = AsyncMock()

        await service.submit_feedback(message_id="msg-1", feedback="thumbs_up", db=db)

        service.examples.add_example.assert_called_once()
        call_kwargs = service.examples.add_example.call_args
        assert call_kwargs.kwargs["question"] == "How many users?"
        assert call_kwargs.kwargs["query"] == msg.query_generated

    async def test_thumbs_down_deletes_from_cache(self):
        msg = _make_msg()
        session = _make_session()
        user_msg = self._make_user_msg()
        db = _make_db(
            MockResult(row=(msg, session)),  # JOIN: ChatMessage + ChatSession
            MockResult(single=user_msg),  # preceding user msg
            MockResult(),  # delete(QueryCacheEntry)
        )
        service = ChatService(MagicMock(), MagicMock())
        service.cache.compute_embedding_async = AsyncMock(return_value=[0.1])
        service.cache.evict_similar = AsyncMock(return_value=1)

        await service.submit_feedback(message_id="msg-1", feedback="thumbs_down", db=db)

        service.cache.evict_similar.assert_called_once()

    async def test_thumbs_up_does_not_delete_from_cache(self):
        msg = _make_msg()
        session = _make_session()
        user_msg = self._make_user_msg()
        db = _make_db(
            MockResult(row=(msg, session)),  # JOIN: ChatMessage + ChatSession
            MockResult(single=user_msg),  # preceding user msg
        )
        service = ChatService(MagicMock(), MagicMock())
        service.cache.compute_embedding_async = AsyncMock(return_value=[0.1])
        service.examples.add_example = AsyncMock()

        await service.submit_feedback(message_id="msg-1", feedback="thumbs_up", db=db)

        # 2 execute calls (JOIN + user_msg) — no DELETE
        assert db.execute.call_count == 2

    async def test_thumbs_down_does_not_add_to_examples(self):
        msg = _make_msg()
        session = _make_session()
        user_msg = self._make_user_msg()
        db = _make_db(
            MockResult(row=(msg, session)),  # JOIN: ChatMessage + ChatSession
            MockResult(single=user_msg),  # preceding user msg
            MockResult(),
        )
        service = ChatService(MagicMock(), MagicMock())
        service.cache.compute_embedding_async = AsyncMock(return_value=[0.1])
        service.cache.evict_similar = AsyncMock(return_value=0)
        service.examples.add_example = AsyncMock()

        await service.submit_feedback(message_id="msg-1", feedback="thumbs_down", db=db)

        service.examples.add_example.assert_not_called()

    async def test_raises_if_message_not_found(self):
        service = ChatService(MagicMock(), MagicMock())
        db = _make_db(MockResult(row=None))
        with pytest.raises(ValueError, match="not found"):
            await service.submit_feedback("ghost", "thumbs_up", db)


# ── IDOR ownership tests ──────────────────────────────────────────────────────


class TestExecutePendingOwnership:
    """C-1: execute_pending must reject messages belonging to another user's session."""

    async def test_raises_when_session_belongs_to_different_user(self):
        msg = _make_msg(status="pending_approval")
        # Session is owned by a *different* user
        session = _make_session()
        session.user_id = "other-user"
        conn = _make_connection()
        db = _make_db(MockResult(row=(msg, session, conn)))
        service = ChatService(MagicMock(), MagicMock())
        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(_make_adapter()),
            pytest.raises(ValueError, match="not found"),
        ):
            await service.execute_pending(message_id="msg-1", db=db, user_id="current-user")

    async def test_succeeds_when_session_belongs_to_same_user(self):
        msg = _make_msg(status="pending_approval")
        session = _make_session()
        session.user_id = "current-user"
        conn = _make_connection()
        adapter = _make_adapter(execute_result=_make_query_result())
        db = _make_db(
            MockResult(row=(msg, session, conn)),
            MockResult(),
        )
        service = ChatService(MagicMock(), MagicMock())
        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
        ):
            resp = await service.execute_pending(message_id="msg-1", db=db, user_id="current-user")
        assert resp.status == "executed"

    async def test_skips_ownership_check_when_user_id_is_none(self):
        """No user_id supplied (e.g. legacy call) — check is skipped, not errored."""
        msg = _make_msg(status="pending_approval")
        session = _make_session()
        session.user_id = "some-user"
        conn = _make_connection()
        adapter = _make_adapter(execute_result=_make_query_result())
        db = _make_db(
            MockResult(row=(msg, session, conn)),
            MockResult(),
        )
        service = ChatService(MagicMock(), MagicMock())
        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
        ):
            resp = await service.execute_pending(message_id="msg-1", db=db, user_id=None)
        assert resp.status == "executed"


class TestEditAndExecuteOwnership:
    """C-2: edit_and_execute must reject messages belonging to another user's session."""

    async def test_raises_when_session_belongs_to_different_user(self):
        msg = _make_msg(status="pending_approval")
        session = _make_session()
        session.user_id = "other-user"
        conn = _make_connection()
        db = _make_db(MockResult(row=(msg, session, conn)))
        service = ChatService(MagicMock(), MagicMock())
        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(_make_adapter()),
            pytest.raises(ValueError, match="not found"),
        ):
            await service.edit_and_execute(
                message_id="msg-1",
                edited_query="SELECT 1",
                db=db,
                user_id="current-user",
            )

    async def test_succeeds_when_session_belongs_to_same_user(self):
        msg = _make_msg(status="pending_approval")
        session = _make_session()
        session.user_id = "current-user"
        conn = _make_connection()
        adapter = _make_adapter(valid=True, execute_result=_make_query_result())
        db = _make_db(
            MockResult(row=(msg, session, conn)),
            MockResult(),
        )
        service = ChatService(MagicMock(), MagicMock())
        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
        ):
            resp = await service.edit_and_execute(
                message_id="msg-1",
                edited_query="SELECT 1",
                db=db,
                user_id="current-user",
            )
        assert resp.status == "executed"


class TestGetOrCreateSessionOwnership:
    """H-1: _get_or_create_session must not return sessions owned by other users."""

    async def test_does_not_return_session_owned_by_different_user(self):
        from app.services.chat_service import _get_or_create_session

        # DB returns None (session not found for this user — WHERE filters it out)
        db = _make_db(
            MockResult(single=None),  # select(ChatSession) — no match
        )
        session = await _get_or_create_session(
            session_id="sess-other",
            connection_id="conn-1",
            provider_name="claude",
            title="New session",
            db=db,
            user_id="current-user",
        )
        # Should have created a new session, not returned the other user's
        assert session.id != "sess-other"
        db.flush.assert_called_once()

    async def test_returns_session_owned_by_same_user(self):
        from app.services.chat_service import _get_or_create_session

        owned_session = _make_session(id="sess-mine")
        owned_session.user_id = "current-user"
        db = _make_db(MockResult(single=owned_session))

        session = await _get_or_create_session(
            session_id="sess-mine",
            connection_id="conn-1",
            provider_name="claude",
            title="My session",
            db=db,
            user_id="current-user",
        )
        assert session.id == "sess-mine"
        db.flush.assert_not_called()


# ── _check_query_complexity ────────────────────────────────────────────────────


def _make_schema(table_name: str, row_count: int | None) -> DataSourceSchema:
    return DataSourceSchema(
        source_type="postgresql",
        tables=[
            TableInfo(
                catalog=None,
                schema_name="public",
                name=table_name,
                table_type="table",
                row_count_approx=row_count,
            )
        ],
    )


class TestCheckQueryComplexity:
    def test_cross_join_rejected(self):
        assert _check_query_complexity("SELECT * FROM a CROSS JOIN b") is not None

    def test_cross_join_case_insensitive(self):
        assert _check_query_complexity("select * from a cross join b") is not None

    def test_inner_join_allowed(self):
        assert _check_query_complexity("SELECT * FROM a INNER JOIN b ON a.id = b.id") is None

    def test_left_join_allowed(self):
        assert _check_query_complexity("SELECT * FROM a LEFT JOIN b ON a.id = b.id") is None

    def test_large_table_no_where_rejected(self):
        schema = _make_schema("orders", LARGE_TABLE_ROW_THRESHOLD + 1)
        assert _check_query_complexity("SELECT * FROM orders", schema) is not None

    def test_large_table_exactly_at_threshold_allowed(self):
        schema = _make_schema("orders", LARGE_TABLE_ROW_THRESHOLD)
        assert _check_query_complexity("SELECT * FROM orders", schema) is None

    def test_large_table_with_where_allowed(self):
        schema = _make_schema("orders", LARGE_TABLE_ROW_THRESHOLD + 1)
        assert _check_query_complexity("SELECT * FROM orders WHERE id = 1", schema) is None

    def test_small_table_no_where_allowed(self):
        schema = _make_schema("configs", 50)
        assert _check_query_complexity("SELECT * FROM configs", schema) is None

    def test_no_schema_skips_row_count_check(self):
        assert _check_query_complexity("SELECT * FROM orders") is None

    def test_table_with_null_row_count_allowed(self):
        schema = _make_schema("orders", None)
        assert _check_query_complexity("SELECT * FROM orders", schema) is None


# ── _mask_sensitive_result_columns ────────────────────────────────────────────


def _make_response(columns: list[str], rows: list[list]) -> QueryResultsResponse:
    return QueryResultsResponse(
        columns=columns,
        column_types=["text"] * len(columns),
        rows=rows,
        row_count=len(rows),
        truncated=False,
        execution_time_ms=0.0,
    )


class TestMaskSensitiveResultColumns:
    def test_no_privacy_returns_same_object(self):
        resp = _make_response(["id", "ssn"], [[1, "secret"]])
        assert _mask_sensitive_result_columns(resp, None) is resp

    def test_masks_sensitive_pattern_column(self):
        privacy = PrivacySettings()
        resp = _make_response(["id", "email"], [[1, "user@example.com"]])
        masked = _mask_sensitive_result_columns(resp, privacy)
        assert masked.rows[0][1] == "[REDACTED]"
        assert masked.rows[0][0] == 1

    def test_masks_excluded_column_by_bare_name(self):
        privacy = PrivacySettings(excluded_columns=["public.users.internal_code"])
        resp = _make_response(["id", "internal_code"], [[1, "ABC-123"]])
        masked = _mask_sensitive_result_columns(resp, privacy)
        assert masked.rows[0][1] == "[REDACTED]"
        assert masked.rows[0][0] == 1

    def test_excluded_column_match_is_case_insensitive(self):
        privacy = PrivacySettings(excluded_columns=["public.users.InternalCode"])
        resp = _make_response(["id", "internalcode"], [[1, "ABC-123"]])
        masked = _mask_sensitive_result_columns(resp, privacy)
        assert masked.rows[0][1] == "[REDACTED]"

    def test_non_excluded_column_not_masked(self):
        privacy = PrivacySettings(excluded_columns=["public.users.secret"])
        resp = _make_response(["id", "name"], [[1, "Alice"]])
        masked = _mask_sensitive_result_columns(resp, privacy)
        assert masked.rows[0] == [1, "Alice"]

    def test_no_sensitive_columns_returns_same_object(self):
        privacy = PrivacySettings()
        resp = _make_response(["id", "name"], [[1, "Alice"]])
        result = _mask_sensitive_result_columns(resp, privacy)
        assert result is resp
