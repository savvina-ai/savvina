# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for execute_query_stream() — the default batch-slice implementation and
the PostgreSQL asyncpg cursor override."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.datasources.models import QueryResult

# ── Helpers ────────────────────────────────────────────────────────────────────


async def _drain(gen) -> list[QueryResult]:
    return [r async for r in gen]


# ── Default implementation (BaseDataSource) ────────────────────────────────────


class TestDefaultExecuteQueryStream:
    """The default concrete method slices execute_query() results into batches."""

    async def test_single_batch_when_rows_fit(self):
        """Fewer rows than batch_size → one batch with all rows."""
        from app.datasources.adapters.postgresql import PostgreSQLDataSource

        adapter = PostgreSQLDataSource()
        result = QueryResult(
            columns=["id"],
            column_types=["integer"],
            rows=[[1], [2], [3]],
            row_count=3,
            truncated=False,
            execution_time_ms=5.0,
        )
        with patch.object(adapter, "execute_query", new=AsyncMock(return_value=result)):
            # Use the base class default by calling it through a concrete subclass
            # that hasn't overridden execute_query_stream (we'll call super explicitly)
            # For simplicity, test via BaseDataSource directly using a simple subclass
            pass

        # Test via a minimal concrete implementation instead
        from app.datasources.base import BaseDataSource

        class _MinimalDS(BaseDataSource):
            source_type = "test"
            display_name = "Test"
            query_dialect = "sql"
            icon = ""

            async def connect(self, config): ...  # type: ignore[override]
            async def disconnect(self): ...
            async def test_connection(self, config): ...  # type: ignore[override]
            async def introspect(self, privacy=None): ...  # type: ignore[override]
            async def get_sample_values(self, schema, table, column, limit=5): ...  # type: ignore[override]
            async def execute_query(self, query, timeout=30, max_rows=1000):  # noqa: ASYNC109
                return result  # type: ignore[override]

            def validate_query(self, query): ...  # type: ignore[override]
            def format_schema_for_llm(self, schema, privacy=None): ...  # type: ignore[override]
            def get_system_prompt_additions(self):
                return ""

            @classmethod
            def get_config_schema(cls):
                return {}

        ds = _MinimalDS()
        batches = await _drain(ds.execute_query_stream("SELECT 1", batch_size=10))
        assert len(batches) == 1
        assert batches[0].rows == [[1], [2], [3]]
        assert batches[0].columns == ["id"]

    async def test_multiple_batches_when_rows_exceed_batch_size(self):
        """More rows than batch_size → multiple batches."""
        from app.datasources.base import BaseDataSource

        all_rows = [[i] for i in range(7)]
        result = QueryResult(
            columns=["n"],
            column_types=["integer"],
            rows=all_rows,
            row_count=7,
            truncated=False,
            execution_time_ms=5.0,
        )

        class _DS(BaseDataSource):
            source_type = "t"
            display_name = "T"
            query_dialect = "sql"
            icon = ""

            async def connect(self, config): ...  # type: ignore[override]
            async def disconnect(self): ...
            async def test_connection(self, config): ...  # type: ignore[override]
            async def introspect(self, privacy=None): ...  # type: ignore[override]
            async def get_sample_values(self, schema, table, column, limit=5): ...  # type: ignore[override]
            async def execute_query(self, query, timeout=30, max_rows=1000):  # noqa: ASYNC109
                return result  # type: ignore[override]

            def validate_query(self, query): ...  # type: ignore[override]
            def format_schema_for_llm(self, schema, privacy=None): ...  # type: ignore[override]
            def get_system_prompt_additions(self):
                return ""

            @classmethod
            def get_config_schema(cls):
                return {}

        ds = _DS()
        batches = await _drain(ds.execute_query_stream("SELECT 1", batch_size=3))
        # 7 rows / batch_size=3 → 3 batches (3, 3, 1)
        assert len(batches) == 3
        combined = [row for b in batches for row in b.rows]
        assert combined == all_rows

    async def test_truncated_flag_propagates_to_all_batches(self):
        """If execute_query() returned truncated=True, all batches carry it."""
        from app.datasources.base import BaseDataSource

        result = QueryResult(
            columns=["x"],
            column_types=["integer"],
            rows=[[1], [2]],
            row_count=2,
            truncated=True,
            execution_time_ms=1.0,
        )

        class _DS(BaseDataSource):
            source_type = "t"
            display_name = "T"
            query_dialect = "sql"
            icon = ""

            async def connect(self, config): ...  # type: ignore[override]
            async def disconnect(self): ...
            async def test_connection(self, config): ...  # type: ignore[override]
            async def introspect(self, privacy=None): ...  # type: ignore[override]
            async def get_sample_values(self, schema, table, column, limit=5): ...  # type: ignore[override]
            async def execute_query(self, query, timeout=30, max_rows=1000):  # noqa: ASYNC109
                return result  # type: ignore[override]

            def validate_query(self, query): ...  # type: ignore[override]
            def format_schema_for_llm(self, schema, privacy=None): ...  # type: ignore[override]
            def get_system_prompt_additions(self):
                return ""

            @classmethod
            def get_config_schema(cls):
                return {}

        ds = _DS()
        batches = await _drain(ds.execute_query_stream("SELECT 1", batch_size=5))
        assert all(b.truncated for b in batches)


