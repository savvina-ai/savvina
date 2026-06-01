# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""SQLAlchemy model for the verified example library."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class VerifiedExample(Base):
    """A user-verified question → query pair used for few-shot prompting."""

    __tablename__ = "verified_examples"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    # Embedding vector — pgvector vector(384), enables server-side ANN search via HNSW index
    question_embedding: Mapped[list[float] | None] = mapped_column(Vector(384), nullable=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    query_dialect: Mapped[str] = mapped_column(String(50), nullable=False)
    verified_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
