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

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/public", tags=["share"])


# ── Single-message share ─────────────────────────────────────────────────────


@router.get("/share/{token}", response_model=PublicShareResult)
@limiter.limit("60/minute")
async def get_shared_result(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PublicShareResult:
    """Return results for a shared chart link — no authentication required."""
    msg = await db.scalar(select(ChatMessage).where(ChatMessage.share_token == token))
    if not msg or msg.results_json is None:
        raise HTTPException(status_code=404, detail="Shared result not found")
    if msg.share_expires_at and msg.share_expires_at < datetime.now(UTC):
        raise HTTPException(status_code=410, detail="Share link has expired")
    results_data = msg.results_json
    row_limit = get_settings().default_row_limit
    if isinstance(results_data.get("rows"), list) and len(results_data["rows"]) > row_limit:
        results_data = {**results_data, "rows": results_data["rows"][:row_limit]}
    results = QueryResultsResponse(**results_data)
    return PublicShareResult(results=results, query_generated=msg.query_generated)


@router.get("/share/{token}/csv")
@limiter.limit("60/minute")
async def get_shared_result_csv(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Download shared message results as CSV — no authentication required."""
    msg = await db.scalar(select(ChatMessage).where(ChatMessage.share_token == token))
    if not msg or msg.results_json is None:
        raise HTTPException(status_code=404, detail="Shared result not found")
    if msg.share_expires_at and msg.share_expires_at < datetime.now(UTC):
        raise HTTPException(status_code=410, detail="Share link has expired")
    csv_content = await asyncio.to_thread(generate_csv, msg.results_json)
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
    msg = await db.scalar(select(ChatMessage).where(ChatMessage.share_token == token))
    if not msg or msg.results_json is None:
        raise HTTPException(status_code=404, detail="Shared result not found")
    if msg.share_expires_at and msg.share_expires_at < datetime.now(UTC):
        raise HTTPException(status_code=410, detail="Share link has expired")
    xlsx_bytes = await asyncio.to_thread(generate_xlsx, msg.results_json, "Shared Results")
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
    messages = []
    for m in result.scalars().all():
        results_json = m.results_json
        if results_json and isinstance(results_json.get("rows"), list):
            results_json = {**results_json, "rows": results_json["rows"][:10]}
        messages.append(
            PublicMessageSummary(
                role=m.role,
                content=m.content,
                query_generated=m.query_generated,
                query_dialect=m.query_dialect,
                results_json=results_json,
                execution_time_ms=m.execution_time_ms,
                status=m.status,
                created_at=m.created_at,
            )
        )
    return PublicSessionResult(title=session.title, messages=messages)
