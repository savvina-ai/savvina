# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""The stored privilege audit of a connection role.

One JSON column on ``connections`` (``services/privilege_audit.to_dict``): the findings
about the connection role (superuser, BYPASSRLS, owner without FORCE ROW LEVEL SECURITY,
write privileges, SELECT on excluded tables, EXECUTE on SQL-from-string functions) and
``rls_effective`` per row-filtered table, the input of the fence removal (Phase 3b-2).
NULL means the audit never ran.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB, "postgresql")


def upgrade() -> None:
    op.add_column("connections", sa.Column("privilege_audit", _JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("connections", "privilege_audit")
