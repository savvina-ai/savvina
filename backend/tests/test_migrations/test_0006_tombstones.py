# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""0006 creates the semantic_tombstones table."""

from unittest.mock import patch

from tests.migration_utils import load_migration


def test_0006_creates_and_drops_the_tombstones_table() -> None:
    created: list[tuple[str, list[str]]] = []
    indexed: list[str] = []
    dropped_indexes: list[str] = []
    dropped: list[str] = []
    module = load_migration("0006_semantic_tombstones.py")
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
    assert name == "semantic_tombstones"
    assert {"model_id", "connection_id", "kind", "key", "created_at", "created_by"} <= set(columns)
    assert indexed == dropped_indexes == ["ix_semantic_tombstones_model"]
    assert dropped == ["semantic_tombstones"]
    assert module.revision == "0006" and module.down_revision == "0005"
