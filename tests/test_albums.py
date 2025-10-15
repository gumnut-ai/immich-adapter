"""Tests for albums.py endpoints."""

import pytest
from unittest.mock import Mock, patch
from fastapi import HTTPException
from uuid import uuid4

from routers.api.albums import (
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
    Error2,
)
from routers.utils.gumnut_id_conversion import uuid_to_gumnut_asset_id


class TestGetAllAlbums:
    """Test the get_all_albums endpoint."""

    @pytest.mark.anyio
    async def test_get_all_albums_success(
        self, multiple_gumnut_albums, mock_sync_cursor_page
    ):
        """Test successful retrieval of albums."""
        # Setup - mock only the Gumnut client, let conversion functions run naturally
        with patch("routers.api.albums.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.albums.list.return_value = mock_sync_cursor_page(
                multiple_gumnut_albums
            )

            # Execute
            result = await get_all_albums(asset_id=None, shared=None)

            # Assert
            assert isinstance(result, list)
            assert len(result) == 3
            # Check that mocks were called
            mock_get_client.assert_called_once()
            mock_client.albums.list.assert_called_once()
            # make sure that asset_id=None results in no parameter passed to albums.list()
            mock_client.albums.list.assert_called_once_with()
            # Verify the real conversion happened by checking result structure
            assert all(hasattr(album, "id") for album in result)
            assert all(hasattr(album, "albumName") for album in result)

    @pytest.mark.anyio
    async def test_get_all_albums_includes_asset_count(
        self, multiple_gumnut_albums, mock_sync_cursor_page
    ):
        """Test that get_all_albums includes asset_count from Gumnut albums."""
        # Setup - set specific asset counts on the mock albums
        multiple_gumnut_albums[0].asset_count = 5
        multiple_gumnut_albums[1].asset_count = 10
        multiple_gumnut_albums[2].asset_count = 0

        with patch("routers.api.albums.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.albums.list.return_value = mock_sync_cursor_page(
                multiple_gumnut_albums
            )

            # Execute
            result = await get_all_albums(asset_id=None, shared=None)

            # Assert - verify asset counts are preserved from Gumnut albums
            assert len(result) == 3
            assert result[0].assetCount == 5
            assert result[1].assetCount == 10
            assert result[2].assetCount == 0

    @pytest.mark.anyio
    async def test_get_all_albums_with_asset_id(
        self, multiple_gumnut_albums, mock_sync_cursor_page
    ):
        """Test retrieval of albums filtered by asset_id."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.albums.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Return only one album when filtering by asset
            mock_client.albums.list.return_value = mock_sync_cursor_page(
                [multiple_gumnut_albums[0]]
            )

            # Execute with asset_id
            test_asset_uuid = uuid4()
            result = await get_all_albums(asset_id=test_asset_uuid, shared=None)

            # Assert
            assert isinstance(result, list)
            assert len(result) == 1

            # Verify the client was called with the exact converted asset_id
            expected_gumnut_id = uuid_to_gumnut_asset_id(test_asset_uuid)
            mock_client.albums.list.assert_called_once_with(asset_id=expected_gumnut_id)

    @pytest.mark.anyio
    async def test_get_all_albums_with_asset_id_no_results(self, mock_sync_cursor_page):
        """Test retrieval of albums with asset_id that has no albums."""
        # Setup - mock only the Gumnut client
        with patch("routers.api.albums.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Return empty list when no albums contain the asset
            mock_client.albums.list.return_value = mock_sync_cursor_page([])

            # Execute with asset_id
            test_asset_uuid = uuid4()
            result = await get_all_albums(asset_id=test_asset_uuid, shared=None)

            # Assert
            assert isinstance(result, list)
            assert len(result) == 0

            # Verify the client was called with the exact converted asset_id
            expected_gumnut_id = uuid_to_gumnut_asset_id(test_asset_uuid)
            mock_client.albums.list.assert_called_once_with(asset_id=expected_gumnut_id)

    @pytest.mark.anyio
    async def test_get_all_albums_shared_returns_empty(self):
        """Test that shared=True returns empty list."""
        # Execute
        result = await get_all_albums(shared=True)

        # Assert
        assert result == []

    @pytest.mark.anyio
    async def test_get_all_albums_gumnut_error(self):
        """Test handling of Gumnut API errors."""
        # Setup - mock directly in the test
        with patch("routers.api.albums.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.albums.list.side_effect = Exception("API Error")

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await get_all_albums(asset_id=None, shared=None)

            assert exc_info.value.status_code == 500
            assert "Failed to fetch albums" in str(exc_info.value.detail)


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
        result = await get_album_statistics()

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
        result = await get_album_statistics()

        # Assert
        assert result.owned == 0
        assert result.shared == 0
        assert result.notShared == 0

    @pytest.mark.anyio
    async def test_get_album_statistics_gumnut_error(self, mock_gumnut_client):
        """Test handling of Gumnut API errors."""
        # Setup
        mock_gumnut_client.albums.list.side_effect = Exception("API Error")

        # Execute & Assert
        with pytest.raises(HTTPException) as exc_info:
            await get_album_statistics()

        assert exc_info.value.status_code == 500


class TestGetAlbumInfo:
    """Test the get_album_info endpoint."""

    @pytest.mark.anyio
    async def test_get_album_info_success(
        self,
        sample_gumnut_album,
        multiple_gumnut_assets,
        mock_sync_cursor_page,
        sample_uuid,
    ):
        """Test successful retrieval of album info."""
        # Setup - mock only the Gumnut client, let conversion functions run naturally
        with patch("routers.api.albums.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.albums.retrieve.return_value = sample_gumnut_album
            mock_client.albums.assets.list.return_value = mock_sync_cursor_page(
                multiple_gumnut_assets
            )

            # Execute
            result = await get_album_info(sample_uuid)

            # Assert
            # Now result is a real AlbumResponseDto, so use attribute access
            assert hasattr(result, "id")
            assert hasattr(result, "albumName")
            assert result.albumName == "Test Album"  # From sample_gumnut_album.name
            mock_client.albums.retrieve.assert_called_once()
            mock_client.albums.assets.list.assert_called_once()

    @pytest.mark.anyio
    async def test_get_album_info_uses_gumnut_asset_count(
        self,
        sample_gumnut_album,
        sample_uuid,
    ):
        """Test that get_album_info uses asset_count from Gumnut album object."""
        # Setup - mock album with specific asset_count
        sample_gumnut_album.asset_count = 42  # Set specific count

        with patch("routers.api.albums.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.albums.retrieve.return_value = sample_gumnut_album
            # Return empty assets list
            mock_client.albums.assets.list.return_value = []

            # Execute
            result = await get_album_info(sample_uuid)

            # Assert - should use album.asset_count (42) from the Gumnut album object
            assert result.assetCount == 42

    @pytest.mark.anyio
    async def test_get_album_info_without_assets(
        self, sample_gumnut_album, sample_uuid
    ):
        """Test retrieval of album info without assets."""
        # Setup - mock only the Gumnut client, let conversion functions run naturally
        with patch("routers.api.albums.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.albums.retrieve.return_value = sample_gumnut_album
            # Mock assets list to return an empty iterable to avoid the "Mock object is not iterable" error
            mock_client.albums.assets.list.return_value = []

            # Execute
            result = await get_album_info(sample_uuid, withoutAssets=True)

            # Assert
            # Now result is a real AlbumResponseDto, so use attribute access
            assert hasattr(result, "id")
            assert result.albumName == "Test Album"  # From sample_gumnut_album.name
            mock_client.albums.retrieve.assert_called_once()
            # Note: The current implementation always fetches assets but only processes them when withoutAssets is falsy
            mock_client.albums.assets.list.assert_called_once()

    @pytest.mark.anyio
    async def test_get_album_info_not_found(self, sample_uuid):
        """Test handling of album not found."""
        # Setup - mock directly in the test
        with patch("routers.api.albums.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.albums.retrieve.side_effect = Exception("404 Not found")

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await get_album_info(sample_uuid)

            assert exc_info.value.status_code == 404


class TestCreateAlbum:
    """Test the create_album endpoint."""

    @pytest.mark.anyio
    async def test_create_album_success(self, sample_gumnut_album):
        """Test successful album creation."""
        # Setup - mock only the Gumnut client, let conversion functions run naturally
        with patch("routers.api.albums.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            # Update the sample to have the name we want to test
            sample_gumnut_album.name = "New Album"
            sample_gumnut_album.description = "New Description"
            mock_client.albums.create.return_value = sample_gumnut_album

            request = CreateAlbumDto(
                albumName="New Album", description="New Description"
            )

            # Execute
            result = await create_album(request)

            # Assert
            # Now result is a real AlbumResponseDto, so use attribute access
            assert hasattr(result, "albumName")
            assert result.albumName == "New Album"
            mock_client.albums.create.assert_called_once_with(
                name="New Album", description="New Description"
            )

    @pytest.mark.anyio
    async def test_create_album_gumnut_error(self):
        """Test handling of Gumnut API errors during creation."""
        # Setup - mock directly in the test
        with patch("routers.api.albums.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.albums.create.side_effect = Exception("API Error")

            request = CreateAlbumDto(albumName="Test Album")

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await create_album(request)

            assert exc_info.value.status_code == 500


class TestAddAssetsToAlbum:
    """Test the add_assets_to_album endpoint."""

    @pytest.mark.anyio
    async def test_add_assets_success(self, sample_gumnut_album, sample_uuid):
        """Test successful addition of assets to album."""
        # Setup - mock only the Gumnut client, let conversion functions run naturally
        with patch("routers.api.albums.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.albums.retrieve.return_value = sample_gumnut_album
            mock_client.albums.assets.add.return_value = None

            asset_id1 = uuid4()
            asset_id2 = uuid4()

            asset_ids = [asset_id1, asset_id2]
            request = BulkIdsDto(ids=asset_ids)

            # Execute
            result = await add_assets_to_album(sample_uuid, request)

            # Assert
            assert len(result) == 2
            assert all(item.success is True for item in result)
            assert result[0].id == str(asset_id1)
            assert result[1].id == str(asset_id2)
            mock_client.albums.retrieve.assert_called_once()
            assert mock_client.albums.assets.add.call_count == 2

    @pytest.mark.anyio
    async def test_add_assets_album_not_found(self, mock_gumnut_client, sample_uuid):
        """Test adding assets to non-existent album."""
        # Setup
        request = BulkIdsDto(ids=[uuid4()])
        mock_gumnut_client.albums.retrieve.side_effect = Exception("404 Not found")

        # Execute & Assert
        with pytest.raises(HTTPException) as exc_info:
            await add_assets_to_album(sample_uuid, request)

        assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_add_assets_mixed_results(self, sample_gumnut_album, sample_uuid):
        """Test adding assets with some failures."""
        # Setup - mock only the Gumnut client, let conversion functions run naturally
        with patch("routers.api.albums.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.albums.retrieve.return_value = sample_gumnut_album

            # First call succeeds, second fails
            mock_client.albums.assets.add.side_effect = [
                None,  # Success
                Exception("Asset not found"),  # Failure
            ]

            asset_id1 = uuid4()
            asset_id2 = uuid4()

            asset_ids = [asset_id1, asset_id2]
            request = BulkIdsDto(ids=asset_ids)

            # Execute
            result = await add_assets_to_album(sample_uuid, request)

            # Assert
            assert len(result) == 2
            assert result[0].success is True
            assert result[0].id == str(asset_id1)
            assert result[1].success is False
            assert result[1].id == str(asset_id2)
            # Now error is an Error2 enum, check for the not_found value
            assert result[1].error == Error2.not_found


class TestUpdateAlbum:
    """Test the update_album endpoint."""

    @pytest.mark.anyio
    async def test_update_album_success(self, sample_gumnut_album, sample_uuid):
        """Test successful album update."""
        # Setup - mock only the Gumnut client, let conversion functions run naturally
        with patch("routers.api.albums.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.albums.retrieve.return_value = sample_gumnut_album
            # Update the sample to have the name we want to test
            sample_gumnut_album.name = "Updated Album"
            sample_gumnut_album.description = "Updated Description"
            mock_client.albums.update.return_value = sample_gumnut_album

            request = UpdateAlbumDto(
                albumName="Updated Album", description="Updated Description"
            )

            # Execute
            result = await update_album(sample_uuid, request)

            # Assert
            # Now result is a real AlbumResponseDto, so use attribute access
            assert hasattr(result, "albumName")
            assert result.albumName == "Updated Album"
            mock_client.albums.retrieve.assert_called_once()
            mock_client.albums.update.assert_called_once()

    @pytest.mark.anyio
    async def test_update_album_not_found(self, mock_gumnut_client, sample_uuid):
        """Test updating non-existent album."""
        # Setup
        request = UpdateAlbumDto(albumName="Updated Album")
        mock_gumnut_client.albums.retrieve.side_effect = Exception("404 Not found")

        # Execute & Assert
        with pytest.raises(HTTPException) as exc_info:
            await update_album(sample_uuid, request)

        assert exc_info.value.status_code == 404


class TestRemoveAssetFromAlbum:
    """Test the remove_asset_from_album endpoint."""

    @pytest.mark.anyio
    async def test_remove_assets_success(self, sample_gumnut_album, sample_uuid):
        """Test successful removal of assets from album."""
        # Setup - mock only the Gumnut client, let conversion functions run naturally
        with patch("routers.api.albums.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.albums.retrieve.return_value = sample_gumnut_album
            mock_client.albums.assets.remove.return_value = None

            asset_id1 = uuid4()
            asset_id2 = uuid4()

            asset_ids = [asset_id1, asset_id2]
            request = BulkIdsDto(ids=asset_ids)

            # Execute
            result = await remove_asset_from_album(sample_uuid, request)

            # Assert
            assert len(result) == 2
            assert all(item.success is True for item in result)
            assert result[0].id == str(asset_id1)
            assert result[1].id == str(asset_id2)
            mock_client.albums.retrieve.assert_called_once()
            assert mock_client.albums.assets.remove.call_count == 2


class TestDeleteAlbum:
    """Test the delete_album endpoint."""

    @pytest.mark.anyio
    async def test_delete_album_success(
        self, mock_gumnut_client, sample_gumnut_album, sample_uuid
    ):
        """Test successful album deletion."""
        # Setup
        mock_gumnut_client.albums.retrieve.return_value = sample_gumnut_album
        mock_gumnut_client.albums.delete.return_value = None

        # Execute
        result = await delete_album(sample_uuid)

        # Assert
        assert result.status_code == 204
        mock_gumnut_client.albums.retrieve.assert_called_once()
        mock_gumnut_client.albums.delete.assert_called_once()

    @pytest.mark.anyio
    async def test_delete_album_not_found(self, mock_gumnut_client, sample_uuid):
        """Test deleting non-existent album."""
        # Setup
        mock_gumnut_client.albums.retrieve.side_effect = Exception("404 Not found")

        # Execute & Assert
        with pytest.raises(HTTPException) as exc_info:
            await delete_album(sample_uuid)

        assert exc_info.value.status_code == 404


class TestAddAssetsToAlbums:
    """Test the add_assets_to_albums endpoint."""

    @pytest.mark.anyio
    async def test_add_assets_to_albums_success(self, sample_uuid):
        """Test successful addition of assets to multiple albums."""
        # Setup - mock directly in the test
        with patch("routers.api.albums.get_gumnut_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.albums.assets.add.return_value = None

            album_ids = [uuid4(), uuid4()]
            asset_ids = [uuid4()]
            request = AlbumsAddAssetsDto(albumIds=album_ids, assetIds=asset_ids)

            # Execute
            result = await add_assets_to_albums(request)

            # Assert
            # AlbumsAddAssetsResponseDto has success and error attributes, not a results list
            assert result.success is True
            assert mock_client.albums.assets.add.call_count == 2
