"""Tests for albums.py endpoints."""

import asyncio
import pytest
from unittest.mock import AsyncMock, Mock
from gumnut import NotFoundError
from gumnut.types.albums import AssetsAssociationAddResponse
from uuid import uuid4

from tests.conftest import make_sdk_status_error
from routers.api.albums import (
    BULK_ASSOCIATION_CONCURRENCY_LIMIT,
    get_all_albums,
    get_album_statistics,
    get_album_info,
    create_album,
    add_assets_to_album,
    update_album,
    remove_asset_from_album,
    delete_album,
    add_assets_to_albums,
)
from routers.immich_models import (
    CreateAlbumDto,
    UpdateAlbumDto,
    BulkIdsDto,
    AlbumsAddAssetsDto,
    Error1,
)
from routers.utils.gumnut_id_conversion import (
    uuid_to_gumnut_album_id,
    uuid_to_gumnut_asset_id,
    safe_uuid_from_asset_id,
)


def _add_response(
    added: list[str] | None = None, duplicate: list[str] | None = None
) -> AssetsAssociationAddResponse:
    return AssetsAssociationAddResponse(
        added_assets=added or [],
        duplicate_assets=duplicate or [],
    )


class TestGetAllAlbums:
    """Test the get_all_albums endpoint."""

    @pytest.mark.anyio
    async def test_get_all_albums_success(
        self, multiple_gumnut_albums, mock_sync_cursor_page, mock_current_user
    ):
        """Test successful retrieval of albums."""
        # Setup - create mock client
        mock_client = Mock()
        mock_client.albums.list.return_value = mock_sync_cursor_page(
            multiple_gumnut_albums
        )

        # Execute - pass client directly via dependency injection
        result = await get_all_albums(
            asset_id=None,
            shared=None,
            client=mock_client,
            current_user=mock_current_user,
        )

        # Assert
        assert isinstance(result, list)
        assert len(result) == 3
        # Check that mocks were called
        mock_client.albums.list.assert_called_once()
        # make sure that asset_id=None results in no parameter passed to albums.list()
        mock_client.albums.list.assert_called_once_with()
        # Verify the real conversion happened by checking result structure
        assert all(hasattr(album, "id") for album in result)
        assert all(hasattr(album, "albumName") for album in result)

    @pytest.mark.anyio
    async def test_get_all_albums_includes_asset_count(
        self, multiple_gumnut_albums, mock_sync_cursor_page, mock_current_user
    ):
        """Test that get_all_albums includes asset_count from Gumnut albums."""
        # Setup - set specific asset counts on the mock albums
        multiple_gumnut_albums[0].asset_count = 5
        multiple_gumnut_albums[1].asset_count = 10
        multiple_gumnut_albums[2].asset_count = 0

        mock_client = Mock()
        mock_client.albums.list.return_value = mock_sync_cursor_page(
            multiple_gumnut_albums
        )

        # Execute
        result = await get_all_albums(
            asset_id=None,
            shared=None,
            client=mock_client,
            current_user=mock_current_user,
        )

        # Assert - verify asset counts are preserved from Gumnut albums
        assert len(result) == 3
        assert result[0].assetCount == 5
        assert result[1].assetCount == 10
        assert result[2].assetCount == 0

    @pytest.mark.anyio
    async def test_get_all_albums_with_asset_id(
        self, multiple_gumnut_albums, mock_sync_cursor_page, mock_current_user
    ):
        """Test retrieval of albums filtered by asset_id."""
        # Setup - create mock client
        mock_client = Mock()

        # Return only one album when filtering by asset
        mock_client.albums.list.return_value = mock_sync_cursor_page(
            [multiple_gumnut_albums[0]]
        )

        # Execute with asset_id
        test_asset_uuid = uuid4()
        result = await get_all_albums(
            asset_id=test_asset_uuid,
            shared=None,
            client=mock_client,
            current_user=mock_current_user,
        )

        # Assert
        assert isinstance(result, list)
        assert len(result) == 1

        # Verify the client was called with the exact converted asset_id
        expected_gumnut_id = uuid_to_gumnut_asset_id(test_asset_uuid)
        mock_client.albums.list.assert_called_once_with(asset_id=expected_gumnut_id)

    @pytest.mark.anyio
    async def test_get_all_albums_with_asset_id_no_results(
        self, mock_sync_cursor_page, mock_current_user
    ):
        """Test retrieval of albums with asset_id that has no albums."""
        # Setup - create mock client
        mock_client = Mock()

        # Return empty list when no albums contain the asset
        mock_client.albums.list.return_value = mock_sync_cursor_page([])

        # Execute with asset_id
        test_asset_uuid = uuid4()
        result = await get_all_albums(
            asset_id=test_asset_uuid,
            shared=None,
            client=mock_client,
            current_user=mock_current_user,
        )

        # Assert
        assert isinstance(result, list)
        assert len(result) == 0

        # Verify the client was called with the exact converted asset_id
        expected_gumnut_id = uuid_to_gumnut_asset_id(test_asset_uuid)
        mock_client.albums.list.assert_called_once_with(asset_id=expected_gumnut_id)

    @pytest.mark.anyio
    async def test_get_all_albums_with_album_cover_asset_id(
        self, multiple_gumnut_albums, mock_sync_cursor_page, mock_current_user
    ):
        """Test that album_cover_asset_id is converted to albumThumbnailAssetId."""
        # Setup - set album_cover_asset_id on one of the albums
        cover_asset_id0 = uuid_to_gumnut_asset_id(uuid4())
        cover_asset_id2 = uuid_to_gumnut_asset_id(uuid4())
        multiple_gumnut_albums[0].album_cover_asset_id = cover_asset_id0
        multiple_gumnut_albums[1].album_cover_asset_id = None
        multiple_gumnut_albums[2].album_cover_asset_id = cover_asset_id2

        mock_client = Mock()
        mock_client.albums.list.return_value = mock_sync_cursor_page(
            multiple_gumnut_albums
        )

        # Execute
        result = await get_all_albums(
            asset_id=None,
            shared=None,
            client=mock_client,
            current_user=mock_current_user,
        )

        # Assert - verify albumThumbnailAssetId is set correctly
        assert len(result) == 3
        # First album should have the converted asset ID
        expected_uuid0 = str(safe_uuid_from_asset_id(cover_asset_id0))
        assert result[0].albumThumbnailAssetId == expected_uuid0
        # Second album should have empty string (no cover)
        assert result[1].albumThumbnailAssetId == ""
        # Third album should have its converted asset ID
        expected_uuid2 = str(safe_uuid_from_asset_id(cover_asset_id2))
        assert result[2].albumThumbnailAssetId == expected_uuid2

    @pytest.mark.anyio
    async def test_get_all_albums_shared_returns_empty(self):
        """Test that shared=True returns empty list."""
        # Execute
        result = await get_all_albums(shared=True)

        # Assert
        assert result == []

    @pytest.mark.anyio
    async def test_get_all_albums_propagates_sdk_error(self, mock_current_user):
        """SDK errors bubble up; the global GumnutError handler maps them."""
        from gumnut import APIStatusError

        mock_client = Mock()
        mock_client.albums.list.side_effect = make_sdk_status_error(500, "boom")

        with pytest.raises(APIStatusError):
            await get_all_albums(
                asset_id=None,
                shared=None,
                client=mock_client,
                current_user=mock_current_user,
            )


