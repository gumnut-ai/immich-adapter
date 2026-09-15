---
title: "Render Deploy with Docker"
status: deprecated
superseded-by: ../references/uvicorn-settings.md
created: 2025-10-23
last-updated: 2026-09-15
---

# Multi-Stage Docker Deployment Decision Record

> **Deprecated (2026-07-27):** This doc argued for moving the adapter's Render deploy from a native Python runtime to a multi-stage Docker build, which shipped. It does not describe the current build. The repository's `Dockerfile` (and `.dockerignore`) is the source of truth for how the image is built and what it runs; [`docs/references/routes-dtos-and-upstream-compatibility.md`](../references/routes-dtos-and-upstream-compatibility.md#bumping-the-immich-version) owns Immich version pinning and the CI sync check; and the Render `$PORT` / SSL-termination contract now lives in [`docs/references/uvicorn-settings.md`](../references/uvicorn-settings.md). This doc is retained for the decision rationale — the multi-stage-build reasoning, the native-vs-Docker comparison, and the migration/rollback strategy. It is no longer updated as the system changes.
>
> Pruned 2026-07-11 to its stable historical context: the problem framing, the multi-stage-build rationale, the migration/rollback strategy, the Immich version-pinning trade-offs, and the performance comparison. (The Render port-handling gotcha it also retained was extracted and removed in the 2026-07-27 pass below.) The full sample Dockerfile / `.dockerignore` / `render.yaml`, the step-by-step build/config/local-test how-tos, the version-bump command sequences, and the troubleshooting catalog were removed because they are now owned by the code and were drifting from the live build.
>
> Pruned again 2026-07-27: the dated Render price list, the "Current State (Native Python Runtime on Render)" description of a configuration that no longer exists, the completed "What Changes in Your Code?" migration how-to, and the Render port-handling section (extracted to `uvicorn-settings.md`). The claim that the Dockerfile tracks the `release` tag was corrected — it pins a version.
>
> Pruned 2026-07-27 to its decision record; implementation detail was removed as it is owned by the code. A second pass the same day finished the price removal the clause above had claimed but not completed — the "Recommended Render Plan" tier list and the per-month build-cost figures in "Disadvantages" — and collapsed the completed "Zero-Downtime Migration Strategy" phases to the one-line strategy. The trade-off analysis, the native-vs-Docker comparison table, and the performance characteristics are unchanged.
>
> A third pass the same day cut the generic multi-stage-Docker tutorial ("The Concept", the "Key Benefits" list, and the prose around `COPY --from`) — it explained Docker rather than any Gumnut decision, and its benefits list was already restated under "Advantages". What earlier clauses call the retained "multi-stage-build rationale" is now the condensed § "Why Multi-Stage", which keeps the project-specific reason (Immich's web files ship only inside the Immich server image) and the illustrative `COPY --from` snippet. The "Additional Resources" list of unversioned vendor links was also removed.

## Overview

The adapter needed Immich's prebuilt web files, which were published inside the
Immich server image. The native Render runtime required a separate extraction
step and committed static files, so the deployment decision was to use a
multi-stage Docker image that extracted those files during the image build.

## Why Multi-Stage

A multi-stage build let a throwaway first stage pull that image purely to
harvest `/build/www`, and the Python stage copy just those files across. The
large Immich image was left behind and did not reach the final layer:

```docker
# Stage 1: This image contains the files we need
FROM ghcr.io/immich-app/immich-server:release AS immich

# Stage 2: This is our actual app
FROM python:3.14-slim

# Copy ONLY the web files from Stage 1
COPY --from=immich /build/www ./static/
```

(Illustrative decision record, not a current build recipe. The shipped
`Dockerfile` pins a version rather than tracking `release`; the current
version-pinning procedure is in
[`routes-dtos-and-upstream-compatibility.md`](../references/routes-dtos-and-upstream-compatibility.md#bumping-the-immich-version).)

This replaced the manual extraction script and committed static files. The
payoff was that the Immich version became a declared, version-controlled build
input instead of a step someone had to remember to run, and the resulting
image was reproducible from the `Dockerfile` alone.

## Migration outcome

The migration was completed by validating the Docker image on a separate
Render service, then switching the production service's runtime. Render's
health check kept the old container available until the new one was healthy,
and the previous deployment remained available for rollback.

## Immich Version Management

### Version-tag trade-offs

The evaluated tag strategies were:

- `release`: Latest stable release
- `vX.Y.Z`: Specific version (e.g., `v1.95.1`)
- `latest`: Bleeding edge

The pinned-version option was selected. The current procedure for changing the
Immich version lives in
[`routes-dtos-and-upstream-compatibility.md`](../references/routes-dtos-and-upstream-compatibility.md#bumping-the-immich-version);
the trade-off analysis that led to the decision follows.

**Pros of `release` tag:**

- Always get latest stable version
- Automatic security updates
- New features automatically

**Cons of `release` tag:**

- Unexpected changes on rebuild
- Potential breaking changes
- Less predictable

**Pros of pinned version:**

- Predictable builds
- No surprise changes
- Test new versions before deploying

**Cons of pinned version:**

- Manual updates required
- Miss security fixes
- More maintenance

## Performance

### Performance Characteristics

The estimates recorded during the decision were:

**Image Size:**

- Final image: ~400-500MB
- Compressed transfer: ~150-200MB

**Cold Start Time:**

- Download image: 5-10s (Render caches)
- Container start: 3-5s
- App initialization: 2-3s
- **Total: 10-18 seconds**

**Memory Usage:**

- Python runtime: ~100-150MB
- FastAPI app: ~50-100MB
- Static file serving: minimal (kernel cache)
- **Total: ~150-250MB typical**

### Comparison: Native vs Docker Deployment

| Metric | Native Python | Docker Multi-Stage |
|--------|--------------|-------------------|
| Build time (first) | 1-2 min | 4-6 min |
| Build time (cached) | 30-60 sec | 1-2 min |
| Deploy time | 30-60 sec | 1-2 min |
| Image/install size | ~200MB | ~450MB |
| Cold start | 5-8 sec | 10-18 sec |
| Static file management | Manual/committed | Automated |
| Reproducibility | Medium | High |
| Version control | Manual | Declarative |

## Pros and Cons

### Advantages

- **Automated Static File Extraction**: No manual script running. Always uses correct Immich version. No committed static files in repo.
- **Reproducible Builds**: Dockerfile declares exact versions. Anyone can rebuild identical image. Easy to test locally before deploying.
- **Version Control**: Immich version tracked in Git. Easy to see what version is deployed. Simple rollbacks.
- **Clean Repository**: Remove 29MB of static files from repo. Smaller clone size. Faster CI/CD.
- **Declarative Configuration**: Everything in Dockerfile. No hidden build steps. Clear dependencies.
- **Production-Ready**: Non-root user for security. Health checks built-in. Optimized uvicorn settings.

### Disadvantages

- **Longer Build Times**: First build: 4-6 minutes vs 1-2 minutes. Cached builds: 1-2 minutes vs 30 seconds. Large image downloads (800MB Immich image).
- **Larger Final Image**: Docker image: ~450MB vs ~200MB native. More disk space needed. Slower deployment transfers.
- **More Complex**: Dockerfile to maintain. Docker knowledge required. More moving parts.
- **Build Cost**: Uses more Render build minutes than the native runtime. May need a paid plan for longer builds.
- **Cold Starts**: 10-18 seconds vs 5-8 seconds. Matters if you use Render free tier with spindown.

## Decision outcome

Multi-stage Docker deployment was selected because it automated static-file
extraction, made builds reproducible, kept the Immich version in source
control, and removed committed generated files. The rejected alternative was
to keep committed static files: that would have reduced build time, but would
have retained a manual extraction and synchronization burden.
