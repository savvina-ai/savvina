# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for /api/v1/connections/{id}/semantic endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
import json
import logging
import math
from pathlib import Path
import re
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
import pytest

from app.database import get_db
from app.datasources.models import ColumnInfo, DataSourceSchema, SchemaInfo, TableInfo
from app.main import app
from app.semantic import store as semantic_store
from app.semantic.generator import SemanticModelGenerator as _RealGenerator
from app.semantic.models import (
    GENERATION_BATCH_SIZE,
    AggregationType,
    CommonJoin,
    DerivedColumn,
    GenerationProgress,
    GenerationStatus,
    RelationshipEdge,
    Segment,
    SemanticModel,
    SimpleMetric,
    TableSemantic,
)

from .conftest import MockResult, _make_conn, _mock_db

# Minimal dict that validates as SemanticModelResponse
_EMPTY_MODEL = {
    "tables": {},
    "business_metrics": [],
    "common_joins": [],
    "is_user_reviewed": False,
}

_STORE = "app.routers.semantic.semantic_store"


def _populated() -> SemanticModel:
    return SemanticModel.model_validate(
        {
            **_EMPTY_MODEL,
            "tables": {
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"status": {"display_name": "Status"}},
                }
            },
        }
    )


# ── GET /api/v1/connections/{id}/semantic ───────────────────────────────────────


class TestGetSemanticModel:
    async def test_returns_200_when_model_exists(self, http_client):
        db = _mock_db(MockResult(single=_make_conn()))
        app.dependency_overrides[get_db] = lambda: db
        with patch(f"{_STORE}.load_model", new=AsyncMock(return_value=_populated())):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic")
        assert resp.status_code == 200
        assert "public.orders" in resp.json()["tables"]

    async def test_returns_404_when_no_model(self, http_client):
        db = _mock_db(MockResult(single=_make_conn()))
        app.dependency_overrides[get_db] = lambda: db
        with patch(f"{_STORE}.load_model", new=AsyncMock(return_value=None)):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic")
        assert resp.status_code == 404

    async def test_returns_404_when_connection_missing(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/missing/semantic")
        assert resp.status_code == 404


# ── PUT /api/v1/connections/{id}/semantic ───────────────────────────────────────


class TestUpdateSemanticModel:
    async def test_patch_goes_through_the_store_and_returns_its_model(self, http_client):
        db = _mock_db(
            MockResult(single=_make_conn()),  # lock_and_reread_connection
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
        )
        app.dependency_overrides[get_db] = lambda: db
        apply = AsyncMock(return_value=_populated())
        with patch(f"{_STORE}.apply_user_patch", new=apply):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/semantic",
                json={"tables": {"public.orders": {"description": "hand written"}}},
            )
        assert resp.status_code == 200
        assert "public.orders" in resp.json()["tables"]
        patch_arg = apply.await_args.args[2]
        assert patch_arg.tables["public.orders"].description == "hand written"
        assert apply.await_args.kwargs["updated_by"]
        db.commit.assert_awaited_once()

    async def test_invalidates_caches(self, http_client):
        """PUT semantic must invalidate all three cache tables before commit."""
        db = _mock_db(
            MockResult(single=_make_conn()),  # lock_and_reread_connection
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
        )
        app.dependency_overrides[get_db] = lambda: db
        with patch(f"{_STORE}.apply_user_patch", new=AsyncMock(return_value=_populated())):
            await http_client.put("/api/v1/connections/conn-123/semantic", json={})
        # 1 select (lock_and_reread) + 3 deletes; the store write is patched out.
        assert db.execute.await_count == 4

    async def test_store_conflict_is_409(self, http_client):
        db = _mock_db(MockResult(single=_make_conn()))
        app.dependency_overrides[get_db] = lambda: db
        with patch(
            f"{_STORE}.apply_user_patch",
            new=AsyncMock(side_effect=semantic_store.SemanticStoreConflict("moved on")),
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/semantic", json={"notes": ["x"]}
            )
        assert resp.status_code == 409
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_validation_error_is_400(self, http_client):
        db = _mock_db(MockResult(single=_make_conn()))
        app.dependency_overrides[get_db] = lambda: db
        with patch(
            f"{_STORE}.apply_user_patch", new=AsyncMock(side_effect=ValueError("bad shape"))
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/semantic", json={"notes": ["x"]}
            )
        assert resp.status_code == 400
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_returns_404_when_connection_missing(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put("/api/v1/connections/missing/semantic", json={})
        assert resp.status_code == 404


# ── DELETE /api/v1/connections/{id}/semantic ────────────────────────────────────


class TestDeleteSemanticModel:
    async def test_deletes_through_the_store(self, http_client):
        db = _mock_db(
            MockResult(single=_make_conn()),  # lock_and_reread_connection
            MockResult(),
            MockResult(),
            MockResult(),  # cache deletes
        )
        app.dependency_overrides[get_db] = lambda: db
        delete = AsyncMock()
        with patch(f"{_STORE}.delete_model", new=delete):
            resp = await http_client.delete("/api/v1/connections/conn-123/semantic")
        assert resp.status_code == 204
        delete.assert_awaited_once()
        assert delete.await_args.args[1] == "conn-123"
        assert delete.await_args.kwargs["changed_by"]
        db.commit.assert_awaited_once()

    async def test_invalidates_caches(self, http_client):
        """DELETE semantic must invalidate all three cache tables before commit."""
        db = _mock_db(
            MockResult(single=_make_conn()),  # lock_and_reread_connection
            MockResult(),
            MockResult(),
            MockResult(),  # cache deletes
        )
        app.dependency_overrides[get_db] = lambda: db
        with patch(f"{_STORE}.delete_model", new=AsyncMock()):
            await http_client.delete("/api/v1/connections/conn-123/semantic")
        # 1 select (lock_and_reread) + 3 cache deletes; the store delete (rows and blob)
        # is patched out.
        assert db.execute.await_count == 4

    async def test_returns_404_when_connection_missing(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.delete("/api/v1/connections/missing/semantic")
        assert resp.status_code == 404


# ── GET /api/v1/connections/{id}/semantic/drift ─────────────────────────────────


class TestCheckDrift:
    """Check Drift introspects live, so a drop shows up without a separate refresh."""

    def setup_method(self) -> None:
        _reset_limiter()

    async def test_compares_against_a_live_introspection(self, http_client):
        db = _mock_db(MockResult(single=_make_conn()))  # get_connection_or_404
        app.dependency_overrides[get_db] = lambda: db
        stored = SemanticModel(
            tables={
                "public.orders": TableSemantic(display_name="Orders"),
                "public.coupons": TableSemantic(display_name="Coupons"),
            }
        )
        refresh = AsyncMock(return_value=_schema(["orders"]))
        with (
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=stored)),
            patch("app.routers.semantic.refresh_user_schema_cache", new=refresh),
        ):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 200
        body = resp.json()
        assert any('"public.coupons"' in w for w in body["warnings"])
        assert body["schema_source"] == "live"
        assert body["refresh_error"] is None
        refresh.assert_awaited_once()
        # The refreshed cache is persisted.
        db.commit.assert_awaited()

    async def test_what_the_policy_hides_is_not_reported_as_removed(self, http_client):
        """The store keeps hidden annotations; drift must read the policy, not the absence."""
        conn = _make_conn(
            privacy_settings={
                "excluded_tables": ["public.payroll"],
                "excluded_columns": ["public.orders.ssn"],
            }
        )
        db = _mock_db(MockResult(single=conn))
        app.dependency_overrides[get_db] = lambda: db
        stored = SemanticModel(
            tables={
                "public.orders": TableSemantic(
                    display_name="Orders",
                    columns={"id": {"display_name": "Id"}, "ssn": {"display_name": "SSN"}},
                ),
                "public.payroll": TableSemantic(display_name="Payroll"),
                "public.ghosts": TableSemantic(display_name="Ghosts"),
            }
        )
        # The mocked introspection returns payroll although the policy excludes it
        # (the real adapter would leave it out), so the route's own narrowing must
        # remove it before the comparison.
        with (
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=stored)),
            patch(
                "app.routers.semantic.refresh_user_schema_cache",
                new=AsyncMock(return_value=_schema(["orders", "payroll"])),
            ),
        ):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 200
        warnings = resp.json()["warnings"]
        assert not any("payroll" in w or "ssn" in w for w in warnings)
        # A table that is genuinely gone is still reported.
        assert any('"public.ghosts"' in w for w in warnings)

    async def test_flags_actionable_changes_when_a_table_was_removed(self, http_client):
        """``has_actionable_changes`` drives the Apply Drift button."""
        db = _mock_db(MockResult(single=_make_conn()))
        app.dependency_overrides[get_db] = lambda: db
        stored = SemanticModel(
            tables={
                "public.orders": TableSemantic(display_name="Orders"),
                "public.coupons": TableSemantic(display_name="Coupons"),
            }
        )
        with (
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=stored)),
            patch(
                "app.routers.semantic.refresh_user_schema_cache",
                new=AsyncMock(return_value=_schema(["orders"])),
            ),
        ):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 200
        assert resp.json()["has_actionable_changes"] is True

    async def test_an_unreadable_table_alone_is_not_actionable(self, http_client):
        """Reported, but a missing grant is nothing an apply could fix."""
        db = _mock_db(MockResult(single=_make_conn()))
        app.dependency_overrides[get_db] = lambda: db
        stored = SemanticModel(
            tables={
                "public.orders": TableSemantic(display_name="Orders"),
                "public.payroll": TableSemantic(display_name="Payroll"),
            }
        )
        schema = _schema(["orders"])
        schema.unreadable_tables = ["public.payroll"]
        with (
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=stored)),
            patch(
                "app.routers.semantic.refresh_user_schema_cache",
                new=AsyncMock(return_value=schema),
            ),
        ):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 200
        body = resp.json()
        assert body["warning_count"] == 1
        assert "cannot read" in body["warnings"][0]
        assert body["has_actionable_changes"] is False

    async def test_an_unreachable_source_falls_back_to_the_cache_and_says_so(self, http_client):
        from datetime import UTC, datetime

        from app.services.schema_utils import SchemaRefreshError, _schema_to_dict

        usc = MagicMock()
        usc.schema_cache = _schema_to_dict(_schema(["orders"]))
        usc.schema_cached_at = datetime(2026, 9, 24, 7, 16, tzinfo=UTC)
        db = _mock_db(MockResult(single=_make_conn()), MockResult(single=usc))
        app.dependency_overrides[get_db] = lambda: db
        stored = SemanticModel(
            tables={
                "public.orders": TableSemantic(display_name="Orders"),
                "public.ghosts": TableSemantic(display_name="Ghosts"),
            }
        )
        with (
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=stored)),
            patch(
                "app.routers.semantic.refresh_user_schema_cache",
                new=AsyncMock(side_effect=SchemaRefreshError("connection refused")),
            ),
        ):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 200
        body = resp.json()
        assert body["schema_source"] == "cache"
        assert body["schema_cached_at"].startswith("2026-09-24T07:16")
        assert body["refresh_error"] == "connection refused"
        assert any('"public.ghosts"' in w for w in body["warnings"])

    async def test_an_unreachable_source_with_no_cache_is_400(self, http_client):
        from app.services.schema_utils import SchemaRefreshError

        db = _mock_db(
            MockResult(single=_make_conn()),  # get_connection_or_404
            MockResult(single=None),  # no user schema cache to fall back to
        )
        app.dependency_overrides[get_db] = lambda: db
        with (
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=_populated())),
            patch(
                "app.routers.semantic.refresh_user_schema_cache",
                new=AsyncMock(side_effect=SchemaRefreshError("connection refused")),
            ),
        ):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 400
        assert "connection refused" in resp.json()["detail"]

    async def test_returns_404_no_semantic_model(self, http_client):
        db = _mock_db(MockResult(single=_make_conn()))
        app.dependency_overrides[get_db] = lambda: db
        refresh = AsyncMock()
        with (
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=None)),
            patch("app.routers.semantic.refresh_user_schema_cache", new=refresh),
        ):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 404
        # No model, no reason to touch the data source.
        refresh.assert_not_awaited()


# ── Phased generation ───────────────────────────────────────────────────────────


def _schema(names: list[str], columns: dict[str, list[str]] | None = None) -> DataSourceSchema:
    """A ``public`` schema; each table has an ``id`` column unless ``columns`` names its own."""
    cols = columns or {}
    return DataSourceSchema(
        source_type="postgresql",
        schemas=[SchemaInfo(name="public")],
        tables=[
            TableInfo(
                catalog=None,
                schema_name="public",
                name=n,
                table_type="table",
                columns=[
                    ColumnInfo(name=c, data_type="integer", native_type="int4")
                    for c in cols.get(n, ["id"])
                ],
            )
            for n in names
        ],
    )


def _fk_schema() -> DataSourceSchema:
    """``orders.ssn_ref`` is excluded, so the adapter never introspected it."""
    return _schema(
        ["orders", "customers"],
        columns={"orders": ["id", "customer_id"], "customers": ["id"]},
    )


def _fk_edges() -> tuple[RelationshipEdge, RelationshipEdge]:
    """A live FK edge and one on the excluded ``orders.ssn_ref`` (adapter FKs keep it)."""

    def edge(column: str) -> RelationshipEdge:
        return RelationshipEdge(
            from_table="public.orders",
            from_column=column,
            to_table="public.customers",
            to_column="id",
            join_sql=f"public.orders.{column} = public.customers.id",
        )

    return edge("customer_id"), edge("ssn_ref")


def _header(
    status: GenerationStatus,
    schema_hash: str | None,
    done: int = 0,
    total: int = 2,
    run_id: str | None = "run-1",
):
    return semantic_store.SemanticHeader(
        generation_status=str(status),
        schema_hash=schema_hash,
        generation_progress=GenerationProgress(tables_done=done, tables_total=total, batch_size=4),
        generation_run_id=run_id,
    )


def _no_provenance() -> dict:
    return {
        "model": {},
        "table": {},
        "column": {},
        "metric": {},
        "global_item": {},
        "relationship": {},
    }


def _snapshot(
    marks: dict | None = None, protected: dict | None = None
) -> semantic_store.ProvenanceSnapshot:
    return semantic_store.ProvenanceSnapshot(
        marks=marks or _no_provenance(), protected=protected or {"table": {}, "column": {}}
    )


def _reset_limiter() -> None:
    from app.auth.limiter import limiter

    limiter._storage.reset()


