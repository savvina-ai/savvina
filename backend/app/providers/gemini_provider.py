# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Google Gemini provider — via OpenAI-compatible Chat Completions endpoint."""

from __future__ import annotations

import logging

from ..config import get_settings
from .base import _HTTP_TIMEOUT_S, ModelInfo, _raise_if_fetch_error
from .openai_provider import OpenAIProvider
from .registry import register_provider

logger = logging.getLogger(__name__)


@register_provider("gemini")
class GeminiProvider(OpenAIProvider):
    """Google Gemini via its OpenAI-compatible Chat Completions endpoint."""

    provider_name = "gemini"
    display_name = "Google Gemini"
    max_output_tokens = 65536
    context_window = 1_048_576  # 1 M tokens — Gemini 2.5 Flash / 2.0 Flash

    _BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
    _DEFAULT_MODEL = "gemini-2.5-flash"
    default_model = "gemini-2.5-flash"

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
            url = "https://generativelanguage.googleapis.com/v1beta/models"
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, verify=verify) as client:
                resp = await client.get(url, headers={"x-goog-api-key": api_key})
                resp.raise_for_status()
                models_raw: list[dict] = resp.json().get("models", [])
            results: list[ModelInfo] = []
            for entry in models_raw:
                methods: list[str] = entry.get("supportedGenerationMethods", [])
                if "generateContent" not in methods:
                    continue
                name: str = entry.get("name", "")
                model_id = name.removeprefix("models/")
                if model_id:
                    results.append(ModelInfo(id=model_id))
            return results
        except Exception as exc:
            _raise_if_fetch_error(exc, "gemini")
            return []

    @classmethod
    def get_config_schema(cls) -> dict:
        """Config schema for the frontend dynamic form."""
        return {
            "fields": [
                {
                    "name": "api_key",
                    "type": "password",
                    "label": "Gemini API Key",
                    "required": True,
                    "placeholder": "AIza...",
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
