"""Tests for /search/* endpoints."""

import pytest
from unittest.mock import AsyncMock, Mock
from uuid import uuid4
from datetime import datetime, timezone

from routers.api.search import (
    get_explore_data,
    search_person,
    search_assets,
    search_asset_statistics,
    search_random,
    search_smart,
)
from routers.immich_models import (
    AssetTypeEnum,
    AssetVisibility,
    MetadataSearchDto,
    RandomSearchDto,
    SmartSearchDto,
    StatisticsSearchDto,
)
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
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
            # Opt back into the heavy fields the conversion reads, so the
            # response survives the Gumnut API lean-default flip.
            include=["metadata", "people", "file_data"],
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
    async def test_omits_pagination_kwargs_when_unspecified(self, mock_current_user):
        """When size/page are absent, the SDK is called without them so the Gumnut API
        applies its own defaults. Substituting our own defaults would fragment the
        single source of truth."""
        search_response = Mock()
        search_response.data = []

        mock_client = Mock()
        mock_client.search.search = AsyncMock(return_value=search_response)

        request = MetadataSearchDto(description="anything")

        await search_assets(
            request=request, client=mock_client, current_user=mock_current_user
        )

        call_kwargs = mock_client.search.search.call_args.kwargs
        assert "limit" not in call_kwargs
        assert "page" not in call_kwargs

    @pytest.mark.anyio
    async def test_clamps_size_to_gumnut_api_ceiling(self, mock_current_user):
        """The Immich client sends size=1000 by default; the Gumnut API caps at 200.
        The adapter must clamp before forwarding, otherwise the Gumnut API 422s."""
        search_response = Mock()
        search_response.data = []

        mock_client = Mock()
        mock_client.search.search = AsyncMock(return_value=search_response)

        request = MetadataSearchDto(description="anything", size=1000)

        await search_assets(
            request=request, client=mock_client, current_user=mock_current_user
        )

        assert mock_client.search.search.call_args.kwargs["limit"] == 200

    @pytest.mark.anyio
    async def test_response_next_page_is_none(self, mock_current_user):
        """The Immich mobile client does `nextPage?.toInt()` (Dart `?.` only
        short-circuits on null, not on empty string). Returning "" crashes the
        client with FormatException; None is the correct sentinel."""
        search_response = Mock()
        search_response.data = []

        mock_client = Mock()
        mock_client.search.search = AsyncMock(return_value=search_response)

        request = MetadataSearchDto(description="anything")

        result = await search_assets(
            request=request, client=mock_client, current_user=mock_current_user
        )

        assert result.assets.nextPage is None

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
        gumnut_asset.thumbhash = None
        gumnut_asset.created_at = datetime(2024, 6, 1, tzinfo=timezone.utc)
        gumnut_asset.updated_at = datetime(2024, 6, 1, tzinfo=timezone.utc)
        gumnut_asset.local_datetime = datetime(2024, 6, 1, tzinfo=timezone.utc)
        gumnut_asset.width = 1920
        gumnut_asset.height = 1080
        gumnut_asset.duration = None
        # File/provenance scalars live on the nested ``file_data`` group
        # (requested via ``include=file_data``); the adapter reads them from there.
        gumnut_asset.file_data = Mock()
        gumnut_asset.file_data.checksum = "abc123"
        gumnut_asset.file_data.checksum_sha1 = "PaDX6+c+Lhjpm5/ciXUROL1ryaU="
        gumnut_asset.file_data.file_created_at = gumnut_asset.local_datetime
        gumnut_asset.file_data.file_modified_at = gumnut_asset.updated_at
        gumnut_asset.file_data.file_size_bytes = 1024000
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


