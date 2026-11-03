# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""The latest schema drift report of a semantic model.

Two nullable columns on ``semantic_models``: ``last_drift_report`` (a ``DriftReport``
dump) and ``last_drift_checked_at``, written by Check Drift and by the background drift
monitor (``DRIFT_CHECK_INTERVAL_MINUTES``) and served by ``GET .../semantic/drift/last``.
NULL means no check has run since the upgrade. Bookkeeping: no version bump.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB, "postgresql")


def upgrade() -> None:
    op.add_column("semantic_models", sa.Column("last_drift_report", _JSON, nullable=True))
    op.add_column(
        "semantic_models",
        sa.Column("last_drift_checked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("semantic_models", "last_drift_checked_at")
    op.drop_column("semantic_models", "last_drift_report")
