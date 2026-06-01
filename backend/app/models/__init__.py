# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

from __future__ import annotations

# Import all SQLAlchemy models here so that Base.metadata is fully populated
# before Alembic migrations run.
from .app_settings import AppSetting  # noqa: F401
from .cache import QueryCacheEntry  # noqa: F401
from .chat import ChatMessage, ChatSession  # noqa: F401
from .connection import Connection  # noqa: F401
from .example import VerifiedExample  # noqa: F401
from .provider import ProviderConfig  # noqa: F401
from .query_usage import QueryUsage  # noqa: F401
from .semantic_suggestion import SemanticSuggestion  # noqa: F401
from .table_embedding_cache import TableEmbeddingCache  # noqa: F401
from .user import RefreshToken, RevokedAccessToken, User  # noqa: F401
from .user_schema_cache import UserSchemaCache  # noqa: F401
