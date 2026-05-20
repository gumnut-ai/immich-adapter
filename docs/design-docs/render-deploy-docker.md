---
title: "Render Deploy with Docker"
status: completed
created: 2025-10-23
last-updated: 2026-05-19
---

# Multi-Stage Docker Deployment Guide for Render

> **Note —** This doc was pruned on 2026-05-19 to its stable historical
> context: problem, goals, architecture, alternatives, and outcome.
> Implementation-specific detail (sample code, migration steps, rollout
> sequencing) was removed because it is now owned by the code and was
> drifting from the live implementation. For the current state, see the
> [`Dockerfile`](../../Dockerfile) and
> [`running-with-immich-web.md`](../guides/running-with-immich-web.md).

## Overview

This design chose a multi-stage Docker deployment for `immich-adapter` on Render. The key requirement was to ship the Immich web client and the adapter API from the same service without committing the generated Immich web bundle to the repository.

The final approach uses the Immich server Docker image as a build-stage source for prebuilt web files, then copies only `/build/www` into the adapter runtime image. The adapter serves those static files from its FastAPI process while Render handles TLS termination and routes traffic to the container's `$PORT`.

## Context

The adapter originally needed a way to serve Immich Web in production. The compiled Immich web client expects static assets and API endpoints to share one origin:

- Static app entry points such as `/index.html`
- API routes such as `/api/albums`
- WebSocket routes under the same host

Committing the generated web bundle worked short-term but added about 29 MB of generated files to the repo and made Immich version updates manual and easy to forget.

## Design Goals

- Keep Immich static files out of the Git repository.
- Build a reproducible deployment artifact from a declared Immich image version.
- Preserve the single-origin deployment model expected by Immich Web and mobile clients.
- Avoid a separate static-site or reverse-proxy service.
- Keep local and Render behavior close enough that deployment issues can be reproduced.

## Chosen Architecture

The deployment uses a Docker multi-stage build:

1. An Immich image stage provides the prebuilt web bundle from `/build/www`.
2. A Python build stage installs adapter dependencies.
3. The runtime stage copies adapter code, dependencies, and the Immich web files into one image.

At runtime, the adapter binds to `0.0.0.0:$PORT`. Render terminates TLS at the edge and forwards traffic to that internal port. The Dockerfile must not hardcode the development port used by local adapter runs.

Static file serving remains an application concern. `main.py` mounts `SPAStaticFiles(directory="static", html=True)` at the root after API routes so that `/api/*` routes continue to hit the adapter while SPA routes fall back to `index.html`.

## Build Flow

Render builds the Docker image on deployment. The Immich server image is pulled during the build, the web bundle is copied into the final runtime image, and the resulting container serves both the API and the web app.

The main trade-off is build time and image size. The Immich source image is large, but only the web bundle is copied into the final image. The final image is larger and slower to build than a native Python runtime deployment, but the deployment is more reproducible and no longer depends on committed generated assets.

## Immich Version Management

The design considered two versioning modes:

| Mode | Benefit | Risk |
|------|---------|------|
| Floating `release` tag | Automatic updates to the latest stable Immich bundle | Rebuilds can pick up untested UI changes |
| Pinned `vX.Y.Z` tag | Predictable, reproducible builds | Manual updates are required |

The durable decision is to treat the Immich image tag as a deployment input owned by the Dockerfile. Production should prefer pinned tags when compatibility matters; staging can use a floating tag if the team wants early warning on Immich changes.

## Render Considerations

Render injects `$PORT` at runtime. The adapter container must bind to that port and expose a health check route that Render can use before shifting traffic to a new deployment.

The Docker deployment adds a few operational costs compared with native Python runtime deployment:

- First builds are slower because the Immich image and Python dependencies must be fetched.
- Final image transfers are larger than the native runtime install.
- Cold starts can be slower than native runtime starts.

Those costs were accepted because the deployment removes manual static-file extraction and keeps the repository clean.

## Alternatives Considered

| Option | Pros | Cons |
|--------|------|------|
| Commit generated static files | Fast deploys; simple runtime | Generated assets bloat the repo and drift from the intended Immich version |
| Native Python runtime plus manual extraction | Small runtime image | Hidden build step; easy to miss during deploys |
| Render static site plus API service | Clean static hosting | Breaks the single-origin/websocket model unless paired with more routing infrastructure |
| Reverse proxy service | Flexible routing | Additional service and configuration complexity |
| Multi-stage Docker build | Reproducible; no committed static assets; one deployed service | Larger images and slower builds |

## Outcome

The multi-stage Docker approach became the long-term deployment path for serving Immich Web from `immich-adapter`. The live implementation now lives in the repository's Dockerfile, startup configuration, and static-serving code rather than this design doc.

## Additional Resources

- [Docker Multi-Stage Builds](https://docs.docker.com/build/building/multi-stage/)
- [Render Docker Deployment](https://render.com/docs/docker)
- [Immich Docker Images](https://github.com/immich-app/immich/pkgs/container/immich-server)
- [FastAPI in Docker](https://fastapi.tiangolo.com/deployment/docker/)
- [uv Docker Best Practices](https://docs.astral.sh/uv/guides/docker/)
