"""Tests for routers/api/map.py."""

from datetime import datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest

from routers.api.map import MAP_MARKERS_CAP, MAX_ASSETS_SCANNED, get_map_markers
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_asset_id,
    uuid_to_gumnut_asset_id,
)
from tests.conftest import MockSyncCursorPage


def _make_asset(
    *,
    asset_id_uuid=None,
    lat: float | None = None,
    lon: float | None = None,
    city: str | None = None,
    state: str | None = None,
    country: str | None = None,
    metadata_missing: bool = False,
) -> Mock:
    """Mock Gumnut asset with the GPS-relevant subset of metadata fields."""
    asset = Mock()
    asset.id = uuid_to_gumnut_asset_id(asset_id_uuid or uuid4())
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


async def _call_markers(
    *,
    client,
    isArchived=None,
    isFavorite=None,
    fileCreatedAfter=None,
    fileCreatedBefore=None,
    withPartners=None,
    withSharedAlbums=None,
):
    return await get_map_markers(  # type: ignore[call-arg]
        isArchived=isArchived,
        isFavorite=isFavorite,
        fileCreatedAfter=fileCreatedAfter,
        fileCreatedBefore=fileCreatedBefore,
        withPartners=withPartners,
        withSharedAlbums=withSharedAlbums,
        client=client,
    )


class _PaginatedListing:
    """Async iterator that simulates the SDK's per-page pagination.

    Yields one item at a time across pages of `page_size`, tracking how many
    pages were "fetched". Dropping the explicit `break` in `get_map_markers`
    would visibly walk extra pages and fail tests that pin `pages_fetched`.
    """

    def __init__(self, items, page_size: int):
        self._items = items
        self._page_size = page_size
        self.pages_fetched = 0

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for i, item in enumerate(self._items):
            if i % self._page_size == 0:
                self.pages_fetched += 1
            yield item


