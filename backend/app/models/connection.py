# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""SQLAlchemy model for saved connections."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy import JSON, Boolean, DateTime, LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base

# Migration 0005 creates privilege_audit as JSONB on PostgreSQL; the model must say the same.
_JSON = JSON().with_variant(JSONB, "postgresql")


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
    # The last privilege audit of the connection role (services/privilege_audit.to_dict),
    # written on create, test, privacy save and config save. NULL: never audited (every
    # connection from before 3b-1); the UI shows nothing for it until the next Test or save.
    privilege_audit: Mapped[dict | None] = mapped_column(_JSON, nullable=True)
    # 'auto_execute' | 'review_first' | 'generate_only'
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="auto_execute")
    # Cached SemanticModel as a dict
    # Unread since the semantic store; kept for one release, hence deferred.
    semantic_model: Mapped[dict | None] = mapped_column(JSON, nullable=True, deferred=True)
    # Unwritten since the semantic store (the header row's ``updated_at`` is the
    # live value) and no longer served; dropped together with ``semantic_model``.
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
