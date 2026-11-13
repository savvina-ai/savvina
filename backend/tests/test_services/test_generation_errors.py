# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Error messages produced by _generate_query when the LLM reply is unusable.

- Degenerate output (one token repeated until max_tokens, e.g. ``!!!!…`` from
  gpt-oss-120b on Cloudflare Workers AI) is a transient provider failure and is
  reported as such, not as an output-token-limit hit.
- A genuine truncation reports the output budget that was hit, not the
  input+output total.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.providers.base import parse_llm_response
from app.services.validation import _validate_and_correct_query

from .test_tpm_warning import _call_generate, _make_db, _make_provider


def _degenerate_response():
    return parse_llm_response(
        "!" * 4096,
        "@cf/openai/gpt-oss-120b",
        truncated=True,
        input_tokens=15809,
        output_tokens=4096,
    )


def _truncated_response():
    return parse_llm_response(
        "QUERY:\n```sql\nSELECT o.id, o.total, c.name\nFROM orders o JOIN customers c ON",
        "@cf/openai/gpt-oss-120b",
        truncated=True,
        input_tokens=15809,
        output_tokens=4096,
    )


class TestDegenerateOutput:
    async def test_reports_transient_failure_not_token_limit(self):
        provider = _make_provider(side_effects=[_degenerate_response()])

        result = await _call_generate(provider, _make_db())

        assert result.status == "error"
        assert result.error is not None
        assert "try again" in result.error.lower()
        assert "output token limit" not in result.error.lower()
        assert "context window" not in result.error.lower()

    async def test_filler_text_does_not_leak_into_query_or_explanation(self):
        provider = _make_provider(side_effects=[_degenerate_response()])

        result = await _call_generate(provider, _make_db())

        assert not result.generated_query
        assert "!!!" not in (result.explanation or "")


class TestTruncatedOutput:
    async def test_error_names_output_budget_not_total_tokens(self):
        provider = _make_provider(side_effects=[_truncated_response()])

        result = await _call_generate(provider, _make_db())

        assert result.status == "error"
        assert result.error is not None
        assert "4096" in result.error
        assert "19905" not in result.error

    async def test_error_points_at_output_budget_not_context_window(self):
        provider = _make_provider(side_effects=[_truncated_response()])

        result = await _call_generate(provider, _make_db())

        assert "context window" not in result.error.lower()
        assert "max tokens" in result.error.lower()


def _scratchpad_only_response():
    return parse_llm_response("<thought>The user wants a DELETE.\nI must refuse", "gemma-4-31b-it")


def _empty_but_not_scratchpad_response():
    # Parses to no query and no explanation, and holds no scratchpad.
    return parse_llm_response("EXPLANATION:", "gemma-4-31b-it")


class TestScratchpadOnlyOutput:
    async def test_an_empty_reply_without_a_scratchpad_is_not_called_reasoning(self):
        provider = _make_provider(side_effects=[_empty_but_not_scratchpad_response()])

        result = await _call_generate(provider, _make_db())

        assert "reasoning" not in (result.error or "").lower()

    async def test_reports_a_retryable_error_and_leaks_nothing(self):
        provider = _make_provider(side_effects=[_scratchpad_only_response()])

        result = await _call_generate(provider, _make_db())

        assert result.status == "error"
        assert result.error is not None
        assert "reasoning" in result.error.lower()
        assert "try again" in result.error.lower()
        assert "DELETE" not in result.error
        assert not result.generated_query
        assert not result.explanation

    async def test_scratchpad_only_and_truncated_gets_the_output_limit_error(self):
        response = parse_llm_response(
            "<thought>never closed", "m", truncated=True, output_tokens=4096
        )
        provider = _make_provider(side_effects=[response])

        result = await _call_generate(provider, _make_db())

        assert result.status == "error"
        assert "output limit" in (result.error or "").lower()
        assert "internal reasoning" not in (result.error or "").lower()


class TestPlaceholderRefusal:
    async def test_the_models_explanation_becomes_the_error_not_got_unknown(self):
        # A refusing model writes a placeholder in QUERY. It must not become the query: the
        # validator would refuse it as "Got: UNKNOWN" and hide the model's own explanation.
        raw = (
            "QUERY:\n(Cannot generate query due to safety rules)\n\n"
            "EXPLANATION:\nRule 1 forbids deleting data."
        )
        parsed = parse_llm_response(raw, "m")

        val = await _validate_and_correct_query(
            generated_query=parsed.query,
            explanation=parsed.explanation,
            error=None,
            adapter=MagicMock(),
            schema=MagicMock(),
            cache_hit=False,
            provider=None,
            configured_model="m",
            configured_max_tokens=1000,
            system_prompt="",
            history=[],
            message="Delete all orders",
        )

        assert val.status == "error"
        assert val.error == "Rule 1 forbids deleting data."
        assert not val.generated_query
