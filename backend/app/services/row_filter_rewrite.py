# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Per-table row filtering: every base-table reference the admin configured a filter for
becomes a filtered derived table, ``(SELECT * FROM t WHERE (<condition>) OFFSET 0) AS t`` on
PostgreSQL (``LIMIT 18446744073709551615`` on MySQL), fenced so the condition runs first.

The statement is parsed with sqlglot, the matching ``Table`` nodes are replaced and the
tree is regenerated; no text is spliced. Anything the rewriter cannot read or write with
certainty raises ``RowFilterRejectedError`` and the statement is never sent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import ErrorLevel, ParseError, TokenError, UnsupportedError
from sqlglot.tokens import TokenType

from .row_filter_errors import RowFilterRejectedError

# Adapter ``query_dialect`` (lower-cased) -> sqlglot dialect name.
_SQLGLOT_DIALECT = {"postgresql": "postgres", "mysql": "mysql"}

# sqlglot dialect -> how a filtered derived table is fenced. PostgreSQL neither pulls up nor
# pushes quals into a subquery with OFFSET/LIMIT; MySQL neither merges a derived table with
# LIMIT nor pushes conditions into it (18446744073709551615 is its documented "all rows";
# MySQL has no bare OFFSET). A dialect absent here is refused, never fenced by guess.
_FENCE: dict[str, Callable[[exp.Select], exp.Select]] = {
    "postgres": lambda select: select.offset(0),
    "mysql": lambda select: select.limit(18446744073709551615),
}


# Functions that run SQL passed as a string, or read a whole table named by a string: their
# rows never pass through a Table node, so the rewrite cannot filter them.
_UNFILTERABLE_FUNCTION = re.compile(
    r"^(?:(?:query|table|cursor|schema|database)_to_xml\w*"
    r"|ts_stat|ts_rewrite|crosstab\d*|connectby|dblink\w*)$",
    re.IGNORECASE,
)
# Catalog views that expose column values (most common values, histograms, minimum and
# maximum) of every row, per sqlglot dialect, as case-folded (schema, name) pairs. Schema
# ``None`` is an unqualified reference, which PostgreSQL resolves in the catalog first.
_VALUE_STATISTICS_VIEWS: dict[str, frozenset[tuple[str | None, str]]] = {
    "postgres": frozenset(
        (schema, name)
        for schema in (None, "pg_catalog")
        for name in (
            "pg_stats",
            "pg_stats_ext",
            "pg_stats_ext_exprs",
            "pg_statistic",
            "pg_statistic_ext_data",
        )
    ),
    # MariaDB's mysql.column_stats holds the engine-independent statistics: per-column
    # minimum, maximum and histogram, readable with SELECT on the mysql schema. Unqualified,
    # either name is a table of the current database, which can be information_schema or
    # mysql itself (USE), so the bare names are refused too.
    "mysql": frozenset(
        (schema, name)
        for schema, name in (
            ("information_schema", "column_statistics"),
            ("mysql", "column_stats"),
        )
        for schema in (schema, None)
    ),
}


# PostgreSQL keeps the first 63 bytes of every identifier (NAMEDATALEN - 1), quoted or not,
# cutting at a character boundary. MySQL refuses longer names instead.
_PG_MAX_NAME_BYTES = 63


def _engine_name(name: str, read: str) -> str:
    """*name* as the engine stores it: truncated to 63 bytes on PostgreSQL."""
    if read != "postgres":
        return name
    raw = name.encode("utf-8", errors="surrogatepass")
    if len(raw) <= _PG_MAX_NAME_BYTES:
        return name
    return raw[:_PG_MAX_NAME_BYTES].decode("utf-8", errors="ignore")


def sqlglot_dialect(dialect: str) -> str | None:
    """The sqlglot dialect for an adapter's ``query_dialect``; ``None`` when unknown."""
    return _SQLGLOT_DIALECT.get((dialect or "").lower())


def parse_condition(condition: str, dialect: str | None) -> exp.Expression:
    """*condition* parsed as one boolean expression; ``ParseError`` otherwise, or
    ``TokenError`` (not a ``ParseError`` subclass) for an unterminated string or an unknown
    character. Callers catch both.

    ``into=exp.Condition`` makes sqlglot consume every token as part of a single expression,
    so ``tenant_id = 7 UNION SELECT 1``, ``... LIMIT 5``, ``... FOR UPDATE``, a leading
    ``AND`` and unbalanced parentheses all fail to parse.
    """
    return sqlglot.parse_one(condition, read=dialect, into=exp.Condition)


@dataclass(frozen=True)
class _Filter:
    schema: str
    table: str
    condition: exp.Expression

    @property
    def key(self) -> str:
        return f"{self.schema}.{self.table}"


def _filters(row_filters: dict[str, str], read: str) -> list[_Filter]:
    out: list[_Filter] = []
    for key, condition in row_filters.items():
        parts = key.split(".")
        if len(parts) != 2 or any(not p or p != p.strip() for p in parts):
            # validate_row_filters stores only schema.table keys; anything else would match
            # no reference and leave the table unfiltered.
            raise RowFilterRejectedError(
                "A row filter key is not in the form schema.table, so the query was not run."
            )
        schema, table = parts
        try:
            parsed = parse_condition(condition, read)
        except (ParseError, TokenError) as exc:
            # Every stored value was parsed at save time; only a sqlglot upgrade gets here.
            raise RowFilterRejectedError(
                f"The row filter for {key} is not a valid condition for this dialect,"
                " so the query was not run."
            ) from exc
        if _reject_unwritable(parsed, condition, read) is not None:
            raise RowFilterRejectedError(
                f"The row filter for {key} uses a construct the row-filter rewriter cannot"
                " write back for this dialect, so the query was not run."
            )
        parsed = _spell_functions(parsed, read)
        out.append(_Filter(_engine_name(schema, read), _engine_name(table, read), parsed))
    return out


