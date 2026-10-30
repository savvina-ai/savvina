# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""The connection's row filters: the statement the database receives (``_executable``),
unwrapping SQL stored under the Phase 1 wrap, and validating what an admin saves.

The rewrite itself (per-table derived tables) lives in ``row_filter_rewrite``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokens import TokenType
import sqlparse
import sqlparse.tokens as sql_tokens

from ..datasources.models import PrivacySettings
from .row_filter_errors import RowFilterInvalidError, RowFilterRejectedError
from .row_filter_rewrite import apply_row_filters, parse_condition, sqlglot_dialect

__all__ = ["RowFilterInvalidError", "RowFilterRejectedError"]

_MAX_ROW_FILTER_LENGTH = 1000
_MAX_NESTING = 20
_LEADING_WHERE = re.compile(r"^WHERE\b", re.IGNORECASE)
# C0 controls except tab, LF and CR, DEL, and the lone-surrogate range (not encodable as
# UTF-8, so a stored one would raise in the length check or when the query is sent).
# PostgreSQL refuses a NUL anywhere in a query, so a stored NUL would make every query on the
# table fail.
_CONTROL_CHARACTER = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")

# Shown verbatim (no apostrophe, no quote: see RowFilterRejectedError).
_MIGRATION_MESSAGE = (
    "Row filter needs migration: this connection still has the old whole-query filter. "
    "Open the privacy settings of the connection and define per-table filters, or remove "
    "the old filter there."
)


def _parenthesis_depth(text: str, dialect: str) -> int:
    """The deepest parenthesis nesting in *text*, counted on the dialect's own sqlglot tokens:
    parentheses inside strings, quoted names and comments are not counted. The tokenizer is
    linear and does not recurse, so this keeps the sqlglot parse, at save and again at every
    execution, well inside Python's recursion limit. Text the tokenizer refuses counts as 0:
    the parse that follows rejects it anyway."""
    read = sqlglot_dialect(dialect)
    try:
        tokens = Dialect.get_or_raise(read).tokenize(text) if read else sqlglot.tokenize(text)
    except TokenError:
        return 0
    depth = deepest = 0
    for token in tokens:
        if token.token_type == TokenType.L_PAREN:
            depth += 1
            deepest = max(deepest, depth)
        elif token.token_type == TokenType.R_PAREN:
            depth -= 1
    return deepest


def validate_row_filter(text: str | None, dialect: str) -> str | None:
    """*text* stripped, ready to be stored as a row-filter condition; ``None`` when it is empty
    (no filter).

    Raises ``RowFilterInvalidError`` when the text cannot be a single condition: a ``;``, a
    comment start (``--``, ``/*``, and ``#`` unless the dialect is PostgreSQL), a ``$``
    (dollar quoting on PostgreSQL, unsure lexing on MySQL), a leading ``WHERE`` (the word,
    so ``WHERE(…)`` too), a control character (other than tab, LF, CR), more than
    ``_MAX_NESTING`` levels of parentheses, or more than ``_MAX_ROW_FILTER_LENGTH``
    characters. The checks
    are textual and fail closed: a ``;`` inside a string literal is rejected too. Whether
    the text is one well-formed expression is checked by the sqlglot parse in
    ``validate_row_filters``.
    """
    if text is None:
        return None
    if not text.strip():
        return None
    if _CONTROL_CHARACTER.search(text):  # before the strip, which would eat a trailing \x0b
        raise RowFilterInvalidError("must not contain control characters")
    text = text.strip()
    if ";" in text:
        raise RowFilterInvalidError("must be a single condition without ';'")
    comment_starts = ("--", "/*") if (dialect or "").lower() == "postgresql" else ("--", "/*", "#")
    if any(mark in text for mark in comment_starts):
        raise RowFilterInvalidError("must not contain comment markers, even inside quotes")
    if "$" in text:
        raise RowFilterInvalidError("must not contain '$'")
    if _LEADING_WHERE.match(text):
        raise RowFilterInvalidError("is a condition: write it without the leading WHERE")
    if _parenthesis_depth(text, dialect) > _MAX_NESTING:
        raise RowFilterInvalidError(f"is nested too deeply ({_MAX_NESTING} levels max)")
    if len(text) > _MAX_ROW_FILTER_LENGTH:
        raise RowFilterInvalidError(f"is too long ({_MAX_ROW_FILTER_LENGTH} characters max)")
    return text


