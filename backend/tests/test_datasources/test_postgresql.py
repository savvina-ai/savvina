# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for the PostgreSQL data source adapter."""

from typing import ClassVar
from unittest.mock import AsyncMock, patch

import pytest

import app.datasources.adapters.postgresql as _pg_module
from app.datasources.adapters.postgresql import PostgreSQLDataSource, _quote_pg_ident
from app.datasources.models import (
    ColumnInfo,
    DataSourceSchema,
    PrivacySettings,
    RelationshipInfo,
    SchemaInfo,
    TableInfo,
)
from tests.conftest import FakeRecord, MockConnection, MockPool

# ── Helpers ───────────────────────────────────────────────────────────────────


def make_adapter_with_pool(version: str = "PostgreSQL 15.0 (test)"):
    """Return (adapter, mock_conn) with the pool already injected."""
    adapter = PostgreSQLDataSource()
    conn = MockConnection(version=version)
    pool = MockPool(conn)
    adapter._pool = pool
    return adapter, conn


# Privacy shorthand: all optional metadata disabled to minimise side_effect list
MINIMAL_PRIVACY = PrivacySettings(
    include_sample_values=False,
    include_column_comments=False,
    include_row_counts=False,
)

# Reusable fake rows
_SCHEMA_ROW = [FakeRecord({"schema_name": "public"})]
_TABLE_ROWS = [
    FakeRecord({"table_schema": "public", "table_name": "users", "table_type": "BASE TABLE"}),
    FakeRecord({"table_schema": "public", "table_name": "orders", "table_type": "BASE TABLE"}),
]
_COLUMN_ROWS = [
    FakeRecord(
        {
            "table_schema": "public",
            "table_name": "users",
            "column_name": "id",
            "data_type": "integer",
            "udt_name": "int4",
            "is_nullable": "NO",
            "column_default": None,
            "ordinal_position": 1,
        }
    ),
    FakeRecord(
        {
            "table_schema": "public",
            "table_name": "users",
            "column_name": "name",
            "data_type": "text",
            "udt_name": "text",
            "is_nullable": "YES",
            "column_default": None,
            "ordinal_position": 2,
        }
    ),
    FakeRecord(
        {
            "table_schema": "public",
            "table_name": "users",
            "column_name": "email",
            "data_type": "text",
            "udt_name": "text",
            "is_nullable": "YES",
            "column_default": None,
            "ordinal_position": 3,
        }
    ),
    FakeRecord(
        {
            "table_schema": "public",
            "table_name": "orders",
            "column_name": "id",
            "data_type": "integer",
            "udt_name": "int4",
            "is_nullable": "NO",
            "column_default": None,
            "ordinal_position": 1,
        }
    ),
    FakeRecord(
        {
            "table_schema": "public",
            "table_name": "orders",
            "column_name": "user_id",
            "data_type": "integer",
            "udt_name": "int4",
            "is_nullable": "YES",
            "column_default": None,
            "ordinal_position": 2,
        }
    ),
]
_PK_ROWS = [
    FakeRecord({"table_schema": "public", "table_name": "users", "column_name": "id"}),
    FakeRecord({"table_schema": "public", "table_name": "orders", "column_name": "id"}),
]
_FK_ROWS = [
    FakeRecord(
        {
            "from_schema": "public",
            "from_table": "orders",
            "from_column": "user_id",
            "to_schema": "public",
            "to_table": "users",
            "to_column": "id",
        }
    ),
]


def _minimal_side_effect(extra_column_rows=None):
    """Build side_effect list for MINIMAL_PRIVACY (5 fetch calls)."""
    cols = extra_column_rows if extra_column_rows is not None else _COLUMN_ROWS
    return [_SCHEMA_ROW, _TABLE_ROWS, cols, _PK_ROWS, _FK_ROWS]


# ── Config schema ─────────────────────────────────────────────────────────────


