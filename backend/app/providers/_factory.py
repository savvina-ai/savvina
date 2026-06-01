# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Shared provider resolution logic used by both the pipeline and semantic router."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

from sqlalchemy import select

from ..config import get_settings
from ..models.provider import ProviderConfig
from ..utils.encryption import decrypt_value
from .base import BaseLLMProvider
from .registry import create_provider

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def resolve_provider_config(
    provider_id_or_name: str,
    db: AsyncSession,
) -> tuple[BaseLLMProvider, str, str, int]:
    """Resolve and instantiate an LLM provider from a UUID or provider-type name.

    Tries UUID lookup first so that multiple openai_compatible instances (e.g. Groq
    vs Gemini) can be distinguished by their unique config ID.

    Returns ``(provider, configured_model, provider_name, configured_max_tokens)``.
    Raises ``ValueError`` if no API key is configured for the resolved provider.
    """
    settings = get_settings()

    config: ProviderConfig | None = None

    # Try UUID lookup first (frontend sends config UUID for specific instances)
    try:
        uuid.UUID(provider_id_or_name)
        result = await db.execute(
            select(ProviderConfig).where(ProviderConfig.id == provider_id_or_name)
        )
        config = result.scalar_one_or_none()
    except ValueError:
        config = None

    if config is not None:
        provider_name = config.provider_type
    else:
        # Fall back to provider-type name (most recently updated config wins)
        provider_name = provider_id_or_name
        result = await db.execute(
            select(ProviderConfig)
            .where(ProviderConfig.provider_type == provider_name)
            .order_by(ProviderConfig.updated_at.desc())
        )
        config = result.scalars().first()

    configured_model: str = config.model if config else ""
    configured_max_tokens: int = config.max_tokens if config else 4096

    # Ollama needs no API key — only a base URL
    if provider_name == "ollama":
        base_url = config.base_url if (config and config.base_url) else settings.ollama_base_url
        return (
            create_provider("ollama", base_url=base_url),
            configured_model,
            provider_name,
            configured_max_tokens,
        )

    # API-key-based providers
    api_key: str | None = None
    base_url: str | None = None

    if config and config.api_key_encrypted:
        api_key = decrypt_value(config.api_key_encrypted, settings.encryption_key)
        base_url = config.base_url
    else:
        api_key = settings.env_api_key(provider_name)

    if not api_key:
        raise ValueError(f"No API key configured for provider '{provider_name}'")

    kwargs: dict[str, Any] = {"api_key": api_key}
    kwargs["verify_ssl"] = settings.verify_ssl
    if base_url:
        kwargs["base_url"] = base_url
    if configured_model:
        kwargs["default_model"] = configured_model

    return (
        create_provider(provider_name, **kwargs),
        configured_model,
        provider_name,
        configured_max_tokens,
    )
