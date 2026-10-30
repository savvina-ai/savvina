# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for the MySQL / MariaDB data source adapter."""

import asyncio
from contextlib import asynccontextmanager
import re
from unittest.mock import AsyncMock, MagicMock, patch

import aiomysql
import pytest

from app.datasources.adapters.mysql import (
    _WRITE_PRIVILEGES,
    MySQLDataSource,
    _Cursor,
    _DictCursor,
    _is_enum_or_set,
    _parse_enum_values,
    _parse_grant_line,
    _serialize,
    _SSCursor,
)
from app.datasources.adapters.postgresql import PostgreSQLDataSource
from app.datasources.errors import ExplainError
from app.datasources.models import (
    PrivacySettings,
    PrivilegeGrant,
    TablePrivilegeFacts,
    grant_schema_name,
)

# What ALL PRIVILEGES below the global level reports as writes: the write privileges valid at
# that level (FILE and CREATE TABLESPACE are global only), sorted.
SCHEMA_ALL_WRITES = [
    "ALTER",
    "ALTER ROUTINE",
    "CREATE",
    "CREATE ROUTINE",
    "CREATE TEMPORARY TABLES",
    "CREATE VIEW",
    "DELETE",
    "DROP",
    "EVENT",
    "INDEX",
    "INSERT",
    "LOCK TABLES",
    "REFERENCES",
    "TRIGGER",
    "UPDATE",
]
TABLE_ALL_WRITES = [
    "ALTER",
    "CREATE",
    "CREATE VIEW",
    "DELETE",
    "DROP",
    "INDEX",
    "INSERT",
    "REFERENCES",
    "TRIGGER",
    "UPDATE",
]

# ── aiomysql mock helpers ─────────────────────────────────────────────────────


class MockCursor:
    """Lightweight stand-in for an aiomysql cursor."""

    def __init__(self, rows: list | None = None, description: tuple | None = None):
        self._rows = rows or []
        self.description = description
        self.execute = AsyncMock()
        self.fetchall = AsyncMock(return_value=self._rows)
        self.fetchone = AsyncMock(return_value=self._rows[0] if self._rows else None)

    async def __aenter__(self) -> "MockCursor":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


class MockMySQLConn:
    """Lightweight stand-in for an aiomysql connection."""

    def __init__(self, cursor: MockCursor):
        self._cursor = cursor
        self.close = MagicMock()

    def cursor(self, cursor_class: type | None = None) -> MockCursor:
        return self._cursor


class MockMySQLPool:
    """Lightweight stand-in for an aiomysql pool."""

    def __init__(self, conn: MockMySQLConn):
        self._conn = conn
        self.close = MagicMock()
        self.wait_closed = AsyncMock()

    @asynccontextmanager
    async def acquire(self):  # type: ignore[override]
        yield self._conn


def _make_adapter(version: str = "8.0.33") -> tuple[MySQLDataSource, MockMySQLConn]:
    """Return (adapter, conn) with a mock pool already injected."""
    adapter = MySQLDataSource()
    adapter._version = version
    adapter._is_mariadb = "MariaDB" in version
    cursor = MockCursor(rows=[("1",)])
    conn = MockMySQLConn(cursor)
    adapter._pool = MockMySQLPool(conn)  # type: ignore[assignment]
    return adapter, conn


# Privacy shorthand: disables optional metadata to minimise side_effect list
MINIMAL_PRIVACY = PrivacySettings(
    include_sample_values=False,
    include_column_comments=False,
    include_row_counts=False,
)


# ── Helper unit tests ─────────────────────────────────────────────────────────


class TestHelpers:
    def test_is_enum_or_set_enum(self) -> None:
        assert _is_enum_or_set("enum('a','b')") is True

    def test_is_enum_or_set_set(self) -> None:
        assert _is_enum_or_set("set('x','y','z')") is True

    def test_is_enum_or_set_varchar(self) -> None:
        assert _is_enum_or_set("varchar(255)") is False

    def test_parse_enum_values_basic(self) -> None:
        assert _parse_enum_values("enum('active','inactive','pending')") == [
            "active",
            "inactive",
            "pending",
        ]

    def test_parse_enum_values_set(self) -> None:
        assert _parse_enum_values("set('read','write','admin')") == [
            "read",
            "write",
            "admin",
        ]

    def test_parse_enum_values_empty_input(self) -> None:
        assert _parse_enum_values("varchar(10)") == []

    def test_serialize_primitives(self) -> None:
        assert _serialize(42) == 42
        assert _serialize(3.14) == 3.14
        assert _serialize("hello") == "hello"
        assert _serialize(True) is True
        assert _serialize(None) is None

    def test_serialize_bytes_returns_none(self) -> None:
        assert _serialize(b"\x00\xff") is None

    def test_serialize_fallback_to_str(self) -> None:
        from decimal import Decimal

        assert _serialize(Decimal("9.99")) == "9.99"


# ── Config schema ─────────────────────────────────────────────────────────────


class TestConfigSchema:
    def test_has_required_fields(self) -> None:
        schema = MySQLDataSource.get_config_schema()
        field_names = {f["name"] for f in schema["fields"]}
        assert {"host", "port", "database", "username", "password", "ssl"} == field_names

    def test_port_default_is_3306(self) -> None:
        schema = MySQLDataSource.get_config_schema()
        port = next(f for f in schema["fields"] if f["name"] == "port")
        assert port["default"] == 3306

    def test_password_type_is_password(self) -> None:
        schema = MySQLDataSource.get_config_schema()
        pw = next(f for f in schema["fields"] if f["name"] == "password")
        assert pw["type"] == "password"


# ── Validator ─────────────────────────────────────────────────────────────────


class TestMySQLValidator:
    def setup_method(self) -> None:
        self.adapter = MySQLDataSource()

    def test_valid_select(self) -> None:
        result = self.adapter.validate_query("SELECT id, name FROM users")
        assert result.is_valid

    def test_blocks_sleep(self) -> None:
        result = self.adapter.validate_query("SELECT SLEEP(5)")
        assert not result.is_valid
        assert "SLEEP" in result.error_message  # type: ignore[operator]

    def test_blocks_benchmark(self) -> None:
        result = self.adapter.validate_query("SELECT BENCHMARK(1000000, SHA1('x'))")
        assert not result.is_valid
        assert "BENCHMARK" in result.error_message  # type: ignore[operator]

    def test_blocks_load_data(self) -> None:
        result = self.adapter.validate_query("LOAD DATA INFILE '/etc/passwd' INTO TABLE t")
        assert not result.is_valid

    def test_blocks_set_global(self) -> None:
        result = self.adapter.validate_query("SELECT 1; SET GLOBAL max_connections=10")
        assert not result.is_valid

    @pytest.mark.parametrize(
        "query",
        [
            # A value assigned to a user variable comes back under another column, unmasked.
            "SELECT id, @l AS v FROM customers WHERE id = 2 AND (@l := email) IS NOT NULL",
            "SELECT @l AS v FROM (SELECT @l:=email AS x FROM customers) t",
        ],
    )
    def test_blocks_variable_assignment(self, query: str) -> None:
        result = self.adapter.validate_query(query)
        assert not result.is_valid
        assert ":=" in (result.error_message or "")

    def test_allows_reading_a_variable(self) -> None:
        assert self.adapter.validate_query("SELECT @@version").is_valid

    def test_blocks_into_outfile(self) -> None:
        result = self.adapter.validate_query("SELECT * FROM users INTO OUTFILE '/tmp/dump'")
        assert not result.is_valid

    def test_blocks_flush(self) -> None:
        result = self.adapter.validate_query("SELECT 1; FLUSH TABLES")
        assert not result.is_valid

    def test_blocks_load_file_function(self) -> None:
        result = self.adapter.validate_query("SELECT LOAD_FILE('/etc/passwd')")
        assert not result.is_valid

    def test_adds_limit_when_missing(self) -> None:
        result = self.adapter.validate_query("SELECT id FROM users")
        assert result.is_valid
        assert "LIMIT" in (result.sanitized_query or "").upper()

    def test_does_not_duplicate_limit(self) -> None:
        result = self.adapter.validate_query("SELECT id FROM users LIMIT 10")
        assert result.is_valid
        assert (result.sanitized_query or "").upper().count("LIMIT") == 1

    def test_blocks_get_lock(self) -> None:
        result = self.adapter.validate_query("SELECT GET_LOCK('name', 10)")
        assert not result.is_valid

    def test_blocks_release_lock(self) -> None:
        result = self.adapter.validate_query("SELECT RELEASE_LOCK('name')")
        assert not result.is_valid

    def test_blocks_master_pos_wait(self) -> None:
        result = self.adapter.validate_query("SELECT MASTER_POS_WAIT('binlog.0001', 100)")
        assert not result.is_valid

    def test_blocks_source_pos_wait(self) -> None:
        result = self.adapter.validate_query("SELECT SOURCE_POS_WAIT('binlog.0001', 100)")
        assert not result.is_valid

    def test_blocks_set_session(self) -> None:
        result = self.adapter.validate_query("SET SESSION sql_mode = 'STRICT_ALL_TABLES'")
        assert not result.is_valid

    @pytest.mark.parametrize(
        "query",
        [
            # MariaDB: a per-statement variable, here lifting the statement time limit.
            "SET STATEMENT max_statement_time=0 FOR SELECT 1",
            # MariaDB: a per-statement sql_mode the adapter never read at connect.
            "SET STATEMENT sql_mode='ANSI_QUOTES' FOR SELECT 1",
            "set   statement sql_mode='NO_BACKSLASH_ESCAPES' for select 1",
            # MySQL 8: the optimizer hint that sets a variable for this statement.
            "SELECT /*+ SET_VAR(sql_mode='') */ 1",
            "SELECT /*+ SET_VAR (sql_mode='ANSI_QUOTES') */ 1",
            # A comment between the words is whitespace to the server.
            "SET/**/STATEMENT max_statement_time=0 FOR SELECT 1",
            "SET/* a\nb */STATEMENT max_statement_time=0 FOR SELECT 1",
            "SET -- x\nSTATEMENT max_statement_time=0 FOR SELECT 1",
            "SET#x\nSTATEMENT max_statement_time=0 FOR SELECT 1",
            "SET /**/ -- x\n #y\n STATEMENT max_statement_time=0 FOR SELECT 1",
            "SET/**/STATEMENT sql_mode='NO_BACKSLASH_ESCAPES' FOR "
            "SELECT 'a\\' AS k, email AS x FROM users -- '",
            "/* lead */ SET STATEMENT max_statement_time=0 FOR SELECT 1",
            # An executable comment: the server runs its content.
            "/*!SET*/ STATEMENT max_statement_time=0 FOR SELECT 1",
            "/*!100000 SET*/ STATEMENT max_statement_time=0 FOR SELECT 1",
            "/*M!100000 SET STATEMENT max_statement_time=0 FOR*/ SELECT 1",
            "SELECT /*+ SET_VAR/**/(sql_mode='') */ 1",
            "SELECT /*+ SET_VAR -- x\n(sql_mode='') */ 1",
            "SELECT /*+ SET_VAR#x\n(sql_mode='') */ 1",
            "SELECT /*!SET_VAR*/(sql_mode='') 1",
            # A plain SET whose value is a subquery mentions SELECT and so passed the type check.
            "SET sql_mode=(SELECT 'ANSI_QUOTES')",
            "/* c */ SET @a=(SELECT 1)",
            "-- c\nSET max_statement_time=(SELECT 0)",
            # A # comment with no space after it, which sqlparse does not read as a comment.
            "#x\nSET sql_mode=(SELECT 1)",
            # A SET inside an executable comment: the server runs it.
            "/*!SET*/ sql_mode = (SELECT 'ANSI_QUOTES')",
            "/*!40101 SET*/ sql_mode = (SELECT 'ANSI_QUOTES')",
            "/*!SET sql_mode=(SELECT 1)*/",
            "/*M!SET*/ max_statement_time=(SELECT 0)",
            "set sql_mode=(select 1)",
        ],
    )
    def test_blocks_per_statement_variables(self, query: str) -> None:
        result = self.adapter.validate_query(query)
        assert not result.is_valid

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT 1",
            "SELECT asset statement FROM t",
            "SELECT offset_var, reset_var FROM settings",
            "SELECT asset FROM t WHERE label = 'set'",
            "SELECT id FROM t WHERE note = 'set it' -- set\n",
            "SELECT /* set */ id FROM t",
            "WITH s AS (SELECT 1 AS a) SELECT a FROM s",
            "(SELECT 1) UNION ALL (SELECT 2)",
            "SELECT /*! STRAIGHT_JOIN */ a FROM t",
            "SELECT /*+ MAX_EXECUTION_TIME(1000) */ a FROM t",
            "SELECT settings FROM t",
            "SELECT a FROM t WHERE FIND_IN_SET('a', b)",
        ],
    )
    def test_plain_selects_stay_valid(self, query: str) -> None:
        assert self.adapter.validate_query(query).is_valid

    @pytest.mark.parametrize(
        "template",
        [
            "SELECT 1 FROM t WHERE a = 'SET {gaps}'",
            "SELECT 1 FROM t WHERE a = 'SET_VAR {gaps}'",
            "{gaps}SELECT 1",
        ],
        ids=["set", "set_var", "leading"],
    )
    @pytest.mark.parametrize("gap", ["/*/", "-- \n", "*/", "# \n", "/**/ "])
    def test_a_long_run_of_comment_gaps_validates_quickly(self, template: str, gap: str) -> None:
        # Each gap form is matched one way only (atomic group, possessive quantifier), so a
        # run of ambiguous pieces cannot make the pattern scan backtrack exponentially.
        import time

        query = template.format(gaps=gap * 5000)
        start = time.monotonic()
        self.adapter.validate_query(query)
        assert time.monotonic() - start < 1.0

    def test_multi_statement_blocked(self) -> None:
        """Stacked queries via semicolon are rejected even in MySQL context."""
        result = self.adapter.validate_query("SELECT id FROM users; DROP TABLE users")
        assert not result.is_valid

    def test_error_message_names_blocked_function(self) -> None:
        result = self.adapter.validate_query("SELECT SLEEP(5)")
        assert not result.is_valid
        assert "SLEEP" in (result.error_message or "")


