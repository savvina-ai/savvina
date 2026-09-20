# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Pydantic schemas for the application settings API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SettingsResponse(BaseModel):
    """Safe (non-secret) application settings returned by GET /api/settings."""

    app_name: str
    debug: bool
    log_level: str
    ollama_base_url: str
    default_query_timeout: int
    default_row_limit: int
    cache_enabled: bool
    cache_max_age_days: int
    semantic_similarity_threshold: float
    embedding_model: str
    db_pool_size: int
    db_max_overflow: int
    schema_pruning_enabled: bool
    schema_pruning_top_k: int
    bcrypt_rounds: int


class SettingsUpdate(BaseModel):
    """Mutable settings that can be changed at runtime via PUT /api/settings.

    Changes are persisted to the ``app_settings`` table and survive restarts. They
    are also written through to the serving worker's cached ``get_settings()``
    singleton, so the runtime hot path picks them up without a restart; other
    workers see them in responses immediately (reads are DB-authoritative) and in
    their own hot path at next restart.

    Two exceptions. ``db_pool_size`` and ``db_max_overflow`` only take effect after a
    restart in any worker, because the engine is built once at startup.
    ``bcrypt_rounds`` is read from the DB per password operation, so it applies
    immediately everywhere and is never set on the singleton (``Settings`` has no
    such field).

    To revert a setting to its default, delete the corresponding row from
    ``app_settings``.
    """

    default_query_timeout: int | None = Field(default=None, ge=1)
    default_row_limit: int | None = Field(default=None, ge=1)
    cache_enabled: bool | None = None
    cache_max_age_days: int | None = Field(default=None, ge=0)
    semantic_similarity_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    db_pool_size: int | None = Field(default=None, ge=1, le=100)
    db_max_overflow: int | None = Field(default=None, ge=0, le=200)
    schema_pruning_enabled: bool | None = None
    schema_pruning_top_k: int | None = Field(default=None, ge=3, le=100)
    bcrypt_rounds: int | None = Field(default=None, ge=10, le=16)
