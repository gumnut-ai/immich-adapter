import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from services.thumbnail_cache import ThumbnailCache, ThumbnailVariant, opaque_key


def variant(
    url: str = "https://cdn.example.com/thumbnail", thumbhash: str | None = "known-hash"
) -> ThumbnailVariant:
    return ThumbnailVariant(url, "image/webp", thumbhash)


@pytest.mark.anyio
async def test_repeat_waves_alias_known_hash_without_extending_expiry():
    now = [0.0]
    cache = ThumbnailCache(30, 2048, clock=lambda: now[0])
    loads = [AsyncMock(return_value=variant()) for _ in range(100)]
    await asyncio.gather(*(cache.get(str(i), "", load) for i, load in enumerate(loads)))
    now[0] = 13.4
    results = await asyncio.gather(
        *(cache.get(str(i), "known-hash", load) for i, load in enumerate(loads))
    )
    assert all(result.outcome == "hit" for result in results)
    assert sum(load.await_count for load in loads) == 100  # 200 requests, 100 reads
    now[0] = 30.0
    assert (await cache.get("0", "known-hash", loads[0])).outcome == "miss"
    assert loads[0].await_count == 2
    await cache.close()


@pytest.mark.anyio
async def test_distinct_versions_and_scopes_do_not_share():
    cache = ThumbnailCache(30, 20)
    load = AsyncMock(return_value=variant())
    for scope, c in [("user-one", ""), ("user-two", ""), ("user-one", "new-version")]:
        assert (await cache.get(scope, c, load)).outcome == "miss"
    assert load.await_count == 3
    assert (await cache.get("user-one", "known-hash", load)).outcome == "hit"
    await cache.close()


@pytest.mark.anyio
async def test_failures_are_not_cached_or_poisoned():
    cache = ThumbnailCache(30, 10)
    load = AsyncMock(side_effect=[ValueError("failure"), variant()])
    with pytest.raises(ValueError):
        await cache.get("scope", "", load)
    await asyncio.sleep(0)  # run completion cleanup
    assert (await cache.get("scope", "", load)).outcome == "miss"
    assert load.await_count == 2
    await cache.close()


@pytest.mark.anyio
async def test_cancelled_waiter_does_not_cancel_shared_load():
    cache = ThumbnailCache(30, 10)
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return variant()

    async with asyncio.timeout(2):
        first = asyncio.create_task(cache.get("scope", "", load))
        await started.wait()
        second = asyncio.create_task(cache.get("scope", "", load))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert (await second).outcome == "coalesced"
    assert calls == 1
    assert (await cache.get("scope", "", load)).outcome == "hit"
    await cache.close()


@pytest.mark.anyio
async def test_invalidation_fences_running_load_and_new_waiter():
    cache = ThumbnailCache(30, 10)
    started, release = asyncio.Event(), asyncio.Event()

    async def old_load():
        started.set()
        await release.wait()
        return variant("https://cdn.example.com/old")

    async with asyncio.timeout(2):
        first = asyncio.create_task(cache.get("scope", "", old_load))
        await started.wait()
        cache.invalidate()
        new_load = AsyncMock(return_value=variant("https://cdn.example.com/new"))
        fresh = await cache.get("scope", "", new_load)
        assert fresh.outcome == "miss"
        release.set()
        await first
        assert (await cache.get("scope", "", new_load)).variant == fresh.variant
    await cache.close()


@pytest.mark.anyio
async def test_last_waiter_cancellation_does_not_publish_or_poison_next_request():
    cache = ThumbnailCache(30, 10)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def load():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return variant()

    async with asyncio.timeout(2):
        first = asyncio.create_task(cache.get("scope", "", load))
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await cancelled.wait()
        assert (
            await cache.get("scope", "", AsyncMock(return_value=variant()))
        ).outcome == "miss"
    await cache.close()


@pytest.mark.anyio
async def test_lru_capacity_counts_aliases_and_large_entries_bypass_storage():
    cache = ThumbnailCache(30, 2)
    load = AsyncMock(return_value=variant(thumbhash=None))
    for scope in ["one", "two", "one", "three", "two"]:
        await cache.get(scope, "", load)
    assert load.await_count == 4
    large = AsyncMock(return_value=variant("x" * 8193))
    for _ in range(2):
        assert (await cache.get("large", "", large)).outcome == "miss"
    assert large.await_count == 2
    await cache.close()


