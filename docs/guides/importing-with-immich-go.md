---
title: "Importing with immich-go"
last-updated: 2026-07-23
---

# Importing with immich-go

[immich-go](https://github.com/simulot/immich-go) is a standalone CLI that pushes
a photo/video library into an Immich server directly over the REST API — no web
or mobile app involved. It works against the adapter, so it can bulk-import a
local folder (or a Google Photos Takeout archive) into the Gumnut API.

## Authentication: use a Gumnut API key

immich-go authenticates with the `x-api-key` header on every request. The adapter
accepts that header and forwards its value to the Gumnut API as the caller's
credential (see `routers/middleware/auth_middleware.py`). The value must be a
**Gumnut API key** (an `apikey_...` string), which the Gumnut backend validates
directly — the adapter does not mint, store, or verify keys itself.

**Get a key from the Gumnut app, not from Immich.** Mint a Gumnut API key with
**write** access from your Gumnut account, then pass it to immich-go. Minting a
key is a credential-management operation that the Gumnut API only allows from a
first-party (browser) session, so the adapter's own Immich "API Keys" settings
page cannot create one — the adapter authenticates to the Gumnut API with a
delegated OAuth token, which is not permitted to mint keys. That Immich-side page
is therefore a non-functional stub; ignore it and use the key from your Gumnut
account.

A key needs **write** scope to upload; add **delete** scope too if you plan to use
immich-go subcommands that remove or replace assets.

## Running an import

Point immich-go at a running adapter and pass your key:

```bash
immich-go upload from-folder \
  --server http://localhost:3001 \
  --api-key apikey_your_key_here \
  /path/to/photos
```

immich-go prefixes every request with `/api`, so `--server` is the adapter's base
URL (no `/api` suffix). Against a deployed adapter, use its public URL instead of
`localhost:3001`.

## What immich-go does under the hood

For a `upload from-folder` run, immich-go:

1. Handshakes: `GET /api/server/ping` (expects `{"res":"pong"}`), `GET /api/users/me`,
   `GET /api/server/media-types`, then `GET /api/server/about` — whose `version`
   must be a valid semver string (the adapter reports the Immich version from
   `.immich-container-tag`, which satisfies this).
2. Builds a client-side index of what the server already has via paginated
   `POST /api/search/metadata`, and skips assets whose checksum (base64 SHA-1) or
   filename+size already match. Modern immich-go does **not** call
   `/api/assets/bulk-upload-check`.
3. Uploads each new asset with `POST /api/assets` (multipart `assetData`), sending
   an `x-immich-checksum` header so the server can reject an already-stored file.
4. Optionally creates albums (`POST /api/albums`) and adds assets to them
   (`PUT /api/albums/{id}/assets`).
5. If `--tag` is passed, upserts each tag (`PUT /api/tags`, reading the returned
   tag id) and assigns the uploaded assets to it (`PUT /api/tags/{id}/assets`).

## Tags (`--tag`)

The Gumnut API has no tag concept yet, so the adapter emulates tags to keep a
tagged import working: `PUT /api/tags` returns a stable synthetic tag id, and
`PUT /api/tags/{id}/assets` **appends the tag to each asset's description** (as a
`#<tag>` line) rather than storing a real tag. Re-importing the same tag is
idempotent — the line isn't duplicated. The tag therefore shows up in the
asset's description in the web/mobile app, not in the (intentionally disabled)
tags sidebar. This is an interim workaround; when Gumnut gains real tags the
embedded `#`-lines won't migrate automatically.

```bash
immich-go upload from-folder \
  --server http://localhost:3001 \
  --api-key apikey_your_key_here \
  --tag Vacation \
  /path/to/photos
```

## Troubleshooting

- **401 on every call after the ping succeeds**: the `x-api-key` value isn't a
  valid Gumnut API key, or it lacks write scope. The unauthenticated ping/version
  endpoints answer without a key, so a bad key first surfaces at `GET /api/users/me`.
- **"invalid semantic version" at connect time**: `.immich-container-tag` must hold
  a semver value (e.g. `v3.0.3`); immich-go parses `/api/server/about`'s `version`
  and aborts if it can't.
- **`panic: index out of range [0]` on a `--tag` import**: fixed — the tag upsert
  now returns a non-empty response. If you still hit it, the adapter is running an
  old revision where `PUT /api/tags` was a stub returning `[]`.
