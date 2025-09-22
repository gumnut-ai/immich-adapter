"""Utility for creating Gumnut client instances."""

from gumnut import Gumnut
from fastapi import HTTPException

from config.settings import get_settings


def get_gumnut_client() -> Gumnut:
    """
    Create and return a configured Gumnut client instance.

    Returns:
        Gumnut: Configured Gumnut client instance

    Raises:
        HTTPException: If GUMNUT_API_KEY is not configured
    """
    settings = get_settings()
    api_key = settings.gumnut_api_key

    if not api_key:
        raise HTTPException(status_code=500, detail="GUMNUT_API_KEY not configured")

    return Gumnut(api_key=api_key, base_url=settings.gumnut_api_base_url)
