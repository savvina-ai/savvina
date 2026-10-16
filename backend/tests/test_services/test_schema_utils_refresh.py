# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""``refresh_user_schema_cache`` is the one live re-introspection behind both the
explicit schema Refresh and Check Drift: it stores the fresh schema and flushes the
caches built from it only when it changed, unless told to always flush."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.datasources.models import (
    ColumnInfo,
    ConnectionResult,
    DataSourceSchema,
    SchemaInfo,
    TableInfo,
)
from app.semantic.generator import SemanticModelGenerator
from app.services.schema_utils import SchemaRefreshError, refresh_user_schema_cache


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


def _db(existing_cache) -> MagicMock:
    lookup = MagicMock()
    lookup.scalar_one_or_none.return_value = existing_cache
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[lookup, MagicMock(), MagicMock()])
    db.add = MagicMock()
    return db


def _adapter(*, connect=None, introspect=None) -> MagicMock:
    adapter = MagicMock()
    adapter.connect = AsyncMock(return_value=connect or ConnectionResult(True, "ok"))
    adapter.introspect = introspect or AsyncMock(return_value=_schema())
    adapter.disconnect = AsyncMock()
    return adapter


async def _refresh(db, adapter, **kwargs) -> DataSourceSchema:
    with (
        patch("app.utils.encryption.decrypt_value", return_value="{}"),
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

    async def test_a_first_refresh_creates_the_cache_and_flushes(self) -> None:
        db = _db(None)

        await _refresh(db, _adapter())

        db.add.assert_called_once()
        assert db.execute.await_count == 3

    async def test_always_flush_flushes_an_unchanged_schema(self) -> None:
        cache = MagicMock()
        cache.schema_hash = SemanticModelGenerator().compute_schema_hash(_schema())
        db = _db(cache)

        await _refresh(db, _adapter(), always_flush=True)

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
