# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Authentication router — login, register, token refresh, password reset."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
import re
import secrets
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_active_user, get_token_payload
from ..auth.password import hash_password, verify_password
from ..auth.tokens import (
    create_access_token,
    create_refresh_token,
    hash_refresh_token,
)
from ..config import get_settings
from ..database import get_db
from ..models.app_settings import AppSetting
from ..models.chat import ChatMessage, ChatSession
from ..models.query_usage import QueryUsage
from ..models.user import RefreshToken, RevokedAccessToken, User
from ..schemas.auth import (
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    ResetPasswordRequest,
    SessionResponse,
    SetupStatusResponse,
    TokenPairResponse,
    UpdateMeRequest,
    UserResponse,
)
from ..schemas.pagination import PaginatedResponse

logger = logging.getLogger(__name__)

from ..auth.limiter import limiter  # noqa: E402

router = APIRouter()

_MIN_PASSWORD_LEN = 12
_REFRESH_COOKIE_NAME = "savvina_rt"


def _set_refresh_cookie(response: Response, raw_token: str, settings) -> None:
    """Write the refresh token into an HttpOnly cookie."""
    response.set_cookie(
        key=_REFRESH_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        secure=not settings.debug,
        samesite="strict",
        max_age=settings.refresh_token_expire_days * 86_400,
        path="/",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key=_REFRESH_COOKIE_NAME, path="/")


_SPECIAL_CHARS_RE = re.compile(r"[!@#$%^&*(),.?\":{}|<>\-_+=\[\]\\;'~/`]")

_USER_AGENT_MAX_LEN = 255


def _validate_password_strength(password: str) -> None:
    """Validate password meets minimum complexity requirements."""
    if len(password) < _MIN_PASSWORD_LEN:
        raise ValueError(f"Password must be at least {_MIN_PASSWORD_LEN} characters")
    if not re.search(r"[A-Z]", password):
        raise ValueError("Password must contain at least one uppercase letter")
    if not re.search(r"\d", password):
        raise ValueError("Password must contain at least one digit")
    if not _SPECIAL_CHARS_RE.search(password):
        raise ValueError("Password must contain at least one special character")


async def _get_bcrypt_rounds(db: AsyncSession) -> int:
    row = await db.scalar(select(AppSetting).where(AppSetting.key == "bcrypt_rounds"))
    return int(row.value) if row else 12


def _get_ip(request: Request) -> str | None:
    """Extract client IP, trusting X-Real-IP only from configured trusted proxies."""
    from app.auth.limiter import _is_trusted_proxy
    from app.config import get_settings

    direct_ip = request.client.host if request.client else None
    if direct_ip and _is_trusted_proxy(direct_ip, get_settings().trusted_proxies):
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()
    return direct_ip


def _build_user_response(user: User) -> UserResponse:
    """Build UserResponse from User model."""
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
    )


async def _store_refresh_token(
    user_id: str,
    raw_token: str,
    db: AsyncSession,
    request: Request,
) -> None:
    """Persist a hashed refresh token to the database."""
    settings = get_settings()
    expires_at = datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)
    user_agent = request.headers.get("User-Agent", "")[:_USER_AGENT_MAX_LEN]
    rt = RefreshToken(
        user_id=user_id,
        token_hash=hash_refresh_token(raw_token),
        device_hint=user_agent or None,
        ip_address=_get_ip(request),
        expires_at=expires_at,
    )
    db.add(rt)
    await db.commit()


# ── Setup status ──────────────────────────────────────────────────────────────


