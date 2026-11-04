# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tombstones for semantic items the user deleted.

One ``semantic_tombstones`` row per (connection, kind, key) names a metric, segment,
derived column, common join or relationship the user removed from the model through
``PUT /semantic`` (``store.apply_user_patch``). The globals finalisation and Apply Drift
leave a generated item whose key is tombstoned out, so a deletion holds across
regenerations; adding the item back (or **Restore** on the page) removes the tombstone.

Bookkeeping like ``semantic_known_columns``: no version bump, no history, never rebuilt
by ``save_model``, gone with the model (``ON DELETE CASCADE``).

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "semantic_tombstones",
        sa.Column(
            "model_id",
            sa.String(36),
            sa.ForeignKey("semantic_models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("connection_id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("kind", sa.String(32), primary_key=True, nullable=False),
        sa.Column("key", sa.Text(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=True),
    )
    op.create_index("ix_semantic_tombstones_model", "semantic_tombstones", ["model_id"])


def downgrade() -> None:
    op.drop_index("ix_semantic_tombstones_model", table_name="semantic_tombstones")
    op.drop_table("semantic_tombstones")
