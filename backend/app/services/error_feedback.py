# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Describe a failed query to the LLM in our own words: a category plus names it already has."""

from __future__ import annotations

import ast
import re

import sqlparse
import sqlparse.lexer
import sqlparse.tokens as sql_tokens

from ..core.identifiers import unquote_ident
from ..datasources.errors import NAME_ECHOING_CATEGORIES, ErrorCategory
from ..datasources.models import DataSourceSchema

_MAX_NAMES = 10
_MAX_DOTTED_PARTS = 4
_MAX_NAME_LENGTH = 128

# Identifier-shaped tokens, optionally dotted. Each part starts with an ASCII letter or
# underscore and continues with Unicode word characters or "$". Word boundaries keep a
# name from being cut out of a longer glued token: the match may not start right after a
# word character, "$" or "." (so "2amount", "$amount" and "ßamount" yield nothing), nor
# stop before a word character, "$" or a "." that continues into one (so "amount.9x" and
# "amount.ßx" yield nothing). A quote character is simply a boundary, like a comma or a
# space.
_NAME_TOKEN_RE = re.compile(r"(?<![\w$.])[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*(?![\w$]|\.[\w$])")

# PostgreSQL lists the called argument types in parentheses immediately before "does not
# exist" for every function call it rejects. Those are type keywords, not names, and are
# stripped before matching so they can never coincide with a same-named schema column.
_FUNCTION_ARG_LIST_RE = re.compile(r"\([^()]*\)(?=\s+does not exist)", re.IGNORECASE)

# Categories where a schema-only name (never written by the LLM, only present in the
# schema it was shown) is a useful "did you mean" suggestion. Everywhere else it is noise.
_NAME_SUGGESTING_CATEGORIES = frozenset({ErrorCategory.UNKNOWN_NAME, ErrorCategory.AMBIGUOUS_NAME})

# Categories whose engine messages quote names (columns, tables, functions, types) and not
# stored values. Every other category can quote a value ("invalid input syntax ... "Jane"",
# "Duplicate entry 'x'", "Key (x)=(value)", RAISE text). There a name that matches the
# error text is almost always the value itself, so echoing it would be an oracle: the
# LLM could probe candidate values by writing them as aliases and watching which come back.
# PostgreSQL also reports a stored value under these codes (``ssn::regclass`` ->
# ``relation "123456789" does not exist``); its adapter classifies such an error, which
# has no position in the query, as DATA.
_NAME_ECHOING_CATEGORIES = NAME_ECHOING_CATEGORIES

_CATEGORY_SENTENCE: dict[ErrorCategory, str] = {
    ErrorCategory.UNKNOWN_NAME: (
        "A column, table, schema or function name in the query does not exist, or a "
        "function was called with argument types it does not accept."
    ),
    ErrorCategory.AMBIGUOUS_NAME: "A column or alias reference in the query is ambiguous.",
    ErrorCategory.SYNTAX: "The query has a syntax error for this SQL dialect.",
    ErrorCategory.GROUPING: (
        "A selected column is neither grouped nor aggregated, or an aggregate is used "
        "where it is not allowed."
    ),
    ErrorCategory.TYPE_MISMATCH: "An operator or function was applied to incompatible types.",
    ErrorCategory.DATA: (
        "A value stored in the database could not be converted, parsed or fitted into the "
        "type the query asks for."
    ),
    ErrorCategory.DIVISION_BY_ZERO: "The query divides by zero.",
    ErrorCategory.CARDINALITY: "A subquery used as a single value returned more than one row.",
    ErrorCategory.PERMISSION: "The database denied access to an object the query uses.",
    ErrorCategory.UNSUPPORTED: (
        "The database refused the statement: it uses a feature this database does not "
        "support, or it is not a read-only SELECT."
    ),
    ErrorCategory.TIMEOUT: "The query exceeded the time limit.",
    ErrorCategory.OTHER: "The database rejected the query.",
    ErrorCategory.REJECTED: (
        "The query was refused by this application's own checks before it reached the database."
    ),
}

