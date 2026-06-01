# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.


def format_bytes(num_bytes: int | None) -> str:
    """Format a byte count as a human-readable string."""
    if num_bytes is None:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


def format_row_count(count: int | None) -> str:
    """Format a row count with thousands separator."""
    if count is None:
        return "unknown"
    return f"{count:,}"
