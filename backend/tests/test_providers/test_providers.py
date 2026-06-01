# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for the LLM provider layer: parsing, registry, and each provider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.providers.base import LLMResponse, _extract_sql, parse_llm_response
from app.providers.cerebras_provider import CerebrasProvider
from app.providers.claude_provider import ClaudeProvider
from app.providers.gemini_provider import GeminiProvider
from app.providers.groq_provider import GroqProvider
from app.providers.mistral_provider import MistralProvider
from app.providers.ollama_provider import OllamaProvider
from app.providers.openai_compatible_provider import OpenAICompatibleProvider
from app.providers.openai_provider import OpenAIProvider
from app.providers.registry import (
    create_provider,
    get_provider_class,
    list_available_providers,
)

# ── _extract_sql ───────────────────────────────────────────────────────────────


class TestExtractSQL:
    def test_sql_code_block(self):
        text = "```sql\nSELECT 1\n```"
        assert _extract_sql(text) == "SELECT 1"

    def test_generic_code_block(self):
        text = "```\nSELECT 2\n```"
        assert _extract_sql(text) == "SELECT 2"

    def test_prefers_sql_fence_over_generic(self):
        text = "```\nsome text\n```\n\n```sql\nSELECT 3\n```"
        assert _extract_sql(text) == "SELECT 3"

    def test_multiline_sql(self):
        sql = "SELECT a, b\nFROM t\nWHERE id = 1"
        text = f"```sql\n{sql}\n```"
        assert _extract_sql(text) == sql

    def test_no_code_block_returns_empty(self):
        assert _extract_sql("plain text, no fences") == ""

    def test_incomplete_fence_returns_empty(self):
        # Opening fence but no closing
        assert _extract_sql("```sql\nSELECT 1") == ""

    def test_strips_whitespace(self):
        text = "```sql\n  SELECT 1  \n```"
        assert _extract_sql(text) == "SELECT 1"


# ── parse_llm_response ────────────────────────────────────────────────────────

_STRUCTURED = """\
QUERY:
```sql
SELECT COUNT(*) FROM users
```

EXPLANATION:
Counts all rows in the users table."""

_ONLY_SQL_BLOCK = "```sql\nSELECT 1\n```"
_PLAIN_TEXT = "SELECT name FROM users LIMIT 5"


class TestParseLLMResponse:
    def test_structured_extracts_query(self):
        r = parse_llm_response(_STRUCTURED, "test-model")
        assert "SELECT COUNT(*) FROM users" in r.query

    def test_structured_extracts_explanation(self):
        r = parse_llm_response(_STRUCTURED, "test-model")
        assert "Counts all rows" in r.explanation

    def test_structured_sets_model(self):
        r = parse_llm_response(_STRUCTURED, "claude-sonnet-4-6")
        assert r.model == "claude-sonnet-4-6"

    def test_structured_sets_tokens(self):
        r = parse_llm_response(_STRUCTURED, "m", tokens_used=123)
        assert r.tokens_used == 123

    def test_structured_preserves_raw_response(self):
        r = parse_llm_response(_STRUCTURED, "m")
        assert r.raw_response == _STRUCTURED

    def test_fallback_sql_block_without_markers(self):
        r = parse_llm_response(_ONLY_SQL_BLOCK, "m")
        assert r.query == "SELECT 1"
        assert r.explanation == ""

    def test_fallback_plain_text_treated_as_query(self):
        r = parse_llm_response(_PLAIN_TEXT, "m")
        assert r.query == _PLAIN_TEXT
        assert r.explanation == ""

    def test_empty_response(self):
        r = parse_llm_response("", "m")
        assert r.query == ""
        assert r.explanation == ""

    def test_query_with_only_query_marker_uses_fallback(self):
        # Only QUERY: but no EXPLANATION: — falls through to fallback
        text = "QUERY:\n```sql\nSELECT 1\n```"
        r = parse_llm_response(text, "m")
        assert "SELECT 1" in r.query

    def test_case_insensitive_markers(self):
        # parse_llm_response uppercases raw to find markers
        text = "query:\n```sql\nSELECT 1\n```\nexplanation:\nDoes stuff."
        r = parse_llm_response(text, "m")
        assert "SELECT 1" in r.query

    def test_no_tokens_when_none(self):
        r = parse_llm_response("SELECT 1", "m")
        assert r.tokens_used is None

    def test_returns_llm_response(self):
        r = parse_llm_response("SELECT 1", "m")
        assert isinstance(r, LLMResponse)

    def test_query_label_without_explanation_is_stripped(self):
        # LLM emits QUERY: but omits EXPLANATION: — the label must not leak into .query
        raw = 'QUERY:\n[{"$match": {"status": "active"}}]'
        r = parse_llm_response(raw, "m")
        assert r.query.startswith("[")
        assert "QUERY:" not in r.query

    def test_query_label_without_explanation_explanation_is_empty(self):
        raw = 'QUERY:\n[{"$match": {}}]'
        r = parse_llm_response(raw, "m")
        assert r.explanation == ""

    def test_none_literal_query_yields_empty_query(self):
        # LLM writes "None" as the QUERY value when it cannot answer — must not be
        # treated as SQL (would produce a confusing "Got: UNKNOWN" validation error).
        raw = "QUERY:\nNone\n\nEXPLANATION:\nThe schema does not contain a drivers table."
        r = parse_llm_response(raw, "m")
        assert r.query == ""
        assert "schema does not contain" in r.explanation

    def test_none_literal_query_case_insensitive(self):
        raw = "QUERY:\nnone\n\nEXPLANATION:\nCannot answer."
        r = parse_llm_response(raw, "m")
        assert r.query == ""

    def test_null_literal_query_yields_empty_query(self):
        raw = "QUERY:\nNULL\n\nEXPLANATION:\nNo relevant table found."
        r = parse_llm_response(raw, "m")
        assert r.query == ""