class TestGetAlbumStatistics:
    """Test the get_album_statistics endpoint."""

    @pytest.mark.anyio
    async def test_get_album_statistics_success(
        self, mock_gumnut_client, multiple_gumnut_albums, mock_sync_cursor_page
    ):
        """Test successful retrieval of album statistics."""
        # Setup
        mock_gumnut_client.albums.list.return_value = mock_sync_cursor_page(
            multiple_gumnut_albums
        )

        # Execute
        result = await get_album_statistics(client=mock_gumnut_client)

        # Assert
        assert result.owned == 3
        assert result.shared == 0
        assert result.notShared == 3
        mock_gumnut_client.albums.list.assert_called_once()

    @pytest.mark.anyio
    async def test_get_album_statistics_empty(
        self, mock_gumnut_client, mock_sync_cursor_page
    ):
        """Test album statistics with no albums."""
        # Setup
        mock_gumnut_client.albums.list.return_value = mock_sync_cursor_page([])

        # Execute
        result = await get_album_statistics(client=mock_gumnut_client)

        # Assert
        assert result.owned == 0
        assert result.shared == 0
        assert result.notShared == 0

    @pytest.mark.anyio
    async def test_get_album_statistics_propagates_sdk_error(self, mock_gumnut_client):
        """SDK errors bubble up; the global GumnutError handler maps them."""
        from gumnut import APIStatusError

        mock_gumnut_client.albums.list.side_effect = make_sdk_status_error(500, "boom")

        with pytest.raises(APIStatusError):
            await get_album_statistics(client=mock_gumnut_client)


