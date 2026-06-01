# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for the TPM-limit warning added to _generate_query in pipeline.py.

Covers:
- tpm_warning is set when a [TPM_EXCEEDED] retry succeeds.
- tpm_warning is None when the TPM retry also fails (error path unchanged).
- tpm_warning is None for normal (non-TPM) requests.
- No UnboundLocalError when a cache hit short-circuits the LLM path (bug fix).
- DoneEvent carries a warning field (schema completeness check).
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

from app.providers.base import LLMResponse
from app.schemas.sse import DoneEvent
from app.services.pipeline import _generate_query

_PIPELINE = "app.services.pipeline"


# ── Helpers ────────────────────────────────────────────────────────────────────


def _mock_session_factory(mock_db: MagicMock):
    @contextlib.asynccontextmanager
    async def _factory():
        yield mock_db

    return _factory


def _make_db(*, cache_result=None, examples=None, history=None, provider=None) -> MagicMock:
    """AsyncSession that returns controlled values for each pipeline DB read."""

    class _R:
        def __init__(self, v):
            self._v = v

        def scalar_one_or_none(self):
            return self._v

        def scalars(self):
            return self

        def all(self):
            return self._v if self._v is not None else []

        def __iter__(self):
            return iter(self._v if self._v is not None else [])

        def first(self):
            return self._v

    db = MagicMock()
    db.execute = AsyncMock(return_value=_R(None))
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _make_settings(*, cache_enabled: bool = False) -> MagicMock:
    s = MagicMock()
    s.cache_enabled = cache_enabled
    s.default_query_timeout = 30
    s.default_row_limit = 1000
    s.schema_pruning_enabled = False
    return s


def _make_llm_response(query: str = "SELECT 1") -> LLMResponse:
    return LLMResponse(
        query=query,
        explanation="ok",
        raw_response=f"```sql\n{query}\n```\nok",
        model="gpt-4o",
        tokens_used=10,
        input_tokens=8,
        output_tokens=2,
        truncated=False,
    )


def _make_provider(*, side_effects) -> MagicMock:
    """LLM provider whose generate_response raises/returns in sequence."""
    p = MagicMock()
    p.provider_name = "openai"
    p.context_window = None
    p.chars_per_token = 4.0
    p.max_output_tokens = 4096
    p.generate_response = AsyncMock(side_effect=side_effects)
    return p


def _make_adapter() -> MagicMock:
    a = MagicMock()
    a.query_dialect = "postgresql"
    a.format_schema_for_llm = MagicMock(return_value="schema")
    a.validate_query = MagicMock(return_value=MagicMock(is_valid=True))
    return a


def _base_patches(provider: MagicMock, db: MagicMock):
    """Return the context-manager stack needed for every _generate_query call."""
    return [
        patch(f"{_PIPELINE}._create_session", _mock_session_factory(db)),
        patch(
            f"{_PIPELINE}._build_provider",
            new=AsyncMock(return_value=(provider, "gpt-4o", 4096)),
        ),
        patch(f"{_PIPELINE}._load_history", new=AsyncMock(return_value=[])),
        patch(
            f"{_PIPELINE}.PromptBuilder",
            return_value=MagicMock(build_system_prompt=MagicMock(return_value="sys-prompt")),
        ),
    ]


