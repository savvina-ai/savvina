# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Query execution, result masking, SQL injection helpers, and execution correction."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
import logging
import re
from typing import TYPE_CHECKING, Any

from ..datasources.models import DataSourceSchema, PrivacySettings, QueryResult
from ..providers.base import BaseLLMProvider
from ..schemas.chat import QueryResultsResponse
from ..schemas.sse import RowBatchEvent, SqlEvent, SseEvent, StatusEvent
from .correction import (
    _MAX_SELF_CORRECTION_ATTEMPTS,
    _attempt_sql_execution_correction,
    _attempt_zero_result_correction,
)
from .intent_classifier import QueryIntent

if TYPE_CHECKING:
    from ..datasources.base import BaseDataSource

logger = logging.getLogger(__name__)


@dataclass
class _ExecutionResult:
    results_response: QueryResultsResponse | None
    execution_time_ms: float | None
    bytes_scanned: int | None
    status: str
    error: str | None
    generated_query: str | None
    explanation: str
    connected: bool


def _results_to_response(result: QueryResult) -> QueryResultsResponse:
    """Convert a QueryResult dataclass to its Pydantic API schema."""
    return QueryResultsResponse(
        columns=result.columns,
        column_types=result.column_types,
        rows=result.rows,
        row_count=result.row_count,
        truncated=result.truncated,
        execution_time_ms=result.execution_time_ms,
        bytes_scanned=result.bytes_scanned,
    )


def _inject_row_filter(sql: str, row_filter_sql: str, dialect: str) -> str:
    """Enforce a mandatory row-level filter by wrapping the query in a derived table.

    LIMIT is hoisted outside the subquery so the filter applies before row truncation.
    Safe for CTEs: all SQL dialects supported by Savvina allow inline CTEs in subqueries.

    Trust model: ``row_filter_sql`` is sourced exclusively from the connection's
    privacy settings, which are written by an authenticated org-admin through the
    settings UI.  It is never derived from user-supplied chat input, so the
    injection risk is limited to a compromised admin account.
    """
    sql = sql.rstrip(";").rstrip()
    limit_match = re.search(r"(\s+LIMIT\s+\d+)\s*$", sql, re.IGNORECASE)
    limit_clause = ""
    if limit_match:
        limit_clause = limit_match.group(1)
        sql = sql[: limit_match.start()]
    return f"SELECT * FROM ({sql}) AS _sift_q WHERE {row_filter_sql}{limit_clause}"  # noqa: S608


def _mask_sensitive_result_columns(
    response: QueryResultsResponse, privacy: PrivacySettings | None
) -> QueryResultsResponse:
    """Replace values in sensitive or explicitly excluded columns with a redaction marker.

    Applied before results are returned to the client or persisted to results_json,
    so sensitive values never leave the server in plaintext.
    """
    if not privacy:
        return response
    excluded_bare = {c.split(".")[-1].lower() for c in privacy.excluded_columns}
    sensitive_indices = {
        i
        for i, col in enumerate(response.columns)
        if privacy.is_column_sensitive(col) or col.lower() in excluded_bare
    }
    if not sensitive_indices:
        return response
    masked_rows = [
        ["[REDACTED]" if j in sensitive_indices else val for j, val in enumerate(row)]
        for row in response.rows
    ]
    return response.model_copy(update={"rows": masked_rows})


