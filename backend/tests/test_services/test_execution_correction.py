# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Unit tests for _attempt_sql_execution_correction() in chat_service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.datasources.errors import ErrorCategory
from app.datasources.models import DataSourceSchema, ValidationResult
from app.providers.base import LLMResponse
from app.services.correction import (
    _CATEGORY_FALLBACK_HINTS as _FALLBACKS,
)
from app.services.correction import (
    _attempt_sql_execution_correction,
)


def _make_llm_response(query: str = "SELECT 1", explanation: str = "ok") -> LLMResponse:
    return LLMResponse(
        query=query,
        explanation=explanation,
        raw_response=f"QUERY:\n```sql\n{query}\n```\nEXPLANATION: {explanation}",
        model="test-model",
    )


def _make_adapter(valid: bool = True) -> MagicMock:
    adapter = MagicMock()
    adapter.query_dialect = "postgresql"
    adapter.validate_query = MagicMock(
        return_value=ValidationResult(is_valid=valid, error_message=None if valid else "read-only")
    )
    return adapter


def _make_provider(
    llm_response: LLMResponse | None = None,
    raises: Exception | None = None,
) -> MagicMock:
    provider = MagicMock()
    provider.max_output_tokens = 4096
    if raises:
        provider.generate_response = AsyncMock(side_effect=raises)
    else:
        provider.generate_response = AsyncMock(return_value=llm_response or _make_llm_response())
    return provider


class TestAttemptSqlExecutionCorrection:
    async def test_returns_corrected_query_on_success(self) -> None:
        """Happy path: LLM returns a valid corrected query."""
        provider = _make_provider(_make_llm_response("SELECT id FROM orders", "Fixed"))
        adapter = _make_adapter(valid=True)
        schema = DataSourceSchema(source_type="postgresql")

        result_query, result_explanation = await _attempt_sql_execution_correction(
            original_question="Show order ids",
            failed_query="SELECT order_id FROM orders",
            exec_error='column "order_id" does not exist',
            system_prompt="You are an assistant.",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="test-model",
            schema=schema,
            adapter=adapter,
        )

        assert result_query == "SELECT id FROM orders"
        assert result_explanation == "Fixed"

    async def test_returns_none_when_llm_raises(self) -> None:
        """Any exception from generate_response → (None, descriptive message)."""
        provider = _make_provider(raises=Exception("Timeout"))
        adapter = _make_adapter(valid=True)
        schema = DataSourceSchema(source_type="postgresql")

        result_query, result_explanation = await _attempt_sql_execution_correction(
            original_question="q",
            failed_query="SELECT x FROM t",
            exec_error="some db error",
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="",
            schema=schema,
            adapter=adapter,
        )

        assert result_query is None
        assert result_explanation == "auto-correction failed: Timeout"

    async def test_returns_none_when_read_only_check_fails(self) -> None:
        """Corrected query that fails the read-only validator → (None, "")."""
        provider = _make_provider(
            LLMResponse(query="DELETE FROM orders", explanation="", raw_response="", model="m")
        )
        adapter = _make_adapter(valid=False)
        schema = DataSourceSchema(source_type="postgresql")

        result_query, _ = await _attempt_sql_execution_correction(
            original_question="q",
            failed_query="SELECT x FROM t",
            exec_error="err",
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="",
            schema=schema,
            adapter=adapter,
        )

        assert result_query is None

    async def test_attempt_num_appears_in_message(self) -> None:
        """The attempt_num is embedded in the correction message sent to the LLM."""
        provider = _make_provider(LLMResponse(query="", explanation="", raw_response="", model="m"))
        adapter = _make_adapter(valid=True)
        schema = DataSourceSchema(source_type="postgresql")

        await _attempt_sql_execution_correction(
            original_question="q",
            failed_query="SELECT x FROM t",
            exec_error="division by zero",
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="",
            schema=schema,
            adapter=adapter,
            attempt_num=2,
        )

        call_msg = provider.generate_response.call_args.kwargs["user_message"]
        assert "correction attempt 2" in call_msg

    async def test_hint_injected_for_known_error_pattern(self) -> None:
        """A matching _EXEC_ERROR_HINTS pattern is included in the correction message."""
        provider = _make_provider(LLMResponse(query="", explanation="", raw_response="", model="m"))
        adapter = _make_adapter(valid=True)
        schema = DataSourceSchema(source_type="postgresql")

        await _attempt_sql_execution_correction(
            original_question="q",
            failed_query="SELECT revenue / spent FROM ads",
            exec_error="division by zero",
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="",
            schema=schema,
            adapter=adapter,
        )

        call_msg = provider.generate_response.call_args.kwargs["user_message"]
        assert "NULLIF" in call_msg

    async def test_returns_none_when_corrected_query_is_empty(self) -> None:
        """LLM returns no extractable query → (None, "")."""
        provider = _make_provider(
            LLMResponse(query="", explanation="sorry", raw_response="Sorry.", model="m")
        )
        adapter = _make_adapter(valid=True)
        schema = DataSourceSchema(source_type="postgresql")

        result_query, _ = await _attempt_sql_execution_correction(
            original_question="q",
            failed_query="SELECT x FROM t",
            exec_error="err",
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="",
            schema=schema,
            adapter=adapter,
        )

        assert result_query is None


