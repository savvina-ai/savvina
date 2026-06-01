# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""SQLAlchemy models for chat sessions and messages."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class ChatSession(Base):
    """A conversation thread tied to a single connection."""

    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id"), nullable=False, index=True
    )
    # Auto-generated from the first user message
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="New Chat")
    # LLM provider used in this session (e.g. 'claude', 'openai')
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    share_token: Mapped[str | None] = mapped_column(
        String(36), nullable=True, unique=True, index=True
    )
    share_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )


class ChatMessage(Base):
    """A single turn (user question or assistant answer) within a session."""

    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(
        ForeignKey("chat_sessions.id"), nullable=False, index=True
    )
    # 'user' or 'assistant'
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    # The question text (user) or explanation text (assistant)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Generated SQL/query (assistant turns only)
    query_generated: Mapped[str | None] = mapped_column(Text, nullable=True)
    query_dialect: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Serialized QueryResult (rows, columns, types, row_count, …)
    results_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    execution_time_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    bytes_scanned: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # LLM token usage for this assistant turn (None for cache hits).
    # token_count = input_tokens + output_tokens; kept separately for backwards compat.
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 'pending_approval' | 'executed' | 'query_only' | 'error' | 'cached'
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="executed")
    # True when this response came from the query cache (no LLM call made)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 'thumbs_up' | 'thumbs_down' — written by the feedback endpoint
    feedback: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Public share token (UUID) — set on demand via POST /chat/messages/{id}/share
    share_token: Mapped[str | None] = mapped_column(
        String(36), nullable=True, unique=True, index=True
    )
    share_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
