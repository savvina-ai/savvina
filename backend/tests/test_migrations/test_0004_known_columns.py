# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""0004 creates the semantic_known_columns table."""

from unittest.mock import patch

from tests.migration_utils import load_migration


def test_0004_creates_and_drops_the_known_columns_table() -> None:
    created: list[tuple[str, list[str]]] = []
    indexed: list[str] = []
    dropped_indexes: list[str] = []
    dropped: list[str] = []
    module = load_migration("0004_semantic_known_columns.py")
    with (
        patch(
            "alembic.op.create_table",
            lambda name, *columns, **_kw: created.append((name, [c.name for c in columns])),
        ),
        patch("alembic.op.create_index", lambda name, *_a, **_kw: indexed.append(name)),
        patch("alembic.op.drop_index", lambda name, **_kw: dropped_indexes.append(name)),
        patch("alembic.op.drop_table", lambda name, **_kw: dropped.append(name)),
    ):
        module.upgrade()
        module.downgrade()
    [(name, columns)] = created
    assert name == "semantic_known_columns"
    assert {"model_id", "connection_id", "table_key", "columns"} <= set(columns)
    assert indexed == dropped_indexes == ["ix_semantic_known_columns_model"]
    assert dropped == ["semantic_known_columns"]
    assert module.revision == "0004" and module.down_revision == "0003"
