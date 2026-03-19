---
title: "Development Tools"
last-updated: 2026-03-19
---

# Development Tools

Tools for generating models, validating API compatibility, and inspecting the OpenAPI spec.

For context on the adapter's data translation layer and which endpoints are implemented, see the [adapter architecture doc](../architecture/adapter-architecture.md).

## Pydantic Model Generator

The `generate_immich_models.py` tool generates type-safe Pydantic v2 models from the Immich OpenAPI specification.

### Usage

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

### Tag Substitution

When fetching the OpenAPI spec from a GitHub URL, the generator automatically substitutes the Immich version tag from the `.immich-container-tag` file. This ensures the generated models match the specific Immich version you're targeting.

**How it works:**

- When using a GitHub blob URL like `https://github.com/immich-app/immich/blob/main/...`, the generator reads `.immich-container-tag` (e.g., containing `v2.2.2`)
- It converts the URL to use the raw GitHub URL with the specific tag: `https://raw.githubusercontent.com/immich-app/immich/v2.2.2/...`
- The generated file includes a comment header showing which version was used
- If `.immich-container-tag` is missing or empty, it falls back to using `/main/` in the URL

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

## OpenAPI Specification Dumper

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