class TestCorrectionPromptComposition:
    async def _prompt(self, exec_error: str, category: ErrorCategory) -> str:
        provider = _make_provider(_make_llm_response("SELECT id FROM orders", "Fixed"))
        await _attempt_sql_execution_correction(
            original_question="Show order ids",
            failed_query="SELECT CAST(customer_name AS int) FROM orders",
            exec_error=exec_error,
            system_prompt="You are an assistant.",
            history=[],
            provider=provider,
            configured_model="test-model",
            configured_max_tokens=4096,
            schema=DataSourceSchema(source_type="postgresql"),
            adapter=_make_adapter(valid=True),
            error_category=category,
        )
        return provider.generate_response.call_args.kwargs["user_message"]

    async def test_database_text_never_reaches_the_prompt(self) -> None:
        prompt = await self._prompt(
            'invalid input syntax for type integer: "Jane Doe"', ErrorCategory.DATA
        )
        assert "Jane Doe" not in prompt
        assert "could not be converted, parsed or fitted" in prompt
        assert "FAILED QUERY" in prompt

    async def test_text_hint_is_refused_when_it_disagrees_with_the_category(self) -> None:
        # The engine text matches the UNKNOWN_NAME hint, but the code said DATA: the
        # category wins and the DATA fallback hint is used instead.
        prompt = await self._prompt('column does not exist: "Jane Doe"', ErrorCategory.DATA)
        assert "Check for typos" not in prompt
        assert "Guard the conversion" in prompt

    async def test_text_hint_is_accepted_when_the_category_is_other(self) -> None:
        prompt = await self._prompt('column "order_id" does not exist', ErrorCategory.OTHER)
        assert "Check for typos" in prompt

    async def test_category_fallback_hint_when_text_matches_nothing(self) -> None:
        prompt = await self._prompt("Unbekannte Spalte", ErrorCategory.UNKNOWN_NAME)
        assert "Check for typos" in prompt

    async def test_default_category_keeps_old_callers_working(self) -> None:
        provider = _make_provider(_make_llm_response("SELECT 1", "ok"))
        result, _ = await _attempt_sql_execution_correction(
            original_question="q",
            failed_query="SELECT 2",
            exec_error="boom",
            system_prompt="s",
            history=[],
            provider=provider,
            configured_model="m",
            configured_max_tokens=100,
            schema=None,
            adapter=_make_adapter(valid=True),
        )
        assert result == "SELECT 1"

    async def test_syntax_fallback_hint_carries_the_common_causes(self) -> None:
        # MySQL's message has no "syntax error at or near": the category fallback applies,
        # and must stand in for the `near '...'` location the model no longer sees.
        prompt = await self._prompt(
            "You have an error in your SQL syntax; check the manual that corresponds to your "
            "MySQL server version for the right syntax to use near 'rank FROM t' at line 1",
            ErrorCategory.SYNTAX,
        )
        assert "rank FROM t" not in prompt
        assert "derived table" in prompt
        assert "reserved word" in prompt

    async def test_cardinality_fallback_hint(self) -> None:
        prompt = await self._prompt("Subquery returns more than 1 row", ErrorCategory.CARDINALITY)
        assert "more than one row" in prompt
        assert "EXISTS" in prompt

    def test_every_category_except_other_has_a_fallback_hint(self) -> None:
        missing = [c for c in ErrorCategory if c is not ErrorCategory.OTHER and c not in _FALLBACKS]
        assert missing == []


class TestRowFilterHint:
    async def test_hint_is_appended_to_the_error_description(self) -> None:
        provider = _make_provider(_make_llm_response("SELECT id FROM public.orders", "Fixed"))
        adapter = _make_adapter(valid=True)

        await _attempt_sql_execution_correction(
            original_question="q",
            failed_query="SELECT id FROM orders",
            exec_error="Table orders is referenced without a schema and a row filter exists"
            " for it in more than one schema.",
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="test-model",
            schema=None,
            adapter=adapter,
            error_category=ErrorCategory.SYNTAX,
            llm_hint="Table orders is referenced without a schema and a row filter exists"
            " for it in more than one schema. Qualify it as schema.table.",
        )

        prompt = provider.generate_response.call_args.kwargs["user_message"]
        assert "Qualify it as schema.table." in prompt
        assert "The query has a syntax error for this SQL dialect." in prompt

    async def test_no_hint_adds_nothing(self) -> None:
        provider = _make_provider()
        adapter = _make_adapter(valid=True)

        await _attempt_sql_execution_correction(
            original_question="q",
            failed_query="SELECT x FROM t",
            exec_error="some db error",
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="test-model",
            schema=None,
            adapter=adapter,
            error_category=ErrorCategory.SYNTAX,
        )

        prompt = provider.generate_response.call_args.kwargs["user_message"]
        assert "Qualify" not in prompt
        assert "some db error" not in prompt
