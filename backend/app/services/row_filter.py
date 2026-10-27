# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""The connection's row filter: wrapping every executed statement in it, unwrapping stored
SQL, and validating the filter an admin saves.

The wrap is ``SELECT * FROM (\\n<query>\\n) AS _sift_q WHERE <filter><trailing LIMIT>``.
Parenthesis depth of the wrapped query, as each engine lexes it, lives in
``row_filter_scan``.
"""

from __future__ import annotations

import re

import sqlparse
import sqlparse.tokens as sql_tokens

from ..datasources.models import PrivacySettings
from .row_filter_scan import parentheses_balance


class RowFilterRejectedError(ValueError):
    """The query cannot be enclosed by the row-filter wrap, so it is never sent.

    Raised before execution and classified as ``ErrorCategory.SYNTAX``: on the chat paths
    it feeds the self-correction loop like any rejected statement, and its text is our own.
    """


class RowFilterInvalidError(ValueError):
    """The text an admin saved as the row filter cannot be used as one.

    The message completes the sentence "Row filter …" for the admin.
    """


_MAX_ROW_FILTER_LENGTH = 1000
_LEADING_WHERE = re.compile(r"^WHERE\b", re.IGNORECASE)


def validate_row_filter(text: str | None, dialect: str) -> str | None:
    """*text* stripped, ready to be stored as ``row_filter_sql``; ``None`` when it is empty
    (no filter).

    Raises ``RowFilterInvalidError`` when the text cannot be the single condition that
    ``_inject_row_filter`` appends after ``WHERE``: a ``;``, a comment start (``--``,
    ``/*``, and ``#`` unless the dialect is PostgreSQL), a ``$`` (dollar quoting on
    PostgreSQL, unsure lexing on MySQL), a leading ``WHERE`` (the word, so ``WHERE(…)``
    too), parentheses that do not balance as *dialect* lexes them, or more than
    ``_MAX_ROW_FILTER_LENGTH`` characters.
    The checks are textual and fail closed: a ``;`` inside a string literal is rejected
    too.
    """
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    if ";" in text:
        raise RowFilterInvalidError("must be a single condition without ';'")
    comment_starts = ("--", "/*") if (dialect or "").lower() == "postgresql" else ("--", "/*", "#")
    if any(mark in text for mark in comment_starts):
        raise RowFilterInvalidError("must not contain comment markers, even inside quotes")
    if "$" in text:
        raise RowFilterInvalidError("must not contain '$'")
    if _LEADING_WHERE.match(text):
        raise RowFilterInvalidError("is a condition: write it without the leading WHERE")
    if not _parentheses_balance(text, dialect):
        raise RowFilterInvalidError(
            "has parentheses that do not balance, or a string that cannot be checked safely"
            " (a backslash or an unterminated quote)"
        )
    if len(text) > _MAX_ROW_FILTER_LENGTH:
        raise RowFilterInvalidError(f"is too long ({_MAX_ROW_FILTER_LENGTH} characters max)")
    return text


def _parentheses_balance(sql: str, dialect: str) -> bool:
    """Whether *sql*'s parentheses balance as *dialect*'s engine lexes them; ``False`` when
    the scanner cannot lex a construct with certainty. See ``row_filter_scan``."""
    return parentheses_balance(sql, dialect)


# Trailing row-limiting clauses, per engine: numbers only, anchored at the end of the
# right-stripped statement. The matched text is hoisted verbatim (whitespace and case kept).
_PG_TRAILING_LIMIT = re.compile(
    r"(\s+LIMIT\s+\d+(?:\s+OFFSET\s+\d+)?|\s+OFFSET\s+\d+\s+LIMIT\s+\d+)$", re.IGNORECASE
)
_MYSQL_TRAILING_LIMIT = re.compile(
    r"(\s+LIMIT\s+\d+(?:\s+OFFSET\s+\d+)?|\s+LIMIT\s+\d+\s*,\s*\d+)$", re.IGNORECASE
)
_ANY_TRAILING_LIMIT = re.compile(
    r"(\s+LIMIT\s+\d+(?:\s+OFFSET\s+\d+)?"
    r"|\s+OFFSET\s+\d+\s+LIMIT\s+\d+"
    r"|\s+LIMIT\s+\d+\s*,\s*\d+)$",
    re.IGNORECASE,
)
_TRAILING_LIMIT_BY_DIALECT = {
    "postgresql": _PG_TRAILING_LIMIT,
    "mysql": _MYSQL_TRAILING_LIMIT,
}


def _split_trailing_limit(sql: str, dialect: str) -> tuple[str, str]:
    """``(body, tail)``: *sql* without its trailing row-limiting clause, and that clause
    verbatim (``""`` when there is none). *sql* is already right-stripped of whitespace
    and ``;``.

    PostgreSQL: ``LIMIT n``, ``LIMIT n OFFSET m``, ``OFFSET m LIMIT n``. MySQL: ``LIMIT n``,
    ``LIMIT n OFFSET m``, ``LIMIT m, n``. An unrecognised dialect hoists any of them. A
    clause the engine does not have stays inside the wrap; when it precedes a recognised
    trailing ``LIMIT n`` only that ``LIMIT`` is hoisted. Either way the result has fewer rows,
    never more.
    """
    pattern = _TRAILING_LIMIT_BY_DIALECT.get((dialect or "").lower(), _ANY_TRAILING_LIMIT)
    match = pattern.search(sql)
    if not match:
        return sql, ""
    return sql[: match.start()], match.group(1)


def _inject_row_filter(sql: str, row_filter_sql: str, dialect: str) -> str:
    """Enforce a mandatory row-level filter by wrapping the query in a derived table.

    The query goes on its own lines inside the parentheses, so a trailing ``-- note`` (or
    MySQL ``#`` comment) ends before the closing ``)``. A trailing ``LIMIT``/``OFFSET`` clause
    in one of the engine's shapes (``_split_trailing_limit``) is hoisted outside so the filter
    applies before row truncation. Safe for CTEs: all SQL dialects supported by Savvina allow
    inline CTEs in subqueries.

    The filter is applied to the query's *output*, so the query must project every column
    the filter names (``row_filter_sql = "tenant_id = 7"`` needs ``tenant_id`` in the SELECT
    list). One that does not fails at the database: fail-closed by design.

    A query whose parentheses do not balance (``SELECT * FROM t) AS a --``) would close the
    wrap early and leave the rest of the statement outside the filter, so it is rejected
    with ``RowFilterRejectedError`` and never sent. The check lexes as *dialect*'s engine
    does (``row_filter_scan``) and fails closed on anything it cannot read with certainty:
    a backslash in a string literal, a MySQL executable comment, an unterminated string or
    comment. An unrecognised dialect must satisfy both the PostgreSQL and MySQL rules.

    Trust model: ``row_filter_sql`` is sourced exclusively from the connection's
    privacy settings, which are written by an authenticated org-admin through the
    settings UI.  It is never derived from user-supplied chat input, so the
    injection risk is limited to a compromised admin account.
    """
    sql = sql.rstrip().rstrip(";").rstrip()
    sql, limit_clause = _split_trailing_limit(sql, dialect)
    if not _parentheses_balance(sql, dialect):
        raise RowFilterRejectedError(
            "The query cannot be run under the connection's row filter: its parentheses"
            " do not balance, or it uses a construct the filter cannot safely enclose (a"
            " backslash in a string literal, an executable comment, or an unterminated"
            " string or comment)."
        )
    return f"SELECT * FROM (\n{sql}\n) AS _sift_q WHERE {row_filter_sql}{limit_clause}"  # noqa: S608


_WRAP_HEAD = "SELECT * FROM ("
_WRAP_TAIL = " AS _sift_q WHERE "
# At unwrap time the dialect is unknown: every shape ever hoisted is put back.
_HOISTED_LIMIT = _ANY_TRAILING_LIMIT


def _closing_paren(sql: str, open_at: int) -> int | None:
    """Offset of the ``)`` that closes the ``(`` at *open_at*, as sqlparse lexes *sql* (the
    lexing ``_inject_row_filter`` has always used); ``None`` when nothing closes it."""
    depth = 0
    offset = open_at
    for ttype, value in sqlparse.lexer.tokenize(sql[open_at:]):
        if ttype in sql_tokens.Punctuation:
            if value == "(":
                depth += 1
            elif value == ")":
                depth -= 1
                if depth == 0:
                    return offset
        offset += len(value)
    return None


def _lexed_balanced(sql: str) -> bool:
    depth = 0
    for ttype, value in sqlparse.lexer.tokenize(sql):
        if ttype in sql_tokens.Punctuation and value in "()":
            depth += 1 if value == "(" else -1
            if depth < 0:
                return False
    return depth == 0


def _unwrap_once(sql: str) -> str | None:
    if not sql.startswith(_WRAP_HEAD):
        return None
    open_at = len(_WRAP_HEAD) - 1
    close_at = _closing_paren(sql, open_at)
    if close_at is None or not sql.startswith(_WRAP_TAIL, close_at + 1):
        return None
    body = sql[open_at + 1 : close_at]
    if body.endswith("\n"):
        # Current form: the query on its own lines. It was right-stripped, so it neither
        # is empty nor ends in whitespace.
        if not body.startswith("\n") or len(body) < 3:
            return None
        body = body[1:-1]
    if not body.strip() or body != body.rstrip():
        # The pre-Task-9 form held the right-stripped query directly.
        return None
    rest = sql[close_at + 1 + len(_WRAP_TAIL) :]
    limit = _HOISTED_LIMIT.search(rest)
    limit_clause = limit.group(0) if limit else ""
    row_filter = rest[: limit.start()] if limit else rest
    if not row_filter.strip() or not _lexed_balanced(row_filter):
        return None
    return body + limit_clause


def unwrap_row_filter(sql: str) -> str:
    """The query *sql* was before ``_inject_row_filter`` wrapped it; *sql* itself otherwise.

    Reverses both wraps ever stored: the current ``SELECT * FROM (\\n<sql>\\n) AS _sift_q
    WHERE <filter>`` and the pre-Task-9 ``SELECT * FROM (<sql>) AS _sift_q WHERE <filter>``,
    each with the hoisted ``LIMIT``/``OFFSET`` clause moved back. Any filter is accepted, since
    the admin may have changed it since the row was stored, but the shape must match exactly:
    the text starts with the wrap's head, the parenthesis that opens it closes right before
    `` AS _sift_q WHERE ``, and the filter is non-empty with balanced parentheses. A
    wrapped wrap (an old cache hit wrapped again) is unwrapped all the way. The trailing
    ``;`` the wrap dropped is not restored.

    Stored SQL passes through here wherever it is read for the LLM (history, few-shot
    examples) or run again (cache hit, approval, sort), so the filter's columns and
    literals never reach the model and the statement is wrapped exactly once.

    Accepted limit: a query that is itself written in the exact shape cannot be told from
    a wrap and loses its outer layer; run again, it is wrapped in the current filter.
    """
    while (inner := _unwrap_once(sql)) is not None:
        sql = inner
    return sql


def _executable(sql: str, privacy: PrivacySettings | None, dialect: str) -> str:
    """The SQL actually sent to the database: *sql* wrapped in the row filter when one is set.

    Only this form reaches the adapter. The correction prompt, the ``sql`` event, the stored
    message and the cache row all carry the clean query the model wrote — the wrapped form
    names the admin's filter columns and literals, and a corrected query must be wrapped
    again, not executed bare.
    """
    if privacy and privacy.row_filter_sql:
        return _inject_row_filter(sql, privacy.row_filter_sql, dialect)
    return sql