def _normalised(identifier: exp.Identifier, dialect: Dialect) -> str:
    """The name as the engine resolves it: an unquoted identifier folded by the dialect
    (PostgreSQL lowercases, MySQL keeps the case), a quoted one kept as written."""
    return dialect.normalize_identifier(identifier.copy()).name


def _cte_names(tree: exp.Expression, dialect: Dialect) -> set[str]:
    names: set[str] = set()
    for cte in tree.find_all(exp.CTE):
        alias = cte.args.get("alias")
        if isinstance(alias, exp.TableAlias) and isinstance(alias.this, exp.Identifier):
            names.add(_normalised(alias.this, dialect))
    return names


def _reject_unfilterable(tree: exp.Expression, read: str) -> None:
    """Shapes that read rows without a ``Table`` node the rewrite could replace.

    A ``Describe`` or ``Command`` node carries raw text into the output (sqlglot parses
    ``FROM describe d`` as a DESCRIBE, which PostgreSQL reads as the table ``describe``).
    A function on ``_UNFILTERABLE_FUNCTION`` runs SQL from a string or reads a table named
    by a string. A catalog view on ``_VALUE_STATISTICS_VIEWS`` exposes values of every row;
    it is matched in its own schema only, so a user table with the same name elsewhere runs.
    Residual risk: a function not on the list that does the same (an extension, a
    user-defined function) is not caught; native row-level security is the complete answer.
    """
    if tree.find(exp.Describe, exp.Command):
        raise RowFilterRejectedError(
            "The query contains a construct the row filter cannot inspect, so it was not run.",
            llm_hint="Write the query as one plain SELECT statement.",
        )
    for func in tree.find_all(exp.Func):
        name = func.name if isinstance(func, exp.Anonymous) else func.sql_name()
        if _UNFILTERABLE_FUNCTION.match(name):
            text = (
                f"The function {name.lower()} reads rows the row filter cannot see,"
                " so the query was not run."
            )
            raise RowFilterRejectedError(
                text, llm_hint=f"Write the query without the function {name.lower()}."
            )
    views = _VALUE_STATISTICS_VIEWS[read]
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            continue
        schema = table.args.get("db")
        view = table.name.casefold()
        if (schema.name.casefold() if isinstance(schema, exp.Identifier) else None, view) in views:
            text = (
                f"The catalog view {view} exposes values the row filter"
                " cannot see, so the query was not run."
            )
            raise RowFilterRejectedError(
                text, llm_hint=f"Write the query without the catalog view {view}."
            )


def _reject_cte_collisions(ctes: set[str], filters: list[_Filter], read: str) -> None:
    """A CTE named like a filtered table is rejected. Inside its own (non-recursive) body
    the bare name is the base table, outside it is the CTE, and the rewriter does not track
    that scope; treating every such reference as the CTE would leave the base table
    unfiltered. The comparison is case-insensitive, like the loose match in ``_match``."""
    for f in filters:
        table = _engine_name(f.table, read).casefold()
        if any(_engine_name(name, read).casefold() == table for name in ctes):
            text = (
                f"The query defines a CTE named like the filtered table {f.key}, which the"
                " row filter cannot tell from the table, so it was not run."
            )
            raise RowFilterRejectedError(
                text,
                llm_hint=f"Rename the CTE so that it differs from the filtered table {f.key}.",
            )


def _match(table: exp.Table, filters: list[_Filter], dialect: Dialect, read: str) -> _Filter | None:
    """The filter for *table*, ``None`` when the admin listed no such table.

    Exactly one exact match (after dialect normalisation) applies. Several exact matches
    (a bare name present in two filtered schemas) and a match that is only
    case-insensitive are rejected: a reference that could reach a filtered table under
    some engine setting is never sent unfiltered.
    """
    name = _engine_name(_normalised(table.this, dialect), read)
    schema_id = table.args.get("db")
    schema = (
        _engine_name(_normalised(schema_id, dialect), read)
        if isinstance(schema_id, exp.Identifier)
        else None
    )
    reference = f"{schema}.{name}" if schema else name

    exact = [f for f in filters if f.table == name and (schema is None or f.schema == schema)]
    if len(exact) == 1:
        return exact[0]
    if exact:
        text = (
            f"Table {name} is referenced without a schema and a row filter exists for it"
            " in more than one schema."
        )
        raise RowFilterRejectedError(text, llm_hint=f"Qualify the table {name} as schema.table.")
    loose = [
        f
        for f in filters
        if f.table.casefold() == name.casefold()
        and (schema is None or f.schema.casefold() == schema.casefold())
    ]
    if loose:
        text = f"Table {reference} does not match the filtered table {loose[0].key} exactly."
        # The hint gives the dialect's quoted form: unquoted, PostgreSQL folds public.Orders
        # to public.orders and refuses it again, and the model could spend every correction
        # attempt on the case. The hint is not shown to the user, so quotes are fine here.
        quoted = exp.Table(
            this=exp.to_identifier(loose[0].table, quoted=True),
            db=exp.to_identifier(loose[0].schema, quoted=True),
        ).sql(dialect=dialect)
        raise RowFilterRejectedError(
            text,
            llm_hint=f"Write the table {reference} as {quoted}, with that exact case and quoting.",
        )
    return None


