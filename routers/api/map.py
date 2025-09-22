from datetime import datetime
from typing import List
from fastapi import APIRouter, Query

from routers.immich_models import MapMarkerResponseDto, MapReverseGeocodeResponseDto


router = APIRouter(
    prefix="/api/map",
    tags=["map"],
    responses={404: {"description": "Not found"}},
)


@router.get("/markers")
async def get_map_markers(
    isArchived: bool = Query(default=None),
    isFavorite: bool = Query(default=None),
    fileCreatedAfter: datetime = Query(default=None),
    fileCreatedBefore: datetime = Query(default=None),
    withPartners: bool = Query(default=None),
    withSharedAlbums: bool = Query(default=None),
) -> List[MapMarkerResponseDto]:
    """
    Return a list of map markers.
    Gumnut currently does not support mapping, so this is a stub implementation that returns an empty list.
    """

    return []


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
