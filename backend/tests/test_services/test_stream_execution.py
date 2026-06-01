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

from unittest.mock import AsyncMock, MagicMock, patch

from app.datasources.models import ConnectionResult, DataSourceSchema, QueryResult
from app.providers.base import LLMResponse
from app.services.execution import (
    _BatchAccumulator,
    _stream_cursor_batches,
    _stream_execute_with_correction,
    _StreamResult,
)
from app.services.intent_classifier import QueryIntent

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
