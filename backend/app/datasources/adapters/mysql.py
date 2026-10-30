# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""MySQL / MariaDB data source adapter."""

import asyncio
from collections.abc import AsyncGenerator, Iterable
from dataclasses import dataclass, field
import logging
import re
import time
from typing import Any

import aiomysql

from ..base import BaseDataSource, _serialize
from ..errors import ErrorCategory, ExplainError, classify_chain
from ..models import (
    ColumnInfo,
    ConnectionResult,
    DataSourceSchema,
    PrivacySettings,
    PrivilegeFacts,
    PrivilegeGrant,
    QueryResult,
    RelationshipInfo,
    SchemaInfo,
    TableInfo,
    TablePrivilegeFacts,
    ValidationResult,
    grant_schema_name,
)
from ..registry import register_datasource
from ..sampling import withhold_enum_labels
from ..validators.mysql_validator import MySQLValidator

logger = logging.getLogger(__name__)

# Connection pool sizing — keep small to avoid monopolising shared MySQL servers.
# Raise _POOL_MAX_SIZE if the app has more concurrent users and the server allows it.
_POOL_MIN_SIZE: int = 1
_POOL_MAX_SIZE: int = 5

# TCP-level timeout (seconds) for both pool creation and test_connection().
_CONNECT_TIMEOUT_S: int = 10


class _NoWarnings:
    """Skip aiomysql's SHOW WARNINGS after a statement that raised a warning.

    aiomysql passes each warning to warnings.warn, which writes it to stderr and so to the
    server log. MySQL warnings quote stored values (1292 ``Truncated incorrect DOUBLE value:
    '<value>'`` for ``WHERE email = 1``, one per row), sensitive columns included. Turning
    them off on the server (``max_error_count = 0``) needs SESSION_VARIABLES_ADMIN, which a
    read-only role lacks. Nothing in this adapter reads warnings. Every cursor the adapter
    opens is one of the classes below.
    """

    async def _show_warnings(self, conn: Any) -> None:
        return None


class _Cursor(_NoWarnings, aiomysql.Cursor):
    pass


class _DictCursor(_NoWarnings, aiomysql.DictCursor):
    pass


class _SSCursor(_NoWarnings, aiomysql.SSCursor):
    pass


_SYSTEM_SCHEMAS = frozenset({"information_schema", "mysql", "performance_schema", "sys"})

# Generated from _SYSTEM_SCHEMAS so that adding a schema only requires editing the frozenset.
_SYSTEM_SCHEMAS_TUPLE = "(" + ", ".join(f"'{s}'" for s in sorted(_SYSTEM_SCHEMAS)) + ")"


# One line of SHOW GRANTS: the privilege list, the scope (*.*, `db`.*, `db`.`t`) and the
# grantee. A role grant (GRANT `r`@`%` TO ...) has no ON and does not match.
# A scope part is a backtick-quoted name (a backtick inside doubled; a space or a dot inside
# stays inside) or a bare name. The qualified form is tried before the bare \S+, so a quoted
# name with a space is not cut at the space and read as a global grant.
_GRANT_PART = r"`(?:[^`]|``)*`|[^\s`.]+"
_GRANT_LINE = re.compile(
    r"^GRANT\s+(?P<privileges>.+?)\s+ON\s+(?:(?P<kind>TABLE|FUNCTION|PROCEDURE)\s+)?"
    rf"(?P<scope>(?:{_GRANT_PART})\.(?:\*|{_GRANT_PART})|\S+)\s+TO\s",
    re.IGNORECASE,
)
_GRANT_SCOPE = re.compile(rf"^(?P<schema>{_GRANT_PART})\.(?P<table>\*|{_GRANT_PART})$")
# The privileges the audit reports as writes: every privilege that changes data, structure
# or files. SELECT, SHOW VIEW, EXECUTE, USAGE, PROXY, SHOW DATABASES, PROCESS, RELOAD,
# SHUTDOWN, REPLICATION *, CREATE USER and GRANT OPTION are not writes. At the global level
# (over every grantee), ALL PRIVILEGES, or SUPER with every privilege of this set, is
# is_superuser (MySQL 8 prints GRANT ALL ON *.* as the list of static privileges, never as
# ALL PRIVILEGES); SUPER without that is has_super.
_WRITE_PRIVILEGES = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "FILE",
        "ALTER",
        "CREATE",
        "INDEX",
        "TRIGGER",
        "CREATE ROUTINE",
        "ALTER ROUTINE",
        "EVENT",
        "LOCK TABLES",
        "CREATE VIEW",
        "CREATE TEMPORARY TABLES",
        "REFERENCES",
        "CREATE TABLESPACE",
    }
)
_ALL = "ALL PRIVILEGES"
# What ALL PRIVILEGES below the global level reports as writes: the write privileges that
# level can hold (FILE and CREATE TABLESPACE are global only). A REVOKE of ALL PRIVILEGES
# there would take SELECT with it; naming the writes leaves the read access in place.
_SCHEMA_ALL_WRITES = sorted(_WRITE_PRIVILEGES - {"FILE", "CREATE TABLESPACE"})
_TABLE_ALL_WRITES = sorted(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "CREATE",
        "INDEX",
        "TRIGGER",
        "CREATE VIEW",
        "REFERENCES",
    }
)
# MariaDB only: DELETE HISTORY removes the history rows of a system-versioned table, at the
# global, database or table level, and is part of ALL there. MySQL has no such privilege
# (a REVOKE naming it is a syntax error), and the superuser rule leaves it out.
_MARIADB_WRITES = frozenset({"DELETE HISTORY"})
# The most write grants the audit keeps, in the order the fold builds them.
_WRITES_CAP = 500
_COLUMN_PRIVILEGE = re.compile(r"^(?P<name>[A-Z ]+?)\s*\(.*\)$")
_ROLE_PART = re.compile(r"`((?:[^`]|``)*)`@`((?:[^`]|``)*)`")
_SCHEMAS_SQL = (
    "SELECT schema_name FROM information_schema.schemata WHERE schema_name NOT IN"
    " ('mysql', 'sys', 'performance_schema', 'information_schema') ORDER BY 1"
)
# MySQL client/server error numbers a connection or catalog failure of the privilege check
# reads as; the redacted driver text keeps only the number, so the sentence stands in.
_CONNECTION_ERROR_SENTENCES: dict[int, str] = {
    1045: "the server refused the user name or password",
    1044: "the account has no access to the database",
    1049: "the database does not exist",
    1130: "the server does not allow connections from this host",
    1040: "the server has too many connections",
    2003: "the server could not be reached",
    2005: "the host name could not be resolved",
    2013: "the connection was lost",
    1142: "the account may not read a catalog table",
    1143: "the account may not read a catalog table",
    1227: "the account lacks a privilege the check needs",
    3159: "the server requires an encrypted connection",
}
_ERRNO_PREFIX = re.compile(r"^\((\d+),")


def _unquote_grant_part(part: str) -> str:
    if len(part) >= 2 and part[0] == "`" and part[-1] == "`":
        return part[1:-1].replace("``", "`")
    return part


def _split_outside_parentheses(text: str) -> list[str]:
    """*text* split at the commas outside parentheses and backtick-quoted names; a column
    list stays in its item, and a comma or parenthesis inside `` `a,b` `` stays in the name
    (a doubled backtick closes and reopens the quote, so it stays inside too)."""
    items: list[str] = []
    depth = 0
    start = 0
    quoted = False
    for i, char in enumerate(text):
        if char == "`":
            quoted = not quoted
        elif quoted:
            continue
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        elif char == "," and depth == 0:
            items.append(text[start:i])
            start = i + 1
    items.append(text[start:])
    return items


