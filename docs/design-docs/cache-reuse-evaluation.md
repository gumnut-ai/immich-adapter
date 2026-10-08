---
title: Cache instrumentation and endpoint reuse evaluation
status: proposed
created: 2026-10-08
last-updated: 2026-10-08
---

# Cache instrumentation and endpoint reuse evaluation

Recommend measuring the existing thumbnail cache before increasing its capacity
or extending reuse to other endpoints. Retain the current defaults pending a
usable baseline. This record proposes measurement work; it changes no runtime
behavior and grants no new freshness or authorization window.

## Evidence and limits

Source baseline: adapter commit `48f350b932ac08c61628c743fa37bc7e276f4009`,
the merged [thumbnail-cache change](https://github.com/gumnut-ai/immich-adapter/pull/462).
Render reported that exact commit live since 2026-10-08 22:58:43 UTC. Effective
production TTL/capacity overrides were not available in the read evidence.
The code defaults are 30 seconds and 2,048 alias keys per process; those are
not a verified production configuration or a measured optimum.

Sentry was queried in the adapter project, production environment, for
2026-10-08 00:00–23:30 UTC. The SDK configuration in
[`init_sentry`](../../config/sentry.py) samples traces at 0.1; provider
extrapolation/dynamic sampling was not independently verified. Below are
**provider-returned sampled/extrapolated counts**, not exact traffic totals.
Durations are p95 of the returned span population, in milliseconds.

| Adapter transaction | Server count | Server p95 | API HTTP-client count | API HTTP-client p95 |
|---|---:|---:|---:|---:|
| `/api/assets` | 1,940 | 2,490 | 3,400 | 1,587 |
| `/api/assets/{id}/thumbnail` | 740 | 4,579 | 1,010 | 2,828 |
| `/api/sync/stream` | 120 | 3,099 | 1,540 | 100 |
| `/api/sync/ack` | 470 | 31 | — | — |

These populations are not paired journeys. HTTP-client counts were filtered to
the API backend host discovered from `server.address`, excluding CDN traffic;
unattributed HTTP spans were not assigned to the API. Server method attributes
were null. Source maps `/api/assets` to upload, but telemetry does not identify
the client/import tool. Streaming transactions may finish after their bodies
start; stream p95 does not prove complete sync latency. The fixed window spans
multiple releases and is a workload signal, not a cache before/after comparison.
Browsing routes outside thumbnails were sparse or absent in this window.
No identical-read frequency or cache savings can be inferred from these totals.

Post-deploy window: 22:58:43–23:30 UTC on the same day. Generic `cache.get`
results included session lookups. Restricting to
`span.description:thumbnail.variant` returned no results. A native metric-row
search for `thumbnail.cache.lookup` after deployment returned no results, and
the search validator rejected `cache.outcome` / `cache.ttl_seconds` attributes.
Neither result proves the SDK failed to emit: no representative post-deploy
thumbnail workload or provider transport/retention coverage was established.
**Remaining prerequisite:** verify effective settings and native lookup/refresh
receipt under an authorized representative workload before reporting hit rate,
capacity adequacy, or production load reduction.

## Existing reuse and route-to-call inventory

This is a route-family inventory of upstream work, not a generated endpoint
catalog. SDK cursor walks and retries can make several HTTP attempts per call.
Local compatibility stubs have no reusable API result. Authenticated routes
also resolve library scope; routes needing a user DTO fetch it lazily once per
request through [`current_user`](../../routers/utils/current_user.py).

| Route family and source | Upstream calls / existing reuse | Evaluation |
|---|---|---|
| Library resolution, [`gumnut_client`](../../routers/utils/gumnut_client.py), [`library_resolver`](../../services/library_resolver.py) | `libraries.list`, `users.me` on bounded recheck; session choice / API-key Redis TTL; process single-flight keyed by credential and fallback choice | Already addresses the burst stampede. Do not introduce another library/auth cache. |
| Thumbnail, [`assets.view_asset`](../../routers/api/assets.py) | Edited rendering: `assets.retrieve(include=variants)`; edit base: `assets.versions.list(include=variants)`; then CDN stream | Existing credential/session/library/asset/variant/edited/cache-buster scope and miss sharing. CDN 404 after a stored hit performs one live refresh. |
| Upload / import, [`assets`](../../routers/api/assets.py) | `assets.check_existence`; buffered SDK create or direct streaming upload; event-emission asset retrieval | Existence and post-write hydration must remain live. Distinct new assets dominate this family; high volume is not repeat-read evidence. |
| Asset detail / statistics / edits, [`assets`](../../routers/api/assets.py) | Asset retrieve/list; stack retrieve and hydration; version-list reads and writes; bulk mutations | Different include/state/version contracts from thumbnail selections. Do not use a cached signed URL as an asset DTO. |
| Timeline, [`timeline`](../../routers/api/timeline.py) | `assets.counts`; filtered cursor walk of `assets.list` | Repeated filters are plausible but not established. Counts and page membership change on uploads, trash, rating, albums and external writes. |
| Search / explore, [`search`](../../routers/api/search.py) | Filtered asset walks, result hydration by IDs, `search.search`, `people.list` | Parameters/include/ordering/page state differ; query identity alone is insufficient. |
| Albums / maps / memories, [`albums`](../../routers/api/albums.py), [`map`](../../routers/api/map.py), [`memories`](../../routers/api/memories.py) | Album list/retrieve and membership mutations; asset walks for markers/memories | Map uses bounded scan/marker limits in [`map_markers`](../../routers/utils/map_markers.py); no shared result cache. Album access checks must stay live. |
| People / faces, [`people`](../../routers/api/people.py), [`faces`](../../routers/api/faces.py) | People list/retrieve/update; thumbnail retrieves person then streams its URL; faces reads owner, face list, unique people | Face response already retrieves each distinct person once per request. Process reuse adds freshness/revocation risk. |
| Stacks, [`stacks`](../../routers/api/stacks.py), [`stack_conversion`](../../routers/utils/stack_conversion.py) | Stack rows plus member asset walks; shared hydration helpers and collection bulk reads | Preserve all-page membership, live-cover selection and `state=all`; sharing across requests needs a membership/version contract. |
| Sync, [`stream`](../../routers/api/sync/stream.py), [`entity_fetch`](../../routers/api/sync/entity_fetch.py) | `events.get`; chunked current-state entity and FK reads; stack validation; membership reads | Pass-local entity deduplication already skips repeat current-state events. Positive face-owner exposability is reused across pages; suppressed/missing owners are reread to converge. Never cache event pages/checkpoints across passes. |
| Downloads / trash, [`download`](../../routers/api/download.py), [`trash`](../../routers/api/trash.py) | Album authorization, asset cursor walks, versions including exact-original selection; live trash enumeration before mutation | Streaming bytes, signed capabilities, exact-original versions and destructive selections remain live. |
| Users / auth / OAuth, [`users`](../../routers/api/users.py), [`auth`](../../routers/api/auth.py), [`oauth`](../../routers/api/oauth.py) | `users.me`, logout, auth URL and exchange; Redis sessions/checkpoints elsewhere | Validation, token refresh, quota and logout are not cache-expansion candidates. |
| Other compatibility surfaces | Local/static responses or unsupported stubs, owned by their route modules | No upstream call to eliminate; adding a cache would add complexity. |

The library-choice changes and sync reuse are present in the baseline, including
[library single-flight](https://github.com/gumnut-ai/immich-adapter/pull/447),
[stored choice](https://github.com/gumnut-ai/immich-adapter/pull/412), and
[face-owner reuse](https://github.com/gumnut-ai/immich-adapter/pull/459).
Their lifetimes and invalidation rules are independent of the thumbnail TTL.

## Minimal instrumentation proposal

Use the existing cache owner and native Sentry metrics, without a background
sampler, tombstone map or second cache. Existing lookup/refresh counters remain
the incoming-decision and refresh denominators.

| Signal | Emission boundary and meaning |
|---|---|
| Occupied keys / configured capacity / utilization | One observation at entry to each thumbnail lookup, before its lazy expiry removal. Count aliases and expired-but-stored keys. Include configured TTL and capacity in all cache series. A saturation counter increments when this observation equals capacity. This measures **request-weighted** full occupancy, not time spent full or live-entry occupancy. |
| Capacity eviction, `live` / `expired` | At `_store` victim removal, classify with the existing monotonic clock and victim expiry. Count alias keys; two evictions can belong to one asset. A live eviction is not proof of a later miss. |
| Expiration / invalidation / CDN removal | Count actual keys removed at lookup expiry, global mutation clear, and CDN-failure eviction separately. Zero-key clears must not masquerade as lost entries. Generation fencing stays intact. |
| Bypass reason and in-flight observations | Separate disabled/context-unavailable/credential-expired/in-flight-capacity paths with bounded enums. Count owned tasks including invalidated detached tasks when measuring overflow; joinable `_in_flight` alone understates resource use. |
| Loader outcomes | Success/error/timeout/cancellation at actual loads, distinct from coalesced waiters. Admission misses include failed loads; do not call them successful deliveries. |
| API HTTP attempts and duration | Add attempt timing/finalization at the existing shared API HTTP client, including each SDK retry and refresh. It currently has only a response hook; preserve its credential-refresh and vanished-library handling. Restrict to the Gumnut API with bounded operation families/status classes, and account for transport exceptions/cancellation without double counting responses. |
| Delivery and sync outcomes | Successful completed thumbnail streams and sync-complete/abort denominators where native unsampled evidence is absent; producing response headers or starting a stream is not completion. |

No new attribute may contain credentials, user/asset/library IDs, cache keys,
raw request paths, signed URLs or filenames. Environment/release come from SDK
context. Use process identity only if native infrastructure metadata supplies
it within a reviewed cardinality budget; do not invent a process label.
Without process metadata, distributions describe observations across workers,
not the percentage of workers full. Prefer distributions for request-weighted
occupancy; a provider's last-value gauge rollup may not preserve that denominator.

An absent key cannot reveal whether an earlier capacity eviction or mutation
caused its miss. Keep it unknown. Removal counts plus hit/bypass/expiration
rates can indicate pressure without keeping per-key history. Every instrument
must preserve loader, cancellation, invalidation and auth behavior. Verify alias
accounting, live/expired victims with an injected clock, overflow/detached work,
retries, failed streams and attribute privacy in the separate build.

## Baseline and capacity decision

First verify the live SHA, safe effective TTL/capacity values, native receipt,
and measurement window. Read only those nonsecret configuration values; do not
dump the environment. The observation must include thumbnail browsing, uploads
with local invalidations, a full sync and an incremental sync. Record client
surface, workload size, duration, workers/restarts, SDK retries and provider
retention/drop coverage. No synthetic production requests or rollout are
authorized by this evaluation.

Reproducible query specifications, using the provider's validated field schema:

- Server workload: `span.op:http.server environment:production` with explicit
  `timestamp:>=START timestamp:<END`; group `transaction`, return `count()` and
  `p95(span.duration)`. This is sampled/extrapolated context only.
- API attempts in existing traces: `span.op:http.client` with the same window
  and environment, filtering `server.address` to the discovered backend host;
  group transaction and release. Exclude CDN and unknown hosts. Do not assume
  the deployment uses the public API hostname internally.
- Thumbnail timing: `span.op:cache.get span.description:thumbnail.variant`,
  same window/environment/release. Generic cache spans include session cache.
- Native metric receipt: filter `metric.name:thumbnail.cache.lookup`, same
  window/environment/release, inspect rows and validated attributes first.
  The operator must confirm schema access before grouping custom fields.
- Once native receipt is verified, sum counter **values**, not metric row
  counts, grouped by outcome/configuration and process where available. Overall
  hit share is `hit / (hit + miss + coalesced + bypass)`; admitted hit share is
  `hit / (hit + miss + coalesced)`. Report coalescing and bypass separately.
  CDN refresh remains outside the incoming lookup denominator.
- Saturation share is full-occupancy observations / all occupancy observations.
  Eviction-live share is live victims / all capacity victims, with raw key
  counts beside it. These have different denominators from incoming requests.
  Pair removal pressure with API attempts per completed thumbnail request,
  end-to-end latency, errors and sync completion; include retries/refreshes.

Lazy expiry inflates stored occupancy. Thumbhash aliases reduce the distinct
asset capacity. Mutation invalidation affects all credential scopes in a
process; sync acknowledgements also clear it through
[`AuthMiddleware`](../../routers/middleware/auth_middleware.py). Request routing,
scale changes and restarts reset/split cache history. Raw span counts cannot
replace independent counter receipt, and native metrics can still be dropped.

Being full alone is insufficient to change capacity. Retain the default if
live capacity eviction is negligible or mutation/expiry/bypass dominates.
If live eviction accompanies reduced reuse and more API attempts, propose a
separately approved reversible comparison at fixed TTL: compare the current
capacity to one bounded larger value with comparable workload and worker count.
Measure RSS/peak memory, live evictions, attempts per completed request,
latency/errors and sync completion. Do not multiply the entry cap by URL bytes
and call it measured memory: aliases may share a value and Python adds overhead.
Rollback if memory pressure or correctness guardrails regress. Record the
recommendation even when evidence remains too sparse to tune.

## Ranked reuse candidates and contracts

Ranking is by structural opportunity and safety, **not measured savings**.
The next bounded implementation is the telemetry proposal above. No broader
cache is approved until repeat-read evidence and its freshness contract exist.

| Rank / candidate | Exact value, scope and lifetime | Freshness, correctness and expected benefit |
|---|---|---|
| 1. Request-local `users.me` reuse during a library recheck | Raw user response, scoped to backend, exact credential/session and request; library binding/fallback choice preserved. Reuse only within that request. Current DTO cache already handles normal dependency repeats. | `_fetch_library_choice` and `get_current_user_admin` can each read `users.me` on a stale request. Saving at most one call in that subset is plausible; measure its frequency first. Never make the shared library-resolution task mutate another request's state; share explicit results only after token-refresh/library-binding semantics are reviewed. No cached validation result or reuse across mutation boundaries. |
| 2. Person thumbnail selection | Successful thumbnail URL/mimetype only, keyed by backend, credential/session, bound library, person, all rendering/cache-buster parameters and relevant person/cover state. Separate value namespace from asset thumbnail metadata, bounded process owner only after approval. | Repeated identical reads are unproven in this window. Person merge, reassignment, cover edits, deletion and external changes need invalidation; revocation/permission lag needs explicit approval. Clamp to credential/capability expiry; never negative-cache errors. Require miss coalescing, bounded tasks, waiter cancellation, loader timeout and live CDN-404 refresh. Existing thumbnail TTL does not authorize it. |
| 3. Timeline counts / repeated browse query | Counts or a specific page, keyed by backend, credential/session/library, every normalized filter, order, include and cursor. Prefer request-local reuse first; process TTL only with a new contract. | Uploads/trash/rating/album membership and external changes alter results. Independent count/page snapshots must not create gaps/duplicates or hide new data. Sparse browsing evidence; reject process caching for now. Bound result size and coalesce only identical safe reads; never cache page iterators. |
| 4. Stack hydration within one read request | Complete member list and cover input for identical stack/state/include/order, within the same backend/credential/library request; bounded by request work. | First demonstrate duplicate hydration not already removed by bulk helpers. A version or membership change invalidates cover assumptions. Keep mutation preconditions and exact-original downloads live; errors and partial pagination cannot become successful cached results. Little observed traffic justifies no build yet. |

Reject cross-pass sync event/checkpoint caching: it can skip changes, break FK
ordering or advance acknowledgements over unobserved work. Existing pass-local
entity/face-owner reuse owns that boundary. Do not cache suppressed face owners
without preserving their deliberate later-page reread and edit convergence.
Reject process-local authorization, library-role, quota, upload-existence and
destructive-selection caches. High upload traffic is a reason to measure the
upstream operation mix, not to reuse mutation results. Request-local or
sync-run proposals still need explicit memory bounds and failure/cancellation
contracts; a narrow lifetime does not excuse an unbounded collection.

## Decision and follow-up boundary

Proceed with a separately tracked measurement build, then verify native receipt
and produce a matched production baseline after authorized deployment. Keep
capacity, TTL, concurrency, invalidation and endpoint behavior unchanged in
that build. Defer the ranked reuse candidates until a bounded repeat-read
measurement demonstrates worthwhile savings; prefer request-local reuse when
it avoids a new stale-state contract. Any broader authorization/freshness window
and any production capacity comparison require a separate decision.
