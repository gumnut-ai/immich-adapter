# Maximum page size accepted by the Gumnut API. The backend rejects any
# per-page `limit` above this with a 422, so the adapter clamps to it before
# forwarding. Keep in sync with the backend's own per-page ceiling.
GUMNUT_API_MAX_PAGE_SIZE = 200
