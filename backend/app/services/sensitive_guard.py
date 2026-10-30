# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Strict sensitive-column mode: reject a query that uses a sensitive column anywhere but
as a plain item of its SELECT list.

Masking changes what is displayed, not what the query reads: a ``WHERE``, ``JOIN``,
``GROUP BY``, ``HAVING`` or ``ORDER BY`` on a sensitive column works on the real values and
lets a user narrow a masked value down. With ``PrivacySettings.strict_sensitive_columns``
on, ``check_strict_sensitive`` runs on the clean query before the row-filter rewrite
(``row_filter._executable``) and raises ``SensitiveColumnRejectedError`` for any such use,
including an expression in the SELECT list (``lower(email)``, ``count(email)``, a CASE). A
plain column, aliased or not, is allowed: the masking covers its output. ``*`` and ``t.*``
as projection items are allowed too, as is ``count(*)``.

The other rules close the ways a sensitive value is read under another name or without
its name: an alias given to a sensitive column (``email AS e``, in a CTE or a derived table
too, transitively) is itself a sensitive name; a positional ``ORDER BY 2`` / ``GROUP BY 1``
is resolved to the projection item it names; ``JOIN ... USING (email)`` and every NATURAL
join are rejected; a whole-row reference (``u::text``, ``to_jsonb(u)``, ``(u).email``,
``SELECT u FROM u``) and a star anywhere but as a projection item or in ``count(*)`` are
rejected; a column list on a table alias (``c(e) AS (...)``) is rejected.

Fail closed: in strict mode every query is parsed, and one that sqlglot cannot read as one
plain statement is rejected whether or not its text names a sensitive column (a whole-row
reference does not). Pure: no I/O, no settings beyond *privacy*.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, ParseError, TokenError

from ..datasources.models import PrivacySettings
from .result_masking import is_sensitive_name
from .row_filter_errors import SensitiveColumnRejectedError
from .row_filter_rewrite import sqlglot_dialect

_UNPARSED = "Strict sensitive columns: the query could not be parsed, so it was not run."
_UNPARSED_HINT = "Write the query as one plain SELECT statement."
_SHAPE_HINT = (
    "Name the columns you need explicitly, join with an ON condition and refer to columns"
    " by name, not by position."
)


def _message(column: str) -> str:
    return (
        f"Strict sensitive columns: the query uses the column {column} outside its SELECT"
        " list (in a condition, join, grouping, ordering, expression or under another name)."
        " This connection does not allow that."
    )


def _hint(column: str) -> str:
    return (
        f"Do not use the column {column} in WHERE, JOIN, GROUP BY, HAVING, ORDER BY or"
        " inside an expression, and do not refer to it through an alias or a position."
        " Select it as a plain column or leave it out."
    )


def _shape(what: str) -> str:
    return (
        f"Strict sensitive columns: the query uses {what}, which the strict check cannot"
        " follow, so it was not run."
    )


def _reject_column(column: str) -> SensitiveColumnRejectedError:
    return SensitiveColumnRejectedError(_message(column), llm_hint=_hint(column))


def _reject_shape(what: str) -> SensitiveColumnRejectedError:
    return SensitiveColumnRejectedError(_shape(what), llm_hint=_SHAPE_HINT)


def _unparsed() -> SensitiveColumnRejectedError:
    return SensitiveColumnRejectedError(_UNPARSED, llm_hint=_UNPARSED_HINT)


def _parse_one_statement(sql: str, dialect: str) -> exp.Expression:
    """*sql* as one sqlglot tree, or the unparsed rejection (a parse failure, several
    statements, a ``Command``/``Describe`` node that carries raw text)."""
    try:
        parsed = sqlglot.parse(
            sql.rstrip().rstrip(";"),
            read=sqlglot_dialect(dialect),
            error_level=ErrorLevel.IMMEDIATE,
        )
    except (ParseError, TokenError) as exc:
        raise _unparsed() from exc
    statements = [s for s in parsed if s is not None]
    if len(statements) != 1:
        raise _unparsed()
    tree = statements[0]
    for node in tree.walk():
        if isinstance(node, (exp.Command, exp.Describe)):
            raise _unparsed()
    return tree


def _is_sensitive(privacy: PrivacySettings, name: str, laundered: set[str]) -> bool:
    return bool(name) and (is_sensitive_name(privacy, name) or name.lower() in laundered)


