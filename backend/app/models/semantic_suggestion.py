# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""SQLAlchemy model for pending semantic model suggestions from user feedback."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class SemanticSuggestion(Base):
    """A pending correction to a semantic model, surfaced from user feedback."""

    __tablename__ = "semantic_suggestions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_key: Mapped[str] = mapped_column(String(255), nullable=False)
    field: Mapped[str] = mapped_column(String(255), nullable=False)
    # 'add_value_mapping' | 'update_filter' | 'update_description'
    correction_type: Mapped[str] = mapped_column(String(50), nullable=False)
    value: Mapped[dict] = mapped_column(JSON, nullable=False)
    is_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
