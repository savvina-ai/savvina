# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for password hashing utilities."""

from __future__ import annotations

from app.auth.password import hash_password, verify_password


class TestHashPassword:
    async def test_round_trip(self) -> None:
        """hash_password + verify_password must succeed for the same password."""
        pw = "super-secret-password"
        hashed = await hash_password(pw)
        assert await verify_password(pw, hashed)

    async def test_wrong_password_returns_false(self) -> None:
        """verify_password must return False for a wrong password."""
        hashed = await hash_password("correct-horse-battery-staple")
        assert not await verify_password("wrong-password", hashed)

    async def test_hash_format_is_bcrypt(self) -> None:
        """Hash must start with the bcrypt identifier $2b$."""
        hashed = await hash_password("any-password")
        assert hashed.startswith("$2b$")
