# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""MySQL / MariaDB data source adapter."""

from collections.abc import AsyncGenerator
import logging
import re
import time
from typing import Any

import aiomysql

from ..base import BaseDataSource, _serialize
from ..models import (
    ColumnInfo,
    ConnectionResult,
    DataSourceSchema,
    PrivacySettings,
    QueryResult,
    RelationshipInfo,
    SchemaInfo,
    TableInfo,
    ValidationResult,
)
from ..registry import register_datasource
from ..validators.mysql_validator import MySQLValidator

logger = logging.getLogger(__name__)

# Connection pool sizing — keep small to avoid monopolising shared MySQL servers.
# Raise _POOL_MAX_SIZE if the app has more concurrent users and the server allows it.
_POOL_MIN_SIZE: int = 1
_POOL_MAX_SIZE: int = 5

# TCP-level timeout (seconds) for both pool creation and test_connection().
_CONNECT_TIMEOUT_S: int = 10

_SYSTEM_SCHEMAS = frozenset({"information_schema", "mysql", "performance_schema", "sys"})

# Generated from _SYSTEM_SCHEMAS so that adding a schema only requires editing the frozenset.
_SYSTEM_SCHEMAS_TUPLE = "(" + ", ".join(f"'{s}'" for s in sorted(_SYSTEM_SCHEMAS)) + ")"


@register_datasource("mysql")
class MySQLDataSource(BaseDataSource):
    """MySQL / MariaDB data source adapter."""

    source_type = "mysql"
    display_name = "MySQL / MariaDB"
    query_dialect = "MySQL"
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
            )
            async with self._pool.acquire() as conn, conn.cursor() as cur:
                await cur.execute("SELECT VERSION()")
                row = await cur.fetchone()
                version = row[0] if row else "unknown"
            self._version = version
            self._is_mariadb = "MariaDB" in version
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

        async with self._pool.acquire() as conn, conn.cursor(aiomysql.DictCursor) as cur:
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

            # ENUM/SET: extract sample values from column type without any table scan
            sample_values: list[str] | None = None
            if privacy.include_sample_values and _is_enum_or_set(row["column_type"]):
                sample_values = _parse_enum_values(row["column_type"])

            columns_by_table[key].append(
                ColumnInfo(
                    name=col_name,
                    data_type=row["data_type"],
                    native_type=row["column_type"],
                    nullable=row["is_nullable"] == "YES",
                    is_primary_key=row["column_key"] == "PRI",
                    description=comment if privacy.include_column_comments else None,
                    sample_values=sample_values,
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
            async with conn.cursor(aiomysql.DictCursor) as cur:
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
            try:
                await cur.execute(query)
                records = await cur.fetchall()
                description = cur.description or []
            finally:
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

            try:
                async with conn.cursor(aiomysql.SSCursor) as cur:
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

    # ── Validation ─────────────────────────────────────────────────────────────

    def validate_query(self, query: str) -> ValidationResult:
        """Delegate to the MySQL-specific validator."""
        return self._validator.validate(query)

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
