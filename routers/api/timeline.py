from datetime import datetime
from typing import Any, List
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from gumnut import AsyncGumnut
from gumnut.types.asset_count_response import AssetCountResponse, Data

from routers.api.constants import PHOTOS_API_MAX_PAGE_SIZE
from routers.immich_models import (
    AssetOrder,
    AssetTypeEnum,
    AssetVisibility,
    TimeBucketsResponseDto,
)
from routers.utils.asset_conversion import mime_type_to_asset_type
from routers.utils.current_user import get_current_user_id
from routers.utils.error_mapping import map_gumnut_error
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    uuid_to_gumnut_album_id,
    uuid_to_gumnut_person_id,
)

router = APIRouter(
    prefix="/api/timeline",
    tags=["timeline"],
    responses={404: {"description": "Not found"}},
)


async def _fetch_asset_counts(
    client: AsyncGumnut,
    *,
    album_id: str | None = None,
    person_id: str | None = None,
) -> list[Data]:
    """Fetch all monthly asset counts from photos-api, paginating if needed."""
    kwargs: dict[str, Any] = {"group_by": "month", "limit": PHOTOS_API_MAX_PAGE_SIZE}
    if album_id is not None:
        kwargs["album_id"] = album_id
    if person_id is not None:
        kwargs["person_id"] = person_id

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
    if (
        isFavorite
        or isTrashed
        or (visibility is not None and visibility != AssetVisibility.timeline)
    ):
        return []  # Gumnut does not support favorites, trashed, hidden, archived or locked assets, so return empty list

    try:
        album_id = uuid_to_gumnut_album_id(albumId) if albumId else None
        person_id = uuid_to_gumnut_person_id(personId) if personId else None

        raw_buckets = await _fetch_asset_counts(
            client, album_id=album_id, person_id=person_id
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

    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch timeline buckets") from e


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
) -> Any:  # Should be TimeBucketAssetResponseDto, but using Any to bypass Pydantic validation. See comment below.
    """
    This endpoint retrieves assets that match the specified time bucket.
    There seems to be an issue with how TimeBucketAssetResponseDto is generated from the OpenAPI spec.
    The spec shows that certain fields of the DTO (such as "city" and "country") are arrays of strings
    that can be nullable, but the generated DTO does not allow for this. I would think that the fields
    should be defined as such:

        city: Annotated[
            List[str | None], Field(description="Array of city names extracted from EXIF GPS data")
        ]

    To work around this, we return a dict with the correct structure instead of using the DTO directly.
    However, this causes the OpenAPI Compatibility Validator to show a warning for this endpoint.
    """

    try:
        # Compute month boundaries from timeBucket for server-side date filtering.
        # The Immich client may send naive ("2024-01-01T00:00:00") or UTC-aware
        # ("2024-01-01T00:00:00.000Z") timestamps. We always strip timezone info
        # so boundaries are naive, matching the photos-api counts endpoint which
        # groups by date_trunc("month", local_datetime) on the naive column.
        # Uses a half-open interval [month_start, next_month_start) for clean boundaries.
        bucket_date = datetime.fromisoformat(timeBucket).replace(tzinfo=None)
        month_start = bucket_date.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        if month_start.month == 12:
            next_month_start = month_start.replace(year=month_start.year + 1, month=1)
        else:
            next_month_start = month_start.replace(month=month_start.month + 1)
        date_range_query = {
            "local_datetime_after": month_start.isoformat(),
            "local_datetime_before": next_month_start.isoformat(),
        }

        if albumId:
            gumnut_album_id = uuid_to_gumnut_album_id(albumId)
            filtered_assets = [
                a
                async for a in client.assets.list(
                    album_id=gumnut_album_id,
                    extra_query=date_range_query,
                )
            ]
        elif personId:
            filtered_assets = [
                a
                async for a in client.assets.list(
                    person_id=uuid_to_gumnut_person_id(personId),
                    extra_query=date_range_query,
                )
            ]
        else:
            filtered_assets = [
                a async for a in client.assets.list(extra_query=date_range_query)
            ]

        # Build the response arrays based on filtered assets
        asset_count = len(filtered_assets)

        # Initialize arrays for the response
        asset_ids = []
        file_created_at_list = []
        is_image_list = []
        ratio_list = []
        visibility_list = []
        local_offset_hours_list = []

        for asset in filtered_assets:
            asset_id = asset.id
            created_at = asset.local_datetime
            aspect_ratio = (
                asset.width / asset.height if asset.height and asset.width else 1.0
            )
            # get the local datetime offset in hours from UTC
            utc_offset = asset.local_datetime.utcoffset()
            if asset.local_datetime.tzinfo and utc_offset is not None:
                local_datetime_offset = int(utc_offset.total_seconds() / 3600)
            else:
                local_datetime_offset = 0

            # Convert Gumnut asset ID to UUID format for response
            asset_ids.append(str(safe_uuid_from_asset_id(asset_id)))

            # Format file_created_at_list timestamp to ISO 8601 without timezone and 3 digits of milliseconds.
            # This is a format only used for TimeBucketAssetResponseDto.
            # Example: "2023-10-05T09:41:00.123"
            file_created_at_list.append(
                created_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            )

            # Determine if asset is an image (vs video) based on MIME type
            is_image_list.append(
                mime_type_to_asset_type(asset.mime_type) == AssetTypeEnum.IMAGE
            )

            ratio_list.append(float(aspect_ratio))

            # Set visibility (always timeline for now)
            visibility_list.append(AssetVisibility.timeline)

            local_offset_hours_list.append(local_datetime_offset)

        # Return as dict to bypass Pydantic validation issues with None in List[str]
        # XXX revisit this issue later
        return {
            # Fields that should only contain None (as specified)
            "city": [None] * asset_count,
            "country": [None] * asset_count,
            "duration": [None] * asset_count,  # We don't have duration data
            "livePhotoVideoId": [None] * asset_count,
            "projectionType": [None] * asset_count,
            # Real data from assets
            "id": asset_ids,
            "fileCreatedAt": file_created_at_list,
            "isImage": is_image_list,
            "ratio": ratio_list,
            "visibility": visibility_list,
            "localOffsetHours": local_offset_hours_list,
            # Fixed values as specified
            "isFavorite": [False] * asset_count,  # Always False
            "isTrashed": [False] * asset_count,  # Always False
            "ownerId": [str(current_user_id)] * asset_count,  # Current user as owner
            "thumbhash": ["FBgGFYRQjHbAZpiWWpeEhWPANQZr"]
            * asset_count,  # Fixed thumbhash
            # Optional fields with reasonable defaults
            "latitude": [None] * asset_count,  # No GPS data available
            "longitude": [None] * asset_count,  # No GPS data available
            "stack": [None] * asset_count,  # No stack information available
        }

    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch timeline bucket") from e
