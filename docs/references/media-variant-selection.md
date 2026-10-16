---
title: "Media Variant Selection"
last-updated: 2026-10-15
---

# Media Variant Selection

## Thumbnail variant selection by aspect ratio

Use `routers/api/assets.py::_upgrade_variant_for_aspect` for thumbnail
selection; its docstring and `_LANDSCAPE_SMALL_ASPECT_THRESHOLD` comment own
the aspect-ratio rule, display-space assumptions, and bandwidth rationale.
Keep every upgrade target in both `AssetVariant` and `_VIDEO_IMAGE_VARIANTS`
so videos resolve to the `_image` key. The upgrade happens before the URL
existence check and assumes the upgraded rung exists whenever the thumbnail
does; if that backend guarantee changes, gate on the upgraded key's presence.

## Optional thumbnail metadata cache

`THUMBNAIL_METADATA_CACHE_TTL_SECONDS` defaults to `30` seconds, accepts
`0` through `30`, and controls metadata reuse only for
`GET /api/assets/{id}/thumbnail`. A hit skips the Gumnut API call that normally
checks current backend authorization and asset state. Backend API-key/account
revocation, library membership removal, and edits/trash/delete/restore from other processes or
clients can therefore take up to the configured TTL to reach this route.
This applies to newly keyed adapter requests even if the CDN or native client
already caches media bytes. Setting `0` restores a live API call per request.

The initial 30-second TTL accommodates the observed Immich repeat pattern:
in a captured burst, an empty-`c` thumbnail request was commonly followed by
its thumbhash-keyed repeat about 13–15 seconds later. Thirty seconds provides
headroom for that transition while bounding the authorization and external
asset-change window. It is an engineering starting point, not a measured
optimum or a claim about all clients. Compare cache outcomes and upstream
traffic before tuning it; the maximum keeps that freshness window bounded.

