import logging
from datetime import datetime, timedelta
from typing import Any, List, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from gumnut import AsyncGumnut, GumnutError
from gumnut.types.asset_count_response import AssetCountResponse, Data

from routers.api.constants import GUMNUT_API_MAX_PAGE_SIZE
from routers.immich_models import (
    AssetOrder,
    AssetTypeEnum,
    AssetVisibility,
    TimeBucketsResponseDto,
)
from routers.utils.asset_conversion import (
    ASSET_INCLUDE_METADATA_ONLY,
    duration_ms,
    mime_type_to_asset_type,
    resolve_asset_location,
    resolve_capture_datetime,
    resolve_created_at,
)
from routers.utils.current_user import get_current_user_id
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    uuid_to_gumnut_album_id,
    uuid_to_gumnut_person_id,
)
from routers.utils.stack_conversion import TimelineStacks, resolve_timeline_stacks

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/timeline",
    tags=["timeline"],
    responses={404: {"description": "Not found"}},
)


async def fetch_asset_counts(
    client: AsyncGumnut,
    *,
    album_id: str | None = None,
    person_id: str | None = None,
    state: Literal["live", "trashed", "all"] | None = None,
) -> list[Data]:
    """Fetch all monthly asset counts from the Gumnut API, paginating if needed."""
    kwargs: dict[str, Any] = {"group_by": "month", "limit": GUMNUT_API_MAX_PAGE_SIZE}
    if album_id is not None:
        kwargs["album_id"] = album_id
    if person_id is not None:
        kwargs["person_id"] = person_id
    if state is not None:
        kwargs["state"] = state

    all_buckets: list[Data] = []
    while True:
        response: AssetCountResponse = await client.assets.counts(**kwargs)
        all_buckets.extend(response.data)

        if not response.has_more or not response.data:
            break

        # Cursor forward: results are ordered by time_bucket descending,
        # so use the last time_bucket as the upper bound for the next page.
        kwargs["local_datetime_before"] = response.data[-1].time_bucket

    return all_buckets


def month_window(moment: datetime) -> tuple[datetime, datetime]:
    """Return the naive (month_start, next_month_start) window containing `moment`.

    Boundaries are naive on purpose: the Gumnut API counts endpoint groups by
    date_trunc("month", local_datetime) on the naive column, so month windows
    used to fetch a bucket's assets must compare wall-clock local_datetime
    directly. When building `assets.list` date bounds from this window, use
    `month_query_bounds` — passing `month_start` directly as
    `local_datetime_after` silently excludes month-start-midnight assets.
    """
    month_start = moment.replace(
        tzinfo=None, day=1, hour=0, minute=0, second=0, microsecond=0
    )
    if month_start.month == 12:
        next_month_start = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month_start = month_start.replace(month=month_start.month + 1)
    return month_start, next_month_start


def month_query_bounds(moment: datetime) -> tuple[str, str]:
    """ISO-8601 `assets.list` date bounds covering `moment`'s month.

    The Gumnut API treats `local_datetime_after` as exclusive, so the lower
    bound backs off one microsecond from month start — otherwise an asset
    captured exactly at month-start midnight is counted in the month's bucket
    by the counts endpoint but never returned by a listing over the window.
    """
    month_start, next_month_start = month_window(moment)
    return (
        (month_start - timedelta(microseconds=1)).isoformat(),
        next_month_start.isoformat(),
    )