# ── parse_llm_response fallback logging ───────────────────────────────────────


class TestParseLLMResponseFallbackLogging:
    def test_no_warning_for_primary_format(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="app.providers.base"):
            parse_llm_response(_STRUCTURED, "test-model")
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []

    def test_warns_on_code_fence_fallback(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="app.providers.base"):
            parse_llm_response(_ONLY_SQL_BLOCK, "gpt-4o")
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("did not use primary QUERY:/EXPLANATION: format" in m for m in warning_messages)

    def test_warns_on_raw_text_fallback(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="app.providers.base"):
            parse_llm_response(_PLAIN_TEXT, "gpt-4o")
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("did not use primary QUERY:/EXPLANATION: format" in m for m in warning_messages)
        assert any("code-fence extraction failed" in m for m in warning_messages)

    def test_no_heuristic_warning_when_fence_succeeds(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="app.providers.base"):
            parse_llm_response(_ONLY_SQL_BLOCK, "gpt-4o")
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert not any("code-fence extraction failed" in m for m in warning_messages)


_COT_STRUCTURED = """\
REASONING:
1. Tables needed: users (contains the rows we need to count)
2. Joins required: none
3. Filtering/grouping: none
4. Output columns: COUNT(*) aggregate

QUERY:
```sql
SELECT COUNT(*) FROM users
```

EXPLANATION:
Counts all rows in the users table."""


class TestParseLLMResponseWithCoT:
    def test_cot_extracts_query(self):
        r = parse_llm_response(_COT_STRUCTURED, "test-model")
        assert "SELECT COUNT(*) FROM users" in r.query

    def test_cot_extracts_explanation(self):
        r = parse_llm_response(_COT_STRUCTURED, "test-model")
        assert "Counts all rows" in r.explanation

    def test_cot_reasoning_not_in_query(self):
        r = parse_llm_response(_COT_STRUCTURED, "test-model")
        assert "Tables needed" not in r.query
        assert "Joins required" not in r.query
        assert "REASONING" not in r.query

    def test_cot_reasoning_not_in_explanation(self):
        r = parse_llm_response(_COT_STRUCTURED, "test-model")
        assert "Tables needed" not in r.explanation


_EXPLANATION_BEFORE_QUERY = """\
EXPLANATION: This counts all users in the table.
QUERY:
```sql
SELECT COUNT(*) FROM users
```"""


