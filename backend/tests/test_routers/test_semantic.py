# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for /api/v1/connections/{id}/semantic endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from datetime import UTC, datetime
import json
import logging
import math
from pathlib import Path
import re
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
import pytest

from app.database import get_db
from app.datasources.models import (
    ColumnInfo,
    DataSourceSchema,
    PrivacySettings,
    SchemaInfo,
    TableInfo,
)
from app.main import app
from app.semantic import store as semantic_store
from app.semantic.generator import SemanticModelGenerator as _RealGenerator
from app.semantic.models import (
    GENERATION_BATCH_SIZE,
    AggregationType,
    ColumnSemantic,
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


def _locked_header(status: GenerationStatus = GenerationStatus.COMPLETE) -> MagicMock:
    """The model header a check re-reads under the connection lock."""
    return MagicMock(generation_status=status)


class TestCheckDrift:
    """Check Drift introspects live, so a drop shows up without a separate refresh."""

    def setup_method(self) -> None:
        _reset_limiter()

    @pytest.fixture(autouse=True)
    def _baseline_store(self):
        """There is no baseline, so every live table goes through the seeding path; the store
        calls and the lock are mocked so the one-result db queue is never reached."""
        with (
            patch(f"{_STORE}.load_known_columns", new=AsyncMock(return_value={})),
            patch(f"{_STORE}.load_header", new=AsyncMock(return_value=_locked_header())),
            patch(f"{_STORE}.update_known_columns", new=AsyncMock()),
            patch("app.routers.semantic._lock_generation", new=AsyncMock()),
        ):
            yield

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


class TestCheckDriftNewColumns:
    """The check reads the baseline; a table with none is baselined at first sight."""

    def setup_method(self) -> None:
        _reset_limiter()

    @pytest.fixture(autouse=True)
    def _baseline_store(self):
        self.known = AsyncMock(return_value={})
        self.header = AsyncMock(return_value=_locked_header())
        self.known_update = AsyncMock()
        self.lock = AsyncMock()
        with (
            patch(f"{_STORE}.load_known_columns", new=self.known),
            patch(f"{_STORE}.load_header", new=self.header),
            patch(f"{_STORE}.update_known_columns", new=self.known_update),
            patch("app.routers.semantic._lock_generation", new=self.lock),
        ):
            yield

    async def _get(
        self,
        http_client,
        *,
        schema: DataSourceSchema | None = None,
        stored: SemanticModel | None = None,
        conn=None,
        status: GenerationStatus = GenerationStatus.COMPLETE,
        db=None,
        refresh: Callable[..., Awaitable[DataSourceSchema]] | None = None,
    ):
        """GET the drift report; ``status`` applies only to the default stored model, and
        *refresh* replaces the mocked live introspection."""
        db = db or _mock_db(MockResult(single=conn or _make_conn()))  # get_connection_or_404
        app.dependency_overrides[get_db] = lambda: db
        stored = stored or SemanticModel(
            generation_status=status,
            tables={
                "public.orders": TableSemantic(
                    display_name="Orders", columns={"id": ColumnSemantic(display_name="Id")}
                )
            },
        )
        live = schema or _schema(["orders"], {"orders": ["id", "loyalty_tier"]})
        with (
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=stored)),
            patch(
                "app.routers.semantic.refresh_user_schema_cache",
                new=refresh or AsyncMock(return_value=live),
            ),
        ):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 200
        return resp.json()

    async def test_a_column_the_baseline_has_not_seen_is_reported_and_actionable(
        self, http_client
    ) -> None:
        self.known.return_value = {"public.orders": frozenset({"id"})}
        body = await self._get(http_client)
        assert any(
            "1 new column(s)" in w and "public.orders.loyalty_tier" in w for w in body["warnings"]
        )
        assert body["has_actionable_changes"] is True
        self.known_update.assert_not_awaited()

    async def test_a_column_the_baseline_has_seen_is_not_reported(self, http_client) -> None:
        self.known.return_value = {"public.orders": frozenset({"id", "loyalty_tier"})}
        body = await self._get(http_client)
        assert body["warnings"] == []
        assert body["has_actionable_changes"] is False

    async def test_a_table_with_no_baseline_is_baselined_not_reported(self, http_client) -> None:
        body = await self._get(http_client)
        assert body["warnings"] == []
        assert body["has_actionable_changes"] is False
        self.known_update.assert_awaited_once()
        args, kwargs = self.known_update.await_args
        assert args[2] == {"public.orders": frozenset({"id", "loyalty_tier"})}
        assert kwargs == {"only_missing": True}

    async def test_only_the_tables_without_a_baseline_are_written(self, http_client) -> None:
        self.known.return_value = {"public.orders": frozenset({"id"})}
        stored = SemanticModel(
            tables={
                "public.orders": TableSemantic(display_name="Orders"),
                "public.customers": TableSemantic(display_name="Customers"),
            }
        )
        await self._get(
            http_client,
            stored=stored,
            schema=_schema(["orders", "customers"], {"orders": ["id"], "customers": ["id"]}),
        )
        assert self.known_update.await_args.args[2] == {"public.customers": frozenset({"id"})}

    async def test_nothing_is_written_when_every_table_has_a_baseline(self, http_client) -> None:
        self.known.return_value = {"public.orders": frozenset({"id", "loyalty_tier"})}
        await self._get(http_client)
        self.known_update.assert_not_awaited()
        self.lock.assert_not_awaited()

    async def test_a_legacy_near_miss_key_is_reported_as_a_rename_not_a_new_column(
        self, http_client
    ) -> None:
        """No baseline: the live ``status`` is seeded as seen, and the model holds it only
        as ``"Status"``, so the check words it as the rename Apply Drift makes, not as a
        new column."""
        stored = SemanticModel(
            tables={
                "public.orders": TableSemantic(
                    display_name="Orders",
                    columns={
                        "id": ColumnSemantic(display_name="Id"),
                        '"Status"': ColumnSemantic(display_name="Status"),
                    },
                )
            }
        )
        body = await self._get(
            http_client,
            stored=stored,
            schema=_schema(["orders"], {"orders": ["id", "status"]}),
        )
        assert any(
            'Column key "public.orders."Status"" in the semantic model is a near-miss spelling '
            'of "status"; Apply Drift renames it to "status"' in w
            for w in body["warnings"]
        )
        assert not any("new column(s)" in w for w in body["warnings"])
        assert body["has_actionable_changes"] is True

    async def test_an_interrupted_run_is_not_baselined(self, http_client) -> None:
        db = _mock_db(MockResult(single=_make_conn()))
        await self._get(http_client, status=GenerationStatus.TABLES_PARTIAL, db=db)
        self.known_update.assert_not_awaited()
        self.lock.assert_not_awaited()
        db.commit.assert_awaited_once()

    async def test_a_cached_schema_is_not_baselined(self, http_client) -> None:
        """The cache is not what the model has *seen*: a column it lacks must stay new."""
        from datetime import UTC, datetime

        from app.services.schema_utils import SchemaRefreshError, _schema_to_dict

        usc = MagicMock()
        usc.schema_cache = _schema_to_dict(_schema(["orders"]))
        usc.schema_cached_at = datetime(2026, 9, 24, 7, 16, tzinfo=UTC)
        db = _mock_db(MockResult(single=_make_conn()), MockResult(single=usc))
        app.dependency_overrides[get_db] = lambda: db
        stored = SemanticModel(tables={"public.orders": TableSemantic(display_name="Orders")})
        with (
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=stored)),
            patch(
                "app.routers.semantic.refresh_user_schema_cache",
                new=AsyncMock(side_effect=SchemaRefreshError("connection refused")),
            ),
        ):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 200
        assert resp.json()["schema_source"] == "cache"
        self.known_update.assert_not_awaited()
        self.lock.assert_not_awaited()

    async def test_the_schema_refresh_is_committed_before_the_seeding_lock(
        self, http_client
    ) -> None:
        """The refresh writes the schema-cache row and, when the schema changed, deletes cache
        rows; every writer that takes this lock then purges the same rows (a save, a
        suggestion apply, a model delete, a generation batch, a finalisation, an apply).
        Holding them while waiting for the lock can deadlock, so the refresh is committed
        first (as ``apply_drift`` does), then the lock, then the seed."""
        order: list[str] = []

        async def refresh(*_args, **_kwargs) -> DataSourceSchema:
            order.append("refresh")
            return _schema(["orders"], {"orders": ["id", "loyalty_tier"]})

        db = _mock_db(MockResult(single=_make_conn()))
        db.commit.side_effect = lambda: order.append("commit")
        self.lock.side_effect = lambda *_a, **_kw: order.append("lock")

        async def header(*_args, **_kwargs) -> MagicMock:
            order.append("header")
            return _locked_header()

        self.header.side_effect = header
        self.known_update.side_effect = lambda *_a, **_kw: order.append("seed")
        await self._get(http_client, db=db, refresh=refresh)
        # The seeding commit releases the lock; the last commit finds nothing to write.
        assert order == ["refresh", "commit", "lock", "header", "seed", "commit", "commit"]

    async def test_a_run_started_while_the_check_waited_for_the_lock_is_not_baselined(
        self, http_client
    ) -> None:
        """The model was loaded before the introspection; the header re-read under the lock
        sees a Regenerate another tab started in between, whose batches write the baseline."""
        self.header.return_value = _locked_header(GenerationStatus.TABLES_PARTIAL)
        db = _mock_db(MockResult(single=_make_conn()))
        body = await self._get(http_client, db=db)
        self.lock.assert_awaited_once()
        self.known_update.assert_not_awaited()
        # The lock is still released, and the report is the one the check computed.
        assert db.commit.await_count == 3
        assert body["warnings"] == []

    async def test_a_model_deleted_while_the_check_waited_for_the_lock_is_not_baselined(
        self, http_client
    ) -> None:
        self.header.return_value = None
        await self._get(http_client)
        self.known_update.assert_not_awaited()

    async def test_the_seeding_lock_is_the_generation_lock_of_this_connection(
        self, http_client
    ) -> None:
        await self._get(http_client)
        self.lock.assert_awaited_once()
        assert self.lock.await_args.args[0] == "conn-123"
        assert self.lock.await_args.args[2] == "check-drift"

    async def test_a_hidden_column_is_neither_reported_nor_baselined(self, http_client) -> None:
        conn = _make_conn(privacy_settings={"excluded_columns": ["public.orders.ssn"]})
        schema = _schema(["orders"], {"orders": ["id", "ssn"]})
        self.known.return_value = {"public.orders": frozenset({"id"})}
        body = await self._get(http_client, conn=conn, schema=schema)
        assert body["warnings"] == []
        self.known.return_value = {}
        await self._get(http_client, conn=conn, schema=schema)
        assert self.known_update.await_args.args[2] == {"public.orders": frozenset({"id"})}

    async def test_a_hidden_key_the_model_stores_is_seeded_under_its_real_name(
        self, http_client
    ) -> None:
        """So once un-hidden, the quoted key is renamed to ``ssn`` instead of ``ssn`` being
        reported new."""
        conn = _make_conn(privacy_settings={"excluded_columns": ["public.orders.ssn"]})
        stored = SemanticModel(
            tables={
                "public.orders": TableSemantic(
                    display_name="Orders",
                    columns={
                        "id": ColumnSemantic(display_name="Id"),
                        '"ssn"': ColumnSemantic(display_name="SSN"),
                    },
                )
            }
        )
        body = await self._get(
            http_client, conn=conn, stored=stored, schema=_schema(["orders"], {"orders": ["id"]})
        )
        assert body["warnings"] == []
        assert self.known_update.await_args.args[2] == {"public.orders": frozenset({"id", "ssn"})}


