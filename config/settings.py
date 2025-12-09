import logging
import os
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from config.immich_version import ImmichVersion, load_immich_version

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_immich_version() -> ImmichVersion:
    try:
        return load_immich_version()
    except (FileNotFoundError, ValueError) as exc:
        logger.error("Failed to load Immich version: %s", exc)
        return ImmichVersion(major=0, minor=0, patch=0)


class Settings(BaseSettings):
    environment: str | None = None

    # External URL for API responses (set to https://api.gumnut.ai in production)
    gumnut_api_base_url: str = "http://localhost:8000"
    sentry_dsn: str | None = None

    # Redis settings (uses db 1 for isolation from photos-api which uses db 0)
    redis_url: str = "redis://localhost:6379/1"

    # Mobile app OAuth redirect URL (custom URL scheme for mobile deep linking)
    oauth_mobile_redirect_uri: str = "app.immich:///oauth-callback"

    # Session encryption key for Fernet (base64-encoded 32-byte key)
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    session_encryption_key: str | None = None

    # Private field to cache the loaded Immich version
    _immich_version: ImmichVersion | None = None

    @property
    def immich_version(self) -> ImmichVersion:
        return get_immich_version()

    @field_validator("environment")
    def validate_environment(cls, v: str | None) -> str:
        if v is None or v not in ["development", "test", "production"]:
            raise ValueError(
                "ENVIRONMENT must be 'development', 'test', or 'production'"
            )
        return v

    @field_validator("session_encryption_key")
    def validate_session_encryption_key(cls, v: str | None) -> str:
        if v is None or v.strip() == "":
            raise ValueError(
                "SESSION_ENCRYPTION_KEY is required. "
                'Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )
        return v

    @field_validator("gumnut_api_base_url")
    def strip_trailing_slash(cls, v: str) -> str:
        """Strip trailing slashes from URLs to prevent double-slash bugs when building URLs."""
        return v.rstrip("/")


class TestSettings(Settings):
    __test__ = False  # Tell pytest this isn't a test class even though its name starts with "Test"

    # Override with test-specific config
    # Load both files, with .env.test taking precedence
    model_config = SettingsConfigDict(
        env_file=[
            ".env",
            ".env.test",
        ],
        env_file_encoding="utf-8",
        extra="ignore",
    )


class DefaultSettings(Settings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


def _log_settings_overrides() -> dict:
    """
    Get environment variables that override values from the config files
    and log them. Sensitive values like passwords, secrets, and keys are masked.

    Returns:
        Dictionary of field names and their overridden values
    """
    # Get all field names from the Settings class
    field_names = list(Settings.__annotations__.keys())

    overrides = {}
    for field in field_names:
        # Convert field name to expected environment variable format (uppercase with underscores)
        env_var_name = field.upper()
        if env_var_name in os.environ:
            overrides[field] = os.environ[env_var_name]

    # Log the overrides if any exist
    if overrides:
        logger.info("Environment variables overriding config files:")
        for field, value in overrides.items():
            # Mask sensitive values
            if any(sensitive in field for sensitive in ["password", "secret", "key"]):
                masked_value = "********"
                logger.info(f"  {field}: {masked_value}")
            else:
                logger.info(f"  {field}: {value}")

    return overrides


@lru_cache
def get_settings():
    """
    Returns the appropriate settings based on the environment.
    When TESTING is set, it returns TestSettings which loads both .env and .env.test,
    with .env.test values taking precedence.

    Also prints any environment variables that override values from the config files.
    """
    settings_class = TestSettings if os.getenv("TESTING") else DefaultSettings
    _log_settings_overrides()

    return settings_class()
