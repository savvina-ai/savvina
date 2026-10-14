# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Normalised semantic-layer storage: seven tables replacing the blob.

``connections.semantic_model`` is deliberately left in place and untouched —
it stays as a fallback for one release and is dropped in a later migration.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB, "postgresql")


def _provenance() -> list[sa.Column]:
    return [
        sa.Column("user_edited_fields", _JSON, nullable=False, server_default="[]"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(36), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "semantic_models",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "connection_id",
            sa.String(36),
            sa.ForeignKey("connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("schema_hash", sa.String(64), nullable=True),
        sa.Column("source_dialect", sa.String(50), nullable=False, server_default="sql"),
        sa.Column("db_timezone", sa.String(64), nullable=True),
        sa.Column("generated_at", sa.String(40), nullable=True),
        sa.Column("generation_model", sa.String(128), nullable=True),
        sa.Column("generation_status", sa.String(32), nullable=False, server_default="idle"),
        sa.Column("is_user_reviewed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("generation_progress", _JSON, nullable=True),
        sa.Column("generation_warnings", _JSON, nullable=False, server_default="[]"),
        sa.Column("notes", _JSON, nullable=False, server_default="[]"),
        sa.Column("time_expressions", _JSON, nullable=False, server_default="{}"),
        sa.Column("generation_run_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        *_provenance(),
    )
    op.create_index(
        "uq_semantic_models_connection", "semantic_models", ["connection_id"], unique=True
    )

    op.create_table(
        "semantic_tables",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "model_id",
            sa.String(36),
            sa.ForeignKey("semantic_models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("connection_id", sa.String(36), nullable=False),
        sa.Column("table_key", sa.String(512), nullable=False),
        sa.Column("display_name", sa.String(512), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("grain", sa.Text(), nullable=True),
        sa.Column("primary_timestamp_column", sa.String(512), nullable=True),
        sa.Column("primary_date_column", sa.String(512), nullable=True),
        sa.Column("base_sql", sa.Text(), nullable=True),
        sa.Column("domain", sa.String(128), nullable=True),
        sa.Column("default_filters", _JSON, nullable=False, server_default="[]"),
        sa.Column("hierarchies", _JSON, nullable=False, server_default="[]"),
        sa.Column("partition_columns", _JSON, nullable=False, server_default="[]"),
        sa.Column("cluster_columns", _JSON, nullable=False, server_default="[]"),
        sa.Column("generation_run_id", sa.String(36), nullable=True),
        *_provenance(),
    )
    op.create_index(
        "uq_semantic_tables_key", "semantic_tables", ["connection_id", "table_key"], unique=True
    )
    op.create_index("ix_semantic_tables_model", "semantic_tables", ["model_id"])
    op.create_index(
        "ix_semantic_tables_run", "semantic_tables", ["connection_id", "generation_run_id"]
    )

    op.create_table(
        "semantic_columns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "table_id",
            sa.String(36),
            sa.ForeignKey("semantic_tables.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("column_name", sa.String(512), nullable=False),
        sa.Column("display_name", sa.String(512), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_sensitive", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("semantic_type", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("cardinality", sa.String(32), nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column("unit", sa.String(32), nullable=True),
        sa.Column("default_aggregation", sa.String(32), nullable=True),
        sa.Column("is_non_additive", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("time_granularity", sa.String(32), nullable=True),
        sa.Column("value_mappings", _JSON, nullable=False, server_default="[]"),
        *_provenance(),
    )
    op.create_index(
        "uq_semantic_columns_name", "semantic_columns", ["table_id", "column_name"], unique=True
    )
    op.create_index("ix_semantic_columns_table", "semantic_columns", ["table_id"])
    # Partial on PostgreSQL (a flagged column is the rare case); ``postgresql_where``
    # is ignored by SQLite so this is a no-op there rather than an error.
    op.create_index(
        "ix_semantic_columns_sensitive",
        "semantic_columns",
        ["table_id"],
        postgresql_where=sa.text("is_sensitive"),
    )

    op.create_table(
        "semantic_metrics",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "model_id",
            sa.String(36),
            sa.ForeignKey("semantic_models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("connection_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("metric_type", sa.String(32), nullable=False),
        sa.Column("payload", _JSON, nullable=False, server_default="{}"),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        *_provenance(),
    )
    op.create_index(
        "uq_semantic_metrics_name", "semantic_metrics", ["connection_id", "name"], unique=True
    )
    op.create_index("ix_semantic_metrics_model", "semantic_metrics", ["model_id"])

    op.create_table(
        "semantic_relationships",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "model_id",
            sa.String(36),
            sa.ForeignKey("semantic_models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("connection_id", sa.String(36), nullable=False),
        sa.Column("from_table", sa.String(512), nullable=False),
        sa.Column("from_column", sa.String(512), nullable=False),
        sa.Column("to_table", sa.String(512), nullable=False),
        sa.Column("to_column", sa.String(512), nullable=False),
        sa.Column("join_sql", sa.Text(), nullable=False, server_default=""),
        sa.Column("relationship_type", sa.String(32), nullable=False, server_default="many_to_one"),
        sa.Column("is_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("entity_type", sa.String(32), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        *_provenance(),
    )
    op.create_index(
        "ix_semantic_rel_from", "semantic_relationships", ["connection_id", "from_table"]
    )
    op.create_index("ix_semantic_rel_to", "semantic_relationships", ["connection_id", "to_table"])
    op.create_index("ix_semantic_rel_model", "semantic_relationships", ["model_id"])

    op.create_table(
        "semantic_global_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "model_id",
            sa.String(36),
            sa.ForeignKey("semantic_models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("connection_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("payload", _JSON, nullable=False, server_default="{}"),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        *_provenance(),
    )
    op.create_index(
        "uq_semantic_global_kind_name",
        "semantic_global_items",
        ["connection_id", "kind", "name"],
        unique=True,
    )
    op.create_index("ix_semantic_global_model", "semantic_global_items", ["model_id"])

    op.create_table(
        "semantic_model_history",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("connection_id", sa.String(36), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("entity_key", sa.String(1024), nullable=False, server_default=""),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("source", sa.String(32), nullable=False, server_default="user"),
        sa.Column("old_value", _JSON, nullable=True),
        sa.Column("new_value", _JSON, nullable=True),
        sa.Column("changed_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_semantic_history_connection",
        "semantic_model_history",
        ["connection_id", "created_at"],
    )
    op.create_index(
        "ix_semantic_history_entity", "semantic_model_history", ["entity_type", "entity_id"]
    )


def downgrade() -> None:
    op.drop_table("semantic_model_history")
    op.drop_table("semantic_global_items")
    op.drop_table("semantic_relationships")
    op.drop_table("semantic_metrics")
    op.drop_table("semantic_columns")
    op.drop_table("semantic_tables")
    op.drop_table("semantic_models")