class TestGetAlbumInfo:
    """Test the get_album_info endpoint."""

    @pytest.mark.anyio
    async def test_get_album_info_success(
        self,
        sample_gumnut_album,
        multiple_gumnut_assets,
        mock_sync_cursor_page,
        sample_uuid,
        mock_current_user,
    ):
        """Test successful retrieval of album info."""
        # Setup - create mock client
        mock_client = Mock()
        mock_client.albums.retrieve = AsyncMock(return_value=sample_gumnut_album)
        mock_client.assets.list.return_value = mock_sync_cursor_page(
            multiple_gumnut_assets
        )

        # Execute
        result = await get_album_info(
            sample_uuid,
            withoutAssets=False,
            client=mock_client,
            current_user=mock_current_user,
        )

        # Assert
        # Now result is a real AlbumResponseDto, so use attribute access
        assert hasattr(result, "id")
        assert hasattr(result, "albumName")
        assert result.albumName == "Test Album"  # From sample_gumnut_album.name
        mock_client.albums.retrieve.assert_called_once()
        mock_client.assets.list.assert_called_once_with(
            album_id=uuid_to_gumnut_album_id(sample_uuid)
        )

    @pytest.mark.anyio
    async def test_get_album_info_uses_gumnut_asset_count(
        self,
        sample_gumnut_album,
        mock_sync_cursor_page,
        sample_uuid,
        mock_current_user,
    ):
        """Test that get_album_info uses asset_count from Gumnut album object."""
        # Setup - mock album with specific asset_count
        sample_gumnut_album.asset_count = 42  # Set specific count

        mock_client = Mock()
        mock_client.albums.retrieve = AsyncMock(return_value=sample_gumnut_album)
        # Return empty assets list
        mock_client.assets.list.return_value = mock_sync_cursor_page([])

        # Execute
        result = await get_album_info(
            sample_uuid,
            withoutAssets=False,
            client=mock_client,
            current_user=mock_current_user,
        )

        # Assert - should use album.asset_count (42) from the Gumnut album object
        assert result.assetCount == 42

    @pytest.mark.anyio
    async def test_get_album_info_with_album_cover_asset_id(
        self, sample_gumnut_album, mock_sync_cursor_page, sample_uuid, mock_current_user
    ):
        """Test that album_cover_asset_id is converted to albumThumbnailAssetId in get_album_info."""
        # Setup - set album_cover_asset_id on the album
        cover_asset_id = uuid_to_gumnut_asset_id(uuid4())
        sample_gumnut_album.album_cover_asset_id = cover_asset_id

        mock_client = Mock()
        mock_client.albums.retrieve = AsyncMock(return_value=sample_gumnut_album)
        mock_client.assets.list.return_value = mock_sync_cursor_page([])

        # Execute
        result = await get_album_info(
            sample_uuid,
            withoutAssets=False,
            client=mock_client,
            current_user=mock_current_user,
        )

        # Assert - verify albumThumbnailAssetId is set correctly
        expected_uuid = str(safe_uuid_from_asset_id(cover_asset_id))
        assert result.albumThumbnailAssetId == expected_uuid

    @pytest.mark.anyio
    async def test_get_album_info_without_assets(
        self, sample_gumnut_album, sample_uuid, mock_current_user
    ):
        """Test retrieval of album info without assets."""
        # Setup - create mock client
        mock_client = Mock()
        mock_client.albums.retrieve = AsyncMock(return_value=sample_gumnut_album)

        # Execute
        result = await get_album_info(
            sample_uuid,
            withoutAssets=True,
            client=mock_client,
            current_user=mock_current_user,
        )

        # Assert
        # Now result is a real AlbumResponseDto, so use attribute access
        assert hasattr(result, "id")
        assert result.albumName == "Test Album"  # From sample_gumnut_album.name
        mock_client.albums.retrieve.assert_called_once()
        # withoutAssets=True skips the assets.list call entirely
        mock_client.assets.list.assert_not_called()

    @pytest.mark.anyio
    async def test_get_album_info_not_found(self, sample_uuid, mock_current_user):
        """A NotFoundError from the SDK bubbles to the global handler (mapped to 404)."""
        mock_client = Mock()
        mock_client.albums.retrieve = AsyncMock(
            side_effect=make_sdk_status_error(404, "Not found", cls=NotFoundError)
        )

        with pytest.raises(NotFoundError):
            await get_album_info(
                sample_uuid, client=mock_client, current_user=mock_current_user
            )


