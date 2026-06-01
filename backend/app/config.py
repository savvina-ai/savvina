# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

from functools import lru_cache
import logging

from cryptography.fernet import Fernet
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_name: str = "Savvina AI"
    debug: bool = False
    log_level: str = "INFO"
    log_format: str = "json"  # "text" for local development

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        numeric = logging.getLevelName(v.upper())
        if not isinstance(numeric, int):
            raise ValueError(
                f"Invalid log level: {v!r}. Valid values: DEBUG, INFO, WARNING, ERROR, CRITICAL"
            )
        # Resolve to canonical name so that e.g. WARN → WARNING
        return logging.getLevelName(numeric)

    # Database — required; set via DATABASE_URL env var
    database_url: str

    @field_validator("database_url")
    @classmethod
    def database_url_must_be_postgresql(cls, v: str) -> str:
        if not v.startswith("postgresql"):
            raise ValueError(
                "DATABASE_URL must use a PostgreSQL scheme (postgresql+asyncpg://...). "
                "SQLite and other backends are not supported."
            )
        return v

    # Encryption
    encryption_key: str  # Fernet key, REQUIRED

    # LLM Providers (optional — user configures via UI)
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    groq_api_key: str | None = None
    gemini_api_key: str | None = None
    cerebras_api_key: str | None = None
    mistral_api_key: str | None = None
    ollama_base_url: str = "http://ollama:11434"
    # Set false in corporate TLS-intercepted environments if custom CA trust
    # cannot be installed into the container.  Applies to all LLM providers.
    # Accepts env var VERIFY_SSL or legacy OPENAI_VERIFY_SSL.
    verify_ssl: bool = Field(
        default=True,
        validation_alias=AliasChoices("verify_ssl", "openai_verify_ssl"),
    )

    # Cache
    cache_enabled: bool = True
    semantic_similarity_threshold: float = 0.87  # Cosine similarity threshold for cache hits
    # NOTE: changing this model invalidates all stored embeddings in query_cache.
    # Flush the cache (DELETE FROM query_cache) before deploying a model change.
    embedding_model: str = "BAAI/bge-small-en-v1.5"  # 384-dim ONNX retrieval model (fastembed)
    # Entries not accessed within this window are invisible to lookup() and eligible for prune.
    # Set to 0 to disable TTL (entries live until schema refresh or manual clear).
    cache_max_age_days: int = 30

    # Schema pruning — embed tables and select only the most relevant ones per question.
    # Applies to all schemas; the fallback_min=3 guard handles small schemas gracefully.
    schema_pruning_enabled: bool = True
    schema_pruning_top_k: int = 15  # max tables to include after pruning

    # CORS
    cors_origins: list[str] = ["http://localhost:3000"]

    # JWT
    jwt_secret_key: str  # required — JWT_SECRET_KEY env var
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 30

    @field_validator("jwt_algorithm")
    @classmethod
    def jwt_algorithm_must_be_safe(cls, v: str) -> str:
        allowed = {"HS256", "HS384", "HS512"}
        if v not in allowed:
            raise ValueError(f"JWT_ALGORITHM must be one of {sorted(allowed)}, got {v!r}")
        return v

    # Rate limiting
    auth_rate_limit: str = "10/minute"
    trusted_proxies: list[str] = ["127.0.0.1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]

    # Frontend-only settings — configurable from the settings page, persisted in DB.
    # These live on Settings so that setattr() in main.py (which restores DB overrides)
    # has a valid field to write to.
    default_query_timeout: int = 30
    default_row_limit: int = 1000

    # Database connection pool — configurable from the frontend settings page.
    # Changes are persisted to app_settings and take effect on next process restart.
    db_pool_size: int = 10
    db_max_overflow: int = 20

    _WEAK_DEFAULTS: frozenset[str] = frozenset(
        {"change-this", "secret", "changeme", "your-secret-key"}
    )

    @field_validator("encryption_key")
    @classmethod
    def encryption_key_must_be_valid_fernet(cls, v: str) -> str:
        try:
            Fernet(v.encode())
        except Exception as exc:
            raise ValueError(
                "ENCRYPTION_KEY must be a valid Fernet key (32 url-safe base64 bytes). "
                "Generate one with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ) from exc
        return v

    @staticmethod
    def _is_private_origin(origin: str) -> bool:
        """Return True if the origin hostname is a RFC-1918 private address."""
        import re

        m = re.match(r"https?://(\d+\.\d+\.\d+\.\d+)", origin)
        if not m:
            return False
        parts = [int(p) for p in m.group(1).split(".")]
        return (
            parts[0] == 10
            or (parts[0] == 172 and 16 <= parts[1] <= 31)
            or (parts[0] == 192 and parts[1] == 168)
        )

    @model_validator(mode="after")
    def reject_insecure_cors_in_production(self) -> "Settings":
        if not self.debug:
            for origin in self.cors_origins:
                if (
                    origin.startswith("http://")
                    and "localhost" not in origin
                    and "127.0.0.1" not in origin
                    and not self._is_private_origin(origin)
                ):
                    raise ValueError(
                        f"Insecure CORS origin {origin!r} — use https:// in production "
                        f"or set DEBUG=true for local development"
                    )
        return self

    @model_validator(mode="after")
    def reject_insecure_defaults(self) -> "Settings":
        if self.jwt_secret_key.lower() in self._WEAK_DEFAULTS or len(self.jwt_secret_key) < 32:
            raise ValueError(
                "JWT_SECRET_KEY must be at least 32 characters and not a known weak default. "
                'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
            )
        return self

    def env_api_key(self, provider_name: str) -> str | None:
        """Return the env-var API key for a named provider, or None.

        Single source of truth — used by routers and services to avoid
        repeating the provider-name → settings-attribute mapping.
        """
        _map: dict[str, str | None] = {
            "claude": self.anthropic_api_key,
            "openai": self.openai_api_key,
            "openai_compatible": self.openai_api_key,
            "groq": self.groq_api_key,
            "gemini": self.gemini_api_key,
            "cerebras": self.cerebras_api_key,
            "mistral": self.mistral_api_key,
        }
        return _map.get(provider_name)


# Re-exported for routers that reference these constants directly.
DEFAULT_QUERY_TIMEOUT: int = Settings.model_fields["default_query_timeout"].default
DEFAULT_ROW_LIMIT: int = Settings.model_fields["default_row_limit"].default


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings.

    Using @lru_cache instead of a module-level singleton so tests can
    monkeypatch this function or call get_settings.cache_clear() between runs.
    """
    return Settings()
