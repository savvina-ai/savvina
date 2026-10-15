# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for the normalised semantic store."""

from unittest.mock import patch

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.dialects import postgresql

from app.database import Base
from app.models.connection import Connection
from app.models.semantic_store import (
    SemanticColumnRow,
    SemanticGlobalItemRow,
    SemanticHistoryRow,
    SemanticMetricRow,
    SemanticModelRow,
    SemanticRelationshipRow,
    SemanticTableRow,
)
from app.schemas.semantic import ColumnSemanticUpdate, SemanticModelUpdate, TableSemanticUpdate
from app.semantic import store
from app.semantic.models import (
    AggregationType,
    ColumnSemantic,
    CommonJoin,
    DerivedColumn,
    GenerationStatus,
    Hierarchy,
    RelationshipEdge,
    Segment,
    SemanticModel,
    SemanticType,
    SimpleMetric,
    TableSemantic,
    ValueMapping,
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


def _rich_model() -> SemanticModel:
    """A model exercising every section."""
    return SemanticModel(
        tables={
            "public.orders": TableSemantic(
                display_name="Orders",
                description="One row per order",
                grain="one row per order",
                default_filters=["deleted_at IS NULL"],
                primary_timestamp_column="created_at",
                domain="sales",
                hierarchies=[Hierarchy(name="Date", levels=["year", "month", "day"])],
                partition_columns=["created_at"],
                columns={
                    "status": ColumnSemantic(
                        display_name="Status",
                        semantic_type=SemanticType.CATEGORICAL,
                        value_mappings=[ValueMapping(raw_value="p", display_value="Paid")],
                    ),
                    "email": ColumnSemantic(display_name="Email", is_sensitive=True),
                    "total": ColumnSemantic(
                        display_name="Total",
                        semantic_type=SemanticType.MONETARY,
                        currency="EUR",
                        default_aggregation=AggregationType.SUM,
                    ),
                },
            ),
            "public.customers": TableSemantic(
                display_name="Customers",
                columns={"id": ColumnSemantic(display_name="Id")},
            ),
        },
        business_metrics=[
            SimpleMetric(
                name="Revenue",
                definition="SUM(total)",
                description="Gross revenue",
                aggregation=AggregationType.SUM,
                related_tables=["public.orders"],
            ),
            SimpleMetric(
                name="Orders",
                definition="COUNT(*)",
                description="Order count",
                aggregation=AggregationType.COUNT,
                related_tables=["public.orders"],
            ),
        ],
        relationships=[
            RelationshipEdge(
                from_table="public.orders",
                from_column="customer_id",
                to_table="public.customers",
                to_column="id",
                relationship_type="many_to_one",
                join_sql="public.orders.customer_id = public.customers.id",
                is_required=False,
                description=None,
            )
        ],
        segments=[
            Segment(
                name="Paid",
                sql_expression="status = 'p'",
                description="Paid orders",
                applicable_tables=["public.orders"],
            )
        ],
        derived_columns=[
            DerivedColumn(
                name="net",
                sql_expression="total - tax",
                base_tables=["public.orders"],
                description="Net",
                format_hint=None,
                available_on=["public.orders"],
            )
        ],
        common_joins=[
            CommonJoin(
                description="orders to customers",
                tables=["public.orders", "public.customers"],
                join_pattern="public.orders.customer_id = public.customers.id",
            )
        ],
        notes=["budgets are quarterly"],
        time_expressions={"last quarter": "date_trunc('quarter', now())"},
        schema_hash="abc",
        source_dialect="postgresql",
        generation_status=GenerationStatus.COMPLETE,
        generated_at="2026-01-01T00:00:00+00:00",
        generation_model="test-model",
    )


class TestRoundTrip:
    async def test_save_then_load_is_identity(self, semantic_db, connection_id) -> None:
        model = _rich_model()
        await store.save_model(semantic_db, connection_id, model, updated_by="u1")
        await semantic_db.commit()
        semantic_db.expunge_all()

        loaded = await store.load_model(semantic_db, connection_id)
        assert loaded is not None
        assert loaded.model_dump(mode="json") == model.model_dump(mode="json")

    async def test_load_returns_none_without_a_header(self, semantic_db, connection_id) -> None:
        assert await store.load_model(semantic_db, connection_id) is None

    async def test_sections_keep_their_order(self, semantic_db, connection_id) -> None:
        model = _rich_model()
        await store.save_model(semantic_db, connection_id, model, updated_by="u1")
        await semantic_db.commit()
        semantic_db.expunge_all()
        loaded = await store.load_model(semantic_db, connection_id)
        assert [m.name for m in loaded.business_metrics] == ["Revenue", "Orders"]

    async def test_duplicate_metric_names_do_not_raise(self, semantic_db, connection_id) -> None:
        model = _rich_model()
        model.business_metrics.append(
            SimpleMetric(
                name="Revenue",
                definition="SUM(total) * 2",
                description="redefined",
                aggregation=AggregationType.SUM,
            )
        )
        await store.save_model(semantic_db, connection_id, model, updated_by="u1")
        await semantic_db.commit()
        semantic_db.expunge_all()
        loaded = await store.load_model(semantic_db, connection_id)
        # Last wins, first position kept.
        assert [m.name for m in loaded.business_metrics] == ["Revenue", "Orders"]
        assert loaded.business_metrics[0].definition == "SUM(total) * 2"

    async def test_second_save_replaces_rows_and_bumps_header_version(
        self, semantic_db, connection_id
    ) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        smaller = SemanticModel(tables={"public.x": TableSemantic(display_name="X")})
        await store.save_model(semantic_db, connection_id, smaller, updated_by="u1")
        await semantic_db.commit()
        semantic_db.expunge_all()
        header = await semantic_db.scalar(select(SemanticModelRow))
        assert header.version == 2
        tables = (await semantic_db.scalars(select(SemanticTableRow))).all()
        assert [t.table_key for t in tables] == ["public.x"]
        assert (await semantic_db.scalars(select(SemanticColumnRow))).all() == []
        assert (await semantic_db.scalars(select(SemanticMetricRow))).all() == []


async def _seed_legacy_blob(db, cid: str) -> None:
    await db.execute(
        update(Connection).where(Connection.id == cid).values(semantic_model={"tables": {}})
    )


async def _blob_is_sql_null(db, cid: str) -> bool:
    """True only for SQL NULL: a JSON 'null' text (a bound None) does not match ``IS NULL``."""
    count = await db.scalar(
        select(func.count())
        .select_from(Connection)
        .where(Connection.id == cid, Connection.semantic_model.is_(None))
    )
    return count == 1


class TestDeleteAndHistory:
    async def test_delete_cascades_every_row(self, semantic_db, connection_id) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        await store.delete_model(semantic_db, connection_id, changed_by="u1")
        await semantic_db.commit()
        semantic_db.expunge_all()
        for row_type in (
            SemanticModelRow,
            SemanticTableRow,
            SemanticColumnRow,
            SemanticMetricRow,
            SemanticRelationshipRow,
            SemanticGlobalItemRow,
        ):
            assert (await semantic_db.scalars(select(row_type))).all() == []
        assert await store.load_model(semantic_db, connection_id) is None

    async def test_delete_clears_the_legacy_blob_with_sql_null(
        self, semantic_db, connection_id
    ) -> None:
        """The migration that drops the blob must find no connection with a blob but no
        semantic_models row. SQL NULL, not a bound None: on a JSON column, None is
        stored as the JSON text 'null', which ``IS NULL`` does not match."""
        await _seed_legacy_blob(semantic_db, connection_id)
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        await store.delete_model(semantic_db, connection_id, changed_by="u1")
        await semantic_db.commit()
        semantic_db.expunge_all()
        assert await _blob_is_sql_null(semantic_db, connection_id)

    async def test_delete_clears_the_blob_even_without_rows(
        self, semantic_db, connection_id
    ) -> None:
        """Exactly the state the blob-drop migration must never find."""
        await _seed_legacy_blob(semantic_db, connection_id)
        await semantic_db.commit()
        await store.delete_model(semantic_db, connection_id, changed_by="u1")
        await semantic_db.commit()
        semantic_db.expunge_all()
        assert await _blob_is_sql_null(semantic_db, connection_id)

    async def test_delete_without_a_model_writes_no_history(
        self, semantic_db, connection_id
    ) -> None:
        await store.delete_model(semantic_db, connection_id, changed_by="u1")
        await semantic_db.commit()
        assert await semantic_db.scalar(select(SemanticHistoryRow)) is None

    async def test_save_and_delete_write_history(self, semantic_db, connection_id) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await store.delete_model(semantic_db, connection_id, changed_by="u2")
        await semantic_db.commit()
        semantic_db.expunge_all()
        rows = (
            await semantic_db.scalars(
                select(SemanticHistoryRow).order_by(SemanticHistoryRow.created_at)
            )
        ).all()
        assert [(r.entity_type, r.action, r.changed_by) for r in rows] == [
            ("model", "insert", "u1"),
            ("model", "delete", "u2"),
        ]
        assert rows[0].old_value is None
        assert rows[1].new_value is None
        assert rows[1].old_value["notes"] == ["budgets are quarterly"]


class TestFieldListsMatchTheMigration:
    def test_store_and_backfill_agree_on_field_names(self) -> None:
        """0003 duplicates the store's field tuples on purpose (a migration must not
        import app code); this keeps the two copies from drifting apart."""
        mig = load_migration("0003_semantic_blob_backfill.py")
        assert tuple(mig._MODEL_FIELDS) == store._MODEL_FIELDS
        assert tuple(mig._TABLE_FIELDS) == store._TABLE_FIELDS
        assert tuple(mig._COLUMN_FIELDS) == store._COLUMN_FIELDS


class TestSessionBehaviour:
    async def test_load_save_load_in_one_session_sees_the_save(
        self, semantic_db, connection_id
    ) -> None:
        """What a route does: no commit or expunge between the calls."""
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        semantic_db.expunge_all()

        first = await store.load_model(semantic_db, connection_id)
        assert first is not None
        first.notes = ["changed"]
        first.tables.pop("public.customers")
        await store.save_model(semantic_db, connection_id, first, updated_by="u1")
        second = await store.load_model(semantic_db, connection_id)

        assert second is not None
        assert second.notes == ["changed"]
        assert list(second.tables) == ["public.orders"]

    async def test_second_save_writes_an_update_history_row(
        self, semantic_db, connection_id
    ) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        changed = _rich_model()
        changed.notes = ["new note"]
        await store.save_model(semantic_db, connection_id, changed, updated_by="u2")
        await semantic_db.commit()
        semantic_db.expunge_all()

        row = await semantic_db.scalar(
            select(SemanticHistoryRow).where(SemanticHistoryRow.action == "update")
        )
        assert row is not None
        assert row.changed_by == "u2"
        assert row.old_value["notes"] == ["budgets are quarterly"]
        assert row.new_value["notes"] == ["new note"]

    async def test_complete_save_clears_a_stale_generation_run_id(
        self, semantic_db, connection_id
    ) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        header = await semantic_db.scalar(select(SemanticModelRow))
        header.generation_run_id = "run-1"
        await semantic_db.commit()
        semantic_db.expunge_all()

        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        semantic_db.expunge_all()

        header = await semantic_db.scalar(select(SemanticModelRow))
        assert header.generation_status == "complete"
        assert header.generation_run_id is None


class TestFitsPostgresLimits:
    """SQLite enforces no lengths, so these assert on what was stored."""

    async def test_long_and_nul_values_are_capped_and_cleaned(
        self, semantic_db, connection_id, caplog
    ) -> None:
        model = SemanticModel(
            tables={
                "public.orders": TableSemantic(
                    display_name="Orders",
                    description="bad\x00desc",
                    domain="d" * 200,
                    columns={
                        "total": ColumnSemantic(
                            display_name="x" * 600,
                            currency="US Dollars",
                            unit="u" * 40,
                        )
                    },
                )
            },
            notes=["a\x00b"],
        )
        with caplog.at_level("WARNING", logger="app.semantic.store"):
            await store.save_model(semantic_db, connection_id, model, updated_by="u1")
        assert "truncated semantic_tables.domain from 200 to 128 characters" in caplog.text
        assert "truncated semantic_columns.currency from 10 to 8 characters" in caplog.text
        await semantic_db.commit()
        semantic_db.expunge_all()

        loaded = await store.load_model(semantic_db, connection_id)
        assert loaded is not None
        table = loaded.tables["public.orders"]
        assert table.description == "baddesc"
        assert table.domain == "d" * 128
        column = table.columns["total"]
        assert column.display_name == "x" * 512
        assert column.currency == "US Dolla"
        assert column.unit == "u" * 32
        assert loaded.notes == ["ab"]

    async def test_names_differing_only_by_a_nul_collapse_to_one(
        self, semantic_db, connection_id
    ) -> None:
        model = SemanticModel(
            business_metrics=[
                SimpleMetric(
                    name="Revenue",
                    definition="SUM(a)",
                    aggregation=AggregationType.SUM,
                ),
                SimpleMetric(
                    name="Revenue\x00",
                    definition="SUM(b)",
                    aggregation=AggregationType.SUM,
                ),
            ],
            segments=[
                Segment(name="Paid", sql_expression="a"),
                Segment(name="Paid\x00", sql_expression="b"),
            ],
        )
        await store.save_model(semantic_db, connection_id, model, updated_by="u1")
        await semantic_db.commit()
        semantic_db.expunge_all()

        metrics = (await semantic_db.scalars(select(SemanticMetricRow))).all()
        assert [m.name for m in metrics] == ["Revenue"]
        assert metrics[0].payload["definition"] == "SUM(b)"
        segments = (await semantic_db.scalars(select(SemanticGlobalItemRow))).all()
        assert [(g.name, g.payload["sql_expression"]) for g in segments] == [("Paid", "b")]

    async def test_over_long_identity_raises_and_writes_nothing(
        self, semantic_db, connection_id
    ) -> None:
        model = SemanticModel(tables={"t" * 600: TableSemantic(display_name="T")})
        with pytest.raises(ValueError, match="table_key"):
            await store.save_model(semantic_db, connection_id, model, updated_by="u1")
        assert await semantic_db.scalar(select(SemanticModelRow)) is None
        assert await semantic_db.scalar(select(SemanticHistoryRow)) is None

    async def test_history_entity_key_fits_its_column(self, semantic_db, connection_id) -> None:
        """A column key is ``table_key + "." + column_name``: two identity values that
        each fit their 512-character column make 1025, one over ``entity_key``'s 1024."""
        limit = SemanticHistoryRow.__table__.c.entity_key.type.length
        table_key = "t" * SemanticTableRow.__table__.c.table_key.type.length
        column_name = "c" * SemanticColumnRow.__table__.c.column_name.type.length
        assert len(f"{table_key}.{column_name}") > limit

        def annotated(display_name: str) -> dict[str, TableSemantic]:
            return {
                table_key: TableSemantic(
                    display_name="T",
                    columns={column_name: ColumnSemantic(display_name=display_name)},
                )
            }

        await store.save_model(
            semantic_db, connection_id, SemanticModel(tables=annotated("C")), updated_by="u1"
        )
        await semantic_db.commit()
        await _begin(semantic_db, connection_id, tables_total=1)
        # A changed column writes a column history row, keyed by table_key.column_name.
        await store.reconcile_tables(semantic_db, connection_id, annotated("C2"), updated_by="gen")
        await semantic_db.commit()
        semantic_db.expunge_all()

        keys = (
            await semantic_db.scalars(
                select(SemanticHistoryRow.entity_key).where(
                    SemanticHistoryRow.entity_type == "column"
                )
            )
        ).all()
        assert keys, "the changed column should have written a history row"
        assert all(len(key) <= limit for key in keys)

    async def test_history_entity_key_fits_its_column_on_a_user_patch(
        self, semantic_db, connection_id
    ) -> None:
        """The PUT path builds the same ``table_key.column_name`` key in both its
        update and insert branches."""
        limit = SemanticHistoryRow.__table__.c.entity_key.type.length
        table_key = "t" * SemanticTableRow.__table__.c.table_key.type.length
        name_length = SemanticColumnRow.__table__.c.column_name.type.length
        column_name = "c" * name_length
        other_long_name = "d" * name_length
        assert len(f"{table_key}.{column_name}") > limit

        await store.save_model(
            semantic_db,
            connection_id,
            SemanticModel(
                tables={
                    table_key: TableSemantic(
                        display_name="T",
                        columns={column_name: ColumnSemantic(display_name="C")},
                    )
                }
            ),
            updated_by="u1",
        )
        await semantic_db.commit()
        # One column is updated, the other is inserted.
        await store.apply_user_patch(
            semantic_db,
            connection_id,
            SemanticModelUpdate(
                tables={
                    table_key: TableSemanticUpdate(
                        columns={
                            column_name: ColumnSemanticUpdate(display_name="Edited"),
                            other_long_name: ColumnSemanticUpdate(display_name="New"),
                        }
                    )
                }
            ),
            updated_by="u2",
        )
        await semantic_db.commit()
        semantic_db.expunge_all()

        keys = (
            await semantic_db.scalars(
                select(SemanticHistoryRow.entity_key).where(
                    SemanticHistoryRow.entity_type == "column"
                )
            )
        ).all()
        assert len(keys) >= 2, "both the updated and the inserted column should be audited"
        assert all(len(key) <= limit for key in keys)

    def test_fit_values_strips_nul_recursively_in_json_columns(self) -> None:
        fitted = store._fit_values(
            SemanticMetricRow,
            {
                "name": "Rev\x00enue",
                "payload": {"a\x00": ["x\x00y", {"k": "v\x00"}], "n": 1},
                "not_a_column": "keep\x00me",
            },
        )
        assert fitted == {
            "name": "Revenue",
            "payload": {"a": ["xy", {"k": "v"}], "n": 1},
            "not_a_column": "keep\x00me",
        }

    def test_fit_values_truncates_non_identity_but_rejects_identity(self) -> None:
        assert store._fit_values(SemanticTableRow, {"domain": "d" * 200})["domain"] == "d" * 128
        with pytest.raises(ValueError, match="limit is 512"):
            store._fit_values(SemanticTableRow, {"table_key": "k" * 513})
        # Global-item names are Text, bounded by the unique index's byte limit, not a length.
        assert store._fit_values(SemanticGlobalItemRow, {"name": "n" * 2000})["name"] == "n" * 2000
        with pytest.raises(ValueError, match="bytes"):
            store._fit_values(SemanticGlobalItemRow, {"name": "n" * 5000})

    def test_indexed_text_identity_is_bounded_by_bytes_not_characters(self) -> None:
        limit = store._MAX_INDEXED_TEXT_BYTES
        assert (
            store._fit_values(SemanticGlobalItemRow, {"name": "n" * limit})["name"] == "n" * limit
        )
        with pytest.raises(ValueError, match="bytes"):
            store._fit_values(SemanticGlobalItemRow, {"name": "n" * (limit + 1)})
        # A multibyte character counts for its encoded size ("é" is two bytes).
        with pytest.raises(ValueError, match="bytes"):
            store._fit_values(SemanticGlobalItemRow, {"name": "é" * (limit // 2 + 1)})
        # Text that is not an identity stays unbounded.
        long_text = "d" * 10_000
        assert (
            store._fit_values(SemanticTableRow, {"description": long_text})["description"]
            == long_text
        )

    def test_name_rejection_matches_the_save_rule(self) -> None:
        over_index = store._MAX_INDEXED_TEXT_BYTES + 1
        assert store.name_rejection("metric", "Revenue") is None
        assert store.name_rejection("metric", "a" * 512 + "\x00") is None  # NULs go first
        assert "limit is 512" in (store.name_rejection("metric", "a" * 513) or "")
        # Segments and derived columns share the Text ``name`` column with joins, so
        # only the unique index's byte limit bounds them.
        assert store.name_rejection("segment", "s" * 513) is None
        assert store.name_rejection("segment", "s" * over_index) is not None
        assert store.name_rejection("derived_column", "d" * 513) is None
        assert store.name_rejection("derived_column", "d" * over_index) is not None
        assert store.name_rejection("common_join", "j" * store._MAX_INDEXED_TEXT_BYTES) is None
        assert "bytes" in (store.name_rejection("common_join", "j" * over_index) or "")

    async def test_over_long_join_pattern_raises_and_writes_nothing(
        self, semantic_db, connection_id
    ) -> None:
        model = SemanticModel(
            common_joins=[
                CommonJoin(
                    description="too long for the unique index",
                    tables=["public.orders"],
                    join_pattern="j" * (store._MAX_INDEXED_TEXT_BYTES + 1),
                )
            ]
        )
        with pytest.raises(ValueError, match="name"):
            await store.save_model(semantic_db, connection_id, model, updated_by="u1")
        assert await semantic_db.scalar(select(SemanticModelRow)) is None

    def test_column_references_are_as_wide_as_column_names(self) -> None:
        # A truncated reference would name a column that does not exist.
        name_length = SemanticColumnRow.__table__.c.column_name.type.length
        long_ref = "c" * name_length
        for field in ("primary_timestamp_column", "primary_date_column"):
            assert SemanticTableRow.__table__.c[field].type.length == name_length
            assert store._fit_values(SemanticTableRow, {field: long_ref})[field] == long_ref


async def _begin(db, cid, *, tables_total: int = 2, resume: bool = False) -> None:
    await store.begin_generation(
        db,
        cid,
        schema_hash="h",
        tables_total=tables_total,
        tables_done=0,
        batch_size=4,
        source_dialect="postgresql",
        resume=resume,
    )


class TestGenerationRun:
    async def test_begin_generation_creates_a_header_with_a_run_id(
        self, semantic_db, connection_id
    ) -> None:
        await store.begin_generation(
            semantic_db,
            connection_id,
            schema_hash="h1",
            tables_total=3,
            tables_done=0,
            batch_size=4,
            source_dialect="postgresql",
        )
        await semantic_db.commit()
        semantic_db.expunge_all()
        header = await store.load_header(semantic_db, connection_id)
        assert header is not None
        assert header.generation_status == "tables_partial"
        assert header.schema_hash == "h1"
        assert header.generation_run_id is not None
        assert header.generation_progress is not None
        assert header.generation_progress.tables_total == 3

    async def test_begin_generation_never_blanks_existing_rows(
        self, semantic_db, connection_id
    ) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        await _begin(semantic_db, connection_id)
        await semantic_db.commit()
        semantic_db.expunge_all()
        loaded = await store.load_model(semantic_db, connection_id)
        assert set(loaded.tables) == {"public.orders", "public.customers"}
        assert loaded.generation_status == GenerationStatus.TABLES_PARTIAL

    async def test_begin_generation_fits_the_header_strings(
        self, semantic_db, connection_id
    ) -> None:
        await store.begin_generation(
            semantic_db,
            connection_id,
            schema_hash="ab\x00cd",
            tables_total=1,
            tables_done=0,
            batch_size=4,
            source_dialect="d" * 80,
        )
        await semantic_db.commit()
        semantic_db.expunge_all()
        header = await semantic_db.scalar(select(SemanticModelRow))
        assert header.schema_hash == "abcd"
        assert header.source_dialect == "d" * 50

    async def test_fresh_run_mints_a_new_run_id_and_resume_keeps_it(
        self, semantic_db, connection_id
    ) -> None:
        await _begin(semantic_db, connection_id, tables_total=1)
        await semantic_db.commit()
        semantic_db.expunge_all()
        first = (await store.load_header(semantic_db, connection_id)).generation_run_id
        await _begin(semantic_db, connection_id, tables_total=1, resume=True)
        await semantic_db.commit()
        semantic_db.expunge_all()
        assert (await store.load_header(semantic_db, connection_id)).generation_run_id == first
        await _begin(semantic_db, connection_id, tables_total=1)
        await semantic_db.commit()
        semantic_db.expunge_all()
        assert (await store.load_header(semantic_db, connection_id)).generation_run_id != first

    async def test_load_header_is_none_without_a_model(self, semantic_db, connection_id) -> None:
        assert await store.load_header(semantic_db, connection_id) is None


class TestReconcile:
    async def test_inserts_tables_that_have_no_row(self, semantic_db, connection_id) -> None:
        await _begin(semantic_db, connection_id)
        await store.reconcile_tables(
            semantic_db, connection_id, _rich_model().tables, updated_by="u1"
        )
        await semantic_db.commit()
        semantic_db.expunge_all()
        loaded = await store.load_model(semantic_db, connection_id)
        assert set(loaded.tables) == {"public.orders", "public.customers"}
        assert loaded.tables["public.orders"].columns["status"].display_name == "Status"

    async def test_overwrites_unedited_fields_and_keeps_edited_ones(
        self, semantic_db, connection_id
    ) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        semantic_db.expunge_all()
        # Mark the table description and the status column's display_name as hand-edited.
        table_row = await semantic_db.scalar(
            select(SemanticTableRow).where(SemanticTableRow.table_key == "public.orders")
        )
        table_row.user_edited_fields = ["description"]
        col_row = await semantic_db.scalar(
            select(SemanticColumnRow).where(
                SemanticColumnRow.table_id == table_row.id,
                SemanticColumnRow.column_name == "status",
            )
        )
        col_row.user_edited_fields = ["display_name"]
        await semantic_db.commit()
        semantic_db.expunge_all()

        regenerated = _rich_model().tables
        regenerated["public.orders"].description = "LLM rewrote this"
        regenerated["public.orders"].grain = "LLM rewrote the grain"
        regenerated["public.orders"].columns["status"].display_name = "LLM Status"
        regenerated["public.orders"].columns["status"].description = "LLM column description"
        await _begin(semantic_db, connection_id)
        await store.reconcile_tables(semantic_db, connection_id, regenerated, updated_by="gen")
        await semantic_db.commit()
        semantic_db.expunge_all()

        loaded = await store.load_model(semantic_db, connection_id)
        orders = loaded.tables["public.orders"]
        assert orders.description == "One row per order"  # protected
        assert orders.grain == "LLM rewrote the grain"  # not protected
        assert orders.columns["status"].display_name == "Status"  # protected
        assert orders.columns["status"].description == "LLM column description"

    async def test_a_changing_reconcile_bumps_versions_and_writes_history(
        self, semantic_db, connection_id
    ) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        semantic_db.expunge_all()

        regenerated = _rich_model().tables
        regenerated["public.orders"].grain = "LLM rewrote the grain"
        regenerated["public.orders"].columns["status"].description = "LLM column description"
        await _begin(semantic_db, connection_id)
        await store.reconcile_tables(semantic_db, connection_id, regenerated, updated_by="gen")
        await semantic_db.commit()
        semantic_db.expunge_all()

        orders = await semantic_db.scalar(
            select(SemanticTableRow).where(SemanticTableRow.table_key == "public.orders")
        )
        assert orders.version == 2
        assert orders.updated_by == "gen"
        assert orders.grain == "LLM rewrote the grain"
        status = await semantic_db.scalar(
            select(SemanticColumnRow).where(
                SemanticColumnRow.table_id == orders.id,
                SemanticColumnRow.column_name == "status",
            )
        )
        assert status.version == 2
        assert status.updated_by == "gen"
        customers = await semantic_db.scalar(
            select(SemanticTableRow).where(SemanticTableRow.table_key == "public.customers")
        )
        assert customers.version == 1

        history = (
            await semantic_db.scalars(
                select(SemanticHistoryRow).where(SemanticHistoryRow.source == "generation")
            )
        ).all()
        table_entries = [h for h in history if h.entity_type == "table"]
        column_entries = [h for h in history if h.entity_type == "column"]
        assert [(h.entity_key, h.action, h.changed_by) for h in table_entries] == [
            ("public.orders", "update", "gen")
        ]
        assert [h.entity_key for h in column_entries] == ["public.orders.status"]
        assert table_entries[0].old_value["grain"] == "one row per order"
        assert table_entries[0].new_value["grain"] == "LLM rewrote the grain"

    async def test_a_new_column_on_an_existing_table_is_inserted_unprotected(
        self, semantic_db, connection_id
    ) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        semantic_db.expunge_all()

        regenerated = _rich_model().tables
        regenerated["public.customers"].columns["segment"] = ColumnSemantic(display_name="Segment")
        await _begin(semantic_db, connection_id)
        await store.reconcile_tables(semantic_db, connection_id, regenerated, updated_by="gen")
        await semantic_db.commit()
        semantic_db.expunge_all()

        loaded = await store.load_model(semantic_db, connection_id)
        assert set(loaded.tables["public.customers"].columns) == {"id", "segment"}
        assert loaded.tables["public.customers"].columns["segment"].display_name == "Segment"
        segment = await semantic_db.scalar(
            select(SemanticColumnRow).where(SemanticColumnRow.column_name == "segment")
        )
        assert segment.user_edited_fields == []
        assert segment.version == 1

    async def test_never_clears_is_sensitive(self, semantic_db, connection_id) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        semantic_db.expunge_all()
        regenerated = _rich_model().tables
        regenerated["public.orders"].columns["email"].is_sensitive = False
        await _begin(semantic_db, connection_id)
        await store.reconcile_tables(semantic_db, connection_id, regenerated, updated_by="gen")
        await semantic_db.commit()
        semantic_db.expunge_all()
        loaded = await store.load_model(semantic_db, connection_id)
        assert loaded.tables["public.orders"].columns["email"].is_sensitive is True

    async def test_untouched_tables_are_left_alone(self, semantic_db, connection_id) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        semantic_db.expunge_all()
        only_customers = {"public.customers": TableSemantic(display_name="Clients", columns={})}
        await _begin(semantic_db, connection_id)
        await store.reconcile_tables(semantic_db, connection_id, only_customers, updated_by="gen")
        await semantic_db.commit()
        semantic_db.expunge_all()
        loaded = await store.load_model(semantic_db, connection_id)
        assert loaded.tables["public.customers"].display_name == "Clients"
        assert loaded.tables["public.orders"].display_name == "Orders"
        assert "status" in loaded.tables["public.orders"].columns

    async def test_identical_regenerate_writes_no_history_but_stamps_the_run(
        self, semantic_db, connection_id
    ) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        semantic_db.expunge_all()
        before = len((await semantic_db.scalars(select(SemanticHistoryRow))).all())
        await _begin(semantic_db, connection_id)
        await store.reconcile_tables(
            semantic_db, connection_id, _rich_model().tables, updated_by="gen"
        )
        await semantic_db.commit()
        semantic_db.expunge_all()
        after = (await semantic_db.scalars(select(SemanticHistoryRow))).all()
        # begin_generation writes one header row; the reconcile itself writes none.
        assert len(after) == before + 1
        header = await store.load_header(semantic_db, connection_id)
        rows = (await semantic_db.scalars(select(SemanticTableRow))).all()
        assert {r.generation_run_id for r in rows} == {header.generation_run_id}
        assert all(r.version == 1 for r in rows)
        columns = (await semantic_db.scalars(select(SemanticColumnRow))).all()
        assert all(c.version == 1 for c in columns)

    async def test_regenerating_values_that_need_fitting_is_still_a_no_op(
        self, semantic_db, connection_id
    ) -> None:
        def generated() -> dict[str, TableSemantic]:
            return {
                "public.orders": TableSemantic(
                    display_name="Orders",
                    description="bad\x00desc",
                    domain="d" * 200,
                    columns={
                        "total": ColumnSemantic(
                            display_name="x" * 600,
                            description="a\x00b",
                            currency="US Dollars",
                        )
                    },
                ),
                "public.customers": TableSemantic(display_name="Customers", columns={}),
            }

        await _begin(semantic_db, connection_id)
        await store.reconcile_tables(semantic_db, connection_id, generated(), updated_by="gen")
        await semantic_db.commit()
        semantic_db.expunge_all()
        stored = await semantic_db.scalar(
            select(SemanticTableRow).where(SemanticTableRow.table_key == "public.orders")
        )
        assert stored.domain == "d" * 128
        assert stored.description == "baddesc"
        semantic_db.expunge_all()

        await _begin(semantic_db, connection_id)
        await store.reconcile_tables(semantic_db, connection_id, generated(), updated_by="gen")
        await semantic_db.commit()
        semantic_db.expunge_all()

        entity_history = (
            await semantic_db.scalars(
                select(SemanticHistoryRow).where(
                    SemanticHistoryRow.entity_type.in_(["table", "column"])
                )
            )
        ).all()
        assert entity_history == []
        assert all(r.version == 1 for r in (await semantic_db.scalars(select(SemanticTableRow))))
        assert all(r.version == 1 for r in (await semantic_db.scalars(select(SemanticColumnRow))))
        header = await store.load_header(semantic_db, connection_id)
        rows = (await semantic_db.scalars(select(SemanticTableRow))).all()
        assert {r.generation_run_id for r in rows} == {header.generation_run_id}

    async def test_over_long_identity_raises_and_writes_nothing(
        self, semantic_db, connection_id
    ) -> None:
        with pytest.raises(ValueError, match="table_key"):
            await store.reconcile_tables(
                semantic_db,
                connection_id,
                {"t" * 600: TableSemantic(display_name="T")},
                updated_by="gen",
            )
        assert await semantic_db.scalar(select(SemanticModelRow)) is None

    async def test_expected_run_id_that_matches_reconciles(
        self, semantic_db, connection_id
    ) -> None:
        await _begin(semantic_db, connection_id)
        run_id = (await store.load_header(semantic_db, connection_id)).generation_run_id
        await store.reconcile_tables(
            semantic_db,
            connection_id,
            {"public.orders": TableSemantic(display_name="Orders")},
            updated_by="gen",
            expected_run_id=run_id,
        )
        rows = (await semantic_db.scalars(select(SemanticTableRow))).all()
        assert [(r.table_key, r.generation_run_id) for r in rows] == [("public.orders", run_id)]

    async def test_superseded_expected_run_id_conflicts_and_writes_nothing(
        self, semantic_db, connection_id
    ) -> None:
        await _begin(semantic_db, connection_id)
        old_run = (await store.load_header(semantic_db, connection_id)).generation_run_id
        await semantic_db.commit()
        # A fresh init in another transaction: a Core UPDATE leaves the identity-mapped
        # header stale, so this also proves the check reads the row fresh.
        await semantic_db.execute(
            update(SemanticModelRow)
            .where(SemanticModelRow.connection_id == connection_id)
            .values(generation_run_id="new-run")
        )
        with pytest.raises(store.SemanticStoreConflict, match="superseded"):
            await store.reconcile_tables(
                semantic_db,
                connection_id,
                {"public.orders": TableSemantic(display_name="Orders")},
                updated_by="gen",
                expected_run_id=old_run,
            )
        assert (await semantic_db.scalars(select(SemanticTableRow))).all() == []

    async def test_expected_run_id_without_a_header_conflicts(
        self, semantic_db, connection_id
    ) -> None:
        with pytest.raises(store.SemanticStoreConflict):
            await store.reconcile_tables(
                semantic_db,
                connection_id,
                {"public.orders": TableSemantic(display_name="Orders")},
                updated_by="gen",
                expected_run_id="run-1",
            )
        assert await semantic_db.scalar(select(SemanticModelRow)) is None


class TestPendingKeysAndProgress:
    async def test_every_key_is_pending_without_a_run(self, semantic_db, connection_id) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        semantic_db.expunge_all()
        keys = ["public.orders", "public.customers", "public.ghost"]
        assert await store.pending_table_keys(semantic_db, connection_id, keys) == keys

    async def test_a_regenerate_starts_with_every_key_pending_even_though_rows_exist(
        self, semantic_db, connection_id
    ) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await _begin(semantic_db, connection_id)
        await semantic_db.commit()
        semantic_db.expunge_all()
        keys = ["public.orders", "public.customers"]
        assert await store.pending_table_keys(semantic_db, connection_id, keys) == keys
        assert await store.count_tables_in_run(semantic_db, connection_id) == 0

    async def test_reconciled_keys_leave_the_pending_list_in_order(
        self, semantic_db, connection_id
    ) -> None:
        await _begin(semantic_db, connection_id, tables_total=3)
        await store.reconcile_tables(
            semantic_db,
            connection_id,
            {"public.customers": TableSemantic(display_name="Customers")},
            updated_by="gen",
        )
        await semantic_db.commit()
        semantic_db.expunge_all()
        keys = ["public.orders", "public.customers", "public.items"]
        assert await store.pending_table_keys(semantic_db, connection_id, keys) == [
            "public.orders",
            "public.items",
        ]
        progress = await store.advance_progress(semantic_db, connection_id)
        assert progress is not None
        assert progress.tables_done == 1
        assert progress.tables_total == 3

    async def test_resume_keeps_the_stamped_tables_and_a_fresh_run_clears_them(
        self, semantic_db, connection_id
    ) -> None:
        await _begin(semantic_db, connection_id)
        await store.reconcile_tables(
            semantic_db,
            connection_id,
            {"public.customers": TableSemantic(display_name="Customers")},
            updated_by="gen",
        )
        await semantic_db.commit()
        semantic_db.expunge_all()
        await _begin(semantic_db, connection_id, resume=True)
        await semantic_db.commit()
        semantic_db.expunge_all()
        assert await store.count_tables_in_run(semantic_db, connection_id) == 1
        await _begin(semantic_db, connection_id)
        await semantic_db.commit()
        semantic_db.expunge_all()
        assert await store.count_tables_in_run(semantic_db, connection_id) == 0
        assert await store.pending_table_keys(semantic_db, connection_id, ["public.customers"]) == [
            "public.customers"
        ]

    async def test_advance_progress_is_none_without_a_run(self, semantic_db, connection_id) -> None:
        assert await store.advance_progress(semantic_db, connection_id) is None


class TestProvenanceSurvivesFullSave:
    async def _mark(self, db, *, table_fields=(), column_fields=(), header_fields=()) -> None:
        table_row = await db.scalar(
            select(SemanticTableRow).where(SemanticTableRow.table_key == "public.orders")
        )
        table_row.user_edited_fields = list(table_fields)
        col_row = await db.scalar(
            select(SemanticColumnRow).where(
                SemanticColumnRow.table_id == table_row.id,
                SemanticColumnRow.column_name == "status",
            )
        )
        col_row.user_edited_fields = list(column_fields)
        header = await db.scalar(select(SemanticModelRow))
        header.user_edited_fields = list(header_fields)
        await db.commit()
        db.expunge_all()

    async def test_provenance_map_reports_every_marked_row(
        self, semantic_db, connection_id
    ) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        await self._mark(
            semantic_db,
            table_fields=["description"],
            column_fields=["display_name"],
            header_fields=["notes"],
        )
        prov = await store.provenance_map(semantic_db, connection_id)
        assert prov["model"] == {"": ["notes"]}
        assert prov["table"] == {"public.orders": ["description"]}
        assert prov["column"] == {"public.orders.status": ["display_name"]}
        assert prov["metric"] == {} and prov["global_item"] == {} and prov["relationship"] == {}

    async def test_snapshot_reports_marks_and_stored_values_in_one_pass(
        self, semantic_db, connection_id
    ) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        await self._mark(
            semantic_db,
            table_fields=["description"],
            column_fields=["display_name"],
            header_fields=["notes"],
        )
        snap = await store.provenance_snapshot(semantic_db, connection_id)
        assert snap.marks == await store.provenance_map(semantic_db, connection_id)
        assert snap.protected == {
            "table": {"public.orders": {"description": "One row per order"}},
            "column": {"public.orders.status": {"display_name": "Status"}},
        }

    async def test_full_save_uses_the_protected_values_it_is_given(
        self, semantic_db, connection_id
    ) -> None:
        """The globals route passes the snapshot's values so save_model does not
        re-read every row under the lock; the given values win over a re-read."""
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        await self._mark(semantic_db, table_fields=["description"], column_fields=["display_name"])
        snap = await store.provenance_snapshot(semantic_db, connection_id)

        rebuilt = _rich_model()
        rebuilt.tables["public.orders"].description = "LLM rewrote this"
        rebuilt.tables["public.orders"].columns["status"].display_name = "LLM Status"
        await store.save_model(
            semantic_db,
            connection_id,
            rebuilt,
            updated_by="gen",
            source="generation",
            provenance=snap.marks,
            protected={
                "table": {"public.orders": {"description": "from snapshot"}},
                "column": {"public.orders.status": {"display_name": "Snap Status"}},
            },
        )
        await semantic_db.commit()
        semantic_db.expunge_all()

        loaded = await store.load_model(semantic_db, connection_id)
        orders = loaded.tables["public.orders"]
        assert orders.description == "from snapshot"
        assert orders.columns["status"].display_name == "Snap Status"
        prov = await store.provenance_map(semantic_db, connection_id)
        assert prov["table"] == {"public.orders": ["description"]}
        assert prov["column"] == {"public.orders.status": ["display_name"]}

    async def test_full_save_reads_protected_values_only_when_not_given(
        self, semantic_db, connection_id
    ) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        await self._mark(semantic_db, table_fields=["description"], column_fields=["display_name"])
        snap = await store.provenance_snapshot(semantic_db, connection_id)

        with patch.object(store, "provenance_snapshot", wraps=store.provenance_snapshot) as spy:
            await store.save_model(
                semantic_db,
                connection_id,
                _rich_model(),
                updated_by="gen",
                provenance=snap.marks,
                protected=snap.protected,
            )
        spy.assert_not_called()
        await semantic_db.commit()

        with patch.object(store, "provenance_snapshot", wraps=store.provenance_snapshot) as spy:
            await store.save_model(
                semantic_db,
                connection_id,
                _rich_model(),
                updated_by="gen",
                provenance=snap.marks,
            )
        spy.assert_called_once()

    async def test_full_save_rejects_protected_values_without_provenance(
        self, semantic_db, connection_id
    ) -> None:
        with pytest.raises(TypeError, match="provenance"):
            await store.save_model(
                semantic_db,
                connection_id,
                _rich_model(),
                updated_by="gen",
                protected={"table": {}, "column": {}},
            )

    async def test_full_save_self_read_restores_only_the_fields_provenance_names(
        self, semantic_db, connection_id
    ) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        await self._mark(semantic_db, table_fields=["description", "grain"])
        marks = await store.provenance_map(semantic_db, connection_id)
        assert marks["table"]["public.orders"] == ["description", "grain"]
        stored_grain = _rich_model().tables["public.orders"].grain
        marks["table"]["public.orders"] = ["description"]

        rebuilt = _rich_model()
        rebuilt.tables["public.orders"].description = "LLM rewrote this"
        rebuilt.tables["public.orders"].grain = "LLM grain"
        assert stored_grain != "LLM grain"
        await store.save_model(
            semantic_db, connection_id, rebuilt, updated_by="gen", provenance=marks
        )
        await semantic_db.commit()
        semantic_db.expunge_all()

        orders = (await store.load_model(semantic_db, connection_id)).tables["public.orders"]
        assert orders.description == "One row per order"
        assert orders.grain == "LLM grain"

    async def test_full_save_skips_the_self_read_without_table_or_column_marks(
        self, semantic_db, connection_id
    ) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        marks = {
            "model": {},
            "table": {},
            "column": {},
            "metric": {"Revenue": ["payload"]},
            "global_item": {},
            "relationship": {},
        }
        with patch.object(store, "provenance_snapshot", wraps=store.provenance_snapshot) as spy:
            await store.save_model(
                semantic_db, connection_id, _rich_model(), updated_by="gen", provenance=marks
            )
        spy.assert_not_called()

    async def test_full_save_restores_marks_and_stored_values(
        self, semantic_db, connection_id
    ) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        await self._mark(
            semantic_db,
            table_fields=["description"],
            column_fields=["display_name"],
            header_fields=["notes"],
        )
        prov = await store.provenance_map(semantic_db, connection_id)

        rebuilt = _rich_model()
        rebuilt.tables["public.orders"].description = "generator"
        rebuilt.tables["public.orders"].grain = "generator grain"
        rebuilt.tables["public.orders"].columns["status"].display_name = "generator"
        rebuilt.notes = ["generator notes"]
        rebuilt.is_user_reviewed = False
        await store.save_model(
            semantic_db,
            connection_id,
            rebuilt,
            updated_by="gen",
            source="generation",
            provenance=prov,
        )
        await semantic_db.commit()
        semantic_db.expunge_all()

        loaded = await store.load_model(semantic_db, connection_id)
        assert loaded.tables["public.orders"].description == "One row per order"
        assert loaded.tables["public.orders"].grain == "generator grain"
        assert loaded.tables["public.orders"].columns["status"].display_name == "Status"
        assert loaded.notes == ["budgets are quarterly"]
        again = await store.provenance_map(semantic_db, connection_id)
        assert again["table"] == {"public.orders": ["description"]}
        assert again["column"] == {"public.orders.status": ["display_name"]}

    async def test_marks_are_not_carried_onto_keys_absent_from_the_model(
        self, semantic_db, connection_id
    ) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        await self._mark(semantic_db, table_fields=["description"])
        prov = await store.provenance_map(semantic_db, connection_id)
        smaller = SemanticModel(tables={"public.customers": TableSemantic(display_name="C")})
        await store.save_model(
            semantic_db, connection_id, smaller, updated_by="gen", provenance=prov
        )
        await semantic_db.commit()
        semantic_db.expunge_all()
        assert (await store.provenance_map(semantic_db, connection_id))["table"] == {}

    async def test_marks_are_not_carried_onto_columns_absent_from_the_model(
        self, semantic_db, connection_id
    ) -> None:
        await store.save_model(semantic_db, connection_id, _rich_model(), updated_by="u1")
        await semantic_db.commit()
        await self._mark(semantic_db, column_fields=["display_name"])
        prov = await store.provenance_map(semantic_db, connection_id)
        assert prov["column"] == {"public.orders.status": ["display_name"]}
        pruned = _rich_model()
        del pruned.tables["public.orders"].columns["status"]
        await store.save_model(
            semantic_db, connection_id, pruned, updated_by="gen", provenance=prov
        )
        await semantic_db.commit()
        semantic_db.expunge_all()
        loaded = await store.load_model(semantic_db, connection_id)
        assert "status" not in loaded.tables["public.orders"].columns
        assert (await store.provenance_map(semantic_db, connection_id))["column"] == {}

    async def test_relationship_metric_and_global_item_marks_round_trip(
        self, semantic_db, connection_id
    ) -> None:
        model = _rich_model()
        model.relationships[0].description = "hand written"
        await store.save_model(semantic_db, connection_id, model, updated_by="u1")
        await semantic_db.commit()
        for row in (await semantic_db.scalars(select(SemanticRelationshipRow))).all():
            row.user_edited_fields = ["description"]
        for metric_row in (
            await semantic_db.scalars(
                select(SemanticMetricRow).where(SemanticMetricRow.name == "Revenue")
            )
        ).all():
            metric_row.user_edited_fields = ["payload"]
        for item_row in (
            await semantic_db.scalars(
                select(SemanticGlobalItemRow).where(SemanticGlobalItemRow.kind == "common_join")
            )
        ).all():
            item_row.user_edited_fields = ["payload"]
        await semantic_db.commit()
        semantic_db.expunge_all()

        prov = await store.provenance_map(semantic_db, connection_id)
        rel_key = "public.orders.customer_id->public.customers.id"
        assert prov["relationship"] == {rel_key: ["description"]}
        assert prov["metric"] == {"Revenue": ["payload"]}
        assert prov["global_item"] == {
            "common_join:public.orders.customer_id = public.customers.id": ["payload"]
        }

        current = await store.load_model(semantic_db, connection_id)
        fresh = [current.relationships[0].model_copy(update={"description": "generator"})]
        carried = store.carry_edited_relationships(
            current.relationships,
            fresh,
            prov,
            render_tables={"public.orders", "public.customers"},
        )
        rebuilt = current.model_copy(update={"relationships": carried})
        await store.save_model(
            semantic_db,
            connection_id,
            rebuilt,
            updated_by="gen",
            source="generation",
            provenance=prov,
        )
        await semantic_db.commit()
        semantic_db.expunge_all()

        assert await store.provenance_map(semantic_db, connection_id) == prov
        loaded = await store.load_model(semantic_db, connection_id)
        assert [r.description for r in loaded.relationships] == ["hand written"]


class TestCarryEditedRelationships:
    def _edge(self, frm="public.orders", to="public.customers", col="customer_id", desc=None):
        return RelationshipEdge(
            from_table=frm,
            from_column=col,
            to_table=to,
            to_column="id",
            relationship_type="many_to_one",
            join_sql=f"{frm}.{col} = {to}.id",
            is_required=False,
            description=desc,
        )

    def test_unmarked_edges_are_rebuilt_from_fresh(self) -> None:
        current = [self._edge(desc="stale")]
        fresh = [self._edge(desc="fresh")]
        out = store.carry_edited_relationships(
            current,
            fresh,
            {"relationship": {}},
            render_tables={"public.orders", "public.customers"},
        )
        assert [e.description for e in out] == ["fresh"]

    def test_marked_edge_is_kept_and_its_twin_dropped(self) -> None:
        current = [self._edge(desc="hand written")]
        fresh = [self._edge(desc="fresh")]
        prov = {"relationship": {store.relationship_key(current[0]): ["description"]}}
        out = store.carry_edited_relationships(
            current, fresh, prov, render_tables={"public.orders", "public.customers"}
        )
        assert [e.description for e in out] == ["hand written"]

    def test_fresh_edges_outside_render_tables_are_not_emitted(self) -> None:
        fresh = [self._edge(), self._edge(frm="public.items", col="order_id", to="public.orders")]
        out = store.carry_edited_relationships(
            [],
            fresh,
            {"relationship": {}},
            render_tables={"public.orders", "public.customers"},
        )
        assert len(out) == 1

    def test_marked_user_authored_edge_survives_a_partial_render_set(self) -> None:
        manual = self._edge(frm="public.notes", col="order_id", to="public.orders", desc="manual")
        prov = {"relationship": {store.relationship_key(manual): ["payload"]}}
        out = store.carry_edited_relationships([manual], [], prov, render_tables={"public.orders"})
        assert out == [manual]


class TestUserPatch:
    async def _seed(self, db, cid) -> None:
        await store.save_model(db, cid, _rich_model(), updated_by="u1")
        await db.commit()
        db.expunge_all()

    async def test_patch_touches_only_the_addressed_fields_and_marks_them(
        self, semantic_db, connection_id
    ) -> None:
        await self._seed(semantic_db, connection_id)
        patch = SemanticModelUpdate(
            tables={
                "public.orders": TableSemanticUpdate(
                    description="hand written",
                    columns={"status": ColumnSemanticUpdate(display_name="State")},
                )
            }
        )
        model = await store.apply_user_patch(semantic_db, connection_id, patch, updated_by="u2")
        await semantic_db.commit()
        semantic_db.expunge_all()
        orders = model.tables["public.orders"]
        assert orders.description == "hand written"
        assert orders.grain == "one row per order"  # untouched
        assert orders.columns["status"].display_name == "State"
        assert orders.columns["status"].semantic_type == SemanticType.CATEGORICAL  # untouched
        assert orders.columns["email"].is_sensitive is True  # untouched
        prov = await store.provenance_map(semantic_db, connection_id)
        assert prov["table"] == {"public.orders": ["description"]}
        assert prov["column"] == {"public.orders.status": ["display_name"]}

    async def test_echoing_stored_values_marks_nothing_and_bumps_nothing(
        self, semantic_db, connection_id
    ) -> None:
        await self._seed(semantic_db, connection_id)
        history_before = await semantic_db.scalar(
            select(func.count()).select_from(SemanticHistoryRow)
        )
        full = _rich_model().model_dump(mode="json")
        patch = SemanticModelUpdate.model_validate(full)
        await store.apply_user_patch(semantic_db, connection_id, patch, updated_by="u2")
        await semantic_db.commit()
        semantic_db.expunge_all()
        prov = await store.provenance_map(semantic_db, connection_id)
        assert prov == {
            "model": {},
            "table": {},
            "column": {},
            "metric": {},
            "global_item": {},
            "relationship": {},
        }
        rows = (await semantic_db.scalars(select(SemanticTableRow))).all()
        assert all(r.version == 1 for r in rows)
        columns = (await semantic_db.scalars(select(SemanticColumnRow))).all()
        assert all(c.version == 1 for c in columns)
        metrics = (await semantic_db.scalars(select(SemanticMetricRow))).all()
        assert all(m.version == 1 for m in metrics)
        header = await semantic_db.scalar(select(SemanticModelRow))
        assert header.version == 1
        history_after = await semantic_db.scalar(
            select(func.count()).select_from(SemanticHistoryRow)
        )
        assert history_after == history_before

    async def test_null_on_a_not_supplied_field_preserves_the_stored_value(
        self, semantic_db, connection_id
    ) -> None:
        await self._seed(semantic_db, connection_id)
        patch = SemanticModelUpdate(
            tables={
                "public.orders": TableSemanticUpdate(
                    default_filters=None,
                    columns={
                        "status": ColumnSemanticUpdate(value_mappings=None, is_sensitive=None)
                    },
                )
            }
        )
        model = await store.apply_user_patch(semantic_db, connection_id, patch, updated_by="u2")
        await semantic_db.commit()
        assert model.tables["public.orders"].default_filters == ["deleted_at IS NULL"]
        assert len(model.tables["public.orders"].columns["status"].value_mappings) == 1

    async def test_explicit_empty_list_clears(self, semantic_db, connection_id) -> None:
        await self._seed(semantic_db, connection_id)
        patch = SemanticModelUpdate(
            tables={"public.orders": TableSemanticUpdate(default_filters=[])}
        )
        model = await store.apply_user_patch(semantic_db, connection_id, patch, updated_by="u2")
        await semantic_db.commit()
        assert model.tables["public.orders"].default_filters == []

    async def test_new_table_and_column_are_inserted_with_marks(
        self, semantic_db, connection_id
    ) -> None:
        await self._seed(semantic_db, connection_id)
        patch = SemanticModelUpdate(
            tables={
                "public.items": TableSemanticUpdate(
                    display_name="Items",
                    columns={"sku": ColumnSemanticUpdate(display_name="SKU")},
                )
            }
        )
        model = await store.apply_user_patch(semantic_db, connection_id, patch, updated_by="u2")
        await semantic_db.commit()
        semantic_db.expunge_all()
        assert model.tables["public.items"].columns["sku"].display_name == "SKU"
        prov = await store.provenance_map(semantic_db, connection_id)
        assert prov["table"]["public.items"] == ["display_name"]
        assert prov["column"]["public.items.sku"] == ["display_name"]

    async def test_global_sections_replace_wholesale_with_per_item_marks(
        self, semantic_db, connection_id
    ) -> None:
        await self._seed(semantic_db, connection_id)
        metrics = [m.model_dump(mode="json") for m in _rich_model().business_metrics]
        metrics[1]["description"] = "edited by hand"
        patch = SemanticModelUpdate.model_validate({"business_metrics": metrics})
        model = await store.apply_user_patch(semantic_db, connection_id, patch, updated_by="u2")
        await semantic_db.commit()
        semantic_db.expunge_all()
        assert [m.name for m in model.business_metrics] == ["Revenue", "Orders"]
        prov = await store.provenance_map(semantic_db, connection_id)
        assert prov["metric"] == {"Orders": ["payload"]}  # only the changed item is marked

    async def test_reorder_is_a_real_edit(self, semantic_db, connection_id) -> None:
        await self._seed(semantic_db, connection_id)
        metrics = [m.model_dump(mode="json") for m in reversed(_rich_model().business_metrics)]
        patch = SemanticModelUpdate.model_validate({"business_metrics": metrics})
        model = await store.apply_user_patch(semantic_db, connection_id, patch, updated_by="u2")
        await semantic_db.commit()
        semantic_db.expunge_all()
        assert [m.name for m in model.business_metrics] == ["Orders", "Revenue"]
        loaded = await store.load_model(semantic_db, connection_id)
        assert [m.name for m in loaded.business_metrics] == ["Orders", "Revenue"]

    async def test_notes_and_review_flag_are_diff_gated(self, semantic_db, connection_id) -> None:
        await self._seed(semantic_db, connection_id)
        same = SemanticModelUpdate(notes=["budgets are quarterly"], is_user_reviewed=False)
        await store.apply_user_patch(semantic_db, connection_id, same, updated_by="u2")
        await semantic_db.commit()
        semantic_db.expunge_all()
        assert (await store.provenance_map(semantic_db, connection_id))["model"] == {}
        changed = SemanticModelUpdate(notes=["new note"])
        await store.apply_user_patch(semantic_db, connection_id, changed, updated_by="u2")
        await semantic_db.commit()
        semantic_db.expunge_all()
        assert (await store.provenance_map(semantic_db, connection_id))["model"] == {"": ["notes"]}

    async def test_review_flag_flips_and_an_echo_afterwards_marks_nothing_new(
        self, semantic_db, connection_id
    ) -> None:
        await self._seed(semantic_db, connection_id)
        flip = SemanticModelUpdate(is_user_reviewed=True)
        model = await store.apply_user_patch(semantic_db, connection_id, flip, updated_by="u2")
        await semantic_db.commit()
        semantic_db.expunge_all()
        assert model.is_user_reviewed is True
        loaded = await store.load_model(semantic_db, connection_id)
        assert loaded.is_user_reviewed is True
        semantic_db.expunge_all()

        before = await self._rows_and_history(semantic_db, connection_id)
        await self._echo_reloaded_model(semantic_db, connection_id)
        assert await self._rows_and_history(semantic_db, connection_id) == before

    async def test_header_and_section_edits_are_audited(self, semantic_db, connection_id) -> None:
        await self._seed(semantic_db, connection_id)
        seeded = {h.id for h in (await semantic_db.scalars(select(SemanticHistoryRow))).all()}
        header_id = await semantic_db.scalar(select(SemanticModelRow.id))
        full = _rich_model().model_dump(mode="json")
        full["notes"] = ["new note"]
        full["business_metrics"][1]["description"] = "edited by hand"
        full["segments"][0]["sql_expression"] = "status = 'paid'"
        await store.apply_user_patch(
            semantic_db,
            connection_id,
            SemanticModelUpdate.model_validate(full),
            updated_by="u2",
        )
        await semantic_db.commit()
        semantic_db.expunge_all()

        rows = [
            h
            for h in (await semantic_db.scalars(select(SemanticHistoryRow))).all()
            if h.id not in seeded
        ]
        by_type = {h.entity_type: h for h in rows}
        assert sorted(h.entity_type for h in rows) == ["global_item", "metric", "model"]
        assert all(
            (h.action, h.source, h.entity_id, h.changed_by) == ("update", "user", header_id, "u2")
            for h in rows
        )

        model_row = by_type["model"]
        assert model_row.entity_key == ""
        assert model_row.old_value == {"notes": ["budgets are quarterly"]}
        assert model_row.new_value == {"notes": ["new note"]}

        metric_row = by_type["metric"]
        stored_metrics = [m.model_dump(mode="json") for m in _rich_model().business_metrics]
        assert metric_row.entity_key == "business_metrics"
        assert metric_row.old_value == {"items": stored_metrics}
        assert metric_row.new_value == {"items": full["business_metrics"]}

        segment_row = by_type["global_item"]
        assert segment_row.entity_key == "segments"
        assert segment_row.old_value["items"][0]["sql_expression"] == "status = 'p'"
        assert segment_row.new_value == {"items": full["segments"]}

    async def test_a_relationship_edit_writes_one_relationship_history_row(
        self, semantic_db, connection_id
    ) -> None:
        await self._seed(semantic_db, connection_id)
        rels = [r.model_dump(mode="json") for r in _rich_model().relationships]
        rels[0]["description"] = "each order has a customer"
        await store.apply_user_patch(
            semantic_db,
            connection_id,
            SemanticModelUpdate.model_validate({"relationships": rels}),
            updated_by="u2",
        )
        await semantic_db.commit()
        semantic_db.expunge_all()
        rows = (
            await semantic_db.scalars(
                select(SemanticHistoryRow).where(SemanticHistoryRow.entity_type == "relationship")
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].entity_key == "relationships"
        assert rows[0].old_value["items"][0]["description"] is None
        assert rows[0].new_value["items"][0]["description"] == "each order has a customer"

    async def _rows_and_history(self, db, cid) -> tuple:
        rows = []
        for row_cls in (
            SemanticModelRow,
            SemanticTableRow,
            SemanticColumnRow,
            SemanticMetricRow,
            SemanticGlobalItemRow,
            SemanticRelationshipRow,
        ):
            rows.extend((r.id, r.version) for r in (await db.scalars(select(row_cls))).all())
        history = await db.scalar(select(func.count()).select_from(SemanticHistoryRow))
        return sorted(rows), history, await store.provenance_map(db, cid)

    async def _echo_reloaded_model(self, db, cid) -> None:
        loaded = await store.load_model(db, cid)
        echo = SemanticModelUpdate.model_validate(loaded.model_dump(mode="json"))
        await store.apply_user_patch(db, cid, echo, updated_by="u2")
        await db.commit()
        db.expunge_all()

    async def test_items_sent_without_defaulted_keys_are_stored_whole(
        self, semantic_db, connection_id
    ) -> None:
        await self._seed(semantic_db, connection_id)
        metrics = [m.model_dump(mode="json") for m in _rich_model().business_metrics]
        metrics.append({"name": "Margin", "metric_type": "derived", "definition": "a - b"})
        patch = SemanticModelUpdate.model_validate(
            {
                "business_metrics": metrics,
                "tables": {
                    "public.orders": {
                        "columns": {
                            "status": {
                                "value_mappings": [{"raw_value": "r", "display_value": "Refunded"}]
                            }
                        }
                    }
                },
            }
        )
        await store.apply_user_patch(semantic_db, connection_id, patch, updated_by="u2")
        await semantic_db.commit()
        semantic_db.expunge_all()
        margin = await semantic_db.scalar(
            select(SemanticMetricRow).where(SemanticMetricRow.name == "Margin")
        )
        assert margin.payload["filters"] == []
        assert margin.payload["description"] == ""
        status = await semantic_db.scalar(
            select(SemanticColumnRow).where(SemanticColumnRow.column_name == "status")
        )
        assert status.value_mappings == [
            {"raw_value": "r", "display_value": "Refunded", "description": None}
        ]
        before = await self._rows_and_history(semantic_db, connection_id)
        semantic_db.expunge_all()

        await self._echo_reloaded_model(semantic_db, connection_id)
        assert await self._rows_and_history(semantic_db, connection_id) == before

    async def test_echo_of_a_migrated_shape_row_marks_nothing(
        self, semantic_db, connection_id
    ) -> None:
        await self._seed(semantic_db, connection_id)
        # Shapes migration 0003 copies verbatim: defaulted keys missing, a value
        # mapping as a bare string, an unknown semantic type.
        revenue = await semantic_db.scalar(
            select(SemanticMetricRow).where(SemanticMetricRow.name == "Revenue")
        )
        revenue.payload = {
            key: value
            for key, value in revenue.payload.items()
            if key not in ("filters", "measure_filters", "format_hint", "is_non_additive")
        }
        segment = await semantic_db.scalar(
            select(SemanticGlobalItemRow).where(SemanticGlobalItemRow.kind == "segment")
        )
        segment.payload = {k: v for k, v in segment.payload.items() if k != "applicable_tables"}
        status = await semantic_db.scalar(
            select(SemanticColumnRow).where(SemanticColumnRow.column_name == "status")
        )
        status.value_mappings = ["p"]
        status.semantic_type = "legacy_type"
        await semantic_db.commit()
        semantic_db.expunge_all()
        before = await self._rows_and_history(semantic_db, connection_id)
        semantic_db.expunge_all()

        await self._echo_reloaded_model(semantic_db, connection_id)
        after = await self._rows_and_history(semantic_db, connection_id)
        assert after == before
        assert after[2]["metric"] == {}
        assert after[2]["column"] == {}

    async def test_null_semantic_type_preserves_the_stored_value(
        self, semantic_db, connection_id
    ) -> None:
        await self._seed(semantic_db, connection_id)
        patch = SemanticModelUpdate(
            tables={
                "public.orders": TableSemanticUpdate(
                    columns={
                        "status": ColumnSemanticUpdate(semantic_type=None),
                        "new_col": ColumnSemanticUpdate(display_name="New", semantic_type=None),
                    }
                )
            }
        )
        model = await store.apply_user_patch(semantic_db, connection_id, patch, updated_by="u2")
        await semantic_db.commit()
        columns = model.tables["public.orders"].columns
        assert columns["status"].semantic_type == SemanticType.CATEGORICAL
        assert columns["new_col"].semantic_type == SemanticType.UNKNOWN

    async def test_a_new_row_marks_only_the_non_null_fields_sent(
        self, semantic_db, connection_id
    ) -> None:
        await self._seed(semantic_db, connection_id)
        patch = SemanticModelUpdate(
            tables={
                "public.items": TableSemanticUpdate(
                    display_name="Items",
                    description=None,
                    grain=None,
                    columns={
                        "sku": ColumnSemanticUpdate(
                            display_name="SKU", description=None, currency=None
                        )
                    },
                )
            }
        )
        await store.apply_user_patch(semantic_db, connection_id, patch, updated_by="u2")
        await semantic_db.commit()
        semantic_db.expunge_all()
        prov = await store.provenance_map(semantic_db, connection_id)
        assert prov["table"]["public.items"] == ["display_name"]
        assert prov["column"]["public.items.sku"] == ["display_name"]

    async def test_a_new_column_sent_with_defaults_stays_open_to_generation(
        self, semantic_db, connection_id
    ) -> None:
        await self._seed(semantic_db, connection_id)
        # A stale draft re-creating a pruned column echoes the defaults.
        patch = SemanticModelUpdate(
            tables={
                "public.orders": TableSemanticUpdate(
                    columns={
                        "sku": ColumnSemanticUpdate(
                            display_name="SKU",
                            is_sensitive=False,
                            semantic_type=SemanticType.UNKNOWN,
                            value_mappings=[],
                        )
                    }
                )
            }
        )
        await store.apply_user_patch(semantic_db, connection_id, patch, updated_by="u2")
        await semantic_db.commit()
        semantic_db.expunge_all()
        prov = await store.provenance_map(semantic_db, connection_id)
        assert prov["column"]["public.orders.sku"] == ["display_name"]

        generated = _rich_model().tables["public.orders"].model_copy(deep=True)
        generated.columns["sku"] = ColumnSemantic(display_name="Sku", is_sensitive=True)
        await store.reconcile_tables(
            semantic_db, connection_id, {"public.orders": generated}, updated_by="gen"
        )
        await semantic_db.commit()
        semantic_db.expunge_all()
        sku = (
            (await store.load_model(semantic_db, connection_id))
            .tables["public.orders"]
            .columns["sku"]
        )
        assert sku.is_sensitive is True
        assert sku.display_name == "SKU"  # the user's marked field survives

    async def test_patch_without_a_model_creates_one(self, semantic_db, connection_id) -> None:
        patch = SemanticModelUpdate(tables={"public.x": TableSemanticUpdate(display_name="X")})
        model = await store.apply_user_patch(semantic_db, connection_id, patch, updated_by="u2")
        await semantic_db.commit()
        assert set(model.tables) == {"public.x"}

    async def test_values_that_need_fitting_are_an_echo_on_the_second_apply(
        self, semantic_db, connection_id
    ) -> None:
        await self._seed(semantic_db, connection_id)

        def patch() -> SemanticModelUpdate:
            full = _rich_model().model_dump(mode="json")
            full["tables"]["public.orders"]["domain"] = "d" * 200
            full["tables"]["public.orders"]["columns"]["total"]["currency"] = "US Dollars"
            full["notes"] = ["a\x00b"]
            full["business_metrics"][0]["description"] = "gross\x00 revenue"
            full["segments"][0]["description"] = "paid\x00 orders"
            full["relationships"][0]["description"] = "x\x00y"
            return SemanticModelUpdate.model_validate(full)

        async def snapshot() -> tuple:
            rows = []
            for row_cls in (
                SemanticModelRow,
                SemanticTableRow,
                SemanticColumnRow,
                SemanticMetricRow,
                SemanticGlobalItemRow,
                SemanticRelationshipRow,
            ):
                rows.extend(
                    (r.id, r.version) for r in (await semantic_db.scalars(select(row_cls))).all()
                )
            history = await semantic_db.scalar(select(func.count()).select_from(SemanticHistoryRow))
            return sorted(rows), history, await store.provenance_map(semantic_db, connection_id)

        model = await store.apply_user_patch(semantic_db, connection_id, patch(), updated_by="u2")
        await semantic_db.commit()
        semantic_db.expunge_all()
        assert model.tables["public.orders"].domain == "d" * 128
        assert model.tables["public.orders"].columns["total"].currency == "US Dolla"
        assert model.notes == ["ab"]
        # SQLite keeps NULs, so check that what was inserted is the fitted value.
        assert model.business_metrics[0].description == "gross revenue"
        assert model.segments[0].description == "paid orders"
        assert model.relationships[0].description == "xy"
        first = await snapshot()
        assert first[2] == {
            "model": {"": ["notes"]},
            "table": {"public.orders": ["domain"]},
            "column": {"public.orders.total": ["currency"]},
            "metric": {"Revenue": ["payload"]},
            "global_item": {"segment:Paid": ["payload"]},
            "relationship": {"public.orders.customer_id->public.customers.id": ["payload"]},
        }
        semantic_db.expunge_all()

        await store.apply_user_patch(semantic_db, connection_id, patch(), updated_by="u2")
        await semantic_db.commit()
        semantic_db.expunge_all()
        assert await snapshot() == first

    async def test_over_long_identity_raises_and_writes_nothing(
        self, semantic_db, connection_id
    ) -> None:
        patch = SemanticModelUpdate(tables={"t" * 600: TableSemanticUpdate(display_name="T")})
        with pytest.raises(ValueError, match="table_key"):
            await store.apply_user_patch(semantic_db, connection_id, patch, updated_by="u2")
        assert await semantic_db.scalar(select(SemanticModelRow)) is None
        assert await semantic_db.scalar(select(SemanticHistoryRow)) is None


class TestOptimisticLock:
    async def _seed_and_read(self, db, cid) -> SemanticTableRow:
        await store.save_model(db, cid, _rich_model(), updated_by="u1")
        await db.commit()
        db.expunge_all()
        return await db.scalar(
            select(SemanticTableRow).where(SemanticTableRow.table_key == "public.orders")
        )

    async def test_stale_version_raises_instead_of_clobbering(
        self, semantic_db, connection_id
    ) -> None:
        row = await self._seed_and_read(semantic_db, connection_id)
        with pytest.raises(store.SemanticStoreConflict):
            await store.update_table_fields(
                semantic_db,
                row.id,
                expected_version=row.version + 1,
                values={"description": "late writer"},
                updated_by="u2",
                mark_user_edited=True,
            )

    async def test_a_writer_that_committed_first_is_not_overwritten(
        self, semantic_db, connection_id
    ) -> None:
        row = await self._seed_and_read(semantic_db, connection_id)
        table_id, stale_version = row.id, row.version
        # Another transaction commits a write. synchronize_session=False leaves this
        # session's identity-mapped ``row`` as stale as a separate transaction would.
        await semantic_db.execute(
            update(SemanticTableRow)
            .where(SemanticTableRow.id == table_id)
            .values(description="other writer", version=stale_version + 1)
            .execution_options(synchronize_session=False)
        )
        await semantic_db.commit()
        assert row.version == stale_version

        with pytest.raises(store.SemanticStoreConflict):
            await store.update_table_fields(
                semantic_db,
                table_id,
                expected_version=stale_version,
                values={"description": "late writer"},
                updated_by="u2",
                mark_user_edited=True,
            )
        await semantic_db.rollback()
        semantic_db.expunge_all()
        stored = await semantic_db.scalar(
            select(SemanticTableRow).where(SemanticTableRow.id == table_id)
        )
        assert stored.description == "other writer"
        assert stored.version == stale_version + 1
        assert stored.user_edited_fields == []
        table_history = await semantic_db.scalar(
            select(func.count())
            .select_from(SemanticHistoryRow)
            .where(SemanticHistoryRow.entity_type == "table")
        )
        assert table_history == 0

    async def test_matching_version_writes_and_bumps(self, semantic_db, connection_id) -> None:
        row = await self._seed_and_read(semantic_db, connection_id)
        await store.update_table_fields(
            semantic_db,
            row.id,
            expected_version=row.version,
            values={"description": "fresh"},
            updated_by="u2",
            mark_user_edited=True,
        )
        # The in-session object reflects the Core UPDATE without a refresh.
        assert row.version == 2
        assert row.description == "fresh"
        assert row.user_edited_fields == ["description"]
        await semantic_db.commit()
        semantic_db.expunge_all()
        again = await semantic_db.scalar(
            select(SemanticTableRow).where(SemanticTableRow.id == row.id)
        )
        assert again.version == 2
        assert again.description == "fresh"
        assert again.user_edited_fields == ["description"]

    async def test_an_echo_writes_nothing(self, semantic_db, connection_id) -> None:
        row = await self._seed_and_read(semantic_db, connection_id)
        await store.update_table_fields(
            semantic_db,
            row.id,
            expected_version=row.version,
            values={"description": "One row per order", "domain": "sales"},
            updated_by="u2",
            mark_user_edited=True,
        )
        await semantic_db.commit()
        semantic_db.expunge_all()
        again = await semantic_db.scalar(
            select(SemanticTableRow).where(SemanticTableRow.id == row.id)
        )
        assert again.version == 1
        assert again.user_edited_fields == []
