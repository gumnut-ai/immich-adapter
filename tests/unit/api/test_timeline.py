"""Tests for timeline.py endpoints."""

import pytest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4
from datetime import datetime, timezone, timedelta

from routers.api.timeline import (
    get_time_buckets,
    get_time_bucket,
    fetch_asset_counts,
)
from routers.immich_models import (
    AssetOrder,
    AssetVisibility,
)
from routers.utils.gumnut_id_conversion import (
    uuid_to_gumnut_asset_id,
    uuid_to_gumnut_album_id,
    uuid_to_gumnut_person_id,
)

# Expected date range query for January 2024 — the most common test timeBucket.
# Half-open interval: [month_start, next_month_start)
JANUARY_2024_DATE_RANGE = {
    "local_datetime_after": "2024-01-01T00:00:00",
    "local_datetime_before": "2024-02-01T00:00:00",
}


def _make_data(time_bucket: datetime, count: int) -> Mock:
    """Build a mock Data object matching gumnut.types.asset_count_response.Data."""
    d = Mock()
    d.time_bucket = time_bucket
    d.count = count
    return d


def _make_counts_response(data: list[Mock], has_more: bool = False) -> Mock:
    """Build a mock AssetCountResponse."""
    resp = Mock()
    resp.data = data
    resp.has_more = has_more
    return resp


def call_get_time_buckets(**kwargs):
    """Helper function to call get_time_buckets with proper None defaults for Query parameters."""
    defaults = {
        "albumId": None,
        "isFavorite": None,
        "isTrashed": None,
        "key": None,
        "order": None,
        "personId": None,
        "slug": None,
        "tagId": None,
        "userId": None,
        "visibility": None,
        "withCoordinates": None,
        "withPartners": None,
        "withStacked": None,
        "client": None,
    }
    defaults.update(kwargs)
    return get_time_buckets(**defaults)  # type: ignore


def call_get_time_bucket(timeBucket, **kwargs):
    """Helper function to call get_time_bucket with proper None defaults for Query parameters."""
    defaults = {
        "albumId": None,
        "isFavorite": None,
        "isTrashed": None,
        "key": None,
        "order": None,
        "personId": None,
        "slug": None,
        "tagId": None,
        "userId": None,
        "visibility": None,
        "withCoordinates": None,
        "withPartners": None,
        "withStacked": None,
        "client": None,
    }
    defaults.update(kwargs)
    return get_time_bucket(timeBucket, **defaults)  # type: ignore


class TestFetchAssetCounts:
    """Test the fetch_asset_counts helper."""

    @pytest.mark.anyio
    async def test_single_page(self):
        """Single page of results (has_more=False)."""
        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(
            return_value=_make_counts_response(
                [
                    _make_data(datetime(2024, 2, 1), 5),
                    _make_data(datetime(2024, 1, 1), 10),
                ]
            )
        )

        result = await fetch_asset_counts(mock_client)

        assert len(result) == 2
        assert result[0].count == 5
        assert result[1].count == 10
        mock_client.assets.counts.assert_called_once_with(group_by="month", limit=200)

    @pytest.mark.anyio
    async def test_pagination(self):
        """Multiple pages with has_more cursor pagination."""
        mock_client = Mock()
        feb_bucket = _make_data(datetime(2024, 2, 1), 5)
        jan_bucket = _make_data(datetime(2024, 1, 1), 10)

        mock_client.assets.counts = AsyncMock(
            side_effect=[
                _make_counts_response([feb_bucket], has_more=True),
                _make_counts_response([jan_bucket], has_more=False),
            ]
        )

        result = await fetch_asset_counts(mock_client)

        assert len(result) == 2
        assert mock_client.assets.counts.call_count == 2
        # Second call should include local_datetime_before cursor
        second_call_kwargs = mock_client.assets.counts.call_args_list[1][1]
        assert second_call_kwargs["local_datetime_before"] == feb_bucket.time_bucket

    @pytest.mark.anyio
    async def test_pagination_preserves_state(self):
        """state kwarg is preserved on every page, not just the first call.

        fetch_asset_counts mutates kwargs in place inside its while loop to add
        local_datetime_before. A future change that rebuilds kwargs each iteration
        could silently drop state on subsequent pages — assert it on every call.
        """
        mock_client = Mock()
        feb_bucket = _make_data(datetime(2024, 2, 1), 5)
        jan_bucket = _make_data(datetime(2024, 1, 1), 10)

        mock_client.assets.counts = AsyncMock(
            side_effect=[
                _make_counts_response([feb_bucket], has_more=True),
                _make_counts_response([jan_bucket], has_more=False),
            ]
        )

        await fetch_asset_counts(mock_client, state="trashed")

        assert mock_client.assets.counts.call_count == 2
        for call in mock_client.assets.counts.call_args_list:
            assert call.kwargs["state"] == "trashed"

    @pytest.mark.anyio
    async def test_with_album_id(self):
        """album_id is passed through as a kwarg."""
        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(return_value=_make_counts_response([]))

        await fetch_asset_counts(mock_client, album_id="album-123")

        kwargs = mock_client.assets.counts.call_args[1]
        assert kwargs["album_id"] == "album-123"

    @pytest.mark.anyio
    async def test_with_person_id(self):
        """person_id is passed through as a kwarg."""
        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(return_value=_make_counts_response([]))

        await fetch_asset_counts(mock_client, person_id="person-456")

        kwargs = mock_client.assets.counts.call_args[1]
        assert kwargs["person_id"] == "person-456"

    @pytest.mark.anyio
    async def test_empty_response(self):
        """Empty data returns empty list."""
        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(return_value=_make_counts_response([]))

        result = await fetch_asset_counts(mock_client)

        assert result == []


