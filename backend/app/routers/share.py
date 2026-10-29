# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Public (unauthenticated) share endpoints for messages and sessions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from ..auth.limiter import limiter
from ..config import get_settings
from ..database import get_db
from ..models.chat import ChatMessage, ChatSession
from ..schemas.chat import (
    PublicMessageSummary,
    PublicSessionResult,
    PublicShareResult,
    QueryResultsResponse,
)
from ..services.export_service import generate_csv, generate_xlsx
from ..services.stored_results import STALE_ROW_FILTER_TEXT, load_result_masks

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/public", tags=["share"])


# ── Single-message share ─────────────────────────────────────────────────────


async def _shared_message(token: str, db: AsyncSession) -> tuple[dict, str | None]:
    """The shared message's stored results re-masked with the owning connection's current
    privacy settings (``stored_results``) and its served SQL; 404 / 410 as the share
    dictates, and 410 for a result computed under another row-filter map."""
    msg = await db.scalar(select(ChatMessage).where(ChatMessage.share_token == token))
    if not msg or msg.results_json is None:
        raise HTTPException(status_code=404, detail="Shared result not found")
    if msg.share_expires_at and msg.share_expires_at < datetime.now(UTC):
        raise HTTPException(status_code=410, detail="Share link has expired")
    masks = await load_result_masks(db, [msg.session_id])
    [(results_data, query)] = await masks.serve_all(
        [(msg.session_id, msg.results_json, msg.query_generated)]
    )
    if results_data.get("stale_row_filter"):
        # Computed under another row-filter map than the connection has now: gone, as an
        # expired link is. The session share serves the flag per message instead.
        raise HTTPException(status_code=410, detail=STALE_ROW_FILTER_TEXT)
    return results_data, query


@router.get("/share/{token}", response_model=PublicShareResult)
@limiter.limit("60/minute")
async def get_shared_result(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PublicShareResult:
    """Return results for a shared chart link — no authentication required."""
    results_data, query = await _shared_message(token, db)
    row_limit = get_settings().default_row_limit
    if isinstance(results_data.get("rows"), list) and len(results_data["rows"]) > row_limit:
        results_data = {**results_data, "rows": results_data["rows"][:row_limit]}
    results = QueryResultsResponse(**results_data)
    return PublicShareResult(results=results, query_generated=query)


@router.get("/share/{token}/csv")
@limiter.limit("60/minute")
async def get_shared_result_csv(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Download shared message results as CSV — no authentication required."""
    results_data, _query = await _shared_message(token, db)
    csv_content = await asyncio.to_thread(generate_csv, results_data)
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="shared-results.csv"'},
    )


@router.get("/share/{token}/xlsx")
@limiter.limit("60/minute")
async def get_shared_result_xlsx(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Download shared message results as XLSX — no authentication required."""
    results_data, _query = await _shared_message(token, db)
    xlsx_bytes = await asyncio.to_thread(generate_xlsx, results_data, "Shared Results")
    return StreamingResponse(
        iter([xlsx_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="shared-results.xlsx"'},
    )


# ── Session share ────────────────────────────────────────────────────────────


@router.get("/share/session/{token}", response_model=PublicSessionResult)
@limiter.limit("60/minute")
async def get_shared_session(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PublicSessionResult:
    """Return the full conversation thread for a shared session."""
    session = await db.scalar(select(ChatSession).where(ChatSession.share_token == token))
    if not session:
        raise HTTPException(status_code=404, detail="Shared session not found")
    if session.share_expires_at and session.share_expires_at < datetime.now(UTC):
        raise HTTPException(status_code=410, detail="Share link has expired")
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.created_at)
        .limit(200)
    )
    rows = list(result.scalars().all())
    masks = await load_result_masks(db, [session.id] if rows else [])
    previews = []
    for m in rows:
        results_json = m.results_json
        if results_json and isinstance(results_json.get("rows"), list):
            results_json = {**results_json, "rows": results_json["rows"][:10]}
        previews.append((session.id, results_json, m.query_generated))
    served = await masks.serve_all(previews)
    messages = []
    for m, (results_json, query) in zip(rows, served, strict=True):
        messages.append(
            PublicMessageSummary(
                role=m.role,
                content=m.content,
                query_generated=query,
                query_dialect=m.query_dialect,
                results_json=results_json,
                execution_time_ms=m.execution_time_ms,
                status=m.status,
                created_at=m.created_at,
            )
        )
    return PublicSessionResult(title=session.title, messages=messages)
