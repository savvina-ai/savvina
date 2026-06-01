# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Export router — CSV, XLSX, and PDF report downloads for query results."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from ..auth.dependencies import get_current_active_user
from ..auth.limiter import limiter
from ..database import get_db
from ..models.chat import ChatMessage, ChatSession
from ..models.user import User
from ..schemas.export import ReportRequest
from ..services.export_service import generate_csv, generate_xlsx
from ..services.report_service import generate_report

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/export", tags=["export"])


async def _load_message_with_results(
    message_id: str,
    current_user: User,
    db: AsyncSession,
) -> ChatMessage:
    """Load a ChatMessage that belongs to the user and has results."""
    stmt = (
        select(ChatMessage)
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .where(
            ChatMessage.id == message_id,
            ChatSession.user_id == current_user.id,
        )
    )
    msg = await db.scalar(stmt)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if not msg.results_json:
        raise HTTPException(status_code=400, detail="Message has no query results")
    return msg


@router.get("/messages/{message_id}/csv")
@limiter.limit("30/minute")
async def export_message_csv(
    request: Request,
    message_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Download query results as CSV."""
    msg = await _load_message_with_results(message_id, current_user, db)
    csv_content = await asyncio.to_thread(generate_csv, msg.results_json)
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="query-{message_id[:8]}.csv"'},
    )


@router.get("/messages/{message_id}/xlsx")
@limiter.limit("30/minute")
async def export_message_xlsx(
    request: Request,
    message_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Download query results as formatted XLSX."""
    msg = await _load_message_with_results(message_id, current_user, db)
    xlsx_bytes = await asyncio.to_thread(generate_xlsx, msg.results_json, "Query Results")
    return StreamingResponse(
        iter([xlsx_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="query-{message_id[:8]}.xlsx"'},
    )


# ── PDF report ────────────────────────────────────────────────────────────────


@router.post("/report")
@limiter.limit("10/minute")
async def export_report(
    request: Request,
    body: ReportRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Generate a PDF report combining multiple query results."""
    ids = [sec.message_id for sec in body.sections]
    batch_result = await db.execute(
        select(ChatMessage)
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .where(ChatMessage.id.in_(ids), ChatSession.user_id == current_user.id)
    )
    messages_by_id = {m.id: m for m in batch_result.scalars().all()}

    sections: list[dict] = []
    for sec in body.sections:
        msg = messages_by_id.get(sec.message_id)
        if not msg:
            raise HTTPException(
                status_code=404,
                detail=f"Message {sec.message_id!r} not found",
            )
        sections.append(
            {
                "heading": sec.heading or msg.content[:80],
                "sql": msg.query_generated,
                "results_json": msg.results_json,
                "chart_image": sec.chart_image,
                "chart_title": sec.chart_title,
            }
        )

    pdf_bytes = await asyncio.to_thread(generate_report, body.title, sections)
    safe_title = "".join(c for c in body.title if c.isalnum() or c in " -_")[:60]
    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_title}.pdf"',
        },
    )