class TestConfigSchema:
    def setup_method(self):
        self.adapter = PostgreSQLDataSource()

    def test_returns_dict_with_fields_key(self):
        schema = self.adapter.get_config_schema()
        assert "fields" in schema
        assert isinstance(schema["fields"], list)

    def test_all_required_fields_present(self):
        names = {f["name"] for f in self.adapter.get_config_schema()["fields"]}
        assert names == {"host", "port", "database", "username", "password", "ssl_mode"}

    def test_host_is_required(self):
        fields = {f["name"]: f for f in self.adapter.get_config_schema()["fields"]}
        assert fields["host"]["required"] is True

    def test_password_type_is_password(self):
        fields = {f["name"]: f for f in self.adapter.get_config_schema()["fields"]}
        assert fields["password"]["type"] == "password"

    def test_port_default_is_5432(self):
        fields = {f["name"]: f for f in self.adapter.get_config_schema()["fields"]}
        assert fields["port"]["default"] == 5432

    def test_ssl_mode_is_select_type(self):
        fields = {f["name"]: f for f in self.adapter.get_config_schema()["fields"]}
        assert fields["ssl_mode"]["type"] == "select"

    def test_ssl_mode_includes_disable_and_require(self):
        fields = {f["name"]: f for f in self.adapter.get_config_schema()["fields"]}
        opts = fields["ssl_mode"]["options"]
        assert "disable" in opts
        assert "require" in opts


# ── System prompt additions ───────────────────────────────────────────────────


class TestSystemPromptAdditions:
    def setup_method(self):
        self.adapter = PostgreSQLDataSource()

    def test_returns_non_empty_string(self):
        result = self.adapter.get_system_prompt_additions()
        assert isinstance(result, str) and len(result) > 0

    def test_mentions_postgresql(self):
        assert "PostgreSQL" in self.adapter.get_system_prompt_additions()

    def test_mentions_ilike(self):
        assert "ILIKE" in self.adapter.get_system_prompt_additions()

    def test_mentions_date_trunc(self):
        assert "DATE_TRUNC" in self.adapter.get_system_prompt_additions()

    def test_mentions_cte_or_with(self):
        txt = self.adapter.get_system_prompt_additions()
        assert "CTE" in txt or "WITH" in txt


# ── format_schema_for_llm ─────────────────────────────────────────────────────


def _make_test_schema() -> DataSourceSchema:
    users_cols = [
        ColumnInfo("id", "integer", "int4", nullable=False, is_primary_key=True),
        ColumnInfo("name", "text", "text", sample_values=["Alice", "Bob"]),
        ColumnInfo("email", "text", "text"),  # sensitive
        ColumnInfo("status", "varchar", "varchar", sample_values=["active", "inactive"]),
    ]
    orders_cols = [
        ColumnInfo("id", "integer", "int4", nullable=False, is_primary_key=True),
        ColumnInfo("user_id", "integer", "int4"),
    ]
    return DataSourceSchema(
        source_type="postgresql",
        schemas=[SchemaInfo("public")],
        tables=[
            TableInfo(None, "public", "users", "table", users_cols, 1000, "User accounts"),
            TableInfo(None, "public", "orders", "table", orders_cols, 5000),
        ],
        relationships=[
            RelationshipInfo("public", "orders", "user_id", "public", "users", "id"),
        ],
    )


