import logging
from datetime import datetime
from typing import Annotated, Any, List

from fastapi import APIRouter, Depends, Query
from gumnut import AsyncGumnut
from pydantic.json_schema import SkipJsonSchema

from routers.api.constants import PHOTOS_API_MAX_PAGE_SIZE
from routers.immich_models import MapMarkerResponseDto, MapReverseGeocodeResponseDto
from routers.utils.asset_conversion import ASSET_INCLUDE_METADATA_ONLY
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.gumnut_id_conversion import safe_uuid_from_asset_id

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/map",
    tags=["map"],
    responses={404: {"description": "Not found"}},
)


# Hard cap on returned markers. Each list page requests only `metadata` via
# `include` — not faces/people/file_data/asset_urls — since the marker build
# reads just three GPS fields off `metadata`. `metadata` is still the large
# EXIF block, so paging stays the cost driver. The ~280 ms/page figure below
# predates that trim (it was measured against the full asset payload) against a
# real library at ~70% GPS-tagged density, so it's now an upper bound: 2000
# markers ≈ 15 pages ≈ 4 s; 5000 ≈ 31 pages ≈ 11 s — too slow for a map-view
# load. Revisit (a dedicated backend `/map/markers` endpoint) if real usage
# shows 2000 is insufficient. The SDK orders by capture time descending, so
# when the cap fires the oldest GPS-tagged assets are dropped.
MAP_MARKERS_CAP = 2000

# Ceiling on assets scanned, independent of how many fill the marker cap.
# Bounds the worst case where a low-GPS-density library would otherwise walk
# tens of pages chasing a cap it'll never fill (e.g., 5% density × 50K assets
# = 200 pages ≈ ~56 s without this bound). 30 pages ≈ ~8 s.
MAX_ASSETS_SCANNED = 30 * PHOTOS_API_MAX_PAGE_SIZE


@router.get("/markers")
async def get_map_markers(
    isArchived: Annotated[bool | SkipJsonSchema[None], Query()] = None,
    isFavorite: Annotated[bool | SkipJsonSchema[None], Query()] = None,
    fileCreatedAfter: Annotated[datetime | SkipJsonSchema[None], Query()] = None,
    fileCreatedBefore: Annotated[datetime | SkipJsonSchema[None], Query()] = None,
    withPartners: Annotated[bool | SkipJsonSchema[None], Query()] = None,
    withSharedAlbums: Annotated[bool | SkipJsonSchema[None], Query()] = None,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[MapMarkerResponseDto]:
    """Return up to `MAP_MARKERS_CAP` markers for the caller's GPS-tagged assets.

    `withPartners` / `withSharedAlbums` are accepted for client-compatibility
    (Immich clients send them) but have no Gumnut analog, so they're dropped
    at the adapter. `isFavorite=True` / `isArchived=True` short-circuit to
    `[]` because Gumnut doesn't track favorites or archived state — returning
    unfiltered markers would be a wrong answer to a restrictive filter. The
    timeline endpoints handle `isFavorite` the same way; they don't accept
    an `isArchived` filter at all (archived state is folded into `visibility`
    there), so `isArchived` short-circuiting here is map-specific.
    """
    _ = withPartners, withSharedAlbums  # accepted, dropped

    # Gumnut doesn't track favorites or archived state, so a filter on
    # either can never match. Short-circuit instead of silently ignoring
    # the filter and returning unfiltered markers.
    if isFavorite is True or isArchived is True:
        return []

    list_kwargs: dict[str, Any] = {
        "limit": PHOTOS_API_MAX_PAGE_SIZE,
        "include": ASSET_INCLUDE_METADATA_ONLY,
    }
    if fileCreatedAfter is not None:
        list_kwargs["local_datetime_after"] = fileCreatedAfter.isoformat()
    if fileCreatedBefore is not None:
        list_kwargs["local_datetime_before"] = fileCreatedBefore.isoformat()

    markers: list[MapMarkerResponseDto] = []
    assets_scanned = 0
    marker_cap_hit = False
    async for asset in client.assets.list(**list_kwargs):
        assets_scanned += 1
        metadata = asset.metadata
        if (
            metadata is not None
            and metadata.latitude is not None
            and metadata.longitude is not None
        ):
            markers.append(
                MapMarkerResponseDto(
                    id=str(safe_uuid_from_asset_id(asset.id)),
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
        if assets_scanned >= MAX_ASSETS_SCANNED:
            break

    scan_cap_hit = assets_scanned >= MAX_ASSETS_SCANNED and not marker_cap_hit
    logger.info(
        "map markers: scanned %d assets, returned %d markers (marker_cap_hit=%s, scan_cap_hit=%s)",
        assets_scanned,
        len(markers),
        marker_cap_hit,
        scan_cap_hit,
        extra={
            "assets_scanned": assets_scanned,
            "markers_returned": len(markers),
            "marker_cap_hit": marker_cap_hit,
            "scan_cap_hit": scan_cap_hit,
        },
    )
    return markers


@router.get("/reverse-geocode")
async def reverse_geocode(
    lat: float = Query(format="double"),
    lon: float = Query(format="double"),
) -> List[MapReverseGeocodeResponseDto]:
    """
    Reverse geocode a latitude and longitude to a human-readable address.
    Gumnut currently does not support reverse geocoding, so this is a stub implementation that returns an array.
    """

    return []
