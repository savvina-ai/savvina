# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""``routers/_utils``: the connection cache purge."""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from app.routers._utils import _invalidate_connection_caches, lock_and_reread_connection

from .conftest import MockResult, _mock_db


def _tables_deleted(db) -> set[str]:
    return set(_tables_deleted_in_order(db))


def _tables_deleted_in_order(db) -> list[str]:
    return [call.args[0].table.name for call in db.execute.await_args_list]


class TestInvalidateConnectionCaches:
    async def test_purges_the_query_schema_and_embedding_caches(self) -> None:
        db = _mock_db(MockResult(), MockResult(), MockResult())
        await _invalidate_connection_caches("conn-1", db)
        assert _tables_deleted(db) == {"query_cache", "user_schema_caches", "table_embedding_cache"}

    async def test_keep_schema_spares_the_schema_cache(self) -> None:
        """For a caller that has just stored a live introspection (apply-drift): deleting
        it would throw the fresh schema away and 404 the page's next schema read."""
        db = _mock_db(MockResult(), MockResult())
        await _invalidate_connection_caches("conn-1", db, keep_schema=True)
        assert _tables_deleted(db) == {"query_cache", "table_embedding_cache"}

    async def test_deletes_in_the_order_the_schema_refresh_writes(self) -> None:
        """``refresh_user_schema_cache`` writes the schema cache, then deletes the table
        embeddings, then the cached answers. Two transactions that lock the same rows in
        opposite orders can deadlock, so the purge takes them in that order too."""
        db = _mock_db(MockResult(), MockResult(), MockResult())
        await _invalidate_connection_caches("conn-1", db)
        assert _tables_deleted_in_order(db) == [
            "user_schema_caches",
            "table_embedding_cache",
            "query_cache",
        ]

    async def test_keep_schema_deletes_in_the_order_the_schema_refresh_writes(self) -> None:
        db = _mock_db(MockResult(), MockResult())
        await _invalidate_connection_caches("conn-1", db, keep_schema=True)
        assert _tables_deleted_in_order(db) == ["table_embedding_cache", "query_cache"]


class TestLockAndRereadConnection:
    async def test_takes_no_key_update_so_fk_inserts_are_not_blocked(self) -> None:
        db = _mock_db(MockResult(single=object()))
        await lock_and_reread_connection("conn-1", db)
        sql = str(db.execute.await_args.args[0].compile(dialect=postgresql.dialect()))
        assert "FOR NO KEY UPDATE" in sql
