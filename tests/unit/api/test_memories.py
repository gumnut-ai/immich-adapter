"""Tests for memories.py endpoints."""

from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from routers.api.memories import (
    _ASSETS_PER_MEMORY,
    _YEAR_WINDOW,
    decode_memory_id,
    encode_memory_id,
    get_memory,
    memories_statistics,
    search_memories,
)
from routers.immich_models import MemoryType
from routers.utils.gumnut_id_conversion import uuid_to_gumnut_asset_id
from tests.conftest import MockSyncCursorPage


def _call_search(
    *,
    client,
    current_user_id,
    current_user,
    for_param=None,
    isSaved=None,
    isTrashed=None,
    type=None,
):
    return search_memories(  # type: ignore[call-arg]
        for_param=for_param,
        isSaved=isSaved,
        isTrashed=isTrashed,
        type=type,
        client=client,
        current_user_id=current_user_id,
        current_user=current_user,
    )


def _call_statistics(
    *,
    client,
    for_param=None,
    isSaved=None,
    isTrashed=None,
    type=None,
):
    return memories_statistics(  # type: ignore[call-arg]
        for_param=for_param,
        isSaved=isSaved,
        isTrashed=isTrashed,
        type=type,
        client=client,
    )


def _make_asset(asset_id_uuid: UUID, captured_at: datetime) -> Mock:
    """Minimal mock Gumnut asset that survives `convert_gumnut_asset_to_immich`."""
    asset = Mock()
    asset.id = uuid_to_gumnut_asset_id(asset_id_uuid)
    asset.original_file_name = "memory.jpg"
    asset.mime_type = "image/jpeg"
    asset.checksum = "checksum"
    asset.width = 1920
    asset.height = 1080
    asset.created_at = captured_at
    asset.updated_at = captured_at
    asset.local_datetime = captured_at
    asset.metadata = None
    asset.people = []
    asset.trashed_at = None
    asset.file_size_bytes = 1000
    asset.duration_in_seconds = None
    asset.library_id = "library-1"
    return asset


def _stub_assets_per_year(client: Mock, asset_lists_by_year: dict[int, list[Mock]]):
    """Make `client.assets.list(...)` return the right page given its kwargs.

    The endpoint passes `local_datetime_after` as an ISO string with the year
    in the first 4 chars; we route on that.
    """

    def _list(**kwargs):
        after = kwargs.get("local_datetime_after", "")
        year = int(after[:4]) if after else 0
        return MockSyncCursorPage(asset_lists_by_year.get(year, []))

    client.assets.list = Mock(side_effect=_list)


# Use a fixed user UUID with non-zero high bytes to make sure the "low 8 bytes"
# binding logic in the encoder doesn't accidentally drop user identity.
USER_UUID = UUID("11112222-3333-4444-5555-666677778888")


class TestMemoryIdCodec:
    """Round-trip and tamper checks for the synthetic memory ID."""

    def test_round_trip(self):
        encoded = encode_memory_id(USER_UUID, 2024, 5, 4)
        decoded = decode_memory_id(encoded, USER_UUID)
        assert decoded == (2024, 5, 4)

    def test_distinct_inputs_produce_distinct_ids(self):
        a = encode_memory_id(USER_UUID, 2024, 5, 4)
        b = encode_memory_id(USER_UUID, 2024, 5, 5)
        c = encode_memory_id(USER_UUID, 2023, 5, 4)
        assert a != b != c != a

    def test_different_user_id_does_not_decode(self):
        encoded = encode_memory_id(USER_UUID, 2024, 5, 4)
        other_user = uuid4()
        # Vanishingly unlikely the random user shares the same low 8 bytes.
        assert decode_memory_id(encoded, other_user) is None

    def test_random_uuid_is_not_recognized_as_memory(self):
        # A random UUID will not have the marker in bytes 0–3.
        assert decode_memory_id(uuid4(), USER_UUID) is None

    def test_invalid_date_returns_none(self):
        # Manually build a UUID with our marker but month=13.
        marker = b"OTD\x00"
        raw = marker + (2024).to_bytes(2, "big") + bytes([13, 1]) + USER_UUID.bytes[8:]
        assert decode_memory_id(UUID(bytes=raw), USER_UUID) is None