class TestGetMapMarkers:
    @pytest.mark.anyio
    async def test_returns_markers_for_assets_with_gps(self):
        asset_uuid = uuid4()
        client = Mock()
        client.assets.list = Mock(
            return_value=MockSyncCursorPage(
                [
                    _make_asset(
                        asset_id_uuid=asset_uuid,
                        lat=37.7749,
                        lon=-122.4194,
                        city="San Francisco",
                        state="California",
                        country="USA",
                    )
                ]
            )
        )

        result = await _call_markers(client=client)

        assert len(result) == 1
        marker = result[0]
        assert marker.id == str(
            safe_uuid_from_asset_id(uuid_to_gumnut_asset_id(asset_uuid))
        )
        assert marker.lat == 37.7749
        assert marker.lon == -122.4194
        assert marker.city == "San Francisco"
        assert marker.state == "California"
        assert marker.country == "USA"

    @pytest.mark.anyio
    async def test_skips_assets_with_no_metadata(self):
        client = Mock()
        client.assets.list = Mock(
            return_value=MockSyncCursorPage(
                [
                    _make_asset(metadata_missing=True),
                    _make_asset(lat=10.0, lon=20.0),
                ]
            )
        )

        result = await _call_markers(client=client)

        assert len(result) == 1
        assert result[0].lat == 10.0

    @pytest.mark.anyio
    async def test_skips_assets_missing_lat_or_lon(self):
        client = Mock()
        client.assets.list = Mock(
            return_value=MockSyncCursorPage(
                [
                    _make_asset(lat=None, lon=20.0),
                    _make_asset(lat=10.0, lon=None),
                    _make_asset(lat=None, lon=None),
                    _make_asset(lat=37.7749, lon=-122.4194),
                ]
            )
        )

        result = await _call_markers(client=client)

        assert len(result) == 1
        assert (result[0].lat, result[0].lon) == (37.7749, -122.4194)

    @pytest.mark.anyio
    async def test_propagates_null_location_names(self):
        """`city`/`state`/`country` come through as None when the SDK has none."""
        client = Mock()
        client.assets.list = Mock(
            return_value=MockSyncCursorPage([_make_asset(lat=10.0, lon=20.0)])
        )

        result = await _call_markers(client=client)

        assert len(result) == 1
        assert result[0].city is None
        assert result[0].state is None
        assert result[0].country is None

    @pytest.mark.anyio
    async def test_forwards_date_range_filters_to_sdk(self):
        """`fileCreatedAfter`/`fileCreatedBefore` map to `local_datetime_*`."""
        client = Mock()
        client.assets.list = Mock(return_value=MockSyncCursorPage([]))

        after = datetime(2024, 1, 1, 0, 0, 0)
        before = datetime(2024, 6, 1, 0, 0, 0)
        await _call_markers(
            client=client, fileCreatedAfter=after, fileCreatedBefore=before
        )

        kwargs = client.assets.list.call_args.kwargs
        assert kwargs["local_datetime_after"] == after.isoformat()
        assert kwargs["local_datetime_before"] == before.isoformat()

    @pytest.mark.anyio
    async def test_omits_date_range_when_unset(self):
        """No date kwargs forwarded when the client didn't send any."""
        client = Mock()
        client.assets.list = Mock(return_value=MockSyncCursorPage([]))

        await _call_markers(client=client)

        kwargs = client.assets.list.call_args.kwargs
        assert "local_datetime_after" not in kwargs
        assert "local_datetime_before" not in kwargs

    @pytest.mark.anyio
    async def test_partner_and_shared_album_filters_are_dropped(self):
        """`withPartners` / `withSharedAlbums` must not leak into the SDK call."""
        client = Mock()
        client.assets.list = Mock(return_value=MockSyncCursorPage([]))

        await _call_markers(
            client=client,
            withPartners=True,
            withSharedAlbums=True,
        )

        kwargs = client.assets.list.call_args.kwargs
        # Adapter forwards only `limit` (and date range when set).
        assert set(kwargs.keys()) == {"limit"}

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "isFavorite,isArchived",
        [(True, None), (None, True), (True, True)],
    )
    async def test_short_circuits_when_filter_unsupported(self, isFavorite, isArchived):
        """`isFavorite=True` / `isArchived=True` return [] without hitting the SDK.

        Gumnut doesn't track favorites or archived state, so a request that
        filters on either would never match. Silently ignoring the filter
        and returning unfiltered markers would be a wrong answer.
        """
        client = Mock()
        client.assets.list = Mock(
            return_value=MockSyncCursorPage([_make_asset(lat=10.0, lon=20.0)])
        )

        result = await _call_markers(
            client=client, isFavorite=isFavorite, isArchived=isArchived
        )

        assert result == []
        client.assets.list.assert_not_called()

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "isFavorite,isArchived",
        [(False, None), (None, False), (False, False)],
    )
    async def test_does_not_short_circuit_on_false_or_none(
        self, isFavorite, isArchived
    ):
        """`isFavorite=False` / `isArchived=False` should not short-circuit.

        Only `True` indicates the client wants to *restrict* to favorites or
        archived; `False` and `None` mean "no restriction" and should return
        normal results.
        """
        client = Mock()
        client.assets.list = Mock(
            return_value=MockSyncCursorPage([_make_asset(lat=10.0, lon=20.0)])
        )

        result = await _call_markers(
            client=client, isFavorite=isFavorite, isArchived=isArchived
        )

        assert len(result) == 1
        client.assets.list.assert_called_once()

    @pytest.mark.anyio
    async def test_caps_at_marker_limit_and_stops_paging(self):
        """`MAP_MARKERS_CAP` is enforced via explicit break across pages.

        Uses a paginating mock so removing the `break` would walk every page
        past the cap (the assertion on `pages_fetched` would then go up).
        """
        page_size = 50
        total_assets = MAP_MARKERS_CAP + page_size  # one extra page beyond the cap
        assets = [_make_asset(lat=1.0, lon=2.0) for _ in range(total_assets)]
        listing = _PaginatedListing(assets, page_size=page_size)

        client = Mock()
        client.assets.list = Mock(return_value=listing)

        result = await _call_markers(client=client)

        assert len(result) == MAP_MARKERS_CAP
        # Cap is a multiple of page_size, so iteration stops at the last
        # item of page MAP_MARKERS_CAP/page_size; we must NOT have started
        # the next page.
        assert listing.pages_fetched == MAP_MARKERS_CAP // page_size

    @pytest.mark.anyio
    async def test_caps_assets_scanned_when_gps_density_is_low(self):
        """`MAX_ASSETS_SCANNED` bounds total work, not just markers returned.

        A low-GPS-density library would otherwise walk every page chasing a
        marker cap it can never fill. We iterate `MAX_ASSETS_SCANNED + 1`
        non-GPS assets and assert iteration stops at the cap — without the
        bound, the loop would walk every item.
        """
        page_size = 200
        # Twice the scan cap, all metadata-less so they never feed the
        # marker cap. Without MAX_ASSETS_SCANNED, the loop walks every asset.
        total_assets = MAX_ASSETS_SCANNED * 2
        assets = [_make_asset(metadata_missing=True) for _ in range(total_assets)]
        listing = _PaginatedListing(assets, page_size=page_size)

        client = Mock()
        client.assets.list = Mock(return_value=listing)

        result = await _call_markers(client=client)

        # No markers (none had GPS) and iteration stopped before walking
        # everything. With page_size=200 and MAX_ASSETS_SCANNED=6000, the
        # scan cap fires at the last item of page 30, so page 31 must not
        # have started.
        assert result == []
        assert listing.pages_fetched == MAX_ASSETS_SCANNED // page_size
