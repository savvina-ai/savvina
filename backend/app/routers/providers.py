# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Providers router — list, configure, and health-check LLM providers."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from typing import TYPE_CHECKING
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_active_user
from ..config import get_settings
from ..database import get_db
from ..models.provider import ProviderConfig
from ..models.user import User
from ..providers.base import ModelInfo, ProviderAuthError, ProviderConnectError
from ..providers.registry import create_provider, get_provider_class, list_available_providers
from ..schemas.pagination import PaginatedResponse
from ..schemas.provider import FetchModelsRequest, ProviderConfigUpdate, ProviderStatusResponse
from ..utils.encryption import decrypt_value, encrypt_value

router = APIRouter(prefix="/providers", tags=["providers"])


def _is_configured(provider_name: str, config: ProviderConfig | None, settings) -> bool:
    """Return True when the provider has usable credentials."""
    if provider_name == "ollama":
        # Ollama requires no API key, but only treat it as configured when a
        # DB record exists OR OLLAMA_BASE_URL was explicitly set in the env
        # (not just the compile-time default).  This prevents the provider
        # from appearing as "env-configured" on installations that have never
        # touched Ollama.
        return config is not None or bool(os.environ.get("OLLAMA_BASE_URL"))
    if config and config.api_key_encrypted:
        return True
    return bool(settings.env_api_key(provider_name))


def _build_status(
    provider_name: str,
    config: ProviderConfig | None,
    settings,
    *,
    is_healthy: bool = False,
) -> ProviderStatusResponse:
    """Assemble a ProviderStatusResponse from static metadata + saved config."""
    try:
        cls = get_provider_class(provider_name)
        display_name: str = (
            config.display_name if config and config.display_name else cls.display_name
        )
        provider_display_name: str = cls.display_name
        cache_json = config.models_cache_json if config else None
        if isinstance(cache_json, str) and cache_json:
            available_models: list[str] = sorted(
                m["id"] for m in json.loads(cache_json) if m.get("id")
            )
        else:
            available_models = cls.get_available_models()
        raw_model = config.model if config else ""
        current_model: str = raw_model or cls.default_model
    except ValueError:
        display_name = config.display_name if config else provider_name
        provider_display_name = display_name
        available_models = []
        current_model = config.model if config else ""

    return ProviderStatusResponse(
        id=config.id if config else None,
        provider_type=provider_name,
        display_name=display_name,
        provider_display_name=provider_display_name,
        is_configured=_is_configured(provider_name, config, settings),
        is_healthy=is_healthy,
        is_active=config.is_active if config else False,
        current_model=current_model,
        available_models=available_models,
        base_url=config.base_url if config else None,
        updated_at=config.updated_at if config else None,
    )


async def _get_config_or_404(
    config_id: str,
    db: AsyncSession,
) -> ProviderConfig:
    """Fetch a ProviderConfig by UUID id; raise 404 if absent."""
    stmt = select(ProviderConfig).where(ProviderConfig.id == config_id)
    result = await db.execute(stmt)
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail=f"Provider config '{config_id}' not found")
    return config


def _apply_update(config: ProviderConfig, body: ProviderConfigUpdate, settings) -> None:
    """Apply non-None fields from *body* onto *config* in-place, re-encrypting the API key if
    changed."""
    if body.api_key:
        config.api_key_encrypted = encrypt_value(body.api_key, settings.encryption_key)
    if body.base_url is not None:
        config.base_url = body.base_url
    if body.model is not None:
        config.model = body.model
    if body.temperature is not None:
        config.temperature = body.temperature
    if body.max_tokens is not None:
        config.max_tokens = body.max_tokens
    if body.is_active is not None:
        config.is_active = body.is_active
    if body.display_name is not None:
        config.display_name = body.display_name
    config.updated_at = datetime.now(UTC)


class ProviderTestRequest(BaseModel):
    """Payload for testing a provider before saving a config."""

    provider_type: str
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None


