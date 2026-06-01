# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""FastAPI application factory — registers all routers and middleware.

E402 (module level import not at top of file) is suppressed file-wide because
``faulthandler.enable()`` must execute before any C extension is imported
transitively, and logging must be configured before routers are loaded.
All E402 suppressions here are intentional; do not reorder these imports.
"""
# ruff: noqa: E402

from __future__ import annotations

import faulthandler

# Dump a native C-level traceback to stderr on SIGSEGV / SIGFPE / SIGBUS so
# the crash location in a C extension shows up in docker logs instead of just
# silently exiting with code 139. Must run *before* heavy native extensions
# (fastembed / ONNX Runtime) are imported transitively below.
faulthandler.enable()

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import logging
import re
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import MutableHeaders

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Import datasource adapters to trigger @register_datasource decorators
from . import datasources as _datasources_pkg  # noqa: F401

# Import all SQLAlchemy models so Base.metadata is registered
from . import models  # noqa: F401

# Import all LLM providers to trigger @register_provider decorators
from . import providers as _providers_pkg  # noqa: F401
from .config import get_settings
from .core.logging_config import configure_logging as _configure_logging
from .core.request_context import request_id_var
from .database import async_session_maker

logger = logging.getLogger(__name__)

_HSTS_MAX_AGE = 365 * 24 * 3600  # 1 year in seconds

# Configure structured logging at import time so that any exception raised during
# middleware registration, route inclusion, or settings validation is formatted
# correctly (JSON in production, plain text in dev) rather than as bare root-logger output.
_s = get_settings()
_configure_logging(_s.log_level, _s.log_format)

# ── Malicious-request patterns for ScannerGuardMiddleware ────────────────────

_REQUEST_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")

_MALICIOUS_PATH_RE = re.compile(
    r"(?i)(/cgi-bin/|\.php$|/wp-(?:admin|login|content)|/struts2?/|"
    r"/owa/auth|/solr/|/jenkins/|/jndi:|/actuator/|"
    r"/console(?:/j_security_check)?)",
)
_MALICIOUS_QUERY_RE = re.compile(r"\$\{jndi:", re.IGNORECASE)


# ── Middleware classes ────────────────────────────────────────────────────────


class OriginCheckMiddleware:
    """Pure-ASGI middleware — defense-in-depth Origin header validation.

    Rejects mutating requests (POST/PUT/PATCH/DELETE) whose ``Origin`` header
    is present but does not match the configured ``cors_origins`` allowlist.
    Requests without an ``Origin`` header (non-browser clients) are allowed
    through, since they cannot be triggered by a browser-based CSRF attack.
    """

    _MUTATING_METHODS = frozenset({b"POST", b"PUT", b"PATCH", b"DELETE"})

    def __init__(self, app) -> None:
        self.app = app
        self._allowed_origins = frozenset(get_settings().cors_origins)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw_method = scope.get("method", "GET")
        method = raw_method.encode() if isinstance(raw_method, str) else raw_method
        if method not in self._MUTATING_METHODS:
            await self.app(scope, receive, send)
            return

        origin: str | None = None
        for header_name, header_value in scope.get("headers", []):
            if header_name == b"origin":
                origin = header_value.decode("latin-1")
                break

        if origin is not None and origin not in self._allowed_origins:
            logger.warning(
                "Origin rejected: %s for %s %s",
                origin,
                scope.get("method"),
                scope.get("path"),
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": b'{"detail":"Origin not allowed"}'})
            return

        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    """Pure-ASGI middleware — OWASP security headers + SSE duplicate-response guard.

    The duplicate-response guard silently drops a second ``http.response.start``
    message, which prevents a uvicorn RuntimeError when SSE streams are active.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False
        second_response_active = False

        async def send_with_headers(message) -> None:
            nonlocal response_started, second_response_active
            if message["type"] == "http.response.start":
                if response_started:  # duplicate guard — drop silently
                    second_response_active = True
                    return
                response_started = True
                second_response_active = False
                headers = MutableHeaders(scope=message)
                headers.append("X-Content-Type-Options", "nosniff")
                headers.append("X-Frame-Options", "DENY")
                headers.append("Referrer-Policy", "strict-origin-when-cross-origin")
                if not get_settings().debug:
                    headers.append(
                        "Strict-Transport-Security",
                        f"max-age={_HSTS_MAX_AGE}; includeSubDomains",
                    )
                path = scope.get("path", "")
                if path.startswith("/api/"):
                    # API responses: strict — no scripts, no framing
                    headers.append("Content-Security-Policy", "default-src 'none'")
                else:
                    # Frontend assets served directly by FastAPI: allow inline styles
                    # and data URIs required by React and Recharts SVGs
                    headers.append(
                        "Content-Security-Policy",
                        (
                            "default-src 'self'; "
                            "script-src 'self'; "
                            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                            "img-src 'self' data:; "
                            "font-src 'self' data: https://fonts.gstatic.com; "
                            "connect-src 'self'"
                        ),
                    )
                if path.startswith("/api/"):
                    headers.append("Cache-Control", "no-store, private")
            elif message["type"] == "http.response.body" and second_response_active:
                return  # suppress orphaned body frames from the dropped second response
            await send(message)

        await self.app(scope, receive, send_with_headers)


