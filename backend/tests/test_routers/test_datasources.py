# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for GET /api/v1/datasources."""

from __future__ import annotations

from unittest.mock import patch


class TestListDatasources:
    async def test_returns_200(self, http_client):
        resp = await http_client.get("/api/v1/datasources")
        assert resp.status_code == 200

    async def test_response_is_list(self, http_client):
        resp = await http_client.get("/api/v1/datasources")
        assert isinstance(resp.json(), list)

    async def test_each_entry_has_source_type(self, http_client):
        resp = await http_client.get("/api/v1/datasources")
        for entry in resp.json():
            assert "source_type" in entry

    async def test_each_entry_has_config_schema(self, http_client):
        resp = await http_client.get("/api/v1/datasources")
        for entry in resp.json():
            assert "config_schema" in entry

    async def test_postgresql_adapter_is_registered(self, http_client):
        resp = await http_client.get("/api/v1/datasources")
        types = [e["source_type"] for e in resp.json()]
        assert "postgresql" in types

    async def test_empty_list_when_no_adapters_registered(self, http_client):
        with patch("app.routers.datasources.list_available_sources", return_value=[]):
            resp = await http_client.get("/api/v1/datasources")
        assert resp.status_code == 200
        assert resp.json() == []
