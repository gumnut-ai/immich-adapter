"""Pin accounting at the actual cache boundaries without changing cache policy."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from sentry_sdk import metrics

from services.thumbnail_cache import ThumbnailCache, ThumbnailVariant


@pytest.fixture
def observed(monkeypatch):
    count, distribution = Mock(), Mock()
    monkeypatch.setattr(metrics, "count", count)
    monkeypatch.setattr(metrics, "distribution", distribution)
    return count, distribution


def calls(mock, signal):
    return [
        call
        for call in mock.call_args_list
        if call.args[0] == f"thumbnail.cache.{signal}"
    ]


def loader(thumbhash: str | None = "hash"):
    return AsyncMock(
        return_value=ThumbnailVariant(
            "https://cdn.test/private", "image/webp", thumbhash
        )
    )


@pytest.mark.anyio
async def test_aliases_expired_victims_and_lazy_expiry_are_distinct(observed):
    now = 0.0
    cache = ThumbnailCache(30, 2, clock=lambda: now)
    await cache.get("one", "", loader())  # two occupied alias keys
    now = 30.0
    await cache.get("two", "", loader())  # both old keys still stored, but expired
    await cache.get("three", "", loader(None))  # one live victim
    now = 60.0
    await cache.get("two", "hash", loader(None))  # expire just requested alias
    assert [c.args[1] for c in calls(observed[1], "occupied_keys")] == [0, 2, 2, 2]
    assert [c.args[1] for c in calls(observed[1], "utilization")] == [0, 1, 1, 1]
    assert [
        c.kwargs["attributes"]["cache.full"] for c in calls(observed[0], "observation")
    ] == [False, True, True, True]
    removed = calls(observed[0], "removed_keys")
    assert [
        (
            c.args[1],
            c.kwargs["attributes"]["cache.reason"],
            c.kwargs["attributes"].get("cache.victim"),
        )
        for c in removed
    ] == [
        (1, "capacity", "expired"),
        (1, "capacity", "expired"),
        (1, "capacity", "live"),
        (1, "lookup_expiration", None),
    ]
    assert len(cache._entries) == 2  # unrelated lazy-expired key was retained
    for mock in observed:
        for call in mock.call_args_list:
            assert call.kwargs["attributes"]["cache.capacity"] == 2
            assert call.kwargs["attributes"]["cache.ttl_seconds"] == 30
            assert "private" not in str(call.kwargs)
    await cache.close()


@pytest.mark.anyio
async def test_mutation_pre_and_post_clear_count_only_actual_aliases(observed):
    cache = ThumbnailCache(30, 10)
    await cache.get("before", "", loader())
    cache.invalidate()
    await cache.get("during", "", loader())
    cache.invalidate()
    cache.invalidate()  # zero-key clears are not losses
    assert [
        (c.args[1], c.kwargs["attributes"]["cache.reason"])
        for c in calls(observed[0], "removed_keys")
    ] == [(2, "mutation"), (2, "mutation")]
    loaded = loader()
    assert (await cache.get("during", "", loaded)).outcome == "miss"
    cache.evict("during", "", loaded.return_value)
    assert (
        calls(observed[0], "removed_keys")[-1].kwargs["attributes"]["cache.reason"]
        == "cdn_failure"
    )
    assert calls(observed[0], "removed_keys")[-1].args[1] == 2
    await cache.close()


@pytest.mark.anyio
async def test_detached_work_still_counts_toward_overflow_and_cancels(observed):
    cache = ThumbnailCache(30, 10, max_in_flight=1)
    started = asyncio.Event()

    async def blocked():
        started.set()
        await asyncio.Event().wait()
        return loader().return_value

    async with asyncio.timeout(2):
        waiter = asyncio.create_task(cache.get("one", "", blocked))
        await started.wait()
        cache.invalidate()
        assert (await cache.get("two", "", loader())).outcome == "bypass"
        assert calls(observed[1], "in_flight")[-1].args[1] == 1
        assert calls(observed[1], "detached")[-1].args[1] == 1
        assert (
            calls(observed[0], "lookup")[-1].kwargs["attributes"]["cache.bypass_reason"]
            == "in_flight_capacity"
        )
        await cache.close()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    assert [
        c.kwargs["attributes"]["cache.outcome"] for c in calls(observed[0], "load")
    ] == ["cancelled"]


@pytest.mark.anyio
async def test_loader_failures_and_bypasses_keep_separate_denominators(observed):
    cache = ThumbnailCache(30, 10, load_timeout_seconds=0.01)
    with pytest.raises(ValueError):
        await cache.get("error", "", AsyncMock(side_effect=ValueError("private")))

    async def blocked():
        await asyncio.Event().wait()
        return loader().return_value

    with pytest.raises(TimeoutError):
        await cache.get("timeout", "", blocked)
    await cache.get("expired", "", loader(), max_age_seconds=0)
    assert [
        c.kwargs["attributes"]["cache.outcome"] for c in calls(observed[0], "load")
    ] == ["error", "timeout"]
    assert (
        calls(observed[0], "lookup")[-1].kwargs["attributes"]["cache.bypass_reason"]
        == "credential_expired"
    )
    await cache.close()


@pytest.mark.anyio
async def test_metric_failures_cannot_change_cache_results_or_removals(monkeypatch):
    monkeypatch.setattr(
        metrics, "count", Mock(side_effect=ValueError("metrics failed"))
    )
    monkeypatch.setattr(
        metrics, "distribution", Mock(side_effect=ValueError("metrics failed"))
    )
    cache = ThumbnailCache(30, 2)
    load = loader()
    assert (await cache.get("one", "", load)).outcome == "miss"
    assert (await cache.get("one", "hash", load)).outcome == "hit"
    await cache.get("two", "", loader())
    cache.invalidate()
    assert (await cache.get("one", "", load)).outcome == "miss"
    assert load.await_count == 2
    await cache.close()
