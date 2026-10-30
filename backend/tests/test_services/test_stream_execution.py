# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Unit tests for the streaming execution helpers in execution.py.

Covers:
- _BatchAccumulator (QUAL-11 accumulator)
- _stream_cursor_batches (QUAL-11 extracted helper)
- max_rows clamping in _stream_execute_with_correction (BUG-2)
- _stream_execute_with_correction: happy path, self-correction, zero-result
  recovery, connection failure (QUAL-31)
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import get_settings
from app.datasources.errors import ErrorCategory
from app.datasources.models import (
    ConnectionResult,
    DataSourceSchema,
    PrivacySettings,
    QueryResult,
    ValidationResult,
)
from app.providers.base import LLMResponse
from app.services.execution import (
    _BatchAccumulator,
    _stream_cursor_batches,
    _stream_execute_with_correction,
    _StreamResult,
)
from app.services.intent_classifier import QueryIntent
from app.services.row_filter import row_filter_fingerprint
from app.services.row_filter_errors import RowFilterRejectedError
from app.services.row_filter_rewrite import apply_row_filters

# ── Shared helpers ─────────────────────────────────────────────────────────────


def _make_settings(default_row_limit: int = 100, default_query_timeout: int = 30) -> MagicMock:
    s = MagicMock()
    s.default_row_limit = default_row_limit
    s.default_query_timeout = default_query_timeout
    return s


def _make_batch(
    rows: list,
    columns: tuple[str, ...] = ("id",),
    column_types: tuple[str, ...] = ("integer",),
    truncated: bool = False,
    execution_time_ms: float = 5.0,
) -> QueryResult:
    """Build a QueryResult representing one streaming batch."""
    return QueryResult(
        columns=list(columns),
        column_types=list(column_types),
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        execution_time_ms=execution_time_ms,
    )


def _make_adapter(*batch_sequences: list[QueryResult]) -> MagicMock:
    """Build an adapter whose execute_query_stream is an async generator.

    Each positional argument is one list of QueryResult batches for one call.
    Successive calls to execute_query_stream consume the sequences in order.
    """
    call_count = 0

    async def _stream_gen(query, **_kwargs):
        nonlocal call_count
        seq_index = min(call_count, len(batch_sequences) - 1)
        call_count += 1
        for batch in batch_sequences[seq_index]:
            yield batch

    adapter = MagicMock()
    adapter.query_dialect = "postgresql"
    adapter.execute_query_stream = _stream_gen
    adapter.validate_query = MagicMock()
    return adapter


def _make_provider(
    query: str = "SELECT 1",
    explanation: str = "ok",
    raises: Exception | None = None,
) -> MagicMock:
    """Build a provider mock matching the pattern used in test_execution_correction.py."""
    provider = MagicMock()
    provider.max_output_tokens = 4096
    if raises:
        provider.generate_response = AsyncMock(side_effect=raises)
    else:
        provider.generate_response = AsyncMock(
            return_value=LLMResponse(
                query=query,
                explanation=explanation,
                raw_response=f"QUERY:\n```sql\n{query}\n```\nEXPLANATION: {explanation}",
                model="test-model",
            )
        )
    return provider


async def _collect_events(gen) -> list:
    """Drain an SSE async generator into a list."""
    return [event async for event in gen]


async def _run_stream(
    *,
    generated_query: str = "SELECT 1",
    explanation: str = "initial explanation",
    adapter: MagicMock,
    connected: bool = True,
    cache_hit: bool = False,
    provider=None,
    configured_model: str = "test-model",
    configured_max_tokens: int = 4096,
    system_prompt: str = "You are an assistant.",
    history: list | None = None,
    message: str = "Show me the data",
    schema: DataSourceSchema | None = None,
    options: dict | None = None,
    settings=None,
    intent: QueryIntent = QueryIntent.UNKNOWN,
    privacy: PrivacySettings | None = None,
) -> tuple[list, _StreamResult]:
    """Run _stream_execute_with_correction and return (events, result_out)."""
    result_out = _StreamResult()
    events = await _collect_events(
        _stream_execute_with_correction(
            generated_query=generated_query,
            explanation=explanation,
            adapter=adapter,
            config_dict={},
            connected=connected,
            cache_hit=cache_hit,
            provider=provider,
            configured_model=configured_model,
            configured_max_tokens=configured_max_tokens,
            system_prompt=system_prompt,
            history=history or [],
            message=message,
            schema=schema or DataSourceSchema(source_type="postgresql"),
            options=options or {},
            settings=settings or _make_settings(),
            result_out=result_out,
            privacy=privacy,
            intent=intent,
        )
    )
    return events, result_out


# ── _BatchAccumulator ──────────────────────────────────────────────────────────


class TestBatchAccumulator:
    def test_default_state(self) -> None:
        acc = _BatchAccumulator()
        assert acc.columns == []
        assert acc.column_types == []
        assert acc.rows == []
        assert acc.truncated is False
        assert acc.execution_time_ms is None
        assert acc.batch_index == 0

    def test_reset_rows_clears_rows_and_batch_index(self) -> None:
        acc = _BatchAccumulator()
        acc.columns = ["id"]
        acc.column_types = ["integer"]
        acc.rows = [[1], [2]]
        acc.batch_index = 3
        acc.execution_time_ms = 10.0

        acc.reset_rows()

        # rows and batch_index cleared
        assert acc.rows == []
        assert acc.batch_index == 0
        # column metadata preserved
        assert acc.columns == ["id"]
        assert acc.column_types == ["integer"]
        assert acc.execution_time_ms == 10.0


# ── _stream_cursor_batches ─────────────────────────────────────────────────────


