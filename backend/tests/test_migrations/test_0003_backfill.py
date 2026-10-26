# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Blob → rows round trip for migration 0003."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from sqlalchemy import select, text

from app.models.connection import Connection
from app.models.semantic_store import (
    SemanticColumnRow,
    SemanticGlobalItemRow,
    SemanticMetricRow,
    SemanticModelRow,
    SemanticRelationshipRow,
    SemanticTableRow,
)
from app.semantic import store
from app.semantic.models import SemanticModel
from tests.migration_utils import load_migration

# A real generated-shape model: every section populated, a hierarchy, value
# mappings, a sensitive column, two relationship directions and a duplicate
# metric name (legal in a legacy blob, illegal under the unique index).
_BLOB = {
    "tables": {
        "public.orders": {
            "display_name": "Orders",
            "description": "One row per order",
            "grain": "one row per order",
            "default_filters": ["deleted_at IS NULL"],
            "primary_timestamp_column": "created_at",
            "domain": "sales",
            "hierarchies": [{"name": "Date", "levels": ["year", "month", "day"]}],
            "partition_columns": ["created_at"],
            "columns": {
                "status": {
                    "display_name": "Status",
                    "semantic_type": "categorical",
                    "value_mappings": [{"raw_value": "p", "display_value": "Paid"}],
                },
                "email": {"display_name": "Email", "is_sensitive": 1},
            },
        },
        "public.customers": {"display_name": "Customers", "columns": {}},
    },
    "business_metrics": [
        {
            "name": "Revenue",
            "metric_type": "simple",
            "definition": "SUM(total)",
            "description": "d",
            "aggregation": "sum",
        },
        {
            "name": "Revenue",
            "metric_type": "simple",
            "definition": "SUM(total2)",
            "description": "dup",
            "aggregation": "sum",
        },
    ],
    "relationships": [
        {
            "from_table": "public.orders",
            "from_column": "customer_id",
            "to_table": "public.customers",
            "to_column": "id",
            "relationship_type": "many_to_one",
            "join_sql": "o.customer_id = c.id",
        },
        {
            "from_table": "public.orders",
            "from_column": None,
            "to_table": "public.customers",
            "to_column": "id",
        },
    ],
    "segments": [
        {
            "name": "Paid",
            "sql_expression": "status = 'p'",
            "description": "",
            "applicable_tables": ["public.orders"],
        }
    ],
    "derived_columns": [
        {
            "name": "net",
            "sql_expression": "total - tax",
            "base_tables": ["public.orders"],
            "description": "",
            "format_hint": None,
            "available_on": [],
        }
    ],
    "common_joins": [
        {
            "description": "orders to customers",
            "tables": ["public.orders", "public.customers"],
            "join_pattern": "o.customer_id = c.id",
        }
    ],
    "notes": ["budgets are quarterly"],
    "time_expressions": {"last quarter": "x"},
    "schema_hash": "abc",
    "source_dialect": "postgresql",
    "generation_status": "complete",
    "is_user_reviewed": True,
    "generated_at": "2026-01-01T00:00:00+00:00",
    "generation_model": "test",
}


async def _seed_blob(semantic_db, connection_id, blob) -> None:
    await semantic_db.execute(
        text("UPDATE connections SET semantic_model = :blob WHERE id = :id"),
        {"blob": json.dumps(blob) if blob is not None else None, "id": connection_id},
    )
    await semantic_db.commit()


async def _run_backfill(semantic_db) -> None:
    mig = load_migration("0003_semantic_blob_backfill.py")
    conn = await semantic_db.connection()
    await conn.run_sync(mig.backfill_all)
    await semantic_db.commit()


