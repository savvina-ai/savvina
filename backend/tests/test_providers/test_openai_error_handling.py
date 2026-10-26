# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for APIStatusError handling in OpenAIProvider.generate_response().

Covers _normalize_api_error which converts provider HTTP errors into clean, user-readable
ValueError messages: 413 (Groq TPM), 429 (Groq TPM, Gemini quota, OpenAI
insufficient_quota), 402 (HuggingFace), 400+context_length_exceeded (Cerebras),
401/403 (bad key), and catch-all PROVIDER_ERROR.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
from pydantic import BaseModel
import pytest

from app.providers.base import BaseLLMProvider, is_transient_status
from app.providers.openai_provider import OpenAIProvider

_FAKE_URL = "https://api.openai.com/v1/chat/completions"


def _make_provider() -> OpenAIProvider:
    """Instantiate OpenAIProvider without calling __init__ (avoids network setup)."""
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider._client = MagicMock()
    provider._client.with_options.return_value = provider._client
    provider._default_model = "gpt-4o"
    return provider


def _make_api_status_error(status_code: int, body: dict) -> openai.APIStatusError:
    """Create a real openai.APIStatusError with the given status code and JSON body.

    Uses a real httpx.Request + httpx.Response so that openai.APIStatusError.__init__
    can access response.request without raising RuntimeError.
    """
    request = httpx.Request("POST", _FAKE_URL)
    content = json.dumps(body).encode()
    response = httpx.Response(status_code, request=request, content=content)
    return openai.APIStatusError(f"HTTP {status_code}", response=response, body=body)