def _inject_order_by(sql: str, column: str, direction: str, dialect: str) -> str:
    """Replace or inject an ORDER BY clause, keeping LIMIT at the end.

    Quoting rules per dialect:
    - MySQL: backtick — embedded backticks doubled (`` ` `` → ` `` `)
    - PostgreSQL/other: double-quote — embedded double-quotes doubled (SQL standard)

    Both the column name and the direction are sanitised before injection:
    - ``column`` has its delimiter character escaped (never unquoted)
    - ``direction`` is uppercased and asserted to be ASC or DESC

    This function is the last line of defence.  Callers **must** also validate
    ``column`` against the result-set column list returned by the database
    (see ``ChatService.sort_and_execute``) before calling here.
    """
    if direction.upper() not in {"ASC", "DESC"}:
        raise ValueError(f"Invalid sort direction: {direction!r}")

    if dialect == "MySQL":
        quote_char = "`"
        escaped_column = column.replace("`", "``")
    else:
        quote_char = '"'
        escaped_column = column.replace('"', '""')

    order_clause = f"ORDER BY {quote_char}{escaped_column}{quote_char} {direction.upper()}"

    sql_work = sql.rstrip(";").rstrip()

    # Strip existing ORDER BY (handles multi-column; stops at LIMIT or end-of-string)
    sql_no_order = re.sub(
        r"\s+ORDER\s+BY\b.+?(?=\s+LIMIT\b|$)",
        "",
        sql_work,
        flags=re.IGNORECASE | re.DOTALL,
    ).rstrip()

    # Inject before LIMIT, or append at end
    limit_match = re.search(r"\bLIMIT\b", sql_no_order, re.IGNORECASE)
    if limit_match:
        pos = limit_match.start()
        return sql_no_order[:pos].rstrip() + f"\n{order_clause}\n" + sql_no_order[pos:]
    return sql_no_order + f"\n{order_clause}"


async def _execute_auto_query(
    generated_query: str,
    explanation: str,
    adapter: BaseDataSource,
    config_dict: dict,
    connected: bool,
    cache_hit: bool,
    provider: BaseLLMProvider | None,
    configured_model: str,
    configured_max_tokens: int,
    system_prompt: str,
    history: list[dict],
    message: str,
    schema: DataSourceSchema,
    options: dict,
    settings: Any,
    privacy: PrivacySettings | None = None,
    intent: QueryIntent = QueryIntent.UNKNOWN,
) -> _ExecutionResult:
    """Step 12 auto_execute: run query, handle errors, attempt execution correction."""
    if not connected:
        await adapter.connect(config_dict)
        connected = True
    max_rows = min(options.get("max_rows", settings.default_row_limit), settings.default_row_limit)
    if privacy and privacy.row_filter_sql:
        generated_query = _inject_row_filter(
            generated_query, privacy.row_filter_sql, adapter.query_dialect
        )
    results_response: QueryResultsResponse | None = None
    execution_time_ms: float | None = None
    bytes_scanned_val: int | None = None
    status = "cached" if cache_hit else "executed"
    error: str | None = None
    exec_error_str: str | None = None
    current_query = generated_query
    max_exec_attempts = _MAX_SELF_CORRECTION_ATTEMPTS + 1  # initial + retries

    for attempt in range(max_exec_attempts):
        try:
            query_result = await adapter.execute_query(
                current_query,
                timeout=settings.default_query_timeout,
                max_rows=max_rows,
            )
            results_response = _mask_sensitive_result_columns(
                _results_to_response(query_result), privacy
            )
            execution_time_ms = query_result.execution_time_ms
            bytes_scanned_val = query_result.bytes_scanned
            exec_error_str = None
            break
        except Exception as e:
            exec_error_str = str(e)

        if attempt < max_exec_attempts - 1 and not cache_hit and provider is not None:
            corrected_query, corrected_explanation = await _attempt_sql_execution_correction(
                original_question=message,
                failed_query=current_query,
                exec_error=exec_error_str,
                system_prompt=system_prompt,
                history=history,
                provider=provider,
                configured_model=configured_model,
                configured_max_tokens=configured_max_tokens,
                schema=schema,
                adapter=adapter,
                attempt_num=attempt + 1,
            )
            if corrected_query:
                current_query = corrected_query
                explanation = corrected_explanation or explanation
                logger.debug(
                    "Execution self-correction attempt %d succeeded; re-executing", attempt + 1
                )
            else:
                if corrected_explanation:
                    exec_error_str = f"{exec_error_str}\n\nNote: {corrected_explanation}"
                break
        else:
            break

    if exec_error_str:
        error = exec_error_str
        status = "error"

    # ── Zero-result detection ──────────────────────────────────────────────────
    # When execution succeeded but returned 0 rows, ask the LLM to diagnose
    # whether a filter value mismatch, wrong join, or faulty assumption is the
    # cause and optionally provide a corrected query.  EXISTENCE queries ("are
    # there any…") intentionally produce 0 rows, so they are excluded.
    if (
        exec_error_str is None
        and results_response is not None
        and results_response.row_count == 0
        and intent != QueryIntent.EXISTENCE
        and not cache_hit
        and provider is not None
    ):
        zero_query, zero_expl = await _attempt_zero_result_correction(
            original_question=message,
            query=current_query,
            system_prompt=system_prompt,
            history=history,
            provider=provider,
            configured_model=configured_model,
            configured_max_tokens=configured_max_tokens,
            schema=schema,
            adapter=adapter,
        )
        if zero_query:
            try:
                query_result = await adapter.execute_query(
                    zero_query,
                    timeout=settings.default_query_timeout,
                    max_rows=max_rows,
                )
                results_response = _mask_sensitive_result_columns(
                    _results_to_response(query_result), privacy
                )
                execution_time_ms = query_result.execution_time_ms
                bytes_scanned_val = query_result.bytes_scanned
                current_query = zero_query
                explanation = zero_expl or explanation
                logger.debug(
                    "Zero-result correction succeeded; re-executed query returned %d rows",
                    query_result.row_count,
                )
            except Exception as e:
                # Keep the original 0-row result; the correction failed at DB level.
                logger.debug("Zero-result corrected query failed at execution: %s", e)
        elif zero_expl:
            explanation = zero_expl

    return _ExecutionResult(
        results_response=results_response,
        execution_time_ms=execution_time_ms,
        bytes_scanned=bytes_scanned_val,
        status=status,
        error=error,
        generated_query=current_query,
        explanation=explanation,
        connected=connected,
    )


