---
title: "Bulk-ID Operations"
last-updated: 2026-10-01
---

# Bulk-ID Operations

## Bulk-ID Endpoints

For Gumnut API endpoints that accept bulk IDs (e.g., `assets.trash`, `assets.restore`, `assets.delete_list`, and list filters with `ids=...`), chunk the request at `GUMNUT_API_MAX_BULK_IDS`. The Gumnut API rejects over-cap requests with a 422, and neither the API nor the SDK chunks for you, so the loop is the caller's job. The constant in `routers/api/constants.py` is the source of truth for the current API cap:

```python
from itertools import batched
from routers.api.constants import GUMNUT_API_MAX_BULK_IDS

for chunk in batched(asset_uuids, GUMNUT_API_MAX_BULK_IDS):
    gumnut_ids = [uuid_to_gumnut_asset_id(uid) for uid in chunk]
    await client.assets.trash(ids=gumnut_ids)
```

Backend bulk endpoints are idempotent on already-transitioned rows (e.g., `trash_assets` skips already-trashed ids; `restore_assets` skips already-live ids). **Don't add per-id 404 / NotFoundError swallowing for these flows** — let bulk failures (validation, transport, 5xx) propagate to the global `GumnutError` handler. The per-id-loop-with-NotFoundError pattern in [Gumnut SDK Errors](route-errors-and-filters.md#gumnut-sdk-errors) applies to single-asset endpoints (e.g., `client.assets.delete(asset_id)`), not to the bulk variants.

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