# ── connect / disconnect ──────────────────────────────────────────────────────


class TestConnect:
    @staticmethod
    def _pool_with(*rows: tuple):
        """A pool whose one cursor answers the connect's queries with *rows* in order."""
        mock_pool = MagicMock()
        mock_pool.wait_closed = AsyncMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.__aenter__ = AsyncMock(return_value=mock_cur)
        mock_cur.__aexit__ = AsyncMock(return_value=None)
        mock_cur.execute = AsyncMock()
        mock_cur.fetchone = AsyncMock(side_effect=list(rows))
        mock_conn.cursor = MagicMock(return_value=mock_cur)

        @asynccontextmanager
        async def _acquire():
            yield mock_conn

        mock_pool.acquire = _acquire
        return mock_pool, mock_cur

    @pytest.mark.parametrize(
        ("sql_mode", "expected"),
        [
            ("STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION", True),
            ("ANSI_QUOTES,STRICT_TRANS_TABLES", False),
            (
                "REAL_AS_FLOAT,PIPES_AS_CONCAT,ANSI_QUOTES,IGNORE_SPACE,ONLY_FULL_GROUP_BY,ANSI",
                False,
            ),
            ("ansi_quotes", False),
            ("", True),
            (None, True),
            # Some servers and drivers return the variable as bytes.
            (b"ANSI_QUOTES,STRICT_TRANS_TABLES", False),
            (b"STRICT_TRANS_TABLES", True),
        ],
    )
    async def test_connect_reads_ansi_quotes_from_sql_mode(self, sql_mode, expected) -> None:
        pool, cur = self._pool_with(("8.0.33",), (sql_mode,))
        with patch("aiomysql.create_pool", AsyncMock(return_value=pool)):
            adapter = MySQLDataSource()
            result = await adapter.connect(
                {"host": "localhost", "username": "u", "password": "p", "database": "db"}
            )
        assert result.success
        assert adapter.double_quote_is_string_literal is expected
        assert cur.execute.await_args_list[1].args == ("SELECT @@sql_mode",)
        # The class default is untouched: a fresh adapter reads MySQL's default again.
        assert MySQLDataSource.double_quote_is_string_literal is True
        assert MySQLDataSource().double_quote_is_string_literal is True

    @pytest.mark.parametrize(
        ("sql_mode", "expected"),
        [
            ("STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION", True),
            ("NO_BACKSLASH_ESCAPES", False),
            ("STRICT_TRANS_TABLES,NO_BACKSLASH_ESCAPES,ONLY_FULL_GROUP_BY", False),
            (
                "REAL_AS_FLOAT,PIPES_AS_CONCAT,ANSI_QUOTES,IGNORE_SPACE,ONLY_FULL_GROUP_BY,ANSI,"
                "NO_BACKSLASH_ESCAPES",
                False,
            ),
            ("no_backslash_escapes", False),
            (b"STRICT_TRANS_TABLES,NO_BACKSLASH_ESCAPES", False),
            (b"STRICT_TRANS_TABLES", True),
            ("", True),
            (None, True),
        ],
    )
    async def test_connect_reads_no_backslash_escapes_from_sql_mode(
        self, sql_mode, expected
    ) -> None:
        pool, _ = self._pool_with(("8.0.33",), (sql_mode,))
        with patch("aiomysql.create_pool", AsyncMock(return_value=pool)):
            adapter = MySQLDataSource()
            result = await adapter.connect(
                {"host": "localhost", "username": "u", "password": "p", "database": "db"}
            )
        assert result.success
        assert adapter.backslash_is_escape is expected
        assert MySQLDataSource.backslash_is_escape is True
        assert MySQLDataSource().backslash_is_escape is True

    async def test_connect_success(self) -> None:
        mock_pool = MagicMock()
        mock_pool.wait_closed = AsyncMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.__aenter__ = AsyncMock(return_value=mock_cur)
        mock_cur.__aexit__ = AsyncMock(return_value=None)
        mock_cur.execute = AsyncMock()
        mock_cur.fetchone = AsyncMock(return_value=("8.0.33",))
        mock_conn.cursor = MagicMock(return_value=mock_cur)

        @asynccontextmanager
        async def _acquire():
            yield mock_conn

        mock_pool.acquire = _acquire

        with patch("aiomysql.create_pool", AsyncMock(return_value=mock_pool)):
            adapter = MySQLDataSource()
            result = await adapter.connect(
                {"host": "localhost", "username": "u", "password": "p", "database": "db"}
            )

        assert result.success
        assert result.server_version == "8.0.33"
        assert not adapter._is_mariadb

    async def test_the_detected_mode_survives_disconnect(self) -> None:
        # Masking after disconnect still reads the flag the server reported.
        pool, _ = self._pool_with(("8.0.33",), ("ANSI_QUOTES",))
        with patch("aiomysql.create_pool", AsyncMock(return_value=pool)):
            adapter = MySQLDataSource()
            await adapter.connect(
                {"host": "localhost", "username": "u", "password": "p", "database": "db"}
            )
        await adapter.disconnect()
        assert adapter.double_quote_is_string_literal is False

    async def test_a_failed_connect_keeps_the_default(self) -> None:
        with patch("aiomysql.create_pool", AsyncMock(side_effect=Exception("refused"))):
            adapter = MySQLDataSource()
            result = await adapter.connect(
                {"host": "bad", "username": "u", "password": "p", "database": "db"}
            )
        assert not result.success
        assert adapter.double_quote_is_string_literal is True
        assert adapter.backslash_is_escape is True

    async def test_connect_failure_returns_false(self) -> None:
        with patch("aiomysql.create_pool", AsyncMock(side_effect=Exception("refused"))):
            adapter = MySQLDataSource()
            result = await adapter.connect(
                {"host": "bad", "username": "u", "password": "p", "database": "db"}
            )
        assert not result.success
        assert "refused" in result.message

    async def test_connect_detects_mariadb(self) -> None:
        mock_pool = MagicMock()
        mock_pool.wait_closed = AsyncMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.__aenter__ = AsyncMock(return_value=mock_cur)
        mock_cur.__aexit__ = AsyncMock(return_value=None)
        mock_cur.execute = AsyncMock()
        mock_cur.fetchone = AsyncMock(return_value=("10.11.2-MariaDB",))
        mock_conn.cursor = MagicMock(return_value=mock_cur)

        @asynccontextmanager
        async def _acquire():
            yield mock_conn

        mock_pool.acquire = _acquire

        with patch("aiomysql.create_pool", AsyncMock(return_value=mock_pool)):
            adapter = MySQLDataSource()
            result = await adapter.connect(
                {"host": "localhost", "username": "u", "password": "p", "database": "db"}
            )

        assert result.success
        assert adapter._is_mariadb

    async def test_pool_cursors_skip_warnings(self) -> None:
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.__aenter__ = AsyncMock(return_value=mock_cur)
        mock_cur.__aexit__ = AsyncMock(return_value=None)
        mock_cur.execute = AsyncMock()
        mock_cur.fetchone = AsyncMock(return_value=("8.0.33",))
        mock_conn.cursor = MagicMock(return_value=mock_cur)

        @asynccontextmanager
        async def _acquire():
            yield mock_conn

        mock_pool.acquire = _acquire
        create_pool = AsyncMock(return_value=mock_pool)

        with patch("aiomysql.create_pool", create_pool):
            await MySQLDataSource().connect(
                {"host": "localhost", "username": "u", "password": "p", "database": "db"}
            )

        assert create_pool.call_args.kwargs["cursorclass"] is _Cursor

    async def test_test_connection_cursor_skips_warnings(self) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.__aenter__ = AsyncMock(return_value=mock_cur)
        mock_cur.__aexit__ = AsyncMock(return_value=None)
        mock_cur.execute = AsyncMock()
        mock_cur.fetchone = AsyncMock(return_value=("8.0.33",))
        mock_conn.cursor = MagicMock(return_value=mock_cur)
        connect = AsyncMock(return_value=mock_conn)

        with patch("aiomysql.connect", connect):
            await MySQLDataSource().test_connection(
                {"host": "localhost", "username": "u", "password": "p", "database": "db"}
            )

        assert connect.call_args.kwargs["cursorclass"] is _Cursor


# ── MySQL warnings never reach warnings.warn ─────────────────────────────────


class TestCursorsSkipWarnings:
    """A MySQL warning quotes stored values (1292 ``Truncated incorrect DOUBLE value:
    'a@b.c'``); aiomysql would pass it to warnings.warn and so to the server log."""

    def test_aiomysql_still_has_the_hook(self) -> None:
        # The override relies on this private method; an aiomysql upgrade that renames it
        # would silently bring the warnings back.
        assert callable(getattr(aiomysql.Cursor, "_show_warnings", None))

    @pytest.mark.parametrize("cls", [_Cursor, _DictCursor, _SSCursor])
    async def test_show_warnings_reads_and_emits_nothing(self, cls, recwarn) -> None:
        conn = MagicMock()
        conn.show_warnings = AsyncMock(
            return_value=[("Warning", 1292, "Truncated incorrect DOUBLE value: 'a@b.c'")]
        )
        cur = cls.__new__(cls)

        await cur._show_warnings(conn)

        conn.show_warnings.assert_not_awaited()
        assert not [w for w in recwarn if "a@b.c" in str(w.message)]

    def test_adapter_opens_no_stock_cursor(self) -> None:
        # Every cursor the adapter opens must skip warnings: a stock aiomysql cursor class
        # passed to conn.cursor() would bring them back for that query.
        import pathlib

        import app.datasources.adapters.mysql as mod

        source = pathlib.Path(mod.__file__).read_text()
        assert not re.search(r"\.cursor\(\s*aiomysql\.", source)