@router.get("/setup-status", response_model=SetupStatusResponse)
@limiter.limit(get_settings().auth_rate_limit)
async def setup_status(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SetupStatusResponse:
    """Public endpoint returning whether this is a fresh deployment (no users yet)."""
    count = await db.scalar(select(func.count()).select_from(User))
    return SetupStatusResponse(needs_setup=count == 0)


# ── Register ──────────────────────────────────────────────────────────────────


@router.post("/register", response_model=LoginResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit(get_settings().auth_rate_limit)
async def register(
    request: Request,
    response: Response,
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    """First-boot only: create the initial admin account."""
    try:
        _validate_password_strength(body.password)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from None

    async with db.begin():
        # Lock ID = crc32b("savvina:register") = 0x1D107D12 = 7799283410.
        # Prevents two simultaneous /register requests from both seeing 0 users
        # and both succeeding — only one will win the lock, the other will block
        # then fail the count check.
        await db.execute(text("SELECT pg_advisory_xact_lock(7799283410)"))
        count = await db.scalar(select(func.count()).select_from(User))
        if count != 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Setup already complete. Please log in.",
            )

        existing = await db.scalar(select(User).where(User.email == body.email))
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email already exists",
            )

        hashed_pw = await hash_password(body.password, await _get_bcrypt_rounds(db))
        user = User(
            email=body.email,
            password_hash=hashed_pw,
            display_name=body.display_name,
            is_active=True,
        )
        db.add(user)

    raw_refresh = create_refresh_token()
    await _store_refresh_token(user.id, raw_refresh, db, request)

    settings = get_settings()
    _set_refresh_cookie(response, raw_refresh, settings)
    access = create_access_token(user_id=user.id)
    return LoginResponse(
        access_token=access,
        expires_in=settings.access_token_expire_minutes * 60,
        user=_build_user_response(user),
    )


# ── Login ─────────────────────────────────────────────────────────────────────


@router.post("/login", response_model=LoginResponse)
@limiter.limit(get_settings().auth_rate_limit)
async def login(
    request: Request,
    response: Response,
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    """Authenticate with email + password and return JWT token pair."""
    user = await db.scalar(select(User).where(User.email == body.email))
    if user is None or not await verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled",
        )

    raw_refresh = create_refresh_token()
    await _store_refresh_token(user.id, raw_refresh, db, request)

    settings = get_settings()
    _set_refresh_cookie(response, raw_refresh, settings)
    access = create_access_token(user_id=user.id)
    return LoginResponse(
        access_token=access,
        expires_in=settings.access_token_expire_minutes * 60,
        user=_build_user_response(user),
    )


# ── Refresh ───────────────────────────────────────────────────────────────────


@router.post("/refresh", response_model=TokenPairResponse)
@limiter.limit(get_settings().auth_rate_limit)
async def refresh_token(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenPairResponse:
    """Rotate refresh token — issues new pair, revokes old token.

    The refresh token is read exclusively from the HttpOnly ``savvina_rt``
    cookie.  Accepting it in the request body was removed (SEC-4) because
    plain-text tokens in request bodies are exposed to application logs.
    """
    raw_token = request.cookies.get(_REFRESH_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token is invalid or expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    now = datetime.now(UTC)
    token_hash = hash_refresh_token(raw_token)

    stored = await db.scalar(
        select(RefreshToken)
        .where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
            RefreshToken.rotated_at.is_(None),
        )
        # Blocking FOR UPDATE (no skip_locked) is intentional: two concurrent refresh
        # requests for the same token must serialise, not both succeed (CLAUDE.md rule).
        .with_for_update()
    )

    if stored is None:
        suspect = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
        if suspect is not None and suspect.rotated_at is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Token already rotated — use the latest refresh token",
            )
        if suspect is not None:
            all_tokens = await db.scalars(
                select(RefreshToken).where(
                    RefreshToken.user_id == suspect.user_id,
                    RefreshToken.revoked_at.is_(None),
                )
            )
            for t in all_tokens.all():
                t.revoked_at = now
            await db.commit()

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token is invalid or expired",
            headers={"WWW-Authenticate": "Bearer"},
        )

    stored.rotated_at = now
    stored.revoked_at = now

    user = await db.get(User, stored.user_id)
    if user is None or not user.is_active:
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or disabled",
        )

    raw_new = create_refresh_token()
    settings = get_settings()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_refresh_token(raw_new),
            device_hint=stored.device_hint,
            ip_address=_get_ip(request),
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days),
        )
    )
    await db.commit()

    _set_refresh_cookie(response, raw_new, settings)
    new_access = create_access_token(user_id=user.id)
    return TokenPairResponse(
        access_token=new_access,
        expires_in=settings.access_token_expire_minutes * 60,
    )


