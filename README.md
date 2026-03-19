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
```

The default should be good enough to get started with, but feel free to take a look at modify it.

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
docker run --rm -p 8080:8080 \
  -e PORT=8080 \
  -e GUMNUT_API_BASE_URL=http://host.docker.internal:8000 \
  -e ENVIRONMENT=development \
  immich-adapter
```

**Important:** Use `host.docker.internal` instead of `localhost` to access services running on your host machine from within the container.

**Note:** `host.docker.internal` does not work natively in Linux. Add `--add-host=host.docker.internal:<host-gateway>` where `<host-gateway>` is default gateway of the Docker bridge network, which is usually `172.17.0.1`.

**Environment Variables:**
- `PORT`: Port to bind to (default: 8080)
- `GUMNUT_API_BASE_URL`: URL of the Gumnut API backend
- `OAUTH_MOBILE_REDIRECT_URI`: Custom URL scheme for mobile app deep linking during OAuth flow (default: app.immich:///oauth-callback)
- `ENVIRONMENT`: Set to `development` or `production`
- `LOG_LEVEL`: Log level (default: `info`, options: `debug`, `info`, `warning`, `error`)

**Build with custom Immich version:**
```bash
docker build --build-arg IMMICH_VERSION=v2.2.3 -t immich-adapter .
```

### Production Mode

For production deployments or when testing with mobile clients (iOS/Android), use these optimized settings:

```bash
uv run fastapi run --port 3001 \
  --timeout-keep-alive 75 \
  --limit-concurrency 200 \
  --backlog 2048
```

See [docs/references/uvicorn-settings.md](docs/references/uvicorn-settings.md) for details on these settings and why they matter for mobile clients.

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
- [Immich Monitoring](docs/references/immich-monitoring.md) — inspecting mobile/web traffic

## References

- [Architecture](docs/architecture/adapter-architecture.md) — how the adapter works: auth, data translation, pagination, sync, error handling
- [Code Practices](docs/references/code-practices.md) — Python style, endpoint patterns, testing, logging
- [Development Tools](docs/references/development-tools.md) — model generator, API compatibility, OpenAPI spec
- See [docs/](docs/) for design docs and more