class TestCreateAlbum:
    """Test the create_album endpoint."""

    @pytest.mark.anyio
    async def test_create_album_success(self, sample_gumnut_album, mock_current_user):
        """Test successful album creation."""
        # Setup - create mock client
        mock_client = Mock()
        # Update the sample to have the name we want to test
        sample_gumnut_album.name = "New Album"
        sample_gumnut_album.description = "New Description"
        mock_client.albums.create = AsyncMock(return_value=sample_gumnut_album)

        request = CreateAlbumDto(albumName="New Album", description="New Description")

        # Execute
        result = await create_album(
            request, client=mock_client, current_user=mock_current_user
        )

        # Assert
        # Now result is a real AlbumResponseDto, so use attribute access
        assert hasattr(result, "albumName")
        assert result.albumName == "New Album"
        mock_client.albums.create.assert_called_once_with(
            name="New Album", description="New Description"
        )

    @pytest.mark.anyio
    async def test_create_album_propagates_sdk_error(self, mock_current_user):
        """SDK errors bubble up; the global GumnutError handler maps them."""
        from gumnut import APIStatusError

        mock_client = Mock()
        mock_client.albums.create = AsyncMock(
            side_effect=make_sdk_status_error(500, "boom")
        )

        request = CreateAlbumDto(albumName="Test Album")

        with pytest.raises(APIStatusError):
            await create_album(
                request, client=mock_client, current_user=mock_current_user
            )


