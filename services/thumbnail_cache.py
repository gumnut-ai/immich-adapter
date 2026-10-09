"""Bounded, process-local variant metadata reuse for thumbnails and videos."""

import asyncio
import hashlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

import sentry_sdk
from sentry_sdk import metrics

from config.settings import get_settings

type CacheOutcome = Literal["hit", "miss", "coalesced", "bypass"]


def record_thumbnail_cache_lookup(outcome: CacheOutcome, ttl_seconds: float) -> None:
    """Count admission decisions independently of loader success and trace sampling."""
    metrics.count(
        "thumbnail.cache.lookup",
        1,
        attributes={
            "cache.outcome": outcome,
            "cache.enabled": ttl_seconds > 0,
            "cache.ttl_seconds": ttl_seconds,
        },
    )


def record_thumbnail_cache_refresh(ttl_seconds: float) -> None:
    """Keep a CDN-triggered live retry outside the incoming lookup denominator."""
    metrics.count(
        "thumbnail.cache.refresh",
        1,
        attributes={
            "cache.reason": "cdn_404",
            "cache.enabled": ttl_seconds > 0,
            "cache.ttl_seconds": ttl_seconds,
        },
    )


def record_video_cache_lookup(outcome: CacheOutcome, ttl_seconds: float) -> None:
    metrics.count(
        "video.cache.lookup",
        1,
        attributes={
            "cache.outcome": outcome,
            "cache.enabled": ttl_seconds > 0,
            "cache.ttl_seconds": ttl_seconds,
        },
    )


def record_video_cache_refresh(ttl_seconds: float) -> None:
    metrics.count(
        "video.cache.refresh",
        1,
        attributes={
            "cache.reason": "cdn_404",
            "cache.enabled": ttl_seconds > 0,
            "cache.ttl_seconds": ttl_seconds,
        },
    )


def opaque_key(*parts: str) -> str:
    """Length-prefix components so neither credentials nor identifiers are keys."""
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


@dataclass(frozen=True)
class ThumbnailVariant:
    url: str = field(repr=False)
    mimetype: str
    thumbhash: str | None = None


@dataclass(frozen=True)
class CacheResult:
    variant: ThumbnailVariant
    outcome: CacheOutcome


@dataclass(frozen=True)
class _Entry:
    variant: ThumbnailVariant
    expires_at: float


