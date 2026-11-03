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

The other rules close the ways a sensitive value is read under another name or without its
name: an alias given to a sensitive column (``email AS e``, in a CTE or a derived table too,
transitively) is itself a sensitive name, and so is the output name a set operation's first
branch gives a position that a later branch fills with a sensitive column
(``SELECT id AS x … UNION ALL SELECT email …`` makes ``x`` sensitive; a star in a later
branch, a star in the first branch at or before such a position, or an unaliased expression
there is rejected as a shape); a positional ``ORDER BY 2`` / ``GROUP BY 1`` /
``DISTINCT ON (1)``, in parentheses or inside ``ROLLUP``, ``CUBE`` or ``GROUPING SETS`` too, is
resolved to the projection item it names (an ORDER BY on a
parenthesised query, to that query's), and is rejected when a star stands at or before that
position (on a set operation, against every branch) or the item is a subquery that projects
a star; a number in a window's ORDER BY is not checked as a position (PostgreSQL reads it
as a constant, MySQL refuses it), nor is one in an aggregate's own ORDER BY or a ``WITHIN
GROUP`` (PostgreSQL reads it as a constant, MySQL's ``GROUP_CONCAT`` as one of its own
arguments); a subquery used as a value (a scalar subquery in
WHERE/ON/ORDER BY, an ``IN``/``ANY``/``ARRAY`` operand) is rejected when its own SELECT list
carries a sensitive column, a laundered alias or a star, unless it is itself a plain SELECT
item (masked; its alias is laundered, or kept as a "star" name when the subquery projects a
star, and such a name used anywhere else is a star shape); such an item must have an alias
wherever it stands (an unaliased one is named by the engine: MySQL after its SQL text, a
name ORDER BY, GROUP BY, HAVING and an outer query can use and the check cannot follow;
PostgreSQL after the subquery's own output column, or ``?column?`` for an unnamed
expression, which the column rules see already; requiring the alias everywhere keeps the
rule the same on every engine);
``JOIN ... USING (email)`` and every NATURAL join are rejected; on PostgreSQL (MySQL has no
whole-row references) a whole-row reference (``u::text``, ``to_jsonb(u)``, ``(u).email``,
``SELECT u FROM u``: a bare name that is an alias, or the name of a table or a FROM/LATERAL
function that has none;
also ``u.to_jsonb`` and the other record-taking built-ins and aggregates written as an
attribute, ``result_masking.RECORD_FUNCTIONS``) and everywhere a star anywhere but
as a projection item or in ``count(*)`` are rejected; a column list on a table alias
(``c(e) AS (...)``) is rejected. On MySQL a statement that contains the opener of an
executable comment (``/*!``, ``/*M!``) anywhere in its text is rejected: the server runs the
content, sqlglot drops it. On any other dialect a PostgreSQL ``E'…'`` continued on a later
line is rejected before the parse (``result_masking.has_escape_string_continuation``): the
engine and sqlglot's tokenizer read the continuation differently.

Set operations: only ``UNION ALL`` may carry a sensitive item or a star in a branch; a
``UNION`` that removes duplicates, an ``INTERSECT`` or an ``EXCEPT`` reveals whether values
match, so it is rejected wherever it stands (``_check_comparison``). Every operand must be a
SELECT or a set operation (sqlglot misreads ``TABLE users``), and a VALUES list (which
sqlglot rewrites to ``SELECT * FROM (VALUES …)``) is named as such in the shape rejection.

``DISTINCT`` (MySQL ``DISTINCTROW`` too) and ``GROUP BY ALL`` remove duplicates of the whole
projection, which reveals whether the values of a sensitive item match (a count over a derived
``SELECT DISTINCT email`` is the oracle), so a SELECT with either whose list carries a sensitive
column, a laundered alias, a subquery item carrying one, or a star is rejected wherever it
stands (``_check_distinct``). ``DISTINCT ON (…)`` is not affected (``_removes_duplicates``).

Fail closed: in strict mode every query is parsed, and one that sqlglot cannot read as one
plain statement is rejected whether or not its text names a sensitive column (a whole-row
reference does not). A query nested too deeply for Python's recursion limit (in the parser
or a helper) is rejected too. Pure: no I/O, no settings beyond *privacy*.
"""

from __future__ import annotations

from collections import deque
from typing import NamedTuple

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, ParseError, TokenError

from ..datasources.models import PrivacySettings
from .result_masking import (
    EXECUTABLE_COMMENT,
    RECORD_FUNCTIONS,
    has_escape_string_continuation,
    is_sensitive_name,
)
from .row_filter_errors import SensitiveColumnRejectedError
from .row_filter_rewrite import sqlglot_dialect

_UNPARSED = "Strict sensitive columns: the query could not be parsed, so it was not run."
_UNPARSED_HINT = "Write the query as one plain SELECT statement."
_TOO_DEEP = "Strict sensitive columns: the query is nested too deeply to check, so it was not run."
_SHAPE_HINT = (
    "Name the columns you need explicitly, join with an ON condition and refer to columns"
    " by name, not by position."
)
_EXECUTABLE_COMMENT_HINT = (
    "Remove the /*! ... */ comment: write the query without MySQL executable comments."
)
_ESCAPE_STRING_HINT = (
    "Write each E'...' string as one literal on one line, or join the pieces with ||."
)
_ALIAS_HINT = (
    "Give the subquery item an alias, as in (SELECT ... LIMIT 1) AS name, or leave it out;"
    " the alias is masked like the column."
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


def _reject_compared(column: str) -> SensitiveColumnRejectedError:
    """A sensitive column in a set operation that removes duplicates or compares rows."""
    return SensitiveColumnRejectedError(
        f"Strict sensitive columns: the query compares the column {column} across a UNION,"
        " INTERSECT or EXCEPT, which reveals its values. This connection does not allow that.",
        llm_hint=(
            f"Use UNION ALL, or leave the column {column} out of a UNION, INTERSECT or EXCEPT."
        ),
    )


def _reject_distinct(column: str) -> SensitiveColumnRejectedError:
    """A sensitive column under a DISTINCT or a GROUP BY ALL: the rows kept reveal whether
    its values match."""
    return SensitiveColumnRejectedError(
        f"Strict sensitive columns: the query removes duplicates of the column {column} with"
        " DISTINCT or GROUP BY ALL, which reveals whether its values match. This connection"
        " does not allow that.",
        llm_hint=(
            f"Remove DISTINCT and GROUP BY ALL from any SELECT whose list carries the column"
            f" {column}: the column is masked, so its distinct values would all show the same."
        ),
    )


def _too_deep() -> SensitiveColumnRejectedError:
    return SensitiveColumnRejectedError(_TOO_DEEP, llm_hint="Simplify the query.")


def _reject_shape(what: str, hint: str = _SHAPE_HINT) -> SensitiveColumnRejectedError:
    return SensitiveColumnRejectedError(_shape(what), llm_hint=hint)


def _reject_whole_row(name: str) -> SensitiveColumnRejectedError:
    """A bare name that is also a table or alias of the query: a whole-row reference, or a
    column the check cannot tell apart from one without the schema."""
    return SensitiveColumnRejectedError(
        _shape(f"the whole row of {name}"),
        llm_hint=(
            f"If {name} is a column, write it with its table or alias ({name}.{name}, or an"
            " alias you give the table in FROM). Do not read a whole row as a value, cast it"
            " or pass it to a function."
        ),
    )


def _reject_record_function(table: str, name: str) -> SensitiveColumnRejectedError:
    """``u.to_jsonb``: PostgreSQL reads ``u.f`` as ``f(u)`` for a built-in that takes a
    record, so the item is every column of ``u``."""
    return SensitiveColumnRejectedError(
        _shape(f"the whole row of {table} through {table}.{name}"),
        llm_hint=(
            f"Do not call a function on the whole row ({table}.{name} is {name}({table})):"
            f" name the columns you need. If {name} is a column of {table}, write it without"
            " the qualifier."
        ),
    )


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


# Lower-cased laundered name -> what it carries: "column" (a sensitive value) or "star" (a
# scalar subquery that projects a star, whose columns the check cannot see).
_Laundered = dict[str, str]


def _is_sensitive(privacy: PrivacySettings, name: str, laundered: _Laundered) -> bool:
    return bool(name) and (is_sensitive_name(privacy, name) or name.lower() in laundered)


class _Position(NamedTuple):
    """One item of a later branch of a set operation, with what laundering it needs: the
    first branch and the index of its first star (``None`` without one), computed once."""

    first: exp.Select
    first_star: int | None
    branch: exp.Select
    index: int
    item: exp.Expression


def _laundered_names(tree: exp.Expression, privacy: PrivacySettings) -> _Laundered:
    """Lower-cased names under which a sensitive value is read in *tree*, to a fixed point:
    an alias given to a sensitive column or to a subquery that projects one (``email AS e``,
    then ``e AS f``; ``(SELECT email …) AS e``), and a set operation's first-branch output
    name at a position a later branch fills with one (``_launder_position``, which raises the
    shape rejections of its docstring). An alias of a subquery that projects a star, and an
    alias of such an alias, is kept as a "star" name, not a column: the check cannot tell
    which column it reads.

    A worklist, not repeated passes over the tree. The tasks are every alias and every item
    of a later set-operation branch; each is checked once, and again only when a column
    name under it becomes laundered (at most once per such name). A task costs about the
    size of its item, so a chain of aliases written against the walk order (``c0`` reads
    ``c1``, …) or a wide set operation fed by one costs linear time; only an item that reads
    many laundered names (a subquery with a long SELECT list) is re-checked once per name."""
    names: _Laundered = {}
    tasks: list[exp.Alias | _Position] = [a for a in tree.find_all(exp.Alias) if a.alias]
    for setop in tree.find_all(exp.SetOperation):
        first, *rest = _branches(setop) or [None]
        if first is None:
            continue
        first_star = next(
            (i for i, head in enumerate(first.expressions) if _is_star(_unaliased(head))), None
        )
        for branch in rest:
            for index, item in enumerate(branch.expressions):
                tasks.append(_Position(first, first_star, branch, index, item))
    readers: dict[str, set[int]] = {}
    for index, task in enumerate(tasks):
        under = task.this if isinstance(task, exp.Alias) else task.item
        for column in under.find_all(exp.Column):
            readers.setdefault(column.name.lower(), set()).add(index)
    queue = deque(range(len(tasks)))
    queued = [True] * len(tasks)
    while queue:
        index = queue.popleft()
        queued[index] = False
        task = tasks[index]
        if isinstance(task, exp.Alias):
            added = _launder_alias(task, privacy, names)
        else:
            added = _launder_position(task, privacy, names)
        if added is None:
            continue
        for reader in readers.get(added, ()):
            if not queued[reader]:
                queued[reader] = True
                queue.append(reader)
    return names


def _launder_alias(alias: exp.Alias, privacy: PrivacySettings, names: _Laundered) -> str | None:
    """Add *alias*'s name to *names* when the item it names is sensitive; the name added."""
    name = alias.alias.lower()
    if name in names:
        return None
    found = _sensitive_item(alias, privacy, names)
    if found is None:
        return None
    names[name] = found[0]
    return name


def _table_names(tree: exp.Expression) -> set[str]:
    """Lower-cased names under which a row of *tree* can be read whole: every row source's
    alias, or the name PostgreSQL gives it when it has none (an aliased table's own name is
    hidden; an unaliased function in FROM or under LATERAL, ``unnest(…)`` included, is named
    after the function), and the alias of every derived table, VALUES list and CTE. On
    PostgreSQL a bare column of such a name is a whole-row reference."""
    names: set[str] = set()
    for source in tree.find_all(exp.Table, exp.Lateral, exp.Unnest):
        if isinstance(source, exp.Unnest) and not isinstance(source.parent, (exp.From, exp.Join)):
            continue  # a call in an expression, or under a LATERAL that names it
        name = source.alias or _unaliased_source_name(source)
        if name:
            names.add(name.lower())
    for node in tree.find_all(exp.Subquery, exp.CTE, exp.Values):
        if node.alias:
            names.add(node.alias.lower())
    return names


def _unaliased_source_name(source: exp.Expression) -> str:
    """The name PostgreSQL gives an unaliased row source: a table's name, a function's name
    (``generate_series(1, 3)`` is ``generate_series``, ``public.my_fn(1)`` is ``my_fn``, under
    LATERAL too);
    ``""`` when there is none to give (a LATERAL subquery must have an alias)."""
    if isinstance(source, exp.Table) and not isinstance(source.this, exp.Func):
        return source.name
    func = source if isinstance(source, exp.Unnest) else source.this
    while isinstance(func, exp.Dot):  # LATERAL public.f(…): Dot(public, f(…))
        func = func.expression
    if not isinstance(func, exp.Func):
        return ""
    if isinstance(func, exp.Anonymous):
        return func.name
    # A typed function (sqlglot's ExplodingGenerateSeries): its PostgreSQL spelling.
    return func.sql(dialect="postgres").split("(", 1)[0].rsplit(".", 1)[-1]


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


# Parents under which a query's projection is an output or a row source, not a value: the
# statement itself (no parent), FROM/JOIN/LATERAL, a CTE body, a set-operation branch and an
# EXISTS test (its projection is not read as a value). A set operation inside any of them
# still compares its rows: ``_check_comparison`` covers that wherever it stands, an EXISTS
# included (``EXISTS (SELECT u.email INTERSECT SELECT 'x')``).
_SOURCE_PARENTS = (exp.From, exp.Join, exp.Lateral, exp.CTE, exp.SetOperation, exp.Exists)
_WRAPPERS = (exp.Subquery, exp.Paren)


def _unaliased(item: exp.Expression) -> exp.Expression:
    return item.this if isinstance(item, exp.Alias) else item


def _outer_wrapper(node: exp.Expression) -> exp.Expression:
    """*node*, or the outermost parenthesis/subquery wrapper directly around it."""
    while isinstance(node.parent, _WRAPPERS):
        node = node.parent
    return node


def _query_of(node: exp.Expression) -> exp.Select | exp.SetOperation | None:
    """The query inside *node*'s parenthesis/subquery wrappers; ``None`` for anything else."""
    while isinstance(node, _WRAPPERS):
        node = node.this
    return node if isinstance(node, (exp.Select, exp.SetOperation)) else None


def _branches(query: exp.Expression | None) -> list[exp.Select]:
    """The SELECTs of *query* through its set operations and wrappers, left to right."""
    if isinstance(query, exp.SetOperation):
        return _branches(query.this) + _branches(query.expression)
    if isinstance(query, _WRAPPERS):
        return _branches(query.this)
    return [query] if isinstance(query, exp.Select) else []


# What sqlglot hangs around a parenthesised query it reads with a trailing ORDER BY or LIMIT
# in some places (``EXISTS ((SELECT …) ORDER BY 1 LIMIT 1)`` is Exists(Limit(Order(Subquery)))):
# the query is their ``this``, and they sort or cut its rows, not a value.
_QUERY_MODIFIERS = (exp.Order, exp.Limit)


def _is_expression_query(query: exp.Expression) -> bool:
    """*query* is used as a value: a scalar subquery, an IN/ANY/ALL/ARRAY operand, an ORDER
    BY item, a SELECT item. Not the statement, not a source (``_SOURCE_PARENTS``), seen
    through wrappers and through an ORDER BY or LIMIT on the parenthesised query."""
    node = _outer_wrapper(query)
    while isinstance(node.parent, _QUERY_MODIFIERS) and node.parent.this is node:
        node = _outer_wrapper(node.parent)
    parent = node.parent
    return parent is not None and not isinstance(parent, _SOURCE_PARENTS)


def _is_values_branch(branch: exp.Select) -> bool:
    """*branch* is sqlglot's rewrite of a VALUES list: ``SELECT * FROM (VALUES …)``."""
    source = branch.args.get("from_")
    return (
        isinstance(source, exp.From)
        and isinstance(source.this, exp.Values)
        and len(branch.expressions) == 1
        and _is_star(branch.expressions[0])
    )


def _set_operation_star(branch: exp.Select, what: str) -> SensitiveColumnRejectedError:
    """The shape rejection for a star in *branch* of a set operation, named after the VALUES
    list when the star is sqlglot's rewrite of one."""
    return _reject_shape("a VALUES list in a set operation" if _is_values_branch(branch) else what)


def _check_operands(tree: exp.Expression) -> None:
    """Every operand of every set operation in *tree*, through wrappers, is a SELECT or a
    set operation. sqlglot reads ``TABLE users`` as a table named TABLE (nested) or a
    column named TABLE (top level), whose columns the check cannot see. A raw ``Values``
    operand (a parenthesised VALUES list, ``SELECT 1 UNION ALL (VALUES (1))``, which sqlglot
    does not rewrite to a SELECT) is rejected as "a VALUES list in a set operation"."""
    for setop in tree.find_all(exp.SetOperation):
        for operand in (setop.this, setop.expression):
            inner = operand
            while isinstance(inner, _WRAPPERS):
                inner = inner.this
            if isinstance(inner, exp.Values):
                raise _reject_shape("a VALUES list in a set operation")
            if not isinstance(inner, (exp.Select, exp.SetOperation)):
                raise _reject_shape("a set operation operand the check cannot read")


def _check_comparison(
    setop: exp.SetOperation, privacy: PrivacySettings, laundered: _Laundered
) -> None:
    """Reject a set operation other than ``UNION ALL`` (a UNION that removes duplicates, any
    INTERSECT or EXCEPT) when a branch carries a sensitive item: removing duplicates and
    comparing rows reveal whether values match (``SELECT email … INTERSECT SELECT 'x'``)."""
    if isinstance(setop, exp.Union) and not setop.args.get("distinct"):
        return
    for branch in reversed(_branches(setop)):
        found = _sensitive_projection(branch, privacy, laundered)
        if found is None:
            continue
        if found[0] == "star":
            raise _set_operation_star(branch, "a star in a UNION, INTERSECT or EXCEPT")
        raise _reject_compared(found[1])


def _removes_duplicates(select: exp.Select) -> bool:
    """*select* removes duplicate rows of its whole projection: a plain ``DISTINCT``
    (``DISTINCTROW`` parses the same) or a ``GROUP BY ALL``. Not ``DISTINCT ON (…)``: its
    expressions are checked as column uses, and the projection is not compared."""
    distinct = select.args.get("distinct")
    if isinstance(distinct, exp.Distinct) and distinct.args.get("on") is None:
        return True
    group = select.args.get("group")
    return isinstance(group, exp.Group) and bool(group.args.get("all"))


def _check_distinct(select: exp.Select, privacy: PrivacySettings, laundered: _Laundered) -> None:
    """Reject *select* when it removes duplicates and its projection carries a sensitive item
    (``_sensitive_item``: a column, a laundered alias, a subquery item carrying one) or a
    star (the check cannot see what the star brings in)."""
    if not _removes_duplicates(select):
        return
    found = _sensitive_projection(select, privacy, laundered)
    if found is None:
        return
    if found[0] == "star":
        raise _reject_shape("a DISTINCT or GROUP BY ALL over a star")
    raise _reject_distinct(found[1])


def _sensitive_item(
    item: exp.Expression, privacy: PrivacySettings, laundered: _Laundered
) -> tuple[str, str] | None:
    """What a projection *item*, bare or aliased, carries when its values are sensitive:
    ``("column", name)`` for a sensitive column or a laundered alias, ``("star", "*")`` for
    ``*``, ``t.*`` or a laundered "star" name, and for a subquery what its own projection
    carries (any branch). ``None`` otherwise; an expression over a sensitive column is
    rejected by the walk, so it never needs to be followed here."""
    inner = _unaliased(item)
    if _is_star(inner):
        return ("star", "*")
    if isinstance(inner, exp.Column):
        name = inner.name
        if not _is_sensitive(privacy, name, laundered):
            return None
        if not is_sensitive_name(privacy, name) and laundered[name.lower()] == "star":
            return ("star", "*")
        return ("column", name)
    return _sensitive_projection(_query_of(inner), privacy, laundered)


def _sensitive_projection(
    query: exp.Expression | None, privacy: PrivacySettings, laundered: _Laundered
) -> tuple[str, str] | None:
    """The first sensitive item (``_sensitive_item``) in any branch of *query*. Later
    branches first: a set operation launders the first branch's output name at a position a
    later branch fills with a sensitive column (``_laundered_names``), and the rejection
    should name that column, not the laundered name."""
    for branch in reversed(_branches(query)):
        for item in branch.expressions:
            found = _sensitive_item(item, privacy, laundered)
            if found is not None:
                return found
    return None


def _output_name(item: exp.Expression) -> str | None:
    """The name a first branch gives its output: the alias, or a plain column's name. An
    unaliased expression gets an engine-chosen name the check does not predict."""
    if isinstance(item, exp.Alias):
        return item.alias or None
    return item.name if isinstance(item, exp.Column) and not _is_star(item) else None


def _launder_position(
    position: _Position, privacy: PrivacySettings, names: _Laundered
) -> str | None:
    """Add to *names* the first-branch output name at *position* when the later branch's
    item there is sensitive: the engine merges the columns, so
    ``SELECT id AS x … UNION ALL SELECT email …`` makes ``x`` an email. Returns the name
    added. Rejects what the mapping cannot follow: a star in a later branch, a star in the
    first branch at or before such a position (where inside its expansion the position
    lands is unknown) and an unnamed first-branch output there."""
    first, first_star, branch, index, item = position
    found = _sensitive_item(item, privacy, names)
    if found is None:
        return None
    if found[0] == "star":
        raise _set_operation_star(branch, "a star in a later branch of a set operation")
    if first_star is not None and first_star <= index:
        raise _set_operation_star(first, "a star in the first branch of a set operation")
    heads = first.expressions
    if index >= len(heads):
        return None  # the engine reports the branch mismatch
    output = _output_name(heads[index])
    if output is None:
        raise _reject_shape("a set operation that reads a sensitive column under an unnamed output")
    if output.lower() in names:
        return None
    names[output.lower()] = "column"
    return output.lower()


_GROUPING_SETS = (exp.Rollup, exp.Cube, exp.GroupingSets)


def _position_query(literal: exp.Literal) -> exp.Expression | None:
    """The query whose SELECT list *literal* names a position of, or ``None`` when it is not
    a position. A position is the whole of an ORDER BY item, a GROUP BY item or a
    ``DISTINCT ON`` element, through any parentheses around it (``ORDER BY (2)``,
    ``GROUP BY (1)``, ``DISTINCT ON ((2))``). Under a GROUP BY a number nested at any depth in
    parentheses, tuples, ``ROLLUP``, ``CUBE`` or ``GROUPING SETS`` is a position too
    (``GROUP BY (1, 2)``, ``GROUPING SETS (ROLLUP (1))``), while under ORDER BY only
    parentheses around the number itself count (``ORDER BY (2, 1)`` sorts by a constant row).
    PostgreSQL resolves all of these to the projection item, MySQL the plain and
    parenthesised ones (it has no ``ROLLUP (...)`` syntax, only ``GROUP BY ... WITH ROLLUP``,
    whose items are plain GROUP BY items). An ORDER BY that belongs to a function call is
    not a query's (``_sorted_query``)."""
    node: exp.Expression = literal
    while isinstance(node.parent, exp.Paren):
        node = node.parent
    parent = node.parent
    if isinstance(parent, exp.Ordered):
        return _sorted_query(parent) if parent.this is node else None
    if isinstance(parent, exp.Distinct):
        positional = parent.args.get("on") is node
    elif isinstance(parent, exp.Tuple) and isinstance(parent.parent, exp.Distinct):
        positional = parent.parent.args.get("on") is parent
    else:
        while isinstance(parent, (exp.Paren, exp.Tuple, *_GROUPING_SETS)):
            node, parent = parent, parent.parent
        positional = isinstance(parent, exp.Group) and any(e is node for e in parent.expressions)
    return _enclosing_query(literal) if positional else None


def _enclosing_query(node: exp.Expression) -> exp.Expression | None:
    """The query a clause under *node* belongs to. An ORDER BY on a parenthesised query hangs
    on its ``Subquery`` wrapper and refers to the query inside."""
    return node.find_ancestor(exp.Select, exp.SetOperation, exp.Subquery)


def _sorted_query(ordered: exp.Ordered) -> exp.Expression | None:
    """The query an ORDER BY item sorts, whose SELECT list a number in it names: the query
    the ORDER BY hangs on, or the parenthesised query sqlglot puts under it
    (``EXISTS ((SELECT id, email FROM u) ORDER BY 1)`` is Exists(Order(Subquery)), and the 1
    is the inner ``id``). ``None`` for an ORDER BY of a function call: a window
    (``OVER (ORDER BY 1)``), ``WITHIN GROUP (ORDER BY 1)`` or an aggregate's own
    (``string_agg(name, ',' ORDER BY 1)``, ``array_agg(id ORDER BY (2))``). PostgreSQL reads
    that number as a constant, MySQL refuses it in a window (error 3592) and reads it in
    ``GROUP_CONCAT`` as a position among the function's own arguments, whose columns the
    walk checks anyway, so it is never a position in the SELECT list."""
    order = ordered.parent
    if isinstance(order, exp.Order):
        if isinstance(order.this, exp.Expression) and _query_of(order.this) is not None:
            return order.this
        if isinstance(order.parent, (exp.Window, exp.WithinGroup, exp.Func)):
            return None
    return _enclosing_query(ordered)


def _projections(query: exp.Expression | None) -> list[list[exp.Expression]]:
    """The SELECT lists a position in *query* can refer to: the query's own list, or every
    branch's list when it is a set operation (the engine sorts the merged column, so the
    position reaches every branch's item there)."""
    return [list(branch.expressions) for branch in _branches(query)]


def check_strict_sensitive(
    sql: str, privacy: PrivacySettings | None, dialect: str
) -> exp.Expression | None:
    """Raise ``SensitiveColumnRejectedError`` when strict mode is on and *sql* uses a
    sensitive column (a pattern, an explicit name, an excluded column's bare name or an
    alias of one) anywhere but as a plain SELECT item, or uses a shape the check cannot
    follow (module docstring). Returns the parsed tree when strict mode is on, so the
    row-filter rewriter can reuse it (``row_filter._executable``), ``None`` otherwise."""
    if privacy is None or not privacy.strict_sensitive_columns:
        return None
    try:
        return _check(sql, privacy, dialect)
    except RecursionError as exc:  # in the parser or in a recursive helper
        raise _too_deep() from exc


def _check_escape_strings(sql: str) -> None:
    """Reject a PostgreSQL ``E'…'`` continued on a later line: PostgreSQL reads the next
    segment as part of the escape string, sqlglot's tokenizer as a standard string, so where
    the literal ends differs. Result masking's lexer check
    (``result_masking.has_escape_string_continuation``), run before the parse so the refusal
    does not depend on sqlglot failing on it; any other lexer error is left to the parse,
    which fails closed on its own."""
    if has_escape_string_continuation(sql):
        raise _reject_shape("an escape string continued on a later line", _ESCAPE_STRING_HINT)


def _check(sql: str, privacy: PrivacySettings, dialect: str) -> exp.Expression:
    mysql = (dialect or "").lower() == "mysql"
    if mysql and EXECUTABLE_COMMENT.search(sql):
        # The server runs the content, sqlglot drops it: a condition inside is never seen.
        # Checked on the raw text, so the opener inside a string is refused too.
        raise _reject_shape("a MySQL executable comment (/*! ... */)", _EXECUTABLE_COMMENT_HINT)
    if not mysql:
        _check_escape_strings(sql)
    tree = _parse_one_statement(sql, dialect)
    _check_operands(tree)
    laundered = _laundered_names(tree, privacy)
    for setop in tree.find_all(exp.SetOperation):
        _check_comparison(setop, privacy, laundered)
    for select in tree.find_all(exp.Select):
        _check_distinct(select, privacy, laundered)
    tables = _table_names(tree)
    # Attribute notation (u.to_jsonb is to_jsonb(u)) exists on PostgreSQL only.
    attribute_calls = sqlglot_dialect(dialect) == "postgres"
    for node in tree.walk():
        if isinstance(node, exp.Join):
            if (node.method or "").upper() == "NATURAL":
                raise _reject_shape("a natural join")
            for ident in node.args.get("using") or []:
                if _is_sensitive(privacy, ident.name, laundered):
                    if _sensitive_item(exp.column(ident.name), privacy, laundered) == (
                        "star",
                        "*",
                    ):
                        raise _reject_shape("a star inside a subquery used as a value")
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
            elif (
                attribute_calls
                and node.table
                and node.table.lower() in tables
                and name.lower() in RECORD_FUNCTIONS
            ):
                raise _reject_record_function(node.table, name)
            elif not mysql and not node.table and name.lower() in tables:
                # MySQL has no whole-row reference: a bare name there is always a column.
                raise _reject_whole_row(name)
            elif _is_sensitive(privacy, name, laundered) and not _is_plain_select_item(node):
                if _sensitive_item(node, privacy, laundered) == ("star", "*"):
                    raise _reject_shape("a star inside a subquery used as a value")
                raise _reject_column(name)
        elif isinstance(node, (exp.Select, exp.SetOperation)) and _is_expression_query(node):
            found = _sensitive_projection(node, privacy, laundered)
            if found is None:
                continue
            item = _outer_wrapper(node)
            if _is_plain_select_item(item):
                # A scalar subquery as a SELECT item: masked, when it has an alias. An
                # unaliased one is named by the engine: MySQL after its SQL text, a name
                # ORDER BY, GROUP BY, HAVING and an outer query can use and the check
                # cannot follow (PostgreSQL uses the inner column's name, which the
                # column rules see; the alias is required on every engine alike).
                if not isinstance(item.parent, exp.Alias):
                    raise _reject_shape(
                        "an unnamed subquery item that reads a sensitive column", _ALIAS_HINT
                    )
                continue
            if found[0] == "star":
                raise _reject_shape("a star inside a subquery used as a value")
            raise _reject_column(found[1])
        elif isinstance(node, exp.Literal) and not node.is_string:
            query = _position_query(node)
            if query is None or not node.this.isdigit():
                continue
            index = int(node.this) - 1
            if index < 0:
                continue  # the engine refuses position 0
            # Later branches first: the first branch's name at this position may be
            # laundered by a later branch, and the message should name the column itself.
            for items in reversed(_projections(query)):
                # A star at or before the position expands to an unknown number of columns,
                # so the position can land anywhere inside it (SELECT * … ORDER BY 2).
                if any(_is_star(_unaliased(item)) for item in items[: index + 1]):
                    raise _reject_shape("a position that refers to a star")
                if index >= len(items):
                    continue  # the engine reports the bad position
                found = _sensitive_item(items[index], privacy, laundered)
                if found is None:
                    continue
                if found[0] == "star":  # a scalar subquery item that projects a star
                    raise _reject_shape("a position that refers to a star")
                raise _reject_column(found[1])
    return tree