_WITHHELD = (
    "The database's own message is withheld because it can quote values stored in the tables."
)

_REDACTED = "[REDACTED]"
# A PostgreSQL key detail, "Key (email)=(a@b.com) already exists": greedy to the last ")" on
# the line, so a value holding parentheses or quotes is covered whole (over-redacting is the
# safe direction).
_KEY_VALUE_RE = re.compile(r"=\(.*\)")
# Single- or double-quoted spans with doubled-quote escapes. An unterminated span (a message
# cut short) runs to the end of the line. Backtick spans are identifiers and are left alone.
_QUOTED_RE = re.compile(r"'(?:[^']|'')*(?:'|$)|\"(?:[^\"]|\"\")*(?:\"|$)")
# PostgreSQL's operator lookup by name (``regoper`` input) echoes the looked-up text
# unquoted. Under a value-quoting category that text is a stored value: the tail goes.
_UNQUOTED_OPERATOR_RE = re.compile(
    r"^(operator does not exist: |more than one operator named ).+", re.IGNORECASE
)

# pymysql formats every error as ``str((errno, message))``: the message is a Python repr
# (``(1292, "Truncated incorrect DOUBLE value: 'abc'")``). Redacting the quoted spans of
# that text would wipe the whole message, so it is unwrapped first.
# After the unwrap only quoted spans, ``=(…)`` details and the operator tail are redacted, so
# a value MySQL echoes without quotes would be shown (MySQL normally quotes values).
_DRIVER_TUPLE_RE = re.compile(r"^\((\d+), (.*)\)$", re.DOTALL)


def _driver_message(line: str) -> str:
    """The first line of the message inside a pymysql ``(errno, 'message')`` line.

    *line* itself when it is not such a tuple. The repr escapes newlines, so a ``DETAIL:``
    line inside the message only shows once unwrapped; it is dropped like any other second
    line. An empty first line gives ``""`` (the caller falls back to the category sentence).
    """
    match = _DRIVER_TUPLE_RE.match(line)
    if match is None:
        return line
    try:
        message = ast.literal_eval(match.group(2))
    except Exception:  # any text literal_eval cannot build (TypeError too) stays as written
        return line
    if not isinstance(message, str):
        return line
    lines = message.splitlines()
    return lines[0].strip() if lines else ""


def redact_error_for_user(category: ErrorCategory, error: str) -> str:
    """The execution error as the user may see it.

    Name-quoting categories (``_NAME_ECHOING_CATEGORIES``) keep the engine's text: it quotes
    names from the user's own SQL. Every other category can quote a stored value and the
    engine does not say which column it came from, so only the first line is kept
    (``DETAIL:``/``HINT:`` lines can carry unquoted values), a pymysql ``(errno, 'message')``
    line is reduced to its message, every quoted span becomes ``[REDACTED]`` and a key
    detail ``=(…)`` becomes ``=([REDACTED])``. Never empty: the category's own sentence
    stands in. The raw text is not logged: callers log this result.
    """
    if category in _NAME_ECHOING_CATEGORIES:
        return error
    lines = error.splitlines()
    first = lines[0].strip() if lines else ""
    first = _driver_message(first)
    first = _KEY_VALUE_RE.sub(f"=({_REDACTED})", first)
    first = _UNQUOTED_OPERATOR_RE.sub(rf"\g<1>{_REDACTED}", first)
    first = _QUOTED_RE.sub(_REDACTED, first)
    return first or _CATEGORY_SENTENCE[category]


def _sql_names(sql: str) -> dict[str, str]:
    """casefold -> canonical spelling, from identifier tokens the LLM itself wrote.

    ``ttype is sql_tokens.Name`` (not ``in``) is deliberate: ``in`` also admits
    ``Name.Builtin``, which is how sqlparse tags a bare type keyword in a CAST (``AS int``).
    """
    names: dict[str, str] = {}
    for ttype, value in sqlparse.lexer.tokenize(sql[:65536]):
        if ttype is sql_tokens.Name or ttype is sql_tokens.Literal.String.Symbol:
            name = unquote_ident(value)
            names.setdefault(name.casefold(), name)
    return names