# Bare words PostgreSQL reads as the session's value, never as a column (a column of that
# name must be quoted), and that sqlglot parses as a column.
_PG_VALUE_KEYWORDS = frozenset({"user", "current_role", "system_user"})


def _qualified(condition: exp.Expression, table: exp.Identifier, read: str) -> exp.Expression:
    """A copy of *condition* whose unqualified columns outside any nested query are qualified
    with *table*, the derived table's inner table, so they cannot resolve anywhere else.

    Inside a subquery the inner SELECT of the derived table sees the outer queries: a filter
    ``org_id = 7`` on a table that lost ``org_id`` after the save would compare an outer
    query's ``org_id`` and pass every row. ``orders.org_id`` fails instead. Columns of a
    subquery inside the condition resolve in that subquery first and are left as written.
    On PostgreSQL a bare column named like the table (``orders IS NOT NULL``,
    ``(orders).tenant_id``) is the whole row of the inner table when the table has no such
    column, which no outer query can supply first; it is left as written, since
    ``orders.orders`` would name a column the table does not have.
    """
    dialect = Dialect.get_or_raise(read)
    table_name = _normalised(table, dialect)
    out = condition.copy()
    for column in list(out.find_all(exp.Column)):
        ident = column.this
        if (
            column.args.get("table") is not None
            or not isinstance(ident, exp.Identifier)
            or column.find_ancestor(exp.Query) is not None
            or (
                read == "postgres"
                and (
                    (not ident.quoted and ident.name.lower() in _PG_VALUE_KEYWORDS)
                    or _normalised(ident, dialect) == table_name
                )
            )
        ):
            continue
        column.set("table", table.copy())
    return out


def _in_subquery(table: exp.Table, tree: exp.Expression) -> bool:
    """Whether *table* sits anywhere but in the FROM of the outermost SELECT (or of a branch
    of an outermost set operation). There a derived table sees no outer query, so a column
    the table lacks is an error whether qualified or not; anywhere else, conservatively
    including a CTE body and a parenthesised branch, it may see one."""
    node = table.find_ancestor(exp.Select)
    while node is not None and node is not tree:
        parent = node.parent
        if not isinstance(parent, exp.SetOperation) or node.arg_key not in ("this", "expression"):
            return True
        node = parent
    return node is None


def _filtered(
    table: exp.Table, condition: exp.Expression, read: str, *, qualify: bool = True
) -> exp.Subquery:
    """``(SELECT * FROM <table without alias> WHERE (<condition>) <fence>) AS <alias or table
    name>``, with the condition's columns qualified with the table name when *qualify*
    (``_qualified``).

    The fence (``_FENCE``) makes the engine evaluate the condition before anything the outer
    query does with the rows. Without it a predicate the model wrote can run on hidden rows,
    and a data-dependent error (``1/0`` on PostgreSQL, a bad regular expression or JSON path
    on MySQL) tells whether such a row exists, one bit per query, in text the correction
    prompt sees. Neither fence changes a result. Cost: the model's predicates cannot use an
    index on the base table unless the condition itself does; on MySQL every filtered
    reference is materialized in full per query, so an outer LIMIT does not reduce the work,
    and on PostgreSQL a correlated subquery over a filtered table rescans the filtered rows
    per outer row.
    """
    alias = table.args.get("alias")
    alias = alias.copy() if alias else exp.TableAlias(this=table.this.copy())
    inner = table.copy()
    inner.set("alias", None)
    where = _qualified(condition, table.this, read) if qualify else condition.copy()
    select = exp.select("*").from_(inner).where(exp.Paren(this=where))
    fence = _FENCE.get(read)
    if fence is None:
        raise RowFilterRejectedError(
            "Row filters have no fence for this database dialect, so the query was not run."
        )
    select = fence(select)
    return exp.Subquery(this=select, alias=alias)


def _sources(
    tree: exp.Expression, targets: list[exp.Table], dialect: Dialect
) -> list[tuple[str, exp.Table | None]]:
    """Every FROM source of *tree* by the name a column qualifier reaches it under
    (case-folded), with its ``Table`` node when it is one: a table by its alias or name, a
    derived table, ``UNNEST`` or ``VALUES`` list by its alias, an unaliased table function
    by its name."""
    out: list[tuple[str, exp.Table | None]] = []
    for table in targets:
        alias = table.args.get("alias")
        ident = alias.this if isinstance(alias, exp.TableAlias) else table.this
        if isinstance(ident, exp.Anonymous):
            # An unaliased table function goes by the function's name: FROM orders(1).
            ident = (
                ident.this
                if isinstance(ident.this, exp.Identifier)
                else (exp.to_identifier(ident.name) if ident.name else None)
            )
        if isinstance(ident, exp.Identifier):
            out.append((_normalised(ident, dialect).casefold(), table))
    for alias in tree.find_all(exp.TableAlias):
        if not isinstance(alias.parent, (exp.Table, exp.CTE)) and isinstance(
            alias.this, exp.Identifier
        ):
            out.append((_normalised(alias.this, dialect).casefold(), None))
    return out


