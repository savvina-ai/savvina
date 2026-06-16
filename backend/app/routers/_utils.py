# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Shared router utilities."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import delete, select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from ..models.cache import QueryCacheEntry
from ..models.connection import Connection
from ..models.table_embedding_cache import TableEmbeddingCache
from ..models.user_schema_cache import UserSchemaCache

_CACHE_MAX_AGE = 300


async def get_connection_or_404(
    conn_id: str,
    db: AsyncSession,
) -> Connection:
    """Fetch an active Connection by id or raise HTTP 404."""
    stmt = select(Connection).where(Connection.id == conn_id, Connection.is_active.is_(True))
    result = await db.execute(stmt)
    conn = result.scalar_one_or_none()
    if conn is None:
        raise HTTPException(status_code=404, detail=f"Connection '{conn_id}' not found")
    return conn


async def lock_and_reread_connection(conn_id: str, db: AsyncSession) -> Connection:
    """Re-fetch *conn_id* under ``FOR UPDATE``, bypassing the identity-map cache.

    Call immediately before composing a write to ``Connection.semantic_model``
    in any handler that read the connection earlier in the same request, so
    the write merges into the latest committed state rather than a stale
    snapshot from a concurrent request.
    """
    stmt = (
        select(Connection)
        .where(Connection.id == conn_id, Connection.is_active.is_(True))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    conn = result.scalar_one_or_none()
    if conn is None:
        raise HTTPException(status_code=404, detail=f"Connection '{conn_id}' not found")
    return conn


async def _invalidate_connection_caches(connection_id: str, db: AsyncSession) -> None:
    """Delete QueryCacheEntry, UserSchemaCache, and TableEmbeddingCache rows for
    *connection_id*.

    Call before ``db.commit()`` whenever the semantic model, schema, privacy
    settings, or connection config is mutated so that stale cached queries and
    schema data are not served on the next request.
    """
    await db.execute(delete(QueryCacheEntry).where(QueryCacheEntry.connection_id == connection_id))
    await db.execute(delete(UserSchemaCache).where(UserSchemaCache.connection_id == connection_id))
    await db.execute(
        delete(TableEmbeddingCache).where(TableEmbeddingCache.connection_id == connection_id)
    )


def cached_json_response(data: dict | list, request: Request) -> Response:
    """Return a JSON response with ETag and Cache-Control headers.

    If the client sends a matching ``If-None-Match`` header, return 304.
    """
    body = json.dumps(data, default=str, separators=(",", ":"))
    etag = '"' + hashlib.sha256(body.encode()).hexdigest()[:32] + '"'

    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag, "Vary": "Authorization"})

    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Cache-Control": f"private, max-age={_CACHE_MAX_AGE}, must-revalidate",
            "ETag": etag,
            "Vary": "Authorization",
        },
    )
