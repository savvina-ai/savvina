# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for utils/formatting — format_bytes and format_row_count."""

from __future__ import annotations

from app.utils.formatting import format_bytes, format_row_count

# ── format_bytes ──────────────────────────────────────────────────────────────


def test_format_bytes_none():
    assert format_bytes(None) == ""


def test_format_bytes_zero():
    assert format_bytes(0) == "0.0 B"


def test_format_bytes_bytes():
    assert format_bytes(512) == "512.0 B"


def test_format_bytes_just_under_kb():
    assert format_bytes(1023) == "1023.0 B"


def test_format_bytes_exactly_kb():
    assert format_bytes(1024) == "1.0 KB"


def test_format_bytes_mb_boundary():
    assert format_bytes(1024 * 1024) == "1.0 MB"


def test_format_bytes_gb_boundary():
    assert format_bytes(1024 * 1024 * 1024) == "1.0 GB"


# ── format_row_count ──────────────────────────────────────────────────────────


def test_format_row_count_none():
    assert format_row_count(None) == "unknown"


def test_format_row_count_zero():
    assert format_row_count(0) == "0"


def test_format_row_count_small():
    assert format_row_count(42) == "42"


def test_format_row_count_thousands():
    assert format_row_count(1_000_000) == "1,000,000"