@pytest.mark.anyio
async def test_owned_tasks_bounded_even_after_invalidation_and_shutdown_drains():
    cache = ThumbnailCache(30, 10, max_in_flight=1)
    started = asyncio.Event()

    async def blocked():
        started.set()
        await asyncio.Event().wait()
        return variant()

    async with asyncio.timeout(2):
        first = asyncio.create_task(cache.get("one", "", blocked))
        await started.wait()
        cache.invalidate()
        assert (
            await cache.get("two", "", AsyncMock(return_value=variant()))
        ).outcome == "bypass"
        await cache.close()
        with pytest.raises(asyncio.CancelledError):
            await first
    assert not cache._tasks


@pytest.mark.anyio
async def test_loader_timeout_is_not_cached():
    cache = ThumbnailCache(30, 10, load_timeout_seconds=0.01)

    async def blocked():
        await asyncio.Event().wait()
        return variant()

    with pytest.raises(TimeoutError):
        await cache.get("one", "", blocked)
    assert (
        await cache.get("one", "", AsyncMock(return_value=variant()))
    ).outcome == "miss"
    await cache.close()


@pytest.mark.anyio
async def test_default_off_and_expired_credentials_always_load():
    load = AsyncMock(return_value=variant())
    disabled = ThumbnailCache(0, 10)
    enabled = ThumbnailCache(30, 10)
    for _ in range(2):
        assert (await disabled.get("scope", "", load)).outcome == "bypass"
        assert (
            await enabled.get("scope", "", load, max_age_seconds=-1)
        ).outcome == "bypass"
    assert load.await_count == 4
    await disabled.close()
    await enabled.close()


def test_key_components_are_unambiguous_and_opaque():
    assert opaque_key("ab", "c") != opaque_key("a", "bc")
    assert len(opaque_key("credential", "session")) == 64
    assert "credential" not in opaque_key("credential", "session")
    assert "secret-url" not in repr(variant("secret-url"))


@pytest.mark.anyio
async def test_targeted_eviction_removes_aliases_and_preserves_other_scopes():
    cache = ThumbnailCache(30, 10)
    selected = variant()
    load = AsyncMock(return_value=selected)
    await cache.get("one", "", load)
    await cache.get("two", "", load)
    cache.evict("one", "known-hash", selected)
    assert (await cache.get("two", "known-hash", load)).outcome == "hit"
    assert (await cache.get("one", "", load)).outcome == "miss"
    cache.evict("one", "", selected)
    assert (await cache.get("one", "known-hash", load)).outcome == "miss"
    await cache.close()


@pytest.mark.anyio
async def test_targeted_eviction_preserves_independently_refreshed_selection():
    now = [0.0]
    cache = ThumbnailCache(30, 10, clock=lambda: now[0])
    old = variant()
    await cache.get("scope", "", AsyncMock(return_value=old))
    now[0] = 30
    refreshed = variant()  # Equal fields, independently loaded object.
    fresh_load = AsyncMock(return_value=refreshed)
    assert (await cache.get("scope", "known-hash", fresh_load)).outcome == "miss"
    cache.evict("scope", "", old)
    result = await cache.get("scope", "known-hash", fresh_load)
    assert result.outcome == "hit"
    assert result.variant is refreshed
    assert fresh_load.await_count == 1
    await cache.close()


@pytest.mark.anyio
async def test_hit_and_requested_key_expiration_do_not_walk_all_entries():
    now = [0.0]
    cache = ThumbnailCache(30, 1000, clock=lambda: now[0])
    load = AsyncMock(return_value=variant(thumbhash=None))
    for index in range(1000):
        await cache.get(str(index), "", load)
    # Spy on the populated mapping to pin lookup work independent of capacity.
    with patch.object(cache, "_entries", wraps=cache._entries) as entries:
        assert (await cache.get("0", "", load)).outcome == "hit"
        now[0] = 30
        assert (await cache.get("0", "", load)).outcome == "miss"
        entries.items.assert_not_called()
        entries.values.assert_not_called()
        entries.__iter__.assert_not_called()
    await cache.close()