class TestOpenAIProviderAPIStatusErrors:
    async def test_413_raises_clean_value_error(self) -> None:
        """413 from Groq (TPM rate limit) → ValueError with a readable message."""
        provider = _make_provider()
        groq_body = {
            "error": {
                "message": "Limit 12000, Requested 12139",
                "type": "tokens",
                "code": "rate_limit_exceeded",
            }
        }
        provider._client.chat.completions.create = AsyncMock(
            side_effect=_make_api_status_error(413, groq_body)
        )

        with pytest.raises(ValueError, match="token rate limit exceeded"):
            await provider.generate_response(
                system_prompt="sys", user_message="q", conversation_history=[]
            )

    async def test_400_context_length_exceeded_raises_clean_value_error(self) -> None:
        """400 + context_length_exceeded from Cerebras → ValueError with readable message."""
        provider = _make_provider()
        cerebras_body = {
            "message": "Current length is 12162 while limit is 8192",
            "code": "context_length_exceeded",
        }
        provider._client.chat.completions.create = AsyncMock(
            side_effect=_make_api_status_error(400, cerebras_body)
        )

        with pytest.raises(ValueError, match="context window"):
            await provider.generate_response(
                system_prompt="sys", user_message="q", conversation_history=[]
            )

    async def test_413_clean_message_includes_api_detail(self) -> None:
        """The clean ValueError for 413 must include the original API message text."""
        provider = _make_provider()
        groq_body = {
            "error": {
                "message": "Limit 12000, Requested 12139",
                "type": "tokens",
                "code": "rate_limit_exceeded",
            }
        }
        provider._client.chat.completions.create = AsyncMock(
            side_effect=_make_api_status_error(413, groq_body)
        )

        with pytest.raises(ValueError) as exc_info:
            await provider.generate_response(
                system_prompt="sys", user_message="q", conversation_history=[]
            )

        assert "Limit 12000" in str(exc_info.value)

    async def test_429_token_rate_limit_raises_tpm_value_error(self) -> None:
        """429 from Groq free tier with 'token rate limit exceeded' → [TPM_EXCEEDED] ValueError.

        This is the real-world Groq error when qwen3-32b's 6000 TPM limit is breached.
        Previously it fell through to a bare `raise` and the raw JSON error appeared in the UI.
        """
        provider = _make_provider()
        groq_body = {
            "error": {
                "message": (
                    "Request too large: token rate limit exceeded. "
                    "Request too large for model `qwen/qwen3-32b` in organization "
                    "`org_01jsxzr9y7fcyvpe8ry281cb94` service tier `on_demand` on "
                    "tokens per minute (TPM): Limit 6000, Requested 7488"
                ),
                "type": "tokens",
                "code": "rate_limit_exceeded",
            }
        }
        provider._client.chat.completions.create = AsyncMock(
            side_effect=_make_api_status_error(429, groq_body)
        )

        with pytest.raises(ValueError, match=r"\[TPM_EXCEEDED\]"):
            await provider.generate_response(
                system_prompt="sys", user_message="q", conversation_history=[]
            )

    async def test_429_token_rate_limit_message_starts_with_tpm_prefix(self) -> None:
        """The ValueError message must start with [TPM_EXCEEDED] so the retry logic triggers."""
        provider = _make_provider()
        groq_body = {
            "error": {
                "message": "token rate limit exceeded. Limit 6000, Requested 7488",
                "type": "tokens",
                "code": "rate_limit_exceeded",
            }
        }
        provider._client.chat.completions.create = AsyncMock(
            side_effect=_make_api_status_error(429, groq_body)
        )

        with pytest.raises(ValueError) as exc_info:
            await provider.generate_response(
                system_prompt="sys", user_message="q", conversation_history=[]
            )

        assert str(exc_info.value).startswith("[TPM_EXCEEDED]")

    async def test_429_non_tpm_error_raises_rate_limit_value_error(self) -> None:
        """A generic 429 (standard rate limit, not token size) → [RATE_LIMIT] ValueError."""
        provider = _make_provider()
        body = {
            "error": {
                "message": "Rate limit: 100 requests per minute",
                "code": "rate_limit_exceeded",
            },
        }
        provider._client.chat.completions.create = AsyncMock(
            side_effect=_make_api_status_error(429, body)
        )

        with pytest.raises(ValueError, match=r"\[RATE_LIMIT\]"):
            await provider.generate_response(
                system_prompt="sys", user_message="q", conversation_history=[]
            )

    async def test_429_insufficient_quota_raises_quota_exceeded_not_rate_limit(self) -> None:
        """OpenAI's exhausted-balance 429 → [QUOTA_EXCEEDED], which nothing retries."""
        provider = _make_provider()
        body = {
            "error": {
                "message": "You exceeded your current quota, please check your plan.",
                "type": "insufficient_quota",
                "code": "insufficient_quota",
            },
        }
        provider._client.chat.completions.create = AsyncMock(
            side_effect=_make_api_status_error(429, body)
        )

        with pytest.raises(ValueError) as exc_info:
            await provider.generate_response(
                system_prompt="sys", user_message="q", conversation_history=[]
            )

        msg = str(exc_info.value)
        assert msg.startswith("[QUOTA_EXCEEDED]")
        assert "[RATE_LIMIT]" not in msg
        assert "check your plan" in msg

    async def test_429_gemini_quota_raises_rate_limit_with_api_message(self) -> None:
        """Gemini free-tier quota 429 → [RATE_LIMIT] ValueError containing the quota message."""
        provider = _make_provider()
        body = {
            "error": {
                "code": 429,
                "message": (
                    "You exceeded your current quota, please check your plan and billing details. "
                    "Quota exceeded for metric: "
                    "generativelanguage.googleapis.com/generate_content_free_tier_requests"
                ),
                "status": "RESOURCE_EXHAUSTED",
            }
        }
        provider._client.chat.completions.create = AsyncMock(
            side_effect=_make_api_status_error(429, body)
        )

        with pytest.raises(ValueError) as exc_info:
            await provider.generate_response(
                system_prompt="sys", user_message="q", conversation_history=[]
            )

        msg = str(exc_info.value)
        assert msg.startswith("[RATE_LIMIT]")
        assert "quota" in msg.lower()

    async def test_401_raises_auth_error_value_error(self) -> None:
        """401 → [AUTH_ERROR] ValueError."""
        provider = _make_provider()
        body = {"error": {"message": "Invalid API key", "code": "invalid_api_key"}}
        provider._client.chat.completions.create = AsyncMock(
            side_effect=_make_api_status_error(401, body)
        )

        with pytest.raises(ValueError, match=r"\[AUTH_ERROR\]"):
            await provider.generate_response(
                system_prompt="sys", user_message="q", conversation_history=[]
            )

    async def test_402_payment_required_raises_clean_value_error(self) -> None:
        """402 from HuggingFace (depleted credits) → ValueError with a readable message."""
        provider = _make_provider()
        hf_body = {
            "error": (
                "You have depleted your monthly included credits. "
                "Purchase pre-paid credits to continue using Inference Providers."
            )
        }
        provider._client.chat.completions.create = AsyncMock(
            side_effect=_make_api_status_error(402, hf_body)
        )

        with pytest.raises(ValueError, match=r"\[PAYMENT_REQUIRED\]"):
            await provider.generate_response(
                system_prompt="sys", user_message="q", conversation_history=[]
            )

    async def test_402_message_includes_provider_guidance(self) -> None:
        """The 402 ValueError must tell the user to switch providers."""
        provider = _make_provider()
        hf_body = {"error": "credits exhausted"}
        provider._client.chat.completions.create = AsyncMock(
            side_effect=_make_api_status_error(402, hf_body)
        )

        with pytest.raises(ValueError) as exc_info:
            await provider.generate_response(
                system_prompt="sys", user_message="q", conversation_history=[]
            )

        assert "switch to a different provider" in str(exc_info.value)

    async def test_other_400_raises_provider_error_value_error(self) -> None:
        """400 with an unrecognised error code → [PROVIDER_ERROR] ValueError."""
        provider = _make_provider()
        body = {"error": {"message": "Invalid model", "code": "model_not_found"}}
        provider._client.chat.completions.create = AsyncMock(
            side_effect=_make_api_status_error(400, body)
        )

        with pytest.raises(ValueError, match=r"\[PROVIDER_ERROR\]"):
            await provider.generate_response(
                system_prompt="sys", user_message="q", conversation_history=[]
            )


