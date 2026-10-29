# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Query execution, ORDER BY injection, and execution correction.

Result masking lives in ``result_masking``. The streaming path builds one
``_ResultMaskPlan`` per executed statement and applies it to every row batch and to the
final payload; the non-streaming path masks the complete result with
``_mask_sensitive_result_columns``.

The row-filter rewrite lives in ``row_filter``; its names are re-exported here for existing
importers.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
import contextlib
from dataclasses import dataclass, field
import inspect
import logging
import re
from typing import TYPE_CHECKING, Any

from ..config import get_settings
from ..datasources.errors import ErrorCategory, classify_builtin
from ..datasources.models import DataSourceSchema, PrivacySettings, QueryResult
from ..providers.base import BaseLLMProvider
from ..schemas.chat import QueryResultsResponse
from ..schemas.sse import RowBatchEvent, SqlEvent, SseEvent, StatusEvent
from .correction import (
    _MAX_SELF_CORRECTION_ATTEMPTS,
    _attempt_sql_execution_correction,
    _attempt_zero_result_correction,
)
from .error_feedback import redact_error_for_user
from .intent_classifier import QueryIntent
from .result_masking import (
    _double_quote_is_literal,
    _mask_sensitive_result_columns,
    _plan_result_mask,
    _ResultMaskPlan,
)
from .row_filter import (  # noqa: F401 — re-exported for existing importers
    RowFilterRejectedError,
    _executable,
    row_filter_fingerprint,
    unwrap_row_filter,
)

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


def _fingerprint(privacy: PrivacySettings | None) -> str:
    """The fingerprint of the row-filter map a result is computed under."""
    filters = privacy.row_filters if privacy is not None else {}
    return row_filter_fingerprint(filters, get_settings().encryption_key)


def _exec_error_text(exc: BaseException) -> str:
    """User-facing text for a failed execution: never empty, since empty reads as success.

    Shown to the user and stored on the message; never sent to the LLM — the correction
    prompt is composed by ``services/error_feedback.describe_error_for_llm``.
    """
    text = str(exc)
    if text:
        return text
    if isinstance(exc, TimeoutError):
        return "Query timed out"
    return f"Query failed ({type(exc).__name__})"


def _user_facing_error(
    exec_error: str, category: ErrorCategory, note: str | None = None, *, verbatim: bool = False
) -> str:
    """The error shown to the user and stored on the message for a failed execution.

    *exec_error* is the engine's text, redacted by category (``redact_error_for_user``):
    value-quoting categories lose their quoted spans and extra lines. With *verbatim* the
    text is ours (a ``RowFilterRejectedError``) and is shown as it is. This is a guard in
    case the category ever changes: today rejections are classified SYNTAX, which is not
    redacted, but a redacting category would cut the text at the first apostrophe. The raw
    text goes to the server log only. The
    self-correction's note, when it gave one, follows the text.
    """
    logger.debug("Execution failed (%s): %s", category, exec_error)
    shown = exec_error if verbatim else redact_error_for_user(category, exec_error)
    return f"{shown}\n\nNote: {note}" if note else shown


def _user_error_text(adapter: object, exc: BaseException) -> str:
    """``_user_facing_error`` for a single execution with no correction loop (approve,
    edit-and-execute, sort)."""
    return _user_facing_error(
        _exec_error_text(exc), _classify(adapter, exc), verbatim=_is_rejection(exc)
    )


def _is_rejection(exc: BaseException) -> bool:
    return isinstance(exc, RowFilterRejectedError)


def _llm_hint(exc: BaseException) -> str | None:
    return exc.llm_hint if isinstance(exc, RowFilterRejectedError) else None


