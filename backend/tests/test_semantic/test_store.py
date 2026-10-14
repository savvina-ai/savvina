# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for the normalised semantic store."""

from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.database import Base
from app.models.semantic_store import (
    SemanticModelRow,
    SemanticTableRow,
)
from tests.migration_utils import load_migration


class TestRowShapes:
    async def test_is_user_edited_derives_from_user_edited_fields(self) -> None:
        row = SemanticTableRow(user_edited_fields=[])
        assert row.is_user_edited is False
        row.user_edited_fields = ["description"]
        assert row.is_user_edited is True

    async def test_model_row_round_trips(self, semantic_db, connection_id) -> None:
        semantic_db.add(
            SemanticModelRow(
                connection_id=connection_id,
                source_dialect="postgresql",
                notes=["budgets are quarterly"],
                time_expressions={"last quarter": "date_trunc('quarter', now())"},
            )
        )
        await semantic_db.commit()
        semantic_db.expunge_all()
        stored = await semantic_db.scalar(
            select(SemanticModelRow).where(SemanticModelRow.connection_id == connection_id)
        )
        assert stored is not None
        assert stored.version == 1
        assert stored.generation_status == "idle"
        assert stored.generation_run_id is None
        assert stored.notes == ["budgets are quarterly"]
        assert stored.time_expressions == {"last quarter": "date_trunc('quarter', now())"}
        assert stored.is_user_edited is False


_PG = postgresql.dialect()


def _column_shape(column) -> tuple:
    return (
        str(column.type.compile(dialect=_PG)),
        column.nullable,
        column.primary_key,
        tuple(sorted((fk.target_fullname, fk.ondelete) for fk in column.foreign_keys)),
    )


def _index_shape(table: str, columns, unique, where) -> tuple:
    return (table, tuple(columns), bool(unique), str(where) if where is not None else None)


class TestMigrationMatchesOrm:
    """Server defaults are not compared: the ORM uses Python-side defaults by design."""

    def test_0002_creates_the_tables_the_orm_declares(self) -> None:
        tables: dict[str, dict[str, tuple]] = {}
        indexes: dict[str, tuple] = {}

        def fake_create_table(name, *columns, **_kw):
            tables[name] = {c.name: _column_shape(c) for c in columns}

        def fake_create_index(name, table, columns, unique=False, **kw):
            indexes[name] = _index_shape(table, columns, unique, kw.get("postgresql_where"))

        module = load_migration("0002_semantic_normalised_storage.py")
        with (
            patch("alembic.op.create_table", fake_create_table),
            patch("alembic.op.create_index", fake_create_index),
        ):
            module.upgrade()

        assert set(tables) == {
            "semantic_models",
            "semantic_tables",
            "semantic_columns",
            "semantic_metrics",
            "semantic_relationships",
            "semantic_global_items",
            "semantic_model_history",
        }
        orm_index_names: set[str] = set()
        for table_name, migration_cols in tables.items():
            orm_table = Base.metadata.tables[table_name]
            orm_cols = {c.name: _column_shape(c) for c in orm_table.columns}
            col_diff = {
                name: {"migration": migration_cols.get(name), "orm": orm_cols.get(name)}
                for name in migration_cols.keys() | orm_cols.keys()
                if migration_cols.get(name) != orm_cols.get(name)
            }
            assert not col_diff, f"{table_name}: column mismatch {col_diff}"

            orm_indexes = {
                i.name: _index_shape(
                    table_name,
                    (c.name for c in i.columns),
                    i.unique,
                    i.dialect_options["postgresql"]["where"],
                )
                for i in orm_table.indexes
            }
            migration_indexes = {n: s for n, s in indexes.items() if s[0] == table_name}
            idx_diff = {
                name: {"migration": migration_indexes.get(name), "orm": orm_indexes.get(name)}
                for name in migration_indexes.keys() | orm_indexes.keys()
                if migration_indexes.get(name) != orm_indexes.get(name)
            }
            assert not idx_diff, f"{table_name}: index mismatch {idx_diff}"
            orm_index_names |= orm_indexes.keys()
        assert set(indexes) == orm_index_names