class TestAddAssetsToAlbum:
    """Test the add_assets_to_album endpoint."""

    @pytest.mark.anyio
    async def test_add_assets_success(self, sample_uuid):
        """A single bulk call adds every asset and returns success per asset."""
        asset_id1 = uuid4()
        asset_id2 = uuid4()
        gumnut_id1 = uuid_to_gumnut_asset_id(asset_id1)
        gumnut_id2 = uuid_to_gumnut_asset_id(asset_id2)

        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            return_value=_add_response(added=[gumnut_id1, gumnut_id2])
        )

        request = BulkIdsDto(ids=[asset_id1, asset_id2])
        result = await add_assets_to_album(sample_uuid, request, client=mock_client)

        assert [item.id for item in result] == [str(asset_id1), str(asset_id2)]
        assert all(item.success is True for item in result)
        # The endpoint should make a single bulk call, not one per asset.
        mock_client.albums.assets_associations.add.assert_called_once_with(
            uuid_to_gumnut_album_id(sample_uuid),
            asset_ids=[gumnut_id1, gumnut_id2],
        )

    @pytest.mark.anyio
    async def test_add_assets_duplicates_from_response(self, sample_uuid):
        """Duplicates are read from the response body, not inferred from a 409."""
        new_asset = uuid4()
        dup_asset = uuid4()
        new_gid = uuid_to_gumnut_asset_id(new_asset)
        dup_gid = uuid_to_gumnut_asset_id(dup_asset)

        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            return_value=_add_response(added=[new_gid], duplicate=[dup_gid])
        )

        request = BulkIdsDto(ids=[new_asset, dup_asset])
        result = await add_assets_to_album(sample_uuid, request, client=mock_client)

        assert len(result) == 2
        assert result[0].id == str(new_asset)
        assert result[0].success is True
        assert result[1].id == str(dup_asset)
        assert result[1].success is False
        assert result[1].error == Error1.duplicate

    @pytest.mark.anyio
    async def test_add_assets_not_found_falls_back_to_per_item(self, sample_uuid):
        """A 404 on the bulk call falls back to per-item calls to identify the bad IDs."""
        valid_asset = uuid4()
        missing_asset = uuid4()
        valid_gid = uuid_to_gumnut_asset_id(valid_asset)
        missing_gid = uuid_to_gumnut_asset_id(missing_asset)

        bulk_404 = make_sdk_status_error(404, "Not found", cls=NotFoundError)

        async def add_side_effect(album_id, *, asset_ids, **kwargs):
            if asset_ids == [valid_gid, missing_gid]:
                raise bulk_404
            if asset_ids == [valid_gid]:
                return _add_response(added=[valid_gid])
            if asset_ids == [missing_gid]:
                raise make_sdk_status_error(404, "Not found", cls=NotFoundError)
            raise AssertionError(f"unexpected asset_ids: {asset_ids}")

        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=add_side_effect
        )

        request = BulkIdsDto(ids=[valid_asset, missing_asset])
        result = await add_assets_to_album(sample_uuid, request, client=mock_client)

        assert len(result) == 2
        assert result[0].id == str(valid_asset)
        assert result[0].success is True
        assert result[1].id == str(missing_asset)
        assert result[1].success is False
        assert result[1].error == Error1.not_found
        # 1 bulk attempt + 2 per-item fallback calls.
        assert mock_client.albums.assets_associations.add.call_count == 3

    @pytest.mark.anyio
    async def test_add_assets_per_item_fallback_detects_duplicate(self, sample_uuid):
        """Fallback path also reads duplicate_assets from the per-item response."""
        valid_asset = uuid4()
        dup_asset = uuid4()
        missing_asset = uuid4()
        valid_gid = uuid_to_gumnut_asset_id(valid_asset)
        dup_gid = uuid_to_gumnut_asset_id(dup_asset)
        missing_gid = uuid_to_gumnut_asset_id(missing_asset)

        async def add_side_effect(album_id, *, asset_ids, **kwargs):
            if asset_ids == [valid_gid, dup_gid, missing_gid]:
                raise make_sdk_status_error(404, "Not found", cls=NotFoundError)
            if asset_ids == [valid_gid]:
                return _add_response(added=[valid_gid])
            if asset_ids == [dup_gid]:
                return _add_response(duplicate=[dup_gid])
            if asset_ids == [missing_gid]:
                raise make_sdk_status_error(404, "Not found", cls=NotFoundError)
            raise AssertionError(f"unexpected asset_ids: {asset_ids}")

        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=add_side_effect
        )

        request = BulkIdsDto(ids=[valid_asset, dup_asset, missing_asset])
        result = await add_assets_to_album(sample_uuid, request, client=mock_client)

        assert [item.id for item in result] == [
            str(valid_asset),
            str(dup_asset),
            str(missing_asset),
        ]
        assert result[0].success is True
        assert result[1].success is False
        assert result[1].error == Error1.duplicate
        assert result[2].success is False
        assert result[2].error == Error1.not_found

    @pytest.mark.anyio
    async def test_add_assets_other_api_status_error_marks_all(self, sample_uuid):
        """A non-404 4xx/5xx on the bulk call marks every requested asset with the same error."""
        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=make_sdk_status_error(500, "boom")
        )

        asset_id1 = uuid4()
        asset_id2 = uuid4()
        request = BulkIdsDto(ids=[asset_id1, asset_id2])

        result = await add_assets_to_album(sample_uuid, request, client=mock_client)

        assert [item.id for item in result] == [str(asset_id1), str(asset_id2)]
        assert all(item.success is False for item in result)
        assert all(item.error == Error1.unknown for item in result)
        # Should not retry per-item for non-404 errors.
        assert mock_client.albums.assets_associations.add.call_count == 1


