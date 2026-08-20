---
title: "Pagination, Bulk Operations, and Concurrency"
last-updated: 2026-08-19
---

# Pagination, Bulk Operations, and Concurrency

## Forwarding pagination parameters

When forwarding pagination params (`size`, `page`, `limit`) from an Immich request to a Gumnut SDK call, forward only what the client provided — don't substitute an adapter-side default. The SDK uses an `Omit` sentinel; just leave the kwarg out of the call so the Gumnut API applies its own default:

```python
from routers.api.constants import GUMNUT_API_MAX_PAGE_SIZE

search_kwargs: dict[str, Any] = {"query": request.description, ...}
if request.size is not None:
    # Clamp at the Gumnut API per-page ceiling.
    search_kwargs["limit"] = min(int(request.size), GUMNUT_API_MAX_PAGE_SIZE)
if request.page is not None:
    search_kwargs["page"] = int(request.page)
gumnut_results = await client.search.search(**search_kwargs)
```

Substituting an adapter-side default (e.g., `limit = int(request.size) if request.size else 50`) fragments the source of truth — the Gumnut API's `DEFAULT_PAGE_SIZE = 20` and an adapter-hardcoded 50 silently disagree, and a future change to the Gumnut API's default won't propagate. Same principle for any optional kwarg passed through the adapter: preserve the optionality, don't normalize.

Generated Immich DTO constraints can exceed the backend's per-page cap — e.g., `MetadataSearchDto.size` allows `le=1000.0` while the Gumnut API enforces `GUMNUT_API_MAX_PAGE_SIZE`, and the Immich mobile client uses these high values by default. Clamp at the adapter site against `GUMNUT_API_MAX_PAGE_SIZE` (defined in `routers/api/constants.py`); without it the Gumnut API 422s and the user sees a generic "Failed to ..." surface. **Don't shortcut by tightening the generated DTO** (e.g., dropping `Field(le=1000.0)` to `le=200.0`) — `routers/immich_models.py` is overwritten on every Immich version bump, which restores upstream's constraint and silently reintroduces the bug.

## Unpaginated Immich endpoints — cap the walk and log it

A few Immich endpoints take no pagination parameters at all (`GET /map/markers`, `GET /stacks`), so the client cannot ask for a second page and the adapter must either answer with the whole library or truncate. Truncate: pick a cap, `break` out of the walk, and log the counts plus a truncation flag — that log is the only signal a library has outgrown the endpoint, and the input to deciding the read needs a different shape. See `MAP_MARKERS_CAP` (`routers/utils/map_markers.py`) and `SEARCH_STACKS_CAP` (`routers/api/stacks.py`).

Test the cap **before** admitting an item, not after appending it, and set the flag on the same branch that breaks. Deriving the flag from `len(items) >= CAP` after the loop makes a library of exactly `CAP` items report truncation that never happened, which costs the flag the meaning the paragraph above gives it. The cost is one lookahead item — you only know the walk was cut short by seeing the item you declined — which can pull in one extra upstream page. `SEARCH_STACKS_CAP` is the worked example; `map_markers.py` predates the rule and still tests after appending, so copy the stacks shape rather than that one.

Name the boolean after the truncation, not after one bound (`stack_search_truncated`, not `stack_cap_hit`) once a second bound can set it, and record which bound fired in its own field. A flag named for the cap that also fires for a member budget sends an operator to the wrong constant.

Neither a concurrency bound nor an item cap is a *work* bound. `gather_with_concurrency` caps in-flight calls, not total round-trips or peak memory; and an item cap only bounds work when the per-item cost is bounded too — 500 stacks of 3 frames and 500 stacks of 10,000 both satisfy `SEARCH_STACKS_CAP`. Where the listing row carries its own size (`asset_count` on a stack row), budget the *total* alongside the item count (`SEARCH_STACKS_MEMBER_BUDGET`), stop on whichever binds first, and log which one did. Check that the budgeted unit matches what the hydration read actually fetches — a stack row counts live members only while `fetch_stack_members` reads `state="all"`, so trashed frames are hydrated unbudgeted. Where the two can't be made to agree, log the realized total next to the budgeted one (`stack_members_hydrated` vs `stack_members_budgeted`) so the gap is measurable instead of assumed. Keep admission all-or-nothing per item rather than truncating an item's own collection: clients read `assets.length` as the stack's size, so a short array is a wrong answer where a missing stack is merely an incomplete one. Worker limits also do not bound resources staged before submission; acquire admission before staging, as in `asset_edit_renderer._get_render_admission`.

## Counts and Aggregates