class ThumbnailCache:
    """Single-event-loop LRU with bounded shared loads and invalidation fencing.

    Waiter cancellation cannot cancel another request's load. Shutdown cancels
    and drains owned loads. Failed loads are never retained. Invalidation also
    detaches old loads so a later request cannot join a pre-mutation snapshot.
    The loader timeout bounds detached work; the task set bounds its count.
    """

    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int,
        max_in_flight: int = 128,
        load_timeout_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
        namespace: Literal["thumbnail", "video"] = "thumbnail",
        cacheable: Callable[[ThumbnailVariant], bool] | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.max_in_flight = max_in_flight
        self.load_timeout_seconds = load_timeout_seconds
        self._clock = clock
        self.namespace = namespace
        self._cacheable = cacheable
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._in_flight: dict[str, asyncio.Task[ThumbnailVariant]] = {}
        self._tasks: set[asyncio.Task[ThumbnailVariant]] = set()
        self._waiters: dict[asyncio.Task[ThumbnailVariant], int] = {}
        self._generation = 0

    def invalidate(self) -> None:
        self._generation += 1
        self._entries.clear()
        self._in_flight.clear()

    def evict(
        self, scope: str, cache_buster: str | None, variant: ThumbnailVariant
    ) -> None:
        """Forget one failed selection and its known aliases, preserving refreshes.

        A CDN failure belongs to the selection already returned to its caller.
        An independently refreshed entry may have replaced any of these keys
        while the CDN request was running, so remove only the same object.
        """
        keys = {
            opaque_key(scope, cache_buster or ""),
            opaque_key(scope, ""),
        }
        if variant.thumbhash:
            keys.add(opaque_key(scope, variant.thumbhash))
        for key in keys:
            entry = self._entries.get(key)
            if entry is not None and entry.variant is variant:
                del self._entries[key]

    async def close(self) -> None:
        self.invalidate()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _store(self, key: str, entry: _Entry) -> None:
        self._entries[key] = entry
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    async def get(
        self,
        scope: str,
        cache_buster: str | None,
        loader: Callable[[], Awaitable[ThumbnailVariant]],
        *,
        max_age_seconds: float | None = None,
    ) -> CacheResult:
        with sentry_sdk.start_span(
            op="cache.get", name=f"{self.namespace}.variant"
        ) as span:

            def record(outcome: CacheOutcome) -> None:
                if self.namespace == "thumbnail":
                    record_thumbnail_cache_lookup(outcome, self.ttl_seconds)
                else:
                    record_video_cache_lookup(outcome, self.ttl_seconds)
                span.set_data("cache.hit", outcome == "hit")
                span.set_data("cache.coalesced", outcome == "coalesced")
                span.set_data("cache.outcome", outcome)

            return await self._get(scope, cache_buster, loader, max_age_seconds, record)

    async def _get(
        self,
        scope: str,
        cache_buster: str | None,
        loader: Callable[[], Awaitable[ThumbnailVariant]],
        max_age_seconds: float | None,
        record: Callable[[CacheOutcome], None],
    ) -> CacheResult:
        ttl = self.ttl_seconds
        if max_age_seconds is not None:
            ttl = min(ttl, max_age_seconds)
        if ttl <= 0:
            record("bypass")
            return CacheResult(await loader(), "bypass")
        key = opaque_key(scope, cache_buster or "")
        now = self._clock()
        entry = self._entries.get(key)
        if entry is not None and entry.expires_at <= now:
            del self._entries[key]
            entry = None
        if entry is not None:
            self._entries.move_to_end(key)
            record("hit")
            return CacheResult(entry.variant, "hit")
        task = self._in_flight.get(key)
        outcome: Literal["miss", "coalesced"] = "coalesced"
        if task is None:
            if len(self._tasks) >= self.max_in_flight:
                record("bypass")
                return CacheResult(await loader(), "bypass")
            outcome = "miss"
            generation = self._generation
            # Lifetime starts before loading: slow requests must not extend the
            # authorization/freshness window or the caller's credential expiry.
            expires_at = now + ttl

            async def load() -> ThumbnailVariant:
                async with asyncio.timeout(self.load_timeout_seconds):
                    variant = await loader()
                if (
                    generation == self._generation
                    and expires_at > self._clock()
                    and (self._cacheable is None or self._cacheable(variant))
                    and len(variant.url.encode())
                    + len(variant.mimetype.encode())
                    + len((variant.thumbhash or "").encode())
                    <= 8192
                ):
                    entry = _Entry(variant, expires_at)
                    self._store(key, entry)
                    # Only a fetched, known thumbhash proves this empty->hash
                    # transition has the same rendering. Arbitrary c values
                    # remain distinct and require an upstream read.
                    if (
                        self.namespace == "thumbnail"
                        and not cache_buster
                        and variant.thumbhash
                    ):
                        self._store(opaque_key(scope, variant.thumbhash), entry)
                return variant

            task = asyncio.create_task(load())
            self._in_flight[key] = task
            self._tasks.add(task)

            def done(finished: asyncio.Task[ThumbnailVariant]) -> None:
                self._tasks.discard(finished)
                if self._in_flight.get(key) is finished:
                    del self._in_flight[key]
                if not finished.cancelled():
                    finished.exception()

            task.add_done_callback(done)
        record(outcome)
        self._waiters[task] = self._waiters.get(task, 0) + 1
        try:
            await asyncio.wait([task])
            return CacheResult(task.result(), outcome)
        finally:
            remaining = self._waiters[task] - 1
            if remaining:
                self._waiters[task] = remaining
            else:
                del self._waiters[task]
                if not task.done():
                    # No client still needs this result: prevent orphan work
                    # and cache publication from an abandoned request.
                    if self._in_flight.get(key) is task:
                        del self._in_flight[key]
                    task.cancel()


_cache: ThumbnailCache | None = None


def get_thumbnail_cache() -> ThumbnailCache:
    global _cache
    if _cache is None:
        settings = get_settings()
        _cache = ThumbnailCache(
            settings.thumbnail_metadata_cache_ttl_seconds,
            settings.thumbnail_metadata_cache_max_entries,
        )
    return _cache


async def close_thumbnail_cache() -> None:
    global _cache
    if _cache is not None:
        await _cache.close()
        _cache = None