class _Schema(BaseModel):
    value: str = "default"


def _structured_response(content: str | None, finish_reason: str = "stop") -> MagicMock:
    choice = MagicMock()
    choice.message.content = content
    choice.finish_reason = finish_reason
    response = MagicMock()
    response.choices = [choice]
    return response


class TestOpenAIGenerateStructuredGuards:
    async def test_truncated_reply_raises(self) -> None:
        """A reply cut off at max_tokens is incomplete, however valid the JSON prefix."""
        provider = _make_provider()
        provider._client.chat.completions.create = AsyncMock(
            return_value=_structured_response('{"value": "x"}', finish_reason="length")
        )
        with pytest.raises(ValueError, match="cut off"):
            await provider.generate_structured(
                system_prompt="sys", user_message="q", schema_type=_Schema, max_tokens=123
            )

    @pytest.mark.parametrize("content", [None, "", "   "])
    async def test_blank_reply_raises_instead_of_becoming_empty_json(
        self, content: str | None
    ) -> None:
        provider = _make_provider()
        provider._client.chat.completions.create = AsyncMock(
            return_value=_structured_response(content)
        )
        with pytest.raises(ValueError, match=r"\[EMPTY_RESPONSE\]"):
            await provider.generate_structured(
                system_prompt="sys", user_message="q", schema_type=_Schema
            )

    async def test_a_model_that_answers_an_empty_object_still_validates(self) -> None:
        provider = _make_provider()
        provider._client.chat.completions.create = AsyncMock(
            return_value=_structured_response("{}")
        )
        result = await provider.generate_structured(
            system_prompt="sys", user_message="q", schema_type=_Schema
        )
        assert result.value == "default"

    async def test_context_exceeded_400_is_not_retried_in_text_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        body = {"error": {"message": "too long", "code": "context_length_exceeded"}}
        provider._client.chat.completions.create = AsyncMock(
            side_effect=_make_api_status_error(400, body)
        )
        fallback = AsyncMock()
        monkeypatch.setattr(BaseLLMProvider, "generate_structured", fallback)
        with pytest.raises(ValueError, match=r"\[CONTEXT_EXCEEDED\]"):
            await provider.generate_structured(
                system_prompt="sys", user_message="q", schema_type=_Schema
            )
        fallback.assert_not_awaited()

    async def test_other_400_still_falls_back_to_text_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 400 that means "json_object unsupported" keeps the text-mode fallback."""
        provider = _make_provider()
        body = {"error": {"message": "unsupported response_format", "code": "invalid"}}
        provider._client.chat.completions.create = AsyncMock(
            side_effect=_make_api_status_error(400, body)
        )
        sentinel = _Schema(value="from-fallback")
        fallback = AsyncMock(return_value=sentinel)
        monkeypatch.setattr(BaseLLMProvider, "generate_structured", fallback)
        result = await provider.generate_structured(
            system_prompt="sys", user_message="q", schema_type=_Schema
        )
        assert result is sentinel
        fallback.assert_awaited_once()

    async def test_structured_calls_disable_the_sdk_retries(self) -> None:
        provider = _make_provider()
        provider._client.chat.completions.create = AsyncMock(
            return_value=_structured_response('{"value": "x"}')
        )
        await provider.generate_structured(
            system_prompt="sys", user_message="q", schema_type=_Schema
        )
        provider._client.with_options.assert_called_once_with(max_retries=0)

    @pytest.mark.parametrize("status", [408, 409, 500, 501, 502, 503, 504, 520, 524, 529])
    async def test_transient_status_is_tagged_retryable(self, status: int) -> None:
        provider = _make_provider()
        provider._client.chat.completions.create = AsyncMock(
            side_effect=_make_api_status_error(status, {"error": {"message": "down"}})
        )
        with pytest.raises(ValueError, match=r"\[PROVIDER_UNAVAILABLE\]"):
            await provider.generate_structured(
                system_prompt="sys", user_message="q", schema_type=_Schema
            )

    async def test_a_dropped_connection_is_tagged_retryable(self) -> None:
        provider = _make_provider()
        provider._client.chat.completions.create = AsyncMock(
            side_effect=openai.APIConnectionError(request=httpx.Request("POST", _FAKE_URL))
        )
        with pytest.raises(ValueError, match=r"\[PROVIDER_UNAVAILABLE\]"):
            await provider.generate_structured(
                system_prompt="sys", user_message="q", schema_type=_Schema
            )

    async def test_a_permanent_status_is_not_tagged_retryable(self) -> None:
        provider = _make_provider()
        provider._client.chat.completions.create = AsyncMock(
            side_effect=_make_api_status_error(401, {"error": {"message": "bad key"}})
        )
        with pytest.raises(ValueError, match=r"\[AUTH_ERROR\]"):
            await provider.generate_structured(
                system_prompt="sys", user_message="q", schema_type=_Schema
            )

    async def test_429_is_a_rate_limit_not_unavailable(self) -> None:
        provider = _make_provider()
        provider._client.chat.completions.create = AsyncMock(
            side_effect=_make_api_status_error(429, {"error": {"message": "slow down"}})
        )
        with pytest.raises(ValueError, match=r"\[RATE_LIMIT\]") as excinfo:
            await provider.generate_structured(
                system_prompt="sys", user_message="q", schema_type=_Schema
            )
        assert "[PROVIDER_UNAVAILABLE]" not in str(excinfo.value)

    async def test_text_fallback_runs_on_the_no_retry_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The text-mode fallback must not go back to the SDK-default-retry client."""
        provider = _make_provider()
        original_client = provider._client
        scoped_client = MagicMock()
        original_client.with_options.return_value = scoped_client
        body = {"error": {"message": "unsupported response_format", "code": "invalid"}}
        scoped_client.chat.completions.create = AsyncMock(
            side_effect=_make_api_status_error(400, body)
        )
        seen: list[object] = []
        sentinel = _Schema(value="from-fallback")

        async def fake(self, *args, **kwargs):
            seen.append(self._client)
            return sentinel

        monkeypatch.setattr(BaseLLMProvider, "generate_structured", fake)
        result = await provider.generate_structured(
            system_prompt="sys", user_message="q", schema_type=_Schema
        )
        assert result is sentinel
        assert seen == [scoped_client]
        assert provider._client is original_client

    async def test_a_dropped_connection_in_the_text_fallback_is_tagged_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_provider()
        body = {"error": {"message": "unsupported response_format", "code": "invalid"}}
        provider._client.chat.completions.create = AsyncMock(
            side_effect=_make_api_status_error(400, body)
        )
        fallback = AsyncMock(
            side_effect=openai.APIConnectionError(request=httpx.Request("POST", _FAKE_URL))
        )
        monkeypatch.setattr(BaseLLMProvider, "generate_structured", fallback)
        with pytest.raises(ValueError, match=r"\[PROVIDER_UNAVAILABLE\]"):
            await provider.generate_structured(
                system_prompt="sys", user_message="q", schema_type=_Schema
            )


class TestIsTransientStatus:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (400, False),
            (408, True),
            (409, True),
            (429, False),
            (499, False),
            (500, True),
            (501, True),
            (529, True),
            (599, True),
            (600, False),
        ],
    )
    def test_classifies_statuses(self, status: int, expected: bool) -> None:
        assert is_transient_status(status) is expected