# ── introspect ────────────────────────────────────────────────────────────────


class TestIntrospect:
    async def test_excludes_system_schemas(self) -> None:
        """System schemas must never appear even if the DB returns them."""
        adapter, conn = _make_adapter()

        # Simulate DB returning a system schema alongside a real one
        schema_rows = [{"schema_name": "myapp"}, {"schema_name": "mysql"}]
        table_rows: list = []
        column_rows: list = []
        fk_rows: list = []

        call_results = [schema_rows, table_rows, column_rows, fk_rows]
        conn._cursor.fetchall = AsyncMock(side_effect=call_results)

        result = await adapter.introspect(MINIMAL_PRIVACY)

        schema_names = [s.name for s in result.schemas]
        assert "myapp" in schema_names
        assert "mysql" not in schema_names

    async def test_primary_key_flag(self) -> None:
        adapter, conn = _make_adapter()

        schema_rows = [{"schema_name": "shop"}]
        table_rows = [
            {
                "table_schema": "shop",
                "table_name": "products",
                "table_type": "BASE TABLE",
                "table_rows": None,
                "table_comment": "",
            }
        ]
        column_rows = [
            {
                "table_schema": "shop",
                "table_name": "products",
                "column_name": "id",
                "data_type": "int",
                "column_type": "int",
                "is_nullable": "NO",
                "column_key": "PRI",
                "ordinal_position": 1,
                "column_comment": "",
            },
            {
                "table_schema": "shop",
                "table_name": "products",
                "column_name": "name",
                "data_type": "varchar",
                "column_type": "varchar(255)",
                "is_nullable": "YES",
                "column_key": "",
                "ordinal_position": 2,
                "column_comment": "",
            },
        ]
        fk_rows: list = []

        conn._cursor.fetchall = AsyncMock(
            side_effect=[schema_rows, table_rows, column_rows, fk_rows]
        )

        result = await adapter.introspect(MINIMAL_PRIVACY)

        assert len(result.tables) == 1
        table = result.tables[0]
        id_col = next(c for c in table.columns if c.name == "id")
        name_col = next(c for c in table.columns if c.name == "name")
        assert id_col.is_primary_key is True
        assert name_col.is_primary_key is False

    async def test_enum_sample_values_from_column_type(self) -> None:
        adapter, conn = _make_adapter()

        schema_rows = [{"schema_name": "app"}]
        table_rows = [
            {
                "table_schema": "app",
                "table_name": "orders",
                "table_type": "BASE TABLE",
                "table_rows": None,
                "table_comment": "",
            }
        ]
        column_rows = [
            {
                "table_schema": "app",
                "table_name": "orders",
                "column_name": "status",
                "data_type": "enum",
                "column_type": "enum('pending','shipped','delivered')",
                "is_nullable": "YES",
                "column_key": "",
                "ordinal_position": 1,
                "column_comment": "",
            }
        ]
        fk_rows: list = []

        privacy = PrivacySettings(
            include_sample_values=True,
            include_column_comments=False,
            include_row_counts=False,
        )
        conn._cursor.fetchall = AsyncMock(
            side_effect=[schema_rows, table_rows, column_rows, fk_rows]
        )

        result = await adapter.introspect(privacy)

        col = result.tables[0].columns[0]
        assert col.sample_values == ["pending", "shipped", "delivered"]
        assert col.sample_source == "type"

    async def test_foreign_key_relationship(self) -> None:
        adapter, conn = _make_adapter()

        schema_rows = [{"schema_name": "app"}]
        table_rows = [
            {
                "table_schema": "app",
                "table_name": "orders",
                "table_type": "BASE TABLE",
                "table_rows": None,
                "table_comment": "",
            }
        ]
        column_rows = [
            {
                "table_schema": "app",
                "table_name": "orders",
                "column_name": "user_id",
                "data_type": "int",
                "column_type": "int",
                "is_nullable": "YES",
                "column_key": "",
                "ordinal_position": 1,
                "column_comment": "",
            }
        ]
        fk_rows = [
            {
                "from_schema": "app",
                "from_table": "orders",
                "from_column": "user_id",
                "to_schema": "app",
                "to_table": "users",
                "to_column": "id",
            }
        ]

        conn._cursor.fetchall = AsyncMock(
            side_effect=[schema_rows, table_rows, column_rows, fk_rows]
        )

        result = await adapter.introspect(MINIMAL_PRIVACY)

        assert len(result.relationships) == 1
        rel = result.relationships[0]
        assert rel.from_table == "orders"
        assert rel.to_table == "users"

    @staticmethod
    def _enum_rows(column_name: str) -> list[list]:
        """The four fetchall results introspect() reads, for one ENUM column."""
        schema_rows = [{"schema_name": "app"}]
        table_rows = [
            {
                "table_schema": "app",
                "table_name": "orders",
                "table_type": "BASE TABLE",
                "table_rows": None,
                "table_comment": "",
            }
        ]
        column_rows = [
            {
                "table_schema": "app",
                "table_name": "orders",
                "column_name": column_name,
                "data_type": "enum",
                "column_type": "enum('pending','shipped','delivered')",
                "is_nullable": "YES",
                "column_key": "",
                "ordinal_position": 1,
                "column_comment": "",
            }
        ]
        fk_rows: list = []
        return [schema_rows, table_rows, column_rows, fk_rows]

    async def test_enum_labels_withheld_when_sample_values_are_off(self) -> None:
        """The member list rides on native_type too; with the toggle off it must not."""
        adapter, conn = _make_adapter()
        conn._cursor.fetchall = AsyncMock(side_effect=self._enum_rows("status"))

        result = await adapter.introspect(MINIMAL_PRIVACY)

        col = result.tables[0].columns[0]
        assert col.sample_values is None
        assert col.sample_source is None
        assert col.native_type == "enum(…)"

    async def test_enum_labels_withheld_for_a_sensitive_column_even_when_on(self) -> None:
        adapter, conn = _make_adapter()
        conn._cursor.fetchall = AsyncMock(side_effect=self._enum_rows("email_status"))
        privacy = PrivacySettings(
            include_sample_values=True,
            include_column_comments=False,
            include_row_counts=False,
        )

        result = await adapter.introspect(privacy)

        col = result.tables[0].columns[0]
        assert col.sample_values is None
        assert col.sample_source is None
        assert col.native_type == "enum(…)"

    async def test_enum_labels_kept_when_allowed(self) -> None:
        adapter, conn = _make_adapter()
        conn._cursor.fetchall = AsyncMock(side_effect=self._enum_rows("status"))
        privacy = PrivacySettings(
            include_sample_values=True,
            include_column_comments=False,
            include_row_counts=False,
        )

        result = await adapter.introspect(privacy)

        col = result.tables[0].columns[0]
        assert col.native_type == "enum('pending','shipped','delivered')"


# ── format_schema_for_llm ─────────────────────────────────────────────────────


class TestFormatSchemaForLLM:
    def test_uses_backtick_quoting(self) -> None:
        from app.datasources.models import ColumnInfo, DataSourceSchema, TableInfo

        schema = DataSourceSchema(
            source_type="mysql",
            tables=[
                TableInfo(
                    catalog=None,
                    schema_name="mydb",
                    name="users",
                    table_type="table",
                    columns=[
                        ColumnInfo(name="id", data_type="int", native_type="int", nullable=False)
                    ],
                )
            ],
        )
        adapter = MySQLDataSource()
        output = adapter.format_schema_for_llm(schema)
        assert "`mydb`" in output
        assert "`users`" in output
        assert "`id`" in output

    def test_primary_key_annotation(self) -> None:
        from app.datasources.models import ColumnInfo, DataSourceSchema, TableInfo

        schema = DataSourceSchema(
            source_type="mysql",
            tables=[
                TableInfo(
                    catalog=None,
                    schema_name="db",
                    name="t",
                    table_type="table",
                    columns=[
                        ColumnInfo(
                            name="id",
                            data_type="int",
                            native_type="int",
                            nullable=False,
                            is_primary_key=True,
                        )
                    ],
                )
            ],
        )
        adapter = MySQLDataSource()
        output = adapter.format_schema_for_llm(schema)
        assert "Primary Key" in output


# ── registration smoke test ───────────────────────────────────────────────────


class TestRegistration:
    def test_mysql_in_available_sources(self) -> None:
        from app.datasources.registry import list_available_sources

        sources = list_available_sources()
        names = [s["source_type"] for s in sources]
        assert "mysql" in names


# ── _quote_identifier ─────────────────────────────────────────────────────────


class TestQuoteIdentifier:
    def test_wraps_in_backticks(self) -> None:
        adapter = MySQLDataSource()
        assert adapter._quote_identifier("my_table") == "`my_table`"

    def test_wraps_schema_name(self) -> None:
        adapter = MySQLDataSource()
        assert adapter._quote_identifier("mydb") == "`mydb`"

    def test_escapes_embedded_backtick(self) -> None:
        """An identifier containing a backtick must be doubled, not left raw."""
        adapter = MySQLDataSource()
        assert adapter._quote_identifier("tab`le") == "`tab``le`"

    def test_escapes_multiple_backticks(self) -> None:
        adapter = MySQLDataSource()
        assert adapter._quote_identifier("a`b`c") == "`a``b``c`"

    def test_plain_identifier_unchanged_content(self) -> None:
        adapter = MySQLDataSource()
        result = adapter._quote_identifier("order_items")
        assert result == "`order_items`"


# ── execute_query_stream ──────────────────────────────────────────────────────


