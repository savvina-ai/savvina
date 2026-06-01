# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""
Verify that introspect(), get_column_statistics(), and get_sample_values() never
issue queries that perform full sequential scans on user data tables.

OVERHEAD CONTRACT:
- introspect() must only read catalog/pg_stats tables, never user tables
- get_column_statistics() must only query pg_stats
- get_sample_values() must use TABLESAMPLE when pg_stats has no data (never bare SELECT DISTINCT)
"""

import pytest

from app.datasources.adapters.postgresql import PostgreSQLDataSource
from app.datasources.models import PrivacySettings
from tests.conftest import FakeRecord, MockConnection, MockPool

# ── Helpers ───────────────────────────────────────────────────────────────────


def make_adapter() -> tuple[PostgreSQLDataSource, MockConnection]:
    adapter = PostgreSQLDataSource()
    conn = MockConnection()
    pool = MockPool(conn)
    adapter._pool = pool
    return adapter, conn


def _introspect_side_effect_with_stats() -> list:
    """6 fetch calls: schemas, tables, columns, PKs, FKs, pg_stats."""
    return [
        # 1. schemas
        [FakeRecord({"schema_name": "public"})],
        # 2. tables
        [
            FakeRecord(
                {
                    "table_schema": "public",
                    "table_name": "orders",
                    "table_type": "BASE TABLE",
                }
            )
        ],
        # 3. columns
        [
            FakeRecord(
                {
                    "table_schema": "public",
                    "table_name": "orders",
                    "column_name": "status",
                    "data_type": "text",
                    "udt_name": "text",
                    "is_nullable": "YES",
                    "column_default": None,
                    "ordinal_position": 1,
                }
            )
        ],
        # 4. PKs
        [],
        # 5. FKs
        [],
        # 6. pg_stats bulk fetch (the new catalog-safe sample values query)
        [
            FakeRecord(
                {
                    "schemaname": "public",
                    "tablename": "orders",
                    "attname": "status",
                    "most_common_vals": ["pending", "shipped", "delivered"],
                }
            )
        ],
    ]


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestIntrospectOnlyReadsCatalog:
    async def test_no_user_table_queries_issued(self):
        """All fetch calls during introspect() must target catalog or pg_stats tables."""
        adapter, conn = make_adapter()
        conn.fetch.side_effect = _introspect_side_effect_with_stats()

        await adapter.introspect(
            PrivacySettings(
                include_sample_values=True,
                include_column_comments=False,
                include_row_counts=False,
            )
        )

        # Collect all SQL strings from every fetch call
        sql_calls = [str(call.args[0]) for call in conn.fetch.call_args_list]

        for sql in sql_calls:
            sql_lower = sql.lower().strip()
            # Queries against user tables directly are forbidden.
            # The only fetch that is allowed to reference a specific table is pg_stats.
            has_bare_user_table = (
                'from "public"."orders"' in sql_lower or 'from "public"."users"' in sql_lower
            )
            assert not has_bare_user_table, (
                f"introspect() issued a query directly against a user table:\n{sql}"
            )

    async def test_pg_stats_queried_for_sample_values(self):
        """When include_sample_values=True, introspect() must use pg_stats."""
        adapter, conn = make_adapter()
        conn.fetch.side_effect = _introspect_side_effect_with_stats()

        await adapter.introspect(
            PrivacySettings(
                include_sample_values=True,
                include_column_comments=False,
                include_row_counts=False,
            )
        )

        sql_calls = [str(call.args[0]) for call in conn.fetch.call_args_list]
        assert any("pg_stats" in s.lower() for s in sql_calls), (
            "Expected at least one query targeting pg_stats for sample values"
        )

    async def test_sample_values_populated_from_pg_stats(self):
        """Sample values from pg_stats must be attached to the correct column."""
        adapter, conn = make_adapter()
        conn.fetch.side_effect = _introspect_side_effect_with_stats()

        schema = await adapter.introspect(
            PrivacySettings(
                include_sample_values=True,
                include_column_comments=False,
                include_row_counts=False,
            )
        )

        orders = next(t for t in schema.tables if t.name == "orders")
        status_col = next(c for c in orders.columns if c.name == "status")
        assert status_col.sample_values == ["pending", "shipped", "delivered"]

    async def test_sample_values_disabled_means_no_pg_stats_query(self):
        """When include_sample_values=False, pg_stats must NOT be queried."""
        adapter, conn = make_adapter()
        # Only 5 fetch calls expected (no pg_stats)
        conn.fetch.side_effect = [
            [FakeRecord({"schema_name": "public"})],
            [
                FakeRecord(
                    {
                        "table_schema": "public",
                        "table_name": "orders",
                        "table_type": "BASE TABLE",
                    }
                )
            ],
            [
                FakeRecord(
                    {
                        "table_schema": "public",
                        "table_name": "orders",
                        "column_name": "status",
                        "data_type": "text",
                        "udt_name": "text",
                        "is_nullable": "YES",
                        "column_default": None,
                        "ordinal_position": 1,
                    }
                )
            ],
            [],  # PKs
            [],  # FKs
        ]

        await adapter.introspect(
            PrivacySettings(
                include_sample_values=False,
                include_column_comments=False,
                include_row_counts=False,
            )
        )

        sql_calls = [str(call.args[0]) for call in conn.fetch.call_args_list]
        assert not any("pg_stats" in s.lower() for s in sql_calls), (
            "pg_stats must not be queried when include_sample_values=False"
        )


class TestGetColumnStatisticsReadsPgStatsOnly:
    async def test_queries_pg_stats(self):
        """get_column_statistics() must query only pg_stats."""
        adapter, conn = make_adapter()
        conn.fetchrow.return_value = FakeRecord(
            {
                "n_distinct": -0.05,
                "null_frac": 0.0,
                "avg_width": 8,
                "most_common_vals": ["active", "inactive"],
                "most_common_freqs": [0.8, 0.2],
                "correlation": 0.1,
            }
        )

        await adapter.get_column_statistics("public", "users", "status")

        assert conn.fetchrow.call_count == 1
        sql = str(conn.fetchrow.call_args.args[0])
        assert "pg_stats" in sql.lower()
        assert conn.fetch.call_count == 0

    async def test_returns_empty_dict_when_no_stats(self):
        """Returns {} when pg_stats has no row for this column."""
        adapter, conn = make_adapter()
        conn.fetchrow.return_value = None

        result = await adapter.get_column_statistics("public", "users", "email")

        assert result == {}

    async def test_returns_all_stat_fields(self):
        """All six statistic fields are returned."""
        adapter, conn = make_adapter()
        conn.fetchrow.return_value = FakeRecord(
            {
                "n_distinct": 3.0,
                "null_frac": 0.01,
                "avg_width": 6,
                "most_common_vals": ["a", "b"],
                "most_common_freqs": [0.6, 0.4],
                "correlation": 0.5,
            }
        )

        result = await adapter.get_column_statistics("public", "orders", "status")

        assert result["n_distinct"] == 3.0
        assert result["null_frac"] == 0.01
        assert result["avg_width"] == 6
        assert result["most_common_vals"] == ["a", "b"]
        assert result["most_common_freqs"] == [0.6, 0.4]
        assert result["correlation"] == 0.5

    async def test_not_connected_raises(self):
        adapter = PostgreSQLDataSource()  # no pool
        with pytest.raises(RuntimeError, match="Not connected"):
            await adapter.get_column_statistics("public", "users", "id")


class TestGetSampleValuesUsesSafePaths:
    async def test_pg_stats_path_used_when_data_available(self):
        """When pg_stats has most_common_vals, fetch() must NOT be called."""
        adapter, conn = make_adapter()
        conn.fetchrow.return_value = FakeRecord(
            {
                "most_common_vals": ["pending", "shipped", "delivered"],
            }
        )

        result = await adapter.get_sample_values("public", "orders", "status")

        assert result == ["pending", "shipped", "delivered"]
        assert conn.fetchrow.call_count == 1
        sql = str(conn.fetchrow.call_args.args[0])
        assert "pg_stats" in sql.lower()
        # fetch() should NOT have been called (no TABLESAMPLE fallback needed)
        assert conn.fetch.call_count == 0

    async def test_tablesample_fallback_when_pg_stats_empty(self):
        """When pg_stats returns nothing, fallback must use TABLESAMPLE."""
        adapter, conn = make_adapter()
        conn.fetchrow.return_value = None  # no pg_stats data
        conn.fetch.return_value = [
            FakeRecord({"status": "active"}),
            FakeRecord({"status": "inactive"}),
        ]

        await adapter.get_sample_values("public", "users", "status")

        assert conn.fetch.call_count == 1
        sql = str(conn.fetch.call_args.args[0])
        assert "tablesample" in sql.lower(), (
            "Fallback query must use TABLESAMPLE, not a full sequential scan"
        )

    async def test_no_bare_select_distinct_without_tablesample(self):
        """The fallback query must never be a bare SELECT DISTINCT (no TABLESAMPLE)."""
        adapter, conn = make_adapter()
        conn.fetchrow.return_value = None
        conn.fetch.return_value = []

        await adapter.get_sample_values("public", "users", "name")

        sql = str(conn.fetch.call_args.args[0])
        sql_lower = sql.lower()
        # If SELECT DISTINCT is present, TABLESAMPLE must also be present
        if "select distinct" in sql_lower:
            assert "tablesample" in sql_lower, (
                "SELECT DISTINCT without TABLESAMPLE is a full table scan — forbidden"
            )

    async def test_respects_limit_parameter(self):
        """Limit is applied to the pg_stats result."""
        adapter, conn = make_adapter()
        conn.fetchrow.return_value = FakeRecord(
            {
                "most_common_vals": ["a", "b", "c", "d", "e", "f", "g"],
            }
        )

        result = await adapter.get_sample_values("public", "users", "code", limit=3)

        assert result == ["a", "b", "c"]

    async def test_returns_empty_list_on_exception(self):
        """Exceptions in TABLESAMPLE fallback must return [] gracefully."""
        adapter, conn = make_adapter()
        conn.fetchrow.return_value = None
        conn.fetch.side_effect = Exception("column type unsortable")

        result = await adapter.get_sample_values("public", "users", "json_col")

        assert result == []

    async def test_not_connected_raises(self):
        adapter = PostgreSQLDataSource()  # no pool
        with pytest.raises(RuntimeError, match="Not connected"):
            await adapter.get_sample_values("public", "users", "id")
