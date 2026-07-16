import logging
import random
from typing import Annotated, Any, List
from fastapi import APIRouter, Depends, Query
from uuid import UUID
from datetime import datetime
from gumnut import AsyncGumnut
from gumnut.types.asset_response import AssetResponse

from routers.api.constants import GUMNUT_API_MAX_PAGE_SIZE
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.current_user import get_current_user
from routers.utils.gumnut_id_conversion import (
    uuid_to_gumnut_album_id,
    uuid_to_gumnut_person_id,
)
from routers.utils.person_conversion import convert_gumnut_person_to_immich
from routers.immich_models import (
    PersonResponseDto,
    SearchAlbumResponseDto,
    SearchExploreItem,
    SearchExploreResponseDto,
    AssetResponseDto,
    AssetTypeEnum,
    AssetVisibility,
    SearchResponseDto,
    SearchStatisticsResponseDto,
    SearchAssetResponseDto,
    MetadataSearchDto,
    SearchSuggestionType,
    SmartSearchDto,
    RandomSearchDto,
    PlacesResponseDto,
    StatisticsSearchDto,
    UserResponseDto,
)
from routers.api.timeline import fetch_asset_counts, month_query_bounds
from routers.utils.concurrency import gather_with_concurrency
from routers.utils.asset_conversion import (
    ASSET_INCLUDE,
    ASSET_INCLUDE_METADATA_ONLY,
    convert_gumnut_asset_to_immich,
    mime_type_to_asset_type,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/search",
    tags=["search"],
    responses={404: {"description": "Not found"}},
)

# Field limits mirror the Immich server defaults (maxFields=12,
# minAssetsPerField=5); the derivation is described in get_explore_data.
EXPLORE_SCAN_LIMIT = 1000
EXPLORE_MAX_CITIES = 12
EXPLORE_MIN_ASSETS_PER_CITY = 5
EXPLORE_MAX_RECENT_ASSETS = 12

# The Immich server samples 250 assets when the request doesn't specify a size.
RANDOM_DEFAULT_SIZE = 250