class ScannerGuardMiddleware:
    """Pure-ASGI middleware — early rejection of exploit-scanner requests.

    Returns 403 immediately for paths or query strings matching known exploit
    patterns (Log4j JNDI, CGI, PHP, WordPress, Struts2, Spring actuators, etc.)
    before the request reaches any route handler, auth check, or DB connection.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            query = scope.get("query_string", b"").decode("latin-1", errors="replace")
            if _MALICIOUS_PATH_RE.search(path) or _MALICIOUS_QUERY_RE.search(query):
                logger.warning("Scanner rejected: %s %s", scope.get("method", "?"), path)
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": b'{"detail":"Forbidden"}'})
                return
        await self.app(scope, receive, send)


class RequestIDMiddleware:
    """Pure-ASGI middleware — assigns a unique request ID to every HTTP request.

    Reads ``X-Request-ID`` from the incoming headers (to honour upstream load
    balancers) or generates a 16-hex-char UUID4 fragment.  The value is stored
    in the ``request_id_var`` ContextVar so every downstream log line includes
    it automatically, and echoed back on the response via the same header.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id: str | None = None
        for header_name, header_value in scope.get("headers", []):
            if header_name == b"x-request-id":
                candidate = header_value.decode("latin-1", errors="replace")
                if _REQUEST_ID_RE.match(candidate):
                    request_id = candidate
                break
        if not request_id:
            request_id = uuid4().hex[:16]

        token = request_id_var.set(request_id)

        async def send_with_request_id(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.append("X-Request-ID", request_id)  # type: ignore[arg-type]
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            request_id_var.reset(token)


# ── Lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("Starting %s", settings.app_name)

    # Clean up expired refresh tokens on startup
    from sqlalchemy import delete

    from .models.user import RefreshToken

    async with async_session_maker() as _tok_db:
        await _tok_db.execute(
            delete(RefreshToken).where(RefreshToken.expires_at < datetime.now(UTC))
        )
        await _tok_db.commit()
    logger.info("Expired tokens cleaned up")

    # Prune stale query cache entries that have exceeded their configured TTL.
    from .cache.query_cache import QueryCache as _QueryCache

    async with async_session_maker() as _cache_db:
        _cache = _QueryCache(
            settings.embedding_model,
            settings.semantic_similarity_threshold,
            settings.cache_max_age_days,
        )
        _pruned = await _cache.prune_expired(_cache_db)
    if _pruned:
        logger.info("Pruned %d expired query cache entries", _pruned)

    # Restore persisted settings overrides saved via PUT /api/settings.
    from sqlalchemy import select

    from .config import DEFAULT_QUERY_TIMEOUT, DEFAULT_ROW_LIMIT
    from .models.app_settings import AppSetting

    # Seed frontend-only defaults onto the singleton (not backed by env vars).
    settings.default_query_timeout = DEFAULT_QUERY_TIMEOUT
    settings.default_row_limit = DEFAULT_ROW_LIMIT

    _setting_parsers: dict[str, object] = {
        "default_query_timeout": int,
        "default_row_limit": int,
        "cache_enabled": lambda v: v.lower() == "true",
        "cache_max_age_days": int,
        "semantic_similarity_threshold": float,
        "db_pool_size": int,
        "db_max_overflow": int,
        "schema_pruning_enabled": lambda v: v.lower() == "true",
        "schema_pruning_top_k": int,
    }
    async with async_session_maker() as _db:
        _rows = (await _db.execute(select(AppSetting))).scalars().all()
    for _row in _rows:
        if _row.key in _setting_parsers:
            setattr(settings, _row.key, _setting_parsers[_row.key](_row.value))
    if _rows:
        logger.info("Restored %d persisted settings override(s)", len(_rows))

    # Recreate the database engine if pool size was overridden via settings.
    # The engine is created at import time with defaults; here we swap it out
    # so all subsequent requests use the persisted pool configuration.
    _pool_keys = {"db_pool_size", "db_max_overflow"}
    if _pool_keys & {r.key for r in _rows}:
        from sqlalchemy.ext.asyncio import (
            async_sessionmaker as _async_sm,
        )
        from sqlalchemy.ext.asyncio import (
            create_async_engine as _cae,
        )

        import app.database as _db_mod

        _new_engine = _cae(
            settings.database_url,
            echo=settings.debug,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
        )
        await _db_mod.engine.dispose()
        _db_mod.engine = _new_engine
        _db_mod.async_session_maker = _async_sm(_new_engine, expire_on_commit=False)
        logger.info(
            "Database pool recreated: pool_size=%d, max_overflow=%d",
            settings.db_pool_size,
            settings.db_max_overflow,
        )

    # Pre-warm the sentence-transformer model so the first NL query isn't penalized
    # by a cold model load. _get_shared_cache() is the process-level singleton defined
    # in the chat router; calling compute_embedding once triggers lazy model loading.
    from .routers.chat import _get_shared_cache

    try:
        await _get_shared_cache().compute_embedding_async("warmup")
        logger.info("Embedding model loaded")
    except Exception as _warmup_err:
        logger.warning(
            "Embedding model warmup failed — first NL query will trigger model load: %s",
            _warmup_err,
        )

    yield

    # Shutdown
    logger.info("Shutting down %s", settings.app_name)
    import app.database as _db_mod  # deferred to avoid circular import at module level

    await _db_mod.engine.dispose()
    logger.info("Database connection pool disposed")

    from .datasources.adapters.postgresql import close_all_pools as _close_pg_pools

    await _close_pg_pools()
    logger.info("User-DB asyncpg connection pools closed")


from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from .auth.limiter import limiter as _limiter

app = FastAPI(
    title="Savvina AI API",
    description="Conversational analytics — query your data with natural language.",
    version="0.1.0",
    lifespan=lifespan,
)

from pathlib import Path

from fastapi.staticfiles import StaticFiles

_static_dir = Path(__file__).parent.parent / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# Attach limiter to app state immediately after app creation (not in lifespan)
# so that SlowAPIMiddleware can find it even during tests that skip lifespan.
app.state.limiter = _limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Middleware stack (registration order = innermost → outermost) ─────────────
#
# Effective request processing order (outermost → innermost):
#   ScannerGuard → RequestID → CORS → OriginCheck → SecurityHeaders → SlowAPI → route handler
#
# In Starlette, the LAST add_middleware() call becomes the OUTERMOST wrapper.

# Innermost — rate limiting; runs last on request, first on response
app.add_middleware(SlowAPIMiddleware)

# Security headers added to every HTTP response
app.add_middleware(SecurityHeadersMiddleware)

# Origin validation — defense-in-depth CSRF guard for mutating requests
app.add_middleware(OriginCheckMiddleware)

# CORS — explicit methods/headers (no wildcards in production).
# NOTE: Starlette's CORSMiddleware does not accept a callable for allow_origins —
# the list is baked in at process startup. Changing CORS_ORIGINS requires a
# process restart; cors_origins is intentionally NOT in _MUTABLE_KEYS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Request ID — sets ContextVar + echoes X-Request-ID on every response
app.add_middleware(RequestIDMiddleware)

# Outermost — rejects scanner/exploit traffic before it touches any other layer
app.add_middleware(ScannerGuardMiddleware)


# ── Routers ───────────────────────────────────────────────────────────────────

from .routers import (  # noqa: I001
    chat,
    connections,
    datasources,
    export,
    providers,
    semantic,
    settings,
    share,
)
from .routers import auth as auth_router

app.include_router(datasources.router, prefix="/api/v1")
app.include_router(connections.router, prefix="/api/v1")
app.include_router(semantic.router, prefix="/api/v1")
app.include_router(providers.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
app.include_router(settings.router, prefix="/api/v1")
app.include_router(auth_router.router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(share.router, prefix="/api/v1")
app.include_router(export.router, prefix="/api/v1")


# ── Health check ──────────────────────────────────────────────────────────────

from fastapi import Depends, Request
from sqlalchemy import text

from .database import get_db


@app.get("/healthz", tags=["health"])
@_limiter.limit("60/minute")
async def liveness(request: Request) -> dict[str, str]:
    """Liveness probe — returns 200 if the process is up. No DB check."""
    return {"status": "ok"}


@app.get("/health", tags=["health"])
@_limiter.limit("60/minute")
async def health_check(request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    """Readiness probe. Returns 503 if the database is unreachable."""
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("Health check: database unreachable", exc_info=True)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    return {"status": "ok", "app": get_settings().app_name}


@app.get("/api/version", tags=["health"])
@_limiter.limit("60/minute")
async def api_version(request: Request) -> dict[str, str]:
    """Return the current API version."""
    return {"version": "v1", "app_version": app.version}


@app.post("/api/csp-report", status_code=204, tags=["health"])
@_limiter.limit("30/minute")
async def csp_report(request: Request) -> None:
    """Receive Content-Security-Policy violation reports from browsers."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    logger.warning("CSP violation report: %s", body)
