# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""The background drift monitor: which connections it checks, what it stores, and that it
never stops on one failing source. Mocks only: the comparison itself is
``drift_check.drift_report``, tested through the Check Drift route."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.semantic import DriftReport
from app.semantic.models import GenerationStatus, SemanticModel
from app.services import drift_monitor
from app.services.drift_check import DriftCheckUnavailableError
from app.services.schema_utils import SchemaRefreshError

_M = "app.services.drift_monitor"


def _conn(cid: str = "conn-1") -> MagicMock:
    conn = MagicMock()
    conn.id = cid
    conn.is_active = True
    return conn


def _header(status: GenerationStatus = GenerationStatus.COMPLETE) -> MagicMock:
    header = MagicMock()
    header.generation_status = status
    header.schema_hash = "h"
    return header


def _db() -> MagicMock:
    db = MagicMock()
    db.commit = AsyncMock()
    return db


def _report(actionable: bool = False) -> DriftReport:
    return DriftReport(
        connection_id="conn-1",
        warnings=["x"] if actionable else [],
        warning_count=1 if actionable else 0,
        checked_at=datetime.now(UTC).isoformat(),
        has_actionable_changes=actionable,
    )


class TestCheckConnection:
    @pytest.fixture(autouse=True)
    def _store(self):
        self.header = AsyncMock(return_value=_header())
        self.model = AsyncMock(return_value=SemanticModel())
        self.owner = AsyncMock(return_value="user-1")
        self.refresh = AsyncMock(return_value=MagicMock(name="schema"))
        self.report = AsyncMock(return_value=_report())
        with (
            patch(f"{_M}.semantic_store.load_version", new=AsyncMock(return_value=9)),
            patch(f"{_M}.semantic_store.load_header", new=self.header),
            patch(f"{_M}.semantic_store.load_model", new=self.model),
            patch(f"{_M}._owner_user_id", new=self.owner),
            patch(f"{_M}.refresh_user_schema_cache", new=self.refresh),
            patch(f"{_M}.drift_report", new=self.report),
        ):
            yield

    async def test_a_complete_model_is_checked_live_and_its_report_stored(self) -> None:
        db = _db()
        assert await drift_monitor.check_connection(db, _conn()) is True
        # The schema-cache refresh drops embeddings and cached answers when the hash moved.
        assert self.refresh.await_args.kwargs["flush"] == "changed"
        assert self.refresh.await_args.args[1] == "user-1"
        kwargs = self.report.await_args.kwargs
        assert kwargs["schema_source"] == "live" and kwargs["refresh_error"] is None
        assert kwargs["source"] == "monitor" and kwargs["model_version"] == 9
        # The read transaction ends before the introspection.
        db.commit.assert_awaited()

    @pytest.mark.parametrize("status", [GenerationStatus.TABLES_PARTIAL, GenerationStatus.IDLE])
    async def test_a_model_that_is_not_complete_is_skipped(self, status) -> None:
        self.header.return_value = _header(status)
        assert await drift_monitor.check_connection(_db(), _conn()) is False
        self.refresh.assert_not_awaited()

    async def test_a_connection_without_a_model_is_skipped(self) -> None:
        self.header.return_value = None
        assert await drift_monitor.check_connection(_db(), _conn()) is False
        self.refresh.assert_not_awaited()

    async def test_a_connection_with_no_single_user_is_skipped(self) -> None:
        self.owner.return_value = None
        assert await drift_monitor.check_connection(_db(), _conn()) is False
        self.refresh.assert_not_awaited()

    async def test_an_unreachable_source_with_a_cache_reports_from_the_cache(self) -> None:
        self.refresh.side_effect = SchemaRefreshError("connection refused")
        cache = MagicMock()
        cache.schema_cache = {"tables": []}
        cache.schema_cached_at = datetime(2026, 11, 3, tzinfo=UTC)
        db = _db()
        db.scalar = AsyncMock(return_value=cache)
        with patch(f"{_M}._schema_from_dict", return_value=MagicMock(name="cached")):
            assert await drift_monitor.check_connection(db, _conn()) is True
        kwargs = self.report.await_args.kwargs
        assert kwargs["schema_source"] == "cache"
        assert kwargs["refresh_error"] == "connection refused"
        assert kwargs["schema_cached_at"] == "2026-11-03T00:00:00+00:00"

    async def test_an_unreachable_source_without_a_cache_stores_nothing(self) -> None:
        self.refresh.side_effect = SchemaRefreshError("connection refused")
        db = _db()
        db.scalar = AsyncMock(return_value=None)
        assert await drift_monitor.check_connection(db, _conn()) is False
        self.report.assert_not_awaited()

    async def test_a_lock_timeout_is_a_busy_connection_not_a_crash(self) -> None:
        from sqlalchemy.exc import OperationalError

        db = _db()
        await drift_monitor.check_connection(db, _conn())
        lock = self.report.await_args.kwargs["lock"]
        timeout = OperationalError("SELECT", {}, Exception("lock timeout"))
        db.scalar = AsyncMock(side_effect=timeout)
        with (
            patch(f"{_M}.is_lock_timeout", return_value=True),
            pytest.raises(DriftCheckUnavailableError) as info,
        ):
            await lock()
        assert info.value.gone is False

    async def test_the_lock_it_passes_raises_a_domain_error_when_the_connection_is_gone(
        self,
    ) -> None:
        db = _db()
        await drift_monitor.check_connection(db, _conn())
        lock = self.report.await_args.kwargs["lock"]
        db.scalar = AsyncMock(return_value=None)
        with pytest.raises(DriftCheckUnavailableError) as info:
            await lock()
        assert info.value.gone is True


