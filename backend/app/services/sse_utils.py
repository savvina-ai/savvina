# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""SSE wire-format serialization helpers."""

from __future__ import annotations

import json
from typing import Any


def format_sse_event(payload: dict[str, Any]) -> str:
    """Serialize a dict to SSE wire format: 'data: <json>\\n\\n'.

    Uses default=str so datetimes, decimals, and other non-JSON-native types
    are safely coerced without raising.
    """
    return f"data: {json.dumps(payload, default=str)}\n\n"


SSE_HEARTBEAT = ": keepalive\n\n"
