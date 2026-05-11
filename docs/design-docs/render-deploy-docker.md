---
title: "Render Deploy with Docker"
status: deprecated
superseded-by: ../guides/deploying-to-render.md
created: 2025-10-23
last-updated: 2026-05-11
---

# Render Deploy with Docker

> Deprecated: this document is preserved as historical design context. For
> current Render deployment steps, see
> [deploying-to-render.md](../guides/deploying-to-render.md).

## Context

`immich-adapter` needs to serve both the FastAPI adapter and the Immich web UI.
The web UI assets must come from the Immich version the adapter targets; serving
stale or manually copied assets can silently drift from the API compatibility
surface.

The original deployment design evaluated how to make that static asset pipeline
reproducible on Render.

## Decision

Deploy the adapter to Render as a Docker web service using a multi-stage image:

1. Pull a pinned Immich server image and copy the web assets from `/build/www`.
2. Build/install the Python adapter dependencies.
3. Produce a runtime image containing the adapter and the copied Immich web UI.

Render remains responsible for TLS termination and routing. The container binds
to the runtime `PORT` supplied by Render.

## Why Docker

Docker was chosen because it made the deployment artifact self-contained and
reproducible:

- The Immich web asset source is declared in the Dockerfile.
- Static assets are produced during image build rather than committed or copied
  manually.
- The Python runtime, adapter source, and web assets are tested together.
- Local smoke tests can exercise the same artifact shape Render deploys.
- Render does not need a separate predeploy asset extraction step.

## Alternatives Considered

### Native Render Runtime

The native Python runtime was simpler but left static web asset extraction as a
separate operational concern. That made it easier for the served web UI and the
adapter's targeted Immich version to drift.

### Committing Static Assets

Checking generated Immich web files into the repository would have made deploys
simple, but it would bloat the repo and obscure which upstream Immich image
produced the files.

### Manual Extraction Before Deploy

Manual extraction kept Docker simpler, but it made deployments dependent on
operator discipline and local environment state.

## Design Constraints

- The adapter must serve the Immich web UI from a known Immich version.
- Render should be able to build from the repository without hidden local steps.
- Production must bind to Render's `PORT`, not a hard-coded development port.
- The container should expose a health check suitable for Render.
- The image should run as a non-root user where practical.

## Outcome

The Docker deployment model became the production deployment shape. The
implementation has evolved since the original design:

- The Dockerfile pins the Immich server image through `ARG IMMICH_VERSION`.
- `.immich-container-tag` and the Dockerfile default must stay in sync.
- CI checks the Immich version sync contract.
- The runtime command uses uvicorn directly so production can set the WebSocket
  implementation and HTTP connection settings explicitly.
- Current deployment instructions live in an evergreen guide rather than this
  historical decision record.

## Current Documentation

Use these docs for implementation and operations:

- [Deploying to Render](../guides/deploying-to-render.md) - current Render
  setup, environment variables, and local Docker smoke tests
- [Uvicorn Server Settings](../references/uvicorn-settings.md) - runtime command
  and server setting rationale
- [Code Practices](../references/code-practices.md#bumping-the-immich-version)
  - Immich version bumping workflow
