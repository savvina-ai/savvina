# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Anthropic Claude provider."""

from __future__ import annotations

import logging

import anthropic
from anthropic import AsyncAnthropic
import httpx

from ..config import get_settings
from .base import (
    _BM,
    BaseLLMProvider,
    LLMResponse,
    ModelInfo,
    _raise_if_fetch_error,
    parse_llm_response,
)
from .registry import register_provider

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-sonnet-4-6"


@register_provider("claude")
class ClaudeProvider(BaseLLMProvider):
    """LLM provider backed by Anthropic's Claude API."""

    provider_name = "claude"
    display_name = "Anthropic Claude"
    default_model = "claude-sonnet-4-6"
    max_output_tokens = 16000  # Claude 3.5+ models; conservative vs the 64K extended-output limit
    supports_prompt_caching = True

    def __init__(
        self,
        api_key: str,
        default_model: str = "",
        verify_ssl: bool = True,
    ) -> None:
        kwargs: dict = {"api_key": api_key, "timeout": httpx.Timeout(120.0)}
        if not verify_ssl:
            kwargs["http_client"] = httpx.AsyncClient(verify=False)  # noqa: S501
        self._client = AsyncAnthropic(**kwargs)
        self._default_model = default_model or _DEFAULT_MODEL

    @staticmethod
    def _normalize_api_error(exc: anthropic.APIStatusError) -> ValueError:
        """Convert an anthropic.APIStatusError into a descriptive ValueError."""
        api_msg = str(exc.message) if hasattr(exc, "message") else str(exc)
        if exc.status_code == 429:
            msg = api_msg or "Anthropic rate limit exceeded. Please retry later."
            return ValueError(f"[RATE_LIMIT] {msg}")
        if exc.status_code in (401, 403):
            return ValueError(
                f"[AUTH_ERROR] Invalid or expired Anthropic API key (HTTP {exc.status_code}). "
                f"Check your provider credentials."
            )
        if exc.status_code == 400:
            return ValueError(f"[BAD_REQUEST] {api_msg}")
        return ValueError(f"[PROVIDER_ERROR] Anthropic returned HTTP {exc.status_code}. {api_msg}")

    async def generate_response(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: list[dict],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        messages = [*conversation_history, {"role": "user", "content": user_message}]
        try:
            response = await self._client.messages.create(
                model=model or self._default_model,
                system=system_prompt,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except anthropic.APIStatusError as exc:
            raise self._normalize_api_error(exc) from exc
        raw = response.content[0].text
        usage = response.usage
        input_tokens = usage.input_tokens if usage else None
        output_tokens = usage.output_tokens if usage else None
        tokens = (
            (input_tokens or 0) + (output_tokens or 0)
            if input_tokens is not None or output_tokens is not None
            else None
        )
        truncated = response.stop_reason == "max_tokens"
        return parse_llm_response(
            raw,
            response.model,
            tokens,
            truncated=truncated,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    async def generate_response_cached(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: list[dict],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Claude-specific: wraps the system prompt in a cache_control block (5-min TTL).

        On repeated calls within the TTL the cached prefix is served at ~10% of the write
        cost — beneficial when the same system prompt is sent across multiple batch calls.
        """
        messages = [*conversation_history, {"role": "user", "content": user_message}]
        try:
            response = await self._client.messages.create(
                model=model or self._default_model,
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except anthropic.APIStatusError as exc:
            raise self._normalize_api_error(exc) from exc
        raw = response.content[0].text
        usage = response.usage
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        tokens = (
            (input_tokens or 0) + (output_tokens or 0)
            if input_tokens is not None or output_tokens is not None
            else None
        )
        truncated = response.stop_reason == "max_tokens"
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
        """Use Anthropic tool-use to constrain the response to the given Pydantic schema."""
        _tool_name = "submit_structured_result"
        try:
            response = await self._client.messages.create(
                model=model or self._default_model,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
                tools=[
                    {
                        "name": _tool_name,
                        "description": "Submit the structured result as required by the request.",
                        "input_schema": schema_type.model_json_schema(),
                    }
                ],
                tool_choice={"type": "tool", "name": _tool_name},
                max_tokens=max_tokens,
            )
        except anthropic.APIStatusError as exc:
            raise self._normalize_api_error(exc) from exc
        for block in response.content:
            if block.type == "tool_use" and block.name == _tool_name:
                return schema_type.model_validate(block.input)
        raise ValueError("ClaudeProvider.generate_structured: no tool_use block in response")

    async def health_check(self) -> tuple[bool, str]:
        """Verify connectivity by sending a minimal one-token request."""
        try:
            await self._client.messages.create(
                model=self._default_model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            return True, ""
        except Exception as e:
            logger.warning("ClaudeProvider health check failed: %s", e, exc_info=True)
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
        verify = get_settings().verify_ssl
        try:
            async with httpx.AsyncClient(timeout=15.0, verify=verify) as client:
                resp = await client.get(
                    "https://api.anthropic.com/v1/models",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                    },
                )
                resp.raise_for_status()
                data: list[dict] = resp.json().get("data", [])
            return [ModelInfo(id=m["id"]) for m in data if m.get("id", "").startswith("claude-")]
        except Exception as exc:
            _raise_if_fetch_error(exc, "claude")
            return []
