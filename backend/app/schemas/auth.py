# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Pydantic schemas for authentication endpoints."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr


class RegisterRequest(BaseModel):
    """Request body for POST /api/auth/register."""

    email: EmailStr
    password: str
    display_name: str | None = None


class LoginRequest(BaseModel):
    """Request body for POST /api/auth/login."""

    email: str
    password: str


class ResetPasswordRequest(BaseModel):
    """Request body for POST /api/auth/reset-password.

    CE (Community Edition): no current_password required — single-user,
    self-hosted deployment. The endpoint is protected by a valid access token.
    """

    password: str


class UpdateMeRequest(BaseModel):
    """Request body for PUT /api/auth/me."""

    display_name: str | None = None
    current_password: str | None = None
    new_password: str | None = None


class UserResponse(BaseModel):
    """User profile included in login response and GET /auth/me."""

    id: str
    email: str
    display_name: str | None

    model_config = {"from_attributes": True}


class LoginResponse(BaseModel):
    """Response for login and register endpoints."""

    access_token: str
    token_type: str = "bearer"  # noqa: S105
    expires_in: int
    user: UserResponse


class TokenPairResponse(BaseModel):
    """Response for token refresh."""

    access_token: str
    token_type: str = "bearer"  # noqa: S105
    expires_in: int


class SessionResponse(BaseModel):
    """Active refresh token session info."""

    id: str
    device_hint: str | None
    ip_address: str | None
    created_at: str
    expires_at: str

    model_config = {"from_attributes": True}


class SetupStatusResponse(BaseModel):
    """Response for GET /api/auth/setup-status."""

    needs_setup: bool
