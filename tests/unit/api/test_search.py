"""Tests for search endpoints (person, metadata, statistics)."""

import pytest
from unittest.mock import AsyncMock, Mock
from uuid import uuid4
from datetime import datetime, timezone

from routers.api.search import search_person, search_assets, search_asset_statistics
from routers.immich_models import (
    MetadataSearchDto,
    StatisticsSearchDto,
)
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_person_id,
    uuid_to_gumnut_person_id,
)


def _make_person(
    person_id: str, name: str = "Test Person", is_hidden: bool = False
) -> Mock:
    """Create a mock Gumnut PersonResponse."""
    person = Mock()
    person.id = person_id
    person.name = name
    person.birth_date = datetime(1990, 1, 1).date()
    person.is_favorite = False
    person.is_hidden = is_hidden
    person.created_at = datetime.now(timezone.utc)
    person.updated_at = datetime.now(timezone.utc)
    return person


def _make_count_bucket(count: int, time_bucket: datetime) -> Mock:
    """Create a mock asset count bucket."""
    bucket = Mock()
    bucket.count = count
    bucket.time_bucket = time_bucket
    return bucket


class TestSearchPerson:
    """Test the search_person endpoint."""

    @pytest.mark.anyio
    async def test_returns_matching_people(self, mock_sync_cursor_page):
        """Test that search returns people matching the name."""
        person_id = uuid_to_gumnut_person_id(uuid4())
        person = _make_person(person_id, name="Calvin")

        mock_client = Mock()
        mock_client.people.list = Mock(return_value=mock_sync_cursor_page([person]))

        result = await search_person(
            name="Calvin",
            withHidden=None,  # type: ignore[arg-type]
            client=mock_client,
        )

        assert len(result) == 1
        assert result[0].name == "Calvin"
        assert result[0].id == str(safe_uuid_from_person_id(person_id))
        mock_client.people.list.assert_called_once_with(name="Calvin")

    @pytest.mark.anyio
    async def test_returns_empty_list_when_no_matches(self, mock_sync_cursor_page):
        """Test that empty list is returned when no people match."""
        mock_client = Mock()
        mock_client.people.list = Mock(return_value=mock_sync_cursor_page([]))

        result = await search_person(
            name="Nobody",
            withHidden=None,  # type: ignore[arg-type]
            client=mock_client,
        )

        assert result == []

    @pytest.mark.anyio
    async def test_includes_hidden_by_default(self, mock_sync_cursor_page):
        """Test that hidden people are included when withHidden is None."""
        person_id = uuid_to_gumnut_person_id(uuid4())
        hidden_person = _make_person(person_id, name="Hidden", is_hidden=True)

        mock_client = Mock()
        mock_client.people.list = Mock(
            return_value=mock_sync_cursor_page([hidden_person])
        )

        result = await search_person(
            name="Hidden",
            withHidden=None,  # type: ignore[arg-type]
            client=mock_client,
        )

        assert len(result) == 1

    @pytest.mark.anyio
    async def test_excludes_hidden_when_false(self, mock_sync_cursor_page):
        """Test that hidden people are excluded when withHidden is False."""
        person_id = uuid_to_gumnut_person_id(uuid4())
        hidden_person = _make_person(person_id, name="Hidden", is_hidden=True)

        mock_client = Mock()
        mock_client.people.list = Mock(
            return_value=mock_sync_cursor_page([hidden_person])
        )

        result = await search_person(
            name="Hidden", withHidden=False, client=mock_client
        )

        assert result == []

    @pytest.mark.anyio
    async def test_sdk_error_propagates(self):
        """SDK errors bubble up; the global GumnutError handler maps them."""
        from gumnut import APIStatusError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.people.list = Mock(side_effect=make_sdk_status_error(500, "boom"))

        with pytest.raises(APIStatusError):
            await search_person(
                name="Test",
                withHidden=None,  # type: ignore[arg-type]
                client=mock_client,
            )


class TestSearchStatistics:
    """Test the search_asset_statistics endpoint."""

    @pytest.mark.anyio
    async def test_returns_total_from_counts(self):
        """Test that statistics sums bucket counts."""
        buckets = [
            _make_count_bucket(100, datetime(2024, 1, 1, tzinfo=timezone.utc)),
            _make_count_bucket(200, datetime(2024, 2, 1, tzinfo=timezone.utc)),
            _make_count_bucket(50, datetime(2024, 3, 1, tzinfo=timezone.utc)),
        ]
        counts_response = Mock()
        counts_response.data = buckets
        counts_response.has_more = False

        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(return_value=counts_response)

        request = StatisticsSearchDto()
        result = await search_asset_statistics(request=request, client=mock_client)

        assert result.total == 350

    @pytest.mark.anyio
    async def test_returns_zero_when_no_buckets(self):
        """Test that statistics returns 0 when no buckets exist."""
        counts_response = Mock()
        counts_response.data = []
        counts_response.has_more = False

        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(return_value=counts_response)

        request = StatisticsSearchDto()
        result = await search_asset_statistics(request=request, client=mock_client)

        assert result.total == 0

    @pytest.mark.anyio
    async def test_sdk_error_propagates(self):
        """SDK errors bubble up; the global GumnutError handler maps them."""
        from gumnut import APIStatusError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(
            side_effect=make_sdk_status_error(500, "boom")
        )

        request = StatisticsSearchDto()
        with pytest.raises(APIStatusError):
            await search_asset_statistics(request=request, client=mock_client)


