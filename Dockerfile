# syntax=docker/dockerfile:1

# Build arguments for versioning and metadata
# Override with: docker build --build-arg IMMICH_VERSION=v2.2.3 .
# Pinned to specific version for reproducible builds
# Last updated: 2025-11-11 (Immich v2.2.3)
ARG IMMICH_VERSION=v2.2.3
ARG GIT_COMMIT=unknown
ARG BUILD_DATE=unknown

# ============================================================================
# Stage 1: Extract Immich web files
# ============================================================================
FROM ghcr.io/immich-app/immich-server:${IMMICH_VERSION} AS immich-source

# We only need this stage to copy files from - no commands needed
# The /build/www directory in this image contains the web interface files


# ============================================================================
# Stage 2: Build the immich-adapter application
# ============================================================================
FROM python:3.12-slim AS builder

# Copy uv from official image - pinned to specific version for reproducibility
# Version 0.9.8 released 2025-11-07
COPY --from=ghcr.io/astral-sh/uv:0.9.8 /uv /usr/local/bin/uv

# Set working directory
WORKDIR /app

# Copy dependency files (uv.lock is required for --frozen flag)
COPY pyproject.toml uv.lock ./

# Install dependencies (no dev dependencies in production)
# --frozen ensures reproducible builds by requiring exact lock file
RUN uv sync --frozen --no-dev


# ============================================================================
# Stage 3: Runtime image (final, smallest image)
# ============================================================================
FROM python:3.12-slim

# Propagate build args for labels
ARG GIT_COMMIT
ARG BUILD_DATE
ARG IMMICH_VERSION

# Add metadata labels following OCI standard
LABEL org.opencontainers.image.created="${BUILD_DATE}"
LABEL org.opencontainers.image.revision="${GIT_COMMIT}"
LABEL org.opencontainers.image.source="https://github.com/gumnut-ai/immich-adapter"
LABEL org.opencontainers.image.title="Immich Adapter"
LABEL org.opencontainers.image.description="FastAPI server that adapts the Immich API to the Gumnut API"
LABEL immich.version="${IMMICH_VERSION}"

# Create non-root user for security
RUN useradd -m -u 1000 appuser

# Set working directory
WORKDIR /app

# Copy installed dependencies from builder stage with proper ownership
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/pyproject.toml /app/uv.lock ./

# Copy application code
COPY --chown=appuser:appuser . .

# Copy Immich web files from first stage
COPY --from=immich-source --chown=appuser:appuser /build/www ./static/

# Verify critical Immich files exist (fail build if missing)
RUN test -f ./static/index.html || (echo "ERROR: Immich index.html not found in /build/www" && exit 1) && \
    test -d ./static/_app || (echo "ERROR: Immich _app directory not found in /build/www" && exit 1) && \
    echo "✓ Immich static files verified"

# Switch to non-root user
USER appuser

# EXPOSE is documentation only - Render uses PORT env var (typically 10000)
# Local development typically uses 8080
EXPOSE 8080

# Health check (use PORT from environment, default to 8080)
# Increased start-period to 30s to allow for slower container starts
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/api/server/ping || exit 1

# Set Python to use the virtual environment
ENV PATH="/app/.venv/bin:$PATH"

# Prevent Python from writing .pyc files
ENV PYTHONDONTWRITEBYTECODE=1

# Ensure logs flush promptly
ENV PYTHONUNBUFFERED=1

# Default log level (can be overridden via environment variable)
ENV LOG_LEVEL=info

# Run the application with optimized uvicorn settings
# Render sets PORT environment variable automatically (typically 10000)
# Using exec form with shell for proper signal handling and variable substitution
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --log-level ${LOG_LEVEL} --timeout-graceful-shutdown 60 --timeout-keep-alive 75 --limit-concurrency 1000 --backlog 2048"]
