# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Which columns of a query result are ``[REDACTED]``, decided from the SQL that produced it.

Name matching alone is defeated by a rename: ``SELECT email AS contact`` carries the same
values under a name no sensitive pattern matches. So the SELECT list is classified with
sqlparse and a sensitive or excluded column stays masked through an alias, an expression
over it, a CTE, a derived table, a scalar subquery or a set-operation branch. Only
statistical aggregates without ``OVER`` (``COUNT``, ``AVG``, …) and ranking window
functions are exempt; value-returning aggregates (``MIN``, ``MAX``, ``STRING_AGG``, …)
return stored values and are masked like the column itself.

Name matching (a sensitive pattern, or a bare name from ``excluded_columns``) applies as
well. The one thing classification takes away from it is an exempt output: ``COUNT(email)
AS email`` is a statistic, not an address, and is not masked for its name.

The classifier fails closed: when it cannot parse the statement and the SQL names a
sensitive or excluded column, every column is masked (``MASK_ALL_COLUMNS``), and so is
every column when a name it classified as masked is missing from the driver's column list and no
per-position classification lines up with it.

Entry points: ``_plan_result_mask`` builds one ``_ResultMaskPlan`` per executed statement
(classification is the costly part); ``plan.redact(rows, columns)`` is applied to every
streamed ``row_batch`` and to the final payload. ``_mask_sensitive_result_columns`` is the
one-shot form for a complete ``QueryResultsResponse``. ``services/stored_results.py``
re-masks stored results on every read, with a plan built the same way from the stored SQL
and the connection's current privacy settings.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import functools
import logging
import re
from typing import Any

import sqlparse
from sqlparse.sql import Comment, Function, Identifier, IdentifierList, Parenthesis, Where
import sqlparse.tokens as sql_tokens
from sqlparse.utils import remove_quotes

from ..core.identifiers import ident_leaf, strip_ident_quotes, unquote_ident
from ..datasources.models import PrivacySettings
from ..schemas.chat import QueryResultsResponse

logger = logging.getLogger(__name__)


# Aggregates whose result is a statistic, not a stored value: over a sensitive column they
# are exempt from masking (and from the excluded-column backstop on their alias).
#
# Value-returning aggregates (MIN, MAX, string_agg, array_agg, json_agg, any_value, mode,
# percentile_disc, median …) are deliberately absent: each returns one or more values exactly
# as stored, so ``MAX(email)`` is an email address and is masked like the column itself.
#
# Accepted, documented limits: a statistic over a single row is that row's value, so
# ``SUM(salary) … GROUP BY employee_id`` (or any grouping on a unique key) still reveals
# individual values, and so does ``SUM(salary) FILTER (WHERE id = 1)``. Masking cannot tell
# such a grouping or filter from an aggregate over many rows. A windowed aggregate
# (``SUM(salary) OVER (…)``) is not exempt at all: see ``_is_aggregate_expr``.
_STATISTICAL_AGGREGATE_FUNCTIONS: frozenset[str] = frozenset(
    {
        "count",
        "count_if",
        "approx_count_distinct",
        "sum",
        "avg",
        "stddev",
        "stddev_pop",
        "stddev_samp",
        "variance",
        "var_pop",
        "var_samp",
        "corr",
        "covar_pop",
        "covar_samp",
        "regr_slope",
        "regr_intercept",
        "percentile_cont",
        "approx_percentile",
        "bool_and",
        "bool_or",
        "every",
    }
)

# Value-returning aggregates: not exempt, listed so the split stays explicit and tested.
# Nothing consults this set at runtime; anything outside the statistical set is classified
# like any other expression.
_VALUE_AGGREGATE_FUNCTIONS: frozenset[str] = frozenset(
    {
        "min",
        "max",
        "group_concat",
        "array_agg",
        "string_agg",
        "listagg",
        "percentile_disc",
        "median",
        "mode",
        "bit_and",
        "bit_or",
        "json_agg",
        "jsonb_agg",
        "json_arrayagg",
        "json_objectagg",
        "any_value",
        "first",
        "last",
    }
)

# Ranking window functions emit a rank/position/bucket, never the underlying value, so a
# sensitive column can only appear in their OVER clause. Value-returning window functions
# (LAG, LEAD, FIRST_VALUE, LAST_VALUE, NTH_VALUE) are deliberately absent: they surface the
# value and must stay masked.
_RANKING_WINDOW_FUNCTIONS: frozenset[str] = frozenset(
    {"rank", "dense_rank", "row_number", "ntile", "percent_rank", "cume_dist"}
)


def _outermost_call_name(token: object) -> str | None:
    """Return the lowercased name of the outermost function call in *token*, if any."""
    if isinstance(token, Function):
        name = token.get_name()
        return name.lower() if name else None
    if isinstance(token, Identifier):
        inner = token.tokens[0] if token.tokens else None
        if isinstance(inner, Function):
            name = inner.get_name()
            return name.lower() if name else None
    return None


@functools.lru_cache(maxsize=64)
def _excluded_names_for(entries: tuple[str, ...]) -> frozenset[str]:
    return frozenset(unquote_ident(ident_leaf(strip_ident_quotes(c))).lower() for c in entries)


def _excluded_bare_names(privacy: PrivacySettings | None) -> frozenset[str]:
    """Bare, folded column names from ``privacy.excluded_columns`` — the one source for both
    masking sites (final payload and streamed row batches), which must agree."""
    if not privacy:
        return frozenset()
    return _excluded_names_for(tuple(privacy.excluded_columns))


def _is_sensitive_name(privacy: PrivacySettings, value: str) -> bool:
    """A column name whose values are masked: a sensitive pattern or an excluded column."""
    return privacy.is_column_sensitive(value) or value.lower() in _excluded_bare_names(privacy)


def _is_windowed(token: object) -> bool:
    return any(
        t.ttype is sql_tokens.Keyword and t.normalized.upper() == "OVER"
        for t in token.flatten()  # type: ignore[attr-defined]
    )


def _is_word(tok: object, word: str) -> bool:
    ttype = tok.ttype  # type: ignore[attr-defined]
    return (
        ttype is not None
        and (ttype in sql_tokens.Keyword or ttype in sql_tokens.Name)
        and tok.value.upper() == word  # type: ignore[attr-defined]
    )


def _is_punct(tok: object, value: str) -> bool:
    return tok.ttype is sql_tokens.Punctuation and tok.value == value  # type: ignore[attr-defined]


def _after_parens(leaves: list, k: int) -> int | None:
    """The index after the parenthesised group opening at ``leaves[k]``, or ``None``."""
    if k >= len(leaves) or not _is_punct(leaves[k], "("):
        return None
    depth = 0
    for j in range(k, len(leaves)):
        if _is_punct(leaves[j], "("):
            depth += 1
        elif _is_punct(leaves[j], ")"):
            depth -= 1
            if depth == 0:
                return j + 1
    return None


def _is_name_leaf(tok: object) -> bool:
    ttype = tok.ttype  # type: ignore[attr-defined]
    return ttype is not None and (
        ttype in sql_tokens.Name or ttype in sql_tokens.Keyword or ttype is sql_tokens.String.Symbol
    )


def _dotted_name_end(leaves: list, k: int) -> int | None:
    """The index after a (dotted) name ``a``, ``s.a``, ``"S"."a"`` at ``leaves[k]``."""
    if k >= len(leaves) or not _is_name_leaf(leaves[k]):
        return None
    while k + 2 < len(leaves) and _is_punct(leaves[k + 1], ".") and _is_name_leaf(leaves[k + 2]):
        k += 2
    return k + 1


def _call_end(leaves: list, k: int, names: frozenset[str], *, over: bool) -> int | None:
    """The index after ``name(…)`` at ``leaves[k]``, *name* (unqualified or qualified by
    ``pg_catalog``) in *names*, and its own ``FILTER (…)``, ``WITHIN GROUP (…)`` and, with
    *over*, ``OVER …`` clauses; ``None`` when ``leaves[k]`` does not start such a call."""
    j = _dotted_name_end(leaves, k)
    if j is None or unquote_ident(leaves[j - 1].value).lower() not in names:
        return None
    # Unqualified or ``pg_catalog.``: any other qualifier may name a user-defined function
    # that returns stored values.
    if j - 1 > k and not (j - 1 == k + 2 and unquote_ident(leaves[k].value) == "pg_catalog"):
        return None
    pos = _after_parens(leaves, j)
    while pos is not None and pos < len(leaves):
        tok = leaves[pos]
        if _is_word(tok, "WITHIN") and pos + 1 < len(leaves) and _is_word(leaves[pos + 1], "GROUP"):
            pos = _after_parens(leaves, pos + 2)
        elif _is_word(tok, "FILTER"):
            pos = _after_parens(leaves, pos + 1)
        elif over and _is_word(tok, "OVER"):
            nxt = leaves[pos + 1] if pos + 1 < len(leaves) else None
            if nxt is not None and nxt.ttype in sql_tokens.Name:
                pos += 2  # ``OVER w``: a named window
            else:
                pos = _after_parens(leaves, pos + 1)
        else:
            break
    return pos


# Type names of more than one word a cast of a statistic may name.
_MULTI_WORD_TYPES: tuple[tuple[str, ...], ...] = (
    ("double", "precision"),
    ("character", "varying"),
    ("bit", "varying"),
    ("time", "with", "time", "zone"),
    ("time", "without", "time", "zone"),
    ("timestamp", "with", "time", "zone"),
    ("timestamp", "without", "time", "zone"),
)


