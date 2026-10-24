---
title: Cache instrumentation and endpoint reuse evaluation
status: active
created: 2026-10-08
last-updated: 2026-10-23
---

# Cache instrumentation and endpoint reuse evaluation

Measure the existing thumbnail cache before increasing its capacity or
extending reuse to other endpoints. Retain the current defaults pending a
usable baseline. This evaluation remains active because effective production
settings, native metric receipt and a matched baseline are still unverified in
the available records; merged implementation does not establish those results.

Two parts have shipped in the repository: request-local `users.me` reuse in
[PR #465](https://github.com/gumnut-ai/immich-adapter/pull/465), and measurement
instrumentation in
[PR #487](https://github.com/gumnut-ai/immich-adapter/pull/487).
Current contracts live in
[Library scope](../architecture/adapter-architecture.md#library-scope),
[cache observability](../references/media-variant-selection.md#cache-observability)
and [API/stream boundaries](../architecture/adapter-architecture.md#native-request-and-stream-metrics).
This record retains the measurement rationale, historical evidence and open
capacity/reuse decisions. It grants no new freshness or authorization window.

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

The inventory identified existing reuse rather than evidence of savings.
SDK cursor walks and retries can make several HTTP attempts per call; high
route volume alone does not demonstrate duplicate reads.

- Library selection already uses stored choices and in-flight sharing;
  request-local current-user reuse now avoids a second `users.me` read for an
  eligible originating request. The live
  [library contract](../architecture/adapter-architecture.md#library-scope)
  owns scope, freshness and waiter isolation.
- [Thumbnail and video metadata reuse](../references/media-variant-selection.md)
  have separate lifetimes and retain selections, not asset DTOs or media bytes.
  Their presence does not authorize reuse by other endpoints.
- [Sync](../architecture/sync-stream-architecture.md) already deduplicates
  current-state entity reads within a pass. Positive face-owner exposability
  can be reused across pages; suppressed or missing owners are reread so edits
  can converge. Event pages and checkpoints must remain live across passes.
- Asset detail, timeline, search, albums/maps/memories, people and stacks have
  different state, membership, include and pagination contracts. Their remaining
  opportunities and constraints are ranked below; the route modules own their
  call inventories. Upload existence checks, post-write hydration, downloads
  and destructive selections cannot inherit a thumbnail selection's freshness
  window. Local compatibility stubs have no upstream result to reuse.

The historical baseline already included
[library single-flight](https://github.com/gumnut-ai/immich-adapter/pull/447),
[stored choice](https://github.com/gumnut-ai/immich-adapter/pull/412), and
[face-owner reuse](https://github.com/gumnut-ai/immich-adapter/pull/459).
Request-local user reuse and video metadata reuse arrived later in PR #465;
neither has measured savings in that baseline.

## Minimal instrumentation proposal

The proposal is implemented by PR #487. Its policy remains measurement only:
no second cache, background sampler or per-key history, and no change to
cache admission, invalidation, cancellation or authorization. The live
[cache telemetry contract](../references/media-variant-selection.md#cache-observability)
owns occupancy, removal, bypass, in-flight and loader signal definitions;
[native request and stream metrics](../architecture/adapter-architecture.md#native-request-and-stream-metrics)
owns HTTP-attempt and completed-stream boundaries.

The decision requires request-weighted occupancy, live versus expired capacity
victims, actual loads versus coalesced waiters, and completed deliveries rather
than response starts. These distinguish pressure from expiry, mutation clears
and failed work without claiming why an absent key missed. Counter values,
not row counts or sampled spans, supply the denominators. Attributes must stay
bounded and exclude credentials, identifiers, cache keys, raw paths, signed
URLs and filenames. Without native process metadata, occupancy describes
observations across workers, not the percentage of workers full.

Implementation and local emission do not prove production receipt. Effective
settings, provider attribute access, retention/drop coverage and a matched
workload remain prerequisites for the decision below.

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
  Pair removal pressure with API attempts, completed thumbnail deliveries,
  end-to-end latency, errors and sync completion; include retries/refreshes.
  Aggregate API attempts include other routes: attempts / completed thumbnails
  is a workload ratio, not causal per-thumbnail cost.

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
Measure RSS/peak memory, live evictions, the API-attempt workload ratio,
latency/errors and sync completion. Do not multiply the entry cap by URL bytes
and call it measured memory: aliases may share a value and Python adds overhead.
Rollback if memory pressure or correctness guardrails regress. Record the
recommendation even when evidence remains too sparse to tune.

## Ranked reuse candidates and contracts

Ranking is by structural opportunity and safety, **not measured savings**.
Rank 1 is implemented; it is retained to record the narrow accepted scope.
Ranks 2–4 remain deferred. Telemetry implementation is merged, while effective
production settings, native receipt and a matched baseline remain unverified.
No broader cache is approved until repeat-read evidence and its freshness
contract exist.

| Rank / candidate | Exact value, scope and lifetime | Freshness, correctness and expected benefit |
|---|---|---|
| 1. Request-local `users.me` reuse — implemented | A successful library-selection user read can be consumed once by the originating request, through [`get_raw_current_user`](../../routers/utils/gumnut_client.py) and [`get_current_user_admin`](../../routers/utils/current_user.py). See [Library scope](../architecture/adapter-architecture.md#library-scope) for the credential and mutation boundaries. | Saves at most one duplicate call in eligible requests; frequency and savings remain unmeasured. No reuse across requests, coalesced waiters or later mutation boundaries, and no cached authorization result. The [reuse tests](../../tests/unit/utils/test_request_user_reuse.py) cover freshness, token refresh, waiter isolation, failure and upload-dependency boundaries. |
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

Verify the deployed measurement version, effective settings and native receipt
under a separately authorized representative workload, then produce a matched
production baseline. This documentation reconciliation changes no capacity,
TTL, concurrency, invalidation or endpoint behavior. Keep request-local user
reuse within its shipped contract; defer candidates 2–4 until bounded
repeat-read evidence demonstrates worthwhile savings. Prefer request-local
reuse when it avoids a new stale-state contract. Any broader authorization or
freshness window and any production capacity comparison require a separate
decision.
