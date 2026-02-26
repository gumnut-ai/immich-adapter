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
  --limit-concurrency 1000 \
  --backlog 2048
```

**Configuration Explanation:**

- `--timeout-keep-alive 75`: Sets keep-alive timeout to 75 seconds (iOS-friendly, matches typical mobile client expectations)
- `--limit-concurrency 1000`: Limits concurrent connections to prevent resource exhaustion
- `--backlog 2048`: Sets the socket backlog queue size for pending connections (helps with bursts of rapid requests)

**Why these settings matter for mobile clients:**

Mobile clients (especially iOS/Flutter) often make rapid successive requests and use longer keep alive settings. The production configuration:
- Keeps connections alive longer to enable HTTP connection reuse and prevent "Connection closed before full header was received" errors
- Handles bursts of concurrent requests without connection queueing

## Access the application

- **API**: http://localhost:3001 or http://localhost:8080 if using Docker
- **API Docs**: http://localhost:3001/docs and http://localhost:3001/redoc
- **OpenAPI Spec**: http://localhost:3001/openapi.json

## Running with Immich Web

To test the adapter end-to-end with the Immich web UI, you need three services running locally:

```
Browser (localhost:3000)
  → Vite dev server proxy (/api/*)
    → immich-adapter (localhost:3001)
      → photos-api (localhost:8000)
        → Clerk (OAuth provider)
```

### Prerequisites

1. **photos-api** running on `localhost:8000` — see the [photos-api README](../photos/photos-api/README.md) for setup
2. **Clerk OAuth configured** in photos-api — see the "Configure Clerk OAuth" section in the photos-api README
3. **immich-adapter** running on `localhost:3001` (see [Running the Application](#running-the-application) above)
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

## Development Commands

### Core Commands

- **Lint**: `uv run ruff check --fix`
- **Format**: `uv run ruff format`
- **Type check**: `uv run pyright`
- **Test**: `uv run pytest`
- **Test single file**: `uv run pytest tests/path/to/test_file.py::test_function_name`

## Development Tools

### Pydantic Model Generator

The `generate_immich_models.py` tool generates type-safe Pydantic v2 models from the Immich OpenAPI specification.

#### Usage

```bash
# Generate models from local file (default: immich.json)
uv run tools/generate_immich_models.py

# Generate from Immich repository URL with tag substitution - see below
uv run tools/generate_immich_models.py \
  --immich-spec https://github.com/immich-app/immich/blob/main/open-api/immich-openapi-specs.json

# Custom output location
uv run tools/generate_immich_models.py \
  --immich-spec immich.json \
  --output src/models.py
```

This generates 300+ Pydantic models with:

- Full type safety and validation
- Proper field constraints from OpenAPI schema
- Support for nested model relationships
- Modern Pydantic v2 syntax with `Annotated` fields

Generated models are used in FastAPI endpoints for request/response validation:

```python
from routers.immich_models import ServerFeaturesDto

@router.get("/features", response_model=ServerFeaturesDto)
async def get_features() -> ServerFeaturesDto:
    return ServerFeaturesDto(**features_data)
```

Always run linting and formatting on the generated model file before committing; the script will not do this by itself.

#### Tag Substitution

When fetching the OpenAPI spec from a GitHub URL, the generator automatically substitutes the Immich version tag from the `.immich-container-tag` file. This ensures the generated models match the specific Immich version you're targeting.

**How it works:**

- When using a GitHub blob URL like `https://github.com/immich-app/immich/blob/main/...`, the generator reads `.immich-container-tag` (e.g., containing `v2.2.2`)
- It converts the URL to use the raw GitHub URL with the specific tag: `https://raw.githubusercontent.com/immich-app/immich/v2.2.2/...`
- The generated file includes a comment header showing which version was used
- If `.immich-container-tag` is missing or empty, it falls back to using `/main/` in the URL

### API Compatibility Tool

The `validate_api_compatibility.py` tool ensures that immich-adapter correctly implements the Immich API endpoints.

### Usage

```bash
# Compare specific endpoints
uv run tools/validate_api_compatibility.py \
  --endpoints=server,users \
  --immich-spec=https://github.com/immich-app/immich/blob/main/open-api/immich-openapi-specs.json \
  --adapter-spec=http://localhost:3001/openapi.json

# Compare all endpoints
uv run tools/validate_api_compatibility.py \
  --immich-spec=https://github.com/immich-app/immich/blob/main/open-api/immich-openapi-specs.json \
  --adapter-spec=http://localhost:3001/openapi.json

# Use local specification files
uv run tools/validate_api_compatibility.py \
  --endpoints=server \
  --immich-spec=./immich-openapi.json \
  --adapter-spec=./adapter-openapi.json

# Show verbose output including info-level differences
uv run tools/validate_api_compatibility.py \
  --endpoints=server \
  --immich-spec=https://github.com/immich-app/immich/blob/main/open-api/immich-openapi-specs.json \
  --adapter-spec=http://localhost:3001/openapi.json \
  --verbose
```

### Exit Codes

The tool returns an exit code equal to the number of error-level incompatibilities found:

- `0`: All specified endpoints are compatible
- `>0`: Number of incompatible differences found

### CI Integration

The API compatibility check runs automatically in GitHub Actions on:

- Push to main branch
- Pull requests
- Manual workflow dispatch

The workflow checks the `server` endpoint by default, but this can be customized via workflow inputs.

### OpenAPI Specification Dumper

The `dump_openapi_json.py` tool dumps the OpenAPI specification from the FastAPI app:

```bash
# Dump to stdout
uv run tools/dump_openapi_json.py

# Save to file
uv run tools/dump_openapi_json.py > openapi_spec.json

# Use with the compatibility validator
uv run tools/dump_openapi_json.py > /tmp/spec.json
uv run tools/validate_api_compatibility.py \
  --endpoints=server \
  --immich-spec=https://github.com/immich-app/immich/blob/main/open-api/immich-openapi-specs.json \
  --adapter-spec=/tmp/spec.json
```

This is useful for:

- Debugging OpenAPI spec generation
- Comparing specs without running a server
- Offline analysis of the API specification

## Code Style and Best Practices

### Python Style Guide

- **Type Hints**: Use modern Python 3.12+ syntax (`int | None` instead of `Optional[int]`)
- **Naming**: Use `snake_case` for all variables, functions, and SQLAlchemy model attributes
- **Imports**: Always place imports at the top of files (inline imports only to prevent circular dependencies)
- **Dependencies**: Use `uv` for dependency management, not pip or poetry
- **Versioning**: Version dependencies appropriately in `pyproject.toml`

### Immich API Integration

#### HTTP Response Status Codes

Always use `fastapi.status` constants for `statusCode` - never use just the numeric value

```python
# In route handlers:
raise HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Human-readable error description"
)

# Resulting JSON response:
# {"message": "...", "statusCode": 401, "error": "Unauthorized"}
```

#### Error Response Format

- All HTTP error responses must use Immich's expected format, not FastAPI's default:

```json
{
  "message": "Human-readable error description",
  "statusCode": 401,
  "error": "Unauthorized"
}
```

- `message`: Description of what went wrong
- `statusCode`: HTTP status code (duplicated in body for client convenience)
- `error`: HTTP status phrase (e.g., "Bad Request", "Unauthorized", "Internal Server Error")

This format is enforced by the global exception handler in `config/exceptions.py`. Raise `HTTPException` with a `detail` message and the handler will format it correctly.

**Note:** In middleware (e.g., `auth_middleware.py`), you must return `JSONResponse` directly with this format, as `HTTPException` raised in `BaseHTTPMiddleware.dispatch()` is not caught by FastAPI's exception handlers due to Starlette's middleware architecture.

#### Defining Endpoint Parameters

- Use `Annotated` to specify attributes, such as `Query()`, `Path()`, `Body()` functions, or numeric or string validations, but do not use `Default` - the default value should be specified as part of the Python declaration.
- If a parameter is not required, use `| SkipJsonSchema[None]` after defining the type to allow Pydantic to accept the `None` type, but prevent `None` from being exposed in the OpenAPI schema. 
- If the exposed parameter name needs to be camelCase, use `alias="camelCase"` within the function and then use an appropriate snake_case name for the parameter in the function signature.

Example:
```python
asset_id: Annotated[UUID | SkipJsonSchema[None], Query(alias="assetId")] = None,
```

When implementing new endpoints:

1. **Generate models**: Use `generate_immich_models.py` to create up-to-date Pydantic models
2. **Import models**: Use generated models from `routers.immich_models` for type safety
3. **Defining parameters**: When declaring parameters, use `Annotated` to specify attributes, such as `Query` or numeric validations, but do not use `Default` within Annotated. If a type `asset_id: Annotated[UUID | SkipJsonSchema[None], Query(alias="assetId")] = None,`
4. **Validate compatibility**: Run `validate_api_compatibility.py` to ensure correct implementation
5. **Test endpoints**: Verify responses match Immich API expectations

### Testing Guidelines

- All tests should be async and use `@pytest.mark.anyio` decorator
- Run tests from the project directory, not repository root
- Use model factories for test data creation

### Logging Best Practices

Use structured logging with the `extra` parameter when logging model IDs:

```python
logger.info(f"Created library {library.id}", extra={"library_id": library.id})
```

This enables better searching and correlation in Sentry.

## Immich Monitoring and Logging

The goal of the adapter is to implement the Immich OpenAPI as accurately as possible. At times you will find that the Immich documentation does not deeply describe the format of data returned by endpoints or the actual data itself (such as `/sync/stream`).

### Immich Web Client

The Immich web client is easy to monitor - use the developer tools in the web browser of your choice.

### Immich Mobile Clients

To monitor a mobile client, you will need a proxy server to be "in the middle" between the mobile client and the server. Unfortunately Immich uses the Flutter framework which is able to bypass the system proxy settings - your proxy server will not see any of calls from the client to the server.

To get around this, you'll need to set up a reverse proxy - the mobile client thinks it is talking to the Immich server, but it is actually talking to a proxy server on your development machine (which logs the traffic) which then forwards the traffic on to the actual Immich server.

#### Example Generic Reverse Proxy Setup

* Choose local listen endpoint: `http://<dev-machine-ip>:<port>`
* Configure upstream: `https://<real-immich-host>:<port>`
* Set Immich mobile "Server Endpoint URL" to the local listen endpoint
* Ensure device can reach dev machine IP (same Wi‑Fi/VPN)
* _TLS note:_ if intercepting HTTPS, you may need to trust a local CA on the device and set "Allow self-signed SSL certificates" in the Advanced section of the mobile client Settings; if not intercepting, use simple pass-through/forwarding mode

#### Example Proxyman Setup

* Select "Reverse Proxy..." from the "Tools Menu"
* Check "Enable Reverse Proxy Tool" if not already checked
* Click "+" in the lower left to create a new reverse proxy
* Specify a name for the proxy, the local port, the remote host or IP address, and the remote port
* If you are using OAuth with the Immich mobile client, you will need to run immich-adapter with a SSL certificate, and you will need to check "Force Using SSL when connecting to Remote Port"
* Click "Add" to create and start the reverse proxy

With Proxyman, if you are using OAuth, you will not specify https for the protocol of the immich-adapter server as the SSL connection is handled by Proxyman.
