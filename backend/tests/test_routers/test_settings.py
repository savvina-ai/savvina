# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for GET /api/v1/settings and PUT /api/v1/settings."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database import get_db
from app.main import app


def _make_settings_db(initial_rows: list | None = None):
    """Return a mock AsyncSession for settings endpoints.

    Captures db.merge(AppSetting) calls and returns them on subsequent
    db.execute(select(AppSetting)) calls, simulating a real DB round-trip.
    """
    _state: dict[str, str] = {}
    if initial_rows:
        for row in initial_rows:
            _state[row.key] = row.value

    async def _merge(obj: object) -> object:
        if hasattr(obj, "key") and hasattr(obj, "value"):
            _state[obj.key] = str(obj.value)
        return obj

    async def _execute(stmt: object) -> object:
        rows = []
        for key, value in _state.items():
            m = MagicMock()
            m.key = key
            m.value = value
            rows.append(m)
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        return result

    mock = MagicMock()
    mock.execute = AsyncMock(side_effect=_execute)
    mock.merge = AsyncMock(side_effect=_merge)
    mock.commit = AsyncMock()
    mock.scalar = AsyncMock(return_value=0)
    mock.refresh = AsyncMock()
    mock.add = MagicMock()
    mock.delete = AsyncMock()
    txn = AsyncMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    mock.begin = MagicMock(return_value=txn)
    return mock


class TestGetSettings:
    @pytest.fixture(autouse=True)
    def _override_db(self):
        mock = _make_settings_db()
        app.dependency_overrides[get_db] = lambda: mock
        yield
        app.dependency_overrides.pop(get_db, None)

    async def test_returns_200(self, http_client):
        resp = await http_client.get("/api/v1/settings")
        assert resp.status_code == 200

    async def test_response_has_required_fields(self, http_client):
        resp = await http_client.get("/api/v1/settings")
        body = resp.json()
        required = {
            "app_name",
            "debug",
            "log_level",
            "ollama_base_url",
            "default_query_timeout",
            "default_row_limit",
            "cache_enabled",
            "semantic_similarity_threshold",
            "embedding_model",
            "db_pool_size",
            "db_max_overflow",
            "schema_pruning_enabled",
            "schema_pruning_top_k",
            "bcrypt_rounds",
        }
        assert required.issubset(body.keys())

    async def test_no_secrets_in_response(self, http_client):
        resp = await http_client.get("/api/v1/settings")
        body = resp.json()
        assert "encryption_key" not in body

    async def test_numeric_fields_are_positive(self, http_client):
        resp = await http_client.get("/api/v1/settings")
        body = resp.json()
        assert body["default_query_timeout"] > 0
        assert body["default_row_limit"] > 0

    async def test_threshold_in_range(self, http_client):
        resp = await http_client.get("/api/v1/settings")
        body = resp.json()
        assert 0.0 <= body["semantic_similarity_threshold"] <= 1.0

    async def test_bcrypt_rounds_defaults_to_constant(self, http_client):
        """With no persisted row the reported work factor is the one hashing uses."""
        from app.config import DEFAULT_BCRYPT_ROUNDS

        resp = await http_client.get("/api/v1/settings")
        assert resp.json()["bcrypt_rounds"] == DEFAULT_BCRYPT_ROUNDS


def test_settings_update_fields_match_mutable_parsers():
    """Every PUT field needs a parser, or the persisted row is unreadable at boot."""
    from app.config import MUTABLE_SETTING_PARSERS
    from app.schemas.settings import SettingsUpdate

    assert set(SettingsUpdate.model_fields) == set(MUTABLE_SETTING_PARSERS)


@pytest.fixture
def _restore_singleton():
    """Snapshot and restore the process-level Settings fields a PUT now writes through.

    PUT /settings mutates the cached Settings object so hot-path readers see the change
    without a restart; without this the mutation would leak into every later test.
    """
    from app.config import SINGLETON_SETTING_PARSERS, get_settings

    s = get_settings()
    saved = {key: getattr(s, key) for key in SINGLETON_SETTING_PARSERS}
    yield
    for key, value in saved.items():
        setattr(s, key, value)


