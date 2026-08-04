# Immich Adapter for the Gumnut API

A FastAPI server that exposes endpoints compatible with the Immich API,
then calls out to Gumnut on the backend. The overall goal is to make
Gumnut compatible with the Immich ecosystem of apps and integrations.

## Getting Started

1. **Install uv**

```bash
curl -sSf https://astral.sh/uv/install.sh | bash
```

Or see: https://docs.astral.sh/uv/getting-started/installation/

2. **Install application dependencies**

```bash
uv sync
```

3. **Configure application environment**

```bash
cp .env.example .env

# Generate a Fernet key, then put the output in SESSION_ENCRYPTION_KEY in .env.
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The copied file supplies development defaults, but two adapter-owned prerequisites
still need to be ready before startup:

- `SESSION_ENCRYPTION_KEY` must contain the generated Fernet key. Keep this value
  stable across restarts so existing sessions can still be decrypted, and do not
  commit it.
- Redis must be running at `REDIS_URL` (the default is
  `redis://localhost:6379/1`). The adapter pings Redis during startup, so a
  missing or unreachable Redis instance prevents the application from starting.

The Gumnut API must also be reachable at `GUMNUT_API_BASE_URL`; the development
default is `http://localhost:8000`.

For a quick local Redis instance, run:

```bash
docker run --rm --name immich-adapter-redis -p 6379:6379 redis:7
```

## Running the Application

### Development Mode

```bash
uv run fastapi dev --port 3001
```

You can run the app on other ports, of course, but we picked 3001 here
to avoid conflicting with other apps commonly run alongside `immich-adapter`.

### Running with Docker

Build and run the application in a Docker container:

```bash
# Build the image
docker build -t immich-adapter .

# Run the container
export SESSION_ENCRYPTION_KEY="$(uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

docker run --rm -p 8080:8080 \
  -e PORT=8080 \
  -e GUMNUT_API_BASE_URL=http://host.docker.internal:8000 \
  -e REDIS_URL=redis://host.docker.internal:6379/1 \
  -e SESSION_ENCRYPTION_KEY="$SESSION_ENCRYPTION_KEY" \
  -e ENVIRONMENT=development \
  immich-adapter
```

The generated key is held in the shell only for this example. Persist the same
value in your deployment secret store when sessions must survive container
restarts. This example assumes the Gumnut API and Redis are running on the host
at ports `8000` and `6379`.
If Redis runs in another container, put both containers on a shared Docker
network and use the Redis container's network name in `REDIS_URL` instead.

**Important:** Use `host.docker.internal` instead of `localhost` to access services running on your host machine from within the container.

**Note:** `host.docker.internal` does not work natively in Linux. Add `--add-host=host.docker.internal:<host-gateway>` where `<host-gateway>` is default gateway of the Docker bridge network, which is usually `172.17.0.1`.

**Environment Variables:**
- `PORT`: Port to bind to (default: 8080)
- `GUMNUT_API_BASE_URL`: URL of the Gumnut API backend
- `REDIS_URL`: Redis connection URL (default: `redis://localhost:6379/1`)
- `SESSION_ENCRYPTION_KEY`: Required Fernet key for encrypting stored sessions
- `OAUTH_MOBILE_REDIRECT_URI`: Custom URL scheme for mobile app deep linking during OAuth flow (default: app.immich:///oauth-callback)
- `TRASH_RETENTION_DAYS`: Trash retention window surfaced to Immich clients as `trashDays` (default: `90`)
- `ENVIRONMENT`: Set to `development` or `production`
- `LOG_LEVEL`: Log level (default: `info`, options: `debug`, `info`, `warning`, `error`)

**Build with a specific Immich version:**
```bash
docker build --build-arg IMMICH_VERSION=v3.1.0 -t immich-adapter .
```

### Production Mode

For production deployments or when testing with mobile clients (iOS/Android), use these optimized settings:

```bash
uv run uvicorn main:app --port 3001 \
  --ws websockets-sansio \
  --timeout-keep-alive 75 \
  --limit-concurrency 200 \
  --backlog 2048
```

This invokes `uvicorn` directly because the `fastapi` CLI doesn't expose `--ws`, and the default `--ws auto` routes Socket.IO through a deprecated WebSocket transport. See [docs/references/uvicorn-settings.md](docs/references/uvicorn-settings.md) for details on each flag.

## Access the Application

- **API**: http://localhost:3001 or http://localhost:8080 if using Docker
- **API Docs**: http://localhost:3001/docs and http://localhost:3001/redoc
- **OpenAPI Spec**: http://localhost:3001/openapi.json

## Development Commands

- **Lint**: `uv run ruff check --fix`
- **Format**: `uv run ruff format`
- **Type check**: `uv run pyright`
- **Test**: `uv run pytest`
- **Test single file**: `uv run pytest tests/path/to/test_file.py::test_function_name`

## Guides

- [Running with Immich Web](docs/guides/running-with-immich-web.md) — static files or dev server
- [Running with Immich Mobile](docs/guides/running-with-immich-mobile.md) — HTTPS setup with mkcert
- [Importing with immich-go](docs/guides/importing-with-immich-go.md) — bulk-importing a library with `x-api-key` auth via a Gumnut API key

## References

- [Architecture](docs/architecture/adapter-architecture.md) — how the adapter works: auth, data translation, pagination, sync, error handling
- [Code Practices](docs/references/code-practices.md) — Python style, endpoint patterns, testing, logging
- [Documentation Conventions](docs/references/documentation-conventions.md) — frontmatter, maps, lifecycle, paths, and freshness
- [Development Tools](docs/references/development-tools.md) — model generator, API compatibility, OpenAPI spec, dependency automation
- [GitHub Actions Best Practices](docs/references/github-actions-best-practices.md) — workflow security and review rules
- See [docs/](docs/) for design docs and more
