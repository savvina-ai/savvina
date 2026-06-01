# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""SQLAlchemy model for the query cache."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class QueryCacheEntry(Base):
    """Persists a question → generated_query pair for exact and semantic cache lookups."""

    __tablename__ = "query_cache"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "question_normalized", name="uq_query_cache_conn_question"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id"), nullable=False, index=True
    )
    # Normalized (lowercase, stripped) question — used for exact match
    question_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    # Raw (original) question text — returned to the user on a cache hit
    question_raw: Mapped[str] = mapped_column(Text, nullable=False)
    # Embedding vector — pgvector vector(384), enables server-side ANN search via HNSW index
    question_embedding: Mapped[list[float] | None] = mapped_column(Vector(384), nullable=True)
    generated_query: Mapped[str] = mapped_column(Text, nullable=False)
    query_dialect: Mapped[str] = mapped_column(String(50), nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QueryCacheStats(Base):
    """Per-connection query-cache miss counter.

    Updated atomically via PostgreSQL upsert on every cache miss so the
    get_cache_stats endpoint can report a real miss_count instead of 0.
    """

    __tablename__ = "query_cache_stats"

    connection_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("connections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    miss_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
