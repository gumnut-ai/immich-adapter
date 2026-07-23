"""Tests for albums.py endpoints."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock, Mock
from gumnut import NotFoundError
from gumnut.types.albums import AssetsAssociationAddResponse
from uuid import uuid4

from tests.conftest import make_sdk_connection_error, make_sdk_status_error
from routers.api.constants import GUMNUT_API_MAX_BULK_IDS, GUMNUT_API_MAX_PAGE_SIZE
from routers.api.albums import (
    get_all_albums,
    get_album_statistics,
    get_album_info,
    get_album_map_markers,
    create_album,
    add_assets_to_album,
    update_album,
    remove_asset_from_album,
    delete_album,
    add_assets_to_albums,
    router as albums_router,
)
from routers.utils.map_markers import GEOTAGGED_WORLD_BBOX
from routers.immich_models import (
    AlbumUserRole,
    CreateAlbumDto,
    UpdateAlbumDto,
    BulkIdsDto,
    AlbumsAddAssetsDto,
    BulkIdErrorReason,
)
from routers.utils.concurrency import BULK_FANOUT_CONCURRENCY_LIMIT
from routers.utils.gumnut_id_conversion import (
    uuid_to_gumnut_album_id,
    uuid_to_gumnut_asset_id,
    safe_uuid_from_asset_id,
)


def _add_response(
    added: list[str] | None = None,
    duplicate: list[str] | None = None,
    not_found: list[str] | None = None,
) -> AssetsAssociationAddResponse:
    return AssetsAssociationAddResponse(
        added_assets=added or [],
        duplicate_assets=duplicate or [],
        not_found_assets=not_found or [],
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
        mock_client.albums.list.assert_called_once_with(limit=GUMNUT_API_MAX_PAGE_SIZE)
        # Verify the real conversion happened by checking result structure
        assert all(hasattr(album, "id") for album in result)
        assert all(hasattr(album, "albumName") for album in result)
        # v3: owner is carried in albumUsers[0]; owner/ownerId/assets are gone.
        assert all(
            album.albumUsers[0].user.id == mock_current_user.id for album in result
        )
        assert all(album.albumUsers[0].role == AlbumUserRole.owner for album in result)

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
    async def test_get_all_albums_normalizes_start_end_dates(
        self, multiple_gumnut_albums, mock_sync_cursor_page, mock_current_user
    ):
        """A naive album start/end date must not 500; it is made tz-aware.

        The Gumnut API serializes an album's start/end date timezone-naive when
        the bounding asset's capture timezone is unknown. AlbumResponseDto
        requires timezone-aware values, so a non-null naive date previously
        raised a pydantic ValidationError (HTTP 500). The conversion now routes
        these through the keep-local-time helper: the wall-clock is preserved
        and labeled UTC, and None passes through untouched.
        """
        # Album 0: non-null naive start/end dates (wall-clock, no tzinfo).
        multiple_gumnut_albums[0].start_date = datetime(2011, 5, 7, 16, 17, 59, 500000)
        multiple_gumnut_albums[0].end_date = datetime(2013, 7, 21, 19, 55, 13, 700000)
        # Album 1: dates absent — must stay None.
        multiple_gumnut_albums[1].start_date = None
        multiple_gumnut_albums[1].end_date = None
        # Album 2: tz-aware non-UTC date (offset known) — wall-clock is kept and
        # re-labeled UTC, not instant-converted, matching how each asset's
        # localDateTime is rendered.
        multiple_gumnut_albums[2].start_date = datetime(
            2020, 1, 1, 10, 0, 0, tzinfo=timezone(timedelta(hours=-8))
        )
        multiple_gumnut_albums[2].end_date = datetime(
            2020, 1, 2, 22, 30, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))
        )

        mock_client = Mock()
        mock_client.albums.list.return_value = mock_sync_cursor_page(
            multiple_gumnut_albums
        )

        # Previously raised ValidationError -> 500; must now succeed.
        result = await get_all_albums(
            asset_id=None,
            shared=None,
            client=mock_client,
            current_user=mock_current_user,
        )

        # Naive dates become tz-aware with the wall-clock preserved (keepLocalTime).
        assert result[0].startDate == datetime(
            2011, 5, 7, 16, 17, 59, 500000, tzinfo=timezone.utc
        )
        assert result[0].endDate == datetime(
            2013, 7, 21, 19, 55, 13, 700000, tzinfo=timezone.utc
        )
        # None stays None.
        assert result[1].startDate is None
        assert result[1].endDate is None
        # Aware dates keep their wall-clock and are re-labeled UTC (not converted),
        # so the album's range matches the local times shown on its assets.
        assert result[2].startDate == datetime(
            2020, 1, 1, 10, 0, 0, tzinfo=timezone.utc
        )
        assert result[2].endDate == datetime(2020, 1, 2, 22, 30, 0, tzinfo=timezone.utc)

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
        mock_client.albums.list.assert_called_once_with(
            asset_id=expected_gumnut_id, limit=GUMNUT_API_MAX_PAGE_SIZE
        )

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
        mock_client.albums.list.assert_called_once_with(
            asset_id=expected_gumnut_id, limit=GUMNUT_API_MAX_PAGE_SIZE
        )

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
        expected_uuid0 = safe_uuid_from_asset_id(cover_asset_id0)
        assert result[0].albumThumbnailAssetId == expected_uuid0
        # Second album should have None (no cover)
        assert result[1].albumThumbnailAssetId is None
        # Third album should have its converted asset ID
        expected_uuid2 = safe_uuid_from_asset_id(cover_asset_id2)
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
        mock_gumnut_client.albums.list.assert_called_once_with(
            limit=GUMNUT_API_MAX_PAGE_SIZE
        )

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
        sample_uuid,
        mock_current_user,
    ):
        """Test successful retrieval of album info.

        Immich v3's AlbumResponseDto no longer inlines assets, so the endpoint
        must not fetch the album's assets. The owner is carried in
        albumUsers[0].
        """
        # Setup - create mock client
        mock_client = Mock()
        mock_client.albums.retrieve = AsyncMock(return_value=sample_gumnut_album)

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
        # v3 no longer inlines assets — the endpoint must not fetch them.
        mock_client.assets.list.assert_not_called()
        # Owner is derived from albumUsers[0].
        assert result.albumUsers[0].user.id == mock_current_user.id
        assert result.albumUsers[0].role == AlbumUserRole.owner

    @pytest.mark.anyio
    async def test_get_album_info_uses_gumnut_asset_count(
        self,
        sample_gumnut_album,
        sample_uuid,
        mock_current_user,
    ):
        """Test that get_album_info uses asset_count from Gumnut album object."""
        # Setup - mock album with specific asset_count
        sample_gumnut_album.asset_count = 42  # Set specific count

        mock_client = Mock()
        mock_client.albums.retrieve = AsyncMock(return_value=sample_gumnut_album)

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
        self, sample_gumnut_album, sample_uuid, mock_current_user
    ):
        """Test that album_cover_asset_id is converted to albumThumbnailAssetId in get_album_info."""
        # Setup - set album_cover_asset_id on the album
        cover_asset_id = uuid_to_gumnut_asset_id(uuid4())
        sample_gumnut_album.album_cover_asset_id = cover_asset_id

        mock_client = Mock()
        mock_client.albums.retrieve = AsyncMock(return_value=sample_gumnut_album)

        # Execute
        result = await get_album_info(
            sample_uuid,
            withoutAssets=False,
            client=mock_client,
            current_user=mock_current_user,
        )

        # Assert - verify albumThumbnailAssetId is set correctly
        expected_uuid = safe_uuid_from_asset_id(cover_asset_id)
        assert result.albumThumbnailAssetId == expected_uuid

    @pytest.mark.anyio
    async def test_get_album_info_without_assets(
        self, sample_gumnut_album, sample_uuid, mock_current_user
    ):
        """The vestigial withoutAssets=True is still accepted and returns the album.

        Assets are never inlined in v3 regardless of this param, so it no longer
        gates anything, but clients may still send it.
        """
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
        # Assets are never fetched in v3 (no longer inlined in the response).
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
        """An empty initial asset list creates an empty album."""
        # Setup - create mock client
        mock_client = Mock()
        # Update the sample to have the name we want to test
        sample_gumnut_album.name = "New Album"
        sample_gumnut_album.description = "New Description"
        mock_client.albums.create = AsyncMock(return_value=sample_gumnut_album)
        mock_client.albums.assets_associations.add = AsyncMock()

        request = CreateAlbumDto(albumName="New Album", description="New Description")

        # Execute
        result = await create_album(
            request, client=mock_client, current_user=mock_current_user
        )

        # Assert
        # Now result is a real AlbumResponseDto, so use attribute access
        assert hasattr(result, "albumName")
        assert result.albumName == "New Album"
        assert result.assetCount == 0
        mock_client.albums.create.assert_called_once_with(
            name="New Album", description="New Description"
        )
        mock_client.albums.assets_associations.add.assert_not_awaited()

    @pytest.mark.anyio
    async def test_create_album_with_single_asset(
        self, sample_gumnut_album, mock_current_user
    ):
        """A supplied asset is associated and reflected in the create response."""
        asset_uuid = uuid4()
        gumnut_asset_id = uuid_to_gumnut_asset_id(asset_uuid)
        mock_client = Mock()
        mock_client.albums.create = AsyncMock(return_value=sample_gumnut_album)
        mock_client.albums.assets_associations.add = AsyncMock(
            return_value=_add_response(added=[gumnut_asset_id])
        )

        result = await create_album(
            CreateAlbumDto(albumName="Single", assetIds=[asset_uuid]),
            client=mock_client,
            current_user=mock_current_user,
        )

        assert result.assetCount == 1
        mock_client.albums.assets_associations.add.assert_awaited_once_with(
            sample_gumnut_album.id,
            asset_ids=[gumnut_asset_id],
        )

    @pytest.mark.anyio
    async def test_create_album_with_multiple_assets(
        self, sample_gumnut_album, mock_current_user
    ):
        """Multiple initial assets are associated and counted."""
        asset_uuids = [uuid4(), uuid4()]
        gumnut_asset_ids = [
            uuid_to_gumnut_asset_id(asset_uuid) for asset_uuid in asset_uuids
        ]
        mock_client = Mock()
        mock_client.albums.create = AsyncMock(return_value=sample_gumnut_album)
        mock_client.albums.assets_associations.add = AsyncMock(
            return_value=_add_response(added=gumnut_asset_ids)
        )

        result = await create_album(
            CreateAlbumDto(albumName="Multiple", assetIds=asset_uuids),
            client=mock_client,
            current_user=mock_current_user,
        )

        assert result.assetCount == 2
        mock_client.albums.assets_associations.add.assert_awaited_once_with(
            sample_gumnut_album.id,
            asset_ids=gumnut_asset_ids,
        )

    @pytest.mark.anyio
    async def test_create_album_rolls_back_partial_asset_failure(
        self, sample_gumnut_album, mock_current_user
    ):
        """A response-reported missing asset fails and removes the partial album."""
        added_uuid = uuid4()
        missing_uuid = uuid4()
        added_id = uuid_to_gumnut_asset_id(added_uuid)
        missing_id = uuid_to_gumnut_asset_id(missing_uuid)
        mock_client = Mock()
        mock_client.albums.create = AsyncMock(return_value=sample_gumnut_album)
        mock_client.albums.assets_associations.add = AsyncMock(
            return_value=_add_response(added=[added_id], not_found=[missing_id])
        )
        mock_client.albums.delete = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await create_album(
                CreateAlbumDto(
                    albumName="Partial",
                    assetIds=[added_uuid, missing_uuid],
                ),
                client=mock_client,
                current_user=mock_current_user,
            )

        assert exc_info.value.status_code == 404
        mock_client.albums.delete.assert_awaited_once_with(sample_gumnut_album.id)

    @pytest.mark.anyio
    async def test_create_album_propagates_creation_sdk_error(self, mock_current_user):
        """Album creation SDK errors bubble to the global handler."""
        from gumnut import APIStatusError

        mock_client = Mock()
        mock_client.albums.create = AsyncMock(
            side_effect=make_sdk_status_error(500, "boom")
        )

        with pytest.raises(APIStatusError):
            await create_album(
                CreateAlbumDto(albumName="Test Album"),
                client=mock_client,
                current_user=mock_current_user,
            )

    @pytest.mark.anyio
    async def test_create_album_rolls_back_association_sdk_error(
        self, mock_current_user
    ):
        """Association SDK errors roll back and bubble to the global handler."""
        from gumnut import APIStatusError

        mock_client = Mock()
        sample_album = Mock(
            id=uuid_to_gumnut_album_id(uuid4()),
            name="Test Album",
            description=None,
        )
        mock_client.albums.create = AsyncMock(return_value=sample_album)
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=make_sdk_status_error(500, "boom")
        )
        mock_client.albums.delete = AsyncMock()

        request = CreateAlbumDto(albumName="Test Album", assetIds=[uuid4()])

        with pytest.raises(APIStatusError):
            await create_album(
                request, client=mock_client, current_user=mock_current_user
            )
        mock_client.albums.delete.assert_awaited_once_with(sample_album.id)


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

        assert [item.id for item in result] == [asset_id1, asset_id2]
        assert all(item.success is True for item in result)
        mock_client.albums.assets_associations.add.assert_called_once_with(
            uuid_to_gumnut_album_id(sample_uuid),
            asset_ids=[gumnut_id1, gumnut_id2],
        )

    @pytest.mark.anyio
    async def test_add_assets_duplicates_from_response(self, sample_uuid):
        """Duplicates are read from the response body."""
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
        assert result[0].id == new_asset
        assert result[0].success is True
        assert result[1].id == dup_asset
        assert result[1].success is False
        assert result[1].error == BulkIdErrorReason.duplicate

    @pytest.mark.anyio
    async def test_add_assets_not_found_assets_from_response(self, sample_uuid):
        """`not_found_assets` returned in the body maps to per-id not_found.

        Upstream returns 200 with the not_found bucket — these are ids that
        don't exist or aren't in the album's library, but the call as a whole
        succeeded.
        """
        new_asset = uuid4()
        missing_asset = uuid4()
        new_gid = uuid_to_gumnut_asset_id(new_asset)
        missing_gid = uuid_to_gumnut_asset_id(missing_asset)

        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            return_value=_add_response(added=[new_gid], not_found=[missing_gid])
        )

        request = BulkIdsDto(ids=[new_asset, missing_asset])
        result = await add_assets_to_album(sample_uuid, request, client=mock_client)

        assert len(result) == 2
        assert result[0].id == new_asset
        assert result[0].success is True
        assert result[1].id == missing_asset
        assert result[1].success is False
        assert result[1].error == BulkIdErrorReason.not_found

    @pytest.mark.anyio
    async def test_add_assets_not_found_marks_all(self, sample_uuid):
        """A 404 on the bulk call marks every requested asset as not_found.

        Upstream validates membership before any DB write, so a 404 means the
        whole batch failed — no per-item retry, no partial commit to recover.
        """
        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=make_sdk_status_error(404, "Not found", cls=NotFoundError)
        )

        asset_id1 = uuid4()
        asset_id2 = uuid4()
        request = BulkIdsDto(ids=[asset_id1, asset_id2])

        result = await add_assets_to_album(sample_uuid, request, client=mock_client)

        assert [item.id for item in result] == [asset_id1, asset_id2]
        assert all(item.success is False for item in result)
        assert all(item.error == BulkIdErrorReason.not_found for item in result)
        assert mock_client.albums.assets_associations.add.call_count == 1

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

        assert [item.id for item in result] == [asset_id1, asset_id2]
        assert all(item.success is False for item in result)
        assert all(item.error == BulkIdErrorReason.unknown for item in result)
        assert mock_client.albums.assets_associations.add.call_count == 1

    @pytest.mark.anyio
    async def test_add_assets_transport_error_marks_all(self, sample_uuid):
        """An SDK transport error on the bulk call marks every asset as unknown."""
        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=make_sdk_connection_error()
        )

        asset_id1 = uuid4()
        asset_id2 = uuid4()
        request = BulkIdsDto(ids=[asset_id1, asset_id2])

        result = await add_assets_to_album(sample_uuid, request, client=mock_client)

        assert [item.id for item in result] == [asset_id1, asset_id2]
        assert all(item.success is False for item in result)
        assert all(item.error == BulkIdErrorReason.unknown for item in result)
        assert mock_client.albums.assets_associations.add.call_count == 1

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "total, expected_call_count",
        [
            # Exact-boundary cases: pinning these locks the chunking math
            # against a future hand-rolled `if len(ids) > N` style split.
            (GUMNUT_API_MAX_BULK_IDS, 1),
            (GUMNUT_API_MAX_BULK_IDS + 1, 2),
            (GUMNUT_API_MAX_BULK_IDS * 2 + 5, 3),
        ],
    )
    async def test_add_assets_chunks_large_request(
        self, sample_uuid, total, expected_call_count
    ):
        """A request over the upstream limit is split across SDK calls.

        Each chunk's `added` set is merged into the final response, results
        come back in input order, and each chunk receives only its own ids.
        """
        asset_uuids = [uuid4() for _ in range(total)]
        gumnut_ids = [uuid_to_gumnut_asset_id(u) for u in asset_uuids]

        # Each chunk's response echoes the chunk's gumnut_ids back as `added`.
        responses = [
            _add_response(added=gumnut_ids[i : i + GUMNUT_API_MAX_BULK_IDS])
            for i in range(0, total, GUMNUT_API_MAX_BULK_IDS)
        ]
        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(side_effect=responses)

        request = BulkIdsDto(ids=asset_uuids)
        result = await add_assets_to_album(sample_uuid, request, client=mock_client)

        assert [item.id for item in result] == asset_uuids
        assert all(item.success is True for item in result)

        assert (
            mock_client.albums.assets_associations.add.call_count == expected_call_count
        )
        for idx, call in enumerate(
            mock_client.albums.assets_associations.add.call_args_list
        ):
            expected_chunk = gumnut_ids[
                idx * GUMNUT_API_MAX_BULK_IDS : (idx + 1) * GUMNUT_API_MAX_BULK_IDS
            ]
            assert call.kwargs["asset_ids"] == expected_chunk

    @pytest.mark.anyio
    async def test_add_assets_duplicate_in_later_chunk_surfaces_in_response(
        self, sample_uuid
    ):
        """`duplicate_assets` returned by a non-first chunk surfaces correctly
        in the per-asset response. Locks down the cross-chunk merge contract
        for `duplicate.update(...)` (the `added` merge has the symmetric
        coverage in `test_add_assets_chunks_large_request`)."""
        total = GUMNUT_API_MAX_BULK_IDS * 2
        asset_uuids = [uuid4() for _ in range(total)]
        gumnut_ids = [uuid_to_gumnut_asset_id(u) for u in asset_uuids]
        # Chunk 1 → all added. Chunk 2 → all added except the last id, which
        # the upstream reports as duplicate.
        chunk2_dup_index = total - 1
        chunk1_added = gumnut_ids[:GUMNUT_API_MAX_BULK_IDS]
        chunk2_added = gumnut_ids[GUMNUT_API_MAX_BULK_IDS:chunk2_dup_index]
        chunk2_dup = [gumnut_ids[chunk2_dup_index]]
        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=[
                _add_response(added=chunk1_added),
                _add_response(added=chunk2_added, duplicate=chunk2_dup),
            ]
        )

        request = BulkIdsDto(ids=asset_uuids)
        result = await add_assets_to_album(sample_uuid, request, client=mock_client)

        for item in result[:chunk2_dup_index]:
            assert item.success is True
        assert result[chunk2_dup_index].success is False
        assert result[chunk2_dup_index].error == BulkIdErrorReason.duplicate

    @pytest.mark.anyio
    async def test_add_assets_not_found_in_later_chunk_surfaces_in_response(
        self, sample_uuid
    ):
        """`not_found_assets` returned by a non-first chunk surfaces correctly
        in the per-asset response. Locks down the cross-chunk merge contract
        for `not_found.update(...)` — symmetric to the duplicate test above."""
        total = GUMNUT_API_MAX_BULK_IDS * 2
        asset_uuids = [uuid4() for _ in range(total)]
        gumnut_ids = [uuid_to_gumnut_asset_id(u) for u in asset_uuids]
        chunk2_missing_index = total - 1
        chunk1_added = gumnut_ids[:GUMNUT_API_MAX_BULK_IDS]
        chunk2_added = gumnut_ids[GUMNUT_API_MAX_BULK_IDS:chunk2_missing_index]
        chunk2_missing = [gumnut_ids[chunk2_missing_index]]
        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=[
                _add_response(added=chunk1_added),
                _add_response(added=chunk2_added, not_found=chunk2_missing),
            ]
        )

        request = BulkIdsDto(ids=asset_uuids)
        result = await add_assets_to_album(sample_uuid, request, client=mock_client)

        for item in result[:chunk2_missing_index]:
            assert item.success is True
        assert result[chunk2_missing_index].success is False
        assert result[chunk2_missing_index].error == BulkIdErrorReason.not_found

    @pytest.mark.anyio
    async def test_add_assets_empty_request(self, sample_uuid):
        """An empty `ids` list issues no SDK calls and returns an empty result."""
        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock()

        result = await add_assets_to_album(
            sample_uuid, BulkIdsDto(ids=[]), client=mock_client
        )

        assert result == []
        mock_client.albums.assets_associations.add.assert_not_called()

    @pytest.mark.anyio
    async def test_add_assets_chunk_failure_isolated_to_chunk(self, sample_uuid):
        """A failed chunk only fails its own ids; other chunks still succeed."""
        total = GUMNUT_API_MAX_BULK_IDS * 2
        asset_uuids = [uuid4() for _ in range(total)]
        gumnut_ids = [uuid_to_gumnut_asset_id(u) for u in asset_uuids]

        # Chunk 1 fails with 500; chunk 2 succeeds with all ids added.
        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=[
                make_sdk_status_error(500, "boom"),
                _add_response(added=gumnut_ids[GUMNUT_API_MAX_BULK_IDS:]),
            ]
        )

        request = BulkIdsDto(ids=asset_uuids)
        result = await add_assets_to_album(sample_uuid, request, client=mock_client)

        # Chunk 1 ids are unknown; chunk 2 ids succeed.
        for item in result[:GUMNUT_API_MAX_BULK_IDS]:
            assert item.success is False
            assert item.error == BulkIdErrorReason.unknown
        for item in result[GUMNUT_API_MAX_BULK_IDS:]:
            assert item.success is True
        assert mock_client.albums.assets_associations.add.call_count == 2

    @pytest.mark.anyio
    async def test_add_assets_chunk_transport_error_isolated(self, sample_uuid):
        """A transport error on one chunk only fails its own ids."""
        total = GUMNUT_API_MAX_BULK_IDS * 2
        asset_uuids = [uuid4() for _ in range(total)]
        gumnut_ids = [uuid_to_gumnut_asset_id(u) for u in asset_uuids]

        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=[
                _add_response(added=gumnut_ids[:GUMNUT_API_MAX_BULK_IDS]),
                make_sdk_connection_error(),
            ]
        )

        request = BulkIdsDto(ids=asset_uuids)
        result = await add_assets_to_album(sample_uuid, request, client=mock_client)

        for item in result[:GUMNUT_API_MAX_BULK_IDS]:
            assert item.success is True
        for item in result[GUMNUT_API_MAX_BULK_IDS:]:
            assert item.success is False
            assert item.error == BulkIdErrorReason.unknown

    @pytest.mark.anyio
    async def test_add_assets_missing_from_response_marked_unknown(
        self, sample_uuid, caplog
    ):
        """An asset_id absent from added/duplicate/not_found sets is marked unknown.

        Defensive against drift in the upstream response shape — if the Gumnut API
        ever introduces a new bucket the adapter doesn't yet handle, assets
        falling into it surface as unknown + warning instead of silently
        succeeding.
        """
        present = uuid4()
        missing = uuid4()
        present_gid = uuid_to_gumnut_asset_id(present)

        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            return_value=_add_response(added=[present_gid])
        )

        request = BulkIdsDto(ids=[present, missing])
        with caplog.at_level(logging.WARNING):
            result = await add_assets_to_album(sample_uuid, request, client=mock_client)

        assert result[0].id == present
        assert result[0].success is True
        assert result[1].id == missing
        assert result[1].success is False
        assert result[1].error == BulkIdErrorReason.unknown
        assert any(
            "missing from add_assets bulk response" in record.message
            for record in caplog.records
        )


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
        """albumThumbnailAssetId is forwarded to the SDK as album_cover_asset_id and echoed back."""
        cover_uuid = uuid4()
        cover_gumnut_id = uuid_to_gumnut_asset_id(cover_uuid)
        sample_gumnut_album.album_cover_asset_id = cover_gumnut_id

        mock_client = Mock()
        sample_gumnut_album.name = "Updated Album"
        mock_client.albums.update = AsyncMock(return_value=sample_gumnut_album)

        request = UpdateAlbumDto(
            albumName="Updated Album", albumThumbnailAssetId=cover_uuid
        )

        result = await update_album(
            sample_uuid, request, client=mock_client, current_user=mock_current_user
        )

        mock_client.albums.update.assert_called_once_with(
            uuid_to_gumnut_album_id(sample_uuid),
            name="Updated Album",
            album_cover_asset_id=cover_gumnut_id,
        )
        assert result.albumThumbnailAssetId == safe_uuid_from_asset_id(cover_gumnut_id)

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

        assert [item.id for item in result] == [asset_id1, asset_id2]
        assert all(item.success is True for item in result)
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

        assert [item.id for item in result] == [asset_id1, asset_id2]
        assert all(item.success is False for item in result)
        assert all(item.error == BulkIdErrorReason.not_found for item in result)

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
        assert all(item.error == BulkIdErrorReason.unknown for item in result)
        assert mock_client.albums.assets_associations.remove.call_count == 1

    @pytest.mark.anyio
    async def test_remove_assets_transport_error_marks_all(self, sample_uuid):
        """An SDK transport error on the bulk call marks every asset as unknown."""
        mock_client = Mock()
        mock_client.albums.assets_associations.remove = AsyncMock(
            side_effect=make_sdk_connection_error()
        )

        asset_id1 = uuid4()
        asset_id2 = uuid4()
        request = BulkIdsDto(ids=[asset_id1, asset_id2])

        result = await remove_asset_from_album(sample_uuid, request, client=mock_client)

        assert all(item.success is False for item in result)
        assert all(item.error == BulkIdErrorReason.unknown for item in result)
        assert mock_client.albums.assets_associations.remove.call_count == 1

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "total, expected_call_count",
        [
            # Exact-boundary cases: pinning these locks the chunking math
            # against a future hand-rolled `if len(ids) > N` style split.
            (GUMNUT_API_MAX_BULK_IDS, 1),
            (GUMNUT_API_MAX_BULK_IDS + 1, 2),
            (GUMNUT_API_MAX_BULK_IDS * 2 + 5, 3),
        ],
    )
    async def test_remove_assets_chunks_large_request(
        self, sample_uuid, total, expected_call_count
    ):
        """A request over the upstream limit is split across SDK calls."""
        asset_uuids = [uuid4() for _ in range(total)]
        gumnut_ids = [uuid_to_gumnut_asset_id(u) for u in asset_uuids]

        mock_client = Mock()
        mock_client.albums.assets_associations.remove = AsyncMock(return_value=None)

        request = BulkIdsDto(ids=asset_uuids)
        result = await remove_asset_from_album(sample_uuid, request, client=mock_client)

        assert [item.id for item in result] == asset_uuids
        assert all(item.success is True for item in result)

        assert (
            mock_client.albums.assets_associations.remove.call_count
            == expected_call_count
        )
        for idx, call in enumerate(
            mock_client.albums.assets_associations.remove.call_args_list
        ):
            expected_chunk = gumnut_ids[
                idx * GUMNUT_API_MAX_BULK_IDS : (idx + 1) * GUMNUT_API_MAX_BULK_IDS
            ]
            assert call.kwargs["asset_ids"] == expected_chunk

    @pytest.mark.anyio
    async def test_remove_assets_empty_request(self, sample_uuid):
        """An empty `ids` list issues no SDK calls and returns an empty result."""
        mock_client = Mock()
        mock_client.albums.assets_associations.remove = AsyncMock()

        result = await remove_asset_from_album(
            sample_uuid, BulkIdsDto(ids=[]), client=mock_client
        )

        assert result == []
        mock_client.albums.assets_associations.remove.assert_not_called()

    @pytest.mark.anyio
    async def test_remove_assets_chunk_failure_isolated_to_chunk(self, sample_uuid):
        """A failed chunk only fails its own ids; other chunks still succeed."""
        total = GUMNUT_API_MAX_BULK_IDS * 2
        asset_uuids = [uuid4() for _ in range(total)]

        mock_client = Mock()
        mock_client.albums.assets_associations.remove = AsyncMock(
            side_effect=[
                make_sdk_status_error(500, "boom"),
                None,
            ]
        )

        request = BulkIdsDto(ids=asset_uuids)
        result = await remove_asset_from_album(sample_uuid, request, client=mock_client)

        for item in result[:GUMNUT_API_MAX_BULK_IDS]:
            assert item.success is False
            assert item.error == BulkIdErrorReason.unknown
        for item in result[GUMNUT_API_MAX_BULK_IDS:]:
            assert item.success is True
        assert mock_client.albums.assets_associations.remove.call_count == 2

    @pytest.mark.anyio
    async def test_remove_assets_chunk_transport_error_isolated(self, sample_uuid):
        """A transport error on one chunk only fails its own ids."""
        total = GUMNUT_API_MAX_BULK_IDS * 2
        asset_uuids = [uuid4() for _ in range(total)]

        mock_client = Mock()
        mock_client.albums.assets_associations.remove = AsyncMock(
            side_effect=[
                None,
                make_sdk_connection_error(),
            ]
        )

        request = BulkIdsDto(ids=asset_uuids)
        result = await remove_asset_from_album(sample_uuid, request, client=mock_client)

        for item in result[:GUMNUT_API_MAX_BULK_IDS]:
            assert item.success is True
        for item in result[GUMNUT_API_MAX_BULK_IDS:]:
            assert item.success is False
            assert item.error == BulkIdErrorReason.unknown


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
        mock_client.albums.assets_associations.add = AsyncMock(
            return_value=_add_response()
        )

        album_ids = [uuid4(), uuid4()]
        asset_ids = [uuid4()]
        request = AlbumsAddAssetsDto(albumIds=album_ids, assetIds=asset_ids)

        result = await add_assets_to_albums(request, client=mock_client)

        assert result.success is True
        assert mock_client.albums.assets_associations.add.call_count == 2

    @pytest.mark.anyio
    async def test_add_assets_to_albums_chunks_assets_for_each_album(self):
        """Each album receives limit-sized asset chunks in input order."""
        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            return_value=_add_response()
        )

        album_ids = [uuid4(), uuid4()]
        asset_ids = [uuid4() for _ in range(GUMNUT_API_MAX_BULK_IDS + 1)]
        request = AlbumsAddAssetsDto(albumIds=album_ids, assetIds=asset_ids)

        result = await add_assets_to_albums(request, client=mock_client)

        assert result.success is True
        calls_by_album: dict[str, list[list[str]]] = {
            uuid_to_gumnut_album_id(album_id): [] for album_id in album_ids
        }
        for add_call in mock_client.albums.assets_associations.add.await_args_list:
            calls_by_album[add_call.args[0]].append(add_call.kwargs["asset_ids"])

        expected_ids = [uuid_to_gumnut_asset_id(asset_id) for asset_id in asset_ids]
        for album_id in album_ids:
            chunks = calls_by_album[uuid_to_gumnut_album_id(album_id)]
            assert [len(chunk) for chunk in chunks] == [
                GUMNUT_API_MAX_BULK_IDS,
                1,
            ]
            assert [asset_id for chunk in chunks for asset_id in chunk] == expected_ids

    @pytest.mark.anyio
    async def test_add_assets_to_albums_surfaces_later_chunk_not_found(self):
        """A successful upstream response can still report missing assets."""
        mock_client = Mock()
        asset_ids = [uuid4() for _ in range(GUMNUT_API_MAX_BULK_IDS + 1)]
        gumnut_ids = [uuid_to_gumnut_asset_id(asset_id) for asset_id in asset_ids]
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=[
                _add_response(added=gumnut_ids[:GUMNUT_API_MAX_BULK_IDS]),
                _add_response(not_found=[gumnut_ids[-1]]),
            ]
        )

        result = await add_assets_to_albums(
            AlbumsAddAssetsDto(albumIds=[uuid4()], assetIds=asset_ids),
            client=mock_client,
        )

        assert result.success is False
        assert result.error == BulkIdErrorReason.not_found

    @pytest.mark.anyio
    async def test_add_assets_to_albums_continues_after_chunk_not_found(self):
        """Missing assets in one chunk do not prevent later chunks from running."""
        mock_client = Mock()
        asset_ids = [uuid4() for _ in range(GUMNUT_API_MAX_BULK_IDS + 1)]
        gumnut_ids = [uuid_to_gumnut_asset_id(asset_id) for asset_id in asset_ids]
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=[
                _add_response(not_found=[gumnut_ids[0]]),
                _add_response(added=[gumnut_ids[-1]]),
            ]
        )

        result = await add_assets_to_albums(
            AlbumsAddAssetsDto(albumIds=[uuid4()], assetIds=asset_ids),
            client=mock_client,
        )

        assert result.success is False
        assert result.error == BulkIdErrorReason.not_found
        assert mock_client.albums.assets_associations.add.await_count == 2

    @pytest.mark.anyio
    async def test_add_assets_to_albums_rejects_excess_upstream_calls(self):
        """The album-by-chunk product cannot exceed the per-request budget."""
        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            return_value=_add_response()
        )
        request = AlbumsAddAssetsDto(
            albumIds=[uuid4() for _ in range(BULK_FANOUT_CONCURRENCY_LIMIT + 1)],
            assetIds=[uuid4()],
        )

        with pytest.raises(HTTPException) as exc_info:
            await add_assets_to_albums(request, client=mock_client)

        assert exc_info.value.status_code == 422
        assert "maximum upstream call budget" in exc_info.value.detail
        mock_client.albums.assets_associations.add.assert_not_awaited()

    @pytest.mark.anyio
    async def test_add_assets_to_albums_unexpected_conflict_records_unknown(self):
        """A surprise ConflictError falls back to `unknown` via the shared mapping.

        Upstream returns 200 with `duplicate_assets` rather than 409, so a
        ConflictError on this path is unexpected. `classify_bulk_item_error`
        recognizes only `NotFoundError` / `AuthenticationError` /
        `PermissionDeniedError`; anything else maps to `unknown` rather than
        guessing a more specific code at the call site.
        """
        from gumnut import ConflictError

        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=make_sdk_status_error(409, "duplicate", cls=ConflictError)
        )

        request = AlbumsAddAssetsDto(albumIds=[uuid4()], assetIds=[uuid4()])
        result = await add_assets_to_albums(request, client=mock_client)

        assert result.success is False
        assert result.error == BulkIdErrorReason.unknown

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
        assert result.error == BulkIdErrorReason.not_found

    @pytest.mark.anyio
    async def test_add_assets_to_albums_first_error_is_sticky(self):
        """`first_error` records the first failure across albums; later
        failures with a different classification do not overwrite it."""
        from gumnut import PermissionDeniedError

        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=[
                make_sdk_status_error(403, "Forbidden", cls=PermissionDeniedError),
                make_sdk_status_error(404, "Not found", cls=NotFoundError),
            ]
        )

        request = AlbumsAddAssetsDto(albumIds=[uuid4(), uuid4()], assetIds=[uuid4()])
        result = await add_assets_to_albums(request, client=mock_client)

        assert result.success is False
        assert result.error == BulkIdErrorReason.no_permission

    @pytest.mark.anyio
    async def test_add_assets_to_albums_partial_failure(self):
        """One success + one failure returns success=False with the failure's error."""
        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=[
                _add_response(),
                make_sdk_status_error(404, "Not found", cls=NotFoundError),
            ]
        )

        request = AlbumsAddAssetsDto(albumIds=[uuid4(), uuid4()], assetIds=[uuid4()])
        result = await add_assets_to_albums(request, client=mock_client)

        assert result.success is False
        assert result.error == BulkIdErrorReason.not_found

    @pytest.mark.anyio
    async def test_add_assets_to_albums_uses_bounded_concurrency(self):
        """Album fan-out runs in parallel but never exceeds the concurrency limit."""
        mock_client = Mock()

        active = 0
        peak = 0
        lock = asyncio.Lock()

        async def add_side_effect(*args, **kwargs):
            nonlocal active, peak
            async with lock:
                active += 1
                peak = max(peak, active)
            await asyncio.sleep(0.01)
            async with lock:
                active -= 1
            return _add_response()

        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=add_side_effect
        )

        album_ids = [uuid4() for _ in range(BULK_FANOUT_CONCURRENCY_LIMIT)]
        request = AlbumsAddAssetsDto(albumIds=album_ids, assetIds=[uuid4()])

        result = await add_assets_to_albums(request, client=mock_client)

        assert result.success is True
        assert peak > 1, "expected concurrent execution"
        assert peak <= BULK_FANOUT_CONCURRENCY_LIMIT


