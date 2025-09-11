from fastapi import APIRouter

router = APIRouter(
    prefix="/api/albums",
    tags=["albums"],
    responses={404: {"description": "Not found"}},
)


# @router.get("")
# async def get_albums(
#     db: AsyncSession = Depends(get_async_session),
# ) -> list[ImmichAlbum]:
#     albums = await db.scalars(select(Album))
#     album_asset_counts = await db.execute(
#         select(
#             AlbumAsset.album_id, func.count(AlbumAsset.asset_id).label("count")
#         ).group_by(AlbumAsset.album_id)
#     )

#     # Convert album_asset_counts to a dictionary
#     album_asset_counts_dict = {
#         album_id: count for album_id, count in album_asset_counts.all()
#     }

#     # Convert to response format
#     response = []
#     for album in albums:
#         immich_album = build_immich_album(
#             album, album_asset_counts_dict.get(album.id, 0)
#         )
#         response.append(immich_album)
#     return response


# class CreateAlbumRequest(BaseModel):
#     albumName: str
#     albumUsers: list[dict[str, str]] | None = None
#     assetIds: list[str] | None = None
#     description: str | None = None


# @router.post("")
# async def create_album(
#     request: CreateAlbumRequest, db: AsyncSession = Depends(get_async_session)
# ) -> ImmichAlbum:
#     # Create new album
#     # If album name is empty, set it to "New Album"
#     album = Album(
#         name=request.albumName or "New Album",
#         description=request.description or "",
#     )

#     # Add to database
#     db.add(album)
#     await db.commit()
#     await db.refresh(album)

#     return build_immich_album(album)


# @router.get("/{album_uuid}")
# async def get_album(
#     album_uuid: UUID, db: AsyncSession = Depends(get_async_session)
# ) -> ImmichAlbum:
#     album_id = Album.uuid_to_id(album_uuid)
#     print(f"Getting album {album_id} (id: {album_id})")

#     album = await db.execute(select(Album).where(Album.id == album_id))
#     album = album.scalar_one_or_none()
#     if not album:
#         raise HTTPException(status_code=404, detail="Album not found")

#     return build_immich_album(album)


# class UpdateAlbumRequest(BaseModel):
#     albumName: str | None = None
#     description: str | None = None
#     albumThumbnailAssetId: UUID | None = None


# @router.patch("/{album_uuid}")
# async def update_album(
#     album_uuid: UUID,
#     request: UpdateAlbumRequest,
#     db: AsyncSession = Depends(get_async_session),
# ) -> ImmichAlbum:
#     album_id = Album.uuid_to_id(album_uuid)
#     album = await db.execute(select(Album).where(Album.id == album_id))
#     album = album.scalar_one_or_none()
#     if not album:
#         raise HTTPException(status_code=404, detail="Album not found")

#     if request.albumThumbnailAssetId:
#         asset_id = Asset.uuid_to_id(request.albumThumbnailAssetId)
#         asset = await db.get(Asset, asset_id)
#         if not asset:
#             raise HTTPException(status_code=404, detail="Asset not found")

#         album.album_cover_asset_id = asset_id

#     if request.albumName is not None:
#         album.name = request.albumName

#     if request.description is not None:
#         album.description = request.description

#     await db.commit()
#     await db.refresh(album)
#     return build_immich_album(album)


# class UpdateAlbumAssetsRequest(BaseModel):
#     ids: list[UUID]


# @router.put("/{album_uuid}/assets")
# async def update_album_assets(
#     album_uuid: UUID,
#     request: UpdateAlbumAssetsRequest,
#     db: AsyncSession = Depends(get_async_session),
# ):
#     album_id = Album.uuid_to_id(album_uuid)
#     album = await db.execute(select(Album).where(Album.id == album_id))
#     album = album.scalar_one_or_none()
#     if not album:
#         raise HTTPException(
#             status_code=404,
#             detail=f"Album not found {album_uuid} -> {Album.uuid_to_id(album_uuid)}",
#         )

#     # Get existing album assets - only select asset_ids
#     existing_album_asset_ids = set(
#         await db.scalars(
#             select(AlbumAsset.asset_id).where(AlbumAsset.album_id == album_id)
#         )
#     )

#     # For each asset id in the request, check if it exists in the existing album assets
#     # If it does, add it to the response as a duplicate
#     # If it doesn't, add it to the album assets and add it to the response as a success
#     response = []
#     new_album_assets = []

#     for asset_uuid in request.ids:
#         asset_id = Asset.uuid_to_id(asset_uuid)
#         if asset_id in existing_album_asset_ids:
#             # Asset already exists in album, mark as duplicate
#             response.append(
#                 {"id": asset_uuid, "success": False, "error": "DUPLICATE_ASSET"}
#             )
#         else:
#             # Asset doesn't exist in album, add it
#             new_album_assets.append(AlbumAsset(album_id=album_id, asset_id=asset_id))
#             response.append({"id": asset_uuid, "success": True})

#     # Add new album assets to database
#     if new_album_assets:
#         db.add_all(new_album_assets)
#         await db.commit()

#     return response
