"""Tests for albums.py endpoints."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, Mock
from gumnut import NotFoundError
from gumnut.types.albums import AssetsAssociationAddResponse
from uuid import uuid4

from tests.conftest import make_sdk_connection_error, make_sdk_status_error
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
    AlbumUserRole,
    CreateAlbumDto,
    UpdateAlbumDto,
    BulkIdsDto,
    AlbumsAddAssetsDto,
    BulkIdErrorReason,
)
from routers.utils.concurrency import BULK_FANOUT_CONCURRENCY_LIMIT
from routers.utils.gumnut_client import BULK_CHUNK_SIZE
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
        # Check that mocks were called
        mock_client.albums.list.assert_called_once()
        # make sure that asset_id=None results in no parameter passed to albums.list()
        mock_client.albums.list.assert_called_once_with()
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
            (BULK_CHUNK_SIZE, 1),
            (BULK_CHUNK_SIZE + 1, 2),
            (BULK_CHUNK_SIZE * 2 + 5, 3),
        ],
    )
    async def test_add_assets_chunks_large_request(
        self, sample_uuid, total, expected_call_count
    ):
        """A request larger than BULK_CHUNK_SIZE is split across multiple SDK calls.

        Each chunk's `added` set is merged into the final response, results
        come back in input order, and each chunk receives only its own ids.
        """
        asset_uuids = [uuid4() for _ in range(total)]
        gumnut_ids = [uuid_to_gumnut_asset_id(u) for u in asset_uuids]

        # Each chunk's response echoes the chunk's gumnut_ids back as `added`.
        responses = [
            _add_response(added=gumnut_ids[i : i + BULK_CHUNK_SIZE])
            for i in range(0, total, BULK_CHUNK_SIZE)
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
                idx * BULK_CHUNK_SIZE : (idx + 1) * BULK_CHUNK_SIZE
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
        total = BULK_CHUNK_SIZE * 2
        asset_uuids = [uuid4() for _ in range(total)]
        gumnut_ids = [uuid_to_gumnut_asset_id(u) for u in asset_uuids]
        # Chunk 1 → all added. Chunk 2 → all added except the last id, which
        # the upstream reports as duplicate.
        chunk2_dup_index = total - 1
        chunk1_added = gumnut_ids[:BULK_CHUNK_SIZE]
        chunk2_added = gumnut_ids[BULK_CHUNK_SIZE:chunk2_dup_index]
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
        total = BULK_CHUNK_SIZE * 2
        asset_uuids = [uuid4() for _ in range(total)]
        gumnut_ids = [uuid_to_gumnut_asset_id(u) for u in asset_uuids]
        chunk2_missing_index = total - 1
        chunk1_added = gumnut_ids[:BULK_CHUNK_SIZE]
        chunk2_added = gumnut_ids[BULK_CHUNK_SIZE:chunk2_missing_index]
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
        total = BULK_CHUNK_SIZE * 2
        asset_uuids = [uuid4() for _ in range(total)]
        gumnut_ids = [uuid_to_gumnut_asset_id(u) for u in asset_uuids]

        # Chunk 1 fails with 500; chunk 2 succeeds with all ids added.
        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=[
                make_sdk_status_error(500, "boom"),
                _add_response(added=gumnut_ids[BULK_CHUNK_SIZE:]),
            ]
        )

        request = BulkIdsDto(ids=asset_uuids)
        result = await add_assets_to_album(sample_uuid, request, client=mock_client)

        # Chunk 1 ids are unknown; chunk 2 ids succeed.
        for item in result[:BULK_CHUNK_SIZE]:
            assert item.success is False
            assert item.error == BulkIdErrorReason.unknown
        for item in result[BULK_CHUNK_SIZE:]:
            assert item.success is True
        assert mock_client.albums.assets_associations.add.call_count == 2

    @pytest.mark.anyio
    async def test_add_assets_chunk_transport_error_isolated(self, sample_uuid):
        """A transport error on one chunk only fails its own ids."""
        total = BULK_CHUNK_SIZE * 2
        asset_uuids = [uuid4() for _ in range(total)]
        gumnut_ids = [uuid_to_gumnut_asset_id(u) for u in asset_uuids]

        mock_client = Mock()
        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=[
                _add_response(added=gumnut_ids[:BULK_CHUNK_SIZE]),
                make_sdk_connection_error(),
            ]
        )

        request = BulkIdsDto(ids=asset_uuids)
        result = await add_assets_to_album(sample_uuid, request, client=mock_client)

        for item in result[:BULK_CHUNK_SIZE]:
            assert item.success is True
        for item in result[BULK_CHUNK_SIZE:]:
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
            (BULK_CHUNK_SIZE, 1),
            (BULK_CHUNK_SIZE + 1, 2),
            (BULK_CHUNK_SIZE * 2 + 5, 3),
        ],
    )
    async def test_remove_assets_chunks_large_request(
        self, sample_uuid, total, expected_call_count
    ):
        """A request larger than BULK_CHUNK_SIZE is split across multiple SDK calls."""
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
                idx * BULK_CHUNK_SIZE : (idx + 1) * BULK_CHUNK_SIZE
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
        total = BULK_CHUNK_SIZE * 2
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

        for item in result[:BULK_CHUNK_SIZE]:
            assert item.success is False
            assert item.error == BulkIdErrorReason.unknown
        for item in result[BULK_CHUNK_SIZE:]:
            assert item.success is True
        assert mock_client.albums.assets_associations.remove.call_count == 2

    @pytest.mark.anyio
    async def test_remove_assets_chunk_transport_error_isolated(self, sample_uuid):
        """A transport error on one chunk only fails its own ids."""
        total = BULK_CHUNK_SIZE * 2
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

        for item in result[:BULK_CHUNK_SIZE]:
            assert item.success is True
        for item in result[BULK_CHUNK_SIZE:]:
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
        mock_client.albums.assets_associations.add = AsyncMock(return_value=None)

        album_ids = [uuid4(), uuid4()]
        asset_ids = [uuid4()]
        request = AlbumsAddAssetsDto(albumIds=album_ids, assetIds=asset_ids)

        result = await add_assets_to_albums(request, client=mock_client)

        assert result.success is True
        assert mock_client.albums.assets_associations.add.call_count == 2

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
                None,
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

        mock_client.albums.assets_associations.add = AsyncMock(
            side_effect=add_side_effect
        )

        album_ids = [uuid4() for _ in range(BULK_FANOUT_CONCURRENCY_LIMIT + 5)]
        request = AlbumsAddAssetsDto(albumIds=album_ids, assetIds=[uuid4()])

        result = await add_assets_to_albums(request, client=mock_client)

        assert result.success is True
        assert peak > 1, "expected concurrent execution"
        assert peak <= BULK_FANOUT_CONCURRENCY_LIMIT