def _make_geo_asset(
    *,
    lat: float | None = None,
    lon: float | None = None,
    city: str | None = None,
    state: str | None = None,
    country: str | None = None,
    metadata_missing: bool = False,
) -> Mock:
    """Mock Gumnut asset with the GPS-relevant subset of metadata fields."""
    asset = Mock()
    asset.id = uuid_to_gumnut_asset_id(uuid4())
    if metadata_missing:
        asset.metadata = None
    else:
        metadata = Mock()
        metadata.latitude = lat
        metadata.longitude = lon
        metadata.city = city
        metadata.state = state
        metadata.country = country
        asset.metadata = metadata
    return asset


class TestGetAlbumMapMarkers:
    """Test the get_album_map_markers endpoint (GET /albums/{id}/map-markers)."""

    @pytest.mark.anyio
    async def test_returns_markers_for_geotagged_album_assets(
        self, sample_uuid, sample_gumnut_album, mock_sync_cursor_page
    ):
        """Geotagged assets become markers; coordinate-less assets are skipped."""
        asset_with_gps = _make_geo_asset(
            lat=40.5, lon=-74.1, city="Trenton", state="NJ", country="USA"
        )
        asset_no_metadata = _make_geo_asset(metadata_missing=True)
        asset_no_coords = _make_geo_asset(lat=None, lon=None, city="Nowhere")

        mock_client = Mock()
        mock_client.albums.retrieve = AsyncMock(return_value=sample_gumnut_album)
        mock_client.assets.list.return_value = mock_sync_cursor_page(
            [asset_with_gps, asset_no_metadata, asset_no_coords]
        )

        result = await get_album_map_markers(sample_uuid, client=mock_client)

        assert len(result) == 1
        marker = result[0]
        assert marker.id == safe_uuid_from_asset_id(asset_with_gps.id)
        assert marker.lat == 40.5
        assert marker.lon == -74.1
        assert marker.city == "Trenton"
        assert marker.state == "NJ"
        assert marker.country == "USA"

    @pytest.mark.anyio
    async def test_forwards_album_id_and_world_bbox(
        self, sample_uuid, sample_gumnut_album, mock_sync_cursor_page
    ):
        """The album filter is AND-combined with the geotagged world bbox."""
        mock_client = Mock()
        mock_client.albums.retrieve = AsyncMock(return_value=sample_gumnut_album)
        mock_client.assets.list.return_value = mock_sync_cursor_page([])

        await get_album_map_markers(sample_uuid, client=mock_client)

        kwargs = mock_client.assets.list.call_args.kwargs
        assert kwargs["album_id"] == uuid_to_gumnut_album_id(sample_uuid)
        assert kwargs["bbox"] == GEOTAGGED_WORLD_BBOX

    @pytest.mark.anyio
    async def test_key_and_slug_are_dropped_not_forwarded(
        self, sample_uuid, sample_gumnut_album, mock_sync_cursor_page
    ):
        """`key` / `slug` are accepted for client compat then dropped.

        They're shared-link tokens this adapter doesn't honor (`_ = key, slug`).
        This mirrors the global endpoint's `test_partner_and_shared_album_filters_are_dropped`
        and guards against a future edit wiring them into `retrieve`/`list` — the
        `retrieve` path especially, since threading a token there would be a
        silent shared-link authorization change.
        """
        mock_client = Mock()
        mock_client.albums.retrieve = AsyncMock(return_value=sample_gumnut_album)
        mock_client.assets.list.return_value = mock_sync_cursor_page([])

        await get_album_map_markers(
            sample_uuid,
            key="shared-link-key",
            slug="shared-slug",
            client=mock_client,
        )

        # The album retrieve gets only the album id — no key/slug threaded in.
        retrieve_call = mock_client.albums.retrieve.call_args
        assert "key" not in retrieve_call.kwargs
        assert "slug" not in retrieve_call.kwargs
        assert "shared-link-key" not in retrieve_call.args
        assert "shared-slug" not in retrieve_call.args

        # The asset list forwards only album_id + limit + include + bbox.
        assert set(mock_client.assets.list.call_args.kwargs.keys()) == {
            "album_id",
            "limit",
            "include",
            "bbox",
        }

    @pytest.mark.anyio
    async def test_empty_album_returns_empty_list(
        self, sample_uuid, sample_gumnut_album, mock_sync_cursor_page
    ):
        """An album with no geotagged assets returns an empty marker list."""
        mock_client = Mock()
        mock_client.albums.retrieve = AsyncMock(return_value=sample_gumnut_album)
        mock_client.assets.list.return_value = mock_sync_cursor_page([])

        result = await get_album_map_markers(sample_uuid, client=mock_client)

        assert result == []

    @pytest.mark.anyio
    async def test_missing_album_raises_not_found_before_listing(self, sample_uuid):
        """A missing album 404s via retrieve; markers are never listed."""
        mock_client = Mock()
        mock_client.albums.retrieve = AsyncMock(
            side_effect=make_sdk_status_error(404, "Not found", cls=NotFoundError)
        )
        mock_client.assets.list = Mock()

        with pytest.raises(NotFoundError):
            await get_album_map_markers(sample_uuid, client=mock_client)

        mock_client.assets.list.assert_not_called()

    def test_route_registered(self):
        """The album map-markers route is registered on the albums router."""
        paths = [getattr(route, "path", None) for route in albums_router.routes]
        assert "/api/albums/{id}/map-markers" in paths
