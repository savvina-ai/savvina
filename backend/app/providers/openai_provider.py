# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""OpenAI GPT provider."""

from __future__ import annotations

import json
import logging

import httpx
import openai

from ..config import get_settings
from .base import (
    _BM,
    _HTTP_TIMEOUT_S,
    BaseLLMProvider,
    LLMResponse,
    ModelInfo,
    _raise_if_fetch_error,
    parse_llm_response,
)
from .registry import register_provider

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gpt-4o"


# Keywords in a model id that indicate it is NOT a chat/completion model
_OPENAI_EXCLUDE_KEYWORDS = frozenset(
    ["embedding", "whisper", "dall-e", "tts", "transcription", "realtime", "moderation", "search"]
)


def _parse_openai_models_response(
    data: list[dict],
    keyword_exclusions: frozenset[str] = frozenset(),
) -> list[ModelInfo]:
    """Parse an OpenAI-compatible ``/models`` response into a filtered list of ModelInfo.

    Handles both the minimal format (id only) and the richer Groq-style format
    (context_window, max_completion_tokens, active).  Also accepts OpenRouter's
    ``context_length`` field as a fallback for ``context_window``.
    """
    results: list[ModelInfo] = []
    all_exclusions = _OPENAI_EXCLUDE_KEYWORDS | keyword_exclusions
    for entry in data:
        model_id: str = entry.get("id", "")
        if not model_id:
            continue
        # Drop inactive models when the provider explicitly signals it
        if entry.get("active") is False:
            continue
        # Keyword-based exclusion (case-insensitive)
        lower_id = model_id.lower()
        if any(kw in lower_id for kw in all_exclusions):
            continue
        # Context window: prefer context_window, fall back to context_length (OpenRouter)
        ctx: int | None = entry.get("context_window") or entry.get("context_length")
        if isinstance(ctx, int) and ctx < 4096:
            continue
        max_tokens: int | None = entry.get("max_completion_tokens")
        results.append(
            ModelInfo(
                id=model_id,
                context_window=ctx if isinstance(ctx, int) else None,
                max_completion_tokens=max_tokens if isinstance(max_tokens, int) else None,
            )
        )
    return results


