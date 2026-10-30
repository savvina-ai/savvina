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

from app.providers.base import parse_llm_response

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
