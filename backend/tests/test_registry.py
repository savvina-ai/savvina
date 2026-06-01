# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for app/datasources/registry.py."""

import pytest

from app.datasources import postgresql  # noqa: F401 — ensures adapter is registered
from app.datasources.adapters.postgresql import PostgreSQLDataSource
from app.datasources.registry import (
    _REGISTRY,
    create_datasource,
    get_datasource_class,
    list_available_sources,
)


class TestRegistry:
    def test_postgresql_is_registered(self):
        assert "postgresql" in _REGISTRY

    def test_get_datasource_class_returns_correct_class(self):
        assert get_datasource_class("postgresql") is PostgreSQLDataSource

    def test_get_datasource_class_unknown_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown data source"):
            get_datasource_class("nonexistent_db")

    def test_error_message_lists_available_sources(self):
        """The error message should hint at what IS available."""
        with pytest.raises(ValueError, match="postgresql"):
            get_datasource_class("nonexistent_db")

    def test_create_datasource_returns_instance(self):
        ds = create_datasource("postgresql")
        assert isinstance(ds, PostgreSQLDataSource)

    def test_create_datasource_returns_new_instance_each_call(self):
        ds1 = create_datasource("postgresql")
        ds2 = create_datasource("postgresql")
        assert ds1 is not ds2

    def test_list_available_sources_is_list(self):
        sources = list_available_sources()
        assert isinstance(sources, list)

    def test_list_available_sources_contains_postgresql(self):
        types = [s["source_type"] for s in list_available_sources()]
        assert "postgresql" in types

    def test_list_available_sources_entry_structure(self):
        pg = next(s for s in list_available_sources() if s["source_type"] == "postgresql")
        assert pg["display_name"] == "PostgreSQL"
        assert pg["query_dialect"] == "PostgreSQL"
        assert "config_schema" in pg
        assert "fields" in pg["config_schema"]
        assert isinstance(pg["config_schema"]["fields"], list)

    def test_list_available_sources_has_icon(self):
        pg = next(s for s in list_available_sources() if s["source_type"] == "postgresql")
        assert pg["icon"]  # non-empty