class TestParseLLMResponseExplanationBeforeQuery:
    def test_extracts_query(self):
        r = parse_llm_response(_EXPLANATION_BEFORE_QUERY, "test-model")
        assert "SELECT COUNT(*) FROM users" in r.query

    def test_extracts_explanation(self):
        r = parse_llm_response(_EXPLANATION_BEFORE_QUERY, "test-model")
        assert "This counts all users" in r.explanation

    def test_explanation_marker_not_in_query(self):
        r = parse_llm_response(_EXPLANATION_BEFORE_QUERY, "test-model")
        assert "EXPLANATION:" not in r.query.upper()

    def test_inline_sql_without_fence(self):
        raw = "EXPLANATION: Simple.\nQUERY:\nSELECT 1"
        r = parse_llm_response(raw, "test-model")
        assert r.query == "SELECT 1"


# ── Provider registry ─────────────────────────────────────────────────────────


class TestProviderRegistry:
    def test_claude_registered(self):
        cls = get_provider_class("claude")
        assert cls is ClaudeProvider

    def test_openai_registered(self):
        cls = get_provider_class("openai")
        assert cls is OpenAIProvider

    def test_ollama_registered(self):
        cls = get_provider_class("ollama")
        assert cls is OllamaProvider

    def test_unknown_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            get_provider_class("grok")

    def test_list_available_providers_includes_all_four(self):
        names = [p["name"] for p in list_available_providers()]
        assert "claude" in names
        assert "openai" in names
        assert "openai_compatible" in names
        assert "ollama" in names

    def test_list_available_providers_has_display_name(self):
        for p in list_available_providers():
            assert "display_name" in p
            assert p["display_name"]

    def test_list_available_providers_has_models(self):
        for p in list_available_providers():
            assert "available_models" in p
            # All providers return [] from get_available_models() — models are
            # fetched dynamically via fetch_available_models() and cached in DB
            assert isinstance(p["available_models"], list)

    def test_create_provider_claude(self):
        provider = create_provider("claude", api_key="sk-test")
        assert isinstance(provider, ClaudeProvider)

    def test_create_provider_openai(self):
        provider = create_provider("openai", api_key="sk-test")
        assert isinstance(provider, OpenAIProvider)

    def test_create_provider_ollama(self):
        provider = create_provider("ollama")
        assert isinstance(provider, OllamaProvider)

    def test_openai_compatible_registered(self):
        cls = get_provider_class("openai_compatible")
        assert cls is OpenAICompatibleProvider

    def test_create_provider_openai_compatible(self):
        provider = create_provider(
            "openai_compatible", api_key="sk-test", base_url="https://api.groq.com/openai/v1"
        )
        assert isinstance(provider, OpenAICompatibleProvider)


# ── ClaudeProvider ────────────────────────────────────────────────────────────


def _fake_anthropic_response(text: str, model: str = "claude-sonnet-4-6"):
    """Build a mock that looks like an anthropic.types.Message."""
    content_block = MagicMock()
    content_block.text = text
    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 20
    msg = MagicMock()
    msg.content = [content_block]
    msg.model = model
    msg.usage = usage
    return msg