def _parse_grant_line(
    line: str,
) -> tuple[frozenset[str], dict[str, tuple[str, ...]], str | None, str | None, str] | None:
    """``(privileges, column_privileges, schema, table, kind)`` of one ``SHOW GRANTS`` line;
    ``None`` for a line that grants no privilege (a role grant). ``kind`` is the object kind
    the grant is on: ``TABLE`` (also without the keyword, and for a global or schema grant),
    ``FUNCTION`` or ``PROCEDURE``. ``ALL`` reads as ``ALL PRIVILEGES``;
    ``USAGE`` (no privilege) is dropped. A column-level item (``UPDATE (a, b)``) is not a
    privilege on the table: it lands in ``column_privileges`` under its privilege name with
    the column names unquoted and de-duplicated in order. ``schema None``: global (``*.*``,
    and the ``''@''`` scope of ``GRANT PROXY``); ``table None``: the whole schema."""
    match = _GRANT_LINE.match(line.strip())
    if match is None:
        return None
    privileges: set[str] = set()
    column_privileges: dict[str, tuple[str, ...]] = {}
    # A column list holds commas of its own (SELECT (a, b)): the split keeps it inside its
    # item, so a column name is never read as a privilege.
    for raw in _split_outside_parentheses(match.group("privileges")):
        name = " ".join(raw.split()).upper()
        if not name or name == "USAGE":
            continue
        column = _COLUMN_PRIVILEGE.match(name)
        if column is not None:
            # The column names were upper-cased with the rest: re-read them from the raw text.
            inner = raw[raw.index("(") + 1 : raw.rindex(")")]
            column_privileges[column.group("name").strip()] = tuple(
                dict.fromkeys(
                    _unquote_grant_part(c.strip()) for c in _split_outside_parentheses(inner)
                )
            )
            continue
        privileges.add(_ALL if name == "ALL" else name)
    kind = (match.group("kind") or "TABLE").upper()
    scope_text = match.group("scope")
    # *.* is the global scope (_GRANT_SCOPE would read it as schema "*"). A scope that is
    # neither that nor schema.table (the ''@'' of GRANT PROXY) reads as global too:
    # over-reporting is the safe direction for a privilege audit.
    scope = None if scope_text == "*.*" else _GRANT_SCOPE.match(scope_text)
    if scope is None:
        return frozenset(privileges), column_privileges, None, None, kind
    schema = _unquote_grant_part(scope.group("schema"))
    table = scope.group("table")
    # The names are returned as written. A schema-level grant is a pattern: SHOW GRANTS writes
    # a literal "_" or "%" in the schema name as \_ or \% (and a backslash as \\), and a
    # REVOKE must name it so (grant_schema_name gives the schema it matches). A table-level
    # grant is not a pattern: MySQL prints its database name literally.
    if table == "*":
        return frozenset(privileges), column_privileges, schema, None, kind
    return frozenset(privileges), column_privileges, schema, _unquote_grant_part(table), kind


def _has(privileges: set[str], wanted: frozenset[str]) -> bool:
    return _ALL in privileges or bool(privileges & wanted)


# A grant's scope as the parser reads it: (None, None) global, (schema literal, None) the
# schema, (schema, table) the table.
_Scope = tuple[str | None, str | None]


@dataclass
class _GrantSet:
    """One grantee's privileges, parsed once from its ``SHOW GRANTS`` lines: the privileges
    per scope and the columns per (table scope, privilege) of a column-level grant."""

    privileges: dict[_Scope, set[str]] = field(default_factory=dict)
    columns: dict[tuple[_Scope, str], list[str]] = field(default_factory=dict)

    def add(
        self, scope: _Scope, privileges: Iterable[str], columns: dict[str, Iterable[str]]
    ) -> None:
        self.privileges.setdefault(scope, set()).update(privileges)
        for privilege, names in columns.items():
            held = self.columns.setdefault((scope, privilege), [])
            held.extend(n for n in names if n not in held)

    def update(self, other: "_GrantSet") -> None:
        for scope, privileges in other.privileges.items():
            self.add(scope, privileges, {})
        for (scope, privilege), names in other.columns.items():
            self.add(scope, (), {privilege: names})

    def minus(self, direct: "_GrantSet") -> "_GrantSet":
        """What this set holds beyond *direct*: per scope the privileges *direct* lacks, per
        (scope, privilege) the columns it lacks; ``ALL PRIVILEGES`` in *direct* at a scope
        covers everything there. MySQL prints the account's and a role's privileges on one
        line per scope, so a role's share is only per privilege. Residual: a privilege held
        both directly and through the role is attributed to the account only."""
        share = _GrantSet()
        for scope, privileges in self.privileges.items():
            held = direct.privileges.get(scope, set())
            if _ALL not in held and privileges - held:
                share.privileges[scope] = privileges - held
        for (scope, privilege), names in self.columns.items():
            if _ALL in direct.privileges.get(scope, set()):
                continue
            held_names = direct.columns.get((scope, privilege), [])
            rest = [n for n in names if n not in held_names]
            if rest:
                share.columns[(scope, privilege)] = rest
        return share


def _grant_set(lines: list[str]) -> _GrantSet:
    """The ``SHOW GRANTS`` lines parsed into one ``_GrantSet``. Role grant lines add nothing,
    and neither does a grant on a FUNCTION or PROCEDURE: it is no privilege on a table (MySQL
    gives a routine's creator EXECUTE and ALTER ROUTINE on it), and a REVOKE ON TABLE naming
    it fails. CREATE ROUTINE / ALTER ROUTINE on a schema or globally stay writes."""
    grants = _GrantSet()
    for line in lines:
        parsed = _parse_grant_line(line)
        if parsed is not None and parsed[4] == "TABLE":
            privileges, columns, schema, table, _ = parsed
            grants.add((schema, table), privileges, columns)
    return grants


def _writes_of(privileges: set[str], scope: _Scope, mariadb: bool) -> list[str]:
    """The write privileges of *privileges* at *scope*, sorted. ``ALL PRIVILEGES`` stays whole
    at the global level (the superuser case) and is the writes valid at the level below it."""
    extra = _MARIADB_WRITES if mariadb else frozenset()
    if _ALL in privileges:
        if scope[0] is None:
            return [_ALL]
        return sorted({*(_SCHEMA_ALL_WRITES if scope[1] is None else _TABLE_ALL_WRITES), *extra})
    return sorted(privileges & (_WRITE_PRIVILEGES | extra))


