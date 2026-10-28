# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Pydantic schemas for connections and privacy settings."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field

from ..datasources.models import PrivacySettings

# ── Privacy Settings ───────────────────────────────────────────────────────────


class PrivacySettingsUpdate(BaseModel):
    """Partial update for privacy settings — all fields optional."""

    include_sample_values: bool | None = None
    include_column_comments: bool | None = None
    include_row_counts: bool | None = None
    sensitive_column_patterns: list[str] | None = None
    excluded_schemas: list[str] | None = None
    excluded_tables: list[str] | None = None
    excluded_columns: list[str] | None = None
    # Per-table row filters, {"schema.table": "<condition>"} (see services/row_filter.py:
    # validate_row_filters). None (or absent) leaves the stored map unchanged; {} clears it,
    # which a connection with a legacy row_filter_sql refuses unless clear_legacy_row_filter
    # is sent too.
    row_filters: dict[str, str] | None = None
    # A command, not a setting: remove the legacy whole-query filter (row_filter_sql) and
    # leave the connection unfiltered unless row_filters is sent as well. Never stored.
    clear_legacy_row_filter: bool = False


# ── Execution Mode ──────────────────────────────────────────────────────────────

ExecutionMode = Literal["auto_execute", "review_first", "generate_only"]


class ExecutionModeUpdate(BaseModel):
    execution_mode: ExecutionMode


# ── Connection requests ────────────────────────────────────────────────────────


class ConnectionCreate(BaseModel):
    """Payload for creating a new connection."""

    name: str = Field(..., min_length=1, max_length=255)
    source_type: str = Field(..., min_length=1)
    # Raw (unencrypted) connection params; encrypted before storage
    config: dict[str, Any]
    privacy_settings: dict[str, Any] | None = None
    execution_mode: ExecutionMode = "auto_execute"


class ConnectionTest(BaseModel):
    """Payload for testing a connection before saving it."""

    source_type: str
    config: dict[str, Any]


# ── Connection responses ───────────────────────────────────────────────────────


class ConnectionResponse(BaseModel):
    """Summary response — no sensitive fields."""

    id: str
    name: str
    source_type: str
    execution_mode: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ConnectionDetail(ConnectionResponse):
    """Full detail response — includes cached metadata."""

    privacy_settings: dict | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def row_filter_needs_migration(self) -> bool:
        """True while the stored settings hold a legacy ``row_filter_sql`` and no
        ``row_filters`` map: every execution is refused until the admin acts."""
        return PrivacySettings.from_dict(self.privacy_settings or {}).row_filter_needs_migration


class ConnectionConfigResponse(BaseModel):
    """Decrypted connection config returned for editing."""

    name: str
    source_type: str
    config: dict[str, Any]

    model_config = {"from_attributes": True}


class ConnectionConfigUpdate(BaseModel):
    """Payload for updating connection name and/or credentials config."""

    name: str | None = None
    config: dict[str, Any] | None = None