class TestGetTimeBuckets:
    """Test the get_time_buckets endpoint."""

    @pytest.mark.anyio
    async def test_get_time_buckets_success(self):
        """Test successful retrieval of time buckets."""
        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(
            return_value=_make_counts_response(
                [
                    _make_data(datetime(2024, 2, 1), 1),
                    _make_data(datetime(2024, 1, 1), 2),
                ]
            )
        )

        result = await call_get_time_buckets(client=mock_client)

        assert len(result) == 2
        # Descending order by default
        assert result[0].timeBucket == "2024-02-01"
        assert result[0].count == 1
        assert result[1].timeBucket == "2024-01-01"
        assert result[1].count == 2

    @pytest.mark.anyio
    async def test_get_time_buckets_with_album_id(self, sample_uuid):
        """Test time buckets with album filter."""
        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(
            return_value=_make_counts_response([_make_data(datetime(2024, 1, 1), 1)])
        )

        result = await call_get_time_buckets(albumId=sample_uuid, client=mock_client)

        assert len(result) == 1
        assert result[0].timeBucket == "2024-01-01"
        assert result[0].count == 1
        kwargs = mock_client.assets.counts.call_args[1]
        assert kwargs["album_id"] == uuid_to_gumnut_album_id(sample_uuid)

    @pytest.mark.anyio
    async def test_get_time_buckets_with_person_id(self, sample_uuid):
        """Test time buckets with person filter."""
        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(
            return_value=_make_counts_response([_make_data(datetime(2024, 1, 1), 1)])
        )

        result = await call_get_time_buckets(personId=sample_uuid, client=mock_client)

        assert len(result) == 1
        assert result[0].timeBucket == "2024-01-01"
        assert result[0].count == 1
        kwargs = mock_client.assets.counts.call_args[1]
        assert kwargs["person_id"] == uuid_to_gumnut_person_id(sample_uuid)

    @pytest.mark.anyio
    async def test_get_time_buckets_ascending_order(self):
        """Test time buckets with ascending order."""
        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(
            return_value=_make_counts_response(
                [
                    _make_data(datetime(2024, 2, 1), 1),
                    _make_data(datetime(2024, 1, 1), 2),
                ]
            )
        )

        result = await call_get_time_buckets(order=AssetOrder.asc, client=mock_client)

        assert len(result) == 2
        # Should be reversed to ascending
        assert result[0].timeBucket == "2024-01-01"
        assert result[1].timeBucket == "2024-02-01"

    @pytest.mark.anyio
    async def test_get_time_buckets_filtered_out_conditions(self):
        """Test that certain conditions return empty list immediately."""
        result = await call_get_time_buckets(isFavorite=True)
        assert result == []

        result = await call_get_time_buckets(visibility=AssetVisibility.archive)
        assert result == []

    @pytest.mark.anyio
    async def test_get_time_buckets_is_trashed_passes_state(self):
        """isTrashed=True routes through to backend counts with state='trashed'."""
        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(
            return_value=_make_counts_response([_make_data(datetime(2024, 1, 1), 4)])
        )

        result = await call_get_time_buckets(isTrashed=True, client=mock_client)

        assert len(result) == 1
        assert result[0].count == 4
        kwargs = mock_client.assets.counts.call_args[1]
        assert kwargs["state"] == "trashed"

    @pytest.mark.anyio
    async def test_get_time_buckets_is_trashed_false_omits_state(self):
        """isTrashed=False (or absent) does not pass state, so the backend default (live) applies."""
        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(
            return_value=_make_counts_response([_make_data(datetime(2024, 1, 1), 1)])
        )

        await call_get_time_buckets(isTrashed=False, client=mock_client)

        kwargs = mock_client.assets.counts.call_args[1]
        assert "state" not in kwargs

    @pytest.mark.anyio
    async def test_get_time_buckets_combines_state_with_album_id(self, sample_uuid):
        """isTrashed=True together with albumId forwards both kwargs — neither branch
        overwrites the other in fetch_asset_counts."""
        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(
            return_value=_make_counts_response([_make_data(datetime(2024, 1, 1), 1)])
        )

        await call_get_time_buckets(
            isTrashed=True, albumId=sample_uuid, client=mock_client
        )

        kwargs = mock_client.assets.counts.call_args[1]
        assert kwargs["state"] == "trashed"
        assert kwargs["album_id"] == uuid_to_gumnut_album_id(sample_uuid)

    @pytest.mark.anyio
    async def test_get_time_buckets_empty_assets(self):
        """Test time buckets with no assets."""
        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(return_value=_make_counts_response([]))

        result = await call_get_time_buckets(client=mock_client)

        assert result == []

    @pytest.mark.anyio
    async def test_get_time_buckets_propagates_sdk_error(self):
        """SDK errors bubble up; the global GumnutError handler maps them."""
        from gumnut import APIStatusError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(
            side_effect=make_sdk_status_error(500, "boom")
        )

        with pytest.raises(APIStatusError):
            await call_get_time_buckets(client=mock_client)

    @pytest.mark.anyio
    async def test_get_time_buckets_propagates_auth_error(self):
        """Authentication errors bubble up as AuthenticationError."""
        from gumnut import AuthenticationError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(
            side_effect=make_sdk_status_error(
                401, "Invalid API key", cls=AuthenticationError
            )
        )

        with pytest.raises(AuthenticationError):
            await call_get_time_buckets(client=mock_client)

    @pytest.mark.anyio
    async def test_get_time_buckets_normalizes_to_month_start(self):
        """Test that time_bucket values are normalized to YYYY-MM-01."""
        mock_client = Mock()
        # Even if the API returns a mid-month datetime, we normalize to the 1st
        mock_client.assets.counts = AsyncMock(
            return_value=_make_counts_response(
                [_make_data(datetime(2024, 3, 15, 12, 30, 45), 3)]
            )
        )

        result = await call_get_time_buckets(client=mock_client)

        assert len(result) == 1
        assert result[0].timeBucket == "2024-03-01"