class TestOwnerUserId:
    @staticmethod
    def _db(*answers: list[str]) -> MagicMock:
        db = MagicMock()
        results = []
        for ids in answers:
            r = MagicMock()
            r.all.return_value = ids
            results.append(r)
        db.scalars = AsyncMock(side_effect=results)
        return db

    @pytest.mark.parametrize(
        ("owners", "users", "expected"),
        [
            (["u1"], None, "u1"),  # the cache's one active owner
            (["u1", "u2"], None, None),  # several owners: skipped
            ([], ["u9"], "u9"),  # no cache yet: the single active user (CE)
            ([], ["u8", "u9"], None),  # no cache and several users: skipped
            ([], [], None),
        ],
    )
    async def test_picks_one_user_or_none(self, owners, users, expected) -> None:
        answers = [owners] if users is None else [owners, users]
        assert await drift_monitor._owner_user_id(self._db(*answers), _conn()) == expected


class TestCheckAllOnce:
    @staticmethod
    def _sessions(ids: list[str]):
        """A session factory: the first session lists *ids*, each later one gets a
        connection by id."""

        @asynccontextmanager
        async def factory():
            db = _db()
            scalars = MagicMock()
            scalars.all.return_value = ids
            db.scalars = AsyncMock(return_value=scalars)
            db.get = AsyncMock(side_effect=lambda _model, cid: _conn(cid))
            yield db

        return factory

    async def test_one_failing_connection_does_not_stop_the_next(self) -> None:
        calls: list[str] = []

        async def check(_db, conn):
            calls.append(conn.id)
            if conn.id == "a":
                raise RuntimeError("boom")
            if conn.id == "b":
                raise DriftCheckUnavailableError("the connection was deleted", gone=True)
            return True

        with (
            patch(f"{_M}.database.async_session_maker", new=self._sessions(["a", "b", "c"])),
            patch(f"{_M}.check_connection", new=check),
        ):
            assert await drift_monitor.check_all_once() == ["c"]
        assert calls == ["a", "b", "c"]


class TestRunForever:
    async def test_sleeps_the_interval_first_then_checks_and_never_applies(self) -> None:
        sleeps: list[float] = []

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise asyncio.CancelledError

        check = AsyncMock(return_value=[])
        with (
            patch(f"{_M}.asyncio.sleep", new=sleep),
            patch(f"{_M}.check_all_once", new=check),
            pytest.raises(asyncio.CancelledError),
        ):
            await drift_monitor.run_forever(15)
        assert sleeps == [900, 900]
        check.assert_awaited_once()
        # Detect only: the module has no path to the apply.
        assert "apply_drift" not in vars(drift_monitor)

    async def test_a_failing_pass_is_logged_and_the_loop_goes_on(self) -> None:
        sleeps: list[float] = []

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                raise asyncio.CancelledError

        check = AsyncMock(side_effect=[RuntimeError("db down"), []])
        with (
            patch(f"{_M}.asyncio.sleep", new=sleep),
            patch(f"{_M}.check_all_once", new=check),
            pytest.raises(asyncio.CancelledError),
        ):
            await drift_monitor.run_forever(1)
        assert check.await_count == 2
