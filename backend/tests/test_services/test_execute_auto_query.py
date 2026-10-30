# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""_execute_auto_query: row filter on every execution, clean SQL everywhere else, masking."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import get_settings
from app.datasources.errors import ErrorCategory
from app.datasources.models import DataSourceSchema, PrivacySettings, QueryResult, ValidationResult
from app.providers.base import LLMResponse
from app.services.execution import _execute_auto_query
from app.services.intent_classifier import QueryIntent
from app.services.row_filter import row_filter_fingerprint
from app.services.row_filter_rewrite import apply_row_filters

_FILTER = "tenant_id = 7"
# Every table the statements below reference, so each executed statement is rewritten.
_FILTERS = {f"public.{t}": _FILTER for t in ("t", "orders", "ordes", "ordrs", "users")}


def _filtered(sql: str) -> str:
    rewritten = apply_row_filters(sql, _FILTERS, "postgresql")
    assert "WHERE (tenant_id = 7)" in rewritten
    return rewritten


def _result(columns: list[str], rows: list[list]) -> QueryResult:
    return QueryResult(
        columns=columns,
        column_types=["text"] * len(columns),
        rows=rows,
        row_count=len(rows),
        truncated=False,
        execution_time_ms=1.0,
    )


def _settings() -> MagicMock:
    s = MagicMock()
    s.default_row_limit = 100
    s.default_query_timeout = 30
    return s


def _provider(*queries: str) -> MagicMock:
    """A provider whose successive generate_response calls return *queries* in order."""
    provider = MagicMock()
    provider.max_output_tokens = 4096
    provider.generate_response = AsyncMock(
        side_effect=[
            LLMResponse(
                query=q,
                explanation="fixed",
                raw_response=f"QUERY:\n```sql\n{q}\n```\nEXPLANATION: fixed",
                model="test-model",
            )
            for q in queries
        ]
    )
    return provider


def _adapter(*execute_side_effect) -> MagicMock:
    adapter = MagicMock()
    adapter.query_dialect = "postgresql"
    adapter.execute_query = AsyncMock(side_effect=list(execute_side_effect))
    adapter.validate_query = MagicMock(
        return_value=ValidationResult(is_valid=True, error_message=None)
    )
    return adapter


def _executed(adapter: MagicMock) -> list[str]:
    return [call.args[0] for call in adapter.execute_query.await_args_list]


def _llm_text(provider: MagicMock) -> str:
    """Everything every LLM call was handed, as one string."""
    return "\n".join(
        str(value)
        for call in provider.generate_response.call_args_list
        for value in (*call.args, *call.kwargs.values())
    )


async def _run(adapter, *, generated_query: str, privacy=None, provider=None):
    return await _execute_auto_query(
        generated_query=generated_query,
        explanation="e",
        adapter=adapter,
        config_dict={},
        connected=True,
        cache_hit=False,
        provider=provider,
        configured_model="m",
        configured_max_tokens=1024,
        system_prompt="",
        history=[],
        message="q",
        schema=DataSourceSchema(source_type="postgresql"),
        options={},
        settings=_settings(),
        privacy=privacy,
        intent=QueryIntent.UNKNOWN,
    )


