# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Query execution, ORDER BY injection, and execution correction.

Result masking lives in ``result_masking``. The streaming path builds one
``_ResultMaskPlan`` per executed statement and applies it to every row batch and to the
final payload; the non-streaming path masks the complete result with
``_mask_sensitive_result_columns``.

The row-filter rewrite lives in ``row_filter``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
import contextlib
from dataclasses import dataclass, field, fields, replace
import inspect
import logging
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import ErrorLevel, ParseError, TokenError
from sqlglot.tokens import TokenType

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
    _backslash_is_escape,
    _double_quote_is_literal,
    _mask_sensitive_result_columns,
    _plan_result_mask,
    _ResultMaskPlan,
)
from .row_filter import _executable, row_filter_fingerprint

# The base is imported from its defining module: the row_filter facade re-exports only the
# row-filter names.
from .row_filter_errors import QueryRejectedError
from .row_filter_rewrite import sqlglot_dialect

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
    # True when the error came from running the statement and a different query could fix
    # it (``_is_query_fault``); a cached query that fails this way is evicted and regenerated
    # (``ChatService._regenerate_after_cached_failure``). False for a success and for any
    # other failure: a connection failure, a timeout, a permission error, a hint-less
    # rejection.
    query_fault: bool = False


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

    Shown to the user and stored on the message; never sent to the LLM, except for a
    ``QueryRejectedError``, whose own text the correction prompt carries
    (``ErrorCategory.REJECTED``, ``services/error_feedback.describe_error_for_llm``). Every
    other correction prompt is composed by ``describe_error_for_llm`` from known parts.
    """
    text = str(exc)
    if text:
        return text
    if isinstance(exc, TimeoutError):
        return "Query timed out"
    return f"Query failed ({type(exc).__name__})"


# Shown for ``ErrorCategory.CONNECTION`` in place of the driver's text, which names the
# driver's own state ("Cancelled during execution", "(0, 'Not connected')").
_CONNECTION_LOST = "The connection to the database was lost while the query ran. Try again."


def _user_facing_error(
    exec_error: str, category: ErrorCategory, note: str | None = None, *, verbatim: bool = False
) -> str:
    """The error shown to the user and stored on the message for a failed execution.

    *exec_error* is the engine's text, redacted by category (``redact_error_for_user``):
    value-quoting categories lose their quoted spans and extra lines. With *verbatim* the
    text is ours (a ``QueryRejectedError``) and is shown as it is. Rejections are classified
    ``REJECTED``, a name-echoing category that is not redacted; *verbatim* is the guard
    should that ever change, since a redacting category would cut the text at the first
    apostrophe. A lost connection (``CONNECTION``) is shown as our own sentence. The raw
    text is not logged: the log holds what the user sees. The self-correction's note, when
    it gave one, follows the text.
    """
    if verbatim:
        shown = exec_error
    elif category is ErrorCategory.CONNECTION:
        shown = _CONNECTION_LOST
    else:
        shown = redact_error_for_user(category, exec_error)
    logger.debug("Execution failed (%s): %s", category, shown)
    return f"{shown}\n\nNote: {note}" if note else shown


def _user_error_text(adapter: object, exc: BaseException) -> str:
    """``_user_facing_error`` for a single execution with no correction loop (approve,
    edit-and-execute, sort)."""
    return _user_facing_error(
        _exec_error_text(exc), _classify(adapter, exc), verbatim=_is_rejection(exc)
    )


def _is_rejection(exc: BaseException) -> bool:
    return isinstance(exc, QueryRejectedError)


def _llm_hint(exc: BaseException) -> str | None:
    return exc.llm_hint if isinstance(exc, QueryRejectedError) else None


# Failures no rewritten query can fix: a permission denial (a rewrite would be refused
# the same way, or would reach the data by a workaround) and a lost connection.
_UNCORRECTABLE = frozenset({ErrorCategory.PERMISSION, ErrorCategory.CONNECTION})


def _correctable(
    rejected: bool, hint: str | None, category: ErrorCategory = ErrorCategory.OTHER
) -> bool:
    """Whether a correction attempt can help with the latest failure.

    *rejected* is True when the failure is a ``QueryRejectedError``. Such a rejection
    without an ``llm_hint`` (``None`` or empty, as ``describe_error_for_llm`` reads it) is
    nothing the model can change (a row filter that needs migration, a malformed key, a
    missing fence), and neither is a permission denial or a lost connection (*category*,
    ``_UNCORRECTABLE``): every correction loop ends at once, with no LLM call, and the user
    sees the failure.
    """
    if category in _UNCORRECTABLE:
        return False
    return not rejected or bool(hint)


def _is_query_fault(category: ErrorCategory, rejected: bool, hint: str | None) -> bool:
    """Whether a statement's failure is the query's fault: a different query could succeed.

    A timeout, a permission error and a connection lost while the statement ran are the
    database's state, not the query's text (the same SQL can succeed later, a rewrite would
    be refused the same way), and a rejection that ``_correctable`` rules out is nothing a
    model can change. A failed connect never runs the statement.
    """
    if category is ErrorCategory.TIMEOUT:
        return False
    return _correctable(rejected, hint, category)


def _classify(adapter: object, exc: BaseException) -> ErrorCategory:
    """``adapter.classify_error`` with a never-raise guard, then the builtin timeout fallback.

    A mock or wrapper without the hook (or returning something that is not an
    ``ErrorCategory``) reads OTHER rather than failing the turn. An adapter's ``REJECTED``
    also reads OTHER: that category is reserved for ``QueryRejectedError``, whose text is
    ours and is shown and sent to the LLM unredacted.
    """
    if isinstance(exc, QueryRejectedError):
        return ErrorCategory.REJECTED
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
    if not isinstance(category, ErrorCategory) or category is ErrorCategory.REJECTED:
        category = ErrorCategory.OTHER
    return classify_builtin(exc) if category is ErrorCategory.OTHER else category


def _connect_failed_result(
    conn_result: object, generated_query: str, explanation: str
) -> _ExecutionResult:
    """The result of a turn whose connect failed: the statement never ran."""
    return _ExecutionResult(
        results_response=None,
        execution_time_ms=None,
        bytes_scanned=None,
        status="error",
        error=f"Connection failed: {getattr(conn_result, 'message', '')}",
        generated_query=generated_query,
        explanation=explanation,
        connected=False,
        query_fault=False,  # the statement never ran
    )


def _log_zero_result_failure(adapter: object, exc: BaseException) -> None:
    """Log a refined zero-result query that failed or was refused; the original 0-row result
    stands. Both paths write this line. The log holds the redacted text, as the user-facing
    path does, and the redaction runs only when the line is written."""
    if not logger.isEnabledFor(logging.DEBUG):
        return
    category = _classify(adapter, exc)
    logger.debug(
        "Zero-result refinement failed, keeping the original result (%s, %s): %s",
        category,
        type(exc).__name__,
        redact_error_for_user(category, _exec_error_text(exc)),
    )


_UNSORTABLE = "The query could not be parsed as a single SELECT, so it cannot be sorted."

# The clauses of the outermost query that follow its ORDER BY.
_ROW_LIMIT_TOKENS = frozenset({TokenType.LIMIT, TokenType.OFFSET, TokenType.FETCH})


def _inject_order_by(sql: str, column: str, direction: str, dialect: str) -> str:
    """*sql* sorted by the result column *column*: its outermost query's ORDER BY replaced.

    The statement is tokenized and parsed with sqlglot in the connection's dialect and must
    be exactly one query (a SELECT, a set operation or a parenthesised query), else the sort
    is refused with a ``ValueError``. The outermost query's clauses are the ones outside
    every parenthesis, so a CTE, a subquery, a window's or an aggregate's ORDER BY and every
    string literal and comment are left alone. Its ORDER BY is removed and the new one is
    placed before its first LIMIT, OFFSET or FETCH, which keep their order and text.

    The rest of the statement is kept as written, not regenerated: the server's sql_mode
    (ANSI_QUOTES, NO_BACKSLASH_ESCAPES) is only known after connect, and the checks that
    ``row_filter._executable`` runs for it must see the original text. A regenerated
    statement would also turn a double-quoted identifier under ANSI_QUOTES into a string.

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
    direction = direction.upper()
    if direction not in {"ASC", "DESC"}:
        raise ValueError(f"Invalid sort direction: {direction!r}")

    if (dialect or "").lower() == "mysql":
        order_clause = "ORDER BY `" + column.replace("`", "``") + "` " + direction
    else:
        order_clause = 'ORDER BY "' + column.replace('"', '""') + '" ' + direction

    read = sqlglot_dialect(dialect)
    original = _parse_one_query(sql, read)
    # A locking clause (FOR UPDATE, FOR SHARE, LOCK IN SHARE MODE, SKIP LOCKED) follows the
    # ORDER BY and is not a read: the sort is refused (the validator refuses it too).
    if original.find(exp.Lock) is not None:
        raise ValueError(_UNSORTABLE)

    tokens = Dialect.get_or_raise(read).tokenize(sql)
    # The statement's text ends before its trailing semicolons; a comment after its last
    # token stays with the last clause.
    while tokens and tokens[-1].token_type is TokenType.SEMICOLON:
        tokens.pop()
    end = tokens[-1].end + 1 if tokens else 0
    # Keep a comment after the last token unless a semicolon precedes it.
    rest = sql[end:]
    text = (sql[:end] if ";" in rest else sql).rstrip()
    order_start: int | None = None
    order_end: int | None = None
    limit_start: int | None = None
    depth = 0
    for token in tokens:
        if token.token_type is TokenType.L_PAREN:
            depth += 1
        elif token.token_type is TokenType.R_PAREN:
            depth -= 1
        elif depth == 0 and token.token_type is TokenType.ORDER_BY and order_start is None:
            order_start = token.start
        elif depth == 0 and token.token_type in _ROW_LIMIT_TOKENS:
            if order_start is not None and order_end is None:
                order_end = token.start
            if limit_start is None:
                limit_start = token.start
    if order_start is not None:
        # The ORDER BY runs to the next LIMIT, OFFSET or FETCH, or to the end.
        removed_end = order_end if order_end is not None else len(text)
        text = text[:order_start] + text[removed_end:]
        if limit_start is not None and limit_start >= removed_end:
            limit_start -= removed_end - order_start
    if limit_start is None:
        result = text.rstrip() + f"\n{order_clause}"
    else:
        result = text[:limit_start].rstrip() + f"\n{order_clause}\n" + text[limit_start:]

    # The scan reads tokens, not the grammar: an identifier spelled like a clause (a column
    # named offset or limit) would be taken for one. The edited statement must parse to the
    # original with only the outer ORDER BY replaced.
    try:
        edited = _parse_one_query(result, read)
    except ValueError:
        raise ValueError(_UNSORTABLE) from None
    wanted_order = sqlglot.parse_one(f"SELECT 1 {order_clause}", read=read).args["order"]
    edited_order = edited.args.get("order")
    edited, original = edited.copy(), original.copy()
    edited.set("order", None)
    original.set("order", None)
    if edited_order != wanted_order or edited != original:
        raise ValueError(_UNSORTABLE)
    return result


