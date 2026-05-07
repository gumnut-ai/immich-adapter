"""Helpers for bulk-id endpoints with per-item response contracts.

For Immich endpoints that accept `BulkIdsDto` and must return
`List[BulkIdResponseDto]` with per-id `success` / `error` mapping (e.g. the
album add/remove flows). The trash-style flow — where errors propagate to the
global `GumnutError` handler unmapped — uses the simpler `for chunk in
batched(...)` pattern in-place; see `routers/api/trash.py` and the
"Bulk-ID Endpoints" section of `docs/references/code-practices.md` for the
distinction.

Two helpers live here:

- `chunked_per_item_bulk` — for endpoints whose SDK call accepts a list of
  ids per chunk (`client.albums.assets_associations.add` etc.). Owns the
  chunking loop and per-chunk error mapping.
- `classify_bulk_item_call` — for parallel-fan-out endpoints that call a
  single-item SDK method per input (`client.people.update`,
  `client.albums.assets_associations.add` per album, etc.) under
  `gather_with_concurrency`. Owns the per-item error mapping so call sites
  don't re-roll the `APIStatusError` / `GumnutError` try/except shape.
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass
from enum import Enum
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

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BulkChunkSuccess[T]:
    """Successful per-chunk outcome — carries the SDK response."""

    chunk_uuids: tuple[UUID, ...]
    response: T


@dataclass(frozen=True, slots=True)
class BulkChunkError:
    """Failed per-chunk outcome — carries the classified `Error1`."""

    chunk_uuids: tuple[UUID, ...]
    error: Error1


type BulkChunkOutcome[T] = BulkChunkSuccess[T] | BulkChunkError


async def chunked_per_item_bulk[T](
    asset_uuids: list[UUID],
    sdk_call: Callable[[list[str]], Awaitable[T]],
    *,
    log_context: str,
    log_extra: dict[str, Any],
) -> AsyncIterator[BulkChunkOutcome[T]]:
    """Chunk `asset_uuids` and call `sdk_call` per chunk under `BULK_CHUNK_SIZE`.

    For each chunk: convert uuids to gumnut asset ids, await `sdk_call` with
    the chunked id list, and yield either a `BulkChunkSuccess[T]` or a
    `BulkChunkError`. On `APIStatusError` the chunk yields a
    `BulkChunkError` with `error = classify_bulk_item_error(...)`; on a
    transport-level `GumnutError` it yields a `BulkChunkError` with
    `error = Error1.unknown` after logging via `log_bulk_transport_error`
    (the helper augments `log_extra` with `chunk_size` and `request_size` so
    triage keeps full-request visibility).

    The caller is responsible for composing the final
    `List[BulkIdResponseDto]` from the yielded outcomes — that's where
    response-shape variation between endpoints lives (e.g. `add` accumulates
    `added`/`duplicate`/`not_found` sets, `remove` only needs error vs
    success).
    """
    request_size = len(asset_uuids)
    gumnut_ids = [uuid_to_gumnut_asset_id(u) for u in asset_uuids]
    for chunk in batched(zip(gumnut_ids, asset_uuids), BULK_CHUNK_SIZE):
        chunk_gumnut_ids = [g for g, _ in chunk]
        chunk_uuids = tuple(u for _, u in chunk)
        try:
            response = await sdk_call(chunk_gumnut_ids)
        except APIStatusError as exc:
            yield BulkChunkError(
                chunk_uuids=chunk_uuids,
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
            yield BulkChunkError(
                chunk_uuids=chunk_uuids,
                error=Error1.unknown,
            )
            continue
        yield BulkChunkSuccess(
            chunk_uuids=chunk_uuids,
            response=response,
        )


async def classify_bulk_item_call[E: Enum](
    coro: Coroutine[Any, Any, Any],
    *,
    error_enum: type[E],
    log_context: str,
    log_extra: dict[str, Any],
) -> E | None:
    """Run one per-item bulk SDK coroutine; return None on success or a classified error.

    Mirrors the per-chunk policy in `chunked_per_item_bulk` for endpoints
    that fan out one SDK call per input id under `gather_with_concurrency`
    (e.g. per-person updates, per-album asset adds). On `APIStatusError` the
    caller gets back `classify_bulk_item_error(exc, error_enum)`; on a
    transport-level `GumnutError` the helper logs via
    `log_bulk_transport_error` and returns `error_enum["unknown"]`.

    The result discards the success value — every current call site only
    cares about success-or-error. If a future endpoint needs the response
    payload, switch to a tagged-outcome shape like `BulkChunkOutcome`.
    """
    try:
        await coro
    except APIStatusError as exc:
        return classify_bulk_item_error(exc, error_enum)
    except GumnutError as exc:
        log_bulk_transport_error(
            logger,
            context=log_context,
            exc=exc,
            extra=log_extra,
        )
        return error_enum["unknown"]
    return None
