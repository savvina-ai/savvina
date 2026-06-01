# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Chat router — NL-to-SQL pipeline, sessions, cache management, and examples."""

from __future__ import annotations

from functools import lru_cache
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import case, delete, func, select

from ..auth.dependencies import get_current_active_user
from ..auth.limiter import limiter
from ..auth.tokens import decode_access_token
from ..cache.example_library import ExampleLibrary
from ..cache.query_cache import QueryCache
from ..config import get_settings
from ..database import get_db
from ..datasources.registry import create_datasource
from ..models.cache import QueryCacheEntry, QueryCacheStats
from ..models.chat import ChatMessage, ChatSession
from ..models.example import VerifiedExample
from ..models.user import User
from ..schemas.cache import (
    CacheEntryResponse,
    CacheStatsResponse,
    ExampleCreate,
    ExampleResponse,
    ExampleUpdate,
    TopCachedQuery,
)
from ..schemas.chat import (
    ChatRequest,
    ChatResponse,
    EditAndExecuteRequest,
    FeedbackRequest,
    MessageResponse,
    QueryResultsResponse,
    SessionResponse,
    ShareRequest,
    ShareResponse,
    SortRequest,
)
from ..schemas.pagination import PaginatedResponse
from ..services.chat_service import ChatService, ResourceNotFoundError
from ..services.sse_utils import format_sse_event
from ._utils import get_connection_or_404

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


@lru_cache(maxsize=1)
def _get_shared_cache() -> QueryCache:
    s = get_settings()
    return QueryCache(s.embedding_model, s.semantic_similarity_threshold, s.cache_max_age_days)


@lru_cache(maxsize=1)
def _get_shared_examples() -> ExampleLibrary:
    return ExampleLibrary()


def _make_chat_service() -> ChatService:
    return ChatService(_get_shared_cache(), _get_shared_examples())


def _chat_rate_limit_key(request: Request) -> str:
    """Rate-limit POST /chat by authenticated user ID, falling back to IP."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            payload = decode_access_token(auth[7:])
            sub = payload.get("sub")
            if sub:
                return str(sub)
        except HTTPException:
            # Expired / invalid token — fall back to IP so per-user budget is lost
            # only for that specific request, not silently swallowed for all errors.
            logger.debug("Rate-limit key: token invalid, falling back to client IP")
    return request.client.host if request.client else "unknown"


# ── Chat pipeline ──────────────────────────────────────────────────────────────


@router.post("")
@limiter.limit("20/minute", key_func=_chat_rate_limit_key)
async def process_message(
    request: Request,
    body: ChatRequest,
    current_user: User = Depends(get_current_active_user),
) -> StreamingResponse:
    """Run the NL-to-SQL pipeline, streaming typed SSE events as the pipeline progresses.

    The DB session is managed inside ``stream_message`` rather than injected here,
    so pool slots are never held for the duration of the HTTP connection (PERF-5).

    Client-disconnect detection is handled by polling ``request.is_disconnected()``
    after each yielded event and calling ``aclose()`` on the generator when detected.
    This cancels in-flight LLM calls and releases all DB connections immediately
    rather than waiting for the 120 s provider timeout (PROD-6).
    """
    service = _make_chat_service()

    async def event_generator():
        stream = service.stream_message(
            connection_id=body.connection_id,
            session_id=body.session_id,
            message=body.message,
            provider_name=body.provider,
            options=body.options.model_dump(),
            user_id=current_user.id,
        )
        try:
            async for event in stream:
                yield format_sse_event(event)
                if await request.is_disconnected():
                    logger.info(
                        "Client disconnected; cancelling SSE stream for user %s",
                        current_user.id,
                    )
                    break
        finally:
            await stream.aclose()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/execute/{message_id}", response_model=ChatResponse)
@limiter.limit("30/minute")
async def execute_pending(
    request: Request,
    message_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    """Execute a query that is awaiting approval (review_first mode)."""
    service = _make_chat_service()
    try:
        return await service.execute_pending(
            message_id=message_id,
            db=db,
            user_id=current_user.id,
        )
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@router.post("/edit/{message_id}", response_model=ChatResponse)
@limiter.limit("30/minute")
async def edit_and_execute(
    request: Request,
    message_id: str,
    body: EditAndExecuteRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    """Execute a user-edited version of a generated query."""
    service = _make_chat_service()
    try:
        return await service.edit_and_execute(
            message_id=message_id,
            edited_query=body.edited_query,
            db=db,
            user_id=current_user.id,
        )
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@router.post("/sort/{message_id}", response_model=QueryResultsResponse)
@limiter.limit("30/minute")
async def sort_results(
    request: Request,
    message_id: str,
    body: SortRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> QueryResultsResponse:
    """Re-execute the original query with ORDER BY injected for server-side sort."""
    service = _make_chat_service()
    try:
        return await service.sort_and_execute(
            message_id=message_id,
            sort_column=body.sort_column,
            sort_order=body.sort_order,
            db=db,
            user_id=current_user.id,
        )
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@router.post("/feedback/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def submit_feedback(
    request: Request,
    message_id: str,
    body: FeedbackRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Submit thumbs-up/down feedback on an assistant message."""
    service = _make_chat_service()
    try:
        connection_id = await service.submit_feedback(
            message_id=message_id,
            feedback=body.feedback,
            db=db,
            user_id=current_user.id,
        )
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    if body.semantic_correction:
        from ..models.semantic_suggestion import SemanticSuggestion

        if connection_id:
            correction = body.semantic_correction
            suggestion = SemanticSuggestion(
                connection_id=connection_id,
                table_key=correction.table_key,
                field=correction.field,
                correction_type=correction.correction_type,
                value=correction.value,
                source_message_id=message_id,
            )
            db.add(suggestion)
            await db.commit()


