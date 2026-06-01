# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""LLM provider setup, prompt compression, chat history loading, and query generation."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ..cache.example_library import ExampleLibrary
from ..cache.query_cache import QueryCache
from ..database import async_session_maker
from ..datasources.base import BaseDataSource
from ..datasources.models import DataSourceSchema, PrivacySettings
from ..models.chat import ChatMessage
from ..providers._factory import resolve_provider_config
from ..providers.base import BaseLLMProvider
from ..semantic.models import SemanticModel
from .prompt_builder import PromptBuilder
from .schema_utils import _is_fallback_query, _validate_columns_against_schema

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Module-level alias for the session factory — assign to this name so that unit
# tests can patch it without touching the shared database module.
_create_session = async_session_maker

logger = logging.getLogger(__name__)

# Safe token budget for providers with low per-minute token limits (e.g. Groq free tier).
_TPM_FALLBACK_BUDGET_TOKENS = 8_000

# Number of top tables in the pruned schema to keep fully annotated at compression level 5.
# The pruned schema is already relevance-ranked, so the first N tables are the most critical.
_PROTECTED_TABLE_COUNT = 3

_HISTORY_TURN_LIMIT = 20

# Fraction of the available context budget used when pre-emptively compressing a prompt.
# The 0.9 leaves a 10% safety margin for token estimation imprecision.
_CONTEXT_BUDGET_SAFETY_FACTOR = 0.9

# Tighter factor applied on a context-window retry (CONTEXT_EXCEEDED error).
# The 0.85 provides extra headroom after the first attempt still overflowed.
_CONTEXT_RETRY_BUDGET_FACTOR = 0.85


@dataclass
class _GenerationResult:
    generated_query: str | None
    explanation: str
    error: str | None
    status: str
    cache_hit: bool
    provider: BaseLLMProvider | None
    configured_model: str
    configured_max_tokens: int
    system_prompt: str
    history: list[dict]
    tokens_used: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Embedding deferred so cache.store() can be called after successful execution.
    cache_embedding: list[float] | None = None
    tpm_warning: str | None = None


def _split_schema_by_priority(
    schema: DataSourceSchema,
    max_protected: int = _PROTECTED_TABLE_COUNT,
) -> tuple[DataSourceSchema, DataSourceSchema]:
    """Split schema into (protected, remainder) for tiered level-5 compression.

    After schema pruning, schema.tables is already the relevance-ranked subset for
    the current question. The first max_protected entries are the most critical tables;
    their annotations are preserved even at minimal compression so the model retains
    the column vocabulary needed for correct GROUP BY / column references.
    """
    return (
        DataSourceSchema(
            source_type=schema.source_type,
            schemas=schema.schemas,
            tables=schema.tables[:max_protected],
            relationships=schema.relationships,
            metadata=schema.metadata,
        ),
        DataSourceSchema(
            source_type=schema.source_type,
            schemas=schema.schemas,
            tables=schema.tables[max_protected:],
            relationships=schema.relationships,
            metadata=schema.metadata,
        ),
    )


def _compact_semantic_model(semantic: SemanticModel) -> SemanticModel:
    """Return a stripped copy of semantic retaining only cross-table business context.

    Keeps business_metrics, segments, time_expressions, and notes (which are
    token-cheap and high-value for the LLM) while dropping the heavy per-table /
    per-column descriptions, relationships, common_joins, and derived_columns.
    Used at compression level 3 so business rules survive even on large schemas.
    """
    return semantic.model_copy(
        update={
            "tables": {},
            "relationships": [],
            "common_joins": [],
            "derived_columns": [],
        }
    )