class TestBackfill:
    async def test_blob_round_trips_into_rows(self, semantic_db, connection_id) -> None:
        await _seed_blob(semantic_db, connection_id, _BLOB)
        await _run_backfill(semantic_db)

        header = await semantic_db.scalar(
            select(SemanticModelRow).where(SemanticModelRow.connection_id == connection_id)
        )
        assert header is not None
        assert header.generation_status == "complete"
        assert header.is_user_reviewed is True
        assert header.notes == ["budgets are quarterly"]
        assert header.user_edited_fields == []

        tables = {
            t.table_key: t for t in (await semantic_db.scalars(select(SemanticTableRow))).all()
        }
        assert set(tables) == {"public.orders", "public.customers"}
        assert tables["public.orders"].hierarchies == [
            {"name": "Date", "levels": ["year", "month", "day"]}
        ]
        columns = {
            c.column_name: c for c in (await semantic_db.scalars(select(SemanticColumnRow))).all()
        }
        assert columns["status"].value_mappings == [{"raw_value": "p", "display_value": "Paid"}]
        # Legacy truthy-but-not-bool survives as a real bool.
        assert columns["email"].is_sensitive is True

        metrics = (await semantic_db.scalars(select(SemanticMetricRow))).all()
        assert [m.name for m in metrics] == ["Revenue"]  # duplicate skipped, first wins
        rels = (await semantic_db.scalars(select(SemanticRelationshipRow))).all()
        assert len(rels) == 1  # the edge with a null endpoint is dropped
        items = {
            (g.kind, g.name)
            for g in (await semantic_db.scalars(select(SemanticGlobalItemRow))).all()
        }
        assert items == {
            ("segment", "Paid"),
            ("derived_column", "net"),
            ("common_join", "o.customer_id = c.id"),
        }

    async def test_backfilled_rows_validate_as_a_semantic_model(
        self, semantic_db, connection_id
    ) -> None:
        """What the store will assemble from these rows must be a valid SemanticModel."""
        await _seed_blob(semantic_db, connection_id, _BLOB)
        await _run_backfill(semantic_db)
        from app.semantic import store

        model = await store.load_model(semantic_db, connection_id)
        assert isinstance(model, SemanticModel)
        assert model.tables["public.orders"].columns["email"].is_sensitive is True
        assert [m.name for m in model.business_metrics] == ["Revenue"]

    async def test_is_idempotent(self, semantic_db, connection_id) -> None:
        await _seed_blob(semantic_db, connection_id, _BLOB)
        await _run_backfill(semantic_db)
        await _run_backfill(semantic_db)
        headers = (await semantic_db.scalars(select(SemanticModelRow))).all()
        assert len(headers) == 1

    async def test_non_object_blob_is_skipped_not_fatal(self, semantic_db, connection_id) -> None:
        await _seed_blob(semantic_db, connection_id, ["not", "a", "model"])
        await _run_backfill(semantic_db)
        assert (await semantic_db.scalars(select(SemanticModelRow))).all() == []

    async def test_null_blob_writes_nothing(self, semantic_db, connection_id) -> None:
        await _run_backfill(semantic_db)
        assert (await semantic_db.scalars(select(SemanticModelRow))).all() == []

    async def test_inactive_connection_is_not_backfilled(self, semantic_db, connection_id) -> None:
        """A soft-deleted connection's semantic rows would be orphans no route deletes."""
        await _add_connection(semantic_db, _SECOND_ID)
        await semantic_db.execute(
            text("UPDATE connections SET is_active = :f WHERE id = :id"),
            {"f": False, "id": _SECOND_ID},
        )
        await _seed_blob(semantic_db, connection_id, _BLOB)
        await _seed_blob(semantic_db, _SECOND_ID, _BLOB)
        await _run_backfill(semantic_db)

        assert set((await _counts(semantic_db, _SECOND_ID)).values()) == {0}
        assert (await _counts(semantic_db, connection_id))["semantic_models"] == 1


_SECOND_ID = "22222222-2222-2222-2222-222222222222"
_MIG = "0003_semantic_blob_backfill.py"


async def _add_connection(semantic_db, cid: str) -> None:
    semantic_db.add(
        Connection(id=cid, name=f"conn {cid[:2]}", source_type="postgresql", config_encrypted=b"")
    )
    await semantic_db.commit()


async def _counts(semantic_db, cid: str) -> dict[str, int]:
    """Row counts per semantic table for one connection (columns via their table)."""
    out: dict[str, int] = {}
    for table in (
        "semantic_models",
        "semantic_tables",
        "semantic_metrics",
        "semantic_relationships",
        "semantic_global_items",
    ):
        out[table] = await semantic_db.scalar(
            sa.select(sa.func.count())
            .select_from(sa.table(table))
            .where(sa.column("connection_id") == cid)
        )
    out["semantic_columns"] = await semantic_db.scalar(
        text(
            "SELECT count(*) FROM semantic_columns c JOIN semantic_tables t "
            "ON c.table_id = t.id WHERE t.connection_id = :c"
        ),
        {"c": cid},
    )
    return out