class TestGenerateInit:
    def setup_method(self) -> None:
        _reset_limiter()

    async def test_fresh_run_plans_every_key_and_blanks_nothing(self, http_client) -> None:
        conn = _make_conn()
        begin = AsyncMock()
        # One result only, for the row lock: any direct row write would fail the request.
        db = _mock_db(MockResult(single=_make_conn()))
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(f"{_STORE}.begin_generation", new=begin),
        ):
            mock_load.return_value = (_schema(["orders", "customers"]), conn)
            gen = mock_gen.return_value
            gen.compute_schema_hash.return_value = "hash-abc"
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post("/api/v1/connections/conn-123/semantic/generate/init")

        assert resp.status_code == 200
        body = resp.json()
        assert body["pending_table_keys"] == ["public.orders", "public.customers"]
        assert body["resumed"] is False
        assert body["batch_size"] == GENERATION_BATCH_SIZE
        assert body["batch_count"] == 1
        kwargs = begin.await_args.kwargs
        assert kwargs["schema_hash"] == "hash-abc"
        assert kwargs["tables_total"] == 2
        assert kwargs["tables_done"] == 0
        assert kwargs["batch_size"] == GENERATION_BATCH_SIZE
        assert kwargs["source_dialect"] == "postgresql"
        assert kwargs["resume"] is False
        assert db.execute.await_count == 1
        assert db.execute.await_args.args[0]._for_update_arg is not None
        db.commit.assert_awaited_once()

    async def test_resume_returns_only_pending_keys(self, http_client) -> None:
        conn = _make_conn()
        begin = AsyncMock()
        pending = AsyncMock(return_value=["public.customers"])
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(
                f"{_STORE}.load_header",
                new=AsyncMock(return_value=_header(GenerationStatus.TABLES_PARTIAL, "h")),
            ),
            patch(f"{_STORE}.pending_table_keys", new=pending),
            patch(f"{_STORE}.begin_generation", new=begin),
        ):
            mock_load.return_value = (_schema(["orders", "customers"]), conn)
            gen = mock_gen.return_value
            gen.compute_schema_hash.return_value = "h"
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(
                "/api/v1/connections/conn-123/semantic/generate/init?resume=true"
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["resumed"] is True
        assert body["pending_table_keys"] == ["public.customers"]
        assert pending.await_args.args[2] == ["public.orders", "public.customers"]
        assert begin.await_args.kwargs["tables_done"] == 1
        assert begin.await_args.kwargs["resume"] is True

    async def test_resume_without_an_interrupted_run_is_400(self, http_client) -> None:
        begin = AsyncMock()
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(f"{_STORE}.load_header", new=AsyncMock(return_value=None)),
            patch(f"{_STORE}.begin_generation", new=begin),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            mock_gen.return_value.compute_schema_hash.return_value = "h"
            db = _mock_db(MockResult(single=_make_conn()))
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(
                "/api/v1/connections/conn-123/semantic/generate/init?resume=true"
            )
        assert resp.status_code == 400
        db.rollback.assert_awaited()  # the row lock is released
        db.commit.assert_not_awaited()
        begin.assert_not_awaited()

    async def test_resume_of_a_finished_run_is_400(self, http_client) -> None:
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(
                f"{_STORE}.load_header",
                new=AsyncMock(return_value=_header(GenerationStatus.COMPLETE, "h")),
            ),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            mock_gen.return_value.compute_schema_hash.return_value = "h"
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(
                "/api/v1/connections/conn-123/semantic/generate/init?resume=true"
            )
        assert resp.status_code == 400

    async def test_resume_onto_a_changed_schema_is_409(self, http_client) -> None:
        begin = AsyncMock()
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(
                f"{_STORE}.load_header",
                new=AsyncMock(return_value=_header(GenerationStatus.TABLES_PARTIAL, "old")),
            ),
            patch(f"{_STORE}.begin_generation", new=begin),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            mock_gen.return_value.compute_schema_hash.return_value = "new"
            db = _mock_db(MockResult(single=_make_conn()))
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(
                "/api/v1/connections/conn-123/semantic/generate/init?resume=true"
            )
        assert resp.status_code == 409
        db.rollback.assert_awaited()  # the row lock is released
        db.commit.assert_not_awaited()
        detail = resp.json()["detail"]
        assert "schema changed" in detail.lower()
        # Points at the non-destructive fix first, and says what Restart costs.
        assert "regenerate (keeps your edits)" in detail
        assert "Restart from scratch discards them" in detail
        begin.assert_not_awaited()

    async def test_takes_the_row_lock_before_reading_run_state(self, http_client) -> None:
        calls: list[str] = []

        async def lock(*_a, **_k):
            calls.append("lock")
            return _make_conn()

        async def header(*_a, **_k):
            calls.append("load_header")
            return _header(GenerationStatus.TABLES_PARTIAL, "h")

        async def pending(*_a, **_k):
            calls.append("pending_table_keys")
            return []

        async def begin(*_a, **_k):
            calls.append("begin_generation")

        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch("app.routers.semantic.lock_and_reread_connection", new=lock),
            patch(f"{_STORE}.load_header", new=header),
            patch(f"{_STORE}.pending_table_keys", new=pending),
            patch(f"{_STORE}.begin_generation", new=begin),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            mock_gen.return_value.compute_schema_hash.return_value = "h"
            app.dependency_overrides[get_db] = lambda: _mock_db()
            resp = await http_client.post(
                "/api/v1/connections/conn-123/semantic/generate/init?resume=true"
            )
        assert resp.status_code == 200
        assert calls == ["lock", "load_header", "pending_table_keys", "begin_generation"]

    async def test_connection_deleted_before_the_lock_is_409(self, http_client) -> None:
        begin = AsyncMock()
        db = _mock_db(MockResult(single=None))  # lock_and_reread finds no row
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(f"{_STORE}.begin_generation", new=begin),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            mock_gen.return_value.compute_schema_hash.return_value = "h"
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post("/api/v1/connections/conn-123/semantic/generate/init")
        assert resp.status_code == 409
        begin.assert_not_awaited()
        db.rollback.assert_awaited()

    async def test_resume_of_a_migrated_run_without_hash_or_run_id_is_a_fresh_run(
        self, http_client
    ) -> None:
        """Migration 0003 backfills a blob-era interrupted run with neither a schema
        hash nor a run id. There is no run to resume: the user gets a fresh,
        non-destructive run instead of a false 'schema changed'."""
        begin = AsyncMock()
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(
                f"{_STORE}.load_header",
                new=AsyncMock(
                    return_value=_header(GenerationStatus.TABLES_PARTIAL, None, run_id=None)
                ),
            ),
            patch(f"{_STORE}.begin_generation", new=begin),
        ):
            mock_load.return_value = (_schema(["orders", "customers"]), _make_conn())
            mock_gen.return_value.compute_schema_hash.return_value = "h"
            db = _mock_db(MockResult(single=_make_conn()))
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(
                "/api/v1/connections/conn-123/semantic/generate/init?resume=true"
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["resumed"] is False
        assert body["pending_table_keys"] == ["public.orders", "public.customers"]
        assert begin.await_args.kwargs["resume"] is False
        assert begin.await_args.kwargs["tables_done"] == 0
        db.commit.assert_awaited_once()


_BATCH_URL = "/api/v1/connections/conn-123/semantic/generate/batch"


class TestGenerateBatch:
    def setup_method(self) -> None:
        _reset_limiter()

    def _provider(self):
        return (MagicMock(), "model-x", "claude")

    def _patches(
        self,
        mock_gen_target: str = "app.routers.semantic.SemanticModelGenerator",
        headers: list | None = None,
        reconcile: AsyncMock | None = None,
        advance: AsyncMock | None = None,
    ):
        """The patches a batch that reaches the LLM needs; returns them for ``with``."""
        hdrs = iter(headers or [_header(GenerationStatus.TABLES_PARTIAL, "h")] * 2)
        return (
            patch("app.routers.semantic._load_schema_for_connection"),
            patch(
                "app.routers.semantic._resolve_provider",
                new=AsyncMock(return_value=self._provider()),
            ),
            patch(mock_gen_target),
            patch(f"{_STORE}.load_header", new=AsyncMock(side_effect=lambda *_: next(hdrs))),
            patch(f"{_STORE}.reconcile_tables", new=reconcile or AsyncMock()),
            patch(
                f"{_STORE}.advance_progress",
                new=advance
                or AsyncMock(
                    return_value=GenerationProgress(tables_done=1, tables_total=2, batch_size=4)
                ),
            ),
        )

    @staticmethod
    def _wire(mock_load, mock_gen, generated, schema=None) -> MagicMock:
        mock_load.return_value = (schema or _schema(["orders", "customers"]), _make_conn())
        gen = mock_gen.return_value
        gen.compute_schema_hash.return_value = "h"
        gen._build_relationship_graph.return_value = []
        if isinstance(generated, BaseException):
            gen.generate_tables = AsyncMock(side_effect=generated)
        else:
            gen.generate_tables = AsyncMock(return_value=generated)
        return gen

    async def test_passes_table_keys_to_the_generator_and_reconciles(self, http_client) -> None:
        generated = {"public.orders": TableSemantic(display_name="Orders")}
        reconcile = AsyncMock()
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(reconcile=reconcile)
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            gen = self._wire(mock_load, mock_gen, generated)
            db = _mock_db(MockResult(single=_make_conn()))  # lock_and_reread_connection
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})

        assert resp.status_code == 200
        assert resp.json() == {
            "connection_id": "conn-123",
            "table_keys": ["public.orders"],
            "tables_done": 1,
            "tables_total": 2,
            "batch_size": 4,
            "omitted_table_keys": [],
        }
        assert gen.generate_tables.await_args.kwargs["table_keys"] == ["public.orders"]
        assert reconcile.await_args.args[2] == generated
        assert reconcile.await_args.kwargs["updated_by"] == "admin-1"
        assert reconcile.await_args.kwargs["expected_run_id"] == "run-1"
        # The connection row lock is taken before the reconcile.
        stmt = db.execute.await_args_list[0].args[0]
        assert stmt._for_update_arg is not None
        db.commit.assert_awaited_once()

    async def test_tables_the_batch_did_not_name_are_not_reconciled(self, http_client) -> None:
        generated = {
            "public.orders": TableSemantic(display_name="Orders"),
            "public.customers": TableSemantic(display_name="Not asked for"),
        }
        reconcile = AsyncMock()
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(reconcile=reconcile)
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, generated)
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})

        assert resp.status_code == 200
        assert set(reconcile.await_args.args[2]) == {"public.orders"}

    async def test_omitted_tables_are_reported_and_not_reconciled(self, http_client) -> None:
        generated = {"public.orders": TableSemantic(display_name="Orders")}
        reconcile = AsyncMock()
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(reconcile=reconcile)
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, generated)
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(
                _BATCH_URL, json={"table_keys": ["public.orders", "public.customers"]}
            )
        assert resp.status_code == 200
        assert resp.json()["omitted_table_keys"] == ["public.customers"]
        assert set(reconcile.await_args.args[2]) == {"public.orders"}

    async def test_a_provider_throttle_is_429_so_the_client_retries(
        self, http_client, caplog
    ) -> None:
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches()
        with (
            caplog.at_level(logging.WARNING, logger="app.routers.semantic"),
            p_load as mock_load,
            p_prov,
            p_gen as mock_gen,
            p_hdr,
            p_rec,
            p_adv,
        ):
            self._wire(
                mock_load,
                mock_gen,
                ValueError("LLM call failed in generate_tables: [RATE_LIMIT] slow down"),
            )
            app.dependency_overrides[get_db] = lambda: _mock_db()
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 429
        assert "[RATE_LIMIT]" in resp.json()["detail"]
        # Expected and retried: a warning naming the provider, no traceback.
        [record] = [r for r in caplog.records if r.name == "app.routers.semantic"]
        assert record.levelno == logging.WARNING
        assert record.exc_info is None
        assert "provider=claude" in record.getMessage()

    async def test_a_provider_outage_is_503_so_the_client_retries(
        self, http_client, caplog
    ) -> None:
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches()
        with (
            caplog.at_level(logging.WARNING, logger="app.routers.semantic"),
            p_load as mock_load,
            p_prov,
            p_gen as mock_gen,
            p_hdr,
            p_rec,
            p_adv,
        ):
            self._wire(
                mock_load,
                mock_gen,
                ValueError(
                    "LLM call failed in generate_tables: [PROVIDER_UNAVAILABLE] "
                    "The provider returned HTTP 503."
                ),
            )
            app.dependency_overrides[get_db] = lambda: _mock_db()
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 503
        assert "[PROVIDER_UNAVAILABLE]" in resp.json()["detail"]
        # Expected and retried: a warning, no traceback.
        [record] = [r for r in caplog.records if r.name == "app.routers.semantic"]
        assert record.levelno == logging.WARNING
        assert record.exc_info is None

    async def test_an_exhausted_quota_is_422_not_a_retried_throttle(self, http_client) -> None:
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches()
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(
                mock_load,
                mock_gen,
                ValueError("LLM call failed in generate_tables: [QUOTA_EXCEEDED] no credit"),
            )
            app.dependency_overrides[get_db] = lambda: _mock_db()
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 422
        assert "[QUOTA_EXCEEDED]" in resp.json()["detail"]

    async def test_a_timeout_is_504_and_logged_as_a_warning(self, http_client, caplog) -> None:
        async def never_answers(**_kw):
            await asyncio.sleep(10)

        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches()
        with (
            caplog.at_level(logging.WARNING, logger="app.routers.semantic"),
            patch("app.routers.semantic._GENERATION_TIMEOUT_SECONDS", 0.01),
            p_load as mock_load,
            p_prov,
            p_gen as mock_gen,
            p_hdr,
            p_rec,
            p_adv,
        ):
            gen = self._wire(mock_load, mock_gen, {})
            gen.generate_tables = never_answers
            app.dependency_overrides[get_db] = lambda: _mock_db()
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 504
        assert "timed out" in resp.json()["detail"]
        [record] = [r for r in caplog.records if r.name == "app.routers.semantic"]
        assert record.levelno == logging.WARNING
        assert "provider=claude" in record.getMessage()

    async def test_an_fk_on_an_excluded_column_does_not_reach_the_generator(
        self, http_client
    ) -> None:
        """Adapter FKs are filtered by table only; the adapter never introspects an
        excluded column, so an FK edge on one must not name it in the prompt."""
        live, hidden = _fk_edges()
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches()
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            gen = self._wire(mock_load, mock_gen, {}, schema=_fk_schema())
            gen._build_relationship_graph.return_value = [live, hidden]
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})

        assert resp.status_code == 200
        assert gen.generate_tables.await_args.kwargs["relationship_edges"] == [live]

    async def test_omitted_tables_are_logged_and_left_pending(self, http_client, caplog) -> None:
        generated = {"public.orders": TableSemantic(display_name="Orders")}
        reconcile = AsyncMock()
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(reconcile=reconcile)
        with (
            caplog.at_level(logging.WARNING, logger="app.routers.semantic"),
            p_load as mock_load,
            p_prov,
            p_gen as mock_gen,
            p_hdr,
            p_rec,
            p_adv,
        ):
            self._wire(mock_load, mock_gen, generated)
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(
                _BATCH_URL, json={"table_keys": ["public.orders", "public.customers"]}
            )

        assert resp.status_code == 200
        assert resp.json()["table_keys"] == ["public.orders", "public.customers"]
        assert set(reconcile.await_args.args[2]) == {"public.orders"}
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("public.customers" in r.getMessage() for r in warnings)
        assert not any("public.orders'" in r.getMessage() for r in warnings)

    async def test_without_init_is_400(self, http_client) -> None:
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch(f"{_STORE}.load_header", new=AsyncMock(return_value=None)),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            app.dependency_overrides[get_db] = lambda: _mock_db()
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 400

    async def test_a_partial_header_without_a_run_id_is_400(self, http_client) -> None:
        """Migration 0003 backfills an interrupted blob-era run as tables_partial with no
        run id. Stamping tables with None would leave progress at 0 and make a later
        resume regenerate everything, so the batch sends the client to /generate/init."""
        reconcile = AsyncMock()
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(
                f"{_STORE}.load_header",
                new=AsyncMock(
                    return_value=_header(GenerationStatus.TABLES_PARTIAL, "h", run_id=None)
                ),
            ),
            patch(f"{_STORE}.reconcile_tables", new=reconcile),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            mock_gen.return_value.compute_schema_hash.return_value = "h"
            app.dependency_overrides[get_db] = lambda: _mock_db()
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 400
        assert "/generate/init" in resp.json()["detail"]
        reconcile.assert_not_awaited()
        mock_gen.return_value.generate_tables.assert_not_called()

    async def test_changed_schema_is_409(self, http_client) -> None:
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(
                f"{_STORE}.load_header",
                new=AsyncMock(return_value=_header(GenerationStatus.TABLES_PARTIAL, "stale")),
            ),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            mock_gen.return_value.compute_schema_hash.return_value = "fresh"
            app.dependency_overrides[get_db] = lambda: _mock_db()
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "regenerate (keeps your edits)" in detail
        assert "Restart from scratch discards them" in detail

    async def test_unknown_key_is_400(self, http_client) -> None:
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(
                f"{_STORE}.load_header",
                new=AsyncMock(return_value=_header(GenerationStatus.TABLES_PARTIAL, "h")),
            ),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            mock_gen.return_value.compute_schema_hash.return_value = "h"
            app.dependency_overrides[get_db] = lambda: _mock_db()
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.ghost"]})
        assert resp.status_code == 400
        assert "public.ghost" in resp.json()["detail"]

    async def test_more_than_batch_size_keys_is_422(self, http_client) -> None:
        app.dependency_overrides[get_db] = lambda: _mock_db()
        resp = await http_client.post(
            _BATCH_URL,
            json={"table_keys": [f"public.t{i}" for i in range(GENERATION_BATCH_SIZE + 1)]},
        )
        assert resp.status_code == 422

    async def test_status_moved_on_during_the_llm_call_is_409(self, http_client) -> None:
        reconcile = AsyncMock()
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(
            headers=[
                _header(GenerationStatus.TABLES_PARTIAL, "h"),  # pre-LLM guard
                _header(GenerationStatus.COMPLETE, "h"),  # post-lock re-read
            ],
            reconcile=reconcile,
        )
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, {})
            db = _mock_db(MockResult(single=_make_conn()))
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 409
        reconcile.assert_not_awaited()
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_a_batch_from_a_superseded_run_is_409(self, http_client) -> None:
        """A fresh /generate/init during the LLM call keeps the status tables_partial
        but mints a new run id; reconciling would stamp these tables with it."""
        reconcile = AsyncMock()
        advance = AsyncMock()
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(
            headers=[
                _header(GenerationStatus.TABLES_PARTIAL, "h", run_id="run-1"),
                _header(GenerationStatus.TABLES_PARTIAL, "h", run_id="run-2"),
            ],
            reconcile=reconcile,
            advance=advance,
        )
        generated = {"public.orders": TableSemantic(display_name="Orders")}
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, generated)
            db = _mock_db(MockResult(single=_make_conn()))
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 409
        assert "restarted" in resp.json()["detail"]
        reconcile.assert_not_awaited()
        advance.assert_not_awaited()
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_connection_deleted_during_the_llm_call_is_409(self, http_client) -> None:
        reconcile = AsyncMock()
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(reconcile=reconcile)
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, {"public.orders": TableSemantic(display_name="O")})
            db = _mock_db(MockResult(single=None))  # lock_and_reread finds no row
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 409
        assert "deleted" in resp.json()["detail"]
        reconcile.assert_not_awaited()
        db.rollback.assert_awaited()

    async def test_store_value_error_is_400_not_500(self, http_client) -> None:
        reconcile = AsyncMock(side_effect=ValueError("table_key 'x...' is 600 characters"))
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(reconcile=reconcile)
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, {"public.orders": TableSemantic(display_name="O")})
            db = _mock_db(MockResult(single=_make_conn()))
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 400
        assert "600 characters" in resp.json()["detail"]
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_run_superseded_at_the_write_is_409(self, http_client) -> None:
        reconcile = AsyncMock(side_effect=semantic_store.SemanticStoreConflict("superseded"))
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(reconcile=reconcile)
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, {"public.orders": TableSemantic(display_name="O")})
            db = _mock_db(MockResult(single=_make_conn()))
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 409
        assert "restarted" in resp.json()["detail"]
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_progress_value_error_is_400_not_500(self, http_client) -> None:
        advance = AsyncMock(side_effect=ValueError("malformed progress"))
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(advance=advance)
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, {"public.orders": TableSemantic(display_name="O")})
            db = _mock_db(MockResult(single=_make_conn()))
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 400
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_generator_value_error_is_422_and_logs_the_traceback(
        self, http_client, caplog
    ) -> None:
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches()
        with (
            caplog.at_level(logging.ERROR, logger="app.routers.semantic"),
            p_load as mock_load,
            p_prov,
            p_gen as mock_gen,
            p_hdr,
            p_rec,
            p_adv,
        ):
            self._wire(mock_load, mock_gen, ValueError("LLM call failed in generate_tables: boom"))
            app.dependency_overrides[get_db] = lambda: _mock_db()
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 422
        assert "boom" in resp.json()["detail"]
        errors = [r for r in caplog.records if r.name == "app.routers.semantic"]
        assert errors and errors[-1].exc_info is not None