def _drop_schema_qualifiers(
    columns: list[exp.Column],
    bare: dict[int, _Filter],
    sources: list[tuple[str, exp.Table | None]],
    dialect: Dialect,
    read: str,
) -> None:
    """``schema.table.column`` naming an unaliased filtered reference becomes
    ``table.column`` (``catalog.schema.table.column`` is refused): the reference is now a
    derived table aliased with the bare table name, which a schema-qualified column no
    longer reaches.

    Refused when another source of the statement goes by the same bare name (another
    schema's table, an alias, a derived table): ``table.column`` could then resolve to it.
    A filtered reference with an alias is left alone, since the engine already refused
    ``schema.table.column`` against it before the rewrite.
    """
    filters = {(f.schema, f.table): f for f in bare.values()}
    hits: list[tuple[exp.Column, _Filter]] = []
    for column in columns:
        key = (
            _engine_name(_normalised(column.args["db"], dialect), read),
            _engine_name(_normalised(column.args["table"], dialect), read),
        )
        if key in filters:
            hits.append((column, filters[key]))
    for f in {id(f): f for _, f in hits}.values():
        name = f.table.casefold()
        if any(
            ref == name and (table is None or bare.get(id(table)) is not f)
            for ref, table in sources
        ):
            text = (
                f"The query qualifies a column of the filtered table {f.key} with its schema,"
                " and another table in the query has the same name, so the row filter cannot"
                " rewrite it and it was not run."
            )
            raise RowFilterRejectedError(
                text,
                llm_hint=f"Give the table {f.key} an alias and qualify its columns with the alias.",
            )
    for column, f in hits:
        if column.args.get("catalog") is not None:
            # PostgreSQL refuses a reference to another database; without the catalog the
            # column would run, on the filtered rows.
            raise RowFilterRejectedError(
                f"The query qualifies a column of the filtered table {f.key} with a database"
                " name, which the row filter cannot rewrite, so it was not run.",
                llm_hint=(
                    f"Qualify the columns of the table {f.key} with the table name or an alias."
                ),
            )
    for column, _ in hits:
        column.set("db", None)


def _names_the_result(select: exp.Select) -> bool:
    """Whether *select* names the statement's result columns: the statement itself, through
    the first branch of a set operation or an unaliased parenthesised branch."""
    node: exp.Expression = select
    while node.parent is not None:
        parent = node.parent
        if not (
            (isinstance(parent, exp.SetOperation) and node.arg_key == "this")
            or (isinstance(parent, exp.Subquery) and not parent.args.get("alias"))
        ):
            return False
        node = parent
    return True


@dataclass
class _OutputNames:
    """Per item of one SELECT: the name the engine gives it as written (``None``: aliased,
    or not known for certain) and, on MySQL, a fallback (``_keep_output_names``)."""

    select: exp.Select
    names: list[str | None]
    fallbacks: list[str | None]


# MySQL limits: a result column name may be 256 characters, a derived table or CTE column
# name (and so any SELECT but the result's, conservatively) 64.
_MYSQL_RESULT_NAME = 256
_MYSQL_COLUMN_NAME = 64


def _engine_output_names(tree: exp.Expression, sql: str, read: str) -> list[_OutputNames]:
    """For every SELECT, the column name the engine gives each item of *sql* as written.
    Where no outer query reads the names (a later set-operation branch, an ``IN`` or
    ``EXISTS`` subquery) the alias is harmless and keeps a ``GROUP BY``, ``ORDER BY`` or
    ``HAVING`` on the engine's name working. PostgreSQL: ``_pg_output_name``. MySQL: the
    item's text as written (``_mysql_item_texts``), except for a column, a literal or
    ``NULL``, which the server names after the column or the value and sqlglot writes back
    unchanged; and, for an item whose text is not known, the fallback: its clean SQL as
    sqlglot writes it, cut to 64 characters, used only when the item turns out to hold a
    filtered table (otherwise the server would name it after the rewritten text, the admin's
    condition included)."""
    selects = list(tree.find_all(exp.Select))
    out: list[_OutputNames] = []
    if read == "postgres":
        for select in selects:
            names = [
                None if isinstance(item, exp.Alias) else _pg_output_name(item, sql)
                for item in select.expressions
            ]
            out.append(_OutputNames(select, names, [None] * len(names)))
        return out
    if read != "mysql" or not selects:
        return out
    try:
        tokens = sqlglot.tokenize(sql, read="mysql")
    except TokenError:
        return out
    named_by_value = (exp.Alias, exp.Column, exp.Literal, exp.Null, exp.Boolean, exp.Star)
    for select in selects:
        limit = _MYSQL_RESULT_NAME if _names_the_result(select) else _MYSQL_COLUMN_NAME
        names: list[str | None] = []
        fallbacks: list[str | None] = []
        for item, text in zip(
            select.expressions, _mysql_item_texts(sql, tokens, select, limit), strict=True
        ):
            if isinstance(item.unnest(), named_by_value):
                names.append(None)
                fallbacks.append(None)
                continue
            names.append(text)
            clean = item.sql(dialect=read, **_WRITE)[:_MYSQL_COLUMN_NAME].rstrip()
            fallbacks.append(None if text is not None or "`" in clean else clean)
        out.append(_OutputNames(select, names, fallbacks))
    return out


