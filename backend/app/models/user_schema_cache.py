# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Per-user schema cache — stores each user's view of a connection's schema."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class UserSchemaCache(Base):
    """Per-user cached DataSourceSchema for a specific connection."""

    __tablename__ = "user_schema_caches"
    __table_args__ = (UniqueConstraint("connection_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    connection_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    schema_cache: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    schema_cached_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    schema_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