class TestStreamCursorBatches:
    async def test_yields_row_batch_events(self) -> None:
        batches = [
            _make_batch([[1], [2]], columns=("id",)),
            _make_batch([[3]], columns=("id",)),
        ]
        adapter = _make_adapter(batches)
        acc = _BatchAccumulator()

        events = [
            event
            async for event in _stream_cursor_batches(
                adapter, "SELECT id FROM t", timeout=30, max_rows=100, acc=acc
            )
        ]

        assert len(events) == 2
        assert events[0]["type"] == "row_batch"
        assert events[0]["batch_index"] == 0
        assert events[1]["batch_index"] == 1

    async def test_accumulates_columns_and_rows(self) -> None:
        batches = [
            _make_batch(
                [[10], [20]], columns=("n",), column_types=("bigint",), execution_time_ms=7.5
            ),
            _make_batch([[30]], columns=("n",), column_types=("bigint",)),
        ]
        adapter = _make_adapter(batches)
        acc = _BatchAccumulator()

        [
            _
            async for _ in _stream_cursor_batches(
                adapter, "SELECT n FROM t", timeout=30, max_rows=100, acc=acc
            )
        ]

        assert acc.columns == ["n"]
        assert acc.column_types == ["bigint"]
        assert acc.rows == [[10], [20], [30]]
        assert acc.execution_time_ms == 7.5  # captured from first batch
        assert acc.batch_index == 2

    async def test_propagates_truncated_flag(self) -> None:
        batches = [_make_batch([[1]], truncated=True)]
        adapter = _make_adapter(batches)
        acc = _BatchAccumulator()

        [
            _
            async for _ in _stream_cursor_batches(
                adapter, "SELECT 1", timeout=30, max_rows=1, acc=acc
            )
        ]

        assert acc.truncated is True

    async def test_empty_stream_leaves_accumulator_untouched(self) -> None:
        adapter = _make_adapter([])  # no batches
        acc = _BatchAccumulator()

        events = [
            _
            async for _ in _stream_cursor_batches(
                adapter, "SELECT 1", timeout=30, max_rows=100, acc=acc
            )
        ]

        assert events == []
        assert acc.rows == []
        assert acc.batch_index == 0


# ── max_rows clamping (BUG-2) ─────────────────────────────────────────────────


class TestMaxRowsClamping:
    """Verify _stream_execute_with_correction clamps max_rows to settings.default_row_limit."""

    async def test_default_row_limit_used_when_no_option(self) -> None:
        """options without 'max_rows' → settings.default_row_limit is used."""
        captured: dict = {}

        async def _capturing_stream(query, *, timeout, batch_size, max_rows):  # noqa: ASYNC109
            captured["max_rows"] = max_rows
            yield _make_batch([[1]])

        adapter = MagicMock()
        adapter.query_dialect = "postgresql"
        adapter.execute_query_stream = _capturing_stream

        settings = _make_settings(default_row_limit=50)
        await _run_stream(adapter=adapter, options={}, settings=settings)

        assert captured["max_rows"] == 50

    async def test_user_max_rows_clamped_to_limit(self) -> None:
        """options['max_rows'] > default_row_limit → clamped to default_row_limit."""
        captured: dict = {}

        async def _capturing_stream(query, *, timeout, batch_size, max_rows):  # noqa: ASYNC109
            captured["max_rows"] = max_rows
            yield _make_batch([[1]])

        adapter = MagicMock()
        adapter.query_dialect = "postgresql"
        adapter.execute_query_stream = _capturing_stream

        settings = _make_settings(default_row_limit=100)
        await _run_stream(adapter=adapter, options={"max_rows": 9999}, settings=settings)

        assert captured["max_rows"] == 100  # clamped, not 9999

    async def test_user_max_rows_below_limit_respected(self) -> None:
        """options['max_rows'] < default_row_limit → user value is preserved."""
        captured: dict = {}

        async def _capturing_stream(query, *, timeout, batch_size, max_rows):  # noqa: ASYNC109
            captured["max_rows"] = max_rows
            yield _make_batch([[1]])

        adapter = MagicMock()
        adapter.query_dialect = "postgresql"
        adapter.execute_query_stream = _capturing_stream

        settings = _make_settings(default_row_limit=100)
        await _run_stream(adapter=adapter, options={"max_rows": 10}, settings=settings)

        assert captured["max_rows"] == 10  # user value preserved


# ── Happy path ────────────────────────────────────────────────────────────────


class TestStreamExecuteHappyPath:
    async def test_returns_execution_result_with_rows(self) -> None:
        adapter = _make_adapter([_make_batch([[1], [2]], columns=("id",))])
        _events, result_out = await _run_stream(adapter=adapter)

        assert result_out.result is not None
        assert result_out.result.status == "executed"
        assert result_out.result.error is None
        assert result_out.result.results_response is not None
        assert result_out.result.results_response.rows == [[1], [2]]
        assert result_out.result.results_response.columns == ["id"]

    async def test_yields_row_batch_events(self) -> None:
        adapter = _make_adapter(
            [
                _make_batch([[1], [2]]),
                # (only one batch sequence needed here)
            ]
        )
        events, _ = await _run_stream(adapter=adapter)

        row_events = [e for e in events if e["type"] == "row_batch"]
        assert len(row_events) == 1
        assert row_events[0]["rows"] == [[1], [2]]

    async def test_status_is_cached_when_cache_hit(self) -> None:
        adapter = _make_adapter([_make_batch([[1]])])
        _, result_out = await _run_stream(adapter=adapter, cache_hit=True)

        assert result_out.result.status == "cached"

    async def test_explanation_preserved_from_input(self) -> None:
        adapter = _make_adapter([_make_batch([[1]])])
        _, result_out = await _run_stream(adapter=adapter, explanation="my explanation")

        assert result_out.result.explanation == "my explanation"


# ── Self-correction retry loop ────────────────────────────────────────────────