class MockSSCursor:
    """Lightweight stand-in for an aiomysql SSCursor.

    Accepts a list of rows and returns them in pages via fetchmany().
    """

    def __init__(
        self,
        rows: list,
        description: tuple | None = None,
    ) -> None:
        self._rows = list(rows)
        self._pos = 0
        self.description = description or (("col1", None, None, None, None, None, None),)
        self.execute = AsyncMock()

    async def fetchmany(self, size: int) -> list:
        chunk = self._rows[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    async def __aenter__(self) -> "MockSSCursor":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


class MockMySQLConnWithSSCursor:
    """Connection mock that returns different cursor types on request."""

    def __init__(
        self,
        ss_cursor: MockSSCursor,
        plain_cursor: MockCursor | None = None,
    ) -> None:
        self._ss_cursor = ss_cursor
        self._plain_cursor = plain_cursor or MockCursor()
        self.close = MagicMock()

    def cursor(self, cursor_class: type | None = None) -> MockSSCursor | MockCursor:
        import aiomysql

        if cursor_class is not None and issubclass(cursor_class, aiomysql.SSCursor):
            return self._ss_cursor
        return self._plain_cursor


def _make_stream_adapter(
    rows: list,
    description: tuple | None = None,
    version: str = "8.0.33",
) -> MySQLDataSource:
    """Return a MySQLDataSource with pool/cursor mocks set up for streaming."""
    adapter = MySQLDataSource()
    adapter._version = version
    adapter._is_mariadb = "MariaDB" in version

    ss_cursor = MockSSCursor(rows=rows, description=description)
    plain_cursor = MockCursor()
    conn = MockMySQLConnWithSSCursor(ss_cursor=ss_cursor, plain_cursor=plain_cursor)
    adapter._pool = MockMySQLPool(conn)  # type: ignore[assignment]
    return adapter


class TestExecuteQueryStream:
    async def test_yields_rows_in_batches(self) -> None:
        """Stream splits rows across multiple QueryResult batches."""
        rows = [(i,) for i in range(10)]
        adapter = _make_stream_adapter(rows=rows)

        batches = []
        async for batch in adapter.execute_query_stream(
            "SELECT id FROM t", batch_size=3, max_rows=10
        ):
            batches.append(batch)

        # 10 rows / batch_size=3 → 3 full + 1 partial = 4 batches
        assert len(batches) == 4
        assert batches[0].rows == [[0], [1], [2]]
        assert batches[-1].rows == [[9]]

    async def test_respects_max_rows(self) -> None:
        """Stream stops at max_rows even if more data is available."""
        rows = [(i,) for i in range(100)]
        adapter = _make_stream_adapter(rows=rows)

        total_rows: list = []
        async for batch in adapter.execute_query_stream(
            "SELECT id FROM t", batch_size=10, max_rows=25
        ):
            total_rows.extend(batch.rows)

        assert len(total_rows) == 25

    async def test_empty_result_yields_nothing(self) -> None:
        """A query returning no rows produces zero batches."""
        adapter = _make_stream_adapter(rows=[])
        # Override SSCursor description to None to simulate empty result
        ss_cursor = MockSSCursor(rows=[], description=None)
        ss_cursor.description = None

        adapter._pool._conn._ss_cursor = ss_cursor  # type: ignore[attr-defined]

        batches = []
        async for batch in adapter.execute_query_stream("SELECT 1 WHERE FALSE"):
            batches.append(batch)

        assert batches == []

    async def test_truncated_flag_set_at_max_rows(self) -> None:
        """truncated=True when the returned row count equals max_rows."""
        rows = [(i,) for i in range(50)]
        adapter = _make_stream_adapter(rows=rows)

        last_batch = None
        async for batch in adapter.execute_query_stream(
            "SELECT id FROM t", batch_size=10, max_rows=20
        ):
            last_batch = batch

        assert last_batch is not None
        assert last_batch.truncated is True
        assert last_batch.row_count == 20

    async def test_not_truncated_when_all_rows_fit(self) -> None:
        """truncated=False when total rows are fewer than max_rows."""
        rows = [(i,) for i in range(5)]
        adapter = _make_stream_adapter(rows=rows)

        last_batch = None
        async for batch in adapter.execute_query_stream(
            "SELECT id FROM t", batch_size=10, max_rows=100
        ):
            last_batch = batch

        assert last_batch is not None
        assert last_batch.truncated is False

    async def test_column_names_present_in_every_batch(self) -> None:
        """Column names are propagated to every yielded batch."""
        desc = (("id", None, None, None, None, None, None),)
        rows = [(i,) for i in range(6)]
        adapter = _make_stream_adapter(rows=rows, description=desc)

        async for batch in adapter.execute_query_stream(
            "SELECT id FROM t", batch_size=2, max_rows=6
        ):
            assert batch.columns == ["id"]

    async def test_execution_time_nonzero_only_on_first_batch(self) -> None:
        """execution_time_ms > 0 on first batch, 0.0 on subsequent batches."""
        rows = [(i,) for i in range(6)]
        adapter = _make_stream_adapter(rows=rows)

        batches = []
        async for batch in adapter.execute_query_stream(
            "SELECT id FROM t", batch_size=2, max_rows=6
        ):
            batches.append(batch)

        assert len(batches) == 3
        assert batches[0].execution_time_ms >= 0.0
        assert batches[1].execution_time_ms == 0.0
        assert batches[2].execution_time_ms == 0.0

    async def test_sets_and_resets_timeout_mysql(self) -> None:
        """MAX_EXECUTION_TIME is SET before query and reset to 0 afterward (MySQL)."""
        rows = [(1,)]
        adapter = _make_stream_adapter(rows=rows, version="8.0.33")
        plain_cursor = adapter._pool._conn._plain_cursor  # type: ignore[attr-defined]

        async for _ in adapter.execute_query_stream("SELECT 1", timeout=15):
            pass

        all_calls = plain_cursor.execute.call_args_list
        # SET call: ("SET SESSION MAX_EXECUTION_TIME = %s", (15000,))
        set_call = next(
            (
                c
                for c in all_calls
                if "MAX_EXECUTION_TIME" in c.args[0]
                and c.args[0] != "SET SESSION MAX_EXECUTION_TIME = 0"
            ),
            None,
        )
        assert set_call is not None, "SET MAX_EXECUTION_TIME call not found"
        assert set_call.args[1] == (15_000,), f"Expected (15000,) got {set_call.args[1]}"
        # RESET call: ("SET SESSION MAX_EXECUTION_TIME = 0",)
        reset_calls = [c.args[0] for c in all_calls]
        assert any("MAX_EXECUTION_TIME = 0" in c for c in reset_calls), (
            "reset MAX_EXECUTION_TIME = 0 not found"
        )

    async def test_sets_and_resets_timeout_mariadb(self) -> None:
        """max_statement_time is SET before query and reset to 0 afterward (MariaDB)."""
        rows = [(1,)]
        adapter = _make_stream_adapter(rows=rows, version="10.11.2-MariaDB")
        plain_cursor = adapter._pool._conn._plain_cursor  # type: ignore[attr-defined]

        async for _ in adapter.execute_query_stream("SELECT 1", timeout=5):
            pass

        calls = [c.args[0] for c in plain_cursor.execute.call_args_list]
        assert any("max_statement_time" in c for c in calls)
        assert any(
            c.args == ("SET SESSION max_statement_time = 0",)
            for c in plain_cursor.execute.call_args_list
        )


# ── read-only session ─────────────────────────────────────────────────────────


def _executed_statements(cursor: MockCursor) -> list[str]:
    return [str(call.args[0]) for call in cursor.execute.await_args_list]


class TestReadOnlySession:
    async def test_execute_query_runs_read_only_and_resets(self) -> None:
        adapter, conn = _make_adapter()

        await adapter.execute_query("SELECT 1")

        statements = _executed_statements(conn._cursor)
        assert statements.index("SET SESSION TRANSACTION READ ONLY") < statements.index("SELECT 1")
        assert statements.index("SELECT 1") < statements.index("SET SESSION TRANSACTION READ WRITE")

    async def test_execute_query_resets_even_when_the_query_fails(self) -> None:
        adapter, conn = _make_adapter()

        async def _execute(query: str, *_args: object) -> None:
            if query == "SELECT 1":
                raise RuntimeError("boom")

        conn._cursor.execute = AsyncMock(side_effect=_execute)

        with pytest.raises(RuntimeError):
            await adapter.execute_query("SELECT 1")

        assert "SET SESSION TRANSACTION READ WRITE" in _executed_statements(conn._cursor)

    async def test_stream_runs_read_only_and_resets(self) -> None:
        adapter = _make_stream_adapter(rows=[("a",)])

        async for _batch in adapter.execute_query_stream("SELECT 1"):
            pass

        plain = adapter._pool._conn._plain_cursor  # type: ignore[attr-defined]
        statements = _executed_statements(plain)
        assert "SET SESSION TRANSACTION READ ONLY" in statements
        assert statements[-1] == "SET SESSION TRANSACTION READ WRITE"


class TestExplain:
    async def test_plans_inside_the_read_only_session_and_reads_no_row(self) -> None:
        adapter, conn = _make_adapter()
        await adapter.explain("SELECT 1", timeout=7)
        statements = [c.args[0] for c in conn._cursor.execute.call_args_list]
        assert statements == [
            "SET SESSION TRANSACTION READ ONLY",
            "SET SESSION sql_notes = 0",
            "EXPLAIN SELECT 1",
            "SET SESSION sql_notes = DEFAULT",
            "SET SESSION TRANSACTION READ WRITE",
        ]
        conn._cursor.fetchall.assert_not_called()

    async def test_mariadb_bounds_the_plan_with_max_statement_time(self) -> None:
        adapter, conn = _make_adapter(version="10.11.6-MariaDB")
        await adapter.explain("SELECT 1", timeout=7)
        calls = [c.args for c in conn._cursor.execute.call_args_list]
        assert calls[0] == ("SET SESSION max_statement_time = %s", (7,))
        assert ("EXPLAIN SELECT 1",) in calls
        assert calls[-1] == ("SET SESSION max_statement_time = 0",)

    async def test_engine_error_becomes_explain_error_with_the_cause(self) -> None:
        adapter, conn = _make_adapter()
        cause = RuntimeError("Unknown column 'nosuch' in 'field list'")
        # READ ONLY and sql_notes succeed, the EXPLAIN raises, both resets still run.
        conn._cursor.execute.side_effect = [None, None, cause, None, None]
        with pytest.raises(ExplainError, match="nosuch") as info:
            await adapter.explain("SELECT nosuch FROM t", timeout=7)
        assert info.value.__cause__ is cause
        calls = [c.args for c in conn._cursor.execute.call_args_list]
        assert calls[-2:] == [
            ("SET SESSION sql_notes = DEFAULT",),
            ("SET SESSION TRANSACTION READ WRITE",),
        ]

    async def test_reserved_word_aliases_are_quoted_like_execute_query(self) -> None:
        adapter, conn = _make_adapter()
        await adapter.explain("SELECT 1 AS rank FROM t", timeout=7)
        statements = [c.args[0] for c in conn._cursor.execute.call_args_list]
        assert "EXPLAIN SELECT 1 AS `rank` FROM t" in statements

    async def test_mariadb_engine_error_still_resets_the_session(self) -> None:
        adapter, conn = _make_adapter(version="10.11.6-MariaDB")
        cause = RuntimeError("Unknown column 'nosuch' in 'field list'")
        # max_statement_time, READ ONLY and sql_notes succeed, the EXPLAIN raises, all three
        # resets still run.
        conn._cursor.execute.side_effect = [None, None, None, cause, None, None, None]
        with pytest.raises(ExplainError, match="nosuch"):
            await adapter.explain("SELECT nosuch FROM t", timeout=7)
        calls = [c.args for c in conn._cursor.execute.call_args_list]
        assert calls[-3:] == [
            ("SET SESSION sql_notes = DEFAULT",),
            ("SET SESSION TRANSACTION READ WRITE",),
            ("SET SESSION max_statement_time = 0",),
        ]

    async def test_a_cancelled_plan_closes_the_connection_and_sends_no_reset(self) -> None:
        """The router cancels a plan that outlives its budget (Task 4). aiomysql closes the
        connection when a read is cancelled, so a reset sent afterwards raises
        InterfaceError, which would replace the CancelledError that asyncio.wait_for turns
        into a timeout. No reset is sent; the connection is closed."""
        adapter, conn = _make_adapter(version="10.11.6-MariaDB")
        planning = asyncio.Event()

        async def execute(statement: str, args: object = None) -> None:
            if planning.is_set():
                # The driver already dropped the connection when the read was cancelled.
                raise aiomysql.InterfaceError("Cancelled during execution")
            if statement.startswith("EXPLAIN"):
                planning.set()
                # The server is still planning (waiting on a lock) until the task is cancelled.
                await asyncio.Event().wait()

        conn._cursor.execute = AsyncMock(side_effect=execute)
        task = asyncio.create_task(adapter.explain("SELECT 1", timeout=7))
        await planning.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        statements = [c.args[0] for c in conn._cursor.execute.call_args_list]
        assert statements[-1] == "EXPLAIN SELECT 1"
        conn.close.assert_called_once_with()

    async def test_not_connected_raises(self) -> None:
        adapter = MySQLDataSource()
        with pytest.raises(RuntimeError):
            await adapter.explain("SELECT 1", timeout=7)


class TestParseGrantLine:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("GRANT USAGE ON *.* TO `u`@`%`", (frozenset(), {}, None, None)),
            (
                "GRANT SELECT, INSERT ON `shop`.* TO `u`@`%`",
                (frozenset({"SELECT", "INSERT"}), {}, "shop", None),
            ),
            (
                "GRANT SELECT ON `shop`.`orders` TO `u`@`%`",
                (frozenset({"SELECT"}), {}, "shop", "orders"),
            ),
            (
                "GRANT ALL PRIVILEGES ON *.* TO `u`@`%` WITH GRANT OPTION",
                (frozenset({"ALL PRIVILEGES"}), {}, None, None),
            ),
            (
                "GRANT ALL ON `shop`.* TO `u`@`%`",
                (frozenset({"ALL PRIVILEGES"}), {}, "shop", None),
            ),
            (
                "GRANT SUPER, FILE ON *.* TO `u`@`%`",
                (frozenset({"SUPER", "FILE"}), {}, None, None),
            ),
            (
                "GRANT SELECT (id, email), INSERT (id), UPDATE ON `shop`.`users` TO `u`@`%`",
                (
                    frozenset({"UPDATE"}),
                    {"SELECT": ("id", "email"), "INSERT": ("id",)},
                    "shop",
                    "users",
                ),
            ),
            (
                "GRANT UPDATE (`a b`, c) ON `shop`.`users` TO `u`@`%`",
                (frozenset(), {"UPDATE": ("a b", "c")}, "shop", "users"),
            ),
            (
                "GRANT PROXY ON ''@'' TO `u`@`%` WITH GRANT OPTION",
                (frozenset({"PROXY"}), {}, None, None),
            ),
            # MySQL 8 prints the PROXY scope with empty backticks.
            (
                "GRANT PROXY ON ``@`` TO `u`@`%` WITH GRANT OPTION",
                (frozenset({"PROXY"}), {}, None, None),
            ),
            # A column list holds commas of its own: none of its names is a privilege.
            (
                "GRANT SELECT (`email`, `id`, `name`), INSERT ON `shop`.`t` TO `u`@`%`",
                (frozenset({"INSERT"}), {"SELECT": ("email", "id", "name")}, "shop", "t"),
            ),
            (
                "GRANT SELECT (id, name, email) ON shop.t TO u@'%'",
                (frozenset(), {"SELECT": ("id", "name", "email")}, "shop", "t"),
            ),
            (
                "GRANT INSERT (a), UPDATE (b) ON `shop`.`t` TO `u`@`%`",
                (frozenset(), {"INSERT": ("a",), "UPDATE": ("b",)}, "shop", "t"),
            ),
            # A backtick inside a name is doubled; a dot inside stays inside.
            (
                "GRANT SELECT ON `a.b`.`c``d` TO `u`@`%`",
                (frozenset({"SELECT"}), {}, "a.b", "c`d"),
            ),
            # A space inside a quoted name stays inside: the scope is not cut at it.
            (
                "GRANT ALL ON `my db`.* TO `u`@`%`",
                (frozenset({"ALL PRIVILEGES"}), {}, "my db", None),
            ),
            (
                "GRANT SELECT ON `my db`.`t 1` TO `u`@`%`",
                (frozenset({"SELECT"}), {}, "my db", "t 1"),
            ),
            (
                "GRANT EXECUTE ON FUNCTION `shop`.`f` TO `u`@`%`",
                (frozenset({"EXECUTE"}), {}, "shop", "f"),
            ),
            ("GRANT `r1`@`%`,`r2`@`%` TO `u`@`%`", None),
            # SHOW GRANTS escapes a literal "_" or "%" in a schema-level grant (a pattern); the
            # parser keeps the name as written, so a REVOKE can name the stored grant.
            (
                "GRANT SELECT, INSERT ON `sample\\_delivery`.* TO `u`@`%`",
                (frozenset({"SELECT", "INSERT"}), {}, "sample\\_delivery", None),
            ),
            (
                "GRANT SELECT ON `sample_delivery`.`my_t` TO `u`@`%`",
                (frozenset({"SELECT"}), {}, "sample_delivery", "my_t"),
            ),
            # A table-level grant prints the database name literally: no unescape.
            (
                "GRANT SELECT ON `a\\_b`.`t` TO `u`@`%`",
                (frozenset({"SELECT"}), {}, "a\\_b", "t"),
            ),
            # A comma inside a backticked column name is part of the name; a doubled backtick
            # inside stays inside.
            (
                "GRANT UPDATE (`a,b`, `c`) ON `shop`.`t` TO `u`@`%`",
                (frozenset(), {"UPDATE": ("a,b", "c")}, "shop", "t"),
            ),
            (
                "GRANT UPDATE (`x``,y`, z) ON `shop`.`t` TO `u`@`%`",
                (frozenset(), {"UPDATE": ("x`,y", "z")}, "shop", "t"),
            ),
            # Column names are de-duplicated, in order.
            (
                "GRANT UPDATE (b, a, b) ON `shop`.`t` TO `u`@`%`",
                (frozenset(), {"UPDATE": ("b", "a")}, "shop", "t"),
            ),
            ("GRANT `analyst` TO `u`@`%`", None),
            ("", None),
        ],
    )
    def test_lines(self, line: str, expected) -> None:
        parsed = _parse_grant_line(line)
        assert (None if parsed is None else parsed[:4]) == expected

    @pytest.mark.parametrize(
        ("line", "kind"),
        [
            ("GRANT SELECT ON `shop`.`orders` TO `u`@`%`", "TABLE"),
            ("GRANT SELECT ON TABLE `shop`.`orders` TO `u`@`%`", "TABLE"),
            ("GRANT INSERT ON `shop`.* TO `u`@`%`", "TABLE"),
            ("GRANT EXECUTE ON FUNCTION `shop`.`f` TO `u`@`%`", "FUNCTION"),
            ("GRANT EXECUTE, ALTER ROUTINE ON PROCEDURE `shop`.`p` TO `u`@`%`", "PROCEDURE"),
            ("grant execute on procedure `shop`.`p` to `u`@`%`", "PROCEDURE"),
        ],
    )
    def test_the_object_kind(self, line: str, kind: str) -> None:
        assert _parse_grant_line(line)[4] == kind

    def test_proxy_scope_is_global_but_grants_no_write(self) -> None:
        privileges, _, schema, table, _ = _parse_grant_line("GRANT PROXY ON ''@'' TO `u`@`%`")
        assert (schema, table) == (None, None) and "PROXY" in privileges
        assert not (privileges & _WRITE_PRIVILEGES)