class TestClaudeProvider:
    def setup_method(self):
        self.provider = ClaudeProvider(api_key="sk-test")

    async def test_generate_response_returns_llm_response(self):
        raw = _STRUCTURED
        mock_response = _fake_anthropic_response(raw)
        self.provider._client.messages.create = AsyncMock(return_value=mock_response)

        result = await self.provider.generate_response(
            system_prompt="You are helpful.",
            user_message="How many users?",
            conversation_history=[],
        )
        assert isinstance(result, LLMResponse)

    async def test_generate_response_extracts_query(self):
        mock_response = _fake_anthropic_response(_STRUCTURED)
        self.provider._client.messages.create = AsyncMock(return_value=mock_response)

        result = await self.provider.generate_response("sys", "msg", [])
        assert "SELECT COUNT(*) FROM users" in result.query

    async def test_generate_response_passes_system_prompt(self):
        mock_response = _fake_anthropic_response(_STRUCTURED)
        create = AsyncMock(return_value=mock_response)
        self.provider._client.messages.create = create

        await self.provider.generate_response("my system", "hello", [])
        call_kwargs = create.call_args.kwargs
        assert call_kwargs["system"] == "my system"

    async def test_generate_response_includes_history(self):
        mock_response = _fake_anthropic_response(_STRUCTURED)
        create = AsyncMock(return_value=mock_response)
        self.provider._client.messages.create = create
        history = [{"role": "user", "content": "prev"}, {"role": "assistant", "content": "ans"}]

        await self.provider.generate_response("sys", "new", history)
        messages = create.call_args.kwargs["messages"]
        assert messages[0]["content"] == "prev"
        assert messages[-1]["content"] == "new"

    async def test_generate_response_uses_explicit_model(self):
        mock_response = _fake_anthropic_response(_STRUCTURED, model="claude-opus-4-6")
        create = AsyncMock(return_value=mock_response)
        self.provider._client.messages.create = create

        await self.provider.generate_response("sys", "msg", [], model="claude-opus-4-6")
        assert create.call_args.kwargs["model"] == "claude-opus-4-6"

    async def test_generate_response_uses_default_model_when_none(self):
        mock_response = _fake_anthropic_response(_STRUCTURED)
        create = AsyncMock(return_value=mock_response)
        self.provider._client.messages.create = create

        await self.provider.generate_response("sys", "msg", [], model=None)
        assert create.call_args.kwargs["model"] == self.provider._default_model

    async def test_generate_response_sums_tokens(self):
        mock_response = _fake_anthropic_response(_STRUCTURED)
        self.provider._client.messages.create = AsyncMock(return_value=mock_response)

        result = await self.provider.generate_response("sys", "msg", [])
        assert result.tokens_used == 30  # 10 + 20

    async def test_health_check_returns_true_on_success(self):
        self.provider._client.messages.create = AsyncMock(return_value=MagicMock())
        ok, detail = await self.provider.health_check()
        assert ok is True
        assert detail == ""

    async def test_health_check_returns_false_on_error(self):
        self.provider._client.messages.create = AsyncMock(side_effect=Exception("timeout"))
        ok, detail = await self.provider.health_check()
        assert ok is False
        assert "timeout" in detail

    def test_get_available_models_returns_empty(self):
        # Models are fetched dynamically; the static list is intentionally empty
        assert ClaudeProvider.get_available_models() == []


# ── OpenAIProvider ────────────────────────────────────────────────────────────


def _fake_openai_response(text: str, model: str = "gpt-4o"):
    choice = MagicMock()
    choice.message.content = text
    usage = MagicMock()
    usage.total_tokens = 50
    completion = MagicMock()
    completion.choices = [choice]
    completion.model = model
    completion.usage = usage
    return completion


class TestOpenAIProvider:
    def setup_method(self):
        self.provider = OpenAIProvider(api_key="sk-test")

    async def test_generate_response_returns_llm_response(self):
        mock_completion = _fake_openai_response(_STRUCTURED)
        self.provider._client.chat.completions.create = AsyncMock(return_value=mock_completion)

        result = await self.provider.generate_response("sys", "msg", [])
        assert isinstance(result, LLMResponse)

    async def test_generate_response_extracts_query(self):
        mock_completion = _fake_openai_response(_STRUCTURED)
        self.provider._client.chat.completions.create = AsyncMock(return_value=mock_completion)

        result = await self.provider.generate_response("sys", "msg", [])
        assert "SELECT COUNT(*) FROM users" in result.query

    async def test_generate_response_includes_system_message(self):
        mock_completion = _fake_openai_response(_STRUCTURED)
        create = AsyncMock(return_value=mock_completion)
        self.provider._client.chat.completions.create = create

        await self.provider.generate_response("my system", "hello", [])
        messages = create.call_args.kwargs["messages"]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "my system"

    async def test_generate_response_user_message_is_last(self):
        mock_completion = _fake_openai_response(_STRUCTURED)
        create = AsyncMock(return_value=mock_completion)
        self.provider._client.chat.completions.create = create

        await self.provider.generate_response("sys", "user question", [])
        messages = create.call_args.kwargs["messages"]
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "user question"

    async def test_generate_response_records_tokens(self):
        mock_completion = _fake_openai_response(_STRUCTURED)
        self.provider._client.chat.completions.create = AsyncMock(return_value=mock_completion)

        result = await self.provider.generate_response("sys", "msg", [])
        assert result.tokens_used == 50

    async def test_health_check_returns_true_on_success(self):
        self.provider._client.models.list = AsyncMock(return_value=MagicMock())
        ok, detail = await self.provider.health_check()
        assert ok is True
        assert detail == ""

    async def test_health_check_returns_false_on_error(self):
        self.provider._client.models.list = AsyncMock(side_effect=Exception("auth"))
        ok, detail = await self.provider.health_check()
        assert ok is False
        assert "auth" in detail

    def test_get_available_models_returns_empty(self):
        # Models are fetched dynamically; the static list is intentionally empty
        assert OpenAIProvider.get_available_models() == []

    def test_accepts_custom_base_url(self):
        provider = OpenAIProvider(api_key="x", base_url="http://custom:1234/v1")
        # Just verify it constructs without error
        assert provider is not None


