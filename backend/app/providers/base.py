# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Base class and shared response-parsing utilities for LLM providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import json
import logging
from typing import TypeVar

from json_repair import repair_json
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Exported for provider overrides that need the same bound TypeVar.
_BM = TypeVar("_BM", bound=BaseModel)

# Shared HTTP timeout constants used by provider fetch_available_models() implementations.
_HTTP_TIMEOUT_S: float = 15.0
_HEALTH_CHECK_TIMEOUT_S: float = 5.0


class ProviderConnectError(Exception):
    """Raised when a provider's API is unreachable (network/TLS/DNS failure)."""


class ProviderAuthError(Exception):
    """Raised when a provider rejects credentials (HTTP 401/403)."""


def _raise_if_fetch_error(exc: Exception, provider: str) -> None:
    """Re-raise *exc* as a typed provider error for known httpx failure modes.

    Call this from the ``except Exception`` handler in ``fetch_available_models``
    implementations *before* returning ``[]``.  Unknown errors are logged and
    swallowed so callers fall back gracefully.
    """
    import httpx

    if isinstance(exc, httpx.ConnectError):
        raise ProviderConnectError(
            f"Cannot connect to {provider} API — check network/firewall settings"
        ) from exc
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
        raise ProviderAuthError(f"Invalid API key (HTTP {exc.response.status_code})") from exc
    logger.warning("fetch_available_models failed for %s", provider, exc_info=True)


@dataclass
class ModelInfo:
    """Metadata about a single model returned by a provider's models API."""

    id: str
    context_window: int | None = field(default=None)
    max_completion_tokens: int | None = field(default=None)


@dataclass
class LLMResponse:
    """Parsed response returned by every provider's generate_response()."""

    query: str  # Extracted SQL/query string
    explanation: str  # Human-readable explanation of the query
    raw_response: str  # Full raw text from the LLM
    model: str  # Actual model identifier used
    # Total tokens (input + output). Kept for backwards compatibility; derived from the split
    # fields when both are provided.
    tokens_used: int | None = None
    # Tokens consumed by the prompt (system + history + user message).
    input_tokens: int | None = None
    # Tokens produced by the model in the completion.
    output_tokens: int | None = None
    truncated: bool = False  # True when finish_reason=="length" (max_tokens hit)


