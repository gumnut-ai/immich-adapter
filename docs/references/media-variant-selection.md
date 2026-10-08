---
title: "Media Variant Selection"
last-updated: 2026-10-08
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

`THUMBNAIL_METADATA_CACHE_TTL_SECONDS` defaults to `0` (disabled), accepts
`0` through `30`, and controls metadata reuse only for
`GET /api/assets/{id}/thumbnail`. Enabling `30` requires an explicit deployment
decision: a hit skips the Gumnut API call that normally checks current backend
authorization and asset state. Backend API-key/account revocation, library
membership removal, and edits/trash/delete/restore from other processes or
clients can therefore take up to the configured TTL to reach this route.
This applies to newly keyed adapter requests even if the CDN or native client
already caches media bytes. Setting `0` restores a live API call per request.

The authentication middleware still loads and checks the Redis session on
every request, including hits, so adapter session deletion blocks the next
request. Session JWTs with missing, malformed, or expired `exp` bypass the
cache; expiry parsing is only a cache denial gate, never signature validation.
The Gumnut API validates credentials on misses. Entries expire before the
session JWT's reported expiry. Credential refresh, session changes, and library
switches change the isolated cache scope. Restricted API keys without a
resolved library stay on the live path.

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

Sentry's `thumbnail.variant` span reports `cache.hit`, `cache.coalesced`, and
`cache.outcome` (`hit`, `miss`, `coalesced`, or `bypass`). Compare these with
upstream request counts when evaluating an explicitly enabled deployment;
process-local hit rates depend on worker routing and mutation traffic.
