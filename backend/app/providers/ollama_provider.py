# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Ollama local-LLM provider.

Ollama exposes an OpenAI-compatible API at ``/v1``, so this provider
inherits :class:`OpenAIProvider` and only overrides the constructor,
health check, and model list.
"""

from __future__ import annotations

import logging

import httpx
import openai

from .base import _HEALTH_CHECK_TIMEOUT_S, ModelInfo, _raise_if_fetch_error
from .openai_provider import OpenAIProvider
from .registry import register_provider

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "llama3"


@register_provider("ollama")
class OllamaProvider(OpenAIProvider):
    """LLM provider backed by a local Ollama server via its OpenAI-compatible API."""

    provider_name = "ollama"
    display_name = "Ollama (Local)"
    default_model = "llama3"

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        default_model: str = "",
        verify_ssl: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._verify_ssl = verify_ssl
        # Use the OpenAI-compatible endpoint; no real API key needed
        self._client = openai.AsyncOpenAI(
            api_key="ollama",
            base_url=f"{self._base_url}/v1",
            timeout=httpx.Timeout(120.0),
        )
        self._default_model = default_model or _DEFAULT_MODEL

    async def health_check(self) -> tuple[bool, str]:
        """Return (True, "") if the Ollama server responds on its native /api/tags endpoint."""
        try:
            async with httpx.AsyncClient(
                timeout=_HEALTH_CHECK_TIMEOUT_S, verify=self._verify_ssl
            ) as client:
                response = await client.get(f"{self._base_url}/api/tags")
                if response.status_code == 200:
                    return True, ""
                return False, f"Ollama returned HTTP {response.status_code}"
        except Exception as e:
            logger.warning("OllamaProvider health check failed: %s", e, exc_info=True)
            return False, str(e)

    @classmethod
    def get_available_models(cls) -> list[str]:
        return []

    async def list_running_models(self) -> list[str]:
        """Fetch the list of models currently available on the Ollama server."""
        try:
            async with httpx.AsyncClient(
                timeout=_HEALTH_CHECK_TIMEOUT_S, verify=self._verify_ssl
            ) as client:
                response = await client.get(f"{self._base_url}/api/tags")
                data = response.json()
                return [m["name"] for m in data.get("models", [])]
        except Exception:
            logger.debug("OllamaProvider.list_running_models failed", exc_info=True)
            return []

    @classmethod
    async def fetch_available_models(
        cls,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> list[ModelInfo]:
        """Fetch models actually pulled on the Ollama server via /api/tags."""
        effective_url = (base_url or "http://localhost:11434").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=_HEALTH_CHECK_TIMEOUT_S) as client:
                resp = await client.get(f"{effective_url}/api/tags")
                resp.raise_for_status()
                data = resp.json()
                return [ModelInfo(id=m["name"]) for m in data.get("models", [])]
        except Exception as exc:
            _raise_if_fetch_error(exc, "ollama")
            return []
