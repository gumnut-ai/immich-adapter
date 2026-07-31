import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, List
from uuid import UUID, uuid4

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
from routers.utils.asset_conversion import ASSET_INCLUDE, convert_gumnut_asset_to_immich
from routers.utils.current_user import get_current_user, get_current_user_id
from routers.utils.gumnut_client import get_authenticated_gumnut_client


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/memories",
    tags=["memories"],
    responses={404: {"description": "Not found"}},
)


# Synthesized (not persisted) memory IDs — see `encode_memory_id` for layout.
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

    `for_param` carries the user's local calendar date, in either of two wire
    forms: Immich v3.0.3+ web sends `yyyy-MM-dd`, which pydantic parses to
    midnight; earlier clients sent the local wall-clock as a fictitious UTC
    value (the `keepLocalTime` hack — see `docs/references/code-practices.md`
    "Immich web 'today' wire format"). Both carry local y/m/d, so pull the
    components off as-is and apply no timezone math. The parameter stays typed
    `datetime` rather than `date` to keep accepting both forms; narrowing it to
    match the v3.0.3 spec would 422 the older one.

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

    Uses naive `local_datetime` bounds so the Gumnut API compares against each
    asset's wall-clock capture time directly. `local_datetime_after` is
    exclusive (matching `timeline.py`), so the microsecond on the midnight
    boundary is skipped — accepted edge case.

    Returns `[]` if the (year, month, day) tuple isn't a real date — e.g.
    Feb 29 in a non-leap year, which the year-window fan-out hits every leap
    day. Without this, `datetime(...)` raises ValueError and `asyncio.gather`
    fail-fast tanks the whole `/memories` call.

    `limit` is per-page; the explicit break caps total iteration (see
    `docs/references/code-practices.md` "Counts and Aggregates").
    """
    try:
        day_start = datetime(year, month, day)
    except ValueError:
        return []
    day_end = day_start + timedelta(days=1)
    assets: list[AssetResponse] = []
    async for asset in client.assets.list(
        local_datetime_after=day_start.isoformat(),
        local_datetime_before=day_end.isoformat(),
        state="live",
        limit=limit,
        include=ASSET_INCLUDE,
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
        id=encode_memory_id(user_uuid, year, month, day),
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
    """Fan out per-year `assets.list` calls and pair each result with its year.

    `return_exceptions=True` so a transient backend failure on one year (29
    others in flight) yields a degraded result instead of a 500 — memories are
    a soft surface; the carousel renders fine with N-1 years.
    """
    results = await asyncio.gather(
        *(_fetch_assets_for_day(client, y, month, day, limit) for y in years),
        return_exceptions=True,
    )
    paired: list[tuple[int, list[AssetResponse]]] = []
    for year, result in zip(years, results):
        if isinstance(result, Exception):
            logger.warning(
                f"OnThisDay memories fetch failed for {year}-{month:02d}-{day:02d}",
                extra={"year": year, "month": month, "day": day},
                exc_info=result,
            )
            paired.append((year, []))
        elif isinstance(result, BaseException):
            # `asyncio.CancelledError` and other non-Exception BaseExceptions
            # are control-flow signals, not transient backend errors — let them
            # propagate instead of swallowing them as a degraded result.
            raise result
        else:
            paired.append((year, result))
    return paired


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
        id=uuid4(),
        assets=[],
        createdAt=datetime.now(tz=timezone.utc),
        data=OnThisDayDto(year=2024),
        isSaved=False,
        memoryAt=datetime.now(tz=timezone.utc),
        ownerId=current_user_id,
        type=MemoryType.on_this_day,
        updatedAt=datetime.now(tz=timezone.utc),
    )


@router.get("/statistics")
async def memories_statistics(
    for_param: datetime = Query(default=None, alias="for"),
    isSaved: bool = Query(default=None),
    isTrashed: bool = Query(default=None),
    type: MemoryType = Query(default=None),
) -> MemoryStatisticsResponseDto:
    """
    Get memory statistics.
    This is a stub implementation that returns zero total — no upstream
    Immich client (web or mobile) calls this endpoint, so synthesizing a
    real count would burn round-trips for a value nobody reads.
    """
    return MemoryStatisticsResponseDto(total=0)


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
        id=id,
        assets=[],
        createdAt=datetime.now(tz=timezone.utc),
        data=OnThisDayDto(year=2024),
        isSaved=request.isSaved or False,
        memoryAt=request.memoryAt or datetime.now(tz=timezone.utc),
        ownerId=current_user_id,
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