When a response only needs a count over a person's / album's assets, read the precomputed field off the parent entity rather than enumerating a paginator. `PersonResponse.asset_count` and `AlbumResponse.asset_count` are computed in O(1) by the Gumnut API and already trusted elsewhere in the adapter (e.g., `_immich_people_sort_key`, album conversion). Enumerating with `len([a async for a in client.assets.list(person_ids=[...])])` fans out into N paginated GETs of full asset payloads — this scaled to >10s on large persons.

Note that an `async for` paginator is always truthy: `if not client.assets.list(...)` is dead code, not an empty-list guard. The page contents are only known after iteration runs, so use the precomputed count rather than trying to short-circuit.

The SDK's `limit` kwarg on paginated methods (e.g., `client.assets.list(..., limit=20)`) is the **per-page** size, not a result cap. `async for` walks every page until `has_more` is false, so the loop will yield far more than `limit` items if the result set is larger. When you genuinely only want N items (e.g., a thumbnail preview, or a "non-empty" probe), break out explicitly:

```python
assets: list[AssetResponse] = []
async for asset in client.assets.list(local_datetime_after=..., limit=N):
    assets.append(asset)
    if len(assets) >= N:
        break
```

Without the break, a `limit=1` "is this non-empty?" probe on a busy day burns one round-trip per matching asset.

The `local_datetime_after` / `local_datetime_before` list filters are **exclusive on both ends**. Don't pass a month start directly as the after-bound — an asset captured exactly at month-start midnight is counted in that month's `counts` bucket but excluded from the listing. Build month windows with `month_query_bounds` in `routers/api/timeline.py`, which backs the after-bound off by one microsecond.

## Parallel Fan-Out with `asyncio.gather`

For endpoints that fan out N parallel backend calls where partial results are friendlier than a 500 (e.g., the OnThisDay memories carousel — N-1 years still produces a useful response), pass `return_exceptions=True` so a single transient failure doesn't cancel the others. Filter on `Exception`, not `BaseException`, so `asyncio.CancelledError` (which inherits from `BaseException`) propagates instead of being swallowed as a backend error:

```python
results = await asyncio.gather(
    *(_per_year(client, y) for y in years),
    return_exceptions=True,
)
for year, result in zip(years, results):
    if isinstance(result, Exception):
        logger.warning(f"...failed for {year}", exc_info=result)
        # substitute a degraded value
    elif isinstance(result, BaseException):
        # Re-raise CancelledError and other control-flow signals so request
        # cancellation isn't silently swallowed.
        raise result
    else:
        ...
```

`gather(return_exceptions=True)` captures `CancelledError` like any other exception, so a naive `isinstance(result, BaseException)` check disguises cancellation as a transient failure. See `routers/api/memories.py::_gather_year_assets` for the canonical shape.

## Bounded fan-out for per-item SDK calls

For bulk endpoints that have to call a single-item SDK method per input (no bulk SDK variant exists — e.g., `client.people.update`, `client.people.delete`, or per-album SDK calls inside a multi-album fan-out), use `gather_with_concurrency` from `routers/utils/concurrency.py` instead of a sequential `for` loop. It runs coroutines in parallel under a `BULK_FANOUT_CONCURRENCY_LIMIT` semaphore, preserves input order in the result list, and propagates the first exception. Propagating is **not** stopping — the siblings keep running, so an aborted batch still costs its full fan-out upstream, and only the first exception *to occur* is ever visible (completion order, not input order). See `gather_with_concurrency`'s docstring before relying on either. The same helper applies when the parallelizable unit is a multi-step coroutine rather than a single SDK call (e.g. `reassign_faces` parallelizes per-`(asset, sourcePerson)` pairs whose dominant cost is a `client.faces.list` call; the inner per-face `client.faces.update` loop stays sequential because pairs almost always yield 0–1 faces).

```python
from routers.utils.concurrency import gather_with_concurrency

results = await gather_with_concurrency(
    [_update_one_person(client, item) for item in people_data.people]
)
```

When the endpoint returns `List[BulkIdResponseDto]`, catch per-item errors **inside** the per-item coroutine and return a typed result — don't rely on the helper to surface them. When the endpoint contract is "fail the response on first error" (e.g. `delete_people` returning 204), the default propagation is exactly right; let the global `GumnutError` handler take over.

