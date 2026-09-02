---
title: "Development Tools"
last-updated: 2026-09-01
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

For GitHub blob URLs, the generator substitutes the tag from `.immich-container-tag`. The tag file must be non-empty; the generator does not fall back to a branch. The generated file's header records the resolved version.

To generate models for a different version, pass its explicit `raw.githubusercontent.com/immich-app/immich/<tag>/open-api/immich-openapi-specs.json` URL.

The generator's `datamodel-code-generator` dependency is unpinned (`>=0.25.0`, resolved fresh by `uv run`), so a regeneration can carry codegen-version stylistic churn (e.g. the `date`→`date_aliased` import alias) independent of any spec change — expected, not a wire change. Validate a regeneration diff against the targeted spec's known changes, not against an assumption that every hunk is spec-driven.

### Constraint Preprocessing

Before handing the spec to `datamodel-code-generator`, the generator drops constraints codegen would misapply to non-string types — currently `pattern` on schemas whose `format` maps to a non-string type (`uuid`, `date-time`, `date`, `time`), which otherwise yields `UUID` / `AwareDatetime` / `date` / `time` fields that raise `TypeError` at value validation under the pinned pydantic (and it collapses the now-redundant `RootModel[UUID]` id wrappers into plain `UUID`). Patterns on genuine string fields are kept. See `strip_non_string_patterns` in `tools/spec_preprocess.py`; if a future spec trips the same class of error for another non-string `format`, add it to `_NON_STRING_PATTERN_FORMATS` rather than hand-editing the generated file.

### After Regenerating: Sweep Stub Breakage via Pyright

A regeneration that **retypes** a field (e.g. `str` → `UUID` ids) silently turns hardcoded literals in stub endpoints into latent 500s — stubs have no test coverage, so the suite stays green while the endpoint fails response validation on every call. Don't hunt these by grep (partial sweeps have missed sites repeatedly); enumerate them from pyright's error list — `Literal['...'] cannot be assigned to parameter ... of type UUID` pinpoints every offending literal. Dynamic `str(...)`-of-UUID values coerce fine at runtime and are style cleanup, not defects; invalid *literals* are the class that 500s.

A regen that makes a field **required** breaks the same stubs through a different error — `Argument missing for parameter "<name>"` at every hand-construction site. Sweep it the same way: for a stub with no smoke test yet, pyright is the only pre-runtime signal. Note the limits of that signal — it catches a missing required argument and an *incompatible* retype (`str` → `UUID`), but **not** a *widening* one (`int` → `float` still accepts an int literal) and **not** a tightened `Field(pattern=…/min_length=…/ge=…/le=…)` constraint. A widening retype is harmless by itself; the hazard is a constraint an existing literal now violates, which fails only at value validation — so the construction smoke test [routes and compatibility reference](./routes-dtos-and-upstream-compatibility.md#bumping-the-immich-version) prescribes is the backstop. The v3.0.3 retarget's `percentageLimit` (`int` → `float`, `le=9007199254740991` → `le=1.0`) is the near-miss that shows why: pyright saw nothing, and the stub's `percentageLimit=1` stayed valid only because upstream's default sits exactly on the new bound.

Pyright also misses a change from `T | None = None` to `T = <default>`: presence checks remain type-correct but always succeed. Review regenerated default changes and test whether omission should forward the schema default or remain distinguishable through `model_fields_set`; see [Omit vs explicit-null](./routes-dtos-and-upstream-compatibility.md#omit-vs-explicit-null-in-update-style-dtos--use-model_fields_set).

## API Compatibility Tool

The `validate_api_compatibility.py` tool ensures that immich-adapter correctly implements the Immich API endpoints.

### Usage

```bash
# Compare specific endpoints (omit --endpoints to compare all)
uv run tools/validate_api_compatibility.py \
  --endpoints=server,users \
  --immich-spec=https://github.com/immich-app/immich/blob/main/open-api/immich-openapi-specs.json \
  --adapter-spec=http://localhost:3001/openapi.json
```

Both `--immich-spec` and `--adapter-spec` accept local file paths as well as URLs. Run with `--help` for the full flag set (e.g., `--verbose` for info-level differences).

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

The `dump_openapi_json.py` tool prints the adapter's OpenAPI specification from the FastAPI app to stdout, without running a server:

```bash
uv run tools/dump_openapi_json.py | sed -n '/^{/,$p' > /tmp/spec.json
```

Importing the app emits log lines to **stdout** ahead of the JSON, so a bare `> /tmp/spec.json` yields a file the validator rejects (`Extra data: line 1 column 5`). Strip everything before the first `{`, then feed the result to the compatibility validator via `--adapter-spec=/tmp/spec.json`.

The script runs in its own isolated PEP 723 environment, not the project venv, so its inline `dependencies` header is a **parallel copy** of every package the app pulls in at import time. Adding a `pyproject.toml` dependency that any module imports at import time (directly or transitively from `main`) requires adding it to the script header too — otherwise the check-compatibility CI job fails with `Error importing main app: No module named '...'` while every other check passes.

## Gumnut SDK Release Lag

The generated `gumnut-sdk` can trail the deployed Gumnut API. When an endpoint
documented in the live spec (`https://api.gumnut.ai/openapi.json`) is missing
from the installed SDK, check the latest release on PyPI before designing a
raw-client workaround — the typed method may have shipped in a version this
repo hasn't picked up yet (asset-version `append`/`replace` landed in 0.159.0
this way).

## Dependency Update Automation

[`renovate.json`](../../renovate.json) configures Renovate for the dependency surfaces we want to keep moving automatically without turning every version bump into a weekly manual chore.

### What Renovate Manages

- **GitHub Actions** in `.github/workflows/`, grouped into a single `github-actions` update stream.
- **Dockerfile base images**, grouped into a single `container base images` update stream.

### Guardrails

Renovate is limited to the `github-actions` and `dockerfile` managers, and gates PRs behind a `minimumReleaseAge` and a weekly `schedule` to keep dependency churn predictable. The exact values live in [`renovate.json`](../../renovate.json) (`minimumReleaseAge`, `schedule`, `dependencyDashboard`).

### Not Managed by Renovate

The `ghcr.io/immich-app/immich-server` image is intentionally excluded. The adapter treats the target Immich version as a coordinated compatibility decision, not a routine dependency bump, so update it manually via the [Upgrading the Immich Target Version](../guides/upgrading-immich-version.md) guide, which ends with the pin-bump mechanics in [Routes, DTOs, and Upstream Compatibility](./routes-dtos-and-upstream-compatibility.md#bumping-the-immich-version).