def _keep_output_names(entry: _OutputNames, inserted: set[int], read: str) -> None:
    """Alias each item whose name, written back, would differ from the engine's name for the
    query as written (sqlglot writes ``strpos(email, '@')`` as ``POSITION('@' IN email)``,
    which PostgreSQL names ``position``; MySQL names an item after its text, which sqlglot
    respaces). So the result columns, an outer query reading a CTE or derived table by that
    name, an ``ORDER BY``, ``GROUP BY`` or ``HAVING`` on it and the masking's name matching
    see what they would without the filter. Called after the replacement, so a MySQL item
    holding a filtered table is compared with its rewritten text; such an item whose text is
    not known gets its fallback, never the rewritten text (*inserted*: the ids of the
    derived tables the rewrite inserted)."""
    for item, name, fallback in zip(
        list(entry.select.expressions), entry.names, entry.fallbacks, strict=True
    ):
        if isinstance(item, exp.Alias):
            continue
        if (
            name is None
            and fallback is not None
            and any(id(node) in inserted for node in item.walk())
        ):
            name = fallback
        if name is None or _written_back_name(item, read) == name:
            continue
        item.replace(exp.alias_(item.copy(), exp.to_identifier(name, quoted=True)))


def _written_back_name(item: exp.Expression, read: str) -> str | None:
    text = item.sql(dialect=read, **_WRITE)
    if read != "postgres":
        return text
    source = f"SELECT {text}"
    try:
        reparsed = sqlglot.parse_one(source, read=read, error_level=ErrorLevel.IMMEDIATE)
    except (ParseError, TokenError):
        return None
    if not isinstance(reparsed, exp.Select) or len(reparsed.expressions) != 1:
        return None
    return _pg_output_name(reparsed.expressions[0], source)


# PostgreSQL names a bare literal or an operator expression "?column?".
_PG_NAMELESS = (
    exp.Literal,
    exp.Null,
    exp.Add,
    exp.Sub,
    exp.Mul,
    exp.Div,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.And,
    exp.Or,
    exp.Not,
    exp.Neg,
    exp.DPipe,
    exp.Is,
    exp.In,
    exp.Between,
    exp.Like,
    exp.ILike,
)
_PG_FUNCTION_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_$]*|"(?:[^"]|"")+"')


def _pg_output_name(item: exp.Expression, text: str) -> str | None:
    """The name PostgreSQL gives the unaliased SELECT item *item*, parsed from *text*, or
    ``None`` when not known for certain. A column keeps its name; a function call is named
    after the function as written (sqlglot records the position of the name token in the
    node's ``meta``; a call written with keyword syntax, such as ``position(a IN b)``, has
    none), also under a window, a ``FILTER`` or a schema; a cast keeps the name of what it
    casts; a literal or an operator expression is ``?column?``."""
    while isinstance(item, exp.Paren):
        item = item.this
    if isinstance(item, exp.Column):
        ident = item.this
        if not isinstance(ident, exp.Identifier):
            return None  # t.*
        return ident.name if ident.quoted else ident.name.lower()
    if isinstance(item, (exp.Window, exp.Filter)):
        return _pg_output_name(item.this, text)
    if isinstance(item, exp.Dot) and isinstance(item.expression, exp.Func):
        return _pg_output_name(item.expression, text)
    start, end = item.meta.get("start"), item.meta.get("end")
    if isinstance(item, (exp.Func, exp.Binary, exp.Unary)) and (
        isinstance(start, int) and isinstance(end, int)
    ):
        written = text[start : end + 1]
        if not _PG_FUNCTION_NAME.fullmatch(written):
            return None
        return written[1:-1].replace('""', '"') if written.startswith('"') else written.lower()
    if isinstance(item, exp.Cast):
        inner = _pg_output_name(item.this, text)
        return None if inner == "?column?" else inner  # a cast literal is named after the type
    return "?column?" if isinstance(item, _PG_NAMELESS) else None


# Words between SELECT and the first item that are not part of it.
_MYSQL_SELECT_MODIFIERS = frozenset(
    {
        "ALL",
        "DISTINCT",
        "DISTINCTROW",
        "HIGH_PRIORITY",
        "STRAIGHT_JOIN",
        "SQL_SMALL_RESULT",
        "SQL_BIG_RESULT",
        "SQL_BUFFER_RESULT",
        "SQL_NO_CACHE",
        "SQL_CACHE",
        "SQL_CALC_FOUND_ROWS",
    }
)
# Tokens that end a SELECT list at its own nesting level.
_LIST_END = frozenset(
    {
        TokenType.FROM,
        TokenType.INTO,
        TokenType.WHERE,
        TokenType.GROUP_BY,
        TokenType.HAVING,
        TokenType.WINDOW,
        TokenType.QUALIFY,
        TokenType.ORDER_BY,
        TokenType.LIMIT,
        TokenType.OFFSET,
        TokenType.FETCH,
        TokenType.UNION,
        TokenType.EXCEPT,
        TokenType.INTERSECT,
        TokenType.FOR,
        TokenType.LOCK,
        TokenType.SEMICOLON,
    }
)


