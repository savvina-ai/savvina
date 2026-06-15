# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""JWT creation, decoding, and refresh token utilities."""

from datetime import UTC, datetime, timedelta
import hashlib
import secrets
import uuid

from fastapi import HTTPException, status
import jwt
from jwt.exceptions import PyJWTError

from ..config import get_settings


def create_access_token(
    user_id: str,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a signed JWT access token.

    Payload includes jti (unique token ID) for future per-token revocation.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    expire = now + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=settings.access_token_expire_minutes)
    )
    payload = {
        "sub": user_id,
        "type": "access",
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": expire,
    }
    if settings.jwt_secret_key is None:  # pragma: no cover
        raise RuntimeError("jwt_secret_key must be set by resolve_jwt_secret")
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


_REFRESH_TOKEN_BYTES = 64


def create_refresh_token() -> str:
    """Return a cryptographically secure random refresh token (raw, not hashed)."""
    return secrets.token_urlsafe(_REFRESH_TOKEN_BYTES)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT access token.

    Raises HTTPException(401) on any validation failure.
    """
    settings = get_settings()
    if settings.jwt_secret_key is None:  # pragma: no cover
        raise RuntimeError("jwt_secret_key must be set by resolve_jwt_secret")
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


def hash_refresh_token(token: str) -> str:
    """Return the sha256 hex digest of the raw refresh token.

    sha256 is correct here — fast lookup key, not a password store.
    The resulting digest is always exactly 64 hex characters.
    """
    return hashlib.sha256(token.encode()).hexdigest()