class TestGrantSchemaName:
    @pytest.mark.parametrize(
        ("literal", "expected"),
        [
            ("sample\\_delivery", "sample_delivery"),
            ("a\\%b", "a%b"),
            ("a\\\\b", "a\\b"),
            ("a\\\\_b", "a\\_b"),
            ("plain", "plain"),
        ],
    )
    def test_unescapes_the_pattern_escapes_in_one_pass(self, literal: str, expected: str) -> None:
        assert grant_schema_name(literal) == expected


def _grants_cursor(
    *,
    user: str = "savvina@%",
    grants: list[str],
    tables: list[tuple[str, str]] = (),
    roles: str = "NONE",
    role_grants: dict[str, list[str]] | None = None,
    role_failures: set[str] = frozenset(),
    schemas: list[str] = ("shop",),
    direct_fails: bool = False,
) -> MockCursor:
    """A cursor that answers each statement by what it is: CURRENT_USER() and CURRENT_ROLE()
    through fetchone (``user``, then ``roles``, its raw value); the direct statement (SHOW
    GRANTS FOR the account named explicitly, or its CURRENT_USER() fallback) with ``grants``,
    the explicit form raising OperationalError when ``direct_fails``; a role statement from
    ``role_grants`` keyed by the statement's tail (the ``USING x`` role on MySQL, the ``FOR
    x`` role on MariaDB, or the bare "SHOW GRANTS" fallback), a key in ``role_failures``
    raising OperationalError; the information_schema.tables lookup with ``tables``; the
    schemata lookup with ``schemas``. Any other statement (MariaDB's max_statement_time)
    answers nothing."""
    cursor = MockCursor()
    answers = {k: [(g,) for g in v] for k, v in (role_grants or {}).items()}
    pending: list[list] = [[]]
    original_execute = cursor.execute
    explicit = f"SHOW GRANTS FOR {_account(user)}"

    async def execute(statement: str, params=None):
        if statement in (explicit, "SHOW GRANTS FOR CURRENT_USER()"):
            if direct_fails and statement == explicit:
                raise aiomysql.OperationalError(1142, "SELECT command denied")
            pending[0] = [(g,) for g in grants]
        elif (key := _role_key(statement)) is not None:
            if key in role_failures:
                raise aiomysql.OperationalError(1141, "There is no such grant defined")
            pending[0] = answers.get(key, [])
        elif "information_schema.tables" in statement:
            pending[0] = [tuple(t) for t in tables]
        elif "information_schema.schemata" in statement:
            pending[0] = [(s,) for s in schemas]
        return await original_execute(statement, params)

    async def fetchall():
        answer, pending[0] = pending[0], []
        return answer

    cursor.execute = AsyncMock(side_effect=execute)
    cursor.fetchone = AsyncMock(side_effect=[(user,), (roles,)])
    cursor.fetchall = AsyncMock(side_effect=fetchall)
    return cursor


def _account(user: str) -> str:
    """``u@h`` as SHOW GRANTS FOR names it: split at the last ``@``, each part backticked."""
    name, _, host = user.rpartition("@")
    return f"`{name.replace('`', '``')}`@`{host.replace('`', '``')}`"


def _role_key(statement: str) -> str | None:
    """The role a role statement names (the direct statement is matched before this)."""
    if statement == "SHOW GRANTS":
        return "SHOW GRANTS"
    m = re.match(r"SHOW GRANTS FOR .+? USING (.+)$", statement)
    if m:
        return m.group(1)
    m = re.match(r"SHOW GRANTS FOR (.+)$", statement)
    return m.group(1) if m else None


def _grants_adapter(version: str = "8.0.33", **kwargs) -> tuple[MySQLDataSource, MockMySQLConn]:
    adapter = MySQLDataSource()
    adapter._version = version
    adapter._is_mariadb = "MariaDB" in version
    conn = MockMySQLConn(_grants_cursor(**kwargs))
    adapter._pool = MockMySQLPool(conn)  # type: ignore[assignment]
    return adapter, conn


