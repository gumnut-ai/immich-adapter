import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from gumnut import AsyncGumnut
from gumnut.types.asset_response import AssetResponse
from pydantic.json_schema import SkipJsonSchema

from routers.immich_models import (
    BulkIdResponseDto,
    BulkIdsDto,
    MemoryCreateDto,
    MemoryResponseDto,
    MemoryStatisticsResponseDto,
    MemoryType,
    MemoryUpdateDto,
    OnThisDayDto,
    UserResponseDto,
)
from routers.utils.asset_conversion import convert_gumnut_asset_to_immich
from routers.utils.current_user import get_current_user, get_current_user_id
from routers.utils.gumnut_client import get_authenticated_gumnut_client


router = APIRouter(
    prefix="/api/memories",
    tags=["memories"],
    responses={404: {"description": "Not found"}},
)


# Memory IDs are synthesized rather than persisted. The first 4 bytes of the
# UUID act as a marker so we can recognize and decode our own IDs; the next 4
# bytes encode the (year, month, day) the memory points at; the last 8 bytes
# bind the ID to the user that generated it (low half of the user UUID), so a
# memory ID minted for user A returns 404 when fetched by user B.
_MEMORY_ID_MARKER = b"OTD\x00"
# Read window: synthesize memories for "this day" across the previous 30 years.
# Upstream Immich uses the earliest asset year as the floor, but determining
# that requires an extra round-trip; a fixed window covers practically every
# digital photo library and keeps the request to N parallel asset queries.
_YEAR_WINDOW = 30
# Match upstream `getByDayOfYear`'s lateral limit so each memory ships at most
# 20 thumbnails — the carousel only renders one per memory anyway.
_ASSETS_PER_MEMORY = 20


def encode_memory_id(user_id: UUID, year: int, month: int, day: int) -> UUID:
    """Pack a (user, year, month, day) tuple into a deterministic UUID."""
    user_low = user_id.bytes[8:]
    payload = (
        _MEMORY_ID_MARKER + year.to_bytes(2, "big") + bytes([month, day]) + user_low
    )
    return UUID(bytes=payload)


def decode_memory_id(memory_id: UUID, user_id: UUID) -> tuple[int, int, int] | None:
    """Reverse `encode_memory_id`, returning None if the ID is not ours.

    Returns None when the marker doesn't match, the user binding doesn't match
    the authenticated user, or the encoded date components are obviously out of
    range. The caller maps None → 404.
    """
    raw = memory_id.bytes
    if raw[:4] != _MEMORY_ID_MARKER:
        return None
    if raw[8:] != user_id.bytes[8:]:
        return None
    year = int.from_bytes(raw[4:6], "big")
    month = raw[6]
    day = raw[7]
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    try:
        date(year, month, day)
    except ValueError:
        return None
    return year, month, day


def _local_today(for_param: datetime | None) -> tuple[int, int, int]:
    """Return today's (year, month, day) in the user's local timezone.

    The Immich web client encodes "today" by taking its local wall-clock time
    and pretending it's UTC (`setZone('utc', { keepLocalTime: true })`). The
    wire value's date components are therefore the user's local date, and
    pulling them off the parsed datetime as-is gives us today in their tz —
    no offset arithmetic needed.

    The year matters for the search window: a Sydney user just past midnight
    on Jan 1 sees `for` carrying the new year while the server's UTC clock
    still reads Dec 31 of the prior year. Threading the local year through
    keeps the window aligned with what the user would call "the past 30
    years" instead of slicing off the year just ended.

    When the client omits `for`, fall back to today UTC; the carousel always
    sends `for`, so this branch is only hit by direct API consumers.
    """
    if for_param is None:
        today = datetime.now(tz=timezone.utc)
        return today.year, today.month, today.day
    return for_param.year, for_param.month, for_param.day


def _year_window(reference_year: int) -> list[int]:
    """Years to consider: previous year back to (previous year - window)."""
    start = reference_year - 1
    return list(range(start, start - _YEAR_WINDOW, -1))