def _build_provider_kwargs(
    provider_name: str,
    api_key: str | None,
    model: str | None,
    base_url: str | None,
    settings,
) -> dict:
    """Build constructor kwargs for any non-Ollama provider from user-supplied config fields.

    Raises ValueError when *api_key* is absent — Ollama is the only provider
    that does not require a key and must be handled separately by callers.
    """
    if not api_key:
        raise ValueError(f"No API key configured for provider '{provider_name}'")
    kwargs: dict = {"api_key": api_key, "verify_ssl": settings.verify_ssl}
    if base_url:
        kwargs["base_url"] = base_url
    if model:
        kwargs["default_model"] = model
    return kwargs


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.get("", response_model=PaginatedResponse[ProviderStatusResponse])
async def list_providers(
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[ProviderStatusResponse]:
    """List all saved provider configs plus any registered providers without a config."""
    settings = get_settings()
    result = await db.execute(select(ProviderConfig).order_by(ProviderConfig.updated_at.desc()))
    all_configs = result.scalars().all()
    responses = [_build_status(cfg.provider_type, cfg, settings) for cfg in all_configs]

    configured_types = {cfg.provider_type for cfg in all_configs}
    for p in list_available_providers():
        if p["name"] not in configured_types:
            responses.append(_build_status(p["name"], None, settings))

    total = len(responses)
    page = responses[offset : offset + limit]
    return PaginatedResponse(items=page, total=total, limit=limit, offset=offset)


@router.post("/test")
async def test_new_provider(
    body: ProviderTestRequest,
    _user: User = Depends(get_current_active_user),
) -> dict:
    """Test provider credentials before saving a config."""
    settings = get_settings()
    kwargs: dict = {}
    try:
        if body.provider_type == "ollama":
            kwargs["base_url"] = body.base_url or settings.ollama_base_url
        else:
            api_key = body.api_key or settings.env_api_key(body.provider_type)
            kwargs = _build_provider_kwargs(
                body.provider_type, api_key, body.model, body.base_url, settings
            )
        provider = create_provider(body.provider_type, **kwargs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    is_ok, detail = await provider.health_check()
    if is_ok:
        return {"success": True, "message": f"{body.provider_type} is healthy"}
    return {"success": False, "message": detail}


@router.post("/models", response_model=list[str])
async def fetch_models_for_type(
    body: FetchModelsRequest,
    _user: User = Depends(get_current_active_user),
) -> list[str]:
    """Fetch available models from a provider API using the supplied credentials.

    Intended for use *before* saving a config so the user can pick a model
    from a live list.  Falls back to the hardcoded model list on error.
    """
    try:
        cls = get_provider_class(body.provider_type)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    settings = get_settings()
    api_key = body.api_key or settings.env_api_key(body.provider_type)
    try:
        models: list[ModelInfo] = await cls.fetch_available_models(api_key, body.base_url)
    except ProviderConnectError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ProviderAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return sorted(m.id for m in models)


@router.post(
    "/{provider_type}",
    response_model=ProviderStatusResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_provider_config(
    provider_type: str,
    body: ProviderConfigUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ProviderStatusResponse:
    """Create a new saved configuration for a provider type (admin only)."""
    try:
        get_provider_class(provider_type)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    settings = get_settings()
    config = ProviderConfig(
        id=str(uuid.uuid4()),
        provider_type=provider_type,
        display_name=body.display_name or provider_type,
        is_active=False,
        model="",
        temperature=0.0,
        max_tokens=4096,
        updated_at=datetime.now(UTC),
    )
    db.add(config)
    _apply_update(config, body, settings)
    await db.commit()
    await db.refresh(config)
    return _build_status(provider_type, config, settings)


@router.get("/{config_id}", response_model=ProviderStatusResponse)
async def get_provider(
    config_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ProviderStatusResponse:
    """Return the status of a specific saved provider config."""
    settings = get_settings()
    config = await _get_config_or_404(config_id, db)
    return _build_status(config.provider_type, config, settings)


@router.put("/{config_id}/config", response_model=ProviderStatusResponse)
async def update_provider_config(
    config_id: str,
    body: ProviderConfigUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ProviderStatusResponse:
    """Update a saved provider config by its UUID id (admin only)."""
    settings = get_settings()
    config = await _get_config_or_404(config_id, db)
    try:
        get_provider_class(config.provider_type)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    _apply_update(config, body, settings)
    await db.commit()
    await db.refresh(config)
    return _build_status(config.provider_type, config, settings)


@router.delete("/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider_config(
    config_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a saved provider configuration by its UUID id (admin only)."""
    config = await _get_config_or_404(config_id, db)
    await db.delete(config)
    await db.commit()


@router.post("/{config_id}/models", response_model=list[str])
async def refresh_saved_provider_models(
    config_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[str]:
    """Fetch models from the provider API using a saved config's credentials.

    Persists the result to ``models_cache_json`` so subsequent loads of the
    settings page reflect the live model list without re-fetching.
    """
    settings = get_settings()
    config = await _get_config_or_404(config_id, db)
    provider_name = config.provider_type
    try:
        cls = get_provider_class(provider_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    api_key: str | None = None
    if provider_name != "ollama":
        if config.api_key_encrypted:
            api_key = decrypt_value(config.api_key_encrypted, settings.encryption_key)
        else:
            api_key = settings.env_api_key(provider_name)

    base_url = config.base_url or (settings.ollama_base_url if provider_name == "ollama" else None)
    try:
        models: list[ModelInfo] = await cls.fetch_available_models(api_key, base_url)
    except ProviderConnectError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ProviderAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Persist to cache
    sorted_models = sorted(models, key=lambda m: m.id)
    config.models_cache_json = json.dumps(
        [
            {
                "id": m.id,
                "context_window": m.context_window,
                "max_completion_tokens": m.max_completion_tokens,
            }
            for m in sorted_models
        ]
    )
    config.updated_at = datetime.now(UTC)
    await db.commit()

    return [m.id for m in sorted_models]


@router.post("/{config_id}/test")
async def test_provider(
    config_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Instantiate a saved provider config and run its health check."""
    settings = get_settings()
    config = await _get_config_or_404(config_id, db)
    provider_name = config.provider_type

    kwargs: dict = {}
    if provider_name == "ollama":
        kwargs["base_url"] = config.base_url or settings.ollama_base_url
    else:
        api_key: str | None = None
        if config.api_key_encrypted:
            api_key = decrypt_value(config.api_key_encrypted, settings.encryption_key)
        else:
            api_key = settings.env_api_key(provider_name)

        try:
            kwargs = _build_provider_kwargs(
                provider_name, api_key, config.model or None, config.base_url, settings
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None

    try:
        provider = create_provider(provider_name, **kwargs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    is_ok, detail = await provider.health_check()
    if is_ok:
        return {"success": True, "message": f"{provider_name} is healthy"}
    return {"success": False, "message": detail}
