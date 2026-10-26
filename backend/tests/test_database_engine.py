# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""The app engine bounds every row-lock wait server-side."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from app import database

if TYPE_CHECKING:
    import pytest


def _settings(lock_timeout_seconds: int) -> SimpleNamespace:
    return SimpleNamespace(
        database_url="postgresql+asyncpg://u:p@h/db",
        debug=False,
        db_pool_size=1,
        db_max_overflow=0,
        db_lock_timeout_seconds=lock_timeout_seconds,
    )


def test_engine_sets_a_server_side_lock_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(database, "get_settings", lambda: _settings(7))
    monkeypatch.setattr(
        database, "create_async_engine", lambda url, **kwargs: captured.update(kwargs) or object()
    )
    database._make_engine()
    assert captured["connect_args"] == {"server_settings": {"lock_timeout": "7000"}}


def test_zero_disables_the_lock_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(database, "get_settings", lambda: _settings(0))
    monkeypatch.setattr(
        database, "create_async_engine", lambda url, **kwargs: captured.update(kwargs) or object()
    )
    database._make_engine()
    assert captured["connect_args"] == {"server_settings": {"lock_timeout": "0"}}


async def test_rebuild_engine_swaps_in_an_engine_built_by_the_one_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _OldEngine:
        async def dispose(self) -> None:
            calls.append("dispose")

    new_engine = object()
    monkeypatch.setattr(database, "engine", _OldEngine())
    monkeypatch.setattr(database, "async_session_maker", None)
    monkeypatch.setattr(database, "_make_engine", lambda: calls.append("make") or new_engine)
    await database.rebuild_engine()
    assert calls == ["dispose", "make"]
    assert database.engine is new_engine
    assert database.async_session_maker.kw["bind"] is new_engine
    assert database.async_session_maker.kw["expire_on_commit"] is False
