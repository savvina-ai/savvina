# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Groq provider — fast inference via Groq's OpenAI-compatible Chat Completions endpoint."""

from __future__ import annotations

import logging

from ..config import get_settings
from .base import _HTTP_TIMEOUT_S, ModelInfo, _raise_if_fetch_error
from .openai_provider import OpenAIProvider, _parse_openai_models_response
from .registry import register_provider

logger = logging.getLogger(__name__)


@register_provider("groq")
class GroqProvider(OpenAIProvider):
    """Groq's fast LPU-accelerated inference via its OpenAI-compatible Chat Completions endpoint."""

    provider_name = "groq"
    display_name = "Groq"
    max_output_tokens = 32768
    context_window = 131_072  # 128 K — Llama 3.3 70B, Llama 4, GPT-OSS all share this limit

    _BASE_URL = "https://api.groq.com/openai/v1"
    _DEFAULT_MODEL = "llama-3.3-70b-versatile"
    default_model = "llama-3.3-70b-versatile"

    def __init__(
        self,
        api_key: str,
        default_model: str = "",
        verify_ssl: bool = True,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=self._BASE_URL,
            verify_ssl=verify_ssl,
            default_model=default_model or self._DEFAULT_MODEL,
        )

    async def health_check(self) -> tuple[bool, str]:
        """Verify connectivity with a minimal single-token completion request."""
        try:
            await self._client.chat.completions.create(
                model=self._default_model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            return True, ""
        except Exception as e:
            logger.warning("GroqProvider health check failed: %s", e, exc_info=True)
            return False, str(e)

    @classmethod
    def get_available_models(cls) -> list[str]:
        return []

    _GROQ_EXCLUDE = frozenset(["whisper", "orpheus", "prompt-guard", "safeguard", "playai"])

    @classmethod
    async def fetch_available_models(
        cls,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> list[ModelInfo]:
        if not api_key:
            return []
        import httpx

        verify = get_settings().verify_ssl
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, verify=verify) as client:
                resp = await client.get(
                    f"{cls._BASE_URL}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                resp.raise_for_status()
                data: list[dict] = resp.json().get("data", [])
            return _parse_openai_models_response(data, cls._GROQ_EXCLUDE)
        except Exception as exc:
            _raise_if_fetch_error(exc, "groq")
            return []

    @classmethod
    def get_config_schema(cls) -> dict:
        """Config schema for the frontend dynamic form."""
        return {
            "fields": [
                {
                    "name": "api_key",
                    "type": "password",
                    "label": "Groq API Key",
                    "required": True,
                    "placeholder": "gsk_...",
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
