# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Generic paginated response wrapper for list endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class PaginatedResponse[T](BaseModel):
    """Wraps a page of items with total count and pagination metadata."""

    items: list[T]
    total: int
    limit: int
    offset: int