_GLOBALS_URL = "/api/v1/connections/conn-123/semantic/generate/globals"


class TestGenerateGlobals:
    def setup_method(self) -> None:
        _reset_limiter()

    @staticmethod
    def _partial(**fields) -> SemanticModel:
        return SemanticModel.model_validate(
            {
                **_EMPTY_MODEL,
                "generation_status": "tables_partial",
                "tables": {"public.orders": {"display_name": "Orders", "columns": {}}},
                **fields,
            }
        )

    async def _run(
        self,
        http_client,
        *,
        model: SemanticModel,
        schema: DataSourceSchema,
        llm=([], [], [], [], []),
        provenance: dict | None = None,
        protected: dict | None = None,
        save: AsyncMock | None = None,
        db: MagicMock | None = None,
        extra_patches: tuple = (),
        fresh_edges: list[RelationshipEdge] | None = None,
        conn: MagicMock | None = None,
        headers: tuple | None = None,
    ):
        """POST /generate/globals with the store and LLM mocked; returns (resp, save, gen).

        ``save`` echoes the model it is given unless the test configured what it
        returns (or raises): the route answers with ``save_model``'s result.
        ``headers`` is what ``load_header`` returns before the LLM call and after the
        lock; by default the model's own status with run id ``run-1`` both times.
        """
        if headers is None:
            same = _header(model.generation_status, "h")
            headers = (same, same)
        save = save or AsyncMock()
        if save.side_effect is None and not isinstance(save.return_value, SemanticModel):
            save.side_effect = lambda _db, _cid, final, **_kw: final
        db = db or _mock_db(
            MockResult(single=_make_conn()),  # lock_and_reread_connection
            MockResult(),
            MockResult(),
            MockResult(),  # cache deletes
        )
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch(
                "app.routers.semantic._resolve_provider",
                new=AsyncMock(return_value=(MagicMock(), "model-x", "claude")),
            ),
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(f"{_STORE}.load_header", new=AsyncMock(side_effect=list(headers))),
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=model)),
            patch(
                f"{_STORE}.load_model_with_provenance",
                new=AsyncMock(return_value=(model, _snapshot(provenance, protected))),
            ),
            patch(f"{_STORE}.save_model", new=save),
            ExitStack() as stack,
        ):
            # Entered last, so an extra patch overrides a default one above.
            for extra in extra_patches:
                stack.enter_context(extra)
            mock_load.return_value = (schema, conn or _make_conn())
            gen = mock_gen.return_value
            gen._build_relationship_graph.return_value = fresh_edges or []
            if isinstance(llm, BaseException):
                gen.generate_globals = AsyncMock(side_effect=llm)
            else:
                gen.generate_globals = AsyncMock(return_value=llm)
            gen.build_time_expressions.return_value = {}
            gen.compute_schema_hash.return_value = "h"
            gen._auto_generate_notes.return_value = []
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_GLOBALS_URL)
        return resp, save, gen

    async def test_finalises_over_the_stored_tables_and_keeps_edited_metrics(
        self, http_client
    ) -> None:
        partial = self._partial(
            business_metrics=[
                {
                    "name": "Hand",
                    "metric_type": "simple",
                    "definition": "1",
                    "description": "kept",
                    "aggregation": "sum",
                }
            ]
        )
        llm_metrics = [
            SimpleMetric(
                name="Hand", definition="2", description="llm", aggregation=AggregationType.SUM
            ),
            SimpleMetric(
                name="Fresh", definition="3", description="llm", aggregation=AggregationType.SUM
            ),
        ]
        provenance = {**_no_provenance(), "metric": {"Hand": ["payload"]}}
        protected = {"table": {"public.orders": {"description": "kept"}}, "column": {}}
        db = _mock_db(MockResult(single=_make_conn()), MockResult(), MockResult(), MockResult())
        resp, save, _ = await self._run(
            http_client,
            model=partial,
            schema=_schema(["orders"]),
            llm=(llm_metrics, [], [], [], []),
            provenance=provenance,
            protected=protected,
            db=db,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["generation_status"] == "complete"
        assert body["generation_progress"] is None
        names = {m["name"]: m["description"] for m in body["business_metrics"]}
        assert names == {"Hand": "kept", "Fresh": "llm"}
        saved = save.await_args.args[2]
        assert saved.generation_status == GenerationStatus.COMPLETE
        assert save.await_args.kwargs["source"] == "generation"
        assert save.await_args.kwargs["provenance"] == provenance
        assert save.await_args.kwargs["protected"] == protected
        db.commit.assert_awaited_once()

    async def test_without_a_partial_model_is_400(self, http_client) -> None:
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch(f"{_STORE}.load_header", new=AsyncMock(return_value=None)),
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=None)),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            app.dependency_overrides[get_db] = lambda: _mock_db()
            resp = await http_client.post(_GLOBALS_URL)
        assert resp.status_code == 400

    async def test_tables_and_columns_that_left_the_schema_are_pruned(self, http_client) -> None:
        partial = self._partial(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "primary_timestamp_column": "dropped_col",
                    "columns": {
                        "id": {"display_name": "ID"},
                        "dropped_col": {"display_name": "Gone"},
                    },
                },
                "public.stale": {"display_name": "Stale", "columns": {}},
            }
        )
        resp, save, gen = await self._run(
            http_client, model=partial, schema=_schema(["orders", "unannotated"])
        )

        assert resp.status_code == 200
        saved = save.await_args.args[2]
        assert set(saved.tables) == {"public.orders"}
        assert set(saved.tables["public.orders"].columns) == {"id"}
        assert saved.tables["public.orders"].primary_timestamp_column is None
        assert set(resp.json()["tables"]) == {"public.orders"}
        # The LLM is not shown the stale table either.
        assert set(gen.generate_globals.await_args.kwargs["all_tables"]) == {"public.orders"}

    async def test_marked_edges_that_left_the_schema_are_dropped(self, http_client) -> None:
        to_unannotated = RelationshipEdge(
            from_table="public.orders",
            from_column="customer_id",
            to_table="public.customers",
            to_column="id",
            join_sql="public.orders.customer_id = public.customers.id",
        )
        to_gone_table = RelationshipEdge(
            from_table="public.orders",
            from_column="customer_id",
            to_table="public.ghosts",
            to_column="id",
            join_sql="public.orders.customer_id = public.ghosts.id",
        )
        from_gone_column = RelationshipEdge(
            from_table="public.orders",
            from_column="region_id",
            to_table="public.customers",
            to_column="id",
            join_sql="public.orders.region_id = public.customers.id",
        )
        to_gone_column = RelationshipEdge(
            from_table="public.orders",
            from_column="customer_id",
            to_table="public.customers",
            to_column="legacy_id",
            join_sql="public.orders.customer_id = public.customers.legacy_id",
        )
        edges = [to_unannotated, to_gone_table, from_gone_column, to_gone_column]
        partial = self._partial(relationships=[e.model_dump(mode="json") for e in edges])
        provenance = {
            **_no_provenance(),
            "relationship": {semantic_store.relationship_key(e): ["description"] for e in edges},
        }
        # Only public.orders is annotated; public.customers is real but unannotated.
        schema = _schema(
            ["orders", "customers"],
            columns={"orders": ["id", "customer_id"], "customers": ["id"]},
        )
        resp, save, _ = await self._run(
            http_client, model=partial, schema=schema, provenance=provenance
        )

        assert resp.status_code == 200
        saved = save.await_args.args[2]
        assert saved.relationships == [to_unannotated]

    async def test_fresh_edges_are_bounded_by_the_pruned_tables(self, http_client) -> None:
        """``render_tables`` is the pruned set: a fresh FK edge to a table that is still
        stored but left the schema must not be rebuilt into the saved model."""
        live_edge = RelationshipEdge(
            from_table="public.orders",
            from_column="customer_id",
            to_table="public.customers",
            to_column="id",
            join_sql="public.orders.customer_id = public.customers.id",
        )
        stale_edge = RelationshipEdge(
            from_table="public.orders",
            from_column="legacy_id",
            to_table="public.legacy",
            to_column="id",
            join_sql="public.orders.legacy_id = public.legacy.id",
        )
        partial = self._partial(
            tables={
                "public.orders": {"display_name": "Orders", "columns": {}},
                "public.customers": {"display_name": "Customers", "columns": {}},
                # Stored, but no longer in the live schema: prune_to_schema removes it.
                "public.legacy": {"display_name": "Legacy", "columns": {}},
            }
        )
        schema = _schema(
            ["orders", "customers"],
            columns={"orders": ["id", "customer_id", "legacy_id"], "customers": ["id"]},
        )
        resp, save, _ = await self._run(
            http_client, model=partial, schema=schema, fresh_edges=[live_edge, stale_edge]
        )

        assert resp.status_code == 200
        saved = save.await_args.args[2]
        assert set(saved.tables) == {"public.orders", "public.customers"}
        assert live_edge in saved.relationships
        assert stale_edge not in saved.relationships

    async def test_an_fk_on_an_excluded_column_is_neither_prompted_nor_saved(
        self, http_client
    ) -> None:
        live, hidden = _fk_edges()
        partial = self._partial(
            tables={
                "public.orders": {"display_name": "Orders", "columns": {}},
                "public.customers": {"display_name": "Customers", "columns": {}},
            }
        )
        resp, save, gen = await self._run(
            http_client,
            model=partial,
            schema=_fk_schema(),
            fresh_edges=[live, hidden],
            conn=_make_conn(privacy_settings={"excluded_columns": ["public.orders.ssn_ref"]}),
        )

        assert resp.status_code == 200
        assert gen.generate_globals.await_args.kwargs["relationship_edges"] == [live]
        assert save.await_args.args[2].relationships == [live]

    async def test_what_the_privacy_policy_hides_survives_globals(self, http_client) -> None:
        """The schema the route sees has excluded tables removed and excluded columns
        never introspected; that absence is the policy, not drift."""
        hidden_edge = RelationshipEdge(
            from_table="public.orders",
            from_column="customer_id",
            to_table="hr.people",
            to_column="id",
            join_sql="public.orders.customer_id = hr.people.id",
        )
        partial = self._partial(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {
                        "id": {"display_name": "ID"},
                        "customer_id": {"display_name": "Customer"},
                        "ssn": {"display_name": "SSN", "description": "hand written"},
                    },
                },
                "hr.people": {"display_name": "People", "description": "hand written"},
            },
            relationships=[hidden_edge.model_dump(mode="json")],
        )
        provenance = {
            **_no_provenance(),
            "table": {"hr.people": ["description"]},
            "column": {"public.orders.ssn": ["description"]},
            "relationship": {semantic_store.relationship_key(hidden_edge): ["join_sql"]},
        }
        conn = _make_conn(
            privacy_settings={
                "excluded_schemas": ["hr"],
                "excluded_columns": ["public.orders.ssn"],
            }
        )
        schema = _schema(["orders"], columns={"orders": ["id", "customer_id"]})
        resp, save, gen = await self._run(
            http_client, model=partial, schema=schema, provenance=provenance, conn=conn
        )

        assert resp.status_code == 200
        saved = save.await_args.args[2]
        assert set(saved.tables) == {"public.orders", "hr.people"}
        assert saved.tables["hr.people"].description == "hand written"
        assert saved.tables["public.orders"].columns["ssn"].description == "hand written"
        assert saved.relationships == [hidden_edge]
        # Kept in the store, but never shown to the LLM.
        llm_tables = gen.generate_globals.await_args.kwargs["all_tables"]
        assert set(llm_tables) == {"public.orders"}
        assert set(llm_tables["public.orders"].columns) == {"id", "customer_id"}

    async def test_a_near_miss_drop_is_warned_once_per_call(self, http_client, caplog) -> None:
        """prune_to_schema runs twice (LLM input, then the save); only one warns."""
        partial = self._partial(
            tables={
                "public.orders": {"display_name": "Orders", "columns": {}},
                "public.Orders": {"display_name": "Legacy spelling", "columns": {}},
            }
        )
        with caplog.at_level(logging.WARNING, logger="app.semantic.generator"):
            resp, save, _ = await self._run(http_client, model=partial, schema=_schema(["orders"]))
        assert resp.status_code == 200
        assert set(save.await_args.args[2].tables) == {"public.orders"}
        near_miss = [r for r in caplog.records if "near-miss" in r.getMessage()]
        assert len(near_miss) == 1

    async def test_unstorable_generated_names_are_dropped_not_fatal(
        self, http_client, caplog
    ) -> None:
        long_name = "x" * 513
        fits_once_nuls_go = "a" * 512 + "\x00"
        # Segments, derived columns and joins share a Text ``name`` column that only
        # the unique index's byte limit bounds; a metric name is VARCHAR(512).
        too_long_for_index = "x" * (semantic_store._MAX_INDEXED_TEXT_BYTES + 1)
        metrics = [
            SimpleMetric(name=long_name, definition="1", aggregation=AggregationType.SUM),
            SimpleMetric(name="ok", definition="1", aggregation=AggregationType.SUM),
            SimpleMetric(name=fits_once_nuls_go, definition="1", aggregation=AggregationType.SUM),
        ]
        segments = [
            Segment(name=too_long_for_index, sql_expression="1=1"),
            Segment(name="seg", sql_expression="1=1"),
        ]
        derived = [
            DerivedColumn(name=too_long_for_index, sql_expression="a/b"),
            DerivedColumn(name="ratio", sql_expression="a/b"),
        ]
        # A join is keyed by its pattern, which is Text bounded by the unique index.
        joins = [
            CommonJoin(
                description="too long for the index",
                tables=["public.orders"],
                join_pattern="j" * (semantic_store._MAX_INDEXED_TEXT_BYTES + 1),
            ),
            CommonJoin(description="fine", tables=["public.orders"], join_pattern="o.id = c.id"),
        ]
        with caplog.at_level(logging.WARNING, logger="app.routers.semantic"):
            resp, save, _ = await self._run(
                http_client,
                model=self._partial(),
                schema=_schema(["orders"]),
                llm=(metrics, joins, derived, [], segments),
            )

        assert resp.status_code == 200
        saved = save.await_args.args[2]
        assert [m.name for m in saved.business_metrics] == ["ok", fits_once_nuls_go]
        assert [s.name for s in saved.segments] == ["seg"]
        assert [d.name for d in saved.derived_columns] == ["ratio"]
        assert [j.join_pattern for j in saved.common_joins] == ["o.id = c.id"]
        dropped = [r for r in caplog.records if "dropped generated" in r.getMessage()]
        assert len(dropped) == 4

    async def test_store_value_error_is_400_not_500(self, http_client) -> None:
        db = _mock_db(MockResult(single=_make_conn()))
        resp, _, _ = await self._run(
            http_client,
            model=self._partial(),
            schema=_schema(["orders"]),
            save=AsyncMock(side_effect=ValueError("from_table 'x...' is 600 characters")),
            db=db,
        )
        assert resp.status_code == 400
        assert "600 characters" in resp.json()["detail"]
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_generator_value_error_is_422_and_logs_the_traceback(
        self, http_client, caplog
    ) -> None:
        save = AsyncMock()
        with caplog.at_level(logging.ERROR, logger="app.routers.semantic"):
            resp, _, _ = await self._run(
                http_client,
                model=self._partial(),
                schema=_schema(["orders"]),
                llm=ValueError("LLM call failed: boom"),
                save=save,
            )
        assert resp.status_code == 422
        errors = [r for r in caplog.records if r.name == "app.routers.semantic"]
        assert errors and errors[-1].exc_info is not None
        save.assert_not_awaited()

    async def test_provenance_is_read_after_the_row_lock(self, http_client) -> None:
        calls: list[str] = []

        async def lock(*_a, **_k):
            calls.append("lock")
            return _make_conn()

        async def provenance(*_a, **_k):
            calls.append("provenance")
            return self._partial(), _snapshot()

        resp, _, _ = await self._run(
            http_client,
            model=self._partial(),
            schema=_schema(["orders"]),
            db=_mock_db(MockResult(), MockResult(), MockResult()),  # cache deletes only
            extra_patches=(
                patch("app.routers.semantic.lock_and_reread_connection", new=lock),
                patch(f"{_STORE}.load_model_with_provenance", new=provenance),
            ),
        )
        assert resp.status_code == 200
        assert calls == ["lock", "provenance"]

    async def test_connection_deleted_during_the_llm_call_is_409(self, http_client) -> None:
        """A vanished connection under the lock is 409 (state moved on), nothing saved."""
        save = AsyncMock()
        db = _mock_db()
        resp, _, _ = await self._run(
            http_client,
            model=self._partial(),
            schema=_schema(["orders"]),
            save=save,
            db=db,
            extra_patches=(
                patch(
                    "app.routers.semantic.lock_and_reread_connection",
                    new=AsyncMock(side_effect=HTTPException(status_code=404, detail="gone")),
                ),
            ),
        )
        assert resp.status_code == 409
        assert "deleted" in resp.json()["detail"]
        save.assert_not_awaited()
        db.rollback.assert_awaited()

    async def test_a_run_restarted_during_the_llm_call_is_409(self, http_client) -> None:
        """A fresh /generate/init during the globals call mints a new run id; finalising
        would mark that new run complete before its batches ran."""
        save = AsyncMock()
        db = _mock_db(MockResult(single=_make_conn()))
        resp, _, _ = await self._run(
            http_client,
            model=self._partial(),
            schema=_schema(["orders"]),
            save=save,
            db=db,
            headers=(
                _header(GenerationStatus.TABLES_PARTIAL, "h", run_id="run-1"),
                _header(GenerationStatus.TABLES_PARTIAL, "h", run_id="run-2"),
            ),
        )
        assert resp.status_code == 409
        assert "restarted" in resp.json()["detail"]
        save.assert_not_awaited()
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_a_run_started_over_a_complete_model_during_the_llm_call_is_409(
        self, http_client
    ) -> None:
        save = AsyncMock()
        complete = self._partial(generation_status="complete")
        resp, _, _ = await self._run(
            http_client,
            model=complete,
            schema=_schema(["orders"]),
            save=save,
            db=_mock_db(MockResult(single=_make_conn())),
            headers=(
                _header(GenerationStatus.COMPLETE, "h", run_id=None),
                _header(GenerationStatus.TABLES_PARTIAL, "h", run_id="run-2"),
            ),
        )
        assert resp.status_code == 409
        save.assert_not_awaited()

    async def test_re_finalising_a_complete_model_is_not_a_restart(self, http_client) -> None:
        """A complete model has no run id before or after the LLM call."""
        complete = self._partial(generation_status="complete")
        none_run = _header(GenerationStatus.COMPLETE, "h", run_id=None)
        resp, save, _ = await self._run(
            http_client,
            model=complete,
            schema=_schema(["orders"]),
            headers=(none_run, none_run),
        )
        assert resp.status_code == 200
        save.assert_awaited_once()

    async def test_returns_the_model_as_stored(self, http_client) -> None:
        """The response is what ``save_model`` stored and returned, not ``final``."""
        stored = self._partial(notes=["as stored"])
        save = AsyncMock(return_value=stored)
        load = AsyncMock(return_value=self._partial())
        resp, _, _ = await self._run(
            http_client,
            model=self._partial(),
            schema=_schema(["orders"]),
            save=save,
            extra_patches=(patch(f"{_STORE}.load_model", new=load),),
        )
        assert resp.status_code == 200
        assert resp.json()["notes"] == ["as stored"]
        save.assert_awaited_once()
        load.assert_awaited_once()  # the pre-LLM read only; nothing re-read after the save

    async def test_a_provider_throttle_in_globals_is_429(self, http_client) -> None:
        resp, save, _ = await self._run(
            http_client,
            model=self._partial(),
            schema=_schema(["orders"]),
            llm=ValueError("LLM call failed in generate_globals: [RATE_LIMIT] slow down"),
        )
        assert resp.status_code == 429
        save.assert_not_awaited()

    async def test_tables_without_an_annotation_are_named_in_a_warning(self, http_client) -> None:
        resp, _, _ = await self._run(
            http_client,
            model=self._partial(),  # only public.orders is annotated
            schema=_schema(["orders", "customers", "invoices"]),
        )
        assert resp.status_code == 200
        [warning] = resp.json()["generation_warnings"]
        assert "2 table(s) have no annotation" in warning
        assert "public.customers" in warning and "public.invoices" in warning
        assert warning.endswith("Regenerate to annotate them.")

    async def test_a_table_a_batch_stored_during_the_llm_call_is_finalised(
        self, http_client
    ) -> None:
        """The post-lock read, not the pre-LLM one, is what is finalised: a table a
        concurrent batch stored meanwhile is saved and is not called unannotated."""
        pre_lock = self._partial()  # public.orders only
        under_lock = self._partial(
            tables={
                "public.orders": {"display_name": "Orders", "columns": {}},
                "public.customers": {"display_name": "Customers", "columns": {}},
            }
        )
        resp, save, gen = await self._run(
            http_client,
            model=pre_lock,
            schema=_schema(["orders", "customers", "invoices"]),
            extra_patches=(
                patch(
                    f"{_STORE}.load_model_with_provenance",
                    new=AsyncMock(return_value=(under_lock, _snapshot())),
                ),
            ),
        )
        assert resp.status_code == 200
        assert set(gen.generate_globals.await_args.kwargs["all_tables"]) == {"public.orders"}
        assert set(save.await_args.args[2].tables) == {"public.orders", "public.customers"}
        [warning] = resp.json()["generation_warnings"]
        assert "1 table(s) have no annotation (public.invoices)" in warning
        assert "public.customers" not in warning

    async def test_a_model_deleted_during_the_llm_call_is_409(self, http_client) -> None:
        save = AsyncMock()
        db = _mock_db(MockResult(single=_make_conn()))
        resp, _, _ = await self._run(
            http_client,
            model=self._partial(),
            schema=_schema(["orders"]),
            save=save,
            db=db,
            extra_patches=(
                patch(
                    f"{_STORE}.load_model_with_provenance",
                    new=AsyncMock(return_value=(None, semantic_store._empty_snapshot())),
                ),
            ),
        )
        assert resp.status_code == 409
        assert "deleted" in resp.json()["detail"]
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()
        save.assert_not_awaited()

    async def test_a_hand_edit_naming_a_dropped_column_is_voided_before_the_save(
        self, http_client
    ) -> None:
        partial = self._partial(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "primary_timestamp_column": "created_ts",
                    "columns": {"id": {"display_name": "Id"}},
                }
            }
        )
        provenance = {
            **_no_provenance(),
            "table": {"public.orders": ["primary_timestamp_column", "description"]},
        }
        protected = {
            "table": {
                "public.orders": {"primary_timestamp_column": "created_ts", "description": "hand"}
            },
            "column": {},
        }
        resp, save, _ = await self._run(
            http_client,
            model=partial,
            schema=_schema(["orders"]),  # orders has only ``id`` now
            provenance=provenance,
            protected=protected,
        )
        assert resp.status_code == 200
        kwargs = save.await_args.kwargs
        assert kwargs["provenance"]["table"] == {"public.orders": ["description"]}
        assert kwargs["protected"]["table"] == {"public.orders": {"description": "hand"}}
        assert save.await_args.args[2].tables["public.orders"].primary_timestamp_column is None
        [note] = resp.json()["generation_warnings"]
        assert "public.orders.primary_timestamp_column" in note


