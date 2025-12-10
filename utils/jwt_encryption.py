"""JWT encryption utilities using Fernet symmetric encryption."""

import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from config.settings import get_settings

logger = logging.getLogger(__name__)


class JWTEncryptionError(Exception):
    """Raised when JWT encryption or decryption fails."""

    pass


class MissingEncryptionKeyError(Exception):
    """Raised when SESSION_ENCRYPTION_KEY is not configured."""

    pass


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    """
    Get or create a Fernet instance for JWT encryption.

    Requires SESSION_ENCRYPTION_KEY to be set in environment/settings.

    Returns:
        Fernet instance for encryption/decryption

    Raises:
        MissingEncryptionKeyError: If SESSION_ENCRYPTION_KEY is not set
    """
    settings = get_settings()
    key = settings.session_encryption_key

    if key is None:
        raise MissingEncryptionKeyError(
            "SESSION_ENCRYPTION_KEY is required but not set. "
            'Generate a key with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )

    return Fernet(key)


def encrypt_jwt(jwt: str) -> str:
    """
    Encrypt a JWT token for secure storage.

    Args:
        jwt: The JWT token to encrypt

    Returns:
        Base64-encoded encrypted token

    Raises:
        MissingEncryptionKeyError: If SESSION_ENCRYPTION_KEY is not set
        JWTEncryptionError: If encryption fails
    """
    try:
        fernet = _get_fernet()
        encrypted = fernet.encrypt(jwt.encode())
        return encrypted.decode()
    except MissingEncryptionKeyError:
        raise
    except Exception as e:
        raise JWTEncryptionError(f"Failed to encrypt JWT: {e}") from e


def decrypt_jwt(encrypted_jwt: str) -> str:
    """
    Decrypt an encrypted JWT token.

    Args:
        encrypted_jwt: The base64-encoded encrypted token

    Returns:
        The decrypted JWT token

    Raises:
        MissingEncryptionKeyError: If SESSION_ENCRYPTION_KEY is not set
        JWTEncryptionError: If decryption fails (invalid token or wrong key)
    """
    try:
        fernet = _get_fernet()
        decrypted = fernet.decrypt(encrypted_jwt.encode())
        return decrypted.decode()
    except MissingEncryptionKeyError:
        raise
    except InvalidToken as e:
        raise JWTEncryptionError(
            "Failed to decrypt JWT: invalid token or wrong encryption key"
        ) from e
    except Exception as e:
        raise JWTEncryptionError(f"Failed to decrypt JWT: {e}") from e


def clear_fernet_cache() -> None:
    """
    Clear the cached Fernet instance.

    Useful for testing when switching encryption keys.
    """
    _get_fernet.cache_clear()
