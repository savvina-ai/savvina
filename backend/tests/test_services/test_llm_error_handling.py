# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests that LLM provider errors are caught and returned as error-status ChatResponse.

Covers the try/except guard added around _build_provider() + generate_response() in
chat_service.process_message() to prevent openai.RateLimitError (and any other provider
exception) from bubbling up as an unhandled HTTP 500.
"""

from __future__ import annotations

import contextlib
from contextlib import ExitStack, contextmanager
import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.schemas.chat import ChatResponse
from app.services.chat_service import ChatService

# ── Reuse the same helpers as test_chat_service ──────────────────────────────


class MockResult:
    """Simulates the object returned by AsyncSession.execute()."""

    def __init__(self, single=None, multiple=None):
        self._single = single
        self._multiple = multiple if multiple is not None else []

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


def _make_db(*execute_returns) -> MagicMock:
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


def _make_connection(*, execution_mode: str = "auto_execute") -> MagicMock:
    conn = MagicMock()
    conn.id = "conn-1"
    conn.source_type = "postgresql"
    conn.config_encrypted = b"encrypted-bytes"
    conn.privacy_settings = None
    conn.execution_mode = execution_mode
    conn.schema_cache = _SCHEMA_CACHE_DICT
    conn.semantic_model = None
    return conn


def _make_adapter() -> MagicMock:
    from app.datasources.models import ValidationResult

    adapter = MagicMock()
    adapter.source_type = "postgresql"
    adapter.display_name = "PostgreSQL"
    adapter.query_dialect = "postgresql"
    adapter.get_system_prompt_additions = MagicMock(return_value="")
    adapter.format_schema_for_llm = MagicMock(return_value="-- DDL --")
    adapter.validate_query = MagicMock(
        return_value=ValidationResult(is_valid=True, error_message=None)
    )
    adapter.execute_query = AsyncMock()
    adapter.connect = AsyncMock()
    adapter.disconnect = AsyncMock()
    adapter.introspect = AsyncMock()
    return adapter


def _make_service() -> ChatService:
    service = ChatService(MagicMock(), MagicMock())
    service.cache.lookup = AsyncMock(return_value=None)  # cache miss
    service.cache.store = AsyncMock()
    service.cache.compute_embedding_async = AsyncMock(return_value=[0.0])
    service.examples.find_similar_examples = AsyncMock(return_value=[])
    return service


_MODULE = "app.services.chat_service"
_PIPELINE_MODULE = "app.services.pipeline"
_FACTORY_MODULE = "app.providers._factory"


def _make_null_db() -> MagicMock:
    """Return a mock AsyncSession where all execute() calls return empty results.

    Used to patch ``_create_session`` in ``pipeline`` so that ``_generate_query``'s
    internal DB reads (cache lookup, examples, history, provider config) fall through
    to ``None`` / empty without hitting a real database.
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
    """Return a factory that yields *first_db* for Session A, *second_db* for Session B."""
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


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestLLMErrorHandling:
    """Verify that exceptions from the LLM provider are caught and returned
    as ChatResponse(status='error') rather than propagating as HTTP 500."""

    async def test_generic_exception_returns_error_status(self):
        """Any Exception from generate_response must yield status='error'."""
        conn = _make_connection()
        adapter = _make_adapter()
        failing_provider = MagicMock()
        failing_provider.max_output_tokens = 8192
        failing_provider.context_window = None
        failing_provider.generate_response = AsyncMock(
            side_effect=Exception("Something went wrong")
        )
        db = _make_db(
            MockResult(single=conn),
            MockResult(single=_make_usc()),
            MockResult(),
            MockResult(),
            MockResult(single=None),
        )
        service = _make_service()

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            _patch_create_provider(failing_provider),
            patch(f"{_PIPELINE_MODULE}._create_session", _mock_session_factory(_make_null_db())),
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
        assert resp.status == "error"

    async def test_error_message_forwarded_not_overwritten(self):
        """The provider error message must be forwarded in resp.error,
        not replaced by 'No query was generated'."""
        conn = _make_connection()
        adapter = _make_adapter()
        failing_provider = MagicMock()
        failing_provider.max_output_tokens = 8192
        failing_provider.context_window = None
        failing_provider.generate_response = AsyncMock(
            side_effect=Exception("Rate limit reached for model gpt-4o")
        )
        db = _make_db(
            MockResult(single=conn),
            MockResult(single=_make_usc()),
            MockResult(),
            MockResult(),
            MockResult(single=None),
        )
        service = _make_service()

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            _patch_create_provider(failing_provider),
            patch(f"{_PIPELINE_MODULE}._create_session", _mock_session_factory(_make_null_db())),
            patch(f"{_MODULE}._create_session", _mock_two_sessions(db, _make_null_db())),
        ):
            resp = await service.process_message(
                connection_id="conn-1",
                session_id=None,
                message="How many users?",
                provider_name="openai",
                options={},
            )

        assert resp.error is not None
        assert "Rate limit" in resp.error
        assert resp.error != "No query was generated"

    async def test_execute_query_not_called_on_llm_error(self):
        """The adapter must NOT execute a query when the LLM call fails."""
        conn = _make_connection()
        adapter = _make_adapter()
        failing_provider = MagicMock()
        failing_provider.max_output_tokens = 8192
        failing_provider.context_window = None
        failing_provider.generate_response = AsyncMock(
            side_effect=Exception("Auth error: invalid API key")
        )
        db = _make_db(
            MockResult(single=conn),
            MockResult(single=_make_usc()),
            MockResult(),
            MockResult(),
            MockResult(single=None),
        )
        service = _make_service()

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            _patch_create_provider(failing_provider),
            patch(f"{_PIPELINE_MODULE}._create_session", _mock_session_factory(_make_null_db())),
            patch(f"{_MODULE}._create_session", _mock_two_sessions(db, _make_null_db())),
        ):
            await service.process_message(
                connection_id="conn-1",
                session_id=None,
                message="How many users?",
                provider_name="openai",
                options={},
            )

        adapter.execute_query.assert_not_called()

    async def test_cache_store_not_called_on_llm_error(self):
        """Cache must NOT be populated when the LLM call fails — no query to cache."""
        conn = _make_connection()
        adapter = _make_adapter()
        failing_provider = MagicMock()
        failing_provider.max_output_tokens = 8192
        failing_provider.context_window = None
        failing_provider.generate_response = AsyncMock(side_effect=Exception("Model overloaded"))
        db = _make_db(
            MockResult(single=conn),
            MockResult(single=_make_usc()),
            MockResult(),
            MockResult(),
            MockResult(single=None),
        )
        service = _make_service()

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            _patch_create_provider(failing_provider),
            patch(f"{_PIPELINE_MODULE}._create_session", _mock_session_factory(_make_null_db())),
            patch(f"{_MODULE}._create_session", _mock_two_sessions(db, _make_null_db())),
        ):
            await service.process_message(
                connection_id="conn-1",
                session_id=None,
                message="How many users?",
                provider_name="groq",
                options={},
            )

        service.cache.store.assert_not_called()

    async def test_connection_not_found_raises_value_error(self):
        """Missing connection must raise ValueError (pre-existing behaviour, not changed)."""
        import pytest

        db = _make_db(MockResult(single=None))  # connection not found
        service = _make_service()

        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            patch(f"{_MODULE}.create_datasource"),
            patch(f"{_FACTORY_MODULE}.create_provider"),
            patch(f"{_MODULE}._create_session", _mock_session_factory(db)),
            pytest.raises(ValueError, match="conn-missing"),
        ):
            await service.process_message(
                connection_id="conn-missing",
                session_id=None,
                message="q?",
                provider_name="claude",
                options={},
            )

    async def test_build_provider_exception_returns_error_status(self):
        """Exception raised during _build_provider itself is also caught."""
        conn = _make_connection()
        adapter = _make_adapter()
        db = _make_db(
            MockResult(single=conn),
            MockResult(single=_make_usc()),
            MockResult(),
            MockResult(),
            MockResult(single=None),
        )
        service = _make_service()

        # Patch create_provider (called inside _build_provider) to raise
        with (
            _patch_settings(_make_settings()),
            _patch_decrypt({"host": "localhost"}),
            _patch_create_datasource(adapter),
            patch(
                f"{_FACTORY_MODULE}.create_provider",
                side_effect=ValueError("Unknown provider"),
            ),
            patch(f"{_PIPELINE_MODULE}._create_session", _mock_session_factory(_make_null_db())),
            patch(f"{_MODULE}._create_session", _mock_two_sessions(db, _make_null_db())),
        ):
            resp = await service.process_message(
                connection_id="conn-1",
                session_id=None,
                message="How many users?",
                provider_name="unknown-provider",
                options={},
            )

        assert resp.status == "error"
