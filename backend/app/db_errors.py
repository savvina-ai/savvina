# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Database errors the app answers specifically instead of with a bare 500."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.exc import DBAPIError

_LOCK_NOT_AVAILABLE = "55P03"


def is_lock_timeout(exc: DBAPIError) -> bool:
    """True for Postgres' ``lock_timeout`` cancellation (SQLSTATE 55P03)."""
    code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    return code == _LOCK_NOT_AVAILABLE


async def db_error_handler(request: Request, exc: DBAPIError) -> JSONResponse:
    """503 + Retry-After for a lock timeout; anything else is re-raised unchanged.

    Re-raising from a handler reaches the server-error middleware, so every other
    ``DBAPIError`` keeps its normal 500 and traceback log.
    """
    if is_lock_timeout(exc):
        return JSONResponse(
            status_code=503,
            content={
                "detail": "This connection is busy with another operation; try again shortly."
            },
            headers={"Retry-After": "5"},
        )
    raise exc
