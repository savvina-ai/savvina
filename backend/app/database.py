# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Async SQLAlchemy engine, session factory, and PostgreSQL connection pool."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    s = get_settings()
    return create_async_engine(
        s.database_url,
        echo=s.debug,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=s.db_pool_size,
        max_overflow=s.db_max_overflow,
        connect_args={"server_settings": {"lock_timeout": str(s.db_lock_timeout_seconds * 1000)}},
    )


engine = _make_engine()

async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


async def rebuild_engine() -> None:
    """Replace the module engine and session factory from the current settings.

    Lifespan calls this after overlaying persisted pool settings onto the cached
    ``Settings``; ``_make_engine`` stays the only place an app engine is built, so every
    connection option (pool sizing, ``lock_timeout``) applies to the rebuilt engine too.
    """
    global engine, async_session_maker
    await engine.dispose()
    engine = _make_engine()
    async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session
