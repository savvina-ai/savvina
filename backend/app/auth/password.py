# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Password hashing and verification using bcrypt (async, non-blocking)."""

from __future__ import annotations

import asyncio

import bcrypt


def _hash_sync(plain: str, rounds: int = 12) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=rounds)).decode()


def _verify_sync(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        # Malformed hash — treat as verification failure, not a bug
        return False


async def hash_password(plain: str, rounds: int = 12) -> str:
    """Return bcrypt hash of a plain-text password without blocking the event loop."""
    return await asyncio.to_thread(_hash_sync, plain, rounds)


async def verify_password(plain: str, hashed: str) -> bool:
    """Return True if plain matches the bcrypt hash, without blocking the event loop."""
    return await asyncio.to_thread(_verify_sync, plain, hashed)
