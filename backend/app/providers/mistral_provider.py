# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Mistral AI provider — via Mistral's OpenAI-compatible Chat Completions endpoint."""

from __future__ import annotations

import logging

from ..config import get_settings
from .base import _HTTP_TIMEOUT_S, ModelInfo, _raise_if_fetch_error
from .openai_provider import OpenAIProvider
from .registry import register_provider

logger = logging.getLogger(__name__)


@register_provider("mistral")
class MistralProvider(OpenAIProvider):
    """Mistral AI via its OpenAI-compatible Chat Completions endpoint."""

    provider_name = "mistral"
    display_name = "Mistral AI"
    max_output_tokens = 32768
    context_window = 131_072  # 128 K — mistral-large/small; open-mixtral-8x7b is 32 K (legacy)

    _BASE_URL = "https://api.mistral.ai/v1"
    _DEFAULT_MODEL = "mistral-large-latest"
    default_model = "mistral-large-latest"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        default_model: str = "",
        verify_ssl: bool = True,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url or self._BASE_URL,
            default_model=default_model or self._DEFAULT_MODEL,
            verify_ssl=verify_ssl,
        )

    async def health_check(self) -> tuple[bool, str]:
        """Verify connectivity with a minimal single-token completion request."""
        return await self._ping_completion()

    @classmethod
    def get_available_models(cls) -> list[str]:
        return []

    _MISTRAL_EXCLUDE = frozenset(["embed", "moderation"])

    @classmethod
    async def fetch_available_models(
        cls,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> list[ModelInfo]:
        if not api_key:
            return []
        import httpx

        url = f"{(base_url or cls._BASE_URL).rstrip('/')}/models"
        verify = get_settings().verify_ssl
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, verify=verify) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
                resp.raise_for_status()
                data: list[dict] = resp.json().get("data", [])
            return cls._parse_mistral_models(data)
        except Exception as exc:
            _raise_if_fetch_error(exc, "mistral")
            return []

    @classmethod
    def _parse_mistral_models(cls, data: list[dict]) -> list[ModelInfo]:
        """Parse Mistral's model list (uses capabilities.completion_chat and max_context_length)."""
        results: list[ModelInfo] = []
        seen: set[str] = set()
        for m in data:
            model_id: str = m.get("id", "")
            if not model_id or model_id in seen:
                continue
            caps = m.get("capabilities", {})
            if not caps.get("completion_chat", True):
                continue
            ctx: int | None = m.get("max_context_length")
            if ctx is not None and ctx < 4096:
                continue
            if any(kw in model_id.lower() for kw in cls._MISTRAL_EXCLUDE):
                continue
            seen.add(model_id)
            results.append(ModelInfo(id=model_id, context_window=ctx))
        return results

    @classmethod
    def get_config_schema(cls) -> dict:
        """Config schema for the frontend dynamic form."""
        return {
            "fields": [
                {
                    "name": "api_key",
                    "type": "password",
                    "label": "Mistral API Key",
                    "required": True,
                    "placeholder": "...",
                },
                {
                    "name": "model",
                    "type": "string",
                    "label": "Model",
                    "required": False,
                    "placeholder": f"e.g. {cls._DEFAULT_MODEL}",
                },
            ],
        }
