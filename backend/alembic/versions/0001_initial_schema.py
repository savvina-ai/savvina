# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Initial schema — community edition database from scratch.

Single consolidated migration for fresh installs. Run exactly one
``alembic upgrade head`` from an empty database.

question_embedding / embedding columns use vector(384) (pgvector) for ANN
search via HNSW indexes. BAAI/bge-small-en-v1.5 natively outputs float32
384-dimensional vectors.

Incorporates:
  0002 revoked_access_tokens deny-list
  0003 query_cache unique constraint on (connection_id, question_normalized)
  0004 chat_messages composite index (session_id, role, created_at)
  0005 query_cache.question_original renamed to question_raw
  SEC-3 users.tokens_invalidated_at for cross-session token revocation

Revision ID: 0001
Revises:
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

_VECTOR_DIM = 384


def upgrade() -> None:
    conn = op.get_bind()

    # ── pgvector extension ─────────────────────────────────────────────────────
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

    # ------------------------------------------------------------------ users
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tokens_invalidated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # ------------------------------------------------------------ refresh_tokens
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("device_hint", sa.String(255), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_refresh_token_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_token_user_revoked", "refresh_tokens", ["user_id", "revoked_at"])
    op.create_index("ix_refresh_token_expires", "refresh_tokens", ["expires_at"])

    # -------------------------------------------------------------- connections
    op.create_table(
        "connections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("config_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("privacy_settings", sa.JSON(), nullable=True),
        sa.Column("execution_mode", sa.String(20), nullable=False, server_default="auto_execute"),
        sa.Column("semantic_model", sa.JSON(), nullable=True),
        sa.Column("semantic_model_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_connections_name_active",
        "connections",
        ["name"],
        unique=True,
        postgresql_where="is_active = TRUE",
    )

    # -------------------------------------------------------------- chat_sessions
    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("connection_id", sa.String(36), sa.ForeignKey("connections.id"), nullable=False),
        sa.Column("title", sa.String(255), nullable=False, server_default="New Chat"),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("share_token", sa.String(36), nullable=True, unique=True),
        sa.Column("share_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_chat_session_connection_id", "chat_sessions", ["connection_id"])
    op.create_index("ix_chat_session_user_id", "chat_sessions", ["user_id"])
    op.create_index("ix_chat_session_share_token", "chat_sessions", ["share_token"])

    # ------------------------------------------------------------- chat_messages
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("chat_sessions.id"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("query_generated", sa.Text(), nullable=True),
        sa.Column("query_dialect", sa.String(50), nullable=True),
        sa.Column("results_json", sa.JSON(), nullable=True),
        sa.Column("execution_time_ms", sa.Float(), nullable=True),
        sa.Column("bytes_scanned", sa.BigInteger(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="executed"),
        sa.Column("cache_hit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("feedback", sa.String(20), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("share_token", sa.String(36), nullable=True, unique=True),
        sa.Column("share_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_chat_message_session_id", "chat_messages", ["session_id"])
    op.create_index("ix_chat_message_share_token", "chat_messages", ["share_token"])

    # --------------------------------------------------------------- query_cache
    op.create_table(
        "query_cache",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("connection_id", sa.String(36), sa.ForeignKey("connections.id"), nullable=False),
        sa.Column("question_normalized", sa.Text(), nullable=False),
        sa.Column("question_raw", sa.Text(), nullable=False),
        sa.Column("generated_query", sa.Text(), nullable=False),
        sa.Column("query_dialect", sa.String(50), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True),
    )
    conn.execute(
        text(f"ALTER TABLE query_cache ADD COLUMN question_embedding vector({_VECTOR_DIM})")
    )
    op.create_index("ix_query_cache_connection_id", "query_cache", ["connection_id"])

    # --------------------------------------------------------- verified_examples
    op.create_table(
        "verified_examples",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("connection_id", sa.String(36), sa.ForeignKey("connections.id"), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("query_dialect", sa.String(50), nullable=False),
        sa.Column(
            "verified_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    conn.execute(
        text(f"ALTER TABLE verified_examples ADD COLUMN question_embedding vector({_VECTOR_DIM})")
    )
    op.create_index("ix_verified_example_connection_id", "verified_examples", ["connection_id"])

    # --------------------------------------------------------- provider_configs
    op.create_table(
        "provider_configs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider_type", sa.String(50), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("api_key_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("base_url", sa.String(255), nullable=True),
        sa.Column("model", sa.String(100), nullable=False, server_default=""),
        sa.Column("temperature", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("max_tokens", sa.Integer(), nullable=False, server_default="8192"),
        sa.Column("models_cache_json", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_provider_config_type", "provider_configs", ["provider_type"])

    # ------------------------------------------------------------- app_settings
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # -------------------------------------------------------- semantic_suggestions
    op.create_table(
        "semantic_suggestions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "connection_id",
            sa.String(36),
            sa.ForeignKey("connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("table_key", sa.String(255), nullable=False),
        sa.Column("field", sa.String(255), nullable=False),
        sa.Column("correction_type", sa.String(50), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("is_applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_message_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_semantic_suggestion_connection_id", "semantic_suggestions", ["connection_id"]
    )

    # ------------------------------------------------------- user_schema_caches
    op.create_table(
        "user_schema_caches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "connection_id",
            sa.String(36),
            sa.ForeignKey("connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("schema_cache", sa.JSON(), nullable=True),
        sa.Column("schema_cached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("schema_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("connection_id", "user_id"),
    )
    op.create_index("ix_user_schema_cache_connection_id", "user_schema_caches", ["connection_id"])
    op.create_index("ix_user_schema_cache_user_id", "user_schema_caches", ["user_id"])

    # ---------------------------------------------------- table_embedding_cache
    op.create_table(
        "table_embedding_cache",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "connection_id",
            sa.String(36),
            sa.ForeignKey("connections.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("table_key", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    conn.execute(
        text(f"ALTER TABLE table_embedding_cache ADD COLUMN embedding vector({_VECTOR_DIM})")
    )
    conn.execute(
        text(
            "ALTER TABLE table_embedding_cache"
            " ADD CONSTRAINT uq_tec_conn_user_key"
            " UNIQUE (connection_id, user_id, table_key)"
        )
    )

    # -------------------------------------------------------------- query_usage
    op.create_table(
        "query_usage",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("date", sa.String(10), nullable=False),  # YYYY-MM-DD
        sa.Column("query_count", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("user_id", "date"),
    )
    op.create_index("ix_query_usage_user_id", "query_usage", ["user_id"])
    op.create_index("ix_query_usage_date", "query_usage", ["date"])
    op.create_index("ix_query_usage_user_date", "query_usage", ["user_id", "date"])

    # --------------------------------------------------------- query_cache_stats
    op.create_table(
        "query_cache_stats",
        sa.Column(
            "connection_id",
            sa.String(36),
            sa.ForeignKey("connections.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("miss_count", sa.Integer(), nullable=False, server_default="0"),
    )

    # ------------------------------------------------- composite indexes
    op.create_index(
        "ix_chat_message_session_created", "chat_messages", ["session_id", "created_at"]
    )
    op.create_index(
        "ix_query_cache_connection_created", "query_cache", ["connection_id", "created_at"]
    )
    op.create_index(
        "ix_query_cache_conn_question",
        "query_cache",
        ["connection_id", "question_normalized"],
    )
    op.create_index(
        "ix_chat_message_session_role_created",
        "chat_messages",
        ["session_id", "role", "created_at"],
    )

    # ─────────────────────────────────────────── HNSW indexes (pgvector ANN search)
    conn.execute(
        text(
            "CREATE INDEX ix_query_cache_embedding_hnsw"
            " ON query_cache USING hnsw (question_embedding vector_cosine_ops)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX ix_verified_examples_embedding_hnsw"
            " ON verified_examples USING hnsw (question_embedding vector_cosine_ops)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX ix_table_embedding_cache_hnsw"
            " ON table_embedding_cache USING hnsw (embedding vector_cosine_ops)"
        )
    )

    # ─────────────────────────────────── revoked_access_tokens deny-list (SEC-1)
    op.create_table(
        "revoked_access_tokens",
        sa.Column("jti", sa.String(36), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_revoked_access_token_expires", "revoked_access_tokens", ["expires_at"])

    # ─────────────────── unique constraint on query_cache(conn, question) (BUG-6)
    op.create_unique_constraint(
        "uq_query_cache_conn_question",
        "query_cache",
        ["connection_id", "question_normalized"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_query_cache_conn_question", "query_cache", type_="unique")
    op.drop_index("ix_revoked_access_token_expires", table_name="revoked_access_tokens")
    op.drop_table("revoked_access_tokens")
    op.drop_index("ix_chat_message_session_role_created", table_name="chat_messages")
    op.drop_index("ix_query_cache_conn_question", table_name="query_cache")
    op.drop_index("ix_query_cache_connection_created", table_name="query_cache")
    op.drop_index("ix_chat_message_session_created", table_name="chat_messages")
    op.drop_table("query_cache_stats")
    op.drop_table("query_usage")
    op.drop_table("table_embedding_cache")
    op.drop_table("user_schema_caches")
    op.drop_table("semantic_suggestions")
    op.drop_table("app_settings")
    op.drop_table("provider_configs")
    op.drop_table("verified_examples")
    op.drop_table("query_cache")
    op.drop_table("chat_messages")
    op.drop_table("chat_sessions")
    op.drop_index("uq_connections_name_active", table_name="connections")
    op.drop_table("connections")
    op.drop_table("refresh_tokens")
    op.drop_table("users")
