# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Unit tests for _attempt_sql_correction() in chat_service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.datasources.models import (
    ColumnInfo,
    DataSourceSchema,
    SchemaInfo,
    TableInfo,
    ValidationResult,
)
from app.providers.base import LLMResponse
from app.services.correction import _attempt_sql_correction


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


class TestAttemptSqlCorrection:
    async def test_returns_corrected_query_on_success(self) -> None:
        """Happy path: LLM returns a valid corrected query."""
        provider = _make_provider(_make_llm_response("SELECT id FROM customers", "Fixed"))
        adapter = _make_adapter(valid=True)
        schema = DataSourceSchema(source_type="postgresql")  # empty → column check skipped

        result_query, result_explanation = await _attempt_sql_correction(
            original_question="How many customers?",
            failed_query="SELECT nonexistent FROM customers",
            validation_error='column "nonexistent" does not exist in table "customers"',
            system_prompt="You are an assistant.",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="test-model",
            schema=schema,
            adapter=adapter,
        )

        assert result_query == "SELECT id FROM customers"
        assert result_explanation == "Fixed"

    async def test_returns_none_when_llm_raises(self) -> None:
        """Any exception from generate_response → (None, "")."""
        provider = _make_provider(raises=Exception("Network error"))
        adapter = _make_adapter(valid=True)
        schema = DataSourceSchema(source_type="postgresql")

        result_query, result_explanation = await _attempt_sql_correction(
            original_question="q",
            failed_query="SELECT x FROM t",
            validation_error="col x not found",
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="",
            schema=schema,
            adapter=adapter,
        )

        assert result_query is None
        assert result_explanation == ""

    async def test_returns_none_when_corrected_query_is_empty(self) -> None:
        """LLM returns no extractable query → (None, "")."""
        provider = _make_provider(
            LLMResponse(query="", explanation="", raw_response="Sorry.", model="m")
        )
        adapter = _make_adapter(valid=True)
        schema = DataSourceSchema(source_type="postgresql")

        result_query, _ = await _attempt_sql_correction(
            original_question="q",
            failed_query="SELECT x FROM t",
            validation_error="err",
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="",
            schema=schema,
            adapter=adapter,
        )

        assert result_query is None

    async def test_returns_none_when_read_only_check_fails(self) -> None:
        """Corrected query fails the read-only validator → (None, "")."""
        provider = _make_provider(
            LLMResponse(query="DELETE FROM customers", explanation="", raw_response="", model="m")
        )
        adapter = _make_adapter(valid=False)
        schema = DataSourceSchema(source_type="postgresql")

        result_query, _ = await _attempt_sql_correction(
            original_question="q",
            failed_query="SELECT x FROM t",
            validation_error="err",
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="",
            schema=schema,
            adapter=adapter,
        )

        assert result_query is None

    async def test_returns_none_when_schema_error_persists(self) -> None:
        """Corrected query still references a non-existent column → (None, "")."""
        provider = _make_provider(_make_llm_response("SELECT c.still_wrong FROM customers c", ""))
        adapter = _make_adapter(valid=True)
        schema = DataSourceSchema(
            source_type="postgresql",
            schemas=[SchemaInfo(name="public")],
            tables=[
                TableInfo(
                    catalog=None,
                    schema_name="public",
                    name="customers",
                    table_type="table",
                    columns=[ColumnInfo(name="id", data_type="int", native_type="int4")],
                )
            ],
        )

        result_query, _ = await _attempt_sql_correction(
            original_question="q",
            failed_query="SELECT c.nonexistent FROM customers c",
            validation_error="err",
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="",
            schema=schema,
            adapter=adapter,
        )

        assert result_query is None

    async def test_correction_prompt_includes_original_question(self) -> None:
        """The user_message sent to the LLM must contain the original question."""
        provider = _make_provider(LLMResponse(query="", explanation="", raw_response="", model="m"))
        adapter = _make_adapter(valid=True)
        schema = DataSourceSchema(source_type="postgresql")

        await _attempt_sql_correction(
            original_question="What are the top products?",
            failed_query="SELECT x FROM t",
            validation_error="err",
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="",
            schema=schema,
            adapter=adapter,
        )

        call_kwargs = provider.generate_response.call_args.kwargs
        assert "What are the top products?" in call_kwargs["user_message"]

    async def test_retries_on_second_schema_error(self) -> None:
        """If attempt 1 still has schema errors, attempt 2 is called with the updated
        query/error."""
        schema = DataSourceSchema(
            source_type="postgresql",
            schemas=[SchemaInfo(name="public")],
            tables=[
                TableInfo(
                    catalog=None,
                    schema_name="public",
                    name="orders",
                    table_type="table",
                    columns=[ColumnInfo(name="id", data_type="int", native_type="int4")],
                )
            ],
        )
        # Attempt 1 returns a query that still references a bad column; attempt 2 fixes it.
        # Use table-qualified references so the schema validator can detect the error.
        attempt1_response = _make_llm_response("SELECT o.still_bad FROM orders o", "try 1")
        attempt2_response = _make_llm_response("SELECT o.id FROM orders o", "try 2")
        provider = MagicMock()
        provider.max_output_tokens = 4096
        provider.generate_response = AsyncMock(side_effect=[attempt1_response, attempt2_response])
        adapter = _make_adapter(valid=True)

        result_query, result_explanation = await _attempt_sql_correction(
            original_question="List order ids",
            failed_query="SELECT o.nonexistent FROM orders o",
            validation_error='column "nonexistent" not found in table "orders"',
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="",
            schema=schema,
            adapter=adapter,
            max_attempts=2,
        )

        assert result_query == "SELECT o.id FROM orders o"
        assert result_explanation == "try 2"
        assert provider.generate_response.call_count == 2
        # Second call should reference the updated failed query from attempt 1
        second_call_msg = provider.generate_response.call_args_list[1].kwargs["user_message"]
        assert "still_bad" in second_call_msg

    async def test_gives_up_after_max_attempts(self) -> None:
        """If all attempts return queries with schema errors, returns (None, "")."""
        schema = DataSourceSchema(
            source_type="postgresql",
            schemas=[SchemaInfo(name="public")],
            tables=[
                TableInfo(
                    catalog=None,
                    schema_name="public",
                    name="users",
                    table_type="table",
                    columns=[ColumnInfo(name="id", data_type="int", native_type="int4")],
                )
            ],
        )
        # Both attempts return a query that still has a bad column (table-qualified so validator
        # catches it).
        bad_response = _make_llm_response("SELECT u.always_wrong FROM users u", "nope")
        provider = MagicMock()
        provider.max_output_tokens = 4096
        provider.generate_response = AsyncMock(return_value=bad_response)
        adapter = _make_adapter(valid=True)

        result_query, _ = await _attempt_sql_correction(
            original_question="q",
            failed_query="SELECT u.bad FROM users u",
            validation_error='column "bad" not found in table "users"',
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="",
            schema=schema,
            adapter=adapter,
            max_attempts=2,
        )

        assert result_query is None
        assert provider.generate_response.call_count == 2

    async def test_attempt_counter_in_message(self) -> None:
        """Correction messages include an attempt counter so the LLM knows how many tries remain."""
        provider = _make_provider(LLMResponse(query="", explanation="", raw_response="", model="m"))
        adapter = _make_adapter(valid=True)
        schema = DataSourceSchema(source_type="postgresql")

        await _attempt_sql_correction(
            original_question="q",
            failed_query="SELECT x FROM t",
            validation_error="err",
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="",
            schema=schema,
            adapter=adapter,
            max_attempts=2,
        )

        first_call_msg = provider.generate_response.call_args_list[0].kwargs["user_message"]
        assert "attempt 1 of 2" in first_call_msg
