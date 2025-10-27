import logging
import os
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from routers.utils.oauth_utils import normalize_redirect_uri

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    environment: str | None = None

    # External URL for API responses (set to https://api.gumnut.ai in production)
    gumnut_api_base_url: str = "http://localhost:8000"
    sentry_dsn: str | None = None

    # OAuth allowed redirect URIs (comma-separated in .env)
    oauth_allowed_redirect_uris: str = "http://localhost:3000/auth/callback"

    @field_validator("environment")
    def validate_environment(cls, v: str | None) -> str:
        if v is None or v not in ["development", "test", "production"]:
            raise ValueError(
                "ENVIRONMENT must be 'development', 'test', or 'production'"
            )
        return v

    @property
    def oauth_allowed_redirect_uris_list(self) -> set[str]:
        """
        Parse comma-separated redirect URIs into a set for validation.

        Returns a set of allowed redirect URIs. Returns set with default
        localhost URI if not configured.
        """
        raw = self.oauth_allowed_redirect_uris or "http://localhost:3000/auth/callback"
        return {normalize_redirect_uri(u.strip()) for u in raw.split(",") if u.strip()}


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
