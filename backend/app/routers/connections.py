# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Connections router — CRUD, test, schema refresh, privacy, execution mode."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
import json
import logging
from typing import TYPE_CHECKING
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import delete, func, select, update

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_active_user
from ..auth.limiter import limiter
from ..config import get_settings
from ..database import get_db
from ..datasources.adapters.postgresql import evict_pool as _evict_pg_pool
from ..datasources.models import PrivacySettings
from ..datasources.registry import create_datasource
from ..models.cache import QueryCacheEntry
from ..models.chat import ChatMessage, ChatSession
from ..models.connection import Connection
from ..models.example import VerifiedExample
from ..models.semantic_suggestion import SemanticSuggestion
from ..models.table_embedding_cache import TableEmbeddingCache
from ..models.user import User
from ..models.user_schema_cache import UserSchemaCache
from ..schemas.connection import (
    ConnectionConfigResponse,
    ConnectionConfigUpdate,
    ConnectionCreate,
    ConnectionDetail,
    ConnectionResponse,
    ConnectionTest,
    ExecutionModeUpdate,
    PrivacySettingsUpdate,
)
from ..schemas.pagination import PaginatedResponse
from ..utils.encryption import decrypt_value, encrypt_value
from ._utils import _invalidate_connection_caches, cached_json_response, get_connection_or_404

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/connections", tags=["connections"])

_MASK_SENTINEL = "**redacted**"
_SENSITIVE_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pass",
        "secret",
        "key",
        "api_key",
        "token",
        "private_key",
        "client_secret",
        "access_key",
        "secret_key",
    }
)


def _mask_config(config: dict) -> dict:
    """Return a copy of the config dict with sensitive field values replaced by a sentinel."""
    return {
        k: _MASK_SENTINEL if k.lower() in _SENSITIVE_CONFIG_KEYS else v for k, v in config.items()
    }


def _unmask_config(new_config: dict, existing_config: dict) -> dict:
    """Replace sentinel values in new_config with the original values from existing_config.

    Allows the UI to round-trip masked fields without the user re-entering credentials.
    """
    return {
        k: existing_config[k] if v == _MASK_SENTINEL and k in existing_config else v
        for k, v in new_config.items()
    }


# ── Test new connection before saving ─────────────────────────────────────────


@router.post("/test")
async def test_new_connection(
    body: ConnectionTest,
    _user: User = Depends(get_current_active_user),
) -> dict:
    """Test connectivity for a new connection config without saving it."""
    logger.info("Testing new connection: source_type=%s", body.source_type)
    try:
        adapter = create_datasource(body.source_type)
    except ValueError as e:
        logger.warning("Unknown source_type '%s': %s", body.source_type, e)
        raise HTTPException(status_code=400, detail=str(e)) from None

    result = await adapter.test_connection(body.config)
    if result.success:
        logger.info(
            "Connection test succeeded: source_type=%s, server_version=%s",
            body.source_type,
            result.server_version,
        )
        response: dict = {"success": True, "message": result.message}
        if result.server_version:
            response["server_version"] = result.server_version
        return response

    logger.warning(
        "Connection test failed: source_type=%s, error=%s",
        body.source_type,
        result.message,
    )
    raise HTTPException(status_code=400, detail=result.message)


# ── CRUD ────────────────────────────────────────────────────────────────────────


