# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""In-memory SQLite fixture for the semantic store tests.

Foreign keys ON (SQLite defaults them OFF, which would make the fixture weaker
than production); only the tables these tests need, created via
``__table__.create`` rather than Alembic, so it proves nothing about
migration/model agreement (that is ``tests/test_migrations``' job). SQLite
stores ``DateTime(timezone=True)`` tz-naive, so a round-tripped timestamp comes
back naive.
"""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
from app.models.semantic_suggestion import SemanticSuggestion

_TABLES = (
    Connection,
    SemanticModelRow,
    SemanticTableRow,
    SemanticColumnRow,
    SemanticMetricRow,
    SemanticRelationshipRow,
    SemanticGlobalItemRow,
    SemanticHistoryRow,
    SemanticSuggestion,
)


@pytest.fixture
def semantic_tables() -> tuple[type, ...]:
    """The ORM models ``semantic_db`` creates. A module that needs more (the
    generation flow test also exercises the cache tables) overrides this."""
    return _TABLES


@pytest_asyncio.fixture
async def semantic_db(semantic_tables) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        for model in semantic_tables:
            await conn.run_sync(model.__table__.create)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def connection_id(semantic_db: AsyncSession) -> str:
    """Seed the parent ``Connection`` row and return its id.

    ``semantic_models.connection_id`` carries a real FK to ``connections.id`` and
    the fixture above enables ``PRAGMA foreign_keys=ON``, so any test that writes
    a ``SemanticModelRow`` needs this parent row first.
    """
    cid = "11111111-1111-1111-1111-111111111111"
    semantic_db.add(
        Connection(
            id=cid,
            name="test connection",
            source_type="postgresql",
            config_encrypted=b"",
        )
    )
    await semantic_db.commit()
    return cid
