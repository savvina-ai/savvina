# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Pydantic schemas for connections and privacy settings."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field, field_validator

from ..datasources.models import PrivacySettings
from ..services.privilege_audit import from_dict as audit_from_dict
from ..services.privilege_audit import to_dict as audit_to_dict

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


# ── Privilege audit ────────────────────────────────────────────────────────────


class AuditFindingResponse(BaseModel):
    """One finding of the privilege audit (``services/privilege_audit.AuditFinding``)."""

    code: str
    tables: list[str] = []
    functions: list[str] = []
    remediation: str
    more_tables: int = 0  # tables beyond the cut of ``tables``


class PrivilegeAuditResponse(BaseModel):
    """The stored privilege audit (``services/privilege_audit.PrivilegeAudit``)."""

    checked_at: datetime
    role: str
    status: Literal["ok", "unavailable"]
    error: str | None
    findings: list[AuditFindingResponse]
    rls_in_use: bool
    rls_effective: dict[str, bool]


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
    # The last privilege audit; None when the audit never ran. The stored JSON passes
    # through privilege_audit.from_dict, so a tampered or outdated column serves None, not
    # a 500, and a finding code this version does not know is dropped.
    privilege_audit: PrivilegeAuditResponse | None = None

    @field_validator("privilege_audit", mode="before")
    @classmethod
    def _well_formed_audit(cls, value: object) -> object:
        if value is None:
            return None
        audit = audit_from_dict(value)
        return None if audit is None else audit_to_dict(audit)

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
