# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""A Postgres lock timeout is a retryable 503; any other database error stays a 500."""

from __future__ import annotations

from fastapi import FastAPI
import httpx
from sqlalchemy.exc import DBAPIError

from app.db_errors import db_error_handler


class _LockNotAvailableError(Exception):
    sqlstate = "55P03"


def _app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(DBAPIError, db_error_handler)

    @app.get("/lock-timeout")
    async def lock_timeout() -> None:
        raise DBAPIError("SELECT ...", {}, _LockNotAvailableError("lock timeout"))

    @app.get("/other")
    async def other() -> None:
        raise DBAPIError("SELECT ...", {}, Exception("boom"))

    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_a_lock_timeout_is_a_503_with_retry_after() -> None:
    async with _client(_app()) as client:
        resp = await client.get("/lock-timeout")
    assert resp.status_code == 503
    assert "busy" in resp.json()["detail"]
    assert resp.headers["retry-after"] == "5"


async def test_any_other_database_error_is_still_a_500() -> None:
    async with _client(_app()) as client:
        resp = await client.get("/other")
    assert resp.status_code == 500


def test_the_app_registers_the_handler() -> None:
    from app.main import app

    assert app.exception_handlers[DBAPIError] is db_error_handler
