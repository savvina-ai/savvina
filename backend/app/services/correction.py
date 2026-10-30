# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Self-correction loops for schema errors, complexity violations, execution errors, and no-rows."""

from __future__ import annotations

import logging

from ..datasources.base import BaseDataSource
from ..datasources.errors import ErrorCategory
from ..datasources.models import DataSourceSchema
from ..providers.base import BaseLLMProvider
from .error_feedback import describe_error_for_llm, redact_error_for_user
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


_PERMISSION_ADVICE = (
    "The database has denied access to one or more objects or columns in your query. "
    "Do NOT attempt to rewrite the query to retrieve that data via a workaround. "
    "Instead, respond with an EXPLANATION only (no QUERY block) telling the user "
    "which object is restricted and that it cannot be accessed with their current credentials."
)

_UNKNOWN_NAME_ADVICE = (
    "A column or relation name was not found. Check for typos and confirm the exact names "
    "from the schema. Qualify ambiguous column names with the table name. If a function "
    "exists but rejects its arguments, cast them explicitly with CAST(... AS ...) instead of "
    "switching to a different function."
)

_AMBIGUOUS_ADVICE = (
    "A column or alias reference is ambiguous. "
    "For ORDER BY: use column ordinal positions (e.g. ORDER BY 3 DESC) instead of alias names. "
    "For SELECT/WHERE/JOIN: qualify the column with its table or CTE name "
    "(e.g. cte_name.column_name or table_alias.column_name)."
)

_DIVISION_ADVICE = (
    "Use NULLIF(divisor, 0) to guard against division by zero. Example: revenue / NULLIF(spent, 0)."
)

# Each hint is keyed by a fragment of the engine's text and tagged with the category it
# belongs to. A text match is used only when it agrees with the classified category (or
# the category is OTHER): the text is read here for hint selection and never copied into
# the prompt. Dict order is match order; the backtick entry must precede the generic
# "syntax error at or near" one.
_EXEC_ERROR_HINTS: dict[str, tuple[ErrorCategory, str]] = {
    "division by zero": (ErrorCategory.DIVISION_BY_ZERO, _DIVISION_ADVICE),
    "aggregate functions are not allowed in where": (
        ErrorCategory.GROUPING,
        "Aggregate functions (SUM, AVG, COUNT, MIN, MAX) cannot appear in a WHERE clause. "
        "Move aggregate conditions to a HAVING clause.",
    ),
    "select permission was denied on the column": (ErrorCategory.PERMISSION, _PERMISSION_ADVICE),
    "the select permission was denied": (ErrorCategory.PERMISSION, _PERMISSION_ADVICE),
    "must appear in the group by clause": (
        ErrorCategory.GROUPING,
        "Every non-aggregated column in SELECT must appear in GROUP BY. "
        "If the column is a scalar from a single-row CTE, wrap it in MAX() or MIN(). "
        "Prefer a single aggregate query over CTEs for percentages: "
        "SUM(CASE WHEN ... THEN 1 ELSE 0 END)::numeric / COUNT(*) * 100.",
    ),
    'filter" is not supported': (
        ErrorCategory.UNSUPPORTED,
        "The FILTER clause on aggregate functions is not supported by this database. "
        "Replace agg(...) FILTER (WHERE condition) with agg(CASE WHEN condition THEN expr END). "
        "Example: COUNT(*) FILTER (WHERE status = 'x') → COUNT(CASE WHEN status = 'x' THEN 1 END).",
    ),
    "does not exist": (ErrorCategory.UNKNOWN_NAME, _UNKNOWN_NAME_ADVICE),
    "ambiguous column": (
        ErrorCategory.AMBIGUOUS_NAME,
        "A column name appears in multiple joined tables. Qualify it with its table name, "
        "e.g. table_name.column_name.",
    ),
    "invalid input syntax for type": (
        ErrorCategory.DATA,
        "A value cannot be cast to the expected column type. Use an explicit CAST or :: operator "
        "(e.g. column::integer) and ensure the value matches the target type.",
    ),
    # The composed description no longer carries the database's text, so the model cannot
    # itself notice that the reported location is a backtick — say so explicitly, and only
    # where the raw text actually names one.
    'at or near "`': (
        ErrorCategory.SYNTAX,
        "The database's message points at a backtick (`) in your query. "
        'PostgreSQL uses double quotes (") for quoted identifiers, not backticks — '
        'replace `name` with "name". '
        "Also check for unclosed parentheses, missing commas, or incorrect clause ordering.",
    ),
    "syntax error at or near": (
        ErrorCategory.SYNTAX,
        "There is a SQL syntax error in your query for this SQL dialect. "
        "Check for typos, incorrect keywords, unclosed parentheses, missing commas, "
        "or incorrect clause ordering.",
    ),
    "is ambiguous": (ErrorCategory.AMBIGUOUS_NAME, _AMBIGUOUS_ADVICE),
}