class TestAuditPrivileges:
    async def test_read_only_account(self) -> None:
        adapter, conn = _grants_adapter(
            grants=[
                "GRANT USAGE ON *.* TO `savvina`@`%`",
                "GRANT SELECT ON `shop`.`orders` TO `savvina`@`%`",
            ],
            tables=[("shop", "orders")],
        )
        facts = await adapter.audit_privileges(
            [("shop", "orders"), ("shop", "customers")], timeout=7
        )
        assert facts.role == "savvina@%"
        assert facts.is_superuser is False
        assert facts.writes == [] and facts.has_super is False
        assert facts.sql_from_string_functions == []
        assert facts.bypasses_rls is False
        assert facts.tables == [
            TablePrivilegeFacts(
                "shop",
                "orders",
                True,
                can_select=True,
                select_grants=[PrivilegeGrant("shop", "orders", ["SELECT"], "savvina@%")],
            ),
            TablePrivilegeFacts("shop", "customers", False),
        ]
        assert facts.tables[0].select_grants == [
            PrivilegeGrant("shop", "orders", ["SELECT"], "savvina@%")
        ]
        statements = [c.args[0] for c in conn._cursor.execute.call_args_list]
        assert statements[:3] == [
            "SELECT CURRENT_USER()",
            "SHOW GRANTS FOR `savvina`@`%`",
            "SELECT CURRENT_ROLE()",
        ]
        assert statements[3].startswith(
            "SELECT table_schema, table_name FROM information_schema.tables"
        )
        assert conn._cursor.execute.call_args_list[3].args[1] == [
            "shop",
            "orders",
            "shop",
            "customers",
        ]
        assert "information_schema.schemata" in statements[-1]

    async def test_global_and_schema_grants_cover_every_table(self) -> None:
        adapter, _ = _grants_adapter(
            grants=[
                "GRANT SELECT, FILE ON *.* TO `savvina`@`%`",
                "GRANT INSERT, UPDATE, DELETE ON `shop`.* TO `savvina`@`%`",
                "GRANT DROP ON `other`.* TO `savvina`@`%`",
            ],
            tables=[("shop", "orders")],
        )
        facts = await adapter.audit_privileges([("shop", "orders")], timeout=7)
        assert facts.is_superuser is False
        assert facts.writes == [
            PrivilegeGrant(None, None, ["FILE"], "savvina@%"),
            PrivilegeGrant("other", None, ["DROP"], "savvina@%"),
            PrivilegeGrant("shop", None, ["DELETE", "INSERT", "UPDATE"], "savvina@%"),
        ]
        assert facts.tables == [
            TablePrivilegeFacts(
                "shop",
                "orders",
                True,
                can_select=True,
                can_write=True,
                select_grants=[PrivilegeGrant(None, None, ["SELECT"], "savvina@%")],
            )
        ]

    async def test_escaped_schema_grant_keeps_the_literal_and_covers_its_tables(self) -> None:
        adapter, _ = _grants_adapter(
            grants=[
                "GRANT USAGE ON *.* TO `savvina`@`%`",
                "GRANT ALL PRIVILEGES ON `sample\\_delivery`.* TO `savvina`@`%`",
            ],
            tables=[("sample_delivery", "orders")],
        )
        facts = await adapter.audit_privileges([("sample_delivery", "orders")], timeout=7)
        assert facts.writes == [
            PrivilegeGrant("sample\\_delivery", None, SCHEMA_ALL_WRITES, "savvina@%")
        ]
        assert facts.tables == [
            TablePrivilegeFacts(
                "sample_delivery",
                "orders",
                True,
                can_select=True,
                can_write=True,
                select_grants=[PrivilegeGrant("sample\\_delivery", None, ["SELECT"], "savvina@%")],
            )
        ]

    async def test_two_grant_literals_matching_one_schema_are_united(self) -> None:
        adapter, _ = _grants_adapter(
            grants=[
                "GRANT SELECT ON `a_b`.* TO `u`@`%`",
                "GRANT INSERT ON `a\\_b`.* TO `u`@`%`",
            ],
            tables=[("a_b", "t")],
        )
        facts = await adapter.audit_privileges([("a_b", "t")], timeout=7)
        assert facts.tables == [
            TablePrivilegeFacts(
                "a_b",
                "t",
                True,
                can_select=True,
                can_write=True,
                select_grants=[PrivilegeGrant("a_b", None, ["SELECT"], "savvina@%")],
            )
        ]
        assert facts.writes == [PrivilegeGrant("a\\_b", None, ["INSERT"], "savvina@%")]

    async def test_table_level_grant_alone_writes_only_that_table(self) -> None:
        adapter, _ = _grants_adapter(
            grants=[
                "GRANT USAGE ON *.* TO `savvina`@`%`",
                "GRANT SELECT, UPDATE ON `shop`.`orders` TO `savvina`@`%`",
                "GRANT SELECT ON `shop`.`items` TO `savvina`@`%`",
            ],
            tables=[("shop", "orders"), ("shop", "items")],
        )
        facts = await adapter.audit_privileges([("shop", "orders"), ("shop", "items")], timeout=7)
        assert facts.writes == [PrivilegeGrant("shop", "orders", ["UPDATE"], "savvina@%")]
        by_name = {t.table_name: t for t in facts.tables}
        assert by_name["orders"].can_write is True
        assert by_name["items"].can_write is False

    async def test_global_all_privileges_is_a_superuser_and_a_schema_one_is_not(self) -> None:
        adapter, _ = _grants_adapter(
            user="root@localhost", grants=["GRANT ALL PRIVILEGES ON *.* TO `root`@`%`"]
        )
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.is_superuser is True
        adapter, _ = _grants_adapter(grants=["GRANT ALL PRIVILEGES ON `shop`.* TO `u`@`%`"])
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.is_superuser is False
        assert facts.writes == [PrivilegeGrant("shop", None, SCHEMA_ALL_WRITES, "savvina@%")]
        # A quoted schema name with a space is a schema grant, not a global one.
        adapter, _ = _grants_adapter(grants=["GRANT ALL ON `my db`.* TO `u`@`%`"])
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.is_superuser is False
        assert facts.writes == [PrivilegeGrant("my db", None, SCHEMA_ALL_WRITES, "savvina@%")]

    async def test_a_bare_name_yields_one_row_per_schema_and_the_system_schemas_are_excluded(
        self,
    ) -> None:
        adapter, conn = _grants_adapter(
            grants=["GRANT SELECT ON *.* TO `u`@`%`"],
            tables=[("shop", "audit_log"), ("archive", "audit_log")],
        )
        facts = await adapter.audit_privileges([(None, "audit_log"), (None, "ghost")], timeout=7)
        via_global = [PrivilegeGrant(None, None, ["SELECT"], "savvina@%")]
        assert facts.tables == [
            TablePrivilegeFacts(
                "archive", "audit_log", True, can_select=True, select_grants=via_global
            ),
            TablePrivilegeFacts(
                "shop", "audit_log", True, can_select=True, select_grants=via_global
            ),
            TablePrivilegeFacts(None, "ghost", False),
        ]
        sql, params = conn._cursor.execute.call_args_list[3].args
        assert "table_name IN (%s, %s)" in sql
        assert (
            "table_schema NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')"
            in sql
        )
        assert params == ["audit_log", "ghost"]

    async def test_duplicates_are_read_once(self) -> None:
        adapter, conn = _grants_adapter(grants=[], tables=[("shop", "orders")])
        facts = await adapter.audit_privileges(
            [("shop", "orders"), ("shop", "orders"), (None, "orders")], timeout=7
        )
        assert [(t.schema_name, t.table_name) for t in facts.tables] == [("shop", "orders")]
        assert conn._cursor.execute.call_args_list[3].args[1] == ["shop", "orders", "orders"]

    async def test_no_table_skips_the_lookup(self) -> None:
        adapter, conn = _grants_adapter(grants=[])
        await adapter.audit_privileges([], timeout=7)
        statements = [c.args[0] for c in conn._cursor.execute.call_args_list]
        assert statements[:3] == [
            "SELECT CURRENT_USER()",
            "SHOW GRANTS FOR `savvina`@`%`",
            "SELECT CURRENT_ROLE()",
        ]
        assert len(statements) == 4 and "information_schema.schemata" in statements[3]

    async def test_mariadb_adds_the_current_roles_grants_under_max_statement_time(self) -> None:
        adapter, conn = _grants_adapter(
            version="10.11.6-MariaDB",
            grants=["GRANT USAGE ON *.* TO `u`@`%`", "GRANT `reader` TO `u`@`%`"],
            roles="reader",
            role_grants={"`reader`": ["GRANT SELECT ON `shop`.* TO `reader`"]},
            tables=[("shop", "orders")],
        )
        facts = await adapter.audit_privileges([("shop", "orders")], timeout=7)
        assert facts.tables[0].select_grants == [PrivilegeGrant("shop", None, ["SELECT"], "reader")]
        assert facts.tables[0].can_select is True
        calls = [c.args for c in conn._cursor.execute.call_args_list]
        statements = [c[0] for c in calls]
        assert calls[0] == ("SET SESSION max_statement_time = %s", (7,))
        assert "SELECT CURRENT_ROLE()" in statements
        assert "SHOW GRANTS FOR `reader`" in statements
        assert calls[-1] == ("SET SESSION max_statement_time = 0",)

    async def test_mariadb_without_a_role_reads_no_role_grants(self) -> None:
        adapter, conn = _grants_adapter(version="10.11.6-MariaDB", grants=[])
        conn._cursor.fetchone = AsyncMock(side_effect=[("u@%",), (None,)])
        await adapter.audit_privileges([], timeout=7)
        statements = [c.args[0] for c in conn._cursor.execute.call_args_list]
        assert [s for s in statements if s.startswith("SHOW GRANTS")] == ["SHOW GRANTS FOR `u`@`%`"]

    async def test_a_driver_exception_passes_through_and_mariadb_still_resets(self) -> None:
        adapter, conn = _grants_adapter(version="10.11.6-MariaDB", grants=[])
        cause = RuntimeError("Lost connection")
        conn._cursor.execute.side_effect = [None, cause, None]
        with pytest.raises(RuntimeError, match="Lost connection"):
            await adapter.audit_privileges([], timeout=7)
        assert conn._cursor.execute.call_args_list[-1].args == (
            "SET SESSION max_statement_time = 0",
        )

    async def test_a_cancelled_audit_closes_the_connection_and_sends_no_reset(self) -> None:
        adapter, conn = _grants_adapter(version="10.11.6-MariaDB", grants=[])
        conn._cursor.execute.side_effect = [None, asyncio.CancelledError()]
        with pytest.raises(asyncio.CancelledError):
            await adapter.audit_privileges([], timeout=7)
        conn.close.assert_called_once()
        assert len(conn._cursor.execute.call_args_list) == 2

    async def test_not_connected_raises(self) -> None:
        with pytest.raises(RuntimeError):
            await MySQLDataSource().audit_privileges([], timeout=7)

    async def test_super_alone_is_has_super_and_global_all_is_superuser(self) -> None:
        adapter, _ = _grants_adapter(grants=["GRANT SUPER ON *.* TO `savvina`@`%`"])
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.has_super is True and facts.is_superuser is False and facts.writes == []
        adapter, _ = _grants_adapter(grants=["GRANT ALL PRIVILEGES ON *.* TO `savvina`@`%`"])
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.has_super is False and facts.is_superuser is True
        assert facts.writes == [PrivilegeGrant(None, None, ["ALL PRIVILEGES"], "savvina@%")]

    async def test_mysql_8_prints_global_all_as_the_static_list_and_it_is_a_superuser(
        self,
    ) -> None:
        # The first line of SHOW GRANTS FOR root@'%' on MySQL 8.0.46 (GRANT ALL ON *.*).
        line = (
            "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, RELOAD, SHUTDOWN, PROCESS, FILE,"
            " REFERENCES, INDEX, ALTER, SHOW DATABASES, SUPER, CREATE TEMPORARY TABLES,"
            " LOCK TABLES, EXECUTE, REPLICATION SLAVE, REPLICATION CLIENT, CREATE VIEW,"
            " SHOW VIEW, CREATE ROUTINE, ALTER ROUTINE, CREATE USER, EVENT, TRIGGER,"
            " CREATE TABLESPACE, CREATE ROLE, DROP ROLE ON *.* TO `savvina`@`%`"
            " WITH GRANT OPTION"
        )
        adapter, _ = _grants_adapter(grants=[line])
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.is_superuser is True and facts.has_super is False

    async def test_super_with_only_some_writes_is_has_super(self) -> None:
        adapter, _ = _grants_adapter(grants=["GRANT INSERT, UPDATE, SUPER ON *.* TO `savvina`@`%`"])
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.has_super is True and facts.is_superuser is False
        assert facts.writes == [PrivilegeGrant(None, None, ["INSERT", "UPDATE"], "savvina@%")]

    async def test_super_and_every_write_across_the_account_and_a_role_is_a_superuser(
        self,
    ) -> None:
        writes = ", ".join(sorted(_WRITE_PRIVILEGES))
        adapter, _ = _grants_adapter(
            grants=["GRANT SUPER ON *.* TO `savvina`@`%`"],
            roles="`dba`@`%`",
            role_grants={"`dba`@`%`": [f"GRANT {writes}, SUPER ON *.* TO `savvina`@`%`"]},
        )
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.is_superuser is True and facts.has_super is False

    async def test_all_below_the_global_level_is_the_writes_valid_at_that_level(self) -> None:
        # Revoking ALL PRIVILEGES on a database or table would take SELECT with it: the
        # writes name each write privilege that level can hold. Global ALL stays whole.
        adapter, _ = _grants_adapter(
            grants=[
                "GRANT ALL PRIVILEGES ON `shop`.* TO `savvina`@`%`",
                "GRANT ALL PRIVILEGES ON `other`.`t` TO `savvina`@`%`",
            ],
            tables=[("shop", "orders")],
        )
        facts = await adapter.audit_privileges([("shop", "orders")], timeout=7)
        assert facts.writes == [
            PrivilegeGrant("shop", None, SCHEMA_ALL_WRITES, "savvina@%"),
            PrivilegeGrant("other", "t", TABLE_ALL_WRITES, "savvina@%"),
        ]
        (table,) = facts.tables
        assert table.can_select is True and table.can_write is True
        assert table.select_grants == [PrivilegeGrant("shop", None, ["SELECT"], "savvina@%")]

    async def test_mariadb_all_below_the_global_level_includes_delete_history(self) -> None:
        # MariaDB's ALL on a database or table holds DELETE HISTORY (removes the history rows
        # of a system-versioned table); MySQL has no such privilege, so its lists stay as is.
        adapter, _ = _grants_adapter(
            version="10.11.6-MariaDB",
            grants=[
                "GRANT ALL PRIVILEGES ON `shop`.* TO `savvina`@`%`",
                "GRANT ALL PRIVILEGES ON `other`.`t` TO `savvina`@`%`",
            ],
        )
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.writes == [
            PrivilegeGrant(
                "shop", None, sorted([*SCHEMA_ALL_WRITES, "DELETE HISTORY"]), "savvina@%"
            ),
            PrivilegeGrant(
                "other", "t", sorted([*TABLE_ALL_WRITES, "DELETE HISTORY"]), "savvina@%"
            ),
        ]

    async def test_mariadb_delete_history_alone_is_a_write(self) -> None:
        adapter, _ = _grants_adapter(
            version="10.11.6-MariaDB",
            grants=[
                "GRANT USAGE ON *.* TO `savvina`@`%`",
                "GRANT SELECT, DELETE HISTORY ON `shop`.`orders` TO `savvina`@`%`",
            ],
            tables=[("shop", "orders")],
        )
        facts = await adapter.audit_privileges([("shop", "orders")], timeout=7)
        assert facts.writes == [PrivilegeGrant("shop", "orders", ["DELETE HISTORY"], "savvina@%")]
        assert facts.tables[0].can_write is True

    async def test_mariadb_super_and_every_write_but_delete_history_is_a_superuser(self) -> None:
        # The superuser rule compares with the writes MySQL also has: DELETE HISTORY is not
        # needed for it.
        writes = ", ".join(sorted(_WRITE_PRIVILEGES))
        adapter, _ = _grants_adapter(
            version="10.11.6-MariaDB", grants=[f"GRANT {writes}, SUPER ON *.* TO `savvina`@`%`"]
        )
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.is_superuser is True and facts.has_super is False

    async def test_routine_grants_are_not_table_grants(self) -> None:
        # MySQL gives a routine's creator EXECUTE and ALTER ROUTINE on it; a REVOKE ON TABLE
        # naming them fails (ERROR 1144). A routine named like a table does not merge with it.
        adapter, _ = _grants_adapter(
            grants=[
                "GRANT USAGE ON *.* TO `savvina`@`%`",
                "GRANT EXECUTE, ALTER ROUTINE ON PROCEDURE `shop`.`orders` TO `savvina`@`%`",
                "GRANT EXECUTE, ALTER ROUTINE ON FUNCTION `shop`.`f` TO `savvina`@`%`",
                "GRANT SELECT ON `shop`.`orders` TO `savvina`@`%`",
            ],
            tables=[("shop", "orders")],
        )
        facts = await adapter.audit_privileges([("shop", "orders")], timeout=7)
        assert facts.writes == []
        (table,) = facts.tables
        assert table.can_write is False and table.can_select is True
        assert table.select_grants == [PrivilegeGrant("shop", "orders", ["SELECT"], "savvina@%")]

    async def test_a_routine_line_alone_produces_nothing(self) -> None:
        for line in (
            "GRANT EXECUTE, ALTER ROUTINE ON PROCEDURE `shop`.`p` TO `savvina`@`%`",
            "GRANT ALL PRIVILEGES ON FUNCTION `shop`.`p` TO `savvina`@`%`",
        ):
            adapter, _ = _grants_adapter(grants=[line], tables=[("shop", "p")])
            facts = await adapter.audit_privileges([("shop", "p")], timeout=7)
            assert facts.writes == [], line
            (table,) = facts.tables
            assert (table.can_select, table.can_write, table.select_grants) == (False, False, [])

    async def test_create_and_alter_routine_on_a_schema_stay_writes(self) -> None:
        adapter, _ = _grants_adapter(
            grants=["GRANT CREATE ROUTINE, ALTER ROUTINE ON `shop`.* TO `savvina`@`%`"]
        )
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.writes == [
            PrivilegeGrant("shop", None, ["ALTER ROUTINE", "CREATE ROUTINE"], "savvina@%")
        ]

    async def test_super_grantees_name_the_account(self) -> None:
        adapter, _ = _grants_adapter(grants=["GRANT SUPER, PROCESS ON *.* TO `savvina`@`%`"])
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.has_super is True and facts.super_grantees == ["savvina@%"]

    async def test_super_only_through_an_active_role_names_the_role(self) -> None:
        usage = "GRANT USAGE ON *.* TO `savvina`@`%`"
        adapter, _ = _grants_adapter(
            grants=[usage],
            roles="`ops`@`%`",
            role_grants={"`ops`@`%`": ["GRANT SUPER ON *.* TO `savvina`@`%`"]},
        )
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.has_super is True and facts.super_grantees == ["ops@%"]

    async def test_no_super_grantees_without_has_super(self) -> None:
        adapter, _ = _grants_adapter(grants=["GRANT ALL PRIVILEGES ON *.* TO `savvina`@`%`"])
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.is_superuser is True and facts.super_grantees == []
        adapter, _ = _grants_adapter(grants=["GRANT SELECT ON *.* TO `savvina`@`%`"])
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.super_grantees == []

    async def test_the_widened_write_set(self) -> None:
        adapter, _ = _grants_adapter(
            grants=[
                "GRANT SELECT, ALTER, CREATE, INDEX, TRIGGER, LOCK TABLES, EVENT, SHOW VIEW"
                " ON `shop`.* TO `savvina`@`%`"
            ]
        )
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.writes == [
            PrivilegeGrant(
                "shop",
                None,
                ["ALTER", "CREATE", "EVENT", "INDEX", "LOCK TABLES", "TRIGGER"],
                "savvina@%",
            )
        ]

    async def test_column_level_write_grants_are_reported_with_their_columns(self) -> None:
        adapter, _ = _grants_adapter(
            grants=["GRANT SELECT (id), UPDATE (status, note) ON `shop`.`orders` TO `savvina`@`%`"],
            tables=[("shop", "orders")],
        )
        facts = await adapter.audit_privileges([("shop", "orders")], timeout=7)
        assert facts.writes == [
            PrivilegeGrant("shop", "orders", ["UPDATE"], "savvina@%", ["status", "note"])
        ]
        assert facts.tables[0].can_write is True
        assert facts.tables[0].can_select is False  # a column SELECT is not table SELECT

    async def test_schemas_are_read_without_the_system_ones(self) -> None:
        adapter, conn = _grants_adapter(grants=[], schemas=["other", "shop"])
        facts = await adapter.audit_privileges([], timeout=7)
        statements = [c.args[0] for c in conn._cursor.execute.call_args_list]
        assert statements[-1] == (
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name NOT IN"
            " ('mysql', 'sys', 'performance_schema', 'information_schema') ORDER BY 1"
        )
        assert facts.schemas == ["other", "shop"]

    async def test_mysql_8_active_roles_are_attributed_through_using(self) -> None:
        # The USING output merges the account's and the role's privileges into one line per
        # scope, as MySQL prints it: the role's share is per privilege, not per line.
        role_line = "GRANT `writer`@`%` TO `savvina`@`%`"
        usage = "GRANT USAGE ON *.* TO `savvina`@`%`"
        adapter, conn = _grants_adapter(
            grants=[usage, "GRANT UPDATE ON `shop`.`orders` TO `savvina`@`%`", role_line],
            roles="`writer`@`%`",
            role_grants={
                "`writer`@`%`": [
                    usage,
                    "GRANT SELECT, INSERT ON `shop`.* TO `savvina`@`%`",
                    "GRANT UPDATE, DELETE ON `shop`.`orders` TO `savvina`@`%`",
                    role_line,
                ]
            },
            tables=[("shop", "orders")],
        )
        facts = await adapter.audit_privileges([("shop", "orders")], timeout=7)
        statements = [c.args[0] for c in conn._cursor.execute.call_args_list]
        assert "SHOW GRANTS FOR `savvina`@`%` USING `writer`@`%`" in statements
        assert facts.writes == [
            PrivilegeGrant("shop", None, ["INSERT"], "writer@%"),
            PrivilegeGrant("shop", "orders", ["UPDATE"], "savvina@%"),
            PrivilegeGrant("shop", "orders", ["DELETE"], "writer@%"),
        ]
        assert facts.tables[0].select_grants == [
            PrivilegeGrant("shop", None, ["SELECT"], "writer@%")
        ]

    async def test_two_active_roles(self) -> None:
        adapter, _ = _grants_adapter(
            grants=["GRANT SELECT ON *.* TO `savvina`@`%`"],
            roles="`a`@`%`,`b`@`%`",
            role_grants={
                "`a`@`%`": ["GRANT SELECT, DROP ON *.* TO `savvina`@`%`"],
                "`b`@`%`": ["GRANT SELECT, FILE ON *.* TO `savvina`@`%`"],
            },
        )
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.writes == [
            PrivilegeGrant(None, None, ["DROP"], "a@%"),
            PrivilegeGrant(None, None, ["FILE"], "b@%"),
        ]

    async def test_a_merged_line_splits_per_privilege_and_per_column(self) -> None:
        # Checked live on MySQL 8.0.46: the account and the role share each scope's line.
        adapter, _ = _grants_adapter(
            user="rv_u@%",
            grants=[
                "GRANT USAGE ON *.* TO `rv_u`@`%`",
                "GRANT SELECT ON `sample_delivery`.* TO `rv_u`@`%` WITH GRANT OPTION",
                "GRANT UPDATE (`id`) ON `sample_delivery`.`orders` TO `rv_u`@`%`",
                "GRANT `rv_r1`@`%` TO `rv_u`@`%`",
            ],
            roles="`rv_r1`@`%`",
            role_grants={
                "`rv_r1`@`%`": [
                    "GRANT USAGE ON *.* TO `rv_u`@`%`",
                    "GRANT SELECT, INSERT ON `sample_delivery`.* TO `rv_u`@`%` WITH GRANT OPTION",
                    "GRANT UPDATE (`id`, `status`) ON `sample_delivery`.`orders` TO `rv_u`@`%`",
                    "GRANT `rv_r1`@`%` TO `rv_u`@`%`",
                ]
            },
            tables=[("sample_delivery", "orders")],
        )
        facts = await adapter.audit_privileges([("sample_delivery", "orders")], timeout=7)
        assert facts.writes == [
            PrivilegeGrant("sample_delivery", None, ["INSERT"], "rv_r1@%"),
            PrivilegeGrant("sample_delivery", "orders", ["UPDATE"], "rv_r1@%", ["status"]),
            PrivilegeGrant("sample_delivery", "orders", ["UPDATE"], "rv_u@%", ["id"]),
        ]
        assert facts.tables[0].select_grants == [
            PrivilegeGrant("sample_delivery", None, ["SELECT"], "rv_u@%")
        ]

    async def test_a_role_adding_columns_to_a_column_privilege_gets_only_its_columns(
        self,
    ) -> None:
        adapter, _ = _grants_adapter(
            grants=["GRANT SELECT (`a`), UPDATE (`a`) ON `shop`.`t` TO `savvina`@`%`"],
            roles="`writer`@`%`",
            role_grants={
                "`writer`@`%`": [
                    "GRANT SELECT (`a`, `b`), UPDATE (`a`, `c`) ON `shop`.`t` TO `savvina`@`%`"
                ]
            },
        )
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.writes == [
            PrivilegeGrant("shop", "t", ["UPDATE"], "savvina@%", ["a"]),
            PrivilegeGrant("shop", "t", ["UPDATE"], "writer@%", ["c"]),
        ]

    async def test_a_roles_all_over_the_accounts_select_prints_all_privileges(self) -> None:
        # MySQL 8.0.46, the account with SELECT on sample_delivery.*, its active role with ALL:
        # the USING form prints one ALL PRIVILEGES line for the scope.
        adapter, _ = _grants_adapter(
            user="rv_a@%",
            grants=[
                "GRANT USAGE ON *.* TO `rv_a`@`%`",
                "GRANT SELECT ON `sample_delivery`.* TO `rv_a`@`%`",
                "GRANT `rv_ra`@`%` TO `rv_a`@`%`",
            ],
            roles="`rv_ra`@`%`",
            role_grants={
                "`rv_ra`@`%`": [
                    "GRANT USAGE ON *.* TO `rv_a`@`%`",
                    "GRANT ALL PRIVILEGES ON `sample_delivery`.* TO `rv_a`@`%`",
                    "GRANT `rv_ra`@`%` TO `rv_a`@`%`",
                ]
            },
            tables=[("sample_delivery", "orders")],
        )
        facts = await adapter.audit_privileges([("sample_delivery", "orders")], timeout=7)
        assert facts.writes == [
            PrivilegeGrant("sample_delivery", None, SCHEMA_ALL_WRITES, "rv_ra@%")
        ]
        assert facts.tables[0].select_grants == [
            PrivilegeGrant("sample_delivery", None, ["SELECT"], "rv_a@%"),
            PrivilegeGrant("sample_delivery", None, ["SELECT"], "rv_ra@%"),
        ]

    async def test_a_roles_table_update_next_to_the_accounts_column_update(self) -> None:
        # MySQL 8.0.46, the account with INSERT and UPDATE (id) on orders, its active role with
        # table-level UPDATE: the USING form prints both UPDATE items on one line.
        adapter, _ = _grants_adapter(
            user="rv_b@%",
            grants=[
                "GRANT USAGE ON *.* TO `rv_b`@`%`",
                "GRANT INSERT, UPDATE (`id`) ON `sample_delivery`.`orders` TO `rv_b`@`%`",
                "GRANT `rv_rb`@`%` TO `rv_b`@`%`",
            ],
            roles="`rv_rb`@`%`",
            role_grants={
                "`rv_rb`@`%`": [
                    "GRANT USAGE ON *.* TO `rv_b`@`%`",
                    "GRANT INSERT, UPDATE, UPDATE (`id`) ON `sample_delivery`.`orders`"
                    " TO `rv_b`@`%`",
                    "GRANT `rv_rb`@`%` TO `rv_b`@`%`",
                ]
            },
        )
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.writes == [
            PrivilegeGrant("sample_delivery", "orders", ["INSERT"], "rv_b@%"),
            PrivilegeGrant("sample_delivery", "orders", ["UPDATE"], "rv_rb@%"),
            PrivilegeGrant("sample_delivery", "orders", ["UPDATE"], "rv_b@%", ["id"]),
        ]

    async def test_all_privileges_held_directly_covers_the_roles_share_at_that_scope(
        self,
    ) -> None:
        adapter, _ = _grants_adapter(
            grants=["GRANT ALL PRIVILEGES ON `shop`.* TO `savvina`@`%`"],
            roles="`writer`@`%`",
            role_grants={
                "`writer`@`%`": [
                    "GRANT ALL PRIVILEGES ON `shop`.* TO `savvina`@`%`",
                    "GRANT INSERT ON `other`.* TO `savvina`@`%`",
                ]
            },
        )
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.writes == [
            PrivilegeGrant("shop", None, SCHEMA_ALL_WRITES, "savvina@%"),
            PrivilegeGrant("other", None, ["INSERT"], "writer@%"),
        ]

    async def test_a_failing_role_lookup_falls_back_to_the_folded_grants(self) -> None:
        usage = "GRANT USAGE ON *.* TO `savvina`@`%`"
        adapter, conn = _grants_adapter(
            grants=[usage],
            roles="`writer`@`%`",
            role_failures={"`writer`@`%`"},
            role_grants={"SHOW GRANTS": [usage, "GRANT INSERT ON `shop`.* TO `savvina`@`%`"]},
        )
        facts = await adapter.audit_privileges([], timeout=7)
        statements = [c.args[0] for c in conn._cursor.execute.call_args_list]
        assert "SHOW GRANTS FOR `writer`@`%`" in statements and "SHOW GRANTS" in statements
        # Attributed to the account: the role's share cannot be told apart.
        assert facts.writes == [PrivilegeGrant("shop", None, ["INSERT"], "savvina@%")]

    async def test_mariadb_reads_the_current_role_with_show_grants_for(self) -> None:
        adapter, conn = _grants_adapter(
            version="10.11.6-MariaDB",
            grants=["GRANT USAGE ON *.* TO `savvina`@`%`"],
            roles="reader",
            role_grants={"`reader`": ["GRANT SELECT ON `shop`.* TO `reader`"]},
            tables=[("shop", "orders")],
        )
        facts = await adapter.audit_privileges([("shop", "orders")], timeout=7)
        statements = [c.args[0] for c in conn._cursor.execute.call_args_list]
        assert "SHOW GRANTS FOR `reader`" in statements
        assert facts.tables[0].select_grants == [PrivilegeGrant("shop", None, ["SELECT"], "reader")]

    async def test_select_grants_name_every_level_that_gives_select(self) -> None:
        # Each level is a separate grant a REVOKE must name at its own scope: global, then
        # the schema literal as printed (escapes kept), then the table.
        adapter, _ = _grants_adapter(
            grants=[
                "GRANT SELECT ON *.* TO `savvina`@`%`",
                "GRANT ALL PRIVILEGES ON `sample\\_delivery`.* TO `savvina`@`%`",
                "GRANT SELECT ON `sample_delivery`.`orders` TO `savvina`@`%`",
            ],
            tables=[("sample_delivery", "orders")],
        )
        facts = await adapter.audit_privileges([("sample_delivery", "orders")], timeout=7)
        assert facts.tables[0].select_grants == [
            PrivilegeGrant(None, None, ["SELECT"], "savvina@%"),
            PrivilegeGrant("sample\\_delivery", None, ["SELECT"], "savvina@%"),
            PrivilegeGrant("sample_delivery", "orders", ["SELECT"], "savvina@%"),
        ]
        assert facts.tables[0].can_select is True

    async def test_the_direct_grants_name_the_account_explicitly(self) -> None:
        # CURRENT_USER() folds the active roles on MySQL 8; the account named explicitly
        # does not. Split at the last "@" (a user name may hold one), backticks doubled.
        insert = "GRANT INSERT ON `shop`.* TO `a``b@c`@`d``e`"
        adapter, conn = _grants_adapter(
            user="a`b@c@d`e",
            grants=["GRANT USAGE ON *.* TO `a``b@c`@`d``e`", insert],
            roles="`writer`@`%`",
            role_grants={"`writer`@`%`": ["GRANT DROP ON *.* TO `a``b@c`@`d``e`", insert]},
        )
        facts = await adapter.audit_privileges([], timeout=7)
        statements = [c.args[0] for c in conn._cursor.execute.call_args_list]
        assert statements[1] == "SHOW GRANTS FOR `a``b@c`@`d``e`"
        assert "SHOW GRANTS FOR `a``b@c`@`d``e` USING `writer`@`%`" in statements
        assert "SHOW GRANTS FOR CURRENT_USER()" not in statements
        assert facts.writes == [
            PrivilegeGrant(None, None, ["DROP"], "writer@%"),
            PrivilegeGrant("shop", None, ["INSERT"], "a`b@c@d`e"),
        ]

    async def test_a_failing_explicit_account_falls_back_to_current_user(self) -> None:
        # The CURRENT_USER() form folds the active roles in: every line is the account's,
        # and the USING output adds nothing beyond it.
        folded = [
            "GRANT DROP ON *.* TO `savvina`@`%`",
            "GRANT INSERT ON `shop`.* TO `savvina`@`%`",
            "GRANT `writer`@`%` TO `savvina`@`%`",
        ]
        adapter, conn = _grants_adapter(
            grants=folded,
            direct_fails=True,
            roles="`writer`@`%`",
            role_grants={"`writer`@`%`": folded},
        )
        facts = await adapter.audit_privileges([], timeout=7)
        statements = [c.args[0] for c in conn._cursor.execute.call_args_list]
        assert statements[1:3] == [
            "SHOW GRANTS FOR `savvina`@`%`",
            "SHOW GRANTS FOR CURRENT_USER()",
        ]
        assert "SHOW GRANTS FOR CURRENT_USER() USING `writer`@`%`" in statements
        assert facts.writes == [
            PrivilegeGrant(None, None, ["DROP"], "savvina@%"),
            PrivilegeGrant("shop", None, ["INSERT"], "savvina@%"),
        ]

    async def test_after_the_current_user_fallback_a_role_is_not_read_on_its_own(self) -> None:
        # The folded direct lines already hold the roles' share: SHOW GRANTS FOR role is
        # skipped, and what the bare SHOW GRANTS adds is attributed to the account.
        usage = "GRANT USAGE ON *.* TO `savvina`@`%`"
        adapter, conn = _grants_adapter(
            grants=[usage, "GRANT INSERT ON `shop`.* TO `savvina`@`%`"],
            direct_fails=True,
            roles="`writer`@`%`",
            role_failures={"`writer`@`%`"},
            role_grants={
                "SHOW GRANTS": [usage, "GRANT INSERT, DELETE ON `shop`.* TO `savvina`@`%`"]
            },
        )
        facts = await adapter.audit_privileges([], timeout=7)
        statements = [c.args[0] for c in conn._cursor.execute.call_args_list]
        assert "SHOW GRANTS FOR `writer`@`%`" not in statements
        assert "SHOW GRANTS" in statements
        assert facts.writes == [
            PrivilegeGrant("shop", None, ["DELETE", "INSERT"], "savvina@%"),
        ]

    async def test_writes_are_capped_at_500(self) -> None:
        grants = [f"GRANT INSERT ON `shop`.`t{i:03d}` TO `savvina`@`%`" for i in range(501)]
        adapter, _ = _grants_adapter(grants=grants)
        facts = await adapter.audit_privileges([], timeout=7)
        assert len(facts.writes) == 500 and facts.writes_truncated is True
        assert facts.writes[-1] == PrivilegeGrant("shop", "t499", ["INSERT"], "savvina@%")
        adapter, _ = _grants_adapter(grants=grants[:500])
        facts = await adapter.audit_privileges([], timeout=7)
        assert len(facts.writes) == 500 and facts.writes_truncated is False