class TestSearchMemories:
    @pytest.mark.anyio
    async def test_for_param_local_date_used_directly(self, mock_current_user):
        """`for` is treated as the user's local wall-clock date — month/day are
        pulled off the parsed datetime as-is, regardless of its tz offset."""
        client = Mock()
        # Pretend the user has one asset in 2024 on the requested local date.
        captured = datetime(2024, 5, 4, 12, 0, tzinfo=timezone.utc)
        asset = _make_asset(uuid4(), captured)
        _stub_assets_per_year(client, {2024: [asset]})

        # `for` = 2026-05-04 carrying a UTC tag (the `keepLocalTime` hack).
        for_param = datetime(2026, 5, 4, 23, 0, tzinfo=timezone.utc)
        result = await _call_search(
            client=client,
            current_user_id=UUID(mock_current_user.id),
            current_user=mock_current_user,
            for_param=for_param,
        )

        # Confirm we queried for May 4 of every year in the window — never May 5.
        assert len(client.assets.list.call_args_list) == _YEAR_WINDOW
        for call in client.assets.list.call_args_list:
            after = call.kwargs["local_datetime_after"]
            assert after.endswith("-05-04T00:00:00")
            assert call.kwargs["local_datetime_before"].endswith("-05-05T00:00:00")
            assert call.kwargs["limit"] == _ASSETS_PER_MEMORY

        # Only one year had assets, so only one memory comes back.
        assert len(result) == 1
        assert result[0].data.year == 2024
        assert len(result[0].assets) == 1
        # ID is decodable back to the same year/month/day.
        decoded = decode_memory_id(UUID(result[0].id), UUID(mock_current_user.id))
        assert decoded == (2024, 5, 4)

    @pytest.mark.anyio
    async def test_drops_years_with_no_assets(self, mock_current_user):
        client = Mock()
        captured = datetime(2022, 5, 4, 12, 0, tzinfo=timezone.utc)
        # Two non-empty years in the window; the rest should be filtered.
        _stub_assets_per_year(
            client,
            {
                2022: [_make_asset(uuid4(), captured)],
                2020: [_make_asset(uuid4(), captured.replace(year=2020))],
            },
        )

        result = await _call_search(
            client=client,
            current_user_id=UUID(mock_current_user.id),
            current_user=mock_current_user,
            for_param=datetime(2026, 5, 4, tzinfo=timezone.utc),
        )

        years = [m.data.year for m in result]
        assert years == [2022, 2020]

    @pytest.mark.anyio
    async def test_is_saved_true_short_circuits(self, mock_current_user):
        """Synthetic memories are never saved — short-circuit before fan-out."""
        client = Mock()
        client.assets.list = Mock()  # Should never be called.

        result = await _call_search(
            client=client,
            current_user_id=UUID(mock_current_user.id),
            current_user=mock_current_user,
            isSaved=True,
        )

        assert result == []
        client.assets.list.assert_not_called()

    @pytest.mark.anyio
    async def test_is_trashed_true_short_circuits(self, mock_current_user):
        client = Mock()
        client.assets.list = Mock()

        result = await _call_search(
            client=client,
            current_user_id=UUID(mock_current_user.id),
            current_user=mock_current_user,
            isTrashed=True,
        )

        assert result == []
        client.assets.list.assert_not_called()

    @pytest.mark.anyio
    async def test_type_other_than_on_this_day_is_currently_impossible_but_handled(
        self, mock_current_user
    ):
        """`MemoryType` only contains `on_this_day` today, so an explicit
        on_this_day filter must not short-circuit; any future non-OTD type
        would short-circuit."""
        client = Mock()
        captured = datetime(2024, 5, 4, 12, 0, tzinfo=timezone.utc)
        _stub_assets_per_year(client, {2024: [_make_asset(uuid4(), captured)]})

        result = await _call_search(
            client=client,
            current_user_id=UUID(mock_current_user.id),
            current_user=mock_current_user,
            type=MemoryType.on_this_day,
            for_param=datetime(2026, 5, 4, tzinfo=timezone.utc),
        )

        assert len(result) == 1