class TestUpdateAlbum:
    """Test the update_album endpoint."""

    @pytest.mark.anyio
    async def test_update_album_success(
        self, sample_gumnut_album, sample_uuid, mock_current_user
    ):
        """Test successful album update."""
        mock_client = Mock()
        sample_gumnut_album.name = "Updated Album"
        sample_gumnut_album.description = "Updated Description"
        mock_client.albums.update = AsyncMock(return_value=sample_gumnut_album)

        request = UpdateAlbumDto(
            albumName="Updated Album", description="Updated Description"
        )

        result = await update_album(
            sample_uuid, request, client=mock_client, current_user=mock_current_user
        )

        assert result.albumName == "Updated Album"
        mock_client.albums.update.assert_called_once()

    @pytest.mark.anyio
    async def test_update_album_with_album_cover_asset_id(
        self, sample_gumnut_album, sample_uuid, mock_current_user
    ):
        """Test that album_cover_asset_id is converted to albumThumbnailAssetId in update_album."""
        cover_asset_id = uuid_to_gumnut_asset_id(uuid4())
        sample_gumnut_album.album_cover_asset_id = cover_asset_id

        mock_client = Mock()
        sample_gumnut_album.name = "Updated Album"
        sample_gumnut_album.description = "Updated Description"
        mock_client.albums.update = AsyncMock(return_value=sample_gumnut_album)

        request = UpdateAlbumDto(
            albumName="Updated Album", description="Updated Description"
        )

        # Execute
        result = await update_album(
            sample_uuid, request, client=mock_client, current_user=mock_current_user
        )

        # Assert - verify albumThumbnailAssetId is set correctly
        expected_uuid = str(safe_uuid_from_asset_id(cover_asset_id))
        assert result.albumThumbnailAssetId == expected_uuid

    @pytest.mark.anyio
    async def test_update_album_not_found_propagates(
        self, mock_gumnut_client, sample_uuid, mock_current_user
    ):
        """A NotFoundError from the SDK bubbles up to the global handler."""
        request = UpdateAlbumDto(albumName="Updated Album")
        mock_gumnut_client.albums.update = AsyncMock(
            side_effect=make_sdk_status_error(404, "Not found", cls=NotFoundError)
        )

        with pytest.raises(NotFoundError):
            await update_album(
                sample_uuid,
                request,
                client=mock_gumnut_client,
                current_user=mock_current_user,
            )