# ── OllamaProvider ────────────────────────────────────────────────────────────


class TestOllamaProvider:
    def setup_method(self):
        self.provider = OllamaProvider(base_url="http://localhost:11434")

    def test_inherits_from_openai_provider(self):
        assert isinstance(self.provider, OpenAIProvider)

    def test_default_model_is_llama3(self):
        assert self.provider._default_model == "llama3"

    def test_base_url_stored(self):
        assert self.provider._base_url == "http://localhost:11434"

    def test_trailing_slash_stripped_from_base_url(self):
        p = OllamaProvider(base_url="http://localhost:11434/")
        assert not p._base_url.endswith("/")

    async def test_health_check_returns_true_on_200(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("app.providers.ollama_provider.httpx.AsyncClient", return_value=mock_client):
            ok, detail = await self.provider.health_check()
        assert ok is True
        assert detail == ""

    async def test_health_check_returns_false_on_non_200(self):
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("app.providers.ollama_provider.httpx.AsyncClient", return_value=mock_client):
            ok, detail = await self.provider.health_check()
        assert ok is False
        assert "503" in detail

    async def test_health_check_returns_false_on_connection_error(self):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))

        with patch("app.providers.ollama_provider.httpx.AsyncClient", return_value=mock_client):
            ok, detail = await self.provider.health_check()
        assert ok is False
        assert "connection refused" in detail

    def test_get_available_models_returns_empty(self):
        # Models are fetched live from /api/tags; the static list is intentionally empty
        assert OllamaProvider.get_available_models() == []

    async def test_list_running_models_parses_response(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"models": [{"name": "llama3"}, {"name": "mistral"}]}
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("app.providers.ollama_provider.httpx.AsyncClient", return_value=mock_client):
            models = await self.provider.list_running_models()
        assert models == ["llama3", "mistral"]

    async def test_list_running_models_returns_empty_on_error(self):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))

        with patch("app.providers.ollama_provider.httpx.AsyncClient", return_value=mock_client):
            models = await self.provider.list_running_models()
        assert models == []

    async def test_generate_response_inherits_from_openai(self):
        # Ollama generate_response uses the same OpenAI-compatible logic
        mock_completion = _fake_openai_response(_STRUCTURED, model="llama3")
        self.provider._client.chat.completions.create = AsyncMock(return_value=mock_completion)

        result = await self.provider.generate_response("sys", "msg", [])
        assert isinstance(result, LLMResponse)
        assert "SELECT COUNT(*) FROM users" in result.query

    def test_default_model_param_accepted(self):
        p = OllamaProvider(default_model="llama3:70b")
        assert p._default_model == "llama3:70b"

    def test_verify_ssl_param_accepted(self):
        p = OllamaProvider(verify_ssl=False)
        assert p._verify_ssl is False


# ── OpenAICompatibleProvider ───────────────────────────────────────────────────


