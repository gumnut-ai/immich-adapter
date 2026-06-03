---
title: "Development Tools"
last-updated: 2026-06-03
---

# Development Tools

Tools for generating models, validating API compatibility, inspecting the OpenAPI spec, and keeping selected dependency surfaces current.

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

This generates the 300+ typed Pydantic v2 models the adapter imports, with field constraints derived from the OpenAPI schema.

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

This lets you debug or compare the adapter's OpenAPI spec without running a server.

## Dependency Update Automation

[`renovate.json`](../../renovate.json) configures Renovate for the dependency surfaces we want to keep moving automatically without turning every version bump into a weekly manual chore.

### What Renovate Manages

- **GitHub Actions** in `.github/workflows/`, grouped into a single `github-actions` update stream.
- **Dockerfile base images**, grouped into a single `container base images` update stream.

### Guardrails

- Renovate is limited to the `github-actions` and `dockerfile` managers.
- Updates must be at least **14 days old** before Renovate opens a PR (`minimumReleaseAge`).
- Renovate runs **before 6am on Monday**, which keeps dependency churn predictable.
- The dependency dashboard is enabled so maintainers can see pending updates in one place.

### Not Managed by Renovate

The `ghcr.io/immich-app/immich-server` image is intentionally excluded. The adapter treats the target Immich version as a coordinated compatibility decision, not a routine dependency bump, so update it manually via the workflow in [Code Practices](./code-practices.md#bumping-the-immich-version).
