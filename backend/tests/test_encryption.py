# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for app/utils/encryption.py."""

from cryptography.fernet import InvalidToken
import pytest

from app.utils.encryption import decrypt_value, encrypt_value, generate_encryption_key


class TestGenerateEncryptionKey:
    def test_returns_string(self):
        key = generate_encryption_key()
        assert isinstance(key, str)

    def test_key_is_non_empty(self):
        assert len(generate_encryption_key()) > 0

    def test_key_is_valid_fernet_key(self):
        from cryptography.fernet import Fernet

        key = generate_encryption_key()
        Fernet(key.encode())  # must not raise

    def test_keys_are_unique(self):
        assert generate_encryption_key() != generate_encryption_key()


class TestEncryptValue:
    def test_returns_bytes(self):
        key = generate_encryption_key()
        assert isinstance(encrypt_value("hello", key), bytes)

    def test_nondeterministic(self):
        """Fernet uses a random IV so encrypting the same value twice differs."""
        key = generate_encryption_key()
        assert encrypt_value("hello", key) != encrypt_value("hello", key)

    def test_different_keys_different_ciphertext(self):
        enc1 = encrypt_value("hello", generate_encryption_key())
        enc2 = encrypt_value("hello", generate_encryption_key())
        assert enc1 != enc2


class TestDecryptValue:
    def test_round_trip(self):
        key = generate_encryption_key()
        original = "super-secret-password-123"
        assert decrypt_value(encrypt_value(original, key), key) == original

    def test_empty_string_round_trip(self):
        key = generate_encryption_key()
        assert decrypt_value(encrypt_value("", key), key) == ""

    def test_unicode_round_trip(self):
        key = generate_encryption_key()
        original = "café résumé 日本語 🔐"
        assert decrypt_value(encrypt_value(original, key), key) == original

    def test_wrong_key_raises_invalid_token(self):
        key1 = generate_encryption_key()
        key2 = generate_encryption_key()
        encrypted = encrypt_value("secret", key1)
        with pytest.raises(InvalidToken):
            decrypt_value(encrypted, key2)

    def test_tampered_ciphertext_raises(self):
        key = generate_encryption_key()
        encrypted = bytearray(encrypt_value("secret", key))
        encrypted[10] ^= 0xFF  # flip bits
        with pytest.raises(InvalidToken):
            decrypt_value(bytes(encrypted), key)