def _facts_from_grants(
    role: str,
    grants: list[tuple[str, _GrantSet]],
    tables: list[tuple[str | None, str]],
    existing: set[tuple[str, str]],
    schemas: list[str],
    mariadb: bool = False,
) -> PrivilegeFacts:
    """Fold the ``(grantee, grant set)`` pairs per grantee and level and answer for every
    requested table. A table's privileges are the union, over every grantee, of the global,
    its schema's, its own and its column grants. Every write grant becomes a
    ``PrivilegeGrant`` naming the grantee a ``REVOKE`` must name. *mariadb* adds
    ``DELETE HISTORY`` to the writes."""
    write_set = _WRITE_PRIVILEGES | _MARIADB_WRITES if mariadb else _WRITE_PRIVILEGES
    by_grantee: dict[str, _GrantSet] = {}
    for grantee, grant_set in grants:
        by_grantee.setdefault(grantee, _GrantSet()).update(grant_set)
    global_p: dict[str, set[str]] = {}
    schema_p: dict[tuple[str, str], set[str]] = {}  # (grantee, schema literal as written)
    table_p: dict[tuple[str, str, str], set[str]] = {}
    column_p: dict[tuple[str, str, str, str], list[str]] = {}  # (grantee, schema, table, priv)
    for grantee, grant_set in by_grantee.items():
        for (schema, table), privileges in grant_set.privileges.items():
            if schema is None:
                global_p.setdefault(grantee, set()).update(privileges)
            elif table is None:
                # Keyed by the name as written: that is what a REVOKE has to name.
                schema_p[(grantee, schema)] = privileges
            else:
                table_p[(grantee, schema, table)] = privileges
        for ((schema, table), privilege), names in grant_set.columns.items():
            # MySQL grants columns at the table level only.
            if schema is not None and table is not None:
                column_p[(grantee, schema, table, privilege)] = names

    all_global: set[str] = set().union(*global_p.values()) if global_p else set()
    is_superuser = _ALL in all_global or ("SUPER" in all_global and all_global >= _WRITE_PRIVILEGES)
    has_super = "SUPER" in all_global and not is_superuser
    facts = PrivilegeFacts(
        role=role,
        is_superuser=is_superuser,
        has_super=has_super,
        # Each grantee (after the role diff) whose global privileges hold SUPER: each needs
        # its own REVOKE.
        super_grantees=(
            [g for g, p in sorted(global_p.items()) if "SUPER" in p] if has_super else []
        ),
        schemas=sorted(schemas),
    )
    for grantee, p in sorted(global_p.items()):
        if writes := _writes_of(p, (None, None), mariadb):
            facts.writes.append(PrivilegeGrant(None, None, writes, grantee))
    for (grantee, schema), p in sorted(schema_p.items()):
        if writes := _writes_of(p, (schema, None), mariadb):
            facts.writes.append(PrivilegeGrant(schema, None, writes, grantee))
    for (grantee, schema, table), p in sorted(table_p.items()):
        if writes := _writes_of(p, (schema, table), mariadb):
            facts.writes.append(PrivilegeGrant(schema, table, writes, grantee))
    for (grantee, schema, table, privilege), names in sorted(column_p.items()):
        if privilege in write_set:
            facts.writes.append(PrivilegeGrant(schema, table, [privilege], grantee, list(names)))
    facts.writes_truncated = len(facts.writes) > _WRITES_CAP
    del facts.writes[_WRITES_CAP:]

    # The schema grants by the schema they match, per grantee. Two literals can match one
    # schema (`a_b` is a wildcard, `a\_b` is not): their union, and each literal kept, since
    # a REVOKE must name the grant as printed.
    matched: dict[tuple[str, str], set[str]] = {}
    literals: dict[tuple[str, str], list[str]] = {}
    for (grantee, literal), p in sorted(schema_p.items()):
        matched.setdefault((grantee, grant_schema_name(literal)), set()).update(p)
        literals.setdefault((grantee, grant_schema_name(literal)), []).append(literal)
    grantees = sorted(by_grantee)
    select = frozenset({"SELECT"})

    def fact(schema: str, table: str) -> TablePrivilegeFacts:
        # One grant per level that gives SELECT, at the scope a REVOKE must name.
        select_grants: list[PrivilegeGrant] = []
        can_write = False
        for grantee in grantees:
            if _has(global_p.get(grantee, set()), select):
                select_grants.append(PrivilegeGrant(None, None, ["SELECT"], grantee))
            for literal in literals.get((grantee, schema), []):
                if _has(schema_p[(grantee, literal)], select):
                    select_grants.append(PrivilegeGrant(literal, None, ["SELECT"], grantee))
            if _has(table_p.get((grantee, schema, table), set()), select):
                select_grants.append(PrivilegeGrant(schema, table, ["SELECT"], grantee))
            p = (
                global_p.get(grantee, set())
                | matched.get((grantee, schema), set())
                | table_p.get((grantee, schema, table), set())
            )
            if _has(p, write_set) or any(
                k[:3] == (grantee, schema, table) and k[3] in write_set for k in column_p
            ):
                can_write = True
        return TablePrivilegeFacts(
            schema_name=schema,
            table_name=table,
            exists=True,
            can_select=bool(select_grants),
            can_write=can_write,
            select_grants=select_grants,
        )

    found_names = {t for _, t in existing}
    seen: set[tuple[str, str]] = set()
    for schema, table in tables:
        if schema is not None:
            if (schema, table) in seen:
                continue
            seen.add((schema, table))
            facts.tables.append(
                fact(schema, table)
                if (schema, table) in existing
                else TablePrivilegeFacts(schema, table, False)
            )
        elif table in found_names:
            for s, t in sorted(existing):
                if t == table and (s, t) not in seen:
                    seen.add((s, t))
                    facts.tables.append(fact(s, t))
        else:
            facts.tables.append(TablePrivilegeFacts(None, table, False))
    return facts


def _quote_account(account: str) -> str | None:
    """``u@h`` from ``CURRENT_USER()`` as ``SHOW GRANTS FOR`` names it: split at the last
    ``@`` (a user name may hold one, a host may not), each part in backticks with a backtick
    doubled. ``None`` for a value without ``@``."""
    name, at, host = account.rpartition("@")
    if not at:
        return None
    return f"`{name.replace('`', '``')}`@`{host.replace('`', '``')}`"


def _active_roles(value: object) -> list[tuple[str, str]]:
    """``(as printed, u@h form)`` per active role from ``CURRENT_ROLE()``: MySQL prints
    ``` `r`@`%`,`s`@`%` ```, MariaDB a bare name; ``NONE``/NULL is no role."""
    text = "" if value is None else str(value)
    if not text or text.upper() == "NONE":
        return []
    found = _ROLE_PART.findall(text)
    if found:
        return [
            (f"`{u}`@`{h}`", f"{u.replace('``', '`')}@{h.replace('``', '`')}") for u, h in found
        ]
    return [(text, text)]


