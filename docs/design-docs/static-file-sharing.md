---
title: "Static File Sharing Proposal"
status: active
created: 2025-10-26
last-updated: 2025-10-26
---

# Proposed Immich-adapter Static File Sharing

## The Problem

When Immich-web is run locally in development code, vite is used to proxy requests from `/api/*` to a server specified by `IMMICH_SERVER_URL`.

In a production setting, the compiled Immich web client expects to have both the static files of the single page application and the backend API endpoints served from a single server. For example:

- https://photos.gumnut.ai/index.html
  - the root of the SPA
- https://photos.gumnut.ai/api/albums
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

## Proposed Solution

Adding the ability to serve static files to immich-adapter appears to be the best solution. Serving static files is a built-in feature of FastAPI, so the implementation is fairly simple. The functionality that will need to be supported is:

- Flagging files requested from `/_app/immutable` with a specific `Cache-Control` header
- Ensuring `.br` or `.gz` files are served to the client if the client can support them and the compressed version of the requested file exists
- Serving `index.html` for SPA routing

This functionality allows a Gumnut developer to either continue using an immich-web development server pointing to a immich-adapter server (using environment variable `IMMICH_SERVER_URL`), or to use immich-adapter to serve both. No configuration changes are needed to switch between the two - they can both work at the same time. While working locally, the developer can choose either method, but if they are working with the mobile client, they will need to use immich-adapter to serve both. (HTTPS is needed on the immich-adapter server for mobile OAuth to work, due to redirect URIs to anything other than localhost requiring HTTPS.)

### Static File Source Proposals

The final part of sharing the immich-web static files is the actual files themselves - where do we get them?

The terminally simple method is to build the immich-web files locally, copy them into the immich-adapter repository and commit them. This method requires constant diligence to track commits in the immich repository and then to build and commit those files to the immich-adapter repository - a non-starter.

A second method is to include the immich repository as a `git submodule` within the immich-adapter repository. As with the simple method, this does require keeping track of new commits to the immich repository, but only a commit to a single file within the immich-adapter repository is required. A helper script will be written to build the static files for immich-web. This script will be used by both developers and the Render deploy process.

A third method is to create a bash script that will use docker to pull a pre-built immich-server container from the GitHub Container Registry, extract the built immich-web files from the container, and copy them to a directory in immich-web for hosting, and then clean up. The specific container to retrieve will be specified by a tag defined in `.immich-container-tag`. While the script is intended to be used by the build process on Render, a developer can also use it to mimic the production environment.

### Static File Source Decision

We will implement the script to pull the immich container and extract the sources with the use of docker.

## Appendix

### Git Submodules Basics

A git submodule is a git repository embedded inside another git repository. It allows you to keep a separate repository as a subdirectory of your main repository while maintaining its own history and version control.

### How It Works for Your Use Case

#### Setup

```bash
# In immich-adapter repository
git submodule add https://github.com/immich-app/immich.git immich
```

This creates:

- A `immich/` directory containing the full Immich repository
- A `.gitmodules` file tracking the submodule configuration
- A commit reference in immich-adapter that "pins" to a specific commit in the Immich repo

#### Key Characteristics

1. **Pinned to Specific Commits**: The immich-adapter repository doesn't track the latest Immich code automatically -- it points to a *specific commit hash*. This is stored in your immich-adapter repo.

2. **Updating the Submodule**: When new Immich commits are available:

   ```bash
   cd immich
   git pull origin main  # or checkout specific commit
   cd ..
   git add immich
   git commit -m "Update immich submodule to version X.Y.Z"
   ```

   This updates the pinned commit in immich-adapter.

3. **Cloning for Developers**:

   ```bash
   git clone --recurse-submodules <immich-adapter-repo>
   # or if already cloned:
   git submodule update --init --recursive
   ```

4. **Building Static Files**: Your helper script would:
   - Navigate to the `immich/web` directory
   - Run the build process
   - Copy the built files to where immich-adapter serves them from

#### Benefits for Your Solution

- **Version Control**: Exact tracking of which Immich version you're using
- **Reproducibility**: Anyone cloning immich-adapter gets the exact same Immich version
- **No Binary Commits**: You commit a 40-character hash instead of megabytes of static files
- **Clear Updates**: `git status` shows when the submodule reference has changed

#### Workflow Impact

- Developers must remember to initialize/update submodules
- Your Render deployment script needs to include submodule initialization
- Updates require two steps: update submodule, then commit the reference

### What Happens If a Developer Does Not Use --recurse-submodules

#### What They'll See

```bash
git clone <immich-adapter-repo>
cd immich-adapter
ls immich/
# The immich/ directory exists but is EMPTY
```

The submodule directory is created but contains no files. Git knows a submodule should exist there (from `.gitmodules`), but hasn't fetched the actual code.

#### Problems This Causes

1. **Build Script Fails**: When they run your helper script to build static files, it will fail because `immich/web/` doesn't exist or is empty
2. **Confusing Error Messages**: They might see errors like:
   - `cd: immich/web: No such file or directory`
   - `npm: command not found` (if the script tries to run in an empty directory)
   - File not found errors
3. **Silent Issue**: `git status` won't show anything wrong -- the repository looks fine from Git's perspective

#### How Developers Fix It

They need to manually initialize the submodules:

```bash
git submodule update --init --recursive
```

#### Best Practices to Help Developers

1. **Clear README instructions**: Mention the submodule requirement prominently in your README

2. **Check in your build script**:

   ```bash
   #!/bin/bash
   if [ ! -f "immich/web/package.json" ]; then
       echo "Error: Immich submodule not initialized"
       echo "Run: git submodule update --init --recursive"
       exit 1
   fi
   ```

3. **Post-clone hook or setup script**:

   ```bash
   # setup.sh
   git submodule update --init --recursive
   cd immich/web && npm install
   ```

#### Summary

This is one of the main pain points of submodules -- they're not automatic and can confuse new developers who forget to initialize them.
