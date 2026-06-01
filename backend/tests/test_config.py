# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for app/config.py — Settings and get_settings()."""

from typing import ClassVar

from pydantic import ValidationError
import pytest

from app.config import Settings, get_settings


class TestGetSettings:
    def test_returns_settings_instance(self):
        assert isinstance(get_settings(), Settings)

    def test_is_cached(self):
        """lru_cache must return the same object on repeated calls."""
        assert get_settings() is get_settings()

    def test_default_app_name(self):
        assert get_settings().app_name == "Savvina AI"

    def test_encryption_key_loaded_from_env(self):
        # The conftest sets ENCRYPTION_KEY before any import
        assert get_settings().encryption_key != ""

    def test_database_url_is_set(self):
        assert get_settings().database_url

    def test_default_cors_origins(self):
        default = Settings.model_fields["cors_origins"].default
        assert "http://localhost:3000" in default

    def test_cache_enabled_by_default(self):
        assert get_settings().cache_enabled is True

    def test_similarity_threshold_default(self):
        default = Settings.model_fields["semantic_similarity_threshold"].default
        assert default == 0.87

    def test_embedding_model_default(self):
        assert get_settings().embedding_model == "BAAI/bge-small-en-v1.5"

    def test_debug_is_false_by_default(self):
        default = Settings.model_fields["debug"].default
        assert default is False

    def test_llm_keys_optional_by_default(self):
        s = get_settings()
        # These are None if not set in the environment
        assert s.anthropic_api_key is None or isinstance(s.anthropic_api_key, str)
        assert s.openai_api_key is None or isinstance(s.openai_api_key, str)

    def test_log_format_default_is_json(self):
        assert Settings.model_fields["log_format"].default == "json"


class TestLogLevelValidator:
    _REQUIRED: ClassVar[dict] = {
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "encryption_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "jwt_secret_key": "a" * 32,
    }

    @pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    def test_accepts_valid_levels(self, level):
        s = Settings(**self._REQUIRED, log_level=level)
        assert s.log_level == level

    @pytest.mark.parametrize("level", ["debug", "info", "warning", "error", "critical"])
    def test_normalizes_to_uppercase(self, level):
        s = Settings(**self._REQUIRED, log_level=level)
        assert s.log_level == level.upper()

    def test_normalizes_warn_to_warning(self):
        # WARN is a Python logging alias; normalize to the canonical name
        s = Settings(**self._REQUIRED, log_level="WARN")
        assert s.log_level == "WARNING"

    def test_rejects_arbitrary_string(self):
        with pytest.raises(ValidationError, match="Invalid log level"):
            Settings(**self._REQUIRED, log_level="VERBOSE")
