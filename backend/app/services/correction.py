# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Self-correction loops for schema errors, complexity violations, execution errors, and no-rows."""

from __future__ import annotations

import logging

from ..datasources.base import BaseDataSource
from ..datasources.models import DataSourceSchema
from ..providers.base import BaseLLMProvider
from .schema_utils import _check_query_complexity, _validate_columns_against_schema

logger = logging.getLogger(__name__)

# Maximum number of self-correction retries per phase (schema, complexity, execution).
# Total LLM calls per phase = _MAX_SELF_CORRECTION_ATTEMPTS + 1 (initial generation not counted).
_MAX_SELF_CORRECTION_ATTEMPTS: int = 2


async def _attempt_sql_correction(
    *,
    original_question: str,
    failed_query: str,
    validation_error: str,
    system_prompt: str,
    history: list[dict],
    provider: BaseLLMProvider,
    configured_model: str,
    configured_max_tokens: int,
    schema: DataSourceSchema,
    adapter: BaseDataSource,
    max_attempts: int = _MAX_SELF_CORRECTION_ATTEMPTS,
) -> tuple[str | None, str]:
    """Ask the LLM to self-correct a query that failed schema validation.

    Loops up to max_attempts times, feeding the latest failed query and error back on
    each iteration.  Re-validates the corrected query with both validators after each attempt.
    Returns (corrected_query, explanation) on success, or (None, "") if still invalid.
    """
    current_query = failed_query
    current_error = validation_error

    for attempt in range(max_attempts):
        correction_message = (
            f"The query you generated failed schema validation"
            f" (attempt {attempt + 1} of {max_attempts}):\n\n"
            f"FAILED QUERY:\n```sql\n{current_query}\n```\n\n"
            f"VALIDATION ERROR:\n{current_error}\n\n"
            f"Please correct the query using ONLY the columns listed as available in the "
            f"validation error above. "
            f"If the concept you are trying to compute does not exist as a stored column, "
            f"derive it mathematically from the available columns rather than referencing "
            f"a column that does not exist. "
            f"The original question was: {original_question}"
        )
        logger.debug(
            "SQL self-correction attempt %d/%d for error: %s",
            attempt + 1,
            max_attempts,
            current_error,
        )
        try:
            llm_response = await provider.generate_response(
                system_prompt=system_prompt,
                user_message=correction_message,
                conversation_history=history,
                model=configured_model or None,
                temperature=0.0,
                max_tokens=min(configured_max_tokens, provider.max_output_tokens),
            )
        except Exception as exc:
            logger.warning("Self-correction LLM call failed: %s", exc, exc_info=True)
            break

        corrected = llm_response.query
        if not corrected:
            break

        read_only_check = adapter.validate_query(corrected)
        if not read_only_check.is_valid:
            logger.debug(
                "Corrected query failed read-only check (attempt %d): %s",
                attempt + 1,
                read_only_check.error_message,
            )
            current_query = corrected
            current_error = read_only_check.error_message or "read-only validation failed"
            continue

        if schema is not None:
            new_schema_error = _validate_columns_against_schema(corrected, schema)
            if new_schema_error:
                logger.debug(
                    "Corrected query still has schema errors (attempt %d); retrying",
                    attempt + 1,
                )
                current_query = corrected
                current_error = new_schema_error
                continue

        return corrected, llm_response.explanation or ""

    return None, ""


async def _attempt_complexity_correction(
    *,
    original_question: str,
    failed_query: str,
    complexity_error: str,
    system_prompt: str,
    history: list[dict],
    provider: BaseLLMProvider,
    configured_model: str,
    configured_max_tokens: int,
    schema: DataSourceSchema | None,
    adapter: BaseDataSource,
    max_attempts: int = _MAX_SELF_CORRECTION_ATTEMPTS,
) -> tuple[str | None, str]:
    """Ask the LLM to rewrite a query that failed a complexity check (e.g. CROSS JOIN).

    Loops up to max_attempts times, feeding the latest error back on each iteration.
    Returns (corrected_query, explanation) on success, or (None, "") if still invalid.
    """
    current_query = failed_query
    current_error = complexity_error

    for attempt in range(max_attempts):
        correction_message = (
            f"The query you generated was rejected due to a complexity issue"
            f" (attempt {attempt + 1} of {max_attempts}):\n\n"
            f"FAILED QUERY:\n```sql\n{current_query}\n```\n\n"
            f"REJECTION REASON:\n{current_error}\n\n"
            f"Please rewrite the query to avoid this issue. "
            f"The original question was: {original_question}"
        )
        logger.debug(
            "Complexity self-correction attempt %d/%d for error: %s",
            attempt + 1,
            max_attempts,
            current_error,
        )
        try:
            llm_response = await provider.generate_response(
                system_prompt=system_prompt,
                user_message=correction_message,
                conversation_history=history,
                model=configured_model or None,
                temperature=0.0,
                max_tokens=min(configured_max_tokens, provider.max_output_tokens),
            )
        except Exception as exc:
            logger.warning("Complexity self-correction LLM call failed: %s", exc, exc_info=True)
            break

        corrected = llm_response.query
        if not corrected:
            break

        read_only_check = adapter.validate_query(corrected)
        if not read_only_check.is_valid:
            logger.debug(
                "Complexity-corrected query failed read-only check (attempt %d): %s",
                attempt + 1,
                read_only_check.error_message,
            )
            current_query = corrected
            current_error = read_only_check.error_message or "read-only validation failed"
            continue

        new_complexity_error = _check_query_complexity(corrected, schema)
        if new_complexity_error:
            logger.debug(
                "Complexity-corrected query still fails complexity check (attempt %d); retrying",
                attempt + 1,
            )
            current_query = corrected
            current_error = new_complexity_error
            continue

        if schema is not None:
            schema_error = _validate_columns_against_schema(corrected, schema)
            if schema_error:
                logger.debug(
                    "Complexity-corrected query has schema errors (attempt %d); retrying",
                    attempt + 1,
                )
                current_query = corrected
                current_error = schema_error
                continue

        return corrected, llm_response.explanation or ""

    return None, ""


