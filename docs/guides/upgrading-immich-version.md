---
title: "Upgrading the Immich Target Version"
last-updated: 2026-09-02
---

# Upgrading the Immich Target Version

The adapter targets exactly one Immich version, pinned in `.immich-container-tag` and mirrored in the `Dockerfile`. Each Immich release (or release candidate) is a compatibility decision, not a dependency bump: the OpenAPI spec delta has to be read for removed, deprecated, promoted, and new endpoints, and each finding has to be resolved against what the Gumnut API can support. This guide walks that process end to end, in the order that avoids rework.

It is written to be driven by an agent. A prompt that hands the whole procedure to one is at the end of the doc (see [Running this guide with an agent](#running-this-guide-with-an-agent)).

## Prerequisites

- A local clone of [`immich-app/immich`](https://github.com/immich-app/immich). The commands below use `$IMMICH` for its path; the Gumnut dev root keeps one at `../immich`.
- `uv` (for the adapter tools) and Docker (only for extracting the web bundle in the final step).
- A scratch directory for spec files. The examples use `$S`.

```bash
export IMMICH=../immich
export S="$(mktemp -d)"   # or any scratch directory
```

Keep these variables in one persistent shell for the whole procedure. An agent whose command tool starts a fresh shell for each call must repeat this setup, along with the `OLD` and `NEW` declarations below, in every call that uses them; pausing between steps does not preserve shell state.

## Step 0 — Identify the current and target versions

```bash
cat .immich-container-tag                          # current pin, e.g. v3.1.0
git -C "$IMMICH" fetch --tags origin
git -C "$IMMICH" tag --sort=-creatordate | head    # candidates, e.g. v3.2.0, v3.2.0-rc.1
```

Always work from **tags**, never from the fork's working tree or `main`. The working tree answers for whatever version that clone was last left on, and `main`'s spec reports the *last released* version in `info.version` (main still said `3.1.0` well after the v3.1.0 tag), so it cannot tell you what the next release will contain. Release candidates are tagged `vX.Y.Z-rc.N` and are the right target when you want to prepare before GA.

Read the upstream release notes for the target (`https://github.com/immich-app/immich/releases/tag/<tag>`) before diffing. They name the client-facing behavior changes that a spec diff cannot show, and they are the fastest way to spot a change to the mobile sync protocol or the web timeline.

## Step 1 — Extract both specs from git

```bash
OLD=$(cat .immich-container-tag)
NEW=v3.2.0            # the tag you are evaluating

git -C "$IMMICH" show "$OLD:open-api/immich-openapi-specs.json" > "$S/old.json"
git -C "$IMMICH" show "$NEW:open-api/immich-openapi-specs.json" > "$S/new.json"
```

Both files are large (about 600 KB) and a raw `diff` is unreadable: the schema section churns on annotation and generator changes that do not alter the wire. Read them structurally instead.

## Step 2 — Diff the specs structurally

Immich annotates every operation with `x-immich-state` (`Alpha`, `Beta`, `Stable`, `Deprecated`, `Internal`, or absent for a few unannotated routes) and `x-immich-history` (a list of `{version, state}` entries, where a `Deprecated` entry carries a `replacementId` naming the operation that supersedes it). Deprecated operations also set the standard OpenAPI `deprecated: true`. The history is keyed by **major** version, so a promotion from Beta to Stable inside a major only shows up by comparing `x-immich-state` between the two specs, which is what this script does.

```bash
uv run python - "$S/old.json" "$S/new.json" <<'EOF'
import json, sys
old, new = [json.load(open(p)) for p in sys.argv[1:3]]
METHODS = ("get", "post", "put", "patch", "delete")

def ops(spec):
    return {(m.upper(), p): o for p, ps in spec["paths"].items()
            for m, o in ps.items() if m in METHODS}

o, n = ops(old), ops(new)
print(f"versions: {old['info']['version']} -> {new['info']['version']}\n")

def show(title, keys, src):
    print(f"== {title} ({len(keys)})")
    for k in sorted(keys):
        op = src[k]
        print(f"  {k[0]:6} {k[1]:55} {op.get('operationId','?'):35} {op.get('x-immich-state','(no state)')}")
    print()

show("removed operations", set(o) - set(n), o)
show("added operations", set(n) - set(o), n)

print("== state changes")
for k in sorted(set(o) & set(n)):
    a, b = o[k].get("x-immich-state"), n[k].get("x-immich-state")
    if a != b:
        repl = [h.get("replacementId") for h in n[k].get("x-immich-history", []) if h.get("replacementId")]
        print(f"  {k[0]:6} {k[1]:55} {a or '(no state)'} -> {b or '(no state)'}" + (f"  replaced by {repl[-1]}" if repl else ""))
print()

print("== operations whose definition changed (params, body, responses, docs)")
for k in sorted(set(o) & set(n)):
    if o[k] != n[k]:
        print(f"  {k[0]:6} {k[1]}")
print()

so, sn = old["components"]["schemas"], new["components"]["schemas"]
print("== schemas added:  ", sorted(set(sn) - set(so)))
print("== schemas removed:", sorted(set(so) - set(sn)))
print("== schemas changed:", sorted(k for k in so if k in sn and so[k] != sn[k]))
EOF
```

For any operation or schema the script lists as changed, look at the actual delta before deciding it matters:

```bash
uv run python -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["paths"][sys.argv[2]][sys.argv[3]], indent=1))' "$S/old.json" /albums/{id}/user/{userId} put > "$S/op-old.json"
uv run python -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["paths"][sys.argv[2]][sys.argv[3]], indent=1))' "$S/new.json" /albums/{id}/user/{userId} put > "$S/op-new.json"
diff "$S/op-old.json" "$S/op-new.json"
```

An operation appears in both sections when its state and its definition changed. Keep that overlap: otherwise a promotion or deprecation could hide a simultaneous parameter, body, or response change. Description-only deltas still deserve a read. `x-immich-history` entries can also sit on individual parameters, where the script above does not look. The v3.0.3 to v3.1.0 delta was entirely parameter descriptions, yet one of them carried a history entry deprecating the `"me"` value for `userId` on the album-user routes, slated for removal in Immich v4.

## Step 3 — Classify every finding

Work through the script's sections in this order. The first two are hard requirements; the rest are decisions to record.

### Removed operations

The adapter follows a clean-cut policy: when Immich removes a route, the adapter drops its handler and tests rather than keeping a shim, because the deployment pins the client version and there is no mixed-version window to serve. For each removed operation:

```bash
grep -rn '"/<path>"' routers/api/           # find the handler; paths are declared relative to the router prefix
grep -rln '<handler_name>' tests/
```

Delete the handler, its tests, and any WebSocket emission that referenced it. If the removal changes a feature-area classification, update [Feature Compatibility](../references/feature-compatibility.md). Then follow the doc-update sweep in [Routes, DTOs, and Upstream Compatibility § Implementing New Endpoints](../references/routes-dtos-and-upstream-compatibility.md#implementing-new-endpoints), step 9, which also covers removals.

### Deprecated operations

A `Deprecated` state means the route still exists at the target version, so the pinned clients may still call it. Keep serving it. Record two things:

1. **The replacement.** The `replacementId` in the history names the operation that supersedes it. Check whether the adapter implements the replacement, and whether the pinned clients have already moved to it (see [Step 5](#step-5--read-the-clients-not-just-the-spec)). If the clients now call the replacement and the adapter only implements the old route, that is a current compatibility difference.
2. **The removal horizon.** Deprecations announce what the next major will delete. Note them in the PR so the next major-version upgrade starts with a list.

### Promoted operations (Alpha or Beta to Stable)

A promotion changes how much weight to put on an endpoint. Something the adapter stubbed because it was Alpha is now a contract Immich intends to keep, and client code is more likely to depend on it. For each promotion:

```bash
grep -rn '"/<path>"' routers/api/       # is it implemented at all?
grep -n -i 'stub' routers/api/<router>.py
```

Stubs identify themselves in their docstrings ("This is a stub implementation ...") or by returning `501`. If a promoted endpoint is currently a stub, decide whether to implement it properly: does a pinned client reach it, is a benign empty read honest, and does the Gumnut API have the model? Record any changed feature-area classification in [Feature Compatibility](../references/feature-compatibility.md) after completing the dependency check in [Step 6](#step-6--check-what-the-gumnut-api-and-sdk-can-support). Promotion alone does not force an implementation; an intentional unsupported area can stay intentional, but its classification must still fit the promoted contract.

### Added operations

New routes fall into three buckets, and the spec cannot tell you which:

- **Client-reached.** A pinned web or mobile client calls it. It needs at least a compatibility response, and usually an implementation.
- **Reachable but gated.** The client calls it only when a server feature flag or user preference enables the feature. The adapter can keep it unreachable by leaving the gate off (see the `/server/features` and `/me/preferences` audits in [Routes § Implementing New Endpoints](../references/routes-dtos-and-upstream-compatibility.md#implementing-new-endpoints)).
- **Not a client route.** Admin, integrity, plugin, and other operator surfaces that normal clients never hit. These become intentionally unsupported areas and return a 404 from the adapter.

Decide the bucket in [Step 5](#step-5--read-the-clients-not-just-the-spec), then the dependency in [Step 6](#step-6--check-what-the-gumnut-api-and-sdk-can-support). The unannotated routes (no `x-immich-state`) that appeared in v3 were plugins, workflows, backchannel logout, and album map markers; treat "no state" as Alpha until upstream says otherwise.

### Changed schemas and operation definitions

Schema changes are what break the adapter at runtime. The ones to look for:

- **New required fields** on response DTOs. Hand-built stub responses fail validation with a 500 on every call, and stubs have almost no test coverage.
- **Retypes**, especially `str` to `UUID` or `int` to `float`, and **tightened constraints** (`pattern`, `min_length`, `ge`/`le`).
- **Removed or renamed fields** that a handler populates.
- **New sync types.** Look for `Sync*V<n>` schemas and new `SyncRequestType`/`SyncEntityType` enum values. Those route to the sync stream and need the process in [Sync Stream Architecture](../architecture/sync-stream-architecture.md).

Step 4 turns this list into concrete failures.

## Step 4 — Regenerate models against the candidate spec

Regenerating does not require bumping the pin: pass the extracted spec as a local file.

```bash
uv run tools/generate_immich_models.py --immich-spec "$S/new.json"
uv run ruff format routers/immich_models.py
git diff --stat routers/immich_models.py
uv run pyright
uv run pytest
```

Read the model diff against the schema list from Step 2, not against an assumption that every hunk is spec-driven. The generator's `datamodel-code-generator` dependency is unpinned, so a regeneration can carry pure codegen churn (import aliases, formatting) that is not a wire change. [Development Tools § Pydantic Model Generator](../references/development-tools.md#pydantic-model-generator) explains the constraint preprocessing and the pyright sweep that finds broken stubs: every `Argument missing for parameter` and `Literal[...] cannot be assigned to ... UUID` error is a stub that would 500. Pyright does not catch a widening retype or a tightened constraint; the per-stub construction smoke tests and the full test run are the backstop for those.

Fix what breaks. If a fix is more than mechanical (a field the adapter cannot populate, a type the Gumnut API does not have), it belongs in Step 6.

Note that a regeneration from a local file records that path in the generated header instead of the tag. Regenerate from the default GitHub URL in the final bump step so the committed header names the tag.

## Step 5 — Read the clients, not just the spec

The spec pins shapes. What the pinned clients actually send, which routes they call, and what they render from a response are all in the client source, and that is where compatibility bugs live. For every added, promoted, or deprecated operation, and for every implemented operation whose definition changed, check the pinned clients at the **target** tag:

```bash
# Web client: functions are named after the operationId from the spec
git -C "$IMMICH" grep -n '<operationId>' "$NEW" -- web/src

# Mobile client: generated API methods share the operationId name; sync types live in the sync stream service
git -C "$IMMICH" grep -n '<operationId>' "$NEW" -- mobile/lib mobile/openapi/lib/api

# What changed in the clients between versions, scoped to areas the adapter implements
git -C "$IMMICH" diff --stat "$OLD" "$NEW" -- web/src/lib/services web/src/lib/utils mobile/lib/domain mobile/lib/infrastructure
git -C "$IMMICH" diff "$OLD" "$NEW" -- server/src/controllers server/src/dtos server/src/enum.ts
```

Use `git show <tag>:<path>` and `git grep <pattern> <tag> -- <path>` rather than checking out the tag, so the working tree cannot answer for the wrong version. Two things the spec never shows:

- **Routes excluded from OpenAPI.** Controllers marked `@ApiExcludeEndpoint` are absent from the spec and from this diff. `git -C "$IMMICH" grep -n ApiExcludeEndpoint "$NEW" -- server/src/controllers` lists them.
- **Response guarantees.** Array ordering, which rows a collection includes, and free-form string values that clients hard-match live in the server's mappers and repositories. [Routes § Verifying upstream behavior](../references/routes-dtos-and-upstream-compatibility.md#verifying-upstream-behavior--read-the-immich-source-not-just-the-spec) has the specifics.

Treat any claim about client behavior, including one in this guide or in a task description, as a hypothesis until it is read in the tagged source.

## Step 6 — Check what the Gumnut API and SDK can support

For each endpoint you are considering implementing, the question is whether the data exists behind the adapter. Check in this order; each layer can lag the one below it.

1. **The installed SDK.** The adapter uses the `gumnut-sdk` package pinned in `pyproject.toml`.

   ```bash
   uv run python -c "import gumnut; c = gumnut.AsyncGumnut(api_key='x'); print([a for a in dir(c) if not a.startswith('_')])"
   uv run python -c "import gumnut; print([m for m in dir(gumnut.AsyncGumnut(api_key='x').assets) if not m.startswith('_')])"
   ```

2. **The latest SDK on PyPI.** The generated SDK trails the deployed API, so a missing method may already exist in a newer release. Bump the SDK before designing a raw-client workaround, following [Routes § Bumping the Gumnut SDK](../references/routes-dtos-and-upstream-compatibility.md#bumping-the-gumnut-sdk).
3. **The live Gumnut API.** `https://api.gumnut.ai/openapi.json` is the contract the SDK is generated from. If the endpoint or field is there but not in any SDK release, a raw `client.get`/`client.post` call is the temporary bridge; note it so the next SDK bump can replace it.
4. **Nothing.** The Gumnut API has no model for the feature. The adapter cannot fake durable state: a benign empty read can be compatible, but new or revised mutation behavior must not claim success when nothing was stored. Classify the feature as product-dependent, deferred, or intentionally unsupported in [Feature Compatibility](../references/feature-compatibility.md), describing the missing Gumnut API capability rather than citing a ticket.

## Step 7 — Run the compatibility validator against both specs

The validator reports which Immich routes and parameters the adapter does not expose. Run it against the old and new specs and diff the reports so only the delta needs attention.

```bash
ENVIRONMENT=test SESSION_ENCRYPTION_KEY=dummy-key \
  uv run tools/dump_openapi_json.py | sed -n '/^{/,$p' > "$S/adapter.json"

uv run tools/validate_api_compatibility.py --immich-spec="$S/old.json" --adapter-spec="$S/adapter.json" > "$S/compat-old.txt"
uv run tools/validate_api_compatibility.py --immich-spec="$S/new.json" --adapter-spec="$S/adapter.json" > "$S/compat-new.txt"
diff "$S/compat-old.txt" "$S/compat-new.txt"
```

The exit code is the number of error-level incompatibilities, so both runs will exit non-zero: a `missing_endpoint` error is what an intentional 404 looks like to the validator, and the baseline already carries dozens. The validator sorts its findings so the report diff contains actual additions and removals rather than reordered baseline rows. Reconcile every changed entry with Step 2: new `missing_endpoint` errors normally correspond to added operations left unimplemented, while method, parameter, request-body, response, and schema findings can come from changed operations. See [Development Tools § API Compatibility Tool](../references/development-tools.md#api-compatibility-tool) for flags.

## Step 8 — Decide what to do with a release candidate

Do all of the above against an RC. Do **not** pin production to one: the pin is what `/server/version` reports to clients and what the Dockerfile pulls, and an RC image is not guaranteed to stay published. Keep the work on a branch, and when GA lands re-run Step 2 with the RC as `$OLD` and the GA tag as `$NEW` to confirm the delta is empty or small, then finish with Step 9.

## Step 9 — Bump the pin when the release is live

The pin lives in two files that CI checks against each other (the `check-immich-version-sync` job in `.github/workflows/ci.yml`). Render builds the image straight from the repo, so the Dockerfile default is what ships. The full rationale is in [Routes § Bumping the Immich Version](../references/routes-dtos-and-upstream-compatibility.md#bumping-the-immich-version).

1. **Update the pin.** Write the tag to `.immich-container-tag`, and in `Dockerfile` update `ARG IMMICH_VERSION` and the `Last updated` comment beside it.
2. **Regenerate the models from the tagged spec** so the header records the tag, and run the checks:

   ```bash
   uv run tools/generate_immich_models.py \
     --immich-spec https://github.com/immich-app/immich/blob/main/open-api/immich-openapi-specs.json
   uv run ruff format && uv run ruff check --fix && uv run pyright && uv run pytest
   ```

   The generator substitutes the tag from `.immich-container-tag` into the URL, so this fetches the tagged spec even though the URL says `main`.

3. **Extract the new web bundle locally.** Production picks the bundle up from the Dockerfile stage; your local checkout does not.

   ```bash
   ./scripts/extract-immich-web.py -f ./static
   ```

   The script reads the tag from `.immich-container-tag`, pulls `ghcr.io/immich-app/immich-server:<tag>`, and writes `static/.extracted-tag`. The adapter logs a loud warning on startup when that marker no longer matches the pin, so a forgotten extraction announces itself. `static/` is gitignored; nothing to commit. Then run the stack per [Running with Immich Web](running-with-immich-web.md) and click through login, timeline, an album, a person, search, and an upload with the new client. This is the only step that exercises the real client against the adapter, and it is where behavior changes the spec could not show surface.

4. **Update feature compatibility.** Reconcile every feature-area decision from Step 3 with `docs/references/feature-compatibility.md`. Change a classification only when the release or accompanying implementation changes the product boundary or current support; keep route, DTO, and version details in code and the PR body. Bump `last-updated` when the reference changes.
5. **Sweep other version mentions.** Docs cite versions in two ways, and only one should change:

   ```bash
   grep -rn "$OLD" --include='*.md' --include='Dockerfile' --include='*.py' --include='*.yml' . | grep -v '^./.venv'
   ```

   Update mentions that describe the **current** state (the README's build example and any "as of vX.Y.Z the web client passes ..." claim, after re-verifying the claim against the new tag). Leave mentions that pin a **historical event** to the version where it happened (a past retype, a near-miss, an inventory baseline); those are correct as written.

6. **Documentation lint and the map.** `uv run scripts/lint_docs.py --fix` bumps any `last-updated` you missed. If you added a guide or reference, add its row to the Documentation Map in `AGENTS.md`.
7. **Write the PR body as the record.** Summarize the spec delta by bucket (removed, deprecated with replacement, promoted, added, schema changes), state the decision for each added or promoted endpoint, and list anything deferred by the feature-compatibility classification or by a missing Gumnut API capability. The repo is public, so describe backend dependencies in terms of the Gumnut API contract, not tickets or private paths. This body is what the next upgrade reads first.

## Running this guide with an agent

Paste this into an agent session from the repo root, filling in the tag:

> Read `docs/guides/upgrading-immich-version.md` and walk me through upgrading the adapter from the version in `.immich-container-tag` to Immich `<tag>`. The upstream clone is at `../immich`. Go one step at a time: run the step's commands, show me the results and your classification, and wait for my go-ahead before moving on. Keep the guide's shell variables in a persistent session, or re-declare them in every command call that starts a fresh shell. In Step 3, present each removed, deprecated, promoted, and added operation with your recommended disposition and the evidence from the client source. In Step 6, tell me for each candidate endpoint whether the installed SDK, a newer SDK release, the live Gumnut API, or nothing supports it. Do not bump `.immich-container-tag` or the Dockerfile until I say the release is live.

For a release that is already GA and where you want the whole thing done, replace the last two sentences with: "Complete every step including the Step 9 bump, and open a PR whose body follows Step 9 item 7."
