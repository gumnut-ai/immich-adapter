---
title: "Render Deploy with Docker"
status: deprecated
superseded-by: ../references/uvicorn-settings.md
created: 2025-10-23
last-updated: 2026-07-27
---

# Multi-Stage Docker Deployment Guide for Render

> **Deprecated (2026-07-27):** This doc argued for moving the adapter's Render deploy from a native Python runtime to a multi-stage Docker build, which shipped. It does not describe the current build. The repository's `Dockerfile` (and `.dockerignore`) is the source of truth for how the image is built and what it runs; [`docs/references/code-practices.md`](../references/code-practices.md) § "Bumping the Immich Version" owns Immich version pinning and the CI sync check; and the Render `$PORT` / SSL-termination contract now lives in [`docs/references/uvicorn-settings.md`](../references/uvicorn-settings.md). This doc is retained for the decision rationale — the multi-stage-build reasoning, the native-vs-Docker comparison, and the migration/rollback strategy. It is no longer updated as the system changes.
>
> Pruned 2026-07-11 to its stable historical context: the problem framing, the multi-stage-build rationale, the migration/rollback strategy, the Immich version-pinning trade-offs, and the performance comparison. (The Render port-handling gotcha it also retained was extracted and removed in the 2026-07-27 pass below.) The full sample Dockerfile / `.dockerignore` / `render.yaml`, the step-by-step build/config/local-test how-tos, the version-bump command sequences, and the troubleshooting catalog were removed because they are now owned by the code and were drifting from the live build.
>
> Pruned again 2026-07-27: the dated Render price list, the "Current State (Native Python Runtime on Render)" description of a configuration that no longer exists, the completed "What Changes in Your Code?" migration how-to, and the Render port-handling section (extracted to `uvicorn-settings.md`). The claim that the Dockerfile tracks the `release` tag was corrected — it pins a version.

## Overview

This guide explains how to deploy immich-adapter to Render using a multi-stage Dockerfile that automatically extracts Immich web files during the Docker build process.

**Current Static Files Size**: 29MB (from `static/` directory)

## How Multi-Stage Docker Builds Work

### The Concept

Multi-stage builds allow you to use multiple `FROM` statements in a single Dockerfile. Each `FROM` instruction starts a new build stage. You can selectively copy artifacts from one stage to another, leaving behind everything you don't need.

**For this project:**

- **Stage 1**: Pull the Immich server image (contains web files at `/build/www`)
- **Stage 2**: Build your Python application
- **Between stages**: Copy only the web files from Stage 1 to Stage 2

### Key Benefits

1. **Automated Extraction**: No manual script running - happens during Docker build
2. **Single Source of Truth**: Dockerfile declares exactly which Immich version to use
3. **Reproducible**: Anyone can rebuild the exact same image
4. **Clean Final Image**: Stage 1 artifacts don't bloat the final image (only copied files remain)
5. **Version Control**: Immich version is tracked in Git via Dockerfile

### The Magic: `COPY --from`

```docker
# Stage 1: This image contains the files we need
FROM ghcr.io/immich-app/immich-server:release AS immich

# Stage 2: This is our actual app
FROM python:3.14-slim

# Copy ONLY the web files from Stage 1
COPY --from=immich /build/www ./static/
```

The `--from=immich` flag tells Docker: "copy from the `immich` stage, not from the build context"

## Complete Implementation

The multi-stage `Dockerfile` — three stages: extract Immich web files from `ghcr.io/immich-app/immich-server`, build the Python dependencies with `uv`, then assemble a slim non-root runtime that serves on `${PORT:-8080}` with a `/api/server/ping` health check — and its `.dockerignore` live at the repository root. See the repository's `Dockerfile` and `.dockerignore` for the current build.

## Migration from Native Runtime

### Migration Steps

Migrated to Docker deploy: add the `Dockerfile` and `.dockerignore`, test the image locally, switch the Render service runtime from Python to Docker (via `render.yaml` or the dashboard), then push to trigger the build and verify the health check and static-file serving.

### Zero-Downtime Migration Strategy

1. **Create staging service first**
   - Deploy Docker version to a new Render service (staging)
   - Test thoroughly
   - Compare with production (native runtime)

2. **When ready, switch production**
   - Update production service to Docker runtime
   - Render will build new image
   - Health checks ensure smooth cutover
   - Old container stays running until new one is healthy

3. **Rollback if needed**
   - Render keeps previous deployment
   - Can rollback via dashboard in seconds

## Immich Version Management

### Version Tags

Immich provides these Docker tags:

- `release`: Latest stable release (recommended)
- `vX.Y.Z`: Specific version (e.g., `v1.95.1`)
- `latest`: Bleeding edge (not recommended)

### Pinning to Specific Version

The pinned-version option is the one that shipped: the `Dockerfile` declares `ARG IMMICH_VERSION`, kept in sync with `.immich-container-tag` and enforced by a CI job. See [`docs/references/code-practices.md`](../references/code-practices.md) § "Bumping the Immich Version" for the current procedure. The trade-off analysis that led there:

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

### Recommended Approach

**Development/Staging:**

```docker
FROM ghcr.io/immich-app/immich-server:release AS immich-source
```

**Production:** pin to a specific `vX.Y.Z` tag so rebuilds are predictable, and bump it deliberately after testing each new Immich version.

## Performance

### Performance Characteristics

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

**Recommended Render Plan:**

- Development/Testing: Free tier (512MB, may be tight)
- Production (low traffic): Starter ($7/month)
- Production (medium traffic): Standard ($25/month)

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
- **Build Cost**: Uses more Render build minutes. ~$0.15-0.20/month vs ~$0.05/month. May need paid plan for longer builds.
- **Cold Starts**: 10-18 seconds vs 5-8 seconds. Matters if you use Render free tier with spindown.

## Conclusion

Multi-stage Docker deployment is the **best long-term solution** for automatically extracting Immich web files on Render:

**Use this approach if:**

- You want automated extraction
- You're building for production
- You value reproducibility
- Build time isn't critical (1-6 minutes)
- You're comfortable with Docker

**Use committed files instead if:**

- You need to deploy TODAY
- Build time is critical (< 1 minute)
- You're on Render free tier (build minute limits)
- Docker complexity isn't worth it for your use case

## Additional Resources

- [Docker Multi-Stage Builds](https://docs.docker.com/build/building/multi-stage/)
- [Render Docker Deployment](https://render.com/docs/docker)
- [Immich Docker Images](https://github.com/immich-app/immich/pkgs/container/immich-server)
- [FastAPI in Docker](https://fastapi.tiangolo.com/deployment/docker/)
- [uv Docker Best Practices](https://docs.astral.sh/uv/guides/docker/)
