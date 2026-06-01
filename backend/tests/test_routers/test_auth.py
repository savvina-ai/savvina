# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for /api/v1/auth endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from httpx import AsyncClient

from app.auth.dependencies import get_current_active_user
from app.database import get_db
from app.main import app
from app.models.user import RefreshToken, User

from .conftest import MockResult, _default_user, _mock_db

# ── POST /api/v1/auth/register ───────────────────────────────────────────────────


_VALID_REGISTER_PAYLOAD: dict[str, str] = {
    "email": "new@example.com",
    "password": "Long-enough-1!",
}


class TestRegister:
    async def test_short_password_returns_400(self, http_client) -> None:
        payload = {**_VALID_REGISTER_PAYLOAD, "email": "a@b.com", "password": "short"}
        resp = await http_client.post("/api/v1/auth/register", json=payload)
        assert resp.status_code == 400

    async def test_duplicate_email_returns_409(self, http_client) -> None:
        existing_user = MagicMock(spec=User)
        db = MagicMock()
        # scalar side_effect: [bcrypt_rounds lookup, user count check, existing user lookup]
        db.scalar = AsyncMock(side_effect=[None, 0, existing_user])
        db.commit = AsyncMock()
        db.flush = AsyncMock()
        db.add = MagicMock()
        db.execute = AsyncMock(return_value=MagicMock())
        app.dependency_overrides[get_db] = lambda: db
        payload = {**_VALID_REGISTER_PAYLOAD, "email": "dup@example.com"}
        resp = await http_client.post("/api/v1/auth/register", json=payload)
        assert resp.status_code == 409

    async def test_success_returns_201(self, http_client) -> None:
        from app.schemas.auth import UserResponse

        mock_user_resp = UserResponse(
            id="test-user-id",
            email="new@example.com",
            display_name=None,
        )

        db = MagicMock()
        # side_effect: [user count check, email uniqueness check, bcrypt_rounds lookup]
        db.scalar = AsyncMock(side_effect=[0, None, None])
        db.commit = AsyncMock()
        db.flush = AsyncMock()
        db.add = MagicMock()
        db.execute = AsyncMock(return_value=MockResult(rows=[]))

        app.dependency_overrides[get_db] = lambda: db
        with (
            patch(
                "app.routers.auth._build_user_response",
                return_value=mock_user_resp,
            ),
            patch("app.routers.auth._store_refresh_token", new=AsyncMock()),
        ):
            resp = await http_client.post(
                "/api/v1/auth/register",
                json=_VALID_REGISTER_PAYLOAD,
            )
        assert resp.status_code == 201


# ── POST /api/v1/auth/login ──────────────────────────────────────────────────────


class TestLogin:
    async def test_missing_fields_returns_422(self, http_client) -> None:
        resp = await http_client.post("/api/v1/auth/login", json={})
        assert resp.status_code == 422

    async def test_unknown_email_returns_401(self, http_client) -> None:
        db = MagicMock()
        db.scalar = AsyncMock(return_value=None)
        db.execute = AsyncMock(return_value=MockResult(rows=[]))
        db.add = MagicMock()
        db.commit = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "irrelevant"},
        )
        assert resp.status_code == 401

    async def test_wrong_password_returns_401(self, http_client) -> None:
        from app.auth.password import hash_password

        user = MagicMock(spec=User)
        user.id = "u1"
        user.password_hash = await hash_password("correct-password")
        user.is_active = True

        db = MagicMock()
        db.scalar = AsyncMock(return_value=user)
        db.execute = AsyncMock(return_value=MockResult(rows=[]))
        db.add = MagicMock()
        db.commit = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/auth/login",
            json={"email": "user@example.com", "password": "wrong-password"},
        )
        assert resp.status_code == 401

    async def test_inactive_user_returns_403(self, http_client) -> None:
        from app.auth.password import hash_password

        user = MagicMock(spec=User)
        user.id = "u1"
        user.password_hash = await hash_password("correct-password")
        user.is_active = False

        db = MagicMock()
        db.scalar = AsyncMock(return_value=user)
        db.execute = AsyncMock(return_value=MockResult(rows=[]))
        db.add = MagicMock()
        db.commit = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.post(
            "/api/v1/auth/login",
            json={"email": "inactive@example.com", "password": "correct-password"},
        )
        assert resp.status_code == 403


