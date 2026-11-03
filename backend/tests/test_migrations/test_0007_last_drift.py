# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""0007 adds the semantic_models last-drift-report columns."""

from unittest.mock import patch

from sqlalchemy.dialects import postgresql

from app.models.semantic_store import SemanticModelRow
from tests.migration_utils import load_migration


def test_0007_adds_and_drops_the_last_drift_columns() -> None:
    added: list[tuple[str, object]] = []
    dropped: list[tuple[str, str]] = []
    module = load_migration("0007_semantic_last_drift.py")
    with (
        patch("alembic.op.add_column", lambda table, column, **_kw: added.append((table, column))),
        patch(
            "alembic.op.drop_column", lambda table, column, **_kw: dropped.append((table, column))
        ),
    ):
        module.upgrade()
        module.downgrade()
    assert [(t, c.name) for t, c in added] == [
        ("semantic_models", "last_drift_report"),
        ("semantic_models", "last_drift_checked_at"),
    ]
    assert sorted(dropped) == [
        ("semantic_models", "last_drift_checked_at"),
        ("semantic_models", "last_drift_report"),
    ]
    assert module.revision == "0007" and module.down_revision == "0006"
    pg = postgresql.dialect()
    for _table, column in added:
        orm_column = SemanticModelRow.__table__.c[column.name]
        assert column.nullable is True and orm_column.nullable is True
        assert str(column.type.compile(dialect=pg)) == str(orm_column.type.compile(dialect=pg))
