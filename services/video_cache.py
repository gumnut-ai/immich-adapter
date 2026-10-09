"""Optional video selection reuse; the media-variant reference owns its contract."""

import re
from urllib.parse import parse_qs, urlsplit

from config.settings import get_settings
from services.thumbnail_cache import ThumbnailCache, ThumbnailVariant

_cache: ThumbnailCache | None = None


def is_cacheable_video_selection(variant: ThumbnailVariant) -> bool:
    """Admit only the known non-expiring Gumnut CDN capability format.

    The live API authorizes and supplies the URL. This checks its format, not
    its signature. Unknown hosts or signing parameters may encode an expiry
    we cannot bound, so their selections are never retained.
    """
    try:
        url = urlsplit(variant.url)
        query = parse_qs(url.query, keep_blank_values=True)
        signature = query.get("verify", [])
        return (
            variant.mimetype.startswith("video/")
            and url.scheme == "https"
            and url.hostname == "assets.gumnut.ai"
            and url.port in {None, 443}
            and url.username is None
            and url.password is None
            and not url.fragment
            and bool(url.path.strip("/"))
            and set(query) <= {"verify", "dl"}
            and len(signature) == 1
            and re.fullmatch(r"[A-Za-z0-9_-]{43}", signature[0]) is not None
            and len(query.get("dl", [])) <= 1
        )
    except ValueError:
        return False


def get_video_cache() -> ThumbnailCache:
    global _cache
    if _cache is None:
        settings = get_settings()
        _cache = ThumbnailCache(
            settings.video_metadata_cache_ttl_seconds,
            settings.video_metadata_cache_max_entries,
            namespace="video",
            cacheable=is_cacheable_video_selection,
        )
    return _cache


async def close_video_cache() -> None:
    global _cache
    if _cache is not None:
        await _cache.close()
        _cache = None
