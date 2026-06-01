# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Unit tests for _attempt_sql_execution_correction() in chat_service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.datasources.models import DataSourceSchema, ValidationResult
from app.providers.base import LLMResponse
from app.services.correction import _attempt_sql_execution_correction


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