def _laundered_names(tree: exp.Expression, privacy: PrivacySettings) -> set[str]:
    """Lower-cased aliases given to a sensitive column anywhere in *tree* (``email AS e``,
    then ``e AS f``), to a fixed point."""
    names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for alias in tree.find_all(exp.Alias):
            inner = alias.this
            if not isinstance(inner, exp.Column) or isinstance(inner.this, exp.Star):
                continue
            if (
                alias.alias
                and _is_sensitive(privacy, inner.name, names)
                and alias.alias.lower() not in names
            ):
                names.add(alias.alias.lower())
                changed = True
    return names


def _table_names(tree: exp.Expression) -> set[str]:
    """Lower-cased names and aliases of every table, derived table and CTE in *tree*: a
    bare column of such a name is a whole-row reference."""
    names: set[str] = set()
    for table in tree.find_all(exp.Table):
        names.add(table.name.lower())
        if table.alias:
            names.add(table.alias.lower())
    for node in tree.find_all(exp.Subquery, exp.CTE):
        if node.alias:
            names.add(node.alias.lower())
    return names


def _is_plain_select_item(node: exp.Expression) -> bool:
    """*node* is one of a SELECT's projection items, bare or under one alias."""
    item = node
    parent = node.parent
    if isinstance(parent, exp.Alias):
        item, parent = parent, parent.parent
    return isinstance(parent, exp.Select) and any(e is item for e in parent.expressions)


def _is_star(item: exp.Expression) -> bool:
    return isinstance(item, exp.Star) or (
        isinstance(item, exp.Column) and isinstance(item.this, exp.Star)
    )


def _is_positional(literal: exp.Literal) -> bool:
    """*literal* is the whole of an ORDER BY item or a GROUP BY item."""
    parent = literal.parent
    if isinstance(parent, exp.Ordered):
        return parent.this is literal
    return isinstance(parent, exp.Group) and any(e is literal for e in parent.expressions)


def _projection(node: exp.Expression) -> list[exp.Expression]:
    """The SELECT list a positional item under *node* refers to (a set operation's first
    branch)."""
    query = node.find_ancestor(exp.Select, exp.SetOperation)
    while isinstance(query, exp.SetOperation):
        query = query.this
    return list(query.expressions) if isinstance(query, exp.Select) else []


def check_strict_sensitive(sql: str, privacy: PrivacySettings | None, dialect: str) -> None:
    """Raise ``SensitiveColumnRejectedError`` when strict mode is on and *sql* uses a
    sensitive column (a pattern, an explicit name, an excluded column's bare name or an
    alias of one) anywhere but as a plain SELECT item, or uses a shape the check cannot
    follow (module docstring). Returns silently otherwise."""
    if privacy is None or not privacy.strict_sensitive_columns:
        return
    tree = _parse_one_statement(sql, dialect)
    laundered = _laundered_names(tree, privacy)
    tables = _table_names(tree)
    for node in tree.walk():
        if isinstance(node, exp.Join):
            if (node.method or "").upper() == "NATURAL":
                raise _reject_shape("a natural join")
            for ident in node.args.get("using") or []:
                if _is_sensitive(privacy, ident.name, laundered):
                    raise _reject_column(ident.name)
        elif isinstance(node, exp.TableAlias) and node.columns:
            raise _reject_shape("a column list on a table alias")
        elif isinstance(node, exp.Star):
            # A bare * is an output (SELECT *) or count(*); t.* is read under its Column.
            if not isinstance(node.parent, (exp.Select, exp.Count, exp.Column)):
                raise _reject_shape("a star outside the SELECT list")
        elif isinstance(node, exp.Column):
            name = node.name
            if isinstance(node.this, exp.Star):
                if not _is_plain_select_item(node):
                    raise _reject_shape("a star outside the SELECT list")
            elif not node.table and name.lower() in tables:
                raise _reject_shape(f"the whole row of {name}")
            elif _is_sensitive(privacy, name, laundered) and not _is_plain_select_item(node):
                raise _reject_column(name)
        elif isinstance(node, exp.Literal) and not node.is_string and _is_positional(node):
            if not node.this.isdigit():
                continue
            items = _projection(node)
            index = int(node.this) - 1
            if not 0 <= index < len(items):
                continue  # the engine reports the bad position
            item = items[index].this if isinstance(items[index], exp.Alias) else items[index]
            if _is_star(item):
                raise _reject_shape("a position that refers to a star")
            if isinstance(item, exp.Column) and _is_sensitive(privacy, item.name, laundered):
                raise _reject_column(item.name)
