---
title: "Immich Adapter Architecture"
last-updated: 2026-07-31
---

# Immich Adapter Architecture

## Overview

The immich-adapter is a Python FastAPI backend that sits between Immich clients (web and mobile apps) and the Gumnut API. Its job is protocol translation: it accepts native Immich API calls and converts them into Gumnut SDK calls, returning Immich-formatted responses.

```
Immich Client (web/mobile)
        │
        ▼
  immich-adapter (FastAPI, port 3001)
  ├── Observability middleware: Sentry `interface` / `user_agent.original`
  ├── Auth middleware: session token → JWT lookup
  ├── Route handlers: translate request → Gumnut SDK call
  ├── WebSocket server: real-time events via Socket.IO
  └── Redis: sessions, checkpoints, encrypted JWTs
        │
        ▼
  Gumnut API (port 8000)
  ├── JWT validation (Clerk)
  ├── Business logic (SQLAlchemy, PostgreSQL + pgvector)
  └── Celery workers (ML, image processing)
```

Immich clients are unmodified — either the original open-source Immich apps or lightly customized forks. The adapter conforms to Immich's OpenAPI spec so clients work without changes. Gumnut may not support the latest Immich client version if it introduces breaking API changes.

### What the adapter does

- **Protocol translation** — Accepts Immich OpenAPI requests, converts to Gumnut SDK calls, returns Immich-formatted responses
- **Session management** — Generates session tokens at login, stores encrypted Gumnut JWTs in Redis
- **Request observability** — Tags Sentry spans with client interface and raw User-Agent, and attributes authenticated requests to the internal `intuser_*` user id
- **Incremental sync** — Manages per-session checkpoints for mobile sync, implements two-phase event ordering
- **WebSocket events** — Distributes real-time upload/delete notifications to connected devices
- **Static file serving** — Serves the Immich web UI

### What it doesn't do

- **JWT validation** — The backend validates JWT claims; the adapter just stores and forwards them
- **OAuth implementation** — OAuth flows are delegated to the backend
- **Authorization** — The backend enforces access control via JWT claims
- **User data storage** — All user data lives in Gumnut; the adapter only stores session metadata in Redis
- **Image processing** — ML inference, thumbnail generation, etc. are handled by Celery workers in the backend

### Single-library assumption

Immich has no concept the adapter can map a Gumnut library onto — `/api/libraries` is a stub returning an empty list, and no Immich request carries a library selector. Every Gumnut listing call the adapter makes therefore goes out **unqualified by `library_id`**: the timeline, search, trash, memories, map markers, stack reads, and the sync stream all rely on the backend inferring the caller's only library.

The Gumnut API makes `library_id` optional for a single-library account and **required** for an account that owns more than one. So the adapter today serves single-library accounts only, and a multi-library account fails on essentially every listing endpoint rather than on any one of them. Closing that gap is a cross-cutting change — pick a library (or fan out across all of them) once, in shared request context, and thread it through every list call — not something an individual route should solve locally. A route that qualified its own calls in isolation would report full results while its neighbours still failed, which is harder to diagnose than a uniform failure.

## Authentication and Session Management

The adapter uses a **session token architecture** that decouples client authentication from backend JWT lifecycle. This is necessary because Immich clients expect stable authentication tokens, while Gumnut JWTs have short lifetimes and refresh frequently.

### Login flow

