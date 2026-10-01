---
title: "Project Conventions"
last-updated: 2026-10-01
---

# Project Conventions

## Python Style Guide

- **Type Hints**: Use modern Python 3.12+ syntax (`int | None` instead of `Optional[int]`). Add type annotations to all function parameters and return types.
- **Type narrowing — overloads, not asserts**: When a helper returns `T | None` but a specific call site is guaranteed to receive non-None input, narrow with `@overload` decorators on the helper (`@overload def f(x: T) -> T; @overload def f(x: None) -> None; @overload def f(x: T | None) -> T | None`) rather than `assert x is not None` at the call site. Asserts are stripped under `python -O`, obscure whether the None branch is actually reachable, and only narrow at one site instead of helping every caller. See `to_actual_utc` / `to_immich_local_datetime` in `routers/utils/datetime_utils.py` for the pattern. For genuine runtime defense (input that *can* be invalid), use exceptions, not `assert`.
- **Named types over bare tuples**: When a function returns multiple values whose positional meaning is ambiguous (e.g. two `int | None` a caller could transpose), return a small `NamedTuple` (or dataclass) with named fields and a docstring rather than a bare `tuple[...]`. Named fields make call sites self-documenting and let the type carry what `None` means. See `ImmichUserQuota` / `map_user_quota` in `routers/utils/current_user.py`.
- **Naming**: Use `snake_case` for all variables, functions, and SQLAlchemy model attributes
- **Imports**: Always place imports at the top of files (inline imports only to prevent circular dependencies)
- **Dependencies**: Use `uv` for dependency management, not pip or poetry. Version dependencies appropriately in `pyproject.toml`.
- **Running Python**: Always use `uv run` to execute Python commands (e.g., `uv run pytest`, `uv run python`). Never use bare `python` or `pip` — pyenv versions may not match `.python-version`.

## Project Conventions

- **Single source of truth for rationale**: explain a non-obvious *why* once; cite it elsewhere in one line rather than restating (copies multiply churn and drift). Home by scope: one symbol → its docstring; spanning files → one reference doc; a decision → the design doc. Never link to a doc *and* restate it.
- **Comments earn their place**: explain what the code can't. Delete the road not taken ("did X not Y", unless Y is a trap someone will re-reach for), a "today" coincidence ("A equals B today" — enforce it in code if it's load-bearing, else cut it), and prose restating what the structure already shows (an explicit dict entry already means "explicit, not the default").
- **Committed text must stand on its own**: This is a public repository, so everything committed (code comments, docstrings, test names and docstrings, design docs under `docs/design-docs/`, `docs/architecture/`, `docs/references/`, `docs/guides/`, plus PR bodies and commit messages) has to be usable by a reader who can't open a Gumnut ticket or a private sibling repo. This is not a secrecy rule — tracker IDs and internal project names aren't confidential, and scrubbing them is not the goal. The goal is that a pointer to something unreachable carries no information, so the reasoning has to live *here*. Three habits, one rule:
  - **Write the rationale, don't cite the ticket.** "GUM-713" tells an outside reader nothing they can act on; "the recently shipped end-to-end Range path" tells them what they need. An ID may ride along where it genuinely helps a teammate, but never as the only explanation — and let the Linear issue link to the PR rather than the other way around.
  - **Describe the contract, don't cite a private path.** Instead of `photos-api/routers/...`, state the behavior this adapter relies on. That note is also the more durable one: it survives the other repo's refactors.
  - **Use the backend's public name.** **The Gumnut API** (`api.gumnut.ai`) — or "the Gumnut backend" where generic phrasing reads better — never `photos-api`, the internal project name, which an outside reader can't map to anything. Applies to comments, docstrings, tests, and docs alike, and to identifiers that embed the name (`GUMNUT_API_MAX_PAGE_SIZE`, not `PHOTOS_API_MAX_PAGE_SIZE`).

  Existing references predate this rule; sweep them out opportunistically when editing nearby content. (The literal `photos-api` / `GUM-713` tokens in this bullet are deliberate examples of what to avoid.) Redacting PII from captured example data *is* a confidentiality rule, and a separate one — see `AGENTS.md`.
- **Module organization**: `services/` is for stateful classes with methods (stores, pipelines, WebSocket handlers). `utils/` is for stateless utility functions and helpers. Don't put classes with state in `utils/`.
- **Constructor parameters**: Accept specific parameters rather than the full `Settings` object in constructors. This keeps classes decoupled from the config layer and easier to test.
- **Branching**: Always create a new branch from `main` before making changes. Name it after the change (`fix-upload-timeout`), not after a ticket — a descriptive name is more useful in a branch list to everyone, inside the team and out. Don't modify files on an existing feature branch for unrelated work.
- **File editing**: Always read a file before editing it. Never edit historical database migration files.
- **Datetime handling**: When working with datetimes as strings, ensure the proper format is used, as Immich has different formats for different use cases. If you cannot determine the proper format to use, ask for clarification.
- **Asset date and DTO normalization rules**: Follow [Asset Field Conversion](asset-field-conversion.md#asset-date-fields-and-dto-normalization).
- **Immich local-wall-clock query values**: Follow [Route and DTO Contracts](route-and-dto-contracts.md#immich-web-today-wire-format).

## Pull Requests

- When updating pull requests with additional commits, update the PR description to include the latest changes
- Always run tests and formatting before creating a PR