# MySQL server error numbers (MySQL Reference Manual, "Server Error Message Reference").
# 1969 is MariaDB-only (max_statement_time); 3024 is MySQL-only (MAX_EXECUTION_TIME).
_MYSQL_ERRNO_CODES: dict[int, ErrorCategory] = {
    1054: ErrorCategory.UNKNOWN_NAME,  # ER_BAD_FIELD_ERROR
    1146: ErrorCategory.UNKNOWN_NAME,  # ER_NO_SUCH_TABLE
    1305: ErrorCategory.UNKNOWN_NAME,  # ER_SP_DOES_NOT_EXIST
    1049: ErrorCategory.UNKNOWN_NAME,  # ER_BAD_DB_ERROR
    1582: ErrorCategory.UNKNOWN_NAME,  # ER_WRONG_PARAMCOUNT_TO_NATIVE_FCT
    1583: ErrorCategory.UNKNOWN_NAME,  # ER_WRONG_PARAMETERS_TO_NATIVE_FCT
    1052: ErrorCategory.AMBIGUOUS_NAME,  # ER_NON_UNIQ_ERROR
    1066: ErrorCategory.AMBIGUOUS_NAME,  # ER_NONUNIQ_TABLE
    1064: ErrorCategory.SYNTAX,  # ER_PARSE_ERROR
    1149: ErrorCategory.SYNTAX,  # ER_SYNTAX_ERROR
    1248: ErrorCategory.SYNTAX,  # ER_DERIVED_MUST_HAVE_ALIAS
    1222: ErrorCategory.SYNTAX,  # ER_WRONG_NUMBER_OF_COLUMNS_IN_SELECT
    1055: ErrorCategory.GROUPING,  # ER_WRONG_FIELD_WITH_GROUP
    1056: ErrorCategory.GROUPING,  # ER_WRONG_GROUP_FIELD
    1111: ErrorCategory.GROUPING,  # ER_INVALID_GROUP_FUNC_USE
    1140: ErrorCategory.GROUPING,  # ER_MIX_OF_GROUP_FUNC_AND_FIELDS
    3065: ErrorCategory.GROUPING,  # ER_FIELD_IN_ORDER_NOT_SELECT
    1267: ErrorCategory.TYPE_MISMATCH,  # ER_CANT_AGGREGATE_2COLLATIONS
    1270: ErrorCategory.TYPE_MISMATCH,  # ER_CANT_AGGREGATE_3COLLATIONS
    1271: ErrorCategory.TYPE_MISMATCH,  # ER_CANT_AGGREGATE_NCOLLATIONS
    # SQLSTATE 21000, but "Operand should contain 1 column(s)" is not a row-count problem.
    1241: ErrorCategory.TYPE_MISMATCH,  # ER_OPERAND_COLUMNS
    1292: ErrorCategory.DATA,  # ER_TRUNCATED_WRONG_VALUE
    1366: ErrorCategory.DATA,  # ER_TRUNCATED_WRONG_VALUE_FOR_FIELD
    1264: ErrorCategory.DATA,  # ER_WARN_DATA_OUT_OF_RANGE
    1265: ErrorCategory.DATA,  # WARN_DATA_TRUNCATED
    1406: ErrorCategory.DATA,  # ER_DATA_TOO_LONG
    1690: ErrorCategory.DATA,  # ER_DATA_OUT_OF_RANGE
    1365: ErrorCategory.DIVISION_BY_ZERO,  # ER_DIVISION_BY_ZERO
    1242: ErrorCategory.CARDINALITY,  # ER_SUBQUERY_NO_1_ROW
    1142: ErrorCategory.PERMISSION,  # ER_TABLEACCESS_DENIED_ERROR
    1143: ErrorCategory.PERMISSION,  # ER_COLUMNACCESS_DENIED_ERROR
    1044: ErrorCategory.PERMISSION,  # ER_DBACCESS_DENIED_ERROR
    1227: ErrorCategory.PERMISSION,  # ER_SPECIFIC_ACCESS_DENIED_ERROR
    1370: ErrorCategory.PERMISSION,  # ER_PROCACCESS_DENIED_ERROR
    1235: ErrorCategory.UNSUPPORTED,  # ER_NOT_SUPPORTED_YET
    1290: ErrorCategory.UNSUPPORTED,  # ER_OPTION_PREVENTS_STATEMENT (read_only server)
    1792: ErrorCategory.UNSUPPORTED,  # ER_CANT_EXECUTE_IN_READ_ONLY_TRANSACTION (Task 11)
    3024: ErrorCategory.TIMEOUT,  # ER_QUERY_TIMEOUT
    1969: ErrorCategory.TIMEOUT,  # ER_STATEMENT_TIMEOUT (MariaDB)
}