@router.get("/explore")
async def get_explore_data(
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> List[SearchExploreResponseDto]:
    """
    Return curated explore categories: one representative image per city
    ("exifInfo.city", the group the Immich web and mobile explore pages
    render as Places) and a recent-images group ("createdAt"). The recents
    group approximates the Immich server's most-recently-*uploaded* group
    using capture-time order, the only ordering the Gumnut list API offers.

    Cities are derived from the `EXPLORE_SCAN_LIMIT` most recent live assets;
    only cities with at least `EXPLORE_MIN_ASSETS_PER_CITY` images in that
    window are included, capped at `EXPLORE_MAX_CITIES`.
    """
    scanned: list[AssetResponse] = []
    async for asset in client.assets.list(
        state="live",
        limit=GUMNUT_API_MAX_PAGE_SIZE,
        include=ASSET_INCLUDE_METADATA_ONLY,
    ):
        scanned.append(asset)
        if len(scanned) >= EXPLORE_SCAN_LIMIT:
            break

    # Newest-first scan order, so the first asset seen for a city is its
    # most recent image and becomes the representative.
    city_representative: dict[str, str] = {}
    city_counts: dict[str, int] = {}
    recent_ids: list[str] = []
    for asset in scanned:
        if mime_type_to_asset_type(asset.mime_type) != AssetTypeEnum.IMAGE:
            continue
        if len(recent_ids) < EXPLORE_MAX_RECENT_ASSETS:
            recent_ids.append(asset.id)
        city = asset.metadata.city if asset.metadata else None
        if city:
            city_str = str(city)
            city_counts[city_str] = city_counts.get(city_str, 0) + 1
            city_representative.setdefault(city_str, asset.id)

    cities = [
        city
        for city in city_representative
        if city_counts[city] >= EXPLORE_MIN_ASSETS_PER_CITY
    ][:EXPLORE_MAX_CITIES]

    # Re-fetch the representatives with the full include set (the scan is
    # metadata-only) in one batched call.
    wanted_ids = list(
        dict.fromkeys([city_representative[city] for city in cities] + recent_ids)
    )
    assets_by_id: dict[str, AssetResponse] = {}
    if wanted_ids:
        async for asset in client.assets.list(ids=wanted_ids, include=ASSET_INCLUDE):
            assets_by_id[asset.id] = asset

    converted = {
        asset_id: convert_gumnut_asset_to_immich(asset, current_user)
        for asset_id, asset in assets_by_id.items()
    }

    # Assets may disappear between the scan and the batched re-fetch; skip
    # any representative that no longer resolves.
    city_items = [
        SearchExploreItem(value=city, data=converted[city_representative[city]])
        for city in cities
        if city_representative[city] in converted
    ]
    recent_items = [
        SearchExploreItem(
            value=assets_by_id[asset_id].created_at.isoformat(),
            data=converted[asset_id],
        )
        for asset_id in recent_ids
        if asset_id in converted
    ]

    return [
        SearchExploreResponseDto(fieldName="exifInfo.city", items=city_items),
        SearchExploreResponseDto(fieldName="createdAt", items=recent_items),
    ]


@router.post("/large-assets")
async def search_large_assets(
    albumIds: list[UUID] = Query(default=None),
    city: str = Query(default=None, nullable=True),
    country: str = Query(default=None, nullable=True),
    createdAfter: datetime = Query(default=None),
    createdBefore: datetime = Query(default=None),
    deviceId: str = Query(default=None),
    isEncoded: bool = Query(default=None),
    isFavorite: bool = Query(default=None),
    isMotion: bool = Query(default=None),
    isNotInAlbum: bool = Query(default=None),
    isOffline: bool = Query(default=None),
    lensModel: str = Query(default=None, nullable=True),
    libraryId: UUID = Query(default=None, nullable=True),
    make: str = Query(default=None),
    minFileSize: int = Query(default=None, ge=0),
    model: str = Query(default=None, nullable=True),
    personIds: list[UUID] = Query(default=None),
    rating: int = Query(default=None, ge=-1, le=5, type="number"),
    size: int = Query(default=None, ge=1, le=1000, type="number"),
    state: str = Query(default=None, nullable=True),
    tagIds: list[UUID] = Query(default=None, nullable=True),
    takenAfter: datetime = Query(default=None),
    takenBefore: datetime = Query(default=None),
    trashedAfter: datetime = Query(default=None),
    trashedBefore: datetime = Query(default=None),
    type: AssetTypeEnum = Query(default=None),
    updatedAfter: datetime = Query(default=None),
    updatedBefore: datetime = Query(default=None),
    visibility: AssetVisibility = Query(default=None),
    withDeleted: bool = Query(default=None),
    withExif: bool = Query(default=None),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[AssetResponseDto]:
    """
    Search for large assets based on minimum file size.
    This is a stub implementation as Gumnut does not currently track file size.
    Returns an empty list.
    """

    return []


@router.get("/person")
async def search_person(
    name: str,
    withHidden: Annotated[bool, Query()] = False,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[PersonResponseDto]:
    """Search for people by name.

    ``withHidden`` matches Immich's ``!withHidden`` rule: only an explicit true
    includes hidden people, so omitting the param excludes them.
    """
    people = [p async for p in client.people.list(name=name)]
    if not withHidden:
        people = [p for p in people if not p.is_hidden]
    return [convert_gumnut_person_to_immich(p) for p in people]


@router.get("/places")
async def search_places(
    name: str = Query(),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[PlacesResponseDto]:
    """
    Search for places by name.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.get("/suggestions")
async def get_search_suggestions(
    type: SearchSuggestionType,
    country: str = Query(default=None),
    includeNull: bool = Query(default=None),
    make: str = Query(default=None),
    model: str = Query(default=None),
    state: str = Query(default=None),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[str]:
    """
    Get search suggestions.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.post("/statistics")
async def search_asset_statistics(
    request: StatisticsSearchDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> SearchStatisticsResponseDto:
    """Get asset count statistics."""
    buckets = await fetch_asset_counts(client)
    total = sum(bucket.count for bucket in buckets)
    return SearchStatisticsResponseDto(total=total)


@router.post("/metadata")
async def search_assets(
    request: MetadataSearchDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> SearchResponseDto:
    """Search for assets by metadata filters."""
    person_ids = None
    if request.personIds:
        person_ids = [uuid_to_gumnut_person_id(pid) for pid in request.personIds]

    search_kwargs: dict[str, Any] = {
        "query": request.description,
        # Web sends these in the keepLocalTime wire format, so its offset is
        # fictitious (see code practices, "Immich web 'today' wire format");
        # mobile converts to real UTC and sends a genuine instant. Forwarded
        # as-is, so the two clients' date filters differ by the user's UTC
        # offset — reconciling them changes user-visible search results, so it
        # needs a decision about which form the Gumnut API's capture-time
        # comparison expects rather than a fix here.
        "captured_after": request.takenAfter,
        "captured_before": request.takenBefore,
        "person_ids": person_ids,
        "include": ASSET_INCLUDE,
    }
    if request.size is not None:
        # Clamp at the Gumnut API per-page ceiling. The Immich client default
        # is 1000; without this, the Gumnut API 422s.
        search_kwargs["limit"] = min(int(request.size), GUMNUT_API_MAX_PAGE_SIZE)
    if request.page is not None:
        search_kwargs["page"] = int(request.page)

    gumnut_results = await client.search.search(**search_kwargs)

    immich_assets = []
    if gumnut_results and gumnut_results.data:
        for item in gumnut_results.data:
            immich_assets.append(
                convert_gumnut_asset_to_immich(item.asset, current_user)
            )

    return SearchResponseDto(
        albums=SearchAlbumResponseDto(count=0, facets=[], items=[], total=0),
        assets=SearchAssetResponseDto(
            count=len(immich_assets),
            facets=[],
            items=immich_assets,
            nextPage=None,
            total=len(immich_assets),
        ),
    )


@router.post("/smart")
async def search_smart(
    request: SmartSearchDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> SearchResponseDto:
    """Smart search for assets."""
    search_kwargs: dict[str, Any] = {"query": request.query, "include": ASSET_INCLUDE}
    if request.size is not None:
        # Clamp at the Gumnut API per-page ceiling. The Immich client default
        # is 1000; without this, the Gumnut API 422s.
        search_kwargs["limit"] = min(int(request.size), GUMNUT_API_MAX_PAGE_SIZE)
    if request.page is not None:
        search_kwargs["page"] = int(request.page)

    gumnut_assets = await client.search.search(**search_kwargs)

    immich_assets = []
    if gumnut_assets:
        for item in gumnut_assets.data:
            immich_assets.append(
                convert_gumnut_asset_to_immich(item.asset, current_user)
            )

    return SearchResponseDto(
        albums=SearchAlbumResponseDto(count=0, facets=[], items=[], total=0),
        assets=SearchAssetResponseDto(
            count=len(immich_assets),
            facets=[],
            items=immich_assets,
            nextPage=None,
            total=len(immich_assets),
        ),
    )


@router.get("/cities")
async def get_assets_by_city(
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[AssetResponseDto]:
    """
    Get cities for search.
    This is a stub implementation that returns an empty list.
    """
    return []


async def _fetch_month_assets_at_offsets(
    client: AsyncGumnut,
    time_bucket: datetime,
    offsets: list[int],
    *,
    album_id: str | None,
    person_id: str | None,
) -> list[AssetResponse]:
    """Fetch the assets at the given newest-first offsets within a month bucket.

    Pages through the month only as far as the largest requested offset. If
    the library changed between the counts call and this fetch, offsets past
    the end of the month are silently skipped, so the sample may come up
    short rather than erroring.
    """
    after_bound, before_bound = month_query_bounds(time_bucket)
    wanted = set(offsets)
    max_offset = max(offsets)

    list_kwargs: dict[str, Any] = {
        "local_datetime_after": after_bound,
        "local_datetime_before": before_bound,
        "state": "live",
        "limit": GUMNUT_API_MAX_PAGE_SIZE,
        "include": ASSET_INCLUDE,
    }
    if album_id is not None:
        list_kwargs["album_id"] = album_id
    if person_id is not None:
        list_kwargs["person_id"] = person_id

    picked: list[AssetResponse] = []
    index = 0
    async for asset in client.assets.list(**list_kwargs):
        if index in wanted:
            picked.append(asset)
        index += 1
        if index > max_offset:
            break
    if len(picked) < len(wanted):
        logger.debug(
            "random sample month %s yielded %d of %d requested offsets",
            time_bucket.isoformat(),
            len(picked),
            len(wanted),
        )
    return picked


@router.post("/random")
async def search_random(
    request: RandomSearchDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user: UserResponseDto = Depends(get_current_user),
) -> List[AssetResponseDto]:
    """
    Return a uniform random sample of live assets.

    Samples without a full-library scan: fetches the per-month asset counts,
    draws distinct global indices across the newest-first ordering, and pages
    only the months containing sampled indices.

    Supported filters: `size` (defaults to `RANDOM_DEFAULT_SIZE`, matching the
    Immich server), single-element `albumIds` / `personIds`, and `type`.
    `type` is applied to the drawn sample (the Gumnut API cannot filter by
    asset type server-side), so each matching asset is equally likely but the
    response may hold fewer than `size` items when the library is sparse in
    that type. Any other restricting filter (date bounds, location/camera
    metadata, tags, rating, etc.) has no Gumnut API translation and returns
    an empty list rather than silently sampling assets the caller filtered
    out — the same posture the timeline endpoint takes for favorites and
    non-timeline visibility. Response-shape hints (`withExif`, `withPeople`,
    `withStacked`) are always satisfied (the sample converts with the full
    include set), and `withDeleted` is ignored: it *widens* the requested set
    to include trashed assets, so a live-only sample still matches the filter.
    """
    if request.isFavorite or (
        request.visibility is not None
        and request.visibility != AssetVisibility.timeline
    ):
        return []
    if request.albumIds and len(request.albumIds) > 1:
        return []
    if request.personIds and len(request.personIds) > 1:
        return []
    # Restricting filters with no Gumnut API translation. Sampling without
    # applying them would return assets the caller explicitly filtered out,
    # so return empty instead. Booleans count only when truthy: `False` on
    # flags like isMotion/isEncoded matches effectively every Gumnut asset
    # (the backing features don't exist), so it doesn't restrict the sample.
    unsupported_value_filters = (
        request.city,
        request.country,
        request.state,
        request.createdAfter,
        request.createdBefore,
        request.takenAfter,
        request.takenBefore,
        request.trashedAfter,
        request.trashedBefore,
        request.updatedAfter,
        request.updatedBefore,
        request.lensModel,
        request.libraryId,
        request.make,
        request.model,
        request.ocr,
        request.rating,
    )
    unsupported_flag_filters = (
        request.isEncoded,
        request.isMotion,
        request.isNotInAlbum,
        request.isOffline,
    )
    if (
        any(value is not None for value in unsupported_value_filters)
        or any(unsupported_flag_filters)
        or request.tagIds
    ):
        return []

    album_id = (
        uuid_to_gumnut_album_id(request.albumIds[0]) if request.albumIds else None
    )
    person_id = (
        uuid_to_gumnut_person_id(request.personIds[0]) if request.personIds else None
    )

    buckets = await fetch_asset_counts(client, album_id=album_id, person_id=person_id)
    total = sum(bucket.count for bucket in buckets)
    if total == 0:
        return []

    sample_size = min(
        int(request.size) if request.size is not None else RANDOM_DEFAULT_SIZE, total
    )
    picks = sorted(random.sample(range(total), sample_size))

    # Map sampled global indices (over the newest-first ordering the counts
    # buckets and asset listings share) to per-month offsets.
    months_with_offsets: list[tuple[datetime, list[int]]] = []
    cursor = 0
    pick_iter = iter(picks)
    current_pick = next(pick_iter, None)
    for bucket in buckets:
        bucket_end = cursor + bucket.count
        offsets: list[int] = []
        while current_pick is not None and current_pick < bucket_end:
            offsets.append(current_pick - cursor)
            current_pick = next(pick_iter, None)
        if offsets:
            months_with_offsets.append((bucket.time_bucket, offsets))
        cursor = bucket_end

    # A 250-asset sample over a long-lived library can touch hundreds of
    # months; bound the fan-out so one request can't swamp the backend.
    month_results = await gather_with_concurrency(
        [
            _fetch_month_assets_at_offsets(
                client, time_bucket, offsets, album_id=album_id, person_id=person_id
            )
            for time_bucket, offsets in months_with_offsets
        ]
    )

    sampled = [asset for month_assets in month_results for asset in month_assets]
    if request.type is not None:
        # Post-sample filter — rationale in the docstring's `type` note.
        sampled = [
            asset
            for asset in sampled
            if mime_type_to_asset_type(asset.mime_type) == request.type
        ]
    random.shuffle(sampled)
    return [convert_gumnut_asset_to_immich(asset, current_user) for asset in sampled]