# Used when no text hint both matched and agreed with the category (a non-English server,
# or the only textual match was refused). Dialect-neutral by construction: these fire for
# PostgreSQL and MySQL alike.
_CATEGORY_FALLBACK_HINTS: dict[ErrorCategory, str] = {
    ErrorCategory.UNKNOWN_NAME: _UNKNOWN_NAME_ADVICE,
    ErrorCategory.AMBIGUOUS_NAME: _AMBIGUOUS_ADVICE,
    ErrorCategory.GROUPING: (
        "Every non-aggregated column in SELECT must appear in GROUP BY, or be wrapped in an "
        "aggregate such as MAX() or MIN(). Aggregate conditions belong in HAVING, not WHERE."
    ),
    ErrorCategory.DIVISION_BY_ZERO: _DIVISION_ADVICE,
    ErrorCategory.PERMISSION: _PERMISSION_ADVICE,
    ErrorCategory.DATA: (
        "Guard the conversion: filter out the rows whose values cannot be converted before "
        "casting, cast with a CASE that checks the value's shape first, or select a different "
        "column that already has the right type."
    ),
    ErrorCategory.TYPE_MISMATCH: (
        "An operator or function received operands of incompatible types. Cast one side "
        "explicitly so both sides share a type, and check the argument types the function expects."
    ),
    # The model no longer sees the engine's `near '...'` location, so name the usual causes.
    ErrorCategory.SYNTAX: (
        "A reserved word used as an alias or column name must be quoted with the dialect's "
        "identifier quote (double quotes in PostgreSQL, backticks in MySQL). "
        "Every derived table (subquery in FROM) needs an alias, and all UNION/INTERSECT/EXCEPT "
        "branches need the same number of columns. "
        "Also check for unclosed parentheses, missing or trailing commas, and clause order "
        "(SELECT ... FROM ... WHERE ... GROUP BY ... HAVING ... ORDER BY ... LIMIT)."
    ),
    ErrorCategory.CARDINALITY: (
        "A subquery used as a single value returned more than one row. Use IN or EXISTS "
        "instead of =, aggregate the subquery (MAX or MIN), or add LIMIT 1 only if any one "
        "row is acceptable."
    ),
    ErrorCategory.TIMEOUT: (
        "The query exceeded the time limit. Narrow it: filter earlier, aggregate instead "
        "of returning raw rows, or reduce the number of joins."
    ),
    ErrorCategory.UNSUPPORTED: (
        "Only a single read-only SELECT (or WITH … SELECT) is permitted. Rewrite the statement "
        "as one, and avoid features this database engine lacks."
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
    error_category: ErrorCategory = ErrorCategory.OTHER,
    llm_hint: str | None = None,
) -> tuple[str | None, str]:
    """Ask the LLM to self-correct a query that failed at execution time (single attempt).

    Sends the failed query and *our own* description of the runtime error (plus a targeted
    hint) back to the LLM. The database's error text is read here to pick a hint and is
    never copied into the prompt: it can quote a value stored in the tables. Re-validates
    the corrected query before returning it. Returns (corrected_query, explanation) on
    success, or (None, "") if still invalid. *llm_hint* is a ``QueryRejectedError.llm_hint``,
    appended to the description as it is.
    """
    hint = ""
    lowered = exec_error.lower()
    for pattern, (category, advice) in _EXEC_ERROR_HINTS.items():
        if pattern not in lowered:
            continue
        # A text match is trusted only when it agrees with the classified category, or
        # when nothing more specific was determined (OTHER). Under OTHER a fixed hint may
        # be chosen from the raw text, which reveals at most which of the fixed phrases
        # appeared in the engine message: accepted, because the phrases are engine wording,
        # not data, and OTHER has no better signal.
        if error_category is not ErrorCategory.OTHER and error_category is not category:
            continue
        hint = f"\n\nHint: {advice}"
        break
    if not hint:
        fallback = _CATEGORY_FALLBACK_HINTS.get(error_category)
        if fallback:
            hint = f"\n\nHint: {fallback}"

    error_description = describe_error_for_llm(
        error_category, exec_error, sql=failed_query, schema=schema, hint=llm_hint
    )
    correction_message = (
        f"The query you generated failed when executed against the database"
        f" (correction attempt {attempt_num}):\n\n"
        f"FAILED QUERY:\n```sql\n{failed_query}\n```\n\n"
        f"EXECUTION ERROR:\n{error_description}{hint}\n\n"
        f"Please correct the query to fix the above error. "
        f"The original question was: {original_question}"
    )
    logger.debug(
        "SQL execution self-correction attempt %d for: %s",
        attempt_num,
        redact_error_for_user(error_category, exec_error),
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