@router.get("/buckets")
async def get_time_buckets(
    albumId: UUID = Query(default=None),
    isFavorite: bool = Query(default=None),
    isTrashed: bool = Query(default=None),
    key: str = Query(default=None),
    order: AssetOrder = Query(default=None),
    personId: UUID = Query(default=None),
    slug: str = Query(default=None),
    tagId: UUID = Query(default=None),
    userId: UUID = Query(default=None),
    visibility: AssetVisibility = Query(default=None),
    withCoordinates: bool = Query(default=None),
    withPartners: bool = Query(default=None),
    withStacked: bool = Query(default=None),
    bbox: str = Query(default=None),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[TimeBucketsResponseDto]:
    if isFavorite or (
        visibility is not None and visibility != AssetVisibility.timeline
    ):
        return []  # Gumnut does not support favorites, hidden, archived or locked assets, so return empty list

    album_id = uuid_to_gumnut_album_id(albumId) if albumId else None
    person_id = uuid_to_gumnut_person_id(personId) if personId else None

    raw_buckets = await fetch_asset_counts(
        client,
        album_id=album_id,
        person_id=person_id,
        state="trashed" if isTrashed else None,
    )

    # Map to Immich format: normalize time_bucket to month start (YYYY-MM-01)
    buckets = [
        TimeBucketsResponseDto(
            timeBucket=bucket.time_bucket.strftime("%Y-%m-01"),
            count=bucket.count,
        )
        for bucket in raw_buckets
    ]

    # The counts endpoint returns results in descending order by default.
    # Reverse only if ascending order is requested.
    if order == AssetOrder.asc:
        buckets.reverse()

    return buckets


@router.get("/bucket")
async def get_time_bucket(
    timeBucket: str,
    albumId: UUID = Query(default=None),
    isFavorite: bool = Query(default=None),
    isTrashed: bool = Query(default=None),
    key: str = Query(default=None),
    order: AssetOrder = Query(default=None),
    personId: UUID = Query(default=None),
    slug: str = Query(default=None),
    tagId: UUID = Query(default=None),
    userId: UUID = Query(default=None),
    visibility: AssetVisibility = Query(default=None),
    withCoordinates: bool = Query(default=None),
    withPartners: bool = Query(default=None),
    withStacked: bool = Query(default=None),
    bbox: str = Query(default=None),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user_id: UUID = Depends(get_current_user_id),
) -> Any:  # Preserve raw dict passthrough so optional columns stay omitted.
    """Retrieve assets that match the specified time bucket."""

    # Compute month boundaries from timeBucket for server-side date filtering.
    # The Immich client may send naive ("2024-01-01T00:00:00") or UTC-aware
    # ("2024-01-01T00:00:00.000Z") timestamps; the month helpers strip
    # timezone info so boundaries are naive (see month_window's docstring).
    after_bound, before_bound = month_query_bounds(datetime.fromisoformat(timeBucket))
    date_range_query = {
        "local_datetime_after": after_bound,
        "local_datetime_before": before_bound,
    }

    list_kwargs: dict[str, Any] = {
        "extra_query": date_range_query,
        "include": ASSET_INCLUDE_METADATA_ONLY,
    }
    if isTrashed:
        list_kwargs["state"] = "trashed"
    if albumId:
        list_kwargs["album_id"] = uuid_to_gumnut_album_id(albumId)
    elif personId:
        list_kwargs["person_ids"] = [uuid_to_gumnut_person_id(personId)]

    filtered_assets = [a async for a in client.assets.list(**list_kwargs)]

    # Collapse and badge together, gated on `withStacked` and skipped for trash
    # — see "Timeline stack collapse" in docs/architecture/adapter-architecture.md
    # for why each gate is load-bearing.
    stacks: TimelineStacks | None = None
    if withStacked and not isTrashed:
        # Which month, how big, and which filter shape — a burst of these
        # records is otherwise unnarrowable to any of the three.
        degradation_context = {
            "time_bucket": timeBucket,
            "bucket_asset_count": len(filtered_assets),
            "album_id": str(albumId) if albumId else None,
            "person_id": str(personId) if personId else None,
        }
        try:
            stacks = await resolve_timeline_stacks(client, filtered_assets)
        except GumnutError:
            # The assets already came back fine; a stacks-resource failure
            # should cost the badges, not the month. Falling back to an empty
            # resolution reproduces the pre-stack response — every frame, all
            # tuples null — on the app's primary view.
            #
            # Deliberately WARNING for every upstream status, including the 5xx
            # that *Upstream response log levels* maps to ERROR: that rule is
            # for calls whose failure fails the request. This one is designed to
            # fail, so an outage here is expected and quiet, and the ERROR
            # channel is reserved for the clause below.
            logger.warning(
                "Failed to resolve timeline stacks; returning the bucket uncollapsed",
                exc_info=True,
                extra=degradation_context,
            )
            stacks = TimelineStacks.empty()
        except Exception:
            # Same degradation, because the promise above is about the endpoint,
            # not about which exception family broke it: `resolve_timeline_stacks`
            # does non-SDK work in this scope too (the `zip(..., strict=True)`
            # that names its ordering invariant, the capture-time sort key), and
            # a raise from any of it would turn a feature designed to degrade
            # into a 500 on the app's primary view. ERROR because reaching here
            # means an adapter bug rather than an upstream one.
            logger.error(
                "Unexpected error resolving timeline stacks; returning the "
                "bucket uncollapsed",
                exc_info=True,
                extra=degradation_context,
            )
            stacks = TimelineStacks.empty()
        filtered_assets = [
            asset for asset in filtered_assets if not stacks.is_collapsed_away(asset)
        ]

    asset_count = len(filtered_assets)

    asset_ids = []
    created_at_list = []  # Upload timestamps (actual UTC)
    # Capture timestamps, currently emitted as wall-clock local time. Immich v3
    # expects actual UTC here (clients derive local time by adding
    # localOffsetHours), so known-timezone captures display shifted — a known
    # gap, fixed separately from the createdAt addition.
    file_created_at_list = []
    is_image_list = []
    ratio_list = []
    visibility_list = []
    local_offset_hours_list = []
    is_trashed_list = []
    duration_list: list[int | None] = []
    thumbhash_list: list[str | None] = []
    stack_list: list[list[str] | None] = []
    city_list: list[str | None] = []
    country_list: list[str | None] = []
    latitude_list: list[float | None] = []
    longitude_list: list[float | None] = []

    for asset in filtered_assets:
        asset_id = asset.id
        captured_at = resolve_capture_datetime(asset)
        aspect_ratio = (
            asset.width / asset.height if asset.width and asset.height else 1.0
        )
        utc_offset = captured_at.utcoffset()
        if captured_at.tzinfo and utc_offset is not None:
            local_datetime_offset = int(utc_offset.total_seconds() / 3600)
        else:
            local_datetime_offset = 0

        asset_ids.append(str(safe_uuid_from_asset_id(asset_id)))

        # Immich's TimeBucketAssetResponseDto needs ISO 8601 without timezone
        # and exactly 3 digits of milliseconds (e.g., "2023-10-05T09:41:00.123").
        created_at_list.append(
            resolve_created_at(asset).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        )
        file_created_at_list.append(captured_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3])

        is_image_list.append(
            mime_type_to_asset_type(asset.mime_type) == AssetTypeEnum.IMAGE
        )

        ratio_list.append(float(aspect_ratio))

        visibility_list.append(AssetVisibility.timeline)

        local_offset_hours_list.append(local_datetime_offset)

        is_trashed_list.append(bool(asset.trashed_at))

        # Forward each asset's duration: duration_ms returns integer
        # milliseconds (Immich v3), or None on NULL upstream — preserving this
        # bucket's prior all-None output for images / not-yet-extracted videos.
        duration_list.append(duration_ms(asset.duration))

        # Forward each asset's real thumbhash (base64 ThumbHash) so clients
        # render a distinct placeholder per tile. None until upstream generates
        # it. Previously every tile shipped one shared hardcoded constant.
        thumbhash_list.append(asset.thumbhash)

        # None for a loose asset, and also for a stack the adapter could not
        # resolve — see `TimelineStacks`. Appended in the same loop as every
        # other column so the parallel arrays stay index-aligned.
        stack_list.append(stacks.tuple_for(asset) if stacks is not None else None)

        location = resolve_asset_location(asset)
        city_list.append(location.city)
        country_list.append(location.country)
        latitude_list.append(location.latitude if withCoordinates is True else None)
        longitude_list.append(location.longitude if withCoordinates is True else None)

    # Build Immich's columnar timeline response with index-aligned arrays.
    response: dict[str, Any] = {
        "city": city_list,
        "country": country_list,
        "createdAt": created_at_list,
        "duration": duration_list,
        "fileCreatedAt": file_created_at_list,
        "id": asset_ids,
        "isFavorite": [False] * asset_count,
        "isImage": is_image_list,
        "isTrashed": is_trashed_list,
        "latitude": latitude_list,
        "livePhotoVideoId": [None] * asset_count,
        "localOffsetHours": local_offset_hours_list,
        "longitude": longitude_list,
        "ownerId": [str(current_user_id)] * asset_count,
        "projectionType": [None] * asset_count,
        "ratio": ratio_list,
        "thumbhash": thumbhash_list,
        "visibility": visibility_list,
    }

    # Omitted rather than all-nulls when stacks weren't requested, matching
    # upstream. The client reads `stack?.at(i)`, so absence and a null entry
    # behave identically for a loose asset.
    if stacks is not None:
        response["stack"] = stack_list

    return response
