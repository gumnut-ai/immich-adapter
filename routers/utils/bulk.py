"""Helpers for chunked bulk-id endpoints with per-item response contracts.

For Immich endpoints that accept `BulkIdsDto` and must return
`List[BulkIdResponseDto]` with per-id `success` / `error` mapping (e.g. the
album add/remove flows). The trash-style flow — where errors propagate to the
global `GumnutError` handler unmapped — uses the simpler `for chunk in
batched(...)` pattern in-place; see `routers/api/trash.py` and the
"Bulk-ID Endpoints" section of `docs/references/code-practices.md` for the
distinction.
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from itertools import batched
from typing import Any
from uuid import UUID

from gumnut import APIStatusError, GumnutError

from routers.immich_models import Error1
from routers.utils.error_mapping import (
    classify_bulk_item_error,
    log_bulk_transport_error,
)
from routers.utils.gumnut_client import BULK_CHUNK_SIZE
from routers.utils.gumnut_id_conversion import uuid_to_gumnut_asset_id


@dataclass(frozen=True, slots=True)
class BulkChunkOutcome[T]:
    """Per-chunk outcome yielded by `chunked_per_item_bulk`.

    Exactly one of `response` / `error` is set: success yields the SDK
    response, failure yields the classified `Error1`.
    """

    chunk_uuids: tuple[UUID, ...]
    chunk_gumnut_ids: list[str]
    response: T | None
    error: Error1 | None


async def chunked_per_item_bulk[T](
    asset_uuids: list[UUID],
    sdk_call: Callable[[list[str]], Awaitable[T]],
    *,
    log_context: str,
    log_extra: dict[str, Any],
    logger: logging.Logger,
) -> AsyncIterator[BulkChunkOutcome[T]]:
    """Chunk `asset_uuids` and call `sdk_call` per chunk under `BULK_CHUNK_SIZE`.

    For each chunk: convert uuids to gumnut asset ids, await `sdk_call` with
    the chunked id list, and yield a `BulkChunkOutcome`. On `APIStatusError`
    the chunk is yielded with `error = classify_bulk_item_error(...)`; on a
    transport-level `GumnutError` the chunk is yielded with
    `error = Error1.unknown` after logging via `log_bulk_transport_error`
    (the helper augments `log_extra` with `chunk_size` and `request_size` so
    triage keeps full-request visibility).

    The caller is responsible for composing the final
    `List[BulkIdResponseDto]` from the yielded outcomes — that's where
    response-shape variation between endpoints lives (e.g. `add` accumulates
    `added`/`duplicate` sets, `remove` only needs error vs success).
    """
    request_size = len(asset_uuids)
    gumnut_ids = [uuid_to_gumnut_asset_id(u) for u in asset_uuids]
    for chunk in batched(zip(gumnut_ids, asset_uuids), BULK_CHUNK_SIZE):
        chunk_gumnut_ids = [g for g, _ in chunk]
        chunk_uuids = tuple(u for _, u in chunk)
        try:
            response = await sdk_call(chunk_gumnut_ids)
        except APIStatusError as exc:
            yield BulkChunkOutcome(
                chunk_uuids=chunk_uuids,
                chunk_gumnut_ids=chunk_gumnut_ids,
                response=None,
                error=classify_bulk_item_error(exc, Error1),
            )
            continue
        except GumnutError as exc:
            log_bulk_transport_error(
                logger,
                context=log_context,
                exc=exc,
                extra={
                    **log_extra,
                    "chunk_size": len(chunk_uuids),
                    "request_size": request_size,
                },
            )
            yield BulkChunkOutcome(
                chunk_uuids=chunk_uuids,
                chunk_gumnut_ids=chunk_gumnut_ids,
                response=None,
                error=Error1.unknown,
            )
            continue
        yield BulkChunkOutcome(
            chunk_uuids=chunk_uuids,
            chunk_gumnut_ids=chunk_gumnut_ids,
            response=response,
            error=None,
        )
