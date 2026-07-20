# Maximum page size accepted by the Gumnut API. The backend rejects any
# per-page `limit` above this with a 422, so the adapter clamps to it before
# forwarding. Keep in sync with the backend's own per-page ceiling.
GUMNUT_API_MAX_PAGE_SIZE = 200

# Maximum number of IDs accepted by the Gumnut API's bulk filters and mutation
# endpoints. Keep all adapter-side chunking tied to this single upstream limit.
GUMNUT_API_MAX_BULK_IDS = 200

# Placeholder device_id the adapter sends to the Gumnut API on upload. The
# Gumnut API requires device_asset_id/device_id, but Immich v3 dropped both
# from the upload DTO (clients no longer send them). The adapter passes this
# constant device_id plus a unique per-upload device_asset_id (a fresh UUID) —
# unique so two distinct assets never collapse onto one device tuple. Dedup-safe:
# the backend deduplicates by checksum, not by device identifier, so true
# re-uploads are still caught (see docs/design-docs/checksum-support.md).
GUMNUT_UPLOAD_DEVICE_ID = "gumnut-device"

# Placeholder license key for the stubbed license surfaces (the adapter has no
# licensing). Immich v3's UserLicense DTO enforces the key pattern
# ^IM(SV|CL)(-[\dA-Za-z]{4}){8}$ on responses, so any stub value must match it
# or the returning endpoint 500s on response validation.
STUB_LICENSE_KEY = "IMSV-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA"
