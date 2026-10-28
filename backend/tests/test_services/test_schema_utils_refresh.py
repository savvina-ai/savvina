# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""``refresh_user_schema_cache`` is the one live re-introspection behind the explicit
schema Refresh, Check Drift, Apply Drift and ``get_or_refresh_schema``'s miss path: it
stores the fresh schema and flushes the caches built from it as its ``flush`` policy
says (by default only when it changed)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.datasources.models import (
    ColumnInfo,
    ConnectionResult,
    DataSourceSchema,
    SchemaInfo,
    TableInfo,
)
from app.semantic.generator import SemanticModelGenerator
from app.services.schema_utils import (
    SchemaRefreshError,
    get_or_refresh_schema,
    refresh_user_schema_cache,
)


def _schema() -> DataSourceSchema:
    return DataSourceSchema(
        source_type="postgresql",
        schemas=[SchemaInfo(name="public")],
        tables=[
            TableInfo(
                catalog=None,
                schema_name="public",
                name="orders",
                table_type="table",
                columns=[ColumnInfo(name="id", data_type="integer", native_type="int4")],
            )
        ],
    )


def _conn() -> MagicMock:
    conn = MagicMock()
    conn.id = "conn-1"
    conn.source_type = "postgresql"
    conn.config_encrypted = b"x"
    conn.privacy_settings = None
    return conn


def _lookup(row) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    return result