def _classify(adapter: object, exc: BaseException) -> ErrorCategory:
    """``adapter.classify_error`` with a never-raise guard, then the builtin timeout fallback.

    A mock or wrapper without the hook (or returning something that is not an
    ``ErrorCategory``) reads OTHER rather than failing the turn.
    """
    if isinstance(exc, RowFilterRejectedError):
        return ErrorCategory.SYNTAX
    try:
        category = adapter.classify_error(exc)  # type: ignore[attr-defined]
    except Exception:
        category = ErrorCategory.OTHER
    if inspect.isawaitable(category):
        # An AsyncMock adapter hands back a coroutine: close it so it is not reported as
        # "never awaited", then read it as OTHER.
        close = getattr(category, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()
        category = ErrorCategory.OTHER
    if not isinstance(category, ErrorCategory):
        category = ErrorCategory.OTHER
    return classify_builtin(exc) if category is ErrorCategory.OTHER else category


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
    """Step 12 auto_execute: run query, handle errors, attempt execution correction.

    Every executed statement is rewritten by the row filter (``_executable``), including each
    corrected and zero-result-refined query; the returned ``generated_query`` (stored on the
    message and in the cache) and every correction prompt carry the clean SQL. Each result is
    masked by the plan of the statement that produced it.
    """
    if not connected:
        await adapter.connect(config_dict)
        connected = True
    max_rows = min(options.get("max_rows", settings.default_row_limit), settings.default_row_limit)
    dialect = adapter.query_dialect
    dq_literal = _double_quote_is_literal(adapter)
    results_response: QueryResultsResponse | None = None
    execution_time_ms: float | None = None
    bytes_scanned_val: int | None = None
    status = "cached" if cache_hit else "executed"
    error: str | None = None
    # The engine's text: read by the correction-prompt builder (which never copies it into
    # the prompt) and, through _user_facing_error, the basis of what the user is shown.
    exec_error: str | None = None
    error_note: str | None = None
    exec_category = ErrorCategory.OTHER
    exec_verbatim = False
    exec_hint: str | None = None
    clean_query = generated_query  # the model's SQL: prompts, storage and the cache use this
    max_exec_attempts = _MAX_SELF_CORRECTION_ATTEMPTS + 1  # initial + retries

    async def _run(executed: str) -> QueryResult:
        return await adapter.execute_query(
            executed,
            timeout=settings.default_query_timeout,
            max_rows=max_rows,
        )

    def _masked(query_result: QueryResult, executed: str, clean: str) -> QueryResultsResponse:
        masked = _mask_sensitive_result_columns(
            _results_to_response(query_result),
            privacy,
            sql=executed,
            original_sql=clean,
            double_quote_is_string_literal=dq_literal,
        )
        return masked.model_copy(update={"row_filter_fingerprint": _fingerprint(privacy)})

    for attempt in range(max_exec_attempts):
        try:
            executed = _executable(clean_query, privacy, dialect)
            query_result = await _run(executed)
            results_response = _masked(query_result, executed, clean_query)
            execution_time_ms = query_result.execution_time_ms
            bytes_scanned_val = query_result.bytes_scanned
            exec_error = None
            break
        except Exception as e:
            exec_error = _exec_error_text(e)
            exec_category = _classify(adapter, e)
            exec_verbatim = _is_rejection(e)
            exec_hint = _llm_hint(e)

        if attempt < max_exec_attempts - 1 and not cache_hit and provider is not None:
            corrected_query, corrected_explanation = await _attempt_sql_execution_correction(
                original_question=message,
                failed_query=clean_query,
                exec_error=exec_error,
                system_prompt=system_prompt,
                history=history,
                provider=provider,
                configured_model=configured_model,
                configured_max_tokens=configured_max_tokens,
                schema=schema,
                adapter=adapter,
                attempt_num=attempt + 1,
                error_category=exec_category,
                llm_hint=exec_hint,
            )
            if corrected_query:
                clean_query = corrected_query
                explanation = corrected_explanation or explanation
                logger.debug(
                    "Execution self-correction attempt %d succeeded; re-executing", attempt + 1
                )
            else:
                error_note = corrected_explanation or None
                break
        else:
            break

    if exec_error:
        error = _user_facing_error(exec_error, exec_category, error_note, verbatim=exec_verbatim)
        status = "error"

    # ── Zero-result detection ──────────────────────────────────────────────────
    # When execution succeeded but returned 0 rows, ask the LLM to diagnose
    # whether a filter value mismatch, wrong join, or faulty assumption is the
    # cause and optionally provide a corrected query.  EXISTENCE queries ("are
    # there any…") intentionally produce 0 rows, so they are excluded.
    if (
        exec_error is None
        and results_response is not None
        and results_response.row_count == 0
        and intent != QueryIntent.EXISTENCE
        and not cache_hit
        and provider is not None
    ):
        zero_query, zero_expl = await _attempt_zero_result_correction(
            original_question=message,
            query=clean_query,
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
                zero_executed = _executable(zero_query, privacy, dialect)
                query_result = await _run(zero_executed)
                results_response = _masked(query_result, zero_executed, zero_query)
                execution_time_ms = query_result.execution_time_ms
                bytes_scanned_val = query_result.bytes_scanned
                clean_query = zero_query
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
        generated_query=clean_query,
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
    # The mask of the statement whose rows these are; set by _stream_statement.
    mask_plan: _ResultMaskPlan | None = None
    # The fingerprint of the row-filter map the statement ran under; set by _stream_statement.
    row_filter_fingerprint: str = ""

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


async def _stream_statement(
    adapter: BaseDataSource,
    clean_query: str,
    *,
    privacy: PrivacySettings | None,
    dq_literal: bool,
    timeout: int,  # noqa: ASYNC109
    max_rows: int,
    acc: _BatchAccumulator,
) -> AsyncGenerator[RowBatchEvent, None]:
    """Execute one statement on the streaming path and yield its masked ``row_batch`` events.

    The database receives *clean_query* rewritten by the row filter; the result mask is
    planned once for this statement (from the clean SQL) and stored on *acc*, so every
    batch here and the final payload built from *acc* are redacted by the same plan.
    """
    executed = _executable(clean_query, privacy, adapter.query_dialect)
    plan = _plan_result_mask(
        privacy, executed, clean_query, double_quote_is_string_literal=dq_literal
    )
    acc.mask_plan = plan
    acc.row_filter_fingerprint = _fingerprint(privacy)
    async for event in _stream_cursor_batches(
        adapter, executed, timeout=timeout, max_rows=max_rows, acc=acc
    ):
        if plan is not None:
            rows = plan.redact(event["rows"], event["columns"])
            if rows is not event["rows"]:
                event = {**event, "rows": rows}
        yield event


def _stream_payload(acc: _BatchAccumulator) -> QueryResultsResponse:
    """The final result from *acc*, masked by the plan of the statement that produced it."""
    rows = acc.rows if acc.mask_plan is None else acc.mask_plan.redact(acc.rows, acc.columns)
    return QueryResultsResponse(
        columns=acc.columns,
        column_types=acc.column_types,
        rows=rows,
        row_count=len(acc.rows),
        truncated=acc.truncated,
        execution_time_ms=acc.execution_time_ms or 0.0,
        row_filter_fingerprint=acc.row_filter_fingerprint,
    )


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
    privacy: PrivacySettings | None = None,
    dq_literal: bool = False,
) -> AsyncGenerator[SseEvent, None]:
    """Yield SSE events for the self-correction retry loop.

    Preconditions: out["exec_error"] is truthy, provider is not None, and
    cache_hit is False — the caller should skip this generator otherwise.

    Each corrected query is executed through _stream_statement: rewritten by the row filter,
    its batches masked by a plan built for it. The prompt and the ``sql`` event carry the
    clean query.

    Mutates *out* in-place:
    - ``out["clean_query"]``    — the corrected query as the model wrote it, on success
    - ``out["explanation"]``    — updated to the corrected explanation on success
    - ``out["exec_error"]``     — set to None on success; the latest failure's text when
                                  all correction attempts are exhausted
    - ``out["exec_category"]``  — the category of the latest failure
    - ``out["exec_hint"]``      — the latest failure's ``RowFilterRejectedError.llm_hint``,
                                  else None
    - ``out["exec_verbatim"]``  — True when the latest failure was a ``RowFilterRejectedError``
    - ``out["error_note"]``     — the correction's note when it gave up with one
    """
    for _attempt in range(1, _MAX_SELF_CORRECTION_ATTEMPTS + 1):
        yield StatusEvent(type="status", message="Correcting query…")
        corrected_query, corrected_explanation = await _attempt_sql_execution_correction(
            original_question=message,
            failed_query=out["clean_query"],
            exec_error=out["exec_error"],
            system_prompt=system_prompt,
            history=history,
            provider=provider,
            configured_model=configured_model,
            configured_max_tokens=configured_max_tokens,
            schema=schema,
            adapter=adapter,
            attempt_num=_attempt,
            error_category=out["exec_category"],
            llm_hint=out["exec_hint"],
        )
        if not corrected_query:
            out["error_note"] = corrected_explanation or None
            return
        out["clean_query"] = corrected_query
        out["explanation"] = corrected_explanation or out["explanation"]
        yield SqlEvent(type="sql", query=out["clean_query"], dialect=adapter.query_dialect)
        yield StatusEvent(type="status", message="Re-executing corrected query…")
        acc.reset_rows()
        out["exec_error"] = None
        out["exec_category"] = ErrorCategory.OTHER
        out["exec_hint"] = None
        out["exec_verbatim"] = False
        try:
            async for event in _stream_statement(
                adapter,
                out["clean_query"],
                privacy=privacy,
                dq_literal=dq_literal,
                timeout=settings.default_query_timeout,
                max_rows=max_rows,
                acc=acc,
            ):
                yield event
        except Exception as exc:
            out["exec_error"] = _exec_error_text(exc)
            out["exec_category"] = _classify(adapter, exc)
            out["exec_hint"] = _llm_hint(exc)
            out["exec_verbatim"] = _is_rejection(exc)
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
    privacy: PrivacySettings | None = None,
    dq_literal: bool = False,
) -> AsyncGenerator[SseEvent, None]:
    """Yield SSE events for the zero-result recovery phase.

    Preconditions: acc.rows is empty, intent is not EXISTENCE, cache_hit is
    False, and provider is not None — the caller should skip this generator
    when those conditions are not all met.

    The refined query is executed through _stream_statement (row filter, its own mask
    plan); the prompt and the ``sql`` event carry the clean query.

    Mutates *out* in-place:
    - ``out["clean_query"]``           — the refined query, only once it has run
    - ``out["explanation"]``           — the refined explanation, only once the refined
                                         query has run (or when no query was refined)
    - ``out["zero_result_reexecuted"]``— True if re-execution succeeded, False
                                         if it raised an exception
    *acc* is also updated in-place when re-execution succeeds; callers should rebuild
    their payload from *acc* (``_stream_payload``) when out["zero_result_reexecuted"]
    is True.
    """
    yield StatusEvent(type="status", message="Query returned no rows — refining…")
    zero_query, zero_expl = await _attempt_zero_result_correction(
        original_question=message,
        query=out["clean_query"],
        system_prompt=system_prompt,
        history=history,
        provider=provider,
        configured_model=configured_model,
        configured_max_tokens=configured_max_tokens,
        schema=schema,
        adapter=adapter,
    )
    if zero_query:
        yield SqlEvent(type="sql", query=zero_query, dialect=adapter.query_dialect)
        acc.reset_rows()
        try:
            async for event in _stream_statement(
                adapter,
                zero_query,
                privacy=privacy,
                dq_literal=dq_literal,
                timeout=settings.default_query_timeout,
                max_rows=max_rows,
                acc=acc,
            ):
                yield event
        except Exception as exc:
            # Keep the original 0-row result, query and explanation, as _execute_auto_query
            # does: the refined query is adopted only once it has run.
            logger.debug("Zero-result re-execution failed, keeping original: %s", exc)
            out["zero_result_reexecuted"] = False
        else:
            out["clean_query"] = zero_query
            out["explanation"] = zero_expl or out["explanation"]
            out["zero_result_reexecuted"] = True
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

    Yields RowBatchEvent/StatusEvent/SqlEvent objects as execution progresses, applying
    self-correction and zero-result recovery using the same helpers as _execute_auto_query.
    Every executed statement is rewritten by the row filter; every ``row_batch`` event and
    the final payload are masked by the plan of the statement that produced them, so a
    client watching the stream and one reading the stored message see the same values.
    Prompts, ``sql`` events and the stored result carry the clean SQL. Stores the final
    _ExecutionResult in result_out.result.
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
    dq_literal = _double_quote_is_literal(adapter)
    acc = _BatchAccumulator()
    out: dict = {
        "clean_query": generated_query,  # the model's SQL: prompts, events, storage
        "explanation": explanation,
        "exec_error": None,  # the engine's text; see _user_facing_error for what is shown
        "error_note": None,
        "exec_category": ErrorCategory.OTHER,
        "exec_hint": None,
        "exec_verbatim": False,
        "zero_result_reexecuted": False,
    }

    # Initial streaming execution
    try:
        async for event in _stream_statement(
            adapter,
            out["clean_query"],
            privacy=privacy,
            dq_literal=dq_literal,
            timeout=settings.default_query_timeout,
            max_rows=max_rows,
            acc=acc,
        ):
            yield event
    except Exception as exc:
        out["exec_error"] = _exec_error_text(exc)
        out["exec_category"] = _classify(adapter, exc)
        out["exec_hint"] = _llm_hint(exc)
        out["exec_verbatim"] = _is_rejection(exc)

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
            privacy=privacy,
            dq_literal=dq_literal,
        ):
            yield event

    if out["exec_error"]:
        result_out.result = _ExecutionResult(
            results_response=None,
            execution_time_ms=acc.execution_time_ms,
            bytes_scanned=None,
            status="error",
            error=_user_facing_error(
                out["exec_error"],
                out["exec_category"],
                out["error_note"],
                verbatim=out["exec_verbatim"],
            ),
            generated_query=out["clean_query"],
            explanation=out["explanation"],
            connected=connected,
        )
        return

    results_response = _stream_payload(acc)

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
            privacy=privacy,
            dq_literal=dq_literal,
        ):
            yield event

        if out["zero_result_reexecuted"]:
            results_response = _stream_payload(acc)

    result_out.result = _ExecutionResult(
        results_response=results_response,
        execution_time_ms=acc.execution_time_ms,
        bytes_scanned=None,
        status="cached" if cache_hit else "executed",
        error=None,
        generated_query=out["clean_query"],
        explanation=out["explanation"],
        connected=connected,
    )