The authentication middleware still loads and checks the Redis session on
every request, including hits, so adapter session deletion blocks the next
request. Session JWTs with missing, malformed, or expired `exp` bypass the
cache; expiry parsing is only a cache denial gate, never signature validation.
The Gumnut API validates credentials on misses. Entries expire before the
session JWT's reported expiry. Credential refresh, session changes, and library
switches change the isolated cache scope. An explicitly selected-library API
key can use the cache once the adapter resolves and binds that library; a
restricted key without a resolved library stays on the live path. See
[Library scope](../architecture/adapter-architecture.md#library-scope) for the
selection and eligibility rules.

`services/thumbnail_cache.py::ThumbnailCache` stores only a selected URL, MIME
type, and optional known thumbhash. Keys hash the backend URL, exact credential,
session, library, asset, requested size, edited mode, and `c` query value; no
keys, credentials, URLs, or identifiers are emitted by cache telemetry. A
successful empty-`c` current-rendering read also primes the fetched thumbhash
key with the same expiration. An unknown nonempty `c` uses a separate key and
fetches fresh metadata. Edit-base reads have no thumbhash in their API
response, so they reuse only the exact `c`. Variant selection, CDN streaming, and forwarded
cache headers retain their existing behavior.

The cache is process-local and clears on restart. It uses no Redis media
payloads or new dependency. `THUMBNAIL_METADATA_CACHE_MAX_ENTRIES` defaults to
`2048` and accepts `1` through `10000`; aliases count as entries. Each retained
entry is capped at 8 KiB of strings. A lookup expires only its requested key;
capacity evicts the least recently used entries, including expired entries
that have not been accessed again. At most 128 loads are owned
and coalesced at once, each with a 60-second timeout; excess requests use the
existing live path. Errors are never stored. Cancellation propagates to the
request while another waiter can finish the shared load. Cancelling the last
waiter cancels its unfinished load. Shutdown cancels and drains owned loads
before closing upstream HTTP clients.

The authentication middleware invalidates the local cache before and after
protected mutations, including failures after a backend commit. Generation
fencing prevents a pre-mutation load from repopulating it or serving a later
request. This conservative invalidation also covers read-like POST routes.
Concurrent requests already reading or holding old metadata can still finish.
Other adapter workers and external writes rely on TTL expiration. A CDN 404
evicts only the failed selection and its known aliases; unrelated entries and
independently refreshed selections stay warm. An actual cache hit retries
metadata once through the live API. Upstream metadata failures and failures
on a fresh CDN lookup are not retried by this cache.

## Video playback metadata cache

Video metadata reuse is always enabled with a fixed five-second lifetime,
defined by `services/video_cache.py::VIDEO_METADATA_CACHE_TTL_SECONDS`,
and applies only to `GET /api/assets/{id}/video/playback`.
It reuses the current original-video URL and MIME type across short
Range bursts. Each request still opens a separate CDN stream with its own
Range header, status, and response headers. Original downloads and edit-base
reads remain live. This does not cache media bytes or partial responses.

Reuse delays Gumnut API authorization and external asset-state checks
by up to five seconds, including API-key/account revocation, membership
removal, and edits or deletion outside this process. Redis session checks
remain live on every request. The same credential, session, library, JWT-expiry,
load bounds, cancellation, mutation fencing, and shutdown rules as the
thumbnail cache apply, in a separate cache without thumbhash aliases.
`VIDEO_METADATA_CACHE_MAX_ENTRIES` defaults to `256` and accepts `1` through
`10000`. The five-second lifetime bounds freshness; production request logs
suggest short repeat gaps, but do not establish a hit rate or an optimal TTL.

Only video selections using the known `https://assets.gumnut.ai` capability
format are retained: a single 43-character base64url `verify` signature and
optional `dl` parameter. This CDN contract uses an HMAC over the versioned
object path and has no time expiry. Format checking does not validate the
signature; the live API authorizes the selection and the CDN verifies each
stream. Unknown hosts, unsigned URLs, and additional signing parameters are
not retained because their expiry cannot be bounded here. They can still
share a fresh in-flight API load. A stored hit that encounters a CDN 404
evicts that selection and retries live metadata once with the same Range;
fresh failures, 403s, and other CDN errors propagate without a cache retry.

### Cache observability

The Sentry counter `thumbnail.cache.lookup` emits once per thumbnail metadata
lookup decision, before loading or waiting. Its `cache.outcome` attribute is
`hit`, `miss`, `coalesced`, or `bypass`; `cache.enabled` indicates whether the
configured TTL is positive, and `cache.ttl_seconds` records that TTL. Bypasses
carry `cache.bypass_reason`: `disabled`, `context_unavailable` (including
unknown/malformed credential expiry), `credential_expired`, or
`in_flight_capacity`. All thumbnail cache series include `cache.capacity`
and the configured TTL/enabled attributes. Failures and cancellations retain their
decision count. Requests rejected by authentication before a lookup are outside
this denominator. No credentials, asset identifiers, cache keys, or URLs are
added as metric attributes.

For a consistent environment, release, time window, and TTL, sum the counter
grouped by `cache.outcome`. Calculate overall hit rate as
`hit / (hit + miss + coalesced + bypass)`. For the subset admitted to caching,
use `hit / (hit + miss + coalesced)`, and report bypass share separately.
Coalesced requests share an existing load rather than read a stored entry;
report their share separately instead of classifying them as hits. These are
lookup decisions, not successful thumbnail deliveries.

`thumbnail.cache.refresh` separately counts live metadata retries after an
actual cache hit encounters a CDN 404, with `cache.reason=cdn_404` and the same
configured TTL attributes. It emits before the retry, including failed retries,
and does not add another incoming lookup. Compare both counters with upstream
request counts and latency to measure load reduction; SDK retries, failures,
worker routing, and mutation traffic affect that comparison.

`thumbnail.cache.occupied_keys` and `thumbnail.cache.utilization` are native
distributions observed once at each incoming lookup, **before** lazy expiry.
Aliases and expired-but-stored keys count toward occupancy. The corresponding
`thumbnail.cache.observation` counter carries `cache.full=true/false`;
sum its values for `true` divided by the total measures request-weighted
saturation. This is neither time spent full nor live-entry occupancy, and it
cannot establish the percentage of workers full. There is no background
sampler. `thumbnail.cache.in_flight` observes all owned loads, including
invalidated/detached loads; `thumbnail.cache.detached` observes the subset no
longer joinable. Overflow bypass loads are outside that owned-task limit.

`thumbnail.cache.removed_keys` counts actual alias keys removed, grouped by
`cache.reason`: `capacity`, `lookup_expiration`, `mutation`, `cdn_failure`, or
`shutdown`. Capacity victims also carry `cache.victim=live/expired`, classified
with the cache clock at eviction. Conservative pre/post mutation clears count
only keys present on each side, including read-like POSTs; empty clears emit
no removal. CDN failures remove only matching selections and aliases. These
are key counts, not distinct assets or requests. A live eviction does not
prove a later miss was caused by pressure: absent-key causes remain unknown,
with no per-key history retained.

`thumbnail.cache.load` counts owned loader invocations by
`cache.outcome=success/error/timeout/cancelled`. Coalesced waiters and bypass
loads do not add loader counts; successful loads need not be retained or
delivered. Use [native request and stream metrics](../architecture/adapter-architecture.md#native-request-and-stream-metrics)
for actual API attempts and completed-stream denominators.

Native metrics are independent of transaction trace sampling. The
`thumbnail.variant` span still reports `cache.hit`, `cache.coalesced`, and
`cache.outcome` for timing analysis, including failed or cancelled loads, but
sampled spans are not the hit-rate denominator. Confirm metric receipt and
retention in the deployed Sentry project before relying on production ratios;
transport failures or provider limits can drop telemetry.

Sum counter **values**, never metric rows. Compare matching environment,
release, TTL/capacity, workload and worker/restart cohorts. Verify native
receipt, attribute access, retention and drop coverage in the Sentry project
before reporting ratios; local emission does not establish provider receipt.
Environment/release and any infrastructure process identity come from native
SDK context; no per-process identifier is added here. Occupancy distributions
aggregate request observations across workers when process metadata is absent.
Keep capacity at 2048 until live evictions coincide with reduced reuse and more
API traffic. A capacity comparison requires separate approval at fixed TTL,
with measured memory, errors, latency and sync-completion guardrails.

Video reuse emits the corresponding `video.cache.lookup`, `video.cache.refresh`,
and `video.variant` names with the same outcome and TTL attributes. Analyze
video and thumbnail populations separately. These counters measure lookup
decisions rather than successful playback, and retained-cache effectiveness
still requires a production observation window after deployment.
