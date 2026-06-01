# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Shared fixtures and helpers for router tests."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from app.auth.dependencies import get_current_active_user, get_token_payload
from app.main import app
from app.models.user import User

_MOCK_TOKEN_PAYLOAD = {
    "jti": "test-jti-00000000-0000-0000-0000-000000000000",
    "exp": 9_999_999_999,
    "sub": "admin-1",
    "type": "access",
}

# ── Mock result helper ────────────────────────────────────────────────────────


class MockResult:
    """Lightweight stand-in for a SQLAlchemy `CursorResult` / `ChunkedIteratorResult`."""

    def __init__(
        self,
        single=None,
        rows: list | None = None,
        row=None,
    ):
        self._single = single
        self._rows = rows if rows is not None else []
        self._row = row

    def scalar(self):
        return self._single

    def scalar_one_or_none(self):
        return self._single

    def scalar_one(self):
        if self._single is None:
            raise RuntimeError("No row found")
        return self._single

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        if self._rows:
            return self._rows[0]
        return self._single

    def one(self):
        return self._row

    def one_or_none(self):
        return self._row

    def __iter__(self):
        return iter(self._rows)


# ── Factory helpers ───────────────────────────────────────────────────────────


def _mock_db(*results) -> MagicMock:
    """Build a mock AsyncSession returning the given MockResult objects in order."""
    session = MagicMock()
    session.execute = AsyncMock(side_effect=list(results))
    session.scalar = AsyncMock(return_value=0)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    session.delete = AsyncMock()
    session.merge = AsyncMock()
    # Support `async with db.begin()` — the context manager commits on exit.
    # __aexit__ returns False (not True) to match real SQLAlchemy behaviour:
    # returning True would suppress exceptions raised inside the `async with`
    # block, which would silently swallow errors and mask test failures.
    _txn = AsyncMock()
    _txn.__aenter__ = AsyncMock(return_value=None)
    _txn.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=_txn)
    return session


def _make_conn(**kwargs) -> MagicMock:
    """Return a MagicMock shaped like a Connection ORM row."""
    now = datetime.now(UTC)
    m = MagicMock()
    m.id = kwargs.get("id", "conn-123")
    m.name = kwargs.get("name", "Test DB")
    m.source_type = kwargs.get("source_type", "postgresql")
    m.execution_mode = kwargs.get("execution_mode", "auto_execute")
    m.is_active = kwargs.get("is_active", True)
    m.semantic_model_updated_at = kwargs.get("semantic_model_updated_at")
    m.created_at = kwargs.get("created_at", now)
    m.updated_at = kwargs.get("updated_at", now)
    m.privacy_settings = kwargs.get("privacy_settings")
    m.semantic_model = kwargs.get("semantic_model")
    m.config_encrypted = kwargs.get("config_encrypted", b"fake-encrypted-config")
    return m


def _make_admin_user() -> User:
    """Return a MagicMock shaped like an admin User for router tests."""
    user = MagicMock(spec=User)
    user.id = "admin-1"
    user.email = "admin@test.com"
    user.display_name = "Admin User"
    user.is_active = True
    return user


def _make_regular_user() -> User:
    """Return a MagicMock shaped like a regular User for router tests."""
    user = MagicMock(spec=User)
    user.id = "user-1"
    user.email = "user@test.com"
    user.display_name = "Test User"
    user.is_active = True
    return user


def _default_user() -> User:
    """Return a minimal admin User suitable for auth bypass in router tests."""
    return _make_admin_user()


def _mock_auth(app, user: User | None = None) -> None:
    """Override auth dependency with a test user."""
    test_user = user or _default_user()
    app.dependency_overrides[get_current_active_user] = lambda: test_user


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def apply_default_auth():
    """Apply default auth mock for all router tests (can be overridden per-test)."""
    _mock_auth(app)
    app.dependency_overrides[get_token_payload] = lambda: _MOCK_TOKEN_PAYLOAD
    yield


@pytest.fixture(autouse=True)
def clear_overrides(apply_default_auth):
    """Ensure dependency overrides don't bleed between tests."""
    yield
    app.dependency_overrides.clear()


@pytest.fixture
async def http_client():
    """AsyncClient wired to the FastAPI app."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client
