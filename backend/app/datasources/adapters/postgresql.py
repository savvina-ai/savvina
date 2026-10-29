# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

import asyncio
from collections.abc import AsyncGenerator
import hashlib
import json as _json
import logging
import time
from typing import Any

import asyncpg

from ..base import BaseDataSource, _serialize
from ..errors import (
    NAME_ECHOING_CATEGORIES,
    ErrorCategory,
    ExplainError,
    category_for_sqlstate,
    classify_chain,
    refine_undefined_function,
)
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
from ..validators.postgresql_validator import PostgreSQLValidator

logger = logging.getLogger(__name__)

# Pools keyed by a hash of connection credentials, reused across requests.
_pool_cache: dict[str, asyncpg.Pool] = {}

# Timeout (seconds) for establishing the initial connection(s) when creating a
# new pool.  Prevents a stalled TCP/auth handshake from blocking the event loop.
_POOL_CONNECT_TIMEOUT_S: int = 30

# Timeout (seconds) for the single ephemeral connection used by test_connection().
# Mirrors the connect_timeout=10 used in the MySQL adapter.
_TEST_CONNECT_TIMEOUT_S: int = 10

# Connection pool sizing — same defaults as the MySQL adapter.
# Raise _POOL_MAX_SIZE if the app has more concurrent users and the server allows it.
_POOL_MIN_SIZE: int = 1
_POOL_MAX_SIZE: int = 5


def _pool_key(config: dict) -> str:
    fields = {
        k: config.get(k) for k in ("host", "port", "database", "username", "password", "ssl_mode")
    }
    return hashlib.sha256(_json.dumps(fields, sort_keys=True).encode()).hexdigest()


async def evict_pool(config: dict) -> None:
    """Close and remove the cached pool for these credentials.

    Call this when a connection's config is updated or the connection is deleted
    so stale pools don't linger with outdated credentials.
    """
    key = _pool_key(config)
    pool = _pool_cache.pop(key, None)
    if pool is not None:
        await pool.close()


async def close_all_pools() -> None:
    """Close and evict every cached asyncpg pool.

    Called during application shutdown to ensure server-side connections are
    released before the process exits.  Safe to call more than once.
    """
    keys = list(_pool_cache.keys())
    for key in keys:
        pool = _pool_cache.pop(key, None)
        if pool is not None and not pool.is_closing():
            await pool.close()


def _column_types(record: Any) -> list[str]:
    """Python type name of each field of *record*, by position (``unknown`` for NULL)."""
    return [type(v).__name__ if v is not None else "unknown" for v in record.values()]


def _quote_pg_ident(name: str) -> str:
    """Return a double-quoted PostgreSQL identifier with any embedded quotes escaped."""
    return '"' + name.replace('"', '""') + '"'