async def _fetch_assets_for_day(
    client: AsyncGumnut,
    year: int,
    month: int,
    day: int,
    limit: int,
) -> list[AssetResponse]:
    """Fetch up to `limit` non-trashed assets captured on (year, month, day).

    Uses naive `local_datetime` bounds so the photos-api compares against each
    asset's wall-clock capture time directly (matching what the user would
    intuitively call "the photos from May 4, 2024"), regardless of the device
    timezone the photo was originally captured in.

    Note: SDK `local_datetime_after` is exclusive, so passing the exact
    midnight boundary skips assets captured at exactly 00:00:00.000000 — we
    accept that microsecond edge case for symmetry with `timeline.py`.

    The SDK's `limit` parameter is the per-page size, not a result cap —
    `async for` walks every page until `has_more` is false. We break out
    explicitly so callers actually receive at most `limit` assets; without
    this, `/statistics` (which only needs to know each year is non-empty)
    would burn a round-trip per asset on busy days.
    """
    day_start = datetime(year, month, day)
    day_end = day_start + timedelta(days=1)
    assets: list[AssetResponse] = []
    async for asset in client.assets.list(
        local_datetime_after=day_start.isoformat(),
        local_datetime_before=day_end.isoformat(),
        state="live",
        limit=limit,
    ):
        assets.append(asset)
        if len(assets) >= limit:
            break
    return assets


def _build_memory(
    user_uuid: UUID,
    year: int,
    month: int,
    day: int,
    assets: list[AssetResponse],
    current_user: UserResponseDto,
) -> MemoryResponseDto:
    memory_at = datetime(year, month, day, tzinfo=timezone.utc)
    return MemoryResponseDto(
        id=str(encode_memory_id(user_uuid, year, month, day)),
        assets=[
            convert_gumnut_asset_to_immich(asset, current_user) for asset in assets
        ],
        createdAt=memory_at,
        updatedAt=memory_at,
        memoryAt=memory_at,
        data=OnThisDayDto(year=year),
        isSaved=False,
        ownerId=current_user.id,
        type=MemoryType.on_this_day,
    )


def _filters_exclude_synthetic(
    *,
    is_saved: bool | None,
    is_trashed: bool | None,
    memory_type: MemoryType | None,
) -> bool:
    """True when the requested filters select for memories we never synthesize.

    Synthetic OnThisDay memories are always live (not trashed), unsaved, and of
    type `on_this_day`. Any filter that excludes those guarantees an empty
    result, so we can short-circuit before fanning out asset queries.
    """
    if is_saved is True:
        return True
    if is_trashed is True:
        return True
    if memory_type is not None and memory_type != MemoryType.on_this_day:
        return True
    return False


async def _gather_year_assets(
    client: AsyncGumnut,
    years: list[int],
    month: int,
    day: int,
    limit: int,
) -> list[tuple[int, list[AssetResponse]]]:
    """Fan out per-year `assets.list` calls and pair each result with its year."""
    asset_lists = await asyncio.gather(
        *(_fetch_assets_for_day(client, y, month, day, limit) for y in years)
    )
    return list(zip(years, asset_lists))


@router.get("")
async def search_memories(
    for_param: Annotated[datetime | SkipJsonSchema[None], Query(alias="for")] = None,
    isSaved: Annotated[bool | SkipJsonSchema[None], Query()] = None,
    isTrashed: Annotated[bool | SkipJsonSchema[None], Query()] = None,
    type: Annotated[MemoryType | SkipJsonSchema[None], Query()] = None,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user_id: UUID = Depends(get_current_user_id),
    current_user: UserResponseDto = Depends(get_current_user),
) -> List[MemoryResponseDto]:
    """Synthesize OnThisDay memories for the user across a 30-year window."""
    if _filters_exclude_synthetic(
        is_saved=isSaved, is_trashed=isTrashed, memory_type=type
    ):
        return []

    reference_year, month, day = _local_today(for_param)
    years = _year_window(reference_year)
    year_assets = await _gather_year_assets(
        client, years, month, day, _ASSETS_PER_MEMORY
    )

    return [
        _build_memory(current_user_id, year, month, day, assets, current_user)
        for year, assets in year_assets
        if assets
    ]


