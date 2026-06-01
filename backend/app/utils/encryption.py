# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Fernet symmetric encryption helpers for storing sensitive credentials.

The encryption key must be a URL-safe base64-encoded 32-byte key as produced by
``Fernet.generate_key()``.  It is read from ``config.ENCRYPTION_KEY`` and must
never be logged or returned to clients.  Use ``generate_encryption_key()`` when
bootstrapping a new deployment.
"""

from cryptography.fernet import Fernet


def encrypt_value(value: str, key: str) -> bytes:
    """Encrypt a string value using Fernet symmetric encryption."""
    f = Fernet(key.encode() if isinstance(key, str) else key)
    return f.encrypt(value.encode())


def decrypt_value(encrypted: bytes, key: str) -> str:
    """Decrypt a Fernet-encrypted value back to a string."""
    f = Fernet(key.encode() if isinstance(key, str) else key)
    return f.decrypt(encrypted).decode()


def generate_encryption_key() -> str:
    """Generate a new Fernet encryption key."""
    return Fernet.generate_key().decode()