class TestGetTimeBucket:
    """Test the get_time_bucket endpoint."""

    @pytest.mark.anyio
    async def test_get_time_bucket_success(
        self, multiple_gumnut_assets, mock_sync_cursor_page
    ):
        """Test successful retrieval of time bucket assets with server-side date filtering."""
        mock_client = Mock()

        assets = multiple_gumnut_assets
        assets[0].id = uuid_to_gumnut_asset_id(uuid4())
        assets[0].local_datetime = datetime(
            2024, 1, 15, 10, 0, 0, tzinfo=timezone(timedelta(hours=-5))
        )
        assets[0].created_at = assets[0].local_datetime
        assets[0].mime_type = "image/jpeg"
        assets[0].width = 1920
        assets[0].height = 1280

        assets[1].id = uuid_to_gumnut_asset_id(uuid4())
        assets[1].local_datetime = datetime(
            2024, 1, 25, 16, 0, 0, tzinfo=timezone(timedelta(hours=2))
        )
        assets[1].created_at = assets[1].local_datetime
        assets[1].mime_type = "image/png"
        assets[1].width = 1080
        assets[1].height = 1080

        mock_client.assets.list.return_value = mock_sync_cursor_page(assets[:2])

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = uuid4()

            result = await call_get_time_bucket(
                timeBucket="2024-01-01T00:00:00", client=mock_client
            )

            assert isinstance(result, dict)
            assert len(result["id"]) == 2
            assert len(result["fileCreatedAt"]) == 2
            assert len(result["isImage"]) == 2
            assert len(result["ratio"]) == 2

            assert result["isImage"][0] is True
            assert result["isImage"][1] is True

            assert result["ratio"][0] == 1920 / 1280
            assert result["ratio"][1] == 1080 / 1080

            assert result["localOffsetHours"][0] == -5
            assert result["localOffsetHours"][1] == 2
            assert result["fileCreatedAt"][0] == "2024-01-15T10:00:00.000"
            assert result["fileCreatedAt"][1] == "2024-01-25T16:00:00.000"

            assert all(fav is False for fav in result["isFavorite"])
            assert all(trash is False for trash in result["isTrashed"])
            assert all(vis == AssetVisibility.timeline for vis in result["visibility"])

            mock_client.assets.list.assert_called_once_with(
                extra_query=JANUARY_2024_DATE_RANGE
            )

    @pytest.mark.anyio
    async def test_get_time_bucket_ratio_passes_through_asset_dims(
        self, multiple_gumnut_assets, mock_sync_cursor_page
    ):
        """The timeline's per-asset ``ratio`` is ``asset.width / asset.height``.

        photos-api owns display-space dims at ingest; the adapter passes them
        through without consulting orientation. For a portrait, photos-api
        emits width=2268, height=4032 and ratio is width/height.
        """
        mock_client = Mock()

        assets = multiple_gumnut_assets
        assets[0].id = uuid_to_gumnut_asset_id(uuid4())
        assets[0].local_datetime = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        assets[0].created_at = assets[0].local_datetime
        assets[0].mime_type = "image/jpeg"
        # Portrait dims as photos-api returns them in display space.
        assets[0].width = 2268
        assets[0].height = 4032
        metadata = Mock()
        metadata.orientation = 6
        assets[0].metadata = metadata

        mock_client.assets.list.return_value = mock_sync_cursor_page([assets[0]])

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = uuid4()

            result = await call_get_time_bucket(
                timeBucket="2024-01-01T00:00:00", client=mock_client
            )

            assert len(result["ratio"]) == 1
            assert result["ratio"][0] == 2268 / 4032

    @pytest.mark.anyio
    async def test_get_time_bucket_emits_per_asset_duration(
        self, multiple_gumnut_assets, mock_sync_cursor_page
    ):
        """The bucket's ``duration`` array carries a real ``HH:MM:SS.ffffff``
        string for a video whose upstream duration is populated, and stays
        ``None`` for an asset whose upstream duration is NULL (image, or video
        not yet extracted) — matching the prior all-None behavior."""
        mock_client = Mock()

        assets = multiple_gumnut_assets
        assets[0].id = uuid_to_gumnut_asset_id(uuid4())
        assets[0].local_datetime = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        assets[0].created_at = assets[0].local_datetime
        assets[0].mime_type = "video/mp4"
        assets[0].duration = 65.25

        assets[1].id = uuid_to_gumnut_asset_id(uuid4())
        assets[1].local_datetime = datetime(2024, 1, 16, 10, 0, 0, tzinfo=timezone.utc)
        assets[1].created_at = assets[1].local_datetime
        assets[1].mime_type = "image/jpeg"
        assets[1].duration = None

        mock_client.assets.list.return_value = mock_sync_cursor_page(assets[:2])

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = uuid4()

            result = await call_get_time_bucket(
                timeBucket="2024-01-01T00:00:00", client=mock_client
            )

            assert result["duration"] == ["00:01:05.250000", None]

    @pytest.mark.anyio
    async def test_get_time_bucket_emits_per_asset_thumbhash(
        self, multiple_gumnut_assets, mock_sync_cursor_page
    ):
        """The bucket's ``thumbhash`` array carries each asset's real upstream
        value (and ``None`` when not yet generated) — not the single shared
        hardcoded constant the endpoint used to ship for every tile."""
        mock_client = Mock()

        assets = multiple_gumnut_assets
        assets[0].id = uuid_to_gumnut_asset_id(uuid4())
        assets[0].local_datetime = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        assets[0].created_at = assets[0].local_datetime
        assets[0].thumbhash = "1QcSHQRnh493V4dIh4eXh1h4kJUI"

        assets[1].id = uuid_to_gumnut_asset_id(uuid4())
        assets[1].local_datetime = datetime(2024, 1, 16, 10, 0, 0, tzinfo=timezone.utc)
        assets[1].created_at = assets[1].local_datetime
        assets[1].thumbhash = None

        mock_client.assets.list.return_value = mock_sync_cursor_page(assets[:2])

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = uuid4()

            result = await call_get_time_bucket(
                timeBucket="2024-01-01T00:00:00", client=mock_client
            )

            assert result["thumbhash"] == ["1QcSHQRnh493V4dIh4eXh1h4kJUI", None]

    @pytest.mark.anyio
    async def test_get_time_bucket_with_album_id(
        self, multiple_gumnut_assets, mock_sync_cursor_page, sample_uuid
    ):
        """Test time bucket with album filter uses server-side date filtering."""
        mock_client = Mock()

        assets = multiple_gumnut_assets
        assets[0].id = uuid_to_gumnut_asset_id(uuid4())
        assets[0].local_datetime = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        assets[0].created_at = assets[0].local_datetime
        assets[0].mime_type = "image/jpeg"
        assets[0].width = 1920
        assets[0].height = 1080

        mock_client.assets.list.return_value = mock_sync_cursor_page([assets[0]])

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = sample_uuid

            result = await call_get_time_bucket(
                timeBucket="2024-01-01T00:00:00",
                albumId=sample_uuid,
                client=mock_client,
            )

            assert len(result["id"]) == 1
            mock_client.assets.list.assert_called_once_with(
                album_id=uuid_to_gumnut_album_id(sample_uuid),
                extra_query=JANUARY_2024_DATE_RANGE,
            )

    @pytest.mark.anyio
    async def test_get_time_bucket_with_person_id(
        self, multiple_gumnut_assets, mock_sync_cursor_page, sample_uuid
    ):
        """Test time bucket with person filter uses server-side date filtering."""
        mock_client = Mock()

        assets = multiple_gumnut_assets
        assets[0].id = uuid_to_gumnut_asset_id(uuid4())
        assets[0].local_datetime = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        assets[0].created_at = assets[0].local_datetime
        assets[0].mime_type = "image/jpeg"
        assets[0].width = 1920
        assets[0].height = 1080

        mock_client.assets.list.return_value = mock_sync_cursor_page([assets[0]])

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = sample_uuid

            result = await call_get_time_bucket(
                timeBucket="2024-01-01T00:00:00",
                personId=sample_uuid,
                client=mock_client,
            )

            assert len(result["id"]) == 1
            mock_client.assets.list.assert_called_once_with(
                person_id=uuid_to_gumnut_person_id(sample_uuid),
                extra_query=JANUARY_2024_DATE_RANGE,
            )

    @pytest.mark.anyio
    async def test_get_time_bucket_no_matching_assets(self, mock_sync_cursor_page):
        """Test time bucket when server returns no assets for the date range."""
        mock_client = Mock()
        mock_client.assets.list.return_value = mock_sync_cursor_page([])

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = uuid4()

            result = await call_get_time_bucket(
                timeBucket="2024-01-01T00:00:00", client=mock_client
            )

            assert len(result["id"]) == 0
            assert len(result["fileCreatedAt"]) == 0
            assert len(result["isImage"]) == 0

            mock_client.assets.list.assert_called_once_with(
                extra_query=JANUARY_2024_DATE_RANGE
            )

    @pytest.mark.anyio
    async def test_get_time_bucket_with_non_utc_timezone(self, mock_sync_cursor_page):
        """Test handling of assets with non-UTC timezone offsets."""
        mock_client = Mock()

        mock_asset = Mock()
        mock_asset.id = uuid_to_gumnut_asset_id(uuid4())
        mock_asset.local_datetime = datetime(
            2024, 1, 15, 20, 0, 0, tzinfo=timezone(timedelta(hours=10))
        )
        mock_asset.created_at = mock_asset.local_datetime
        mock_asset.mime_type = "image/jpeg"
        mock_asset.width = 1920
        mock_asset.height = 1280
        mock_asset.duration = None

        mock_client.assets.list.return_value = mock_sync_cursor_page([mock_asset])

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = uuid4()

            result = await call_get_time_bucket(
                timeBucket="2024-01-01T00:00:00", client=mock_client
            )

            assert len(result["id"]) == 1
            assert result["ratio"][0] == 1920 / 1280
            assert result["localOffsetHours"][0] == 10
            assert result["isImage"][0] is True
            mock_client.assets.list.assert_called_once_with(
                extra_query=JANUARY_2024_DATE_RANGE
            )

    @pytest.mark.anyio
    async def test_get_time_bucket_missing_attributes(self, mock_sync_cursor_page):
        """Test handling of assets with missing attributes."""
        mock_client = Mock()

        mock_asset = Mock()
        mock_asset.id = uuid_to_gumnut_asset_id(uuid4())
        mock_asset.local_datetime = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        mock_asset.created_at = None
        mock_asset.mime_type = ""
        mock_asset.width = None
        mock_asset.height = None
        mock_asset.duration = None

        mock_client.assets.list.return_value = mock_sync_cursor_page([mock_asset])

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = uuid4()

            result = await call_get_time_bucket(
                timeBucket="2024-01-01T00:00:00", client=mock_client
            )

            assert len(result["id"]) == 1
            assert result["ratio"][0] == 1.0
            assert result["localOffsetHours"][0] == 0
            assert result["isImage"][0] is False

    @pytest.mark.anyio
    async def test_get_time_bucket_invalid_date_format(self):
        """Test handling of invalid timeBucket format."""
        mock_client = Mock()
        mock_client.assets.list.return_value = []

        with pytest.raises(Exception):
            await call_get_time_bucket(
                timeBucket="invalid-date-format", client=mock_client
            )

    @pytest.mark.anyio
    async def test_get_time_bucket_propagates_sdk_error(self):
        """SDK errors bubble up; the global GumnutError handler maps them."""
        from gumnut import APIStatusError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.assets.list.side_effect = make_sdk_status_error(500, "boom")

        with pytest.raises(APIStatusError):
            await call_get_time_bucket(
                timeBucket="2024-01-01T00:00:00", client=mock_client
            )

    @pytest.mark.anyio
    async def test_get_time_bucket_propagates_auth_error(self):
        """Authentication errors bubble up as AuthenticationError."""
        from gumnut import AuthenticationError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.assets.list.side_effect = make_sdk_status_error(
            401, "Invalid API key", cls=AuthenticationError
        )

        with pytest.raises(AuthenticationError):
            await call_get_time_bucket(
                timeBucket="2024-01-01T00:00:00", client=mock_client
            )

    @pytest.mark.anyio
    async def test_get_time_bucket_timezone_offsets(self, mock_sync_cursor_page):
        """Test timezone offset calculation for assets with different timezones."""
        mock_client = Mock()

        assets = []

        asset1 = Mock()
        asset1.id = uuid_to_gumnut_asset_id(uuid4())
        asset1.local_datetime = datetime(
            2024, 1, 15, 10, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))
        )
        asset1.created_at = asset1.local_datetime
        asset1.mime_type = "image/jpeg"
        asset1.width = 1920
        asset1.height = 1080
        asset1.duration = None
        assets.append(asset1)

        asset2 = Mock()
        asset2.id = uuid_to_gumnut_asset_id(uuid4())
        asset2.local_datetime = datetime(
            2024, 1, 15, 14, 0, 0, tzinfo=timezone(timedelta(hours=-8))
        )
        asset2.created_at = asset2.local_datetime
        asset2.mime_type = "image/png"
        asset2.width = 1024
        asset2.height = 768
        asset2.duration = None
        assets.append(asset2)

        asset3 = Mock()
        asset3.id = uuid_to_gumnut_asset_id(uuid4())
        asset3.local_datetime = datetime(2024, 1, 15, 18, 0, 0, tzinfo=timezone.utc)
        asset3.created_at = asset3.local_datetime
        asset3.mime_type = "video/mp4"
        asset3.width = 3840
        asset3.height = 2160
        asset3.duration = None
        assets.append(asset3)

        asset4 = Mock()
        asset4.id = uuid_to_gumnut_asset_id(uuid4())
        asset4.local_datetime = datetime(
            2024,
            1,
            15,
            20,
            0,
            0,
            tzinfo=timezone(timedelta(hours=-3, minutes=-30)),
        )
        asset4.created_at = asset4.local_datetime
        asset4.mime_type = "image/jpeg"
        asset4.width = 1600
        asset4.height = 1200
        asset4.duration = None
        assets.append(asset4)

        mock_client.assets.list.return_value = mock_sync_cursor_page(assets)

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = uuid4()

            result = await call_get_time_bucket(
                timeBucket="2024-01-01T00:00:00", client=mock_client
            )

            assert len(result["id"]) == 4
            assert result["localOffsetHours"][0] == 5
            assert result["localOffsetHours"][1] == -8
            assert result["localOffsetHours"][2] == 0
            assert result["localOffsetHours"][3] == -3

    @pytest.mark.anyio
    async def test_get_time_bucket_no_timezone_info(self, mock_sync_cursor_page):
        """Test timezone offset calculation for assets without timezone info (naive datetime)."""
        mock_client = Mock()

        assets = []

        asset1 = Mock()
        asset1.id = uuid_to_gumnut_asset_id(uuid4())
        asset1.local_datetime = datetime(2024, 1, 15, 10, 0, 0)
        asset1.created_at = asset1.local_datetime
        asset1.mime_type = "image/jpeg"
        asset1.width = 1920
        asset1.height = 1080
        asset1.duration = None
        assets.append(asset1)

        asset2 = Mock()
        asset2.id = uuid_to_gumnut_asset_id(uuid4())
        asset2.local_datetime = datetime(2024, 1, 15, 14, 0, 0)
        asset2.created_at = asset2.local_datetime
        asset2.mime_type = "image/png"
        asset2.width = 1024
        asset2.height = 768
        asset2.duration = None
        assets.append(asset2)

        mock_client.assets.list.return_value = mock_sync_cursor_page(assets)

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = uuid4()

            result = await call_get_time_bucket(
                timeBucket="2024-01-01T00:00:00", client=mock_client
            )

            assert len(result["id"]) == 2
            assert result["localOffsetHours"][0] == 0
            assert result["localOffsetHours"][1] == 0

    @pytest.mark.anyio
    async def test_get_time_bucket_mixed_timezone_info(self, mock_sync_cursor_page):
        """Test timezone offset calculation for mixed assets (some with tzinfo, some without)."""
        mock_client = Mock()

        assets = []

        asset1 = Mock()
        asset1.id = uuid_to_gumnut_asset_id(uuid4())
        asset1.local_datetime = datetime(
            2024, 1, 15, 10, 0, 0, tzinfo=timezone(timedelta(hours=2))
        )
        asset1.created_at = asset1.local_datetime
        asset1.mime_type = "image/jpeg"
        asset1.width = 1920
        asset1.height = 1080
        asset1.duration = None
        assets.append(asset1)

        asset2 = Mock()
        asset2.id = uuid_to_gumnut_asset_id(uuid4())
        asset2.local_datetime = datetime(2024, 1, 15, 14, 0, 0)
        asset2.created_at = asset2.local_datetime
        asset2.mime_type = "image/png"
        asset2.width = 1024
        asset2.height = 768
        asset2.duration = None
        assets.append(asset2)

        asset3 = Mock()
        asset3.id = uuid_to_gumnut_asset_id(uuid4())
        asset3.local_datetime = datetime(
            2024, 1, 15, 18, 0, 0, tzinfo=timezone(timedelta(hours=-5))
        )
        asset3.created_at = asset3.local_datetime
        asset3.mime_type = "video/mp4"
        asset3.width = 3840
        asset3.height = 2160
        asset3.duration = None
        assets.append(asset3)

        asset4 = Mock()
        asset4.id = uuid_to_gumnut_asset_id(uuid4())
        asset4.local_datetime = datetime(2024, 1, 15, 20, 0, 0)
        asset4.created_at = asset4.local_datetime
        asset4.mime_type = "image/jpeg"
        asset4.width = 1600
        asset4.height = 1200
        asset4.duration = None
        assets.append(asset4)

        mock_client.assets.list.return_value = mock_sync_cursor_page(assets)

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = uuid4()

            result = await call_get_time_bucket(
                timeBucket="2024-01-01T00:00:00", client=mock_client
            )

            assert len(result["id"]) == 4
            assert result["localOffsetHours"][0] == 2
            assert result["localOffsetHours"][1] == 0
            assert result["localOffsetHours"][2] == -5
            assert result["localOffsetHours"][3] == 0

    @pytest.mark.anyio
    async def test_get_time_bucket_is_trashed_passes_state_and_populates_flag(
        self, multiple_gumnut_assets, mock_sync_cursor_page
    ):
        """isTrashed=True routes to assets.list(state='trashed') and the per-asset
        isTrashed flag is sourced from each asset's trashed_at."""
        mock_client = Mock()

        assets = multiple_gumnut_assets
        assets[0].id = uuid_to_gumnut_asset_id(uuid4())
        assets[0].local_datetime = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        assets[0].mime_type = "image/jpeg"
        assets[0].width = 1920
        assets[0].height = 1080
        assets[0].trashed_at = datetime(2024, 1, 20, 12, 0, 0, tzinfo=timezone.utc)

        assets[1].id = uuid_to_gumnut_asset_id(uuid4())
        assets[1].local_datetime = datetime(2024, 1, 25, 16, 0, 0, tzinfo=timezone.utc)
        assets[1].mime_type = "image/png"
        assets[1].width = 1080
        assets[1].height = 1080
        assets[1].trashed_at = datetime(2024, 1, 26, 9, 0, 0, tzinfo=timezone.utc)

        mock_client.assets.list.return_value = mock_sync_cursor_page(assets[:2])

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = uuid4()

            result = await call_get_time_bucket(
                timeBucket="2024-01-01T00:00:00",
                isTrashed=True,
                client=mock_client,
            )

            assert len(result["id"]) == 2
            assert result["isTrashed"] == [True, True]
            mock_client.assets.list.assert_called_once_with(
                extra_query=JANUARY_2024_DATE_RANGE,
                state="trashed",
            )

    @pytest.mark.anyio
    async def test_get_time_bucket_default_path_reads_trashed_at_per_asset(
        self, multiple_gumnut_assets, mock_sync_cursor_page
    ):
        """Live path reads each asset's trashed_at — defensive: backend should already
        filter trashed rows, but the per-asset flag must reflect reality, not a constant."""
        mock_client = Mock()

        assets = multiple_gumnut_assets
        for asset in assets[:2]:
            asset.id = uuid_to_gumnut_asset_id(uuid4())
            asset.local_datetime = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
            asset.mime_type = "image/jpeg"
            asset.width = 1920
            asset.height = 1080
        # Both assets are live (trashed_at=None from the fixture).
        mock_client.assets.list.return_value = mock_sync_cursor_page(assets[:2])

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = uuid4()

            result = await call_get_time_bucket(
                timeBucket="2024-01-01T00:00:00", client=mock_client
            )

            assert result["isTrashed"] == [False, False]
            kwargs = mock_client.assets.list.call_args.kwargs
            assert "state" not in kwargs

    @pytest.mark.anyio
    async def test_get_time_bucket_combines_state_with_album_id(
        self, multiple_gumnut_assets, mock_sync_cursor_page, sample_uuid
    ):
        """isTrashed=True together with albumId forwards both kwargs — the album branch
        does not overwrite or short-circuit the state kwarg in list_kwargs."""
        mock_client = Mock()

        assets = multiple_gumnut_assets
        assets[0].id = uuid_to_gumnut_asset_id(uuid4())
        assets[0].local_datetime = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        assets[0].mime_type = "image/jpeg"
        assets[0].width = 1920
        assets[0].height = 1080
        assets[0].trashed_at = datetime(2024, 1, 20, 12, 0, 0, tzinfo=timezone.utc)

        mock_client.assets.list.return_value = mock_sync_cursor_page([assets[0]])

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = sample_uuid

            result = await call_get_time_bucket(
                timeBucket="2024-01-01T00:00:00",
                isTrashed=True,
                albumId=sample_uuid,
                client=mock_client,
            )

            assert result["isTrashed"] == [True]
            mock_client.assets.list.assert_called_once_with(
                extra_query=JANUARY_2024_DATE_RANGE,
                state="trashed",
                album_id=uuid_to_gumnut_album_id(sample_uuid),
            )