# Hints injected into the correction prompt for well-known SQL runtime errors.
_EXEC_ERROR_HINTS: dict[str, str] = {
    "division by zero": (
        "Use NULLIF(divisor, 0) to guard against division by zero. "
        "Example: revenue / NULLIF(spent, 0)."
    ),
    "aggregate functions are not allowed in where": (
        "Aggregate functions (SUM, AVG, COUNT, MIN, MAX) cannot appear in a WHERE clause. "
        "Move aggregate conditions to a HAVING clause."
    ),
    "select permission was denied on the column": (
        "The database has denied SELECT access to one or more columns in your query. "
        "Do NOT attempt to rewrite the query to retrieve that data via a workaround. "
        "Instead, respond with an EXPLANATION only (no QUERY block) telling the user "
        "which column is restricted and that it cannot be accessed with their current credentials."
    ),
    "the select permission was denied": (
        "The database has denied SELECT access to one or more objects in your query. "
        "Do NOT attempt to rewrite the query to retrieve that data via a workaround. "
        "Instead, respond with an EXPLANATION only (no QUERY block) telling the user "
        "which object is restricted and that it cannot be accessed with their current credentials."
    ),
    "must appear in the group by clause": (
        "Every non-aggregated column in SELECT must appear in GROUP BY. "
        "If the column is a scalar from a single-row CTE, wrap it in MAX() or MIN(). "
        "Prefer a single aggregate query over CTEs for percentages: "
        "SUM(CASE WHEN ... THEN 1 ELSE 0 END)::numeric / COUNT(*) * 100."
    ),
    'filter" is not supported': (
        "The FILTER clause on aggregate functions is not supported by this database. "
        "Replace agg(...) FILTER (WHERE condition) with agg(CASE WHEN condition THEN expr END). "
        "Example: COUNT(*) FILTER (WHERE status = 'x') → COUNT(CASE WHEN status = 'x' THEN 1 END)."
    ),
    "does not exist": (
        "A column or relation name was not found. Check for typos and confirm the exact names "
        "from the schema. Qualify ambiguous column names with the table name."
    ),
    "ambiguous column": (
        "A column name appears in multiple joined tables. Qualify it with its table name, "
        "e.g. table_name.column_name."
    ),
    "invalid input syntax for type": (
        "A value cannot be cast to the expected column type. Use an explicit CAST or :: operator "
        "(e.g. column::integer) and ensure the value matches the target type."
    ),
    "syntax error at or near": (
        "There is a SQL syntax error near the highlighted token. "
        'If the token is a backtick (`), replace it with double quotes (") — '
        "PostgreSQL uses double quotes for quoted identifiers, not backticks. "
        "Also check for unclosed parentheses, missing commas, or incorrect clause ordering."
    ),
    "is ambiguous": (
        "A column or alias reference is ambiguous. "
        "For ORDER BY: use column ordinal positions (e.g. ORDER BY 3 DESC) instead of alias names. "
        "For SELECT/WHERE/JOIN: qualify the column with its table or CTE name "
        "(e.g. cte_name.column_name or table_alias.column_name)."
    ),
}


