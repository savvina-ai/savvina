# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Pydantic schemas for PDF report generation."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ReportSection(BaseModel):
    """One section in a report — corresponds to a single chat message with results."""

    message_id: str
    heading: str | None = None
    chart_image: str | None = None  # base64 data-URL PNG captured from the frontend chart
    chart_title: str | None = None  # rendered as PDF text above the chart image


class ReportRequest(BaseModel):
    """Request body for generating a PDF report from multiple query results."""

    title: str = Field(..., min_length=1, max_length=255)
    sections: list[ReportSection] = Field(..., min_length=1, max_length=50)