class TestUpdateSettings:
    @pytest.fixture(autouse=True)
    def _override_db(self, _restore_singleton):
        mock = _make_settings_db()
        app.dependency_overrides[get_db] = lambda: mock
        yield
        app.dependency_overrides.pop(get_db, None)

    async def test_returns_200(self, http_client):
        resp = await http_client.put(
            "/api/v1/settings",
            json={"default_row_limit": 500},
        )
        assert resp.status_code == 200

    async def test_updated_value_reflected_in_response(self, http_client):
        resp = await http_client.put(
            "/api/v1/settings",
            json={"default_row_limit": 250},
        )
        assert resp.json()["default_row_limit"] == 250

    async def test_can_toggle_cache_enabled(self, http_client):
        resp = await http_client.put(
            "/api/v1/settings",
            json={"cache_enabled": False},
        )
        assert resp.status_code == 200
        assert resp.json()["cache_enabled"] is False

    async def test_invalid_threshold_returns_422(self, http_client):
        resp = await http_client.put(
            "/api/v1/settings",
            json={"semantic_similarity_threshold": 1.5},
        )
        assert resp.status_code == 422

    async def test_zero_row_limit_returns_422(self, http_client):
        resp = await http_client.put(
            "/api/v1/settings",
            json={"default_row_limit": 0},
        )
        assert resp.status_code == 422

    async def test_partial_update_leaves_other_fields_unchanged(self, http_client):
        from app.config import get_settings

        original_timeout = get_settings().default_query_timeout
        await http_client.put("/api/v1/settings", json={"default_row_limit": 999})
        after = (await http_client.get("/api/v1/settings")).json()
        assert after["default_query_timeout"] == original_timeout

    async def test_can_update_pool_size(self, http_client):
        resp = await http_client.put("/api/v1/settings", json={"db_pool_size": 5})
        assert resp.status_code == 200
        assert resp.json()["db_pool_size"] == 5

    async def test_invalid_pool_size_returns_422(self, http_client):
        resp = await http_client.put("/api/v1/settings", json={"db_pool_size": 0})
        assert resp.status_code == 422

    async def test_can_update_max_overflow(self, http_client):
        resp = await http_client.put("/api/v1/settings", json={"db_max_overflow": 50})
        assert resp.status_code == 200
        assert resp.json()["db_max_overflow"] == 50

    async def test_can_update_bcrypt_rounds(self, http_client):
        resp = await http_client.put("/api/v1/settings", json={"bcrypt_rounds": 14})
        assert resp.status_code == 200
        assert resp.json()["bcrypt_rounds"] == 14

    async def test_invalid_bcrypt_rounds_returns_422(self, http_client):
        resp = await http_client.put("/api/v1/settings", json={"bcrypt_rounds": 9})
        assert resp.status_code == 422

    async def test_can_toggle_schema_pruning(self, http_client):
        resp = await http_client.put(
            "/api/v1/settings",
            json={"schema_pruning_enabled": False},
        )
        assert resp.status_code == 200
        assert resp.json()["schema_pruning_enabled"] is False

    async def test_invalid_top_k_returns_422(self, http_client):
        resp = await http_client.put(
            "/api/v1/settings",
            json={"schema_pruning_top_k": 2},
        )
        assert resp.status_code == 422

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("default_query_timeout", 60),
            ("default_row_limit", 250),
            ("cache_enabled", False),
            ("cache_max_age_days", 7),
            ("cache_max_age_days", 0),  # ge=0: "TTL off" must persist, not be dropped
            ("semantic_similarity_threshold", 0.95),
            ("db_pool_size", 5),
            ("db_max_overflow", 50),
            ("schema_pruning_enabled", False),
            ("schema_pruning_top_k", 8),
            ("bcrypt_rounds", 14),
        ],
    )
    async def test_every_mutable_key_round_trips_through_get(self, http_client, key, value):
        """PUT persists the typed value as a string; GET parses it back to the same value."""
        resp = await http_client.put("/api/v1/settings", json={key: value})
        assert resp.status_code == 200
        assert (await http_client.get("/api/v1/settings")).json()[key] == value

    async def test_response_has_all_required_fields(self, http_client):
        resp = await http_client.put(
            "/api/v1/settings",
            json={"default_query_timeout": 60},
        )
        required = {
            "app_name",
            "debug",
            "log_level",
            "ollama_base_url",
            "default_query_timeout",
            "default_row_limit",
            "cache_enabled",
            "semantic_similarity_threshold",
            "embedding_model",
            "db_pool_size",
            "db_max_overflow",
            "schema_pruning_enabled",
            "schema_pruning_top_k",
            "bcrypt_rounds",
        }
        assert required.issubset(resp.json().keys())