async def _call_generate(provider: MagicMock, db: MagicMock, **kwargs):
    cache = MagicMock()
    cache.lookup = AsyncMock(return_value=None)
    cache.compute_embedding_async = AsyncMock(return_value=[0.1])
    examples = MagicMock()
    examples.find_similar_examples = AsyncMock(return_value=[])
    adapter = _make_adapter()
    schema = MagicMock()
    settings = _make_settings()

    with contextlib.ExitStack() as stack:
        for p in _base_patches(provider, db):
            stack.enter_context(p)
        return await _generate_query(
            cache=cache,
            examples=examples,
            connection_id="conn-1",
            message="how many rows?",
            session_id=None,
            adapter=adapter,
            schema=schema,
            privacy=None,
            semantic_model=None,
            provider_name="openai",
            options={},
            settings=settings,
            bypass_cache=True,
            **kwargs,
        )


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestTpmWarning:
    async def test_tpm_warning_set_when_retry_succeeds(self):
        """[TPM_EXCEEDED] on first call, success on retry → tpm_warning is set."""
        tpm_error = ValueError("[TPM_EXCEEDED] Request too large: token rate limit exceeded.")
        provider = _make_provider(side_effects=[tpm_error, _make_llm_response()])
        db = _make_db()

        result = await _call_generate(provider, db)

        assert result.tpm_warning is not None
        assert "token" in result.tpm_warning.lower() or "TPM" in result.tpm_warning
        assert result.error is None
        assert result.generated_query == "SELECT 1"

    async def test_tpm_warning_none_when_retry_also_fails(self):
        """[TPM_EXCEEDED] on both calls → no tpm_warning, error is set instead."""
        tpm_error = ValueError("[TPM_EXCEEDED] Request too large: token rate limit exceeded.")
        provider = _make_provider(side_effects=[tpm_error, tpm_error])
        db = _make_db()

        result = await _call_generate(provider, db)

        assert result.tpm_warning is None
        assert result.error is not None
        assert result.status == "error"

    async def test_tpm_retry_budget_is_smaller_than_original_prompt(self):
        """Budget passed to _compress_prompt on TPM retry must be < current prompt length.

        Regression test for the bug where tpm_budget could exceed the current prompt
        length, causing _compress_prompt to return the full prompt unchanged and the
        retry to send an identical payload (guaranteed 413 again).
        """
        tpm_error = ValueError("[TPM_EXCEEDED] Request too large: token rate limit exceeded.")
        provider = _make_provider(side_effects=[tpm_error, _make_llm_response()])
        db = _make_db()

        captured_budget: list[int] = []

        def capturing_compress(**kwargs):
            captured_budget.append(kwargs["budget_chars"])
            return "compressed-prompt"

        with patch(f"{_PIPELINE}._compress_prompt", side_effect=capturing_compress):
            result = await _call_generate(provider, db)

        # _compress_prompt called exactly once (TPM retry); pre-emptive path skipped
        # because context_window=None on the mock provider.
        assert len(captured_budget) == 1
        # PromptBuilder mock returns "sys-prompt" (10 chars); message is "how many rows?" (14).
        original_len = len("sys-prompt") + len("how many rows?")
        assert captured_budget[0] < original_len
        assert result.tpm_warning is not None

    async def test_tpm_warning_none_for_normal_request(self):
        """No TPM error → tpm_warning is None."""
        provider = _make_provider(side_effects=[_make_llm_response()])
        db = _make_db()

        result = await _call_generate(provider, db)

        assert result.tpm_warning is None
        assert result.error is None

    async def test_no_unbound_error_on_cache_hit(self):
        """Cache hit path must not raise UnboundLocalError for _tpm_hit."""
        cache_entry = MagicMock()
        cache_entry.generated_query = "SELECT cached"
        cache_entry.cached_question = "how many rows?"

        cache = MagicMock()
        cache.lookup = AsyncMock(return_value=cache_entry)
        cache.compute_embedding_async = AsyncMock(return_value=[0.1])
        examples = MagicMock()
        examples.find_similar_examples = AsyncMock(return_value=[])
        adapter = _make_adapter()
        # validate_query called on cached query — must be valid to keep the cache hit
        adapter.validate_query = MagicMock(return_value=MagicMock(is_valid=True))
        schema = MagicMock()
        settings = _make_settings(cache_enabled=True)
        provider = _make_provider(side_effects=[])  # should never be called

        db = _make_db()

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch(f"{_PIPELINE}._create_session", _mock_session_factory(db)))
            stack.enter_context(
                patch(
                    f"{_PIPELINE}._build_provider",
                    new=AsyncMock(return_value=(provider, "gpt-4o", 4096)),
                )
            )
            stack.enter_context(patch(f"{_PIPELINE}._load_history", new=AsyncMock(return_value=[])))
            stack.enter_context(
                patch(
                    f"{_PIPELINE}.PromptBuilder",
                    return_value=MagicMock(
                        build_system_prompt=MagicMock(return_value="sys-prompt")
                    ),
                )
            )
            result = await _generate_query(
                cache=cache,
                examples=examples,
                connection_id="conn-1",
                message="how many rows?",
                session_id=None,
                adapter=adapter,
                schema=schema,
                privacy=None,
                semantic_model=None,
                provider_name="openai",
                options={},
                settings=settings,
                bypass_cache=False,
            )

        assert result.cache_hit is True
        assert result.generated_query == "SELECT cached"
        assert result.tpm_warning is None  # no TPM hit on cache path


class TestDoneEventSchema:
    def test_done_event_has_warning_field(self):
        """DoneEvent TypedDict must include a warning field — schema completeness."""
        event: DoneEvent = {
            "type": "done",
            "session_id": "s1",
            "message_id": "m1",
            "execution_time_ms": 100.0,
            "cache_hit": False,
            "status": "executed",
            "token_count": 10,
            "input_tokens": 8,
            "output_tokens": 2,
            "warning": "TPM limit hit",
        }
        assert event["warning"] == "TPM limit hit"

    def test_done_event_warning_accepts_none(self):
        """warning field can be None (normal path)."""
        event: DoneEvent = {
            "type": "done",
            "session_id": "s1",
            "message_id": "m1",
            "execution_time_ms": None,
            "cache_hit": False,
            "status": "error",
            "token_count": None,
            "input_tokens": None,
            "output_tokens": None,
            "warning": None,
        }
        assert event["warning"] is None
