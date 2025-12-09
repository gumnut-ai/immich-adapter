"""Unit tests for JWT encryption utilities."""

import pytest
from unittest.mock import patch, MagicMock

from cryptography.fernet import Fernet

from utils.jwt_encryption import (
    JWTEncryptionError,
    MissingEncryptionKeyError,
    clear_fernet_cache,
    decrypt_jwt,
    encrypt_jwt,
)


# Test JWT token
TEST_JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"

# Valid Fernet key for testing
TEST_FERNET_KEY = Fernet.generate_key().decode()


@pytest.fixture
def mock_settings():
    """Fixture that provides mocked settings with a valid encryption key."""
    with patch("utils.jwt_encryption.get_settings") as mock:
        mock.return_value = MagicMock(session_encryption_key=TEST_FERNET_KEY)
        clear_fernet_cache()
        yield mock
        clear_fernet_cache()


@pytest.fixture
def mock_settings_no_key():
    """Fixture that provides mocked settings with no encryption key."""
    with patch("utils.jwt_encryption.get_settings") as mock:
        mock.return_value = MagicMock(session_encryption_key=None)
        clear_fernet_cache()
        yield mock
        clear_fernet_cache()


class TestEncryptDecrypt:
    """Tests for encrypt_jwt and decrypt_jwt."""

    def test_encrypt_and_decrypt_roundtrip(self, mock_settings):
        """Test that encrypting then decrypting returns original value."""
        encrypted = encrypt_jwt(TEST_JWT)
        decrypted = decrypt_jwt(encrypted)

        assert decrypted == TEST_JWT

    def test_encrypted_value_differs_from_original(self, mock_settings):
        """Test that encrypted value is different from original."""
        encrypted = encrypt_jwt(TEST_JWT)

        assert encrypted != TEST_JWT
        # Fernet output is base64-encoded
        assert encrypted.startswith("gAAAAA")

    def test_encrypt_different_each_time(self, mock_settings):
        """Test that encrypting same value produces different ciphertext each time."""
        encrypted1 = encrypt_jwt(TEST_JWT)
        # Clear cache to get fresh Fernet (same key)
        clear_fernet_cache()
        encrypted2 = encrypt_jwt(TEST_JWT)

        # Fernet includes a timestamp, so same input produces different output
        assert encrypted1 != encrypted2

        # But both should decrypt to the same value
        decrypted1 = decrypt_jwt(encrypted1)
        clear_fernet_cache()
        decrypted2 = decrypt_jwt(encrypted2)
        assert decrypted1 == decrypted2 == TEST_JWT

    def test_decrypt_with_wrong_key_raises_error(self, mock_settings):
        """Test that decrypting with wrong key raises JWTEncryptionError."""
        # Encrypt with the fixture's key
        encrypted = encrypt_jwt(TEST_JWT)

        clear_fernet_cache()

        # Try to decrypt with a different key
        different_key = Fernet.generate_key().decode()
        mock_settings.return_value.session_encryption_key = different_key

        with pytest.raises(JWTEncryptionError) as exc_info:
            decrypt_jwt(encrypted)

        assert "invalid token or wrong encryption key" in str(exc_info.value)

    def test_decrypt_invalid_token_raises_error(self, mock_settings):
        """Test that decrypting invalid token raises JWTEncryptionError."""
        with pytest.raises(JWTEncryptionError) as exc_info:
            decrypt_jwt("not-a-valid-encrypted-token")

        assert "invalid token" in str(exc_info.value).lower()

    def test_encrypt_empty_string(self, mock_settings):
        """Test encrypting and decrypting empty string."""
        encrypted = encrypt_jwt("")
        decrypted = decrypt_jwt(encrypted)

        assert decrypted == ""


class TestMissingEncryptionKey:
    """Tests for missing encryption key behavior."""

    def test_encrypt_raises_error_when_key_not_configured(self, mock_settings_no_key):
        """Test that MissingEncryptionKeyError is raised when key is not set."""
        with pytest.raises(MissingEncryptionKeyError) as exc_info:
            encrypt_jwt(TEST_JWT)

        assert "SESSION_ENCRYPTION_KEY is required" in str(exc_info.value)

    def test_decrypt_raises_error_when_key_not_configured(self, mock_settings_no_key):
        """Test that MissingEncryptionKeyError is raised on decrypt when key is not set."""
        with pytest.raises(MissingEncryptionKeyError) as exc_info:
            decrypt_jwt("some-encrypted-token")

        assert "SESSION_ENCRYPTION_KEY is required" in str(exc_info.value)

    def test_error_message_includes_generation_instructions(self, mock_settings_no_key):
        """Test that error message includes key generation instructions."""
        with pytest.raises(MissingEncryptionKeyError) as exc_info:
            encrypt_jwt(TEST_JWT)

        assert "Fernet.generate_key()" in str(exc_info.value)


class TestJWTEncryptionError:
    """Tests for JWTEncryptionError exception."""

    def test_error_is_exception(self):
        """Test that JWTEncryptionError is an Exception."""
        error = JWTEncryptionError("test error")
        assert isinstance(error, Exception)
        assert str(error) == "test error"

    def test_error_preserves_cause(self):
        """Test that JWTEncryptionError preserves the cause."""
        original_error = ValueError("original error")

        try:
            raise JWTEncryptionError("wrapper error") from original_error
        except JWTEncryptionError as e:
            assert e.__cause__ is original_error
