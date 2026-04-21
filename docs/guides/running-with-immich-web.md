---
title: "Running with Immich Web"
last-updated: 2026-03-19
---

# Running with Immich Web

There are two ways to run the Immich web UI locally: using pre-built static files (simpler) or running the Vite dev server from source (needed for frontend changes).

Both approaches require the adapter to be running — see the [README](../../README.md) for setup instructions.

## Option 1: Static Files

The simplest way to run the Immich web UI locally is to extract the pre-built static files from the Immich Docker image. The adapter serves them directly on port 3001 — no separate web server needed.

```
Browser (localhost:3001)
  → immich-adapter (serves static files + API)
    → photos-api (localhost:8000)
      → Clerk (OAuth provider)
```

For details on how the adapter handles requests in this setup, see the [adapter architecture doc](../architecture/adapter-architecture.md).

### Prerequisites

1. **Docker** running locally
2. **photos-api** running on `localhost:8000` — see the photos-api README for setup
3. **Clerk OAuth configured** in photos-api — see the "Configure Clerk OAuth" section in the photos-api README
4. **immich-adapter** running on `localhost:3001`

### Extract Immich web files

Run the extraction script to pull the web files from the Immich Docker image into `static/`:

```bash
./scripts/extract-immich-web.py -f ./static
```

The script reads the Immich version from `.immich-container-tag`, pulls `ghcr.io/immich-app/immich-server:<tag>`, and copies the pre-built web files into `static/`. The `-f` flag overwrites any existing files.

The script also writes a marker file `static/.extracted-tag` recording which tag it extracted. The adapter reads this marker at startup and logs a loud warning if it no longer matches `.immich-container-tag` — your cue to re-run the extraction. CI and fresh clones (no marker present) are unaffected.

Other useful options:

```bash
./scripts/extract-immich-web.py -f -s ./static   # Skip pull if image already exists locally
./scripts/extract-immich-web.py -t v2.5.6 ./static  # Use a specific tag instead of .immich-container-tag
```

### Verify the setup

1. Open http://localhost:3001 in your browser
2. The app should redirect to Clerk for OAuth authentication
3. After signing in, you should be redirected back to the Immich web UI

### Troubleshooting

- **"Failed to pull image"**: Ensure Docker is running
- **OAuth `invalid_client` error**: Check that `CLERK_OAUTH_CLIENT_ID` in photos-api `.env` is set to a real value (not the placeholder)
- **OAuth `redirect_uri` mismatch**: Add `http://localhost:3001/auth/login` as an allowed redirect URI in the Clerk OAuth application settings
- **OAuth `invalid_scope` error**: Enable the `openid`, `email`, and `profile` scopes on the Clerk OAuth application

## Option 2: Dev Server

If you need to modify the Immich web UI or debug frontend behavior, you can run the Immich web dev server from source instead. This uses Vite's hot-reload on port 3000 and proxies API requests to the adapter.

```
Browser (localhost:3000)
  → Vite dev server proxy (/api/*)
    → immich-adapter (localhost:3001)
      → photos-api (localhost:8000)
        → Clerk (OAuth provider)
```

### Prerequisites

1. **photos-api** running on `localhost:8000` — see the photos-api README for setup
2. **Clerk OAuth configured** in photos-api — see the "Configure Clerk OAuth" section in the photos-api README
3. **immich-adapter** running on `localhost:3001`
4. The [Immich repository](https://github.com/immich-app/immich) cloned as a sibling directory (`../immich/`)

### Build and run Immich web

1. **Build the Immich TypeScript SDK** (shared types used by the web app):

```bash
cd ../immich/open-api/typescript-sdk
pnpm install
pnpm run build
```

2. **Install web dependencies**:

```bash
cd ../immich/web
pnpm install
```

3. **Start the Immich web dev server**, pointing it at the immich-adapter:

```bash
cd ../immich/web
IMMICH_SERVER_URL=http://localhost:3001/ pnpm run dev
```

The web app will be available at http://localhost:3000. The Vite dev server proxies all `/api` requests to the immich-adapter via the `IMMICH_SERVER_URL` environment variable.

### Verify the setup

1. Open http://localhost:3000 in your browser
2. The app should redirect to Clerk for OAuth authentication
3. After signing in, you should be redirected back to the Immich web UI

### Troubleshooting

- **404 errors from the proxy**: Ensure the immich-adapter is actually running on port 3001 and no other service is using that port
- **OAuth `invalid_client` error**: Check that `CLERK_OAUTH_CLIENT_ID` in photos-api `.env` is set to a real value (not the placeholder)
- **OAuth `redirect_uri` mismatch**: Add `http://localhost:3000/auth/login` as an allowed redirect URI in the Clerk OAuth application settings
- **OAuth `invalid_scope` error**: Enable the `openid`, `email`, and `profile` scopes on the Clerk OAuth application

## Monitoring Traffic

Use the developer tools in your web browser to inspect API requests and responses between the Immich web client and the adapter.
