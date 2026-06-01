# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""SQLAlchemy model for per-provider LLM configuration."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy import Boolean, DateTime, Float, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class ProviderConfig(Base):
    """Stores API keys and tuning parameters for each LLM provider.

    The ``id`` (UUID) primary key allows multiple configurations of the same
    provider type — e.g. one ``openai_compatible`` row for Groq and another
    for Gemini.  ``provider_type`` identifies the registered provider class
    (e.g. 'claude', 'openai', 'openai_compatible', 'ollama').
    ``display_name`` is the user-facing label shown in the UI.
    """

    __tablename__ = "provider_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # 'claude' | 'openai' | 'openai_compatible' | 'ollama'
    provider_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # User-facing label, e.g. 'Groq (Free)', 'My Gemini', 'Anthropic Claude'
    display_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    # Fernet-encrypted API key — None for providers that don't require one (e.g. Ollama)
    api_key_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # Required for openai_compatible; optional override for others
    base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Active model identifier (e.g. 'claude-sonnet-4-6', 'gpt-4o', 'llama3')
    model: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=8192)
    # Whether this provider entry is currently enabled by the user
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # JSON array of {id, context_window, max_completion_tokens} fetched from the provider API
    models_cache_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
