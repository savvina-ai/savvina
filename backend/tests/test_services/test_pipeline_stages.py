# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Unit tests for extracted pipeline stage functions (QUAL-52) and
_stream_execute_with_correction (QUAL-9).

Each stage is tested in isolation by mocking its direct dependencies, not the
full database or LLM stack.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.datasources.models import PrivacySettings, QueryResult
from app.services.chat_service import (
    _PipelineError,
    _run_shared_pipeline,
    _stage_generate,
    _stage_load_connection,
    _stage_prune_and_filter,
    _stage_resolve_schema,
    _stage_validate,
)
from app.services.execution import (
    _stream_execute_with_correction,
    _StreamResult,
)
from app.services.intent_classifier import QueryIntent

_MODULE = "app.services.chat_service"
_EXEC_MODULE = "app.services.execution"


# ── Shared helpers ─────────────────────────────────────────────────────────────


def _make_settings(**overrides) -> MagicMock:
    s = MagicMock()
    s.encryption_key = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
    s.cache_enabled = False
    s.default_query_timeout = 30
    s.default_row_limit = 1000
    s.schema_pruning_enabled = True
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _make_conn_row(
    *,
    conn_id: str = "conn-1",
    source_type: str = "postgresql",
    execution_mode: str = "auto_execute",
    privacy_settings: dict | None = None,
    semantic_model: dict | None = None,
) -> MagicMock:
    conn = MagicMock()
    conn.id = conn_id
    conn.source_type = source_type
    conn.config_encrypted = b"encrypted-bytes"
    conn.privacy_settings = privacy_settings
    conn.execution_mode = execution_mode
    conn.semantic_model = semantic_model
    return conn


def _make_db_returning(value) -> MagicMock:
    """AsyncSession whose first execute() returns a scalar_one_or_none of value."""

    class _R:
        def __init__(self, v):
            self._v = v

        def scalar_one_or_none(self):
            return self._v

    db = MagicMock()
    db.execute = AsyncMock(return_value=_R(value))
    return db


def _make_schema_result(*, schema=None, connected=False, embeddings_available=False) -> MagicMock:
    sr = MagicMock()
    sr.schema = schema or MagicMock()
    sr.connected = connected
    sr.embeddings_available = embeddings_available
    return sr


def _make_gen_result(**kwargs) -> MagicMock:
    g = MagicMock()
    g.generated_query = kwargs.get("generated_query", "SELECT 1")
    g.explanation = kwargs.get("explanation", "ok")
    g.error = kwargs.get("error")
    g.status = kwargs.get("status", "executed")
    g.cache_hit = kwargs.get("cache_hit", False)
    g.cache_embedding = kwargs.get("cache_embedding")
    g.provider = kwargs.get("provider")
    g.configured_model = kwargs.get("configured_model", "m")
    g.configured_max_tokens = kwargs.get("configured_max_tokens", 4096)
    g.system_prompt = kwargs.get("system_prompt", "sys")
    g.history = kwargs.get("history", [])
    g.tokens_used = kwargs.get("tokens_used", 10)
    g.input_tokens = kwargs.get("input_tokens", 8)
    g.output_tokens = kwargs.get("output_tokens", 2)
    return g


def _make_val_result(**kwargs) -> MagicMock:
    v = MagicMock()
    v.generated_query = kwargs.get("generated_query", "SELECT 1")
    v.explanation = kwargs.get("explanation", "ok")
    v.error = kwargs.get("error")
    v.status = kwargs.get("status", "executed")
    return v


# ── _stage_load_connection ─────────────────────────────────────────────────────


class TestStageLoadConnection:
    async def test_returns_tuple_on_success(self):
        conn = _make_conn_row()
        db = _make_db_returning(conn)
        settings = _make_settings()

        adapter = MagicMock()

        with (
            patch(f"{_MODULE}.decrypt_value", return_value=json.dumps({"host": "db"})),
            patch(f"{_MODULE}.create_datasource", return_value=adapter),
        ):
            result_conn, config_dict, _privacy, result_adapter = await _stage_load_connection(
                "conn-1", db, settings
            )

        assert result_conn is conn
        assert config_dict == {"host": "db"}
        assert result_adapter is adapter

    async def test_raises_pipeline_error_when_not_found(self):
        db = _make_db_returning(None)
        settings = _make_settings()

        with pytest.raises(_PipelineError, match="not found"):
            await _stage_load_connection("missing-id", db, settings)

    async def test_privacy_defaults_to_empty_when_none(self):
        conn = _make_conn_row(privacy_settings=None)
        db = _make_db_returning(conn)
        settings = _make_settings()

        with (
            patch(f"{_MODULE}.decrypt_value", return_value=json.dumps({})),
            patch(f"{_MODULE}.create_datasource", return_value=MagicMock()),
        ):
            _, _, privacy, _ = await _stage_load_connection("conn-1", db, settings)

        assert isinstance(privacy, PrivacySettings)