class TestUpdateWritesThroughToSingleton:
    """A saved setting must reach the hot path, not just the DB-backed response.

    `GET /settings` reads `app_settings`, but the query pipeline branches on the cached
    `Settings` object (`settings.cache_enabled` in `services/pipeline.py`), which the
    lifespan seeds once at boot. Without a write-through the toggle showed the new value
    while the pipeline kept using the boot-time one until the next restart — the cache
    read as enabled in the UI and never ran a lookup.
    """

    @pytest.fixture(autouse=True)
    def _override_db(self, _restore_singleton):
        mock = _make_settings_db()
        app.dependency_overrides[get_db] = lambda: mock
        yield
        app.dependency_overrides.pop(get_db, None)

    async def test_cache_enabled_reaches_the_singleton(self, http_client):
        from app.config import get_settings

        await http_client.put("/api/v1/settings", json={"cache_enabled": False})
        assert get_settings().cache_enabled is False

        await http_client.put("/api/v1/settings", json={"cache_enabled": True})
        assert get_settings().cache_enabled is True

    async def test_threshold_reaches_the_singleton_as_float(self, http_client):
        from app.config import get_settings

        await http_client.put("/api/v1/settings", json={"semantic_similarity_threshold": 0.95})
        assert get_settings().semantic_similarity_threshold == 0.95

    async def test_schema_pruning_reaches_the_singleton(self, http_client):
        from app.config import get_settings

        await http_client.put("/api/v1/settings", json={"schema_pruning_enabled": False})
        assert get_settings().schema_pruning_enabled is False

    async def test_bcrypt_rounds_is_not_set_on_the_singleton(self, http_client):
        """`Settings` has no bcrypt_rounds field — a setattr would raise and 500 the PUT."""
        from app.config import get_settings

        resp = await http_client.put("/api/v1/settings", json={"bcrypt_rounds": 13})
        assert resp.status_code == 200
        assert not hasattr(get_settings(), "bcrypt_rounds")

    async def test_cache_max_age_days_reaches_lookup_without_restart(self, http_client):
        """The shared QueryCache is built once and lru_cached in routers/chat.py, so the
        TTL has to be read live from the singleton on every lookup — a PUT after the
        cache was constructed must change the freshness window it queries with."""
        from unittest.mock import patch

        from app.cache.query_cache import QueryCache, _fresh_condition
        from app.config import get_settings

        cache = QueryCache("all-MiniLM-L6-v2", 0.9)  # constructed before the PUT

        await http_client.put("/api/v1/settings", json={"cache_max_age_days": 3})
        assert get_settings().cache_max_age_days == 3

        # An exact hit on the first execute() means the embedding model is never loaded.
        entry = MagicMock()
        entry.id = "entry-1"
        result = MagicMock()
        result.scalar_one_or_none.return_value = entry
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()
        with patch("app.cache.query_cache._fresh_condition", wraps=_fresh_condition) as fc:
            await cache.lookup("conn-1", "How many users?", db)
        fc.assert_called_once_with(3)
