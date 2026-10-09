"""Request isolation and expiry gates for optional thumbnail metadata reuse."""

import base64
import json
import math
import time
from dataclasses import dataclass

from fastapi import Depends, Request
from gumnut import AsyncGumnut

from config.settings import get_settings
from routers.utils.gumnut_client import (
    get_authenticated_gumnut_client,
    get_bound_library_id,
)
from services.thumbnail_cache import opaque_key


@dataclass(frozen=True)
class ThumbnailCacheContext:
    scope: str
    credential_expires_at: float | None

    def remaining_seconds(self) -> float | None:
        if self.credential_expires_at is None:
            return None
        return self.credential_expires_at - time.time()


def _jwt_expiry(credential: str) -> float | None:
    """Read an expiry only to deny cache reuse; upstream verifies signatures.

    An entry can only originate from a successful call with exactly this
    credential. Missing/malformed expiry fails closed to the live API path.
    """
    try:
        payload = credential.split(".")
        if len(payload) != 3:
            return None
        claims = json.loads(base64.urlsafe_b64decode(payload[1] + "=="))
        expiry = claims.get("exp")
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
            return None
        expiry = float(expiry)
        return expiry if math.isfinite(expiry) and expiry > time.time() else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def get_thumbnail_cache_context(
    request: Request,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> ThumbnailCacheContext | None:
    settings = get_settings()
    return _metadata_cache_context(
        request, settings.thumbnail_metadata_cache_ttl_seconds
    )


def get_video_cache_context(
    request: Request,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> ThumbnailCacheContext | None:
    return _metadata_cache_context(
        request, get_settings().video_metadata_cache_ttl_seconds
    )


def _metadata_cache_context(
    request: Request, ttl_seconds: float
) -> ThumbnailCacheContext | None:
    settings = get_settings()
    if ttl_seconds <= 0:
        return None
    credential = getattr(request.state, "jwt_token", None)
    session = getattr(request.state, "session_token", None)
    library = get_bound_library_id()
    # Restricted/unresolved keys stay live: the backend may choose their scope.
    if not isinstance(credential, str) or not library:
        return None
    expires_at = None
    if session:
        expires_at = _jwt_expiry(credential)
        if expires_at is None:
            return None
    elif not credential.startswith("apikey_"):
        return None
    return ThumbnailCacheContext(
        opaque_key(settings.gumnut_api_base_url, credential, session or "", library),
        expires_at,
    )
