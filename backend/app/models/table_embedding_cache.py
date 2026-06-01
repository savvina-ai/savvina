# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Per-table embedding cache — one row per (connection, user, table) for schema pruning ANN."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class TableEmbeddingCache(Base):
    """Stores a pgvector embedding for each table, keyed by connection + user + table_key.

    Replaces the single BYTEA blob in user_schema_caches.table_embeddings, enabling
    server-side ANN search via the HNSW index instead of in-memory Python loops.
    """

    __tablename__ = "table_embedding_cache"
    __table_args__ = (UniqueConstraint("connection_id", "user_id", "table_key"),)

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
    table_key: Mapped[str] = mapped_column(Text, nullable=False)  # "schema_name.table_name"
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