def _compress_prompt(
    *,
    builder: PromptBuilder,
    datasource: BaseDataSource,
    schema: DataSourceSchema,
    privacy: PrivacySettings | None,
    semantic_model: SemanticModel | None,
    few_shot_examples: list | None,
    user_question: str,
    user_message: str,
    budget_chars: int,
) -> str:
    """Progressively strip optional prompt sections to fit within budget_chars.

    Tries in order:
      1. Full prompt (few-shot + semantic model + full privacy)
      2. No few-shot examples
      3. No few-shot, compact semantic (business metrics / segments / time exprs only)
      4. No few-shot, no semantic model
      5. No few-shot, no semantic model, minimal schema (no sample values / comments / row counts)
    Returns the shortest prompt that fits, or the smallest possible if none fit.
    """
    compact_semantic = (
        _compact_semantic_model(semantic_model) if semantic_model is not None else None
    )
    # Level 5: preserve user exclusions but drop all optional per-column annotations.
    _min_pvt = PrivacySettings(
        include_sample_values=False,
        include_column_comments=False,
        include_row_counts=False,
        excluded_schemas=privacy.excluded_schemas if privacy else [],
        excluded_tables=privacy.excluded_tables if privacy else [],
        excluded_columns=privacy.excluded_columns if privacy else [],
        sensitive_column_patterns=privacy.sensitive_column_patterns if privacy else [],
    )
    for examples, semantic, pvt in [
        (few_shot_examples, semantic_model, privacy),  # full
        (None, semantic_model, privacy),  # no few-shot
        (None, compact_semantic, privacy),  # no few-shot, compact semantic
        (None, None, privacy),  # no few-shot, no semantic
    ]:
        prompt = builder.build_system_prompt(
            datasource=datasource,
            schema=schema,
            privacy=pvt,
            semantic_model=semantic,
            few_shot_examples=examples,
            user_question=user_question,
        )
        if len(prompt) + len(user_message) <= budget_chars:
            return prompt

    # Level 5: tiered schema — protect top tables' annotations, strip the rest.
    # The pruned schema is relevance-ranked, so the first _PROTECTED_TABLE_COUNT
    # tables are the primary targets for the question; keeping their column comments
    # and sample values prevents structurally wrong queries (e.g., count vs GROUP BY).
    schema_protected, schema_rest = _split_schema_by_priority(schema)
    composite_schema = (
        datasource.format_schema_for_llm(schema_protected, privacy)
        + "\n"
        + datasource.format_schema_for_llm(schema_rest, _min_pvt)
    )
    prompt = builder.build_system_prompt(
        datasource=datasource,
        schema=schema,
        privacy=_min_pvt,
        semantic_model=None,
        few_shot_examples=None,
        user_question=user_question,
        schema_override=composite_schema,
    )
    return prompt  # return smallest even if still over; API error handled by caller


async def _build_provider(
    provider_id_or_name: str, db: AsyncSession
) -> tuple[BaseLLMProvider, str, int]:
    """Instantiate an LLM provider, preferring saved DB config over env defaults.

    Accepts either a config UUID (specific instance, as sent by the frontend
    ProviderSelector) or a provider-type name (e.g. "claude").

    Returns ``(provider, configured_model, configured_max_tokens)``.
    Callers should pass ``model=configured_model or None`` to
    ``generate_response`` so the user-chosen model is always honoured.
    """
    provider, model, _name, max_tokens = await resolve_provider_config(provider_id_or_name, db)
    return provider, model, max_tokens


async def _load_history(session_id: str, db: AsyncSession) -> list[dict]:
    """Load the most recent N chat turns as role/content dicts for the LLM history.

    Capped at _HISTORY_TURN_LIMIT to prevent unbounded memory growth on long sessions.
    The prompt compression step handles context-window overflow from large individual turns.
    """
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(_HISTORY_TURN_LIMIT)
    )
    messages = list(reversed(result.scalars().all()))
    history = []
    for msg in messages:
        if msg.role == "assistant" and msg.query_generated:
            content = f"QUERY:\n```sql\n{msg.query_generated}\n```\n\nEXPLANATION:\n{msg.content}"
        else:
            content = msg.content
        history.append({"role": msg.role, "content": content})
    return history


