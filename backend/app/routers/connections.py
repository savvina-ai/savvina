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
from ..datasources.registry import create_datasource, get_datasource_class
from ..models.chat import ChatMessage, ChatSession
from ..models.connection import Connection
from ..models.example import VerifiedExample
from ..models.semantic_suggestion import SemanticSuggestion
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
from ..semantic import store as semantic_store
from ..services.row_filter import RowFilterInvalidError, validate_row_filters
from ..services.schema_utils import SchemaRefreshError, refresh_user_schema_cache
from ..utils.encryption import decrypt_value, encrypt_value
from ._utils import (
    _invalidate_connection_caches,
    cached_json_response,
    get_connection_or_404,
    lock_and_reread_connection,
)

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


def _query_dialect(source_type: str) -> str:
    """The adapter's dialect name, for row-filter validation; ``""`` (every scanner) when
    the source type is not registered."""
    try:
        return get_datasource_class(source_type).query_dialect
    except ValueError:
        return ""


_LEGACY_FILTER_DETAIL = (
    "row_filters: this connection still has the old whole-query filter. Define per-table"
    " filters, or send clear_legacy_row_filter to remove it."
)


def _validated_row_filters(raw: object, source_type: str) -> dict[str, str]:
    """*raw* as ``validate_row_filters`` returns it, or HTTP 422 with the reason."""
    try:
        return validate_row_filters(raw, _query_dialect(source_type))
    except RowFilterInvalidError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
    privacy = body.privacy_settings
    if privacy is not None:
        # The legacy whole-query filter is never written: a new connection cannot be born
        # in the migration state.
        privacy = {k: v for k, v in privacy.items() if k != "row_filter_sql"}
        if "row_filters" in privacy:
            privacy["row_filters"] = _validated_row_filters(
                privacy["row_filters"], body.source_type
            )
    settings = get_settings()
    config_encrypted = encrypt_value(json.dumps(body.config), settings.encryption_key)
    conn = Connection(
        id=str(uuid.uuid4()),
        name=body.name,
        source_type=body.source_type,
        config_encrypted=config_encrypted,
        privacy_settings=privacy,
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
    # Soft-delete never fires ON DELETE CASCADE, so the semantic rows (and the legacy
    # blob, which the store clears with them) go explicitly.
    await semantic_store.delete_model(db, connection_id, changed_by=_user.id)
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
    try:
        schema = await refresh_user_schema_cache(
            conn, current_user.id, db, settings.encryption_key, flush="always"
        )
    except SchemaRefreshError as e:
        raise HTTPException(status_code=400, detail=f"Schema refresh failed: {e}") from e
    await db.commit()
    return asdict(schema)


# ── Privacy & execution mode ───────────────────────────────────────────────────


@router.put("/{connection_id}/privacy", response_model=ConnectionDetail)
async def update_privacy(
    connection_id: str,
    body: PrivacySettingsUpdate,
    _user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionDetail:
    # Row lock: two simultaneous saves cannot both read the old JSON and let the later one
    # overwrite the earlier one's map. A stale tab sending an old map is not covered.
    conn = await lock_and_reread_connection(connection_id, db)
    prior: dict = dict(conn.privacy_settings) if conn.privacy_settings else {}
    existing: dict = dict(prior)
    changes = body.model_dump(exclude_none=True, exclude={"clear_legacy_row_filter"})
    existing.update(changes)
    legacy = bool(existing.get("row_filter_sql"))
    if body.row_filters is not None:
        existing["row_filters"] = _validated_row_filters(body.row_filters, conn.source_type)
        if existing["row_filters"]:
            # Defining real filters ends the migration state.
            existing.pop("row_filter_sql", None)
        elif legacy and not body.clear_legacy_row_filter:
            # An empty map must not drop the old filter silently (an unrelated save sends
            # the whole form): the admin removes it explicitly or defines filters.
            raise HTTPException(status_code=422, detail=_LEGACY_FILTER_DETAIL)
    if body.clear_legacy_row_filter:
        existing.pop("row_filter_sql", None)
        existing.setdefault("row_filters", {})
    await db.execute(
        update(Connection).where(Connection.id == connection_id).values(privacy_settings=existing)
    )
    # A row-filter change does not alter introspection: keep the schema cache so the page's
    # schema read does not 404 after the save. Cached answers hold only generated SQL, which
    # runs through the row-filter rewrite again on every hit, so flushing them (and the
    # embeddings, which the helper flushes together with them) is a safe extra, not a
    # correctness need.
    # The UI sends the whole form, so look at stored values, not at the keys sent. A key
    # missing from storage counts as changed.
    changed = {k for k, v in changes.items() if k not in prior or prior[k] != v}
    await _invalidate_connection_caches(connection_id, db, keep_schema=changed <= {"row_filters"})
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