# ── Streaming execution ────────────────────────────────────────────────────────

_SSE_BATCH_SIZE = 50


@dataclass
class _BatchAccumulator:
    """Mutable container updated by _stream_cursor_batches on every yielded batch.

    Call reset_rows() before each retry to clear stale row data while keeping
    column metadata from the last successful first batch.
    """

    columns: list[str] = field(default_factory=list)
    column_types: list[str] = field(default_factory=list)
    rows: list = field(default_factory=list)
    truncated: bool = False
    execution_time_ms: float | None = None
    batch_index: int = 0

    def reset_rows(self) -> None:
        """Clear accumulated rows and batch counter before a retry execution."""
        self.rows = []
        self.batch_index = 0


@dataclass
class _StreamResult:
    """Mutable container so _stream_execute_with_correction can return its result."""

    result: _ExecutionResult | None = None


async def _stream_cursor_batches(
    adapter: BaseDataSource,
    query: str,
    *,
    timeout: int,  # noqa: ASYNC109
    max_rows: int,
    acc: _BatchAccumulator,
) -> AsyncGenerator[RowBatchEvent, None]:
    """Execute *query* via the streaming adapter and yield RowBatchEvent objects.

    Side-effects: updates *acc* in-place (columns, column_types, rows, truncated,
    execution_time_ms, batch_index).  Call acc.reset_rows() before each retry to
    clear stale row data without losing column metadata from prior executions.
    """
    async for batch_result in adapter.execute_query_stream(
        query,
        timeout=timeout,
        batch_size=_SSE_BATCH_SIZE,
        max_rows=max_rows,
    ):
        if acc.batch_index == 0:
            acc.columns = batch_result.columns
            acc.column_types = batch_result.column_types
            acc.execution_time_ms = batch_result.execution_time_ms
        acc.rows.extend(batch_result.rows)
        acc.truncated = batch_result.truncated
        yield RowBatchEvent(
            type="row_batch",
            rows=batch_result.rows,
            columns=batch_result.columns,
            column_types=batch_result.column_types,
            batch_index=acc.batch_index,
            truncated=batch_result.truncated,
        )
        acc.batch_index += 1