# ── _stage_resolve_schema ──────────────────────────────────────────────────────


class TestStageResolveSchema:
    async def test_returns_schema_result_on_success(self):
        conn = _make_conn_row()
        sr = _make_schema_result(connected=True)

        with patch(f"{_MODULE}._resolve_schema", new=AsyncMock(return_value=sr)) as mock_rs:
            result = await _stage_resolve_schema(
                conn, MagicMock(), {}, None, False, MagicMock(), "user-1", MagicMock()
            )

        assert result is sr
        mock_rs.assert_awaited_once()

    async def test_wraps_exception_in_pipeline_error(self):
        conn = _make_conn_row()

        with (
            patch(f"{_MODULE}._resolve_schema", new=AsyncMock(side_effect=RuntimeError("DB down"))),
            pytest.raises(_PipelineError, match="Schema error"),
        ):
            await _stage_resolve_schema(
                conn, MagicMock(), {}, None, False, MagicMock(), "user-1", MagicMock()
            )


# ── _stage_prune_and_filter ────────────────────────────────────────────────────


class TestStagePruneAndFilter:
    async def test_returns_unpruned_schema_when_no_embeddings(self):
        schema = MagicMock()
        sr = _make_schema_result(schema=schema, embeddings_available=False)
        conn = _make_conn_row(semantic_model=None)
        settings = _make_settings(schema_pruning_enabled=True)

        pruned_schema, semantic_model = await _stage_prune_and_filter(
            sr, conn, "question", settings, MagicMock(), MagicMock(), "conn-1", "u", None
        )

        assert pruned_schema is schema
        assert semantic_model is None

    async def test_returns_unpruned_schema_when_pruning_disabled(self):
        schema = MagicMock()
        sr = _make_schema_result(schema=schema, embeddings_available=True)
        conn = _make_conn_row(semantic_model=None)
        settings = _make_settings(schema_pruning_enabled=False)

        with patch(f"{_MODULE}._select_relevant_tables") as mock_prune:
            pruned_schema, _ = await _stage_prune_and_filter(
                sr, conn, "question", settings, MagicMock(), MagicMock(), "conn-1", "u", None
            )

        mock_prune.assert_not_called()
        assert pruned_schema is schema

    async def test_calls_select_relevant_tables_when_embeddings_exist(self):
        original_schema = MagicMock()
        pruned = MagicMock()
        sr = _make_schema_result(schema=original_schema, embeddings_available=True)
        conn = _make_conn_row(semantic_model=None)
        settings = _make_settings(schema_pruning_enabled=True)

        with patch(
            f"{_MODULE}._select_relevant_tables", new=AsyncMock(return_value=pruned)
        ) as mock_prune:
            result_schema, _ = await _stage_prune_and_filter(
                sr, conn, "sales by region", settings, MagicMock(), MagicMock(), "conn-1", "u", None
            )

        mock_prune.assert_awaited_once()
        assert result_schema is pruned


# ── _stage_generate ────────────────────────────────────────────────────────────


class TestStageGenerate:
    async def test_returns_gen_result_on_success(self):
        gen = _make_gen_result()

        with patch(f"{_MODULE}._generate_query", new=AsyncMock(return_value=gen)):
            result = await _stage_generate(
                cache=MagicMock(),
                examples=MagicMock(),
                connection_id="c",
                message="q",
                session_id=None,
                adapter=MagicMock(),
                schema=MagicMock(),
                privacy=None,
                semantic_model=None,
                provider_name="claude",
                options={},
                settings=_make_settings(),
            )

        assert result is gen

    async def test_raises_pipeline_error_on_exception(self):
        with (
            patch(
                f"{_MODULE}._generate_query", new=AsyncMock(side_effect=RuntimeError("LLM timeout"))
            ),
            pytest.raises(_PipelineError, match="Generation error"),
        ):
            await _stage_generate(
                cache=MagicMock(),
                examples=MagicMock(),
                connection_id="c",
                message="q",
                session_id=None,
                adapter=MagicMock(),
                schema=MagicMock(),
                privacy=None,
                semantic_model=None,
                provider_name="claude",
                options={},
                settings=_make_settings(),
            )


# ── _stage_validate ────────────────────────────────────────────────────────────