class TestFormatSchemaForLLM:
    def setup_method(self):
        self.adapter = PostgreSQLDataSource()
        self.schema = _make_test_schema()

    def test_both_tables_included(self):
        out = self.adapter.format_schema_for_llm(self.schema)
        assert "public.users" in out
        assert "public.orders" in out

    def test_create_table_syntax_used(self):
        out = self.adapter.format_schema_for_llm(self.schema)
        assert "CREATE TABLE public.users" in out

    def test_schema_header_comment(self):
        assert "-- Schema: public" in self.adapter.format_schema_for_llm(self.schema)

    def test_primary_key_annotated(self):
        assert "Primary Key" in self.adapter.format_schema_for_llm(self.schema)

    def test_not_null_present_for_pk_column(self):
        assert "NOT NULL" in self.adapter.format_schema_for_llm(self.schema)

    def test_fk_annotation_shown(self):
        assert "FK: user_id" in self.adapter.format_schema_for_llm(self.schema)

    # ── Sensitive column (bug fix #1) ─────────────────────────────────────────

    def test_sensitive_column_present_in_output(self):
        """email must NOT be silently dropped — it must appear with [SENSITIVE]."""
        out = self.adapter.format_schema_for_llm(self.schema)
        assert "email" in out

    def test_sensitive_column_annotated_sensitive(self):
        out = self.adapter.format_schema_for_llm(self.schema)
        assert "[SENSITIVE]" in out

    def test_sensitive_column_has_no_sample_values(self):
        """email has no sample_values in the test schema, so none should appear."""
        out = self.adapter.format_schema_for_llm(self.schema)
        # The only annotation on the email line should be [SENSITIVE]
        email_line = next(
            (
                line
                for line in out.splitlines()
                if "email" in line and line.strip().startswith("email")
            ),
            None,
        )
        assert email_line is not None
        assert "[SENSITIVE]" in email_line
        assert "e.g." not in email_line

    # ── Privacy: row counts ────────────────────────────────────────────────────

    def test_row_count_shown_by_default(self):
        assert "1,000" in self.adapter.format_schema_for_llm(self.schema)

    def test_row_count_hidden_when_disabled(self):
        p = PrivacySettings(include_row_counts=False)
        assert "1,000" not in self.adapter.format_schema_for_llm(self.schema, p)

    # ── Privacy: sample values ─────────────────────────────────────────────────

    def test_sample_values_shown_by_default(self):
        out = self.adapter.format_schema_for_llm(self.schema)
        assert "Alice" in out

    def test_sample_values_hidden_when_disabled(self):
        p = PrivacySettings(include_sample_values=False)
        assert "Alice" not in self.adapter.format_schema_for_llm(self.schema, p)

    # ── Privacy: excluded tables ───────────────────────────────────────────────

    def test_excluded_table_absent(self):
        p = PrivacySettings(excluded_tables=["orders"])
        out = self.adapter.format_schema_for_llm(self.schema, p)
        assert "public.orders" not in out
        assert "public.users" in out

    def test_excluded_schema_hides_all_its_tables(self):
        p = PrivacySettings(excluded_schemas=["public"])
        out = self.adapter.format_schema_for_llm(self.schema, p)
        assert "public.users" not in out
        assert "public.orders" not in out

    # ── Privacy: excluded columns ─────────────────────────────────────────────

    def test_excluded_column_absent(self):
        p = PrivacySettings(excluded_columns=["public.users.name"])
        out = self.adapter.format_schema_for_llm(self.schema, p)
        col_lines = [line for line in out.splitlines() if line.strip().startswith("name ")]
        assert len(col_lines) == 0

    # ── Privacy: comments ─────────────────────────────────────────────────────

    def test_table_description_shown_when_comments_enabled(self):
        p = PrivacySettings(include_column_comments=True)
        assert "User accounts" in self.adapter.format_schema_for_llm(self.schema, p)

    def test_table_description_hidden_when_comments_disabled(self):
        p = PrivacySettings(include_column_comments=False)
        assert "User accounts" not in self.adapter.format_schema_for_llm(self.schema, p)

    def test_empty_schema_returns_string(self):
        out = self.adapter.format_schema_for_llm(DataSourceSchema(source_type="postgresql"))
        assert isinstance(out, str)


# ── validate_query ────────────────────────────────────────────────────────────


