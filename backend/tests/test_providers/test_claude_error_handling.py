# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for APIStatusError handling in ClaudeProvider.

Covers _normalize_api_error which converts Anthropic HTTP errors into descriptive
ValueError messages: 429 rate-limit, 401/403 auth, 400 bad-request, and generic errors.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import anthropic
import httpx
from pydantic import BaseModel
import pytest

from app.providers.claude_provider import ClaudeProvider

_FAKE_URL = "https://api.anthropic.com/v1/messages"


def _make_provider() -> ClaudeProvider:
    provider = ClaudeProvider.__new__(ClaudeProvider)
    provider._client = MagicMock()
    provider._client.with_options.return_value = provider._client
    provider._default_model = "claude-sonnet-4-6"
    return provider


def _make_api_status_error(status_code: int, message: str) -> anthropic.APIStatusError:
    request = httpx.Request("POST", _FAKE_URL)
    response = httpx.Response(status_code, request=request, text=message)
    return anthropic.APIStatusError(
        message, response=response, body={"error": {"message": message}}
    )


class TestClaudeProviderAPIStatusErrors:
    async def test_429_raises_rate_limit_value_error(self) -> None:
        """Anthropic 429 → [RATE_LIMIT] ValueError."""
        provider = _make_provider()
        provider._client.messages.create = AsyncMock(
            side_effect=_make_api_status_error(429, "Rate limit exceeded")
        )

        with pytest.raises(ValueError, match=r"\[RATE_LIMIT\]"):
            await provider.generate_response(
                system_prompt="sys", user_message="q", conversation_history=[]
            )

    async def test_401_raises_auth_error_value_error(self) -> None:
        """Anthropic 401 → [AUTH_ERROR] ValueError."""
        provider = _make_provider()
        provider._client.messages.create = AsyncMock(
            side_effect=_make_api_status_error(401, "Invalid API key")
        )

        with pytest.raises(ValueError, match=r"\[AUTH_ERROR\]"):
            await provider.generate_response(
                system_prompt="sys", user_message="q", conversation_history=[]
            )

    async def test_403_raises_auth_error_value_error(self) -> None:
        """Anthropic 403 → [AUTH_ERROR] ValueError."""
        provider = _make_provider()
        provider._client.messages.create = AsyncMock(
            side_effect=_make_api_status_error(403, "Permission denied")
        )

        with pytest.raises(ValueError, match=r"\[AUTH_ERROR\]"):
            await provider.generate_response(
                system_prompt="sys", user_message="q", conversation_history=[]
            )

    async def test_400_raises_bad_request_value_error(self) -> None:
        """Anthropic 400 → [BAD_REQUEST] ValueError."""
        provider = _make_provider()
        provider._client.messages.create = AsyncMock(
            side_effect=_make_api_status_error(400, "max_tokens exceeds model limit")
        )

        with pytest.raises(ValueError, match=r"\[BAD_REQUEST\]"):
            await provider.generate_response(
                system_prompt="sys", user_message="q", conversation_history=[]
            )

    async def test_500_raises_provider_error_value_error(self) -> None:
        """Anthropic 500 → [PROVIDER_ERROR] ValueError."""
        provider = _make_provider()
        provider._client.messages.create = AsyncMock(
            side_effect=_make_api_status_error(500, "Internal server error")
        )

        with pytest.raises(ValueError, match=r"\[PROVIDER_ERROR\]"):
            await provider.generate_response(
                system_prompt="sys", user_message="q", conversation_history=[]
            )

    async def test_429_message_propagated(self) -> None:
        """The 429 ValueError must include the API error message."""
        provider = _make_provider()
        provider._client.messages.create = AsyncMock(
            side_effect=_make_api_status_error(429, "Anthropic rate limit: 60 req/min")
        )

        with pytest.raises(ValueError) as exc_info:
            await provider.generate_response(
                system_prompt="sys", user_message="q", conversation_history=[]
            )

        assert "60 req/min" in str(exc_info.value)

    async def test_generate_structured_429_raises_rate_limit(self) -> None:
        """generate_structured also surfaces 429 as [RATE_LIMIT] ValueError."""
        provider = _make_provider()
        provider._client.messages.create = AsyncMock(
            side_effect=_make_api_status_error(429, "Rate limit exceeded")
        )

        from pydantic import BaseModel

        class _Schema(BaseModel):
            value: str

        with pytest.raises(ValueError, match=r"\[RATE_LIMIT\]"):
            await provider.generate_structured(
                system_prompt="sys", user_message="q", schema_type=_Schema
            )

    async def test_generate_response_cached_429_raises_rate_limit(self) -> None:
        """generate_response_cached also surfaces 429 as [RATE_LIMIT] ValueError."""
        provider = _make_provider()
        provider._client.messages.create = AsyncMock(
            side_effect=_make_api_status_error(429, "Rate limit exceeded")
        )

        with pytest.raises(ValueError, match=r"\[RATE_LIMIT\]"):
            await provider.generate_response_cached(
                system_prompt="sys", user_message="q", conversation_history=[]
            )


