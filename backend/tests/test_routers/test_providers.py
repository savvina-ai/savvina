# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for /api/v1/providers endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from app.database import get_db
from app.main import app

from .conftest import MockResult, _mock_db


def _make_prov_config(**kwargs) -> MagicMock:
    """Return a MagicMock shaped like a ProviderConfig ORM row."""
    now = datetime.now(UTC)
    m = MagicMock()
    m.id = kwargs.get("id", "cfg-test-id")
    m.provider_type = kwargs.get("provider_type", "claude")
    m.display_name = kwargs.get("display_name", "Claude")
    m.api_key_encrypted = kwargs.get("api_key_encrypted")
    m.base_url = kwargs.get("base_url")
    m.model = kwargs.get("model", "")
    m.temperature = kwargs.get("temperature")
    m.max_tokens = kwargs.get("max_tokens")
    m.is_active = kwargs.get("is_active", False)
    m.updated_at = kwargs.get("updated_at", now)
    return m


# ── GET /api/v1/providers ────────────────────────────────────────────────────────


class TestListProviders:
    async def test_returns_200(self, http_client):
        db = _mock_db(MockResult(rows=[]))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/providers")
        assert resp.status_code == 200

    async def test_response_is_paginated(self, http_client):
        db = _mock_db(MockResult(rows=[]))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/providers")
        body = resp.json()
        assert "items" in body
        assert isinstance(body["items"], list)

    async def test_each_entry_has_required_fields(self, http_client):
        db = _mock_db(MockResult(rows=[]))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/providers")
        required = {
            "provider_type",
            "display_name",
            "is_configured",
            "is_healthy",
            "is_active",
            "current_model",
            "available_models",
        }
        for entry in resp.json()["items"]:
            assert required.issubset(entry.keys())

    async def test_ollama_not_configured_without_env_or_db(self, http_client, monkeypatch):
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        db = _mock_db(MockResult(rows=[]))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/providers")
        providers = {p["provider_type"]: p for p in resp.json()["items"]}
        if "ollama" in providers:
            assert providers["ollama"]["is_configured"] is False

    async def test_is_healthy_always_false_on_list(self, http_client):
        db = _mock_db(MockResult(rows=[]))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/providers")
        for p in resp.json()["items"]:
            assert p["is_healthy"] is False

    async def test_saved_config_appears_with_id(self, http_client):
        """Each saved config row is listed individually with its own id."""
        config = _make_prov_config(
            id="cfg-groq", provider_type="openai_compatible", display_name="Groq"
        )
        db = _mock_db(MockResult(rows=[config]))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/providers")
        entries = [e for e in resp.json()["items"] if e["provider_type"] == "openai_compatible"]
        assert any(e["id"] == "cfg-groq" for e in entries)


# ── POST /api/v1/providers/{provider_type} ───────────────────────────────────────