async def _stream_self_correction_phase(
    *,
    adapter: BaseDataSource,
    settings: Any,
    max_rows: int,
    acc: _BatchAccumulator,
    out: dict,
    provider: BaseLLMProvider,
    configured_model: str,
    configured_max_tokens: int,
    system_prompt: str,
    history: list[dict],
    message: str,
    schema: DataSourceSchema,
) -> AsyncGenerator[SseEvent, None]:
    """Yield SSE events for the self-correction retry loop.

    Preconditions: out["exec_error"] is truthy, provider is not None, and
    cache_hit is False — the caller should skip this generator otherwise.

    Mutates *out* in-place:
    - ``out["current_query"]``  — updated to the corrected query on success
    - ``out["explanation"]``    — updated to the corrected explanation on success
    - ``out["exec_error"]``     — set to None on success; unchanged (or appended)
                                  when all correction attempts are exhausted
    """
    for _attempt in range(1, _MAX_SELF_CORRECTION_ATTEMPTS + 1):
        yield StatusEvent(type="status", message="Correcting query…")
        corrected_query, corrected_explanation = await _attempt_sql_execution_correction(
            original_question=message,
            failed_query=out["current_query"],
            exec_error=out["exec_error"],
            system_prompt=system_prompt,
            history=history,
            provider=provider,
            configured_model=configured_model,
            configured_max_tokens=configured_max_tokens,
            schema=schema,
            adapter=adapter,
            attempt_num=_attempt,
        )
        if not corrected_query:
            if corrected_explanation:
                out["exec_error"] = f"{out['exec_error']}\n\nNote: {corrected_explanation}"
            return
        out["current_query"] = corrected_query
        out["explanation"] = corrected_explanation or out["explanation"]
        yield SqlEvent(type="sql", query=out["current_query"], dialect=adapter.query_dialect)
        yield StatusEvent(type="status", message="Re-executing corrected query…")
        acc.reset_rows()
        out["exec_error"] = None
        try:
            async for event in _stream_cursor_batches(
                adapter,
                out["current_query"],
                timeout=settings.default_query_timeout,
                max_rows=max_rows,
                acc=acc,
            ):
                yield event
        except Exception as exc:
            out["exec_error"] = str(exc)
        if not out["exec_error"]:
            return


async def _stream_zero_result_phase(
    *,
    adapter: BaseDataSource,
    settings: Any,
    max_rows: int,
    acc: _BatchAccumulator,
    out: dict,
    provider: BaseLLMProvider,
    configured_model: str,
    configured_max_tokens: int,
    system_prompt: str,
    history: list[dict],
    message: str,
    schema: DataSourceSchema,
    intent: QueryIntent,
) -> AsyncGenerator[SseEvent, None]:
    """Yield SSE events for the zero-result recovery phase.

    Preconditions: acc.rows is empty, intent is not EXISTENCE, cache_hit is
    False, and provider is not None — the caller should skip this generator
    when those conditions are not all met.

    Mutates *out* in-place:
    - ``out["current_query"]``         — updated to the refined query if one was
                                         produced by the LLM
    - ``out["explanation"]``           — updated to the refined explanation when
                                         available
    - ``out["zero_result_reexecuted"]``— True if re-execution succeeded, False
                                         if it raised an exception
    *acc* is also updated in-place via _stream_cursor_batches when re-execution
    succeeds; callers should rebuild their QueryResultsResponse from *acc* when
    out["zero_result_reexecuted"] is True.
    """
    yield StatusEvent(type="status", message="Query returned no rows — refining…")
    zero_query, zero_expl = await _attempt_zero_result_correction(
        original_question=message,
        query=out["current_query"],
        system_prompt=system_prompt,
        history=history,
        provider=provider,
        configured_model=configured_model,
        configured_max_tokens=configured_max_tokens,
        schema=schema,
        adapter=adapter,
    )
    if zero_query:
        out["current_query"] = zero_query
        out["explanation"] = zero_expl or out["explanation"]
        out["zero_result_reexecuted"] = True
        yield SqlEvent(type="sql", query=out["current_query"], dialect=adapter.query_dialect)
        acc.reset_rows()
        try:
            async for event in _stream_cursor_batches(
                adapter,
                out["current_query"],
                timeout=settings.default_query_timeout,
                max_rows=max_rows,
                acc=acc,
            ):
                yield event
        except Exception as exc:
            logger.debug("Zero-result re-execution failed, keeping original: %s", exc)
            out["zero_result_reexecuted"] = False
    elif zero_expl:
        out["explanation"] = zero_expl


