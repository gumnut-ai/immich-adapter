"""Canonical request mapping and sync upsert dependency order."""

from routers.immich_models import SyncEntityType, SyncRequestType

# FK parents stream before children. Stack rows precede member assets because
# the mobile timeline hides assets whose stack row has not arrived.
SYNC_TYPE_ORDER: list[tuple[SyncRequestType, str, SyncEntityType]] = [
    (SyncRequestType.StacksV1, "stack", SyncEntityType.StackV1),
    (SyncRequestType.AssetsV1, "asset", SyncEntityType.AssetV1),
    (SyncRequestType.AssetsV2, "asset", SyncEntityType.AssetV2),
    (SyncRequestType.AlbumsV1, "album", SyncEntityType.AlbumV1),
    (SyncRequestType.AlbumsV2, "album", SyncEntityType.AlbumV2),
    # AlbumUsersV1 is derived from the same album events as AlbumV1/V2. It
    # follows the album FK parent and the UserV1 pass, which runs before this
    # order. The v3 client requires an owner-role row to display each album.
    (SyncRequestType.AlbumUsersV1, "album", SyncEntityType.AlbumUserV1),
    (SyncRequestType.AlbumToAssetsV1, "album_asset", SyncEntityType.AlbumToAssetV1),
    (SyncRequestType.AssetExifsV1, "metadata", SyncEntityType.AssetExifV1),
    (SyncRequestType.PeopleV1, "person", SyncEntityType.PersonV1),
    (SyncRequestType.AssetFacesV1, "face", SyncEntityType.AssetFaceV1),
    (SyncRequestType.AssetFacesV2, "face", SyncEntityType.AssetFaceV2),
]