class TestRemoveAssetFromAlbum:
    """Test the remove_asset_from_album endpoint."""

    @pytest.mark.anyio
    async def test_remove_assets_success(self, sample_uuid):
        """A single bulk call removes every asset and returns success per asset."""
        asset_id1 = uuid4()
        asset_id2 = uuid4()
        gumnut_id1 = uuid_to_gumnut_asset_id(asset_id1)
        gumnut_id2 = uuid_to_gumnut_asset_id(asset_id2)

        mock_client = Mock()
        mock_client.albums.assets_associations.remove = AsyncMock(return_value=None)

        request = BulkIdsDto(ids=[asset_id1, asset_id2])
        result = await remove_asset_from_album(sample_uuid, request, client=mock_client)

        assert [item.id for item in result] == [str(asset_id1), str(asset_id2)]
        assert all(item.success is True for item in result)
        # The endpoint should make a single bulk call, not one per asset.
        mock_client.albums.assets_associations.remove.assert_called_once_with(
            uuid_to_gumnut_album_id(sample_uuid),
            asset_ids=[gumnut_id1, gumnut_id2],
        )

    @pytest.mark.anyio
    async def test_remove_assets_album_not_found_marks_all(self, sample_uuid):
        """A 404 on the bulk call (album missing) marks every asset as not_found."""
        mock_client = Mock()
        mock_client.albums.assets_associations.remove = AsyncMock(
            side_effect=make_sdk_status_error(404, "Not found", cls=NotFoundError)
        )

        asset_id1 = uuid4()
        asset_id2 = uuid4()
        request = BulkIdsDto(ids=[asset_id1, asset_id2])

        result = await remove_asset_from_album(sample_uuid, request, client=mock_client)

        assert [item.id for item in result] == [str(asset_id1), str(asset_id2)]
        assert all(item.success is False for item in result)
        assert all(item.error == Error1.not_found for item in result)

    @pytest.mark.anyio
    async def test_remove_assets_other_error_marks_all(self, sample_uuid):
        """A non-404 4xx/5xx marks every asset with the classified error."""
        mock_client = Mock()
        mock_client.albums.assets_associations.remove = AsyncMock(
            side_effect=make_sdk_status_error(500, "boom")
        )

        asset_id1 = uuid4()
        asset_id2 = uuid4()
        request = BulkIdsDto(ids=[asset_id1, asset_id2])

        result = await remove_asset_from_album(sample_uuid, request, client=mock_client)

        assert all(item.success is False for item in result)
        assert all(item.error == Error1.unknown for item in result)
        assert mock_client.albums.assets_associations.remove.call_count == 1


class TestDeleteAlbum:
    """Test the delete_album endpoint."""

    @pytest.mark.anyio
    async def test_delete_album_success(self, mock_gumnut_client, sample_uuid):
        """Test successful album deletion."""
        mock_gumnut_client.albums.delete = AsyncMock(return_value=None)

        result = await delete_album(sample_uuid, client=mock_gumnut_client)

        assert result.status_code == 204
        mock_gumnut_client.albums.delete.assert_called_once()

    @pytest.mark.anyio
    async def test_delete_album_not_found_propagates(
        self, mock_gumnut_client, sample_uuid
    ):
        """A NotFoundError from the SDK bubbles up to the global handler."""
        mock_gumnut_client.albums.delete = AsyncMock(
            side_effect=make_sdk_status_error(404, "Not found", cls=NotFoundError)
        )

        with pytest.raises(NotFoundError):
            await delete_album(sample_uuid, client=mock_gumnut_client)