# ── POST /api/v1/connections/{id}/semantic/apply-drift ───────────────────────────

_APPLY_DRIFT_URL = "/api/v1/connections/conn-123/semantic/apply-drift"


class TestApplyDrift:
    """Structural drift is applied without an LLM call; new tables get one pass. The
    response carries the stored model and a by-name summary of the change."""

    def setup_method(self) -> None:
        _reset_limiter()

    @staticmethod
    def _complete(**fields) -> SemanticModel:
        return SemanticModel.model_validate(
            {
                **_EMPTY_MODEL,
                "generation_status": "complete",
                "is_user_reviewed": True,
                "tables": {
                    "public.orders": {
                        "display_name": "Orders",
                        "columns": {"id": {"display_name": "Id"}},
                    },
                    "public.coupons": {
                        "display_name": "Coupons",
                        "columns": {"id": {"display_name": "Id"}},
                    },
                },
                **fields,
            }
        )

    @staticmethod
    def _db(conn=None) -> MagicMock:
        conn = conn or _make_conn()
        return _mock_db(
            MockResult(single=conn),  # get_connection_or_404
            MockResult(single=conn),  # lock_and_reread_connection (_lock_generation)
            # _invalidate_connection_caches: the query and embedding cache deletes (the
            # schema cache is kept; the introspection's own cache writes are mocked away).
            MockResult(),
            MockResult(),
        )

    async def _run(
        self,
        http_client,
        *,
        model: SemanticModel,
        schema: DataSourceSchema | BaseException,
        header_status: GenerationStatus = GenerationStatus.COMPLETE,
        post_lock_status: GenerationStatus | None = None,
        generated: dict | BaseException | Callable | None = None,
        llm=([], [], [], [], []),
        provenance: dict | None = None,
        protected: dict | None = None,
        current: tuple | None = None,
        db: MagicMock | None = None,
        conn: MagicMock | None = None,
        fresh_edges: list[RelationshipEdge] | None = None,
        save: AsyncMock | None = None,
        dry_run: bool = False,
        on_record: Callable | None = None,
        last_apply: object = None,
    ):
        """POST /apply-drift with the introspection, store and LLM mocked.

        Returns ``(resp, save, gen, resolve)``. ``save`` echoes the model it is
        given. ``schema`` is what the live introspection returns, or the error it
        raises. ``header_status`` is the pre-check header; ``post_lock_status``
        the one re-read under the lock (defaults to the same).
        ``load_model_with_provenance`` answers the pre-check read with ``model`` and
        the given provenance; ``current`` replaces what it returns for the re-read
        under the lock (default: the same). ``save`` replaces the ``save_model`` mock.
        ``dry_run`` posts the preview (``?dry_run=true``). The ``record_drift_apply``,
        ``load_model_with_provenance``, ``load_model`` and introspection mocks are
        left on ``self.record``, ``self.loader``, ``self.load_model`` and
        ``self.refresh``; ``on_record`` is the ``record_drift_apply`` mock's side effect.
        ``last_apply`` is the summary of the newest logged apply (default: none logged).
        """
        conn = conn or _make_conn()
        db = db or self._db(conn)
        save = save or AsyncMock(side_effect=lambda _db, _cid, final, **_kw: final)
        headers = [
            _header(header_status, "h", run_id=None),
            _header(post_lock_status or header_status, "h", run_id=None),
        ]
        refresh = (
            AsyncMock(side_effect=schema)
            if isinstance(schema, BaseException)
            else AsyncMock(return_value=schema)
        )
        resolve = AsyncMock(return_value=(MagicMock(), "model-x", "claude"))
        pre = (model, _snapshot(provenance, protected))
        locked = current if current is not None else pre
        loader = AsyncMock(side_effect=[pre, locked])
        load_model = AsyncMock(return_value=model)
        record = AsyncMock(side_effect=on_record)
        self.record, self.loader, self.load_model = record, loader, load_model
        self.refresh = refresh
        lister = AsyncMock(
            return_value=[]
            if last_apply is None
            else [
                semantic_store.DriftApplyRecord(
                    applied_at=datetime(2026, 10, 16, tzinfo=UTC),
                    applied_by="u1",
                    summary=last_apply,  # type: ignore[arg-type]
                )
            ]
        )
        self.lister = lister
        with (
            patch("app.routers.semantic.refresh_user_schema_cache", new=refresh),
            patch("app.routers.semantic._resolve_provider", new=resolve),
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(f"{_STORE}.load_header", new=AsyncMock(side_effect=headers)),
            patch(f"{_STORE}.load_model", new=load_model),
            patch(f"{_STORE}.load_model_with_provenance", new=loader),
            patch(f"{_STORE}.save_model", new=save),
            patch(f"{_STORE}.record_drift_apply", new=record),
            patch(f"{_STORE}.list_drift_applies", new=lister),
        ):
            gen = mock_gen.return_value
            gen._build_relationship_graph.return_value = fresh_edges or []
            if isinstance(generated, BaseException) or callable(generated):
                gen.generate_tables = AsyncMock(side_effect=generated)
            else:
                gen.generate_tables = AsyncMock(return_value=generated or {})
            gen.generate_globals = AsyncMock(return_value=llm)
            gen.build_time_expressions.return_value = {}
            gen.compute_schema_hash.return_value = "h"
            gen._auto_generate_notes.return_value = []
            # The real check: the apply re-runs it over the metrics it actually added.
            gen._validate_metrics_time_columns.side_effect = (
                _RealGenerator()._validate_metrics_time_columns
            )
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_APPLY_DRIFT_URL + ("?dry_run=true" if dry_run else ""))
        return resp, save, gen, resolve

    async def test_without_a_model_is_404_and_never_introspects(self, http_client) -> None:
        refresh = AsyncMock()
        with (
            patch(
                f"{_STORE}.load_model_with_provenance",
                new=AsyncMock(return_value=(None, _snapshot())),
            ),
            patch("app.routers.semantic.refresh_user_schema_cache", new=refresh),
        ):
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(_APPLY_DRIFT_URL)
        assert resp.status_code == 404
        refresh.assert_not_awaited()

    async def test_an_interrupted_run_is_409(self, http_client) -> None:
        """Pruning under a run would race its batches; finish or restart it first."""
        resp, save, gen, _ = await self._run(
            http_client,
            model=self._complete(generation_status="tables_partial"),
            schema=_schema(["orders"]),
            header_status=GenerationStatus.TABLES_PARTIAL,
        )
        assert resp.status_code == 409
        assert "finish" in resp.json()["detail"].lower()
        save.assert_not_awaited()
        gen.generate_tables.assert_not_awaited()
        # Refused before the data source is touched.
        self.refresh.assert_not_awaited()

    async def test_an_unreachable_source_is_400_and_nothing_is_written(self, http_client) -> None:
        """Deleting from a cached schema could delete what still exists: live only."""
        from app.services.schema_utils import SchemaRefreshError

        db = self._db()
        resp, save, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=SchemaRefreshError("connection refused"),
            db=db,
        )
        assert resp.status_code == 400
        assert "connection refused" in resp.json()["detail"]
        save.assert_not_awaited()
        db.commit.assert_not_awaited()

    async def test_no_drift_returns_the_model_unchanged_and_says_so(self, http_client) -> None:
        db = self._db()
        resp, save, gen, resolve = await self._run(
            http_client, model=self._complete(), schema=_schema(["orders", "coupons"]), db=db
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body["semantic_model"]["tables"]) == {"public.orders", "public.coupons"}
        assert body["summary"]["changed"] is False
        save.assert_not_awaited()
        gen.generate_tables.assert_not_awaited()
        resolve.assert_not_awaited()
        # The refreshed schema cache is still persisted.
        db.commit.assert_awaited_once()

    async def test_a_removed_table_leaves_with_everything_over_it(self, http_client) -> None:
        """No LLM call: removal is structural. Metrics, joins, derived columns, segments
        and relationships that name the gone table go with it; the rest stays. The
        summary names each one."""
        model = self._complete(
            business_metrics=[
                {
                    "name": "Coupon uses",
                    "metric_type": "simple",
                    "definition": "COUNT(*)",
                    "description": "over coupons",
                    "aggregation": "count",
                    "related_tables": ["public.coupons"],
                },
                {
                    "name": "Orders",
                    "metric_type": "simple",
                    "definition": "COUNT(*)",
                    "description": "over orders",
                    "aggregation": "count",
                    "related_tables": ["public.orders"],
                },
            ],
            common_joins=[
                {
                    "tables": ["public.orders", "public.coupons"],
                    "join_pattern": "orders.coupon_id = coupons.id",
                    "description": "x",
                }
            ],
            derived_columns=[
                {
                    "name": "coupon_rate",
                    "sql_expression": "1",
                    "description": "x",
                    "base_tables": ["public.coupons"],
                }
            ],
            segments=[
                {
                    "name": "discounted",
                    "description": "x",
                    "sql_expression": "coupon_id IS NOT NULL",
                    "applicable_tables": ["public.coupons"],
                },
                {
                    "name": "all",
                    "description": "x",
                    "sql_expression": "1=1",
                    "applicable_tables": [],
                },
            ],
            relationships=[
                {
                    "from_table": "public.orders",
                    "from_column": "coupon_id",
                    "to_table": "public.coupons",
                    "to_column": "id",
                    "join_sql": "public.orders.coupon_id = public.coupons.id",
                }
            ],
        )
        db = self._db()
        resp, save, gen, resolve = await self._run(
            http_client, model=model, schema=_schema(["orders"]), db=db
        )
        assert resp.status_code == 200
        body = resp.json()["semantic_model"]
        assert set(body["tables"]) == {"public.orders"}
        assert [m["name"] for m in body["business_metrics"]] == ["Orders"]
        assert body["common_joins"] == []
        assert body["derived_columns"] == []
        assert [s["name"] for s in body["segments"]] == ["all"]
        assert body["relationships"] == []
        summary = resp.json()["summary"]
        assert summary["changed"] is True
        assert summary["tables_removed"] == ["public.coupons"]
        assert summary["metrics_removed"] == ["Coupon uses"]
        assert summary["joins_removed"] == ["orders.coupon_id = coupons.id"]
        assert summary["derived_columns_removed"] == ["coupon_rate"]
        assert summary["segments_removed"] == ["discounted"]
        assert summary["relationships_removed"] == ["public.orders.coupon_id → public.coupons.id"]
        assert summary["tables_added"] == []
        # Structural only: no provider, no LLM.
        resolve.assert_not_awaited()
        gen.generate_tables.assert_not_awaited()
        gen.generate_globals.assert_not_awaited()
        # Review state is untouched when nothing was generated.
        assert body["is_user_reviewed"] is True
        assert body["generation_status"] == "complete"
        assert save.await_args.kwargs["source"] == "drift"
        # The schema cache, then the model.
        assert db.commit.await_count == 2

    async def test_a_removed_column_and_its_references_are_cleared(self, http_client) -> None:
        model = self._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "primary_timestamp_column": "dropped_at",
                    "columns": {
                        "id": {"display_name": "Id"},
                        "dropped_at": {"display_name": "Dropped"},
                    },
                }
            }
        )
        resp, _, _, _ = await self._run(
            http_client, model=model, schema=_schema(["orders"], {"orders": ["id"]})
        )
        assert resp.status_code == 200
        table = resp.json()["semantic_model"]["tables"]["public.orders"]
        assert set(table["columns"]) == {"id"}
        assert table["primary_timestamp_column"] is None
        assert resp.json()["summary"]["columns_removed"] == {"public.orders": ["dropped_at"]}

    async def test_a_hand_set_reference_to_a_dropped_column_is_not_written_back(
        self, http_client
    ) -> None:
        """A marked ``primary_timestamp_column`` on a gone column would otherwise come
        back as a protected value over the pruned row. ``drop_dangling_edits`` trims it,
        and the note reaches both the model and the summary."""
        model = self._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "primary_timestamp_column": "dropped_at",
                    "columns": {
                        "id": {"display_name": "Id"},
                        "dropped_at": {"display_name": "Dropped"},
                    },
                }
            }
        )
        provenance = {
            **_no_provenance(),
            "table": {"public.orders": ["primary_timestamp_column"]},
        }
        protected = {
            "table": {"public.orders": {"primary_timestamp_column": "dropped_at"}},
            "column": {},
        }
        resp, save, _, _ = await self._run(
            http_client,
            model=model,
            schema=_schema(["orders"], {"orders": ["id"]}),
            provenance=provenance,
            protected=protected,
        )
        assert resp.status_code == 200
        assert save.await_args.kwargs["provenance"]["table"].get("public.orders", []) == []
        assert save.await_args.kwargs["protected"]["table"].get("public.orders", {}) == {}
        assert any("dropped_at" in w for w in resp.json()["semantic_model"]["generation_warnings"])
        assert any("dropped_at" in w for w in resp.json()["summary"]["warnings"])

    async def test_what_the_policy_hides_is_not_removed(self, http_client) -> None:
        conn = _make_conn(privacy_settings={"excluded_tables": ["public.coupons"]})
        resp, save, _, _ = await self._run(
            http_client, model=self._complete(), schema=_schema(["orders"]), conn=conn
        )
        assert resp.status_code == 200
        # Hidden, not gone: no drift, nothing written.
        assert set(resp.json()["semantic_model"]["tables"]) == {"public.orders", "public.coupons"}
        assert resp.json()["summary"]["changed"] is False
        save.assert_not_awaited()

    async def test_a_table_the_role_cannot_read_is_kept(self, http_client) -> None:
        schema = _schema(["orders"])
        schema.unreadable_tables = ["public.coupons"]
        resp, save, _, _ = await self._run(http_client, model=self._complete(), schema=schema)
        assert resp.status_code == 200
        assert set(resp.json()["semantic_model"]["tables"]) == {"public.orders", "public.coupons"}
        save.assert_not_awaited()

    async def test_a_table_the_role_cannot_read_survives_other_drift(self, http_client) -> None:
        """With something else to apply, the prune runs: the unreadable table is kept
        (``keep_unreadable``), not removed."""
        model = self._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"id": {"display_name": "Id"}, "legacy": {"display_name": "L"}},
                },
                "public.coupons": {"display_name": "Coupons", "columns": {}},
            }
        )
        schema = _schema(["orders"])
        schema.unreadable_tables = ["public.coupons"]
        resp, save, _, _ = await self._run(http_client, model=model, schema=schema)
        assert resp.status_code == 200
        save.assert_awaited_once()
        assert set(resp.json()["semantic_model"]["tables"]) == {"public.orders", "public.coupons"}
        summary = resp.json()["summary"]
        assert summary["tables_removed"] == []
        assert summary["columns_removed"] == {"public.orders": ["legacy"]}

    async def test_new_tables_are_annotated_and_globals_merged_additively(
        self, http_client
    ) -> None:
        """Only the new tables go to the LLM; one globals pass follows. An existing
        metric keeps its payload against a same-named LLM one; new names are added."""
        model = self._complete(
            business_metrics=[
                {
                    "name": "Orders",
                    "metric_type": "simple",
                    "definition": "COUNT(*)",
                    "description": "mine",
                    "aggregation": "count",
                    "related_tables": ["public.orders"],
                }
            ],
        )
        llm_metrics = [
            SimpleMetric(
                name="Orders", definition="1", description="llm", aggregation=AggregationType.SUM
            ),
            SimpleMetric(
                name="Events",
                definition="2",
                description="llm",
                aggregation=AggregationType.SUM,
                related_tables=["public.events"],
            ),
        ]
        resp, save, gen, resolve = await self._run(
            http_client,
            model=model,
            schema=_schema(["orders", "coupons", "events"]),
            generated={"public.events": TableSemantic(display_name="Events")},
            llm=(llm_metrics, [], [], ["llm said so"], []),
        )
        assert resp.status_code == 200
        body = resp.json()["semantic_model"]
        assert set(body["tables"]) == {"public.orders", "public.coupons", "public.events"}
        assert gen.generate_tables.await_args.kwargs["table_keys"] == ["public.events"]
        gen.generate_globals.assert_awaited_once()
        assert set(gen.generate_globals.await_args.kwargs["all_tables"]) == {
            "public.orders",
            "public.coupons",
            "public.events",
        }
        names = {m["name"]: m["description"] for m in body["business_metrics"]}
        assert names == {"Orders": "mine", "Events": "llm"}
        # The globals pass's own warnings are not carried: they may name LLM metrics
        # the merge discarded. The time-column check re-runs over the added ones.
        assert body["generation_warnings"] == []
        # New LLM content: review state is reset, like a generation run.
        assert body["is_user_reviewed"] is False
        assert body["generation_model"] == "model-x"
        summary = resp.json()["summary"]
        assert summary["tables_added"] == ["public.events"]
        assert summary["metrics_added"] == ["Events"]
        assert summary["metrics_removed"] == []
        assert summary["warnings"] == []
        resolve.assert_awaited_once()
        save.assert_awaited_once()

    async def test_new_tables_are_sent_in_batches_and_skipped_ones_reported(
        self, http_client
    ) -> None:
        names = [f"t{i}" for i in range(GENERATION_BATCH_SIZE + 1)]
        resp, _, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", *names]),
            generated={},
        )
        assert resp.status_code == 200
        assert gen.generate_tables.await_count == 2
        sent = [
            k for call in gen.generate_tables.await_args_list for k in call.kwargs["table_keys"]
        ]
        assert sorted(sent) == sorted(f"public.{n}" for n in names)
        # The LLM returned nothing for any of them: still missing, and said so.
        assert resp.json()["summary"]["tables_not_annotated"] == sorted(
            f"public.{n}" for n in names
        )
        # Nothing landed in the model: not a change, whatever was attempted.
        assert resp.json()["summary"]["changed"] is False

    async def test_an_llm_failure_is_mapped_and_nothing_is_written(self, http_client) -> None:
        db = self._db()
        resp, save, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", "events"]),
            generated=ValueError("LLM call failed: boom"),
            db=db,
        )
        assert resp.status_code == 422
        assert "boom" in resp.json()["detail"]
        save.assert_not_awaited()
        # Only the refreshed schema cache, committed before the first LLM call so it
        # is neither held across the calls nor lost to their failure.
        db.commit.assert_awaited_once()

    async def test_a_run_started_meanwhile_discards_the_apply(self, http_client) -> None:
        """A /generate/init that landed while the LLM ran owns the rows now."""
        db = self._db()
        resp, save, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", "events"]),
            generated={"public.events": TableSemantic(display_name="Events")},
            post_lock_status=GenerationStatus.TABLES_PARTIAL,
            db=db,
        )
        assert resp.status_code == 409
        save.assert_not_awaited()
        db.rollback.assert_awaited()
        db.commit.assert_awaited_once()  # the schema cache, before the LLM calls

    async def test_a_model_deleted_meanwhile_is_409(self, http_client) -> None:
        db = self._db()
        resp, save, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders"]),
            current=(None, _snapshot()),
            db=db,
        )
        assert resp.status_code == 409
        save.assert_not_awaited()

    async def test_the_schema_cache_is_committed_before_the_llm_runs(self, http_client) -> None:
        """The cache refresh holds row locks (upsert + cache deletes); it must not stay
        open through the LLM calls, nor be lost when one fails."""
        db = self._db()
        order: list[str] = []
        db.commit.side_effect = lambda: order.append("commit")

        def annotate(**_kw):
            order.append("llm")
            return {"public.events": TableSemantic(display_name="Events")}

        resp, _, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", "events"]),
            generated=annotate,
            db=db,
        )
        assert resp.status_code == 200
        assert order == ["commit", "llm", "commit"]

    async def test_no_globals_pass_when_the_llm_annotated_no_new_table(self, http_client) -> None:
        """Globals over tables the LLM returned nothing for would be a wasted call
        whose warnings would pollute the model."""
        resp, _, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", "events"]),
            generated={},
            llm=([], [], [], ["stray warning"], []),
        )
        assert resp.status_code == 200
        gen.generate_tables.assert_awaited_once()
        gen.generate_globals.assert_not_awaited()
        assert resp.json()["semantic_model"]["generation_warnings"] == []
        assert resp.json()["summary"]["tables_not_annotated"] == ["public.events"]

    async def test_a_table_annotated_by_a_run_meanwhile_is_not_reported_missing(
        self, http_client
    ) -> None:
        """The apply's LLM returned nothing for ``events``, but a run finished while it
        ran, so the re-read under the lock already holds the table: it is not missing."""
        with_events = self._complete(
            tables={
                "public.orders": {"display_name": "Orders", "columns": {}},
                "public.coupons": {"display_name": "Coupons", "columns": {}},
                "public.events": {"display_name": "Events", "columns": {}},
            }
        )
        resp, _, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", "events"], {"orders": [], "coupons": []}),
            generated={},
            current=(with_events, _snapshot()),
        )
        assert resp.status_code == 200
        assert resp.json()["summary"]["tables_not_annotated"] == []
        assert "public.events" in resp.json()["semantic_model"]["tables"]

    @staticmethod
    def _edge(from_table: str, from_column: str, to_table: str) -> RelationshipEdge:
        return RelationshipEdge(
            from_table=from_table,
            from_column=from_column,
            to_table=to_table,
            to_column="id",
            join_sql=f"{from_table}.{from_column} = {to_table}.id",
        )

    @classmethod
    def _payroll_model(cls) -> SemanticModel:
        return cls._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {
                        "id": {"display_name": "Id"},
                        "payroll_id": {"display_name": "Payroll"},
                        "legacy": {"display_name": "Legacy"},
                    },
                },
                "public.payroll": {"display_name": "Payroll", "columns": {}},
            },
            relationships=[cls._edge("public.orders", "payroll_id", "public.payroll")],
        )

    async def test_an_edge_to_a_policy_hidden_table_survives_other_drift(self, http_client) -> None:
        """An unmarked FK edge to a table the policy hides is not regenerated while it
        is hidden, so it must be carried over — not reported as removed."""
        conn = _make_conn(privacy_settings={"excluded_tables": ["public.payroll"]})
        resp, _, _, _ = await self._run(
            http_client,
            model=self._payroll_model(),
            schema=_schema(["orders"], {"orders": ["id", "payroll_id"]}),
            conn=conn,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert [r["to_table"] for r in body["semantic_model"]["relationships"]] == [
            "public.payroll"
        ]
        assert body["summary"]["columns_removed"] == {"public.orders": ["legacy"]}
        assert body["summary"]["relationships_removed"] == []

    async def test_an_edge_to_an_unreadable_table_survives_other_drift(self, http_client) -> None:
        schema = _schema(["orders"], {"orders": ["id", "payroll_id"]})
        schema.unreadable_tables = ["public.payroll"]
        resp, _, _, _ = await self._run(http_client, model=self._payroll_model(), schema=schema)
        assert resp.status_code == 200
        body = resp.json()
        assert [r["to_table"] for r in body["semantic_model"]["relationships"]] == [
            "public.payroll"
        ]
        assert body["summary"]["relationships_removed"] == []

    async def test_live_catalog_edges_are_kept_and_new_ones_added(self, http_client) -> None:
        """Relationships are rebuilt from the catalog: an unchanged edge neither leaves
        nor arrives, the edge over a removed table leaves, and a new table's FK arrives."""
        model = self._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"id": {"display_name": "I"}},
                },
                "public.customers": {"display_name": "C", "columns": {"id": {"display_name": "I"}}},
                "public.coupons": {"display_name": "Coupons", "columns": {}},
            },
            relationships=[
                self._edge("public.orders", "customer_id", "public.customers"),
                self._edge("public.orders", "coupon_id", "public.coupons"),
            ],
        )
        schema = _schema(
            ["orders", "customers", "events"],
            {"orders": ["id", "customer_id", "event_id"], "customers": ["id"], "events": ["id"]},
        )
        resp, _, _, _ = await self._run(
            http_client,
            model=model,
            schema=schema,
            generated={"public.events": TableSemantic(display_name="Events")},
            fresh_edges=[
                self._edge("public.orders", "customer_id", "public.customers"),
                self._edge("public.orders", "event_id", "public.events"),
            ],
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["relationships_removed"] == ["public.orders.coupon_id → public.coupons.id"]
        assert summary["relationships_added"] == ["public.orders.event_id → public.events.id"]
        kept = {
            (r["from_column"], r["to_table"])
            for r in resp.json()["semantic_model"]["relationships"]
        }
        assert kept == {("customer_id", "public.customers"), ("event_id", "public.events")}

    async def test_a_save_the_store_rejects_is_400_and_only_the_cache_was_committed(
        self, http_client
    ) -> None:
        db = self._db()
        save = AsyncMock(side_effect=ValueError("table key too long"))
        resp, save, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders"]),
            save=save,
            db=db,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["detail"]
        save.assert_awaited_once()
        db.rollback.assert_awaited()
        db.commit.assert_awaited_once()  # the schema cache

    async def test_a_segment_over_several_tables_loses_only_the_removed_ones(
        self, http_client
    ) -> None:
        """Removed when every table it applies to is gone; otherwise the gone tables
        are dropped from its list so it never names a table that no longer exists."""
        model = self._complete(
            segments=[
                {
                    "name": "mixed",
                    "description": "x",
                    "sql_expression": "1=1",
                    "applicable_tables": ["public.orders", "public.coupons"],
                },
                {
                    "name": "gone",
                    "description": "x",
                    "sql_expression": "1=1",
                    "applicable_tables": ["public.coupons"],
                },
            ]
        )
        resp, _, _, _ = await self._run(http_client, model=model, schema=_schema(["orders"]))
        assert resp.status_code == 200
        segments = resp.json()["semantic_model"]["segments"]
        assert [(s["name"], s["applicable_tables"]) for s in segments] == [
            ("mixed", ["public.orders"])
        ]
        assert resp.json()["summary"]["segments_removed"] == ["gone"]

    async def test_a_hand_edited_metric_over_a_removed_table_is_removed(self, http_client) -> None:
        """The metric leaves even though a user edited it, and its provenance mark
        leaves with it: ``save_model`` stamps marks by name, so a mark left behind
        would land on any same-named item the LLM adds."""
        model = self._complete(
            business_metrics=[
                {
                    "name": "Coupon uses",
                    "metric_type": "simple",
                    "definition": "COUNT(*)",
                    "description": "hand written",
                    "aggregation": "count",
                    "related_tables": ["public.coupons"],
                }
            ]
        )
        provenance = {**_no_provenance(), "metric": {"Coupon uses": ["definition"]}}
        resp, save, _, _ = await self._run(
            http_client, model=model, schema=_schema(["orders"]), provenance=provenance
        )
        assert resp.status_code == 200
        assert resp.json()["semantic_model"]["business_metrics"] == []
        assert resp.json()["summary"]["metrics_removed"] == ["Coupon uses"]
        assert save.await_args.kwargs["provenance"]["metric"] == {}

    async def test_a_dry_run_names_what_would_change_and_writes_nothing(self, http_client) -> None:
        model = self._complete(
            business_metrics=[
                {
                    "name": "Coupon uses",
                    "metric_type": "simple",
                    "definition": "COUNT(*)",
                    "description": "x",
                    "aggregation": "count",
                    "related_tables": ["public.coupons"],
                }
            ],
        )
        db = self._db()
        resp, save, gen, resolve = await self._run(
            http_client, model=model, schema=_schema(["orders"]), db=db, dry_run=True
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["dry_run"] is True
        assert summary["changed"] is True
        assert summary["tables_removed"] == ["public.coupons"]
        assert summary["metrics_removed"] == ["Coupon uses"]
        # The model is returned as stored: nothing was applied.
        assert set(resp.json()["semantic_model"]["tables"]) == {"public.orders", "public.coupons"}
        save.assert_not_awaited()
        resolve.assert_not_awaited()
        gen.generate_tables.assert_not_awaited()
        db.rollback.assert_not_awaited()
        # Only the refreshed schema cache is committed (right after the diff); the
        # preview itself commits nothing.
        db.commit.assert_awaited_once()

    async def test_a_dry_run_lists_new_tables_without_calling_the_llm(self, http_client) -> None:
        resp, save, gen, resolve = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", "events"]),
            dry_run=True,
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["tables_added"] == ["public.events"]
        assert summary["dry_run"] is True
        # A new table alone is a change: the apply would annotate it.
        assert summary["changed"] is True
        gen.generate_tables.assert_not_awaited()
        gen.generate_globals.assert_not_awaited()
        resolve.assert_not_awaited()
        save.assert_not_awaited()

    async def test_a_dry_run_with_nothing_to_apply_says_so(self, http_client) -> None:
        resp, save, _, _ = await self._run(
            http_client, model=self._complete(), schema=_schema(["orders", "coupons"]), dry_run=True
        )
        assert resp.status_code == 200
        assert resp.json()["summary"]["changed"] is False
        assert resp.json()["summary"]["dry_run"] is True
        save.assert_not_awaited()

    async def test_a_dry_run_names_the_hand_edits_the_apply_would_trim(self, http_client) -> None:
        model = self._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "primary_timestamp_column": "dropped_at",
                    # Stored but gone from the schema: the drift the preview acts on.
                    "columns": {
                        "id": {"display_name": "Id"},
                        "dropped_at": {"display_name": "Dropped"},
                    },
                }
            }
        )
        resp, save, _, _ = await self._run(
            http_client,
            model=model,
            schema=_schema(["orders"], {"orders": ["id"]}),
            provenance={
                **_no_provenance(),
                "table": {"public.orders": ["primary_timestamp_column"]},
            },
            protected={
                "table": {"public.orders": {"primary_timestamp_column": "dropped_at"}},
                "column": {},
            },
            dry_run=True,
        )
        assert resp.status_code == 200
        assert any("dropped_at" in w for w in resp.json()["summary"]["warnings"])
        save.assert_not_awaited()

    async def test_the_model_is_read_once_before_the_lock(self, http_client) -> None:
        """The drift and the preview come from one read, so a write landing between
        two reads cannot make the preview contradict itself. The apply reads once
        more (under the row lock: ``test_the_apply_rereads_under_the_row_lock``)."""
        resp, _, _, _ = await self._run(
            http_client, model=self._complete(), schema=_schema(["orders"]), dry_run=True
        )
        assert resp.status_code == 200
        assert self.loader.await_count == 1
        self.load_model.assert_not_awaited()
        resp, _, _, _ = await self._run(
            http_client, model=self._complete(), schema=_schema(["orders"])
        )
        assert resp.status_code == 200
        assert self.loader.await_count == 2
        self.load_model.assert_not_awaited()

    async def test_an_apply_is_logged_with_its_summary(self, http_client) -> None:
        db = self._db()
        commits_when_logged: list[int] = []
        resp, _, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders"]),
            db=db,
            on_record=lambda *_a, **_kw: commits_when_logged.append(db.commit.await_count),
        )
        assert resp.status_code == 200
        self.record.assert_awaited_once()
        logged = self.record.await_args.args[2]
        assert logged == resp.json()["summary"]
        assert self.record.await_args.kwargs["applied_by"]
        # In the save's transaction: staged after the cache commit and before the
        # commit that stores the model, which carries the log row with it.
        assert commits_when_logged == [1]
        assert db.commit.await_count == 2

    async def test_a_preview_or_a_no_op_is_not_logged(self, http_client) -> None:
        await self._run(
            http_client, model=self._complete(), schema=_schema(["orders"]), dry_run=True
        )
        self.record.assert_not_awaited()
        await self._run(http_client, model=self._complete(), schema=_schema(["orders", "coupons"]))
        self.record.assert_not_awaited()

    async def test_a_rejected_save_is_not_logged(self, http_client) -> None:
        resp, _, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders"]),
            save=AsyncMock(side_effect=ValueError("table key too long")),
        )
        assert resp.status_code == 400
        self.record.assert_not_awaited()

    # ── review fixes ──

    @staticmethod
    def _annotate_all(**kw) -> dict:
        return {k: TableSemantic(display_name=k) for k in kw["table_keys"]}

    async def test_a_removed_metric_replaced_by_the_llm_does_not_keep_its_hand_edit_mark(
        self, http_client
    ) -> None:
        """The hand-edited metric leaves with its table; the LLM's same-named metric
        over the new table is a new item — unmarked, and reported as a replacement."""
        model = self._complete(
            business_metrics=[
                {
                    "name": "Coupon uses",
                    "metric_type": "simple",
                    "definition": "COUNT(*)",
                    "description": "hand written",
                    "aggregation": "count",
                    "related_tables": ["public.coupons"],
                }
            ]
        )
        llm_metric = SimpleMetric(
            name="Coupon uses",
            definition="COUNT(*)",
            description="llm",
            aggregation=AggregationType.COUNT,
            related_tables=["public.vouchers"],
        )
        resp, save, _, _ = await self._run(
            http_client,
            model=model,
            schema=_schema(["orders", "vouchers"]),
            provenance={**_no_provenance(), "metric": {"Coupon uses": ["definition"]}},
            generated=self._annotate_all,
            llm=([llm_metric], [], [], [], []),
        )
        assert resp.status_code == 200
        [metric] = resp.json()["semantic_model"]["business_metrics"]
        assert metric["description"] == "llm"
        assert "Coupon uses" not in save.await_args.kwargs["provenance"]["metric"]
        summary = resp.json()["summary"]
        assert summary["metrics_removed"] == ["Coupon uses"]
        assert summary["metrics_added"] == ["Coupon uses"]

    @pytest.mark.parametrize(
        ("kind", "section", "stored", "llm_at", "llm_item", "identity"),
        [
            (
                "segment",
                "segments",
                {
                    "name": "discounted",
                    "description": "hand",
                    "sql_expression": "coupon_id IS NOT NULL",
                    "applicable_tables": ["public.coupons"],
                },
                4,
                Segment(
                    name="discounted",
                    description="llm",
                    sql_expression="voucher_id IS NOT NULL",
                    applicable_tables=["public.vouchers"],
                ),
                "discounted",
            ),
            (
                "common_join",
                "joins",
                {
                    "tables": ["public.orders", "public.coupons"],
                    "join_pattern": "orders.ref = refs.id",
                    "description": "hand",
                },
                1,
                CommonJoin(
                    tables=["public.orders", "public.vouchers"],
                    join_pattern="orders.ref = refs.id",
                    description="llm",
                ),
                "orders.ref = refs.id",
            ),
            (
                "derived_column",
                "derived_columns",
                {
                    "name": "rate",
                    "sql_expression": "1",
                    "description": "hand",
                    "base_tables": ["public.coupons"],
                },
                2,
                DerivedColumn(
                    name="rate",
                    sql_expression="2",
                    description="llm",
                    base_tables=["public.vouchers"],
                ),
                "rate",
            ),
        ],
        ids=["segment", "common_join", "derived_column"],
    )
    async def test_a_removed_global_item_replaced_by_the_llm_does_not_keep_its_mark(
        self, http_client, kind, section, stored, llm_at, llm_item, identity
    ) -> None:
        """Global items are marked as ``kind:identity`` (a join's identity is its
        ``join_pattern``); each kind's mark leaves with its removed item."""
        model_field = {"joins": "common_joins"}.get(section, section)
        llm: list = [[], [], [], [], []]
        llm[llm_at] = [llm_item]
        mark = f"{kind}:{identity}"
        resp, save, _, _ = await self._run(
            http_client,
            model=self._complete(**{model_field: [stored]}),
            schema=_schema(["orders", "vouchers"]),
            provenance={**_no_provenance(), "global_item": {mark: ["payload"]}},
            generated=self._annotate_all,
            llm=tuple(llm),
        )
        assert resp.status_code == 200
        [item] = resp.json()["semantic_model"][model_field]
        assert item["description"] == "llm"
        assert mark not in save.await_args.kwargs["provenance"]["global_item"]
        summary = resp.json()["summary"]
        assert summary[f"{section}_removed"] == summary[f"{section}_added"] == [identity]

    async def test_new_tables_over_the_cap_are_left_for_the_next_apply(self, http_client) -> None:
        """One request must finish inside the proxy's 900s: at most three batches and
        a globals pass. The rest are named in a warning, not sent."""
        from app.routers.semantic import _DRIFT_MAX_NEW_TABLES

        names = [f"t{i:02d}" for i in range(_DRIFT_MAX_NEW_TABLES + 2)]
        keys = sorted(f"public.{n}" for n in names)
        resp, _, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", *names]),
            generated=self._annotate_all,
        )
        assert resp.status_code == 200
        sent = [
            k for call in gen.generate_tables.await_args_list for k in call.kwargs["table_keys"]
        ]
        assert sent == keys[:_DRIFT_MAX_NEW_TABLES]
        assert gen.generate_tables.await_count == _DRIFT_MAX_NEW_TABLES // GENERATION_BATCH_SIZE
        body = resp.json()
        summary = body["summary"]
        assert summary["tables_added"] == keys[:_DRIFT_MAX_NEW_TABLES]
        # Not "the LLM returned nothing": they were never sent.
        assert summary["tables_not_annotated"] == []
        [warning] = [w for w in summary["warnings"] if "2 more new tables" in w]
        assert keys[-1] in warning and "Check Drift" in warning
        assert warning in body["semantic_model"]["generation_warnings"]

    def test_the_new_table_cap_fits_the_proxy_timeout(self) -> None:
        """Every table batch and the globals pass may each take the full LLM timeout;
        together they must finish before the proxy gives up on the request."""
        from app.routers.semantic import _DRIFT_MAX_NEW_TABLES, _GENERATION_TIMEOUT_SECONDS

        nginx_conf = Path(__file__).resolve().parents[3] / "frontend" / "nginx.conf"
        if not nginx_conf.is_file():
            pytest.skip("frontend/nginx.conf is not in this checkout")
        location = re.search(
            r"location[^{]*semantic/\(generate\|apply-drift\)[^{]*\{([^}]*)\}",
            nginx_conf.read_text(),
        )
        assert location, "no .../semantic/(generate|apply-drift) location in nginx.conf"
        timeout = re.search(r"proxy_read_timeout\s+(\d+)s?\s*;", location.group(1))
        assert timeout, "the location sets no proxy_read_timeout"
        nginx_apply_drift_timeout_seconds = int(timeout.group(1))
        llm_calls = math.ceil(_DRIFT_MAX_NEW_TABLES / GENERATION_BATCH_SIZE) + 1
        assert llm_calls * _GENERATION_TIMEOUT_SECONDS < nginx_apply_drift_timeout_seconds

    @pytest.mark.parametrize("dry_run", [False, True], ids=["apply", "preview"])
    async def test_tables_the_last_apply_could_not_annotate_go_last(
        self, http_client, dry_run: bool
    ) -> None:
        """Otherwise tables the LLM keeps omitting would hold the front of the sorted
        list forever and starve the tables after the cap. The preview orders the same
        way, so it lists exactly what the apply sends."""
        from app.routers.semantic import _DRIFT_MAX_NEW_TABLES

        names = [f"t{i:02d}" for i in range(_DRIFT_MAX_NEW_TABLES + 2)]
        keys = sorted(f"public.{n}" for n in names)
        stuck = keys[:2]
        resp, _, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", *names]),
            generated=self._annotate_all,
            last_apply={"tables_not_annotated": stuck, "changed": True},
            dry_run=dry_run,
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["tables_added"] == keys[2:]
        [warning] = [w for w in summary["warnings"] if "2 more new tables" in w]
        assert all(k in warning for k in stuck)
        if not dry_run:
            sent = [
                k for call in gen.generate_tables.await_args_list for k in call.kwargs["table_keys"]
            ]
            assert sent == keys[2:]

    @pytest.mark.parametrize(
        "last_apply",
        [
            {},
            {"tables_not_annotated": "public.t00"},
            {"tables_not_annotated": [1, None]},
            ["not", "a", "summary"],
        ],
        ids=["field-missing", "field-a-string", "entries-not-strings", "summary-not-a-dict"],
    )
    async def test_an_unreadable_last_apply_leaves_the_sorted_order(
        self, http_client, last_apply
    ) -> None:
        from app.routers.semantic import _DRIFT_MAX_NEW_TABLES

        names = [f"t{i:02d}" for i in range(_DRIFT_MAX_NEW_TABLES + 1)]
        keys = sorted(f"public.{n}" for n in names)
        resp, _, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", *names]),
            generated=self._annotate_all,
            last_apply=last_apply,
        )
        assert resp.status_code == 200
        assert resp.json()["summary"]["tables_added"] == keys[:_DRIFT_MAX_NEW_TABLES]
        sent = [
            k for call in gen.generate_tables.await_args_list for k in call.kwargs["table_keys"]
        ]
        assert sent == keys[:_DRIFT_MAX_NEW_TABLES]

    async def test_a_preview_over_the_cap_lists_only_what_this_apply_would_annotate(
        self, http_client
    ) -> None:
        from app.routers.semantic import _DRIFT_MAX_NEW_TABLES

        names = [f"t{i:02d}" for i in range(_DRIFT_MAX_NEW_TABLES + 1)]
        keys = sorted(f"public.{n}" for n in names)
        resp, _, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", *names]),
            dry_run=True,
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["tables_added"] == keys[:_DRIFT_MAX_NEW_TABLES]
        assert any(
            "1 more new table will not be annotated by this apply" in w and keys[-1] in w
            for w in summary["warnings"]
        )
        gen.generate_tables.assert_not_awaited()

    @staticmethod
    def _noisy_model() -> SemanticModel:
        """A hand edit on a dropped column and a near-miss table key: both trims log."""
        return TestApplyDrift._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "primary_timestamp_column": "dropped_at",
                    "columns": {
                        "id": {"display_name": "Id"},
                        "dropped_at": {"display_name": "Dropped"},
                    },
                },
                "public.Orders": {"display_name": "Orders (blob era)", "columns": {}},
            }
        )

    async def test_a_dry_run_logs_no_trim_it_did_not_make(self, http_client, caplog) -> None:
        kwargs = {
            "model": self._noisy_model(),
            "schema": _schema(["orders"], {"orders": ["id"]}),
            "provenance": {
                **_no_provenance(),
                "table": {"public.orders": ["primary_timestamp_column"]},
            },
            "protected": {
                "table": {"public.orders": {"primary_timestamp_column": "dropped_at"}},
                "column": {},
            },
        }
        with caplog.at_level(logging.WARNING, logger="app.semantic.generator"):
            resp, _, _, _ = await self._run(http_client, dry_run=True, **kwargs)
        assert resp.status_code == 200
        # The preview still names the trim; it just does not log it as done.
        assert any("dropped_at" in w for w in resp.json()["summary"]["warnings"])
        assert "Dangling hand edit" not in caplog.text
        assert "near-miss" not in caplog.text
        # The apply itself does log both.
        _reset_limiter()
        with caplog.at_level(logging.WARNING, logger="app.semantic.generator"):
            resp, _, _, _ = await self._run(http_client, **kwargs)
        assert resp.status_code == 200
        assert "Dangling hand edit" in caplog.text
        assert "near-miss" in caplog.text

    async def test_hidden_and_dropped_tables_stay_out_of_the_globals_prompt(
        self, http_client
    ) -> None:
        conn = _make_conn(privacy_settings={"excluded_tables": ["public.payroll"]})
        model = self._complete(
            tables={
                "public.orders": {"display_name": "Orders", "columns": {}},
                "public.coupons": {"display_name": "Coupons", "columns": {}},
                "public.payroll": {"display_name": "Payroll", "columns": {}},
            }
        )
        resp, _, gen, _ = await self._run(
            http_client,
            model=model,
            # payroll is live but hidden; coupons is gone; events is new.
            schema=_schema(["orders", "payroll", "events"], {"orders": [], "events": ["id"]}),
            conn=conn,
            generated=self._annotate_all,
        )
        assert resp.status_code == 200
        assert set(gen.generate_globals.await_args.kwargs["all_tables"]) == {
            "public.orders",
            "public.events",
        }
        # Hidden is kept in the model, gone is not.
        assert set(resp.json()["semantic_model"]["tables"]) == {
            "public.orders",
            "public.payroll",
            "public.events",
        }

    async def test_same_named_llm_joins_derived_columns_and_segments_never_replace(
        self, http_client
    ) -> None:
        model = self._complete(
            common_joins=[
                {
                    "tables": ["public.orders", "public.coupons"],
                    "join_pattern": "orders.coupon_id = coupons.id",
                    "description": "mine",
                }
            ],
            derived_columns=[
                {"name": "net", "sql_expression": "1", "description": "mine", "base_tables": []}
            ],
            segments=[{"name": "big", "description": "mine", "sql_expression": "1=1"}],
        )
        llm_join = CommonJoin(
            tables=["public.orders", "public.events"],
            join_pattern="orders.coupon_id = coupons.id",
            description="llm",
        )
        # Each names the new table, so only the never-replace rule keeps them out.
        llm_derived = DerivedColumn(
            name="net", sql_expression="2", description="llm", base_tables=["public.events"]
        )
        llm_segment = Segment(
            name="big",
            description="llm",
            sql_expression="2=2",
            applicable_tables=["public.events"],
        )
        resp, _, _, _ = await self._run(
            http_client,
            model=model,
            schema=_schema(["orders", "coupons", "events"]),
            generated=self._annotate_all,
            llm=([], [llm_join], [llm_derived], [], [llm_segment]),
        )
        assert resp.status_code == 200
        body = resp.json()["semantic_model"]
        assert [j["description"] for j in body["common_joins"]] == ["mine"]
        assert [d["description"] for d in body["derived_columns"]] == ["mine"]
        assert [s["description"] for s in body["segments"]] == ["mine"]

    async def test_the_caches_are_invalidated_before_the_save_commits(self, http_client) -> None:
        db = self._db()
        order: list[str] = []
        db.commit.side_effect = lambda: order.append("commit")
        invalidate = AsyncMock(side_effect=lambda *_a, **_k: order.append("invalidate"))
        with patch("app.routers.semantic._invalidate_connection_caches", new=invalidate):
            resp, _, _, _ = await self._run(
                http_client, model=self._complete(), schema=_schema(["orders"]), db=db
            )
        assert resp.status_code == 200
        assert order == ["commit", "invalidate", "commit"]
        assert invalidate.await_args.args[0] == "conn-123"
        # The schema cache was refreshed from a live introspection one commit ago: kept.
        assert invalidate.await_args.kwargs == {"keep_schema": True}

    async def test_an_unmarked_live_edge_the_catalog_no_longer_has_is_removed(
        self, http_client
    ) -> None:
        """Both endpoints live, but the catalog does not rebuild it: it is the
        catalog's to rebuild, so it is not re-added like a hidden edge would be."""
        model = self._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"id": {"display_name": "I"}, "customer_id": {"display_name": "C"}},
                },
                "public.customers": {"display_name": "C", "columns": {"id": {"display_name": "I"}}},
                "public.coupons": {"display_name": "Coupons", "columns": {}},
            },
            relationships=[self._edge("public.orders", "customer_id", "public.customers")],
        )
        resp, _, _, _ = await self._run(
            http_client,
            model=model,
            schema=_schema(
                ["orders", "customers"], {"orders": ["id", "customer_id"], "customers": ["id"]}
            ),
            fresh_edges=[],
        )
        assert resp.status_code == 200
        assert resp.json()["summary"]["relationships_removed"] == [
            "public.orders.customer_id → public.customers.id"
        ]
        assert resp.json()["semantic_model"]["relationships"] == []

    async def test_a_deleted_catalog_edge_is_not_brought_back_by_unrelated_drift(
        self, http_client
    ) -> None:
        """The user deleted the FK edge orders.customer_id → customers (a deletion leaves
        no mark). A column drop elsewhere must not re-add it, on apply or on preview: a
        catalog edge the model lacks is added only when it touches a newly annotated
        table — the rule the LLM items already follow."""
        model = self._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {
                        "id": {"display_name": "I"},
                        "customer_id": {"display_name": "C"},
                        "legacy": {"display_name": "L"},
                    },
                },
                "public.customers": {"display_name": "C", "columns": {"id": {"display_name": "I"}}},
            },
            relationships=[],
        )
        schema = _schema(
            ["orders", "customers"], {"orders": ["id", "customer_id"], "customers": ["id"]}
        )
        fk = self._edge("public.orders", "customer_id", "public.customers")
        for dry_run in (True, False):
            resp, _, _, _ = await self._run(
                http_client, model=model, schema=schema, fresh_edges=[fk], dry_run=dry_run
            )
            assert resp.status_code == 200
            summary = resp.json()["summary"]
            assert summary["columns_removed"] == {"public.orders": ["legacy"]}
            assert summary["relationships_added"] == []
        assert resp.json()["semantic_model"]["relationships"] == []

    def _edge_drift_model(self, edges: list[RelationshipEdge]) -> SemanticModel:
        """orders(id, customer_id, legacy) and customers(id) holding ``edges``."""
        return self._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {
                        "id": {"display_name": "I"},
                        "customer_id": {"display_name": "C"},
                        "legacy": {"display_name": "L"},
                    },
                },
                "public.customers": {"display_name": "C", "columns": {"id": {"display_name": "I"}}},
            },
            relationships=[e.model_dump() for e in edges],
        )

    async def test_an_edge_the_model_and_the_catalog_both_have_survives_unrelated_drift(
        self, http_client
    ) -> None:
        fk = self._edge("public.orders", "customer_id", "public.customers")
        schema = _schema(
            ["orders", "customers"], {"orders": ["id", "customer_id"], "customers": ["id"]}
        )
        resp, _, _, _ = await self._run(
            http_client, model=self._edge_drift_model([fk]), schema=schema, fresh_edges=[fk]
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["columns_removed"] == {"public.orders": ["legacy"]}
        assert summary["relationships_added"] == summary["relationships_removed"] == []
        assert [r["from_column"] for r in resp.json()["semantic_model"]["relationships"]] == [
            "customer_id"
        ]

    async def test_a_hand_edited_edge_survives_even_when_the_catalog_lacks_it(
        self, http_client
    ) -> None:
        fk = self._edge("public.orders", "customer_id", "public.customers")
        marks = {
            **_no_provenance(),
            "relationship": {semantic_store.relationship_key(fk): ["description"]},
        }
        schema = _schema(
            ["orders", "customers"], {"orders": ["id", "customer_id"], "customers": ["id"]}
        )
        resp, _, _, _ = await self._run(
            http_client,
            model=self._edge_drift_model([fk]),
            schema=schema,
            fresh_edges=[],
            provenance=marks,
        )
        assert resp.status_code == 200
        assert resp.json()["summary"]["relationships_removed"] == []
        assert [r["from_column"] for r in resp.json()["semantic_model"]["relationships"]] == [
            "customer_id"
        ]

    async def test_an_llm_item_the_store_would_reject_is_dropped_not_fatal(
        self, http_client
    ) -> None:
        too_long = "m" * 600
        llm_metrics = [
            SimpleMetric(
                name=too_long,
                definition="1",
                description="x",
                aggregation=AggregationType.SUM,
                related_tables=["public.events"],
            ),
            SimpleMetric(
                name="Events",
                definition="2",
                description="x",
                aggregation=AggregationType.SUM,
                related_tables=["public.events"],
            ),
        ]
        resp, save, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", "events"]),
            generated=self._annotate_all,
            llm=(llm_metrics, [], [], [], []),
        )
        assert resp.status_code == 200
        assert [m["name"] for m in resp.json()["semantic_model"]["business_metrics"]] == ["Events"]
        save.assert_awaited_once()

    async def test_a_hidden_column_stored_quoted_is_kept_not_removed(self, http_client) -> None:
        """A blob-era ``'"ssn"'`` key for a column the policy hides: the check calls
        it hidden, so the apply must not drop it or name it in the summary (and log)."""
        conn = _make_conn(privacy_settings={"excluded_columns": ["public.orders.ssn"]})
        model = self._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"id": {"display_name": "Id"}, '"ssn"': {"display_name": "S"}},
                },
                "public.coupons": {"display_name": "Coupons", "columns": {}},
            }
        )
        resp, _, _, _ = await self._run(
            http_client, model=model, schema=_schema(["orders"]), conn=conn
        )
        assert resp.status_code == 200
        assert set(resp.json()["semantic_model"]["tables"]["public.orders"]["columns"]) == {
            "id",
            '"ssn"',
        }
        summary = resp.json()["summary"]
        assert summary["columns_removed"] == {}
        assert summary["tables_removed"] == ["public.coupons"]
        assert "ssn" not in json.dumps(self.record.await_args.args[2])

    async def test_a_quoted_duplicate_of_a_live_column_is_drift_the_apply_removes(
        self, http_client
    ) -> None:
        """Check Drift reports ``'"status"'`` beside a live ``status`` and the apply drops
        it — one rule, so the summary never names a removal the check did not."""
        model = self._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {
                        "id": {"display_name": "Id"},
                        "status": {"display_name": "S"},
                        '"status"': {"display_name": "S (quoted)"},
                    },
                },
                "public.coupons": {"display_name": "Coupons", "columns": {}},
            }
        )
        resp, save, _, _ = await self._run(
            http_client,
            model=model,
            schema=_schema(["orders", "coupons"], {"orders": ["id", "status"], "coupons": ["id"]}),
        )
        assert resp.status_code == 200
        columns = resp.json()["semantic_model"]["tables"]["public.orders"]["columns"]
        assert set(columns) == {"id", "status"}
        # Under its stored spelling: the bare name is still a column of the table.
        assert resp.json()["summary"]["columns_removed"] == {"public.orders": ['"status"']}
        save.assert_awaited_once()

    async def test_llm_items_over_unchanged_tables_are_not_added(self, http_client) -> None:
        """The globals pass sees every table, but only items over a newly annotated
        table are new: one the user deleted over an unchanged table stays deleted."""

        def metric(name: str, *tables: str) -> SimpleMetric:
            return SimpleMetric(
                name=name,
                definition="COUNT(*)",
                description="llm",
                aggregation=AggregationType.COUNT,
                related_tables=list(tables),
            )

        llm = (
            [
                metric("order_count", "public.orders"),  # deleted by the user earlier
                metric("event_count", "public.events"),
                metric("events_per_order", "orders", "events"),  # bare names
            ],
            [
                CommonJoin(tables=["public.orders"], join_pattern="o.a = o.b", description="x"),
                CommonJoin(
                    tables=["public.orders", "public.events"],
                    join_pattern="orders.id = events.order_id",
                    description="x",
                ),
            ],
            [
                DerivedColumn(name="old", sql_expression="1", description="x"),
                DerivedColumn(
                    name="new", sql_expression="1", description="x", base_tables=["public.events"]
                ),
            ],
            [],
            [
                Segment(name="everywhere", description="x", sql_expression="1=1"),
                Segment(
                    name="recent",
                    description="x",
                    sql_expression="1=1",
                    applicable_tables=["public.events"],
                ),
            ],
        )
        resp, _, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", "events"]),
            generated=self._annotate_all,
            llm=llm,
        )
        assert resp.status_code == 200
        body = resp.json()["semantic_model"]
        assert [m["name"] for m in body["business_metrics"]] == [
            "event_count",
            "events_per_order",
        ]
        assert [j["join_pattern"] for j in body["common_joins"]] == ["orders.id = events.order_id"]
        assert [d["name"] for d in body["derived_columns"]] == ["new"]
        # A segment with no tables applies everywhere: not about the new table.
        assert [s["name"] for s in body["segments"]] == ["recent"]

    async def test_warnings_name_only_the_metrics_the_apply_added(self, http_client) -> None:
        """A time-series metric over a fiscal-period table is worth a warning — but
        not about an LLM metric the merge discarded in favour of the user's own."""
        model = self._complete(
            business_metrics=[
                {
                    "name": "Budget trend",
                    "metric_type": "simple",
                    "definition": "SUM(amount)",
                    "description": "mine",
                    "aggregation": "sum",
                    "related_tables": ["public.orders"],
                }
            ]
        )

        def fiscal(name: str) -> SimpleMetric:
            return SimpleMetric(
                name=name,
                definition="SUM(amount) OVER (ORDER BY date_trunc('month', created_at))",
                description="llm",
                aggregation=AggregationType.SUM,
                related_tables=["public.budgets"],
            )

        resp, _, _, _ = await self._run(
            http_client,
            model=model,
            schema=_schema(
                ["orders", "coupons", "budgets"],
                {"budgets": ["id", "fiscal_year", "fiscal_quarter", "created_at"]},
            ),
            generated=self._annotate_all,
            llm=([fiscal("Budget trend"), fiscal("Budget pace")], [], [], ["stale"], []),
        )
        assert resp.status_code == 200
        warnings = resp.json()["semantic_model"]["generation_warnings"]
        assert len(warnings) == 1
        assert '"Budget pace"' in warnings[0]
        assert resp.json()["summary"]["warnings"] == warnings

    async def test_the_apply_rereads_under_the_row_lock(self, http_client) -> None:
        """The LLM calls run unlocked; the model the apply saves over is read after
        the connection row lock is taken, so a PUT landing meanwhile is not lost."""
        loads_when_locked: list[int] = []
        lock = AsyncMock(side_effect=lambda *_a: loads_when_locked.append(self.loader.await_count))
        with patch("app.routers.semantic._lock_generation", new=lock):
            resp, save, _, _ = await self._run(
                http_client, model=self._complete(), schema=_schema(["orders"])
            )
        assert resp.status_code == 200
        # After the pre-check read, before the re-read the save is built from.
        assert loads_when_locked == [1]
        assert self.loader.await_count == 2
        save.assert_awaited_once()

    async def test_a_new_table_stored_meanwhile_is_left_as_stored(self, http_client) -> None:
        """A Regenerate (or another apply) stored ``events`` while this apply's LLM ran.
        The re-read under the lock has it: it is no longer new, so its annotation is
        not overwritten and no global item is added over it as if it were."""
        with_events = self._complete(
            tables={
                "public.orders": {"display_name": "Orders", "columns": {}},
                "public.coupons": {"display_name": "Coupons", "columns": {}},
                "public.events": {"display_name": "Events (regenerated)", "columns": {}},
            },
            business_metrics=[
                {
                    "name": "event_count",
                    "metric_type": "simple",
                    "definition": "COUNT(*)",
                    "description": "regenerated",
                    "aggregation": "count",
                    "related_tables": ["public.events"],
                }
            ],
        )
        llm_metric = SimpleMetric(
            name="events_total",
            definition="COUNT(*)",
            description="apply",
            aggregation=AggregationType.COUNT,
            related_tables=["public.events"],
        )
        resp, _, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", "events"], {"orders": [], "coupons": []}),
            generated=lambda **_kw: {"public.events": TableSemantic(display_name="Events (apply)")},
            llm=([llm_metric], [], [], [], []),
            current=(with_events, _snapshot()),
        )
        assert resp.status_code == 200
        body = resp.json()["semantic_model"]
        assert body["tables"]["public.events"]["display_name"] == "Events (regenerated)"
        assert [m["name"] for m in body["business_metrics"]] == ["event_count"]
        # Nothing new from the LLM landed: the review state is untouched.
        assert body["is_user_reviewed"] is True
        summary = resp.json()["summary"]
        assert summary["tables_added"] == []
        assert summary["metrics_added"] == []
        assert summary["tables_not_annotated"] == []

    async def test_a_table_added_and_dropped_during_the_llm_calls_leaves_with_its_items(
        self, http_client
    ) -> None:
        """The drift is recomputed against the model re-read under the lock: a PUT that
        landed meanwhile added ``public.tmp_report`` (already dropped in the database)
        with a metric over it — both leave, and the summary names both."""
        meanwhile = self._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"id": {"display_name": "Id"}},
                },
                "public.coupons": {
                    "display_name": "Coupons",
                    "columns": {"id": {"display_name": "Id"}},
                },
                "public.tmp_report": {"display_name": "Tmp", "columns": {}},
            },
            business_metrics=[
                {
                    "name": "tmp_rows",
                    "metric_type": "simple",
                    "definition": "COUNT(*)",
                    "description": "x",
                    "aggregation": "count",
                    "related_tables": ["public.tmp_report"],
                }
            ],
        )
        resp, _, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", "events"]),
            generated={"public.events": TableSemantic(display_name="Events")},
            current=(meanwhile, _snapshot()),
        )
        assert resp.status_code == 200
        body = resp.json()["semantic_model"]
        assert "public.tmp_report" not in body["tables"]
        assert body["business_metrics"] == []
        summary = resp.json()["summary"]
        assert summary["tables_removed"] == ["public.tmp_report"]
        assert summary["metrics_removed"] == ["tmp_rows"]

    async def test_a_drift_another_tab_applied_meanwhile_is_not_applied_again(
        self, http_client
    ) -> None:
        """Pre-lock the drift removes ``coupons``; by the re-read another apply already
        has. Nothing is rewritten, purged or logged, and the lock is released."""
        already = self._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"id": {"display_name": "Id"}},
                }
            }
        )
        db = self._db()
        invalidate = AsyncMock()
        with patch("app.routers.semantic._invalidate_connection_caches", new=invalidate):
            resp, save, _, _ = await self._run(
                http_client,
                model=self._complete(),
                schema=_schema(["orders"]),
                current=(already, _snapshot()),
                db=db,
            )
        assert resp.status_code == 200
        assert resp.json()["summary"]["changed"] is False
        assert set(resp.json()["semantic_model"]["tables"]) == {"public.orders"}
        save.assert_not_awaited()
        self.record.assert_not_awaited()
        invalidate.assert_not_awaited()
        # The schema cache commit only; the rollback releases the row lock.
        db.commit.assert_awaited_once()
        db.rollback.assert_awaited_once()

    async def test_an_apply_the_llm_added_nothing_to_is_logged_but_not_saved(
        self, http_client
    ) -> None:
        """The only drift is a new table the LLM returned nothing for: the model is as it
        was, so no row rewrite and no cache purge. The skipped table is still logged (and
        committed, which releases the lock): the next apply orders its new tables by the
        newest entry's ``tables_not_annotated``, so without it they would starve the rest."""
        db = self._db()
        invalidate = AsyncMock()
        with patch("app.routers.semantic._invalidate_connection_caches", new=invalidate):
            resp, save, _, _ = await self._run(
                http_client,
                model=self._complete(),
                schema=_schema(["orders", "coupons", "events"]),
                generated={},
                db=db,
            )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["changed"] is False
        assert summary["tables_not_annotated"] == ["public.events"]
        save.assert_not_awaited()
        invalidate.assert_not_awaited()
        self.record.assert_awaited_once()
        assert self.record.await_args.args[2] == summary
        # The schema cache, then the log entry; no rollback.
        assert db.commit.await_count == 2
        db.rollback.assert_not_awaited()

    async def test_an_ambiguous_bare_table_name_is_not_taken_for_the_new_table(
        self, http_client
    ) -> None:
        """``events`` names ``public.events`` as much as the new ``sales.events``, so
        an item over it is not evidence that it is about the new table."""
        model = self._complete(
            tables={
                "public.orders": {"display_name": "Orders", "columns": {}},
                "public.coupons": {"display_name": "Coupons", "columns": {}},
                "public.events": {"display_name": "Events", "columns": {}},
            }
        )
        schema = _schema(["orders", "coupons", "events"], {"orders": [], "coupons": []})
        schema.tables.append(
            TableInfo(
                catalog=None,
                schema_name="sales",
                name="events",
                table_type="table",
                columns=[ColumnInfo(name="id", data_type="integer", native_type="int4")],
            )
        )

        def metric(name: str, table: str) -> SimpleMetric:
            return SimpleMetric(
                name=name,
                definition="COUNT(*)",
                description="llm",
                aggregation=AggregationType.COUNT,
                related_tables=[table],
            )

        resp, _, gen, _ = await self._run(
            http_client,
            model=model,
            schema=schema,
            generated=self._annotate_all,
            llm=(
                [metric("ambiguous", "events"), metric("qualified", "sales.events")],
                [],
                [],
                [],
                [],
            ),
        )
        assert resp.status_code == 200
        assert gen.generate_tables.await_args.kwargs["table_keys"] == ["sales.events"]
        names = [m["name"] for m in resp.json()["semantic_model"]["business_metrics"]]
        assert names == ["qualified"]