@register_provider("openai")
class OpenAIProvider(BaseLLMProvider):
    """LLM provider backed by the OpenAI Chat Completions API."""

    provider_name = "openai"
    display_name = "OpenAI GPT"
    default_model = "gpt-4o"
    max_output_tokens = 16384

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        verify_ssl: bool = True,
        default_model: str = "",
    ) -> None:
        kwargs: dict = {"api_key": api_key, "timeout": httpx.Timeout(120.0)}
        if base_url:
            kwargs["base_url"] = base_url
        # Allow runtime override for environments with TLS interception.
        if not verify_ssl:
            kwargs["http_client"] = httpx.AsyncClient(verify=False)  # noqa: S501
        self._client = openai.AsyncOpenAI(**kwargs)
        self._default_model = default_model or _DEFAULT_MODEL

    @staticmethod
    def _normalize_api_error(exc: openai.APIStatusError) -> ValueError:
        """Convert an openai.APIStatusError into a descriptive ValueError.

        Covers Gemini, Groq, Cerebras, Mistral, and any other OpenAI-compatible
        provider that routes through this class.
        """
        try:
            body = exc.response.json()
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}
        # Groq nests under "error"; Cerebras puts fields at the top level
        err = body.get("error") or body
        api_msg = err.get("message", "") if isinstance(err, dict) else ""
        code = err.get("code", "") if isinstance(err, dict) else ""

        if exc.status_code == 402:
            return ValueError(
                "[PAYMENT_REQUIRED] Your HuggingFace monthly credits are exhausted. "
                "Purchase pre-paid credits at huggingface.co, or switch to a "
                "different provider (e.g. Claude, Gemini, OpenAI)."
            )
        if exc.status_code == 413:
            return ValueError(
                f"[TPM_EXCEEDED] Request too large: token rate limit exceeded. {api_msg} "
                f"Try a provider with higher limits (e.g. Claude, Gemini, OpenAI)."
            )
        # Groq returns 429 for per-minute token limit errors where the request itself
        # is too large (not just transient rate limiting). Detect by message content.
        if exc.status_code == 429 and "token rate limit exceeded" in api_msg.lower():
            return ValueError(
                f"[TPM_EXCEEDED] Request too large: token rate limit exceeded. {api_msg} "
                f"Try a provider with higher limits (e.g. Claude, Gemini, OpenAI)."
            )
        if exc.status_code == 400 and code == "context_length_exceeded":
            return ValueError(
                f"[CONTEXT_EXCEEDED] Prompt too large for this model's context window."
                f" {api_msg} Try a provider with a larger context window"
                f" (e.g. Claude, Gemini, OpenAI), or reduce the number of tables."
            )
        if exc.status_code == 429:
            hint = api_msg or "You have exceeded the provider's rate limit or quota."
            return ValueError(f"[RATE_LIMIT] {hint}")
        if exc.status_code in (401, 403):
            return ValueError(
                f"[AUTH_ERROR] Invalid or expired API key (HTTP {exc.status_code}). "
                f"Check your provider credentials."
            )
        return ValueError(f"[PROVIDER_ERROR] Provider returned HTTP {exc.status_code}. {api_msg}")

    async def generate_response(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: list[dict],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        # Mistral (code 3240) rejects assistant messages with null content; coerce to "".
        _history = [
            {**m, "content": m.get("content") or "[no response]"}
            if m.get("role") == "assistant"
            else m
            for m in conversation_history
        ]
        messages = [
            {"role": "system", "content": system_prompt},
            *_history,
            {"role": "user", "content": user_message},
        ]
        try:
            response = await self._client.chat.completions.create(
                model=model or self._default_model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except openai.APIStatusError as exc:
            raise self._normalize_api_error(exc) from exc
        if not response.choices:
            raise ValueError(
                "[EMPTY_RESPONSE] The model returned no choices. "
                "This can happen with free-tier routing (e.g. openrouter/free) when the "
                "selected model is overloaded or filtered the request. Try again or use a "
                "specific model name."
            )
        raw = response.choices[0].message.content or ""
        usage = response.usage
        tokens = usage.total_tokens if usage else None
        input_tokens = usage.prompt_tokens if usage else None
        output_tokens = usage.completion_tokens if usage else None
        truncated = response.choices[0].finish_reason == "length"
        return parse_llm_response(
            raw,
            response.model,
            tokens,
            truncated=truncated,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    async def generate_structured(
        self,
        system_prompt: str,
        user_message: str,
        schema_type: type[_BM],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> _BM:
        """Use OpenAI JSON-object mode to constrain the response to valid JSON."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        try:
            response = await self._client.chat.completions.create(
                model=model or self._default_model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format={"type": "json_object"},
            )
        except openai.APIStatusError as exc:
            # Fall back to base (text-mode) only for 400 — provider doesn't support json_object.
            # Any other status (429 rate-limit, 401 auth, …) is a real error; surface it.
            if exc.status_code != 400:
                raise self._normalize_api_error(exc) from exc
            return await super().generate_structured(
                system_prompt,
                user_message,
                schema_type,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        if not response.choices:
            raise ValueError("OpenAIProvider.generate_structured: no choices in response")
        raw = response.choices[0].message.content or "{}"
        return schema_type.model_validate(json.loads(raw))

    async def health_check(self) -> tuple[bool, str]:
        """Verify connectivity by listing available models."""
        try:
            await self._client.models.list()
            return True, ""
        except Exception as e:
            logger.warning("OpenAIProvider health check failed: %s", e, exc_info=True)
            return False, str(e)

    async def _ping_completion(self) -> tuple[bool, str]:
        """Verify connectivity with a minimal single-token completion request.

        Subclasses that don't support the /models endpoint (Gemini, Mistral, Cerebras)
        delegate their health_check to this method.
        """
        try:
            await self._client.chat.completions.create(
                model=self._default_model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            return True, ""
        except Exception as e:
            logger.warning("%s health check failed: %s", self.provider_name, e, exc_info=True)
            return False, str(e)

    @classmethod
    def get_available_models(cls) -> list[str]:
        return []

    @classmethod
    async def fetch_available_models(
        cls,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> list[ModelInfo]:
        if not api_key:
            return []
        url = f"{(base_url or 'https://api.openai.com/v1').rstrip('/')}/models"
        verify = get_settings().verify_ssl
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, verify=verify) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
                resp.raise_for_status()
                data: list[dict] = resp.json().get("data", [])
            return _parse_openai_models_response(data)
        except Exception as exc:
            _raise_if_fetch_error(exc, "openai")
            return []