def _parse_one_query(sql: str, read: str | None) -> exp.Query:
    """*sql* parsed as exactly one query, or ``ValueError(_UNSORTABLE)``."""
    try:
        parsed = sqlglot.parse(sql, read=read, error_level=ErrorLevel.IMMEDIATE)
    except (ParseError, TokenError) as exc:
        raise ValueError(_UNSORTABLE) from exc
    statements = [
        statement
        for statement in parsed
        if statement is not None and not isinstance(statement, exp.Semicolon)
    ]
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError(_UNSORTABLE)
    return statements[0]


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
    # A failed connect is reported by the adapter's result, not raised. The turn ends at
    # once, as on the streaming path: the statement never runs, so nothing is corrected.
    if not connected:
        conn_result = await adapter.connect(config_dict)
        if not getattr(conn_result, "success", True):
            return _connect_failed_result(conn_result, generated_query, explanation)
        connected = True
    max_rows = min(options.get("max_rows", settings.default_row_limit), settings.default_row_limit)
    dialect = adapter.query_dialect
    dq_literal = _double_quote_is_literal(adapter)
    bs_escape = _backslash_is_escape(adapter)
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
            executed = _executable(
                clean_query,
                privacy,
                dialect,
                double_quote_is_string_literal=dq_literal,
                backslash_is_escape=bs_escape,
            )
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

        if (
            attempt < max_exec_attempts - 1
            and not cache_hit
            and provider is not None
            and _correctable(exec_verbatim, exec_hint, exec_category)
        ):
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

    query_fault = False
    if exec_error:
        error = _user_facing_error(exec_error, exec_category, error_note, verbatim=exec_verbatim)
        status = "error"
        query_fault = _is_query_fault(exec_category, exec_verbatim, exec_hint)

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
                zero_executed = _executable(
                    zero_query,
                    privacy,
                    dialect,
                    double_quote_is_string_literal=dq_literal,
                    backslash_is_escape=bs_escape,
                )
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
                _log_zero_result_failure(adapter, e)
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
        query_fault=query_fault,
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

    def restore(self, saved: _BatchAccumulator) -> None:
        """Put back the state of *saved*, a ``dataclasses.replace`` copy taken before a run.
        The copy is shallow: safe because ``reset_rows`` gives the run a new row list and
        the run assigns, never mutates, the column lists."""
        for item in fields(self):
            setattr(self, item.name, getattr(saved, item.name))


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


