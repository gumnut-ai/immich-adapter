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

```bash
uv run fastapi dev --port 3001
```

You can run the app on other ports, of course, but we picked 3001 here
to avoid conflicting with other apps commonly run alongside `immich-adapter`.

## Access the application

- **API**: http://localhost:3001
- **API Docs**: http://localhost:3001/docs and http://localhost:3001/redoc
- **OpenAPI Spec**: http://localhost:3001/openapi.json

## API Compatibility Tool

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

## Development Commands

### Core Commands

- **Lint**: `uv run ruff check --fix`
- **Format**: `uv run ruff format`
- **Type check**: `uv run pyright`
- **Test**: `uv run pytest`
- **Test single file**: `uv run pytest tests/path/to/test_file.py::test_function_name`

## Code Style and Best Practices

### Python Style Guide

- **Type Hints**: Use modern Python 3.12+ syntax (`int | None` instead of `Optional[int]`)
- **Naming**: Use `snake_case` for all variables, functions, and SQLAlchemy model attributes
- **Imports**: Always place imports at the top of files (inline imports only to prevent circular dependencies)
- **Dependencies**: Use `uv` for dependency management, not pip or poetry
- **Versioning**: Version dependencies appropriately in `pyproject.toml`

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