Both web and mobile clients authenticate via OAuth (Immich's `/api/auth/login` email/password endpoint exists as a stub but is not functional). The flow:

1. Client calls `POST /api/oauth/authorize` → adapter forwards to the Gumnut API → returns Clerk OAuth URL
2. Client opens the OAuth URL in a browser → user authenticates with Clerk
3. Clerk redirects back to the adapter's `POST /api/oauth/callback` with an authorization code
4. Adapter exchanges the code with the Gumnut API for a JWT, generates a UUID session token, encrypts the JWT, stores it in Redis
5. Client receives the session token (via `immich_access_token` cookie for web, `accessToken` in JSON body for mobile)

For mobile, OAuth providers that don't support custom URL schemes (e.g., `app.immich:///`) are handled via `GET /api/oauth/mobile-redirect`, which receives the OAuth response at an HTTPS URL and redirects to the mobile app's custom scheme.

### Request flow

1. Auth middleware extracts the session token from the request (cookie, `Authorization: Bearer`, or `x-immich-user-token` header)
2. Looks up the encrypted JWT in Redis using the session token
3. Decrypts the JWT and creates a Gumnut SDK client authenticated with it
4. Route handler uses the SDK client to make API calls
5. On response, middleware checks for `x-new-access-token` header (backend JWT refresh)
6. If present, updates the stored JWT in Redis — the client's session token remains unchanged — and deletes the header from the outbound response, so the refreshed JWT never reaches the client

**API-key clients (e.g. immich-go).** A request carrying an `x-api-key` header takes a separate, sessionless branch. The header value is a Gumnut API key (`apikey_...`) that the backend validates directly, so the middleware forwards it straight through as the SDK credential — it does **not** look up Redis, and there is no session token. Because there is no session, the JWT-refresh step (5–6) is a no-op for these requests: API keys are long-lived and non-refreshing, and the backend does not emit `x-new-access-token` for them. The `x-api-key` header is checked before the session-token sources above; Immich web (cookie) and mobile (Bearer / `x-immich-user-token`) never send it. Key *management* through the Immich API-keys endpoints remains stubbed — minting a Gumnut key requires a first-party browser session that the adapter's delegated OAuth token can't perform (see `docs/guides/importing-with-immich-go.md`).

### Request observability

`ObservabilityTagsMiddleware` is registered last in `main.py`, so it wraps outermost and can annotate requests even when `AuthMiddleware` returns a 401 before a route handler runs.

- **`interface` tag** — A low-cardinality Sentry tag/span attribute describing the Immich client behind the request: `immich-mobile-ios`, `immich-mobile-android`, or `immich-web`. Classification uses the mobile `deviceType` header first, falls back to `immich-ios` / `immich-android` transfer User-Agents, and treats standard browser User-Agents as web. Unrecognized callers stay unset.
- **`user_agent.original` span attribute** — The raw `User-Agent` header is attached to the active span following the OpenTelemetry semantic convention. Because it is high-cardinality, it is emitted as a span attribute rather than a tag.
- **Authenticated user attribution** — Sentry `user.id` is set to the internal `intuser_*` id; no email or other PII is attached. For session-token clients (Immich web and mobile), `AuthMiddleware` sets it from the session's stored UUID-form id as soon as Redis resolves, so read-heavy routes that never resolve a user DTO (streaming, sync, timeline buckets) are attributed too. `x-api-key` clients (immich-go) carry no session, so they are attributed only when a handler resolves the current user, via the cached `users.me()` response.

### Session storage (Redis)

```
session:{uuid}             → Hash { user_id, device_type, device_os, app_version, ... }
session:{uuid}:checkpoints → Hash { "asset_v1": "opaque_cursor|", "album_v1": "opaque_cursor|", ... }
user:{user_id}:sessions    → Set { session_uuid_1, session_uuid_2, ... }
sessions:by_updated_at     → Sorted Set { session_uuid: timestamp, ... }
```

Session storage is ~3KB per device, enabling horizontal scaling of the adapter.

### Session lifecycle

- **TTL**: Session Redis keys are set to expire based on the underlying JWT's expiry time. When a JWT is refreshed, the TTL is updated. Sessions with no expiry persist until stale cleanup (90+ days inactive).
- **Cookie flags**: `Secure` (protocol-aware — disabled for local HTTP dev), `SameSite=lax`, `Max-Age=400 days` (`COOKIE_MAX_AGE_SECONDS` in `routers/utils/cookies.py`); `ACCESS_TOKEN` and `AUTH_TYPE` are `HttpOnly`, `IS_AUTHENTICATED` is intentionally JS-readable as a frontend-visible flag.
- **Why 400 days**: Without `Max-Age`/`Expires`, browsers and `HTTPCookieStorage` on iOS treat `Set-Cookie` as a session cookie that lives only in memory. iOS drops in-memory cookies when it reaps the backgrounded app, forcing a re-login on every cold launch. The upstream Immich server and the iOS client both encode a 400-day lifetime; the adapter matches so the contract is consistent across the stack. If you ever bound session lifetime, change `COOKIE_MAX_AGE_SECONDS` and the Redis session TTL together.
- **Logout**: Deletes the Redis session key, clears cookies, emits `on_session_delete` WebSocket event to notify connected clients
- **JWT refresh failure**: If the backend cannot refresh an expired JWT, the next request using that session returns 401. The client must re-authenticate via OAuth.

### Wire-compatibility constraints

These bind every adapter change, not just auth work. They follow from the adapter's purpose — being indistinguishable from an Immich server to an unmodified Immich client — so they are not expected to relax:

- **The Immich clients cannot be modified.** Web and mobile are the upstream apps or light forks; nothing may require a client-side change to work.
- **Endpoint signatures cannot change.** Request and response shapes must match Immich's OpenAPI spec, which is why `routers/immich_models.py` is generated from that spec rather than hand-written.
- **No endpoints can be added.** Any Gumnut-specific need has to be expressed through an endpoint Immich already defines.
- **Tokens handed to clients must be long-lived.** The Immich client treats its access token as persistent and has no refresh path, which is the reason the adapter mints a stable session token instead of forwarding the short-lived Gumnut JWT.

When a Gumnut capability has no Immich-shaped home, the outcome is a stub or a translation compromise (see "Endpoint Implementation Status" and the search-filter discussion below), never a protocol extension.

### Cookie security properties

All auth cookies are minted in one place — `set_auth_cookies` in `routers/utils/cookies.py` — so the flags below hold for every path that authenticates a browser. Keep new auth endpoints going through that helper rather than calling `response.set_cookie` directly.

- **`HttpOnly`** on `immich_access_token` and `immich_auth_type`. `immich_is_authenticated` is deliberately JS-readable so the web frontend can branch on it; it carries no credential.
- **`Secure`** is protocol-aware, not hardcoded: the OAuth callback passes `request.url.scheme == "https"`, so deployed HTTPS traffic gets the flag while plain-HTTP local dev still works. It is not a value to "turn on" — it is already on wherever the scheme warrants it.
- **`SameSite=Lax`**, blocking the cookie on cross-site subrequests.
- **`Path=/`** comes from Starlette's default; **no `Domain` is set**, so the cookies are host-only to the adapter's own origin. Adding a parent `Domain` (e.g. a `.gumnut.ai` wildcard) to share the session across subdomains would expose the session token to every host under that domain, so it is a deliberate trade, not a convenience toggle.
- **`Max-Age`** is 400 days (see "Session lifecycle" above). The cookie carries the *session token*, not the Gumnut JWT, so its lifetime is intentionally decoupled from JWT expiry; the Redis session TTL is what tracks the JWT.

### CORS

The adapter registers **no CORS middleware**. It serves the Immich web bundle from its own origin (`static/` mounted at `/` in `main.py`), so browser traffic is same-origin and there is nothing to allow. Any request arriving cross-origin today gets no CORS headers and is blocked by the browser.

If CORS is ever introduced, `allow_credentials=True` must never be paired with `allow_origins=["*"]`. That pairing is not the harmless no-op it looks like: browsers reject a literal `*` alongside credentials, but Starlette's `CORSMiddleware` resolves the combination by echoing the request's own `Origin` back (`allow_all_origins and allow_credentials` → `allow_explicit_origin`), which is a wildcard that actually works. Because the session token rides in a cookie, that would let any origin issue credentialed `fetch()` calls and read authenticated responses. Enumerate exact origins instead.

### Mobile OAuth callback interception (open gap)

The mobile OAuth callback currently lands on `GET /api/oauth/mobile-redirect` — unauthenticated by design, and listed in `AuthMiddleware.UNAUTHENTICATED_PATHS` — which 302s to a **custom URL scheme** (`OAUTH_MOBILE_REDIRECT_URI`, default `app.immich:///oauth-callback`) with the provider's query string, including the authorization `code`, appended.

Custom schemes carry no ownership verification. Registration is first-come-first-served on iOS and ambiguous on Android, where several installed apps may claim the same scheme, so any app on the device can register `app.immich://` and receive the authorization code.

The hardening that removes this is an HTTPS redirect URI covered by **iOS Universal Links / Android App Links**. Their security property is OS-enforced domain verification: iOS honors the link only if an `apple-app-site-association` file naming the app's team ID is served from `/.well-known/` on that domain, and Android only if an `assetlinks.json` there carries the app signing certificate's SHA-256 fingerprint. An attacker who knows the `client_id` still cannot host those files on a domain they do not control, so the OS refuses to route the callback to their app.

**This is not implemented.** The adapter serves no `apple-app-site-association` and no `assetlinks.json` — `routers/well_known.py` exposes only `/.well-known/immich`, and the extracted web bundle's `.well-known/` carries only upstream Immich's `security.txt`. Closing the gap requires publishing the team ID and signing fingerprint of a mobile build, which is a build-and-distribution decision rather than an adapter change: the "clients cannot be modified" constraint above means the adapter cannot vouch for an app whose signing identity it does not own. PKCE reduces the exposure in the meantime — the adapter forwards the client's `codeChallenge` to the backend when requesting the authorization URL — but it does not remove the interception itself.

### OAuth client registration ownership

The Clerk OAuth client credentials live on the **Gumnut API**, not the adapter. The backend holds the client id and secret, builds the authorization URL, and performs the code exchange; the adapter has no client id or secret in `config/settings.py` and reaches those operations through the SDK (`client.oauth.auth_url(...)` and the exchange call in `routers/api/oauth.py`).

There is a standing argument that this is the wrong side of the boundary. `redirect_uri` is the mechanism that ties an authorization code back to a legitimate client, and the adapter's redirect URIs are adapter-specific — the web callback handed to `POST /api/oauth/authorize`, and the `/api/oauth/mobile-redirect` URL that `rewrite_redirect_uri` substitutes for the mobile scheme — while every other Gumnut OAuth client (MCP hosts, dynamically registered clients) has its own id and its own allowlist. Registering Immich as its own OAuth client, owning its id, secret, and redirect-URI allowlist, would make each client's allowlist independently verifiable instead of pooling adapter redirect URIs into a shared backend client. Doing so is a backend-side change; it is recorded here so the reasoning is not rediscovered from scratch.

One product constraint shapes that work: Gumnut's own surfaces (dashboard, photos web, photos mobile) are first-party and should not show an OAuth consent screen — login and you are in — while genuinely third-party integrations should. Immich-on-Gumnut is first-party.

**Related docs:**
- `docs/architecture/session-checkpoint-implementation.md` — Session and checkpoint storage details
- `docs/design-docs/auth-design.md` (deprecated) — historical record of the session-token decision and the alternatives considered

## Data Translation Layer

### ID translation

Gumnut uses prefixed short UUIDs (e.g., `asset_BM3nUmJ6fkBqBADyz5FEiu`), while Immich uses standard UUIDs. The `routers/utils/gumnut_id_conversion.py` module handles bidirectional conversion using the `shortuuid` library.

| Entity | Gumnut prefix | Example |
|--------|---------------|---------|
| Asset | `asset_` | `asset_BM3nUmJ6fkBqBADyz5FEiu` |
| Album | `album_` | `album_K7xFp2mNqRsTvWyZ3aB4cD` |
| Person | `person_` | `person_J5wEn1lMpQrStUxY2zA3bC` |
| Face | `face_` | `face_H4vDm0kLoOnRtTwX1yA2bB` |
| User | `intuser_` | `intuser_G3uCl9jKnNmQsSvW0xZ1aA` |
| Stack (burst) | `asset_stack_` | `asset_stack_F2tBk8iJmMlPrRuV9wY0zZ` |

All Gumnut IDs are encoded using the `shortuuid` library and are deterministically convertible to/from standard UUIDs (e.g., `asset_BM3nUmJ6fkBqBADyz5FEiu` ↔ `550e8400-e29b-41d4-a716-446655440000`). Immich clients always see standard UUIDs.

`asset_` is a strict prefix of `asset_stack_`, so use the `stack` conversion pair for stacks and the `asset` pair for assets — never route one through the other. Crossing them raises rather than silently decoding; see `safe_uuid_from_stack_id` for why.

### Model mapping

Each entity type has a dedicated conversion module in `routers/utils/`:

| Module | Gumnut type | Immich type | Key mappings |
|--------|------------|-------------|--------------|
| `asset_conversion.py` | `AssetResponse` | `AssetResponseDto` | `local_datetime` → `fileCreatedAt`, `mime_type` → `type` (IMAGE/VIDEO/AUDIO/OTHER), EXIF extraction, nested `stack` summary joined from a caller-resolved lookup |
| `album_conversion.py` | `AlbumResponse` | `AlbumResponseDto` | `name` → `albumName`, `album_cover_asset_id` → `albumThumbnailAssetId`, album date range normalization |
| `stack_conversion.py` | stack row + member `AssetResponse`s | `StackResponseDto`, `AssetStackResponseDto`, time-bucket `stack` tuples | member hydration via the `stack_id` asset filter, effective-cover resolution (Immich requires a non-null `primaryAssetId`; an auto-detected burst has no pinned cover), live-only `assets` with the cover first, the batched per-asset stack summaries below, plus the lean collapsed-timeline path (see [Timeline stack collapse](#timeline-stack-collapse)) |
| `person_conversion.py` | `PersonResponse` | `PersonResponseDto` | `is_favorite` → `isFavorite`, `thumbnail_face_url` → `thumbnailPath`, null name → "Unknown Person" |

`album_conversion.py` also routes `start_date` / `end_date` through `to_immich_local_datetime()` before emitting `AlbumResponseDto.startDate` / `endDate`. Album date ranges are derived from assets' `local_datetime`, so they intentionally share the same keep-local-time normalization as each asset's `localDateTime`: naive values are labeled `UTC` without shifting the wall-clock, and `None` passes through unchanged. This keeps the album range aligned with the dates shown on its assets and avoids list-response DTO validation failures when a local capture timezone is unknown.

#### Nested stack summaries on asset responses

Every REST surface that emits an `AssetResponseDto` fills its nested `stack` block (`{id, primaryAssetId, assetCount}`) — asset detail and update, the upload-success WebSocket payload, metadata/smart/random/explore search, and memories. At Immich v3.1.0 the web client reads that block off all of them: `web/src/lib/components/assets/thumbnail/Thumbnail.svelte` renders the burst badge from `stack.assetCount` (and `GalleryViewer` — search results, folders, shared links, the memory grid — leaves `showStackedIcon` at its `true` default), while `web/src/lib/components/asset-viewer/AssetViewer.svelte::refreshStack()` keys off `stack.id` to decide whether to fetch the full stack. The latter is what makes the `/stacks` reads reachable from a shipped client at all. This is deliberately **wider than upstream Immich**, whose `server/src/services/asset.service.ts` passes `withStack: true` only on `GET /assets/{id}` even though its own clients read the block everywhere.

`resolve_asset_stack_summaries` in `routers/utils/stack_conversion.py` owns the resolution for a whole page at once, and `convert_gumnut_asset_to_immich` only joins against the lookup it returns — the converter stays I/O-free, and a route emitting assets in groups (memories) resolves once across the flattened set rather than per group. Stack rows are read in `list_stacks(ids=...)` chunks, so N assets sharing one burst cost one row lookup; only stacks with **no pinned cover** additionally cost a member read, since a pinned row already names its cover. That read stops at the first live member, which is the cover an unpinned burst gets. The reads run under the shared `gather_with_concurrency` bound and a `STACK_SUMMARY_COVER_READ_BUDGET` ceiling — needed because `/search/random` admits up to 1000 assets in one response, so no page size bounds the burst count. Stacks past the budget ship without a summary, logged under `stack_summary_truncated`.

Three cases yield `stack=null` rather than a block: a `stack_id` that resolves to no row (logged once per batch, not per asset); a stack with **fewer than two live members** — zero is the same not-representable rule that makes `/stacks` omit it from the list and 404 it from the detail route, and one would render a burst badge reading "1" on a lone photo, a shape upstream never emits because it deletes a stack that falls below two assets; and a stack read that fails upstream, which **degrades the page instead of failing it**. The live-member rule is decided from the row's count, with only the *zero* half re-decided from the members on the unpinned path: an all-trashed burst therefore survives a stale count, but a count still reading 2 while one frame has just been trashed will badge "2" until the next read. A *pinned* stack skips the member read entirely by design, so a stale-high count there can likewise emit a summary whose detail read 404s (see `resolve_stack_cover` for why that branch trades the read away). That last one matters because the global `GumnutError` handler forwards an upstream status verbatim: a 404 from the stacks lookup would otherwise reach Immich web as "this asset is gone" on `GET /assets/{id}`. It also keeps the resolver from overriding a caller's own posture — `search_memories` deliberately tolerates a failed year. Contrast the `/stacks` routes, where `hydrate_stacks` fails loudly because the stack *is* the response. A stack's *own* members, inside a `StackResponseDto`, deliberately carry `stack=null`, matching upstream's `mapStack`.

### Field naming convention

Gumnut uses `snake_case` (Python convention), Immich uses `camelCase` (TypeScript convention). The conversion functions handle this mapping explicitly rather than using automatic case conversion, since some fields have non-trivial transformations (e.g., `mime_type` → `type` enum, EXIF data extraction).

## Pagination and List Translation

The Gumnut API uses **cursor-based pagination** (`limit` + `starting_after_id`), while Immich clients expect **offset-based pagination** (`page` + `size`). The adapter bridges this gap differently depending on the endpoint.

### Pattern 1: Load-all with client-side pagination

Used when Immich clients expect offset-based pagination or need the full result set for client-side features (e.g., total counts, filtering).

**How it works:**
1. Exhaust the Gumnut SDK's async paginator at the max page size to minimize upstream page fetches: `[p async for p in client.entity.list(limit=GUMNUT_API_MAX_PAGE_SIZE)]`
2. Apply any filters (e.g., `withHidden`)
3. Apply sorting (e.g., people endpoint sorts to match Immich's expected order)
4. Slice for the requested page: `all_items[(page-1)*size : page*size]`
5. Return with `total`, `hasNextPage`, and other metadata

**`GET /api/people` — `total` is a post-filter count, unlike upstream's.** Upstream sources both `total` and `hidden` from a single count that ignores `withHidden`, so it guarantees `total >= hidden`. The adapter instead counts `total` after the hidden filter runs, while `hidden` still counts every hidden person — so `total < hidden` is reachable, and `total - hidden` is only meaningful when no filter ran.

Immich web's readers of `total` all stay correct:

- **People page and people-manage page** — both load with `withHidden=true`, so no filter runs and their counts (`total - hidden` and a plain header count respectively) match upstream.
- **Explore page** — gates its People section on `total > 0` with `withHidden=false`. Here the counts genuinely differ, in the adapter's favor: a fully-hidden people set hides the section, where upstream's unfiltered `total` renders the header over an empty grid.
- **Face editor** — compares `candidates.length` against `total` to stop paging, having built `candidates` from the same filtered response.

Mobile reads only the `people` array. A third-party client assuming upstream's `total >= hidden` invariant would be affected.

**Endpoints using this pattern:**

| Endpoint | SDK call | Client-side logic |
|----------|----------|-------------------|
| `GET /api/people` | `client.people.list(name_filter="all", limit=GUMNUT_API_MAX_PAGE_SIZE)` | Filter hidden → sort (hidden, favorite, named, asset count, alphabetical, created_at) → paginate |
| `GET /api/albums` | `client.albums.list(limit=GUMNUT_API_MAX_PAGE_SIZE)` | Convert all to list, no pagination exposed |
| `GET /api/albums/statistics` | `client.albums.list(limit=GUMNUT_API_MAX_PAGE_SIZE)` | Count total albums from the full set |
| `GET /api/assets/statistics` | `client.assets.list(limit=GUMNUT_API_MAX_PAGE_SIZE)` | Count total/images/videos from full set |

**Performance implications:** Memory usage scales with total entity count, not page size. For a library with 10,000 people, every `GET /api/people` request loads all 10,000 into memory. This is acceptable for current Gumnut library sizes but will need optimization (e.g., server-side sorting support in the Gumnut API) as libraries grow.

### Pattern 2: Server-side cursor pagination

Used when the adapter can leverage the Gumnut API's cursor-based pagination internally, even though the external interface may differ. The Gumnut API has two cursor mechanisms depending on the endpoint:

- **Entity list endpoints** (assets, people, albums): `limit` + `starting_after_id` (cursor is an entity ID)
- **Events endpoint**: `limit` + `after_cursor` (cursor is an opaque position token)

Both support optional time-bound filters (e.g., `local_datetime_before`, `created_at_lt`) that constrain the result set but are not themselves cursors.

**How it works:**
1. Call the Gumnut API with a `limit` and cursor parameter
2. Check `response.has_more` for next page
3. Advance the cursor (last entity ID or returned cursor token) for subsequent pages

**Criterion-less `POST /api/search/metadata`.** immich-go exposes numeric page
requests while the Gumnut API exposes asset cursors. The adapter bridges them
without an exact count: it walks the cursor listing from the beginning until the
requested page, collects that page plus one matching lookahead asset, and stops.
The lookahead determines numeric-string `nextPage`; `count` and deprecated
`total` both contain the returned page length, matching Immich's current search
response. A real `trashedAfter` lower bound is applied before page positions are
counted.

This translation is stateless, with memory bounded to one SDK page plus the
requested page. Page N still walks through the preceding N - 1 pages because a
numeric page cannot encode the Gumnut cursor, but it no longer exhausts the
remaining library just to compute an exact total. A request carrying a real
search criterion continues to use `client.search.search`, which mandates one.

**Camera and place filters are folded into the query, not applied as filters.**
Immich's `make` / `model` / `lensModel` / `city` / `state` / `country` have no
typed equivalent on the Gumnut API, but all six are indexed in its full-text
metadata corpus, so `_compose_free_text_query` appends their values to the
free-text query on both `/search/metadata` and `/search/smart`. Without this a
filters-only request reaches the API with no criterion and 400s — which is what
Immich's Explore and Places pages and its asset detail panel all generate.

The cost is that **query terms are OR-ed, not intersected**, and retrieval is
capped at a fixed candidate count. Adding a term therefore widens the candidate
pool rather than narrowing it: searching "beach" with `make=Canon` against a
library of thousands of Canon photos can crowd the beach matches out. Weigh
that before folding any further Immich filter into the query string — a filter
that is common in the library degrades the caller's own search term. Exact
filtering needs a typed parameter on the Gumnut API.

**Endpoints using this pattern:**

| Endpoint | SDK call | Cursor + filters |
|----------|----------|-----------------|
| `POST /api/search/metadata` (criterion-less) | `client.assets.list(state=…, order=…)` | Rewalk to numeric page, apply `trashedAfter`, collect one lookahead asset |
| `GET /api/timeline/buckets` | `client.assets.counts(group_by="month")` | `starting_after_id` cursor, `local_datetime_before` filter, paginate until `has_more=false` |
| Sync stream (internal) | `client.events.get(...)` | `after_cursor` opaque cursor, `created_at_lt` time bound |

Note: Even with cursor pagination, the timeline buckets endpoint still loads all pages before returning to the client, since Immich expects the complete bucket list.

### Pattern 3: Date-range filtering

Used for timeline bucket contents where the date range is known in advance.

**How it works:**
1. Parse the `timeBucket` parameter to determine month boundaries
2. Query with `local_datetime_after` and `local_datetime_before` as half-open interval `[month_start, next_month_start)`
3. Load all assets within the range

**Endpoints using this pattern:**

| Endpoint | SDK call | Filter mechanism |
|----------|----------|-----------------|
| `GET /api/timeline/bucket` | `client.assets.list(extra_query={local_datetime_after, local_datetime_before})` | Date range from timeBucket param |

### Pattern 4: Single entity fetch

Used for detail endpoints where no pagination is needed.

**Endpoints:** `GET /api/assets/{id}`, `GET /api/people/{id}`, `GET /api/albums/{id}`, etc.

### Pagination constants

- `GUMNUT_API_MAX_PAGE_SIZE` — Used as the `limit` parameter when internally paginating Gumnut API responses.
- `GUMNUT_API_MAX_BULK_IDS` — Used to chunk ID-filtered reads and bulk mutations at the Gumnut API's per-request cap.

Both constants are defined in `routers/api/constants.py`.

### Offset-based pagination limitations

Immich clients use offset-based pagination (`page`/`size`), which is inherently fragile when the underlying data changes between page requests. If an entity is added or removed between page 1 and page 2, clients may see duplicates or skip items. This is a fundamental limitation of the Immich pagination model, not something the adapter can fix — cursor-based pagination (which Gumnut uses internally) avoids this problem by anchoring to a specific item rather than an offset.

## Sorting and Ordering

The adapter must return entities in the order Immich clients expect, which may differ from Gumnut's default ordering.

### People ordering

Gumnut returns people ordered by `created_at DESC` (newest first). Immich clients expect:

1. **Hidden status** — visible people first (`is_hidden ASC`)
2. **Favorite status** — favorites first (`is_favorite DESC`)
3. **Named people first** — non-empty name before empty/null
4. **Asset count descending** — people appearing in more photos first
5. **Name alphabetically** — A-Z within same asset count tier
6. **Creation date ascending** — oldest first as tiebreaker

The adapter applies this sort in memory before pagination slicing (see `_immich_people_sort_key` in `routers/api/people.py`).

### Timeline ordering

Timeline bucket contents are returned in reverse chronological order by default (`local_datetime` descending). The `order` query parameter can reverse this to ascending.

### Timeline stack collapse

Immich renders a burst as a single tile carrying a frame-count badge, and it is the **server** that produces that shape. Under `withStacked`, upstream's bucket query keeps an asset only when the asset is loose or is its stack's primary, and attaches a `[stackId, liveCount]` tuple to the survivor. The web client collapses nothing itself — it reads the tuple, renders the badge, and treats whichever asset carries the tuple as the stack's primary. So the tuple alone is not enough: emitting it without collapsing badges every frame of a burst instead of showing one tile.

`GET /api/timeline/bucket` therefore does both, in `routers/api/timeline.py` over the helpers in `routers/utils/stack_conversion.py`:

1. Collect the distinct `stack_id`s of the bucket's assets and read their rows through `client.stacks.list_stacks(ids=…)`, chunked at `GUMNUT_API_MAX_BULK_IDS`.
2. Resolve each stack's **timeline cover** — the one frame that represents it in the grid.
3. Drop every other member, then build the columnar arrays over the survivors so all of them stay index-aligned.
4. Emit `[stack UUID, live count]` per surviving stacked asset, both elements as strings (upstream emits `count(…)::text` and the client parses element 1 with `Number.parseInt`).

**Both halves are gated on `withStacked`**, matching upstream, which puts its per-asset select and its aggregate inside the same conditional — when the flag is falsy the `stack` key is absent from the response entirely. The gate is load-bearing rather than cosmetic: Immich web's album month view requests buckets *without* `withStacked` while still passing `showStackedIcon` to the thumbnail, so upstream albums are badge-free only because the field is missing. Collapsing or badging unconditionally would drop burst frames from albums and badge them where upstream never does.

**Trash opts out**, diverging from upstream deliberately. Upstream's predicate keys on the live primary, so a trashed non-primary frame is filtered out of the trash view — the one place it must stay visible, since trash is the only route back to restoring it. The Gumnut API's `asset_count` is a live count that would misdescribe a trash-only view besides. No Immich client requests `withStacked` on the trash view, so the divergence costs nothing in practice.

#### Timeline cover vs. effective primary

Both surfaces run the same selection, `resolve_effective_primary`. What differs is the member set they run it over: `/stacks` resolves against every member, the timeline only against live ones (`select_timeline_cover` is that call plus a liveness filter and an ID unwrap). One rule and two inputs, rather than two rules, because a second hand-written copy would let the surfaces drift into showing different frames for the same all-live burst with nothing failing.

The divergence is worth having because collapse is destructive in a way a stack listing is not. `/stacks` naming a trashed pin still lists the live frames beside it; the timeline naming one would drop every frame that is not the cover and erase the burst from the grid. Upstream Immich has that hole — a trashed pin hides its stack's live frames — but it promotes a new primary on permanent deletion, so the state is transient. The Gumnut API preserves a trashed pin until permanent deletion too, which stretches that window to the whole retention period and makes matching upstream here a standing way to lose photos.

The consequence is that the two surfaces disagree about a trashed pin: `/stacks` reports the user's pin, the timeline shows a frame that exists. Nothing on the wire can tell, because the bucket tuple carries no asset ID.

#### Cost, and the bucket-count divergence

Resolution usually costs nothing beyond the row read; `resolve_timeline_stacks` documents the fast path, the two shapes that reach the fallback member read, and the per-request cap on it.

**`GET /api/timeline/buckets` counts are not collapsed.** Upstream applies the identical predicate to its count query so counts and contents agree by construction; the adapter cannot, because `assets.counts` has no stack-aware filter and `assets.list` exposes only a single-stack `stack_id` filter — there is no "belongs to any stack" filter to walk. Computing the per-month deduction would mean paginating every stack in the library plus a member read each, on a hot endpoint.

Counts therefore overstate a month containing bursts. This is cosmetic: the client uses the count for a pre-load month height estimate, which the real layout replaces once the bucket loads. Two things still read the raw count — the scrubber, which snapshots it and refreshes only on a viewport resize, and `getRandomAsset()`, whose shuffle can pick an index past a collapsed month's real contents and no-op for that press. Neither shows a wrong asset. The clean fix is a backend stack-collapse option on both `assets.counts` and `assets.list`, which would return the adapter to a pass-through.

Every failure mode degrades the same way, because degrading to the pre-stack timeline always beats hiding photos behind a cover that may not exist. A stack is left unresolved — frames uncollapsed, tuple null — when its row is missing from the `list_stacks` response (dissolved between the two reads), when its member read fails, when it falls past the read cap, when its ID will not decode to an Immich UUID, or when it has no live members. A failure of the stacks resource as a whole is caught at the route and degrades the entire bucket the same way: every frame kept, every tuple null (the `stack` key is still present, unlike the `withStacked`-falsy path where it is absent), rather than a 5xx on the app's primary view.

The first four cases each log one aggregate record per request rather than one per stack, per *Per-item degradation on hot endpoints* in `docs/references/code-practices.md`. The no-live-members case logs nothing on purpose: in a live bucket it collapses nothing and hides nothing, so there is no signal to raise.

### Album and asset ordering

Albums use Gumnut's default ordering. Criterion-less asset enumeration forwards the Immich request's ascending/descending order to the Gumnut API and does not re-sort the returned assets in the adapter.

## Trash and Deletion Semantics

Immich's trash flow is implemented end to end. The adapter preserves Immich's public wire contract and translates it into the backend's soft-delete primitives.

### Delete and restore flows

- `DELETE /api/assets` soft-deletes by default. `force=true` permanently deletes; `force=false` or an omitted `force` value routes through the trash path.
- `POST /api/trash/restore/assets`, `POST /api/trash/restore`, and `POST /api/trash/empty` are real implementations backed by the backend trash endpoints.
- `GET /api/server/config` surfaces `trashDays` from `TRASH_RETENTION_DAYS`, so the web trash page shows the deployed retention period.

### Returned counts are approximate

`TrashResponseDto.count` is not a count of rows the backend actually transitioned — no backend trash endpoint returns one.

- `POST /api/trash/restore/assets` returns `len(request.ids)`: the upstream restore endpoint answers with an empty acknowledgment body carrying no per-row count, and already-live ids are silently skipped backend-side.
- `POST /api/trash/restore` and `POST /api/trash/empty` return the count of ids enumerated *before* mutating. A concurrent request that transitions some of those ids between enumeration and the chunked mutation makes the true count slightly smaller.

**Enumerate before mutate.** Both restore-all and empty-trash call `_list_trashed_ids` to collect the full trashed id list before issuing any mutation (`routers/api/trash.py`). This is load-bearing, not incidental: the enumeration walks a cursor-paginated `state="trashed"` listing, and mutating mid-walk shrinks the result set out from under the cursor, making resumption ill-defined. The approximate count above is the price of that ordering. The SDK's one-shot `assets.empty_trash` is deliberately unused for the same reason, plus one more — it purges without yielding the id list the flow needs for per-id delete events.

### Trash-aware read paths

- `GET /api/timeline/buckets` and `GET /api/timeline/bucket` pass `state="trashed"` when `isTrashed=true`.
- `GET /api/assets/statistics` does the same for trash-only counts.
- `AssetResponseDto.isTrashed` is sourced from each asset's `trashed_at` field rather than a hardcoded placeholder.

### Sync and realtime propagation

Trash state travels through both client update channels:

- The sync stream emits `SyncAssetV1.deletedAt` from `trashed_at`, and asset hydration uses `state="all"` so `asset_trashed` events do not disappear during fetch.
- Socket.IO emits `on_asset_trash` / `on_asset_restore` with batched id arrays and reserves `on_asset_delete` for permanent deletes.

### Remaining limitations

- `trashDays` is a shared deploy-time environment-variable contract (`TRASH_RETENTION_DAYS`, documented in `.env.example` and the README), not a value the adapter discovers from the backend at runtime. Adapter and backend must be configured with the same number or the web trash page misreports the retention window.
- Trash visibility on the search surface is partial. The criterion-less `POST /api/search/metadata` enumeration path honors `withDeleted` (widen to live + trashed) and `trashedAfter` (trashed-only, with the timestamp bound applied client-side). Criterion-bearing metadata searches route to `client.search.search`, which has no trash selector, and `trashedBefore` is treated as a restricting filter that keeps a request off the enumeration path entirely. `GET /api/search/large-assets` is a full stub, so it surfaces nothing at all.

**Related docs:**
- `docs/design-docs/trash-soft-delete-adapter.md` (deprecated) — historical record of the trash decision and the backend capabilities it depended on

## Sync Protocol

The adapter implements Immich's incremental sync protocol, allowing mobile clients to stay in sync with the backend without re-downloading everything on each app open.

### Two-phase streaming

The sync stream (`/api/sync/stream`) yields events in two phases to prevent FK constraint violations in the mobile client's SQLite database:

1. **Phase 1 — Upserts:** All new/updated entities in FK dependency order (parents before children):
   assets → albums → album_assets → metadata → people → faces

2. **Phase 2 — Deletes:** All deletions in reverse FK order (children before parents):
   faces → album_assets → people → albums → assets

### Checkpoint system

Each session maintains per-entity-type checkpoints in Redis, stored as opaque cursor strings from the Gumnut API events endpoint. The ack string format is `"{entity_type}|{cursor}|"` (e.g., `"asset_v1|eyJ0eXAi...|"`). On the next sync:

1. Adapter captures `snapshot_time = NOW()` as a consistent upper bound
2. For each entity type, queries the Gumnut API events with `after_cursor` (from the last checkpoint) and `created_at_lt` (the snapshot time)
3. The Gumnut API handles ordering and tie-breaking — entities with the same timestamp are ordered by cursor position
4. Streams entities to the client with ack strings
5. Client sends `POST /sync/ack` incrementally during the stream (not at the end), enabling crash recovery — if the client crashes mid-sync, it resumes from the last acknowledged cursor
6. Adapter updates the checkpoint cursor in Redis

**Related docs:**
- `docs/architecture/session-checkpoint-implementation.md` — Checkpoint storage and coordination details
- `docs/design-docs/sync-stream-event-ordering.md` — FK ordering design rationale
- `docs/references/immich-sync-communication.md` — Immich sync protocol message formats

## WebSocket Events

The adapter runs a Socket.IO server for real-time notifications to connected Immich clients.

### Room-based messaging

Each authenticated user joins a room named with their Gumnut user ID. All of a user's devices (phone, tablet, browser) join the same room, so events are broadcast to all connected devices simultaneously.

### Events

| Event | Trigger | Payload |
|-------|---------|---------|
| `on_server_version` | Client connects | Server version info |
| `on_upload_success` | Asset uploaded via adapter (images immediately; videos after the shared 3s WebSocket delay) | Asset ID and metadata |
| `AssetUploadReadyV1` | Asset uploaded via adapter on the same schedule as `on_upload_success` | Compact sync payload (`SyncAssetV1` + `SyncAssetExifV1`) |
| `on_asset_trash` | Assets soft-deleted via adapter | Array of trashed asset IDs |
| `on_asset_restore` | Assets restored via adapter | Array of restored asset IDs |
| `on_asset_delete` | Asset permanently deleted via adapter | Single deleted asset ID |
| `on_session_delete` | Session invalidated | Session ID |

**Related docs:**
- `docs/architecture/websocket-implementation.md` — Socket.IO setup, room management, event handling

## Error Handling

### Error response format

All HTTP errors conform to Immich's expected format:

```json
{
  "message": "Human-readable description",
  "statusCode": 401,
  "error": "Unauthorized"
}
```

Route handlers raise `HTTPException(status_code=..., detail="...")` and a global handler formats the response. Middleware returns `JSONResponse` directly (since `HTTPException` doesn't work in `BaseHTTPMiddleware`).

### Gumnut SDK error mapping

A global `GumnutError` exception handler in `config/exceptions.py` (registered in `main.py`) maps any Stainless SDK exception raised during request handling to an Immich-shaped JSON response. Routes do not need per-call `try/except` for SDK errors — they bubble to the handler. Watch for routes with a legacy catch-all `except Exception` arm (e.g., `finish_oauth`'s 500 fallback): the catch-all swallows SDK errors before the handler sees them, and the local pattern misleads new code into hand-rolling parallel mappings. Add a re-raise arm for the SDK exception (`except BadRequestError: raise`) above the catch-all instead.

Dispatch is by `isinstance` against the typed SDK hierarchy:

| SDK exception | Client status | Detail |
|---------------|---------------|--------|
| `RateLimitError` | 502 | "Upstream temporarily unavailable" |
| `APIStatusError` subclasses (`NotFoundError`, `AuthenticationError`, …) | `exc.status_code` | from `body.detail` / `body.message` / `body.error` / `exc.message` |
| `APIResponseValidationError` | 502 | "Upstream returned invalid response" |
| `APIConnectionError` / `APITimeoutError` | 502 | "Upstream unreachable" |
| generic `GumnutError` | 500 | "Internal error" |

`map_gumnut_error` in `routers/utils/error_mapping.py` is reserved for the upload paths (`_upload_buffered`, `_upload_streaming`), which need to enrich the upstream log record with call-site context (filename, device IDs, `exc_info=True`) that the global handler can't see. `ClientDisconnect` is handled separately: `upload_asset` catches it from both buffered and streaming uploads and returns HTTP 499 Client Closed Request instead of routing that client-aborted request through `map_gumnut_error`.

### Rate limit protection

Immich clients have no HTTP 429 handling — a rate limit response causes sync failures, broken thumbnails, and upload errors with no automatic recovery. The adapter protects against this:

1. The Gumnut SDK (Stainless-generated) has built-in retry with exponential backoff and jitter for 429/5xx responses
2. If SDK retries are exhausted, the global `GumnutError` handler maps `RateLimitError` to **502 Bad Gateway** (not 429) for Immich clients. 502 is semantically correct — the adapter is a gateway and the upstream is unavailable. 503 would imply the adapter itself is overloaded, which isn't the case. (The upload paths' `map_gumnut_error` does the same when called directly.)
3. Immich clients display a generic error on 5xx and do not automatically retry — there is no risk of tight retry loops from the client side
4. Custom retry wrappers must not be added on top of SDK retry (causes retry amplification)

### Bulk error handling

Bulk operations use three implementation shapes according to their Immich response contract and the available Gumnut API operation:

1. **Chunked bulk calls with per-item results** (single-album `add_assets_to_album` / `remove_asset_from_album`). The adapter merges each chunk's response into `BulkIdResponseDto[]`, mapping chunk failures back to the IDs in that chunk.
2. **Chunked bulk mutations with request-level errors** (`delete_assets` and the trash routes). Successful chunks emit their events; validation, transport, and server failures propagate through the global `GumnutError` handler.
3. **Bounded per-item or per-album fan-out** (`update_people` and multi-album `add_assets_to_albums`). Independent entities run under `gather_with_concurrency`. Multi-album adds chunk each album's asset IDs sequentially, preserve successful response bodies that report missing assets, and cap the album-by-chunk call product before starting work.

The per-item and fan-out shapes use `classify_bulk_item_error()` to map `APIStatusError` subclasses to the canonical `not_found` / `no_permission` / `unknown` buckets. Plain chunked mutations intentionally leave errors to the global handler instead of translating them into per-item results.

## Endpoint Implementation Status

The adapter implements a subset of Immich's API surface. Unimplemented endpoints return stub responses (empty lists, 204 No Content, or hardcoded values) so Immich clients don't break.

### Fully implemented

| Area | Endpoints | Notes |
|------|-----------|-------|
| Assets | Upload, download (original + thumbnail), video playback, delete, bulk delete, statistics, single-asset and bulk metadata edit | Streaming downloads via `StreamingResponse`; video playback streams the `original` variant from CDN with Range/seek support; `DELETE /api/assets` soft-deletes by default and permanently deletes when `force=true`; `PUT /api/assets/{id}` forwards `description`, paired `latitude` + `longitude`, and `dateTimeOriginal` to the Gumnut API and emits `on_asset_update`; `PUT /api/assets` forwards the same in-scope fields via `client.assets.bulk_update_assets`, chunking over `GUMNUT_API_MAX_BULK_IDS`. Capture time uses one of three mutually exclusive datetime modes: absolute `dateTimeOriginal` (optionally localized by `timeZone`) replicated as one homogeneous `change`; per-asset `dateTimeRelative` minute-shift (matching Immich, which applies this field as minutes); or standalone `timeZone` reinterpret. The two per-asset modes read each chunk's current `original_datetime` (`assets.list(state="all", ids=...)`, including trashed assets so they aren't silently skipped, matching the homogeneous path) before writing a heterogeneous per-item change, skipping ids with no existing capture time; conflicting datetime modes are rejected with 422. `isFavorite`/`rating`/`visibility` are silently ignored on both paths; `livePhotoVideoId` on the single-asset path and `duplicateId` on the bulk path are silently ignored; the bulk path skips WebSocket emission |
| Trash | Restore-by-ids, restore-all, empty-trash | `trashDays` comes from `TRASH_RETENTION_DAYS`; web and mobile clients see real trash state |
| Albums | CRUD, add/remove assets, statistics | User sharing not supported (returns 501) |
| People | CRUD, list with pagination/sort/filter, thumbnails, statistics, merge | |
| Faces | List, create, delete, reassign | Create draws a user-specified box on an asset and links it to a person (Immich's "create a face on-the-fly" flow) |
| Timeline | Time buckets (monthly), bucket contents | Date-range filtering with timezone handling, including `isTrashed=true`; bucket contents collapse bursts and emit `[stackId, count]` tuples under `withStacked` (see [Timeline stack collapse](#timeline-stack-collapse)) |
| Search | Smart search, metadata search, person search, statistics, random sampling, explore (cities + recents) | Camera/place filters are folded into the query, not filtered on (see above); places, cities, suggestions, and large-assets are stubs |
| Sync | Stream, ack | Two-phase ordering, checkpoint management |
| Auth | OAuth login/callback, logout, session management | Clerk OAuth via the Gumnut API |
| WebSockets | Real-time upload/trash/restore/delete notifications | Socket.IO with room-based messaging |
| Memories (read) | Search, get-by-id, statistics for OnThisDay memories | Synthesized from per-day asset queries; mutations still stubbed |
| Map (markers) | `GET /map/markers` and album-scoped `GET /albums/{id}/map-markers` return GPS-tagged assets | Server-side geotag filter via `client.assets.list(bbox=...)`; the album route also passes the album filter; capped at 2000 markers, with a degraded-path scan bound if the coordinate filter is unavailable; reverse-geocode still stubbed |
| Stacks (read) | `GET /stacks` and `GET /stacks/{id}` return real burst stacks with their live members | Both go through `routers/utils/stack_conversion.py` for member hydration and cover resolution. The list walks the Gumnut API's stack cursor rather than answering with one page, since Immich's `searchStacks` has no pagination; bounded by both a 500-stack cap and a 5000-member hydration budget spent from each row's own asset count, whichever binds first, with walked/budgeted/hydrated/returned counts, a truncation flag, and which bound fired all logged. `primaryAssetId` is answered by resolving the asset's own stack and comparing effective covers, not by forwarding the backend's pinned-cover filter, and an unmatched or unknown ID yields `[]` rather than a 404. A stack with no live members is omitted from the list and 404s from the detail route. Every REST asset response also carries its own nested `stack` summary (see "Nested stack summaries on asset responses"), which is what makes these reads reachable from a client. Writes still stubbed |

### Stub implementations

| Area | Why stubbed |
|------|-------------|
| Libraries | Gumnut has a different library model |
| Tags | Not yet implemented in Gumnut |
| Map (reverse-geocode) | `/map/reverse-geocode` is unused by shipped Immich clients; not wired up |
| Memories (write) | Synthetic memories have no persistence layer; create/update/delete and asset add/remove are no-ops |
| Asset metadata (custom) | Gumnut doesn't support arbitrary key-value metadata |
| Notifications | Push notifications not implemented |
| Partners | User sharing not implemented |
| Duplicates | Duplicate detection handled differently in Gumnut |
| Stacks (write) | Create, update-cover, delete, bulk-delete, and remove-asset still return fake or empty responses; the Gumnut API's stack resource covers all of them, so this is remaining adapter work rather than a backend gap. The read routes and the timeline surface are not stubbed — bucket contents already collapse bursts and carry stack tuples |

## Key Files

| File/Directory | Purpose |
|----------------|---------|
| `routers/api/` | All HTTP route handlers, organized by Immich API domain |
| `routers/api/sync/` | Sync protocol implementation (stream, ack, checkpoint) |
| `routers/api/constants.py` | Shared Gumnut API page-size and bulk-ID limits |
| `routers/middleware/auth_middleware.py` | Session token extraction, JWT lookup, token refresh |
| `routers/middleware/observability_middleware.py` | Request-scoped Sentry tagging (`interface`, `user_agent.original`) |
| `routers/utils/gumnut_id_conversion.py` | Bidirectional Gumnut ↔ Immich ID conversion |
| `routers/utils/asset_conversion.py` | Asset model translation and EXIF extraction |
| `routers/utils/album_conversion.py` | Album model translation |
| `routers/utils/person_conversion.py` | Person model translation |
| `routers/utils/stack_conversion.py` | Burst-stack member hydration, cover resolution, stack DTO building, and collapsed-timeline stack tuples |
| `routers/utils/error_mapping.py` | Gumnut SDK exceptions → HTTPException mapping |
| `routers/immich_models.py` | Pydantic models matching Immich's OpenAPI spec |
| `config/` | Settings, Redis, Sentry configuration |