class TestTimezoneAwareTimeBucket:
    """Test that timezone-aware timeBucket values are handled correctly.

    The Immich client may send UTC-aware timestamps (e.g. "2025-10-01T00:00:00.000Z").
    The adapter must strip timezone info so that date boundaries use naive local time,
    consistent with how the counts endpoint groups by date_trunc("month", local_datetime).
    """

    @pytest.mark.anyio
    async def test_utc_timebucket_stripped_to_naive(self, mock_sync_cursor_page):
        """UTC-aware timeBucket produces the same naive boundaries as a naive one."""
        mock_client = Mock()
        mock_client.assets.list.return_value = mock_sync_cursor_page([])

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = uuid4()
            await call_get_time_bucket(
                timeBucket="2025-10-01T00:00:00.000Z", client=mock_client
            )

            assert mock_client.assets.list.call_count == 1
            assert mock_client.assets.list.call_args.kwargs["extra_query"] == {
                "local_datetime_after": "2025-10-01T00:00:00",
                "local_datetime_before": "2025-11-01T00:00:00",
            }

    @pytest.mark.anyio
    async def test_positive_offset_timebucket_stripped_to_naive(
        self, mock_sync_cursor_page
    ):
        """Timezone-aware timeBucket with positive offset is stripped to naive."""
        mock_client = Mock()
        mock_client.assets.list.return_value = mock_sync_cursor_page([])

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = uuid4()
            await call_get_time_bucket(
                timeBucket="2024-06-01T00:00:00+05:30", client=mock_client
            )

            assert mock_client.assets.list.call_count == 1
            assert mock_client.assets.list.call_args.kwargs["extra_query"] == {
                "local_datetime_after": "2024-06-01T00:00:00",
                "local_datetime_before": "2024-07-01T00:00:00",
            }