@router.post("", status_code=201)
async def create_memory(
    request: MemoryCreateDto, current_user_id: UUID = Depends(get_current_user_id)
) -> MemoryResponseDto:
    """
    Create a new memory.
    This is a stub implementation that returns a fake memory response.
    """
    return MemoryResponseDto(
        id="memory-id",
        assets=[],
        createdAt=datetime.now(tz=timezone.utc),
        data=OnThisDayDto(year=2024),
        isSaved=False,
        memoryAt=datetime.now(tz=timezone.utc),
        ownerId=str(current_user_id),
        type=MemoryType.on_this_day,
        updatedAt=datetime.now(tz=timezone.utc),
    )


@router.get("/statistics")
async def memories_statistics(
    for_param: Annotated[datetime | SkipJsonSchema[None], Query(alias="for")] = None,
    isSaved: Annotated[bool | SkipJsonSchema[None], Query()] = None,
    isTrashed: Annotated[bool | SkipJsonSchema[None], Query()] = None,
    type: Annotated[MemoryType | SkipJsonSchema[None], Query()] = None,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> MemoryStatisticsResponseDto:
    """Count years with at least one matching asset for today's local date."""
    if _filters_exclude_synthetic(
        is_saved=isSaved, is_trashed=isTrashed, memory_type=type
    ):
        return MemoryStatisticsResponseDto(total=0)

    reference_year, month, day = _local_today(for_param)
    years = _year_window(reference_year)
    # Cap at 1 per year — we only need to know whether each year is non-empty.
    year_assets = await _gather_year_assets(client, years, month, day, limit=1)
    return MemoryStatisticsResponseDto(
        total=sum(1 for _, assets in year_assets if assets)
    )


@router.get("/{id}")
async def get_memory(
    id: UUID,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
    current_user_id: UUID = Depends(get_current_user_id),
    current_user: UserResponseDto = Depends(get_current_user),
) -> MemoryResponseDto:
    """Resolve a synthesized memory ID back to its assets."""
    decoded = decode_memory_id(id, current_user_id)
    if decoded is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found"
        )
    year, month, day = decoded
    assets = await _fetch_assets_for_day(client, year, month, day, _ASSETS_PER_MEMORY)
    if not assets:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found"
        )
    return _build_memory(current_user_id, year, month, day, assets, current_user)


@router.put("/{id}")
async def update_memory(
    id: UUID,
    request: MemoryUpdateDto,
    current_user_id: UUID = Depends(get_current_user_id),
) -> MemoryResponseDto:
    """
    Update a memory.
    This is a stub implementation that returns a fake memory response.
    """
    return MemoryResponseDto(
        id=str(id),
        assets=[],
        createdAt=datetime.now(tz=timezone.utc),
        data=OnThisDayDto(year=2024),
        isSaved=request.isSaved or False,
        memoryAt=request.memoryAt or datetime.now(tz=timezone.utc),
        ownerId=str(current_user_id),
        type=MemoryType.on_this_day,
        updatedAt=datetime.now(tz=timezone.utc),
    )


@router.delete("/{id}", status_code=204)
async def delete_memory(id: UUID):
    """
    Delete a memory.
    This is a stub implementation that does not perform any action.
    """
    return


@router.delete("/{id}/assets")
async def remove_memory_assets(
    id: UUID, request: BulkIdsDto
) -> List[BulkIdResponseDto]:
    """
    Get assets for a memory.
    This is a stub implementation that returns an empty list.
    """
    return []


@router.put("/{id}/assets")
async def add_memory_assets(id: UUID, request: BulkIdsDto) -> List[BulkIdResponseDto]:
    """
    Get assets for a memory.
    This is a stub implementation that returns an empty list.
    """
    return []