# ── PostgreSQL cursor override ─────────────────────────────────────────────────


class TestPostgreSQLExecuteQueryStream:
    """Tests for the asyncpg server-side cursor implementation."""

    def _make_record(self, **fields) -> MagicMock:
        r = MagicMock()
        r.keys.return_value = list(fields.keys())
        r.__getitem__ = lambda self, k: fields[k]
        return r

    def _make_pool(self, records: list) -> MagicMock:
        """Build a mock asyncpg pool whose cursor() yields the given records."""
        cursor_mock = MagicMock()

        async def _aiter():
            for rec in records:
                yield rec

        cursor_mock.__aiter__ = lambda self: _aiter()

        conn_mock = MagicMock()
        conn_mock.execute = AsyncMock()
        conn_mock.cursor = MagicMock(return_value=cursor_mock)

        # transaction context manager
        txn = MagicMock()
        txn.__aenter__ = AsyncMock(return_value=None)
        txn.__aexit__ = AsyncMock(return_value=False)
        conn_mock.transaction = MagicMock(return_value=txn)

        # pool.acquire() context manager
        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=conn_mock)
        acquire_ctx.__aexit__ = AsyncMock(return_value=False)

        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_ctx)
        return pool

    async def test_yields_batches_of_correct_size(self):
        from app.datasources.adapters.postgresql import PostgreSQLDataSource

        records = [self._make_record(id=i, val=f"v{i}") for i in range(5)]
        pool = self._make_pool(records)

        adapter = PostgreSQLDataSource()
        adapter._pool = pool  # type: ignore[assignment]

        batches = await _drain(adapter.execute_query_stream("SELECT id, val FROM t", batch_size=2))
        # 5 records / batch_size=2 → 3 batches (2, 2, 1)
        assert len(batches) == 3
        total_rows = sum(len(b.rows) for b in batches)
        assert total_rows == 5

    async def test_final_batch_sets_truncated_when_max_rows_hit(self):
        """When exactly max_rows are read, the final batch's truncated flag is True."""
        from app.datasources.adapters.postgresql import PostgreSQLDataSource

        records = [self._make_record(n=i) for i in range(5)]
        pool = self._make_pool(records)

        adapter = PostgreSQLDataSource()
        adapter._pool = pool  # type: ignore[assignment]

        # max_rows=5 means we hit the limit exactly — truncated should be True
        batches = await _drain(
            adapter.execute_query_stream("SELECT n FROM t", batch_size=10, max_rows=5)
        )
        assert batches[-1].truncated is True

    async def test_not_truncated_when_all_rows_fit(self):
        from app.datasources.adapters.postgresql import PostgreSQLDataSource

        records = [self._make_record(n=i) for i in range(3)]
        pool = self._make_pool(records)

        adapter = PostgreSQLDataSource()
        adapter._pool = pool  # type: ignore[assignment]

        batches = await _drain(
            adapter.execute_query_stream("SELECT n FROM t", batch_size=10, max_rows=100)
        )
        assert batches[-1].truncated is False

    async def test_raises_when_not_connected(self):
        import pytest

        from app.datasources.adapters.postgresql import PostgreSQLDataSource

        adapter = PostgreSQLDataSource()
        # _pool is None (not connected)
        with pytest.raises(RuntimeError, match="Not connected"):
            async for _ in adapter.execute_query_stream("SELECT 1"):
                pass

    async def test_columns_extracted_from_first_record(self):
        from app.datasources.adapters.postgresql import PostgreSQLDataSource

        records = [self._make_record(alpha=1, beta=2)]
        pool = self._make_pool(records)

        adapter = PostgreSQLDataSource()
        adapter._pool = pool  # type: ignore[assignment]

        batches = await _drain(adapter.execute_query_stream("SELECT alpha, beta FROM t"))
        assert batches[0].columns == ["alpha", "beta"]