# ── POST /api/v1/auth/logout ─────────────────────────────────────────────────────


class TestLogout:
    async def test_returns_204(self, http_client) -> None:
        db = MagicMock()
        db.scalar = AsyncMock(return_value=None)
        db.commit = AsyncMock()
        db.add = MagicMock()
        db.execute = AsyncMock(return_value=MockResult(rows=[]))
        app.dependency_overrides[get_db] = lambda: db
        # SEC-4: refresh token must be sent via HttpOnly cookie, not request body
        resp = await http_client.post(
            "/api/v1/auth/logout",
            headers={"Cookie": "savvina_rt=some-token"},
        )
        assert resp.status_code == 204


# ── GET /api/v1/auth/me ──────────────────────────────────────────────────────────


class TestGetMe:
    async def test_returns_200(self, http_client) -> None:
        user = _default_user()
        app.dependency_overrides[get_current_active_user] = lambda: user

        db = MagicMock()
        db.execute = AsyncMock(return_value=MockResult(rows=[]))
        app.dependency_overrides[get_db] = lambda: db

        resp = await http_client.get("/api/v1/auth/me")
        assert resp.status_code == 200
        body = resp.json()
        assert "email" in body

    async def test_unauthenticated_returns_401(self, http_client) -> None:
        """Removing auth override causes the real dependency to run — 401 with no JWT."""
        app.dependency_overrides.clear()
        db = MagicMock()
        db.scalar = AsyncMock(return_value=None)
        db.execute = AsyncMock(return_value=MockResult(rows=[]))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/auth/me")
        assert resp.status_code == 401


# ── GET /api/v1/auth/sessions ────────────────────────────────────────────────────


class TestGetSessions:
    async def test_returns_200(self, http_client) -> None:
        token = MagicMock(spec=RefreshToken)
        token.id = "tok-1"
        token.device_hint = "Chrome"
        token.ip_address = "127.0.0.1"
        token.created_at = datetime.now(UTC)
        token.expires_at = datetime.now(UTC) + timedelta(days=30)

        scalars_result = MagicMock()
        scalars_result.all = MagicMock(return_value=[token])

        db = MagicMock()
        db.scalar = AsyncMock(return_value=1)
        db.scalars = AsyncMock(return_value=scalars_result)
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/auth/sessions")
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert isinstance(body["items"], list)
        assert body["total"] == 1


# ── C-4: X-Real-IP used instead of X-Forwarded-For ───────────────────────────


class TestGetIpUsesRealIP:
    """C-4: _get_ip() and the rate-limiter key must read X-Real-IP, not X-Forwarded-For."""

    def _make_request(self, headers: dict) -> MagicMock:
        """Build a minimal mock Request with the given headers."""
        req = MagicMock()
        req.headers = headers
        req.client = MagicMock()
        req.client.host = "10.0.0.1"
        return req

    def test_returns_x_real_ip_when_present(self) -> None:
        from app.routers.auth import _get_ip

        req = self._make_request({"X-Real-IP": "203.0.113.5"})
        assert _get_ip(req) == "203.0.113.5"

    def test_ignores_x_forwarded_for_when_x_real_ip_present(self) -> None:
        """A spoofed XFF must not override the nginx-set X-Real-IP."""
        from app.routers.auth import _get_ip

        req = self._make_request({"X-Real-IP": "203.0.113.5", "X-Forwarded-For": "1.2.3.4"})
        assert _get_ip(req) == "203.0.113.5"

    def test_falls_back_to_client_host_when_no_x_real_ip(self) -> None:
        from app.routers.auth import _get_ip

        req = self._make_request({})
        assert _get_ip(req) == "10.0.0.1"

    def test_returns_none_when_no_x_real_ip_and_no_client(self) -> None:
        from app.routers.auth import _get_ip

        req = MagicMock()
        req.headers = {}
        req.client = None
        assert _get_ip(req) is None

    def test_limiter_key_uses_x_real_ip(self) -> None:
        """Rate-limiter key function must return X-Real-IP, not XFF."""
        from app.auth.limiter import _real_ip

        req = self._make_request({"X-Real-IP": "203.0.113.99", "X-Forwarded-For": "9.9.9.9"})
        assert _real_ip(req) == "203.0.113.99"

    def test_limiter_key_falls_back_to_client_host(self) -> None:
        from app.auth.limiter import _real_ip

        req = self._make_request({})
        assert _real_ip(req) == "10.0.0.1"

    def test_limiter_key_returns_loopback_when_no_client(self) -> None:
        from app.auth.limiter import _real_ip

        req = MagicMock()
        req.headers = {}
        req.client = None
        assert _real_ip(req) == "127.0.0.1"