class TestCheckDriftSeedingLock:
    """The seeding write takes the real generation lock, so a vanished connection is a 409."""

    def setup_method(self) -> None:
        _reset_limiter()

    async def test_a_connection_deleted_during_the_check_is_409_and_seeds_nothing(
        self, http_client
    ) -> None:
        # get_connection_or_404 finds the connection; the lock's re-read does not.
        db = _mock_db(MockResult(single=_make_conn()), MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        stored = SemanticModel(tables={"public.orders": TableSemantic(display_name="Orders")})
        known_update = AsyncMock()
        with (
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=stored)),
            patch(f"{_STORE}.load_known_columns", new=AsyncMock(return_value={})),
            patch(f"{_STORE}.update_known_columns", new=known_update),
            patch(
                "app.routers.semantic.refresh_user_schema_cache",
                new=AsyncMock(return_value=_schema(["orders"])),
            ),
        ):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 409
        assert "Connection was deleted during check-drift" in resp.json()["detail"]
        # The refreshed cache was committed before the lock; the failed lock rolled back.
        db.commit.assert_awaited_once()
        db.rollback.assert_awaited_once()
        known_update.assert_not_awaited()


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

    @pytest.fixture(autouse=True)
    def _known_update(self):
        """The baseline write and the stored column keys it reads are mocked in every batch
        test; ``self.known_update`` and ``self.column_names`` are they (no column stored by
        default)."""
        with (
            patch(f"{_STORE}.update_known_columns", new=AsyncMock(return_value=None)) as mocked,
            patch(f"{_STORE}.load_column_names", new=AsyncMock(return_value={})) as names,
        ):
            self.known_update = mocked
            self.column_names = names
            yield

    @pytest.fixture(autouse=True)
    def _invalidate(self):
        """The cache purge is mocked in every batch test; ``self.invalidate`` is it."""
        with patch(
            "app.routers.semantic._invalidate_connection_caches",
            new=AsyncMock(return_value=None),
        ) as mocked:
            self.invalidate = mocked
            yield

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

    async def test_a_batch_records_the_live_columns_of_the_tables_it_reconciled(
        self, http_client
    ) -> None:
        generated = {"public.orders": TableSemantic(display_name="Orders")}
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches()
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(
                mock_load,
                mock_gen,
                generated,
                schema=_schema(["orders", "customers"], columns={"orders": ["id", "status"]}),
            )
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})

        assert resp.status_code == 200
        args = self.known_update.await_args.args
        assert args[1] == "conn-123"
        # The live columns, annotated or not: ``generated`` annotates none of them, and
        # ``customers`` is not in the batch.
        assert args[2] == {"public.orders": {"id", "status"}}

    async def test_a_batch_records_the_real_name_of_a_stored_key_the_policy_hides(
        self, http_client
    ) -> None:
        """The reconciled table keeps the row the policy hides; the baseline keeps its
        real name, so the column is renamed, not re-annotated, once un-hidden."""
        generated = {"public.orders": TableSemantic(display_name="Orders")}
        conn = _make_conn(privacy_settings={"excluded_columns": ["public.orders.ssn"]})
        self.column_names.return_value = {"public.orders": ["id", '"ssn"']}
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches()
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(
                mock_load,
                mock_gen,
                generated,
                schema=_schema(["orders"], columns={"orders": ["id", "status"]}),
            )
            mock_load.return_value = (mock_load.return_value[0], conn)
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})

        assert resp.status_code == 200
        assert self.column_names.await_args.args[1:] == ("conn-123", {"public.orders"})
        assert self.known_update.await_args.args[2] == {"public.orders": {"id", "status", "ssn"}}

    async def test_a_table_the_llm_omitted_gets_no_baseline_entry(self, http_client) -> None:
        generated = {"public.orders": TableSemantic(display_name="Orders")}
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches()
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, generated)
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(
                _BATCH_URL, json={"table_keys": ["public.orders", "public.customers"]}
            )

        assert resp.status_code == 200
        # It stays pending, so the model has not seen it yet.
        assert set(self.known_update.await_args.args[2]) == {"public.orders"}

    async def test_a_batch_whose_reconcile_conflicts_records_nothing(self, http_client) -> None:
        reconcile = AsyncMock(side_effect=semantic_store.SemanticStoreConflict("superseded"))
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(reconcile=reconcile)
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, {"public.orders": TableSemantic(display_name="O")})
            db = _mock_db(MockResult(single=_make_conn()))
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})

        assert resp.status_code == 409
        self.known_update.assert_not_awaited()
        db.commit.assert_not_awaited()

    async def test_a_batch_from_a_superseded_run_records_nothing(self, http_client) -> None:
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(
            headers=[
                _header(GenerationStatus.TABLES_PARTIAL, "h", run_id="run-1"),
                _header(GenerationStatus.TABLES_PARTIAL, "h", run_id="run-2"),
            ],
        )
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, {"public.orders": TableSemantic(display_name="O")})
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})

        assert resp.status_code == 409
        self.known_update.assert_not_awaited()

    async def test_a_baseline_write_value_error_is_400_not_500(self, http_client) -> None:
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches()
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, {"public.orders": TableSemantic(display_name="O")})
            self.known_update.side_effect = ValueError("table_key 'x...' is 600 characters")
            db = _mock_db(MockResult(single=_make_conn()))
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 400
        assert "600 characters" in resp.json()["detail"]
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_a_batch_purges_the_cached_answers_but_keeps_the_schema_cache(
        self, http_client
    ) -> None:
        order: list[str] = []
        reconcile = AsyncMock(side_effect=lambda *_a, **_k: order.append("reconcile"))
        self.known_update.side_effect = lambda *_a, **_k: order.append("baseline")
        self.invalidate.side_effect = lambda *_a, **_k: order.append("purge")
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(reconcile=reconcile)
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, {"public.orders": TableSemantic(display_name="O")})
            db = _mock_db(MockResult(single=_make_conn()))
            db.commit.side_effect = lambda: order.append("commit")
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})

        assert resp.status_code == 200
        # Purged in the batch's own transaction, after its writes succeeded.
        assert order == ["reconcile", "baseline", "purge", "commit"]
        self.invalidate.assert_awaited_once_with("conn-123", db, keep_schema=True)

    async def test_a_batch_that_conflicts_purges_nothing(self, http_client) -> None:
        reconcile = AsyncMock(side_effect=semantic_store.SemanticStoreConflict("superseded"))
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(reconcile=reconcile)
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, {"public.orders": TableSemantic(display_name="O")})
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})

        assert resp.status_code == 409
        self.invalidate.assert_not_awaited()

    async def test_a_batch_whose_store_write_fails_purges_nothing(self, http_client) -> None:
        reconcile = AsyncMock(side_effect=ValueError("table_key 'x...' is 600 characters"))
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(reconcile=reconcile)
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, {"public.orders": TableSemantic(display_name="O")})
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})

        assert resp.status_code == 400
        self.invalidate.assert_not_awaited()

    async def test_a_batch_whose_baseline_write_fails_purges_nothing(self, http_client) -> None:
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches()
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, {"public.orders": TableSemantic(display_name="O")})
            self.known_update.side_effect = ValueError("table_key 'x...' is 600 characters")
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})

        assert resp.status_code == 400
        self.invalidate.assert_not_awaited()

    async def test_a_batch_the_llm_answered_nothing_for_purges_nothing(self, http_client) -> None:
        """Every table stays pending and no annotation changed, so the cached answers
        still match the model."""
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches()
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, {})
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})

        assert resp.status_code == 200
        assert resp.json()["omitted_table_keys"] == ["public.orders"]
        self.invalidate.assert_not_awaited()

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
        known: dict[str, frozenset[str]] | None = None,
    ):
        """POST /generate/globals with the store and LLM mocked; returns (resp, save, gen).

        ``save`` echoes the model it is given unless the test configured what it
        returns (or raises): the route answers with ``save_model``'s result.
        ``headers`` is what ``load_header`` returns before the LLM call and after the
        lock; by default the model's own status with run id ``run-1`` both times.
        ``known`` is the baseline the finalisation reads (none by default) and
        ``self.known_update`` the write it makes.
        """
        self.known_update = AsyncMock(return_value=None)
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
            patch(f"{_STORE}.load_known_columns", new=AsyncMock(return_value=known or {})),
            patch(f"{_STORE}.update_known_columns", new=self.known_update),
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

    async def test_finalising_baselines_unreached_tables_and_drops_gone_columns_and_tables(
        self, http_client
    ) -> None:
        partial = self._partial(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"id": {"display_name": "Id"}},
                },
                "public.unchanged": {"display_name": "Unchanged", "columns": {}},
                "public.events": {"display_name": "Events", "columns": {}},
                "public.stale": {"display_name": "Stale", "columns": {}},
            }
        )
        schema = _schema(["orders", "unchanged", "events"])
        resp, save, _ = await self._run(
            http_client,
            model=partial,
            schema=schema,
            known={
                # ``old`` left the database: the entry loses it.
                "public.orders": frozenset({"id", "old"}),
                "public.unchanged": frozenset({"id"}),
                # ``stale`` is no longer stored: its entry is not rewritten, the prune drops it.
                "public.stale": frozenset({"id"}),
            },
        )

        assert resp.status_code == 200
        assert set(save.await_args.args[2].tables) == {
            "public.orders",
            "public.unchanged",
            "public.events",
        }
        args, kwargs = self.known_update.await_args
        assert args[1] == "conn-123"
        assert args[2]["public.orders"] == {"id"}
        # A stored table the run never reached: seeded with its live columns.
        assert args[2]["public.events"] == {"id"}
        # Only what changed is written: a table whose entry is already right is not in it.
        assert "public.unchanged" not in args[2]
        assert "public.stale" not in args[2]
        assert kwargs == {"prune": True}

    async def test_a_hand_added_column_the_baseline_lacks_is_recorded(self, http_client) -> None:
        partial = self._partial(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"id": {"display_name": "Id"}, "extra": {"display_name": "Hand"}},
                }
            }
        )
        resp, _, _ = await self._run(
            http_client,
            model=partial,
            schema=_schema(["orders"], columns={"orders": ["id", "extra"]}),
            known={"public.orders": frozenset({"id"})},
        )

        assert resp.status_code == 200
        # What the model stores it has seen: the column is not "new" at the next check.
        assert self.known_update.await_args.args[2] == {"public.orders": {"id", "extra"}}

    async def test_an_unreadable_table_is_left_out_of_the_baseline_write(self, http_client) -> None:
        """An unreadable table is absent from the live schema but stays stored, so the
        finalisation neither rewrites its entry nor lets the prune delete it."""
        partial = self._partial(
            tables={
                "public.orders": {"display_name": "Orders", "columns": {}},
                "public.payroll": {"display_name": "Payroll", "columns": {}},
            }
        )
        schema = _schema(["orders"])
        schema.unreadable_tables = ["public.payroll"]
        resp, save, _ = await self._run(
            http_client,
            model=partial,
            schema=schema,
            known={"public.payroll": frozenset({"id", "salary"})},
        )

        assert resp.status_code == 200
        assert "public.payroll" in save.await_args.args[2].tables
        assert self.known_update.await_args.args[2] == {"public.orders": {"id"}}

    async def test_a_table_the_policy_hides_is_left_out_of_the_baseline_write(
        self, http_client
    ) -> None:
        """A table the privacy policy hides is absent from the live schema but stays
        stored, so the finalisation neither rewrites its entry nor lets the prune delete it."""
        partial = self._partial(
            tables={
                "public.orders": {"display_name": "Orders", "columns": {}},
                "public.payroll": {"display_name": "Payroll", "columns": {}},
            }
        )
        resp, save, _ = await self._run(
            http_client,
            model=partial,
            schema=_schema(["orders"]),  # what the loader returns with payroll excluded
            conn=_make_conn(privacy_settings={"excluded_tables": ["public.payroll"]}),
            known={"public.payroll": frozenset({"id", "salary"})},
        )

        assert resp.status_code == 200
        assert "public.payroll" in save.await_args.args[2].tables
        assert self.known_update.await_args.args[2] == {"public.orders": {"id"}}

    async def test_a_hidden_column_stays_in_the_baseline_only_if_the_model_stores_it(
        self, http_client
    ) -> None:
        """The schema the route sees never introspected the hidden column. One the model
        never annotated leaves the entry, so it is reported new once un-hidden; one it
        stores stays seen under its real name."""
        partial = self._partial(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"id": {"display_name": "Id"}},
                },
                "public.customers": {
                    "display_name": "Customers",
                    "columns": {
                        "id": {"display_name": "Id"},
                        "ssn": {"display_name": "Hand-annotated"},
                    },
                },
            }
        )
        resp, save, _ = await self._run(
            http_client,
            model=partial,
            schema=_schema(["orders", "customers"]),  # ``ssn`` was never introspected
            conn=_make_conn(
                privacy_settings={"excluded_columns": ["public.orders.ssn", "public.customers.ssn"]}
            ),
            known={
                "public.orders": frozenset({"id", "ssn"}),
                "public.customers": frozenset({"id", "ssn"}),
            },
        )

        assert resp.status_code == 200
        # The save keeps the annotation the policy hides, and the baseline its name.
        assert "ssn" in save.await_args.args[2].tables["public.customers"].columns
        assert self.known_update.await_args.args[2] == {"public.orders": {"id"}}

    @staticmethod
    def _orders_holding(*column_keys: str) -> SemanticModel:
        """A partial model whose ``orders`` stores the given column keys (live: ``id``,
        ``status``)."""
        return TestGenerateGlobals._partial(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {key: {"display_name": key} for key in column_keys},
                }
            }
        )

    async def test_a_hidden_quoted_key_is_recorded_under_its_real_name(self, http_client) -> None:
        resp, save, _ = await self._run(
            http_client,
            model=self._orders_holding("id", '"ssn"'),
            schema=_schema(["orders"]),  # ``ssn`` is hidden
            conn=_make_conn(privacy_settings={"excluded_columns": ["public.orders.ssn"]}),
            known={"public.orders": frozenset({"id"})},
        )

        assert resp.status_code == 200
        assert '"ssn"' in save.await_args.args[2].tables["public.orders"].columns
        assert self.known_update.await_args.args[2] == {"public.orders": {"id", "ssn"}}

    @pytest.mark.parametrize("near_miss", ['"Status"', "Status"], ids=["quoted", "case-only"])
    async def test_a_column_the_run_did_not_annotate_is_renamed_from_its_near_miss_key(
        self, http_client, near_miss
    ) -> None:
        """The run left ``status`` unannotated and the model holds it only under a
        near-miss key: the key is renamed to ``status`` with its annotation and its hand
        edits, before the prune could drop it, and the baseline keeps ``status``."""
        resp, save, _ = await self._run(
            http_client,
            model=self._orders_holding("id", near_miss),
            schema=_schema(["orders"], columns={"orders": ["id", "status"]}),
            provenance={
                **_no_provenance(),
                "table": {"public.orders": ["primary_timestamp_column"]},
                "column": {f"public.orders.{near_miss}": ["display_name"]},
            },
            protected={
                "table": {"public.orders": {"primary_timestamp_column": near_miss}},
                "column": {f"public.orders.{near_miss}": {"display_name": near_miss}},
            },
            known={"public.orders": frozenset({"id", "status"})},
        )

        assert resp.status_code == 200
        columns = save.await_args.args[2].tables["public.orders"].columns
        assert set(columns) == {"id", "status"}
        assert columns["status"].display_name == near_miss
        kwargs = save.await_args.kwargs
        assert kwargs["provenance"]["column"] == {"public.orders.status": ["display_name"]}
        assert kwargs["protected"]["column"] == {
            "public.orders.status": {"display_name": near_miss}
        }
        # The hand-set reference follows the rename instead of being cleared as dangling.
        assert kwargs["provenance"]["table"] == {"public.orders": ["primary_timestamp_column"]}
        assert kwargs["protected"]["table"] == {
            "public.orders": {"primary_timestamp_column": "status"}
        }
        # ``status`` stays seen: the baseline already says so, so nothing is written.
        assert self.known_update.await_args.args[2] == {}

    async def test_a_seeded_table_renames_a_column_held_only_under_a_near_miss_key(
        self, http_client
    ) -> None:
        """No baseline entry: the table is seeded with its live columns, so ``status`` is
        seen and its near-miss key renamed, as Check Drift would report it."""
        resp, save, _ = await self._run(
            http_client,
            model=self._orders_holding("id", '"Status"'),
            schema=_schema(["orders"], columns={"orders": ["id", "status"]}),
            known={},
        )

        assert resp.status_code == 200
        assert set(save.await_args.args[2].tables["public.orders"].columns) == {"id", "status"}
        assert self.known_update.await_args.args[2] == {"public.orders": {"id", "status"}}

    async def test_a_near_miss_key_beside_its_annotated_real_name_is_dropped_as_a_duplicate(
        self, http_client
    ) -> None:
        """The run annotated ``status`` (or it was stored already): the near-miss key is a
        duplicate, dropped by the prune, not renamed over the stored column."""
        resp, save, _ = await self._run(
            http_client,
            model=self._orders_holding("id", "status", '"Status"'),
            schema=_schema(["orders"], columns={"orders": ["id", "status"]}),
            known={"public.orders": frozenset({"id", "status"})},
        )

        assert resp.status_code == 200
        columns = save.await_args.args[2].tables["public.orders"].columns
        assert set(columns) == {"id", "status"}
        assert columns["status"].display_name == "status"
        # ``status`` keeps its annotation and the baseline entry already says so: no write.
        assert self.known_update.await_args.args[2] == {}

    async def test_the_baseline_is_written_before_the_commit(self, http_client) -> None:
        db = _mock_db(MockResult(single=_make_conn()), MockResult(), MockResult(), MockResult())

        async def write_before_commit(*_args, **_kwargs) -> None:
            db.commit.assert_not_awaited()

        known_update = AsyncMock(side_effect=write_before_commit)
        resp, _, _ = await self._run(
            http_client,
            model=self._partial(),
            schema=_schema(["orders"]),
            db=db,
            extra_patches=(patch(f"{_STORE}.update_known_columns", new=known_update),),
        )

        assert resp.status_code == 200
        known_update.assert_awaited_once()
        db.commit.assert_awaited_once()

    async def test_a_failed_save_records_nothing(self, http_client) -> None:
        resp, _, _ = await self._run(
            http_client,
            model=self._partial(),
            schema=_schema(["orders"]),
            save=AsyncMock(side_effect=ValueError("from_table 'x...' is 600 characters")),
        )

        assert resp.status_code == 400
        self.known_update.assert_not_awaited()


