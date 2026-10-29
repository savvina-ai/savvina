# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""A closed vocabulary for why a query failed, derived from each engine's documented codes.

The LLM is told the category, never the engine's message text. PostgreSQL and MySQL both
quote the offending stored value in cast and parse errors (``invalid input syntax for type
integer: "Jane Doe"``), which is table data leaving the deployment through a correction
prompt. ``services/error_feedback.describe_error_for_llm`` turns a category into prose.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
import re


class ErrorCategory(StrEnum):
    """Why the database rejected a query. The LLM is told this, never the engine's text."""

    UNKNOWN_NAME = "unknown_name"
    AMBIGUOUS_NAME = "ambiguous_name"
    SYNTAX = "syntax"
    GROUPING = "grouping"
    TYPE_MISMATCH = "type_mismatch"
    DATA = "data"
    DIVISION_BY_ZERO = "division_by_zero"
    CARDINALITY = "cardinality"
    PERMISSION = "permission"
    UNSUPPORTED = "unsupported"
    TIMEOUT = "timeout"
    OTHER = "other"


class ExplainError(Exception):
    """``BaseDataSource.explain`` could not plan the statement. The message is the engine's
    text (it may quote a value; redact by category before showing it) and ``__cause__`` is
    the driver exception, for ``classify_error``."""


# Categories whose engine messages quote names (columns, tables, functions, types) and not
# stored values. The user sees their text as is and the LLM is told the names in it that it
# already has (``services/error_feedback``); every other category can quote a value. An
# adapter whose engine reports a value under one of these codes must classify it as DATA
# (PostgreSQL: ``PostgreSQLDataSource._classify_one``).
NAME_ECHOING_CATEGORIES = frozenset(
    {
        ErrorCategory.UNKNOWN_NAME,
        ErrorCategory.AMBIGUOUS_NAME,
        ErrorCategory.GROUPING,
        ErrorCategory.TYPE_MISMATCH,
        ErrorCategory.PERMISSION,
        ErrorCategory.SYNTAX,
    }
)


# PostgreSQL SQLSTATEs (PostgreSQL docs, Appendix A "Error Codes"). Exact codes first,
# then two-character classes.
_SQLSTATE_EXACT: dict[str, ErrorCategory] = {
    "42703": ErrorCategory.UNKNOWN_NAME,  # undefined_column
    "42P01": ErrorCategory.UNKNOWN_NAME,  # undefined_table
    "42883": ErrorCategory.UNKNOWN_NAME,  # undefined_function (see refine_undefined_function)
    "42704": ErrorCategory.UNKNOWN_NAME,  # undefined_object
    "3F000": ErrorCategory.UNKNOWN_NAME,  # invalid_schema_name
    "3D000": ErrorCategory.UNKNOWN_NAME,  # invalid_catalog_name
    "42702": ErrorCategory.AMBIGUOUS_NAME,  # ambiguous_column
    "42725": ErrorCategory.AMBIGUOUS_NAME,  # ambiguous_function
    "42P09": ErrorCategory.AMBIGUOUS_NAME,  # ambiguous_alias
    "42601": ErrorCategory.SYNTAX,  # syntax_error
    "42803": ErrorCategory.GROUPING,  # grouping_error
    "42804": ErrorCategory.TYPE_MISMATCH,  # datatype_mismatch
    "42846": ErrorCategory.TYPE_MISMATCH,  # cannot_coerce
    "42P18": ErrorCategory.TYPE_MISMATCH,  # indeterminate_datatype
    "22012": ErrorCategory.DIVISION_BY_ZERO,
    "21000": ErrorCategory.CARDINALITY,  # cardinality_violation
    "42501": ErrorCategory.PERMISSION,  # insufficient_privilege
    "0A000": ErrorCategory.UNSUPPORTED,  # feature_not_supported
    # read_only_sql_transaction: a write the validator missed, refused by the read-only
    # transaction every query runs in. "Unsupported" is the honest category — the model
    # must rewrite the statement as a read, not look for a workaround.
    "25006": ErrorCategory.UNSUPPORTED,
    "57014": ErrorCategory.TIMEOUT,  # query_canceled (statement_timeout)
}
_SQLSTATE_CLASS: dict[str, ErrorCategory] = {
    "22": ErrorCategory.DATA,  # data_exception
    "28": ErrorCategory.PERMISSION,  # invalid_authorization_specification
}


def category_for_sqlstate(state: str | None) -> ErrorCategory:
    """Map a five-character SQLSTATE onto a category; ``OTHER`` when it says nothing specific."""
    if not state or len(state) != 5:
        return ErrorCategory.OTHER
    state = state.upper()
    return _SQLSTATE_EXACT.get(state) or _SQLSTATE_CLASS.get(state[:2], ErrorCategory.OTHER)


def exception_chain(exc: BaseException) -> list[BaseException]:
    """*exc* followed by its ``__cause__``/``__context__`` ancestors, each once."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return chain


def classify_chain(
    exc: BaseException, per_error: Callable[[BaseException], ErrorCategory | None]
) -> ErrorCategory:
    """Walk ``exception_chain(exc)``, returning the first non-``None``, non-``OTHER`` answer
    *per_error* gives for any ancestor; ``OTHER`` if none does.

    The single try/except around the whole walk is the adapters' never-raise guarantee: a
    hostile or unfamiliar exception shape (unhashable ``errno``, a ``__str__`` that raises)
    must degrade the hint, never crash query error handling.
    """
    try:
        for err in exception_chain(exc):
            category = per_error(err)
            if category is not None and category is not ErrorCategory.OTHER:
                return category
        return ErrorCategory.OTHER
    except Exception:
        return ErrorCategory.OTHER


# PostgreSQL reports 42883 (undefined_function) for a misspelled function, a function from
# another dialect and a real function called with the wrong argument types alike, so a
# *function* message stays UNKNOWN_NAME. Only an *operator* message ("operator does not
# exist: integer + text") is refined: a missing operator is almost always a type problem.
_UNDEFINED_OPERATOR = re.compile(r"^operator does not exist", re.IGNORECASE)


def refine_undefined_function(message: str) -> ErrorCategory:
    """Sharpen a PostgreSQL 42883 to TYPE_MISMATCH for an operator; else UNKNOWN_NAME."""
    if _UNDEFINED_OPERATOR.search(message or ""):
        return ErrorCategory.TYPE_MISMATCH
    return ErrorCategory.UNKNOWN_NAME


def classify_builtin(exc: BaseException) -> ErrorCategory:
    """TIMEOUT for a standard-library timeout at *exc* or one link below it; ``OTHER`` otherwise.

    Looks no deeper on purpose: a connect timeout surfaces several links down a network
    stack, and "the query exceeded the time limit" is the wrong advice for it. Not a driver
    claim, so it is the fallback after an adapter's own answer, never ahead of it.
    """
    try:
        near = [exc, exc.__cause__ or exc.__context__]
        if any(isinstance(err, TimeoutError) for err in near):
            return ErrorCategory.TIMEOUT
    except Exception:
        return ErrorCategory.OTHER
    return ErrorCategory.OTHER
