# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""SQLAlchemy model for saved connections."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy import JSON, Boolean, DateTime, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class Connection(Base):
    """A saved data source connection with encrypted credentials."""

    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # Fernet-encrypted JSON blob of connection params (host, port, user, password, …)
    config_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # PrivacySettings serialized as a plain dict
    privacy_settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 'auto_execute' | 'review_first' | 'generate_only'
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="auto_execute")
    # Cached SemanticModel as a dict
    semantic_model: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    semantic_model_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
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
