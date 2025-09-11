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
- **API Docs**: http://localhost:8000/docs and http://localhost:8000/redoc

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
