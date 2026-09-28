---
title: Logging
last-updated: 2026-09-27
---

# Logging

Use structured logging with key/value metadata in the `extra` dict. Include relevant identifiers for traceability.

```python
logger.info(f"Created library {library.id}", extra={"library_id": library.id})
logger.info("WebSocket connected", extra={"sid": sid, "user_id": user_id, "device_type": session.device_type})
```

This enables better searching and correlation in Sentry.

Never log signed CDN URLs: their query strings are bearer credentials. Use the
`asset_id` and bare hostname for correlation.

## Upstream response log levels

For responses/errors from upstream Gumnut API calls, use status-based severity:

- `404` → `INFO`
- Other `4xx` (including `400`, `401`, `403`, `422`, `429`) → `WARNING`
- `507` (over-quota upload) → `WARNING`
- Other `5xx` → `ERROR`

When possible, use shared helpers in `routers/utils/error_mapping.py` (`upstream_status_log_level` / `log_upstream_response`) instead of ad-hoc `if/else` logging branches.

## Per-item degradation on hot endpoints — log one aggregate record

When a request degrades by skipping some of N items (a stack the timeline can't resolve, an entity a batch drops), log once per request with counts — `"%d of %d …"` plus `extra={"<thing>_count": n, "<denominator>_count": total, "sample_<thing>_ids": ids[:10]}` — not once per item. Most causes of these are systemic rather than per-item, so a per-item record floods precisely when the signal matters, and on an endpoint a client calls repeatedly (timeline buckets, once per month scrolled) it multiplies again. The ratio is also what separates "all of them" from "one of them", which is the first thing triage needs, and the sample turns that into something to go look at — every record wants one, including where the skipped items are a list's tail rather than scattered failures. Name the denominator for the population the ratio is actually over rather than reusing one name: `resolve_timeline_stacks` is the worked example across four such paths and needs three — `requested_stack_count` (every stack in the bucket), `partial_stack_count` (only those classified partial, which is the population the read cap slices), and `attempted_stack_count` (only those a member read was actually issued for). Where several records share one user-visible effect, name each **numerator** for its own cause rather than that effect — all four of those records leave stacks uncollapsed, so an `uncollapsed_stack_count` on any one of them reads as the total and silently under-reports when alerted on.

When the degradation swallows an exception, one field naming the cause belongs on the record too — a count cannot separate an expired token from throttling from an outage, and for a path the route-level guard never sees, this record is the only signal there is.

**Reserved `extra` keys**: Python's `LogRecord` has reserved attributes (`filename`, `module`, `name`, `msg`, `args`, `levelname`, `pathname`, `lineno`, etc.). Using these as `extra` keys causes a `KeyError` at runtime. Use prefixed names instead (e.g., `upload_filename` instead of `filename`).
