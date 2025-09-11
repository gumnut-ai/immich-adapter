# from datetime import datetime
# from uuid import UUID

# from dateutil.relativedelta import relativedelta
from fastapi import APIRouter
# , Depends, HTTPException
# from sqlalchemy import Select, exists, func, select
# from sqlalchemy.ext.asyncio import AsyncSession
# from sqlalchemy.orm import joinedload

# from database.config import get_async_session
# from database.models.album import Album
# from database.models.album_asset import AlbumAsset
# from database.models.asset import Asset
# from database.models.asset_metric import AssetMetric
# from database.models.asset_stack import AssetStack
# from routers.immich.models import ImmichAsset, build_immich_asset

router = APIRouter(
    prefix="/api/timeline",
    tags=["timeline"],
    responses={404: {"description": "Not found"}},
)


# def filter_by_album(statement: Select, album_uuid: UUID) -> Select:
#     statement = statement.join(Asset.album_assets)
#     statement = statement.where(AlbumAsset.album_id == Album.uuid_to_id(album_uuid))
#     return statement


# # Not sure if treating low quality images as archived is the best way
# def filter_out_archived(statement: Select) -> Select:
#     # First perform the outer join to preserve all assets
#     statement = statement.join(Asset.asset_metrics, isouter=True)

#     # Now create a condition that checks:
#     # 1. Either there's no matching "liqe_mix" metric for this asset
#     # 2. Or there is one and its score is >= 1.375
#     subquery = (
#         select(1)
#         .where(AssetMetric.asset_id == Asset.id)
#         .where(AssetMetric.model_name == "liqe_mix")
#     )

#     statement = statement.where(
#         # Either no "liqe_mix" metric exists for this asset
#         ~exists(subquery).correlate(Asset)
#         # Or if it exists, the score is at least 1.375
#         | exists(subquery.where(AssetMetric.score >= 1.375)).correlate(Asset)
#     )

#     return statement


# @router.get("/buckets")
# async def get_buckets(
#     size: str,
#     albumId: UUID | None = None,
#     order: str = "desc",
#     isArchived: bool = False,
#     db: AsyncSession = Depends(get_async_session),
# ):
#     # Group assets by year and month from local_datetime
#     month_start = func.date_trunc("month", Asset.local_datetime).label("month_start")

#     order_by = (
#         month_start.desc()
#         if order == "desc"
#         else month_start.asc()
#         if order == "asc"
#         else None
#     )

#     if order_by is None:
#         raise HTTPException(
#             status_code=400,
#             detail=f"Invalid order parameter {order}, must be 'asc' or 'desc'",
#         )

#     statement = select(
#         month_start,
#         func.count().label("count"),
#     )

#     # Filter by archived if provided
#     if not isArchived:
#         statement = filter_out_archived(statement)

#     # Filter by album if provided
#     if albumId:
#         statement = filter_by_album(statement, albumId)

#     statement = statement.group_by(month_start).order_by(order_by)

#     # Execute query and format results
#     results = []
#     for row in await db.execute(statement):
#         # Format timestamp as ISO string with Z suffix for UTC, because that's what Immich expects
#         time_bucket = row.month_start.strftime("%Y-%m-%dT00:00:00.000Z")
#         results.append({"timeBucket": time_bucket, "count": row.count})

#     return results


# @router.get("/bucket")
# async def get_bucket(
#     timeBucket: datetime,
#     albumId: UUID | None = None,
#     order: str = "desc",
#     isArchived: bool = False,
#     db: AsyncSession = Depends(get_async_session),
# ) -> list[ImmichAsset]:
#     # Add 1 month to the time bucket start
#     time_bucket_end = timeBucket + relativedelta(months=1)

#     # Convert to offset-naive for DB query
#     if timeBucket.tzinfo is not None:
#         timeBucket = timeBucket.replace(tzinfo=None)
#     if time_bucket_end.tzinfo is not None:
#         time_bucket_end = time_bucket_end.replace(tzinfo=None)

#     # Query the database for assets in the given time bucket
#     statement = select(Asset).where(
#         Asset.local_datetime >= timeBucket,
#         Asset.local_datetime < time_bucket_end,
#     )

#     if not isArchived:
#         statement = filter_out_archived(statement)

#     stack_assets = True

#     if albumId:
#         statement = filter_by_album(statement, albumId)
#         # Don't stack assets for albums
#         stack_assets = False

#     # Eagerly load exif and stack relationships to avoid MissingGreenlet
#     statement = statement.options(joinedload(Asset.exif), joinedload(Asset.stack))

#     if stack_assets:
#         # Filter for assets that are primary assets in their stacks
#         statement = statement.join(
#             AssetStack, Asset.id == AssetStack.primary_asset_id, isouter=True
#         )
#         statement = statement.where(
#             (Asset.stack_id.is_(None)) | (Asset.id == AssetStack.primary_asset_id)
#         )

#     assets = await db.scalars(statement)

#     return [build_immich_asset(asset) for asset in assets]
