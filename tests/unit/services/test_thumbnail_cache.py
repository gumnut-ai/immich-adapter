import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
import sentry_sdk
from sentry_sdk.transport import Transport

from services.thumbnail_cache import ThumbnailCache, ThumbnailVariant, opaque_key


@pytest.fixture
def metric_count():
    with patch("services.thumbnail_cache.metrics.count") as count:
        yield count


def lookup_outcomes(count):
    assert all(
        call.args == ("thumbnail.cache.lookup", 1) for call in count.call_args_list
    )
    return [call.kwargs["attributes"]["cache.outcome"] for call in count.call_args_list]


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
async def test_failures_are_not_cached_or_poisoned(metric_count):
    cache = ThumbnailCache(30, 10)
    load = AsyncMock(side_effect=[ValueError("failure"), variant()])
    with pytest.raises(ValueError):
        await cache.get("scope", "", load)
    await asyncio.sleep(0)  # run completion cleanup
    assert (await cache.get("scope", "", load)).outcome == "miss"
    assert load.await_count == 2
    assert lookup_outcomes(metric_count) == ["miss", "miss"]
    await cache.close()


@pytest.mark.anyio
async def test_cancelled_waiter_does_not_cancel_shared_load(metric_count):
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
        assert lookup_outcomes(metric_count) == ["miss", "coalesced"]
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert (await second).outcome == "coalesced"
    assert calls == 1
    assert (await cache.get("scope", "", load)).outcome == "hit"
    assert lookup_outcomes(metric_count) == ["miss", "coalesced", "hit"]
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
async def test_last_waiter_cancellation_does_not_publish_or_poison_next_request(
    metric_count,
):
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
        assert lookup_outcomes(metric_count) == ["miss", "miss"]
    await cache.close()


@pytest.mark.anyio
async def test_failed_shared_load_counts_each_waiter_decision(metric_count):
    cache = ThumbnailCache(30, 10)
    started, release = asyncio.Event(), asyncio.Event()

    async def load():
        started.set()
        await release.wait()
        raise ValueError("failure")

    async with asyncio.timeout(2):
        first = asyncio.create_task(cache.get("scope", "", load))
        await started.wait()
        second = asyncio.create_task(cache.get("scope", "", load))
        await asyncio.sleep(0)
        assert lookup_outcomes(metric_count) == ["miss", "coalesced"]
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, ValueError) for result in results)
    assert lookup_outcomes(metric_count) == ["miss", "coalesced"]
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
async def test_owned_tasks_bounded_even_after_invalidation_and_shutdown_drains(
    metric_count,
):
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
    assert lookup_outcomes(metric_count) == ["miss", "bypass"]


@pytest.mark.anyio
async def test_loader_timeout_is_not_cached(metric_count):
    cache = ThumbnailCache(30, 10, load_timeout_seconds=0.01)

    async def blocked():
        await asyncio.Event().wait()
        return variant()

    with pytest.raises(TimeoutError):
        await cache.get("one", "", blocked)
    assert (
        await cache.get("one", "", AsyncMock(return_value=variant()))
    ).outcome == "miss"
    assert lookup_outcomes(metric_count) == ["miss", "miss"]
    await cache.close()


@pytest.mark.anyio
async def test_disabled_and_expired_credentials_always_load(metric_count):
    load = AsyncMock(return_value=variant())
    disabled = ThumbnailCache(0, 10)
    enabled = ThumbnailCache(30, 10)
    for _ in range(2):
        assert (await disabled.get("scope", "", load)).outcome == "bypass"
        assert (
            await enabled.get("scope", "", load, max_age_seconds=-1)
        ).outcome == "bypass"
    assert load.await_count == 4
    assert lookup_outcomes(metric_count) == ["bypass"] * 4
    assert [
        call.kwargs["attributes"]["cache.enabled"]
        for call in metric_count.call_args_list
    ] == [False, True, False, True]
    assert [
        call.kwargs["attributes"]["cache.ttl_seconds"]
        for call in metric_count.call_args_list
    ] == [0, 30, 0, 30]
    await disabled.close()
    await enabled.close()


@pytest.mark.anyio
async def test_bypass_is_counted_before_failed_loader(metric_count):
    cache = ThumbnailCache(0, 10)

    async def failed_load():
        assert lookup_outcomes(metric_count) == ["bypass"]
        raise ValueError("failure")

    with pytest.raises(ValueError):
        await cache.get("scope", "", failed_load)
    assert lookup_outcomes(metric_count) == ["bypass"]
    await cache.close()


@pytest.mark.anyio
async def test_native_metrics_survive_zero_trace_sampling_without_sensitive_attributes():
    class CaptureTransport(Transport):
        def __init__(self):
            super().__init__()
            self.envelopes = []

        def capture_envelope(self, envelope):
            self.envelopes.append(envelope)

    transport = CaptureTransport()
    client = sentry_sdk.Client(
        dsn="https://public@example.com/1",
        transport=transport,
        default_integrations=False,
        traces_sample_rate=0,
    )
    cache = ThumbnailCache(30, 10)
    sensitive = "credential-library-session-asset-secret"
    try:
        with sentry_sdk.isolation_scope(), sentry_sdk.new_scope() as scope:
            scope.set_client(client)
            scope.set_user({"id": sensitive})
            with sentry_sdk.start_transaction(name="thumbnail", op="http.server") as tx:
                assert tx.sampled is False
                load = AsyncMock(
                    return_value=variant("https://cdn.example.com/?secret")
                )
                await cache.get(sensitive, "cache-buster-secret", load)
                await cache.get(sensitive, "cache-buster-secret", load)
            client.flush()
        items = [
            metric
            for envelope in transport.envelopes
            for item in envelope.items
            if item.type == "trace_metric"
            for metric in item.payload.json["items"]
        ]
        assert len(items) == 2
        assert [item["attributes"]["cache.outcome"]["value"] for item in items] == [
            "miss",
            "hit",
        ]
        for item in items:
            assert item["name"] == "thumbnail.cache.lookup"
            assert item["type"] == "counter"
            assert item["value"] == 1
            assert {
                key: value
                for key, value in item["attributes"].items()
                if key.startswith("cache.")
            } == {
                "cache.outcome": {
                    "value": item["attributes"]["cache.outcome"]["value"],
                    "type": "string",
                },
                "cache.enabled": {"value": True, "type": "boolean"},
                "cache.ttl_seconds": {"value": 30, "type": "integer"},
            }
            assert set(item["attributes"]) - {
                "cache.outcome",
                "cache.enabled",
                "cache.ttl_seconds",
            } <= {
                "sentry.sdk.name",
                "sentry.sdk.version",
                "process.runtime.name",
                "process.runtime.version",
                "server.address",
                "sentry.environment",
                "sentry.release",
            }
        assert "secret" not in json.dumps(items)
        assert not any(
            item.type == "transaction"
            for envelope in transport.envelopes
            for item in envelope.items
        )
    finally:
        await cache.close()
        client.close()


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
