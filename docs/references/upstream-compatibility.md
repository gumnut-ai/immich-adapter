---
title: "Upstream Compatibility"
last-updated: 2026-10-01
---

# Upstream Compatibility

## Verifying upstream behavior — read the Immich source, not just the spec

The OpenAPI spec pins request/response *shapes*. It says nothing about the behavior clients actually depend on: which query params a given view sends, what the server filters or computes before answering, what the client does with a field once it has it. Adapter parity bugs live in that gap, and neither the spec nor recall closes it — check a local checkout of `immich-app/immich` at the tag in `.immich-container-tag`. Read it as `git show <tag>:<path>` rather than off the checkout's working tree, which tracks whatever version that clone was last left on and will answer for the wrong release without saying so (`git fetch --tags origin <tag>` first if it resolves). `server/src/repositories/` and `server/src/services/` for what the server computes; `web/src/lib/` and `web/src/routes/` for what the client sends and renders.

Generated models can also omit upstream zod refinements. Before synthesizing values, check the pinned upstream schema rather than relying only on `routers/immich_models.py`; edit-row UUID versions and rotation angles are examples.

Treat any claim about upstream behavior in a task description as a hypothesis until it is read there, including one you wrote yourself. The timeline stack work was specified on the premise that the Immich *client* collapses a burst using the per-asset tuple; upstream collapses server-side and the client only draws a badge, so building to the spec as written would have shipped a badge on every frame of every burst.

## Bumping the Immich Version

This section covers the mechanics of moving the pin. Evaluating a new release first (spec diff, endpoint classification, client and SDK checks) is the [Upgrading the Immich Target Version](../guides/upgrading-immich-version.md) guide.

The Immich version the adapter targets is pinned in **two** files that must be kept in sync:

1. `.immich-container-tag` — read at runtime by `config/immich_version.py`, by `tools/generate_immich_models.py` when regenerating models from the OpenAPI spec, and by `scripts/extract-immich-web.py` when extracting web assets locally.
2. `Dockerfile`'s `ARG IMMICH_VERSION` — pulls `ghcr.io/immich-app/immich-server:${IMMICH_VERSION}` in the build stage to copy static web files into the image, and stamps the `immich.version` OCI label.

The two are not auto-synced, but CI enforces that they match (see the `check-immich-version-sync` job in `.github/workflows/ci.yml`). Render builds the image automatically from the repo without any way to inject a build-arg sourced from `.immich-container-tag`, so the Dockerfile default is what ships to production. When bumping the Immich version:

1. Update `.immich-container-tag`
2. Update the `ARG IMMICH_VERSION` default and the "Last updated" comment in `Dockerfile`
3. Regenerate `routers/immich_models.py` (see [development tools](development-tools.md))

Forgetting step 2 causes silent drift — the served web UI stays on the old Immich version while the API models advance.

A regen can add newly-required fields to (or retype) the generated DTOs, breaking endpoint stubs that hand-construct them at **runtime** (pydantic `ValidationError` → 500); these stubs have no callers in most tests, so the break hides until a client hits the route. Keep a construction smoke test per hand-built-DTO stub — `assert isinstance(await <endpoint>(), <Dto>)`, see `tests/unit/api/test_{system_config,jobs,license}.py` — and, as with an SDK bump, run the **full** `uv run pytest` after regenerating.

Audit new search-DTO fields against `_ENUMERATION_HONORABLE_FIELDS` in `routers/api/search.py`. Add fields that only order, paginate, or shape results; unlisted fields are treated as restricting and route criterion-less requests to a search call the Gumnut API rejects.

## Bumping the Gumnut SDK

The SDK is auto-generated (Stainless), so a version bump can add **newly-required** fields to response models (e.g. `FaceResponse.source` arrived in 0.116). Tests construct these models directly as fixtures, so a bump can break suites unrelated to the endpoint you're touching. Run the **full** `uv run pytest` after a bump (not just the changed endpoint's tests), and when a required field is added, `grep` the tests for `<Model>(` to fix every direct construction.

