"""Tests for routers/utils/concurrency.py."""

import asyncio

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
        """First exception bubbles up; pending siblings are cancelled."""

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
        `semaphore.acquire()` until a slot frees. If a running task raises
        first, `asyncio.gather` cancels the waiters mid-acquire — the inner
        coroutines were never awaited and would otherwise trigger
        `RuntimeWarning: coroutine was never awaited` when GC'd.
        """

        async def boom() -> int:
            raise RuntimeError("boom")

        async def never_runs() -> int:  # pragma: no cover — cancelled before entry
            return 1

        with pytest.raises(RuntimeError, match="boom"):
            await gather_with_concurrency(
                [boom()] + [never_runs() for _ in range(5)],
                limit=1,
            )

        unawaited = [
            w
            for w in recwarn.list
            if issubclass(w.category, RuntimeWarning)
            and "was never awaited" in str(w.message)
        ]
        assert unawaited == [], f"unexpected unawaited-coroutine warnings: {unawaited}"