async def _stream_execute_with_correction(
    *,
    generated_query: str,
    explanation: str,
    adapter: BaseDataSource,
    config_dict: dict,
    connected: bool,
    cache_hit: bool,
    provider: BaseLLMProvider | None,
    configured_model: str,
    configured_max_tokens: int,
    system_prompt: str,
    history: list[dict],
    message: str,
    schema: DataSourceSchema,
    options: dict,
    settings: Any,
    result_out: _StreamResult,
    privacy: PrivacySettings | None = None,
    intent: QueryIntent = QueryIntent.UNKNOWN,
) -> AsyncGenerator[SseEvent, None]:
    """Streaming counterpart of _execute_auto_query.

    Yields RowBatchEvent/StatusEvent/SqlEvent objects as execution progresses,
    applying self-correction and zero-result recovery using the same helpers as
    _execute_auto_query.  Stores the final _ExecutionResult in result_out.result.
    """
    if not connected:
        conn_result = await adapter.connect(config_dict)
        if not conn_result.success:
            result_out.result = _ExecutionResult(
                results_response=None,
                execution_time_ms=None,
                bytes_scanned=None,
                status="error",
                error=f"Connection failed: {conn_result.message}",
                generated_query=generated_query,
                explanation=explanation,
                connected=False,
            )
            return
        connected = True

    max_rows = min(options.get("max_rows", settings.default_row_limit), settings.default_row_limit)
    acc = _BatchAccumulator()
    out: dict = {
        "current_query": generated_query,
        "explanation": explanation,
        "exec_error": None,
        "zero_result_reexecuted": False,
    }

    # Initial streaming execution
    try:
        async for event in _stream_cursor_batches(
            adapter,
            out["current_query"],
            timeout=settings.default_query_timeout,
            max_rows=max_rows,
            acc=acc,
        ):
            yield event
    except Exception as exc:
        out["exec_error"] = str(exc)

    # Self-correction retry loop
    if out["exec_error"] and not cache_hit and provider is not None:
        async for event in _stream_self_correction_phase(
            adapter=adapter,
            settings=settings,
            max_rows=max_rows,
            acc=acc,
            out=out,
            provider=provider,
            configured_model=configured_model,
            configured_max_tokens=configured_max_tokens,
            system_prompt=system_prompt,
            history=history,
            message=message,
            schema=schema,
        ):
            yield event

    if out["exec_error"]:
        result_out.result = _ExecutionResult(
            results_response=None,
            execution_time_ms=acc.execution_time_ms,
            bytes_scanned=None,
            status="error",
            error=out["exec_error"],
            generated_query=out["current_query"],
            explanation=out["explanation"],
            connected=connected,
        )
        return

    results_response = QueryResultsResponse(
        columns=acc.columns,
        column_types=acc.column_types,
        rows=acc.rows,
        row_count=len(acc.rows),
        truncated=acc.truncated,
        execution_time_ms=acc.execution_time_ms or 0.0,
    )

    # Zero-result detection — same logic as _execute_auto_query
    if (
        len(acc.rows) == 0
        and intent != QueryIntent.EXISTENCE
        and not cache_hit
        and provider is not None
    ):
        async for event in _stream_zero_result_phase(
            adapter=adapter,
            settings=settings,
            max_rows=max_rows,
            acc=acc,
            out=out,
            provider=provider,
            configured_model=configured_model,
            configured_max_tokens=configured_max_tokens,
            system_prompt=system_prompt,
            history=history,
            message=message,
            schema=schema,
            intent=intent,
        ):
            yield event

        if out["zero_result_reexecuted"]:
            results_response = QueryResultsResponse(
                columns=acc.columns,
                column_types=acc.column_types,
                rows=acc.rows,
                row_count=len(acc.rows),
                truncated=acc.truncated,
                execution_time_ms=acc.execution_time_ms or 0.0,
            )

    result_out.result = _ExecutionResult(
        results_response=results_response,
        execution_time_ms=acc.execution_time_ms,
        bytes_scanned=None,
        status="cached" if cache_hit else "executed",
        error=None,
        generated_query=out["current_query"],
        explanation=out["explanation"],
        connected=connected,
    )
