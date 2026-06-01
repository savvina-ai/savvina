# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Pydantic schemas for LLM provider configuration and status."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

# ── Request bodies ─────────────────────────────────────────────────────────────


class FetchModelsRequest(BaseModel):
    """Payload for fetching available models from a provider API before saving a config."""

    provider_type: str
    api_key: str | None = None
    base_url: str | None = None


class ProviderConfigUpdate(BaseModel):
    """Update a provider's configuration. All fields are optional."""

    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    is_active: bool | None = None
    display_name: str | None = None


# ── Response bodies ────────────────────────────────────────────────────────────


class ProviderStatusResponse(BaseModel):
    """Combined static metadata + live status for one LLM provider."""

    id: str | None = None
    provider_type: str
    display_name: str
    provider_display_name: str
    # True when an API key (or local server) is configured
    is_configured: bool
    # True when the provider's health check passes
    is_healthy: bool
    is_active: bool
    current_model: str
    available_models: list[str]
    base_url: str | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
