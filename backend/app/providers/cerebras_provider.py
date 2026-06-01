# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Cerebras provider — fast inference via Cerebras's OpenAI-compatible Chat Completions endpoint."""

from __future__ import annotations

import logging

from ..config import get_settings
from .base import _HTTP_TIMEOUT_S, ModelInfo, _raise_if_fetch_error
from .openai_provider import OpenAIProvider, _parse_openai_models_response
from .registry import register_provider

logger = logging.getLogger(__name__)


@register_provider("cerebras")
class CerebrasProvider(OpenAIProvider):
    """Cerebras inference via its OpenAI-compatible Chat Completions endpoint."""

    provider_name = "cerebras"
    display_name = "Cerebras"
    chars_per_token = 2.0

    _BASE_URL = "https://api.cerebras.ai/v1"
    _DEFAULT_MODEL = "qwen-3-235b-a22b-instruct-2507"
    default_model = "qwen-3-235b-a22b-instruct-2507"

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

        url = f"{(base_url or cls._BASE_URL).rstrip('/')}/models"
        verify = get_settings().verify_ssl
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, verify=verify) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
                resp.raise_for_status()
                data: list[dict] = resp.json().get("data", [])
            return _parse_openai_models_response(data)
        except Exception as exc:
            _raise_if_fetch_error(exc, "cerebras")
            return []

    @classmethod
    def get_config_schema(cls) -> dict:
        """Config schema for the frontend dynamic form."""
        return {
            "fields": [
                {
                    "name": "api_key",
                    "type": "password",
                    "label": "Cerebras API Key",
                    "required": True,
                    "placeholder": "csk-...",
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
