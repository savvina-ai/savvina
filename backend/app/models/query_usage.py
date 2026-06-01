# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""QueryUsage ORM model for per-user daily query counting."""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class QueryUsage(Base):
    """Daily query counter — one row per user per day."""

    __tablename__ = "query_usage"
    __table_args__ = (
        UniqueConstraint("user_id", "date"),
        Index("ix_query_usage_user_date", "user_id", "date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    query_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