_DRIFT_HISTORY_URL = "/api/v1/connections/conn-123/semantic/drift/history"


class TestDriftApplyHistory:
    async def test_lists_logged_applies(self, http_client) -> None:
        from datetime import UTC, datetime

        records = [
            semantic_store.DriftApplyRecord(
                applied_at=datetime(2026, 10, 16, 9, 30, tzinfo=UTC),
                applied_by="u1",
                summary={"changed": True, "tables_removed": ["ecommerce.coupons"]},
            )
        ]
        lister = AsyncMock(return_value=records)
        app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
        with patch(f"{_STORE}.list_drift_applies", new=lister):
            resp = await http_client.get(_DRIFT_HISTORY_URL + "?limit=5")
        assert resp.status_code == 200
        body = resp.json()
        assert body[0]["applied_at"].startswith("2026-10-16T09:30")
        assert body[0]["applied_by"] == "u1"
        assert body[0]["summary"]["tables_removed"] == ["ecommerce.coupons"]
        # A field the logged summary lacks takes its default, so older rows still render.
        assert body[0]["summary"]["metrics_removed"] == []
        assert lister.await_args.kwargs["limit"] == 5

    async def test_an_unreadable_entry_is_skipped_not_fatal(self, http_client, caplog) -> None:
        """A summary that no longer validates (a field whose type changed, a row
        written by hand) must not take the whole list down with a 500."""
        from datetime import UTC, datetime

        at = datetime(2026, 10, 16, 9, 30, tzinfo=UTC)
        records = [
            semantic_store.DriftApplyRecord(
                applied_at=at, applied_by="u1", summary={"columns_removed": ["x"]}
            ),
            semantic_store.DriftApplyRecord(
                applied_at=at, applied_by="u2", summary={"tables_removed": ["public.a"]}
            ),
        ]
        app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
        with (
            patch(f"{_STORE}.list_drift_applies", new=AsyncMock(return_value=records)),
            caplog.at_level(logging.WARNING, logger="app.routers.semantic"),
        ):
            resp = await http_client.get(_DRIFT_HISTORY_URL)
        assert resp.status_code == 200
        assert [e["applied_by"] for e in resp.json()] == ["u2"]
        assert "skipping unreadable entry" in caplog.text

    async def test_the_list_is_filled_past_entries_it_cannot_read(self, http_client) -> None:
        """``limit`` valid entries, not ``limit`` rows minus the unreadable ones: the
        route pages on until it has enough, so the page's "last N" is the real count."""
        from datetime import UTC, datetime

        at = datetime(2026, 10, 16, 9, 30, tzinfo=UTC)

        def record(summary: dict) -> semantic_store.DriftApplyRecord:
            return semantic_store.DriftApplyRecord(applied_at=at, applied_by="u1", summary=summary)

        first_page = [record({"columns_removed": ["x"]})] + [
            record({"tables_removed": [f"public.t{i}"]}) for i in range(9)
        ]
        second_page = [record({"tables_removed": ["public.t9"]})]
        lister = AsyncMock(side_effect=[first_page, second_page])
        app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
        with patch(f"{_STORE}.list_drift_applies", new=lister):
            resp = await http_client.get(_DRIFT_HISTORY_URL + "?limit=10")
        assert resp.status_code == 200
        assert [e["summary"]["tables_removed"] for e in resp.json()] == [
            [f"public.t{i}"] for i in range(10)
        ]
        assert [c.kwargs for c in lister.await_args_list] == [
            {"limit": 10, "offset": 0},
            {"limit": 1, "offset": 10},
        ]

    async def test_a_short_page_ends_the_list(self, http_client) -> None:
        """Fewer rows than asked means the log ran out: no second query for nothing."""
        from datetime import UTC, datetime

        at = datetime(2026, 10, 16, 9, 30, tzinfo=UTC)
        records = [
            semantic_store.DriftApplyRecord(
                applied_at=at, applied_by="u1", summary={"tables_removed": ["public.a"]}
            )
        ]
        lister = AsyncMock(return_value=records)
        app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
        with patch(f"{_STORE}.list_drift_applies", new=lister):
            resp = await http_client.get(_DRIFT_HISTORY_URL + "?limit=10")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        lister.assert_awaited_once()

    async def test_a_missing_connection_is_404(self, http_client) -> None:
        app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=None))
        resp = await http_client.get("/api/v1/connections/missing/semantic/drift/history")
        assert resp.status_code == 404
        # The connection check's own 404, not a route that does not exist.
        assert resp.json()["detail"] == "Connection 'missing' not found"

    @pytest.mark.parametrize("limit", [0, 51])
    async def test_a_limit_out_of_range_is_422(self, http_client, limit: int) -> None:
        lister = AsyncMock(return_value=[])
        app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
        with patch(f"{_STORE}.list_drift_applies", new=lister):
            resp = await http_client.get(f"{_DRIFT_HISTORY_URL}?limit={limit}")
        assert resp.status_code == 422
        lister.assert_not_awaited()