class TestCreateProviderConfig:
    async def test_returns_201(self, http_client):
        db = _mock_db(MockResult())
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/providers/claude",
            json={
                "api_key": "sk-ant-test",
                "model": "claude-3-5-sonnet-20241022",
                "is_active": True,
            },
        )
        assert resp.status_code == 201

    async def test_response_has_provider_type(self, http_client):
        db = _mock_db(MockResult())
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/providers/claude",
            json={"api_key": "sk-ant-test"},
        )
        assert resp.json()["provider_type"] == "claude"

    async def test_add_called_for_new_config(self, http_client):
        db = _mock_db(MockResult())
        app.dependency_overrides[get_db] = lambda: db
        await http_client.post(
            "/api/v1/providers/openai_compatible",
            json={"display_name": "Groq", "base_url": "https://api.groq.com/openai/v1"},
        )
        db.add.assert_called_once()

    async def test_multiple_openai_compatible_configs_allowed(self, http_client):
        """Creating a second openai_compatible config should also return 201."""
        db = _mock_db(MockResult())
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/providers/openai_compatible",
            json={
                "display_name": "Gemini",
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["provider_type"] == "openai_compatible"


# ── GET /api/v1/providers/{config_id} ────────────────────────────────────────────


class TestGetProvider:
    async def test_returns_200_for_known_config(self, http_client):
        config = _make_prov_config(id="cfg-1", provider_type="claude")
        db = _mock_db(MockResult(single=config))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/providers/cfg-1")
        assert resp.status_code == 200

    async def test_returns_provider_type(self, http_client):
        config = _make_prov_config(id="cfg-1", provider_type="claude")
        db = _mock_db(MockResult(single=config))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/providers/cfg-1")
        assert resp.json()["provider_type"] == "claude"

    async def test_returns_200_for_ollama(self, http_client):
        config = _make_prov_config(id="cfg-2", provider_type="ollama")
        db = _mock_db(MockResult(single=config))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/providers/cfg-2")
        assert resp.status_code == 200
        assert resp.json()["is_configured"] is True

    async def test_returns_404_for_unknown_config(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/providers/nonexistent-id")
        assert resp.status_code == 404


# ── PUT /api/v1/providers/{config_id}/config ─────────────────────────────────────


class TestUpdateProviderConfig:
    async def test_updates_existing_config_returns_200(self, http_client):
        existing = _make_prov_config(id="cfg-1", provider_type="claude", is_active=False)
        db = _mock_db(MockResult(single=existing), MockResult())
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/providers/cfg-1/config",
            json={"is_active": True},
        )
        assert resp.status_code == 200

    async def test_response_has_provider_type(self, http_client):
        existing = _make_prov_config(id="cfg-1", provider_type="claude")
        db = _mock_db(MockResult(single=existing), MockResult())
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/providers/cfg-1/config",
            json={"api_key": "sk-ant-test", "model": "claude-3-opus-20240229", "is_active": True},
        )
        assert resp.json()["provider_type"] == "claude"

    async def test_not_found_returns_404(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/providers/nonexistent-id/config",
            json={"is_active": True},
        )
        assert resp.status_code == 404

    async def test_can_update_display_name(self, http_client):
        existing = _make_prov_config(id="cfg-1", provider_type="claude")
        db = _mock_db(MockResult(single=existing), MockResult())
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put(
            "/api/v1/providers/cfg-1/config",
            json={"display_name": "My Claude"},
        )
        assert resp.status_code == 200
        assert existing.display_name == "My Claude"


# ── POST /api/v1/providers/{config_id}/test ──────────────────────────────────────