class TestOpenAICompatibleProvider:
    def setup_method(self):
        self.provider = OpenAICompatibleProvider(
            api_key="sk-test",
            base_url="https://api.groq.com/openai/v1",
        )

    def test_inherits_from_openai_provider(self):
        assert isinstance(self.provider, OpenAIProvider)

    def test_provider_name(self):
        assert self.provider.provider_name == "openai_compatible"

    def test_display_name(self):
        assert "OpenAI-Compatible" in self.provider.display_name

    def test_base_url_forwarded_to_client(self):
        # base_url is consumed by the AsyncOpenAI constructor; verify no error on init
        p = OpenAICompatibleProvider(api_key="x", base_url="http://custom:8080/v1")
        assert p is not None

    def test_base_url_is_required(self):
        # base_url is required; omitting it should raise TypeError
        with pytest.raises(TypeError):
            OpenAICompatibleProvider(api_key="sk-test")

    def test_default_model_stored(self):
        p = OpenAICompatibleProvider(
            api_key="sk-test",
            base_url="https://api.groq.com/openai/v1",
            default_model="llama-3.3-70b-versatile",
        )
        assert p._default_model == "llama-3.3-70b-versatile"

    def test_default_model_defaults_to_empty_string(self):
        assert self.provider._default_model == ""

    def test_get_available_models_returns_empty_list(self):
        # OpenAI-compatible services vary; model list is service-specific
        models = OpenAICompatibleProvider.get_available_models()
        assert models == []

    def test_get_config_schema_has_fields_and_presets(self):
        schema = OpenAICompatibleProvider.get_config_schema()
        assert "fields" in schema
        assert "presets" in schema
        field_names = [f["name"] for f in schema["fields"]]
        assert "base_url" in field_names
        assert "api_key" in field_names
        assert "model" in field_names

    def test_get_config_schema_presets_include_github_and_huggingface(self):
        schema = OpenAICompatibleProvider.get_config_schema()
        presets = schema["presets"]
        assert "github" in presets
        assert "huggingface" in presets
        assert "azure.com" in presets["github"]["base_url"]

    async def test_generate_response_inherits_openai_logic(self):
        mock_completion = _fake_openai_response(_STRUCTURED, model="llama-3.3-70b-versatile")
        self.provider._client.chat.completions.create = AsyncMock(return_value=mock_completion)

        result = await self.provider.generate_response("sys", "msg", [])
        assert isinstance(result, LLMResponse)
        assert "SELECT COUNT(*) FROM users" in result.query

    async def test_generate_response_passes_system_prompt(self):
        mock_completion = _fake_openai_response(_STRUCTURED)
        create = AsyncMock(return_value=mock_completion)
        self.provider._client.chat.completions.create = create

        await self.provider.generate_response("my system", "hello", [])
        messages = create.call_args.kwargs["messages"]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "my system"

    async def test_health_check_returns_true_on_success(self):
        self.provider._default_model = "llama-3.3-70b-versatile"
        mock_response = MagicMock()
        self.provider._client.chat.completions.create = AsyncMock(return_value=mock_response)
        ok, detail = await self.provider.health_check()
        assert ok is True
        assert detail == ""

    async def test_health_check_returns_false_on_error(self):
        self.provider._default_model = "llama-3.3-70b-versatile"
        self.provider._client.chat.completions.create = AsyncMock(side_effect=Exception("auth"))
        ok, detail = await self.provider.health_check()
        assert ok is False
        assert "auth" in detail

    async def test_health_check_fails_without_model(self):
        assert self.provider._default_model == ""
        ok, detail = await self.provider.health_check()
        assert ok is False
        assert "model" in detail.lower()


# ── Dedicated provider classes (Groq, Gemini, Cerebras, Mistral) ──────────────