async def _generate_query(
    cache: QueryCache,
    examples: ExampleLibrary,
    connection_id: str,
    message: str,
    session_id: str | None,
    adapter: BaseDataSource,
    schema: DataSourceSchema,
    privacy: PrivacySettings | None,
    semantic_model: SemanticModel | None,
    provider_name: str,
    options: dict,
    settings: Any,
    bypass_cache: bool = False,
    force_refresh: bool = False,
) -> _GenerationResult:
    """Steps 6-8 + cache store: cache lookup, LLM generation, post-processing.

    All DB reads (cache lookup, examples, history, provider config) are scoped to a
    short-lived session that is closed **before** the LLM call.  This ensures the
    connection pool slot is never held during LLM inference.
    """
    generated_query: str | None = None
    explanation: str = ""
    error: str | None = None
    status: str = "executed"
    cache_hit = False
    tokens_used: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    provider: BaseLLMProvider | None = None
    configured_model: str = ""
    configured_max_tokens: int = 4096
    system_prompt: str = ""
    history: list[dict] = []
    embedding: list[float] | None = None
    similar_examples: list | None = None
    _eligible_for_cache: bool = False

    # ── DB reads: cache, examples, history, provider ──────────────────────────
    # Session is acquired here and released at the end of the block — before the
    # LLM call — so the pool slot is never held during inference.
    async with _create_session() as db:
        cache_result = None
        if settings.cache_enabled and not bypass_cache and not force_refresh:
            cache_result = await cache.lookup(connection_id, message, db)

        # If the cached query references tables not visible in this user's schema, treat it as a
        # cache miss and regenerate. Prevents a privileged user's cached query from being served
        # to a less-privileged user who lacks access to some of those tables.
        if cache_result is not None and schema is not None:
            _cache_schema_error = _validate_columns_against_schema(
                cache_result.generated_query, schema
            )
            if _cache_schema_error:
                logger.info(
                    "Cache hit references inaccessible tables for this user — bypassing cache: %s",
                    _cache_schema_error,
                )
                cache_result = None

        # Reject stale cache entries that are not valid SQL (e.g. "[no response]" stored by a
        # previous LLM failure). validate_query() is a pure-text check — no DB connection needed.
        if cache_result is not None:
            _cached_val = adapter.validate_query(cache_result.generated_query)
            if not _cached_val.is_valid:
                logger.info(
                    "Cached query failed validation — treating as cache miss: %s",
                    _cached_val.error_message,
                )
                cache_result = None

        # If the cached query references sensitive columns, bypass the cache and regenerate.
        # Column-level visibility restrictions are not reflected in INFORMATION_SCHEMA, so the
        # schema check above cannot catch them. Checking the query text against the connection's
        # sensitive_column_patterns is the only way to detect whether a privileged user's cached
        # result would fail for a less-privileged user.
        if cache_result is not None and privacy is not None:
            _cached_lower = cache_result.generated_query.lower()
            if any(pat in _cached_lower for pat in privacy.sensitive_column_patterns):
                logger.info(
                    "Cache hit references sensitive column(s) — bypassing cache to allow "
                    "privacy rules to apply for this user"
                )
                cache_result = None

        if cache_result is not None:
            generated_query = cache_result.generated_query
            explanation = (
                f"(Retrieved from cache — original question: {cache_result.cached_question})"
            )
            cache_hit = True
        else:
            embedding = await cache.compute_embedding_async(message)
            similar_examples = await examples.find_similar_examples(
                connection_id, message, embedding, db, query_dialect=adapter.query_dialect
            )

            system_prompt = PromptBuilder().build_system_prompt(
                datasource=adapter,
                schema=schema,
                privacy=privacy,
                semantic_model=semantic_model,
                few_shot_examples=similar_examples or None,
                user_question=message,
            )

            if session_id:
                history = await _load_history(session_id, db)

            # Provider config loaded inside the session — provider object itself is
            # a plain Python instance (not an ORM model) and is safe to use after close.
            try:
                provider, configured_model, configured_max_tokens = await _build_provider(
                    provider_name, db
                )
            except Exception as build_exc:
                # Unknown / misconfigured provider — surface as an error result so
                # callers return ChatResponse(status='error') rather than raising.
                logger.warning("Failed to build provider '%s': %s", provider_name, build_exc)
                return _GenerationResult(
                    generated_query="",
                    explanation="",
                    error=str(build_exc),
                    status="error",
                    cache_hit=False,
                    cache_embedding=None,
                    provider=None,
                    configured_model="",
                    configured_max_tokens=0,
                    system_prompt="",
                    history=[],
                )
    # ── Session closed here — pool slot returned before LLM call ─────────────

    _tpm_hit = False
    if not cache_hit:
        _tpm_retry_attempted = False
        _ctx_retry_attempted = False
        llm_response = None
        # 4 attempts: base + ctx-retry + tpm-retry + one final try with both compressions
        for _attempt in range(4):
            try:
                if isinstance(provider.context_window, int) and provider.context_window > 0:
                    max_out = min(configured_max_tokens, provider.max_output_tokens)
                    budget_chars = int(
                        (provider.context_window - max_out)
                        * provider.chars_per_token
                        * _CONTEXT_BUDGET_SAFETY_FACTOR
                    )
                    history_chars = sum(len(m.get("content", "")) for m in history)
                    prompt_budget = max(0, budget_chars - history_chars)
                    original_len = len(system_prompt) + len(message)
                    if original_len > prompt_budget:
                        system_prompt = _compress_prompt(
                            builder=PromptBuilder(),
                            datasource=adapter,
                            schema=schema,
                            privacy=privacy,
                            semantic_model=semantic_model,
                            few_shot_examples=similar_examples or None,
                            user_question=message,
                            user_message=message,
                            budget_chars=prompt_budget,
                        )
                        logger.debug(
                            "Prompt compressed for %s context window"
                            " (%d -> %d chars, history=%d) — semantic_model_included=%s",
                            provider.provider_name,
                            original_len,
                            len(system_prompt) + len(message),
                            history_chars,
                            "## Business Context" in system_prompt,
                        )

                logger.debug(
                    "Prompt sent to LLM: total=%d chars, semantic_context=%s,"
                    " status_values_visible=%s",
                    len(system_prompt),
                    "## Business Context" in system_prompt,
                    "confirmed=" in system_prompt,
                )
                llm_response = await provider.generate_response(
                    system_prompt=system_prompt,
                    user_message=message,
                    conversation_history=history,
                    model=configured_model or None,
                    temperature=0.0,
                    max_tokens=min(configured_max_tokens, provider.max_output_tokens),
                )
                break
            except Exception as e:
                err_str = str(e)
                if err_str.startswith("[CONTEXT_EXCEEDED]") and not _ctx_retry_attempted:
                    _ctx_retry_attempted = True
                    max_out = min(configured_max_tokens, provider.max_output_tokens)
                    budget_tokens = (
                        (provider.context_window or _TPM_FALLBACK_BUDGET_TOKENS) - max_out - 200
                    )
                    ctx_budget = max(
                        0,
                        int(
                            budget_tokens * provider.chars_per_token * _CONTEXT_RETRY_BUDGET_FACTOR
                        ),
                    )
                    system_prompt = _compress_prompt(
                        builder=PromptBuilder(),
                        datasource=adapter,
                        schema=schema,
                        privacy=privacy,
                        semantic_model=semantic_model,
                        few_shot_examples=None,
                        user_question=message,
                        user_message=message,
                        budget_chars=ctx_budget,
                    )
                    logger.debug(
                        "Context window exceeded for %s; retrying"
                        " with tighter prompt (%d chars budget)",
                        provider.provider_name,
                        ctx_budget,
                    )
                    continue
                if err_str.startswith("[TPM_EXCEEDED]") and not _tpm_retry_attempted:
                    _tpm_retry_attempted = True
                    _tpm_hit = True
                    max_out = min(configured_max_tokens, provider.max_output_tokens)
                    tpm_budget = max(0, (_TPM_FALLBACK_BUDGET_TOKENS - max_out) * 4)
                    current_len = len(system_prompt) + len(message)
                    if tpm_budget >= current_len:
                        tpm_budget = int(current_len * 0.75)
                    system_prompt = _compress_prompt(
                        builder=PromptBuilder(),
                        datasource=adapter,
                        schema=schema,
                        privacy=privacy,
                        semantic_model=semantic_model,
                        few_shot_examples=None,
                        user_question=message,
                        user_message=message,
                        budget_chars=tpm_budget,
                    )
                    logger.warning(
                        "TPM limit (HTTP 413) hit on %s — retrying with compressed prompt "
                        "(%d chars budget, was %d chars). Query quality may be degraded: "
                        "few-shot examples and schema annotations have been stripped.",
                        provider.provider_name,
                        tpm_budget,
                        len(system_prompt),
                    )
                    continue
                logger.warning("LLM provider error (%s): %s", type(e).__name__, e)
                error = err_str.removeprefix("[TPM_EXCEEDED] ").removeprefix("[CONTEXT_EXCEEDED] ")
                status = "error"
                break
        if not error and llm_response is not None:
            tokens_used = llm_response.tokens_used
            input_tokens = llm_response.input_tokens
            output_tokens = llm_response.output_tokens

            # If the model ran out of tokens, the SQL is incomplete — do not execute it.
            if llm_response.truncated:
                logger.warning(
                    "LLM response truncated (finish_reason=length, tokens_used=%s) — "
                    "discarding partial query",
                    tokens_used,
                )
                error = (
                    f"The model hit its output token limit ({tokens_used} tokens) and produced "
                    "an incomplete query. Try a provider with a larger context window "
                    "(e.g. Claude, Gemini, OpenAI), or reduce the number of tables in scope."
                )
                status = "error"
                generated_query = llm_response.query  # keep for display, but won't execute
            else:
                generated_query = llm_response.query
                explanation = llm_response.explanation

            _cache_validation = adapter.validate_query(generated_query) if generated_query else None
            _eligible_for_cache = (
                settings.cache_enabled
                and generated_query
                and not llm_response.truncated
                and _cache_validation is not None
                and _cache_validation.is_valid
                and not _is_fallback_query(generated_query)
            )
            if not _eligible_for_cache:
                if _cache_validation is not None and not _cache_validation.is_valid:
                    logger.info(
                        "Not caching invalid query (%s): %s",
                        _cache_validation.error_message,
                        generated_query[:100],
                    )
                elif llm_response.truncated:
                    logger.info(
                        "Skipping cache for truncated LLM response (tokens_used=%s)",
                        tokens_used,
                    )

    tpm_warning: str | None = None
    if _tpm_hit and not error:
        tpm_warning = (
            "Token-per-minute (TPM) limit hit — the prompt was automatically compressed to fit. "
            "Query quality may be reduced. Consider using a model with a higher token limit"
            " for best results."
        )

    return _GenerationResult(
        generated_query=generated_query,
        explanation=explanation,
        error=error,
        status=status,
        cache_hit=cache_hit,
        tokens_used=tokens_used,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        provider=provider,
        configured_model=configured_model,
        configured_max_tokens=configured_max_tokens,
        system_prompt=system_prompt,
        history=history,
        cache_embedding=embedding if _eligible_for_cache else None,
        tpm_warning=tpm_warning,
    )