class BaseLLMProvider(ABC):
    """Abstract base class for all LLM provider adapters."""

    provider_name: str = ""
    display_name: str = ""
    default_model: str = ""
    # Maximum output tokens this provider/model family supports.
    # Callers should clamp their requested max_tokens to this value.
    max_output_tokens: int = 8192
    # Total context window (input + output) in tokens.
    # Set for providers with a hard context limit; None means no known hard limit.
    context_window: int | None = None
    # Approximate characters per token for this provider's typical schema/DDL content.
    # Used to budget prompt compression. Conservative (low) values are safer for small windows.
    chars_per_token: float = 4.0
    # Whether this provider supports explicit prompt caching (cache_control blocks).
    # OpenAI caches automatically server-side; others are no-ops. Only Claude is True.
    supports_prompt_caching: bool = False

    @abstractmethod
    async def generate_response(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: list[dict],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Send a prompt to the LLM and return a parsed LLMResponse."""
        ...

    async def generate_response_cached(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: list[dict],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Prompt-cached variant. Default delegates to generate_response (no-op cache)."""
        return await self.generate_response(
            system_prompt,
            user_message,
            conversation_history,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    @abstractmethod
    async def health_check(self) -> tuple[bool, str]:
        """Return (True, "") if reachable, or (False, error_detail) on failure."""
        ...

    @classmethod
    @abstractmethod
    def get_available_models(cls) -> list[str]:
        """Return the list of model identifiers this provider supports."""
        ...

    @classmethod
    async def fetch_available_models(
        cls,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> list[ModelInfo]:
        """Fetch live models from the provider API.

        Override in each provider to query the real API and return richer
        metadata (context_window, max_completion_tokens). Returns [] when
        credentials are missing or the request fails.
        """
        return []

    async def generate_structured(
        self,
        system_prompt: str,
        user_message: str,
        schema_type: type[_BM],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> _BM:
        """Generate a response validated against a Pydantic model schema.

        Default implementation: calls generate_response with a JSON instruction
        appended to the system prompt, extracts the JSON from the raw response,
        and validates it with schema_type.model_validate.  Providers with native
        structured-output support (Claude tool-use, OpenAI json_object) override
        this to use their API-level constraints instead.
        """
        enhanced = system_prompt + "\nRespond ONLY with a single valid JSON object."
        response = await self.generate_response(
            enhanced,
            user_message,
            [],
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text = _extract_json_from_text(response.raw_response)
        data = json.loads(repair_json(text))
        return schema_type.model_validate(data)


# ── Response parsing ──────────────────────────────────────────────────────────


def _parse_structured_response(raw: str) -> tuple[str, str]:
    """Extract (query, explanation) when both QUERY: and EXPLANATION: markers are present.

    Guards against EXPLANATION: appearing before QUERY: in two stages:
    1. Try rfind to find the last EXPLANATION: (handles preamble mentions).
    2. If still before QUERY: (only one occurrence), consume the rest as the
       query section and use the text between EXPLANATION: and QUERY: as the
       explanation.
    """
    raw_upper = raw.upper()
    q_pos = raw_upper.find("QUERY:") + len("QUERY:")
    e_pos = raw_upper.find("EXPLANATION:")
    # Guard 1: if EXPLANATION: appears before QUERY: (e.g. mentioned in REASONING),
    # find the LAST EXPLANATION: instead so the slice is non-empty.
    if e_pos < q_pos:
        e_pos = raw_upper.rfind("EXPLANATION:")
    # Guard 2: single EXPLANATION: precedes QUERY: — consume the rest as the
    # query section; treat the text before QUERY: as the explanation.
    if e_pos < q_pos:
        q_section = raw[q_pos:].strip()
        explanation = raw[e_pos + len("EXPLANATION:") : q_pos - len("QUERY:")].strip()
    else:
        q_section = raw[q_pos:e_pos].strip()
        explanation = raw[e_pos + len("EXPLANATION:") :].strip()
    extracted = _extract_sql(q_section)
    if not extracted and q_section.upper().startswith("QUERY:"):
        q_section = q_section[len("QUERY:") :].strip()
    query = extracted or q_section
    # Safety: strip any trailing EXPLANATION/REASONING text that leaked in when
    # the LLM omitted the closing code fence or wrote sections without separation.
    for _marker in ("EXPLANATION:", "REASONING:"):
        _marker_pos = query.upper().find(_marker)
        if _marker_pos != -1:
            query = query[:_marker_pos].strip()
    # LLM wrote a null signal as the query — surface the explanation instead.
    if query.strip().lower() in ("none", "null", "n/a"):
        query = ""
    return query, explanation


def _parse_unstructured_response(raw: str, model: str) -> tuple[str, str]:
    """Extract (query, explanation) when structured QUERY:/EXPLANATION: markers are absent.

    Tries code-fence extraction first, then falls back to SQL-keyword heuristics.
    Natural-language refusals are returned as explanations rather than broken SQL.
    """
    logger.warning(
        "parse_llm_response: model %r did not use primary QUERY:/EXPLANATION: format "
        "(raw_len=%d); falling back to code-fence extraction",
        model,
        len(raw),
    )
    query = _extract_sql(raw)
    explanation = ""
    if not query:
        logger.warning(
            "parse_llm_response: model %r — code-fence extraction failed; "
            "applying raw-text heuristics (first 300): %r",
            model,
            raw[:300],
        )
        candidate = raw.strip()
        # LLM used QUERY: marker but omitted EXPLANATION: — strip the label
        if candidate.upper().startswith("QUERY:"):
            candidate = candidate[len("QUERY:") :].strip()
        # If the fallback text doesn't start with a SQL keyword or comment it is
        # a natural-language refusal/explanation, not broken SQL.  Use it as the
        # explanation so the caller gets a sensible message instead of a
        # confusing "Got: UNKNOWN" validation error.
        sql_starters = (
            "SELECT",
            "WITH",
            "INSERT",
            "UPDATE",
            "DELETE",
            "CREATE",
            "DROP",
            "ALTER",
            "EXPLAIN",
            "--",
            "/*",
            "[",
            "{",
        )
        if any(candidate.upper().lstrip().startswith(kw) for kw in sql_starters):
            query = candidate
        else:
            explanation = candidate
    return query, explanation


def parse_llm_response(
    raw: str,
    model: str,
    tokens_used: int | None = None,
    truncated: bool = False,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> LLMResponse:
    """
    Parse an LLM response into a structured :class:`LLMResponse`.

    Expected format (instructed via the system prompt)::

        QUERY:
        ```sql
        SELECT ...
        ```

        EXPLANATION:
        Brief explanation of what the query does.

    Parsing strategy:
    1. If both ``QUERY:`` and ``EXPLANATION:`` markers are present, extract
       each section explicitly.
    2. Otherwise fall back to finding any SQL code block (````sql`` or `` ``` ``).
    3. Last resort: treat the entire response as the query.
    """
    raw_upper = raw.upper()
    if "QUERY:" in raw_upper and "EXPLANATION:" in raw_upper:
        query, explanation = _parse_structured_response(raw)
    else:
        query, explanation = _parse_unstructured_response(raw, model)

    if not query.strip() and not explanation.strip() and raw.strip():
        logger.warning(
            "parse_llm_response: both query and explanation empty despite non-empty raw "
            "(%d chars) — raw (first 600): %r",
            len(raw),
            raw[:600],
        )
    logger.debug(
        "parse_llm_response: query (first 300)=%r  explanation (first 200)=%r",
        query[:300],
        explanation[:200],
    )
    # Derive total if split fields were provided but the caller didn't pass an explicit total.
    if tokens_used is None and (input_tokens is not None or output_tokens is not None):
        tokens_used = (input_tokens or 0) + (output_tokens or 0)
    return LLMResponse(
        query=query.strip(),
        explanation=explanation.strip(),
        raw_response=raw,
        model=model,
        tokens_used=tokens_used,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        truncated=truncated,
    )


def _extract_sql(text: str) -> str:
    """
    Extract a SQL/JSON string from a markdown code block.

    Tries ````json`` first, then ````sql``, then any triple-backtick fence.
    Returns an empty string if no code block is found.
    """
    logger.debug("_extract_sql input (first 300): %r", text[:300])

    # Try ```json ... ```
    if "```json" in text:
        start = text.find("```json") + len("```json")
        end = text.find("```", start)
        if end != -1:
            result = text[start:end].strip()
            logger.debug("_extract_sql: extracted via ```json (first 200): %r", result[:200])
            return result

    # Try ```sql ... ```
    if "```sql" in text:
        start = text.find("```sql") + len("```sql")
        end = text.find("```", start)
        if end != -1:
            result = text[start:end].strip()
            logger.debug("_extract_sql: extracted via ```sql (first 200): %r", result[:200])
            return result

    # Try any ``` ... ``` fence
    if "```" in text:
        fence_start = text.find("```")
        after_fence = text.find("\n", fence_start)
        if after_fence != -1:
            closing = text.find("```", after_fence)
            if closing != -1:
                result = text[after_fence:closing].strip()
                logger.debug(
                    "_extract_sql: extracted via generic fence (first 200): %r", result[:200]
                )
                return result

    logger.debug("_extract_sql: no code fence found, returning empty string")
    return ""


def _extract_json_from_text(text: str) -> str:
    """Extract a JSON object from an LLM response string.

    Handles pure JSON, code-fenced JSON, and JSON embedded in prose.
    Used by BaseLLMProvider.generate_structured's fallback path.
    """
    text = text.strip()
    if text.startswith("{"):
        return text
    if "```" in text:
        fence = text.find("```")
        after = text.find("\n", fence)
        if after != -1:
            close = text.find("```", after)
            if close != -1:
                return text[after:close].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text