@register_datasource("postgresql")
class PostgreSQLDataSource(BaseDataSource):
    """PostgreSQL data source adapter — the reference implementation."""

    source_type = "postgresql"
    display_name = "PostgreSQL"
    query_dialect = "PostgreSQL"
    icon = "🐘"

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None
        self._validator = PostgreSQLValidator()

    # ── Connection management ──────────────────────────────────────────────────

    async def connect(self, config: dict) -> ConnectionResult:
        """Establish a connection pool using the decrypted config dict."""
        host = config.get("host", "?")
        port = config.get("port", 5432)
        database = config.get("database", "?")
        logger.debug(
            "Creating connection pool: host=%s, port=%s, database=%s, ssl=%s",
            host,
            port,
            database,
            config.get("ssl_mode", "prefer"),
        )
        key = _pool_key(config)
        cached = _pool_cache.get(key)
        if cached is not None and not cached.is_closing():
            self._pool = cached
            logger.debug(
                "Reusing cached connection pool: host=%s, database=%s",
                host,
                database,
            )
            return ConnectionResult(success=True, message="Connected successfully")

        try:
            ssl = config.get("ssl_mode", "prefer")
            self._pool = await asyncio.wait_for(
                asyncpg.create_pool(
                    host=config["host"],
                    port=int(config.get("port", 5432)),
                    database=config["database"],
                    user=config["username"],
                    password=config["password"],
                    ssl=ssl if ssl != "disable" else None,
                    min_size=_POOL_MIN_SIZE,
                    max_size=_POOL_MAX_SIZE,
                    command_timeout=60,
                ),
                timeout=_POOL_CONNECT_TIMEOUT_S,
            )
            async with self._pool.acquire() as conn:
                version = await conn.fetchval("SELECT version()")
            _pool_cache[key] = self._pool
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
        except TimeoutError:
            msg = (
                f"Connection timed out after {_POOL_CONNECT_TIMEOUT_S}s"
                " — check host, port, and firewall/security-group rules"
            )
            logger.warning(
                "Connection pool timed out: host=%s, database=%s",
                host,
                database,
            )
            _pool_cache.pop(key, None)
            if self._pool is not None:
                await self._pool.close()
                self._pool = None
            return ConnectionResult(success=False, message=msg)
        except Exception as exc:
            logger.warning(
                "Failed to create connection pool: host=%s, database=%s, error=%s",
                host,
                database,
                exc,
            )
            # Close and evict the pool if it was partially created before failure.
            _pool_cache.pop(key, None)
            if self._pool is not None:
                await self._pool.close()
                self._pool = None
            return ConnectionResult(success=False, message=str(exc) or repr(exc))

    async def disconnect(self) -> None:
        """Release this adapter's reference to the pool.

        The pool stays in _pool_cache for reuse by the next request with the
        same credentials. Call evict_pool() explicitly to actually close it
        (e.g. when credentials change or the connection is deleted).
        """
        self._pool = None

    async def test_connection(self, config: dict) -> ConnectionResult:
        """Test connectivity without persisting a connection pool."""
        host = config.get("host", "?")
        port = config.get("port", 5432)
        database = config.get("database", "?")
        logger.debug(
            "Testing connection (no pool): host=%s, port=%s, database=%s, ssl=%s",
            host,
            port,
            database,
            config.get("ssl_mode", "prefer"),
        )
        try:
            ssl = config.get("ssl_mode", "prefer")
            conn = await asyncpg.connect(
                host=config["host"],
                port=int(config.get("port", 5432)),
                database=config["database"],
                user=config["username"],
                password=config["password"],
                ssl=ssl if ssl != "disable" else None,
                timeout=_TEST_CONNECT_TIMEOUT_S,
            )
            try:
                version = await conn.fetchval("SELECT version()")
            finally:
                await conn.close()
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
        except TimeoutError:
            msg = (
                f"Connection timed out after {_TEST_CONNECT_TIMEOUT_S}s"
                " — check host, port, and firewall/security-group rules"
            )
            logger.warning(
                "Connection test timed out: host=%s, database=%s",
                host,
                database,
            )
            return ConnectionResult(success=False, message=msg)
        except Exception as exc:
            logger.warning(
                "Connection test failed: host=%s, database=%s, error=%s",
                host,
                database,
                exc,
            )
            return ConnectionResult(success=False, message=str(exc) or repr(exc))

    # ── Schema introspection — fetch helpers ──────────────────────────────────

    async def _fetch_schemas(self, conn: object) -> list:
        return await conn.fetch(
            """
            SELECT schema_name
            FROM information_schema.schemata
            WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
              AND has_schema_privilege(current_user, schema_name, 'USAGE')
            ORDER BY schema_name
            """
        )

    async def _fetch_tables(self, conn: object) -> list:
        return await conn.fetch(
            """
            SELECT table_schema, table_name, table_type
            FROM information_schema.tables
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
              AND has_schema_privilege(current_user, table_schema, 'USAGE')
            ORDER BY table_schema, table_name
            """
        )

    async def _fetch_all_relations(self, conn: object) -> set[tuple[str, str]] | None:
        """Every user table, view and foreign table, whatever the role may see.

        ``information_schema`` lists only what the role holds a privilege on, so a
        table it cannot read looks dropped. ``pg_class`` lists every relation, which is
        what tells the two apart. ``None`` when the catalog cannot be read (a hardened
        server can revoke it): the caller then cannot name unreadable tables.
        """
        try:
            rows = await conn.fetch(
                r"""
                SELECT n.nspname AS table_schema, c.relname AS table_name
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
                  AND n.nspname NOT LIKE 'pg\_temp\_%'
                  AND n.nspname NOT LIKE 'pg\_toast\_temp\_%'
                """
            )
        except asyncpg.PostgresError as exc:
            logger.warning("Cannot list unreadable tables: pg_class read failed: %s", exc)
            return None
        return {(row["table_schema"], row["table_name"]) for row in rows}

    async def _fetch_columns(self, conn: object) -> list:
        return await conn.fetch(
            """
            SELECT table_schema, table_name, column_name, data_type, udt_name,
                   is_nullable, column_default, ordinal_position
            FROM information_schema.columns
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
              AND has_schema_privilege(current_user, table_schema, 'USAGE')
            ORDER BY table_schema, table_name, ordinal_position
            """
        )

    async def _fetch_primary_keys(self, conn: object) -> set[tuple[str, str, str]]:
        rows = await conn.fetch(
            """
            SELECT tc.table_schema, tc.table_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
              AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND has_schema_privilege(current_user, tc.table_schema, 'USAGE')
            """
        )
        return {(row["table_schema"], row["table_name"], row["column_name"]) for row in rows}

    async def _fetch_foreign_keys(self, conn: object) -> list[RelationshipInfo]:
        rows = await conn.fetch(
            """
            SELECT
                kcu.table_schema  AS from_schema,
                kcu.table_name    AS from_table,
                kcu.column_name   AS from_column,
                ccu.table_schema  AS to_schema,
                ccu.table_name    AS to_table,
                ccu.column_name   AS to_column
            FROM information_schema.key_column_usage kcu
            JOIN information_schema.referential_constraints rc
              ON kcu.constraint_name   = rc.constraint_name
              AND kcu.constraint_schema = rc.constraint_schema
            JOIN information_schema.constraint_column_usage ccu
              ON rc.unique_constraint_name   = ccu.constraint_name
              AND rc.unique_constraint_schema = ccu.constraint_schema
            WHERE has_schema_privilege(current_user, kcu.table_schema, 'USAGE')
              AND has_schema_privilege(current_user, ccu.table_schema, 'USAGE')
            """
        )
        return [
            RelationshipInfo(
                from_schema=row["from_schema"],
                from_table=row["from_table"],
                from_column=row["from_column"],
                to_schema=row["to_schema"],
                to_table=row["to_table"],
                to_column=row["to_column"],
            )
            for row in rows
        ]

    async def _fetch_comments(self, conn: object) -> dict[tuple[str, str, str | None], str]:
        rows = await conn.fetch(
            """
            SELECT
                n.nspname                                   AS schema_name,
                c.relname                                   AS table_name,
                col.attname                                 AS column_name,
                d.description
            FROM pg_catalog.pg_description d
            JOIN pg_catalog.pg_class c       ON d.objoid = c.oid
            JOIN pg_catalog.pg_namespace n   ON c.relnamespace = n.oid
            LEFT JOIN pg_catalog.pg_attribute col
              ON d.objoid = col.attrelid AND d.objsubid = col.attnum
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND d.description IS NOT NULL
            """
        )
        return {
            (row["schema_name"], row["table_name"], row["column_name"]): row["description"]
            for row in rows
        }

    async def _fetch_row_counts(self, conn: object) -> dict[tuple[str, str], int]:
        rows = await conn.fetch("SELECT schemaname, relname, n_live_tup FROM pg_stat_user_tables")
        return {(row["schemaname"], row["relname"]): row["n_live_tup"] for row in rows}

    async def _fetch_sample_values(
        self, conn: object, schema_names: list[str]
    ) -> dict[tuple[str, str, str], list[str]]:
        if not schema_names:
            return {}
        rows = await conn.fetch(
            """
            SELECT schemaname, tablename, attname, most_common_vals::text
            FROM pg_stats
            WHERE schemaname = ANY($1::text[])
              AND most_common_vals IS NOT NULL
            """,
            schema_names,
        )
        return {
            (row["schemaname"], row["tablename"], row["attname"]): self._parse_pg_text_array(
                row["most_common_vals"]
            )
            for row in rows
        }

    # ── Schema introspection — coordinator ────────────────────────────────────

    async def introspect(self, privacy: PrivacySettings | None = None) -> DataSourceSchema:
        """Discover schemas, tables, columns, relationships respecting privacy."""
        if self._pool is None:
            raise RuntimeError("Not connected. Call connect() first.")
        if privacy is None:
            privacy = PrivacySettings()

        async with self._pool.acquire() as conn:
            schema_rows = await self._fetch_schemas(conn)
            table_rows = await self._fetch_tables(conn)
            column_rows = await self._fetch_columns(conn)
            pk_set = await self._fetch_primary_keys(conn)
            relationships = await self._fetch_foreign_keys(conn)
            comments = await self._fetch_comments(conn) if privacy.include_column_comments else {}
            row_counts = await self._fetch_row_counts(conn) if privacy.include_row_counts else {}

            schemas = [
                SchemaInfo(name=row["schema_name"])
                for row in schema_rows
                if row["schema_name"] not in privacy.excluded_schemas
            ]

            sample_values = (
                await self._fetch_sample_values(conn, [s.name for s in schemas])
                if privacy.include_sample_values
                else {}
            )
            all_relations = await self._fetch_all_relations(conn)

        # What exists but the role cannot see. An excluded table is left out: the policy
        # already hides it, and its name has no business in the schema cache.
        visible_relations = {(row["table_schema"], row["table_name"]) for row in table_rows}
        unreadable_tables = sorted(
            f"{schema}.{table}"
            for schema, table in (all_relations or set()) - visible_relations
            if not privacy.is_table_excluded(schema, table)
        )

        # Build column lookup per (schema, table)
        columns_by_table: dict[tuple[str, str], list[ColumnInfo]] = {}
        for row in column_rows:
            schema = row["table_schema"]
            table = row["table_name"]
            col_name = row["column_name"]

            if privacy.is_table_excluded(schema, table):
                continue
            # Only skip explicitly excluded columns here. Sensitive columns
            # (pattern-matched) are retained so they remain visible to the LLM
            # as [SENSITIVE] in format_schema_for_llm.
            if f"{schema}.{table}.{col_name}" in privacy.excluded_columns:
                continue

            key = (schema, table)
            if key not in columns_by_table:
                columns_by_table[key] = []

            comment = comments.get((schema, table, col_name))
            columns_by_table[key].append(
                ColumnInfo(
                    name=col_name,
                    data_type=row["data_type"],
                    native_type=row["udt_name"],
                    nullable=row["is_nullable"] == "YES",
                    is_primary_key=(schema, table, col_name) in pk_set,
                    description=comment,
                )
            )

        # Build table list
        _table_type_map = {
            "BASE TABLE": "table",
            "VIEW": "view",
            "MATERIALIZED VIEW": "materialized_view",
            "FOREIGN": "external",
        }
        tables: list[TableInfo] = []
        for row in table_rows:
            schema = row["table_schema"]
            table = row["table_name"]

            if privacy.is_table_excluded(schema, table):
                continue

            table_type = _table_type_map.get(row["table_type"], row["table_type"].lower())
            table_comment = comments.get((schema, table, None))
            row_count = row_counts.get((schema, table))

            tables.append(
                TableInfo(
                    catalog=None,
                    schema_name=schema,
                    name=table,
                    table_type=table_type,
                    columns=columns_by_table.get((schema, table), []),
                    row_count_approx=row_count,
                    description=table_comment,
                )
            )

        # Attach sample values — catalog-read from pg_stats, zero table scan.
        if privacy.include_sample_values:
            for table_info in tables:
                for col in table_info.columns:
                    if privacy.is_column_excluded(
                        table_info.schema_name, table_info.name, col.name
                    ) or privacy.is_column_sensitive(col.name):
                        continue
                    key = (table_info.schema_name, table_info.name, col.name)
                    vals = sample_values.get(key, [])
                    if vals:
                        col.sample_values = [str(v) for v in vals[:5]]
                        col.sample_source = "stats"

        return DataSourceSchema(
            source_type="postgresql",
            schemas=schemas,
            tables=tables,
            relationships=relationships,
            unreadable_tables=unreadable_tables,
        )

    # OVERHEAD: catalog-read (pg_stats) with user-data fallback using TABLESAMPLE
    async def get_sample_values(
        self,
        schema: str,
        table: str,
        column: str,
        limit: int = 5,
    ) -> list[str]:
        """Retrieve sample values using pg_stats first (zero table overhead).

        Falls back to TABLESAMPLE SYSTEM(1) only if pg_stats has no data.
        NEVER runs a full SELECT DISTINCT scan.
        """
        if self._pool is None:
            raise RuntimeError("Not connected.")
        pg_stats_query = """
            SELECT most_common_vals::text
            FROM pg_stats
            WHERE schemaname = $1
              AND tablename  = $2
              AND attname    = $3
              AND most_common_vals IS NOT NULL
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(pg_stats_query, schema, table, column)
            if row and row["most_common_vals"]:
                return self._parse_pg_text_array(row["most_common_vals"])[:limit]

            # Fallback: TABLESAMPLE reads ~0.1% of rows (BERNOULLI, row-level sampling).
            # Only reached when autovacuum has not yet populated pg_stats.
            q_col = _quote_pg_ident(column)
            q_schema = _quote_pg_ident(schema)
            q_table = _quote_pg_ident(table)
            sample_query = (
                f"SELECT DISTINCT {q_col} "  # noqa: S608
                f"FROM {q_schema}.{q_table} TABLESAMPLE BERNOULLI(0.1) "
                f"WHERE {q_col} IS NOT NULL "
                f"LIMIT {limit}"
            )
            try:
                rows = await conn.fetch(sample_query)
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

        start = time.monotonic()
        # READ ONLY is enforced by the engine: a write the text validator missed — including
        # SELECT … INTO, nextval()/setval() and CREATE TABLE AS — is refused with SQLSTATE 25006.
        async with self._pool.acquire() as conn, conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL statement_timeout = {timeout * 1000}")
            # The row-filter rewriter reads string literals as standard strings (a backslash
            # is an ordinary character). Pin that here so a server, database or role default
            # of off cannot make the engine read a literal differently from the rewriter.
            await conn.execute("SET LOCAL standard_conforming_strings = on")
            records = await conn.fetch(query)

        elapsed_ms = (time.monotonic() - start) * 1000

        if not records:
            return QueryResult(
                columns=[],
                column_types=[],
                rows=[],
                row_count=0,
                execution_time_ms=elapsed_ms,
            )

        # By position, never by name: an asyncpg Record looked up by name returns the last
        # field of that name, so ``SELECT count(*), count(DISTINCT x)`` (two columns named
        # ``count``) would show the second value twice. keys()/values() keep every field.
        columns = list(records[0].keys())
        column_types = _column_types(records[0])

        truncated = len(records) > max_rows
        rows: list[list[Any]] = [[_serialize(v) for v in r.values()] for r in records[:max_rows]]

        return QueryResult(
            columns=columns,
            column_types=column_types,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            execution_time_ms=elapsed_ms,
        )

    async def explain(self, sql: str, timeout: int) -> None:  # noqa: ASYNC109
        """Plan *sql* inside a read-only transaction under ``statement_timeout``.

        No table row is read, but the planner evaluates constant immutable and stable
        expressions (``1/0`` fails here); volatile functions are not evaluated. Nothing is
        returned. The plan rows asyncpg returns are discarded.
        """
        if self._pool is None:
            raise RuntimeError("Not connected.")
        async with self._pool.acquire() as conn, conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL statement_timeout = {timeout * 1000}")
            await conn.execute("SET LOCAL standard_conforming_strings = on")
            try:
                # fetch runs through the extended query protocol: exactly one statement,
                # unlike execute without arguments (simple protocol, several statements).
                await conn.fetch(f"EXPLAIN {sql}")
            except Exception as exc:
                raise ExplainError(str(exc) or type(exc).__name__) from exc

    async def execute_query_stream(
        self,
        query: str,
        timeout: int = 30,  # noqa: ASYNC109
        batch_size: int = 50,
        max_rows: int = 1000,
    ) -> AsyncGenerator[QueryResult, None]:
        """Stream query results using an asyncpg server-side cursor.

        Yields QueryResult batches as the cursor reads rows, so large result sets
        can be streamed to the client without buffering everything in memory first.
        asyncpg cursors require an active transaction — the context managers keep
        the connection and transaction alive across all yield points.
        """
        if self._pool is None:
            raise RuntimeError("Not connected.")

        start = time.monotonic()
        total = 0
        batch: list[list[Any]] = []
        columns: list[str] | None = None
        column_types: list[str] | None = None
        first_batch = True

        # READ ONLY is enforced by the engine: a write the text validator missed — including
        # SELECT … INTO, nextval()/setval() and CREATE TABLE AS — is refused with SQLSTATE 25006.
        async with self._pool.acquire() as conn, conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL statement_timeout = {timeout * 1000}")
            # The row-filter rewriter reads string literals as standard strings (a backslash
            # is an ordinary character). Pin that here so a server, database or role default
            # of off cannot make the engine read a literal differently from the rewriter.
            await conn.execute("SET LOCAL standard_conforming_strings = on")
            async for record in conn.cursor(query, prefetch=batch_size):
                if columns is None:
                    columns = list(record.keys())
                    column_types = _column_types(record)

                if total >= max_rows:
                    # We've hit the limit; yield remaining batch then stop
                    break

                batch.append([_serialize(v) for v in record.values()])  # by position
                total += 1

                if len(batch) >= batch_size:
                    elapsed = (time.monotonic() - start) * 1000 if first_batch else 0.0
                    yield QueryResult(
                        columns=columns,
                        column_types=column_types,
                        rows=batch,
                        row_count=total,
                        truncated=total >= max_rows,
                        execution_time_ms=elapsed,
                    )
                    batch = []
                    first_batch = False

            # Yield any remaining rows in the last partial batch
            if columns and batch:
                elapsed = (time.monotonic() - start) * 1000 if first_batch else 0.0
                yield QueryResult(
                    columns=columns,
                    column_types=column_types,
                    rows=batch,
                    row_count=total,
                    truncated=total >= max_rows,
                    execution_time_ms=elapsed,
                )

    # ── Validation ─────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_pg_text_array(raw: str | list | None) -> list[str]:
        """Parse a PostgreSQL anyarray::text value like '{val1,"val 2",NULL}' into a list.

        pg_stats.most_common_vals is type anyarray — it can be cast to text but not text[].
        This parses the resulting string representation using csv.reader so quoted values
        with commas are handled correctly. NULL elements are dropped.

        Also accepts a Python list (e.g. from asyncpg mocks in tests) and passes it through.
        """
        if raw is None:
            return []
        if isinstance(raw, list):
            return [str(v) for v in raw if str(v).upper() != "NULL"]
        if not raw or raw in ("{}", ""):
            return []
        import csv
        from io import StringIO

        inner = raw.strip("{}")
        reader = csv.reader(StringIO(inner), quotechar='"', skipinitialspace=False)
        return [v for v in next(reader, []) if v.upper() != "NULL"]

    def validate_query(self, query: str) -> ValidationResult:
        """Delegate to the PostgreSQL-specific validator."""
        return self._validator.validate(query)

    def classify_error(self, exc: BaseException) -> ErrorCategory:
        """Map an asyncpg failure via its SQLSTATE (PostgreSQL docs, Appendix A "Error Codes")."""
        return classify_chain(exc, self._classify_one)

    @staticmethod
    def _classify_one(err: BaseException) -> ErrorCategory | None:
        # Restricted to asyncpg's own hierarchy: an unrelated exception can carry a
        # same-named `.sqlstate` attribute and must not be read as a database verdict.
        if not isinstance(err, asyncpg.PostgresError):
            return None
        state = getattr(err, "sqlstate", None)
        if not state:
            return None
        category = category_for_sqlstate(state)
        if category in NAME_ECHOING_CATEGORIES and _may_quote_a_value(err, category):
            return ErrorCategory.DATA
        if state.upper() == "42883":
            return refine_undefined_function(str(err))
        return category

    def get_system_prompt_additions(self) -> str:
        """Return PostgreSQL-specific instructions for the LLM."""
        return """\
