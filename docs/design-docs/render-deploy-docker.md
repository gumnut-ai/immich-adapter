---
title: "Render Deploy with Docker"
status: completed
created: 2025-10-23
last-updated: 2026-07-11
---

# Multi-Stage Docker Deployment Guide for Render

> **Note —** Pruned 2026-07-11 to its stable historical context: the problem framing, the multi-stage-build rationale, the Render port-handling gotcha, the migration/rollback strategy, the Immich version-pinning trade-offs, and the cost/performance comparison. The full sample Dockerfile / `.dockerignore` / `render.yaml`, the step-by-step build/config/local-test how-tos, the version-bump command sequences, and the troubleshooting catalog were removed because they are now owned by the code and were drifting from the live build. For the current build, see the repository's `Dockerfile` and `.dockerignore`.

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
FROM python:3.12-slim

# Copy ONLY the web files from Stage 1
COPY --from=immich /build/www ./static/
```

The `--from=immich` flag tells Docker: "copy from the `immich` stage, not from the build context"

## Important: Render Port Handling

**Key Concept**: Render handles SSL/TLS termination and routing for you.

- **Externally**: Your app is accessed via standard ports (80 for HTTP, 443 for HTTPS)
- **Internally**: Render sets the `PORT` environment variable (typically `10000`)
- **Your app**: Should bind to `0.0.0.0:$PORT` (NOT a specific port like 3001)

**Flow:**

```
Internet → https://your-app.onrender.com:443
         → Render Load Balancer (SSL termination)
         → Your container at $PORT (e.g., 10000)
```

**What this means:**

- Use `--port ${PORT:-8080}` in your CMD (environment variable with fallback)
- Render sets `PORT` automatically (you don't need to configure it)
- For local development, set `PORT=8080` or any port you prefer
- Never hardcode port 3001 or any specific port in production Dockerfile

## Complete Implementation

The multi-stage `Dockerfile` — three stages: extract Immich web files from `ghcr.io/immich-app/immich-server`, build the Python dependencies with `uv`, then assemble a slim non-root runtime that serves on `${PORT:-8080}` with a `/api/server/ping` health check — and its `.dockerignore` live at the repository root. See the repository's `Dockerfile` and `.dockerignore` for the current build.

## Migration from Native Runtime

### Current State (Native Python Runtime on Render)

Your current setup likely uses:

- **Runtime**: Python 3.12
- **Build Command**: `uv sync`
- **Start Command**: `uv run uvicorn main:app --host 0.0.0.0 --port $PORT` or similar
- **Static files**: Committed to repository or extracted separately

**Note on Ports**: Render automatically sets the `PORT` environment variable (typically 10000 for web services). Your application should bind to `0.0.0.0:$PORT` and Render handles SSL termination and routing from standard ports (80/443) to your app.

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

### What Changes in Your Code?

**Good news: MINIMAL code changes required!**

The Dockerfile handles:

- Installing dependencies (via uv)
- Copying static files (from Immich image)
- Setting up the runtime environment

Your application code (`main.py`, routers, etc.) requires **NO changes**:

- `SPAStaticFiles(directory="static", html=True)` still works
- Static files are at `./static/` in the container
- All imports and paths remain the same

**Only change needed:**

- Remove committed `static/` files from repository (optional, saves space)
- Update `.gitignore` to exclude `static/` directory

## Immich Version Management

### Version Tags

Immich provides these Docker tags:

- `release`: Latest stable release (recommended)
- `vX.Y.Z`: Specific version (e.g., `v1.95.1`)
- `latest`: Bleeding edge (not recommended)

### Pinning to Specific Version

**Current Dockerfile uses `release` tag:**

```docker
FROM ghcr.io/immich-app/immich-server:release AS immich-source
```

**To pin to specific version:**

```docker
FROM ghcr.io/immich-app/immich-server:v1.95.1 AS immich-source
```

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

## Cost and Performance

### Render Pricing (as of 2025)

**Build Minutes:**

- Free tier: 750 minutes/month
- Paid: $0.008/minute

**Estimated Monthly Build Cost:**

- First build: 5 minutes = $0.04
- 10 subsequent builds: 10 x 1.5 min = 15 min = $0.12
- **Total: ~$0.15-0.20/month for builds**

**Runtime:**

- Starter plan: $7/month (512MB RAM, shared CPU)
- Standard plan: $25/month (2GB RAM, shared CPU)
- Pro plan: $85/month (4GB RAM, dedicated CPU)

**Bandwidth:** Included (100GB/month on free, unlimited on paid)

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
