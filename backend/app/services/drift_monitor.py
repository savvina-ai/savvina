# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Background schema drift check (``DRIFT_CHECK_INTERVAL_MINUTES``; 0 disables it).

Every interval, each active connection whose semantic model is complete is introspected
and compared with its model (``drift_check.drift_report``, the comparison behind
``GET .../semantic/drift``). The report is stored on the model's header and served by
``GET .../semantic/drift/last``, which the Semantic Model page polls to show a banner.
The introspection goes through ``refresh_user_schema_cache(flush="changed")``, which
stores the schema cache and, when the schema hash moved, drops the connection's table
embeddings and cached answers: chat then reads the current schema, not the stale one.

Detect only (decision A1, 2026-11-03): the monitor never runs Apply Drift, so no LLM call
is made unattended; the user applies from the page.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from .. import database
from ..config import get_settings
from ..db_errors import is_lock_timeout
from ..models.connection import Connection
from ..models.user import User
from ..models.user_schema_cache import UserSchemaCache
from ..semantic import store as semantic_store
from ..semantic.models import GenerationStatus
from .drift_check import DriftCheckUnavailableError, drift_report
from .schema_utils import SchemaRefreshError, _schema_from_dict, refresh_user_schema_cache

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def _owner_user_id(db: AsyncSession, conn: Connection) -> str | None:
    """The user whose schema cache the check refreshes: the active owner of the
    connection's ``UserSchemaCache`` row, else the single active user (Community Edition
    has exactly one). ``None`` when there is no such user or more than one candidate (the
    check refreshes one user's cache and would leave the others stale), and the
    connection is skipped."""
    owners = (
        await db.scalars(
            select(UserSchemaCache.user_id)
            .join(User, User.id == UserSchemaCache.user_id)
            .where(UserSchemaCache.connection_id == conn.id, User.is_active.is_(True))
            .distinct()
            .limit(2)
        )
    ).all()
    if owners:
        return owners[0] if len(owners) == 1 else None
    users = (await db.scalars(select(User.id).where(User.is_active.is_(True)).limit(2))).all()
    return users[0] if len(users) == 1 else None


async def _lock(db: AsyncSession, connection_id: str) -> None:
    """The connection row lock ``drift_report`` takes before it writes the baseline, as
    the routes take it (``FOR NO KEY UPDATE``), raising ``DriftCheckUnavailableError``
    instead of HTTP when the connection is gone or inactive."""
    try:
        found = await db.scalar(
            select(Connection.id)
            .where(Connection.id == connection_id, Connection.is_active.is_(True))
            .with_for_update(key_share=True)
        )
    except DBAPIError as exc:
        if is_lock_timeout(exc):
            raise DriftCheckUnavailableError("the connection is busy", gone=False) from exc
        raise
    if found is None:
        raise DriftCheckUnavailableError("the connection was deleted", gone=True)


async def check_connection(db: AsyncSession, conn: Connection) -> bool:
    """Check one connection; ``True`` when a report was stored. Skips (``False``, one log
    line) a connection with no complete model, no user to refresh for, or an unreachable
    source with no cached schema to fall back on."""
    header = await semantic_store.load_header(db, conn.id)
    if header is None or header.generation_status != GenerationStatus.COMPLETE:
        return False  # no model, or a run in progress: nothing to compare yet
    user_id = await _owner_user_id(db, conn)
    if user_id is None:
        logger.info("drift monitor: %s skipped: no single user owns its schema cache", conn.id)
        return False
    # The version before the content, as the route reads it (see ``drift_report``).
    model_version = await semantic_store.load_version(db, conn.id)
    stored_model = await semantic_store.load_model(db, conn.id)
    if stored_model is None:
        return False
    # End the read transaction before the introspection, which can take a while: the
    # session would otherwise sit idle in transaction on a pooled connection
    # (``expire_on_commit=False`` keeps what was loaded).
    await db.commit()

    schema_source: Literal["live", "cache"] = "live"
    schema_cached_at: str | None = None
    refresh_error: str | None = None
    try:
        schema = await refresh_user_schema_cache(
            conn, user_id, db, get_settings().encryption_key, flush="changed"
        )
    except SchemaRefreshError as exc:
        cache = await db.scalar(
            select(UserSchemaCache).where(
                UserSchemaCache.connection_id == conn.id, UserSchemaCache.user_id == user_id
            )
        )
        if cache is None or not cache.schema_cache:
            logger.warning("drift monitor: %s unreachable and nothing cached: %s", conn.id, exc)
            return False
        logger.warning(
            "drift monitor: %s unreachable, compared the cached schema: %s", conn.id, exc
        )
        schema = _schema_from_dict(cache.schema_cache)
        schema_source = "cache"
        schema_cached_at = cache.schema_cached_at.isoformat() if cache.schema_cached_at else None
        refresh_error = str(exc)

    report = await drift_report(
        db,
        conn,
        stored_model,
        schema,
        schema_source=schema_source,
        schema_cached_at=schema_cached_at,
        refresh_error=refresh_error,
        lock=lambda: _lock(db, conn.id),
        source="monitor",
        model_version=model_version,
    )
    if report.has_actionable_changes:
        logger.warning(
            "drift monitor: %s: %d drift warning(s); apply them from the Semantic Model page",
            conn.id,
            report.warning_count,
        )
    else:
        logger.info("drift monitor: %s checked, no actionable drift", conn.id)
    return True


async def check_all_once() -> list[str]:
    """One pass over every active connection, each in its own session and its own
    try/except, so one unreachable or broken source never stops the others. Returns the
    ids of the connections a report was stored for."""
    # ``database.async_session_maker`` read at call time: ``rebuild_engine`` replaces it.
    async with database.async_session_maker() as db:
        ids = list(
            (await db.scalars(select(Connection.id).where(Connection.is_active.is_(True)))).all()
        )
    checked: list[str] = []
    for connection_id in ids:
        try:
            async with database.async_session_maker() as db:
                conn = await db.get(Connection, connection_id)
                if conn is None or not conn.is_active:
                    continue
                if await check_connection(db, conn):
                    checked.append(connection_id)
        except DriftCheckUnavailableError as exc:
            logger.info("drift monitor: %s skipped: %s", connection_id, exc)
        except Exception:
            logger.exception("drift monitor: checking %s failed", connection_id)
    return checked


async def run_forever(interval_minutes: int) -> None:
    """Run ``check_all_once`` every *interval_minutes*, first after one interval (the app
    has just started). A failing pass is logged and the loop goes on; cancelling the task
    (at shutdown) ends it. Detect only: nothing here applies drift."""
    while True:
        await asyncio.sleep(interval_minutes * 60)
        try:
            checked = await check_all_once()
            logger.info("drift monitor: checked %d connection(s)", len(checked))
        except Exception:
            logger.exception("drift monitor: pass failed")
