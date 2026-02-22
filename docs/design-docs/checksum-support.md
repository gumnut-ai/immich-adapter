---
title: "Checksum Support"
status: completed
created: 2025-11-25
last-updated: 2025-11-25
---

# Asset Checksum & Deduplication Analysis

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

**Source**: `immich/server/src/services/asset-media.service.ts:272-300`

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

Add a `checksum_sha1` column to the existing Assets table alongside the existing SHA-256 checksum.

### Schema Changes

**Gumnut Assets Table:**

```sql
ALTER TABLE assets
ADD COLUMN checksum_sha1 BYTEA;

-- Index for checksum lookups
CREATE INDEX idx_assets_checksum_sha1
ON assets(checksum_sha1);

-- Composite index for library-scoped deduplication
CREATE INDEX idx_assets_library_checksum_sha1
ON assets(library_id, checksum_sha1);
```

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