class TestDateRangeFiltering:
    """Test that date-range query parameters are computed correctly."""

    @pytest.mark.anyio
    async def test_february_leap_year(self, mock_sync_cursor_page):
        """Test exclusive end boundary for February in a leap year (2024)."""
        mock_client = Mock()
        mock_client.assets.list.return_value = mock_sync_cursor_page([])

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = uuid4()
            await call_get_time_bucket(
                timeBucket="2024-02-01T00:00:00", client=mock_client
            )

            mock_client.assets.list.assert_called_once_with(
                extra_query={
                    "local_datetime_after": "2024-02-01T00:00:00",
                    "local_datetime_before": "2024-03-01T00:00:00",
                }
            )

    @pytest.mark.anyio
    async def test_february_non_leap_year(self, mock_sync_cursor_page):
        """Test exclusive end boundary for February in a non-leap year (2023)."""
        mock_client = Mock()
        mock_client.assets.list.return_value = mock_sync_cursor_page([])

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = uuid4()
            await call_get_time_bucket(
                timeBucket="2023-02-01T00:00:00", client=mock_client
            )

            mock_client.assets.list.assert_called_once_with(
                extra_query={
                    "local_datetime_after": "2023-02-01T00:00:00",
                    "local_datetime_before": "2023-03-01T00:00:00",
                }
            )

    @pytest.mark.anyio
    async def test_thirty_day_month(self, mock_sync_cursor_page):
        """Test exclusive end boundary for a 30-day month (April)."""
        mock_client = Mock()
        mock_client.assets.list.return_value = mock_sync_cursor_page([])

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = uuid4()
            await call_get_time_bucket(
                timeBucket="2024-04-01T00:00:00", client=mock_client
            )

            mock_client.assets.list.assert_called_once_with(
                extra_query={
                    "local_datetime_after": "2024-04-01T00:00:00",
                    "local_datetime_before": "2024-05-01T00:00:00",
                }
            )

    @pytest.mark.anyio
    async def test_december(self, mock_sync_cursor_page):
        """Test exclusive end boundary for December (year boundary)."""
        mock_client = Mock()
        mock_client.assets.list.return_value = mock_sync_cursor_page([])

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = uuid4()
            await call_get_time_bucket(
                timeBucket="2024-12-01T00:00:00", client=mock_client
            )

            mock_client.assets.list.assert_called_once_with(
                extra_query={
                    "local_datetime_after": "2024-12-01T00:00:00",
                    "local_datetime_before": "2025-01-01T00:00:00",
                }
            )

    @pytest.mark.anyio
    async def test_album_id_uses_server_side_filtering(
        self, multiple_gumnut_assets, mock_sync_cursor_page, sample_uuid
    ):
        """Test that albumId branch now uses server-side date filtering via assets.list."""
        mock_client = Mock()

        assets = multiple_gumnut_assets
        assets[0].id = uuid_to_gumnut_asset_id(uuid4())
        assets[0].local_datetime = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        assets[0].mime_type = "image/jpeg"
        assets[0].width = 1920
        assets[0].height = 1080

        mock_client.assets.list.return_value = mock_sync_cursor_page([assets[0]])

        with patch("routers.api.timeline.get_current_user_id") as mock_user_id:
            mock_user_id.return_value = sample_uuid

            result = await call_get_time_bucket(
                timeBucket="2024-01-01T00:00:00",
                albumId=sample_uuid,
                client=mock_client,
            )

            assert len(result["id"]) == 1
            mock_client.assets.list.assert_called_once_with(
                album_id=uuid_to_gumnut_album_id(sample_uuid),
                extra_query=JANUARY_2024_DATE_RANGE,
            )