class TestRowFilterAndCleanSql:
    async def test_executed_sql_is_rewritten_but_result_carries_clean_sql(self) -> None:
        adapter = MagicMock()
        adapter.query_dialect = "postgresql"
        adapter.execute_query = AsyncMock(return_value=_result(["id"], [[1]]))
        privacy = PrivacySettings(row_filters=_FILTERS)

        exe = await _run(adapter, generated_query="SELECT id FROM t", privacy=privacy)

        executed = adapter.execute_query.await_args_list[0].args[0]
        assert executed == _filtered("SELECT id FROM t")
        assert exe.generated_query == "SELECT id FROM t"

    async def test_corrected_query_is_rewritten_again(self) -> None:
        adapter = MagicMock()
        adapter.query_dialect = "postgresql"
        adapter.execute_query = AsyncMock(
            side_effect=[RuntimeError("boom"), _result(["id"], [[1]])]
        )
        adapter.validate_query = MagicMock(
            return_value=ValidationResult(is_valid=True, error_message=None)
        )
        adapter.classify_error = MagicMock(return_value=ErrorCategory.UNKNOWN_NAME)
        privacy = PrivacySettings(row_filters=_FILTERS)

        exe = await _run(
            adapter,
            generated_query="SELECT id FROM ordrs",
            privacy=privacy,
            provider=_provider("SELECT id FROM orders"),
        )

        second = adapter.execute_query.await_args_list[1].args[0]
        assert second == _filtered("SELECT id FROM orders")
        assert exe.generated_query == "SELECT id FROM orders"
        assert exe.status == "executed"

    async def test_every_correction_attempt_is_rewritten_and_prompts_stay_clean(self) -> None:
        adapter = _adapter(RuntimeError("boom"), RuntimeError("boom"), _result(["id"], [[1]]))
        adapter.classify_error = MagicMock(return_value=ErrorCategory.UNKNOWN_NAME)
        provider = _provider("SELECT id FROM ordes", "SELECT id FROM orders")

        exe = await _run(
            adapter,
            generated_query="SELECT id FROM ordrs",
            privacy=PrivacySettings(row_filters=_FILTERS),
            provider=provider,
        )

        assert _executed(adapter) == [
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
        assert exe.generated_query == "SELECT id FROM orders"
        assert exe.status == "executed"

    async def test_failed_turn_carries_clean_sql(self) -> None:
        adapter = _adapter(RuntimeError("a"), RuntimeError("b"), RuntimeError("c"))
        provider = _provider("SELECT id FROM ordes", "SELECT id FROM orders")

        exe = await _run(
            adapter,
            generated_query="SELECT id FROM ordrs",
            privacy=PrivacySettings(row_filters=_FILTERS),
            provider=provider,
        )

        assert all("WHERE (tenant_id = 7)" in sql for sql in _executed(adapter))
        assert len(_executed(adapter)) == 3
        assert exe.status == "error"
        assert exe.generated_query == "SELECT id FROM orders"

    async def test_zero_result_reexecution_is_rewritten_and_prompt_stays_clean(self) -> None:
        adapter = _adapter(_result(["id"], []), _result(["id"], [[1]]))
        provider = _provider("SELECT id FROM t WHERE id > 0")

        exe = await _run(
            adapter,
            generated_query="SELECT id FROM t WHERE id < 0",
            privacy=PrivacySettings(row_filters=_FILTERS),
            provider=provider,
        )

        assert _executed(adapter) == [
            _filtered("SELECT id FROM t WHERE id < 0"),
            _filtered("SELECT id FROM t WHERE id > 0"),
        ]
        prompt = provider.generate_response.call_args.kwargs["user_message"]
        assert "SELECT id FROM t WHERE id < 0" in prompt
        assert "WHERE (tenant_id" not in _llm_text(provider)
        # The zero-result prompt carries only the clean query. The schema shown to the LLM
        # may still list tenant_id as a column name, which is acceptable: not a value.
        assert "tenant_id" not in _llm_text(provider)
        assert exe.generated_query == "SELECT id FROM t WHERE id > 0"
        assert exe.results_response.rows == [[1]]


class TestMaskingAndErrorText:
    async def test_aliased_sensitive_column_is_masked(self) -> None:
        adapter = MagicMock()
        adapter.query_dialect = "postgresql"
        adapter.execute_query = AsyncMock(return_value=_result(["contact"], [["a@x.com"]]))

        exe = await _run(
            adapter, generated_query="SELECT email AS contact FROM users", privacy=PrivacySettings()
        )

        assert exe.results_response.rows == [["[REDACTED]"]]

    async def test_masked_and_rewritten_together(self) -> None:
        """The derived table's inner ``SELECT *`` neither hides the alias nor defeats the
        aggregate exemption: classification reads the clean SQL."""
        sql = "SELECT email AS contact, COUNT(email) AS n FROM users GROUP BY email"
        adapter = _adapter(_result(["contact", "n"], [["a@x.com", 3]]))

        exe = await _run(
            adapter, generated_query=sql, privacy=PrivacySettings(row_filters=_FILTERS)
        )

        assert _executed(adapter) == [_filtered(sql)]
        assert exe.results_response.rows == [["[REDACTED]", 3]]

    async def test_corrected_query_result_is_masked_by_its_own_sql(self) -> None:
        """The mask follows the SQL that produced the rows: a classification of the failed
        query would read ``contact`` as an aliased email and redact the count."""
        adapter = _adapter(RuntimeError("boom"), _result(["contact"], [[5]]))
        provider = _provider("SELECT COUNT(email) AS contact FROM users")

        exe = await _run(
            adapter,
            generated_query="SELECT email AS contact FROM users",
            privacy=PrivacySettings(),
            provider=provider,
        )

        assert exe.results_response.rows == [[5]]

    async def test_zero_result_reexecution_is_masked(self) -> None:
        adapter = _adapter(_result(["id"], []), _result(["contact"], [["a@x.com"]]))
        provider = _provider("SELECT email AS contact FROM users")

        exe = await _run(
            adapter,
            generated_query="SELECT id FROM users WHERE id < 0",
            privacy=PrivacySettings(),
            provider=provider,
        )

        assert exe.generated_query == "SELECT email AS contact FROM users"
        assert exe.results_response.rows == [["[REDACTED]"]]

    async def test_database_text_never_reaches_the_correction_prompt(self) -> None:
        adapter = MagicMock()
        adapter.query_dialect = "postgresql"
        adapter.execute_query = AsyncMock(
            side_effect=[
                RuntimeError('invalid input syntax for type integer: "Jane Doe"'),
                _result(["id"], [[1]]),
            ]
        )
        adapter.validate_query = MagicMock(
            return_value=ValidationResult(is_valid=True, error_message=None)
        )
        adapter.classify_error = MagicMock(return_value=ErrorCategory.DATA)
        provider = _provider("SELECT id FROM customers")

        exe = await _run(
            adapter,
            generated_query="SELECT CAST(name AS int) FROM customers",
            provider=provider,
        )

        for call in provider.generate_response.call_args_list:
            for value in call.kwargs.values():
                assert "Jane" not in str(value)
        assert exe.results_response.rows == [[1]]

    async def test_database_text_never_reaches_an_llm_call_with_a_filter_set(self) -> None:
        adapter = _adapter(
            RuntimeError('invalid input syntax for type integer: "Jane Doe"'),
            _result(["id"], []),
            _result(["id"], [[1]]),
        )
        adapter.classify_error = MagicMock(return_value=ErrorCategory.DATA)
        provider = _provider("SELECT id FROM customers", "SELECT id FROM customers2")

        exe = await _run(
            adapter,
            generated_query="SELECT CAST(name AS int) FROM customers",
            privacy=PrivacySettings(row_filters=_FILTERS),
            provider=provider,
        )

        # Both the execution correction and the zero-result refinement ran.
        assert provider.generate_response.await_count == 2
        assert "Jane" not in _llm_text(provider)
        assert "WHERE (tenant_id" not in _llm_text(provider)
        assert exe.status == "executed"
        assert exe.results_response.rows == [[1]]

    async def test_empty_exception_text_still_reads_as_an_error(self) -> None:
        adapter = MagicMock()
        adapter.query_dialect = "postgresql"
        adapter.execute_query = AsyncMock(side_effect=TimeoutError())

        exe = await _run(adapter, generated_query="SELECT 1")

        assert exe.status == "error"
        assert exe.error == "Query timed out"


class TestUserFacingErrorText:
    """The error the user sees withholds quoted values for value-quoting categories."""

    async def test_data_error_value_is_redacted_without_correction(self) -> None:
        adapter = _adapter(RuntimeError('invalid input syntax for type integer: "Jane Doe"'))
        adapter.classify_error = MagicMock(return_value=ErrorCategory.DATA)

        exe = await _run(adapter, generated_query="SELECT CAST(name AS int) FROM customers")

        assert exe.status == "error"
        assert "Jane" not in exe.error
        assert "invalid input syntax for type integer" in exe.error

    async def test_note_is_appended_to_the_redacted_text(self) -> None:
        adapter = _adapter(RuntimeError('invalid input syntax for type integer: "Jane Doe"'))
        adapter.classify_error = MagicMock(return_value=ErrorCategory.DATA)

        with patch(
            "app.services.execution._attempt_sql_execution_correction",
            new=AsyncMock(return_value=(None, "gave up")),
        ):
            exe = await _run(
                adapter,
                generated_query="SELECT CAST(name AS int) FROM customers",
                provider=_provider(),
            )

        assert "Jane" not in exe.error
        assert exe.error.endswith("\n\nNote: gave up")

    async def test_name_error_text_is_kept(self) -> None:
        adapter = _adapter(RuntimeError('column "amont" does not exist'))
        adapter.classify_error = MagicMock(return_value=ErrorCategory.UNKNOWN_NAME)

        exe = await _run(adapter, generated_query="SELECT amont FROM orders")

        assert exe.error == 'column "amont" does not exist'


class TestFingerprint:
    async def test_result_carries_the_fingerprint_of_the_filters_it_ran_under(self) -> None:
        adapter = _adapter(_result(["id"], [[1]]))
        privacy = PrivacySettings(row_filters=_FILTERS)
        exe = await _run(adapter, generated_query="SELECT id FROM t", privacy=privacy)
        expected = row_filter_fingerprint(_FILTERS, get_settings().encryption_key)
        assert exe.results_response.row_filter_fingerprint == expected
        assert exe.results_response.stale_row_filter is False

    async def test_no_filters_gives_the_empty_fingerprint(self) -> None:
        adapter = _adapter(_result(["id"], [[1]]))
        exe = await _run(adapter, generated_query="SELECT id FROM t", privacy=PrivacySettings())
        assert exe.results_response.row_filter_fingerprint == ""

    async def test_no_privacy_gives_the_empty_fingerprint(self) -> None:
        adapter = _adapter(_result(["id"], [[1]]))
        exe = await _run(adapter, generated_query="SELECT id FROM t", privacy=None)
        assert exe.results_response.row_filter_fingerprint == ""


class TestZeroResultLog:
    async def test_a_failed_refinement_is_logged_redacted(self, caplog) -> None:
        adapter = _adapter(_result(["id"], []), RuntimeError('value "secret-value" out of range'))
        adapter.classify_error = MagicMock(return_value=ErrorCategory.DATA)
        provider = _provider("SELECT id FROM t WHERE id > 0")
        with caplog.at_level(logging.DEBUG, logger="app.services.execution"):
            exe = await _run(
                adapter, generated_query="SELECT id FROM t WHERE id < 0", provider=provider
            )
        assert exe.results_response.rows == []
        assert exe.generated_query == "SELECT id FROM t WHERE id < 0"
        assert "secret-value" not in caplog.text
        assert "Zero-result corrected query failed at execution (data): " in caplog.text