class TestAddAssetsToAlbums:
    """Test the add_assets_to_albums endpoint."""

    @pytest.mark.anyio
    async def test_add_assets_to_albums_success(self, sample_uuid):
        """Test successful addition of assets to multiple albums."""
        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(return_value=None)

        album_ids = [uuid4(), uuid4()]
        asset_ids = [uuid4()]
        request = AlbumsAddAssetsDto(albumIds=album_ids, assetIds=asset_ids)

        result = await add_assets_to_albums(request, client=mock_client)

        assert result.success is True
        assert mock_client.albums.assets_associations.add.call_count == 2

    @pytest.mark.anyio
    async def test_add_assets_to_albums_conflict_records_duplicate(self):
        """A ConflictError on an album add records first_error = duplicate."""
        from gumnut import ConflictError

        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=make_sdk_status_error(409, "duplicate", cls=ConflictError)
        )

        request = AlbumsAddAssetsDto(albumIds=[uuid4()], assetIds=[uuid4()])
        result = await add_assets_to_albums(request, client=mock_client)

        assert result.success is False
        from routers.immich_models import BulkIdErrorReason

        assert result.error == BulkIdErrorReason.duplicate

    @pytest.mark.anyio
    async def test_add_assets_to_albums_not_found_records_not_found(self):
        """A NotFoundError on an album add records first_error = not_found."""
        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=make_sdk_status_error(404, "Not found", cls=NotFoundError)
        )

        request = AlbumsAddAssetsDto(albumIds=[uuid4()], assetIds=[uuid4()])
        result = await add_assets_to_albums(request, client=mock_client)

        assert result.success is False
        from routers.immich_models import BulkIdErrorReason

        assert result.error == BulkIdErrorReason.not_found

    @pytest.mark.anyio
    async def test_add_assets_to_albums_first_error_is_sticky(self):
        """`first_error` records the first failure across albums; later
        failures with a different classification do not overwrite it."""
        from gumnut import ConflictError

        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=[
                make_sdk_status_error(409, "duplicate", cls=ConflictError),
                make_sdk_status_error(404, "Not found", cls=NotFoundError),
            ]
        )

        request = AlbumsAddAssetsDto(albumIds=[uuid4(), uuid4()], assetIds=[uuid4()])
        result = await add_assets_to_albums(request, client=mock_client)

        assert result.success is False
        from routers.immich_models import BulkIdErrorReason

        assert result.error == BulkIdErrorReason.duplicate

    @pytest.mark.anyio
    async def test_add_assets_to_albums_partial_failure(self):
        """One success + one failure returns success=False with the failure's error."""
        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=[
                None,
                make_sdk_status_error(404, "Not found", cls=NotFoundError),
            ]
        )

        request = AlbumsAddAssetsDto(albumIds=[uuid4(), uuid4()], assetIds=[uuid4()])
        result = await add_assets_to_albums(request, client=mock_client)

        assert result.success is False
        from routers.immich_models import BulkIdErrorReason

        assert result.error == BulkIdErrorReason.not_found

    @pytest.mark.anyio
    async def test_add_assets_to_albums_uses_bounded_concurrency(self):
        """Album fan-out runs in parallel but never exceeds the concurrency limit."""
        mock_client = Mock()

        active_calls = 0
        max_active_calls = 0
        call_counter_lock = asyncio.Lock()

        async def add_side_effect(*args, **kwargs):
            nonlocal active_calls, max_active_calls
            async with call_counter_lock:
                active_calls += 1
                max_active_calls = max(max_active_calls, active_calls)

            await asyncio.sleep(0.01)

            async with call_counter_lock:
                active_calls -= 1

        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=add_side_effect
        )

        album_ids = [uuid4() for _ in range(BULK_ASSOCIATION_CONCURRENCY_LIMIT + 5)]
        request = AlbumsAddAssetsDto(albumIds=album_ids, assetIds=[uuid4()])

        result = await add_assets_to_albums(request, client=mock_client)

        assert result.success is True
        assert max_active_calls > 1
        assert max_active_calls <= BULK_ASSOCIATION_CONCURRENCY_LIMIT

    @pytest.mark.anyio
    async def test_add_assets_to_albums_first_error_sticky_by_input_order(self):
        """The sticky first error follows request order, not completion order."""
        from gumnut import ConflictError
        from routers.immich_models import BulkIdErrorReason

        mock_client = Mock()

        first_album_id = uuid4()
        second_album_id = uuid4()
        first_gumnut_album_id = uuid_to_gumnut_album_id(first_album_id)

        async def add_side_effect(album_id, *args, **kwargs):
            if album_id == first_gumnut_album_id:
                await asyncio.sleep(0.03)
                raise make_sdk_status_error(409, "duplicate", cls=ConflictError)

            await asyncio.sleep(0.0)
            raise make_sdk_status_error(404, "Not found", cls=NotFoundError)

        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=add_side_effect
        )

        request = AlbumsAddAssetsDto(
            albumIds=[first_album_id, second_album_id],
            assetIds=[uuid4()],
        )
        result = await add_assets_to_albums(request, client=mock_client)

        assert result.success is False
        assert result.error == BulkIdErrorReason.duplicate