class TestValidateQuery:
    def setup_method(self):
        self.adapter = PostgreSQLDataSource()

    def test_valid_select_passes(self):
        assert self.adapter.validate_query("SELECT 1").is_valid

    def test_drop_rejected(self):
        assert not self.adapter.validate_query("DROP TABLE users").is_valid

    def test_pg_sleep_blocked(self):
        assert not self.adapter.validate_query("SELECT pg_sleep(10)").is_valid

    def test_limit_added(self):
        r = self.adapter.validate_query("SELECT * FROM users")
        assert "LIMIT" in r.sanitized_query.upper()


# ── test_connection ───────────────────────────────────────────────────────────


class TestTestConnection:
    _cfg: ClassVar[dict[str, object]] = {
        "host": "localhost",
        "port": 5432,
        "database": "test",
        "username": "u",
        "password": "p",
    }

    async def test_success_returns_true(self):
        mock_conn = MockConnection(version="PostgreSQL 15.0")
        with patch("asyncpg.connect", new=AsyncMock(return_value=mock_conn)):
            result = await PostgreSQLDataSource().test_connection(self._cfg)
        assert result.success is True
        assert result.server_version == "PostgreSQL 15.0"

    async def test_success_closes_connection(self):
        """Connection must always be closed after a successful test."""
        mock_conn = MockConnection()
        with patch("asyncpg.connect", new=AsyncMock(return_value=mock_conn)):
            await PostgreSQLDataSource().test_connection(self._cfg)
        mock_conn.close.assert_called_once()

    async def test_failure_returns_false(self):
        with patch("asyncpg.connect", new=AsyncMock(side_effect=Exception("refused"))):
            result = await PostgreSQLDataSource().test_connection(self._cfg)
        assert result.success is False
        assert "refused" in result.message

    async def test_connection_closed_even_if_fetchval_raises(self):
        """Bug fix #3: try/finally guarantees close() on fetchval failure."""
        mock_conn = MockConnection()
        mock_conn.fetchval = AsyncMock(side_effect=RuntimeError("oops"))
        with patch("asyncpg.connect", new=AsyncMock(return_value=mock_conn)):
            result = await PostgreSQLDataSource().test_connection(self._cfg)
        assert result.success is False
        mock_conn.close.assert_called_once()


# ── connect ───────────────────────────────────────────────────────────────────


class TestConnect:
    _cfg: ClassVar[dict[str, object]] = {
        "host": "localhost",
        "port": 5432,
        "database": "test",
        "username": "u",
        "password": "p",
    }

    @pytest.fixture(autouse=True)
    def clear_pool_cache(self):
        _pg_module._pool_cache.clear()
        yield
        _pg_module._pool_cache.clear()

    async def test_success_sets_pool(self):
        mock_conn = MockConnection()
        pool = MockPool(mock_conn)
        with patch("asyncpg.create_pool", new=AsyncMock(return_value=pool)):
            result = await PostgreSQLDataSource().connect(self._cfg)
        assert result.success is True

    async def test_create_pool_failure_returns_error(self):
        with patch("asyncpg.create_pool", new=AsyncMock(side_effect=Exception("No route"))):
            result = await PostgreSQLDataSource().connect(self._cfg)
        assert result.success is False
        assert "No route" in result.message

    async def test_pool_cleaned_up_if_version_fetch_fails(self):
        """Bug fix #4: pool must be closed if connect() fails after pool creation."""
        mock_conn = MockConnection()
        mock_conn.fetchval = AsyncMock(side_effect=Exception("Auth failed"))
        pool = MockPool(mock_conn)

        adapter = PostgreSQLDataSource()
        with patch("asyncpg.create_pool", new=AsyncMock(return_value=pool)):
            result = await adapter.connect(self._cfg)

        assert result.success is False
        assert adapter._pool is None  # pool reference cleared
        pool.close.assert_called_once()  # pool was actually closed


# ── introspect ────────────────────────────────────────────────────────────────