def _schema_names(schema: DataSourceSchema | None) -> dict[str, str]:
    """casefold -> canonical spelling, from the privacy-filtered schema the LLM was shown."""
    names: dict[str, str] = {}
    if schema is None:
        return names
    for entry in schema.schemas:
        names[entry.name.casefold()] = entry.name
    for table in schema.tables:
        names[table.name.casefold()] = table.name
        if table.schema_name:
            names[table.schema_name.casefold()] = table.schema_name
        for col in table.columns:
            names[col.name.casefold()] = col.name
    return names


def _canonical_names(
    sql: str, schema: DataSourceSchema | None, category: ErrorCategory
) -> dict[str, str]:
    """casefold -> preferred spelling, eligible for this error category.

    A name the LLM wrote is always eligible. A name that exists only in the schema is
    added only for the categories where a "did you mean" helps; the schema's spelling wins
    on a collision, since the schema is the authoritative source.
    """
    canonical = _sql_names(sql)
    if category in _NAME_SUGGESTING_CATEGORIES:
        canonical = {**canonical, **_schema_names(schema)}
    return {name: spelling for name, spelling in canonical.items() if len(name) >= 2}


def mentioned_names(
    error: str,
    *,
    sql: str,
    schema: DataSourceSchema | None,
    category: ErrorCategory = ErrorCategory.OTHER,
) -> list[str]:
    """Names in *error* that the LLM already has, canonically spelled, first-appearance order.

    No engine text is copied: the output is limited to names the LLM wrote in its SQL or,
    for the suggesting categories, was shown in the schema, and only for categories whose
    messages quote names (``_NAME_ECHOING_CATEGORIES``); any other category returns ``[]``.
    A token is listed only when every dotted part is a known name. A chain longer than
    ``_MAX_DOTTED_PARTS`` or ``_MAX_NAME_LENGTH`` is rejected outright rather than truncated.
    """
    if category not in _NAME_ECHOING_CATEGORIES:
        return []
    canonical = _canonical_names(sql, schema, category)
    found: list[str] = []
    error = _FUNCTION_ARG_LIST_RE.sub("", error[:65536])
    for match in _NAME_TOKEN_RE.finditer(error):
        token = match.group(0)
        if len(token) > _MAX_NAME_LENGTH:
            continue
        parts = token.split(".")
        if len(parts) > _MAX_DOTTED_PARTS:
            continue
        folded = [part.casefold() for part in parts]
        if not all(part in canonical for part in folded):
            continue
        resolved = ".".join(canonical[part] for part in folded)
        if resolved not in found:
            found.append(resolved)
            if len(found) == _MAX_NAMES:
                break
    return found


def describe_error_for_llm(
    category: ErrorCategory,
    error: str,
    *,
    sql: str,
    schema: DataSourceSchema | None,
    hint: str | None = None,
) -> str:
    """The execution-error text for a correction prompt, composed only from known parts.

    *error* — the engine's text — is read for names and never copied, except for
    ``REJECTED``, where it is this application's own sentence and is passed on as it is.
    *hint* is our own sentence for a rejection the model can act on
    (``QueryRejectedError.llm_hint``).
    """
    if category is ErrorCategory.REJECTED:
        # Our own refusal (QueryRejectedError): the text names columns from the query or
        # the settings and never a value, so the model reads it as written.
        parts = [error.strip() or _CATEGORY_SENTENCE[category]]
    else:
        parts = [_CATEGORY_SENTENCE[category], _WITHHELD]
        names = mentioned_names(error, sql=sql, schema=schema, category=category)
        if names:
            parts.append("Names the error refers to: " + ", ".join(names) + ".")
    if hint:
        parts.append(hint)
    return " ".join(parts)
