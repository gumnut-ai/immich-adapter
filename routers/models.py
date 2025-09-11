# from base64 import b64encode
from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ExifInfo(BaseModel):
    make: str | None = None
    model: str | None = None
    exifImageWidth: int | None = None
    exifImageHeight: int | None = None
    fileSizeInByte: int | None = None
    orientation: str | None = None
    dateTimeOriginal: datetime | None = None
    modifyDate: datetime | None = None
    timeZone: str | None = None
    lensModel: str | None = None
    fNumber: float | None = None
    focalLength: float | None = None
    iso: int | None = None
    exposureTime: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    description: str = ""
    projectionType: str | None = None
    rating: int | None = None


class ImmichPerson(BaseModel):
    id: UUID
    name: str | None = None
    birthDate: date | None = None
    thumbnailPath: str | None = None
    isHidden: bool = False
    isFavorite: bool = False
    updatedAt: datetime


class ImmichStackSummary(BaseModel):
    id: UUID
    primaryAssetId: UUID
    assetCount: int


class ImmichAsset(BaseModel):
    id: UUID
    deviceAssetId: str
    ownerId: str = "d6773835-4b91-4c7d-8667-26bd5daa1a45"  # TODO: placeholder
    deviceId: str
    libraryId: str | None = None
    type: str
    originalPath: str | None = None
    originalFileName: str
    originalMimeType: str | None = None
    thumbhash: str | None = None
    fileCreatedAt: datetime
    fileModifiedAt: datetime
    localDateTime: datetime
    updatedAt: datetime
    isFavorite: bool = False
    isArchived: bool = False
    isTrashed: bool = False
    duration: str = "0:00:00.00000"
    exifInfo: ExifInfo = Field(default_factory=ExifInfo)
    livePhotoVideoId: str | None = None
    people: list[ImmichPerson] = []
    checksum: str
    stack: ImmichStackSummary | None = None
    isOffline: bool = False
    hasMetadata: bool = True
    duplicateId: str | None = None
    resized: bool = True

    model_config = ConfigDict(from_attributes=True)


class ImmichAlbum(BaseModel):
    id: UUID
    albumName: str
    description: str | None = None
    albumThumbnailAssetId: UUID | None = None
    createdAt: datetime
    updatedAt: datetime
    startDate: datetime | None = None
    endDate: datetime | None = None
    lastModifiedAssetTimestamp: datetime | None = None
    ownerId: UUID | None = None
    owner: UUID | None = None
    albumUsers: list[UUID]
    shared: bool
    hasSharedLink: bool
    assets: list[ImmichAsset]
    assetCount: int
    isActivityEnabled: bool
    order: str

    model_config = ConfigDict(from_attributes=True)


class ImmichStack(BaseModel):
    id: UUID
    primaryAssetId: UUID
    assets: list[ImmichAsset]

    model_config = ConfigDict(from_attributes=True)


# def build_immich_exif(exif: Exif) -> ExifInfo:
#     if exif is None:
#         return ExifInfo()

#     # Convert exposure_time (float) to a fraction string like "1/66"
#     exposure_time_str = None
#     if exif.exposure_time is not None:
#         if exif.exposure_time >= 1:
#             exposure_time_str = str(exif.exposure_time)
#         else:
#             denominator = round(1 / exif.exposure_time)
#             exposure_time_str = f"1/{denominator}"

#     return ExifInfo(
#         make=exif.make,
#         model=exif.model,
#         exifImageWidth=None,
#         exifImageHeight=None,
#         fileSizeInByte=None,
#         orientation=str(exif.orientation),
#         dateTimeOriginal=exif.original_datetime,
#         modifyDate=exif.modified_datetime,
#         lensModel=exif.lens_model,
#         fNumber=exif.f_number,
#         focalLength=exif.focal_length,
#         iso=exif.iso,
#         exposureTime=exposure_time_str,
#         latitude=exif.latitude,
#         longitude=exif.longitude,
#     )


# def build_immich_asset(asset: Asset) -> ImmichAsset:
#     # Convert binary checksum to base64 string
#     checksum_b64 = b64encode(asset.checksum).decode("utf-8")
#     stack_summary = (
#         ImmichStackSummary(
#             id=AssetStack.id_to_uuid(asset.stack_id),
#             primaryAssetId=Asset.id_to_uuid(asset.stack.primary_asset_id),
#             assetCount=len(asset.stack.assets),
#         )
#         if asset.stack_id
#         else None
#     )

#     return ImmichAsset(
#         id=Asset.id_to_uuid(asset.id),
#         deviceAssetId=asset.device_asset_id,
#         deviceId=asset.device_id,
#         type="IMAGE" if asset.mime_type.startswith("image/") else "VIDEO",
#         originalFileName=asset.original_file_name,
#         originalMimeType=asset.mime_type,
#         fileCreatedAt=asset.file_created_at,
#         fileModifiedAt=asset.file_modified_at,
#         localDateTime=asset.local_datetime,
#         updatedAt=asset.updated_at,
#         checksum=checksum_b64,
#         exifInfo=build_immich_exif(asset.exif),
#         stack=stack_summary,
#     )


# def build_immich_album(album: Album, asset_count: int = 0) -> ImmichAlbum:
#     album_cover_asset_id = (
#         Asset.id_to_uuid(album.album_cover_asset_id)
#         if album.album_cover_asset_id
#         else None
#     )

#     return ImmichAlbum(
#         id=Album.id_to_uuid(album.id),
#         albumName=album.name,
#         description=album.description,
#         albumThumbnailAssetId=album_cover_asset_id,
#         createdAt=album.created_at,
#         updatedAt=album.updated_at,
#         ownerId=UUID("d6773835-4b91-4c7d-8667-26bd5daa1a45"),  # TODO: placeholder
#         owner=None,
#         albumUsers=[],
#         shared=False,
#         hasSharedLink=False,
#         assets=[],
#         assetCount=asset_count,
#         isActivityEnabled=True,
#         order="desc",
#     )


# def build_immich_stack(stack: AssetStack) -> ImmichStack:
#     return ImmichStack(
#         id=AssetStack.id_to_uuid(stack.id),
#         primaryAssetId=Asset.id_to_uuid(stack.primary_asset_id),
#         assets=[build_immich_asset(asset) for asset in stack.assets],
#     )


# def build_immich_person(person: Person) -> ImmichPerson:
#     """
#     Convert a Person model to the Immich API format.
#     """
#     return ImmichPerson(
#         id=Person.id_to_uuid(person.id),
#         name=person.name,
#         birthDate=person.birth_date,
#         isHidden=person.is_hidden,
#         isFavorite=person.is_favorite,
#         updatedAt=person.updated_at,
#     )
