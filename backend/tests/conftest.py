# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""
Top-level test configuration.

IMPORTANT: All env vars must be set *before* any app module is imported,
because `database.py` calls `get_settings()` at module level when creating the
SQLAlchemy engine.  Setting them here (conftest.py is loaded first by pytest)
guarantees the correct order.  The DATABASE_URL points to a PostgreSQL test
database; all router tests mock the DB session so no real connection is made.
"""

from contextlib import asynccontextmanager
import os
from unittest.mock import AsyncMock

# Valid Fernet key used only for tests (base64-url encoding of 32 bytes)
TEST_ENCRYPTION_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="

# ── Required env vars ─────────────────────────────────────────────────────────
# Force test keys regardless of host/container env so local and Docker runs match.
# IMPORTANT: All keys must be set BEFORE any app module is imported.
os.environ["ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
os.environ["JWT_SECRET_KEY"] = "a" * 64  # 64-char key satisfies the >= 32 char validator
os.environ["DATABASE_URL"] = "postgresql+asyncpg://savvina:changeme@db:5432/savvina_test"

# Clear the lru_cache so a fresh Settings() is built from the env vars above
from app.config import get_settings  # noqa: E402

get_settings.cache_clear()


# ── asyncpg mock helpers ──────────────────────────────────────────────────────
# Defined here so they are importable from any test module.


class FakeRecord(dict):
    """Dict subclass that mimics asyncpg.Record for testing.

    asyncpg.Record supports subscript access (record["col"]) and .keys().
    A plain dict subclass provides both without any extra work.
    """


class MockConnection:
    """Lightweight stand-in for an asyncpg Connection."""

    def __init__(self, version: str = "PostgreSQL 15.0 (test)"):
        self.fetchval = AsyncMock(return_value=version)
        self.fetch = AsyncMock(return_value=[])
        self.fetchrow = AsyncMock(return_value=None)
        self.execute = AsyncMock(return_value=None)
        self.close = AsyncMock()

    @asynccontextmanager
    async def transaction(self):
        """Simulate an asyncpg transaction context manager."""
        yield


class MockPool:
    """Lightweight stand-in for an asyncpg Pool."""

    def __init__(self, conn: MockConnection):
        self._conn = conn
        self.close = AsyncMock()

    def is_closing(self) -> bool:
        return False

    @asynccontextmanager
    async def acquire(self):
        yield self._conn