class TestIntrospect:
    async def test_basic_schema_and_tables(self):
        adapter, conn = make_adapter_with_pool()
        conn.fetch.side_effect = _minimal_side_effect()

        result = await adapter.introspect(MINIMAL_PRIVACY)

        assert len(result.schemas) == 1
        assert result.schemas[0].name == "public"
        assert {t.name for t in result.tables} == {"users", "orders"}

    async def test_columns_populated(self):
        adapter, conn = make_adapter_with_pool()
        conn.fetch.side_effect = _minimal_side_effect()

        result = await adapter.introspect(MINIMAL_PRIVACY)

        users = next(t for t in result.tables if t.name == "users")
        col_names = [c.name for c in users.columns]
        assert "id" in col_names
        assert "name" in col_names
        assert "email" in col_names  # sensitive — but MUST still be present (bug fix #1)

    async def test_primary_key_flagged(self):
        adapter, conn = make_adapter_with_pool()
        conn.fetch.side_effect = _minimal_side_effect()

        result = await adapter.introspect(MINIMAL_PRIVACY)

        users = next(t for t in result.tables if t.name == "users")
        pk_cols = [c for c in users.columns if c.is_primary_key]
        assert len(pk_cols) == 1
        assert pk_cols[0].name == "id"

    async def test_foreign_keys_captured(self):
        adapter, conn = make_adapter_with_pool()
        conn.fetch.side_effect = _minimal_side_effect()

        result = await adapter.introspect(MINIMAL_PRIVACY)

        assert len(result.relationships) == 1
        rel = result.relationships[0]
        assert rel.from_table == "orders"
        assert rel.from_column == "user_id"
        assert rel.to_table == "users"
        assert rel.to_column == "id"

    async def test_excluded_table_absent_from_result(self):
        adapter, conn = make_adapter_with_pool()
        # Return a column only for users (secrets has no column rows anyway)
        conn.fetch.side_effect = [
            _SCHEMA_ROW,
            [
                FakeRecord(
                    {
                        "table_schema": "public",
                        "table_name": "users",
                        "table_type": "BASE TABLE",
                    }
                ),
                FakeRecord(
                    {
                        "table_schema": "public",
                        "table_name": "secrets",
                        "table_type": "BASE TABLE",
                    }
                ),
            ],
            [
                FakeRecord(
                    {
                        "table_schema": "public",
                        "table_name": "users",
                        "column_name": "id",
                        "data_type": "integer",
                        "udt_name": "int4",
                        "is_nullable": "NO",
                        "column_default": None,
                        "ordinal_position": 1,
                    }
                )
            ],
            [],  # pks
            [],  # fks
        ]
        p = PrivacySettings(
            excluded_tables=["secrets"],
            include_sample_values=False,
            include_column_comments=False,
            include_row_counts=False,
        )

        result = await adapter.introspect(p)

        table_names = [t.name for t in result.tables]
        assert "users" in table_names
        assert "secrets" not in table_names

    async def test_sensitive_column_present_bug_fix(self):
        """Bug fix #1: sensitive columns (email) must NOT be dropped during introspection."""
        adapter, conn = make_adapter_with_pool()
        cols = [
            FakeRecord(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "id",
                    "data_type": "integer",
                    "udt_name": "int4",
                    "is_nullable": "NO",
                    "column_default": None,
                    "ordinal_position": 1,
                }
            ),
            FakeRecord(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "email",
                    "data_type": "text",
                    "udt_name": "text",
                    "is_nullable": "YES",
                    "column_default": None,
                    "ordinal_position": 2,
                }
            ),
        ]
        conn.fetch.side_effect = [
            _SCHEMA_ROW,
            [
                FakeRecord(
                    {
                        "table_schema": "public",
                        "table_name": "users",
                        "table_type": "BASE TABLE",
                    }
                )
            ],
            cols,
            [],
            [],
        ]

        result = await adapter.introspect(MINIMAL_PRIVACY)

        users = result.tables[0]
        col_names = [c.name for c in users.columns]
        assert "email" in col_names  # must survive

    async def test_explicitly_excluded_column_absent(self):
        """Explicitly excluded columns (not just sensitive ones) must be dropped."""
        adapter, conn = make_adapter_with_pool()
        cols = [
            FakeRecord(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "id",
                    "data_type": "integer",
                    "udt_name": "int4",
                    "is_nullable": "NO",
                    "column_default": None,
                    "ordinal_position": 1,
                }
            ),
            FakeRecord(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "internal_notes",
                    "data_type": "text",
                    "udt_name": "text",
                    "is_nullable": "YES",
                    "column_default": None,
                    "ordinal_position": 2,
                }
            ),
        ]
        conn.fetch.side_effect = [
            _SCHEMA_ROW,
            [
                FakeRecord(
                    {
                        "table_schema": "public",
                        "table_name": "users",
                        "table_type": "BASE TABLE",
                    }
                )
            ],
            cols,
            [],
            [],
        ]
        p = PrivacySettings(
            excluded_columns=["public.users.internal_notes"],
            include_sample_values=False,
            include_column_comments=False,
            include_row_counts=False,
        )

        result = await adapter.introspect(p)

        col_names = [c.name for c in result.tables[0].columns]
        assert "id" in col_names
        assert "internal_notes" not in col_names

    async def test_row_counts_attached_when_enabled(self):
        adapter, conn = make_adapter_with_pool()
        conn.fetch.side_effect = [
            _SCHEMA_ROW,
            [
                FakeRecord(
                    {
                        "table_schema": "public",
                        "table_name": "users",
                        "table_type": "BASE TABLE",
                    }
                )
            ],
            [
                FakeRecord(
                    {
                        "table_schema": "public",
                        "table_name": "users",
                        "column_name": "id",
                        "data_type": "integer",
                        "udt_name": "int4",
                        "is_nullable": "NO",
                        "column_default": None,
                        "ordinal_position": 1,
                    }
                )
            ],
            [],  # pks
            [],  # fks
            # include_column_comments=False → no comments fetch
            [FakeRecord({"schemaname": "public", "relname": "users", "n_live_tup": 42})],
        ]
        p = PrivacySettings(
            include_sample_values=False, include_column_comments=False, include_row_counts=True
        )

        result = await adapter.introspect(p)

        assert result.tables[0].row_count_approx == 42

    async def test_not_connected_raises_runtime_error(self):
        adapter = PostgreSQLDataSource()  # pool never set
        with pytest.raises(RuntimeError, match="Not connected"):
            await adapter.introspect()


