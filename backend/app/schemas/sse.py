# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""TypedDict definitions for Server-Sent Events (SSE) chat stream payloads."""

from __future__ import annotations

from typing import Any, Literal

from typing_extensions import TypedDict


class StatusEvent(TypedDict):
    """Pipeline progress update — emitted at the start of each stage."""

    type: Literal["status"]
    message: str


class SqlEvent(TypedDict):
    """Generated SQL block — emitted as soon as the LLM returns it, before execution."""

    type: Literal["sql"]
    query: str
    dialect: str


class ExplanationEvent(TypedDict):
    """LLM explanation of the query — emitted as a single event after generation."""

    type: Literal["explanation"]
    text: str


class RowBatchEvent(TypedDict):
    """Batch of result rows — emitted as the database cursor reads them."""

    type: Literal["row_batch"]
    rows: list[list[Any]]
    columns: list[str]
    column_types: list[str]
    batch_index: int
    truncated: bool


class ErrorEvent(TypedDict):
    """Error details — execution failure, validation failure, or credential missing."""

    type: Literal["error"]
    message: str


class DoneEvent(TypedDict):
    """Terminal event — signals the stream is complete."""

    type: Literal["done"]
    session_id: str
    message_id: str
    execution_time_ms: float | None
    cache_hit: bool
    status: str
    token_count: int | None
    input_tokens: int | None
    output_tokens: int | None
    warning: str | None


SseEvent = StatusEvent | SqlEvent | ExplanationEvent | RowBatchEvent | ErrorEvent | DoneEvent