You are querying a PostgreSQL database. Use PostgreSQL-specific syntax:
- Use double quotes for identifiers with special characters or reserved words
- Use schema.table notation (e.g., public.customers)
- Functions: DATE_TRUNC(), COALESCE(), ARRAY_AGG(), STRING_AGG(), NOW(), EXTRACT(), TO_CHAR()
- Window functions: ROW_NUMBER(), RANK(), LAG(), LEAD()
- Use CTEs (WITH clauses) for complex queries
- Use ILIKE for case-insensitive matching
- Date intervals: INTERVAL '30 days', CURRENT_DATE, CURRENT_TIMESTAMP
- Type casting: column::TYPE or CAST(column AS TYPE)"""

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
                    "default": 5432,
                    "placeholder": "5432",
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
                    "name": "ssl_mode",
                    "type": "select",
                    "label": "SSL Mode",
                    "required": False,
                    "default": "prefer",
                    "options": [
                        "disable",
                        "allow",
                        "prefer",
                        "require",
                        "verify-ca",
                        "verify-full",
                    ],
                },
            ]
        }


# ── Helpers ────────────────────────────────────────────────────────────────────


# PostgreSQL reports a stored value it failed to look up or parse under the same SQLSTATEs
# as a mistake in the query: ``ssn::regclass`` -> 42P01 ``relation "123456789" does not
# exist``. A name-quoting category (``NAME_ECHOING_CATEGORIES``) keeps the engine's text
# for the user and lists matching names for the LLM, so such an error must read as DATA.
# What tells them apart, measured on PostgreSQL 16 (asyncpg 0.30: ``.position`` is the
# server's 'P' field, a 1-based offset as a str, or None; ``.context`` is 'W'):
#
#   SQLSTATE  genuine error in the query           pos   value-driven error            pos
#   42703     column "nope" does not exist         8     has_column_privilege('u', col) -
#   42P01     relation "nope" does not exist       15    col::regclass                  -
#   42883     function nofunc(integer) …; operator 8/10  col::regproc, col::regoper     -
#             does not exist: integer + boolean;
#             could not identify an equality op…   17
#   42704     type "nosuchtype" does not exist     11    current_setting(col),          -
#             current_setting('nosuch')            -     col::regconfig/regrole/regtype
#   3F000     nosch.f()                            8     col::regnamespace,             -
#             has_schema_privilege('nosch', …)     -     has_table_privilege(col, …)
#   3D000     has_database_privilege('nodb', …)    -     pg_database_size(col)          -
#   42702     column reference "id" is ambiguous   8     (none found)
#   42725     function amb(integer) is not unique  8     '+'::text::regoper             -
#   42601     syntax error at end of input         22    to_tsquery(col), col::tsquery, -
#                                                        col::jsonpath
#                                                        col::regtype, to_regtype(col)  6 *
#             query_to_xml(col, …)                 -     (internal_position 1)
#   42803     … must appear in the GROUP BY clause 8     (none found)
#   42804     argument of WHERE must be boolean    23    (none found)
#   42846     cannot cast type integer to date     9     (none found)
#   42P18     cannot determine type of empty array 8     (none found)
#   42501     permission denied for table hidden   -     set_config(col, …) when col   -
#             permission denied for schema s       15    names a superuser-only setting;
#             (direct reference); for function     -     col::regclass when col names a
#                                                        schema the role cannot use
#
# * regtype input runs the SQL parser on the stored text: the position points into the
#   value, and the server adds the context line 'invalid type name "<value>"'.
#
# Every genuine error found has a position and no context, except permission errors (most
# have no position, so 42501/28xxx are exempt from the position test) and a few lookups of a
# literal the model wrote (current_setting('nosuch'), has_schema_privilege('nosch', …)),
# which read as DATA: the safe direction. So a name-quoting SQLSTATE is DATA when the error
# has no position (PERMISSION excepted), or when it has an error context or an internal
# query (the failing text came from a function or the data, not the model's SQL).
# Residual: a permission error caused by a stored value that names an existing schema or a
# superuser-only setting keeps PERMISSION; it can only echo such a name.
def _may_quote_a_value(err: BaseException, category: ErrorCategory) -> bool:
    """Whether a PostgreSQL error under a name-quoting *category* may quote a stored value."""
    if getattr(err, "context", None) or getattr(err, "internal_query", None):
        return True
    if getattr(err, "internal_position", None):
        return True
    if category is ErrorCategory.PERMISSION:
        return False
    return not getattr(err, "position", None)