# ── execute_query ─────────────────────────────────────────────────────────────


class TestExecuteQuery:
    async def test_returns_columns_and_rows(self):
        adapter, conn = make_adapter_with_pool()
        conn.fetch = AsyncMock(
            return_value=[
                FakeRecord({"id": 1, "name": "Alice"}),
                FakeRecord({"id": 2, "name": "Bob"}),
            ]
        )

        result = await adapter.execute_query("SELECT id, name FROM users")

        assert result.columns == ["id", "name"]
        assert result.row_count == 2
        assert result.rows[0] == [1, "Alice"]
        assert result.rows[1] == [2, "Bob"]
        assert result.truncated is False

    async def test_empty_result(self):
        adapter, conn = make_adapter_with_pool()
        conn.fetch = AsyncMock(return_value=[])

        result = await adapter.execute_query("SELECT * FROM empty_table")

        assert result.columns == []
        assert result.row_count == 0
        assert result.rows == []

    async def test_truncation_when_over_max_rows(self):
        adapter, conn = make_adapter_with_pool()
        conn.fetch = AsyncMock(return_value=[FakeRecord({"id": i}) for i in range(10)])

        result = await adapter.execute_query("SELECT id FROM t", max_rows=5)

        assert result.truncated is True
        assert result.row_count == 5
        assert len(result.rows) == 5

    async def test_null_values_serialised_as_none(self):
        adapter, conn = make_adapter_with_pool()
        conn.fetch = AsyncMock(return_value=[FakeRecord({"id": 1, "note": None})])

        result = await adapter.execute_query("SELECT id, note FROM t")

        assert result.rows[0] == [1, None]

    async def test_column_type_unknown_for_null_first_row(self):
        """Bug fix #7: NoneType → 'unknown' for NULL values in the first row."""
        adapter, conn = make_adapter_with_pool()
        conn.fetch = AsyncMock(return_value=[FakeRecord({"id": None})])

        result = await adapter.execute_query("SELECT id FROM t")

        assert result.column_types == ["unknown"]

    async def test_column_type_for_non_null_values(self):
        adapter, conn = make_adapter_with_pool()
        conn.fetch = AsyncMock(return_value=[FakeRecord({"id": 1, "name": "Alice"})])

        result = await adapter.execute_query("SELECT id, name FROM t")

        assert result.column_types == ["int", "str"]

    async def test_execution_time_is_non_negative(self):
        adapter, conn = make_adapter_with_pool()
        conn.fetch = AsyncMock(return_value=[FakeRecord({"n": 1})])

        result = await adapter.execute_query("SELECT 1 AS n")

        assert result.execution_time_ms >= 0.0

    async def test_statement_timeout_set_locally(self):
        """Bug fix #2: SET LOCAL used inside a transaction for pool safety."""
        adapter, conn = make_adapter_with_pool()
        conn.fetch = AsyncMock(return_value=[])

        await adapter.execute_query("SELECT 1", timeout=15)

        # The execute call should set LOCAL statement_timeout (15 * 1000 ms)
        conn.execute.assert_called_once_with("SET LOCAL statement_timeout = 15000")

    async def test_not_connected_raises_runtime_error(self):
        adapter = PostgreSQLDataSource()
        with pytest.raises(RuntimeError, match="Not connected"):
            await adapter.execute_query("SELECT 1")