def _type_end(leaves: list, k: int) -> int | None:
    """The index after a type name at ``leaves[k]``: a (dotted, quoted) name or a listed
    multi-word type, then optional integer modifiers ``(10, 2)``, then optional ``[]``
    pairs. ``None`` for anything else."""
    end: int | None = None
    words: list[str] = []
    for j in range(k, len(leaves)):  # sqlparse reads ``double precision`` as one token
        ttype = leaves[j].ttype
        if ttype is None or not (ttype in sql_tokens.Name or ttype in sql_tokens.Keyword):
            break
        words.extend(leaves[j].value.lower().split())
        if tuple(words) in _MULTI_WORD_TYPES:
            end = j + 1
        if not any(t[: len(words)] == tuple(words) for t in _MULTI_WORD_TYPES):
            break
    if end is None:
        end = _dotted_name_end(leaves, k)
        if end is None:
            return None
    if end < len(leaves) and _is_punct(leaves[end], "("):  # ``(10, 2)``: integers only
        j = end + 1
        while True:
            if j + 1 >= len(leaves) or leaves[j].ttype not in sql_tokens.Literal.Number.Integer:
                return None
            if _is_punct(leaves[j + 1], ")"):
                end = j + 2
                break
            if not _is_punct(leaves[j + 1], ","):
                return None
            j += 2
    while end + 1 < len(leaves) and _is_punct(leaves[end], "[") and _is_punct(leaves[end + 1], "]"):
        end += 2
    return end


def _statistic_end(leaves: list, k: int) -> int | None:
    """The index after a whole statistic at ``leaves[k]``: a statistical aggregate call with
    its own ``FILTER``/``WITHIN GROUP`` and any ``::type`` casts, ``CAST(<statistic> AS
    <type>)``, or ``ROUND(<statistic>[, <integer>])``. ``None`` for anything else."""
    if k + 1 < len(leaves) and _is_punct(leaves[k + 1], "("):
        if _is_word(leaves[k], "CAST"):
            inner = _statistic_end(leaves, k + 2)
            if inner is None or inner >= len(leaves) or not _is_word(leaves[inner], "AS"):
                return None
            end = _type_end(leaves, inner + 1)
            if end is None or end >= len(leaves) or not _is_punct(leaves[end], ")"):
                return None
            return end + 1
        if _is_word(leaves[k], "ROUND"):
            inner = _statistic_end(leaves, k + 2)
            if inner is None or inner >= len(leaves):
                return None
            if _is_punct(leaves[inner], ")"):
                return inner + 1
            if (
                inner + 2 < len(leaves)
                and _is_punct(leaves[inner], ",")
                and leaves[inner + 1].ttype in sql_tokens.Literal.Number.Integer
                and _is_punct(leaves[inner + 2], ")")
            ):
                return inner + 3
            return None
    pos = _call_end(leaves, k, _STATISTICAL_AGGREGATE_FUNCTIONS, over=False)
    while pos is not None and pos < len(leaves) and _is_punct(leaves[pos], "::"):
        pos = _type_end(leaves, pos + 1)
    return pos


def _item_leaves(item: object) -> list:
    """*item*'s significant leaf tokens without its alias (and the ``AS`` before it)."""
    leaves = [
        t
        for t in item.flatten()  # type: ignore[attr-defined]
        if not t.is_whitespace and t.ttype not in sql_tokens.Comment
    ]
    alias = _alias_leaf(item)
    if alias is not None and leaves and leaves[-1] is alias:
        leaves.pop()
        if leaves and _is_word(leaves[-1], "AS"):
            leaves.pop()
    return leaves


def _is_whole_call(item: object, names: frozenset[str]) -> bool:
    """*item* is, apart from its alias, one call of a function in *names* followed by nothing
    but its own ``FILTER (…)``, ``WITHIN GROUP (…)`` or ``OVER …`` clauses.

    The exemption is for the call's value alone: with anything else at the item's top level
    (``|| string_agg(email, ',')``, ``+ 1``, ``::text``) the item is an expression over the
    call and is classified like any other. Read on the leaf tokens, since sqlparse groups an
    item followed by ``FILTER``/``WITHIN GROUP`` into token runs that hold the rest of it.
    """
    if _outermost_call_name(item) not in names:
        return False
    leaves = _item_leaves(item)
    return _call_end(leaves, 0, names, over=True) == len(leaves)


def _is_ranking_window_expr(token: object) -> bool:
    return _is_whole_call(token, _RANKING_WINDOW_FUNCTIONS)


def _is_aggregate_expr(token: object) -> bool:
    """A statistical aggregate over a group, as the whole item (``_statistic_end``): the call
    with its own ``FILTER``/``WITHIN GROUP``, optionally cast (``::numeric(10, 2)``,
    ``CAST(… AS int)``) or rounded to an integer number of places (``ROUND(…, 2)``), and
    nothing else apart from its alias. A cast or a rounding of a statistic is still that
    statistic. With ``OVER (…)`` anywhere it is a window function: a running total or a
    one-row partition returns single values, so it is not exempt."""
    if _is_windowed(token):
        return False
    leaves = _item_leaves(token)
    return _statistic_end(leaves, 0) == len(leaves)


def _token_references_sensitive(
    token: object,
    privacy: PrivacySettings,
    *,
    double_quote_is_string_literal: bool = False,
) -> bool:
    """True if any identifier token inside *token* names a sensitive or excluded column.

    A bare ``salary`` is a ``Name``. A double-quoted ``"salary"`` is ``String.Symbol``:
    sqlparse cannot know whether that is an identifier or a string literal, but the adapter
    can (``double_quote_is_string_literal``). A backtick-quoted one is a ``Name`` carrying
    its delimiters; every token is tested unquoted so an anchored pattern can match it.
    """
    flat = token.flatten() if hasattr(token, "flatten") else []
    for tok in flat:
        is_reference = tok.ttype is sql_tokens.Name or (
            tok.ttype is sql_tokens.String.Symbol and not double_quote_is_string_literal
        )
        if is_reference and _is_sensitive_name(privacy, unquote_ident(tok.value)):
            return True
    return False


_OUTPUT_NAME_TTYPES = (sql_tokens.Name, sql_tokens.String.Symbol, sql_tokens.Keyword)


def _raw_output_token(token: Identifier, stripped: str) -> str | None:
    """Return the raw token that sqlparse reduced to *stripped*, or ``None``.

    ``get_name()``/``get_alias()`` run each candidate through ``remove_quotes``; applying
    it to every leaf and comparing recovers the raw spelling (delimiters and doubling).
    The **last** match wins: the output name is the rightmost leaf — the column of a dotted
    path, or the alias when one was given.
    """
    hit: str | None = None
    for tok in token.flatten():
        if tok.ttype in _OUTPUT_NAME_TTYPES and remove_quotes(tok.value) == stripped:
            hit = tok.value
    return hit


def _output_alias(token: object) -> str | None:
    """Return the output column alias/name for a SELECT item token, lowercased.

    sqlparse strips one surrounding quote pair and keeps the doubling inside, so
    ``"pay""rate"`` comes back as ``pay""rate`` while the driver names the column
    ``pay"rate``. Map back to the raw token and unquote that, the same call
    ``_token_references_sensitive`` makes, so both halves agree.
    """
    if isinstance(token, _ComposedItem):
        return token.output_name
    if not isinstance(token, Identifier):
        return None
    explicit = _explicit_alias(token)
    if explicit is not _NO_EXPLICIT_ALIAS:
        return explicit  # type: ignore[return-value]
    name = token.get_alias() or token.get_name()
    if not name:
        return None
    raw = _raw_output_token(token, name)
    return unquote_ident(raw if raw is not None else name).lower()


class _ComposedItem(Identifier):
    """A SELECT item sqlparse split across several tokens (``email 'c'``, ``email result``,
    ``agg(…) WITHIN GROUP (…) AS p``, ``email IS NULL AS e``), rejoined into one item.

    ``output_name`` is the driver's column name, unquoted and lowercased, or ``None`` when it
    cannot be determined.
    """

    def __init__(self, tokens: list, output_name: str | None) -> None:
        super().__init__(tokens)
        self.output_name = output_name

    def get_alias(self) -> str | None:
        return self.output_name

    def get_name(self) -> str | None:
        return self.output_name


_NO_EXPLICIT_ALIAS = object()


def _alias_token_name(tok: object) -> str | None:
    """The output name an alias token gives: ``'Email Address'`` (MySQL), ``"Pay"``, a bare
    name, or a word sqlparse reads as a keyword or type name (``date``, ``text``, ``desc``)."""
    if isinstance(tok, Identifier):
        leaves = [t for t in tok.flatten() if not t.is_whitespace]
        if len(leaves) != 1:
            return None
        tok = leaves[0]
    ttype = tok.ttype  # type: ignore[attr-defined]
    if ttype is None:
        return None
    if ttype in sql_tokens.String.Single:
        return tok.value[1:-1].replace("''", "'").lower()  # type: ignore[attr-defined]
    if ttype in sql_tokens.Name or ttype in sql_tokens.String.Symbol or ttype in sql_tokens.Keyword:
        return unquote_ident(tok.value).lower()  # type: ignore[attr-defined]
    return None


def _is_alias_word(tok: object) -> bool:
    """A token that can only be an alias when it directly follows an expression."""
    if isinstance(tok, Identifier):
        leaves = [t for t in tok.flatten() if not t.is_whitespace]
        return len(leaves) == 1 and leaves[0].ttype in _OUTPUT_NAME_TTYPES
    ttype = tok.ttype  # type: ignore[attr-defined]
    return ttype is not None and (
        ttype in sql_tokens.String.Single or ttype in sql_tokens.Keyword or ttype in sql_tokens.Name
    )


def _ends_expression(tok: object) -> bool:
    """False for a token after which an alias cannot follow (``::``, ``.``, ``+``, ``IS``)."""
    ttype = tok.ttype  # type: ignore[attr-defined]
    if ttype is None:
        return True
    return not (
        ttype is sql_tokens.Punctuation
        or ttype in sql_tokens.Operator
        or ttype in sql_tokens.Keyword
    )


