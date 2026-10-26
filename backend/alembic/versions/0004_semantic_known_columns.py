# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Per-table baseline of the live column names a semantic model has seen.

One ``semantic_known_columns`` row per (connection, table key) holds the live column
names the model has seen for that table. Drift detection reports a live column as
*added* only when it is in neither the stored table nor this baseline, so a column the
LLM omitted or a user deleted is not mistaken for a new one. A table with no row has no
baseline yet.

Its own table, not a column of ``semantic_models``: the baseline grows with
tables times columns and is written per batch, so each write must touch only the
tables it names. ``save_model`` rebuilds the table and column rows but never these.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB, "postgresql")


def upgrade() -> None:
    op.create_table(
        "semantic_known_columns",
        sa.Column(
            "model_id",
            sa.String(36),
            sa.ForeignKey("semantic_models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("connection_id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("table_key", sa.String(512), primary_key=True, nullable=False),
        sa.Column("columns", _JSON, nullable=False, server_default="[]"),
    )
    op.create_index("ix_semantic_known_columns_model", "semantic_known_columns", ["model_id"])


def downgrade() -> None:
    op.drop_index("ix_semantic_known_columns_model", table_name="semantic_known_columns")
    op.drop_table("semantic_known_columns")