class TestStageValidate:
    async def test_returns_validation_result_on_success(self):
        val = _make_val_result()
        gen = _make_gen_result()

        with patch(f"{_MODULE}._validate_and_correct_query", new=AsyncMock(return_value=val)):
            result = await _stage_validate(gen, MagicMock(), MagicMock(), "q")

        assert result is val

    async def test_raises_pipeline_error_on_exception(self):
        gen = _make_gen_result()

        with (
            patch(
                f"{_MODULE}._validate_and_correct_query",
                new=AsyncMock(side_effect=RuntimeError("validator crash")),
            ),
            pytest.raises(_PipelineError, match="Validation error"),
        ):
            await _stage_validate(gen, MagicMock(), MagicMock(), "q")

    async def test_soft_validation_error_does_not_raise(self):
        """val.error being set is a soft error — it should not raise _PipelineError."""
        val = _make_val_result(error="invalid column", status="error")
        gen = _make_gen_result()

        with patch(f"{_MODULE}._validate_and_correct_query", new=AsyncMock(return_value=val)):
            result = await _stage_validate(gen, MagicMock(), MagicMock(), "q")

        assert result.error == "invalid column"


# ── _run_shared_pipeline ───────────────────────────────────────────────────────


class TestRunSharedPipeline:
    async def test_returns_pipeline_context_on_success(self):
        conn = _make_conn_row()
        sr = _make_schema_result()
        gen = _make_gen_result()
        val = _make_val_result()
        session = MagicMock(id="sess-1")
        settings = _make_settings()

        with (
            patch(
                f"{_MODULE}._stage_load_connection",
                new=AsyncMock(return_value=(conn, {}, None, MagicMock())),
            ),
            patch(f"{_MODULE}._stage_resolve_schema", new=AsyncMock(return_value=sr)),
            patch(
                f"{_MODULE}._stage_prune_and_filter",
                new=AsyncMock(return_value=(MagicMock(), None)),
            ),
            patch(f"{_MODULE}._stage_generate", new=AsyncMock(return_value=gen)),
            patch(f"{_MODULE}._stage_validate", new=AsyncMock(return_value=val)),
            patch(f"{_MODULE}._get_or_create_session", new=AsyncMock(return_value=session)),
        ):
            ctx = await _run_shared_pipeline(
                connection_id="conn-1",
                session_id=None,
                message="query",
                provider_name="claude",
                options={},
                db=MagicMock(),
                user_id="user-1",
                cache=MagicMock(),
                examples=MagicMock(),
                settings=settings,
            )

        assert ctx.conn is conn
        assert ctx.gen is gen
        assert ctx.session is session
        assert ctx.generated_query == val.generated_query
        assert ctx.error == val.error

    async def test_propagates_pipeline_error_from_load_stage(self):
        with (
            patch(
                f"{_MODULE}._stage_load_connection",
                new=AsyncMock(side_effect=_PipelineError("Connection 'x' not found")),
            ),
            pytest.raises(_PipelineError, match="not found"),
        ):
            await _run_shared_pipeline(
                connection_id="x",
                session_id=None,
                message="q",
                provider_name="claude",
                options={},
                db=MagicMock(),
                user_id=None,
                cache=MagicMock(),
                examples=MagicMock(),
                settings=_make_settings(),
            )

    async def test_disconnects_adapter_when_later_stage_fails(self):
        adapter = MagicMock()
        adapter.disconnect = AsyncMock()
        conn = _make_conn_row()
        sr = _make_schema_result(connected=True)

        with (
            patch(
                f"{_MODULE}._stage_load_connection",
                new=AsyncMock(return_value=(conn, {}, None, adapter)),
            ),
            patch(f"{_MODULE}._stage_resolve_schema", new=AsyncMock(return_value=sr)),
            patch(
                f"{_MODULE}._stage_prune_and_filter",
                new=AsyncMock(side_effect=_PipelineError("prune fail")),
            ),
            pytest.raises(_PipelineError),
        ):
            await _run_shared_pipeline(
                connection_id="conn-1",
                session_id=None,
                message="q",
                provider_name="claude",
                options={},
                db=MagicMock(),
                user_id=None,
                cache=MagicMock(),
                examples=MagicMock(),
                settings=_make_settings(),
            )

        adapter.disconnect.assert_awaited_once()


# ── _stream_execute_with_correction ───────────────────────────────────────────