For the error-classification half of the per-item coroutine, use `classify_bulk_item_call` from `routers/utils/bulk.py` instead of re-rolling the `APIStatusError` / `GumnutError` try/except. It mirrors the per-chunk policy in `chunked_per_item_bulk` (`classify_bulk_item_error` for `APIStatusError`, `log_bulk_transport_error` + `unknown` for transport failures) and returns `None` on success or a classified enum value (`BulkIdErrorReason`). Wrap the entire SDK-touching segment in one call — including any helper that itself issues SDK calls (e.g. `_resolve_thumbnail_face_id`'s `client.faces.list`) — so the helper catches errors from every SDK round-trip on the path. Endpoint-specific non-SDK exceptions (UUID parse `ValueError`, `HTTPException` from a logical 4xx branch) stay at the call site:

```python
sdk_error = await classify_bulk_item_call(
    _do_one_item(client, item),
    error_enum=BulkIdErrorReason,
    log_context="update_people",
    log_extra={"person_id": item.id},
)
return BulkIdResponseDto(id=item.id, success=sdk_error is None, error=sdk_error)
```

See `routers/api/people.py::_update_one_person` for the canonical multi-step shape (UUID parse → SDK call wrapped → HTTPException out) and `routers/api/albums.py::_add_assets_to_one_album` for the single-call shape. The `tests/unit/utils/test_bulk.py::TestClassifyBulkItemCall` suite pins the helper's contract.

Pin the contract with a concurrency-counter test: an `asyncio.Lock`-guarded `active` / `peak` counter inside the per-item side_effect, asserting `peak > 1` (parallel) and `peak <= BULK_FANOUT_CONCURRENCY_LIMIT` (bounded). See `tests/unit/utils/test_concurrency.py::test_caps_concurrent_in_flight_calls` and the per-endpoint variants in `tests/unit/api/test_people.py` / `test_albums.py`.

If you write a *new* fan-out helper instead of using `gather_with_concurrency`, watch for unawaited-coroutine leaks on cancellation: when callers pass eagerly-constructed coroutines (`[some_coro(x) for x in xs]`) and your wrapper task awaits something *before* `await coro` (a semaphore acquire, a queue, etc.), cancelling the gather itself (an aborted request, an enclosing timeout) cancels those waiting wrappers — the inner `coro` is never awaited and is GC'd later as `RuntimeWarning: coroutine was never awaited` (noisy precisely on the error path). Either build the inner coroutine lazily inside the wrapper, or `coro.close()` it explicitly when the pre-`await coro` cancellation hits. See `gather_with_concurrency`'s `_run` for the canonical shape and `tests/unit/utils/test_concurrency.py::test_cancellation_does_not_warn_unawaited_coroutines` for the regression test pattern. That test is also the cautionary example: a test asserting the *absence* of `RuntimeWarning: coroutine was never awaited` passes trivially unless it both cancels (a sibling exception cancels nothing) and forces the orphans to be finalized — the warning fires at finalization, not when the coroutine is orphaned — yield to the loop before asserting, and treat `gc.collect()` as cycle insurance only. See that test's comment for why the yield is the load-bearing half. Verify such a test by deleting the guard and confirming it fails — and when scripting that mutation, anchor on enough surrounding context to be unique: a snippet that occurs twice in the file silently mutates the wrong site, which reads as a surviving mutation and hides real coverage.

## Bulk-ID Endpoints

For Gumnut API endpoints that accept bulk IDs (e.g., `assets.trash`, `assets.restore`, `assets.delete_list`, and list filters with `ids=...`), chunk the request at `GUMNUT_API_MAX_BULK_IDS`. The Gumnut API rejects over-cap requests with a 422, and neither the API nor the SDK chunks for you, so the loop is the caller's job. The constant in `routers/api/constants.py` is the source of truth for the current API cap:

```python
from itertools import batched
from routers.api.constants import GUMNUT_API_MAX_BULK_IDS

for chunk in batched(asset_uuids, GUMNUT_API_MAX_BULK_IDS):
    gumnut_ids = [uuid_to_gumnut_asset_id(uid) for uid in chunk]
    await client.assets.trash(ids=gumnut_ids)
```

Backend bulk endpoints are idempotent on already-transitioned rows (e.g., `trash_assets` skips already-trashed ids; `restore_assets` skips already-live ids). **Don't add per-id 404 / NotFoundError swallowing for these flows** — let bulk failures (validation, transport, 5xx) propagate to the global `GumnutError` handler. The per-id-loop-with-NotFoundError pattern in [Gumnut SDK Errors](routes-dtos-and-upstream-compatibility.md#gumnut-sdk-errors) applies to single-asset endpoints (e.g., `client.assets.delete(asset_id)`), not to the bulk variants.

**Cross-chunk atomicity is not guaranteed.** Backend bulk endpoints (and the SDK methods that wrap them) commit each call atomically — a single chunk either fully commits or writes nothing — but that guarantee does not extend across the chunked loop above. A failure on chunk N (N ≥ 2) leaves chunks 1..N-1 already committed, with no compensating rollback and no per-chunk error report. The exception propagates as one 5xx to the client. Document this in the handler docstring when the SDK markets the underlying endpoint as atomic (e.g., `bulk_update_assets`), so future readers don't assume the guarantee transitively holds through the adapter's chunking layer.

**Chunk only when partial completion is acceptable or reversible.** For example, `POST /stacks` forwards oversized requests so the backend rejects them atomically; chunking could repoint assets from existing stacks without a reliable rollback.

Pin the no-swallow contract with a `test_*_propagates_sdk_error` test per bulk flow — mock the bulk call to raise via `make_sdk_status_error(500, ...)` and assert `pytest.raises(APIStatusError)`. Without this test, a future refactor that wraps the bulk call in `try/except` would silently regress the contract. See `tests/unit/api/test_assets.py::TestDeleteAssets::test_delete_assets_force_false_propagates_sdk_error` for the canonical shape.

**Reads that feed a bulk write must use `state="all"`.** `client.assets.list(ids=...)` defaults to the live-only filter, so trashed (soft-deleted) ids are silently absent from `page.data`. When a "bulk GET + bulk PATCH" flow reads current values to compute a per-asset write-back (e.g. `update_assets`' `dateTimeRelative` / standalone-`timeZone` modes), pass `state="all"` — otherwise the read-driven path silently skips assets that an unconditional bulk write (one that forwards every id regardless of trash state) would have updated, an asymmetry the same request can expose across different fields. Mirrors sync hydration's read (see `routers/api/sync/entity_fetch.py`). Pin it with a test asserting the read kwargs include `state="all"`.

**Per-item response contract variant.** Some Immich bulk endpoints (e.g. `PUT`/`DELETE /api/albums/{id}/assets`) must return `List[BulkIdResponseDto]` with per-id `success` / `error` mapping, so the no-swallow contract above does not apply — the handler has to catch upstream errors locally and translate them into per-id `BulkIdErrorReason` values. Use `chunked_per_item_bulk` from `routers/utils/bulk.py`: it owns the chunking loop and the `APIStatusError`/`GumnutError` mapping (errors are classified via `classify_bulk_item_error` and transport failures are logged with `chunk_size` + `request_size` extras), and yields per-chunk outcomes as `BulkChunkOutcome[T]` with either a `response` or an `error`. Callers compose the final per-asset list — that's where response-shape variation lives (e.g. `add` accumulates `added`/`duplicate`/`not_found` sets and walks input order to look up each id; `remove` only needs an error vs success branch). Inspect successful response bodies for item-level failures, and do not let one response-reported failure skip later chunks. See `routers/api/albums.py::add_assets_to_album` / `remove_asset_from_album` for canonical call sites and `tests/unit/utils/test_bulk.py` for the helper's contract.

Pin the chunking math with exact-boundary tests at `total = GUMNUT_API_MAX_BULK_IDS` (one chunk, no split) and `total = GUMNUT_API_MAX_BULK_IDS + 1` (two chunks, second is a single element) — these catch off-by-one regressions a future hand-rolled `if len(ids) > N` split would introduce. See the parametrized cases in `tests/unit/utils/test_bulk.py::test_splits_oversized_input_into_ordered_chunks` and `tests/unit/api/test_albums.py::test_*_chunks_large_request`.

**Prefer typed SDK methods; the raw client is a stopgap.** When the SDK doesn't yet expose a typed method for a backend endpoint (Stainless regenerates on a delay after each backend release), call the raw HTTP layer directly via `AsyncGumnut.post()` / `.delete()` with `cast_to=type(None)` for endpoints that return no useful body:

```python
await client.post("/api/some-new-endpoint", body={"ids": gumnut_ids}, cast_to=type(None))
```

`AsyncGumnut` extends `AsyncAPIClient`, whose `.post()` / `.delete()` methods are public, route through the same JWT auth, retry, and response-hook plumbing as the typed methods, and surface the same `GumnutError` hierarchy. Don't import from `gumnut._types` — `cast_to=type(None)` works without it.

The gap a raw call works around closes silently on the next SDK bump — nothing fails to tell you. So: (1) when you touch a raw call site or bump `gumnut-sdk`, check whether the typed method has landed and migrate if so; (2) if a comment must explain the raw call, point at what to re-check rather than asserting the SDK lacks the method — that claim expires.
