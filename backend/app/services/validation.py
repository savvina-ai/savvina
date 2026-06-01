# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Query validation pipeline: null guard, read-only check, column validation, self-correction."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from ..datasources.base import BaseDataSource
from ..datasources.models import DataSourceSchema
from ..providers.base import BaseLLMProvider
from .correction import _attempt_complexity_correction, _attempt_sql_correction
from .schema_utils import (
    _check_query_complexity,
    _is_fallback_query,
    _validate_columns_against_schema,
)

logger = logging.getLogger(__name__)


@dataclass
class _ValidationResult:
    generated_query: str | None
    explanation: str
    error: str | None
    status: str


async def _validate_and_correct_query(
    generated_query: str | None,
    explanation: str,
    error: str | None,
    adapter: BaseDataSource,
    schema: DataSourceSchema,
    cache_hit: bool,
    provider: BaseLLMProvider | None,
    configured_model: str,
    configured_max_tokens: int,
    system_prompt: str,
    history: list[dict],
    message: str,
) -> _ValidationResult:
    """Steps 9-10.5: null guard, read-only validation, column check + self-correction."""
    status = "error" if error else "executed"

    if not generated_query and not error:
        # Use the LLM's own explanation if it returned one (e.g. "table not found in schema")
        # so the user sees a meaningful message rather than a generic fallback.
        error = explanation.strip() if explanation.strip() else "No query was generated"
        explanation = ""
        status = "error"

    if generated_query and not error:
        validation = adapter.validate_query(generated_query)
        if not validation.is_valid:
            error = validation.error_message
            status = "error"
            generated_query = None

    if generated_query and not error:
        complexity_error = _check_query_complexity(generated_query, schema)
        if complexity_error:
            if not cache_hit and provider is not None:
                corrected_query, corrected_explanation = await _attempt_complexity_correction(
                    original_question=message,
                    failed_query=generated_query,
                    complexity_error=complexity_error,
                    system_prompt=system_prompt,
                    history=history,
                    provider=provider,
                    configured_model=configured_model,
                    configured_max_tokens=configured_max_tokens,
                    schema=schema,
                    adapter=adapter,
                )
                if corrected_query:
                    generated_query = corrected_query
                    explanation = corrected_explanation or explanation
                    logger.debug("Complexity self-correction succeeded; using corrected query")
                else:
                    error = complexity_error
                    status = "error"
                    generated_query = None
            else:
                error = complexity_error
                status = "error"
                generated_query = None

    # Detect LLM schema-unavailability fallback queries — skip execution, surface as info
    if generated_query and not error and _is_fallback_query(generated_query):
        logger.info("Detected fallback query — schema data unavailable for this request")
        error = "NO_DATA: The requested data is not available in the connected schema."
        status = "error"
        generated_query = None

    is_sql_dialect = True
    if not error and generated_query and schema is not None and is_sql_dialect:
        schema_error = _validate_columns_against_schema(generated_query, schema)
        if schema_error and not cache_hit and provider is not None:
            # "Table not found in schema" means the LLM hallucinated a table that is
            # inaccessible (blocked by permissions or privacy settings).  Self-correction
            # cannot fix this — the table will never appear — so skip the retry loop and
            # return immediately to avoid wasting tokens and time.
            is_table_not_found = (
                "does not exist in schema" in schema_error and "table" in schema_error.lower()
            )
            if is_table_not_found:
                logger.debug(
                    "Skipping self-correction: table-not-found errors are not fixable (%s)",
                    schema_error,
                )
                error = schema_error
                status = "error"
                generated_query = None
            else:
                corrected_query, corrected_explanation = await _attempt_sql_correction(
                    original_question=message,
                    failed_query=generated_query,
                    validation_error=schema_error,
                    system_prompt=system_prompt,
                    history=history,
                    provider=provider,
                    configured_model=configured_model,
                    configured_max_tokens=configured_max_tokens,
                    schema=schema,
                    adapter=adapter,
                )
                if corrected_query:
                    generated_query = corrected_query
                    explanation = corrected_explanation or explanation
                    logger.debug("Self-correction succeeded; using corrected query")
                else:
                    error = schema_error
                    status = "error"
                    generated_query = None
        elif schema_error:
            error = schema_error
            status = "error"
            generated_query = None

    return _ValidationResult(
        generated_query=generated_query,
        explanation=explanation,
        error=error,
        status=status,
    )
