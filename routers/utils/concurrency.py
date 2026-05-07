"""Bounded-concurrency parallel fan-out for adapter endpoints.

For endpoints that fan out N parallel SDK calls (one per item / album / face)
where the SDK has no bulk variant. Use this instead of a sequential `for` loop
so a batch of N items doesn't take N round-trips.

Sibling to `chunked_per_item_bulk` in `routers/utils/bulk.py`: that helper is
for SDK methods that already accept a list and need chunking; this helper is
for SDK methods that take one item per call and need parallel scheduling.
"""

import asyncio
from collections.abc import Coroutine, Sequence
from typing import Any

# Cap concurrent in-flight SDK calls per request to keep upstream load bounded
# (an Immich client can request hundreds of items in a single bulk endpoint
# call). Sized to be high enough that small batches finish in one wave but low
# enough that one client can't fan out arbitrarily wide.
BULK_FANOUT_CONCURRENCY_LIMIT = 10


async def gather_with_concurrency[T](
    coros: Sequence[Coroutine[Any, Any, T]],
    *,
    limit: int = BULK_FANOUT_CONCURRENCY_LIMIT,
) -> list[T]:
    """Run coroutines in parallel under a bounded semaphore.

    Output preserves input order regardless of completion order — relied on by
    callers that walk the results in input order (e.g. sticky-first-error
    semantics, or zipping back to the input id list).

    If any coroutine raises, ``asyncio.gather`` cancels pending siblings and
    the exception propagates. Callers that need per-item errors must catch
    inside the coroutine and return a result object — don't rely on this
    helper to surface per-item failures.
    """
    semaphore = asyncio.Semaphore(limit)

    async def _run(coro: Coroutine[Any, Any, T]) -> T:
        # Inputs are already-constructed coroutines, so any coro whose `_run`
        # task is cancelled while waiting on the semaphore would otherwise be
        # GC'd unawaited and trigger `RuntimeWarning: coroutine was never
        # awaited`. Close it explicitly in that window.
        try:
            await semaphore.acquire()
        except BaseException:
            coro.close()
            raise
        try:
            return await coro
        finally:
            semaphore.release()

    return await asyncio.gather(*(_run(coro) for coro in coros))