class TestBaselineChanges:
    def test_only_new_and_different_entries_are_returned(self) -> None:
        from app.routers.semantic import _baseline_changes

        known = {"a": frozenset({"x"}), "b": frozenset({"x", "y"})}
        wanted = {"a": ["x"], "b": ["x"], "c": ["z"]}
        assert _baseline_changes(known, wanted) == {"b": {"x"}, "c": {"z"}}


class TestHeldColumnNames:
    """What a baseline write records because the model stores it."""

    _PRIVACY = PrivacySettings(excluded_columns=["public.orders.ssn"])

    @staticmethod
    def _held(stored: list[str], privacy: PrivacySettings | None) -> set[str]:
        from app.routers.semantic import _held_column_names

        return _held_column_names("public.orders", ["id", "status"], stored, privacy)

    def test_a_stored_live_column_is_held_and_an_unstored_one_is_not(self) -> None:
        assert self._held(["id"], None) == {"id"}

    def test_a_stored_key_that_left_the_database_is_not_held(self) -> None:
        assert self._held(["id", "gone"], None) == {"id"}

    @pytest.mark.parametrize("stored_key", ["ssn", '"ssn"'], ids=["real-name", "quoted"])
    def test_a_stored_key_the_policy_hides_is_held_under_its_real_name(
        self, stored_key: str
    ) -> None:
        assert self._held(["id", stored_key], self._PRIVACY) == {"id", "ssn"}

    def test_a_case_only_key_of_a_hidden_column_is_not_held(self) -> None:
        """The policy's match is exact (quotes aside), so ``SSN`` is not hidden: the prune
        drops it as a gone key, and the baseline does not record it."""
        assert self._held(["id", "SSN"], self._PRIVACY) == {"id"}

    def test_without_a_policy_nothing_is_hidden(self) -> None:
        assert self._held(["id", '"ssn"'], None) == {"id"}


class TestEffectiveKnownColumns:
    def test_a_stored_key_the_policy_hides_is_seeded_under_its_real_name(self) -> None:
        """A table with no entry is baselined with what a baseline write would record:
        its live columns and the real name of each stored key the policy hides."""
        from app.routers.semantic import _effective_known_columns

        schema = _schema(["orders"], columns={"orders": ["id"]})  # ``ssn`` is hidden
        stored = {
            "public.orders": TableSemantic(
                display_name="Orders", columns={'"ssn"': ColumnSemantic(display_name="S")}
            )
        }
        privacy = PrivacySettings(excluded_columns=["public.orders.ssn"])
        assert _effective_known_columns({}, stored, schema, privacy) == {
            "public.orders": frozenset({"id", "ssn"})
        }

    def test_a_stored_table_with_no_entry_gets_its_live_columns(self) -> None:
        from app.routers.semantic import _effective_known_columns

        schema = _schema(["orders"], columns={"orders": ["id", "status"]})
        stored = {"public.orders": TableSemantic(display_name="Orders")}
        assert _effective_known_columns({}, stored, schema, None) == {
            "public.orders": frozenset({"id", "status"})
        }

    def test_an_existing_entry_is_kept_even_when_empty(self) -> None:
        from app.routers.semantic import _effective_known_columns

        schema = _schema(["orders"], columns={"orders": ["id", "status"]})
        stored = {"public.orders": TableSemantic(display_name="Orders")}
        known = {"public.orders": frozenset()}
        assert _effective_known_columns(known, stored, schema, None) == known

    def test_a_stored_table_missing_from_the_live_schema_gets_no_entry(self) -> None:
        from app.routers.semantic import _effective_known_columns

        stored = {"public.ghosts": TableSemantic(display_name="Ghosts")}
        assert _effective_known_columns({}, stored, _schema(["orders"]), None) == {}

    def test_a_live_table_the_model_does_not_hold_gets_no_entry(self) -> None:
        from app.routers.semantic import _effective_known_columns

        assert _effective_known_columns({}, {}, _schema(["orders"]), None) == {}

    def test_entries_of_tables_that_are_not_stored_pass_through(self) -> None:
        from app.routers.semantic import _effective_known_columns

        known = {"public.legacy": frozenset({"id"})}
        assert _effective_known_columns(known, {}, _schema(["orders"]), None) == known


class TestLiveColumns:
    def test_names_the_live_columns_of_every_table(self) -> None:
        from app.routers.semantic import _live_columns

        schema = _schema(["orders", "customers"], columns={"orders": ["id", "status"]})
        assert _live_columns(schema) == {
            "public.orders": ["id", "status"],
            "public.customers": ["id"],
        }

    def test_keys_limit_it_to_the_tables_named(self) -> None:
        from app.routers.semantic import _live_columns

        schema = _schema(["orders", "customers"])
        assert _live_columns(schema, {"public.customers", "public.ghost"}) == {
            "public.customers": ["id"]
        }


class TestWithNewColumns:
    @staticmethod
    def _orders() -> dict[str, TableSemantic]:
        return {
            "public.orders": TableSemantic(
                display_name="Orders", columns={"id": ColumnSemantic(display_name="Id")}
            )
        }

    def test_adds_a_missing_column_to_a_held_table(self) -> None:
        from app.routers.semantic import _with_new_columns

        out = _with_new_columns(
            self._orders(), {"public.orders": {"tier": ColumnSemantic(display_name="Tier")}}
        )
        assert set(out["public.orders"].columns) == {"id", "tier"}

    def test_never_replaces_an_existing_column(self) -> None:
        from app.routers.semantic import _with_new_columns

        out = _with_new_columns(
            self._orders(),
            {
                "public.orders": {
                    "id": ColumnSemantic(display_name="Overwritten"),
                    "tier": ColumnSemantic(display_name="Tier"),
                }
            },
        )
        assert out["public.orders"].columns["id"].display_name == "Id"

    def test_a_table_whose_columns_all_exist_is_returned_unchanged(self) -> None:
        from app.routers.semantic import _with_new_columns

        tables = self._orders()
        out = _with_new_columns(
            tables, {"public.orders": {"id": ColumnSemantic(display_name="Overwritten")}}
        )
        assert out["public.orders"] is tables["public.orders"]

    def test_a_table_the_model_does_not_hold_gets_nothing(self) -> None:
        from app.routers.semantic import _with_new_columns

        out = _with_new_columns(
            self._orders(), {"public.ghost": {"a": ColumnSemantic(display_name="A")}}
        )
        assert set(out) == {"public.orders"}

    def test_does_not_mutate_its_input(self) -> None:
        from app.routers.semantic import _with_new_columns

        tables = self._orders()
        _with_new_columns(tables, {"public.orders": {"tier": ColumnSemantic(display_name="Tier")}})
        assert set(tables["public.orders"].columns) == {"id"}

    def test_nothing_to_add_returns_the_tables(self) -> None:
        from app.routers.semantic import _with_new_columns

        tables = {"public.orders": TableSemantic(display_name="Orders")}
        assert _with_new_columns(tables, {}) == tables


