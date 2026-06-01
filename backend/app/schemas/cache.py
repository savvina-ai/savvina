# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Pydantic schemas for cache statistics and the example library."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

# ── Cache stats ────────────────────────────────────────────────────────────────


class TopCachedQuery(BaseModel):
    question: str
    hit_count: int


class CacheStatsResponse(BaseModel):
    total_entries: int
    hit_count: int
    miss_count: int
    hit_rate: float = Field(ge=0.0, le=1.0)
    top_cached_queries: list[TopCachedQuery] = []


class CacheEntryResponse(BaseModel):
    id: str
    question_raw: str
    query_dialect: str
    hit_count: int
    created_at: datetime
    last_hit_at: datetime | None

    model_config = {"from_attributes": True}


# ── Example library ────────────────────────────────────────────────────────────


class ExampleResponse(BaseModel):
    """One entry in the verified-examples list."""

    id: str
    question: str
    query: str
    query_dialect: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ExampleListResponse(BaseModel):
    examples: list[ExampleResponse]
    total: int


class ExampleCreate(BaseModel):
    """Manually add a verified question → query pair."""

    question: str = Field(..., min_length=1)
    query: str = Field(..., min_length=1)


class ExampleUpdate(BaseModel):
    """Partially update a verified example."""

    question: str | None = Field(None, min_length=1)
    query: str | None = Field(None, min_length=1)
