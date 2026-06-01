# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Pydantic schemas for chat sessions and messages."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

MessageStatus = Literal["pending_approval", "executed", "query_only", "error", "cached"]


# ── Request bodies ─────────────────────────────────────────────────────────────


class ChatOptions(BaseModel):
    max_rows: int = Field(default=100, ge=1, le=10_000)
    bypass_cache: bool = False
    force_refresh: bool = False


class ChatRequest(BaseModel):
    connection_id: str
    session_id: str | None = None
    message: str = Field(..., min_length=1)
    provider: str
    options: ChatOptions = Field(default_factory=ChatOptions)


class EditAndExecuteRequest(BaseModel):
    """Run a user-edited version of a generated query."""

    message_id: str
    edited_query: str = Field(..., min_length=1)


class SortRequest(BaseModel):
    """Re-execute the original query with an ORDER BY injected for server-side sort."""

    sort_column: str = Field(..., min_length=1)
    sort_order: Literal["ASC", "DESC"] = "ASC"

    @field_validator("sort_column")
    @classmethod
    def _no_sql_delimiters(cls, v: str) -> str:
        """Reject column names that contain SQL identifier-quoting characters.

        Both backtick (MySQL) and double-quote (PostgreSQL/standard SQL) are used
        as delimiters in ``_inject_order_by``.  A column name that embeds the
        closing delimiter would break out of the quoted context and enable SQL
        injection even when the caller whitelists the value against stored columns.

        Legitimate column names returned by real databases never contain these
        characters; rejecting them here is a safe, strict guard.
        """
        if '"' in v or "`" in v:
            raise ValueError('sort_column must not contain quote characters (" or `)')
        return v


class SemanticCorrection(BaseModel):
    """A user-suggested correction to the semantic model, sourced from feedback."""

    table_key: str  # 'ecommerce.orders'
    field: str  # 'status'
    correction_type: str  # 'add_value_mapping' | 'update_filter' | 'update_description'
    value: dict  # the correction content


class FeedbackRequest(BaseModel):
    message_id: str | None = None  # redundant — path param takes precedence; kept for compat
    # Values must match the mapping in frontend/src/api/chat.ts (submitFeedback).
    feedback: Literal["thumbs_up", "thumbs_down"]
    semantic_correction: SemanticCorrection | None = None


# ── Response bodies ────────────────────────────────────────────────────────────


class QueryResultsResponse(BaseModel):
    """Serialized QueryResult for the API response."""

    columns: list[str]
    column_types: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    execution_time_ms: float
    bytes_scanned: int | None = None


class ChatResponse(BaseModel):
    session_id: str
    message_id: str
    query: str | None = None
    query_dialect: str | None = None
    explanation: str = ""
    results: QueryResultsResponse | None = None
    execution_time_ms: float | None = None
    status: MessageStatus
    cache_hit: bool = False
    error: str | None = None
    token_count: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class SessionResponse(BaseModel):
    id: str
    connection_id: str
    title: str
    provider: str
    created_at: datetime
    updated_at: datetime
    cache_hit_count: int = 0

    model_config = {"from_attributes": True}


class MessageResponse(BaseModel):
    """Full chat message row."""

    id: str
    session_id: str
    role: str
    content: str
    query_generated: str | None
    query_dialect: str | None
    results_json: dict | None
    execution_time_ms: float | None
    bytes_scanned: int | None
    token_count: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    status: str
    cache_hit: bool
    error: str | None
    feedback: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Share schemas ───────────────────────────────────────────────────────────────


class ShareRequest(BaseModel):
    """Optional body for share creation — caller may specify a TTL in days."""

    expires_in_days: int | None = None


class ShareResponse(BaseModel):
    """Token returned when a message is made shareable."""

    share_token: str
    share_expires_at: datetime | None = None


class PublicShareResult(BaseModel):
    """Payload returned by the public (unauthenticated) share endpoint."""

    results: QueryResultsResponse
    query_generated: str | None = None


class PublicMessageSummary(BaseModel):
    """A single message in a shared session thread."""

    role: str
    content: str
    query_generated: str | None = None
    query_dialect: str | None = None
    results_json: dict | None = None
    execution_time_ms: float | None = None
    status: str
    created_at: datetime


class PublicSessionResult(BaseModel):
    """Full conversation thread returned by the public session share endpoint."""

    title: str
    messages: list[PublicMessageSummary]
