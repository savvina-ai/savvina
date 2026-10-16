# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""``routers/_utils``: the connection cache purge."""

from __future__ import annotations

from app.routers._utils import _invalidate_connection_caches

from .conftest import MockResult, _mock_db


def _tables_deleted(db) -> set[str]:
    return {call.args[0].table.name for call in db.execute.await_args_list}


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