@register_datasource("mysql")
class MySQLDataSource(BaseDataSource):
    """MySQL / MariaDB data source adapter."""

    source_type = "mysql"
    display_name = "MySQL / MariaDB"
    query_dialect = "MySQL"
    # MySQL's default. connect() reads @@sql_mode once per pool and clears this on the
    # instance under ANSI_QUOTES, where the server reads "…" as a name, as PostgreSQL does.
    double_quote_is_string_literal = True
    # MySQL's default as well; connect() clears it on the instance under NO_BACKSLASH_ESCAPES,
    # where the server reads a backslash in a string as an ordinary character.
    backslash_is_escape = True
    icon = "🐬"

    def __init__(self) -> None:
        self._pool: aiomysql.Pool | None = None
        self._validator = MySQLValidator()
        self._version: str = ""
        self._is_mariadb: bool = False

    # ── Connection management ──────────────────────────────────────────────────

    async def connect(self, config: dict) -> ConnectionResult:
        """Establish a connection pool using the decrypted config dict."""
        host = config.get("host", "?")
        port = config.get("port", 3306)
        database = config.get("database", "?")
        logger.debug(
            "Creating connection pool: host=%s, port=%s, database=%s",
            host,
            port,
            database,
        )
        try:
            self._pool = await aiomysql.create_pool(
                host=config["host"],
                port=int(config.get("port", 3306)),
                user=config["username"],
                password=config["password"],
                db=config["database"],
                ssl=config.get("ssl", False) or None,
                minsize=_POOL_MIN_SIZE,
                maxsize=_POOL_MAX_SIZE,
                autocommit=True,
                connect_timeout=_CONNECT_TIMEOUT_S,
                cursorclass=_Cursor,
            )
            async with self._pool.acquire() as conn, conn.cursor() as cur:
                await cur.execute("SELECT VERSION()")
                row = await cur.fetchone()
                version = row[0] if row else "unknown"
                await cur.execute("SELECT @@sql_mode")
                row = await cur.fetchone()
                raw_mode = row[0] if row else None
            # Some servers and drivers return the variable as bytes; str() would read b'...'.
            if isinstance(raw_mode, bytes | bytearray):
                sql_mode = bytes(raw_mode).decode("utf-8", errors="replace")
            else:
                sql_mode = "" if raw_mode is None else str(raw_mode)
            self._version = version
            self._is_mariadb = "MariaDB" in version
            # @@sql_mode lists the mode expanded (ANSI appears as its members plus ANSI). The
            # validator refuses the known ways a query can change it for its own run (a SET
            # statement, MariaDB's SET STATEMENT ... FOR, MySQL's SET_VAR optimizer hint), so
            # the session mode read here is the one queries on this pool run under.
            modes = {part.strip().upper() for part in sql_mode.split(",")}
            self.double_quote_is_string_literal = "ANSI_QUOTES" not in modes
            self.backslash_is_escape = "NO_BACKSLASH_ESCAPES" not in modes
            logger.info(
                "Connection pool established: host=%s, database=%s, version=%s",
                host,
                database,
                version,
            )
            return ConnectionResult(
                success=True,
                message="Connected successfully",
                server_version=version,
            )
        except Exception as exc:
            logger.warning(
                "Failed to create connection pool: host=%s, database=%s, error=%s",
                host,
                database,
                exc,
            )
            if self._pool is not None:
                self._pool.close()
                await self._pool.wait_closed()
                self._pool = None
            return ConnectionResult(success=False, message=str(exc) or repr(exc))

    async def disconnect(self) -> None:
        """Close the connection pool."""
        if self._pool is not None:
            logger.debug("Closing connection pool")
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    async def test_connection(self, config: dict) -> ConnectionResult:
        """Test connectivity without persisting a connection pool."""
        host = config.get("host", "?")
        port = config.get("port", 3306)
        database = config.get("database", "?")
        logger.debug(
            "Testing connection (no pool): host=%s, port=%s, database=%s",
            host,
            port,
            database,
        )
        conn = None
        try:
            conn = await aiomysql.connect(
                host=config["host"],
                port=int(config.get("port", 3306)),
                user=config["username"],
                password=config["password"],
                db=config["database"],
                ssl=config.get("ssl", False) or None,
                connect_timeout=_CONNECT_TIMEOUT_S,
                cursorclass=_Cursor,
            )
            async with conn.cursor() as cur:
                await cur.execute("SELECT VERSION()")
                row = await cur.fetchone()
                version = row[0] if row else "unknown"
            logger.info(
                "Connection test passed: host=%s, database=%s, version=%s",
                host,
                database,
                version,
            )
            return ConnectionResult(
                success=True,
                message="Connection successful",
                server_version=version,
            )
        except Exception as exc:
            logger.warning(
                "Connection test failed: host=%s, database=%s, error=%s",
                host,
                database,
                exc,
            )
            return ConnectionResult(success=False, message=str(exc) or repr(exc))
        finally:
            if conn is not None:
                conn.close()

    # ── Schema introspection ───────────────────────────────────────────────────

    async def introspect(self, privacy: PrivacySettings | None = None) -> DataSourceSchema:
        """Discover schemas, tables, columns, relationships respecting privacy."""
        if self._pool is None:
            raise RuntimeError("Not connected. Call connect() first.")
        if privacy is None:
            privacy = PrivacySettings()

        # Pre-build introspection queries — _SYSTEM_SCHEMAS_TUPLE is a hardcoded
        # constant, not user input.  Building outside execute() avoids ruff S608.
        _q_schemas = (
            "SELECT SCHEMA_NAME AS schema_name "
            "FROM INFORMATION_SCHEMA.SCHEMATA "
            "WHERE SCHEMA_NAME NOT IN " + _SYSTEM_SCHEMAS_TUPLE + " "
            "ORDER BY SCHEMA_NAME"
        )
        _q_tables = (
            "SELECT TABLE_SCHEMA AS table_schema, "
            "TABLE_NAME AS table_name, "
            "TABLE_TYPE AS table_type, "
            "TABLE_ROWS AS table_rows, "
            "TABLE_COMMENT AS table_comment "
            "FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA NOT IN " + _SYSTEM_SCHEMAS_TUPLE + " "
            "AND TABLE_TYPE IN ('BASE TABLE', 'VIEW') "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME"
        )
        _q_columns = (
            "SELECT TABLE_SCHEMA AS table_schema, "
            "TABLE_NAME AS table_name, "
            "COLUMN_NAME AS column_name, "
            "DATA_TYPE AS data_type, "
            "COLUMN_TYPE AS column_type, "
            "IS_NULLABLE AS is_nullable, "
            "COLUMN_KEY AS column_key, "
            "ORDINAL_POSITION AS ordinal_position, "
            "COLUMN_COMMENT AS column_comment "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA NOT IN " + _SYSTEM_SCHEMAS_TUPLE + " "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
        )
        _q_fkeys = (
            "SELECT TABLE_SCHEMA AS from_schema, "
            "TABLE_NAME AS from_table, "
            "COLUMN_NAME AS from_column, "
            "REFERENCED_TABLE_SCHEMA AS to_schema, "
            "REFERENCED_TABLE_NAME AS to_table, "
            "REFERENCED_COLUMN_NAME AS to_column "
            "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
            "WHERE REFERENCED_TABLE_NAME IS NOT NULL "
            "AND TABLE_SCHEMA NOT IN " + _SYSTEM_SCHEMAS_TUPLE
        )

        async with self._pool.acquire() as conn, conn.cursor(_DictCursor) as cur:
            # 1. Schemas (MySQL calls them "databases")
            await cur.execute(_q_schemas)
            schema_rows = await cur.fetchall()

            # 2. Tables and views
            await cur.execute(_q_tables)
            table_rows = await cur.fetchall()

            # 3. Columns — COLUMN_KEY='PRI' flags primary keys, COLUMN_COMMENT for descriptions
            await cur.execute(_q_columns)
            column_rows = await cur.fetchall()

            # 4. Foreign keys — KEY_COLUMN_USAGE already has referenced table/column info
            await cur.execute(_q_fkeys)
            fk_rows = await cur.fetchall()

        schemas = [
            SchemaInfo(name=row["schema_name"])
            for row in schema_rows
            if row["schema_name"] not in privacy.excluded_schemas
            and row["schema_name"] not in _SYSTEM_SCHEMAS
        ]

        relationships = [
            RelationshipInfo(
                from_schema=row["from_schema"],
                from_table=row["from_table"],
                from_column=row["from_column"],
                to_schema=row["to_schema"],
                to_table=row["to_table"],
                to_column=row["to_column"],
            )
            for row in fk_rows
        ]

        # Build column lookup per (schema, table)
        columns_by_table: dict[tuple[str, str], list[ColumnInfo]] = {}
        for row in column_rows:
            schema = row["table_schema"]
            table = row["table_name"]
            col_name = row["column_name"]

            if privacy.is_table_excluded(schema, table):
                continue
            if f"{schema}.{table}.{col_name}" in privacy.excluded_columns:
                continue

            key = (schema, table)
            if key not in columns_by_table:
                columns_by_table[key] = []

            comment = row["column_comment"] or None

            # ENUM/SET members are the column's value domain: sample values and the type
            # string both carry them, so both follow the same decision.
            labels_allowed = privacy.column_values_allowed(col_name)
            sample_values: list[str] | None = None
            if labels_allowed and _is_enum_or_set(row["column_type"]):
                sample_values = _parse_enum_values(row["column_type"])

            columns_by_table[key].append(
                ColumnInfo(
                    name=col_name,
                    data_type=row["data_type"],
                    native_type=(
                        row["column_type"]
                        if labels_allowed
                        else withhold_enum_labels(row["column_type"])
                    ),
                    nullable=row["is_nullable"] == "YES",
                    is_primary_key=row["column_key"] == "PRI",
                    description=comment if privacy.include_column_comments else None,
                    sample_values=sample_values,
                    sample_source="type" if sample_values is not None else None,
                )
            )

        _table_type_map = {"BASE TABLE": "table", "VIEW": "view"}
        tables: list[TableInfo] = []
        for row in table_rows:
            schema = row["table_schema"]
            table = row["table_name"]

            if privacy.is_table_excluded(schema, table):
                continue

            table_type = _table_type_map.get(row["table_type"], row["table_type"].lower())
            table_comment = row["table_comment"] or None
            row_count = row["table_rows"] if privacy.include_row_counts else None

            tables.append(
                TableInfo(
                    catalog=None,
                    schema_name=schema,
                    name=table,
                    table_type=table_type,
                    columns=columns_by_table.get((schema, table), []),
                    row_count_approx=row_count,
                    description=table_comment if privacy.include_column_comments else None,
                )
            )

        return DataSourceSchema(
            source_type="mysql",
            schemas=schemas,
            tables=tables,
            relationships=relationships,
        )

    # OVERHEAD: catalog-read for ENUM/SET; small DISTINCT query with LIMIT for others
    async def get_sample_values(
        self,
        schema: str,
        table: str,
        column: str,
        limit: int = 5,
    ) -> list[str]:
        """Retrieve sample values.

        ENUM/SET columns are resolved from INFORMATION_SCHEMA (zero table scan).
        All other columns run a small DISTINCT query bounded by LIMIT.
        """
        if self._pool is None:
            raise RuntimeError("Not connected.")

        async with self._pool.acquire() as conn:
            async with conn.cursor(_DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT COLUMN_TYPE
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
                    """,
                    (schema, table, column),
                )
                row = await cur.fetchone()
                if row and _is_enum_or_set(row["COLUMN_TYPE"]):
                    return _parse_enum_values(row["COLUMN_TYPE"])[:limit]

            # Fallback: bounded DISTINCT query
            q_schema = self._quote_identifier(schema)
            q_table = self._quote_identifier(table)
            q_column = self._quote_identifier(column)
            sample_query = (
                "SELECT DISTINCT " + q_column + " "
                "FROM " + q_schema + "." + q_table + " "
                "WHERE " + q_column + " IS NOT NULL "
                "LIMIT " + str(int(limit))
            )
            try:
                async with conn.cursor() as cur:
                    await cur.execute(sample_query)
                    rows = await cur.fetchall()
                    return [str(r[0]) for r in rows]
            except Exception:
                return []

    # ── Query execution ────────────────────────────────────────────────────────

    async def execute_query(
        self,
        query: str,
        timeout: int = 30,  # noqa: ASYNC109
        max_rows: int = 1000,
    ) -> QueryResult:
        """Execute a read-only query and return standardised results."""
        if self._pool is None:
            raise RuntimeError("Not connected.")

        query = _fix_reserved_aliases(query)
        start = time.monotonic()
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            # Set query timeout — syntax differs between MySQL and MariaDB
            if self._is_mariadb:
                await cur.execute("SET SESSION max_statement_time = %s", (timeout,))
            else:
                await cur.execute("SET SESSION MAX_EXECUTION_TIME = %s", (timeout * 1000,))
            # Enforced by the engine for every autocommitted statement until reset: a write
            # the text validator missed is refused with errno 1792.
            await cur.execute("SET SESSION TRANSACTION READ ONLY")
            try:
                await cur.execute(query)
                records = await cur.fetchall()
                description = cur.description or []
            finally:
                await cur.execute("SET SESSION TRANSACTION READ WRITE")
                # Reset so the pooled connection is clean for reuse
                if self._is_mariadb:
                    await cur.execute("SET SESSION max_statement_time = 0")
                else:
                    await cur.execute("SET SESSION MAX_EXECUTION_TIME = 0")

        elapsed_ms = (time.monotonic() - start) * 1000

        if not records or not description:
            return QueryResult(
                columns=[],
                column_types=[],
                rows=[],
                row_count=0,
                execution_time_ms=elapsed_ms,
            )

        columns = [d[0] for d in description]
        column_types = [
            type(records[0][i]).__name__ if records[0][i] is not None else "unknown"
            for i in range(len(columns))
        ]

        truncated = len(records) > max_rows
        rows: list[list[Any]] = [[_serialize(cell) for cell in row] for row in records[:max_rows]]

        return QueryResult(
            columns=columns,
            column_types=column_types,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            execution_time_ms=elapsed_ms,
        )

    async def explain(self, sql: str, timeout: int) -> None:  # noqa: ASYNC109
        """Plan *sql* with ``EXPLAIN`` inside the read-only session ``execute_query`` uses:
        the optimizer may evaluate constant subqueries, and stored functions declared
        ``DETERMINISTIC``, while planning (volatile functions are not evaluated), and the
        engine refuses a write among them. MySQL's ``MAX_EXECUTION_TIME`` applies to ``SELECT``
        only and aiomysql has no read timeout, so on MySQL the adapter
        itself bounds nothing once connected (the connections router bounds the call with
        ``asyncio.wait_for``);
        ``sql_notes`` is off while planning, so the note that quotes the rewritten statement
        is never raised;
        MariaDB's ``max_statement_time`` applies to every statement and is set to *timeout*.
        A plan takes milliseconds and may read a single row by unique key while planning
        (const tables); nothing is returned. A cancelled plan (the caller's
        ``asyncio.wait_for``) closes the connection and skips the session resets, so the
        cancellation, not a driver error from a reset on the closed connection, reaches the
        caller."""
        if self._pool is None:
            raise RuntimeError("Not connected.")
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            if self._is_mariadb:
                await cur.execute("SET SESSION max_statement_time = %s", (timeout,))
            await cur.execute("SET SESSION TRANSACTION READ ONLY")
            # A successful EXPLAIN raises Note 1003, which quotes the rewritten statement with
            # the condition's literals. aiomysql would fetch it with SHOW WARNINGS and pass it
            # to warnings.warn, so it would reach the logs; with notes off it is never raised.
            await cur.execute("SET SESSION sql_notes = 0")
            cancelled = False
            try:
                await cur.execute(f"EXPLAIN {_fix_reserved_aliases(sql)}")
            except asyncio.CancelledError:
                # The caller stopped waiting (the router's budget). aiomysql closes the
                # connection when a read is cancelled, so a reset here would raise
                # InterfaceError and replace the CancelledError that asyncio.wait_for turns
                # into a timeout. The explicit close() is defensive (a cancellation at an
                # await that is not a packet read); it is synchronous, and the pool discards
                # a closed connection on release.
                cancelled = True
                conn.close()
                raise
            except Exception as exc:
                raise ExplainError(str(exc) or type(exc).__name__) from exc
            finally:
                if not cancelled:
                    await cur.execute("SET SESSION sql_notes = DEFAULT")
                    await cur.execute("SET SESSION TRANSACTION READ WRITE")
                    if self._is_mariadb:
                        await cur.execute("SET SESSION max_statement_time = 0")

    async def audit_privileges(
        self,
        tables: list[tuple[str | None, str]],
        timeout: int,  # noqa: ASYNC109
    ) -> PrivilegeFacts:
        """Read the grants per grantee and the requested tables, in this order:
        ``SELECT CURRENT_USER()``; ``SHOW GRANTS FOR `u`@`h``` with the account named
        explicitly (the account's own lines, grantee the account as ``CURRENT_USER()`` prints
        it; ``SHOW GRANTS FOR CURRENT_USER()`` when the explicit form fails, which on MySQL 8
        folds the active roles in); ``SELECT CURRENT_ROLE()``; per active role, the lines it
        contributes (``_role_share``: grantee the role, or the account when the role's share
        cannot be read); one ``information_schema.tables``
        lookup when tables are wanted (``exists`` means visible to the account: that view
        lists only the tables the account has some privilege on, so a table it cannot read
        at all reads ``exists False``, and no finding follows either way); and the
        non-system schemata. A role the account holds but has not activated is out of scope:
        its privileges do not apply to the session either. Bounded by
        ``max_statement_time`` on MariaDB; on MySQL the router's ``asyncio.wait_for`` is the
        only bound, and a cancelled audit closes the connection and skips the reset, as
        ``explain`` does. See ``BaseDataSource.audit_privileges``."""
        if self._pool is None:
            raise RuntimeError("Not connected.")
        wanted = list(dict.fromkeys(tables))
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            if self._is_mariadb:
                await cur.execute("SET SESSION max_statement_time = %s", (timeout,))
            cancelled = False
            try:
                await cur.execute("SELECT CURRENT_USER()")
                row = await cur.fetchone()
                role = str(row[0]) if row and row[0] is not None else ""
                target, direct_lines = await self._direct_lines(cur, role)
                direct = _grant_set(direct_lines)
                grants: list[tuple[str, _GrantSet]] = [(role, direct)]
                await cur.execute("SELECT CURRENT_ROLE()")
                row = await cur.fetchone()
                for raw, name in _active_roles(row[0] if row else None):
                    grants.append(await self._role_share(cur, raw, name, role, target, direct))
                existing = await self._existing_tables(cur, wanted) if wanted else set()
                await cur.execute(_SCHEMAS_SQL)
                schemas = [str(r[0]) for r in await cur.fetchall()]
            except asyncio.CancelledError:
                cancelled = True
                conn.close()
                raise
            finally:
                if self._is_mariadb and not cancelled:
                    await cur.execute("SET SESSION max_statement_time = 0")
        return _facts_from_grants(role, grants, wanted, existing, schemas, self._is_mariadb)

    @staticmethod
    async def _direct_lines(cur: aiomysql.Cursor, account: str) -> tuple[str, list[str]]:
        """``(target, lines)``: the account's own grant lines and how the statement named it.
        ``SHOW GRANTS FOR `u`@`h``` with the ``CURRENT_USER()`` value named explicitly:
        checked live on MySQL 8.0.46, it lists the account's own grants only and needs no
        privilege on the mysql schema, while ``SHOW GRANTS FOR CURRENT_USER()`` folds the
        active roles' grants in. When the explicit form fails (or the value has no ``@``),
        ``SHOW GRANTS FOR CURRENT_USER()``: the roles' share then cannot be told apart and is
        attributed to the account."""
        quoted = _quote_account(account)
        if quoted is not None:
            statement = f"SHOW GRANTS FOR {quoted}"
            try:
                await cur.execute(statement)
            except aiomysql.Error as exc:
                logger.debug("Direct grants unavailable (%s): %s", statement, exc)
            else:
                return quoted, [str(r[0]) for r in await cur.fetchall()]
        await cur.execute("SHOW GRANTS FOR CURRENT_USER()")
        return "CURRENT_USER()", [str(r[0]) for r in await cur.fetchall()]

    async def _role_share(
        self,
        cur: aiomysql.Cursor,
        raw: str,
        name: str,
        account: str,
        target: str,
        direct: _GrantSet,
    ) -> tuple[str, _GrantSet]:
        """``(grantee, grants)``: what an active role contributes. *target* names the account
        as ``_direct_lines`` did. MariaDB: ``SHOW GRANTS FOR role``, as it is. MySQL 8:
        ``SHOW GRANTS FOR <target> USING role`` beyond the direct grants (``_GrantSet.minus``:
        MySQL merges the account's and the role's privileges into one line per scope; the
        form needs no privilege on the mysql schema); when that fails, ``SHOW GRANTS FOR
        role`` beyond them; when that fails too, the bare ``SHOW GRANTS`` beyond them,
        attributed to the account (the role's share cannot be told apart).

        The difference is the role's share only when *target* is the explicit account: on
        MySQL 8.0.46 (checked live) ``SHOW GRANTS FOR CURRENT_USER()`` folds the active roles
        in and the explicit account does not. After the ``CURRENT_USER()`` fallback the direct
        grants already hold the roles' share, so ``SHOW GRANTS FOR role`` is skipped and what
        remains is attributed to the account."""
        if self._is_mariadb:
            await cur.execute(f"SHOW GRANTS FOR {self._quote_identifier(name)}")
            return name, _grant_set([str(r[0]) for r in await cur.fetchall()])
        statements = [f"SHOW GRANTS FOR {target} USING {raw}"]
        if target != "CURRENT_USER()":
            statements.append(f"SHOW GRANTS FOR {raw}")
        for statement in statements:
            try:
                await cur.execute(statement)
            except aiomysql.Error as exc:
                logger.debug("Role grants unavailable (%s): %s", statement, exc)
                continue
            lines = [str(r[0]) for r in await cur.fetchall()]
            return name, _grant_set(lines).minus(direct)
        await cur.execute("SHOW GRANTS")
        lines = [str(r[0]) for r in await cur.fetchall()]
        return account, _grant_set(lines).minus(direct)

    @staticmethod
    async def _existing_tables(
        cur: aiomysql.Cursor, tables: list[tuple[str | None, str]]
    ) -> set[tuple[str, str]]:
        """The ``(schema, table)`` pairs of *tables* that exist: a qualified pair exactly, a
        bare name in every schema but the system ones."""
        qualified = [(s, t) for s, t in tables if s is not None]
        bare = [t for s, t in tables if s is None]
        clauses: list[str] = []
        params: list[str] = []
        if qualified:
            clauses.append(
                "("
                + " OR ".join("(table_schema = %s AND table_name = %s)" for _ in qualified)
                + ")"
            )
            for schema, table in qualified:
                params.extend([schema, table])
        if bare:
            clauses.append(
                "(table_name IN (" + ", ".join("%s" for _ in bare) + ")"
                f" AND table_schema NOT IN {_SYSTEM_SCHEMAS_TUPLE})"
            )
            params.extend(bare)
        await cur.execute(
            # Only %s placeholders and the fixed system-schema list are interpolated.
            "SELECT table_schema, table_name FROM information_schema.tables WHERE "  # noqa: S608
            + " OR ".join(clauses),
            params,
        )
        return {(str(r[0]), str(r[1])) for r in await cur.fetchall()}

    async def execute_query_stream(
        self,
        query: str,
        timeout: int = 30,  # noqa: ASYNC109
        batch_size: int = 50,
        max_rows: int = 1000,
    ) -> AsyncGenerator[QueryResult, None]:
        """Stream query results using an aiomysql SSCursor (unbuffered server-side cursor).

        Unlike the base-class default which calls execute_query() → fetchall(), this
        override uses SSCursor so rows are pulled from the server incrementally.
        This avoids loading the entire result set into memory before yielding the
        first batch, eliminating the OOM risk for large result sets.

        Timeout is applied via SET SESSION MAX_EXECUTION_TIME (MySQL) or
        max_statement_time (MariaDB) and reset in a finally block on the same
        connection so the pooled connection is clean for reuse.
        """
        if self._pool is None:
            raise RuntimeError("Not connected.")

        query = _fix_reserved_aliases(query)
        start = time.monotonic()
        total = 0
        first_batch = True

        async with self._pool.acquire() as conn:
            # Set query timeout on a regular cursor *before* opening SSCursor.
            # SSCursor keeps the connection in a "reading" state; mixing cursor
            # types on one connection is safe in aiomysql as long as they don't
            # overlap.
            async with conn.cursor() as setup_cur:
                if self._is_mariadb:
                    await setup_cur.execute("SET SESSION max_statement_time = %s", (timeout,))
                else:
                    await setup_cur.execute(
                        "SET SESSION MAX_EXECUTION_TIME = %s", (timeout * 1000,)
                    )
                await setup_cur.execute("SET SESSION TRANSACTION READ ONLY")

            try:
                async with conn.cursor(_SSCursor) as cur:
                    await cur.execute(query)
                    description = cur.description or []

                    if not description:
                        return

                    columns = [d[0] for d in description]
                    column_types: list[str] | None = None

                    while total < max_rows:
                        remaining = max_rows - total
                        records = await cur.fetchmany(min(batch_size, remaining))
                        if not records:
                            break

                        if column_types is None:
                            column_types = [
                                type(records[0][i]).__name__
                                if records[0][i] is not None
                                else "unknown"
                                for i in range(len(columns))
                            ]

                        rows: list[list[Any]] = [
                            [_serialize(cell) for cell in row] for row in records
                        ]
                        total += len(rows)
                        elapsed = (time.monotonic() - start) * 1000 if first_batch else 0.0
                        first_batch = False

                        yield QueryResult(
                            columns=columns,
                            column_types=column_types,
                            rows=rows,
                            row_count=total,
                            truncated=(total >= max_rows),
                            execution_time_ms=elapsed,
                        )
            finally:
                # Reset timeout so the returned connection is clean for reuse.
                async with conn.cursor() as reset_cur:
                    if self._is_mariadb:
                        await reset_cur.execute("SET SESSION max_statement_time = 0")
                    else:
                        await reset_cur.execute("SET SESSION MAX_EXECUTION_TIME = 0")
                    await reset_cur.execute("SET SESSION TRANSACTION READ WRITE")

    # ── Validation ─────────────────────────────────────────────────────────────

    def validate_query(self, query: str) -> ValidationResult:
        """Delegate to the MySQL-specific validator."""
        return self._validator.validate(query)

    def describe_connection_error(self, text: str) -> str | None:
        """A sentence for the MySQL error number at the start of a driver message
        (``(1045, "Access denied ...")``); ``MySQL error N`` for an unknown number; ``None``
        without a number. Never includes the driver's text, which can name the user and
        host. See ``BaseDataSource.describe_connection_error``."""
        match = _ERRNO_PREFIX.match(text.strip())
        if match is None:
            return None
        errno = int(match.group(1))
        return _CONNECTION_ERROR_SENTENCES.get(errno, f"MySQL error {errno}")

    def classify_error(self, exc: BaseException) -> ErrorCategory:
        """Map a pymysql/aiomysql failure via its numeric errno (``exc.args[0]``)."""
        return classify_chain(exc, self._classify_one)

    @staticmethod
    def _classify_one(err: BaseException) -> ErrorCategory | None:
        # `aiomysql.MySQLError` *is* `pymysql.err.MySQLError`; restricting to it keeps an
        # unrelated exception whose first arg happens to be an int from being misread.
        if not isinstance(err, aiomysql.MySQLError):
            return None
        args = getattr(err, "args", ())
        if args and isinstance(args[0], int):
            return _MYSQL_ERRNO_CODES.get(args[0])
        return None

    # ── LLM formatting ─────────────────────────────────────────────────────────

    def _quote_identifier(self, name: str) -> str:
        """Escape a MySQL identifier: double any embedded backtick and wrap in backticks."""
        return "`" + name.replace("`", "``") + "`"

    def _schema_section_label(self) -> str:
        return "Database"

    def get_system_prompt_additions(self) -> str:
        """Return MySQL/MariaDB-specific instructions for the LLM."""
        return """\
You are querying a MySQL / MariaDB database. Use MySQL-compatible syntax:
- BACKTICK RULE (critical — syntax errors result from violations): wrap ALL identifiers \
AND ALL column aliases in backticks: `table`.`column` and AS `alias`. This includes \
every alias in SELECT, CTE definitions, and ORDER BY references.
  Reserved words that MUST be backtick-quoted when used as aliases (not exhaustive):
  RANK, DENSE_RANK, ROW_NUMBER, LEAD, LAG, NTILE, OVER, PARTITION, WINDOW, ROWS, GROUPS,
  YEAR_MONTH, ORDER, GROUP, KEY
  Examples: RANK() OVER (...) AS `rank`   LAG(...) AS `prev_value`   AS `year_month`
  Never write: AS rank   AS year_month   AS lead   (all cause 1064 syntax errors)
- Available functions: DATE_FORMAT(), CONCAT(), COALESCE(), IF(), IFNULL(), \
GROUP_CONCAT(), DATE_ADD(), UNIX_TIMESTAMP(), STR_TO_DATE()
- Window functions (MySQL 8+ / MariaDB 10.2+): ROW_NUMBER(), RANK(), LAG(), LEAD(), \
DENSE_RANK(), NTILE(). Always backtick-quote their result aliases.
- CTEs (WITH clauses) available in MySQL 8+ / MariaDB 10.2+
- No FULL OUTER JOIN — use UNION of LEFT JOIN and RIGHT JOIN instead
- Use LIMIT (not FETCH FIRST) for row limiting
- Date intervals: DATE_ADD(date, INTERVAL 30 DAY), CURDATE(), NOW()
- Case-insensitive matching: LIKE (default on most collations) or LOWER() for explicit handling"""

    @classmethod
    def get_config_schema(cls) -> dict:
        """Return the JSON config schema used by the frontend to render a connection form."""
        return {
            "fields": [
                {
                    "name": "host",
                    "type": "string",
                    "label": "Host",
                    "required": True,
                    "placeholder": "localhost",
                },
                {
                    "name": "port",
                    "type": "integer",
                    "label": "Port",
                    "required": False,
                    "default": 3306,
                    "placeholder": "3306",
                },
                {
                    "name": "database",
                    "type": "string",
                    "label": "Database",
                    "required": True,
                    "placeholder": "my_database",
                },
                {
                    "name": "username",
                    "type": "string",
                    "label": "Username",
                    "required": True,
                    "placeholder": "readonly_user",
                    "credential": True,
                },
                {
                    "name": "password",
                    "type": "password",
                    "label": "Password",
                    "required": True,
                    "credential": True,
                },
                {
                    "name": "ssl",
                    "type": "boolean",
                    "label": "Use SSL",
                    "required": False,
                    "default": False,
                },
            ]
        }


# ── Helpers ────────────────────────────────────────────────────────────────────

# MySQL 8 reserved keywords that LLMs routinely use as unquoted column aliases,
# causing 1064 syntax errors.  Verified against information_schema.keywords +
# manual testing.  Only words that (a) are actually reserved AND (b) commonly
# appear as LLM-generated alias names are included — not the full reserved list.
_RESERVED_ALIAS_WORDS: frozenset[str] = frozenset(
    {
        "rank",
        "dense_rank",
        "row_number",
        "lead",
        "lag",
        "ntile",
        "cume_dist",
        "percent_rank",
        "year_month",
        "over",
        "partition",
        "window",
        "rows",
        "groups",
        "key",
        "order",
        "group",
    }
)

# Pass 1 — AS <word>: column alias definitions.
# (?!`) prevents double-wrapping already-quoted aliases.
_ALIAS_RE = re.compile(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)(?!`)", re.IGNORECASE)

# Pass 2 — ORDER BY / comma sort terms.
# Matches the separator (ORDER BY | ,) + bare word, followed by ASC/DESC/,/;/EOL/closing paren.
# This also fixes aliases referenced in ORDER BY, not just their definitions.
_ORDER_RESERVED_RE = re.compile(
    r"(ORDER\s+BY|,)\s+([A-Za-z_][A-Za-z0-9_]*)(?!`)(?=\s*(?:ASC\b|DESC\b|,|;|\n|\)|$))",
    re.IGNORECASE,
)


def _fix_reserved_aliases(sql: str) -> str:
    """Backtick-quote reserved-word identifiers that LLMs leave unquoted.

    Two passes:
    1. AS <reserved>  — column alias definitions in SELECT / CTE output.
    2. ORDER BY / comma sort terms — alias references in ORDER BY clauses.

    Best-effort; does not parse string literals or comments, but the words
    in _RESERVED_ALIAS_WORDS are unlikely to appear literally inside strings.
    """

    def _fix_as(m: re.Match) -> str:
        word = m.group(1)
        if word.lower() in _RESERVED_ALIAS_WORDS:
            logger.debug("Auto-quoting reserved alias: AS %s -> AS `%s`", word, word)
            return f"AS `{word}`"
        return m.group(0)

    def _fix_order(m: re.Match) -> str:
        prefix, word = m.group(1), m.group(2)
        if word.lower() in _RESERVED_ALIAS_WORDS:
            logger.debug("Auto-quoting reserved ORDER BY ref: %s -> `%s`", word, word)
            return f"{prefix} `{word}`"
        return m.group(0)

    result = _ALIAS_RE.sub(_fix_as, sql)
    result = _ORDER_RESERVED_RE.sub(_fix_order, result)
    return result


def _is_enum_or_set(column_type: str) -> bool:
    """Return True if the MySQL column type is ENUM or SET."""
    return column_type.upper().startswith(("ENUM(", "SET("))


def _parse_enum_values(column_type: str) -> list[str]:
    """Extract quoted values from ENUM(...) or SET(...) column type strings.

    E.g. "enum('active','inactive','pending')" → ["active", "inactive", "pending"]
    """
    match = re.search(r"\((.+)\)$", column_type, re.IGNORECASE)
    if not match:
        return []
    return re.findall(r"'([^']*)'", match.group(1))
