# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for app/config.py — Settings and get_settings()."""

import re
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


class TestResolveJwtSecret:
    _BASE: ClassVar[dict] = {
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "encryption_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        # debug=True so the random-key fallback path is reachable in tests.
        "debug": True,
    }

    def _settings_without_jwt_env(self, **extra):
        """Build Settings with JWT_SECRET_KEY removed from env so the None branch fires."""
        import os

        saved = os.environ.pop("JWT_SECRET_KEY", None)
        try:
            return Settings(**self._BASE, **extra)
        finally:
            if saved is not None:
                os.environ["JWT_SECRET_KEY"] = saved

    def test_none_generates_random_key(self):
        s = self._settings_without_jwt_env()
        assert s.jwt_secret_key is not None
        assert len(s.jwt_secret_key) >= 32

    def test_none_logs_warning(self):
        import logging

        import app.config as cfg

        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record.getMessage())

        handler = Capture(level=logging.WARNING)
        cfg.logger.addHandler(handler)
        cfg.logger.setLevel(logging.WARNING)
        try:
            self._settings_without_jwt_env()
        finally:
            cfg.logger.removeHandler(handler)
            cfg.logger.setLevel(logging.NOTSET)

        assert any("JWT_SECRET_KEY not set" in m for m in records)

    def test_raises_in_production_without_jwt_key(self):
        import os

        saved = os.environ.pop("JWT_SECRET_KEY", None)
        try:
            with pytest.raises(ValidationError, match="JWT_SECRET_KEY must be set in production"):
                Settings(
                    database_url="postgresql+asyncpg://u:p@localhost/db",
                    encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                    debug=False,
                )
        finally:
            if saved is not None:
                os.environ["JWT_SECRET_KEY"] = saved

    def test_rejects_weak_default(self):
        with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
            Settings(**self._BASE, jwt_secret_key="secret")

    def test_rejects_short_key(self):
        with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
            Settings(**self._BASE, jwt_secret_key="short")

    def test_accepts_valid_key(self):
        key = "a" * 32
        s = Settings(**self._BASE, jwt_secret_key=key)
        assert s.jwt_secret_key == key


class TestBehindTlsProxy:
    _BASE: ClassVar[dict] = {
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "encryption_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "jwt_secret_key": "a" * 64,
        "debug": False,
    }

    def test_defaults_to_false(self):
        # Explicit kwarg-free construction: the field default must be off, so a
        # plain-HTTP localhost/LAN install never issues a Secure cookie by accident.
        assert Settings.model_fields["behind_tls_proxy"].default is False

    def test_https_origin_without_flag_is_rejected(self):
        """An https:// origin means a TLS proxy fronts the UI; without the flag the
        refresh cookie would silently lose Secure and HSTS — refuse to start instead."""
        with pytest.raises(ValidationError, match="BEHIND_TLS_PROXY is not set"):
            Settings(
                **self._BASE,
                cors_origins=["https://savvina.example.com"],
                behind_tls_proxy=False,
            )

    def test_https_origin_with_flag_is_accepted(self):
        s = Settings(
            **self._BASE,
            cors_origins=["https://savvina.example.com"],
            behind_tls_proxy=True,
        )
        assert s.behind_tls_proxy is True

    def test_mixed_origins_only_need_flag_for_the_https_one(self):
        with pytest.raises(ValidationError, match=re.escape("'https://savvina.example.com'")):
            Settings(
                **self._BASE,
                cors_origins=["http://localhost:3000", "https://savvina.example.com"],
                behind_tls_proxy=False,
            )

    def test_http_origins_do_not_need_flag(self):
        s = Settings(
            **self._BASE,
            cors_origins=["http://localhost:3000", "http://192.168.1.50:3000"],
            behind_tls_proxy=False,
        )
        assert s.behind_tls_proxy is False

    def test_flag_with_only_http_origins_is_allowed(self):
        """localhost alongside a TLS proxy is a legitimate dev setup — no reverse check."""
        s = Settings(**self._BASE, cors_origins=["http://localhost:3000"], behind_tls_proxy=True)
        assert s.behind_tls_proxy is True

    def test_debug_skips_the_check(self):
        s = Settings(
            **{**self._BASE, "debug": True},
            cors_origins=["https://savvina.example.com"],
            behind_tls_proxy=False,
        )
        assert s.behind_tls_proxy is False