class TestGroqProvider:
    def setup_method(self):
        self.provider = GroqProvider(api_key="sk-test")

    def test_inherits_from_openai_provider(self):
        # GroqProvider inherits directly from OpenAIProvider, not OpenAICompatibleProvider
        assert isinstance(self.provider, OpenAIProvider)

    def test_does_not_inherit_from_openai_compatible(self):
        assert not isinstance(self.provider, OpenAICompatibleProvider)

    def test_provider_name(self):
        assert self.provider.provider_name == "groq"

    def test_display_name(self):
        assert "Groq" in self.provider.display_name

    def test_default_model_is_set(self):
        assert self.provider._default_model == GroqProvider._DEFAULT_MODEL

    def test_custom_model_respected(self):
        p = GroqProvider(api_key="sk-test", default_model="gemma2-9b-it")
        assert p._default_model == "gemma2-9b-it"

    def test_base_url_not_a_constructor_param(self):
        # base_url is fixed; it must not be accepted as a kwarg
        with pytest.raises(TypeError):
            GroqProvider(api_key="sk-test", base_url="https://custom.example.com/v1")

    def test_get_available_models_returns_empty(self):
        # Models are fetched dynamically; the static list is intentionally empty
        assert GroqProvider.get_available_models() == []

    def test_get_config_schema_has_api_key_and_model(self):
        schema = GroqProvider.get_config_schema()
        assert "fields" in schema
        field_names = [f["name"] for f in schema["fields"]]
        assert "api_key" in field_names
        assert "model" in field_names

    def test_get_config_schema_no_base_url_field(self):
        # Named provider — no service selector dropdown
        schema = GroqProvider.get_config_schema()
        field_names = [f["name"] for f in schema["fields"]]
        assert "base_url" not in field_names

    def test_get_config_schema_model_field_is_string_type(self):
        schema = GroqProvider.get_config_schema()
        model_field = next(f for f in schema["fields"] if f["name"] == "model")
        assert model_field["type"] == "string"
        assert "placeholder" in model_field

    async def test_health_check_success(self):
        mock_response = MagicMock()
        self.provider._client.chat.completions.create = AsyncMock(return_value=mock_response)
        ok, detail = await self.provider.health_check()
        assert ok is True
        assert detail == ""

    async def test_health_check_failure(self):
        self.provider._client.chat.completions.create = AsyncMock(side_effect=Exception("auth"))
        ok, detail = await self.provider.health_check()
        assert ok is False
        assert "auth" in detail

    async def test_health_check_uses_default_model(self):
        create = AsyncMock(return_value=MagicMock())
        self.provider._client.chat.completions.create = create
        await self.provider.health_check()
        assert create.call_args.kwargs["model"] == self.provider._default_model


class TestGeminiProvider:
    def setup_method(self):
        self.provider = GeminiProvider(api_key="AIza-test")

    def test_inherits_from_openai_provider(self):
        assert isinstance(self.provider, OpenAIProvider)

    def test_does_not_inherit_from_openai_compatible(self):
        assert not isinstance(self.provider, OpenAICompatibleProvider)

    def test_provider_name(self):
        assert self.provider.provider_name == "gemini"

    def test_display_name(self):
        assert "Gemini" in self.provider.display_name

    def test_default_model_is_set(self):
        assert self.provider._default_model == GeminiProvider._DEFAULT_MODEL

    def test_custom_model_respected(self):
        p = GeminiProvider(api_key="AIza-test", default_model="gemini-1.5-pro")
        assert p._default_model == "gemini-1.5-pro"

    def test_context_window_set(self):
        assert isinstance(GeminiProvider.context_window, int)
        assert GeminiProvider.context_window > 0

    def test_get_available_models_returns_empty(self):
        # Models are fetched dynamically; the static list is intentionally empty
        assert GeminiProvider.get_available_models() == []

    def test_get_config_schema_has_api_key_and_model(self):
        schema = GeminiProvider.get_config_schema()
        assert "fields" in schema
        field_names = [f["name"] for f in schema["fields"]]
        assert "api_key" in field_names
        assert "model" in field_names

    def test_get_config_schema_no_base_url_field(self):
        schema = GeminiProvider.get_config_schema()
        field_names = [f["name"] for f in schema["fields"]]
        assert "base_url" not in field_names

    def test_get_config_schema_model_field_is_string_type(self):
        schema = GeminiProvider.get_config_schema()
        model_field = next(f for f in schema["fields"] if f["name"] == "model")
        assert model_field["type"] == "string"
        assert "placeholder" in model_field

    async def test_health_check_success(self):
        mock_response = MagicMock()
        self.provider._client.chat.completions.create = AsyncMock(return_value=mock_response)
        ok, detail = await self.provider.health_check()
        assert ok is True
        assert detail == ""

    async def test_health_check_failure(self):
        self.provider._client.chat.completions.create = AsyncMock(side_effect=Exception("quota"))
        ok, detail = await self.provider.health_check()
        assert ok is False
        assert "quota" in detail