def _select_token(tokens: list, select: exp.Select) -> int | None:
    """The index of the ``SELECT`` keyword of *select* in *tokens*: the first ``SELECT``
    outside parentheses before the earliest position sqlglot recorded in the SELECT itself
    (its items, FROM, WHERE and so on, not its CTEs, its optimizer hint or nested queries,
    whose own keywords sit inside parentheses or the hint there). ``None`` when there is no
    such position."""
    starts = [
        node.meta["start"]
        for node in select.walk(
            prune=lambda n: n is not select and isinstance(n, (exp.Query, exp.With, exp.Hint))
        )
        if node is not select
        and not isinstance(node, (exp.Query, exp.With, exp.Hint))
        and isinstance(node.meta.get("start"), int)
    ]
    if not starts:
        return None
    lowest = min(starts)
    at = next((i for i, token in enumerate(tokens) if token.start == lowest), None)
    if at is None:
        return None
    depth = 0
    for index in range(at - 1, -1, -1):
        kind = tokens[index].token_type
        if kind == TokenType.R_PAREN:
            depth += 1
        elif kind == TokenType.L_PAREN:
            # Unmatched: it opens a call or a parenthesis of the first item whose start
            # holds no position (IF((SELECT ...) > 0, 1, 0)); the SELECT comes before it.
            depth = max(depth - 1, 0)
        elif kind == TokenType.SELECT and depth == 0:
            return index
    return None


def _mysql_item_texts(sql: str, tokens: list, select: exp.Select, limit: int) -> list[str | None]:
    """Each item of *select* as written in *sql*: MySQL names an unaliased item after that
    text. Found from the tokens: the list starts after the SELECT's keyword
    (``_select_token``) and its modifiers, and is split at commas outside parentheses until a
    clause keyword or the closing parenthesis. ``None`` for every item when the split does
    not line up with the parsed items (their count, or the positions sqlglot recorded), and
    for an item holding a comment (kept in the server's name, dropped by the rewrite), a
    backslash or a control character, or longer than *limit* characters."""
    unknown: list[str | None] = [None] * len(select.expressions)
    start = _select_token(tokens, select)
    if start is None:
        return unknown
    start += 1
    while start < len(tokens) and (
        tokens[start].token_type == TokenType.HINT
        or tokens[start].text.upper() in _MYSQL_SELECT_MODIFIERS
    ):
        start += 1
    pieces: list[tuple[int, int]] = []
    depth, first, index = 0, start, start
    while index < len(tokens):
        kind = tokens[index].token_type
        if kind == TokenType.L_PAREN:
            depth += 1
        elif kind == TokenType.R_PAREN:
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and kind == TokenType.COMMA:
            pieces.append((first, index - 1))
            first = index + 1
        elif depth == 0 and kind in _LIST_END:
            break
        index += 1
    pieces.append((first, index - 1))
    if len(pieces) != len(select.expressions) or any(a > b for a, b in pieces):
        return unknown
    texts: list[str | None] = []
    for item, (a, b) in zip(select.expressions, pieces, strict=True):
        low, high = tokens[a].start, tokens[b].end
        for node in item.walk():
            at, to = node.meta.get("start"), node.meta.get("end")
            if isinstance(at, int) and isinstance(to, int) and not (low <= at and to <= high):
                return unknown
        text = sql[low : high + 1]
        plain = not any(mark in text for mark in ("/*", "--", "#", "\\")) and all(
            ch >= " " or ch in "\t\n\r" for ch in text
        )
        texts.append(text if plain and len(text) <= limit else None)
    return texts


# MySQL functions written without parentheses that sqlglot parses as a column and writes
# back as a quoted name (`utc_date`), which the server reads as a column. MySQL reserves the
# words, so an unquoted, unqualified one is always the function.
_MYSQL_BARE_FUNCTIONS = frozenset({"UTC_DATE", "UTC_TIME", "UTC_TIMESTAMP"})


def _spell_functions(tree: exp.Expression, read: str) -> exp.Expression:
    """*tree* with the function calls sqlglot would write back as something else spelt as
    the engine reads them; returns the root, replaced when it is such a call itself.

    MySQL: a bare ``UTC_DATE``, ``UTC_TIME`` or ``UTC_TIMESTAMP`` (parsed as a column) is
    kept as written, and ``UTC_DATE()`` (which sqlglot writes as ``CURRENT_DATE AT TIME ZONE
    'UTC'``, not MySQL) as ``UTC_DATE()``. PostgreSQL: a bare ``CURRENT_TIME`` (written
    ``CURRENT_TIME()``, a syntax error there) as ``CURRENT_TIME``. The other niladic forms
    (``CURRENT_DATE``, ``CURRENT_TIMESTAMP``, ``CURRENT_USER``, ``LOCALTIME``,
    ``LOCALTIMESTAMP``) are written back in a form each engine accepts."""
    for node in list(tree.walk()):
        new: exp.Expression | None = None
        if read == "mysql":
            ident = node.this if isinstance(node, exp.Column) else None
            if (
                isinstance(ident, exp.Identifier)
                and not ident.quoted
                and not node.table
                and ident.name.upper() in _MYSQL_BARE_FUNCTIONS
            ):
                new = exp.Var(this=ident.name)
            elif isinstance(node, exp.UtcDate):
                new = exp.Anonymous(this="UTC_DATE", expressions=[])
        elif read == "postgres" and isinstance(node, exp.CurrentTime) and node.this is None:
            new = exp.Var(this="CURRENT_TIME")
        if new is None:
            continue
        if node is tree:
            tree = new
        else:
            node.replace(new)
    return tree