# ── Logout ────────────────────────────────────────────────────────────────────


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(get_settings().auth_rate_limit)
async def logout(
    request: Request,
    response: Response,
    payload: dict = Depends(get_token_payload),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke the current access token and the specified refresh token.

    The refresh token is read exclusively from the HttpOnly ``savvina_rt``
    cookie (SEC-4 — removed plain-text body fallback).
    """
    jti = payload.get("jti")
    exp_ts = payload.get("exp")
    if jti and exp_ts:
        db.add(RevokedAccessToken(jti=jti, expires_at=datetime.fromtimestamp(exp_ts, tz=UTC)))

    _clear_refresh_cookie(response)
    raw_token = request.cookies.get(_REFRESH_COOKIE_NAME)
    if raw_token:
        token_hash = hash_refresh_token(raw_token)
        stored = await db.scalar(
            select(RefreshToken).where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.user_id == current_user.id,
            )
        )
        if stored is not None:
            stored.revoked_at = datetime.now(UTC)
    await db.commit()


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(get_settings().auth_rate_limit)
async def logout_all(
    request: Request,
    response: Response,
    payload: dict = Depends(get_token_payload),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke all active refresh tokens and deny-list the current access token."""
    jti = payload.get("jti")
    exp_ts = payload.get("exp")
    if jti and exp_ts:
        db.add(RevokedAccessToken(jti=jti, expires_at=datetime.fromtimestamp(exp_ts, tz=UTC)))

    _clear_refresh_cookie(response)
    now = datetime.now(UTC)
    tokens = await db.scalars(
        select(RefreshToken).where(
            RefreshToken.user_id == current_user.id,
            RefreshToken.revoked_at.is_(None),
        )
    )
    for t in tokens.all():
        t.revoked_at = now
    await db.commit()


# ── Me ────────────────────────────────────────────────────────────────────────


@router.get("/me", response_model=UserResponse)
async def get_me(
    current_user: User = Depends(get_current_active_user),
) -> UserResponse:
    """Return the current authenticated user's profile."""
    return _build_user_response(current_user)


@router.put("/me", response_model=UserResponse)
@limiter.limit(get_settings().auth_rate_limit)
async def update_me(
    request: Request,
    body: UpdateMeRequest,
    payload: dict = Depends(get_token_payload),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Update the current user's display name and/or password."""
    if body.display_name is not None:
        current_user.display_name = body.display_name

    if body.new_password and not body.current_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="current_password is required to set a new password",
        )

    if body.current_password and body.new_password:
        if not await verify_password(body.current_password, current_user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current password is incorrect",
            )
        try:
            _validate_password_strength(body.new_password)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from None
        current_user.password_hash = await hash_password(
            body.new_password, await _get_bcrypt_rounds(db)
        )
        current_user.tokens_invalidated_at = datetime.now(UTC)
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == current_user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        jti = payload.get("jti")
        exp_ts = payload.get("exp")
        if jti and exp_ts:
            db.add(RevokedAccessToken(jti=jti, expires_at=datetime.fromtimestamp(exp_ts, tz=UTC)))

    await db.commit()
    return _build_user_response(current_user)


# ── Password reset ────────────────────────────────────────────────────────────


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(get_settings().auth_rate_limit)
async def reset_password(
    request: Request,
    body: ResetPasswordRequest,
    payload: dict = Depends(get_token_payload),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Set a new password for the authenticated user.

    CE: no current_password required — access token is the only credential gate.
    The access token used to authenticate this request is immediately revoked so
    a stolen token cannot be used to chain into account takeover (SEC-1 + SEC-3).
    """
    try:
        _validate_password_strength(body.password)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from None

    now = datetime.now(UTC)
    current_user.password_hash = await hash_password(body.password, await _get_bcrypt_rounds(db))
    current_user.tokens_invalidated_at = now
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == current_user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    jti = payload.get("jti")
    exp_ts = payload.get("exp")
    if jti and exp_ts:
        db.add(RevokedAccessToken(jti=jti, expires_at=datetime.fromtimestamp(exp_ts, tz=UTC)))
    await db.commit()


# ── Sessions ──────────────────────────────────────────────────────────────────


@router.get("/sessions", response_model=PaginatedResponse[SessionResponse])
async def list_sessions(
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[SessionResponse]:
    """List active (non-expired, non-revoked) refresh token sessions."""
    now = datetime.now(UTC)
    where_clause = [
        RefreshToken.user_id == current_user.id,
        RefreshToken.revoked_at.is_(None),
        RefreshToken.expires_at > now,
    ]
    total = (
        await db.scalar(select(func.count()).select_from(RefreshToken).where(*where_clause)) or 0
    )
    tokens = await db.scalars(select(RefreshToken).where(*where_clause).limit(limit).offset(offset))
    items = [
        SessionResponse(
            id=t.id,
            device_hint=t.device_hint,
            ip_address=t.ip_address,
            created_at=t.created_at.isoformat(),
            expires_at=t.expires_at.isoformat(),
        )
        for t in tokens.all()
    ]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke a specific refresh token session."""
    token = await db.scalar(
        select(RefreshToken).where(
            RefreshToken.id == session_id,
            RefreshToken.user_id == current_user.id,
        )
    )
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    token.revoked_at = datetime.now(UTC)
    await db.commit()


# ── Account deletion ──────────────────────────────────────────────────────────


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_account(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Account deletion — anonymise all PII.

    All writes are wrapped in a savepoint so any mid-sequence DB error rolls
    back atomically.  The outer transaction is committed explicitly after the
    savepoint releases (BUG-1 fix).  A ``SQLAlchemyError`` is caught to ensure
    the session is rolled back cleanly and a safe 500 is returned instead of a
    raw stack trace (QUAL-23 fix).
    """
    import uuid as _uuid

    user_id = current_user.id
    anon_email = f"deleted_{_uuid.uuid4().hex[:12]}@anonymized.local"
    dummy_hash = await hash_password(secrets.token_hex(32))

    try:
        async with db.begin_nested():
            await db.execute(
                update(RefreshToken)
                .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC))
            )

            session_ids_result = await db.execute(
                select(ChatSession.id).where(ChatSession.user_id == user_id)
            )
            session_ids = [row[0] for row in session_ids_result.all()]
            if session_ids:
                await db.execute(delete(ChatMessage).where(ChatMessage.session_id.in_(session_ids)))
                await db.execute(delete(ChatSession).where(ChatSession.user_id == user_id))

            await db.execute(delete(QueryUsage).where(QueryUsage.user_id == user_id))

            current_user.email = anon_email
            current_user.display_name = None
            current_user.password_hash = dummy_hash
            current_user.is_active = False

        await db.commit()  # BUG-1: persist the outer transaction to the database
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.exception("Account deletion failed for user %s", user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Account deletion failed; no changes were made.",
        ) from exc