async def _attempt_sql_execution_correction(
    *,
    original_question: str,
    failed_query: str,
    exec_error: str,
    system_prompt: str,
    history: list[dict],
    provider: BaseLLMProvider,
    configured_model: str,
    configured_max_tokens: int,
    schema: DataSourceSchema | None,
    adapter: BaseDataSource,
    attempt_num: int = 1,
) -> tuple[str | None, str]:
    """Ask the LLM to self-correct a query that failed at execution time (single attempt).

    Sends the failed query and runtime error (plus a targeted hint for known error patterns)
    back to the LLM with explicit correction instructions.  Re-validates the corrected query
    before returning it.  The caller is responsible for looping if multiple retries are needed.
    Returns (corrected_query, explanation) on success, or (None, "") if still invalid.
    """
    hint = ""
    for pattern, advice in _EXEC_ERROR_HINTS.items():
        if pattern in exec_error.lower():
            hint = f"\n\nHint: {advice}"
            break

    correction_message = (
        f"The query you generated failed when executed against the database"
        f" (correction attempt {attempt_num}):\n\n"
        f"FAILED QUERY:\n```sql\n{failed_query}\n```\n\n"
        f"EXECUTION ERROR:\n{exec_error}{hint}\n\n"
        f"Please correct the query to fix the above error. "
        f"The original question was: {original_question}"
    )
    logger.debug("SQL execution self-correction attempt %d for: %s", attempt_num, exec_error)
    try:
        llm_response = await provider.generate_response(
            system_prompt=system_prompt,
            user_message=correction_message,
            conversation_history=history,
            model=configured_model or None,
            temperature=0.0,
            max_tokens=min(configured_max_tokens, provider.max_output_tokens),
        )
    except Exception as exc:
        logger.warning("Execution self-correction LLM call failed: %s", exc, exc_info=True)
        exc_str = str(exc)
        rate_limited = (
            "TPM_EXCEEDED" in exc_str
            or "rate_limit" in exc_str.lower()
            or "token" in exc_str.lower()
            or "413" in exc_str
        )
        if rate_limited:
            return (
                None,
                "auto-correction failed: the AI provider's rate limit was exceeded"
                " while attempting to fix this query",
            )
        return None, f"auto-correction failed: {exc_str}"

    corrected = llm_response.query
    if not corrected:
        return None, ""

    read_only_check = adapter.validate_query(corrected)
    if not read_only_check.is_valid:
        logger.debug(
            "Execution-corrected query failed read-only check: %s",
            read_only_check.error_message,
        )
        return None, ""

    if schema is not None and _validate_columns_against_schema(corrected, schema):
        logger.debug("Execution-corrected query has schema errors; discarding")
        return None, ""

    return corrected, llm_response.explanation or ""


async def _attempt_zero_result_correction(
    *,
    original_question: str,
    query: str,
    system_prompt: str,
    history: list[dict],
    provider: BaseLLMProvider,
    configured_model: str,
    configured_max_tokens: int,
    schema: DataSourceSchema | None,
    adapter: BaseDataSource,
) -> tuple[str | None, str]:
    """Ask the LLM to reconsider a query that executed successfully but returned 0 rows.

    Called only when:
    - Execution succeeded with row_count == 0
    - Intent is not EXISTENCE (for which 0 rows is a valid answer)
    - Provider is available and result is not from cache

    Returns (corrected_query, explanation).  Either value may be empty/None:
    - corrected_query=None, explanation=non-empty → LLM confirmed 0 rows is correct
    - corrected_query=None, explanation=""       → LLM call failed or correction invalid
    - corrected_query=non-empty                  → use this query instead
    """
    correction_message = (
        f"The query you generated executed successfully but returned 0 rows.\n\n"
        f"QUERY THAT RETURNED 0 ROWS:\n```sql\n{query}\n```\n\n"
        "Diagnose whether this is due to one of:\n"
        "1. A filter value that does not match the actual data "
        "(e.g. wrong case, spelling, or format of a string literal in a WHERE clause)\n"
        "2. An overly restrictive date range or numeric filter\n"
        "3. A JOIN condition that eliminates all rows (wrong join column or direction)\n"
        "4. A faulty assumption about which data exists\n\n"
        "If you can identify a specific cause, provide a corrected QUERY block. "
        "If 0 rows is the genuinely correct answer for this question, "
        "provide only an EXPLANATION — no QUERY block.\n\n"
        f"The original question was: {original_question}"
    )
    logger.debug("Zero-result self-correction triggered for query: %.100s", query)
    try:
        llm_response = await provider.generate_response(
            system_prompt=system_prompt,
            user_message=correction_message,
            conversation_history=history,
            model=configured_model or None,
            temperature=0.0,
            max_tokens=min(configured_max_tokens, provider.max_output_tokens),
        )
    except Exception as exc:
        logger.warning("Zero-result correction LLM call failed: %s", exc, exc_info=True)
        return None, ""

    corrected = llm_response.query
    if not corrected:
        return None, llm_response.explanation or ""

    read_only_check = adapter.validate_query(corrected)
    if not read_only_check.is_valid:
        logger.debug(
            "Zero-result corrected query failed read-only check: %s",
            read_only_check.error_message,
        )
        return None, ""

    if schema is not None and _validate_columns_against_schema(corrected, schema):
        logger.debug("Zero-result corrected query has schema errors; discarding")
        return None, ""

    return corrected, llm_response.explanation or ""
