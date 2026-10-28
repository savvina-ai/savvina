# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Stored SQL reaches the LLM (history, few-shot examples) and a cache hit without the
row-filter wrap the pre-Task-9 non-streaming path stored around it."""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

from app.cache.example_library import ExampleEntry
from app.cache.query_cache import CacheHit
from app.providers.base import LLMResponse
from app.services.pipeline import _generate_query, _load_history

_PIPELINE = "app.services.pipeline"
_CLEAN = "SELECT id, tenant_id FROM users"
_OLD_WRAPPED = "SELECT * FROM (SELECT id, tenant_id FROM users) AS _sift_q WHERE tenant_id = 7"
_NEW_WRAPPED = (
    "SELECT * FROM (\nSELECT id, tenant_id FROM users\n) AS _sift_q WHERE tenant_id = 7 LIMIT 5"
)


def _message(role: str, content: str, query: str | None = None) -> MagicMock:
    m = MagicMock()
    m.role = role
    m.content = content
    m.query_generated = query
    return m


class TestLoadHistory:
    async def test_stored_wrapped_query_reaches_the_llm_unwrapped(self) -> None:
        # The query orders newest first; _load_history reverses.
        messages = [
            _message("assistant", "Here they are", _NEW_WRAPPED),
            _message("user", "and the first five?"),
            _message("assistant", "All users", _OLD_WRAPPED),
            _message("user", "list users"),
        ]
        result = MagicMock()
        result.scalars.return_value.all.return_value = messages
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)

        history = await _load_history("sess-1", db)

        assert [h["role"] for h in history] == ["user", "assistant", "user", "assistant"]
        assert history[1]["content"] == f"QUERY:\n```sql\n{_CLEAN}\n```\n\nEXPLANATION:\nAll users"
        assert history[3]["content"] == (
            f"QUERY:\n```sql\n{_CLEAN} LIMIT 5\n```\n\nEXPLANATION:\nHere they are"
        )
        joined = "\n".join(h["content"] for h in history)
        assert "_sift_q" not in joined
        assert "tenant_id = 7" not in joined


def _session_factory(db: MagicMock):
    @contextlib.asynccontextmanager
    async def _factory():
        yield db

    return _factory


def _adapter() -> MagicMock:
    a = MagicMock()
    a.query_dialect = "postgresql"
    a.validate_query = MagicMock(return_value=MagicMock(is_valid=True))
    return a


def _settings() -> MagicMock:
    s = MagicMock()
    s.cache_enabled = True
    return s


async def _generate(cache: MagicMock, examples: MagicMock, adapter: MagicMock, builder=None):
    provider = MagicMock()
    provider.provider_name = "openai"
    provider.context_window = None
    provider.max_output_tokens = 4096
    provider.generate_response = AsyncMock(
        return_value=LLMResponse(
            query="SELECT 1", explanation="ok", raw_response="", model="m", tokens_used=1
        )
    )
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch(f"{_PIPELINE}._create_session", _session_factory(MagicMock())))
        stack.enter_context(
            patch(
                f"{_PIPELINE}._build_provider",
                new=AsyncMock(return_value=(provider, "m", 4096)),
            )
        )
        stack.enter_context(patch(f"{_PIPELINE}._load_history", new=AsyncMock(return_value=[])))
        stack.enter_context(
            patch(
                f"{_PIPELINE}.PromptBuilder",
                return_value=builder
                or MagicMock(build_system_prompt=MagicMock(return_value="sys")),
            )
        )
        return await _generate_query(
            cache=cache,
            examples=examples,
            connection_id="conn-1",
            message="list users",
            session_id="sess-1",
            adapter=adapter,
            schema=None,
            privacy=None,
            semantic_model=None,
            provider_name="openai",
            options={},
            settings=_settings(),
        )


class TestCacheHit:
    async def test_stored_wrapped_query_is_served_clean(self) -> None:
        cache = MagicMock()
        cache.lookup = AsyncMock(
            return_value=CacheHit(
                cached_question="list users",
                generated_query=_OLD_WRAPPED,
                query_dialect="postgresql",
                similarity_score=1.0,
                cache_type="exact",
            )
        )
        adapter = _adapter()

        result = await _generate(cache, MagicMock(), adapter)

        assert result.cache_hit is True
        assert result.generated_query == _CLEAN
        adapter.validate_query.assert_called_once_with(_CLEAN)


class TestFewShotExamples:
    async def test_stored_wrapped_example_reaches_the_prompt_unwrapped(self) -> None:
        cache = MagicMock()
        cache.lookup = AsyncMock(return_value=None)
        cache.compute_embedding_async = AsyncMock(return_value=[0.1])
        examples = MagicMock()
        examples.find_similar_examples = AsyncMock(
            return_value=[
                ExampleEntry(
                    id="ex-1", question="list users", query=_NEW_WRAPPED, query_dialect="pg"
                ),
                ExampleEntry(id="ex-2", question="count", query="SELECT 1", query_dialect="pg"),
            ]
        )
        builder = MagicMock(build_system_prompt=MagicMock(return_value="sys"))

        await _generate(cache, examples, _adapter(), builder)

        shots = builder.build_system_prompt.call_args.kwargs["few_shot_examples"]
        assert [ex.query for ex in shots] == [f"{_CLEAN} LIMIT 5", "SELECT 1"]
        assert [ex.id for ex in shots] == ["ex-1", "ex-2"]