class TestPlanColumnBatches:
    @staticmethod
    def _cols(n: int) -> set[str]:
        return {f"c{i:03d}" for i in range(n)}

    @staticmethod
    def _plan(added, order, *, max_batches=3, ddl_sizes=None, ddl_limit=10**9):
        from app.routers.semantic import _plan_column_batches

        return _plan_column_batches(
            added, order, max_batches=max_batches, ddl_sizes=ddl_sizes or {}, ddl_limit=ddl_limit
        )

    def test_tables_pack_into_a_batch_until_the_column_cap(self) -> None:
        from app.routers.semantic import _DRIFT_MAX_COLUMNS_PER_BATCH as CAP

        added = {"a": self._cols(CAP // 2 + 1), "b": self._cols(CAP // 2 + 1), "c": {"x"}}
        batches, deferred, cut = self._plan(added, ["a", "b", "c"])
        assert [list(b) for b in batches] == [["a"], ["b", "c"]]
        assert deferred == [] and cut == {}

    def test_a_batch_holds_at_most_a_generation_batch_of_tables(self) -> None:
        keys = [f"t{i:02d}" for i in range(GENERATION_BATCH_SIZE + 1)]
        batches, _, _ = self._plan({k: {"x"} for k in keys}, keys)
        assert [list(b) for b in batches] == [keys[:GENERATION_BATCH_SIZE], keys[-1:]]

    def test_a_table_wider_than_the_cap_is_cut_to_the_cap_and_the_cut_is_reported(self) -> None:
        from app.routers.semantic import _DRIFT_MAX_COLUMNS_PER_BATCH as CAP

        batches, _, cut = self._plan({"a": self._cols(CAP + 10)}, ["a"])
        assert batches == [{"a": sorted(self._cols(CAP))}]
        assert cut == {"a": sorted(self._cols(CAP + 10))[CAP:]}

    def test_tables_past_the_batch_budget_are_deferred_in_order(self) -> None:
        from app.routers.semantic import _DRIFT_MAX_COLUMNS_PER_BATCH as CAP

        added = {k: self._cols(CAP // 2 + 1) for k in "abc"}
        batches, deferred, _ = self._plan(added, ["a", "b", "c"], max_batches=1)
        assert [list(b) for b in batches] == [["a"]]
        assert deferred == ["b", "c"]

    def test_no_budget_defers_everything(self) -> None:
        assert self._plan({"a": {"x"}}, ["a"], max_batches=0) == ([], ["a"], {})

    def test_tables_whose_ddl_does_not_fit_one_prompt_go_in_separate_batches(self) -> None:
        added = {"a": {"x"}, "b": {"x"}, "c": {"x"}}
        batches, _, _ = self._plan(
            added, ["a", "b", "c"], ddl_sizes={"a": 12_000, "b": 12_000, "c": 100}, ddl_limit=20_000
        )
        assert [list(b) for b in batches] == [["a"], ["b", "c"]]

    def test_ddl_that_fills_the_limit_exactly_still_fits(self) -> None:
        """``_schema_to_ddl`` drops a table only past the limit, and counts the blocks
        without the newlines that join them: the plain sum is compared."""
        batches, _, _ = self._plan(
            {"a": {"x"}, "b": {"x"}},
            ["a", "b"],
            ddl_sizes={"a": 15_000, "b": 5_000},
            ddl_limit=20_000,
        )
        assert [list(b) for b in batches] == [["a", "b"]]

    def test_a_table_whose_ddl_alone_exceeds_the_limit_is_still_sent_alone(self) -> None:
        batches, deferred, _ = self._plan(
            {"a": {"x"}, "b": {"x"}},
            ["a", "b"],
            ddl_sizes={"a": 30_000, "b": 100},
            ddl_limit=20_000,
        )
        assert [list(b) for b in batches] == [["a"], ["b"]]
        assert deferred == []

    def test_an_oversized_table_between_others_is_sent_alone_and_none_is_dropped(self) -> None:
        """The prompt renders no table after one past the limit, so the oversized table
        must not share a batch with the tables after it either."""
        batches, deferred, _ = self._plan(
            {"a": {"x"}, "b": {"x"}, "c": {"x"}},
            ["a", "b", "c"],
            ddl_sizes={"a": 100, "b": 30_000, "c": 100},
            ddl_limit=20_000,
        )
        assert [list(b) for b in batches] == [["a"], ["b"], ["c"]]
        assert deferred == []


class TestRebuildRelationshipsNewColumns:
    """A catalog edge the model lacks is added when it is on a new column; one on an
    existing column is not."""

    @staticmethod
    def _rebuild(
        catalog: list[RelationshipEdge],
        columns: dict[str, list[str]],
        new_columns: dict[str, set[str]] | None,
    ) -> list[RelationshipEdge]:
        from app.routers.semantic import _rebuild_relationships

        return _rebuild_relationships(
            [],
            catalog,
            _snapshot().marks,
            _schema(["orders", "customers"], columns),
            None,
            render_tables={"public.orders", "public.customers"},
            keep_hidden_endpoints=False,
            new_tables=set(),
            new_columns=new_columns,
        )

    @classmethod
    def _from_edges(cls, new_columns: dict[str, set[str]] | None) -> list[RelationshipEdge]:
        edge = TestApplyDrift._edge
        return cls._rebuild(
            [
                edge("public.orders", "customer_id", "public.customers"),
                edge("public.orders", "region_id", "public.customers"),
            ],
            {"orders": ["id", "customer_id", "region_id"], "customers": ["id"]},
            new_columns,
        )

    def test_an_edge_on_a_new_column_is_added_and_one_on_an_old_column_is_not(self) -> None:
        rebuilt = self._from_edges({"public.orders": {"region_id"}})
        assert [e.from_column for e in rebuilt] == ["region_id"]

    def test_without_new_columns_neither_is_added(self) -> None:
        assert self._from_edges(None) == []

    def test_an_edge_to_a_new_column_is_added(self) -> None:
        to_new = RelationshipEdge(
            from_table="public.orders",
            from_column="customer_id",
            to_table="public.customers",
            to_column="code",
            join_sql="public.orders.customer_id = public.customers.code",
        )
        rebuilt = self._rebuild(
            [to_new],
            {"orders": ["id", "customer_id"], "customers": ["id", "code"]},
            {"public.customers": {"code"}},
        )
        assert rebuilt == [to_new]


class TestDriftAppliedModelNewColumns:
    """``_drift_applied_model`` over a drift from ``compute_drift``: the supplied
    annotations of new columns are added additively, and catalog FK edges follow the
    drift's new columns that the apply records."""

    @staticmethod
    def _apply(
        orders: dict[str, str],
        *,
        known: dict[str, list[str]],
        catalog: list[RelationshipEdge],
        new_columns: dict[str, dict[str, ColumnSemantic]],
        recorded: dict[str, set[str]],
    ) -> SemanticModel:
        """Apply the drift of a model holding ``orders`` (column key → display name) and
        a ``customers`` table holding ``id`` (no relationships) against a live schema
        where orders has ``id``, ``customer_id``, ``status`` and ``region_id``."""
        from app.routers.semantic import _drift_applied_model
        from app.semantic.drift import compute_drift

        schema = _schema(
            ["orders", "customers"],
            {"orders": ["id", "customer_id", "status", "region_id"], "customers": ["id"]},
        )
        model = SemanticModel.model_validate(
            {
                **_EMPTY_MODEL,
                "tables": {
                    "public.orders": {
                        "display_name": "Orders",
                        "columns": {
                            name: {"display_name": label} for name, label in orders.items()
                        },
                    },
                    "public.customers": {
                        "display_name": "Customers",
                        "columns": {"id": {"display_name": "Id"}},
                    },
                },
            }
        )
        drift = compute_drift(schema, model, None, known)
        return _drift_applied_model(
            model,
            _snapshot(),
            schema=schema,
            privacy=None,
            drift=drift,
            dialect="postgresql",
            gen=_RealGenerator(),
            relationship_edges=catalog,
            new_tables={},
            new_columns=new_columns,
            recorded_columns=recorded,
            llm_items=([], [], [], []),
            connection_id="conn-123",
            log=False,
        ).model

    def test_a_near_miss_key_is_renamed_with_its_annotation(self) -> None:
        model = self._apply(
            {"id": "Id", '"status"': "Hand-written"},
            known={"public.orders": ["id", "status"]},
            catalog=[],
            new_columns={},
            recorded={},
        )
        columns = model.tables["public.orders"].columns
        assert set(columns) == {"id", "status"}
        assert columns["status"].display_name == "Hand-written"

    def test_a_supplied_column_the_model_holds_keeps_its_stored_annotation(self) -> None:
        model = self._apply(
            {"id": "Stored"},
            known={"public.orders": ["id", "customer_id", "status", "region_id"]},
            catalog=[],
            new_columns={"public.orders": {"id": ColumnSemantic(display_name="Overwritten")}},
            recorded={},
        )
        assert model.tables["public.orders"].columns["id"].display_name == "Stored"

    def test_a_deleted_edge_on_a_renamed_column_is_not_re_added(self) -> None:
        """The model held ``customer_id`` already, under a stale key: a catalog edge on it
        that the model lacks is one the user deleted."""
        model = self._apply(
            {"id": "Id", '"customer_id"': "Customer"},
            known={"public.orders": ["id", "customer_id", "status", "region_id"]},
            catalog=[TestApplyDrift._edge("public.orders", "customer_id", "public.customers")],
            new_columns={},
            recorded={"public.orders": {"customer_id"}},
        )
        assert model.tables["public.orders"].columns["customer_id"].display_name == "Customer"
        assert model.relationships == []

    def test_an_edge_on_a_recorded_new_column_is_added_though_the_llm_omitted_it(self) -> None:
        model = self._apply(
            {"id": "Id", "customer_id": "Customer", "status": "Status"},
            known={"public.orders": ["id", "customer_id", "status"]},
            catalog=[TestApplyDrift._edge("public.orders", "region_id", "public.customers")],
            new_columns={},
            recorded={"public.orders": {"region_id"}},
        )
        assert "region_id" not in model.tables["public.orders"].columns
        assert [e.from_column for e in model.relationships] == ["region_id"]

    def test_an_edge_on_a_new_column_that_is_not_recorded_is_not_added(self) -> None:
        # Its batch failed, or it was deferred or cut: it stays new, and its edge waits
        # for the apply that records it.
        catalog = [TestApplyDrift._edge("public.orders", "region_id", "public.customers")]
        for recorded in ({}, {"public.orders": set()}, {"public.orders": {"status"}}):
            model = self._apply(
                {"id": "Id", "customer_id": "Customer", "status": "Status"},
                known={"public.orders": ["id", "customer_id", "status"]},
                catalog=catalog,
                new_columns={},
                recorded=recorded,
            )
            assert model.relationships == []

    def test_a_recorded_column_that_drift_does_not_report_as_new_does_not_widen_the_rule(
        self,
    ) -> None:
        model = self._apply(
            {"id": "Id", "customer_id": "Customer", "status": "Status"},
            known={"public.orders": ["id", "customer_id", "status", "region_id"]},
            catalog=[TestApplyDrift._edge("public.orders", "customer_id", "public.customers")],
            new_columns={},
            recorded={"public.orders": {"customer_id"}},
        )
        assert model.relationships == []

    def test_a_missing_edge_on_an_existing_column_is_not_re_added(self) -> None:
        model = self._apply(
            {"id": "Id", "customer_id": "Customer", "status": "Status"},
            known={"public.orders": ["id", "customer_id", "status", "region_id"]},
            catalog=[TestApplyDrift._edge("public.orders", "customer_id", "public.customers")],
            new_columns={},
            recorded={},
        )
        assert model.relationships == []


# ── POST /api/v1/connections/{id}/semantic/apply-drift ───────────────────────────

_APPLY_DRIFT_URL = "/api/v1/connections/conn-123/semantic/apply-drift"
# ``TestApplyDrift._run``'s default for ``known_after_lock``: the baseline under the lock
# is the one read before it.
_SAME = object()


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
            # _invalidate_connection_caches: the embedding and query cache deletes (the
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
        generated_columns: dict | BaseException | Callable | None = None,
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
        known: dict | None = None,
        known_after_lock: dict | object = _SAME,
        provider_error: BaseException | None = None,
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
        ``generated_columns`` is what ``generate_new_columns`` returns, the error it
        raises, or its side effect (default: nothing annotated). ``known`` is the baseline
        ``load_known_columns`` returns before the lock and ``known_after_lock`` the one
        under it (default: the same). ``known`` defaults to ``{}``, what the store returns
        for no baseline: every stored table is baselined in memory and none has new
        columns. Those two mocks are left on ``self.load_known`` and ``self.known_update``
        (``update_known_columns``). ``provider_error`` is what ``_resolve_provider`` raises
        (default: it resolves).
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
        resolve = (
            AsyncMock(side_effect=provider_error)
            if provider_error is not None
            else AsyncMock(return_value=(MagicMock(), "model-x", "claude"))
        )
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
        known = {} if known is None else known
        load_known = AsyncMock(
            side_effect=[known, known if known_after_lock is _SAME else known_after_lock]
        )
        update_known = AsyncMock(return_value=None)
        self.load_known, self.known_update = load_known, update_known
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
            patch(f"{_STORE}.load_known_columns", new=load_known),
            patch(f"{_STORE}.update_known_columns", new=update_known),
        ):
            gen = mock_gen.return_value
            gen._build_relationship_graph.return_value = fresh_edges or []
            if isinstance(generated, BaseException) or callable(generated):
                gen.generate_tables = AsyncMock(side_effect=generated)
            else:
                gen.generate_tables = AsyncMock(return_value=generated or {})
            gen.generate_globals = AsyncMock(return_value=llm)
            if isinstance(generated_columns, BaseException) or callable(generated_columns):
                gen.generate_new_columns = AsyncMock(side_effect=generated_columns)
            else:
                gen.generate_new_columns = AsyncMock(return_value=generated_columns or {})
            # The real size helper and limit: the planner does arithmetic on them.
            gen.table_ddl_sizes.side_effect = _RealGenerator().table_ddl_sizes
            gen._MAX_DDL_CHARS = _RealGenerator._MAX_DDL_CHARS
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

    def test_the_batch_budget_fits_the_proxy_timeout(self) -> None:
        """Every annotation batch (new tables or new columns) and the globals pass may
        each take the full LLM timeout; together they must finish before the proxy gives
        up on the request."""
        from app.routers.semantic import (
            _DRIFT_MAX_BATCHES,
            _DRIFT_MAX_NEW_TABLES,
            _GENERATION_TIMEOUT_SECONDS,
        )

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
        # The new tables alone never take more batches than the budget.
        assert math.ceil(_DRIFT_MAX_NEW_TABLES / GENERATION_BATCH_SIZE) <= _DRIFT_MAX_BATCHES
        llm_calls = _DRIFT_MAX_BATCHES + 1
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

    # ── new columns on held tables ──

    # What ``_complete()`` holds: every stored live column is known.
    _SEEN: ClassVar[dict[str, frozenset[str]]] = {
        "public.orders": frozenset({"id"}),
        "public.coupons": frozenset({"id"}),
    }
    _WIDE: ClassVar[dict[str, list[str]]] = {"orders": ["id", "loyalty_tier"], "coupons": ["id"]}

    async def test_a_new_column_is_annotated_alone_and_added_to_its_table(
        self, http_client
    ) -> None:
        resp, save, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons"], self._WIDE),
            known=self._SEEN,
            generated_columns={
                "public.orders": {"loyalty_tier": ColumnSemantic(display_name="Loyalty tier")}
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        columns = body["semantic_model"]["tables"]["public.orders"]["columns"]
        assert set(columns) == {"id", "loyalty_tier"}
        assert columns["id"]["display_name"] == "Id"
        assert columns["loyalty_tier"]["display_name"] == "Loyalty tier"
        assert body["summary"]["columns_added"] == {"public.orders": ["loyalty_tier"]}
        assert body["summary"]["changed"] is True
        kwargs = gen.generate_new_columns.await_args.kwargs
        assert kwargs["columns_by_table"] == {"public.orders": ["loyalty_tier"]}
        assert set(kwargs["table_context"]) == {"public.orders"}
        gen.generate_tables.assert_not_awaited()
        gen.generate_globals.assert_not_awaited()
        save.assert_awaited_once()

    async def test_an_existing_column_is_never_overwritten_by_the_new_column_pass(
        self, http_client
    ) -> None:
        resp, _, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons"], self._WIDE),
            known=self._SEEN,
            generated_columns={
                "public.orders": {
                    "id": ColumnSemantic(display_name="Overwritten"),
                    "loyalty_tier": ColumnSemantic(display_name="Loyalty tier"),
                }
            },
        )
        columns = resp.json()["semantic_model"]["tables"]["public.orders"]["columns"]
        assert columns["id"]["display_name"] == "Id"

    async def test_a_column_the_baseline_has_seen_is_not_sent_to_the_llm(self, http_client) -> None:
        """Absent from the stored table (deleted by hand, or the LLM omitted it) but known:
        there is nothing to apply."""
        resp, save, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons"], self._WIDE),
            known={**self._SEEN, "public.orders": frozenset({"id", "loyalty_tier"})},
        )
        assert resp.json()["summary"]["changed"] is False
        gen.generate_new_columns.assert_not_awaited()
        save.assert_not_awaited()

    async def test_a_hidden_column_is_never_sent_to_the_llm(self, http_client) -> None:
        conn = _make_conn(privacy_settings={"excluded_columns": ["public.orders.ssn"]})
        resp, _, gen, _ = await self._run(
            http_client,
            conn=conn,
            model=self._complete(),
            schema=_schema(
                ["orders", "coupons"],
                {"orders": ["id", "loyalty_tier", "ssn"], "coupons": ["id"]},
            ),
            known=self._SEEN,
            generated_columns={
                "public.orders": {"loyalty_tier": ColumnSemantic(display_name="Tier")}
            },
        )
        assert resp.status_code == 200
        assert gen.generate_new_columns.await_args.kwargs["columns_by_table"] == {
            "public.orders": ["loyalty_tier"]
        }
        assert "ssn" not in str(resp.json()["semantic_model"]["tables"]["public.orders"])

    async def test_a_model_with_no_baseline_is_baselined_in_memory_and_nothing_is_sent(
        self, http_client
    ) -> None:
        """A legacy table's missing column is not new: the check seeds it, the apply treats
        it as seen."""
        resp, _, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons"], self._WIDE),
            known={},
        )
        assert resp.json()["summary"]["changed"] is False
        gen.generate_new_columns.assert_not_awaited()

    async def test_the_preview_lists_the_new_columns_and_calls_no_llm(self, http_client) -> None:
        resp, save, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons"], self._WIDE),
            known=self._SEEN,
            dry_run=True,
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["dry_run"] is True
        assert summary["columns_added"] == {"public.orders": ["loyalty_tier"]}
        assert summary["changed"] is True
        gen.generate_new_columns.assert_not_awaited()
        save.assert_not_awaited()
        self.known_update.assert_not_awaited()

    async def test_the_preview_warns_about_the_columns_it_would_defer_or_cut(
        self, http_client
    ) -> None:
        """Two batches of new tables leave one: ``coupons`` fills it, cut to the column
        cap, and ``orders`` waits. Neither is listed beyond what the apply would send."""
        from app.routers.semantic import _DRIFT_MAX_COLUMNS_PER_BATCH as CAP

        names = [f"t{i}" for i in range(2 * GENERATION_BATCH_SIZE)]
        wide = [f"c{i:03d}" for i in range(CAP + 2)]
        resp, _, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(
                ["orders", "coupons", *names], {"orders": ["id", "x"], "coupons": ["id", *wide]}
            ),
            known=self._SEEN,
            dry_run=True,
        )
        summary = resp.json()["summary"]
        assert summary["columns_added"] == {"public.coupons": wide[:CAP]}
        assert any(
            "The new columns of 1 more table will not be annotated" in w
            and "public.orders" in w
            and "apply again afterwards" in w
            for w in summary["warnings"]
        )
        assert any(
            "2 more new column(s) on 1 table(s) will not be annotated" in w
            and "public.coupons" in w
            for w in summary["warnings"]
        )

    async def test_a_column_left_out_of_an_answered_table_is_recorded_as_seen_and_warned_about(
        self, http_client
    ) -> None:
        """The batch answered for the table (``loyalty_tier`` came back), so ``region`` is
        not retried forever: drift clears."""
        resp, save, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(
                ["orders", "coupons"],
                {"orders": ["id", "loyalty_tier", "region"], "coupons": ["id"]},
            ),
            known=self._SEEN,
            generated_columns={
                "public.orders": {"loyalty_tier": ColumnSemantic(display_name="Tier")}
            },
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["columns_added"] == {"public.orders": ["loyalty_tier"]}
        assert summary["column_tables_failed"] == []
        assert any(
            "public.orders.region" in w
            and "treated as seen" in w
            and w.endswith("Regenerate to annotate them.")
            for w in summary["warnings"]
        )
        save.assert_awaited_once()
        # Annotated or left out, every column of an answered table is seen now.
        args, kwargs = self.known_update.await_args
        assert args[2]["public.orders"] == {"id", "loyalty_tier", "region"}
        # Only the table whose entry changed is written.
        assert set(args[2]) == {"public.orders"}
        assert kwargs == {"prune": True}

    async def test_a_table_the_batch_returned_nothing_for_is_a_failed_batch_not_seen(
        self, http_client
    ) -> None:
        """An empty answer is a soft failure (a provider returning ``{}``, a payload
        grounding could not place), not evidence that the column needs no annotation."""
        resp, save, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons"], self._WIDE),
            known=self._SEEN,
            generated_columns={},
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["changed"] is False
        assert summary["column_tables_failed"] == ["public.orders"]
        assert any(
            "public.orders.loyalty_tier" in w and "not annotated" in w and "retry" in w
            for w in summary["warnings"]
        )
        save.assert_not_awaited()
        # Not seen: the next check reports the column again. Nothing moved, so the
        # (logged) no-op writes an empty baseline update.
        assert "public.orders" not in self.known_update.await_args.args[2]
        assert self.record.await_count == 1

    async def test_a_hand_added_column_enters_the_baseline_with_the_next_save(
        self, http_client
    ) -> None:
        """What the model stores it has seen: a column annotated by hand (PUT never writes
        the baseline) is recorded by the next apply that writes."""
        model = self._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"id": {"display_name": "Id"}, "by_hand": {"display_name": "H"}},
                },
                "public.coupons": {
                    "display_name": "Coupons",
                    "columns": {"id": {"display_name": "Id"}},
                },
            }
        )
        _, save, _, _ = await self._run(
            http_client,
            model=model,
            schema=_schema(["orders"], {"orders": ["id", "by_hand"]}),  # coupons removed
            known=self._SEEN,
        )
        save.assert_awaited_once()
        assert self.known_update.await_args.args[2]["public.orders"] == {"id", "by_hand"}

    async def test_a_dropped_column_leaves_the_baseline_so_a_return_is_new_again(
        self, http_client
    ) -> None:
        model = self._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"id": {"display_name": "Id"}, "old": {"display_name": "Old"}},
                },
                "public.coupons": {
                    "display_name": "Coupons",
                    "columns": {"id": {"display_name": "Id"}},
                },
            }
        )
        _, save, _, _ = await self._run(
            http_client,
            model=model,
            schema=_schema(["orders", "coupons"]),  # ``old`` is gone from the database
            known={"public.orders": frozenset({"id", "old"}), "public.coupons": frozenset({"id"})},
        )
        save.assert_awaited_once()
        assert self.known_update.await_args.args[2]["public.orders"] == {"id"}

    async def test_a_hidden_column_leaves_the_baseline_entry(self, http_client) -> None:
        """The policy-applied schema is what the model has seen, as in the finalisation:
        once un-hidden, the column is new again unless the model stores it."""
        conn = _make_conn(privacy_settings={"excluded_columns": ["public.orders.ssn"]})
        _, save, _, _ = await self._run(
            http_client,
            conn=conn,
            model=self._complete(),
            schema=_schema(["orders"], {"orders": ["id", "ssn"]}),  # coupons removed
            known={**self._SEEN, "public.orders": frozenset({"id", "ssn"})},
        )
        save.assert_awaited_once()
        assert self.known_update.await_args.args[2]["public.orders"] == {"id"}

    async def test_a_hidden_key_the_model_stores_stays_in_the_baseline(self, http_client) -> None:
        """Under its real name, so once un-hidden the quoted key is renamed to ``ssn``
        rather than ``ssn`` being reported new."""
        conn = _make_conn(privacy_settings={"excluded_columns": ["public.orders.ssn"]})
        _, save, _, _ = await self._run(
            http_client,
            conn=conn,
            model=self._complete(
                tables={
                    "public.orders": {
                        "display_name": "Orders",
                        "columns": {"id": {"display_name": "Id"}, '"ssn"': {"display_name": "S"}},
                    },
                    "public.coupons": {
                        "display_name": "Coupons",
                        "columns": {"id": {"display_name": "Id"}},
                    },
                }
            ),
            schema=_schema(["orders"], {"orders": ["id"]}),  # coupons removed
            known=self._SEEN,
        )
        save.assert_awaited_once()
        assert '"ssn"' in save.await_args.args[2].tables["public.orders"].columns
        assert self.known_update.await_args.args[2]["public.orders"] == {"id", "ssn"}

    async def test_a_hidden_table_keeps_its_baseline_entry(self, http_client) -> None:
        conn = _make_conn(privacy_settings={"excluded_tables": ["public.coupons"]})
        await self._run(
            http_client,
            conn=conn,
            model=self._complete(),
            schema=_schema(["orders", "coupons"], {"orders": ["id", "loyalty_tier"]}),
            known=self._SEEN,
            generated_columns={
                "public.orders": {"loyalty_tier": ColumnSemantic(display_name="Tier")}
            },
        )
        # Never rewritten, so never pruned by the intersection with the live columns:
        # the table is not in the live schema, and its row stays.
        assert "public.coupons" not in self.known_update.await_args.args[2]

    async def test_an_annotated_new_column_enters_the_baseline_with_the_save(
        self, http_client
    ) -> None:
        db = self._db()
        baseline_writes_at_save: list[int] = []
        save = AsyncMock(
            side_effect=lambda _db, _cid, final, **_kw: (
                baseline_writes_at_save.append(self.known_update.await_count) or final
            )
        )
        _, save, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons"], self._WIDE),
            known=self._SEEN,
            generated_columns={
                "public.orders": {"loyalty_tier": ColumnSemantic(display_name="Tier")}
            },
            save=save,
            db=db,
        )
        save.assert_awaited_once()
        assert self.known_update.await_args.args[2]["public.orders"] == {"id", "loyalty_tier"}
        # After the save, in its transaction: the cache commit, then the save's.
        assert baseline_writes_at_save == [0]
        assert db.commit.await_count == 2

    async def test_a_new_table_enters_the_baseline_with_all_its_live_columns(
        self, http_client
    ) -> None:
        await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", "events"], {"events": ["id", "kind"]}),
            known=self._SEEN,
            generated={"public.events": TableSemantic(display_name="Events")},
        )
        assert self.known_update.await_args.args[2]["public.events"] == {"id", "kind"}

    async def test_a_stale_baseline_row_of_a_table_the_apply_adds_is_replaced(
        self, http_client
    ) -> None:
        """A check that seeded the table before an earlier apply removed it leaves a row
        behind; the table's return must not inherit it."""
        await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", "events"], {"events": ["id", "kind"]}),
            known={**self._SEEN, "public.events": frozenset({"id", "gone"})},
            generated={"public.events": TableSemantic(display_name="Events")},
        )
        args, kwargs = self.known_update.await_args
        assert args[2]["public.events"] == {"id", "kind"}
        assert kwargs == {"prune": True}

    async def test_the_rows_of_tables_the_apply_removed_are_pruned(self, http_client) -> None:
        _, save, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders"]),
            known=self._SEEN,
        )
        save.assert_awaited_once()
        args, kwargs = self.known_update.await_args
        # Not rewritten: the prune deletes the row of every table the save left out.
        assert "public.coupons" not in args[2]
        assert kwargs == {"prune": True}

    async def test_a_new_table_the_llm_skipped_is_not_baselined(self, http_client) -> None:
        """Not even from a stale row it left behind (a check seeded it before an earlier
        apply removed it): the row is the prune's, and the table stays new."""
        await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", "events"]),
            known={**self._SEEN, "public.events": frozenset({"id", "gone"})},
            generated={},
        )
        assert "public.events" not in self.known_update.await_args.args[2]

    async def test_new_tables_and_new_columns_share_the_batch_budget(self, http_client) -> None:
        """One more new table than a batch holds takes two batches, leaving one for new
        columns: ``coupons`` gets it, and ``orders``, too wide to share it, waits."""
        from app.routers.semantic import _DRIFT_MAX_BATCHES
        from app.routers.semantic import _DRIFT_MAX_COLUMNS_PER_BATCH as CAP

        assert _DRIFT_MAX_BATCHES == 3  # the split below assumes it
        names = [f"t{i:02d}" for i in range(GENERATION_BATCH_SIZE + 1)]
        wide = [f"c{i:03d}" for i in range(CAP // 2 + 1)]
        resp, save, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(
                ["orders", "coupons", *names],
                {"orders": ["id", *wide], "coupons": ["id", *wide]},
            ),
            known=self._SEEN,
            generated=self._annotate_all,
            generated_columns={"public.coupons": {c: ColumnSemantic(display_name=c) for c in wide}},
        )
        assert resp.status_code == 200
        assert gen.generate_tables.await_count == 2
        gen.generate_globals.assert_awaited_once()
        gen.generate_new_columns.assert_awaited_once()
        assert gen.generate_new_columns.await_args.kwargs["columns_by_table"] == {
            "public.coupons": wide
        }
        summary = resp.json()["summary"]
        assert summary["tables_added"] == sorted(f"public.{n}" for n in names)
        assert summary["columns_added"] == {"public.coupons": wide}
        assert any(
            w.startswith("The new columns of 1 more table were not annotated")
            and "public.orders" in w
            for w in summary["warnings"]
        )
        save.assert_awaited_once()

    async def test_a_throttled_column_phase_beside_a_new_table_is_a_warning(
        self, http_client
    ) -> None:
        resp, save, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", "events"], self._WIDE),
            known=self._SEEN,
            generated={"public.events": TableSemantic(display_name="Events")},
            generated_columns=ValueError("[RATE_LIMIT] slow down"),
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["tables_added"] == ["public.events"]
        assert summary["column_tables_failed"] == ["public.orders"]
        assert any(
            "were not annotated" in w and "[RATE_LIMIT]" in w and "public.orders.loyalty_tier" in w
            for w in summary["warnings"]
        )
        save.assert_awaited_once()
        assert "public.events" in save.await_args.args[2].tables

    async def test_tables_with_new_columns_past_the_batch_cap_are_deferred(
        self, http_client
    ) -> None:
        """Three batches go to new tables first; with none left over, a table with a new
        column waits for the next apply, and the warning says so."""
        names = [f"t{i}" for i in range(3 * GENERATION_BATCH_SIZE)]
        resp, _, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", *names], self._WIDE),
            known=self._SEEN,
            generated={},
        )
        assert resp.status_code == 200
        gen.generate_new_columns.assert_not_awaited()
        warnings = resp.json()["summary"]["warnings"]
        assert any("public.orders" in w and "new columns" in w for w in warnings)

    async def test_columns_past_the_per_batch_cap_stay_new_and_the_warning_counts_them(
        self, http_client
    ) -> None:
        from app.routers.semantic import _DRIFT_MAX_COLUMNS_PER_BATCH as CAP

        extra = [f"c{i:03d}" for i in range(CAP + 5)]
        sent = sorted(extra)[:CAP]
        resp, _, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons"], {"orders": ["id", *extra], "coupons": ["id"]}),
            known=self._SEEN,
            generated_columns={"public.orders": {c: ColumnSemantic(display_name=c) for c in sent}},
        )
        assert gen.generate_new_columns.await_args.kwargs["columns_by_table"] == {
            "public.orders": sent
        }
        assert any("5 more new column(s)" in w for w in resp.json()["summary"]["warnings"])
        # The five left out are not in the baseline: the next check reports them.
        assert not set(extra[-5:]) & self.known_update.await_args.args[2]["public.orders"]

    # ── catalog FK edges on new columns ──

    @classmethod
    def _fk_model(cls, orders: list[str], *, coupons: bool = False) -> SemanticModel:
        """``orders`` holding *orders* and ``customers`` holding ``id``, no relationships
        (and ``coupons``, for drift beside the new column)."""
        tables = {
            "public.orders": {
                "display_name": "Orders",
                "columns": {name: {"display_name": name} for name in orders},
            },
            "public.customers": {"display_name": "C", "columns": {"id": {"display_name": "I"}}},
        }
        if coupons:
            tables["public.coupons"] = {"display_name": "Coupons", "columns": {}}
        return cls._complete(tables=tables, relationships=[])

    _FK_KNOWN: ClassVar[dict[str, frozenset[str]]] = {
        "public.orders": frozenset({"id", "customer_id"}),
        "public.customers": frozenset({"id"}),
    }

    async def test_a_fk_on_a_new_column_is_added_but_a_deleted_edge_on_an_old_column_is_not(
        self, http_client
    ) -> None:
        schema = _schema(
            ["orders", "customers"],
            {"orders": ["id", "customer_id", "region_id"], "customers": ["id"]},
        )
        deleted = self._edge("public.orders", "customer_id", "public.customers")
        fresh = self._edge("public.orders", "region_id", "public.customers")
        resp, _, _, _ = await self._run(
            http_client,
            model=self._fk_model(["id", "customer_id"]),
            schema=schema,
            known=self._FK_KNOWN,
            generated_columns={
                "public.orders": {"region_id": ColumnSemantic(display_name="Region")}
            },
            fresh_edges=[deleted, fresh],
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["relationships_added"] == ["public.orders.region_id → public.customers.id"]

    async def test_the_preview_lists_the_fk_on_a_new_column_it_would_send(
        self, http_client
    ) -> None:
        resp, _, _, _ = await self._run(
            http_client,
            model=self._fk_model(["id", "customer_id"]),
            schema=_schema(
                ["orders", "customers"],
                {"orders": ["id", "customer_id", "region_id"], "customers": ["id"]},
            ),
            known=self._FK_KNOWN,
            fresh_edges=[self._edge("public.orders", "region_id", "public.customers")],
            dry_run=True,
        )
        summary = resp.json()["summary"]
        assert summary["columns_added"] == {"public.orders": ["region_id"]}
        assert summary["relationships_added"] == ["public.orders.region_id → public.customers.id"]

    async def test_the_preview_lists_no_fk_on_a_new_column_it_would_defer(
        self, http_client
    ) -> None:
        names = [f"t{i}" for i in range(3 * GENERATION_BATCH_SIZE)]
        resp, _, _, _ = await self._run(
            http_client,
            model=self._fk_model(["id", "customer_id"]),
            schema=_schema(
                ["orders", "customers", *names],
                {"orders": ["id", "customer_id", "region_id"], "customers": ["id"]},
            ),
            known=self._FK_KNOWN,
            fresh_edges=[self._edge("public.orders", "region_id", "public.customers")],
            dry_run=True,
        )
        summary = resp.json()["summary"]
        assert summary["columns_added"] == {}
        assert summary["relationships_added"] == []

    async def test_a_fk_on_a_new_column_the_llm_omitted_arrives_with_its_answered_table(
        self, http_client
    ) -> None:
        """The batch answered for ``orders`` but left ``region_id`` out: the column is
        recorded as seen, so this is the apply its edge arrives with."""
        resp, save, _, _ = await self._run(
            http_client,
            model=self._fk_model(["id", "customer_id"]),
            schema=_schema(
                ["orders", "customers"],
                {"orders": ["id", "customer_id", "region_id", "tier"], "customers": ["id"]},
            ),
            known=self._FK_KNOWN,
            generated_columns={"public.orders": {"tier": ColumnSemantic(display_name="Tier")}},
            fresh_edges=[self._edge("public.orders", "region_id", "public.customers")],
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["columns_added"] == {"public.orders": ["tier"]}
        assert summary["relationships_added"] == ["public.orders.region_id → public.customers.id"]
        save.assert_awaited_once()
        assert "region_id" in self.known_update.await_args.args[2]["public.orders"]

    async def test_a_fk_on_a_new_column_whose_batch_failed_is_not_added(self, http_client) -> None:
        """Not recorded, so the column stays new and its edge waits for the apply that
        records it. The removal beside it is still saved."""
        resp, save, _, _ = await self._run(
            http_client,
            model=self._fk_model(["id", "customer_id"], coupons=True),
            schema=_schema(
                ["orders", "customers"],
                {"orders": ["id", "customer_id", "region_id"], "customers": ["id"]},
            ),
            known=self._FK_KNOWN,
            generated_columns=RuntimeError("provider down"),
            fresh_edges=[self._edge("public.orders", "region_id", "public.customers")],
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["tables_removed"] == ["public.coupons"]
        assert summary["relationships_added"] == []
        assert summary["columns_added"] == {}
        saved = save.await_args.args[2]
        assert saved.relationships == []
        assert "region_id" not in saved.tables["public.orders"].columns
        assert "public.orders" not in self.known_update.await_args.args[2]

    async def test_a_deleted_fk_on_a_renamed_column_is_not_re_added(self, http_client) -> None:
        """The model held ``customer_id`` under a quoted key: the key is renamed, and a
        missing edge on the column is the user's deletion."""
        resp, _, gen, _ = await self._run(
            http_client,
            model=self._fk_model(["id", '"customer_id"']),
            schema=_schema(
                ["orders", "customers"],
                {"orders": ["id", "customer_id"], "customers": ["id"]},
            ),
            known=self._FK_KNOWN,
            fresh_edges=[self._edge("public.orders", "customer_id", "public.customers")],
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["columns_renamed"] == {"public.orders": {'"customer_id"': "customer_id"}}
        assert summary["columns_added"] == {}
        assert summary["relationships_added"] == []
        gen.generate_new_columns.assert_not_awaited()

    # ── renamed columns: a near-miss key of a live column the baseline has seen ──

    @staticmethod
    def _orders_holding(column_key: str) -> SemanticModel:
        """``orders`` holding ``id`` and *column_key* (labelled "Hand-written"), and
        ``coupons`` holding ``id``."""
        return TestApplyDrift._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {
                        "id": {"display_name": "Id"},
                        column_key: {"display_name": "Hand-written"},
                    },
                },
                "public.coupons": {
                    "display_name": "Coupons",
                    "columns": {"id": {"display_name": "Id"}},
                },
            }
        )

    _STATUS_SEEN: ClassVar[dict[str, frozenset[str]]] = {
        "public.coupons": frozenset({"id"}),
        "public.orders": frozenset({"id", "status"}),
    }

    async def test_a_preview_lists_a_rename_not_a_removal_and_an_addition(
        self, http_client
    ) -> None:
        resp, save, _, _ = await self._run(
            http_client,
            model=self._orders_holding('"status"'),
            schema=_schema(["orders", "coupons"], {"orders": ["id", "status"], "coupons": ["id"]}),
            known=self._STATUS_SEEN,
            dry_run=True,
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["columns_renamed"] == {"public.orders": {'"status"': "status"}}
        assert summary["columns_removed"] == {}
        assert summary["columns_added"] == {}
        assert summary["changed"] is True
        save.assert_not_awaited()

    @pytest.mark.parametrize("near_miss", ['"status"', "Status"], ids=["quoted", "case-only"])
    async def test_a_rename_keeps_the_annotation_and_edits_with_no_llm_call_or_provider(
        self, http_client, near_miss: str
    ) -> None:
        """A rename is structural, like a removal: no provider is resolved, no LLM call is
        made, and the column keeps its annotation and its hand edits under the live name.
        The model stays reviewed: nothing the LLM wrote was added."""
        resp, save, gen, resolve = await self._run(
            http_client,
            model=self._orders_holding(near_miss),
            schema=_schema(["orders", "coupons"], {"orders": ["id", "status"], "coupons": ["id"]}),
            provenance={
                **_no_provenance(),
                "column": {f"public.orders.{near_miss}": ["display_name"]},
            },
            protected={
                "table": {},
                "column": {f"public.orders.{near_miss}": {"display_name": "Hand-written"}},
            },
            known=self._STATUS_SEEN,
            provider_error=self._NO_PROVIDER,
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["columns_renamed"] == {"public.orders": {near_miss: "status"}}
        assert summary["columns_removed"] == summary["columns_added"] == {}
        resolve.assert_not_awaited()
        gen.generate_new_columns.assert_not_awaited()
        save.assert_awaited_once()
        saved = save.await_args.args[2]
        assert saved.tables["public.orders"].columns["status"].display_name == "Hand-written"
        assert near_miss not in saved.tables["public.orders"].columns
        assert saved.is_user_reviewed is True
        kwargs = save.await_args.kwargs
        assert kwargs["provenance"]["column"] == {"public.orders.status": ["display_name"]}
        assert kwargs["protected"]["column"] == {
            "public.orders.status": {"display_name": "Hand-written"}
        }
        self.record.assert_awaited_once()

    async def test_the_baseline_after_a_rename_holds_the_live_name(self, http_client) -> None:
        """A legacy table (no entry) is baselined with its live columns, ``status``
        included, and the rename keeps it there: the column is stored under that name."""
        resp, _, _, _ = await self._run(
            http_client,
            model=self._orders_holding('"status"'),
            schema=_schema(["orders", "coupons"], {"orders": ["id", "status"], "coupons": ["id"]}),
            known={"public.coupons": frozenset({"id"})},
        )
        assert resp.status_code == 200
        assert resp.json()["summary"]["columns_renamed"] == {
            "public.orders": {'"status"': "status"}
        }
        assert self.known_update.await_args.args[2] == {"public.orders": {"id", "status"}}

    async def test_a_rename_beside_a_new_column_without_a_provider_is_saved_with_a_warning(
        self, http_client
    ) -> None:
        """The rename is something else to save, so the unresolvable provider fails only
        the column phase: the rename is applied and the new column is named in a warning."""
        resp, save, gen, _ = await self._run(
            http_client,
            model=self._orders_holding('"status"'),
            schema=_schema(
                ["orders", "coupons"], {"orders": ["id", "status", "region"], "coupons": ["id"]}
            ),
            known=self._STATUS_SEEN,
            provider_error=self._NO_PROVIDER,
        )
        assert resp.status_code == 200
        gen.generate_new_columns.assert_not_awaited()
        summary = resp.json()["summary"]
        assert summary["columns_renamed"] == {"public.orders": {'"status"': "status"}}
        assert summary["columns_added"] == {}
        assert any(
            "provider could not be used" in w and "public.orders.region" in w
            for w in summary["warnings"]
        )
        save.assert_awaited_once()
        assert set(save.await_args.args[2].tables["public.orders"].columns) == {"id", "status"}

    async def test_a_throttled_column_phase_beside_a_rename_is_a_warning(self, http_client) -> None:
        """The rename is something else to save, so a 429 from the new-column batch does
        not propagate: the rename is applied and the column waits for the next apply."""
        resp, save, _, _ = await self._run(
            http_client,
            model=self._orders_holding('"status"'),
            schema=_schema(
                ["orders", "coupons"], {"orders": ["id", "status", "region"], "coupons": ["id"]}
            ),
            known=self._STATUS_SEEN,
            generated_columns=ValueError("[RATE_LIMIT] slow down"),
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["columns_renamed"] == {"public.orders": {'"status"': "status"}}
        assert summary["column_tables_failed"] == ["public.orders"]
        assert any("[RATE_LIMIT]" in w and "public.orders.region" in w for w in summary["warnings"])
        save.assert_awaited_once()
        columns = save.await_args.args[2].tables["public.orders"].columns
        assert "status" in columns
        assert '"status"' not in columns
        assert "region" not in columns

    async def test_a_rename_is_applied_when_the_batch_budget_is_spent(self, http_client) -> None:
        """New tables take every batch, so the new column ``region`` waits for a later
        apply; a rename needs no batch and is applied all the same."""
        names = [f"t{i}" for i in range(3 * GENERATION_BATCH_SIZE)]
        resp, save, gen, _ = await self._run(
            http_client,
            model=self._orders_holding('"status"'),
            schema=_schema(
                ["orders", "coupons", *names],
                {"orders": ["id", "status", "region"], "coupons": ["id"]},
            ),
            known=self._STATUS_SEEN,
            generated={},
        )
        assert resp.status_code == 200
        gen.generate_new_columns.assert_not_awaited()
        summary = resp.json()["summary"]
        assert summary["columns_renamed"] == {"public.orders": {'"status"': "status"}}
        assert any(
            w.startswith("The new columns of 1 more table were not annotated by this apply")
            and "public.orders" in w
            for w in summary["warnings"]
        )
        save.assert_awaited_once()
        columns = save.await_args.args[2].tables["public.orders"].columns
        assert "status" in columns
        assert "region" not in columns

    async def test_a_marked_edge_on_a_renamed_column_is_rekeyed_not_swapped(
        self, http_client
    ) -> None:
        """The model holds ``customer_id`` under a quoted key, with a hand-edited edge on
        that key; the catalog has the edge under the real name. The edge follows the
        rename, keeps its mark under the new key, and the summary shows no relationship
        change."""
        stale = self._edge("public.orders", '"customer_id"', "public.customers")
        model = self._fk_model(["id", '"customer_id"']).model_copy(
            update={"relationships": [stale.model_copy(update={"description": "Hand"})]}
        )
        resp, save, _, _ = await self._run(
            http_client,
            model=model,
            schema=_schema(
                ["orders", "customers"],
                {"orders": ["id", "customer_id"], "customers": ["id"]},
            ),
            provenance={
                **_no_provenance(),
                "relationship": {semantic_store.relationship_key(stale): ["description"]},
            },
            known=self._FK_KNOWN,
            fresh_edges=[self._edge("public.orders", "customer_id", "public.customers")],
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["columns_renamed"] == {"public.orders": {'"customer_id"': "customer_id"}}
        assert summary["relationships_added"] == summary["relationships_removed"] == []
        saved = save.await_args.args[2]
        assert [(e.from_column, e.description) for e in saved.relationships] == [
            ("customer_id", "Hand")
        ]
        renamed_key = semantic_store.relationship_key(saved.relationships[0])
        assert save.await_args.kwargs["provenance"]["relationship"] == {
            renamed_key: ["description"]
        }

    async def test_a_renamed_column_is_not_sent_to_the_llm_beside_a_new_one(
        self, http_client
    ) -> None:
        """Only the new column is asked for; the renamed one keeps its annotation."""
        resp, save, gen, _ = await self._run(
            http_client,
            model=self._orders_holding('"status"'),
            schema=_schema(
                ["orders", "coupons"], {"orders": ["id", "status", "region"], "coupons": ["id"]}
            ),
            known=self._STATUS_SEEN,
            generated_columns={"public.orders": {"region": ColumnSemantic(display_name="R")}},
        )
        assert resp.status_code == 200
        assert gen.generate_new_columns.await_args.kwargs["columns_by_table"] == {
            "public.orders": ["region"]
        }
        summary = resp.json()["summary"]
        assert summary["columns_renamed"] == {"public.orders": {'"status"': "status"}}
        assert summary["columns_added"] == {"public.orders": ["region"]}
        columns = save.await_args.args[2].tables["public.orders"].columns
        assert columns["status"].display_name == "Hand-written"
        assert self.known_update.await_args.args[2]["public.orders"] == {"id", "status", "region"}

    # ── concurrency, review flag, failures ──

    async def test_a_column_another_writer_added_during_the_llm_call_is_not_overwritten(
        self, http_client
    ) -> None:
        before = self._complete()
        meanwhile = self._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {
                        "id": {"display_name": "Id"},
                        "loyalty_tier": {"display_name": "Hand written"},
                    },
                },
                "public.coupons": {
                    "display_name": "Coupons",
                    "columns": {"id": {"display_name": "Id"}},
                },
            }
        )
        resp, save, _, _ = await self._run(
            http_client,
            model=before,
            current=(meanwhile, _snapshot()),
            schema=_schema(["orders", "coupons"], self._WIDE),
            known=self._SEEN,
            generated_columns={
                "public.orders": {"loyalty_tier": ColumnSemantic(display_name="From the LLM")}
            },
        )
        assert resp.status_code == 200
        # The re-read already has the column: nothing is left to apply, nothing is written.
        assert resp.json()["summary"]["changed"] is False
        save.assert_not_awaited()

    async def test_a_column_another_writer_added_beside_other_drift_keeps_its_annotation(
        self, http_client
    ) -> None:
        """The post-lock filter: with a removal still to save, the LLM's annotation of a
        column the re-read holds is dropped. It is not merged over the stored one, and it
        is not new LLM content, so the review flag stays."""
        meanwhile = self._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {
                        "id": {"display_name": "Id"},
                        "loyalty_tier": {"display_name": "Hand written"},
                    },
                },
                "public.coupons": {
                    "display_name": "Coupons",
                    "columns": {"id": {"display_name": "Id"}},
                },
            }
        )
        resp, _, _, _ = await self._run(
            http_client,
            model=self._complete(),
            current=(meanwhile, _snapshot()),
            schema=_schema(["orders"], {"orders": ["id", "loyalty_tier"]}),
            known=self._SEEN,
            generated_columns={
                "public.orders": {"loyalty_tier": ColumnSemantic(display_name="From the LLM")}
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"]["tables_removed"] == ["public.coupons"]
        assert body["summary"]["columns_added"] == {}
        columns = body["semantic_model"]["tables"]["public.orders"]["columns"]
        assert columns["loyalty_tier"]["display_name"] == "Hand written"
        assert body["semantic_model"]["is_user_reviewed"] is True

    async def test_a_column_another_apply_recorded_during_the_llm_call_is_not_reported(
        self, http_client
    ) -> None:
        """The baseline is re-read under the lock: another apply recorded ``loyalty_tier``
        as seen while this one's LLM call ran (that call returned nothing). It is not new
        any more, so it is not warned about, not marked for retry and not rewritten in the
        baseline; the removal beside it is still saved."""
        resp, save, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders"], {"orders": ["id", "loyalty_tier"]}),  # coupons removed
            known=self._SEEN,
            known_after_lock={**self._SEEN, "public.orders": frozenset({"id", "loyalty_tier"})},
            generated_columns={},
        )
        assert resp.status_code == 200
        gen.generate_new_columns.assert_awaited_once()
        summary = resp.json()["summary"]
        assert summary["tables_removed"] == ["public.coupons"]
        assert not any("loyalty_tier" in w for w in summary["warnings"])
        assert summary["column_tables_failed"] == []
        save.assert_awaited_once()
        assert "public.orders" not in self.known_update.await_args.args[2]

    async def test_an_omitted_column_is_recorded_when_nothing_else_changes(
        self, http_client
    ) -> None:
        """The LLM annotated ``loyalty_tier``, which another writer stored meanwhile (the
        post-lock filter drops the annotation), and left ``region`` out. The model is as it
        was, so nothing is saved or logged, but the baseline records ``region`` and is
        committed: that is what clears its drift."""
        db = self._db()
        meanwhile = self._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {
                        "id": {"display_name": "Id"},
                        "loyalty_tier": {"display_name": "Hand written"},
                    },
                },
                "public.coupons": {
                    "display_name": "Coupons",
                    "columns": {"id": {"display_name": "Id"}},
                },
            }
        )
        resp, save, _, _ = await self._run(
            http_client,
            model=self._complete(),
            current=(meanwhile, _snapshot()),
            schema=_schema(
                ["orders", "coupons"],
                {"orders": ["id", "loyalty_tier", "region"], "coupons": ["id"]},
            ),
            known=self._SEEN,
            generated_columns={
                "public.orders": {"loyalty_tier": ColumnSemantic(display_name="From the LLM")}
            },
            db=db,
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["changed"] is False
        assert summary["column_tables_failed"] == summary["tables_not_annotated"] == []
        save.assert_not_awaited()
        self.record.assert_not_awaited()
        self.known_update.assert_awaited_once()
        assert self.known_update.await_args.args[2]["public.orders"] == {
            "id",
            "loyalty_tier",
            "region",
        }
        # The schema cache, then the baseline; no rollback.
        assert db.commit.await_count == 2
        db.rollback.assert_not_awaited()

    async def test_annotated_new_columns_mark_the_model_unreviewed(self, http_client) -> None:
        resp, save, _, _ = await self._run(
            http_client,
            model=self._complete(is_user_reviewed=True),
            schema=_schema(["orders", "coupons"], self._WIDE),
            known=self._SEEN,
            generated_columns={
                "public.orders": {"loyalty_tier": ColumnSemantic(display_name="Loyalty tier")}
            },
        )
        assert resp.status_code == 200
        assert save.await_args.args[2].is_user_reviewed is False

    async def test_a_removal_alone_leaves_the_reviewed_flag_alone(self, http_client) -> None:
        _, save, _, _ = await self._run(
            http_client,
            model=self._complete(is_user_reviewed=True),
            schema=_schema(["orders"]),
            known=self._SEEN,
        )
        assert save.await_args.args[2].is_user_reviewed is True

    async def test_a_failed_new_column_call_does_not_block_the_removals(self, http_client) -> None:
        resp, save, _, _ = await self._run(
            http_client,
            model=self._complete(),
            # coupons is gone from the live schema; orders gained a column.
            schema=_schema(["orders"], {"orders": ["id", "loyalty_tier"]}),
            known=self._SEEN,
            generated_columns=ValueError("boom"),
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["tables_removed"] == ["public.coupons"]
        assert summary["columns_added"] == {}
        assert summary["column_tables_failed"] == ["public.orders"]
        assert any(
            "public.orders.loyalty_tier" in w and "not annotated" in w and "failed" in w
            for w in summary["warnings"]
        )
        save.assert_awaited_once()
        # A failed batch's columns are NOT seen: nothing about ``orders`` changed (its new
        # column is not recorded), so nothing is written for it, and the next check
        # reports the column again.
        assert "public.orders" not in self.known_update.await_args.args[2]

    @pytest.mark.parametrize(
        ("mark", "status_code"),
        [("[RATE_LIMIT]", 429), ("[PROVIDER_UNAVAILABLE]", 503)],
        ids=["throttled", "unavailable"],
    )
    async def test_a_transient_failure_of_a_column_only_apply_propagates_its_status(
        self, http_client, mark: str, status_code: int
    ) -> None:
        """Nothing else to save: the request fails with the provider's 429 or 503, so the
        user sees the throttle and can apply again, rather than a 200 that changed
        nothing."""
        resp, save, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons"], self._WIDE),
            known=self._SEEN,
            generated_columns=ValueError(f"{mark} slow down"),
        )
        assert resp.status_code == status_code
        save.assert_not_awaited()
        self.record.assert_not_awaited()
        self.known_update.assert_not_awaited()

    @pytest.mark.parametrize(
        "removal", ["table", "column"], ids=["removed-table", "removed-column"]
    )
    async def test_a_throttled_column_phase_beside_removals_is_a_warning(
        self, http_client, removal: str
    ) -> None:
        """A removed table or a removed column is something else to save, so the 429
        becomes a warning and the removal is applied."""
        if removal == "table":  # coupons is gone from the database
            model = self._complete()
            schema = _schema(["orders"], {"orders": ["id", "loyalty_tier"]})
            known = self._SEEN
            expected = {"tables_removed": ["public.coupons"]}
        else:  # orders.old is gone from the database
            model = self._complete(
                tables={
                    "public.orders": {
                        "display_name": "Orders",
                        "columns": {"id": {"display_name": "Id"}, "old": {"display_name": "Old"}},
                    },
                    "public.coupons": {
                        "display_name": "Coupons",
                        "columns": {"id": {"display_name": "Id"}},
                    },
                }
            )
            schema = _schema(["orders", "coupons"], self._WIDE)
            known = {**self._SEEN, "public.orders": frozenset({"id", "old"})}
            expected = {"columns_removed": {"public.orders": ["old"]}}
        resp, save, _, _ = await self._run(
            http_client,
            model=model,
            schema=schema,
            known=known,
            generated_columns=ValueError("[RATE_LIMIT] slow down"),
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        for field, value in expected.items():
            assert summary[field] == value
        assert summary["column_tables_failed"] == ["public.orders"]
        assert any(
            "[RATE_LIMIT]" in w and "public.orders.loyalty_tier" in w for w in summary["warnings"]
        )
        save.assert_awaited_once()

    async def test_a_failed_batch_is_logged_for_the_retry_order(self, http_client) -> None:
        resp, save, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons"], self._WIDE),
            known=self._SEEN,
            generated_columns=RuntimeError("provider down"),
        )
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["column_tables_failed"] == ["public.orders"]
        # The failed batch was the only one: no later batch went untried.
        assert summary["warnings"] == [
            "1 new column(s) were not annotated and stay missing from the model (the "
            "new-column LLM call failed (provider down): public.orders.loyalty_tier). Run "
            "Check Drift and apply again to retry."
        ]
        save.assert_not_awaited()
        # Nothing changed, but the failure is logged: the next apply reads it.
        assert self.record.await_count == 1

    async def test_a_table_an_earlier_batch_got_no_answer_for_stays_failed_when_a_later_one_fails(
        self, http_client
    ) -> None:
        """The column cap splits the two tables. ``coupons`` gets an empty answer, then
        ``orders``' batch fails: both are marked, not just the table of the failed batch."""
        from app.routers.semantic import _DRIFT_MAX_COLUMNS_PER_BATCH as CAP

        wide = [f"c{i:03d}" for i in range(CAP // 2 + 1)]
        answers: list[dict | BaseException] = [{}, RuntimeError("provider down")]

        def answer(**_kw) -> dict:
            result = answers.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result

        resp, _, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(
                ["orders", "coupons"], {"orders": ["id", *wide], "coupons": ["id", *wide]}
            ),
            known=self._SEEN,
            generated_columns=answer,
        )
        assert resp.status_code == 200
        assert gen.generate_new_columns.await_count == 2
        summary = resp.json()["summary"]
        assert summary["column_tables_failed"] == ["public.coupons", "public.orders"]
        # One clause per reason, each naming its own tables' columns.
        [warning] = summary["warnings"]
        assert warning.startswith(f"{2 * len(wide)} new column(s) were not annotated")
        assert "the LLM returned nothing for their table: public.coupons.c000, " in warning
        assert "the new-column LLM call failed (provider down): public.orders.c000, " in warning
        assert "not tried" not in warning

    async def test_tables_whose_batch_failed_last_time_are_sent_last(self, http_client) -> None:
        _, _, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(
                ["orders", "coupons"],
                {"orders": ["id", "loyalty_tier"], "coupons": ["id", "code"]},
            ),
            known=self._SEEN,
            generated_columns={},
            last_apply={"column_tables_failed": ["public.coupons"]},
        )
        sent = gen.generate_new_columns.await_args.kwargs["columns_by_table"]
        assert list(sent) == ["public.orders", "public.coupons"]

    async def test_a_demotion_survives_an_apply_that_defers_the_table(self, http_client) -> None:
        names = [f"t{i}" for i in range(3 * GENERATION_BATCH_SIZE)]
        resp, _, _, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders", "coupons", *names], self._WIDE),
            known=self._SEEN,
            generated={},
            last_apply={"column_tables_failed": ["public.orders"]},
        )
        assert resp.json()["summary"]["column_tables_failed"] == ["public.orders"]

    async def test_a_demotion_survives_a_batch_that_was_never_reached(self, http_client) -> None:
        """``coupons`` failed last time, so it is sent last, in its own batch (the column cap
        splits the two). The first batch fails and ends the phase: ``coupons`` was never
        tried, and keeps its mark — or the next apply would put it first again."""
        from app.routers.semantic import _DRIFT_MAX_COLUMNS_PER_BATCH as CAP

        wide = [f"c{i:03d}" for i in range(CAP // 2 + 1)]
        resp, _, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(
                ["orders", "coupons"], {"orders": ["id", *wide], "coupons": ["id", *wide]}
            ),
            known=self._SEEN,
            generated_columns=RuntimeError("provider down"),
            last_apply={"column_tables_failed": ["public.coupons"]},
        )
        assert resp.status_code == 200
        assert list(gen.generate_new_columns.await_args.kwargs["columns_by_table"]) == [
            "public.orders"
        ]
        summary = resp.json()["summary"]
        assert summary["column_tables_failed"] == ["public.coupons", "public.orders"]
        [warning] = summary["warnings"]
        assert "the new-column LLM call failed (provider down): public.orders.c000, " in warning
        assert "the batch after the failed call was not tried: public.coupons.c000, " in warning
        assert "returned nothing" not in warning

    async def test_a_throttle_after_an_answered_batch_keeps_what_was_answered(
        self, http_client
    ) -> None:
        """Nothing but new columns, yet the first batch answered: the 429 on the second is a
        warning, and the first batch's annotations and baseline are saved."""
        from app.routers.semantic import _DRIFT_MAX_COLUMNS_PER_BATCH as CAP

        wide = [f"c{i:03d}" for i in range(CAP // 2 + 1)]
        answers: list[dict | BaseException] = [
            {"public.coupons": {c: ColumnSemantic(display_name=c) for c in wide}},
            ValueError("[RATE_LIMIT] slow down"),
        ]

        def answer(**_kw) -> dict:
            result = answers.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result

        resp, save, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(
                ["orders", "coupons"], {"orders": ["id", *wide], "coupons": ["id", *wide]}
            ),
            known=self._SEEN,
            generated_columns=answer,
        )
        assert resp.status_code == 200
        assert gen.generate_new_columns.await_count == 2
        summary = resp.json()["summary"]
        assert summary["columns_added"] == {"public.coupons": wide}
        assert summary["column_tables_failed"] == ["public.orders"]
        [warning] = summary["warnings"]
        assert "the new-column LLM call failed ([RATE_LIMIT] slow down): public.orders.c000" in (
            warning
        )
        save.assert_awaited_once()
        written = self.known_update.await_args.args[2]
        assert written["public.coupons"] == {"id", *wide}
        assert "public.orders" not in written

    # ── no provider ──

    _NO_PROVIDER = HTTPException(status_code=400, detail="No API key configured for claude")

    async def test_an_unresolvable_provider_does_not_hold_back_the_removals(
        self, http_client
    ) -> None:
        """New columns beside a removal: the column phase fails like a failed call (no
        column recorded, no edge on one), and the removal is saved."""
        resp, save, gen, _ = await self._run(
            http_client,
            model=self._fk_model(["id", "customer_id"], coupons=True),
            schema=_schema(
                ["orders", "customers"],
                {"orders": ["id", "customer_id", "region_id"], "customers": ["id"]},
            ),
            known=self._FK_KNOWN,
            fresh_edges=[self._edge("public.orders", "region_id", "public.customers")],
            provider_error=self._NO_PROVIDER,
        )
        assert resp.status_code == 200
        gen.generate_new_columns.assert_not_awaited()
        summary = resp.json()["summary"]
        assert summary["tables_removed"] == ["public.coupons"]
        assert summary["columns_added"] == {}
        assert summary["relationships_added"] == []
        assert summary["warnings"] == [
            "1 new column(s) were not annotated and stay missing from the model (the LLM "
            "provider could not be used (No API key configured for claude): "
            "public.orders.region_id). Run Check Drift and apply again to retry."
        ]
        save.assert_awaited_once()
        assert "public.orders" not in self.known_update.await_args.args[2]

    @pytest.mark.parametrize(
        "schema",
        [
            _schema(["orders", "coupons"], {"orders": ["id", "loyalty_tier"], "coupons": ["id"]}),
            _schema(["orders", "coupons", "events"]),
        ],
        ids=["column-only", "new-table"],
    )
    async def test_an_unresolvable_provider_fails_an_apply_it_is_required_for(
        self, http_client, schema: DataSourceSchema
    ) -> None:
        """Nothing but new columns, or a new table: the 400 is the answer, nothing is
        written."""
        resp, save, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            schema=schema,
            known=self._SEEN,
            provider_error=self._NO_PROVIDER,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "No API key configured for claude"
        gen.generate_tables.assert_not_awaited()
        gen.generate_new_columns.assert_not_awaited()
        save.assert_not_awaited()
        self.record.assert_not_awaited()
        self.known_update.assert_not_awaited()

    async def test_the_preview_of_new_columns_resolves_no_provider(self, http_client) -> None:
        resp, _, _, resolve = await self._run(
            http_client,
            model=self._complete(),
            schema=_schema(["orders"], {"orders": ["id", "loyalty_tier"]}),
            known=self._SEEN,
            provider_error=self._NO_PROVIDER,
            dry_run=True,
        )
        assert resp.status_code == 200
        assert resp.json()["summary"]["columns_added"] == {"public.orders": ["loyalty_tier"]}
        resolve.assert_not_awaited()

    # ── warnings planned before the lock ──

    async def test_deferred_work_another_writer_stored_meanwhile_is_not_named(
        self, http_client
    ) -> None:
        """Two new tables and two tables with new columns are past the budget. Under the
        lock, one of each is stored already: the warnings name only the others."""
        from app.routers.semantic import _DRIFT_MAX_NEW_TABLES

        names = [f"t{i:02d}" for i in range(_DRIFT_MAX_NEW_TABLES + 2)]
        keys = sorted(f"public.{n}" for n in names)
        wide = {"orders": ["id", "loyalty_tier"], "coupons": ["id", "code"]}
        meanwhile = self._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {
                        "id": {"display_name": "Id"},
                        "loyalty_tier": {"display_name": "Tier"},
                    },
                },
                "public.coupons": {
                    "display_name": "Coupons",
                    "columns": {"id": {"display_name": "Id"}},
                },
                keys[-1]: {"display_name": "Stored meanwhile", "columns": {}},
            }
        )
        resp, _, gen, _ = await self._run(
            http_client,
            model=self._complete(),
            current=(meanwhile, _snapshot()),
            schema=_schema(["orders", "coupons", *names], wide),
            known=self._SEEN,
            generated=self._annotate_all,
        )
        assert resp.status_code == 200
        gen.generate_new_columns.assert_not_awaited()
        warnings = resp.json()["summary"]["warnings"]
        [tables_warning] = [w for w in warnings if "more new table" in w]
        assert tables_warning.startswith("1 more new table was not annotated")
        assert keys[-2] in tables_warning and keys[-1] not in tables_warning
        [columns_warning] = [w for w in warnings if w.startswith("The new columns of")]
        assert columns_warning.startswith("The new columns of 1 more table were not annotated")
        assert "public.coupons" in columns_warning and "public.orders" not in columns_warning

    async def test_cut_columns_another_writer_stored_meanwhile_are_not_counted(
        self, http_client
    ) -> None:
        from app.routers.semantic import _DRIFT_MAX_COLUMNS_PER_BATCH as CAP

        extra = [f"c{i:03d}" for i in range(CAP + 2)]
        meanwhile = self._complete(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {name: {"display_name": name} for name in ["id", *extra[CAP:]]},
                },
                "public.coupons": {
                    "display_name": "Coupons",
                    "columns": {"id": {"display_name": "Id"}},
                },
            }
        )
        resp, _, _, _ = await self._run(
            http_client,
            model=self._complete(),
            current=(meanwhile, _snapshot()),
            schema=_schema(["orders", "coupons"], {"orders": ["id", *extra], "coupons": ["id"]}),
            known=self._SEEN,
            generated_columns={
                "public.orders": {c: ColumnSemantic(display_name=c) for c in extra[:CAP]}
            },
        )
        assert resp.status_code == 200
        assert not any("more new column(s)" in w for w in resp.json()["summary"]["warnings"])


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