A bump can also *close* gaps silently: grep for raw `client.post(` / `client.delete(` call sites and migrate any whose typed method has now landed (see [Bulk-ID Endpoints](bulk-id-operations.md#bulk-id-endpoints)). Nothing fails to prompt this — a raw call keeps working forever.

Some SDK contracts are already pinned by committed tests, so a bump that breaks them fails the suite rather than waiting to be caught by hand. `tests/unit/utils/test_stack_conversion.py` does this for the stack resource — asserting each method still accepts the parameters the stack routes are being built to pass, and that each stack response class still carries the fields `GumnutStackRow` reads. Note what that guard does and doesn't cover: it catches a **renamed or dropped** parameter, not a newly-**required** one, so the full-suite run above is still what surfaces those. Extend the pattern when adding a surface whose SDK contract pyright can't check.

## Implementing New Endpoints

1. **Generate models**: Use `generate_immich_models.py` to create up-to-date Pydantic models (see [development tools](development-tools.md))
2. **Import models**: Use generated models from `routers.immich_models` for type safety
3. **Define parameters**: Follow [Defining Endpoint Parameters](route-and-dto-contracts.md#defining-endpoint-parameters)
4. **Verify parameter semantics**: Check the Immich OpenAPI spec (`https://api.immich.app/endpoints/`) or source code (`immich/server/src/controllers/*.controller.ts` and the matching service) to confirm what each URL path and body parameter represents. URL `{id}` parameters don't always refer to the entity in the URL collection — face/person reassign endpoints in particular swap the natural reading. Both of these accept the **target person** as `{id}` in the path:
   - `PUT /people/{id}/reassign` — `{id}` is the target person (reassign TO); body items are sources.
   - `PUT /faces/{id}` — `{id}` is the target person (reassign TO); body `FaceDto.id` is the face being reassigned.
   - When fixing a path/body or ID-decoding bug in one handler, audit sibling handlers in the same router (and adjacent routers) for the same trap before closing the fix. A one-line search (`grep -rn` for the pattern) is cheap insurance against the same class-of-bug recurring.
   - The spec also can't express **response-shape guarantees** — array ordering, or which rows a collection field includes — and those live in the mapper and repository rather than the controller. At `v3.0.3`, `server/src/dtos/stack.dto.ts::mapStack` partitions so `assets[0]` is always the primary, and `server/src/repositories/stack.repository.ts` filters `deletedAt is null` so the array is live-only; clients depend on both. A field the spec types as a plain array can carry either guarantee, so read the mapper and repository for any collection field the adapter populates.
   - For loosely-specified response values (free-form strings, group/field names), also check how the Immich **clients** consume the response (`immich/web/src/` and `immich/mobile/lib/`) — clients hard-match values the OpenAPI spec doesn't constrain. E.g., the explore page renders only the group with `fieldName == "exifInfo.city"`, so a spec-valid response with different group names would silently render nothing.
   - The same duty applies to any **claim about client behavior you write down** — in a comment, docstring, test name, or doc — not just to implementation decisions. "Web passes `X`" / "no client reads this field" are plausible-sounding and frequently wrong: one param is typically passed differently across its call sites — `withHidden` is passed both ways, and omitted entirely by some callers — so what one caller does rarely generalizes. Grep the fork for **every** caller before asserting or refuting one, and don't commit the resulting tally: it rots on the next fork sync with nothing in CI pinning it.
   - The spec is **not the full route surface**: `@ApiExcludeEndpoint` routes are absent from OpenAPI, generated clients, and spec diffs. Check the controller when determining whether a route exists.
5. **Validate compatibility**: Run `validate_api_compatibility.py` to ensure correct implementation
6. **Test endpoints**: Verify responses match Immich API expectations
7. **Audit `/me/preferences` for a gating boolean**: Many client UI features (memories, tags, ratings, folders, people, shared links, email notifications, cast) are gated client-side on a flag in `UserPreferencesResponseDto`. The default in `routers/api/users.py::userPreferencesResponse` ships most of these as `enabled=False`, which silently hides the corresponding UI even after the backing endpoints are wired up. When implementing an endpoint that backs a client UI feature, grep `routers/api/users.py` for the matching preference field and flip its `enabled` to `True`. The Immich web client checks these via `$preferences?.<area>?.enabled`; missing the flip means the new endpoints become dead code on the client.
8. **Audit `routers/api/server.py::server_features`**: Many client UI features are also gated by a server-feature flag advertised via `GET /server/features`. When promoting an area from stub to a real implementation, flip the matching key from `False` to `True` and update the explanatory comment so it scopes only to the remaining stubbed sub-features. Leaving the flag at `False` after implementing the endpoint silently hides the UI; flipping it to `True` while parts of the area are still stubbed surfaces non-functional UI.
9. **Update the feature-compatibility and architecture references**: Promoting an endpoint from stub to real — **or dropping an endpoint removed upstream** — changes the current compatibility record:
   - `docs/references/feature-compatibility.md` owns feature-area classification and policy. Update it when an area moves between supported/current, product-dependent, deferred compatibility gap (a non-commitment), or intentional unsupported. Do not copy exact routes, schemas, model fields, or feature-flag values into it; those remain owned by code and generated models.
   - `docs/architecture/adapter-architecture.md` owns current system boundaries, translation rules, collection shapes, routing paths, and failure behavior. Update it only when the endpoint changes one of those architectural contracts; it is not an endpoint catalog.
   - Search the feature name and removed path/handler across `docs/` and this reference for other summaries, rationale, feature-gate claims, or caller lists that the change makes false.

   If the endpoint emits a new WebSocket event or changes an existing event's timing, update the event's row in `docs/references/websocket-events-reference.md` **Summary Table** and its section under **Event Details**. Update `docs/architecture/websocket-implementation.md` only when room targeting, payload construction, delivery/failure semantics, or client convergence changes. Bump every edited document's `last-updated`.