# ── Password reset & refresh revocation ─────────────────────────────────────


class TestPasswordReset:
    async def test_reset_password_updates_user_and_revokes_tokens(
        self, http_client: AsyncClient
    ) -> None:
        """Authenticated reset sets new password and revokes all refresh tokens (CE: no current_password)."""  # noqa: E501
        user = MagicMock(spec=User)
        user.id = "u1"
        user.is_active = True
        user.password_hash = "old-hash"

        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        db.scalar = AsyncMock(return_value=None)
        app.dependency_overrides[get_current_active_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: db

        with patch("app.routers.auth.hash_password", new=AsyncMock(return_value="new-hash")):
            resp = await http_client.post(
                "/api/v1/auth/reset-password",
                json={"password": "Reset-Pass-1!"},
            )
        assert resp.status_code == 204
        assert user.password_hash == "new-hash"
        db.execute.assert_called_once()
        db.commit.assert_called_once()

    async def test_reset_unauthenticated_returns_401(self, http_client: AsyncClient) -> None:
        """Unauthenticated request must be rejected."""
        app.dependency_overrides.clear()
        db = MagicMock()
        db.scalar = AsyncMock(return_value=None)
        app.dependency_overrides[get_db] = lambda: db

        resp = await http_client.post(
            "/api/v1/auth/reset-password",
            json={"password": "Reset-Pass-1!"},
        )
        assert resp.status_code == 401


class TestRefreshRotation:
    async def test_refresh_marks_token_rotated_and_revoked(self, http_client: AsyncClient) -> None:
        """Valid refresh rotates the row (audit) and revokes the old token."""
        now = datetime.now(UTC)
        stored = MagicMock(spec=RefreshToken)
        stored.user_id = "u1"
        stored.device_hint = "device"
        stored.rotated_at = None
        stored.revoked_at = None
        stored.expires_at = now + timedelta(days=7)

        user = MagicMock(spec=User)
        user.id = "u1"
        user.is_active = True

        db = MagicMock()
        db.scalar = AsyncMock(return_value=stored)
        db.get = AsyncMock(return_value=user)
        db.add = MagicMock()
        db.commit = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db

        # SEC-4: refresh token must be sent via HttpOnly cookie, not request body
        resp = await http_client.post(
            "/api/v1/auth/refresh",
            headers={"Cookie": "savvina_rt=old-refresh-raw"},
        )
        assert resp.status_code == 200
        assert stored.rotated_at is not None
        assert stored.revoked_at is not None
        db.add.assert_called_once()


# ── GDPR account delete ───────────────────────────────────────────────────────


class TestGDPRDelete:
    async def test_returns_204_and_deactivates_user(self, http_client: AsyncClient) -> None:
        """Account deletion anonymises the user record and deactivates the account."""
        user = _default_user()
        user.email = "before@example.com"
        user.is_active = True
        app.dependency_overrides[get_current_active_user] = lambda: user

        db = _mock_db(
            MockResult(),  # UPDATE RefreshToken (revoke all)
            MockResult(rows=[]),  # SELECT ChatSession.id (no sessions)
            MockResult(),  # DELETE QueryUsage
        )
        app.dependency_overrides[get_db] = lambda: db

        with patch("app.routers.auth.hash_password", new=AsyncMock(return_value="dummy-hash")):
            resp = await http_client.delete("/api/v1/auth/me")
        assert resp.status_code == 204
        assert user.is_active is False
        assert user.email != "before@example.com"
        assert "anonymized.local" in user.email
        assert user.password_hash == "dummy-hash"
        db.commit.assert_called_once()  # BUG-1: outer transaction must be committed

    async def test_revokes_refresh_tokens_via_execute(self, http_client: AsyncClient) -> None:
        """First execute inside the transaction is the token revocation UPDATE."""
        user = _default_user()
        app.dependency_overrides[get_current_active_user] = lambda: user

        db = _mock_db(
            MockResult(),  # UPDATE RefreshToken
            MockResult(rows=[]),  # SELECT session_ids
            MockResult(),  # DELETE QueryUsage
        )
        app.dependency_overrides[get_db] = lambda: db

        resp = await http_client.delete("/api/v1/auth/me")
        assert resp.status_code == 204
        assert db.execute.call_count == 3  # 3 DB operations inside begin_nested()

    async def test_with_sessions_deletes_messages_and_sessions(
        self, http_client: AsyncClient
    ) -> None:
        """When chat sessions exist, both messages and sessions are deleted."""
        user = _default_user()
        app.dependency_overrides[get_current_active_user] = lambda: user

        session_row = MagicMock()
        session_row.__getitem__ = lambda self, idx: "sess-id-1"  # row[0]

        db = _mock_db(
            MockResult(),  # UPDATE RefreshToken
            MockResult(rows=[session_row]),  # SELECT ChatSession.id (one session)
            MockResult(),  # DELETE ChatMessage
            MockResult(),  # DELETE ChatSession
            MockResult(),  # DELETE QueryUsage
        )
        app.dependency_overrides[get_db] = lambda: db

        resp = await http_client.delete("/api/v1/auth/me")
        assert resp.status_code == 204
        assert db.execute.call_count == 5  # includes message + session deletes
        db.commit.assert_called_once()  # BUG-1: outer transaction must be committed

    async def test_db_error_returns_500_and_rolls_back(self, http_client: AsyncClient) -> None:
        """SQLAlchemy error during deletion rolls back cleanly and returns 500 (QUAL-23)."""
        from sqlalchemy.exc import SQLAlchemyError

        user = _default_user()
        app.dependency_overrides[get_current_active_user] = lambda: user

        db = _mock_db()
        db.execute = AsyncMock(side_effect=SQLAlchemyError("simulated DB failure"))
        app.dependency_overrides[get_db] = lambda: db

        resp = await http_client.delete("/api/v1/auth/me")

        assert resp.status_code == 500
        assert "Account deletion failed" in resp.json()["detail"]
        db.rollback.assert_called_once()
        db.commit.assert_not_called()


# ── PUT /api/v1/auth/me — password change ────────────────────────────────────


class TestUpdateMe:
    async def test_password_change_revokes_refresh_tokens(self, http_client: AsyncClient) -> None:
        """Changing password must revoke all existing refresh tokens (M-04)."""
        user = MagicMock(spec=User)
        user.id = "u1"
        user.email = "user@example.com"
        user.display_name = None
        user.password_hash = "old-hash"

        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        db.scalar = AsyncMock(return_value=None)
        app.dependency_overrides[get_current_active_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: db

        with (
            patch("app.routers.auth.verify_password", new=AsyncMock(return_value=True)),
            patch("app.routers.auth.hash_password", new=AsyncMock(return_value="new-hash")),
        ):
            resp = await http_client.put(
                "/api/v1/auth/me",
                json={"current_password": "OldPass-1!", "new_password": "NewPass-Strong-1!"},
            )

        assert resp.status_code == 200
        assert user.password_hash == "new-hash"
        # Must execute exactly one UPDATE to revoke refresh tokens
        db.execute.assert_called_once()
        db.commit.assert_called_once()

    async def test_display_name_change_does_not_revoke_tokens(
        self, http_client: AsyncClient
    ) -> None:
        """Updating display_name only must NOT touch refresh tokens."""
        user = MagicMock(spec=User)
        user.id = "u1"
        user.email = "user@example.com"
        user.display_name = "Old Name"
        user.password_hash = "hash"

        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        app.dependency_overrides[get_current_active_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: db

        resp = await http_client.put(
            "/api/v1/auth/me",
            json={"display_name": "New Name"},
        )

        assert resp.status_code == 200
        assert user.display_name == "New Name"
        # No token revocation for a display-name-only update
        db.execute.assert_not_called()
        db.commit.assert_called_once()


# ── SEC-1: Access token jti deny-list ────────────────────────────────────────


class TestJtiRevocation:
    """SEC-1: Verify access token jti is added to the deny-list on security-sensitive actions."""

    async def test_logout_adds_jti_to_deny_list(self, http_client: AsyncClient) -> None:
        """Logout must insert a RevokedAccessToken row for the current token's jti."""
        from app.models.user import RevokedAccessToken

        from .conftest import _MOCK_TOKEN_PAYLOAD

        db = MagicMock()
        db.scalar = AsyncMock(return_value=None)
        db.commit = AsyncMock()
        db.add = MagicMock()
        app.dependency_overrides[get_db] = lambda: db

        # SEC-4: refresh token must be sent via HttpOnly cookie, not request body
        resp = await http_client.post(
            "/api/v1/auth/logout",
            headers={"Cookie": "savvina_rt=tok"},
        )
        assert resp.status_code == 204

        db.add.assert_called_once()
        added = db.add.call_args[0][0]
        assert isinstance(added, RevokedAccessToken)
        assert added.jti == _MOCK_TOKEN_PAYLOAD["jti"]

    async def test_reset_password_adds_jti_to_deny_list(self, http_client: AsyncClient) -> None:
        """reset_password must deny-list the current access token jti (SEC-1 + SEC-3 fix)."""
        from app.models.user import RevokedAccessToken

        from .conftest import _MOCK_TOKEN_PAYLOAD

        user = MagicMock(spec=User)
        user.id = "u1"
        user.is_active = True
        user.password_hash = "old"

        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        db.scalar = AsyncMock(return_value=None)
        app.dependency_overrides[get_current_active_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: db

        with patch("app.routers.auth.hash_password", new=AsyncMock(return_value="new-hash")):
            resp = await http_client.post(
                "/api/v1/auth/reset-password",
                json={"password": "Reset-Pass-1!"},
            )
        assert resp.status_code == 204

        db.add.assert_called_once()
        added = db.add.call_args[0][0]
        assert isinstance(added, RevokedAccessToken)
        assert added.jti == _MOCK_TOKEN_PAYLOAD["jti"]

    async def test_password_change_in_update_me_adds_jti(self, http_client: AsyncClient) -> None:
        """PUT /me with new_password must deny-list the current access token jti."""
        from app.models.user import RevokedAccessToken

        from .conftest import _MOCK_TOKEN_PAYLOAD

        user = MagicMock(spec=User)
        user.id = "u1"
        user.email = "user@example.com"
        user.display_name = None
        user.password_hash = "old-hash"

        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        db.scalar = AsyncMock(return_value=None)
        app.dependency_overrides[get_current_active_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: db

        with (
            patch("app.routers.auth.verify_password", new=AsyncMock(return_value=True)),
            patch("app.routers.auth.hash_password", new=AsyncMock(return_value="new-hash")),
        ):
            resp = await http_client.put(
                "/api/v1/auth/me",
                json={"current_password": "OldPass-1!", "new_password": "NewPass-Strong-1!"},
            )

        assert resp.status_code == 200
        db.add.assert_called_once()
        added = db.add.call_args[0][0]
        assert isinstance(added, RevokedAccessToken)
        assert added.jti == _MOCK_TOKEN_PAYLOAD["jti"]

    async def test_display_name_change_does_not_add_jti(self, http_client: AsyncClient) -> None:
        """Non-password PUT /me must NOT insert a deny-list entry."""
        user = MagicMock(spec=User)
        user.id = "u1"
        user.email = "user@example.com"
        user.display_name = "Old"
        user.password_hash = "hash"

        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        app.dependency_overrides[get_current_active_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: db

        resp = await http_client.put("/api/v1/auth/me", json={"display_name": "New"})
        assert resp.status_code == 200
        db.add.assert_not_called()

    async def test_revoked_jti_rejected(self) -> None:
        """get_current_user must raise HTTP 401 when the token's jti is in the deny-list."""
        import jwt as _jwt
        import pytest

        from app.auth.dependencies import get_current_user
        from app.auth.tokens import create_access_token
        from app.config import get_settings
        from app.models.user import RevokedAccessToken

        token = create_access_token("u1")
        settings = get_settings()
        payload = _jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        jti = payload["jti"]

        revoked = MagicMock(spec=RevokedAccessToken)
        revoked.jti = jti

        db = MagicMock()
        db.scalar = AsyncMock(return_value=revoked)

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(token=token, db=db)
        assert exc_info.value.status_code == 401
        assert "revoked" in exc_info.value.detail.lower()

    # ── SEC-3: tokens_invalidated_at cross-session revocation ────────────────

    async def test_password_change_sets_tokens_invalidated_at(
        self, http_client: AsyncClient
    ) -> None:
        """PUT /me password change must set tokens_invalidated_at on the user (SEC-3)."""
        user = MagicMock(spec=User)
        user.id = "u1"
        user.email = "user@example.com"
        user.display_name = None
        user.password_hash = "old-hash"
        user.tokens_invalidated_at = None

        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        db.scalar = AsyncMock(return_value=None)
        app.dependency_overrides[get_current_active_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: db

        with (
            patch("app.routers.auth.verify_password", new=AsyncMock(return_value=True)),
            patch("app.routers.auth.hash_password", new=AsyncMock(return_value="new-hash")),
        ):
            resp = await http_client.put(
                "/api/v1/auth/me",
                json={"current_password": "OldPass-1!", "new_password": "NewPass-Strong-1!"},
            )

        assert resp.status_code == 200
        assert user.tokens_invalidated_at is not None

    async def test_reset_password_sets_tokens_invalidated_at(
        self, http_client: AsyncClient
    ) -> None:
        """POST /reset-password must set tokens_invalidated_at on the user (SEC-3)."""
        user = MagicMock(spec=User)
        user.id = "u1"
        user.is_active = True
        user.password_hash = "old"
        user.tokens_invalidated_at = None

        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        db.scalar = AsyncMock(return_value=None)
        app.dependency_overrides[get_current_active_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: db

        with patch("app.routers.auth.hash_password", new=AsyncMock(return_value="new-hash")):
            resp = await http_client.post(
                "/api/v1/auth/reset-password",
                json={"password": "Reset-Pass-1!"},
            )

        assert resp.status_code == 204
        assert user.tokens_invalidated_at is not None

    async def test_token_issued_before_invalidation_is_rejected(self) -> None:
        """get_current_user must raise HTTP 401 when token iat precedes tokens_invalidated_at."""
        import pytest

        from app.auth.dependencies import get_current_user
        from app.auth.tokens import create_access_token
        from tests.test_routers.conftest import MockResult

        token = create_access_token("u1")

        user = MagicMock(spec=User)
        user.id = "u1"
        user.is_active = True
        # Set invalidation timestamp in the future relative to the just-issued token's iat
        user.tokens_invalidated_at = datetime.now(UTC) + timedelta(seconds=60)

        db = MagicMock()
        db.scalar = AsyncMock(return_value=None)  # jti not in deny-list
        db.execute = AsyncMock(return_value=MockResult(single=user))

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(token=token, db=db)
        assert exc_info.value.status_code == 401
        assert "revoked" in exc_info.value.detail.lower()

    async def test_token_issued_after_invalidation_is_accepted(self) -> None:
        """get_current_user must allow tokens whose iat is after tokens_invalidated_at."""

        from app.auth.dependencies import get_current_user
        from app.auth.tokens import create_access_token
        from tests.test_routers.conftest import MockResult

        token = create_access_token("u1")

        user = MagicMock(spec=User)
        user.id = "u1"
        user.is_active = True
        # Invalidation was in the past — new token is valid
        user.tokens_invalidated_at = datetime.now(UTC) - timedelta(hours=1)

        db = MagicMock()
        db.scalar = AsyncMock(return_value=None)  # jti not in deny-list
        db.execute = AsyncMock(return_value=MockResult(single=user))

        result = await get_current_user(token=token, db=db)
        assert result is user
