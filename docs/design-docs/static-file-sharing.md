---
title: "Static File Sharing Decision"
status: deprecated
superseded-by: ../guides/running-with-immich-web.md
created: 2025-10-26
last-updated: 2026-08-07
---

# Static File Sharing Decision

> **Deprecated —** This document records why the adapter serves Immich web
> files itself and why the project chose Docker extraction over a submodule or
> a separate static site. The current extraction workflow lives in
> [`running-with-immich-web.md`](../guides/running-with-immich-web.md), and the
> running adapter behavior is owned by the implementation. This record is
> retained for its historical rationale and is no longer updated as the system
> changes.

## The Problem

When Immich-web is run locally in development code, vite is used to proxy requests from `/api/*` to a server specified by `IMMICH_SERVER_URL`.

In a production setting, the compiled Immich web client expects to have both the static files of the single page application and the backend API endpoints served from a single server. For example:

- https://immich.gumnut.ai/index.html
  - the root of the SPA
- https://immich.gumnut.ai/api/albums
  - endpoint on the backend

## Possible Solutions

After researching the issue, three solutions were found:

- Use a Render static site to pull from the immich repository, build the web client files, and host them. Rewrite rules are used to route `/api/*` calls to the separate immich-adapter server.
- Use a reverse proxy service on Render employing nginx to route `/api/*` endpoint calls to one server (immich-adapter) and static files (basically everything outside of `/api/*`) to Render static site.
- Implement the same functionality in immich-adapter that exists in a production Immich environment - a single server that serves both static files and exposes the Immich endpoints.

### Pros and Cons of Solutions

| Option | Pros | Cons |
|--------|------|------|
| Pure static site | Easy to setup and maintain; No code changes | Does not support websocket access; Cannot be replicated on a developer's machine |
| nginx reverse proxy | Supports everything, including websockets; No code changes | Very complex setup |
| immich-adapter static serving | Supports everything; Can easily be used in development mode, but is not required | Immich web files need to be part of the `immich-adapter` repository |

## Chosen Serving Approach

The project chose to serve the static files from immich-adapter. FastAPI makes
this straightforward, while a custom static-files class provides the behavior
needed by the Immich web client:

- Cache headers for files requested from `/_app/immutable`
- `.br` or `.gz` responses when the client supports them and a compressed file exists
- `index.html` fallback for SPA routes

This supports both the local Vite development-server workflow and the
single-server workflow used for mobile testing. The current setup steps and
the Docker extraction command are maintained in the web-running guide.

### Static File Source Options Considered

The final part of sharing the immich-web static files is the actual files themselves - where do we get them?

The terminally simple method is to build the immich-web files locally, copy them into the immich-adapter repository and commit them. This method requires constant diligence to track commits in the immich repository and then to build and commit those files to the immich-adapter repository - a non-starter.

A second method was to include the immich repository as a `git submodule` within the immich-adapter repository. As with the simple method, this would require keeping track of new commits to the immich repository, but only a commit to a single file within the immich-adapter repository would be required. A helper script would then build the static files for immich-web for developers and the Render deploy process.

A third method was to use Docker to pull a pre-built immich-server container from the GitHub Container Registry, extract the built immich-web files, copy them to a hosting directory, and clean up. The specific container would be selected by a tag in `.immich-container-tag`; developers could use the same process to reproduce the production files locally.

### Static File Source Decision

The project chose the Docker extraction option. The Docker build copies the
web files from the pinned Immich server image, while the extraction script
supports local development. This decision avoids committing built assets and
keeps the selected Immich version in a version-controlled build input.
