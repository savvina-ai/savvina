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
            "schema_pruning_enabled",
            "schema_pruning_top_k",
        }
        assert required.issubset(body.keys())

    async def test_no_secrets_in_response(self, http_client):
        resp = await http_client.get("/api/v1/settings")
        body = resp.json()
        assert "encryption_key" not in body
        assert "anthropic_api_key" not in body
        assert "openai_api_key" not in body

    async def test_numeric_fields_are_positive(self, http_client):
        resp = await http_client.get("/api/v1/settings")
        body = resp.json()
        assert body["default_query_timeout"] > 0
        assert body["default_row_limit"] > 0

    async def test_threshold_in_range(self, http_client):
        resp = await http_client.get("/api/v1/settings")
        body = resp.json()
        assert 0.0 <= body["semantic_similarity_threshold"] <= 1.0


class TestUpdateSettings:
    @pytest.fixture(autouse=True)
    def _override_db(self):
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
            "schema_pruning_enabled",
            "schema_pruning_top_k",
        }
        assert required.issubset(resp.json().keys())
