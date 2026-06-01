# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""OpenAI-compatible provider — GitHub Models, HuggingFace, Together, OpenRouter."""

from __future__ import annotations

import logging
import re as _re
import typing

import httpx
import openai

from ..config import get_settings
from .base import _HTTP_TIMEOUT_S, ModelInfo, _raise_if_fetch_error
from .openai_provider import OpenAIProvider, _parse_openai_models_response
from .registry import register_provider

logger = logging.getLogger(__name__)

_AZUREML_URI_RE = _re.compile(r"azureml://[^/]+/[^/]+/models/([^/]+)/")


def _normalize_model_id(model_id: str) -> str:
    """Convert an azureml:// URI to its short model name; leave other IDs unchanged."""
    m = _AZUREML_URI_RE.match(model_id)
    return m.group(1) if m else model_id


@register_provider("openai_compatible")
class OpenAICompatibleProvider(OpenAIProvider):
    """Generic provider for any service exposing an OpenAI-compatible Chat Completions API.

    For use with GitHub Models, HuggingFace Inference, Together.ai, OpenRouter,
    or any custom base_url endpoint.  Named services (Gemini, Groq, Cerebras,
    Mistral) have their own dedicated provider classes.
    ``generate_response`` is inherited from OpenAIProvider unchanged.
    """

    provider_name = "openai_compatible"
    display_name = "Custom OpenAI-Compatible"

    _OPENROUTER_BASE = "https://openrouter.ai/api/v1"
    _OPENROUTER_HEADERS: typing.ClassVar[dict[str, str]] = {
        "HTTP-Referer": "https://github.com/savvina-ai/savvina",
        "X-Title": "Savvina",
    }

    def __init__(
        self,
        api_key: str,
        base_url: str,
        default_model: str = "",
        verify_ssl: bool = True,
    ) -> None:
        super().__init__(api_key=api_key, base_url=base_url, verify_ssl=verify_ssl)
        self._default_model = default_model  # overrides parent's "gpt-4o" fallback
        if base_url.rstrip("/") == self._OPENROUTER_BASE.rstrip("/"):
            self._client = openai.AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                default_headers=self._OPENROUTER_HEADERS,
                timeout=httpx.Timeout(120.0),
                http_client=None if verify_ssl else httpx.AsyncClient(verify=False),  # noqa: S501
            )

    async def health_check(self) -> tuple[bool, str]:
        """Test connectivity with a minimal single-token completion request."""
        if not self._default_model:
            return False, "No model configured — set a model name before testing"
        try:
            await self._client.chat.completions.create(
                model=self._default_model,
                messages=[{"role": "user", "content": "SELECT 1"}],
                max_tokens=10,
            )
            return True, ""
        except Exception as e:
            logger.warning("OpenAICompatibleProvider health check failed: %s", e, exc_info=True)
            return False, str(e)

    @classmethod
    def get_available_models(cls) -> list[str]:
        # Models are service-specific; return an empty list so the UI shows
        # a free-text field instead of a dropdown.
        return []

    @classmethod
    async def fetch_available_models(
        cls,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> list[ModelInfo]:
        if not api_key or not base_url:
            return []
        url = f"{base_url.rstrip('/')}/models"
        headers: dict[str, str] = {"Authorization": f"Bearer {api_key}"}
        # OpenRouter requires attribution headers
        if base_url.rstrip("/") == cls._OPENROUTER_BASE.rstrip("/"):
            headers.update(cls._OPENROUTER_HEADERS)
        verify = get_settings().verify_ssl
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, verify=verify) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                body = resp.json()
                data: list[dict] = body if isinstance(body, list) else body.get("data", [])
            # Pre-normalise Azure entries: use the short `name` field as the id and
            # skip non-chat models (embeddings, etc.) when a task field is present.
            normalized: list[dict] = []
            for entry in data:
                task = entry.get("task")
                if task and task != "chat-completion":
                    continue
                orig_id = entry.get("id", "")
                # GitHub/Azure entries use `name` as the short model identifier;
                # OpenRouter (and others) use `id` directly — don't overwrite it.
                if task is not None and entry.get("name"):
                    entry = {**entry, "id": entry["name"]}
                elif orig_id.startswith("azureml://"):
                    entry = {**entry, "id": _normalize_model_id(orig_id)}
                normalized.append(entry)
            # No keyword exclusions — user explicitly chose this endpoint
            return _parse_openai_models_response(normalized, frozenset())
        except Exception as exc:
            _raise_if_fetch_error(exc, f"openai_compatible ({base_url})")
            return []

    @classmethod
    def get_config_schema(cls) -> dict:
        """Config schema for the frontend dynamic form."""
        return {
            "fields": [
                {
                    "name": "base_url",
                    "type": "select",
                    "label": "Service",
                    "required": True,
                    "options": [
                        {
                            "label": "GitHub Models (Free)",
                            "value": "https://models.inference.ai.azure.com",
                        },
                        {
                            "label": "HuggingFace (Free)",
                            "value": "https://router.huggingface.co/v1",
                        },
                        {"label": "Together.ai", "value": "https://api.together.xyz/v1"},
                        {"label": "OpenRouter", "value": "https://openrouter.ai/api/v1"},
                        {"label": "Custom URL", "value": "custom"},
                    ],
                },
                {
                    "name": "custom_base_url",
                    "type": "string",
                    "label": "Custom Base URL",
                    "required": False,
                    "placeholder": "https://your-service.com/v1",
                    "required_if": {"base_url": "custom"},
                },
                {
                    "name": "api_key",
                    "type": "password",
                    "label": "API Key",
                    "required": True,
                },
                {
                    "name": "model",
                    "type": "string",
                    "label": "Model Name",
                    "required": True,
                    "placeholder": "e.g., gpt-4o-mini, meta-llama/Llama-3.3-70B",
                },
            ],
            "presets": {
                "github": {
                    "base_url": "https://models.inference.ai.azure.com",
                    "model": "DeepSeek-R1",
                },
                "huggingface": {
                    "base_url": "https://router.huggingface.co/v1",
                    "model": "Qwen/Qwen2.5-Coder-32B-Instruct",
                },
                "together": {
                    "base_url": "https://api.together.xyz/v1",
                    "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
                },
                "openrouter": {
                    "base_url": "https://openrouter.ai/api/v1",
                    "model": "openrouter/free",
                },
            },
        }