def _stream_executable(
    adapter: BaseDataSource, clean_query: str, privacy: PrivacySettings | None, dq_literal: bool
) -> str:
    """*clean_query* as the database receives it on the streaming path (``_executable``)."""
    return _executable(
        clean_query,
        privacy,
        adapter.query_dialect,
        double_quote_is_string_literal=dq_literal,
        backslash_is_escape=_backslash_is_escape(adapter),
    )


async def _stream_statement(
    adapter: BaseDataSource,
    clean_query: str,
    *,
    privacy: PrivacySettings | None,
    dq_literal: bool,
    timeout: int,  # noqa: ASYNC109
    max_rows: int,
    acc: _BatchAccumulator,
    executed: str | None = None,
) -> AsyncGenerator[RowBatchEvent, None]:
    """Execute one statement on the streaming path and yield its masked ``row_batch`` events.

    The database receives *clean_query* rewritten by the row filter (*executed*, when the
    caller already rewrote it); the result mask is planned once for this statement (from
    the clean SQL) and stored on *acc*, so every batch here and the final payload built
    from *acc* are redacted by the same plan.
    """
    if executed is None:
        executed = _stream_executable(adapter, clean_query, privacy, dq_literal)
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
    - ``out["exec_hint"]``      — the latest failure's ``QueryRejectedError.llm_hint``,
                                  else None
    - ``out["exec_verbatim"]``  — True when the latest failure was a ``QueryRejectedError``
    - ``out["error_note"]``     — the correction's note when it gave up with one

    A failure that ``_correctable`` rules out (a rejection without a hint, a permission
    denial, a lost connection) ends the loop before its LLM call, leaving *out* as it is.
    """
    for _attempt in range(1, _MAX_SELF_CORRECTION_ATTEMPTS + 1):
        if not _correctable(out["exec_verbatim"], out["exec_hint"], out["exec_category"]):
            return
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
    plan); the prompt and the ``sql`` event carry the clean query. The ``sql`` event, which
    resets the client's result, is sent only once the row filter accepted the refined query;
    when its run then fails, the original query is announced again with an empty first batch
    of the original columns, so the client ends where the stored message does.

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
        # Keep the original 0-row result, query and explanation when the refined query is
        # refused or fails, as _execute_auto_query does: it is adopted only once it has run.
        try:
            zero_executed = _stream_executable(adapter, zero_query, privacy, dq_literal)
        except Exception as exc:
            _log_zero_result_failure(adapter, exc)
            out["zero_result_reexecuted"] = False
            return
        original = replace(acc)
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
                executed=zero_executed,
            ):
                yield event
        except Exception as exc:
            _log_zero_result_failure(adapter, exc)
            out["zero_result_reexecuted"] = False
            # Back to the original statement: its accumulator, then its SQL and columns on
            # the client, whose result the refined query's ``sql`` event cleared.
            acc.restore(original)
            yield SqlEvent(type="sql", query=out["clean_query"], dialect=adapter.query_dialect)
            # The real adapters yield no batch for a 0-row result, so the original columns
            # are usually empty: the ``sql`` event alone then leaves the client with no
            # columns, as the stored message has. A batch is sent only when there were some.
            if acc.columns:
                yield RowBatchEvent(
                    type="row_batch",
                    rows=[],
                    columns=acc.columns,
                    column_types=acc.column_types,
                    batch_index=0,
                    truncated=acc.truncated,
                )
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
            result_out.result = _connect_failed_result(conn_result, generated_query, explanation)
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
            query_fault=_is_query_fault(
                out["exec_category"], out["exec_verbatim"], out["exec_hint"]
            ),
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
