---
title: "Bounded Fan-Out"
last-updated: 2026-10-01
---

# Bounded Fan-Out

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

For bulk endpoints that have to call a single-item SDK method per input (no bulk SDK variant exists — e.g., `client.people.update`, `client.people.delete`, or per-album SDK calls inside a multi-album fan-out), use `gather_with_concurrency` from `routers/utils/concurrency.py` instead of a sequential `for` loop. Read the helper's docstring for ordering, sibling continuation, first-error,
and `cancel_on_error` semantics. The parallelizable unit can be a multi-step
coroutine, such as a per-`(asset, sourcePerson)` face reassignment; it need not
be a single SDK call.
See `routers/api/people.py::_reassign_one_pair` for the outer-parallel,
inner-sequential shape and its rationale.

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

Pin the contract with a concurrency-counter test: an `asyncio.Lock`-guarded `active` / `peak` counter inside the per-item side_effect, asserting `peak > 1` (parallel) and `peak <= BULK_FANOUT_CONCURRENCY_LIMIT` (bounded). See `tests/unit/utils/test_concurrency.py::test_caps_concurrent_in_flight_calls` and the per-endpoint variants in `tests/unit/api/test_people.py` / `test_albums.py`. When batching a streamed selector before the fan-out (each wave is `_abatched`-then-gathered so staged items and tasks stay bounded), size the test at more than `BULK_FANOUT_CONCURRENCY_LIMIT` items so the bound actually engages — a wave that fits one batch would pass even against an unbounded `asyncio.gather`. `tests/unit/api/test_download.py` pins both the archive and `/info` fan-outs this way.

Testing `cancel_on_error=True`'s in-flight cancellation needs the failing coroutine to `await` before it raises. `_run` closes a queued sibling's coroutine the instant `failed` is set — *before* awaiting it — so a failure that raises synchronously cancels siblings that never started, and a test watching for cancellation inside a *running* sibling sees none. Make the failing item yield first (e.g. a short `asyncio.sleep`), then assert the siblings were cancelled mid-flight. See `tests/unit/api/test_download.py::test_archive_cancels_pending_members_when_one_preflight_fails`.

For a new fan-out helper, close eagerly constructed coroutines when
cancellation occurs before they are awaited, or construct them lazily.
`routers/utils/concurrency.py::gather_with_concurrency` owns the cleanup
mechanics. Pin the absence of unawaited-coroutine warnings with
`tests/unit/utils/test_concurrency.py::test_cancellation_does_not_warn_unawaited_coroutines`;
its comment explains the required event-loop yield and finalization.
Verify the test fails when the guard is removed; anchor any scripted mutation
on unique surrounding context so it changes the intended site.
