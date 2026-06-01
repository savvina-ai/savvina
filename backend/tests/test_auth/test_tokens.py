# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for JWT token utilities."""

from __future__ import annotations

from datetime import timedelta

from fastapi import HTTPException
import pytest

from app.auth.tokens import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    hash_refresh_token,
)


class TestCreateAccessToken:
    def test_returns_non_empty_string(self) -> None:
        token = create_access_token("user-1")
        assert isinstance(token, str) and len(token) > 0

    def test_decode_round_trip(self) -> None:
        token = create_access_token("user-1")
        payload = decode_access_token(token)
        assert payload["sub"] == "user-1"

    def test_expired_token_raises_401(self) -> None:
        token = create_access_token("user-1", expires_delta=timedelta(seconds=-1))
        with pytest.raises(HTTPException) as exc_info:
            decode_access_token(token)
        assert exc_info.value.status_code == 401


class TestCreateRefreshToken:
    def test_returns_non_empty_string(self) -> None:
        token = create_refresh_token()
        assert isinstance(token, str) and len(token) > 0

    def test_tokens_are_unique(self) -> None:
        assert create_refresh_token() != create_refresh_token()


class TestHashRefreshToken:
    def test_hash_is_64_chars(self) -> None:
        """sha256 hex digest is always exactly 64 characters."""
        token = create_refresh_token()
        assert len(hash_refresh_token(token)) == 64

    def test_same_input_same_hash(self) -> None:
        token = "deterministic-input"
        assert hash_refresh_token(token) == hash_refresh_token(token)

    def test_different_inputs_different_hashes(self) -> None:
        assert hash_refresh_token("token-a") != hash_refresh_token("token-b")