class TestBoundedValues:
    async def test_long_non_identity_text_is_capped(self, semantic_db, connection_id) -> None:
        blob = {
            "tables": {
                "public.orders": {
                    "domain": "d" * 200,
                    "columns": {"c": {"display_name": "n" * 600}},
                }
            },
            "business_metrics": [{"name": "M", "metric_type": "t" * 50}],
            "generation_model": "m" * 300,
        }
        await _seed_blob(semantic_db, connection_id, blob)
        await _run_backfill(semantic_db)

        table = await semantic_db.scalar(select(SemanticTableRow))
        column = await semantic_db.scalar(select(SemanticColumnRow))
        metric = await semantic_db.scalar(select(SemanticMetricRow))
        header = await semantic_db.scalar(select(SemanticModelRow))
        assert table.domain == "d" * 128
        assert column.display_name == "n" * 512
        assert metric.metric_type == "t" * 32
        assert header.generation_model == "m" * 128

    async def test_nul_characters_are_stripped_from_string_columns(
        self, semantic_db, connection_id
    ) -> None:
        blob = {"tables": {"public.t": {"display_name": "a\x00b", "columns": {}}}}
        await _seed_blob(semantic_db, connection_id, blob)
        await _run_backfill(semantic_db)
        table = await semantic_db.scalar(select(SemanticTableRow))
        assert table.display_name == "ab"

    async def test_over_long_table_key_skips_only_that_table(
        self, semantic_db, connection_id, caplog
    ) -> None:
        blob = {
            "tables": {
                "k" * 600: {"columns": {"c": {"display_name": "lost with its table"}}},
                "public.ok": {"columns": {"c": {"display_name": "kept"}}},
            },
            "business_metrics": [{"name": "m" * 600}, {"name": "Fine"}],
            "relationships": [
                {
                    "from_table": "f" * 600,
                    "from_column": "a",
                    "to_table": "public.ok",
                    "to_column": "c",
                }
            ],
        }
        await _seed_blob(semantic_db, connection_id, blob)
        with caplog.at_level(logging.WARNING):
            await _run_backfill(semantic_db)

        keys = (await semantic_db.scalars(select(SemanticTableRow.table_key))).all()
        assert keys == ["public.ok"]
        columns = (await semantic_db.scalars(select(SemanticColumnRow.display_name))).all()
        assert columns == ["kept"]
        assert (await semantic_db.scalars(select(SemanticMetricRow.name))).all() == ["Fine"]
        assert (await semantic_db.scalars(select(SemanticRelationshipRow))).all() == []
        assert connection_id in caplog.text and "skipping table" in caplog.text

    async def test_over_long_column_name_skips_only_that_column(
        self, semantic_db, connection_id
    ) -> None:
        blob = {"tables": {"public.t": {"columns": {"c" * 600: {}, "ok": {}}}}}
        await _seed_blob(semantic_db, connection_id, blob)
        await _run_backfill(semantic_db)
        names = (await semantic_db.scalars(select(SemanticColumnRow.column_name))).all()
        assert names == ["ok"]

    async def test_over_long_join_pattern_skips_only_that_item(
        self, semantic_db, connection_id, caplog
    ) -> None:
        """A join pattern is unbounded Text, but it sits in a unique index that
        PostgreSQL caps by bytes; one over the cap would fail the whole connection."""
        blob = {
            "tables": {"public.t": {"columns": {}}},
            "common_joins": [
                {"description": "too long", "tables": [], "join_pattern": "j" * 3000},
                {"description": "fine", "tables": [], "join_pattern": "o.id = c.id"},
            ],
        }
        await _seed_blob(semantic_db, connection_id, blob)
        with caplog.at_level(logging.WARNING):
            await _run_backfill(semantic_db)
        names = (await semantic_db.scalars(select(SemanticGlobalItemRow.name))).all()
        assert names == ["o.id = c.id"]
        assert connection_id in caplog.text and "skipping common_join" in caplog.text

    def test_index_name_cap_matches_the_store(self) -> None:
        """A migration must not import app code, so this test is what keeps the two
        caps equal."""
        mig = load_migration("0003_semantic_blob_backfill.py")
        assert mig._MAX_INDEXED_NAME_BYTES == store._MAX_INDEXED_TEXT_BYTES


class TestBooleanCoercion:
    async def test_string_false_is_false(self, semantic_db, connection_id) -> None:
        blob = {
            "is_user_reviewed": "False",
            "tables": {
                "public.t": {
                    "columns": {
                        "a": {"is_sensitive": "false"},
                        "b": {"is_sensitive": " No "},
                        "c": {"is_sensitive": "yes"},
                        "d": {"is_non_additive": "0"},
                    }
                }
            },
            "relationships": [
                {
                    "from_table": "a",
                    "from_column": "b",
                    "to_table": "c",
                    "to_column": "d",
                    "is_required": "off",
                }
            ],
        }
        await _seed_blob(semantic_db, connection_id, blob)
        await _run_backfill(semantic_db)

        columns = {
            c.column_name: c for c in (await semantic_db.scalars(select(SemanticColumnRow))).all()
        }
        assert columns["a"].is_sensitive is False
        assert columns["b"].is_sensitive is False
        assert columns["c"].is_sensitive is True
        assert columns["d"].is_non_additive is False
        header = await semantic_db.scalar(select(SemanticModelRow))
        assert header.is_user_reviewed is False
        rel = await semantic_db.scalar(select(SemanticRelationshipRow))
        assert rel.is_required is False