def _savepoint(exit_error: BaseException | None = None) -> MagicMock:
    """``db.begin_nested()``: the savepoint's release flushes, which is where a
    concurrent insert's unique-key conflict surfaces."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=None)
    ctx.__aexit__ = AsyncMock(side_effect=exit_error, return_value=False)
    return ctx


def _db(existing_cache, *lookups) -> MagicMock:
    """The cache lookup returns ``existing_cache`` (then each of ``lookups``)."""
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[_lookup(existing_cache), *map(_lookup, lookups), MagicMock(), MagicMock()]
    )
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.begin_nested = MagicMock(return_value=_savepoint())
    return db


def _adapter(*, connect=None, introspect=None) -> MagicMock:
    adapter = MagicMock()
    adapter.connect = AsyncMock(return_value=connect or ConnectionResult(True, "ok"))
    adapter.introspect = introspect or AsyncMock(return_value=_schema())
    adapter.disconnect = AsyncMock()
    return adapter


async def _refresh(db, adapter, **kwargs) -> DataSourceSchema:
    with (
        patch("app.services.schema_utils.decrypt_value", return_value="{}"),
        patch("app.services.schema_utils.create_datasource", return_value=adapter),
    ):
        return await refresh_user_schema_cache(_conn(), "user-1", db, "key", **kwargs)


class TestRefreshUserSchemaCache:
    async def test_an_unchanged_schema_keeps_the_derived_caches(self) -> None:
        cache = MagicMock()
        cache.schema_hash = SemanticModelGenerator().compute_schema_hash(_schema())
        db = _db(cache)

        schema = await _refresh(db, _adapter())

        assert [t.name for t in schema.tables] == ["orders"]
        assert db.execute.await_count == 1  # the cache lookup, no deletes
        assert cache.schema_cache["tables"][0]["name"] == "orders"

    async def test_a_changed_schema_flushes_the_derived_caches(self) -> None:
        cache = MagicMock()
        cache.schema_hash = "stale"
        db = _db(cache)

        await _refresh(db, _adapter())

        assert db.execute.await_count == 3  # lookup + table embeddings + query cache
        assert cache.schema_hash == SemanticModelGenerator().compute_schema_hash(_schema())

    async def test_the_flush_deletes_the_embeddings_before_the_cached_answers(self) -> None:
        """The order ``_invalidate_connection_caches`` deletes in too: two transactions
        that lock the same rows in opposite orders can deadlock."""
        cache = MagicMock()
        cache.schema_hash = "stale"
        db = _db(cache)

        await _refresh(db, _adapter())

        deleted = [call.args[0].table.name for call in db.execute.await_args_list[1:]]
        assert deleted == ["table_embedding_cache", "query_cache"]

    async def test_a_first_refresh_creates_the_cache_and_flushes(self) -> None:
        db = _db(None)

        await _refresh(db, _adapter())

        db.add.assert_called_once()
        assert db.execute.await_count == 3

    async def test_flush_always_flushes_an_unchanged_schema(self) -> None:
        cache = MagicMock()
        cache.schema_hash = SemanticModelGenerator().compute_schema_hash(_schema())
        db = _db(cache)

        await _refresh(db, _adapter(), flush="always")

        assert db.execute.await_count == 3

    @pytest.mark.parametrize(
        "adapter",
        [
            _adapter(connect=ConnectionResult(False, "password authentication failed")),
            _adapter(introspect=AsyncMock(side_effect=OSError("connection refused"))),
        ],
        ids=["connect-refused", "introspect-raises"],
    )
    async def test_an_unreachable_source_raises_and_writes_nothing(self, adapter) -> None:
        db = _db(None)

        with pytest.raises(SchemaRefreshError):
            await _refresh(db, adapter)

        db.execute.assert_not_awaited()
        db.add.assert_not_called()
        adapter.disconnect.assert_awaited_once()

    async def test_flush_never_stores_without_flushing(self) -> None:
        db = _db(None)

        await _refresh(db, _adapter(), flush="never")

        db.add.assert_called_once()
        assert db.execute.await_count == 1  # the lookup only

    async def test_a_concurrent_first_refresh_is_updated_not_a_500(self) -> None:
        """Two first refreshes for one user and connection race on the unique key:
        the loser's insert fails at the savepoint, and it updates the winner's row."""
        winner = MagicMock()
        winner.schema_hash = "stale"
        db = _db(None, winner)
        db.begin_nested = MagicMock(
            return_value=_savepoint(IntegrityError("INSERT", {}, Exception("duplicate key")))
        )

        schema = await _refresh(db, _adapter())

        assert [t.name for t in schema.tables] == ["orders"]
        assert winner.schema_hash == SemanticModelGenerator().compute_schema_hash(_schema())
        assert winner.schema_cache["tables"][0]["name"] == "orders"
        # lookup, re-lookup after the conflict, then the flush (the hashes differed).
        assert db.execute.await_count == 4

    async def test_a_conflict_with_no_row_to_update_is_raised(self) -> None:
        """The unique key conflicted, yet the re-select finds nothing (the winner's
        row was deleted in between): there is nothing to update, so the error stands."""
        db = _db(None, None)
        db.begin_nested = MagicMock(
            return_value=_savepoint(IntegrityError("INSERT", {}, Exception("duplicate key")))
        )

        with pytest.raises(IntegrityError):
            await _refresh(db, _adapter())

    async def test_losing_the_race_to_the_same_schema_flushes_nothing(self) -> None:
        winner = MagicMock()
        winner.schema_hash = SemanticModelGenerator().compute_schema_hash(_schema())
        db = _db(None, winner)
        db.begin_nested = MagicMock(
            return_value=_savepoint(IntegrityError("INSERT", {}, Exception("duplicate key")))
        )

        await _refresh(db, _adapter())

        assert db.execute.await_count == 2


class TestGetOrRefreshSchema:
    """A cache hit is served as stored; a miss is a live refresh that stores the
    schema (with its hash) and flushes nothing."""

    async def _get(self, db, adapter=None, conn=None) -> DataSourceSchema:
        adapter = adapter or _adapter()
        with (
            patch("app.services.schema_utils.decrypt_value", return_value="{}"),
            patch("app.services.schema_utils.create_datasource", return_value=adapter),
        ):
            return await get_or_refresh_schema(conn or _conn(), "user-1", db, "key")

    async def test_a_hit_is_served_without_introspecting(self) -> None:
        from app.services.schema_utils import _schema_to_dict

        cache = MagicMock()
        cache.schema_cache = _schema_to_dict(_schema())
        db = _db(cache)
        adapter = _adapter()

        schema = await self._get(db, adapter)

        assert [t.name for t in schema.tables] == ["orders"]
        adapter.connect.assert_not_awaited()
        db.commit.assert_not_awaited()

    async def test_a_hit_has_the_policy_applied(self) -> None:
        from app.services.schema_utils import _schema_to_dict

        cache = MagicMock()
        cache.schema_cache = _schema_to_dict(_schema())
        conn = _conn()
        conn.privacy_settings = {"excluded_tables": ["public.orders"]}

        schema = await self._get(_db(cache), conn=conn)

        assert schema.tables == []

    @pytest.mark.parametrize("stored_privacy", [None, {}])
    async def test_a_hit_withholds_values_when_the_connection_has_no_privacy_settings(
        self, stored_privacy
    ) -> None:
        """No stored settings means the defaults (sample values off), not "no filter"."""
        from app.services.schema_utils import _schema_to_dict

        schema = _schema()
        schema.tables[0].columns.append(
            ColumnInfo(
                name="status",
                data_type="enum",
                native_type="enum('a','b')",
                sample_values=["a", "b"],
            )
        )
        cache = MagicMock()
        cache.schema_cache = _schema_to_dict(schema)
        conn = _conn()
        conn.privacy_settings = stored_privacy

        result = await self._get(_db(cache), conn=conn)

        status = next(c for c in result.tables[0].columns if c.name == "status")
        assert status.sample_values is None
        assert status.native_type == "enum(…)"

    async def test_a_miss_introspects_stores_the_hash_and_flushes_nothing(self) -> None:
        db = _db(None, None)
        adapter = _adapter()

        schema = await self._get(db, adapter)

        assert [t.name for t in schema.tables] == ["orders"]
        adapter.introspect.assert_awaited_once()
        stored = db.add.call_args.args[0]
        assert stored.schema_hash == SemanticModelGenerator().compute_schema_hash(_schema())
        # The hit lookup and the refresh's own lookup; no cache deletes.
        assert db.execute.await_count == 2
        db.commit.assert_awaited_once()

    async def test_a_row_without_a_schema_is_a_miss_that_updates_it(self) -> None:
        empty = MagicMock()
        empty.schema_cache = None
        empty.schema_hash = None
        db = _db(empty, empty)

        await self._get(db)

        assert empty.schema_cache["tables"][0]["name"] == "orders"
        db.add.assert_not_called()
        assert db.execute.await_count == 2
        db.commit.assert_awaited_once()

    async def test_a_refused_connection_raises_and_writes_nothing(self) -> None:
        db = _db(None, None)
        adapter = _adapter(connect=ConnectionResult(False, "password authentication failed"))

        with pytest.raises(SchemaRefreshError, match="password"):
            await self._get(db, adapter)

        db.add.assert_not_called()
        db.commit.assert_not_awaited()
