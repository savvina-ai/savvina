# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for _attempt_zero_result_correction() in chat_service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.datasources.models import DataSourceSchema, ValidationResult
from app.providers.base import LLMResponse
from app.services.correction import _attempt_zero_result_correction


def _make_provider(
    query: str = "SELECT 1",
    explanation: str = "corrected",
    raises: Exception | None = None,
) -> MagicMock:
    p = MagicMock()
    p.max_output_tokens = 4096
    if raises:
        p.generate_response = AsyncMock(side_effect=raises)
    else:
        p.generate_response = AsyncMock(
            return_value=LLMResponse(
                query=query,
                explanation=explanation,
                raw_response=f"QUERY:\n```sql\n{query}\n```\nEXPLANATION:\n{explanation}",
                model="test-model",
            )
        )
    return p


def _make_adapter(valid: bool = True) -> MagicMock:
    a = MagicMock()
    a.query_dialect = "postgresql"
    a.validate_query = MagicMock(
        return_value=ValidationResult(
            is_valid=valid,
            error_message=None if valid else "blocked keyword",
        )
    )
    return a


class TestAttemptZeroResultCorrection:
    async def test_returns_corrected_query_on_success(self):
        provider = _make_provider(query="SELECT * FROM orders WHERE status = 'active'")
        adapter = _make_adapter(valid=True)

        result_query, result_expl = await _attempt_zero_result_correction(
            original_question="show active orders",
            query="SELECT * FROM orders WHERE status = 'Active'",
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="test",
            schema=DataSourceSchema(source_type="postgresql"),
            adapter=adapter,
        )

        assert result_query == "SELECT * FROM orders WHERE status = 'active'"
        assert result_expl == "corrected"

    async def test_returns_none_and_explanation_when_llm_returns_no_query(self):
        """LLM returns no QUERY block → 0 rows is correct; surface the explanation."""
        provider = _make_provider(
            query="",
            explanation="No orders matched — the filter conditions are correct.",
        )
        adapter = _make_adapter()

        result_query, result_expl = await _attempt_zero_result_correction(
            original_question="show filtered orders",
            query="SELECT 1",
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="",
            schema=None,
            adapter=adapter,
        )

        assert result_query is None
        assert "correct" in result_expl

    async def test_returns_none_empty_on_llm_exception(self):
        provider = _make_provider(raises=Exception("provider timeout"))
        adapter = _make_adapter()

        result_query, result_expl = await _attempt_zero_result_correction(
            original_question="q",
            query="SELECT 1",
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="",
            schema=None,
            adapter=adapter,
        )

        assert result_query is None
        assert result_expl == ""

    async def test_returns_none_when_corrected_fails_read_only_check(self):
        provider = _make_provider(query="DELETE FROM orders")
        adapter = _make_adapter(valid=False)

        result_query, _ = await _attempt_zero_result_correction(
            original_question="q",
            query="SELECT 1",
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="",
            schema=None,
            adapter=adapter,
        )

        assert result_query is None

    async def test_correction_prompt_mentions_zero_rows_and_diagnostics(self):
        """The message sent to the LLM must mention 0 rows and list diagnostic causes."""
        provider = _make_provider(query="")
        adapter = _make_adapter()

        await _attempt_zero_result_correction(
            original_question="find active accounts",
            query="SELECT id FROM accounts WHERE status = 'Active'",
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="",
            schema=None,
            adapter=adapter,
        )

        call_kwargs = provider.generate_response.call_args.kwargs
        msg = call_kwargs["user_message"]
        assert "0 rows" in msg
        assert "filter value" in msg.lower() or "filter" in msg.lower()
        assert "find active accounts" in msg

    async def test_uses_temperature_zero_for_determinism(self):
        provider = _make_provider(query="")
        adapter = _make_adapter()

        await _attempt_zero_result_correction(
            original_question="q",
            query="SELECT 1",
            system_prompt="",
            history=[],
            provider=provider,
            configured_max_tokens=4096,
            configured_model="",
            schema=None,
            adapter=adapter,
        )

        call_kwargs = provider.generate_response.call_args.kwargs
        assert call_kwargs["temperature"] == 0.0

    async def test_passes_history_to_provider(self):
        provider = _make_provider(query="")
        adapter = _make_adapter()
        history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]

        await _attempt_zero_result_correction(
            original_question="q",
            query="SELECT 1",
            system_prompt="",
            history=history,
            provider=provider,
            configured_max_tokens=4096,
            configured_model="",
            schema=None,
            adapter=adapter,
        )

        call_kwargs = provider.generate_response.call_args.kwargs
        assert call_kwargs["conversation_history"] == history