class TestDescribeConnectionError:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "(1045, \"Access denied for user 'u'@'h' (using password: YES)\")",
                "the server refused the user name or password",
            ),
            ("(1044, 'x')", "the account has no access to the database"),
            ("(1049, 'x')", "the database does not exist"),
            ("(1130, 'x')", "the server does not allow connections from this host"),
            ("(1040, 'x')", "the server has too many connections"),
            ("(2003, 'x')", "the server could not be reached"),
            ("(2005, 'x')", "the host name could not be resolved"),
            ("(2013, 'x')", "the connection was lost"),
            ("(1142, 'x')", "the account may not read a catalog table"),
            ("(1143, 'x')", "the account may not read a catalog table"),
            ("(1227, 'x')", "the account lacks a privilege the check needs"),
            ("(3159, 'x')", "the server requires an encrypted connection"),
            ("(9999, 'x')", "MySQL error 9999"),
            ("  (1045, 'x')", "the server refused the user name or password"),
            ("Connection refused", None),
            ("", None),
        ],
    )
    def test_maps_the_error_number(self, text: str, expected: str | None) -> None:
        assert MySQLDataSource().describe_connection_error(text) == expected

    def test_the_base_returns_none(self) -> None:
        assert PostgreSQLDataSource().describe_connection_error("(1045, 'x')") is None
