"""Shared marker retrieval for the map endpoints.

Both the global map endpoint (`GET /api/map/markers`) and the album-scoped
endpoint (`GET /api/albums/{id}/map-markers`) return the same
`MapMarkerResponseDto` shape over the caller's GPS-tagged assets. The paging
loop, marker/scan caps, and structured logging live here so the two routes
share one implementation and can't drift.
"""

import logging
from typing import Any

from gumnut import AsyncGumnut

from routers.api.constants import GUMNUT_API_MAX_PAGE_SIZE
from routers.immich_models import MapMarkerResponseDto
from routers.utils.asset_conversion import ASSET_INCLUDE_METADATA_ONLY
from routers.utils.gumnut_id_conversion import safe_uuid_from_asset_id

logger = logging.getLogger(__name__)


# A bounding box covering the whole globe. Passing it to the Gumnut API's
# coordinate filter returns *only* geotagged assets — not because it narrows the
# area (it spans the planet) but because the filter is a coordinate *range*
# check: an asset with no coordinate has a NULL latitude/longitude, and
# `NULL BETWEEN min AND max` is never true, so every non-geotagged asset is
# excluded even by a world-wide box. That lets the adapter page through markers
# instead of scanning the whole library and discarding coordinate-less assets
# client-side (verified: on a mixed library the newest page drops from 7
# coordinate-less assets to 0 with this box applied). The backend serves it
# index-only from its geo covering index. Order is
# `min_longitude,min_latitude,max_longitude,max_latitude`.
GEOTAGGED_WORLD_BBOX = "-180,-90,180,90"

# Hard cap on returned markers. Each list page requests only `metadata` via
# `include` — not faces/people/file_data/asset_urls — since the marker build
# reads just three GPS fields off `metadata`. Because the coordinate filter
# makes every returned asset a marker, paging cost now scales with the marker
# count, not the library's GPS density: 2000 markers ≈ 2000 / page_size pages
# regardless of how sparsely the library is geotagged. The SDK orders by
# capture time descending, so when the cap fires the oldest GPS-tagged assets
# are dropped.
MAP_MARKERS_CAP = 2000

# Safety net bounding total assets walked. In the normal path the coordinate
# filter returns only geotagged assets, so the marker cap fires first and this
# never triggers. It guards the degraded case where the `bbox` filter is *not*
# applied — an older Gumnut API that ignores the unknown param (e.g. the adapter
# deploying ahead of the API), or a filter regression — which would otherwise
# turn a low-GPS-density library's marker request into a full-library scan.
MAX_ASSETS_SCANNED = 30 * GUMNUT_API_MAX_PAGE_SIZE


async def collect_geotagged_markers(
    client: AsyncGumnut,
    *,
    album_id: str | None = None,
    local_datetime_after: str | None = None,
    local_datetime_before: str | None = None,
) -> list[MapMarkerResponseDto]:
    """Page the caller's GPS-tagged assets into up to `MAP_MARKERS_CAP` markers.

    The world `bbox` filters to geotagged assets server-side (see
    `GEOTAGGED_WORLD_BBOX`) so we page through markers rather than the whole
    library. When `album_id` is given it further restricts to one album's assets
    (AND-combined with the bbox — the two are independent filters). Optional
    `local_datetime_after` / `local_datetime_before` are ISO-8601 strings that
    narrow by capture time.
    """
    list_kwargs: dict[str, Any] = {
        "limit": GUMNUT_API_MAX_PAGE_SIZE,
        "include": ASSET_INCLUDE_METADATA_ONLY,
        # World bbox → geotagged assets only; see GEOTAGGED_WORLD_BBOX.
        "bbox": GEOTAGGED_WORLD_BBOX,
    }
    if album_id is not None:
        list_kwargs["album_id"] = album_id
    if local_datetime_after is not None:
        list_kwargs["local_datetime_after"] = local_datetime_after
    if local_datetime_before is not None:
        list_kwargs["local_datetime_before"] = local_datetime_before

    markers: list[MapMarkerResponseDto] = []
    assets_scanned = 0
    marker_cap_hit = False
    async for asset in client.assets.list(**list_kwargs):
        assets_scanned += 1
        metadata = asset.metadata
        # The bbox filter guarantees a coordinate, but guard defensively so an
        # unexpected null can't crash marker construction (lat/lon are required).
        if (
            metadata is not None
            and metadata.latitude is not None
            and metadata.longitude is not None
        ):
            markers.append(
                MapMarkerResponseDto(
                    id=safe_uuid_from_asset_id(asset.id),
                    lat=metadata.latitude,
                    lon=metadata.longitude,
                    city=metadata.city,
                    state=metadata.state,
                    country=metadata.country,
                )
            )
            if len(markers) >= MAP_MARKERS_CAP:
                marker_cap_hit = True
                break
        # Safety net if the coordinate filter wasn't applied (see
        # MAX_ASSETS_SCANNED) — bounds work so a low-GPS library can't degrade
        # into a full-library scan.
        if assets_scanned >= MAX_ASSETS_SCANNED:
            break

    scan_cap_hit = assets_scanned >= MAX_ASSETS_SCANNED and not marker_cap_hit
    logger.info(
        "map markers: scanned %d assets, returned %d markers "
        "(album_id=%s, marker_cap_hit=%s, scan_cap_hit=%s)",
        assets_scanned,
        len(markers),
        album_id,
        marker_cap_hit,
        scan_cap_hit,
        extra={
            "assets_scanned": assets_scanned,
            "markers_returned": len(markers),
            "album_id": album_id,
            "marker_cap_hit": marker_cap_hit,
            "scan_cap_hit": scan_cap_hit,
        },
    )
    return markers