# ── disconnect ────────────────────────────────────────────────────────────────


class TestDisconnect:
    async def test_clears_reference_without_closing_pool(self):
        # disconnect() releases the adapter's reference but leaves the pool in the
        # module-level cache for reuse. evict_pool() is responsible for actually closing.
        adapter, _conn = make_adapter_with_pool()
        pool = adapter._pool

        await adapter.disconnect()

        pool.close.assert_not_called()
        assert adapter._pool is None

    async def test_disconnect_when_not_connected_is_safe(self):
        adapter = PostgreSQLDataSource()  # pool is None
        await adapter.disconnect()  # must not raise


# ── _quote_pg_ident ────────────────────────────────────────────────────────────


class TestQuotePgIdent:
    def test_simple_identifier(self):
        assert _quote_pg_ident("my_table") == '"my_table"'

    def test_identifier_with_spaces(self):
        assert _quote_pg_ident("my table") == '"my table"'

    def test_identifier_with_embedded_double_quote(self):
        assert _quote_pg_ident('bad"name') == '"bad""name"'

    def test_identifier_with_multiple_double_quotes(self):
        assert _quote_pg_ident('a"b"c') == '"a""b""c"'

    def test_empty_identifier(self):
        assert _quote_pg_ident("") == '""'


# ── PROD-1: close_all_pools ───────────────────────────────────────────────────