class TestSearchSmart:
    """Test the search_smart endpoint."""

    @pytest.mark.anyio
    async def test_response_next_page_is_none(self, mock_current_user):
        """Mirror of the /metadata regression test: the Immich mobile client
        does `nextPage?.toInt()`, so an empty string crashes it. Both /metadata
        and /smart return the same SearchResponseDto shape and the fix needs to
        ship in both places."""
        search_response = Mock()
        search_response.data = []

        mock_client = Mock()
        mock_client.search.search = AsyncMock(return_value=search_response)

        request = SmartSearchDto(query="anything")

        result = await search_smart(
            request=request, client=mock_client, current_user=mock_current_user
        )

        assert result.assets.nextPage is None

    @pytest.mark.anyio
    async def test_omits_pagination_kwargs_when_unspecified(self, mock_current_user):
        """When size/page are absent, the SDK is called without them so the Gumnut API
        applies its own defaults. Substituting our own defaults would fragment the
        single source of truth."""
        search_response = Mock()
        search_response.data = []

        mock_client = Mock()
        mock_client.search.search = AsyncMock(return_value=search_response)

        request = SmartSearchDto(query="anything")

        await search_smart(
            request=request, client=mock_client, current_user=mock_current_user
        )

        call_kwargs = mock_client.search.search.call_args.kwargs
        assert "limit" not in call_kwargs
        assert "page" not in call_kwargs

    @pytest.mark.anyio
    async def test_clamps_size_to_gumnut_api_ceiling(self, mock_current_user):
        """The Immich client sends size=1000 by default; the Gumnut API caps at 200.
        The adapter must clamp before forwarding, otherwise the Gumnut API 422s."""
        search_response = Mock()
        search_response.data = []

        mock_client = Mock()
        mock_client.search.search = AsyncMock(return_value=search_response)

        request = SmartSearchDto(query="anything", size=1000)

        await search_smart(
            request=request, client=mock_client, current_user=mock_current_user
        )

        assert mock_client.search.search.call_args.kwargs["limit"] == 200
        # Smart-search results convert to full AssetResponseDto — opt into the
        # heavy fields so the response survives the lean-default flip.
        assert mock_client.search.search.call_args.kwargs["include"] == [
            "metadata",
            "people",
            "file_data",
        ]


def _make_search_asset(taken_at: datetime, mime_type: str = "image/jpeg") -> Mock:
    """Create a mock Gumnut AssetResponse with the full include set."""
    from routers.utils.gumnut_id_conversion import uuid_to_gumnut_asset_id

    asset = Mock()
    asset.id = uuid_to_gumnut_asset_id(uuid4())
    asset.original_file_name = "photo.jpg"
    asset.mime_type = mime_type
    asset.thumbhash = None
    asset.created_at = taken_at
    asset.updated_at = taken_at
    asset.local_datetime = taken_at
    asset.width = 1920
    asset.height = 1080
    asset.duration = None
    # File/provenance scalars live on the nested ``file_data`` group
    # (requested via ``include=file_data``); the adapter reads them from there.
    asset.file_data = Mock()
    asset.file_data.checksum = "abc123"
    asset.file_data.checksum_sha1 = "PaDX6+c+Lhjpm5/ciXUROL1ryaU="
    asset.file_data.file_created_at = taken_at
    asset.file_data.file_modified_at = taken_at
    asset.file_data.file_size_bytes = 1024000
    asset.metadata = None
    asset.people = []
    asset.trashed_at = None
    return asset


def _scan_view(asset: Mock, city: str | None) -> Mock:
    """Create the metadata-only scan-time view of an asset (same id, city set)."""
    scan = Mock()
    scan.id = asset.id
    scan.mime_type = asset.mime_type
    scan.created_at = asset.created_at
    scan.metadata = Mock()
    scan.metadata.city = city
    return scan


def _counts_response(buckets: list[Mock]) -> Mock:
    response = Mock()
    response.data = buckets
    response.has_more = False
    return response


