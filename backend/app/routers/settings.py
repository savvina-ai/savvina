# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Settings router — read and update application settings (persisted to PostgreSQL)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_active_user
from ..config import (
    DEFAULT_BCRYPT_ROUNDS,
    MUTABLE_SETTING_PARSERS,
    SINGLETON_SETTING_PARSERS,
    get_settings,
)
from ..database import get_db
from ..models.app_settings import AppSetting
from ..models.user import User
from ..schemas.settings import SettingsResponse, SettingsUpdate

router = APIRouter(prefix="/settings", tags=["settings"])


def _write_through_to_singleton(body: SettingsUpdate) -> None:
    """Apply a saved update to the cached ``Settings`` object as well as the DB.

    Reads split by consumer: this router answers from `app_settings`, but the hot path
    branches on the singleton (`settings.cache_enabled` in `services/pipeline.py`,
    `schema_pruning_enabled` in `chat_service`/`federation_setup`,
    `semantic_similarity_threshold` and `cache_max_age_days` in `cache/query_cache`),
    which the lifespan seeds once at boot. Without this, a toggle read back as saved
    while the pipeline kept using the boot-time value until the next restart — the query
    cache showed as enabled in the UI and never ran a lookup.

    Gated on SINGLETON_SETTING_PARSERS, the same allowlist the lifespan restore uses:
    `bcrypt_rounds` is persisted but has no field here (it is read from the DB per
    password operation) and pydantic raises on setattr for an unknown field, which would
    turn a valid save into a 500. Values arrive already typed from the validated body, so
    the parsers are consulted for their keys only.

    Single-process only, by nature: other workers still pick the change up at restart,
    which is what `db_pool_size`/`db_max_overflow` need regardless — the engine is built
    once at startup.
    """
    settings = get_settings()
    for key, value in body.model_dump(exclude_none=True).items():
        if key in SINGLETON_SETTING_PARSERS:
            setattr(settings, key, value)


async def _settings_response(db: AsyncSession) -> SettingsResponse:
    """Build SettingsResponse from DB-authoritative mutable values + env-only immutables.

    Reading mutable settings from DB means all workers see the same values
    immediately after a PUT, without requiring a restart.
    """
    s = get_settings()
    overrides: dict[str, str] = {}
    result = await db.execute(select(AppSetting))
    for row in result.scalars().all():
        if row.key in MUTABLE_SETTING_PARSERS:
            overrides[row.key] = row.value

    def _get(key: str, default):  # type: ignore[no-untyped-def]
        return MUTABLE_SETTING_PARSERS[key](overrides[key]) if key in overrides else default

    return SettingsResponse(
        app_name=s.app_name,
        debug=s.debug,
        log_level=s.log_level,
        ollama_base_url=s.ollama_base_url,
        default_query_timeout=_get("default_query_timeout", s.default_query_timeout),
        default_row_limit=_get("default_row_limit", s.default_row_limit),
        cache_enabled=_get("cache_enabled", s.cache_enabled),
        cache_max_age_days=_get("cache_max_age_days", s.cache_max_age_days),
        semantic_similarity_threshold=_get(
            "semantic_similarity_threshold", s.semantic_similarity_threshold
        ),
        embedding_model=s.embedding_model,
        db_pool_size=_get("db_pool_size", s.db_pool_size),
        db_max_overflow=_get("db_max_overflow", s.db_max_overflow),
        schema_pruning_enabled=_get("schema_pruning_enabled", s.schema_pruning_enabled),
        schema_pruning_top_k=_get("schema_pruning_top_k", s.schema_pruning_top_k),
        bcrypt_rounds=_get("bcrypt_rounds", DEFAULT_BCRYPT_ROUNDS),
    )


@router.get("", response_model=SettingsResponse)
async def get_settings_endpoint(
    _user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SettingsResponse:
    """Return the current application settings (no secrets)."""
    return await _settings_response(db)


@router.put("", response_model=SettingsResponse)
async def update_settings(
    body: SettingsUpdate,
    _user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SettingsResponse:
    """Update mutable application settings. Persisted to DB and written through to this
    worker's cached Settings, so the hot path picks the change up without a restart.
    Other workers see it in responses immediately (those read from DB) and in their own
    hot path at next restart; `db_pool_size`/`db_max_overflow` need one either way."""
    now = datetime.now(UTC)

    # Every field of SettingsUpdate has a parser (tests pin the two sets equal); the
    # gate is here so a field added to one without the other cannot persist a row the
    # startup restore cannot read back.
    for key, value in body.model_dump(exclude_none=True).items():
        if key in MUTABLE_SETTING_PARSERS:
            await db.merge(AppSetting(key=key, value=str(value), updated_at=now))

    await db.commit()
    _write_through_to_singleton(body)
    return await _settings_response(db)