# ── Share ──────────────────────────────────────────────────────────────────────


@router.delete("/feedback/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def retract_feedback(
    request: Request,
    message_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Retract previously submitted feedback on an assistant message.

    For thumbs-up: also removes the verified example that was added to the
    example library, so it no longer influences future few-shot prompting.
    For thumbs-down: clears the feedback field only — evicted cache entries
    cannot be restored.
    """
    service = _make_chat_service()
    try:
        await service.retract_feedback(
            message_id=message_id,
            db=db,
            user_id=current_user.id,
        )
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@router.post("/messages/{message_id}/share", response_model=ShareResponse)
async def share_message(
    message_id: str,
    body: ShareRequest | None = None,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ShareResponse:
    """Generate (or retrieve) a public share token for an executed message.

    Pass ``{"expires_in_days": N}`` to set an optional expiry (1-365 days).
    """
    from datetime import UTC, datetime, timedelta
    import uuid as _uuid

    msg = await db.scalar(
        select(ChatMessage)
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .where(
            ChatMessage.id == message_id,
            ChatSession.user_id == current_user.id,
        )
    )
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.status not in ("executed", "cached"):
        raise HTTPException(status_code=400, detail="Only executed results can be shared")
    if not msg.share_token:
        msg.share_token = str(_uuid.uuid4())
    if body and body.expires_in_days is not None:
        days = max(1, min(body.expires_in_days, 365))
        msg.share_expires_at = datetime.now(UTC) + timedelta(days=days)
    await db.commit()
    return ShareResponse(share_token=msg.share_token, share_expires_at=msg.share_expires_at)


@router.post("/sessions/{session_id}/share", response_model=ShareResponse)
async def share_session(
    session_id: str,
    body: ShareRequest | None = None,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ShareResponse:
    """Generate (or retrieve) a public share token for an entire session.

    Pass ``{"expires_in_days": N}`` to set an optional expiry (1-365 days).
    """
    from datetime import UTC, datetime, timedelta
    import uuid as _uuid

    stmt = select(ChatSession).where(
        ChatSession.id == session_id,
        ChatSession.user_id == current_user.id,
    )
    session = await db.scalar(stmt)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.share_token:
        session.share_token = str(_uuid.uuid4())
    if body and body.expires_in_days is not None:
        days = max(1, min(body.expires_in_days, 365))
        session.share_expires_at = datetime.now(UTC) + timedelta(days=days)
    await db.commit()
    return ShareResponse(share_token=session.share_token, share_expires_at=session.share_expires_at)


# ── Sessions ───────────────────────────────────────────────────────────────────


@router.get("/sessions", response_model=PaginatedResponse[SessionResponse])
async def list_sessions(
    connection_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[SessionResponse]:
    count_stmt = select(func.count()).select_from(ChatSession)
    data_stmt = (
        select(
            ChatSession,
            func.count(case((ChatMessage.cache_hit.is_(True), 1))).label("cache_hit_count"),
        )
        .outerjoin(ChatMessage, ChatMessage.session_id == ChatSession.id)
        .group_by(ChatSession.id)
        .order_by(ChatSession.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    count_stmt = count_stmt.where(ChatSession.user_id == current_user.id)
    data_stmt = data_stmt.where(ChatSession.user_id == current_user.id)
    if connection_id is not None:
        count_stmt = count_stmt.where(ChatSession.connection_id == connection_id)
        data_stmt = data_stmt.where(ChatSession.connection_id == connection_id)
    total = await db.scalar(count_stmt) or 0
    result = await db.execute(data_stmt)
    items = [
        SessionResponse(
            id=row.ChatSession.id,
            connection_id=row.ChatSession.connection_id,
            title=row.ChatSession.title,
            provider=row.ChatSession.provider,
            created_at=row.ChatSession.created_at,
            updated_at=row.ChatSession.updated_at,
            cache_hit_count=row.cache_hit_count,
        )
        for row in result
    ]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SessionResponse:
    stmt = (
        select(
            ChatSession,
            func.count(case((ChatMessage.cache_hit.is_(True), 1))).label("cache_hit_count"),
        )
        .outerjoin(ChatMessage, ChatMessage.session_id == ChatSession.id)
        .where(ChatSession.id == session_id, ChatSession.user_id == current_user.id)
        .group_by(ChatSession.id)
    )
    result = await db.execute(stmt)
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionResponse(
        id=row.ChatSession.id,
        connection_id=row.ChatSession.connection_id,
        title=row.ChatSession.title,
        provider=row.ChatSession.provider,
        created_at=row.ChatSession.created_at,
        updated_at=row.ChatSession.updated_at,
        cache_hit_count=row.cache_hit_count,
    )


@router.get(
    "/sessions/{session_id}/history",
    response_model=PaginatedResponse[MessageResponse],
)
async def get_session_history(
    session_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[MessageResponse]:
    # Fold ownership check into a scalar subquery so the session SELECT is
    # eliminated on the common (non-empty) path.
    owned_session_id = (
        select(ChatSession.id)
        .where(ChatSession.id == session_id, ChatSession.user_id == current_user.id)
        .scalar_subquery()
    )
    total = (
        await db.scalar(
            select(func.count())
            .select_from(ChatMessage)
            .where(ChatMessage.session_id == owned_session_id)
        )
        or 0
    )
    if total == 0:
        # Distinguish "empty session" from "not found / wrong owner"
        exists = await db.scalar(
            select(func.count())
            .select_from(ChatSession)
            .where(ChatSession.id == session_id, ChatSession.user_id == current_user.id)
        )
        if not exists:
            raise HTTPException(status_code=404, detail="Session not found")
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == owned_session_id)
        .order_by(ChatMessage.created_at)
        .limit(limit)
        .offset(offset)
    )
    return PaginatedResponse(
        items=list(result.scalars().all()), total=total, limit=limit, offset=offset
    )


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    stmt = select(ChatSession).where(
        ChatSession.id == session_id, ChatSession.user_id == current_user.id
    )
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    await db.execute(delete(ChatMessage).where(ChatMessage.session_id == session_id))
    await db.delete(session)
    await db.commit()


# ── Cache management ───────────────────────────────────────────────────────────


@router.get("/cache/stats", response_model=CacheStatsResponse)
async def get_cache_stats(
    _user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> CacheStatsResponse:
    """Return query-cache statistics."""
    # miss_count is pulled from a separate table; embed it as a scalar subquery
    # so all three aggregates arrive in one round-trip instead of three.
    miss_subquery = select(func.coalesce(func.sum(QueryCacheStats.miss_count), 0)).scalar_subquery()
    agg_result = await db.execute(
        select(
            func.count(QueryCacheEntry.id).label("total"),
            func.coalesce(func.sum(QueryCacheEntry.hit_count), 0).label("total_hits"),
            miss_subquery.label("total_misses"),
        )
    )
    row = agg_result.one()
    total_entries: int = row.total
    hit_count: int = int(row.total_hits)
    miss_count: int = int(row.total_misses)

    top_result = await db.execute(
        select(QueryCacheEntry).order_by(QueryCacheEntry.hit_count.desc()).limit(5)
    )
    top_entries = top_result.scalars().all()

    total_denominator = hit_count + miss_count
    hit_rate = hit_count / total_denominator if total_denominator > 0 else 0.0
    return CacheStatsResponse(
        total_entries=total_entries,
        hit_count=hit_count,
        miss_count=miss_count,
        hit_rate=hit_rate,
        top_cached_queries=[
            TopCachedQuery(question=e.question_raw, hit_count=e.hit_count) for e in top_entries
        ],
    )


@router.delete("/cache/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def clear_cache(
    connection_id: str,
    _user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Evict all cached queries for a connection."""
    await get_connection_or_404(connection_id, db)
    await _get_shared_cache().invalidate(connection_id, db)


@router.get("/cache/{connection_id}/entries", response_model=PaginatedResponse[CacheEntryResponse])
async def list_cache_entries(
    connection_id: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    search: str | None = Query(None),
    _user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[CacheEntryResponse]:
    """List cached query entries for a connection with optional search and pagination."""
    await get_connection_or_404(connection_id, db)

    base = select(QueryCacheEntry).where(
        QueryCacheEntry.connection_id == connection_id,
    )
    if search:
        base = base.where(QueryCacheEntry.question_raw.ilike(f"%{search}%"))

    total_result = await db.execute(select(func.count()).select_from(base.subquery()))
    total: int = total_result.scalar_one()

    entries_result = await db.execute(
        base.order_by(QueryCacheEntry.hit_count.desc()).limit(limit).offset(offset)
    )
    entries = entries_result.scalars().all()

    return PaginatedResponse(
        items=[CacheEntryResponse.model_validate(e) for e in entries],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.delete("/cache/entries/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cache_entry(
    entry_id: str,
    _user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a single cache entry by ID."""
    result = await db.execute(
        select(QueryCacheEntry).where(
            QueryCacheEntry.id == entry_id,
        )
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cache entry not found")
    # Verify the entry's connection is active — prevents deleting entries for
    # connections the caller cannot reach (raises 404 for inactive/missing connections).
    await get_connection_or_404(entry.connection_id, db)
    await db.delete(entry)
    await db.commit()


# ── Verified examples ──────────────────────────────────────────────────────────


@router.get("/examples/{connection_id}", response_model=PaginatedResponse[ExampleResponse])
async def list_examples(
    connection_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[ExampleResponse]:
    base_where = [VerifiedExample.connection_id == connection_id]
    total = (
        await db.scalar(select(func.count()).select_from(VerifiedExample).where(*base_where)) or 0
    )
    result = await db.execute(
        select(VerifiedExample)
        .where(*base_where)
        .order_by(VerifiedExample.created_at)
        .limit(limit)
        .offset(offset)
    )
    examples = result.scalars().all()
    return PaginatedResponse(
        items=[ExampleResponse.model_validate(e) for e in examples],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/examples/{connection_id}",
    response_model=ExampleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_example(
    connection_id: str,
    body: ExampleCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ExampleResponse:
    """Manually add a verified question -> query pair to the example library."""
    conn = await get_connection_or_404(connection_id, db)
    query_dialect = create_datasource(conn.source_type).query_dialect

    shared_cache = _get_shared_cache()
    embedding = await shared_cache.compute_embedding_async(body.question)

    library = ExampleLibrary()
    entry = await library.add_example(
        connection_id=connection_id,
        question=body.question,
        query=body.query,
        query_dialect=query_dialect,
        embedding=embedding,
        db=db,
    )

    # Evict any cached query for this question so the next request goes to the LLM
    # and picks up the newly verified example as a few-shot.
    await shared_cache.evict_similar(connection_id, body.question, db)

    result = await db.execute(select(VerifiedExample).where(VerifiedExample.id == entry.id))
    row = result.scalar_one()
    return ExampleResponse.model_validate(row)


@router.delete("/examples/{example_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_example(
    example_id: str,
    _current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    stmt = select(VerifiedExample).where(VerifiedExample.id == example_id)
    result = await db.execute(stmt)
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Example not found")
    library = ExampleLibrary()
    await library.remove_example(example_id, db)


@router.put("/examples/{example_id}", response_model=ExampleResponse)
async def update_example(
    example_id: str,
    body: ExampleUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ExampleResponse:
    """Update the question and/or query of an existing verified example."""
    stmt = select(VerifiedExample).where(VerifiedExample.id == example_id)
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Example not found")

    if body.question is not None and body.question != row.question:
        row.question = body.question
        row.question_embedding = await _get_shared_cache().compute_embedding_async(body.question)
    if body.query is not None:
        row.query = body.query

    await db.commit()
    await db.refresh(row)
    return ExampleResponse.model_validate(row)