def _restore_date_part(tree: exp.Expression, sql: str) -> None:
    """PostgreSQL: each ``date_part('unit', x)`` of *sql*, which sqlglot reads as
    ``EXTRACT(UNIT FROM x)``, written back as the call it was. PostgreSQL 14 and later name
    an ``EXTRACT`` column ``extract`` and type it ``numeric``; ``date_part`` is named
    ``date_part`` and typed ``double precision``. The ``Extract`` node keeps no position, so
    the call is recognised from the tokens before the position of its second argument:
    ``date_part`` (or ``"date_part"``), ``(``, a string, ``,`` and any opening parentheses.
    Left as ``EXTRACT`` when that argument holds no position (``date_part('day',
    current_date)``) or the tokens do not read so."""
    extracts = [n for n in tree.find_all(exp.Extract) if not n.meta]
    if not extracts:
        return
    try:
        tokens = sqlglot.tokenize(sql, read="postgres")
    except TokenError:
        return
    by_start = {token.start: index for index, token in enumerate(tokens)}
    for node in extracts:
        argument = node.expression
        starts = [n.meta["start"] for n in argument.walk() if isinstance(n.meta.get("start"), int)]
        at = by_start.get(min(starts)) if starts else None
        if at is None:
            continue
        index = at - 1
        while index >= 0 and tokens[index].token_type == TokenType.L_PAREN:
            index -= 1
        if index < 4:
            continue
        comma, unit, paren, name = (
            tokens[index],
            tokens[index - 1],
            tokens[index - 2],
            tokens[index - 3],
        )
        if (
            comma.token_type == TokenType.COMMA
            and unit.token_type == TokenType.STRING
            and paren.token_type == TokenType.L_PAREN
            and name.token_type in (TokenType.VAR, TokenType.IDENTIFIER)
            and name.text.lower() == "date_part"
            and (name.token_type == TokenType.VAR or name.text == "date_part")
            and tokens[index - 4].token_type != TokenType.DOT
        ):
            call = exp.Anonymous(
                this=name.text if name.token_type == TokenType.VAR else "date_part",
                expressions=[exp.Literal.string(unit.text), argument],
            )
            node.replace(call)


# Generator options for everything the rewriter writes back or compares with it.
_WRITE = {"comments": False, "normalize_functions": False}


def _reject_unwritable(tree: exp.Expression, text: str, read: str) -> str | None:
    """Why *tree*, parsed from *text*, cannot be written back as the engine would read it,
    or ``None``. On PostgreSQL a quoted function name that is not all lowercase
    (``"Lower"(x)``) names that exact function, while sqlglot reads a known name as its own
    function whatever the quoting and writes ``LOWER(x)``. A ``{...}`` literal, which sqlglot
    reads as a struct, is written ``STRUCT(...)``, a call of a function of that name."""
    for node in tree.walk():
        if isinstance(node, exp.Struct) and not node.meta:
            return "braces"
        if read != "postgres" or isinstance(node, exp.Anonymous):
            continue
        start, end = node.meta.get("start"), node.meta.get("end")
        if not (isinstance(node, (exp.Func, exp.Binary, exp.Unary)) and isinstance(start, int)):
            continue
        written = text[start : end + 1] if isinstance(end, int) else ""
        if written.startswith('"') and written[1:-1] != written[1:-1].lower():
            return written
    return None


def _generate(tree: exp.Expression, dialect: str) -> str:
    # sqlglot's default is WARN: it logs and emits best-effort SQL for a construct the
    # generator cannot express. RAISE turns that into a rejection. Comments are dropped: the
    # database never needs them, and the output must not depend on how a sqlglot version
    # sanitises comment text (a "*/" or a MySQL "/*!" inside one).
    # Optimizer hints (MySQL ``/*+ ... */``) are parsed as ``Hint`` nodes, not comments, and
    # their arguments are written back unsanitised: a ``*/`` inside one ends the hint early on
    # a server that reads ``/*+`` as a plain comment (MariaDB). They are only advice: dropped.
    for hint in list(tree.find_all(exp.Hint)):
        hint.pop()
    # normalize_functions=False writes a function sqlglot does not know as it was parsed:
    # upper-cased, PostgreSQL's quoted "MyFunc"(x) would call another function.
    return tree.sql(dialect=dialect, unsupported_level=ErrorLevel.RAISE, **_WRITE)


def apply_row_filters(
    sql: str, row_filters: dict[str, str], dialect: str, *, tree: exp.Expression | None = None
) -> str:
    """*sql* with every filtered base-table reference replaced by a filtered derived table.

    *tree*, when given, is *sql* already parsed with this dialect (the strict check's tree):
    it is rewritten in place and *sql* is not parsed again. The tree is trusted, not checked
    against *sql*: a tree parsed from another statement would be rewritten and returned
    instead of *sql*, so only ``row_filter._executable`` passes it, with the strict check's
    tree of the same *sql*.

    Raises ``RowFilterRejectedError`` when the statement cannot be rewritten safely: an
    unknown dialect, a malformed key, a parse failure, more than one statement, a shape that
    reads rows without a table reference (``_reject_unfilterable``), a CTE named like a
    filtered table, an ambiguous or mis-cased table reference, a nested join inside a filtered table
    reference, a ``schema.table.column`` whose bare table name another source of the
    statement shares (``_drop_schema_qualifiers``), or a construct the generator cannot write
    back. An empty map returns *sql* itself without parsing.

    Besides the replacement, the statement changes in these ways: the condition's columns
    are qualified with the table name when the reference sits inside a subquery
    (``_qualified``); ``schema.table.column`` naming a filtered reference becomes
    ``table.column``; an unaliased item of a SELECT whose names an outer query or the result
    sees, which sqlglot writes back under another name, gets the name the engine gives the
    query as written (``_keep_output_names``); niladic functions sqlglot would misspell are
    kept as functions (``_spell_functions``, in the conditions too); and on PostgreSQL a
    ``date_part`` call stays one (``_restore_date_part``).
    """
    if not row_filters:
        return sql
    read = sqlglot_dialect(dialect)
    if read is None:
        raise RowFilterRejectedError(
            "Row filters are not supported for this database dialect, so the query was not run."
        )
    try:
        return _apply(sql, row_filters, read, tree)
    except RecursionError as exc:
        text = "The query is nested too deeply for the row filter, so it was not run."
        raise RowFilterRejectedError(
            text, llm_hint="Simplify the query so that it is less deeply nested."
        ) from exc


