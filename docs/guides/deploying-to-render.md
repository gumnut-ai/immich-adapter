---
title: "Deploying to Render"
last-updated: 2026-05-11
---

# Deploying to Render

`immich-adapter` deploys to Render as a Docker web service. The Docker image
bundles the FastAPI adapter and the pinned Immich web UI assets.

## Deployment Model

The Dockerfile builds the runtime image in three stages:

1. Pull the pinned Immich server image and copy `/build/www` into `static/`.
2. Install Python dependencies with `uv`.
3. Copy the adapter source, static web files, and runtime dependencies into the
   final image.

The final container runs `uvicorn main:app` and exposes `/api/server/ping` as
its health check.

## Render Service Settings

Create a Render web service with:

| Setting | Value |
|---------|-------|
| Environment | Docker |
| Dockerfile path | `./Dockerfile` |
| Build command | Empty; Render uses the Dockerfile |
| Start command | Empty; Render uses the Dockerfile `CMD` |
| Health check path | `/api/server/ping` |

Render terminates TLS and sets the runtime `PORT` environment variable. The
container must bind to `0.0.0.0:$PORT`; the Dockerfile already does this.

## Environment Variables

Set these application variables for the target environment:

| Variable | Purpose |
|----------|---------|
| `GUMNUT_API_BASE_URL` | Base URL for the Gumnut Photos API. |
| `REDIS_URL` | Redis instance used for sessions, checkpoints, and encrypted JWT storage. |
| `ENVIRONMENT` | `production` for deployed services. |
| `OAUTH_MOBILE_REDIRECT_URI` | Mobile OAuth callback scheme, usually `app.immich:///oauth-callback`. |
| `SESSION_ENCRYPTION_KEY` | Fernet key used to encrypt JWTs in session storage. |
| `TRASH_RETENTION_DAYS` | Trash retention value surfaced to Immich clients; keep in sync with photos-api. |

Optional runtime variables:

| Variable | Purpose |
|----------|---------|
| `SENTRY_DSN` | Sentry project DSN, if error reporting is enabled. |
| `LOG_LEVEL` | Optional uvicorn log level; defaults to `info`. |
| `TIMEOUT_KEEP_ALIVE` | Optional keep-alive override; defaults to `75`. |
| `LIMIT_CONCURRENCY` | Optional uvicorn concurrency cap; defaults to `200`. |
| `BACKLOG` | Optional TCP accept backlog; defaults to `2048`. |

Do not set `PORT` manually in production; Render supplies it.

## Local Docker Test

Build the image:

```bash
docker build -t immich-adapter .
```

Run it locally:

```bash
docker run --rm -p 8080:8080 \
  -e PORT=8080 \
  -e GUMNUT_API_BASE_URL=http://host.docker.internal:8000 \
  -e ENVIRONMENT=development \
  immich-adapter
```

On Linux, `host.docker.internal` is not available by default. Add:

```bash
--add-host=host.docker.internal:host-gateway
```

Smoke test:

```bash
curl http://localhost:8080/api/server/ping
curl http://localhost:8080/
```

## Static Web Assets

The Docker build copies Immich web assets from:

```docker
ghcr.io/immich-app/immich-server:${IMMICH_VERSION}
```

The build fails if `static/index.html` or `static/_app` is missing in the final
image. To inspect the image manually:

```bash
docker run --rm immich-adapter sh -c "ls -la static | head && test -f static/index.html"
```

## Immich Version Updates

The adapter pins the Immich version in two places:

- `.immich-container-tag`
- `Dockerfile` `ARG IMMICH_VERSION`

Keep them in sync. CI checks this contract. See
[Code Practices](../references/code-practices.md#bumping-the-immich-version)
for the full version bumping workflow.

## Runtime Settings

The production Docker `CMD` uses uvicorn directly with mobile-friendly HTTP
settings and the modern WebSocket implementation. See
[Uvicorn Server Settings](../references/uvicorn-settings.md) before changing
runtime flags.

## Troubleshooting

### Service Does Not Start

- Confirm Render is using Docker mode and `./Dockerfile`.
- Confirm required environment variables are present.
- Check that the app binds to `0.0.0.0:$PORT`; do not hard-code `3001` in
  production commands.

### Health Check Fails

- Confirm `/api/server/ping` responds locally in the container.
- Check startup logs for dependency, settings, or import errors.
- Allow for normal cold-start time; the Dockerfile health check has a 30-second
  start period.

### Static Web UI Is Missing

- Confirm `ARG IMMICH_VERSION` references a valid
  `ghcr.io/immich-app/immich-server` tag.
- Confirm the upstream image still contains web assets at `/build/www`.
- Run the static asset inspection command above.

### Backend Calls Fail Locally

Inside Docker, `localhost` refers to the container, not the host. Use
`host.docker.internal` for host services, or add the Linux host-gateway mapping
shown above.