_WRAP_HEAD = "SELECT * FROM ("
_WRAP_TAIL = " AS _sift_q WHERE "
# At unwrap time the dialect is unknown: every shape the Phase 1 wrap ever hoisted is put
# back.
_HOISTED_LIMIT = re.compile(
    r"(\s+LIMIT\s+\d+(?:\s+OFFSET\s+\d+)?"
    r"|\s+OFFSET\s+\d+\s+LIMIT\s+\d+"
    r"|\s+LIMIT\s+\d+\s*,\s*\d+)$",
    re.IGNORECASE,
)


_MAX_ROW_FILTERS = 200
_BARE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_QUOTE_BY_DIALECT = {"postgresql": '"', "mysql": "`"}
_SHAPE = "row_filters must be an object mapping schema.table to a condition"


def quote_identifier(part: str, dialect: str) -> str:
    """*part* as a quoted identifier of *dialect* (``"`` on PostgreSQL, `` ` `` on MySQL; the
    SQL-standard double quote for a dialect not in ``_QUOTE_BY_DIALECT``), the quote character
    doubled inside. For SQL shown to the admin (the privilege audit's remediation): a stored
    key part is the engine-normalised name without quotes, so quoting it always keeps its
    case."""
    quote = _QUOTE_BY_DIALECT.get((dialect or "").lower(), '"')
    return quote + part.replace(quote, quote * 2) + quote


# The longest identifier each engine keeps: PostgreSQL truncates at 63 bytes (NAMEDATALEN
# - 1), MySQL refuses more than 64 characters.
_MAX_NAME = {"postgresql": ("bytes", 63), "mysql": ("characters", 64)}


def _key_part(part: str, dialect: str) -> str | None:
    """*part* as the engine stores the name: a bare part folded as the engine folds an
    unquoted identifier (PostgreSQL lowercases, MySQL keeps the case), or the inside of a
    correctly quoted part with its case kept; ``None`` if neither.

    The stored key is thus the engine-normalised name, which the rewriter compares exactly
    with the engine-normalised reference and ``schema_utils`` compares with the cached
    table. A quoted part may not start or end with whitespace: the rewriter refuses such a
    key at every execution, so it is refused here first."""
    quote = _QUOTE_BY_DIALECT.get((dialect or "").lower())
    if quote and len(part) >= 2 and part[0] == quote and part[-1] == quote:
        inner = part[1:-1]
        ok = inner and quote not in inner and inner == inner.strip()
        ok = ok and not _CONTROL_CHARACTER.search(inner)
        return inner if ok else None
    if not _BARE_NAME.fullmatch(part):
        return None
    return part.lower() if (dialect or "").lower() == "postgresql" else part


def _too_long(name: str, dialect: str) -> bool:
    unit, limit = _MAX_NAME.get((dialect or "").lower(), ("bytes", 63))
    size = len(name.encode("utf-8")) if unit == "bytes" else len(name)
    return size > limit


