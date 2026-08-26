from datetime import datetime
from typing import Annotated, List

from fastapi import APIRouter, Depends, Query
from gumnut import AsyncGumnut
from pydantic.json_schema import SkipJsonSchema

from routers.immich_models import MapMarkerResponseDto, MapReverseGeocodeResponseDto
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.map_markers import collect_geotagged_markers
from routers.utils.rating import rating_filter_value


router = APIRouter(
    prefix="/api/map",
    tags=["map"],
    responses={404: {"description": "Not found"}},
)


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
    """Return a bounded list of markers for the caller's GPS-tagged assets.

    `withPartners` / `withSharedAlbums` are accepted for client-compatibility
    (Immich clients send them) but have no Gumnut analog, so they're dropped
    at the adapter. `isFavorite=True` maps to the backend's exact rating-5
    filter. `isArchived=True` still short-circuits to `[]` because Gumnut has
    no archived state — returning unfiltered markers would be a wrong answer.

    The paging loop, marker/scan caps, and logging live in
    `collect_geotagged_markers` (shared with the album-scoped map endpoint).
    """
    _ = withPartners, withSharedAlbums  # accepted, dropped

    if isArchived is True:
        return []

    return await collect_geotagged_markers(
        client,
        rating=rating_filter_value(is_favorite=isFavorite),
        local_datetime_after=(
            fileCreatedAfter.isoformat() if fileCreatedAfter is not None else None
        ),
        local_datetime_before=(
            fileCreatedBefore.isoformat() if fileCreatedBefore is not None else None
        ),
    )


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
