# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Per-table row filtering: every base-table reference the admin configured a filter for
becomes a filtered derived table, ``(SELECT * FROM t WHERE (<condition>)) AS t``.

The statement is parsed with sqlglot, the matching ``Table`` nodes are replaced and the
tree is regenerated; no text is spliced. Anything the rewriter cannot read or write with
certainty raises ``RowFilterRejectedError`` and the statement is never sent.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import ErrorLevel, ParseError, TokenError, UnsupportedError

from .row_filter_errors import RowFilterRejectedError

# Adapter ``query_dialect`` (lower-cased) -> sqlglot dialect name.
_SQLGLOT_DIALECT = {"postgresql": "postgres", "mysql": "mysql"}


# Functions that run SQL passed as a string, or read a whole table named by a string: their
# rows never pass through a Table node, so the rewrite cannot filter them.
_UNFILTERABLE_FUNCTION = re.compile(
    r"^(?:(?:query|table|cursor|schema|database)_to_xml\w*"
    r"|ts_stat|ts_rewrite|crosstab\d*|connectby|dblink\w*)$",
    re.IGNORECASE,
)
# Catalog views that expose column values (most common values, histograms) of every row,
# per sqlglot dialect: the schemas they live in (``None``: an unqualified reference, which
# resolves to the catalog first) and their names.
_VALUE_STATISTICS_VIEWS = {
    "postgres": (
        {None, "pg_catalog"},
        {"pg_stats", "pg_stats_ext", "pg_stats_ext_exprs", "pg_statistic", "pg_statistic_ext_data"},
    ),
    "mysql": ({"information_schema"}, {"column_statistics"}),
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
            "The query contains a construct the row filter cannot inspect, so it was not run."
        )
    for func in tree.find_all(exp.Func):
        name = func.name if isinstance(func, exp.Anonymous) else func.sql_name()
        if _UNFILTERABLE_FUNCTION.match(name):
            text = (
                f"The function {name.lower()} reads rows the row filter cannot see,"
                " so the query was not run."
            )
            raise RowFilterRejectedError(text, llm_hint=f"{text} Write the query without it.")
    schemas, views = _VALUE_STATISTICS_VIEWS[read]
    for table in tree.find_all(exp.Table):
        schema = table.args.get("db")
        if (
            isinstance(table.this, exp.Identifier)
            and table.name.casefold() in views
            and (schema.name.casefold() if isinstance(schema, exp.Identifier) else None) in schemas
        ):
            text = (
                f"The catalog view {table.name.casefold()} exposes values the row filter"
                " cannot see, so the query was not run."
            )
            raise RowFilterRejectedError(text, llm_hint=f"{text} Write the query without it.")


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
            raise RowFilterRejectedError(text, llm_hint=f"{text} Rename the CTE.")


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
        raise RowFilterRejectedError(text, llm_hint=f"{text} Qualify it as schema.table.")
    loose = [
        f
        for f in filters
        if f.table.casefold() == name.casefold()
        and (schema is None or f.schema.casefold() == schema.casefold())
    ]
    if loose:
        text = f"Table {reference} does not match the filtered table {loose[0].key} exactly."
        raise RowFilterRejectedError(text, llm_hint=f"{text} Write the name as {loose[0].key}.")
    return None


def _filtered(table: exp.Table, condition: exp.Expression) -> exp.Subquery:
    """``(SELECT * FROM <table without alias> WHERE (<condition>)) AS <alias or table name>``."""
    alias = table.args.get("alias")
    alias = alias.copy() if alias else exp.TableAlias(this=table.this.copy())
    inner = table.copy()
    inner.set("alias", None)
    select = exp.select("*").from_(inner).where(exp.Paren(this=condition.copy()))
    return exp.Subquery(this=select, alias=alias)


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
    return tree.sql(dialect=dialect, unsupported_level=ErrorLevel.RAISE, comments=False)


def apply_row_filters(sql: str, row_filters: dict[str, str], dialect: str) -> str:
    """*sql* with every filtered base-table reference replaced by a filtered derived table.

    Raises ``RowFilterRejectedError`` when the statement cannot be rewritten safely: an
    unknown dialect, a malformed key, a parse failure, more than one statement, a shape that
    reads rows without a table reference (``_reject_unfilterable``), a CTE named like a
    filtered table, an ambiguous or mis-cased table reference, a nested join inside a filtered table
    reference, or a construct the generator cannot write back. An empty map returns *sql*
    itself without parsing.
    """
    if not row_filters:
        return sql
    read = sqlglot_dialect(dialect)
    if read is None:
        raise RowFilterRejectedError(
            "Row filters are not supported for this database dialect, so the query was not run."
        )
    try:
        return _apply(sql, row_filters, read)
    except RecursionError as exc:
        text = "The query is nested too deeply for the row filter, so it was not run."
        raise RowFilterRejectedError(text, llm_hint=f"{text} Simplify the query.") from exc


def _apply(sql: str, row_filters: dict[str, str], read: str) -> str:
    try:
        parsed = sqlglot.parse(
            sql.rstrip().rstrip(";"), read=read, error_level=ErrorLevel.IMMEDIATE
        )
    except (ParseError, TokenError) as exc:
        first = str(exc).splitlines()[0] if str(exc) else "parse error"
        raise RowFilterRejectedError(
            "The query could not be parsed by the row-filter rewriter, so it was not run.",
            llm_hint=f"The row-filter rewriter could not parse the query: {first}",
        ) from exc
    statements = [s for s in parsed if s is not None]
    if len(statements) != 1:
        raise RowFilterRejectedError(
            "The row filter needs exactly one statement, so the query was not run."
        )
    tree = statements[0]
    sg = Dialect.get_or_raise(read)
    filters = _filters(row_filters, read)
    _reject_unfilterable(tree, read)
    _reject_cte_collisions(_cte_names(tree, sg), filters, read)
    # Collected before any replacement: the subtrees the rewrite inserts are never visited,
    # so a condition that references its own table runs as the admin wrote it.
    targets = list(tree.find_all(exp.Table))
    for table in targets:
        if not isinstance(table.this, exp.Identifier):
            continue  # a table-valued function such as generate_series(...)
        matched = _match(table, filters, sg, read)
        if matched is None:
            continue  # a table the admin did not list, or a reference to a CTE
        if table.args.get("joins") or table.args.get("laterals"):
            raise RowFilterRejectedError(
                "The query nests a join inside a filtered table reference, which the row"
                " filter cannot rewrite, so it was not run."
            )
        table.replace(_filtered(table, matched.condition))
    try:
        return _generate(tree, read)
    except UnsupportedError as exc:
        raise RowFilterRejectedError(
            "The query uses a construct the row-filter rewriter cannot write back for this"
            " dialect, so it was not run."
        ) from exc
