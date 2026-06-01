# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""FastAPI dependency chain for authentication and authorization."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from ..core.request_context import user_id_var
from ..database import get_db
from ..models.user import RevokedAccessToken, User
from .tokens import decode_access_token

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_token_payload(
    token: str = Depends(oauth2_scheme),
) -> dict:
    """Return the decoded JWT payload for the current request's access token."""
    return decode_access_token(token)


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Decode JWT, load User from DB, raise 401 if invalid or not found."""
    payload = decode_access_token(token)

    jti: str | None = payload.get("jti")
    if jti:
        now = datetime.now(UTC)
        revoked = await db.scalar(
            select(RevokedAccessToken).where(
                RevokedAccessToken.jti == jti,
                RevokedAccessToken.expires_at > now,
            )
        )
        if revoked is not None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has been revoked",
                headers={"WWW-Authenticate": "Bearer"},
            )

    user_id: str | None = payload.get("sub")

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.tokens_invalidated_at is not None:
        iat = payload.get("iat")
        if iat is not None and datetime.fromtimestamp(iat, tz=UTC) < user.tokens_invalidated_at:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has been revoked",
                headers={"WWW-Authenticate": "Bearer"},
            )

    user_id_var.set(user.id)
    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Raise 403 if the user account is disabled."""
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled",
        )
    return current_user
