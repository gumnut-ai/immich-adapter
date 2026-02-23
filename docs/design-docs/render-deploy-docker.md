---
title: "Render Deploy with Docker"
status: completed
created: 2025-10-23
last-updated: 2025-10-23
---

# Multi-Stage Docker Deployment Guide for Render

## Overview

This guide explains how to deploy immich-adapter to Render using a multi-stage Dockerfile that automatically extracts Immich web files during the Docker build process.

**Current Static Files Size**: 29MB (from `static/` directory)

## Table of Contents

1. [How Multi-Stage Docker Builds Work](#how-multi-stage-docker-builds-work)
2. [Complete Implementation](#complete-implementation)
3. [How the Build Process Works](#how-the-build-process-works)
4. [Render Configuration](#render-configuration)
5. [Testing Locally](#testing-locally)
6. [Migration from Native Runtime](#migration-from-native-runtime)
7. [Immich Version Management](#immich-version-management)
8. [Troubleshooting](#troubleshooting)
9. [Cost and Performance](#cost-and-performance)
10. [Pros and Cons](#pros-and-cons)

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

### 1. Create the Dockerfile

Create `Dockerfile` in your project root:

```docker
# syntax=docker/dockerfile:1

# ============================================================================
# Stage 1: Extract Immich web files
# ============================================================================
FROM ghcr.io/immich-app/immich-server:release AS immich-source

# We only need this stage to copy files from - no commands needed
# The /build/www directory in this image contains the web interface files


# ============================================================================
# Stage 2: Build the immich-adapter application
# ============================================================================
FROM python:3.12-slim AS builder

# Install system dependencies needed for building Python packages
RUN apt-get update && apt-get install -y \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package installer)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.cargo/bin:$PATH"

# Set working directory
WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock* ./

# Install dependencies (no dev dependencies in production)
RUN uv sync --frozen --no-dev


# ============================================================================
# Stage 3: Runtime image (final, smallest image)
# ============================================================================
FROM python:3.12-slim

# Install runtime dependencies only (curl for health checks)
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN useradd -m -u 1000 appuser

# Set working directory
WORKDIR /app

# Install uv in runtime (needed for `uv run`)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.cargo/bin:$PATH"

# Copy installed dependencies from builder stage
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/pyproject.toml /app/uv.lock* ./

# Copy application code
COPY --chown=appuser:appuser . .

# Copy Immich web files from first stage
COPY --from=immich-source --chown=appuser:appuser /build/www ./static/

# Switch to non-root user
USER appuser

# Expose port (Render will set PORT environment variable)
EXPOSE 8080

# Health check (use PORT from environment, default to 8080)
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/api/server/ping || exit 1

# Set Python to use the virtual environment
ENV PATH="/app/.venv/bin:$PATH"

# Run the application with optimized uvicorn settings
# Render sets PORT environment variable automatically
CMD uvicorn main:app \
     --host 0.0.0.0 \
     --port ${PORT:-8080} \
     --timeout-keep-alive 75 \
     --limit-concurrency 1000 \
     --backlog 2048
```

### 2. Create .dockerignore

Create `.dockerignore` to exclude unnecessary files from build context:

```
# .dockerignore
.git
.github
.venv
__pycache__
*.pyc
*.pyo
*.pyd
.Python
*.so
*.egg
*.egg-info
dist
build
.pytest_cache
.ruff_cache
.mypy_cache
.coverage
htmlcov
.env
.env.*
!.env.example
*.log
.DS_Store
.vscode
.idea

# Don't include static files - they'll be copied from Immich image
static/

# Don't include test files
tests/
test_*.py

# Don't include documentation
*.md
!README.md
docs/

# Don't include scripts
scripts/
tools/
```

### 3. Create render.yaml

Create `render.yaml` for Render configuration:

```yaml
services:
  - type: web
    name: immich-adapter
    runtime: docker
    repo: https://github.com/your-org/immich-adapter  # Update this
    region: oregon  # Choose closest to your users
    plan: starter  # Or free tier for testing

    # Docker-specific settings
    dockerfilePath: ./Dockerfile
    dockerContext: .

    # Health check path
    healthCheckPath: /api/server/ping

    # Auto-deploy settings
    autoDeploy: true
    branch: main  # Or your production branch

    # Environment variables
    envVars:
      - key: ENVIRONMENT
        value: production

      - key: GUMNUT_API_BASE_URL
        value: https://api.gumnut.example.com  # Update this

      - key: SENTRY_DSN
        sync: false  # Set in Render dashboard

      # Add other environment variables as needed
```

## How the Build Process Works

### Step-by-Step Build Flow

1. **Render receives push to main branch**
   - Webhook triggers build

2. **Build starts - Stage 1 (immich-source)**

   ```bash
   # Render pulls the Immich image
   docker pull ghcr.io/immich-app/immich-server:release
   ```

   - Image size: ~800MB-1.2GB (contains full Immich server)
   - Contains: Node.js, web files, server binaries, etc.
   - We only care about: `/build/www` directory (29MB of static files)

3. **Build continues - Stage 2 (builder)**

   ```bash
   # Install build dependencies and Python packages
   apt-get install curl build-essential
   uv sync --frozen --no-dev
   ```

   - Installs compilation tools needed for some Python packages
   - Creates virtual environment with all dependencies

4. **Build finishes - Stage 3 (runtime)**

   ```bash
   # Create clean runtime image
   - Copy .venv from builder
   - Copy application code from build context
   - Copy /build/www from immich-source stage
   ```

   - Final image size: ~400-500MB (Python base + deps + app + static files)

5. **Render deploys the image**
   - Container starts
   - Health check begins pinging `/api/server/ping`
   - Once healthy, traffic routes to new container
   - Old container shuts down (zero-downtime deployment)

### Build Time Estimates

**First build** (no cache):

- Stage 1 pull: 2-3 minutes (800MB+ image)
- Stage 2 build: 1-2 minutes (install dependencies)
- Stage 3 assembly: 30-60 seconds
- **Total: 4-6 minutes**

**Subsequent builds** (with cache):

- Stage 1 cached: 10-30 seconds (layer already exists)
- Stage 2 cached: 10-30 seconds (if dependencies unchanged)
- Stage 3 assembly: 30-60 seconds
- **Total: 1-2 minutes**

**What triggers full rebuild:**

- Immich image tag changes (e.g., `release` tag points to new version)
- Dependencies change (pyproject.toml/uv.lock modified)
- Base image updates (python:3.12-slim)

**What uses cache:**

- Application code changes only (most common)
- Environment variable changes (no rebuild needed)

## Render Configuration

### Setting Up in Render Dashboard

1. **Create New Web Service**
   - Click "New +" -> "Web Service"
   - Connect your GitHub repository
   - Render auto-detects `render.yaml` if present

2. **Configure Service**
   - **Name**: `immich-adapter`
   - **Runtime**: Docker
   - **Region**: Choose based on user location
   - **Branch**: `main`
   - **Dockerfile Path**: `./Dockerfile`

3. **Environment Variables**

   Add these in the Render dashboard:

   ```
   ENVIRONMENT=production
   GUMNUT_API_BASE_URL=https://your-gumnut-api.com
   SENTRY_DSN=https://your-sentry-dsn
   ```

4. **Advanced Settings**
   - **Health Check Path**: `/api/server/ping`
   - **Auto-Deploy**: Yes (deploy on git push)
   - **Build Command**: Leave empty (Render uses Dockerfile)
   - **Start Command**: Leave empty (uses CMD from Dockerfile)

### Environment Variables in Docker

In the Dockerfile, we use `ENV` for build-time settings and expect runtime variables via Render:

```docker
# Build-time (in Dockerfile)
ENV PATH="/root/.cargo/bin:$PATH"

# Runtime (from Render dashboard)
# These are injected when container starts
# - ENVIRONMENT
# - GUMNUT_API_BASE_URL
# - SENTRY_DSN
```

## Testing Locally

### Prerequisites

- Docker installed
- Docker Compose (optional, for easier testing)

### Build and Run Locally

```bash
# 1. Build the image
docker build -t immich-adapter:local .

# 2. Run the container (use PORT environment variable)
docker run -p 8080:8080 \
  -e GUMNUT_API_BASE_URL=http://localhost:8000 \
  -e ENVIRONMENT=development \
  -e PORT=8080 \
  immich-adapter:local

# 3. Test in browser
open http://localhost:8080

# 4. Check static files are present
docker run --rm immich-adapter:local ls -lh static/_app/version.json
```

### Using Docker Compose (Recommended)

Create `docker-compose.yml`:

```yaml
services:
  app:
    build:
      context: .
      dockerfile: Dockerfile
    ports:
      - "8080:8080"
    environment:
      - ENVIRONMENT=development
      - GUMNUT_API_BASE_URL=http://localhost:8000
      - PORT=8080
    volumes:
      # Mount code for development (optional)
      - .:/app
    restart: unless-stopped

  # Optional: Add Gumnut API service if running locally
  # gumnut-api:
  #   image: your-gumnut-image
  #   ports:
  #     - "8000:8000"
```

**Usage:**

```bash
# Start services
docker-compose up

# Rebuild after changes
docker-compose up --build

# View logs
docker-compose logs -f app

# Stop services
docker-compose down
```

### Verifying Static Files

```bash
# Check static files exist in the image
docker run --rm immich-adapter:local ls -la static/

# Expected output:
# drwxr-xr-x   _app/
# -rw-r--r--   favicon.png
# -rw-r--r--   index.html
# ...

# Check file count matches expected
docker run --rm immich-adapter:local find static/ -type f | wc -l

# Verify version info
docker run --rm immich-adapter:local cat static/_app/version.json
```

### Testing the Web Interface

1. Start the container locally
2. Visit http://localhost:8080
3. Should see Immich login/interface
4. Check browser console for missing static file errors
5. Verify compressed files are served (check Network tab for .br/.gz)

## Migration from Native Runtime

### Current State (Native Python Runtime on Render)

Your current setup likely uses:

- **Runtime**: Python 3.12
- **Build Command**: `uv sync`
- **Start Command**: `uv run uvicorn main:app --host 0.0.0.0 --port $PORT` or similar
- **Static files**: Committed to repository or extracted separately

**Note on Ports**: Render automatically sets the `PORT` environment variable (typically 10000 for web services). Your application should bind to `0.0.0.0:$PORT` and Render handles SSL termination and routing from standard ports (80/443) to your app.

### Migration Steps

#### Step 1: Prepare Dockerfile (Done Above)

Create `Dockerfile` and `.dockerignore` as shown above.

#### Step 2: Test Locally First

```bash
# Build and test locally
docker build -t immich-adapter:test .
docker run -p 8080:8080 \
  -e GUMNUT_API_BASE_URL=http://localhost:8000 \
  -e PORT=8080 \
  immich-adapter:test

# Verify everything works
curl http://localhost:8080/api/server/ping
```

#### Step 3: Create render.yaml or Update Render Dashboard

**Option A: Use render.yaml (Recommended)**

- Commit `render.yaml` to repository
- Render will detect it automatically

**Option B: Manual Dashboard Configuration**

- Go to your service settings
- Change "Runtime" from Python to Docker
- Set "Dockerfile Path" to `./Dockerfile`
- Verify environment variables are still set

#### Step 4: Deploy

```bash
# Commit changes
git add Dockerfile .dockerignore render.yaml
git commit -m "feat: migrate to Docker deployment with automated Immich extraction"
git push origin main

# Render automatically triggers build
# Monitor in Render dashboard
```

#### Step 5: Verify Deployment

- Check build logs for any errors
- Verify health check passes
- Test the deployed application
- Check static files are served correctly

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

**Production:**

```docker
FROM ghcr.io/immich-app/immich-server:v1.95.1 AS immich-source
# TODO: Update to v1.96.0 on 2025-02-01
```

### Updating Immich Version

1. **Check for new versions**

   ```bash
   # Check GitHub releases
   curl -s https://api.github.com/repos/immich-app/immich/releases/latest | jq -r '.tag_name'
   ```

2. **Update Dockerfile**

   ```docker
   FROM ghcr.io/immich-app/immich-server:v1.96.0 AS immich-source
   ```

3. **Test locally first**

   ```bash
   docker build -t immich-adapter:v1.96.0-test .
   docker run -p 3001:3001 immich-adapter:v1.96.0-test
   # Test thoroughly
   ```

4. **Deploy to staging**

   ```bash
   git checkout -b update-immich-v1.96.0
   git add Dockerfile
   git commit -m "chore: update Immich to v1.96.0"
   git push origin update-immich-v1.96.0
   # Deploy to staging environment
   ```

5. **Deploy to production**

   ```bash
   # After testing passes
   git checkout main
   git merge update-immich-v1.96.0
   git push origin main
   ```

### Automated Version Checking

Create a GitHub Action to check for new versions:

```yaml
# .github/workflows/check-immich-version.yml
name: Check Immich Version

on:
  schedule:
    - cron: '0 0 * * 1'  # Weekly on Mondays
  workflow_dispatch:

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Get current version from Dockerfile
        id: current
        run: |
          VERSION=$(grep 'FROM ghcr.io/immich-app/immich-server:' Dockerfile | head -1 | cut -d: -f3 | cut -d' ' -f1)
          echo "version=$VERSION" >> $GITHUB_OUTPUT

      - name: Get latest Immich version
        id: latest
        run: |
          LATEST=$(curl -s https://api.github.com/repos/immich-app/immich/releases/latest | jq -r '.tag_name')
          echo "version=$LATEST" >> $GITHUB_OUTPUT

      - name: Create issue if outdated
        if: steps.current.outputs.version != steps.latest.outputs.version
        uses: actions/github-script@v7
        with:
          script: |
            github.rest.issues.create({
              owner: context.repo.owner,
              repo: context.repo.repo,
              title: 'New Immich version available: ${{ steps.latest.outputs.version }}',
              body: 'Current: ${{ steps.current.outputs.version }}\\nLatest: ${{ steps.latest.outputs.version }}\\n\\nUpdate Dockerfile to use the new version.',
              labels: ['dependencies', 'immich']
            })
```

## Troubleshooting

### Build Failures

#### Error: "failed to solve: image not found"

**Problem:** Can't pull Immich image

**Solutions:**

```bash
# Test pull manually
docker pull ghcr.io/immich-app/immich-server:release

# Check if tag exists
curl -s https://api.github.com/repos/immich-app/immich/releases/latest

# Try specific version instead of 'release'
FROM ghcr.io/immich-app/immich-server:v1.95.1 AS immich-source
```

#### Error: "uv: command not found"

**Problem:** uv installation failed

**Solutions:**

```docker
# Add error checking to uv install
RUN curl -LsSf https://astral.sh/uv/install.sh | sh || \
    (echo "Failed to install uv" && exit 1)
```

#### Error: Build timeout on Render

**Problem:** Build takes > 15 minutes (Render free tier limit)

**Solutions:**

- Upgrade to paid plan (longer timeout)
- Optimize Dockerfile (combine RUN commands)
- Use smaller base image
- Pre-build dependencies in separate stage

### Runtime Issues

#### Static files not found (404 errors)

**Check 1: Verify files exist in container**

```bash
# On Render, use Shell access or check logs
docker run --rm your-image ls -la static/

# Should see:
# index.html
# _app/
# favicon.png
# etc.
```

**Check 2: Verify file permissions**

```bash
docker run --rm your-image ls -lh static/index.html

# Should be readable:
# -rw-r--r-- 1 appuser appuser 1.2K index.html
```

**Check 3: Verify SPAStaticFiles directory**

```python
# In main.py, verify:
app.mount("/", SPAStaticFiles(directory="static", html=True), name="staticFileHosting")

# Directory should be "static" (relative to /app in container)
```

**Fix: Update COPY command if needed**

```docker
# Ensure destination matches main.py expectation
COPY --from=immich-source /build/www ./static/
# Not: /app/static/ or static/ or www/
```

#### Permission denied errors

**Problem:** appuser can't read static files

**Fix: Ensure proper ownership**

```docker
COPY --from=immich-source --chown=appuser:appuser /build/www ./static/
```

#### Container starts but health check fails

**Check health check endpoint:**

```bash
# Test locally (use PORT from environment)
curl http://localhost:${PORT:-8080}/api/server/ping

# Should return 200 OK
```

**Increase health check timing:**

```docker
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/api/server/ping || exit 1
```

### Performance Issues

#### Large image size (>1GB)

**Check current size:**

```bash
docker images immich-adapter:latest
```

**Optimizations:**

1. Use multi-stage builds (already doing this)
2. Use slim base image (already doing this)
3. Clean up apt caches (already doing this)
4. Minimize layers:

```docker
# Combine RUN commands
RUN apt-get update && apt-get install -y curl build-essential \
    && rm -rf /var/lib/apt/lists/*
```

#### Slow cold starts

**Problem:** Container takes 10-30s to start

**Solutions:**

- Health check `start-period` gives more time
- Optimize app startup (lazy load imports)
- Use `--workers 1` to avoid worker spawn overhead
- Pre-compile Python files

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

**Next steps to implement:**

1. Create `Dockerfile` and `.dockerignore` as shown above
2. Test locally with `docker build` and `docker run`
3. Create `render.yaml` or update Render dashboard settings
4. Deploy and monitor build logs
5. Verify static files are served correctly

## Additional Resources

- [Docker Multi-Stage Builds](https://docs.docker.com/build/building/multi-stage/)
- [Render Docker Deployment](https://render.com/docs/docker)
- [Immich Docker Images](https://github.com/immich-app/immich/pkgs/container/immich-server)
- [FastAPI in Docker](https://fastapi.tiangolo.com/deployment/docker/)
- [uv Docker Best Practices](https://docs.astral.sh/uv/guides/docker/)
