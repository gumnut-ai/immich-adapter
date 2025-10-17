from collections import defaultdict
from datetime import datetime
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from gumnut import Gumnut
from routers.utils.dependencies import get_authenticated_gumnut_client
from routers.utils.gumnut_client import get_gumnut_client
from routers.utils.error_mapping import map_gumnut_error
from routers.api.auth import get_current_user_id
from routers.immich_models import (
    AssetOrder,
    TimeBucketsResponseDto,
    AssetVisibility,
)
from typing import Any, List
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    uuid_to_gumnut_album_id,
    uuid_to_gumnut_person_id,
)
from gumnut.types.asset_response import AssetResponse

router = APIRouter(
    prefix="/api/timeline",
    tags=["timeline"],
    responses={404: {"description": "Not found"}},
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
    client: Gumnut = Depends(get_authenticated_gumnut_client),
) -> List[TimeBucketsResponseDto]:
    if (
        isFavorite
        or isTrashed
        or (visibility is not None and visibility != AssetVisibility.timeline)
    ):
        return []  # Gumnut does not support favorites, trashed, hidden, archived or locked assets, so return empty list

    try:
        # Call assets.list() with optional albumId parameter
        if albumId:
            gumnut_album_id = uuid_to_gumnut_album_id(albumId)
            gumnut_assets_response = client.albums.assets.list(gumnut_album_id)
            gumnut_assets = list(gumnut_assets_response)
        elif personId:
            gumnut_assets_response = client.assets.list(
                person_id=uuid_to_gumnut_person_id(personId)
            )
            gumnut_assets = list(gumnut_assets_response)
        else:
            # Get all assets
            gumnut_assets_response = client.assets.list()
            gumnut_assets = list(gumnut_assets_response)

        # Process and group assets by month
        date_counts = defaultdict(int)

        for asset in gumnut_assets:
            # Extract local_datetime or created_at for month grouping
            if isinstance(asset, dict):
                local_datetime = asset.get("local_datetime") or asset.get("created_at")
            else:
                local_datetime = getattr(asset, "local_datetime", None) or getattr(
                    asset, "created_at", None
                )

            if local_datetime:
                # Parse the datetime if it's a string
                if isinstance(local_datetime, str):
                    try:
                        dt = datetime.fromisoformat(
                            local_datetime.replace("Z", "+00:00")
                        )
                    except (ValueError, AttributeError):
                        dt = datetime.now()
                else:
                    dt = local_datetime

                # Group by month only (ignore day and time)
                # Format as YYYY-MM-01 to group by month
                month_key = dt.strftime("%Y-%m-01")
                date_counts[month_key] += 1

        # Sort by month (descending by default)
        sorted_dates = sorted(
            date_counts.items(), key=lambda x: x[0], reverse=(order != AssetOrder.asc)
        )

        # Convert to TimeBucketsResponseDto format
        buckets = [
            TimeBucketsResponseDto(timeBucket=date, count=count)
            for date, count in sorted_dates
        ]

        return buckets

    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch timeline buckets")


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
    client: Gumnut = Depends(get_authenticated_gumnut_client),
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
        # Call assets.list() with optional albumId parameter
        if albumId:
            gumnut_album_id = uuid_to_gumnut_album_id(albumId)
            gumnut_assets_response = client.albums.assets.list(gumnut_album_id)
            gumnut_assets = list(gumnut_assets_response)
        elif personId:
            gumnut_assets_response = client.assets.list(
                person_id=uuid_to_gumnut_person_id(personId)
            )
            gumnut_assets = list(gumnut_assets_response)
        else:
            # Get all assets
            gumnut_assets_response = client.assets.list()
            gumnut_assets = list(gumnut_assets_response)

        # Filter assets by year and month matching timeBucket
        bucketDate = datetime.fromisoformat(timeBucket)
        target_year = bucketDate.year
        target_month = bucketDate.month
        filtered_assets: List[AssetResponse] = [
            asset
            for asset in gumnut_assets
            if asset.local_datetime.year == target_year
            and asset.local_datetime.month == target_month
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
            mime_type = asset.mime_type
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
            is_image_list.append(mime_type.startswith("image/"))

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
            "ownerId": [str(get_current_user_id())] * asset_count,  # Fixed owner ID
            "thumbhash": ["FBgGFYRQjHbAZpiWWpeEhWPANQZr"]
            * asset_count,  # Fixed thumbhash
            # Optional fields with reasonable defaults
            "latitude": [None] * asset_count,  # No GPS data available
            "longitude": [None] * asset_count,  # No GPS data available
            "stack": [None] * asset_count,  # No stack information available
        }

    except Exception as e:
        raise map_gumnut_error(e, "Failed to fetch timeline bucket")
