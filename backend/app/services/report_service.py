# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""PDF report generation using ReportLab Platypus."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import io
import logging
import struct
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus import (
    Image as RLImage,
)

logger = logging.getLogger(__name__)

_MAX_TABLE_ROWS = 200
_PAGE_WIDTH, _PAGE_HEIGHT = letter
_MARGIN_LR = 0.6 * inch
_AVAILABLE_WIDTH = _PAGE_WIDTH - 2 * _MARGIN_LR


def _escape(text: str) -> str:
    """Escape XML entities for ReportLab Paragraph markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _truncate(value: Any, max_len: int = 50) -> str:
    if value is None:
        return ""
    s = str(value)
    return s[:max_len] + "..." if len(s) > max_len else s


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Extract width and height from a PNG file header without Pillow."""
    # PNG signature (8 bytes) + IHDR chunk: 4 (len) + 4 (type) + 4 (w) + 4 (h)
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return 0, 0
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def _compute_col_widths(
    columns: list[str],
    rows: list[list[Any]],
) -> list[float]:
    """Compute proportional column widths based on content length."""
    sample = rows[:50]
    max_lens = []
    for col_idx, col_name in enumerate(columns):
        best = len(col_name)
        for row in sample:
            if col_idx < len(row):
                best = max(best, len(_truncate(row[col_idx])))
        max_lens.append(max(best, 4))

    total_chars = sum(max_lens) or 1
    widths = []
    for ml in max_lens:
        w = (ml / total_chars) * _AVAILABLE_WIDTH
        w = max(w, 0.5 * inch)
        widths.append(w)

    scale = _AVAILABLE_WIDTH / sum(widths)
    return [w * scale for w in widths]


def generate_report(
    title: str,
    sections: list[dict[str, Any]],
) -> bytes:
    """Build a PDF report from a list of sections.

    Each section dict must contain:
        heading: str
        sql: str | None
        results_json: dict  (columns, rows, row_count, truncated)
        chart_image: str | None  (base64 data-URL PNG)
        chart_title: str | None
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=_MARGIN_LR,
        rightMargin=_MARGIN_LR,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        fontSize=18,
        spaceAfter=6,
    )
    subtitle_style = ParagraphStyle(
        "ReportSubtitle",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.grey,
        spaceAfter=20,
    )
    heading_style = ParagraphStyle(
        "SectionHeading",
        parent=styles["Heading2"],
        fontSize=11,
        spaceBefore=16,
        spaceAfter=6,
        wordWrap="CJK",
    )
    chart_title_style = ParagraphStyle(
        "ChartTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        alignment=TA_CENTER,
        textColor=colors.Color(0.15, 0.15, 0.15),
        spaceBefore=6,
        spaceAfter=4,
    )
    sql_style = ParagraphStyle(
        "SqlBlock",
        fontName="Courier",
        fontSize=7,
        leading=9,
        backColor=colors.Color(0.96, 0.96, 0.96),
        borderPadding=6,
        spaceAfter=10,
        wordWrap="CJK",
    )
    cell_style = ParagraphStyle(
        "TableCell",
        fontName="Helvetica",
        fontSize=7,
        leading=9,
        alignment=TA_LEFT,
        wordWrap="CJK",
    )
    header_cell_style = ParagraphStyle(
        "TableHeaderCell",
        fontName="Helvetica-Bold",
        fontSize=7,
        leading=9,
        alignment=TA_LEFT,
        textColor=colors.whitesmoke,
        wordWrap="CJK",
    )
    footer_style = ParagraphStyle(
        "TableFooter",
        parent=styles["Normal"],
        fontSize=7,
        textColor=colors.grey,
        spaceBefore=2,
        spaceAfter=14,
    )

    elements: list = []

    elements.append(Paragraph(_escape(title), title_style))
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    elements.append(Paragraph(f"Generated {ts}", subtitle_style))

    for idx, section in enumerate(sections):
        if idx > 0:
            elements.append(
                HRFlowable(
                    width="100%",
                    thickness=1,
                    color=colors.Color(0.80, 0.80, 0.80),
                    spaceBefore=18,
                    spaceAfter=4,
                )
            )

        heading = section.get("heading") or "Query Result"
        elements.append(Paragraph(_escape(heading), heading_style))

        chart_b64 = section.get("chart_image")
        if chart_b64:
            try:
                raw = base64.b64decode(chart_b64.split(",", 1)[-1])

                # Preserve the PNG's actual aspect ratio
                w_px, h_px = _png_dimensions(raw)
                aspect = h_px / w_px if w_px > 0 and h_px > 0 else 0.45
                img_h = _AVAILABLE_WIDTH * aspect

                chart_img = RLImage(io.BytesIO(raw), width=_AVAILABLE_WIDTH, height=img_h)
                chart_img.hAlign = "LEFT"

                # Keep title + image together — prevents page-break between them
                chart_title = section.get("chart_title") or ""
                chart_block: list = []
                if chart_title:
                    chart_block.append(Paragraph(_escape(chart_title), chart_title_style))
                chart_block.append(chart_img)
                chart_block.append(Spacer(1, 10))
                elements.append(KeepTogether(chart_block))
            except Exception:
                logger.warning("Failed to embed chart image in section %d", idx)

        sql = section.get("sql")
        if sql:
            elements.append(Preformatted(sql, sql_style))

        rj = section.get("results_json")
        if not rj:
            elements.append(Paragraph("No results available.", styles["Normal"]))
            elements.append(Spacer(1, 12))
            continue

        columns: list[str] = rj.get("columns", [])
        rows: list[list[Any]] = rj.get("rows", [])[:_MAX_TABLE_ROWS]
        row_count: int = rj.get("row_count", len(rows))
        truncated: bool = rj.get("truncated", False)

        if not columns:
            elements.append(Paragraph("No columns in result.", styles["Normal"]))
            elements.append(Spacer(1, 12))
            continue

        header_row = [Paragraph(_escape(c), header_cell_style) for c in columns]
        table_data = [header_row]
        for row in rows:
            table_data.append([Paragraph(_escape(_truncate(v)), cell_style) for v in row])

        col_widths = _compute_col_widths(columns, rows)

        t = Table(table_data, colWidths=col_widths, repeatRows=1)
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.2, 0.2, 0.25)),
                    ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.Color(0.85, 0.85, 0.85)),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.Color(0.97, 0.97, 0.97)],
                    ),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        elements.append(t)

        footer_parts = [f"{row_count} row{'s' if row_count != 1 else ''}"]
        if truncated or len(rows) < row_count:
            footer_parts.append("(truncated)")
        elements.append(Paragraph(" - ".join(footer_parts), footer_style))

    doc.build(elements)
    return buf.getvalue()
