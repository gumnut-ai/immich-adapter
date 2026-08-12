---
title: "Checksum Support"
status: deprecated
superseded-by: ../references/asset-and-media-handling.md
created: 2025-11-25
last-updated: 2026-08-11
---

# Asset Checksum & Deduplication Analysis

> **Deprecated —** This document records the checksum-compatibility decision
> that led to storing SHA-1 alongside Gumnut's SHA-256 checksum. It was pruned
> on 2026-08-05 to the upstream protocol, chosen approach, and trade-offs; large
> sample payloads and request walkthroughs are owned by the protocol and code.
> Current adapter behavior lives in [Asset and Media Handling](../references/asset-and-media-handling.md#outbound-asset-checksums--emit-base64-sha-1-never-the-sha-256).
> Two proposed details did not ship:
>
> - **The composite index did not ship.** This doc proposed indexing both `checksum_sha1` and `(library_id, checksum_sha1)`; the backend's asset model carries a single-column index on `checksum_sha1` only. The composite indexes that exist are on the SHA-256 `checksum` column (the `(library_id, checksum)` unique constraint and a live-rows partial index), not on `checksum_sha1`.
> - **The Background section is a description of upstream Immich, not of the adapter.** Of the three endpoints it catalogs, only `POST /api/assets/bulk-upload-check` is implemented here. The adapter has no `POST /api/assets/exist` route, and it does not implement the upload-time `x-immich-checksum` duplicate-detection path — do not read that section as an adapter capability list.

## Background: How Immich Deduplicates Assets

### Immich Client

Both the Immich web and mobile clients compute a base64-encoded SHA-1 hash of the file content.

### Server-Side Deduplication Protocol

The upstream Immich server uses checksum and device-identity checks at multiple
stages:

### 1. Upload Endpoint (`POST /api/assets`)

The client sends its base64 SHA-1 in `x-immich-checksum`. Upstream checks that
checksum within the authenticated owner's assets before accepting the upload,
returning the existing asset for a duplicate and creating a new asset otherwise.

**Source**: `immich/server/src/middleware/asset-upload.interceptor.ts`

### 2. Bulk Upload Check Endpoint (`POST /api/assets/bulk-upload-check`)

This endpoint batch-checks client asset IDs and base64 SHA-1 values before
upload, returning an accept-or-reject decision and the existing server asset ID
for duplicates. This is the checksum-based preflight surface implemented by the
adapter.

**Source**: `immich/server/src/services/asset-media.service.ts`

### 3. Existence Check Endpoint (`POST /api/assets/exist`)

Upstream also supports checking device-local asset IDs for a device. This is a
separate identity-based existence protocol, not a checksum lookup, and the
adapter does not implement this route.

**Source**: `immich/server/src/controllers/asset-media.controller.ts`

## Proposed Solution

### Dedicated Column Approach

Add a `checksum_sha1` column to the existing Assets table alongside the existing SHA-256 checksum, indexed for deduplication lookups. The column lives in the Gumnut backend, not this adapter.

### Key Points

- **Dual checksums**: Store both SHA-256 (security) and SHA-1 (Immich compatibility)
- **Direct queries**: Simple `WHERE checksum_sha1 = ?` lookups without JOINs
- **Best performance**: Sub-millisecond query times
- **Type safety**: Column type enforced at database level (BYTEA)

### Trade-offs

**Advantages:**

- Simplest implementation
- Best query performance (<1ms)
- No JOIN overhead
- Database-enforced type safety

**Disadvantages:**

- Immich-specific schema change to Gumnut backend
- Less flexible for additional metadata
- Fixed schema design