class TestSearchExplore:
    """Test the get_explore_data endpoint."""

    @pytest.mark.anyio
    async def test_returns_city_and_recents_groups(
        self, mock_sync_cursor_page, mock_current_user
    ):
        """Cities with enough images produce an exifInfo.city item; recents fill createdAt."""
        now = datetime(2024, 6, 1, tzinfo=timezone.utc)
        city_assets = [_make_search_asset(now) for _ in range(5)]
        no_city_asset = _make_search_asset(now)
        full_assets = city_assets + [no_city_asset]
        scan_assets = [_scan_view(a, "Sydney") for a in city_assets] + [
            _scan_view(no_city_asset, None)
        ]

        def list_side_effect(**kwargs):
            if "ids" in kwargs:
                wanted = set(kwargs["ids"])
                return mock_sync_cursor_page([a for a in full_assets if a.id in wanted])
            return mock_sync_cursor_page(scan_assets)

        mock_client = Mock()
        mock_client.assets.list = Mock(side_effect=list_side_effect)

        result = await get_explore_data(
            client=mock_client, current_user=mock_current_user
        )

        assert [group.fieldName for group in result] == ["exifInfo.city", "createdAt"]
        city_group, recents_group = result
        assert len(city_group.items) == 1
        assert city_group.items[0].value == "Sydney"
        # The representative is the newest (first-scanned) image for the city.
        assert city_group.items[0].data.originalFileName == "photo.jpg"
        assert len(recents_group.items) == 6

    @pytest.mark.anyio
    async def test_city_below_min_threshold_excluded(
        self, mock_sync_cursor_page, mock_current_user
    ):
        """Cities with fewer images than the minimum don't get an explore entry."""
        now = datetime(2024, 6, 1, tzinfo=timezone.utc)
        full_assets = [_make_search_asset(now) for _ in range(4)]
        scan_assets = [_scan_view(a, "Perth") for a in full_assets]

        def list_side_effect(**kwargs):
            if "ids" in kwargs:
                return mock_sync_cursor_page(full_assets)
            return mock_sync_cursor_page(scan_assets)

        mock_client = Mock()
        mock_client.assets.list = Mock(side_effect=list_side_effect)

        result = await get_explore_data(
            client=mock_client, current_user=mock_current_user
        )

        city_group = result[0]
        assert city_group.fieldName == "exifInfo.city"
        assert city_group.items == []

    @pytest.mark.anyio
    async def test_empty_library_returns_empty_groups(
        self, mock_sync_cursor_page, mock_current_user
    ):
        """An empty library still returns both groups (clients look them up by name)."""
        mock_client = Mock()
        mock_client.assets.list = Mock(return_value=mock_sync_cursor_page([]))

        result = await get_explore_data(
            client=mock_client, current_user=mock_current_user
        )

        assert [group.fieldName for group in result] == ["exifInfo.city", "createdAt"]
        assert all(group.items == [] for group in result)
        # No batched re-fetch when nothing was scanned.
        assert mock_client.assets.list.call_count == 1

    @pytest.mark.anyio
    async def test_videos_are_ignored(self, mock_sync_cursor_page, mock_current_user):
        """Only images count toward cities and recents, matching the Immich server."""
        now = datetime(2024, 6, 1, tzinfo=timezone.utc)
        videos = [_make_search_asset(now, mime_type="video/mp4") for _ in range(5)]
        scan_assets = [_scan_view(a, "Hobart") for a in videos]

        def list_side_effect(**kwargs):
            if "ids" in kwargs:
                return mock_sync_cursor_page(videos)
            return mock_sync_cursor_page(scan_assets)

        mock_client = Mock()
        mock_client.assets.list = Mock(side_effect=list_side_effect)

        result = await get_explore_data(
            client=mock_client, current_user=mock_current_user
        )

        assert all(group.items == [] for group in result)

    @pytest.mark.anyio
    async def test_vanished_representative_skipped(
        self, mock_sync_cursor_page, mock_current_user, monkeypatch
    ):
        """Representatives missing from the batched re-fetch are skipped, not 500s."""
        monkeypatch.setattr("routers.api.search.EXPLORE_MIN_ASSETS_PER_CITY", 1)
        now = datetime(2024, 6, 1, tzinfo=timezone.utc)
        asset = _make_search_asset(now)
        scan_assets = [_scan_view(asset, "Darwin")]

        def list_side_effect(**kwargs):
            if "ids" in kwargs:
                # Asset vanished between the scan and the re-fetch.
                return mock_sync_cursor_page([])
            return mock_sync_cursor_page(scan_assets)

        mock_client = Mock()
        mock_client.assets.list = Mock(side_effect=list_side_effect)

        result = await get_explore_data(
            client=mock_client, current_user=mock_current_user
        )

        assert all(group.items == [] for group in result)

    @pytest.mark.anyio
    async def test_city_cap_applied(
        self, mock_sync_cursor_page, mock_current_user, monkeypatch
    ):
        """No more than the configured maximum number of cities is returned."""
        monkeypatch.setattr("routers.api.search.EXPLORE_MIN_ASSETS_PER_CITY", 1)
        monkeypatch.setattr("routers.api.search.EXPLORE_MAX_CITIES", 2)
        now = datetime(2024, 6, 1, tzinfo=timezone.utc)
        full_assets = [_make_search_asset(now) for _ in range(3)]
        scan_assets = [
            _scan_view(asset, city)
            for asset, city in zip(full_assets, ["Sydney", "Perth", "Hobart"])
        ]

        def list_side_effect(**kwargs):
            if "ids" in kwargs:
                wanted = set(kwargs["ids"])
                return mock_sync_cursor_page([a for a in full_assets if a.id in wanted])
            return mock_sync_cursor_page(scan_assets)

        mock_client = Mock()
        mock_client.assets.list = Mock(side_effect=list_side_effect)

        result = await get_explore_data(
            client=mock_client, current_user=mock_current_user
        )

        city_group = result[0]
        # Newest-first scan order determines which cities make the cut.
        assert [item.value for item in city_group.items] == ["Sydney", "Perth"]

    @pytest.mark.anyio
    async def test_sdk_error_propagates(self, mock_current_user):
        """SDK errors bubble up; the global GumnutError handler maps them."""
        from gumnut import APIStatusError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.assets.list = Mock(side_effect=make_sdk_status_error(500, "boom"))

        with pytest.raises(APIStatusError):
            await get_explore_data(client=mock_client, current_user=mock_current_user)


