from collections import defaultdict
from datetime import datetime
from uuid import UUID
from fastapi import APIRouter, HTTPException, Query
from routers.utils.gumnut_client import get_gumnut_client
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
) -> List[TimeBucketsResponseDto]:
    client = get_gumnut_client()

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
        # Provide more detailed error information
        error_msg = str(e)
        if "401" in error_msg or "Invalid API key" in error_msg:
            raise HTTPException(status_code=401, detail="Invalid Gumnut API key")
        elif "403" in error_msg:
            raise HTTPException(status_code=403, detail="Access denied to Gumnut API")
        elif "404" in error_msg:
            raise HTTPException(status_code=404, detail="Assets not found")
        else:
            raise HTTPException(
                status_code=500, detail=f"Failed to fetch timeline buckets: {error_msg}"
            )


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
    client = get_gumnut_client()

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
        filtered_assets = []

        for asset in gumnut_assets:
            # Extract local_datetime or created_at for date filtering
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
                        continue  # Skip assets with invalid dates
                else:
                    dt = local_datetime

                # Check if asset matches the target year and month
                if dt.year == target_year and dt.month == target_month:
                    filtered_assets.append(asset)

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
            # Extract asset data (handle both dict and object formats)
            if isinstance(asset, dict):
                asset_id = asset.get("id", "unknown")
                created_at = asset.get("local_datetime") or asset.get("created_at")
                mime_type = asset.get("mime_type", "")
                # Use placeholder aspect ratio if not available
                aspect_ratio = asset.get("aspect_ratio", 1.0)
                local_datetime_offset = asset.get("local_datetime_offset", 0)
            else:
                asset_id = getattr(asset, "id", "unknown")
                created_at = getattr(asset, "local_datetime", None) or getattr(
                    asset, "created_at", None
                )
                mime_type = getattr(asset, "mime_type", "")
                aspect_ratio = getattr(asset, "aspect_ratio", 1.0)
                local_datetime_offset = getattr(asset, "local_datetime_offset", 0)

            # Convert Gumnut asset ID to UUID format for response
            asset_ids.append(str(safe_uuid_from_asset_id(asset_id)))

            # Format created_at timestamp
            if created_at:
                if isinstance(created_at, str):
                    try:
                        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        # Format as ISO without timezone (as required)
                        file_created_at_list.append(
                            dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                        )
                    except (ValueError, AttributeError):
                        file_created_at_list.append(
                            "2024-01-01T00:00:00.000"
                        )  # Fallback
                else:
                    file_created_at_list.append(
                        created_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                    )
            else:
                file_created_at_list.append(
                    "2024-01-01T00:00:00.000"
                )  # Fallback timestamp

            # Determine if asset is an image (vs video) based on MIME type
            is_image_list.append(mime_type.startswith("image/") if mime_type else True)

            # Add aspect ratio (width/height)
            ratio_list.append(float(aspect_ratio) if aspect_ratio else 1.0)

            # Set visibility (always timeline for now)
            visibility_list.append(AssetVisibility.timeline)

            # Use dummy timezone offset
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
        # Provide more detailed error information
        error_msg = str(e)
        if "401" in error_msg or "Invalid API key" in error_msg:
            raise HTTPException(status_code=401, detail="Invalid Gumnut API key")
        elif "403" in error_msg:
            raise HTTPException(status_code=403, detail="Access denied to Gumnut API")
        elif "404" in error_msg:
            raise HTTPException(status_code=404, detail="Assets not found")
        else:
            raise HTTPException(
                status_code=500, detail=f"Failed to fetch timeline bucket: {error_msg}"
            )