def _make_adapter_stream(rows=None, cols=None, *, fail_on_first=False) -> MagicMock:
    adapter = MagicMock()
    adapter.query_dialect = "postgresql"
    _cols = cols if cols is not None else ["col"]
    _rows = rows if rows is not None else [[1]]

    call_count = 0

    async def _stream(query, timeout=30, batch_size=50, max_rows=1000):  # noqa: ASYNC109
        nonlocal call_count
        call_count += 1
        if fail_on_first and call_count == 1:
            raise RuntimeError("execution failed")
        yield QueryResult(
            columns=_cols,
            column_types=["text"] * len(_cols),
            rows=_rows,
            row_count=len(_rows),
            truncated=False,
            execution_time_ms=5.0,
        )

    adapter.execute_query_stream = _stream
    adapter.connect = AsyncMock(return_value=MagicMock(success=True))
    adapter.disconnect = AsyncMock()
    return adapter


async def _collect_events(gen) -> list:
    return [e async for e in gen]


class TestStreamExecuteWithCorrection:
    async def test_happy_path_yields_row_batch_and_stores_result(self):
        adapter = _make_adapter_stream(rows=[[42]], cols=["n"])
        result_out = _StreamResult()
        settings = _make_settings()

        events = await _collect_events(
            _stream_execute_with_correction(
                generated_query="SELECT 42 AS n",
                explanation="ok",
                adapter=adapter,
                config_dict={},
                connected=True,
                cache_hit=False,
                provider=None,
                configured_model="m",
                configured_max_tokens=4096,
                system_prompt="sys",
                history=[],
                message="q",
                schema=MagicMock(),
                options={},
                settings=settings,
                result_out=result_out,
            )
        )

        types = [e["type"] for e in events]
        assert "row_batch" in types
        assert result_out.result is not None
        assert result_out.result.status == "executed"
        assert result_out.result.results_response.rows == [[42]]
        assert result_out.result.error is None

    async def test_connects_adapter_when_not_yet_connected(self):
        adapter = _make_adapter_stream()
        result_out = _StreamResult()

        await _collect_events(
            _stream_execute_with_correction(
                generated_query="SELECT 1",
                explanation="ok",
                adapter=adapter,
                config_dict={"host": "db"},
                connected=False,  # not yet connected
                cache_hit=False,
                provider=None,
                configured_model="m",
                configured_max_tokens=4096,
                system_prompt="sys",
                history=[],
                message="q",
                schema=MagicMock(),
                options={},
                settings=_make_settings(),
                result_out=result_out,
            )
        )

        adapter.connect.assert_awaited_once_with({"host": "db"})

    async def test_connection_failure_stores_error_result(self):
        adapter = MagicMock()
        fail_result = MagicMock(success=False, message="auth failed")
        adapter.connect = AsyncMock(return_value=fail_result)
        result_out = _StreamResult()

        events = await _collect_events(
            _stream_execute_with_correction(
                generated_query="SELECT 1",
                explanation="ok",
                adapter=adapter,
                config_dict={},
                connected=False,
                cache_hit=False,
                provider=None,
                configured_model="m",
                configured_max_tokens=4096,
                system_prompt="sys",
                history=[],
                message="q",
                schema=MagicMock(),
                options={},
                settings=_make_settings(),
                result_out=result_out,
            )
        )

        assert events == []  # no events yielded
        assert result_out.result is not None
        assert result_out.result.status == "error"
        assert "auth failed" in result_out.result.error

    async def test_self_correction_retry_on_execution_error(self):
        """When initial execution fails, correction is attempted and re-executed."""
        adapter = _make_adapter_stream(fail_on_first=True)
        result_out = _StreamResult()
        provider = MagicMock()

        with patch(
            f"{_EXEC_MODULE}._attempt_sql_execution_correction",
            new=AsyncMock(return_value=("SELECT 1 -- corrected", "fixed")),
        ) as mock_corr:
            events = await _collect_events(
                _stream_execute_with_correction(
                    generated_query="SELECT bad_col FROM t",
                    explanation="orig",
                    adapter=adapter,
                    config_dict={},
                    connected=True,
                    cache_hit=False,
                    provider=provider,
                    configured_model="m",
                    configured_max_tokens=4096,
                    system_prompt="sys",
                    history=[],
                    message="q",
                    schema=MagicMock(),
                    options={},
                    settings=_make_settings(),
                    result_out=result_out,
                )
            )

        mock_corr.assert_awaited()
        types = [e["type"] for e in events]
        # Should emit: status(Correcting), sql(corrected), status(Re-executing), row_batch
        assert "status" in types
        assert "sql" in types
        assert "row_batch" in types
        assert result_out.result is not None
        assert result_out.result.status == "executed"
        assert result_out.result.generated_query == "SELECT 1 -- corrected"

    async def test_stores_error_result_when_correction_gives_no_query(self):
        """If correction returns no query, the error is stored after max attempts."""
        adapter = _make_adapter_stream(fail_on_first=True)
        result_out = _StreamResult()
        provider = MagicMock()

        with patch(
            f"{_EXEC_MODULE}._attempt_sql_execution_correction",
            new=AsyncMock(return_value=(None, None)),  # no corrected query
        ):
            await _collect_events(
                _stream_execute_with_correction(
                    generated_query="SELECT bad",
                    explanation="orig",
                    adapter=adapter,
                    config_dict={},
                    connected=True,
                    cache_hit=False,
                    provider=provider,
                    configured_model="m",
                    configured_max_tokens=4096,
                    system_prompt="sys",
                    history=[],
                    message="q",
                    schema=MagicMock(),
                    options={},
                    settings=_make_settings(),
                    result_out=result_out,
                )
            )

        assert result_out.result is not None
        assert result_out.result.status == "error"

    async def test_no_correction_attempt_on_cache_hit(self):
        """Self-correction is skipped when query came from cache."""
        adapter = _make_adapter_stream(fail_on_first=True)
        result_out = _StreamResult()
        provider = MagicMock()

        with patch(
            f"{_EXEC_MODULE}._attempt_sql_execution_correction",
            new=AsyncMock(return_value=("corrected", "fixed")),
        ) as mock_corr:
            await _collect_events(
                _stream_execute_with_correction(
                    generated_query="SELECT bad",
                    explanation="orig",
                    adapter=adapter,
                    config_dict={},
                    connected=True,
                    cache_hit=True,  # cache hit — no correction
                    provider=provider,
                    configured_model="m",
                    configured_max_tokens=4096,
                    system_prompt="sys",
                    history=[],
                    message="q",
                    schema=MagicMock(),
                    options={},
                    settings=_make_settings(),
                    result_out=result_out,
                )
            )

        mock_corr.assert_not_called()
        assert result_out.result.status == "error"

    async def test_zero_result_correction_triggers_requery(self):
        """When 0 rows returned and intent != EXISTENCE, zero-result correction runs."""
        adapter = _make_adapter_stream(rows=[], cols=["n"])  # 0 rows
        result_out = _StreamResult()
        provider = MagicMock()

        with patch(
            f"{_EXEC_MODULE}._attempt_zero_result_correction",
            new=AsyncMock(return_value=("SELECT 1 -- zero corrected", "found it")),
        ) as mock_zero:
            await _collect_events(
                _stream_execute_with_correction(
                    generated_query="SELECT n FROM t WHERE false",
                    explanation="orig",
                    adapter=adapter,
                    config_dict={},
                    connected=True,
                    cache_hit=False,
                    provider=provider,
                    configured_model="m",
                    configured_max_tokens=4096,
                    system_prompt="sys",
                    history=[],
                    message="show me the data",
                    schema=MagicMock(),
                    options={},
                    settings=_make_settings(),
                    result_out=result_out,
                    intent=QueryIntent.LOOKUP,
                )
            )

        mock_zero.assert_awaited_once()

    async def test_zero_result_skipped_for_existence_intent(self):
        adapter = _make_adapter_stream(rows=[], cols=["n"])  # 0 rows
        result_out = _StreamResult()
        provider = MagicMock()

        with patch(
            f"{_EXEC_MODULE}._attempt_zero_result_correction",
            new=AsyncMock(return_value=("corrected", "found")),
        ) as mock_zero:
            await _collect_events(
                _stream_execute_with_correction(
                    generated_query="SELECT count(*) FROM t",
                    explanation="orig",
                    adapter=adapter,
                    config_dict={},
                    connected=True,
                    cache_hit=False,
                    provider=provider,
                    configured_model="m",
                    configured_max_tokens=4096,
                    system_prompt="sys",
                    history=[],
                    message="are there any users?",
                    schema=MagicMock(),
                    options={},
                    settings=_make_settings(),
                    result_out=result_out,
                    intent=QueryIntent.EXISTENCE,
                )
            )

        mock_zero.assert_not_called()

    async def test_result_stored_as_cached_when_cache_hit(self):
        adapter = _make_adapter_stream(rows=[[7]])
        result_out = _StreamResult()

        await _collect_events(
            _stream_execute_with_correction(
                generated_query="SELECT 7",
                explanation="cached",
                adapter=adapter,
                config_dict={},
                connected=True,
                cache_hit=True,
                provider=None,
                configured_model="m",
                configured_max_tokens=4096,
                system_prompt="sys",
                history=[],
                message="q",
                schema=MagicMock(),
                options={},
                settings=_make_settings(),
                result_out=result_out,
            )
        )

        assert result_out.result is not None
        assert result_out.result.status == "cached"
