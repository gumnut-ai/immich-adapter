# from uuid import UUID

from fastapi import APIRouter
# , Depends, HTTPException
# from pydantic import BaseModel
# from sqlalchemy import delete, select, update
# from sqlalchemy.ext.asyncio import AsyncSession
# from sqlalchemy.orm import selectinload

# from database.config import get_async_session
# from database.models.asset import Asset
# from database.models.asset_stack import AssetStack
# from routers.immich.models import ImmichStack, build_immich_stack

router = APIRouter(
    prefix="/api/stacks",
    tags=["stacks"],
    responses={404: {"description": "Not found"}},
)


# @router.get("/{stack_uuid}")
# async def get_stack(
#     stack_uuid: UUID, db: AsyncSession = Depends(get_async_session)
# ) -> ImmichStack:
#     result = await db.execute(
#         select(AssetStack)
#         .where(AssetStack.id == AssetStack.uuid_to_id(stack_uuid))
#         .options(
#             selectinload(AssetStack.assets).joinedload(Asset.exif),
#             selectinload(AssetStack.assets).joinedload(Asset.stack),
#         )
#     )
#     stack = result.scalars().first()
#     if stack is None:
#         raise HTTPException(status_code=404, detail="Stack not found")
#     return build_immich_stack(stack)


# class CreateStackRequest(BaseModel):
#     assetIds: list[UUID]


# @router.post("")
# async def create_stack(
#     request: CreateStackRequest, db: AsyncSession = Depends(get_async_session)
# ) -> ImmichStack:
#     """Creates a new stack or merges existing stacks into a new one."""
#     request_asset_ids = [Asset.uuid_to_id(asset_id) for asset_id in request.assetIds]

#     if len(request_asset_ids) == 0:
#         raise HTTPException(
#             status_code=400, detail="At least one asset is required to create a stack"
#         )

#     # Verify all assets exist
#     result = await db.execute(select(Asset).where(Asset.id.in_(request_asset_ids)))
#     assets = result.scalars().all()
#     if len(assets) != len(request_asset_ids):
#         raise HTTPException(status_code=404, detail="One or more assets not found")

#     # Find all existing stacks that contain any of the requested assets
#     existing_stack_ids = set()
#     for asset in assets:
#         if asset.stack_id is not None:
#             existing_stack_ids.add(asset.stack_id)

#     # Find all assets that are in the existing stacks (including ones not in the request)
#     additional_assets = []
#     if existing_stack_ids:
#         result = await db.execute(
#             select(Asset).where(
#                 Asset.stack_id.in_(existing_stack_ids),
#                 Asset.id.notin_(request_asset_ids),
#             )
#         )
#         additional_assets = result.scalars().all()

#     # Create new stack
#     primary_asset_id = request_asset_ids[0]
#     new_stack = AssetStack(primary_asset_id=primary_asset_id)
#     db.add(new_stack)
#     await db.flush()  # Get the stack ID without committing

#     # Update all assets to be part of the new stack
#     all_asset_ids = request_asset_ids + [asset.id for asset in additional_assets]
#     await db.execute(
#         update(Asset).where(Asset.id.in_(all_asset_ids)).values(stack_id=new_stack.id)
#     )

#     # Delete the old stacks
#     if existing_stack_ids:
#         await db.execute(
#             delete(AssetStack).where(AssetStack.id.in_(existing_stack_ids))
#         )

#     await db.commit()

#     # Eagerly load relationships for the new stack before serialization
#     result = await db.execute(
#         select(AssetStack)
#         .where(AssetStack.id == new_stack.id)
#         .options(
#             selectinload(AssetStack.assets).joinedload(Asset.exif),
#             selectinload(AssetStack.assets).joinedload(Asset.stack),
#         )
#     )
#     loaded_stack = result.scalars().first()
#     if loaded_stack is None:
#         raise HTTPException(status_code=404, detail="Stack not found after creation")
#     return build_immich_stack(loaded_stack)


# class DeleteStacksRequest(BaseModel):
#     ids: list[UUID]


# @router.delete("")
# async def delete_stacks(
#     request: DeleteStacksRequest, db: AsyncSession = Depends(get_async_session)
# ):
#     # Convert UUIDs to internal IDs
#     stack_ids = [AssetStack.uuid_to_id(stack_uuid) for stack_uuid in request.ids]

#     if not stack_ids:
#         return {"success": True}  # No stacks to delete

#     # Find all stacks to delete
#     result = await db.execute(
#         select(AssetStack)
#         .options(selectinload(AssetStack.assets))
#         .where(AssetStack.id.in_(stack_ids))
#     )
#     stacks = result.unique().scalars().all()
#     found_stack_ids = [stack.id for stack in stacks]

#     # Check if all stacks were found
#     if len(found_stack_ids) != len(stack_ids):
#         raise HTTPException(status_code=404, detail="One or more stacks not found")

#     # Remove stack_id reference from all assets in these stacks
#     await db.execute(
#         update(Asset).where(Asset.stack_id.in_(stack_ids)).values(stack_id=None)
#     )

#     # Delete the stacks
#     await db.execute(delete(AssetStack).where(AssetStack.id.in_(stack_ids)))

#     await db.commit()

#     return {"success": True}
