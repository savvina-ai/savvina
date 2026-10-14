# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for the normalised semantic store."""

from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.database import Base
from app.models.semantic_store import (
    SemanticColumnRow,
    SemanticGlobalItemRow,
    SemanticHistoryRow,
    SemanticMetricRow,
    SemanticModelRow,
    SemanticRelationshipRow,
    SemanticTableRow,
)
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

    async def test_delete_without_a_model_is_a_noop(self, semantic_db, connection_id) -> None:
        await store.delete_model(semantic_db, connection_id, changed_by="u1")
        await semantic_db.commit()

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
        # Global-item names are unbounded Text: only NULs go.
        long_name = "n" * 5000
        assert store._fit_values(SemanticGlobalItemRow, {"name": long_name})["name"] == long_name


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