def _apply(
    sql: str, row_filters: dict[str, str], read: str, tree: exp.Expression | None = None
) -> str:
    if tree is None:
        tree = _parse_statement(sql, read)
    sg = Dialect.get_or_raise(read)
    filters = _filters(row_filters, read)
    _reject_unfilterable(tree, read)
    unwritable = _reject_unwritable(tree, sql, read)
    if unwritable == "braces":
        raise RowFilterRejectedError(
            "The query uses a construct the row-filter rewriter cannot write back for this"
            " dialect, so it was not run.",
            llm_hint="Write the query without braces.",
        )
    if unwritable is not None:
        raise RowFilterRejectedError(
            "The query calls a quoted function name that the row-filter rewriter would write"
            " back as a different function, so it was not run.",
            llm_hint=f"Write the function name {unwritable} in lowercase without quotes.",
        )
    _reject_cte_collisions(_cte_names(tree, sg), filters, read)
    # Before the tables are collected: neither pass copies a subtree, but a node they
    # replaced must not be the one replaced by a derived table below.
    tree = _spell_functions(tree, read)
    if read == "postgres":
        _restore_date_part(tree, sql)
    # Collected before any replacement: the subtrees the rewrite inserts are never visited,
    # so a condition that references its own table runs as the admin wrote it.
    targets = list(tree.find_all(exp.Table))
    sources = _sources(tree, targets, sg)
    schema_columns = [
        c
        for c in tree.find_all(exp.Column)
        if isinstance(c.args.get("db"), exp.Identifier)
        and isinstance(c.args.get("table"), exp.Identifier)
    ]
    output_names = _engine_output_names(tree, sql, read)
    bare: dict[int, _Filter] = {}  # id of an unaliased filtered reference -> its filter
    inserted: set[int] = set()  # ids of the derived tables inserted
    for table in targets:
        if not isinstance(table.this, exp.Identifier):
            continue  # a table-valued function such as generate_series(...)
        matched = _match(table, filters, sg, read)
        if matched is None:
            continue  # a table the admin did not list, or a reference to a CTE
        if table.args.get("joins") or table.args.get("laterals"):
            raise RowFilterRejectedError(
                "The query nests a join inside a filtered table reference, which the row"
                " filter cannot rewrite, so it was not run.",
                llm_hint=(
                    f"Join the filtered table {matched.key} directly in FROM or JOIN,"
                    " not inside a parenthesised join."
                ),
            )
        if not table.args.get("alias"):
            bare[id(table)] = matched
        derived = _filtered(table, matched.condition, read, qualify=_in_subquery(table, tree))
        inserted.add(id(derived))
        table.replace(derived)
    _drop_schema_qualifiers(schema_columns, bare, sources, sg, read)
    for entry in output_names:
        _keep_output_names(entry, inserted, read)
    try:
        return _generate(tree, read)
    except UnsupportedError as exc:
        raise RowFilterRejectedError(
            "The query uses a construct the row-filter rewriter cannot write back for this"
            " dialect, so it was not run.",
            llm_hint="Write the query with standard SELECT constructs for this database.",
        ) from exc


def _parse_statement(sql: str, read: str) -> exp.Expression:
    """*sql* as one statement, or the rewriter's rejection."""
    try:
        parsed = sqlglot.parse(
            sql.rstrip().rstrip(";"), read=read, error_level=ErrorLevel.IMMEDIATE
        )
    except (ParseError, TokenError) as exc:
        raise RowFilterRejectedError(
            "The query could not be parsed by the row-filter rewriter, so it was not run.",
            llm_hint=_parse_failure_hint(exc),
        ) from exc
    statements = [s for s in parsed if s is not None]
    if len(statements) != 1:
        raise RowFilterRejectedError(
            "The row filter needs exactly one statement, so the query was not run.",
            llm_hint="Write exactly one SELECT statement.",
        )
    return statements[0]


def _parse_failure_hint(exc: ParseError | TokenError) -> str:
    """Where sqlglot stopped, from its structured error, never its message: that carries
    internals (``<Token token_type: TokenType.L_BRACE, ...>``, ``<class '...Paren'>``)."""
    errors = exc.errors if isinstance(exc, ParseError) else []
    first = errors[0] if errors else {}
    line, col = first.get("line"), first.get("col")
    if isinstance(line, int) and isinstance(col, int):
        where = f"the parser stopped at line {line}, column {col}"
        near = first.get("highlight")
        if isinstance(near, str) and 0 < len(near) <= 40 and near.isprintable() and '"' not in near:
            where += f', near "{near}"'
    elif isinstance(exc, TokenError):
        where = "a string or quoted name is not closed, or a character is not valid here"
    else:
        where = "the parser could not read it"
    return (
        f"The row-filter rewriter could not parse the query: {where}."
        " Write it as one plain SELECT statement."
    )
