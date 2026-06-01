# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for services/sse_utils — SSE wire-format serialization."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.services.sse_utils import SSE_HEARTBEAT, format_sse_event


def test_format_sse_event_plain_dict():
    result = format_sse_event({"type": "status", "message": "hello"})
    assert result.startswith("data: ")
    assert result.endswith("\n\n")
    assert '"type": "status"' in result
    assert '"message": "hello"' in result


def test_format_sse_event_datetime_coerced():
    dt = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
    result = format_sse_event({"ts": dt})
    # default=str should coerce datetime to its string representation
    assert "2025-01-15" in result
    assert result.startswith("data: ")
    assert result.endswith("\n\n")


def test_format_sse_event_decimal_coerced():
    result = format_sse_event({"value": Decimal("123.456")})
    assert "123.456" in result
    assert result.startswith("data: ")
    assert result.endswith("\n\n")


def test_sse_heartbeat_format():
    assert SSE_HEARTBEAT == ": keepalive\n\n"