class TestSavepointIsolation:
    async def test_failed_connection_is_rolled_back_and_others_survive(
        self, semantic_db, connection_id, monkeypatch, caplog
    ) -> None:
        await _add_connection(semantic_db, _SECOND_ID)
        await _seed_blob(semantic_db, connection_id, _BLOB)
        await _seed_blob(semantic_db, _SECOND_ID, _BLOB)

        mig = load_migration(_MIG)
        real_insert = mig._insert_chunked

        def failing_insert(conn, table, rows, *rest):
            if table == "semantic_metrics" and rows[0]["connection_id"] == connection_id:
                raise RuntimeError("boom")
            return real_insert(conn, table, rows, *rest)

        monkeypatch.setattr(mig, "_insert_chunked", failing_insert)
        with caplog.at_level(logging.INFO):
            conn = await semantic_db.connection()
            await conn.run_sync(mig.backfill_all)
            await semantic_db.commit()

        assert await _counts(semantic_db, connection_id) == dict.fromkeys(
            (
                "semantic_models",
                "semantic_tables",
                "semantic_columns",
                "semantic_metrics",
                "semantic_relationships",
                "semantic_global_items",
            ),
            0,
        )
        assert await _counts(semantic_db, _SECOND_ID) == {
            "semantic_models": 1,
            "semantic_tables": 2,
            "semantic_columns": 2,
            "semantic_metrics": 1,
            "semantic_relationships": 1,
            "semantic_global_items": 3,
        }
        summary = [r for r in caplog.records if "connections backfilled" in r.getMessage()]
        assert len(summary) == 1 and summary[0].levelno == logging.ERROR
        assert "1/2 connections backfilled" in summary[0].getMessage()
        assert connection_id in summary[0].getMessage()

    async def test_clean_run_logs_info_summary(self, semantic_db, connection_id, caplog) -> None:
        await _seed_blob(semantic_db, connection_id, _BLOB)
        with caplog.at_level(logging.INFO):
            await _run_backfill(semantic_db)
        summary = [r for r in caplog.records if "connections backfilled" in r.getMessage()]
        assert len(summary) == 1 and summary[0].levelno == logging.INFO
        assert "1/1 connections backfilled; failed: none" in summary[0].getMessage()


class TestOtherInputs:
    async def test_empty_object_blob_writes_no_header(self, semantic_db, connection_id) -> None:
        await _seed_blob(semantic_db, connection_id, {})
        await _run_backfill(semantic_db)
        assert (await semantic_db.scalars(select(SemanticModelRow))).all() == []

    async def test_json_string_blob_is_backfilled(self, semantic_db, connection_id) -> None:
        # Seed the JSON text as a JSON *string* value (double-encoded), the shape
        # the ``_as_dict`` string branch exists for.
        await semantic_db.execute(
            text("UPDATE connections SET semantic_model = :blob WHERE id = :id"),
            {"blob": json.dumps(json.dumps(_BLOB)), "id": connection_id},
        )
        await semantic_db.commit()
        await _run_backfill(semantic_db)
        header = await semantic_db.scalar(select(SemanticModelRow))
        assert header is not None and header.generation_status == "complete"


class TestOfflineMode:
    def test_upgrade_refuses_to_render_offline(self) -> None:
        mig = load_migration(_MIG)
        with (
            patch("alembic.op.get_context", return_value=SimpleNamespace(as_sql=True)),
            pytest.raises(RuntimeError, match="offline"),
        ):
            mig.upgrade()

    async def test_json_null_blob_is_skipped_silently(
        self, semantic_db, connection_id, caplog
    ) -> None:
        # What ``DELETE /semantic`` leaves behind: JSON text ``null``, not SQL NULL.
        await semantic_db.execute(
            text("UPDATE connections SET semantic_model = 'null' WHERE id = :id"),
            {"id": connection_id},
        )
        await semantic_db.commit()
        with caplog.at_level(logging.WARNING):
            await _run_backfill(semantic_db)
        assert (await semantic_db.scalars(select(SemanticModelRow))).all() == []
        assert "not a JSON object" not in caplog.text
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    async def test_non_string_relationship_endpoint_is_skipped(
        self, semantic_db, connection_id
    ) -> None:
        good = {"from_table": "a", "from_column": "b", "to_table": "c", "to_column": "d"}
        blob = {"relationships": [{**good, "from_column": 7}, {**good, "to_table": ["c"]}, good]}
        await _seed_blob(semantic_db, connection_id, blob)
        await _run_backfill(semantic_db)
        rels = (await semantic_db.scalars(select(SemanticRelationshipRow))).all()
        assert [(r.from_table, r.from_column, r.to_table, r.to_column) for r in rels] == [
            ("a", "b", "c", "d")
        ]
