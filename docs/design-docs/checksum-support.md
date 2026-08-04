---
title: "Checksum Support"
status: deprecated
superseded-by: ../references/code-practices.md
created: 2025-11-25
last-updated: 2026-07-27
---

# Asset Checksum & Deduplication Analysis

> **Deprecated (2026-07-27):** This doc analyzed Immich's checksum-based deduplication and proposed adding a SHA-1 checksum column to the Gumnut backend, which shipped. The living description of how the adapter handles checksums today — emitting base64 SHA-1 via `resolve_immich_checksum`, and the inbound `bulk_upload_check` dedup path — is [`docs/references/code-practices.md`](../references/code-practices.md) § "Outbound asset checksums". This doc is retained for the decision rationale and the alternatives weighed; it is no longer updated as the system changes. Pruned 2026-07-27 to its decision record; implementation detail was removed as it is owned by the code — specifically the "Schema Changes" section, a column-and-index definition that the backend's asset model owns and that had already drifted from it. Two things proposed below never became reality:
>
> - **The composite index did not ship.** This doc proposed indexing both `checksum_sha1` and `(library_id, checksum_sha1)`; the backend's asset model carries a single-column index on `checksum_sha1` only. The composite indexes that exist are on the SHA-256 `checksum` column (the `(library_id, checksum)` unique constraint and a live-rows partial index), not on `checksum_sha1`.
> - **The Background section is a description of upstream Immich, not of the adapter.** Of the three endpoints it catalogs, only `POST /api/assets/bulk-upload-check` is implemented here. The adapter has no `POST /api/assets/exist` route, and it does not implement the upload-time `x-immich-checksum` duplicate-detection path — do not read that section as an adapter capability list.

## Background: How Immich Deduplicates Assets

### Immich Client

Both the Immich web and mobile clients compute a base64-encoded SHA-1 hash of the file content.

### Server-Side Deduplication Protocol

The Immich server implements deduplication at multiple stages:

### 1. Upload Endpoint (`POST /api/assets`)

**Request Headers:**

```
x-immich-checksum: <base64-encoded-sha1-hash>
```

**Request Body (Form Data):**

```
deviceAssetId: <device-local-asset-id>
deviceId: <device-identifier>
fileCreatedAt: <iso8601-timestamp>
fileModifiedAt: <iso8601-timestamp>
isFavorite: <boolean>
duration: <string>
```

**Deduplication Flow:**

1. Client calculates SHA-1 hash of file before upload
2. Client sends hash in `x-immich-checksum` header
3. Server extracts ownerId from authenticated user: `ownerId = req.user.id`
4. Server queries database: `SELECT * FROM assets WHERE ownerId = ? AND checksum = ?`
5. If match found:
   - Return HTTP 200 (not 201)
   - Response: `{ status: "duplicate", id: "<existing-asset-id>" }`
6. If no match:
   - Accept upload
   - Return HTTP 201
   - Response: `{ status: "created", id: "<new-asset-id>" }`

**Source**: `immich/server/src/middleware/asset-upload.interceptor.ts`

### 2. Bulk Upload Check Endpoint (`POST /api/assets/bulk-upload-check`)

**Purpose**: Batch-check multiple assets before uploading

**Request:**

```json
{
  "assets": [
    {
      "id": "client-asset-id-1",
      "checksum": "<base64-sha1>"
    },
    {
      "id": "client-asset-id-2",
      "checksum": "<base64-sha1>"
    }
  ]
}
```

**Response:**

```json
{
  "results": [
    {
      "id": "client-asset-id-1",
      "action": "reject",
      "reason": "duplicate",
      "assetId": "<existing-server-asset-id>"
    },
    {
      "id": "client-asset-id-2",
      "action": "accept"
    }
  ]
}
```

**Source**: `immich/server/src/services/asset-media.service.ts`

### 3. Existence Check Endpoint (`POST /api/assets/exist`)

**Purpose**: Check if assets exist by device identifiers

**Request:**

```json
{
  "deviceId": "device-uuid",
  "deviceAssetIds": ["device-asset-1", "device-asset-2"]
}
```

**Response:**

```json
{
  "existingIds": ["device-asset-1"]
}
```

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