class TestSearchRandom:
    """Test the search_random endpoint."""

    def _client_with_months(
        self,
        mock_sync_cursor_page,
        months: dict[str, list[Mock]],
        buckets: list[Mock],
    ) -> Mock:
        """Mock counts + a per-month assets.list keyed by local_datetime_after."""
        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(return_value=_counts_response(buckets))

        def list_side_effect(**kwargs):
            return mock_sync_cursor_page(months[kwargs["local_datetime_after"]])

        mock_client.assets.list = Mock(side_effect=list_side_effect)
        return mock_client

    @pytest.mark.anyio
    async def test_returns_empty_for_favorites(self, mock_current_user):
        """Gumnut has no favorites, so isFavorite=True returns an empty list."""
        mock_client = Mock()

        result = await search_random(
            request=RandomSearchDto(isFavorite=True),
            client=mock_client,
            current_user=mock_current_user,
        )

        assert result == []
        mock_client.assets.counts.assert_not_called()

    @pytest.mark.anyio
    async def test_returns_empty_for_non_timeline_visibility(self, mock_current_user):
        """Gumnut has no hidden/archived/locked assets."""
        mock_client = Mock()

        result = await search_random(
            request=RandomSearchDto(visibility=AssetVisibility.archive),
            client=mock_client,
            current_user=mock_current_user,
        )

        assert result == []
        mock_client.assets.counts.assert_not_called()

    @pytest.mark.anyio
    async def test_returns_empty_for_multiple_album_or_person_ids(
        self, mock_current_user
    ):
        """Multi-element albumIds/personIds have no Gumnut API equivalent."""
        mock_client = Mock()

        for request in (
            RandomSearchDto(albumIds=[uuid4(), uuid4()]),
            RandomSearchDto(personIds=[uuid4(), uuid4()]),
        ):
            result = await search_random(
                request=request, client=mock_client, current_user=mock_current_user
            )
            assert result == []
        mock_client.assets.counts.assert_not_called()

    @pytest.mark.anyio
    async def test_returns_empty_for_unsupported_restricting_filters(
        self, mock_current_user
    ):
        """Filters with no Gumnut translation return empty, never a mis-filtered sample."""
        mock_client = Mock()

        for request in (
            RandomSearchDto(takenAfter=datetime(2024, 1, 1, tzinfo=timezone.utc)),
            RandomSearchDto(city="Sydney"),
            RandomSearchDto(rating=0),
            RandomSearchDto(isMotion=True),
            RandomSearchDto(tagIds=[uuid4()]),
        ):
            result = await search_random(
                request=request, client=mock_client, current_user=mock_current_user
            )
            assert result == []
        mock_client.assets.counts.assert_not_called()

    @pytest.mark.anyio
    async def test_type_filter_applied_to_sample(
        self, mock_sync_cursor_page, mock_current_user
    ):
        """`type` filters the drawn sample by MIME-derived asset type."""
        taken = datetime(2024, 1, 15, tzinfo=timezone.utc)
        image = _make_search_asset(taken)
        video = _make_search_asset(taken, mime_type="video/mp4")
        buckets = [_make_count_bucket(2, datetime(2024, 1, 1))]
        months = {"2023-12-31T23:59:59.999999": [image, video]}
        mock_client = self._client_with_months(mock_sync_cursor_page, months, buckets)

        result = await search_random(
            request=RandomSearchDto(size=2, type=AssetTypeEnum.IMAGE),
            client=mock_client,
            current_user=mock_current_user,
        )

        assert [asset.id for asset in result] == [
            str(safe_uuid_from_asset_id(image.id))
        ]

    @pytest.mark.anyio
    async def test_type_filter_with_no_matches_returns_empty(
        self, mock_sync_cursor_page, mock_current_user
    ):
        """A type with no matching assets in the sample yields an empty list."""
        image = _make_search_asset(datetime(2024, 1, 15, tzinfo=timezone.utc))
        buckets = [_make_count_bucket(1, datetime(2024, 1, 1))]
        months = {"2023-12-31T23:59:59.999999": [image]}
        mock_client = self._client_with_months(mock_sync_cursor_page, months, buckets)

        result = await search_random(
            request=RandomSearchDto(size=1, type=AssetTypeEnum.VIDEO),
            client=mock_client,
            current_user=mock_current_user,
        )

        assert result == []

    def test_all_dto_fields_have_a_disposition(self):
        """Force a conscious decision when the generated DTO gains fields.

        The filter guard in search_random is a hand-maintained enumeration of
        RandomSearchDto fields. immich_models.py is regenerated from the
        Immich OpenAPI spec, so a new restricting field would otherwise be
        silently ignored and the endpoint would sample assets the caller
        filtered out.
        """
        translated = {"size", "albumIds", "personIds", "type"}
        guarded = {
            "isFavorite",
            "visibility",
            "city",
            "country",
            "state",
            "createdAfter",
            "createdBefore",
            "takenAfter",
            "takenBefore",
            "trashedAfter",
            "trashedBefore",
            "updatedAfter",
            "updatedBefore",
            "deviceId",
            "lensModel",
            "libraryId",
            "make",
            "model",
            "ocr",
            "rating",
            "tagIds",
            "isEncoded",
            "isMotion",
            "isNotInAlbum",
            "isOffline",
        }
        non_restricting = {"withDeleted", "withExif", "withPeople", "withStacked"}

        assert set(RandomSearchDto.model_fields) == (
            translated | guarded | non_restricting
        )

    @pytest.mark.anyio
    async def test_non_restricting_fields_still_sample(
        self, mock_sync_cursor_page, mock_current_user
    ):
        """Response-shape hints, widening flags, and falsy booleans don't empty the sample."""
        jan_assets = [_make_search_asset(datetime(2024, 1, 15, tzinfo=timezone.utc))]
        buckets = [_make_count_bucket(1, datetime(2024, 1, 1))]
        months = {"2023-12-31T23:59:59.999999": jan_assets}
        mock_client = self._client_with_months(mock_sync_cursor_page, months, buckets)

        result = await search_random(
            request=RandomSearchDto(
                size=1,
                withExif=True,
                withPeople=True,
                withStacked=True,
                withDeleted=True,
                isMotion=False,
                isEncoded=False,
                isFavorite=False,
            ),
            client=mock_client,
            current_user=mock_current_user,
        )

        assert len(result) == 1

    @pytest.mark.anyio
    async def test_returns_empty_when_library_empty(self, mock_current_user):
        """No count buckets means nothing to sample."""
        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(return_value=_counts_response([]))

        result = await search_random(
            request=RandomSearchDto(),
            client=mock_client,
            current_user=mock_current_user,
        )

        assert result == []
        mock_client.assets.list.assert_not_called()

    @pytest.mark.anyio
    async def test_returns_all_assets_when_size_exceeds_total(
        self, mock_sync_cursor_page, mock_current_user
    ):
        """When size >= total, every asset comes back exactly once."""
        feb_assets = [
            _make_search_asset(datetime(2024, 2, 10, tzinfo=timezone.utc))
            for _ in range(2)
        ]
        jan_assets = [
            _make_search_asset(datetime(2024, 1, 15, tzinfo=timezone.utc))
            for _ in range(3)
        ]
        buckets = [
            _make_count_bucket(2, datetime(2024, 2, 1)),
            _make_count_bucket(3, datetime(2024, 1, 1)),
        ]
        months = {
            "2024-01-31T23:59:59.999999": feb_assets,
            "2023-12-31T23:59:59.999999": jan_assets,
        }
        mock_client = self._client_with_months(mock_sync_cursor_page, months, buckets)

        result = await search_random(
            request=RandomSearchDto(size=10),
            client=mock_client,
            current_user=mock_current_user,
        )

        expected_uuids = {
            str(safe_uuid_from_asset_id(a.id)) for a in feb_assets + jan_assets
        }
        assert {asset.id for asset in result} == expected_uuids

    @pytest.mark.anyio
    async def test_month_windows_passed_to_list(
        self, mock_sync_cursor_page, mock_current_user
    ):
        """Sampled months are fetched with naive month-boundary date windows."""
        jan_assets = [
            _make_search_asset(datetime(2024, 1, 15, tzinfo=timezone.utc))
            for _ in range(2)
        ]
        buckets = [_make_count_bucket(2, datetime(2024, 1, 1))]
        months = {"2023-12-31T23:59:59.999999": jan_assets}
        mock_client = self._client_with_months(mock_sync_cursor_page, months, buckets)

        await search_random(
            request=RandomSearchDto(size=2),
            client=mock_client,
            current_user=mock_current_user,
        )

        list_kwargs = mock_client.assets.list.call_args.kwargs
        assert list_kwargs["local_datetime_after"] == "2023-12-31T23:59:59.999999"
        assert list_kwargs["local_datetime_before"] == "2024-02-01T00:00:00"
        assert list_kwargs["state"] == "live"

    @pytest.mark.anyio
    async def test_sample_smaller_than_total(
        self, mock_sync_cursor_page, mock_current_user
    ):
        """A sample smaller than the library returns exactly `size` distinct assets."""
        jan_assets = [
            _make_search_asset(datetime(2024, 1, 15, tzinfo=timezone.utc))
            for _ in range(5)
        ]
        buckets = [_make_count_bucket(5, datetime(2024, 1, 1))]
        months = {"2023-12-31T23:59:59.999999": jan_assets}
        mock_client = self._client_with_months(mock_sync_cursor_page, months, buckets)

        result = await search_random(
            request=RandomSearchDto(size=2),
            client=mock_client,
            current_user=mock_current_user,
        )

        assert len(result) == 2
        all_uuids = {str(safe_uuid_from_asset_id(a.id)) for a in jan_assets}
        assert {asset.id for asset in result} <= all_uuids
        assert len({asset.id for asset in result}) == 2

    @pytest.mark.anyio
    async def test_single_album_filter_forwarded(
        self, mock_sync_cursor_page, mock_current_user
    ):
        """A single-element albumIds filter is forwarded to counts and list."""
        from routers.utils.gumnut_id_conversion import uuid_to_gumnut_album_id

        album_uuid = uuid4()
        jan_assets = [_make_search_asset(datetime(2024, 1, 15, tzinfo=timezone.utc))]
        buckets = [_make_count_bucket(1, datetime(2024, 1, 1))]
        months = {"2023-12-31T23:59:59.999999": jan_assets}
        mock_client = self._client_with_months(mock_sync_cursor_page, months, buckets)

        result = await search_random(
            request=RandomSearchDto(albumIds=[album_uuid], size=1),
            client=mock_client,
            current_user=mock_current_user,
        )

        expected_album_id = uuid_to_gumnut_album_id(album_uuid)
        assert (
            mock_client.assets.counts.call_args.kwargs["album_id"] == expected_album_id
        )
        assert mock_client.assets.list.call_args.kwargs["album_id"] == expected_album_id
        assert len(result) == 1

    @pytest.mark.anyio
    async def test_december_rollover_month_window(
        self, mock_sync_cursor_page, mock_current_user
    ):
        """A December bucket's window rolls over into January of the next year."""
        dec_assets = [
            _make_search_asset(datetime(2024, 12, 15, tzinfo=timezone.utc))
            for _ in range(2)
        ]
        buckets = [_make_count_bucket(2, datetime(2024, 12, 1))]
        months = {"2024-11-30T23:59:59.999999": dec_assets}
        mock_client = self._client_with_months(mock_sync_cursor_page, months, buckets)

        await search_random(
            request=RandomSearchDto(size=2),
            client=mock_client,
            current_user=mock_current_user,
        )

        list_kwargs = mock_client.assets.list.call_args.kwargs
        assert list_kwargs["local_datetime_after"] == "2024-11-30T23:59:59.999999"
        assert list_kwargs["local_datetime_before"] == "2025-01-01T00:00:00"

    @pytest.mark.anyio
    async def test_default_size_caps_at_total(
        self, mock_sync_cursor_page, mock_current_user
    ):
        """With no size, the default (250) is capped at the library total."""
        jan_assets = [
            _make_search_asset(datetime(2024, 1, 15, tzinfo=timezone.utc))
            for _ in range(3)
        ]
        buckets = [_make_count_bucket(3, datetime(2024, 1, 1))]
        months = {"2023-12-31T23:59:59.999999": jan_assets}
        mock_client = self._client_with_months(mock_sync_cursor_page, months, buckets)

        result = await search_random(
            request=RandomSearchDto(),
            client=mock_client,
            current_user=mock_current_user,
        )

        expected_uuids = {str(safe_uuid_from_asset_id(a.id)) for a in jan_assets}
        assert {asset.id for asset in result} == expected_uuids

    @pytest.mark.anyio
    async def test_offset_mapping_across_bucket_boundary(
        self, mock_sync_cursor_page, mock_current_user, monkeypatch
    ):
        """Global indices map to the correct per-month offsets across buckets."""
        feb_assets = [
            _make_search_asset(datetime(2024, 2, 10, tzinfo=timezone.utc))
            for _ in range(2)
        ]
        jan_assets = [
            _make_search_asset(datetime(2024, 1, 15, tzinfo=timezone.utc))
            for _ in range(3)
        ]
        buckets = [
            _make_count_bucket(2, datetime(2024, 2, 1)),
            _make_count_bucket(3, datetime(2024, 1, 1)),
        ]
        months = {
            "2024-01-31T23:59:59.999999": feb_assets,
            "2023-12-31T23:59:59.999999": jan_assets,
        }
        mock_client = self._client_with_months(mock_sync_cursor_page, months, buckets)

        # Pin the draw to global indices 1 and 2: the last asset of the newer
        # (February) bucket and the first asset of the older (January) bucket.
        monkeypatch.setattr(
            "routers.api.search.random.sample", lambda population, k: [1, 2]
        )

        result = await search_random(
            request=RandomSearchDto(size=2),
            client=mock_client,
            current_user=mock_current_user,
        )

        expected_uuids = {
            str(safe_uuid_from_asset_id(feb_assets[1].id)),
            str(safe_uuid_from_asset_id(jan_assets[0].id)),
        }
        assert {asset.id for asset in result} == expected_uuids

    @pytest.mark.anyio
    async def test_stale_counts_return_short_sample(
        self, mock_sync_cursor_page, mock_current_user
    ):
        """If assets vanish between counts and fetch, the sample comes up short, not 500."""
        jan_assets = [
            _make_search_asset(datetime(2024, 1, 15, tzinfo=timezone.utc))
            for _ in range(2)
        ]
        # Counts claim 4 assets but the month only yields 2.
        buckets = [_make_count_bucket(4, datetime(2024, 1, 1))]
        months = {"2023-12-31T23:59:59.999999": jan_assets}
        mock_client = self._client_with_months(mock_sync_cursor_page, months, buckets)

        result = await search_random(
            request=RandomSearchDto(size=4),
            client=mock_client,
            current_user=mock_current_user,
        )

        assert len(result) == 2

    @pytest.mark.anyio
    async def test_sdk_error_propagates(self, mock_current_user):
        """SDK errors bubble up; the global GumnutError handler maps them."""
        from gumnut import APIStatusError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.assets.counts = AsyncMock(
            side_effect=make_sdk_status_error(500, "boom")
        )

        with pytest.raises(APIStatusError):
            await search_random(
                request=RandomSearchDto(),
                client=mock_client,
                current_user=mock_current_user,
            )