def _explicit_alias(item: Identifier) -> object:
    """The output name an alias in *item* gives, ``None`` if there is an alias whose name
    cannot be read, or ``_NO_EXPLICIT_ALIAS`` when sqlparse's own reading should be used.

    Covers what ``get_alias()`` misreads: ``AS 'Email Address'``, ``AS date``/``AS text``
    (Name.Builtin), ``AS desc`` (Keyword), and ``email desc`` without ``AS``.
    """
    parts = _significant(item.tokens)
    for k, t in enumerate(parts):
        if t.ttype is sql_tokens.Keyword and t.normalized.upper() == "AS":
            return _alias_token_name(parts[k + 1]) if k + 1 == len(parts) - 1 else None
    if len(parts) >= 2:
        last, prev = parts[-1], parts[-2]
        if isinstance(last, Identifier):
            if prev.ttype is sql_tokens.Punctuation:
                return _NO_EXPLICIT_ALIAS
            return _explicit_alias(last)
        if last.ttype is not None and _is_alias_word(last) and _ends_expression(prev):
            if last.ttype in sql_tokens.Name:
                return _NO_EXPLICIT_ALIAS  # ``expr alias``: sqlparse reads this one right
            return _alias_token_name(last)
    return _NO_EXPLICIT_ALIAS