class _StructSchema(BaseModel):
    value: str = "default"


def _tool_response(stop_reason: str, tool_input: dict) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = "submit_structured_result"
    block.input = tool_input
    response = MagicMock()
    response.content = [block]
    response.stop_reason = stop_reason
    return response


class TestClaudeGenerateStructuredGuards:
    async def test_max_tokens_stop_raises(self) -> None:
        provider = _make_provider()
        provider._client.messages.create = AsyncMock(
            return_value=_tool_response("max_tokens", {"value": "x"})
        )
        with pytest.raises(ValueError, match="cut off"):
            await provider.generate_structured(
                system_prompt="sys", user_message="q", schema_type=_StructSchema, max_tokens=99
            )

    async def test_complete_tool_input_validates(self) -> None:
        provider = _make_provider()
        provider._client.messages.create = AsyncMock(
            return_value=_tool_response("tool_use", {"value": "x"})
        )
        result = await provider.generate_structured(
            system_prompt="sys", user_message="q", schema_type=_StructSchema
        )
        assert result.value == "x"

    async def test_structured_calls_disable_the_sdk_retries(self) -> None:
        provider = _make_provider()
        provider._client.messages.create = AsyncMock(
            return_value=_tool_response("tool_use", {"value": "x"})
        )
        await provider.generate_structured(
            system_prompt="sys", user_message="q", schema_type=_StructSchema
        )
        provider._client.with_options.assert_called_once_with(max_retries=0)

    @pytest.mark.parametrize("status", [408, 409, 500, 501, 502, 503, 504, 520, 524, 529])
    async def test_transient_status_is_tagged_retryable(self, status: int) -> None:
        provider = _make_provider()
        provider._client.messages.create = AsyncMock(
            side_effect=_make_api_status_error(status, "overloaded")
        )
        with pytest.raises(ValueError, match=r"\[PROVIDER_UNAVAILABLE\]"):
            await provider.generate_structured(
                system_prompt="sys", user_message="q", schema_type=_StructSchema
            )

    async def test_a_dropped_connection_is_tagged_retryable(self) -> None:
        provider = _make_provider()
        provider._client.messages.create = AsyncMock(
            side_effect=anthropic.APIConnectionError(request=httpx.Request("POST", _FAKE_URL))
        )
        with pytest.raises(ValueError, match=r"\[PROVIDER_UNAVAILABLE\]"):
            await provider.generate_structured(
                system_prompt="sys", user_message="q", schema_type=_StructSchema
            )

    @pytest.mark.parametrize(
        ("status", "marker"),
        [(429, r"\[RATE_LIMIT\]"), (400, r"\[BAD_REQUEST\]"), (401, r"\[AUTH_ERROR\]")],
    )
    async def test_non_transient_status_keeps_its_own_tag(self, status: int, marker: str) -> None:
        provider = _make_provider()
        provider._client.messages.create = AsyncMock(
            side_effect=_make_api_status_error(status, "nope")
        )
        with pytest.raises(ValueError, match=marker) as excinfo:
            await provider.generate_structured(
                system_prompt="sys", user_message="q", schema_type=_StructSchema
            )
        assert "[PROVIDER_UNAVAILABLE]" not in str(excinfo.value)