def validate_row_filters(raw: object, dialect: str) -> dict[str, str]:
    """The clean per-table map, or ``RowFilterInvalidError`` naming the key and the reason.

    Keys: ``schema.table``, each part a bare name (``[A-Za-z_][A-Za-z0-9_$]*``) or a quoted
    identifier of the dialect with no quote inside, stored without the quotes; an
    unquoted part is folded as the engine folds it (PostgreSQL lowercases), a quoted part keeps
    its case, so the stored key is the engine-normalised name the rewriter compares exactly with
    the engine-normalised reference.
    Values: ``validate_row_filter``'s textual rules, then a sqlglot parse as one condition.
    Nothing is checked against the cached schema; the connections router verifies new or changed
    pairs with a live EXPLAIN (Phase 3a, section 6).
    """
    if not isinstance(raw, dict):
        raise RowFilterInvalidError(_SHAPE)
    if len(raw) > _MAX_ROW_FILTERS:
        raise RowFilterInvalidError(f"row_filters has at most {_MAX_ROW_FILTERS} entries")
    dialect_name = dialect or "SQL"
    read = sqlglot_dialect(dialect)
    if raw and read is None:
        raise RowFilterInvalidError("row_filters are not supported for this database")
    clean: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise RowFilterInvalidError("row_filters keys and conditions must be strings")
        stripped = key.strip()
        parts = stripped.split(".")
        if len(parts) != 2:
            raise RowFilterInvalidError(f'row_filters key "{stripped}" must be schema.table')
        names = [_key_part(p, dialect) for p in parts]
        if None in names:
            raise RowFilterInvalidError(f'row_filters key "{stripped}" has an invalid name')
        if any(_too_long(n, dialect) for n in names):
            raise RowFilterInvalidError(
                f'row_filters key "{stripped}" is longer than the database allows'
            )
        stored = f"{names[0]}.{names[1]}"
        if stored in clean:
            raise RowFilterInvalidError(f'row_filters key "{stored}" appears twice')
        try:
            condition = validate_row_filter(value, dialect)
        except RowFilterInvalidError as exc:
            raise RowFilterInvalidError(f'row_filters["{stored}"] {exc}') from exc
        if condition is None:
            raise RowFilterInvalidError(f'row_filters["{stored}"] is empty')
        try:
            parsed = parse_condition(condition, read)
        except (ParseError, TokenError) as exc:  # TokenError is not a ParseError
            raise RowFilterInvalidError(
                f'row_filters["{stored}"] is not a valid condition for {dialect_name}'
            ) from exc
        except RecursionError as exc:
            raise RowFilterInvalidError(f'row_filters["{stored}"] is nested too deeply') from exc
        if parsed.find(exp.Placeholder, exp.Parameter):
            raise RowFilterInvalidError(
                f'row_filters["{stored}"] must not contain a placeholder; write the value itself'
            )
        if parsed.find(exp.Insert, exp.Update, exp.Delete, exp.Merge):
            raise RowFilterInvalidError(f'row_filters["{stored}"] must not modify data')
        clean[stored] = condition
    return clean


def _closing_paren(sql: str, open_at: int) -> int | None:
    """Offset of the ``)`` that closes the ``(`` at *open_at*, as sqlparse lexes *sql* (the
    lexing the Phase 1 wrap has always used); ``None`` when nothing closes it."""
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
    """The query *sql* was before the Phase 1 wrap wrapped it; *sql* itself otherwise.

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
    """The SQL actually sent to the database: *sql* with every filtered table reference
    rewritten (``apply_row_filters``) when the connection has row filters.

    Only this form reaches the adapter. The correction prompt, the ``sql`` event, the stored
    message and the cache row all carry the clean query the model wrote: the rewritten form
    names the admin's filter columns and literals, and a corrected query must be rewritten
    again, not executed bare.

    A connection in the migration state (a legacy ``row_filter_sql`` and no map) runs
    nothing: ``RowFilterRejectedError`` with ``_MIGRATION_MESSAGE``. The chat paths check
    that state before generating; this is the safety net for every other caller.
    """
    if privacy is None:
        return sql
    if privacy.row_filter_needs_migration:
        raise RowFilterRejectedError(_MIGRATION_MESSAGE)
    if privacy.row_filters:
        return apply_row_filters(sql, privacy.row_filters, dialect)
    return sql


# Domain separation for the fingerprint MAC: the encryption key is the app's one secret, so
# the message is labelled to keep this MAC distinct from any other HMAC under that key.
# Changing the label (or the version in it) retires every stored result.
_FINGERPRINT_DOMAIN = b"savvina.row_filter_fingerprint.v1\x00"


def row_filter_fingerprint(row_filters: dict[str, str], key: str) -> str:
    """``""`` for no filters, else 16 hex characters of HMAC-SHA256 under *key* over
    ``_FINGERPRINT_DOMAIN`` followed by the canonical JSON of the map (sorted keys, no
    whitespace).

    Stored with every result so a stored result can be refused when the connection's map
    has changed since (``stored_results``). Keyed, not a plain hash: the value travels in
    ``results_json`` to the history and to anyone with a share link, and a plain hash of a
    low-entropy map (``{"public.orders": "tenant_id = 7"}``) would let them confirm a guess
    of the admin's condition. *key* is the app's encryption key; rotating it makes every
    stored result stale, the fail-closed direction.
    """
    if not row_filters:
        return ""
    canonical = json.dumps(row_filters, sort_keys=True, separators=(",", ":"))
    message = _FINGERPRINT_DOMAIN + canonical.encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()[:16]
