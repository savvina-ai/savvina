# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for the MySQL / MariaDB data source adapter."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from app.datasources.adapters.mysql import (
    MySQLDataSource,
    _is_enum_or_set,
    _parse_enum_values,
    _serialize,
)
from app.datasources.models import PrivacySettings

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

        if cursor_class is aiomysql.SSCursor:
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
