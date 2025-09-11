# import mimetypes
# import os
# from datetime import datetime
# from uuid import UUID

from fastapi import APIRouter
# , Depends, File, Form, HTTPException, UploadFile
# from fastapi.responses import FileResponse
# from pydantic import BaseModel

router = APIRouter(
    prefix="/api/assets",
    tags=["assets"],
    responses={404: {"description": "Not found"}},
)


fake_memory_lane = []

fake_bulk_upload_check = {
    "results": [
        {
            "id": "a.jpg",
            "action": "accept",
        },
        {
            "id": "b.jpg",
            "action": "reject",
            "reason": "duplicate",
            "assetId": "cd3873b9-3be0-486f-95f1-136cdf508392",
            "isTrashed": False,
        },
    ]
}


@router.get("/memory-lane")
async def get_memory_lane():
    return fake_memory_lane


@router.post("/bulk-upload-check")
async def bulk_upload_check():
    return fake_bulk_upload_check


# @router.post("")
# async def upload_file(
#     deviceAssetId: str = Form(...),
#     deviceId: str = Form(...),
#     fileCreatedAt: datetime = Form(...),
#     fileModifiedAt: datetime = Form(...),
#     isFavorite: bool = Form(...),
#     duration: str = Form(...),
#     assetData: UploadFile = File(...),
#     db: AsyncSession = Depends(get_async_session),
#     asset_service: AssetService = Depends(),
#     library_service: UserLibraryService = Depends(get_user_library_service),
# ):
#     # Get library context using smart defaulting (no library_id specified for Immich compatibility)
#     library = await library_service.get_library_context(None)

#     checksum = await FileStorageService.calculate_file_checksum(assetData)

#     # Check if file already exists based on owner and checksum
#     # TODO: Check based on owner and library
#     result = await db.execute(
#         select(Asset).where(Asset.checksum == checksum, Asset.library_id == library.id)
#     )
#     existing_asset = result.scalar_one_or_none()
#     if existing_asset:
#         return {"id": Asset.id_to_uuid(existing_asset.id), "status": "duplicate"}

#     if not assetData.filename:
#         raise HTTPException(status_code=427, detail="File has no filename")

#     mime_type = mimetypes.guess_type(assetData.filename)[0]
#     if not mime_type or not (
#         mime_type.startswith("image/") or mime_type.startswith("video/")
#     ):
#         raise HTTPException(status_code=427, detail="File must be an image or video")

#     asset = await asset_service.save_asset(
#         device_asset_id=deviceAssetId,
#         device_id=deviceId,
#         file_created_at=fileCreatedAt,
#         file_modified_at=fileModifiedAt,
#         mime_type=mime_type,
#         checksum=checksum,
#         asset_data=assetData,
#         library_id=library.id,
#     )

#     return {"id": Asset.id_to_uuid(asset.id), "status": "created"}


# class DeleteAssetRequest(BaseModel):
#     ids: list[UUID]


# @router.delete("")
# async def delete_asset(
#     delete_asset_request: DeleteAssetRequest,
#     db: AsyncSession = Depends(get_async_session),
#     asset_service: AssetService = Depends(),
# ):
#     internal_ids = [Asset.uuid_to_id(asset_id) for asset_id in delete_asset_request.ids]
#     await asset_service.delete_assets(internal_ids)

#     return {"success": True}


# @router.get("/{asset_uuid}")
# async def get_asset(
#     asset_uuid: UUID, db: AsyncSession = Depends(get_async_session)
# ) -> ImmichAsset:
#     result = await db.execute(
#         select(Asset).where(Asset.id == Asset.uuid_to_id(asset_uuid))
#     )
#     asset = result.scalar_one_or_none()
#     if not asset:
#         raise HTTPException(
#             status_code=404,
#             detail=f"Asset not found {asset_uuid} -> {Asset.uuid_to_id(asset_uuid)}",
#         )
#     return build_immich_asset(asset)


# @router.get(
#     "/{asset_uuid}/thumbnail",
#     response_class=FileResponse,
#     responses={
#         200: {
#             "description": "Any binary media",
#             "content": {
#                 "image/*": {"schema": {"$ref": "#/components/schemas/BinaryFile"}},
#                 "video/*": {"schema": {"$ref": "#/components/schemas/BinaryFile"}},
#                 "*/*": {"schema": {"$ref": "#/components/schemas/BinaryFile"}},
#             },
#         }
#     },
# )
# async def get_thumbnail(
#     asset_uuid: UUID,
#     size: str | None = None,
#     c: str | None = None,
#     db: AsyncSession = Depends(get_async_session),
#     storage: AssetStorageService = Depends(AssetStorageService),
# ) -> FileResponse:
#     result = await db.execute(
#         select(Asset).where(Asset.id == Asset.uuid_to_id(asset_uuid))
#     )
#     asset = result.scalar_one_or_none()
#     if not asset:
#         raise HTTPException(
#             status_code=404,
#             detail=f"Asset not found {asset_uuid} -> {Asset.uuid_to_id(asset_uuid)}",
#         )

#     preferred_size = size if size is not None else "thumbnail"
#     if preferred_size == "thumbnail" and asset.has_thumbnail:
#         image_type = "thumbnail"
#     elif preferred_size == "preview" and asset.has_preview:
#         image_type = "preview"
#     else:
#         image_type = "original"

#     retrieved_info = await storage.get_file(asset, image_type=image_type)

#     # Construct FileResponse using the retrieved info
#     background_task = None
#     if retrieved_info.is_temporary:
#         background_task = BackgroundTask(os.unlink, retrieved_info.path)

#     return FileResponse(
#         path=retrieved_info.path,
#         filename=retrieved_info.filename,
#         media_type=retrieved_info.media_type,
#         background=background_task,
#     )