_SELECT_STOP_KEYWORDS: frozenset[str] = frozenset(
    {"FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "UNION", "INTERSECT", "EXCEPT"}
)
_SELECT_SKIP_KEYWORDS: frozenset[str] = frozenset({"DISTINCT", "ALL", "TOP"})


_SET_OPERATORS: frozenset[str] = frozenset(
    {
        "UNION",
        "UNION ALL",
        "UNION DISTINCT",
        "INTERSECT",
        "INTERSECT ALL",
        "EXCEPT",
        "EXCEPT ALL",
        "MINUS",
    }
)
# Keywords that end the FROM clause. ``WHERE`` is a sqlparse group, handled by type.
_FROM_STOP_KEYWORDS: frozenset[str] = frozenset(
    {"GROUP", "GROUP BY", "HAVING", "ORDER", "ORDER BY", "LIMIT", "OFFSET", "FETCH", "WINDOW"}
)
_JOIN_KEYWORD_PREFIXES = (
    "JOIN",
    "INNER",
    "LEFT",
    "RIGHT",
    "FULL",
    "CROSS",
    "NATURAL",
    "OUTER",
    "STRAIGHT_JOIN",
)
_FROM_NOISE_KEYWORDS: frozenset[str] = frozenset({"AS", "LATERAL"})
# Keywords that open a join condition; everything up to the next join or comma is skipped.
_JOIN_CONDITION_KEYWORDS: frozenset[str] = frozenset({"ON", "USING"})


@dataclass(frozen=True)
class _SelectItem:
    """One output column of a SELECT list: its lowercased name (None when it cannot be
    determined) and whether it is ``masked``, ``exempt`` (statistical aggregate/ranking) or
    ``plain``. ``opaque`` only ever ends a source's column list in a scope: the source has
    further columns whose names and sensitivity are unknown, so it cannot be enumerated."""

    name: str | None
    kind: str


_OPAQUE = _SelectItem(None, "opaque")

# Lowercased CTE or derived-table alias -> that query's output columns, in order.
_Scope = dict[str, list[_SelectItem]]


class _CteColumns(list):
    """A CTE's output columns, as stored in a ``_Scope``, with the spellings it was defined
    under (``_spelling``). A FROM name spelled otherwise may be a real table of the same
    folded name."""

    def __init__(self, items: Iterable[_SelectItem], spellings: Iterable[str]) -> None:
        super().__init__(items)
        self.spellings = frozenset(spellings)


def _spelling(node: object, dq: bool) -> str | None:
    """The first identifier of *node* as the engine compares names: PostgreSQL folds an
    unquoted name to lower case and keeps a quoted one; MySQL's table-name case sensitivity
    depends on the server, so there (*dq*) every name is kept as written. ``None`` past a
    ``(``, like ``_first_name``."""
    for t in node.flatten():  # type: ignore[attr-defined]
        if t.ttype in (sql_tokens.Name, sql_tokens.String.Symbol):
            quoted = t.ttype is sql_tokens.String.Symbol or t.value.startswith("`")
            value = unquote_ident(t.value)
            return value if quoted or dq else value.lower()
        if t.ttype is sql_tokens.Punctuation and t.value == "(":
            return None
    return None


def _is_comment(token: object) -> bool:
    # A block comment is a sqlparse ``Comment`` group (ttype None); a line comment is a token.
    return isinstance(token, Comment) or token.ttype in sql_tokens.Comment  # type: ignore[attr-defined]


def _significant(tokens: Iterable[object]) -> list:
    return [t for t in tokens if not t.is_whitespace and not _is_comment(t)]  # type: ignore[attr-defined]


def _strip_parens(tokens: Iterable[object]) -> list:
    toks = _significant(tokens)
    if toks and toks[0].ttype is sql_tokens.Punctuation and toks[0].value == "(":
        toks = toks[1:]
    if toks and toks[-1].ttype is sql_tokens.Punctuation and toks[-1].value == ")":
        toks = toks[:-1]
    return toks


def _is_query_parens(token: object) -> bool:
    """True for a Parenthesis whose content is a query (``SELECT``, ``WITH`` or ``((…))``)."""
    if not isinstance(token, Parenthesis):
        return False
    body = _strip_parens(token.tokens)
    if not body:
        return False
    first = body[0]
    if isinstance(first, Parenthesis):
        # ``((SELECT …))`` or ``(SELECT …) UNION (…)``; not an argument list such as
        # ``((SELECT …), 0)``, whose first element merely happens to be a query.
        has_comma = any(t.ttype is sql_tokens.Punctuation and t.value == "," for t in body)
        return not has_comma and _is_query_parens(first)
    return (
        first.ttype is sql_tokens.DML and first.normalized.upper() == "SELECT"
    ) or first.ttype is sql_tokens.Keyword.CTE


def _subquery_parens(node: object) -> Parenthesis | None:
    """The single Parenthesis child of *node* that holds a query, or ``None``."""
    parens = [t for t in getattr(node, "tokens", ()) if isinstance(t, Parenthesis)]
    if len(parens) != 1 or not _is_query_parens(parens[0]):
        return None
    return parens[0]


def _nested_queries(node: object) -> list[Parenthesis]:
    """Every query Parenthesis inside *node*, at any depth, without descending into one."""
    found: list[Parenthesis] = []
    for t in getattr(node, "tokens", ()):
        if _is_query_parens(t):
            found.append(t)
        elif t.is_group:
            found.extend(_nested_queries(t))
    return found


def _first_name(node: object) -> str | None:
    """The first identifier token of *node*, unquoted and lowercased; None past a ``(``."""
    for t in node.flatten():  # type: ignore[attr-defined]
        if t.ttype in (sql_tokens.Name, sql_tokens.String.Symbol):
            return unquote_ident(t.value).lower()
        if t.ttype is sql_tokens.Punctuation and t.value == "(":
            return None
    return None


def _ends_in_wildcard(item: object) -> bool:
    """``*`` or ``t.*`` (any depth) ends in a Wildcard token; ``COUNT(*)`` ends in ``)``."""
    flat = [t for t in item.flatten() if not t.is_whitespace]  # type: ignore[attr-defined]
    return bool(flat) and flat[-1].ttype is sql_tokens.Wildcard


def _wildcard_qualifier(item: object) -> str | None:
    toks = [t for t in item.flatten() if not t.is_whitespace]  # type: ignore[attr-defined]
    if len(toks) == 3 and toks[1].value == "." and toks[2].ttype is sql_tokens.Wildcard:
        return unquote_ident(toks[0].value).lower()
    return None


def _has_top_level_comma(token: object) -> bool:
    """A comma in *token* outside any parenthesis or call (``IN (1, 2)`` does not count)."""
    if isinstance(token, (Parenthesis, Function)):
        return False
    for t in getattr(token, "tokens", ()):
        if t.ttype is sql_tokens.Punctuation and t.value == ",":
            return True
        if t.is_group and not isinstance(t, (Parenthesis, Function)) and _has_top_level_comma(t):
            return True
    return False


def _alias_columns(func: object) -> tuple[str, list[str]] | None:
    """``b(id, k)`` used as an alias with a column list -> ``("b", ["id", "k"])``.

    sqlparse reads ``alias(a, b)`` as a Function. ``None`` when it is not that shape.
    """
    if not isinstance(func, Function):
        return None
    parts = _significant(func.tokens)
    if len(parts) != 2 or not isinstance(parts[0], Identifier):
        return None
    if not isinstance(parts[1], Parenthesis):
        return None
    alias = _first_name(parts[0])
    inner = _strip_parens(parts[1].tokens)
    if alias is None or len(inner) != 1:
        return None
    entries = list(inner[0].get_identifiers()) if isinstance(inner[0], IdentifierList) else inner
    names: list[str] = []
    for entry in entries:
        leaves = [t for t in entry.flatten() if not t.is_whitespace]
        if len(leaves) != 1 or leaves[0].ttype not in _OUTPUT_NAME_TTYPES:
            return None
        names.append(unquote_ident(leaves[0].value).lower())
    return alias, names


def _rename_columns(items: list[_SelectItem], names: list[str]) -> list[_SelectItem]:
    """Apply a column alias list by position.

    When the list does not match the source one to one (a different length, or a source of
    unknown width), it fails closed: every column of the source is masked if any is.
    """
    if len(names) == len(items) and all(i.kind != "opaque" for i in items):
        return [_SelectItem(n, i.kind) for n, i in zip(names, items, strict=True)]
    known = [i for i in items if i.kind != "opaque"]
    any_masked = len(known) != len(items) or any(i.kind == "masked" for i in items)
    kind = "masked" if any_masked else "plain"
    renamed = [_SelectItem(n, kind) for n in names]
    renamed += [_SelectItem(i.name, kind) for i in known[len(names) :]]
    if len(known) != len(items):
        renamed.append(_OPAQUE)
    return renamed


def _merge_sources(old: list[_SelectItem], new: list[_SelectItem]) -> list[_SelectItem]:
    """One entry for two sources of one scope whose names fold to the same key (``"U"`` and
    ``u``), failing closed: which of them a reference means is not tracked.

    The same columns in the same order merge position by position, masked when either is
    (exempt only when both are). Otherwise every column name of either is kept, masked when
    either masks it, and the width is unknown (``_OPAQUE``).
    """

    def kind(kinds: set[str]) -> str:
        if "masked" in kinds:
            return "masked"
        return "exempt" if kinds == {"exempt"} else "plain"

    if [i.name for i in old] == [i.name for i in new] and all(
        i.kind != "opaque" for i in (*old, *new)
    ):
        return [_SelectItem(a.name, kind({a.kind, b.kind})) for a, b in zip(old, new, strict=True)]
    by_name: dict[str | None, set[str]] = {}
    for i in (*old, *new):
        if i.kind != "opaque":
            by_name.setdefault(i.name, set()).add(i.kind)
    return [_SelectItem(n, kind(k)) for n, k in by_name.items()] + [_OPAQUE]


def _store_source(sources: _Scope, key: str, cols: list[_SelectItem]) -> None:
    """``sources[key] = cols``, merged with a source already stored under *key*."""
    old = sources.get(key)
    sources[key] = cols if old is None else _merge_sources(old, cols)


def _masked_names(sources: _Scope) -> set[str]:
    return {i.name for cols in sources.values() for i in cols if i.kind == "masked" and i.name}


def _mentions_masked(node: object, privacy: PrivacySettings, dq: bool, visible: _Scope) -> bool:
    """A name token in *node* matching a sensitive pattern or a masked column in *visible*.

    Coarse on purpose (no qualification, no scoping): it is the fail-closed test for a
    query the resolver cannot classify.
    """
    masked = _masked_names(visible)
    for tok in node.flatten():  # type: ignore[attr-defined]
        is_ref = tok.ttype is sql_tokens.Name or (tok.ttype is sql_tokens.String.Symbol and not dq)
        if not is_ref:
            continue
        value = unquote_ident(tok.value)
        if privacy.is_column_sensitive(value) or value.lower() in masked:
            return True
    return False


class _AllColumns(frozenset):
    """A masked-name set that contains every name. See ``MASK_ALL_COLUMNS``."""

    def __contains__(self, item: object) -> bool:
        return True

    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:
        return "MASK_ALL_COLUMNS"


# The masked-name half of ``_classify_select_columns``'s result when classification failed
# (an exception, sqlparse's token or depth limit) on SQL that names a sensitive or excluded
# column: every column is masked. ``name in MASK_ALL_COLUMNS`` is True for any name, so a
# membership test fails closed on its own; compare by identity (``is``) to detect it, since
# iterating it or combining it with another set yields nothing.
MASK_ALL_COLUMNS: frozenset[str] = _AllColumns()

_IDENTIFIER_WORD = re.compile(r'"((?:[^"]|"")+)"|`((?:[^`]|``)+)`|([A-Za-z_][A-Za-z0-9_$]*)')


def _sql_mentions_sensitive(sql: str, privacy: PrivacySettings) -> bool:
    """Whether any identifier-like word in *sql* names a sensitive or excluded column."""
    for match in _IDENTIFIER_WORD.finditer(sql):
        double_quoted, backticked, bare = match.groups()
        if double_quoted is not None:
            word = double_quoted.replace('""', '"')
        elif backticked is not None:
            word = backticked.replace("``", "`")
        else:
            word = bare
        if _is_sensitive_name(privacy, word):
            return True
    return False


_STRING_TOKEN_NAMES = (
    "STRING",
    "NATIONAL_STRING",
    "UNICODE_STRING",
    "BYTE_STRING",
    "HEREDOC_STRING",
    "RAW_STRING",
    "NATIONAL_RAW_STRING",
    "BIT_STRING",
    "HEX_STRING",
)
_NON_SPACE = re.compile(r"\S")


@functools.lru_cache(maxsize=1)
def _engine_lexing() -> tuple[object, object, frozenset[object], Any]:
    """The two tokenizers, the string token types and ``TokenType`` (imported lazily:
    sqlglot's dialect modules are heavy and only classification needs them)."""
    from sqlglot.dialects.mysql import MySQL
    from sqlglot.dialects.postgres import Postgres
    from sqlglot.tokens import TokenType

    strings = frozenset(getattr(TokenType, n) for n in _STRING_TOKEN_NAMES if hasattr(TokenType, n))
    return MySQL(), Postgres(), strings, TokenType


class _ExecutableCommentError(ValueError):
    """A MySQL ``/*! … */`` or ``/*M! … */`` comment: MySQL runs its content."""


class _EscapeStringContinuationError(ValueError):
    """A PostgreSQL ``E'…'`` continued on a later line: the tokenizer reads the next segment
    as a standard string, PostgreSQL as part of the escape string."""


# Two characters that would start or end a comment for sqlparse when two tokens the engine
# reads apart (MySQL ``2--1``: two minus signs) are emitted side by side.
_COMMENT_JOINS: frozenset[str] = frozenset({"--", "/*", "*/"})
# A backslash sqlparse would read as escaping the ``"`` after it in a quoted identifier.
_BACKSLASH_BEFORE_QUOTE = re.compile(r'\\(?=")')


def _lex_like_the_engine(sql: str, dq: bool) -> str:
    """*sql* rebuilt from the engine's own tokens, so sqlparse sees every boundary where the
    engine has it.

    The classifier reads the SQL the engine runs, so the engine's tokenizer (sqlglot's MySQL
    one when double quotes are strings, *dq*, PostgreSQL's otherwise) decides where every
    literal, quoted identifier and comment ends; sqlparse's own rules disagree with both
    engines (``N'\\'``, ``E'\\\\'`` and nested block comments on PostgreSQL, ``'\\\\'``
    and ``#`` comments on MySQL, PostgreSQL's ``#`` operators read as comments). Each token
    is re-emitted in a form sqlparse cannot misread:

    - the text between tokens is whitespace or a comment: whitespace is kept, a comment
      becomes spaces (newlines kept). A MySQL executable comment (``/*!``, ``/*M!``) raises,
      since MySQL runs its content;
    - a string literal of any kind becomes ``'<decoded text>'`` with every ``'`` and ``\\``
      replaced by a space (its end is what matters; a string alias keeps its name);
    - a double-quoted identifier becomes ``"<name>"`` with ``"`` doubled and a ``\\`` that
      sqlparse would read as escaping the ``"`` after it replaced by ``_``; otherwise the
      name is unchanged, so an excluded column keeps its name. A backtick identifier keeps
      its raw spelling;
    - a PostgreSQL operator spelled with ``#`` (``#``, ``#>>``, ``#-``) has it replaced by
      ``^``, an operator sqlparse does not read as a comment (on MySQL ``#`` only ever starts
      a comment, which is a gap);
    - everything else is kept as written, with a space between two tokens that would
      otherwise join into ``--``, ``/*``, ``*/`` or ``#``.

    Fails closed (raises) where the tokenizer is known to read otherwise than the engine: a
    PostgreSQL ``E'…'`` followed, across a newline, by a ``'…'`` that continues it. A
    tokenizer error (an unterminated literal) propagates too; the caller treats either as a
    classification failure.
    """
    mysql, postgres, strings, token_type = _engine_lexing()
    tokens = (mysql if dq else postgres).tokenize(sql)  # type: ignore[attr-defined]
    out: list[str] = []
    last = 0
    prev = None

    def gap(text: str) -> None:
        if not text:
            return
        if text.isspace():
            out.append(text)
            return
        if "/*!" in text or "/*M!" in text:
            raise _ExecutableCommentError("MySQL executable comment")
        out.append(_NON_SPACE.sub(" ", text))

    for tok in tokens:
        if tok.start < last or tok.end < tok.start:
            raise ValueError("tokens out of order")
        between = sql[last : tok.start]
        if (
            prev is not None
            and prev.token_type is token_type.BYTE_STRING
            and sql[prev.start] in "eE"
            and tok.token_type is token_type.STRING
            and "\n" in between
        ):
            raise _EscapeStringContinuationError("escape string continued on a later line")
        gap(between)
        raw = sql[tok.start : tok.end + 1]
        if tok.token_type in strings:
            piece = "'" + tok.text.replace("'", " ").replace("\\", " ") + "'"
        elif tok.token_type is token_type.IDENTIFIER:
            if raw.startswith('"'):
                piece = '"' + _BACKSLASH_BEFORE_QUOTE.sub("_", tok.text.replace('"', '""') + '"')
            else:
                piece = raw
        elif not dq and "#" in raw:
            piece = raw.replace("#", "^")
        else:
            piece = raw
        if not between and out and (out[-1][-1] + piece[0] in _COMMENT_JOINS or piece[0] == "#"):
            out.append(" ")
        out.append(piece)
        last = tok.end + 1
        prev = tok
    gap(sql[last:])
    return "".join(out)


def _classification_failed(
    sql: str, privacy: PrivacySettings, exc: BaseException
) -> tuple[frozenset[str], set[str]] | None:
    mask_all = _sql_mentions_sensitive(sql, privacy)
    logger.warning(
        "Result-column classification failed (%s) on SQL of length %d; %s",
        type(exc).__name__,
        len(sql),
        "masking every column" if mask_all else "falling back to column-name matching",
    )
    return (MASK_ALL_COLUMNS, set()) if mask_all else None


def _classify_output_items(
    stmt: object, privacy: PrivacySettings, dq: bool
) -> list[_SelectItem] | None:
    """The ordered output columns of the statement's top-level query, or ``None`` when the
    scoped resolver does not understand it. ``_OPAQUE`` stands for columns a wildcard yields
    whose names are unknown."""
    return _classify_query(list(stmt.tokens), privacy, dq, {}, {}, allow_unnamed=False)  # type: ignore[attr-defined]


def _classify_select_columns(
    sql: str,
    privacy: PrivacySettings,
    *,
    double_quote_is_string_literal: bool = False,
) -> tuple[set[str] | frozenset[str], set[str]] | None:
    """Classify *sql*'s output columns into (masked, aggregate-exempt) names.

    Returns ``None`` when classification is uncertain and the caller must fall back to
    column-name matching. Otherwise ``(masked, exempt)``: lowercased output names that must
    be redacted (in addition to name matching, which always applies), and aliases produced
    by statistical aggregate/ranking expressions that must never be redacted even if their
    name collides with a sensitive or excluded column. When part of the output cannot be
    enumerated (a wildcard over a real table) the result is the known masked names with no
    exemptions, or ``None`` if there are none.

    If classification fails outright (an exception, or sqlparse's token/depth limit) and the
    SQL names a sensitive or excluded column, masked is ``MASK_ALL_COLUMNS``: mask every
    column. A caller that *iterates* the masked names must test ``masked is
    MASK_ALL_COLUMNS`` first: the sentinel contains every name but iterates as empty.

    Scope-aware: a column renamed inside a CTE, a derived table or a scalar subquery keeps
    its sensitivity through the rename (column alias lists included), a nested query anywhere
    in a SELECT item masks the item, a whole-row reference (``row_to_json(u)``) masks the
    item, and a set operation masks a position when any branch does. A shape the resolver
    does not understand falls back to the outermost SELECT list alone, then to ``None`` —
    never a wrong "nothing sensitive".
    """
    masked, exempt, _ = _classify_result(sql, privacy, double_quote_is_string_literal)
    return None if masked is None else (masked, exempt)


# One kind per output column (``masked``/``exempt``/``plain``), or ``None`` when the outer
# SELECT list is not fully known (a wildcard over a real table, a fallback, a failure).
_Positions = tuple[str, ...] | None


def _classify_result(
    sql: str, privacy: PrivacySettings, dq: bool
) -> tuple[set[str] | frozenset[str] | None, set[str], _Positions]:
    """``_classify_select_columns``'s (masked, exempt), plus the per-position kinds.

    *masked* is ``None`` where ``_classify_select_columns`` returns ``None``. Positions are
    given only for a fully classified outer list, so their count is the query's column count.
    """
    try:
        parsed = sqlparse.parse(_lex_like_the_engine(sql, dq).strip())
        if not parsed:
            return None, set(), None
        stmt = parsed[0]
        items = _classify_output_items(stmt, privacy, dq)
        if items is None:
            # The scoped resolver gave up (a FROM shape it does not parse …). Do not discard
            # what the outermost SELECT list alone shows: a sensitive reference there is
            # masked whatever the rest of the query looks like.
            outer = _outer_select_list_masked(stmt, privacy, dq)
            return (outer or None), set(), None
    except Exception as exc:
        failed = _classification_failed(sql, privacy, exc)
        return (failed[0] if failed else None), set(), None
    masked = {i.name for i in items if i.kind == "masked" and i.name}
    if any(i.kind == "opaque" for i in items):
        if any(i.kind == "masked" and not i.name for i in items):
            # A masked column that has no name, among columns that cannot be enumerated:
            # neither its position nor a name can single it out.
            return MASK_ALL_COLUMNS, set(), None
        return (masked or None), set(), None
    exempt = {i.name for i in items if i.kind == "exempt" and i.name}
    return masked, exempt, tuple(i.kind for i in items)


def _outer_select_list_masked(stmt: object, privacy: PrivacySettings, dq: bool) -> set[str] | None:
    """Output names of the outermost SELECT list that reference a sensitive name token directly.

    The scope-blind fallback for shapes ``_classify_query`` does not resolve. ``None`` when
    a sensitive item has no determinable output name or the list cannot be read.
    """
    toks = _significant(stmt.tokens)  # type: ignore[attr-defined]
    for idx, t in enumerate(toks):
        if t.ttype is sql_tokens.Keyword and t.normalized.upper() in _SET_OPERATORS:
            toks = toks[:idx]
            break
    collected = _collect_select(toks)
    if collected is None:
        return None
    masked: set[str] = set()
    for item in collected[0]:
        if _ends_in_wildcard(item):
            continue  # its columns are left to name matching
        if _is_aggregate_expr(item) or _is_ranking_window_expr(item):
            continue
        if _token_references_sensitive(item, privacy, double_quote_is_string_literal=dq):
            masked.add(_output_alias(item) or _default_output_name(item))
    return masked


def _classify_query(
    tokens: Iterable[object],
    privacy: PrivacySettings,
    dq: bool,
    scope: _Scope,
    outer: _Scope,
    *,
    allow_unnamed: bool,
) -> list[_SelectItem] | None:
    """A query: an optional WITH, then one SELECT or several joined by set operators.

    *scope* holds the CTEs visible here; *outer* the sources of enclosing queries, for
    correlated references. With *allow_unnamed* a masked column may have no name (a column
    alias list or a scalar-subquery position names it); otherwise that is ``None``.
    """
    toks = _strip_parens(tokens)
    if not toks:
        return None
    if len(toks) == 1 and isinstance(toks[0], Parenthesis):
        return _classify_query(
            toks[0].tokens, privacy, dq, scope, outer, allow_unnamed=allow_unnamed
        )

    scope = dict(scope)
    if toks[0].ttype is sql_tokens.Keyword.CTE:
        recursive = (
            len(toks) > 1
            and toks[1].ttype is sql_tokens.Keyword
            and toks[1].normalized.upper() == "RECURSIVE"
        )
        if recursive:
            toks = [toks[0], *toks[2:]]
        if len(toks) < 2:
            return None
        defs = toks[1]
        entries = list(defs.get_identifiers()) if isinstance(defs, IdentifierList) else [defs]
        for entry in entries:
            if not isinstance(entry, Identifier):
                return None
            parts = _significant(entry.tokens)
            cols: list[str] | None = None
            if parts and isinstance(parts[0], Function):  # ``x(a, b) AS (...)``
                named = _alias_columns(parts[0])
                if named is None:
                    return None
                name, cols = named
            else:
                name = _first_name(entry)
            body = _subquery_parens(entry)
            spelled = _spelling(entry, dq)
            if name is None or body is None or spelled is None:
                return None
            inner = _source_columns(body, cols, privacy, dq, scope, outer)
            if inner is not None and recursive:
                inner = _recursive_fixpoint(
                    name, spelled, inner, body, cols, privacy, dq, scope, outer
                )
            if inner is None:
                return None
            spellings = {spelled}
            old = scope.get(name)
            if old is not None:
                # ``"U"`` and ``u`` in this WITH, or an enclosing CTE of the same folded name:
                # never let one hide the other (legal shadowing over-masks, which is accepted).
                inner = _merge_sources(old, inner)
                spellings |= getattr(old, "spellings", frozenset())
            scope[name] = _CteColumns(inner, spellings)  # later CTEs may reference earlier ones
        toks = toks[2:]

    branches: list[list] = [[]]
    for t in toks:
        if t.ttype is sql_tokens.Keyword and t.normalized.upper() in _SET_OPERATORS:
            branches.append([])
            continue
        branches[-1].append(t)
    results: list[list[_SelectItem]] = []
    for idx, branch in enumerate(branches):
        # Output names come from the first branch only: a later branch may leave a masked
        # column unnamed (``… UNION SELECT MAX(salary) FROM t``).
        unnamed_ok = allow_unnamed or idx > 0
        b = _strip_parens(branch)
        if len(b) == 1 and isinstance(b[0], Parenthesis):
            r = _classify_query(b[0].tokens, privacy, dq, scope, outer, allow_unnamed=unnamed_ok)
        else:
            r = _classify_simple_select(b, privacy, dq, scope, outer, allow_unnamed=unnamed_ok)
        if r is None:
            return None
        results.append(r)
    if len(results) == 1:
        return results[0]
    if any(i.kind == "opaque" for r in results for i in r):
        return _merge_opaque_branches(results, allow_unnamed)
    # Set operation: output names come from the first branch; a position is masked when any
    # branch masks it, and exempt only when every branch does.
    width = len(results[0])
    if any(len(r) != width for r in results):
        return None
    merged: list[_SelectItem] = []
    for i in range(width):
        column = [r[i] for r in results]
        name = column[0].name
        if any(c.kind == "masked" for c in column):
            merged.append(_SelectItem(_set_operation_name(name, allow_unnamed), "masked"))
        elif all(c.kind == "exempt" for c in column):
            merged.append(_SelectItem(name, "exempt"))
        else:
            merged.append(_SelectItem(name, "plain"))
    return merged


# The name PostgreSQL gives an unnamed output column (a literal, ``CASE``, …).
_UNNAMED_COLUMN = "?column?"


def _set_operation_name(name: str | None, allow_unnamed: bool) -> str | None:
    """The name of a masked set-operation column, taken from the first branch. Where every
    masked column needs a name and the first branch gives none (``SELECT 'x' UNION SELECT
    email …``), PostgreSQL's default name: the column is still masked by its position, and
    a name the driver did not return masks every column (the missing-name rule)."""
    return name if name is not None or allow_unnamed else _UNNAMED_COLUMN


def _merge_opaque_branches(
    results: list[list[_SelectItem]], allow_unnamed: bool
) -> list[_SelectItem] | None:
    """A set operation where a branch has columns of unknown width (``_OPAQUE``), failing
    closed: the first branch's columns, each masked when any branch masks a column that may
    line up with it. Every column is masked when a branch after the first has unknown
    columns (their values reach the user under the first branch's names, which name
    matching cannot see through), when a branch has both unknown columns and a masked one,
    or when a branch has unknown columns from position p and any branch a masked column at
    p or later (positions past the unknown columns cannot be lined up). The first branch's width is
    kept when it is known; otherwise the result ends with ``_OPAQUE``, so the names and the
    missing-name rule apply, preceded, when every column is masked, by an unnamed masked
    column, which masks every column of the result (``_classify_result``). Never exempt:
    positions do not line up.
    """

    def first_opaque(r: list[_SelectItem]) -> int:
        return next((k for k, i in enumerate(r) if i.kind == "opaque"), len(r))

    head, rest = results[0], results[1:]
    starts = [first_opaque(r) for r in results]
    every = (
        any(p < len(r) for r, p in zip(rest, starts[1:], strict=True))
        or any(
            p < len(r) and any(i.kind == "masked" for i in r)
            for r, p in zip(results, starts, strict=True)
        )
        # unknown columns from position p in one branch; a masked column at p or later in
        # another cannot be lined up with a name
        or any(
            p < len(results[b]) and any(i.kind == "masked" for r in results for i in r[p:])
            for b, p in enumerate(starts)
        )
    )
    known = first_opaque(head) == len(head)
    if known and any(first_opaque(r) == len(r) and len(r) != len(head) for r in rest):
        return None  # two branches of known, different widths: not understood
    merged: list[_SelectItem] = []
    for k, item in enumerate(head):
        if item.kind == "opaque":
            continue
        lined_up = [r[k] for r in rest if k < first_opaque(r)]
        if not known and k > first_opaque(head):
            lined_up = [i for r in rest for i in r]  # may line up with any later column
        if every or item.kind == "masked" or any(i.kind == "masked" for i in lined_up):
            merged.append(_SelectItem(_set_operation_name(item.name, allow_unnamed), "masked"))
        else:
            merged.append(_SelectItem(item.name, "plain"))
    if known:
        return merged
    if every:
        merged.append(_SelectItem(None, "masked"))  # the unknown columns: every one masked
    return [*merged, _OPAQUE]


_RECURSION_PASSES = 8


def _recursive_fixpoint(
    name: str,
    spelled: str,
    first: list[_SelectItem],
    body: Parenthesis,
    cols: list[str] | None,
    privacy: PrivacySettings,
    dq: bool,
    scope: _Scope,
    outer: _Scope,
) -> list[_SelectItem] | None:
    """Re-classify a ``WITH RECURSIVE`` body with its own columns in scope until stable.

    The first pass reads the self-reference as a real table, so a sensitive value that only
    the recursive branch moves into a column would be missed. A result that does not settle
    within a few passes fails closed: every column masked.
    """
    current = first
    for _ in range(_RECURSION_PASSES):
        own = _CteColumns(current, {spelled})
        again = _source_columns(body, cols, privacy, dq, {**scope, name: own}, outer)
        if again is None:
            return None
        if again == current:
            return current
        current = again
    return [i if i.kind == "opaque" else _SelectItem(i.name, "masked") for i in current]


def _source_columns(
    body: Parenthesis,
    cols: list[str] | None,
    privacy: PrivacySettings,
    dq: bool,
    scope: _Scope,
    outer: _Scope,
) -> list[_SelectItem] | None:
    """The output columns of a CTE or derived-table *body*, renamed by *cols* when given.

    With a column list, a body the resolver cannot classify fails closed: if it mentions a
    sensitive or masked column, every listed name is masked and the width is unknown.
    """
    inner = _classify_query(body.tokens, privacy, dq, scope, outer, allow_unnamed=cols is not None)
    if cols is None:
        return inner
    if inner is None:
        if not _mentions_masked(body, privacy, dq, {**scope, **outer}):
            return None
        return [_SelectItem(n, "masked") for n in cols] + [_OPAQUE]
    return _rename_columns(inner, cols)


def _ends_with_within(item: object) -> bool:
    flat = [t for t in item.flatten() if not t.is_whitespace]  # type: ignore[attr-defined]
    return bool(flat) and flat[-1].ttype is sql_tokens.Name and flat[-1].value.upper() == "WITHIN"


def _composed(parts: list, output_name: str | None) -> _ComposedItem:
    """Join *parts* into one item. A leading Identifier is spliced so its own leading call
    (``SUM(…)``) stays first, where the aggregate check looks."""
    head = parts[0]
    tokens: list = (
        list(head.tokens)
        if isinstance(head, Identifier) and not isinstance(head, _ComposedItem)
        else [head]
    )
    for part in parts[1:]:
        tokens.append(sqlparse.sql.Token(sql_tokens.Whitespace, " "))
        tokens.append(part)
    return _ComposedItem(tokens, output_name)


def _join_within_group(prev: object, group_kw: object, nxt: object) -> _ComposedItem | None:
    """Rejoin ``agg(…) WITHIN`` + ``GROUP`` + ``(ORDER BY …) [AS alias]`` into one item.

    sqlparse ends the item at ``WITHIN`` and reads ``GROUP`` as a clause keyword. The output
    name is the alias after the ``ORDER BY`` group, or none.
    """
    if isinstance(nxt, Identifier):
        explicit = _explicit_alias(nxt)
        if explicit is not _NO_EXPLICIT_ALIAS:
            alias = explicit
        else:
            alias = _output_alias(nxt) if nxt.get_alias() else None
    elif isinstance(nxt, Parenthesis):
        alias = None
    else:
        return None
    return _composed([prev, group_kw, nxt], alias)


def _group_item(group: list) -> object | None:
    """One comma-separated SELECT item from its significant tokens."""
    if (
        len(group) >= 3
        and group[1].ttype is sql_tokens.Keyword
        and group[1].normalized.upper() == "GROUP"
        and _ends_with_within(group[0])
    ):
        joined = _join_within_group(group[0], group[1], group[2])
        if joined is None:
            return None
        group = [joined, *group[3:]]
    if len(group) == 1:
        tok = group[0]
        if tok.ttype is not None and (
            tok.ttype in sql_tokens.Keyword or tok.ttype in sql_tokens.Name
        ):
            return _ComposedItem([tok], unquote_ident(tok.value).lower())  # ``SELECT date``
        if tok.ttype is not None or isinstance(tok, (Identifier, Function)):
            return tok
        return _ComposedItem([tok], None)  # CASE, Parenthesis, Comparison …: no name
    if group[-2].ttype is sql_tokens.Keyword and group[-2].normalized.upper() == "AS":
        return _composed([*group[:-2], group[-1]], _alias_token_name(group[-1]))
    last, prev = group[-1], group[-2]
    if isinstance(last, Identifier) and not _is_alias_word(last):
        explicit = _explicit_alias(last)
        if explicit is _NO_EXPLICIT_ALIAS:
            # ``FILTER (WHERE a) n``: sqlparse groups the alias with the clause before it
            explicit = _output_alias(last) if last.get_alias() else None
        return _composed(group, explicit)  # type: ignore[arg-type]
    if _is_alias_word(last) and _ends_expression(prev):
        return _composed(group, _alias_token_name(last))  # ``email result``, ``email 'c'``
    return _composed(group, None)


def _collect_select(toks: list) -> tuple[list, list] | None:
    """Split one ``SELECT … FROM …`` (significant tokens) into its items and FROM tokens.

    The SELECT list is rebuilt from its comma-separated parts rather than trusted as
    sqlparse groups it: an alias sqlparse does not recognise (``email 'c'``, ``email type``)
    or ``WITHIN GROUP`` splits an item across top-level tokens.
    """
    stream: list = []
    from_tokens: list = []
    in_select = False
    in_from = False
    last_keyword = ""
    skip_count = False  # the row count after TOP
    distinct_on = False  # the ``(…)`` of ``DISTINCT ON (…)`` still precedes the first item
    for token in toks:
        if token.ttype is sql_tokens.DML and token.normalized.upper() == "SELECT":
            if in_select:
                return None  # a second SELECT at this level: shape not understood
            in_select = True
            continue
        if not in_select or _is_comment(token) or token.is_whitespace:
            continue
        if in_from:
            if isinstance(token, Where) or (
                token.ttype is sql_tokens.Keyword
                and token.normalized.upper() in _FROM_STOP_KEYWORDS
            ):
                break
            from_tokens.append(token)
            continue
        if skip_count:
            skip_count = False
            if token.ttype in sql_tokens.Literal or isinstance(token, Parenthesis):
                continue
        if token.ttype is sql_tokens.Keyword:
            upper = token.normalized.upper()
            if upper == "FROM":
                in_from = True
                continue
            if not stream:
                if upper in _SELECT_SKIP_KEYWORDS:
                    skip_count = upper == "TOP"
                    last_keyword = upper
                    continue
                if upper == "ON" and last_keyword == "DISTINCT":
                    distinct_on = True
                    continue
            if upper == "GROUP" and stream and _ends_with_within(stream[-1]):
                stream.append(token)
                continue
            if upper in _SELECT_STOP_KEYWORDS:
                break
            stream.append(token)  # an alias such as ``email result``: grouping decides
            continue
        parts = _significant(token.tokens) if isinstance(token, IdentifierList) else [token]
        if distinct_on:
            distinct_on = False
            first = parts[0]
            if isinstance(first, Parenthesis):
                parts = parts[1:]
            elif isinstance(first, Identifier) and isinstance(
                (inner := _significant(first.tokens))[0], Parenthesis
            ):
                parts = inner[1:] + parts[1:]
            else:
                return None
        stream.extend(parts)
    if not in_select or not stream:
        return None
    select_items: list = []
    group: list = []
    for tok in [*stream, None]:
        if tok is None or (tok.ttype is sql_tokens.Punctuation and tok.value == ","):
            if not group:
                return None
            item = _group_item(group)
            if item is None:
                return None
            select_items.append(item)
            group = []
            continue
        group.append(tok)
    return select_items, from_tokens


def _query_masks(
    paren: Parenthesis, privacy: PrivacySettings, dq: bool, scope: _Scope, outer: _Scope
) -> bool | None:
    """Whether a nested query yields a masked column.

    A query the resolver cannot classify, or one with columns it cannot enumerate, is masked
    when it mentions a sensitive name or a masked column in scope, and ``None`` (not
    understood) otherwise.
    """
    inner = _classify_query(paren.tokens, privacy, dq, scope, outer, allow_unnamed=True)
    if inner is not None:
        if any(i.kind == "masked" for i in inner):
            return True
        if all(i.kind != "opaque" for i in inner):
            return False
    if _mentions_masked(paren, privacy, dq, {**scope, **outer}):
        return True
    return None


@dataclass
class _FromScope:
    """The sources one FROM clause makes visible.

    *scoped*: alias -> columns of a CTE, derived table or renamed source. *order*: every
    source in FROM order, by key, ``None`` for one whose columns are unknown (a real table or
    a table function). *real*: key of each real table -> whether that key is an alias (an
    unaliased table name is a whole-row reference only inside a call; see
    ``_references_sensitive``).
    """

    scoped: _Scope = field(default_factory=dict)
    order: list[str | None] = field(default_factory=list)
    real: dict[str, bool] = field(default_factory=dict)


def _default_output_name(item: object) -> str:
    """The name PostgreSQL gives an unaliased expression: the function name for a call,
    ``?column?`` otherwise. Only a guess for a sensitive item. When the outer SELECT list is
    fully classified, ``_resolve_mask_indices`` judges each column by its position and does not
    consult the guess; otherwise a guess the driver did not return (MySQL names an unaliased
    expression by its text) masks every column."""
    head = _significant(item.tokens)[0] if getattr(item, "is_group", False) else item
    if isinstance(head, sqlparse.sql.Case):
        return "case"
    return _outermost_call_name(item) or "?column?"


def _expand_wildcard(item: object, fs: _FromScope) -> list[_SelectItem]:
    """The columns ``*`` or ``q.*`` yields: known ones by name, ``_OPAQUE`` for the rest."""
    if item.ttype is sql_tokens.Wildcard:  # type: ignore[attr-defined]
        keys: list[str | None] = fs.order
    else:
        qual = _wildcard_qualifier(item)
        keys = [qual] if qual in fs.scoped else []
        if qual not in fs.scoped or qual in fs.real:  # a real table's columns are unknown
            keys.append(None)
    expanded: list[_SelectItem] = []
    for key in keys:
        if key is None:
            expanded.append(_OPAQUE)
        else:
            expanded.extend(fs.scoped[key])
    return expanded


def _classify_simple_select(
    toks: list,
    privacy: PrivacySettings,
    dq: bool,
    scope: _Scope,
    outer: _Scope,
    *,
    allow_unnamed: bool,
) -> list[_SelectItem] | None:
    """One ``SELECT … FROM …`` with no set operator at this level.

    A wildcard over a source whose columns are unknown yields ``_OPAQUE`` in place of those
    columns: the rest of the list is still classified, and the caller treats the result as
    "these masked names plus name matching".
    """
    collected = _collect_select(toks)
    if collected is None:
        return None
    select_items, from_tokens = collected

    fs = _from_sources(from_tokens, privacy, dq, scope, outer)
    if fs is None:
        return None
    # A nested query resolves FROM names against CTEs only; this FROM's derived tables are
    # visible to it as correlated column qualifiers (*outer*), not as table sources.
    nested_scope = scope
    nested_outer = {**outer, **fs.scoped}

    items: list[_SelectItem] = []
    for item in select_items:
        if item.ttype is sql_tokens.Wildcard or _ends_in_wildcard(item):
            if item.ttype is not sql_tokens.Wildcard and _wildcard_qualifier(item) is None:
                return None
            items.extend(_expand_wildcard(item, fs))
            continue
        if _is_aggregate_expr(item) or _is_ranking_window_expr(item):
            items.append(_SelectItem(_output_alias(item), "exempt"))
            continue
        name = _output_alias(item)
        sub = _subquery_parens(item) if isinstance(item, Identifier) else None
        if sub is not None:
            # A scalar subquery: its value is the subquery's column, so only that counts
            # (a sensitive name in its WHERE does not reach the output).
            masked = _query_masks(sub, privacy, dq, nested_scope, nested_outer)
            if masked is None:
                return None
        else:
            masked = _references_sensitive(item, privacy, dq, fs, outer)
            for nested in [] if masked else _nested_queries(item):
                nested_masks = _query_masks(nested, privacy, dq, nested_scope, nested_outer)
                if nested_masks is None:
                    return None
                if nested_masks:
                    masked = True
                    break
        if masked:
            if name is None and not allow_unnamed:
                name = _default_output_name(item)
            items.append(_SelectItem(name, "masked"))
        else:
            items.append(_SelectItem(name, "plain"))
    return items


def _from_sources(
    from_tokens: list, privacy: PrivacySettings, dq: bool, scope: _Scope, outer: _Scope
) -> _FromScope | None:
    """Resolve the FROM clause into the sources it makes visible.

    Join conditions (``ON …``, ``USING (…)``) are skipped. A source renamed by a column alias
    list (``s(a, b)``) is renamed by position. ``None`` when the clause has a shape this does
    not understand (a derived table with no alias, a parenthesised join group).
    """
    refs: list[list] = []  # [node, alias, column list] per source
    in_condition = False
    attachable = False  # the last source may still take a following ``alias(a, b)``
    for t in _significant(from_tokens):
        is_keyword = t.ttype is sql_tokens.Keyword
        upper = t.normalized.upper() if is_keyword else ""
        if in_condition:
            if (is_keyword and upper.startswith(_JOIN_KEYWORD_PREFIXES)) or (
                t.ttype is sql_tokens.Punctuation and t.value == ","
            ):
                in_condition = False
            elif isinstance(t, IdentifierList) or _has_top_level_comma(t):
                return None  # a source may be grouped into the condition
            continue
        if isinstance(t, Function) and attachable:
            named = _alias_columns(t)
            if named is None or refs[-1][1] is not None:
                return None
            refs[-1][1], refs[-1][2] = named
            attachable = False
            continue
        attachable = False
        if isinstance(t, IdentifierList):
            refs.extend(
                [e, None, None]
                for e in t.get_identifiers()
                if not (
                    e.ttype is sql_tokens.Keyword and e.normalized.upper() in _FROM_NOISE_KEYWORDS
                )
            )
            attachable = True
        elif isinstance(t, (Identifier, Function)) or _is_query_parens(t):
            refs.append([t, None, None])
            attachable = True
        elif isinstance(t, Parenthesis):
            return None  # a parenthesised join group
        elif is_keyword:
            if upper in _JOIN_CONDITION_KEYWORDS:
                in_condition = True
            elif not (upper.startswith(_JOIN_KEYWORD_PREFIXES) or upper in _FROM_NOISE_KEYWORDS):
                return None
        elif not (
            t.ttype is sql_tokens.Punctuation
            or t.ttype in sql_tokens.Literal
            or t.ttype in sql_tokens.Operator
        ):
            return None

    fs = _FromScope()
    for node, attached_alias, attached_cols in refs:
        alias: str | None = attached_alias
        cols: list[str] | None = attached_cols
        parts = _significant(node.tokens) if node.is_group else []
        if isinstance(node, Identifier):
            renamed = (
                len(parts) >= 2
                and isinstance(parts[-1], Function)
                and parts[-2].ttype is not sql_tokens.Punctuation
            )
            if renamed:  # ``src AS s(a, b)``
                named = _alias_columns(parts[-1])
                if named is None or alias is not None:
                    return None
                alias, cols = named
            elif alias is None:
                raw = node.get_alias()
                alias = unquote_ident(raw).lower() if raw else None
        elif not isinstance(node, (Function, Parenthesis)):
            return None
        body = node if isinstance(node, Parenthesis) else _subquery_parens(node)
        if body is not None:
            if not alias:
                return None
            inner = _source_columns(body, cols, privacy, dq, scope, {**outer, **fs.scoped})
            if inner is None:
                return None
            _store_source(fs.scoped, alias, inner)
            fs.order.append(alias)
            continue
        is_function = isinstance(node, Function) or bool(parts and isinstance(parts[0], Function))
        real = None if is_function else _first_name(node)
        key = alias or real
        if real is not None and real in scope and key:
            src_cols = scope[real]
            if _spelling(node, dq) not in getattr(src_cols, "spellings", frozenset()):
                # Spelled unlike the CTE (``"U"`` for ``u``): a real table of the same folded
                # name may be meant, whose columns are unknown. Fail closed: both.
                src_cols = _merge_sources(src_cols, [_OPAQUE])
            _store_source(
                fs.scoped, key, _rename_columns(src_cols, cols) if cols is not None else src_cols
            )
            fs.order.append(key)
            continue
        if is_function:
            # A table function (``unnest(emails) AS z``) yields whatever its arguments hold.
            if _mentions_masked(node, privacy, dq, {**scope, **outer, **fs.scoped}):
                if not key:
                    return None
                names = cols if cols is not None else [key]
                _store_source(fs.scoped, key, [_SelectItem(n, "masked") for n in names] + [_OPAQUE])
                fs.order.append(key)
                continue
            fs.order.append(None)
            continue
        if cols is not None:
            # A real table renamed by a column list: which column each name carries is
            # unknown, so every listed name is masked (fail closed).
            if not key:
                return None
            _store_source(fs.scoped, key, [_SelectItem(n, "masked") for n in cols] + [_OPAQUE])
            fs.order.append(key)
            continue
        fs.order.append(None)
        if key:
            fs.real[key] = alias is not None
    return fs


def _alias_leaf(item: object) -> object | None:
    """The leaf token that spells *item*'s explicit alias, if it has one."""
    has_alias = (
        item.output_name is not None
        if isinstance(item, _ComposedItem)
        else isinstance(item, Identifier)
        and (_explicit_alias(item) is not _NO_EXPLICIT_ALIAS or item.get_alias() is not None)
    )
    if not has_alias:
        return None
    leaves = [t for t in item.flatten() if not t.is_whitespace]  # type: ignore[attr-defined]
    return leaves[-1] if leaves else None


def _inside_call(tok: object, item: object) -> bool:
    """True when *tok* sits inside a parenthesis (a call's arguments) below *item*."""
    node = tok.parent  # type: ignore[attr-defined]
    while node is not None and node is not item:
        if isinstance(node, Parenthesis):
            return True
        node = node.parent
    return False


def _whole_row_masked(key: str, tok: object, item: object, fs: _FromScope) -> bool:
    """A bare source name (``row_to_json(u)``, ``SELECT u FROM users u``) or ``u.*`` inside an
    expression references every column of that source."""
    # A key may name both a scoped source and a real table (``users "U", (…) u``): masked
    # if either is.
    if key in fs.scoped and any(i.kind in ("masked", "opaque") for i in fs.scoped[key]):
        return True
    if key in fs.real:
        # A real table's columns are unknown: fail closed. An unaliased table name is read
        # as a whole row only inside a call, since ``SELECT country FROM country`` is the
        # column (a column name wins over a whole-row reference).
        return fs.real[key] or _inside_call(tok, item)
    return False


def _references_sensitive(
    item: object,
    privacy: PrivacySettings,
    dq: bool,
    fs: _FromScope,
    outer: _Scope,
) -> bool:
    """A name token naming a sensitive or excluded column, a column reference that resolves
    to a masked output column of a CTE or derived table in *fs* or, for a correlated
    reference, in an enclosing query's sources (*outer*), or a whole-row reference to a
    source that can carry a sensitive column.

    A double-quoted token is a column reference unless the dialect reads ``"…"`` as a string
    literal (*dq*). Every token is tested unquoted. A qualified reference ``x.c`` is looked
    up in ``x`` alone; an unqualified ``c`` in every visible source, which over-masks on a
    name collision — the safe direction.
    """
    scoped_sources = fs.scoped
    masked_any = _masked_names({**outer, **scoped_sources})
    alias_leaf = _alias_leaf(item)
    toks = [t for t in item.flatten() if not t.is_whitespace]  # type: ignore[attr-defined]
    for idx, tok in enumerate(toks):
        is_ref = tok.ttype is sql_tokens.Name or (tok.ttype is sql_tokens.String.Symbol and not dq)
        if not is_ref:
            continue
        value = unquote_ident(tok.value)
        if _is_sensitive_name(privacy, value):
            return True
        low = value.lower()
        after = toks[idx + 1].value if idx + 1 < len(toks) else ""
        qualified = (
            idx >= 2
            and toks[idx - 1].value == "."
            and toks[idx - 2].ttype in (sql_tokens.Name, sql_tokens.String.Symbol)
        )
        if qualified:
            qual = unquote_ident(toks[idx - 2].value).lower()
            cols = scoped_sources.get(qual)
            if cols is None:
                cols = outer.get(qual)
            if cols is not None and any(i.kind == "masked" and i.name == low for i in cols):
                return True
            continue
        if low in masked_any:
            return True
        if tok is alias_leaf or after == "(":
            continue  # the item's own alias, or a function name
        star = after == "." and idx + 2 < len(toks) and toks[idx + 2].ttype is sql_tokens.Wildcard
        if (after != "." or star) and _whole_row_masked(low, tok, item, fs):
            return True
    return False


def _resolve_mask_indices(
    columns: list[str],
    masked_names: frozenset[str] | set[str] | None,
    excluded_bare: frozenset[str],
    privacy: PrivacySettings,
    exempt_names: frozenset[str] | set[str],
    positions: _Positions = None,
) -> frozenset[int]:
    """Column indices to redact.

    Name matching (a sensitive pattern or an excluded column) applies to every column except
    an exempt one (a statistical aggregate or ranking output, judged by position or, without
    positions, by exempt alias). ``masked_names is None`` → classifier uncertain; name matching
    alone. ``MASK_ALL_COLUMNS`` masks every column.

    With *positions* (one kind per column, from ``_classify_result``) and as many of them as
    columns, each column is judged on its own position: masked if its position is, or if
    name matching hits it and its position is not exempt. An exempt position therefore
    spares only itself, never another column of the same name, and a plain column is not
    masked merely for sharing a masked column's name (``lower(name), lower(email)``). The
    predicted output names are not consulted here: they are a guess, and the row-filter
    rewrite returns a function column under sqlglot's name (``strpos`` → ``position``,
    ``now`` → ``current_timestamp``) while PostgreSQL truncates a long alias. The position
    is the lineage, as engine-side dynamic data masking has it.
    Without positions, a column is masked when its name is a masked name, or when name
    matching hits it and its name is not an exempt alias; and a masked name the driver did
    not return masks every column, since the guess was wrong and nothing else lines up.
    *exempt_names* is required on purpose so no caller can drop that half silently.
    """
    lowered = [col.lower() for col in columns]
    if masked_names is MASK_ALL_COLUMNS:
        return frozenset(range(len(columns)))

    def name_hit(col: str, low: str) -> bool:
        return privacy.is_column_sensitive(col) or low in excluded_bare

    if masked_names is not None and positions is not None and len(positions) == len(columns):
        return frozenset(
            i
            for i, (col, low, kind) in enumerate(zip(columns, lowered, positions, strict=True))
            if kind == "masked" or (kind != "exempt" and name_hit(col, low))
        )
    if masked_names is not None and not set(masked_names) <= set(lowered):
        return frozenset(range(len(columns)))
    masked = masked_names or set()
    exempt = exempt_names if masked_names is not None else set()
    return frozenset(
        i
        for i, (col, low) in enumerate(zip(columns, lowered, strict=True))
        if low in masked or (name_hit(col, low) and low not in exempt)
    )


def _double_quote_is_literal(adapter: object) -> bool:
    """Read ``double_quote_is_string_literal`` off *adapter*, fail-safe: anything but a literal
    ``True`` (a wrapper without the attribute, a mock's auto-attribute) reads ``False``, the
    over-masking direction."""
    return getattr(adapter, "double_quote_is_string_literal", False) is True


_REDACTED = "[REDACTED]"


@dataclass(frozen=True, eq=False)
class _ResultMaskPlan:
    """How to redact one query's result. Classifying the SQL is the costly part, so it is
    done once per executed statement (``_plan_result_mask``); applying the plan to a batch
    of rows is a column-index lookup, memoised per column list.
    """

    privacy: PrivacySettings
    masked: set[str] | frozenset[str] | None
    exempt: set[str]
    positions: _Positions
    _by_columns: dict[tuple[str, ...], frozenset[int]] = field(default_factory=dict)

    def redacted_indices(self, columns: list[str]) -> frozenset[int]:
        """Indices of *columns* whose values must be replaced by ``[REDACTED]``."""
        key = tuple(columns)
        hit = self._by_columns.get(key)
        if hit is None:
            hit = _resolve_mask_indices(
                columns,
                self.masked,
                _excluded_bare_names(self.privacy),
                self.privacy,
                self.exempt,
                self.positions,
            )
            self._by_columns[key] = hit
        return hit

    def redact(self, rows: list[list[Any]], columns: list[str]) -> list[list[Any]]:
        """*rows* with the redacted columns replaced; the same list when nothing is."""
        indices = self.redacted_indices(columns)
        if not indices:
            return rows
        return [[_REDACTED if j in indices else v for j, v in enumerate(row)] for row in rows]


def _plan_result_mask(
    privacy: PrivacySettings | None,
    sql: str | None,
    original_sql: str | None = None,
    *,
    double_quote_is_string_literal: bool = False,
) -> _ResultMaskPlan | None:
    """The mask for the result of *sql*, or ``None`` when *privacy* is unset (no masking).

    *sql* is the statement that ran (possibly row-filter rewritten or re-sorted);
    *original_sql* the model's SQL before the rewrite, preferred for classification since the
    rewrite and the injected ORDER BY keep its columns and their positions. With neither, the
    plan is plain name matching. Build one plan per executed statement — again after every
    self-correction, as the SQL changes — and call ``plan.redact(rows, columns)`` on each
    streamed batch and on the final payload.
    """
    if not privacy:
        return None
    classify_from = original_sql or sql
    if not classify_from:
        return _ResultMaskPlan(privacy, None, set(), None)
    masked, exempt, positions = _classify_result(
        classify_from, privacy, double_quote_is_string_literal
    )
    return _ResultMaskPlan(privacy, masked, exempt, positions)


def _mask_sensitive_result_columns(
    response: QueryResultsResponse,
    privacy: PrivacySettings | None,
    sql: str | None = None,
    original_sql: str | None = None,
    *,
    double_quote_is_string_literal: bool = False,
) -> QueryResultsResponse:
    """Replace values in sensitive or excluded columns with ``[REDACTED]``.

    *sql* is the SQL that ran (possibly row-filter rewritten); *original_sql* the model's SQL
    before the rewrite, preferred for classification so the filtered derived tables do not
    defeat the aggregate exemption. See ``_plan_result_mask`` and ``_resolve_mask_indices``
    for the rules. Applied before results reach the client or the stored message, so
    sensitive values never leave the server in plaintext.
    """
    plan = _plan_result_mask(
        privacy, sql, original_sql, double_quote_is_string_literal=double_quote_is_string_literal
    )
    if plan is None:
        return response
    rows = plan.redact(response.rows, response.columns)
    if rows is response.rows:
        return response
    return response.model_copy(update={"rows": rows})
