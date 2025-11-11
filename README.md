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
- `ENVIRONMENT`: Set to `development` or `production`
- `LOG_LEVEL`: Log level (default: `info`, options: `debug`, `info`, `warning`, `error`)

**Build with custom Immich version:**
```bash
docker build --build-arg IMMICH_VERSION=v2.2.3 -t immich-adapter .
```

## Access the application

- **API**: http://localhost:3001 or http://localhost:8080 if using Docker
- **API Docs**: http://localhost:3001/docs and http://localhost:3001/redoc
- **OpenAPI Spec**: http://localhost:3001/openapi.json

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

# Generate from Immich repository URL
uv run tools/generate_immich_models.py \
  --immich-spec https://raw.githubusercontent.com/immich-app/immich/main/open-api/immich-openapi-specs.json

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

Defining endpoint parameters:

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