class TestSelfCorrectionPhase:
    async def test_corrects_and_retries_on_exec_error(self) -> None:
        """Initial execution fails → correction → re-execution succeeds."""
        success_batch = _make_batch([[99]])

        call_count = 0

        async def _stream_gen(query, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("column not found")
            yield success_batch

        adapter = MagicMock()
        adapter.query_dialect = "postgresql"
        adapter.execute_query_stream = _stream_gen
        adapter.validate_query = MagicMock()

        provider = _make_provider(query="SELECT fixed FROM t", explanation="fixed it")

        with patch(
            "app.services.execution._attempt_sql_execution_correction",
            new=AsyncMock(return_value=("SELECT fixed FROM t", "fixed it")),
        ):
            events, result_out = await _run_stream(
                adapter=adapter,
                provider=provider,
                cache_hit=False,
            )

        assert result_out.result.status == "executed"
        assert result_out.result.error is None
        assert result_out.result.results_response.rows == [[99]]
        assert result_out.result.generated_query == "SELECT fixed FROM t"

        status_events = [e for e in events if e["type"] == "status"]
        assert any("Correcting" in e["message"] for e in status_events)

    async def test_error_result_when_all_corrections_fail(self) -> None:
        """Correction returns no query on every attempt → error stored in result_out."""

        async def _always_fails(query, **_kwargs):
            raise RuntimeError("syntax error")
            yield  # makes this an async generator so Python treats it as AsyncGenerator

        adapter = MagicMock()
        adapter.query_dialect = "postgresql"
        adapter.execute_query_stream = _always_fails
        adapter.validate_query = MagicMock()

        provider = _make_provider()

        with patch(
            "app.services.execution._attempt_sql_execution_correction",
            new=AsyncMock(return_value=(None, "could not correct")),
        ):
            _, result_out = await _run_stream(
                adapter=adapter,
                provider=provider,
                cache_hit=False,
            )

        assert result_out.result.status == "error"
        assert result_out.result.error is not None

    async def test_skipped_when_cache_hit(self) -> None:
        """Self-correction loop must not run when cache_hit=True."""

        async def _fails(query, **_kwargs):
            raise RuntimeError("should trigger correction")
            yield  # makes this an async generator so Python treats it as AsyncGenerator

        adapter = MagicMock()
        adapter.query_dialect = "postgresql"
        adapter.execute_query_stream = _fails

        mock_correction = AsyncMock()
        with patch("app.services.execution._attempt_sql_execution_correction", mock_correction):
            _, result_out = await _run_stream(
                adapter=adapter,
                cache_hit=True,
                provider=_make_provider(),
            )

        mock_correction.assert_not_called()
        assert result_out.result.status == "error"

    async def test_skipped_when_no_provider(self) -> None:
        """Self-correction loop must not run when provider is None."""

        async def _fails(query, **_kwargs):
            raise RuntimeError("db error")
            yield  # makes this an async generator so Python treats it as AsyncGenerator

        adapter = MagicMock()
        adapter.query_dialect = "postgresql"
        adapter.execute_query_stream = _fails

        mock_correction = AsyncMock()
        with patch("app.services.execution._attempt_sql_execution_correction", mock_correction):
            _, result_out = await _run_stream(
                adapter=adapter,
                cache_hit=False,
                provider=None,
            )

        mock_correction.assert_not_called()
        assert result_out.result.status == "error"


# ── Zero-result recovery phase ────────────────────────────────────────────────


class TestZeroResultPhase:
    async def test_refines_on_zero_rows(self) -> None:
        """Zero rows returned → zero-result correction triggered → refined rows returned."""
        empty_batch = _make_batch([], columns=("n",))
        refined_batch = _make_batch([[42]], columns=("n",))

        call_count = 0

        async def _stream_gen(query, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield empty_batch
            else:
                yield refined_batch

        adapter = MagicMock()
        adapter.query_dialect = "postgresql"
        adapter.execute_query_stream = _stream_gen

        with patch(
            "app.services.execution._attempt_zero_result_correction",
            new=AsyncMock(return_value=("SELECT n FROM refined", "refined explanation")),
        ):
            events, result_out = await _run_stream(
                adapter=adapter,
                provider=_make_provider(),
                cache_hit=False,
                intent=QueryIntent.UNKNOWN,
            )

        assert result_out.result.error is None
        assert result_out.result.results_response.rows == [[42]]
        assert result_out.result.generated_query == "SELECT n FROM refined"

        sql_events = [e for e in events if e["type"] == "sql"]
        assert any("refined" in e["query"] for e in sql_events)

    async def test_skipped_for_existence_intent(self) -> None:
        """Zero-result phase must not run for EXISTENCE-intent queries."""
        adapter = _make_adapter([_make_batch([])])

        mock_zero = AsyncMock()
        with patch("app.services.execution._attempt_zero_result_correction", mock_zero):
            _, _result_out = await _run_stream(
                adapter=adapter,
                provider=_make_provider(),
                cache_hit=False,
                intent=QueryIntent.EXISTENCE,
            )

        mock_zero.assert_not_called()

    async def test_skipped_when_cache_hit(self) -> None:
        """Zero-result phase must not run when result is from cache."""
        adapter = _make_adapter([_make_batch([])])

        mock_zero = AsyncMock()
        with patch("app.services.execution._attempt_zero_result_correction", mock_zero):
            _, _result_out = await _run_stream(
                adapter=adapter,
                provider=_make_provider(),
                cache_hit=True,
                intent=QueryIntent.UNKNOWN,
            )

        mock_zero.assert_not_called()

    async def test_skipped_when_no_provider(self) -> None:
        """Zero-result phase must not run when no provider is configured."""
        adapter = _make_adapter([_make_batch([])])

        mock_zero = AsyncMock()
        with patch("app.services.execution._attempt_zero_result_correction", mock_zero):
            _, _result_out = await _run_stream(
                adapter=adapter,
                provider=None,
                cache_hit=False,
                intent=QueryIntent.UNKNOWN,
            )

        mock_zero.assert_not_called()

    async def test_original_kept_when_correction_returns_no_query(self) -> None:
        """When zero-result correction returns (None, explanation), original rows kept."""
        adapter = _make_adapter([_make_batch([])])

        with patch(
            "app.services.execution._attempt_zero_result_correction",
            new=AsyncMock(return_value=(None, "no better query available")),
        ):
            _, result_out = await _run_stream(
                adapter=adapter,
                provider=_make_provider(),
                cache_hit=False,
                intent=QueryIntent.UNKNOWN,
                explanation="original",
            )

        assert result_out.result.error is None
        assert result_out.result.results_response.rows == []
        assert result_out.result.explanation == "no better query available"


# ── Connection guard ───────────────────────────────────────────────────────────


class TestConnectionGuard:
    async def test_connects_when_not_connected(self) -> None:
        """connected=False → adapter.connect() is called before execution."""
        batches = [_make_batch([[1]])]
        adapter = _make_adapter(batches)
        adapter.connect = AsyncMock(return_value=ConnectionResult(success=True, message="ok"))

        _, result_out = await _run_stream(adapter=adapter, connected=False)

        adapter.connect.assert_called_once()
        assert result_out.result.connected is True

    async def test_stores_error_result_on_connection_failure(self) -> None:
        """Connection failure → error _ExecutionResult stored, no rows yielded."""
        adapter = MagicMock()
        adapter.connect = AsyncMock(return_value=ConnectionResult(success=False, message="refused"))

        events, result_out = await _run_stream(adapter=adapter, connected=False)

        assert result_out.result is not None
        assert result_out.result.status == "error"
        assert "refused" in result_out.result.error
        assert result_out.result.connected is False
        # No row_batch events should have been emitted
        assert not any(e.get("type") == "row_batch" for e in events)


class TestCorrectionPromptNeverCarriesTheDatabasesText:
    """A real execution failure must never hand the database's own error text to the
    correction LLM call, even though the retry succeeds and the turn completes."""

    async def test_a_conversion_error_value_never_reaches_the_correction_prompt(self) -> None:
        adapter = MagicMock()
        calls = {"n": 0}

        async def _stream(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError('invalid input syntax for type integer: "Jane Doe"')
                yield  # pragma: no cover — makes this an async generator
            yield _make_batch([[1]], columns=("id",), column_types=("int",))

        adapter.execute_query_stream = _stream
        adapter.query_dialect = "postgresql"
        adapter.validate_query = MagicMock(
            return_value=ValidationResult(is_valid=True, error_message=None)
        )
        adapter.classify_error = MagicMock(return_value=ErrorCategory.DATA)
        provider = _make_provider(query="SELECT id FROM customers", explanation="corrected")

        events, result_out = await _run_stream(
            generated_query="SELECT CAST(customer_name AS int) FROM customers",
            adapter=adapter,
            provider=provider,
            schema=DataSourceSchema(source_type="postgresql"),
        )

        assert any(event["type"] == "sql" for event in events), "expected a correction"
        for call in provider.generate_response.call_args_list:
            for value in call.kwargs.values():
                assert "Jane" not in str(value)
        correction_msg = provider.generate_response.call_args.kwargs["user_message"]
        assert "could not be converted, parsed or fitted" in correction_msg
        assert result_out.result is not None
        assert result_out.result.results_response is not None
        assert result_out.result.results_response.rows[0] == [1]


# ── Masking and row filter on the streaming path ──────────────────────────────

_FILTER = "tenant_id = 7"
# Every table the statements below reference, so each executed statement is rewritten.
_FILTERS = {
    f"public.{t}": _FILTER
    for t in ("t", "orders", "ordes", "ordrs", "users", "customers", "customers2")
}


def _filtered(sql: str) -> str:
    rewritten = apply_row_filters(sql, _FILTERS, "postgresql")
    assert "WHERE (tenant_id = 7)" in rewritten
    return rewritten


def _capturing_adapter(*batch_sequences: list[QueryResult]) -> tuple[MagicMock, list[str]]:
    """Like _make_adapter, but also records every SQL string handed to the database."""
    executed: list[str] = []
    call_count = 0

    async def _stream_gen(query, **_kwargs):
        nonlocal call_count
        executed.append(query)
        seq_index = min(call_count, len(batch_sequences) - 1)
        call_count += 1
        for batch in batch_sequences[seq_index]:
            yield batch

    adapter = MagicMock()
    adapter.query_dialect = "postgresql"
    adapter.execute_query_stream = _stream_gen
    adapter.validate_query = MagicMock()
    return adapter, executed


def _failing_then(
    failures: int, *success_batches: QueryResult, error: str = "boom"
) -> tuple[MagicMock, list[str]]:
    """An adapter whose first *failures* executions raise, then stream *success_batches*."""
    executed: list[str] = []

    async def _stream_gen(query, **_kwargs):
        executed.append(query)
        if len(executed) <= failures:
            raise RuntimeError(error)
            yield  # pragma: no cover — makes this an async generator
        for batch in success_batches:
            yield batch

    adapter = MagicMock()
    adapter.query_dialect = "postgresql"
    adapter.execute_query_stream = _stream_gen
    adapter.validate_query = MagicMock(
        return_value=ValidationResult(is_valid=True, error_message=None)
    )
    adapter.classify_error = MagicMock(return_value=ErrorCategory.UNKNOWN_NAME)
    return adapter, executed


def _sequenced_provider(*queries: str) -> MagicMock:
    """A provider whose successive generate_response calls return *queries* in order."""
    provider = MagicMock()
    provider.max_output_tokens = 4096
    provider.generate_response = AsyncMock(
        side_effect=[
            LLMResponse(
                query=q,
                explanation=f"fix {i}",
                raw_response=f"QUERY:\n```sql\n{q}\n```\nEXPLANATION: fix {i}",
                model="test-model",
            )
            for i, q in enumerate(queries)
        ]
    )
    return provider


def _llm_text(provider: MagicMock) -> str:
    """Everything every LLM call was handed, as one string."""
    return "\n".join(
        str(value)
        for call in provider.generate_response.call_args_list
        for value in (*call.args, *call.kwargs.values())
    )


class TestStreamMasking:
    async def test_sensitive_column_masked_in_row_batches_and_final_result(self) -> None:
        adapter = _make_adapter([_make_batch([[1, "a@x.com"]], columns=("id", "email"))])
        events, result_out = await _run_stream(
            generated_query="SELECT id, email FROM users",
            adapter=adapter,
            privacy=PrivacySettings(),
        )
        row_events = [e for e in events if e["type"] == "row_batch"]
        assert row_events[0]["rows"] == [[1, "[REDACTED]"]]
        assert result_out.result.results_response.rows == [[1, "[REDACTED]"]]

    async def test_aliased_sensitive_column_is_masked(self) -> None:
        adapter = _make_adapter([_make_batch([["a@x.com"]], columns=("contact",))])
        events, result_out = await _run_stream(
            generated_query="SELECT email AS contact FROM users",
            adapter=adapter,
            privacy=PrivacySettings(),
        )
        first_batch = next(e for e in events if e["type"] == "row_batch")
        assert first_batch["rows"] == [["[REDACTED]"]]
        assert result_out.result.results_response.rows == [["[REDACTED]"]]

    async def test_aggregate_over_sensitive_column_is_not_masked(self) -> None:
        adapter = _make_adapter([_make_batch([[5]], columns=("n",))])
        events, result_out = await _run_stream(
            generated_query="SELECT COUNT(email) AS n FROM users",
            adapter=adapter,
            privacy=PrivacySettings(),
        )
        first_batch = next(e for e in events if e["type"] == "row_batch")
        assert first_batch["rows"] == [[5]]
        assert result_out.result.results_response.rows == [[5]]

    async def test_no_privacy_leaves_rows_untouched(self) -> None:
        adapter = _make_adapter([_make_batch([["a@x.com"]], columns=("email",))])
        events, _ = await _run_stream(generated_query="SELECT email FROM users", adapter=adapter)
        first_batch = next(e for e in events if e["type"] == "row_batch")
        assert first_batch["rows"] == [["a@x.com"]]

    async def test_every_row_batch_is_masked(self) -> None:
        cols = ("id", "contact")
        adapter = _make_adapter(
            [
                _make_batch([[1, "a@x.com"]], columns=cols),
                _make_batch([[2, "b@x.com"], [3, "c@x.com"]], columns=cols),
            ]
        )
        events, result_out = await _run_stream(
            generated_query="SELECT id, email AS contact FROM users",
            adapter=adapter,
            privacy=PrivacySettings(),
        )
        batches = [e["rows"] for e in events if e["type"] == "row_batch"]
        assert batches == [
            [[1, "[REDACTED]"]],
            [[2, "[REDACTED]"], [3, "[REDACTED]"]],
        ]
        assert result_out.result.results_response.rows == [
            [1, "[REDACTED]"],
            [2, "[REDACTED]"],
            [3, "[REDACTED]"],
        ]

    async def test_corrected_query_batches_are_masked(self) -> None:
        adapter, _ = _failing_then(1, _make_batch([["a@x.com"]], columns=("contact",)))
        provider = _sequenced_provider("SELECT email AS contact FROM users")

        events, result_out = await _run_stream(
            generated_query="SELECT id FROM ordrs",
            adapter=adapter,
            provider=provider,
            privacy=PrivacySettings(),
        )

        batches = [e["rows"] for e in events if e["type"] == "row_batch"]
        assert batches == [[["[REDACTED]"]]]
        assert result_out.result.results_response.rows == [["[REDACTED]"]]

    async def test_mask_is_reclassified_after_a_correction(self) -> None:
        """The plan follows the SQL that ran: a stale one would still read ``contact`` as the
        aliased email of the failed query and redact the corrected query's count."""
        adapter, _ = _failing_then(1, _make_batch([[5]], columns=("contact",)))
        provider = _sequenced_provider("SELECT COUNT(email) AS contact FROM users")

        events, result_out = await _run_stream(
            generated_query="SELECT email AS contact FROM users",
            adapter=adapter,
            provider=provider,
            privacy=PrivacySettings(),
        )

        batches = [e["rows"] for e in events if e["type"] == "row_batch"]
        assert batches == [[[5]]]
        assert result_out.result.results_response.rows == [[5]]

    async def test_zero_result_reexecution_batches_are_masked(self) -> None:
        adapter, _ = _capturing_adapter(
            [_make_batch([], columns=("id",))],
            [_make_batch([["a@x.com"]], columns=("contact",))],
        )
        adapter.validate_query = MagicMock(
            return_value=ValidationResult(is_valid=True, error_message=None)
        )
        provider = _sequenced_provider("SELECT email AS contact FROM users")

        events, result_out = await _run_stream(
            generated_query="SELECT id FROM users WHERE id < 0",
            adapter=adapter,
            provider=provider,
            privacy=PrivacySettings(),
        )

        non_empty = [e["rows"] for e in events if e["type"] == "row_batch" and e["rows"]]
        assert non_empty == [[["[REDACTED]"]]]
        assert result_out.result.generated_query == "SELECT email AS contact FROM users"
        assert result_out.result.results_response.rows == [["[REDACTED]"]]

    async def test_masked_and_rewritten_together(self) -> None:
        """The derived table's inner ``SELECT *`` neither hides the alias nor defeats the
        aggregate exemption: classification reads the clean SQL."""
        adapter, executed = _capturing_adapter(
            [_make_batch([["a@x.com", 3]], columns=("contact", "n"))]
        )
        sql = "SELECT email AS contact, COUNT(email) AS n FROM users GROUP BY email"

        events, result_out = await _run_stream(
            generated_query=sql,
            adapter=adapter,
            privacy=PrivacySettings(row_filters=_FILTERS),
        )

        assert executed == [_filtered(sql)]
        first_batch = next(e for e in events if e["type"] == "row_batch")
        assert first_batch["rows"] == [["[REDACTED]", 3]]
        assert result_out.result.results_response.rows == [["[REDACTED]", 3]]


class TestStreamRowFilter:
    async def test_executed_sql_is_rewritten_but_result_carries_clean_sql(self) -> None:
        adapter, executed = _capturing_adapter([_make_batch([[1]])])
        privacy = PrivacySettings(row_filters=_FILTERS)
        _, result_out = await _run_stream(
            generated_query="SELECT id FROM t", adapter=adapter, privacy=privacy
        )
        assert executed == [_filtered("SELECT id FROM t")]
        assert result_out.result.generated_query == "SELECT id FROM t"

    async def test_corrected_query_is_rewritten_again_and_prompt_sees_clean_sql(self) -> None:
        call_count = 0
        executed: list[str] = []

        async def _stream_gen(query, **_kwargs):
            nonlocal call_count
            executed.append(query)
            call_count += 1
            if call_count == 1:
                raise RuntimeError("boom")
                yield  # pragma: no cover
            yield _make_batch([[1]])

        adapter = MagicMock()
        adapter.query_dialect = "postgresql"
        adapter.execute_query_stream = _stream_gen
        adapter.validate_query = MagicMock(
            return_value=ValidationResult(is_valid=True, error_message=None)
        )
        provider = _make_provider(query="SELECT id FROM orders", explanation="fixed")
        privacy = PrivacySettings(row_filters=_FILTERS)

        events, result_out = await _run_stream(
            generated_query="SELECT id FROM ordrs",
            adapter=adapter,
            provider=provider,
            privacy=privacy,
        )

        assert executed[1] == _filtered("SELECT id FROM orders")
        assert result_out.result.generated_query == "SELECT id FROM orders"
        sql_events = [e for e in events if e["type"] == "sql"]
        assert sql_events[0]["query"] == "SELECT id FROM orders"
        prompt = provider.generate_response.call_args.kwargs["user_message"]
        assert "tenant_id = 7" not in prompt
        assert "SELECT id FROM ordrs" in prompt

    async def test_every_correction_attempt_is_rewritten_and_prompts_stay_clean(self) -> None:
        adapter, executed = _failing_then(2, _make_batch([[1]]))
        provider = _sequenced_provider("SELECT id FROM ordes", "SELECT id FROM orders")

        events, result_out = await _run_stream(
            generated_query="SELECT id FROM ordrs",
            adapter=adapter,
            provider=provider,
            privacy=PrivacySettings(row_filters=_FILTERS),
        )

        assert executed == [
            _filtered("SELECT id FROM ordrs"),
            _filtered("SELECT id FROM ordes"),
            _filtered("SELECT id FROM orders"),
        ]
        prompts = [c.kwargs["user_message"] for c in provider.generate_response.call_args_list]
        assert "SELECT id FROM ordrs" in prompts[0]
        assert "SELECT id FROM ordes" in prompts[1]
        assert "WHERE (tenant_id" not in _llm_text(provider)
        # Holds only because the mock raises "boom". A real UNKNOWN_NAME error may name
        # tenant_id as a schema name, which is acceptable: it is a name, not a value.
        assert "tenant_id" not in _llm_text(provider)
        assert [e["query"] for e in events if e["type"] == "sql"] == [
            "SELECT id FROM ordes",
            "SELECT id FROM orders",
        ]
        assert result_out.result.generated_query == "SELECT id FROM orders"
        assert result_out.result.status == "executed"

    async def test_failed_turn_carries_clean_sql(self) -> None:
        adapter, executed = _failing_then(99)
        provider = _sequenced_provider("SELECT id FROM ordes", "SELECT id FROM orders")

        _, result_out = await _run_stream(
            generated_query="SELECT id FROM ordrs",
            adapter=adapter,
            provider=provider,
            privacy=PrivacySettings(row_filters=_FILTERS),
        )

        assert all("WHERE (tenant_id = 7)" in sql for sql in executed)
        assert len(executed) == 3
        assert result_out.result.status == "error"
        assert result_out.result.generated_query == "SELECT id FROM orders"

    async def test_zero_result_reexecution_is_rewritten_and_prompt_stays_clean(self) -> None:
        adapter, executed = _capturing_adapter(
            [_make_batch([], columns=("id",))], [_make_batch([[1]], columns=("id",))]
        )
        adapter.validate_query = MagicMock(
            return_value=ValidationResult(is_valid=True, error_message=None)
        )
        provider = _sequenced_provider("SELECT id FROM t WHERE id > 0")

        events, result_out = await _run_stream(
            generated_query="SELECT id FROM t WHERE id < 0",
            adapter=adapter,
            provider=provider,
            privacy=PrivacySettings(row_filters=_FILTERS),
        )

        assert executed == [
            _filtered("SELECT id FROM t WHERE id < 0"),
            _filtered("SELECT id FROM t WHERE id > 0"),
        ]
        prompt = provider.generate_response.call_args.kwargs["user_message"]
        assert "SELECT id FROM t WHERE id < 0" in prompt
        assert "WHERE (tenant_id" not in _llm_text(provider)
        # The zero-result prompt carries only the clean query. The schema shown to the LLM
        # may still list tenant_id as a column name, which is acceptable: not a value.
        assert "tenant_id" not in _llm_text(provider)
        assert [e["query"] for e in events if e["type"] == "sql"] == [
            "SELECT id FROM t WHERE id > 0"
        ]
        assert result_out.result.generated_query == "SELECT id FROM t WHERE id > 0"
        assert result_out.result.results_response.rows == [[1]]

    async def test_database_text_never_reaches_an_llm_call_with_a_filter_set(self) -> None:
        adapter, _ = _failing_then(
            1,
            _make_batch([], columns=("id",)),
            error='invalid input syntax for type integer: "Jane Doe"',
        )
        adapter.classify_error = MagicMock(return_value=ErrorCategory.DATA)
        provider = _sequenced_provider("SELECT id FROM customers", "SELECT id FROM customers2")

        _, result_out = await _run_stream(
            generated_query="SELECT CAST(name AS int) FROM customers",
            adapter=adapter,
            provider=provider,
            privacy=PrivacySettings(row_filters=_FILTERS),
        )

        # Both the execution correction and the zero-result refinement ran.
        assert provider.generate_response.await_count == 2
        assert "Jane" not in _llm_text(provider)
        assert "WHERE (tenant_id" not in _llm_text(provider)
        assert result_out.result.status == "executed"

    async def test_rejection_text_is_shown_verbatim_even_with_an_apostrophe(self) -> None:
        adapter, _ = _capturing_adapter([_make_batch([[1]])])
        privacy = PrivacySettings(row_filters=_FILTERS)
        rejection = RowFilterRejectedError("The admin's filter rejected it.")

        with patch("app.services.row_filter.apply_row_filters", side_effect=rejection):
            _, result_out = await _run_stream(
                generated_query="SELECT id FROM t", adapter=adapter, privacy=privacy
            )

        assert result_out.result.status == "error"
        assert result_out.result.error == "The admin's filter rejected it."

    async def test_rejection_hint_reaches_the_correction_prompt(self) -> None:
        adapter, _ = _capturing_adapter([_make_batch([[1]])])
        provider = _sequenced_provider("SELECT id FROM public.t")
        rejection = RowFilterRejectedError(
            "Table t is ambiguous.", llm_hint="Qualify it as schema.table."
        )
        calls = {"n": 0}
        real = apply_row_filters

        def _first_rejected(
            sql: str, filters: dict[str, str], dialect: str, **kwargs: object
        ) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise rejection
            return real(sql, filters, dialect, **kwargs)

        with patch("app.services.row_filter.apply_row_filters", side_effect=_first_rejected):
            _, result_out = await _run_stream(
                generated_query="SELECT id FROM t",
                adapter=adapter,
                provider=provider,
                privacy=PrivacySettings(row_filters=_FILTERS),
            )

        prompt = provider.generate_response.call_args_list[0].kwargs["user_message"]
        assert "Qualify it as schema.table." in prompt
        assert result_out.result.status == "executed"

    async def test_engine_error_after_a_rejection_is_redacted_and_carries_no_old_hint(self) -> None:
        adapter, _ = _failing_then(99, error="bad value 'secret' in row")
        adapter.classify_error = MagicMock(return_value=ErrorCategory.DATA)
        provider = _sequenced_provider("SELECT id FROM public.t", "SELECT id FROM public.t LIMIT 5")
        rejection = RowFilterRejectedError("first", llm_hint="HINT-A")
        calls = {"n": 0}

        def _first_rejected(
            sql: str, filters: dict[str, str], dialect: str, **kwargs: object
        ) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise rejection
            return apply_row_filters(sql, filters, dialect, **kwargs)

        with patch("app.services.row_filter.apply_row_filters", side_effect=_first_rejected):
            _, result_out = await _run_stream(
                generated_query="SELECT id FROM t",
                adapter=adapter,
                provider=provider,
                privacy=PrivacySettings(row_filters=_FILTERS),
            )

        prompts = [c.kwargs["user_message"] for c in provider.generate_response.call_args_list]
        assert len(prompts) == 2
        assert "HINT-A" in prompts[0]
        assert "HINT-A" not in prompts[1]
        assert result_out.result.status == "error"
        assert "secret" not in result_out.result.error


class TestZeroResultReexecutionFailure:
    async def test_failed_refinement_keeps_the_original_query_and_explanation(self) -> None:
        """A refined query that fails at the database is not adopted: the turn reports the
        original 0-row result under the original query and explanation."""
        executed: list[str] = []

        async def _stream_gen(query, **_kwargs):
            executed.append(query)
            if len(executed) == 1:
                yield _make_batch([], columns=("id",))
                return
            raise RuntimeError("boom")
            yield  # pragma: no cover

        adapter = MagicMock()
        adapter.query_dialect = "postgresql"
        adapter.execute_query_stream = _stream_gen
        adapter.validate_query = MagicMock(
            return_value=ValidationResult(is_valid=True, error_message=None)
        )
        provider = _sequenced_provider("SELECT id FROM t WHERE x = 2")

        events, result_out = await _run_stream(
            generated_query="SELECT id FROM t WHERE x = 1",
            explanation="original",
            adapter=adapter,
            provider=provider,
        )

        assert len(executed) == 2
        # The refined query is still announced before it runs.
        assert [e["query"] for e in events if e["type"] == "sql"] == [
            "SELECT id FROM t WHERE x = 2"
        ]
        assert result_out.result.status == "executed"
        assert result_out.result.generated_query == "SELECT id FROM t WHERE x = 1"
        assert result_out.result.explanation == "original"
        assert result_out.result.results_response.rows == []

    async def test_failed_refinement_is_logged_redacted(self, caplog) -> None:
        executed: list[str] = []

        async def _stream_gen(query, **_kwargs):
            executed.append(query)
            if len(executed) == 1:
                yield _make_batch([], columns=("id",))
                return
            raise RuntimeError('value "secret-value" out of range')
            yield  # pragma: no cover

        adapter = MagicMock()
        adapter.query_dialect = "postgresql"
        adapter.execute_query_stream = _stream_gen
        adapter.validate_query = MagicMock(
            return_value=ValidationResult(is_valid=True, error_message=None)
        )
        adapter.classify_error = MagicMock(return_value=ErrorCategory.DATA)
        provider = _sequenced_provider("SELECT id FROM t WHERE x = 2")

        with caplog.at_level(logging.DEBUG, logger="app.services.execution"):
            await _run_stream(
                generated_query="SELECT id FROM t WHERE x = 1",
                adapter=adapter,
                provider=provider,
            )

        assert len(executed) == 2
        assert "secret-value" not in caplog.text
        assert "Zero-result re-execution failed, keeping original (data): " in caplog.text


class TestStreamUserFacingErrorText:
    async def test_data_error_value_is_redacted_without_correction(self) -> None:
        adapter, _ = _failing_then(99, error='invalid input syntax for type integer: "Jane Doe"')
        adapter.classify_error = MagicMock(return_value=ErrorCategory.DATA)

        _, result_out = await _run_stream(
            generated_query="SELECT CAST(name AS int) FROM customers", adapter=adapter
        )

        assert result_out.result.status == "error"
        assert "Jane" not in result_out.result.error
        assert "invalid input syntax for type integer" in result_out.result.error

    async def test_data_error_value_is_redacted_after_corrections_fail(self) -> None:
        adapter, _ = _failing_then(99, error='invalid input syntax for type integer: "Jane Doe"')
        adapter.classify_error = MagicMock(return_value=ErrorCategory.DATA)
        provider = _sequenced_provider("SELECT CAST(name AS int) FROM c2", "SELECT 1 FROM c3")

        _, result_out = await _run_stream(
            generated_query="SELECT CAST(name AS int) FROM customers",
            adapter=adapter,
            provider=provider,
        )

        assert result_out.result.status == "error"
        assert "Jane" not in result_out.result.error
        assert "Jane" not in _llm_text(provider)

    async def test_note_is_appended_to_the_redacted_text(self) -> None:
        adapter, _ = _failing_then(99, error='invalid input syntax for type integer: "Jane Doe"')
        adapter.classify_error = MagicMock(return_value=ErrorCategory.DATA)

        with patch(
            "app.services.execution._attempt_sql_execution_correction",
            new=AsyncMock(return_value=(None, "gave up")),
        ):
            _, result_out = await _run_stream(
                generated_query="SELECT CAST(name AS int) FROM customers",
                adapter=adapter,
                provider=_make_provider(),
            )

        assert "Jane" not in result_out.result.error
        assert result_out.result.error.endswith("\n\nNote: gave up")


class TestFingerprintOnTheStreamingPath:
    async def test_payload_carries_the_fingerprint(self) -> None:
        adapter = _make_adapter([_make_batch(rows=[[1]], columns=("id",))])
        filters = {"public.t": "tenant_id = 7"}
        _events, result_out = await _run_stream(
            adapter=adapter,
            generated_query="SELECT id FROM t",
            privacy=PrivacySettings(row_filters=filters),
        )
        expected = row_filter_fingerprint(filters, get_settings().encryption_key)
        assert result_out.result.results_response.row_filter_fingerprint == expected

    async def test_no_filters_gives_the_empty_fingerprint(self) -> None:
        adapter = _make_adapter([_make_batch(rows=[[1]], columns=("id",))])
        _events, result_out = await _run_stream(adapter=adapter, generated_query="SELECT id FROM t")
        assert result_out.result.results_response.row_filter_fingerprint == ""


class TestNoBackslashEscapesOnTheStreamingPath:
    async def test_a_backslash_is_refused_and_corrected(self) -> None:
        from app.services import execution
        from app.services.row_filter import _executable

        bypass = "SELECT 'a\\' AS k, email AS x FROM users -- '"
        fixed = "SELECT 'a' AS k FROM users"
        adapter, executed = _capturing_adapter(
            [_make_batch([["a"]], columns=("k",), column_types=("text",))]
        )
        adapter.query_dialect = "MySQL"
        adapter.double_quote_is_string_literal = True
        adapter.backslash_is_escape = False
        provider = _sequenced_provider(fixed)
        with patch.object(execution, "_executable", wraps=_executable) as executable:
            _, result_out = await _run_stream(
                generated_query=bypass,
                adapter=adapter,
                provider=provider,
                privacy=PrivacySettings(),
            )
        first = executable.call_args_list[0]
        assert first.args[0] == bypass
        assert first.kwargs["backslash_is_escape"] is False
        assert executed == [fixed]
        assert "NO_BACKSLASH_ESCAPES" in _llm_text(provider)
        assert result_out.result.status == "executed"

    async def test_the_adapters_backslash_flag_is_passed(self) -> None:
        from app.services import execution
        from app.services.row_filter import _executable

        adapter, _ = _capturing_adapter([_make_batch([[1]])])
        adapter.backslash_is_escape = True
        with patch.object(execution, "_executable", wraps=_executable) as executable:
            await _run_stream(
                generated_query="SELECT id FROM t", adapter=adapter, privacy=PrivacySettings()
            )
        assert executable.call_args.kwargs["backslash_is_escape"] is True


class TestHintlessRejectionEndsTheCorrection:
    """A rejection without an llm_hint (a filter that needs migration, a malformed key, a
    missing fence) is nothing the model can change: no correction call is made and the user
    sees the rejection text as it is."""

    _TEXT = "The row filter on public.t needs migration, so the query was not run."

    async def test_a_hintless_rejection_makes_no_llm_call(self) -> None:
        adapter, executed = _capturing_adapter([_make_batch([[1]])])
        provider = _sequenced_provider("SELECT id FROM public.t")

        with patch(
            "app.services.row_filter.apply_row_filters",
            side_effect=RowFilterRejectedError(self._TEXT),
        ):
            events, result_out = await _run_stream(
                generated_query="SELECT id FROM t",
                adapter=adapter,
                provider=provider,
                privacy=PrivacySettings(row_filters=_FILTERS),
            )

        provider.generate_response.assert_not_awaited()
        assert executed == []
        assert result_out.result.status == "error"
        assert result_out.result.error == self._TEXT
        assert not any(e.get("message") == "Correcting query…" for e in events)

    async def test_a_hintless_rejection_of_a_corrected_query_ends_the_loop(self) -> None:
        adapter, _ = _capturing_adapter([_make_batch([[1]])])
        provider = _sequenced_provider("SELECT id FROM public.t", "SELECT id FROM public.t")
        rejections = [
            RowFilterRejectedError("Table t is ambiguous.", llm_hint="Qualify the table t."),
            RowFilterRejectedError(self._TEXT),
        ]

        with patch("app.services.row_filter.apply_row_filters", side_effect=rejections):
            _, result_out = await _run_stream(
                generated_query="SELECT id FROM t",
                adapter=adapter,
                provider=provider,
                privacy=PrivacySettings(row_filters=_FILTERS),
            )

        assert provider.generate_response.await_count == 1
        assert result_out.result.status == "error"
        assert result_out.result.error == self._TEXT

    async def test_a_rejection_with_a_hint_still_goes_through_correction(self) -> None:
        adapter, _ = _capturing_adapter([_make_batch([[1]])])
        provider = _sequenced_provider("SELECT id FROM public.t")
        rejection = RowFilterRejectedError("Table t is ambiguous.", llm_hint="Qualify it.")
        calls = {"n": 0}

        def _first_rejected(
            sql: str, filters: dict[str, str], dialect: str, **kwargs: object
        ) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise rejection
            return apply_row_filters(sql, filters, dialect, **kwargs)

        with patch("app.services.row_filter.apply_row_filters", side_effect=_first_rejected):
            _, result_out = await _run_stream(
                generated_query="SELECT id FROM t",
                adapter=adapter,
                provider=provider,
                privacy=PrivacySettings(row_filters=_FILTERS),
            )

        assert provider.generate_response.await_count == 1
        assert result_out.result.status == "executed"