class TestTestProvider:
    async def test_ollama_health_check_success(self, http_client):
        config = _make_prov_config(id="cfg-2", provider_type="ollama", base_url=None)
        db = _mock_db(MockResult(single=config))
        app.dependency_overrides[get_db] = lambda: db
        mock_provider = MagicMock()
        mock_provider.health_check = AsyncMock(return_value=(True, ""))
        with patch("app.routers.providers.create_provider", return_value=mock_provider):
            resp = await http_client.post("/api/v1/providers/cfg-2/test")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    async def test_ollama_health_check_failure(self, http_client):
        config = _make_prov_config(id="cfg-2", provider_type="ollama")
        db = _mock_db(MockResult(single=config))
        app.dependency_overrides[get_db] = lambda: db
        mock_provider = MagicMock()
        mock_provider.health_check = AsyncMock(return_value=(False, "connection refused"))
        with patch("app.routers.providers.create_provider", return_value=mock_provider):
            resp = await http_client.post("/api/v1/providers/cfg-2/test")
        assert resp.status_code == 200
        assert resp.json()["success"] is False

    async def test_no_api_key_returns_400(self, http_client):
        config = _make_prov_config(id="cfg-1", provider_type="openai", api_key_encrypted=None)
        db = _mock_db(MockResult(single=config))
        app.dependency_overrides[get_db] = lambda: db
        with patch("app.routers.providers.get_settings") as mock_settings:
            s = MagicMock()
            s.env_api_key = MagicMock(return_value=None)
            mock_settings.return_value = s
            resp = await http_client.post("/api/v1/providers/cfg-1/test")
        assert resp.status_code == 400

    async def test_returns_provider_name_in_response(self, http_client):
        config = _make_prov_config(id="cfg-2", provider_type="ollama")
        db = _mock_db(MockResult(single=config))
        app.dependency_overrides[get_db] = lambda: db
        mock_provider = MagicMock()
        mock_provider.health_check = AsyncMock(return_value=(True, ""))
        with patch("app.routers.providers.create_provider", return_value=mock_provider):
            resp = await http_client.post("/api/v1/providers/cfg-2/test")
        assert "ollama" in resp.json()["message"]

    async def test_not_found_returns_404(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post("/api/v1/providers/nonexistent-id/test")
        assert resp.status_code == 404

    async def test_api_key_from_db_config(self, http_client):
        config = _make_prov_config(
            id="cfg-1",
            provider_type="claude",
            api_key_encrypted=b"some-encrypted-key",
        )
        db = _mock_db(MockResult(single=config))
        app.dependency_overrides[get_db] = lambda: db
        mock_provider = MagicMock()
        mock_provider.health_check = AsyncMock(return_value=(True, ""))
        with (
            patch("app.routers.providers.decrypt_value", return_value="sk-test-key"),
            patch("app.routers.providers.create_provider", return_value=mock_provider),
        ):
            resp = await http_client.post("/api/v1/providers/cfg-1/test")
        assert resp.status_code == 200


class TestTestNewProvider:
    """Tests for POST /api/v1/providers/test (pre-save credential check)."""

    async def test_ollama_success(self, http_client):
        mock_provider = MagicMock()
        mock_provider.health_check = AsyncMock(return_value=(True, ""))
        with (
            patch("app.routers.providers.create_provider", return_value=mock_provider),
            patch("app.routers.providers.get_settings") as ms,
        ):
            ms.return_value.ollama_base_url = "http://ollama:11434"
            resp = await http_client.post(
                "/api/v1/providers/test",
                json={"provider_type": "ollama"},
            )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert "ollama" in resp.json()["message"]

    async def test_named_provider_success(self, http_client):
        mock_provider = MagicMock()
        mock_provider.health_check = AsyncMock(return_value=(True, ""))
        with (
            patch("app.routers.providers.create_provider", return_value=mock_provider),
            patch("app.routers.providers.get_settings") as ms,
        ):
            ms.return_value.verify_ssl = True
            resp = await http_client.post(
                "/api/v1/providers/test",
                json={
                    "provider_type": "groq",
                    "api_key": "sk-test",
                    "model": "llama-3.3-70b-versatile",
                },
            )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert "groq" in resp.json()["message"]

    async def test_health_check_failure_returns_200_with_success_false(self, http_client):
        mock_provider = MagicMock()
        mock_provider.health_check = AsyncMock(return_value=(False, "invalid api key"))
        with (
            patch("app.routers.providers.create_provider", return_value=mock_provider),
            patch("app.routers.providers.get_settings") as ms,
        ):
            ms.return_value.verify_ssl = True
            resp = await http_client.post(
                "/api/v1/providers/test",
                json={"provider_type": "claude", "api_key": "bad-key"},
            )
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        assert "invalid api key" in resp.json()["message"]

    async def test_missing_api_key_returns_400(self, http_client):
        with patch("app.routers.providers.get_settings") as ms:
            ms.return_value.verify_ssl = True
            ms.return_value.env_api_key.return_value = None
            resp = await http_client.post(
                "/api/v1/providers/test",
                json={"provider_type": "claude"},
            )
        assert resp.status_code == 400

    async def test_unknown_provider_type_returns_400(self, http_client):
        with (
            patch(
                "app.routers.providers.create_provider",
                side_effect=ValueError("unknown provider"),
            ),
            patch("app.routers.providers.get_settings") as ms,
        ):
            ms.return_value.verify_ssl = True
            resp = await http_client.post(
                "/api/v1/providers/test",
                json={"provider_type": "unknown-xyz", "api_key": "sk-test"},
            )
        assert resp.status_code == 400
