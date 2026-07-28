"""Tests for routers/utils/concurrency.py."""

import asyncio
import gc

import pytest

from routers.utils.concurrency import (
    BULK_FANOUT_CONCURRENCY_LIMIT,
    gather_with_concurrency,
)


class TestGatherWithConcurrency:
    @pytest.mark.anyio
    async def test_empty_input(self):
        result = await gather_with_concurrency([])
        assert result == []

    @pytest.mark.anyio
    async def test_preserves_input_order_under_jittered_completion(self):
        """Output order tracks input order, not completion order."""

        async def work(idx: int, delay: float) -> int:
            await asyncio.sleep(delay)
            return idx

        # Reverse-correlate delay to index — last input finishes first.
        n = 5
        coros = [work(i, (n - i) * 0.005) for i in range(n)]
        result = await gather_with_concurrency(coros)
        assert result == list(range(n))

    @pytest.mark.anyio
    async def test_caps_concurrent_in_flight_calls(self):
        """No more than `limit` coroutines hold the semaphore at once."""
        active = 0
        peak = 0
        lock = asyncio.Lock()

        async def work() -> None:
            nonlocal active, peak
            async with lock:
                active += 1
                peak = max(peak, active)
            await asyncio.sleep(0.01)
            async with lock:
                active -= 1

        # Schedule far more than the cap so contention is forced.
        n = BULK_FANOUT_CONCURRENCY_LIMIT * 3
        await gather_with_concurrency([work() for _ in range(n)])
        assert peak > 1, "expected concurrent execution"
        assert peak <= BULK_FANOUT_CONCURRENCY_LIMIT

    @pytest.mark.anyio
    async def test_respects_custom_limit(self):
        active = 0
        peak = 0
        lock = asyncio.Lock()

        async def work() -> None:
            nonlocal active, peak
            async with lock:
                active += 1
                peak = max(peak, active)
            await asyncio.sleep(0.005)
            async with lock:
                active -= 1

        custom_limit = 2
        await gather_with_concurrency(
            [work() for _ in range(custom_limit * 4)], limit=custom_limit
        )
        assert peak <= custom_limit

    @pytest.mark.anyio
    async def test_propagates_exception(self):
        """First exception bubbles up (siblings are not cancelled — see
        `gather_with_concurrency`)."""

        async def boom() -> int:
            raise RuntimeError("boom")

        async def slow() -> int:
            await asyncio.sleep(1)
            return 1

        with pytest.raises(RuntimeError, match="boom"):
            await gather_with_concurrency([slow(), boom(), slow()])

    @pytest.mark.anyio
    async def test_cancellation_does_not_warn_unawaited_coroutines(
        self, recwarn: pytest.WarningsRecorder
    ):
        """Cancelled tasks waiting on the semaphore must close their coros.

        With more inputs than the limit, the over-limit coroutines are
        constructed eagerly but their `_run` tasks block on
        `semaphore.acquire()` until a slot frees. Cancelling the *gather*
        — an aborted request, an enclosing timeout — cancels those waiters
        mid-acquire, so their inner coroutines are never awaited and would
        trigger `RuntimeWarning: coroutine was never awaited` when GC'd
        without `_run`'s explicit `close()`.

        Cancelling the gather is the trigger, not a sibling raising: a sibling
        exception propagates without cancelling anything, so every queued
        coroutine still acquires the semaphore and runs. Written that way this
        test passes with the `close()` guard deleted — it has to cancel to
        exercise it.
        """

        async def slow() -> int:
            await asyncio.sleep(5)
            return 1

        task = asyncio.create_task(
            gather_with_concurrency([slow() for _ in range(6)], limit=1)
        )
        # Let the first `_run` acquire the semaphore so the rest are parked
        # mid-`acquire()` — the state the guard exists for.
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The warning fires when an un-awaited coroutine is *finalized*, not
        # when it is orphaned. Yield once so the loop drops its references to
        # the cancelled tasks, then collect — without both steps the orphaned
        # coroutines outlive the assertion and it passes either way.
        await asyncio.sleep(0)
        gc.collect()

        unawaited = [
            w
            for w in recwarn.list
            if issubclass(w.category, RuntimeWarning)
            and "was never awaited" in str(w.message)
        ]
        assert unawaited == [], f"unexpected unawaited-coroutine warnings: {unawaited}"