class TestCloseAllPools:
    """close_all_pools() must drain _pool_cache and close every pool."""

    @pytest.fixture(autouse=True)
    def clear_pool_cache(self):
        _pg_module._pool_cache.clear()
        yield
        _pg_module._pool_cache.clear()

    async def test_all_pools_closed_and_cache_emptied(self):
        from app.datasources.adapters.postgresql import close_all_pools

        pool_a = MockPool(MockConnection())
        pool_b = MockPool(MockConnection())
        _pg_module._pool_cache["key_a"] = pool_a
        _pg_module._pool_cache["key_b"] = pool_b

        await close_all_pools()

        pool_a.close.assert_called_once()
        pool_b.close.assert_called_once()
        assert _pg_module._pool_cache == {}

    async def test_empty_cache_is_a_noop(self):
        """Calling close_all_pools on an empty cache must not raise."""
        from app.datasources.adapters.postgresql import close_all_pools

        await close_all_pools()  # should complete without error

    async def test_idempotent_second_call(self):
        """Second call on an already-emptied cache must not raise."""
        from app.datasources.adapters.postgresql import close_all_pools

        pool = MockPool(MockConnection())
        _pg_module._pool_cache["k"] = pool

        await close_all_pools()
        await close_all_pools()  # cache is empty now — must not raise

        pool.close.assert_called_once()


# ── PROD-3: test_connection timeout ──────────────────────────────────────────


class TestTestConnectionTimeout:
    """asyncpg.connect() must be called with timeout=_TEST_CONNECT_TIMEOUT_S."""

    _cfg: ClassVar[dict[str, object]] = {
        "host": "localhost",
        "port": 5432,
        "database": "db",
        "username": "u",
        "password": "p",
    }

    async def test_timeout_kwarg_passed_to_connect(self):
        from app.datasources.adapters.postgresql import _TEST_CONNECT_TIMEOUT_S

        mock_conn = MockConnection(version="PostgreSQL 15.0")
        connect_mock = AsyncMock(return_value=mock_conn)
        with patch("asyncpg.connect", new=connect_mock):
            await PostgreSQLDataSource().test_connection(self._cfg)

        _, kwargs = connect_mock.call_args
        assert "timeout" in kwargs
        assert kwargs["timeout"] == _TEST_CONNECT_TIMEOUT_S

    async def test_timeout_error_returns_failure(self):
        """asyncio.TimeoutError from connect must map to a failure result, not a crash."""
        import asyncio

        with patch("asyncpg.connect", new=AsyncMock(side_effect=asyncio.TimeoutError)):
            result = await PostgreSQLDataSource().test_connection(self._cfg)

        assert result.success is False


# ── PROD-4: connect() pool-creation timeout ───────────────────────────────────


class TestConnectPoolTimeout:
    """asyncpg.create_pool must be wrapped with asyncio.wait_for so a stalled
    TCP/auth handshake cannot block the event loop indefinitely."""

    _cfg: ClassVar[dict[str, object]] = {
        "host": "localhost",
        "port": 5432,
        "database": "db",
        "username": "u",
        "password": "p",
    }

    @pytest.fixture(autouse=True)
    def clear_pool_cache(self):
        _pg_module._pool_cache.clear()
        yield
        _pg_module._pool_cache.clear()

    async def test_timeout_error_returns_failure(self):
        """asyncio.TimeoutError from create_pool must map to ConnectionResult(success=False)."""
        import asyncio

        with patch("asyncpg.create_pool", new=AsyncMock(side_effect=asyncio.TimeoutError)):
            result = await PostgreSQLDataSource().connect(self._cfg)

        assert result.success is False

    async def test_timeout_error_cleans_up_pool_ref(self):
        """Adapter's _pool attribute must be None after a timeout."""
        import asyncio

        adapter = PostgreSQLDataSource()
        with patch("asyncpg.create_pool", new=AsyncMock(side_effect=asyncio.TimeoutError)):
            await adapter.connect(self._cfg)

        assert adapter._pool is None

    async def test_timeout_error_does_not_populate_cache(self):
        """A failed pool creation must not leave a stale entry in _pool_cache."""
        import asyncio

        with patch("asyncpg.create_pool", new=AsyncMock(side_effect=asyncio.TimeoutError)):
            await PostgreSQLDataSource().connect(self._cfg)

        assert _pg_module._pool_cache == {}
