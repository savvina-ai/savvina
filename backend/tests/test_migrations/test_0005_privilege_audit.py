# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""0005 adds the connections.privilege_audit column."""

from unittest.mock import patch

from sqlalchemy.dialects import postgresql

from app.models.connection import Connection
from tests.migration_utils import load_migration


def test_0005_adds_and_drops_the_privilege_audit_column() -> None:
    added: list[tuple[str, object]] = []
    dropped: list[tuple[str, str]] = []
    module = load_migration("0005_privilege_audit.py")
    with (
        patch("alembic.op.add_column", lambda table, column, **_kw: added.append((table, column))),
        patch(
            "alembic.op.drop_column", lambda table, column, **_kw: dropped.append((table, column))
        ),
    ):
        module.upgrade()
        module.downgrade()
    [(table, column)] = added
    assert table == "connections"
    assert column.name == "privilege_audit"
    assert column.nullable is True
    assert dropped == [("connections", "privilege_audit")]
    assert module.revision == "0005" and module.down_revision == "0004"
    orm_column = Connection.__table__.c.privilege_audit
    assert str(column.type.compile(dialect=postgresql.dialect())) == str(
        orm_column.type.compile(dialect=postgresql.dialect())
    )
    assert orm_column.nullable is True