@router.post("", response_model=ConnectionResponse, status_code=status.HTTP_201_CREATED)
async def create_connection(
    body: ConnectionCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionResponse:
    existing = await db.scalar(
        select(func.count())
        .select_from(Connection)
        .where(Connection.name == body.name, Connection.is_active.is_(True))
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A connection named '{body.name}' already exists.",
        )
    settings = get_settings()
    config_encrypted = encrypt_value(json.dumps(body.config), settings.encryption_key)
    conn = Connection(
        id=str(uuid.uuid4()),
        name=body.name,
        source_type=body.source_type,
        config_encrypted=config_encrypted,
        privacy_settings=body.privacy_settings,
        execution_mode=body.execution_mode,
        is_active=True,
        created_at=datetime.now(UTC),
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return conn


@router.get("", response_model=PaginatedResponse[ConnectionResponse])
async def list_connections(
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    _user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[ConnectionResponse]:
    where = [Connection.is_active.is_(True)]
    total = await db.scalar(select(func.count()).select_from(Connection).where(*where)) or 0
    result = await db.execute(
        select(Connection)
        .where(*where)
        .order_by(Connection.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return PaginatedResponse(
        items=list(result.scalars().all()), total=total, limit=limit, offset=offset
    )


@router.get("/{connection_id}", response_model=ConnectionDetail)
async def get_connection(
    connection_id: str,
    _user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionDetail:
    return await get_connection_or_404(connection_id, db)


@router.post("/{connection_id}/test")
async def test_existing_connection(
    connection_id: str,
    _user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Test an existing saved connection."""
    logger.info("Testing existing connection: id=%s", connection_id)
    settings = get_settings()
    conn = await get_connection_or_404(connection_id, db)
    config_dict = json.loads(decrypt_value(conn.config_encrypted, settings.encryption_key))

    adapter = create_datasource(conn.source_type)
    result = await adapter.test_connection(config_dict)
    if result.success:
        logger.info(
            "Connection test succeeded: id=%s, name=%r, source_type=%s, server_version=%s",
            connection_id,
            conn.name,
            conn.source_type,
            result.server_version,
        )
        response: dict = {"success": True, "message": result.message}
        if result.server_version:
            response["server_version"] = result.server_version
        return response

    logger.warning(
        "Connection test failed: id=%s, name=%r, source_type=%s, error=%s",
        connection_id,
        conn.name,
        conn.source_type,
        result.message,
    )
    raise HTTPException(status_code=400, detail=result.message)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    connection_id: str,
    _user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    settings = get_settings()
    conn = await get_connection_or_404(connection_id, db)
    conn.is_active = False  # type: ignore[assignment]
    session_result = await db.execute(
        select(ChatSession.id).where(ChatSession.connection_id == connection_id)
    )
    session_ids = list(session_result.scalars().all())
    if session_ids:
        await db.execute(delete(ChatMessage).where(ChatMessage.session_id.in_(session_ids)))
        await db.execute(delete(ChatSession).where(ChatSession.connection_id == connection_id))
    await _invalidate_connection_caches(connection_id, db)
    await db.execute(delete(VerifiedExample).where(VerifiedExample.connection_id == connection_id))
    await db.execute(
        delete(SemanticSuggestion).where(SemanticSuggestion.connection_id == connection_id)
    )
    if conn.source_type == "postgresql":
        config_dict = json.loads(decrypt_value(conn.config_encrypted, settings.encryption_key))
        await _evict_pg_pool(config_dict)
    await db.commit()


# ── Schema cache ───────────────────────────────────────────────────────────────


@router.get("/{connection_id}/schema")
async def get_schema(
    request: Request,
    connection_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Return the cached DataSourceSchema dict for this connection."""
    await get_connection_or_404(connection_id, db)
    usc = await _get_user_schema_cache(connection_id, current_user.id, db)
    if not usc or not usc.schema_cache:
        raise HTTPException(status_code=404, detail="No schema cached — run a refresh first")
    return cached_json_response(usc.schema_cache, request)


@router.post("/{connection_id}/schema/refresh")
@limiter.limit("5/minute")
async def refresh_schema(
    request: Request,
    connection_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Re-introspect the data source and update the schema cache."""
    settings = get_settings()
    conn = await get_connection_or_404(connection_id, db)
    config_dict = json.loads(decrypt_value(conn.config_encrypted, settings.encryption_key))

    privacy = PrivacySettings.from_dict(conn.privacy_settings) if conn.privacy_settings else None
    adapter = create_datasource(conn.source_type)
    try:
        await adapter.connect(config_dict)
        schema = await adapter.introspect(privacy)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Schema refresh failed: {e}") from e
    finally:
        await adapter.disconnect()

    from ..semantic.generator import SemanticModelGenerator

    schema_dict = asdict(schema)
    schema_hash = SemanticModelGenerator().compute_schema_hash(schema)
    now = datetime.now(UTC)
    usc = await _get_user_schema_cache(connection_id, current_user.id, db)
    if usc is None:
        db.add(
            UserSchemaCache(
                connection_id=connection_id,
                user_id=current_user.id,
                schema_cache=schema_dict,
                schema_cached_at=now,
                schema_hash=schema_hash,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        usc.schema_cache = schema_dict
        usc.schema_cached_at = now
        usc.schema_hash = schema_hash
        usc.updated_at = now
    # Flush table embeddings so they are recomputed from the fresh schema on next request
    await db.execute(
        delete(TableEmbeddingCache).where(TableEmbeddingCache.connection_id == connection_id)
    )
    await db.execute(delete(QueryCacheEntry).where(QueryCacheEntry.connection_id == connection_id))
    await db.commit()
    return schema_dict


# ── Privacy & execution mode ───────────────────────────────────────────────────


@router.put("/{connection_id}/privacy", response_model=ConnectionDetail)
async def update_privacy(
    connection_id: str,
    body: PrivacySettingsUpdate,
    _user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionDetail:
    conn = await get_connection_or_404(connection_id, db)
    existing: dict = dict(conn.privacy_settings) if conn.privacy_settings else {}
    existing.update(body.model_dump(exclude_none=True))
    await db.execute(
        update(Connection).where(Connection.id == connection_id).values(privacy_settings=existing)
    )
    await _invalidate_connection_caches(connection_id, db)
    await db.commit()
    await db.refresh(conn)
    return conn


@router.put("/{connection_id}/execution-mode", response_model=ConnectionDetail)
async def update_execution_mode(
    connection_id: str,
    body: ExecutionModeUpdate,
    _user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionDetail:
    conn = await get_connection_or_404(connection_id, db)
    await db.execute(
        update(Connection)
        .where(Connection.id == connection_id)
        .values(execution_mode=body.execution_mode)
    )
    await db.commit()
    await db.refresh(conn)
    return conn


# ── Connection config (admin-managed shared config) ───────────────────────────


@router.get("/{connection_id}/config", response_model=ConnectionConfigResponse)
async def get_connection_config(
    connection_id: str,
    _user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionConfigResponse:
    """Return decrypted connection config so the UI can pre-fill the edit form."""
    settings = get_settings()
    conn = await get_connection_or_404(connection_id, db)
    config_dict = json.loads(decrypt_value(conn.config_encrypted, settings.encryption_key))
    return ConnectionConfigResponse(
        name=conn.name,
        source_type=conn.source_type,
        config=_mask_config(config_dict),
    )


@router.put("/{connection_id}/config", response_model=ConnectionResponse)
async def update_connection_config(
    connection_id: str,
    body: ConnectionConfigUpdate,
    _user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionResponse:
    """Update connection name and/or config; clears schema and query cache on config change."""
    settings = get_settings()
    conn = await get_connection_or_404(connection_id, db)
    values: dict = {}
    if body.name is not None:
        conflict = await db.scalar(
            select(func.count())
            .select_from(Connection)
            .where(
                Connection.name == body.name,
                Connection.is_active.is_(True),
                Connection.id != connection_id,
            )
        )
        if conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A connection named '{body.name}' already exists.",
            )
        values["name"] = body.name
    if body.config is not None:
        existing = json.loads(decrypt_value(conn.config_encrypted, settings.encryption_key))
        if _MASK_SENTINEL in body.config.values():
            merged = _unmask_config(body.config, existing)
        else:
            merged = body.config
        values["config_encrypted"] = encrypt_value(json.dumps(merged), settings.encryption_key)
    if values:
        values["updated_at"] = datetime.now(UTC)
        await db.execute(update(Connection).where(Connection.id == connection_id).values(**values))
        if body.config is not None:
            await _invalidate_connection_caches(connection_id, db)
            if conn.source_type == "postgresql":
                await _evict_pg_pool(existing)
        await db.commit()
        await db.refresh(conn)
    return conn


# ── Per-user schema cache ──────────────────────────────────────────────────────


async def _get_user_schema_cache(
    connection_id: str,
    user_id: str,
    db: AsyncSession,
) -> UserSchemaCache | None:
    """Load a user's schema cache entry for a connection."""
    result = await db.execute(
        select(UserSchemaCache).where(
            UserSchemaCache.connection_id == connection_id,
            UserSchemaCache.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()
