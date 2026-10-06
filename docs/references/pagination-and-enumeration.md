---
title: "Pagination and Enumeration"
last-updated: 2026-10-01
---

# Pagination and Enumeration

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

Substituting an adapter-side default (e.g., `limit = int(request.size) if request.size else 50`) fragments the source of truth — the backend default and an adapter-hardcoded 50 can silently disagree, and a future change to the Gumnut API's default won't propagate. Same principle for any optional kwarg passed through the adapter: preserve the optionality, don't normalize.

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