class TestSearchMetadata:
    """Test the search_assets (metadata) endpoint."""

    @pytest.mark.anyio
    async def test_passes_filters_to_sdk(self, mock_current_user):
        """Test that metadata search passes filters to SDK correctly."""
        search_response = Mock()
        search_response.data = []

        mock_client = Mock()
        mock_client.search.search = AsyncMock(return_value=search_response)

        taken_after = datetime(2024, 1, 1, tzinfo=timezone.utc)
        request = MetadataSearchDto(
            description="sunset",
            takenAfter=taken_after,
            size=10,
            page=1,
        )

        await search_assets(
            request=request, client=mock_client, current_user=mock_current_user
        )

        mock_client.search.search.assert_called_once_with(
            query="sunset",
            captured_after=taken_after,
            captured_before=None,
            person_ids=None,
            limit=10,
            page=1,
        )

    @pytest.mark.anyio
    async def test_returns_empty_results(self, mock_current_user):
        """Test that empty results are handled correctly."""
        search_response = Mock()
        search_response.data = []

        mock_client = Mock()
        mock_client.search.search = AsyncMock(return_value=search_response)

        request = MetadataSearchDto(description="nonexistent")

        result = await search_assets(
            request=request, client=mock_client, current_user=mock_current_user
        )

        assert result.assets.count == 0
        assert result.assets.items == []

    @pytest.mark.anyio
    async def test_converts_person_ids(self, mock_current_user):
        """Test that Immich person UUIDs are converted to Gumnut IDs."""
        person_uuid = uuid4()
        gumnut_person_id = uuid_to_gumnut_person_id(person_uuid)

        search_response = Mock()
        search_response.data = []

        mock_client = Mock()
        mock_client.search.search = AsyncMock(return_value=search_response)

        request = MetadataSearchDto(personIds=[person_uuid])

        await search_assets(
            request=request, client=mock_client, current_user=mock_current_user
        )

        call_kwargs = mock_client.search.search.call_args[1]
        assert call_kwargs["person_ids"] == [gumnut_person_id]

    @pytest.mark.anyio
    async def test_converts_search_results_to_immich_assets(self, mock_current_user):
        """Test that non-empty search results are converted via convert_gumnut_asset_to_immich."""
        from routers.utils.gumnut_id_conversion import uuid_to_gumnut_asset_id

        asset_uuid = uuid4()
        gumnut_asset = Mock()
        gumnut_asset.id = uuid_to_gumnut_asset_id(asset_uuid)
        gumnut_asset.original_file_name = "sunset.jpg"
        gumnut_asset.mime_type = "image/jpeg"
        gumnut_asset.checksum = "abc123"
        gumnut_asset.created_at = datetime(2024, 6, 1, tzinfo=timezone.utc)
        gumnut_asset.updated_at = datetime(2024, 6, 1, tzinfo=timezone.utc)
        gumnut_asset.width = 1920
        gumnut_asset.height = 1080
        gumnut_asset.file_size_bytes = 1024000
        gumnut_asset.metadata = None
        gumnut_asset.people = []
        gumnut_asset.trashed_at = None

        search_item = Mock()
        search_item.asset = gumnut_asset

        search_response = Mock()
        search_response.data = [search_item]

        mock_client = Mock()
        mock_client.search.search = AsyncMock(return_value=search_response)

        request = MetadataSearchDto(description="sunset")

        result = await search_assets(
            request=request, client=mock_client, current_user=mock_current_user
        )

        assert result.assets.count == 1
        assert len(result.assets.items) == 1
        assert result.assets.items[0].id == str(asset_uuid)
        assert result.assets.items[0].originalFileName == "sunset.jpg"

    @pytest.mark.anyio
    async def test_sdk_error_propagates(self, mock_current_user):
        """SDK errors bubble up; the global GumnutError handler maps them."""
        from gumnut import APIStatusError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.search.search = AsyncMock(
            side_effect=make_sdk_status_error(500, "boom")
        )

        request = MetadataSearchDto()

        with pytest.raises(APIStatusError):
            await search_assets(
                request=request, client=mock_client, current_user=mock_current_user
            )