class TestCerebrasProvider:
    def setup_method(self):
        self.provider = CerebrasProvider(api_key="csk-test")

    def test_inherits_from_openai_provider(self):
        assert isinstance(self.provider, OpenAIProvider)

    def test_does_not_inherit_from_openai_compatible(self):
        assert not isinstance(self.provider, OpenAICompatibleProvider)

    def test_provider_name(self):
        assert self.provider.provider_name == "cerebras"

    def test_display_name(self):
        assert "Cerebras" in self.provider.display_name

    def test_default_model_is_set(self):
        assert self.provider._default_model == CerebrasProvider._DEFAULT_MODEL

    def test_custom_model_respected(self):
        p = CerebrasProvider(api_key="csk-test", default_model="zai-glm-4.7")
        assert p._default_model == "zai-glm-4.7"

    def test_get_available_models_returns_empty(self):
        # Models are fetched dynamically; the static list is intentionally empty
        assert CerebrasProvider.get_available_models() == []

    def test_get_config_schema_has_api_key_and_model(self):
        schema = CerebrasProvider.get_config_schema()
        assert "fields" in schema
        field_names = [f["name"] for f in schema["fields"]]
        assert "api_key" in field_names
        assert "model" in field_names

    def test_get_config_schema_no_base_url_field(self):
        schema = CerebrasProvider.get_config_schema()
        field_names = [f["name"] for f in schema["fields"]]
        assert "base_url" not in field_names

    def test_get_config_schema_model_field_is_string_type(self):
        schema = CerebrasProvider.get_config_schema()
        model_field = next(f for f in schema["fields"] if f["name"] == "model")
        assert model_field["type"] == "string"
        assert "placeholder" in model_field

    async def test_health_check_success(self):
        mock_response = MagicMock()
        self.provider._client.chat.completions.create = AsyncMock(return_value=mock_response)
        ok, detail = await self.provider.health_check()
        assert ok is True
        assert detail == ""

    async def test_health_check_failure(self):
        self.provider._client.chat.completions.create = AsyncMock(side_effect=Exception("timeout"))
        ok, detail = await self.provider.health_check()
        assert ok is False
        assert "timeout" in detail


class TestMistralProvider:
    def setup_method(self):
        self.provider = MistralProvider(api_key="msk-test")

    def test_inherits_from_openai_provider(self):
        assert isinstance(self.provider, OpenAIProvider)

    def test_does_not_inherit_from_openai_compatible(self):
        assert not isinstance(self.provider, OpenAICompatibleProvider)

    def test_provider_name(self):
        assert self.provider.provider_name == "mistral"

    def test_display_name(self):
        assert "Mistral" in self.provider.display_name

    def test_default_model_is_set(self):
        assert self.provider._default_model == MistralProvider._DEFAULT_MODEL

    def test_custom_model_respected(self):
        p = MistralProvider(api_key="msk-test", default_model="codestral-latest")
        assert p._default_model == "codestral-latest"

    def test_context_window_set(self):
        assert isinstance(MistralProvider.context_window, int)
        assert MistralProvider.context_window > 0

    def test_get_available_models_returns_empty(self):
        # Models are fetched dynamically; the static list is intentionally empty
        assert MistralProvider.get_available_models() == []

    def test_get_config_schema_has_api_key_and_model(self):
        schema = MistralProvider.get_config_schema()
        assert "fields" in schema
        field_names = [f["name"] for f in schema["fields"]]
        assert "api_key" in field_names
        assert "model" in field_names

    def test_get_config_schema_no_base_url_field(self):
        schema = MistralProvider.get_config_schema()
        field_names = [f["name"] for f in schema["fields"]]
        assert "base_url" not in field_names

    def test_get_config_schema_model_field_is_string_type(self):
        schema = MistralProvider.get_config_schema()
        model_field = next(f for f in schema["fields"] if f["name"] == "model")
        assert model_field["type"] == "string"
        assert "placeholder" in model_field

    async def test_health_check_success(self):
        mock_response = MagicMock()
        self.provider._client.chat.completions.create = AsyncMock(return_value=mock_response)
        ok, detail = await self.provider.health_check()
        assert ok is True
        assert detail == ""

    async def test_health_check_failure(self):
        self.provider._client.chat.completions.create = AsyncMock(side_effect=Exception("auth"))
        ok, detail = await self.provider.health_check()
        assert ok is False
        assert "auth" in detail