class TestMemoriesStatistics:
    @pytest.mark.anyio
    async def test_counts_non_empty_years(self):
        client = Mock()
        captured = datetime(2024, 5, 4, 12, 0, tzinfo=timezone.utc)
        _stub_assets_per_year(
            client,
            {
                2024: [_make_asset(uuid4(), captured)],
                2020: [_make_asset(uuid4(), captured.replace(year=2020))],
                2015: [_make_asset(uuid4(), captured.replace(year=2015))],
            },
        )

        result = await _call_statistics(
            client=client,
            for_param=datetime(2026, 5, 4, tzinfo=timezone.utc),
        )

        assert result.total == 3
        # Statistics should request only `limit=1` per year — we only need to
        # know whether the year is non-empty, not fetch full thumbnails.
        for call in client.assets.list.call_args_list:
            assert call.kwargs["limit"] == 1

    @pytest.mark.anyio
    async def test_filters_short_circuit_to_zero(self):
        client = Mock()
        client.assets.list = Mock()

        result = await _call_statistics(client=client, isSaved=True)

        assert result.total == 0
        client.assets.list.assert_not_called()


class TestGetMemory:
    @pytest.mark.anyio
    async def test_returns_memory_for_valid_id(self, mock_current_user):
        user_uuid = UUID(mock_current_user.id)
        memory_id = encode_memory_id(user_uuid, 2024, 5, 4)
        captured = datetime(2024, 5, 4, 12, 0, tzinfo=timezone.utc)
        client = Mock()
        client.assets.list = Mock(
            return_value=MockSyncCursorPage([_make_asset(uuid4(), captured)])
        )

        result = await get_memory(
            id=memory_id,
            client=client,
            current_user_id=user_uuid,
            current_user=mock_current_user,
        )

        assert result.id == str(memory_id)
        assert result.data.year == 2024
        assert len(result.assets) == 1

    @pytest.mark.anyio
    async def test_404_for_random_uuid(self, mock_current_user):
        client = Mock()
        client.assets.list = Mock()  # Should never be called.

        with pytest.raises(HTTPException) as exc:
            await get_memory(
                id=uuid4(),
                client=client,
                current_user_id=UUID(mock_current_user.id),
                current_user=mock_current_user,
            )
        assert exc.value.status_code == 404
        client.assets.list.assert_not_called()

    @pytest.mark.anyio
    async def test_404_when_memory_id_belongs_to_different_user(
        self, mock_current_user
    ):
        other_user = uuid4()
        # Mint an ID for a different user.
        memory_id = encode_memory_id(other_user, 2024, 5, 4)
        client = Mock()
        client.assets.list = Mock()

        with pytest.raises(HTTPException) as exc:
            await get_memory(
                id=memory_id,
                client=client,
                current_user_id=UUID(mock_current_user.id),
                current_user=mock_current_user,
            )
        assert exc.value.status_code == 404
        # Cross-user binding check rejects without ever hitting the backend.
        client.assets.list.assert_not_called()

    @pytest.mark.anyio
    async def test_404_when_year_has_no_assets(self, mock_current_user):
        user_uuid = UUID(mock_current_user.id)
        memory_id = encode_memory_id(user_uuid, 2024, 5, 4)
        client = Mock()
        client.assets.list = Mock(return_value=MockSyncCursorPage([]))

        with pytest.raises(HTTPException) as exc:
            await get_memory(
                id=memory_id,
                client=client,
                current_user_id=user_uuid,
                current_user=mock_current_user,
            )
        assert exc.value.status_code == 404
