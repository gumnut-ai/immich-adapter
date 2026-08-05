---
title: "Updated Authentication Design"
status: deprecated
superseded-by: ../architecture/adapter-architecture.md
created: 2025-12-06
last-updated: 2026-08-05
---

# Updated Authentication Design Document

> **Deprecated —** This document records why the adapter introduced stable
> session tokens in front of Gumnut JWTs. It was pruned on 2026-08-05 to the
> constraints, architecture, rationale, and outcome; endpoint-by-endpoint flows
> and component inventories are owned by the implementation. For current
> behavior, see [`adapter-architecture.md`](../architecture/adapter-architecture.md)
> and [`session-checkpoint-implementation.md`](../architecture/session-checkpoint-implementation.md).

Date: 2025-12-05

## Overview

This document describes the OAuth/OpenID Connect authentication architecture for the Immich Adapter system. The adapter manages **session tokens** for Immich clients while delegating OAuth validation and JWT management to the Gumnut backend.

## Historical Background

Earlier iterations passed the Gumnut JWT directly to clients and derived session identity by hashing that JWT. That model broke session continuity when the backend rotated a token, because a refreshed JWT implied a new hash and orphaned any checkpoints keyed to the old value.

## Implemented Architecture

The adapter now generates a stable UUID session token at login and returns that token to clients instead of the raw JWT. The Gumnut JWT is encrypted and stored in Redis, keyed by the session token. On each request, the middleware extracts the session token, looks up the stored JWT, and forwards it to the backend. When the backend refreshes the JWT, the adapter updates the stored value in Redis while keeping the client's session token unchanged. This keeps sessions and checkpoints stable across JWT refresh cycles, enables immediate session revocation, and ensures raw JWTs are never exposed to clients.

**API-key clients are the exception.** The session-token model above governs the interactive web and mobile clients. Headless Immich API-key clients (e.g. the immich-go CLI) instead send an `x-api-key` header carrying a Gumnut API key (`apikey_...`); the middleware forwards that value straight to the backend as the caller credential, with no Redis session and no JWT refresh (the key is long-lived and the backend does not refresh it). Nothing is stored adapter-side for these requests. This branch is checked ahead of the session-token sources and is documented in `docs/guides/importing-with-immich-go.md`; the sections below describe the session-token path.

## Design Constraints

### Immich Client Compatibility Requirements

- **Cannot modify the Immich clients** - We use the third-party client as-is
- **Cannot change API endpoint signatures** - Must conform to Immich OpenAPI spec
- **Cannot add new endpoints** - Limited to existing Immich API surface
- **Must support long-lived tokens** - Immich client expects persistent access tokens

### Architectural Principles

- **Backend handles OAuth/JWT logic** - Token generation, validation, refresh, and revocation
- **Adapter manages sessions** - Generates session tokens, stores encrypted JWTs
- **Session tokens are stable** - JWT refresh does not invalidate sessions or checkpoints
- **Security in backend** - All OAuth validation and JWT operations in backend

## Architecture

### System Components

```
┌─────────────┐         ┌──────────────────┐         ┌─────────────────┐
│   Immich    │         │ Immich Adapter   │         │ Gumnut Backend  │
│   Client    │         │                  │         │                 │
│ (Mobile/Web)│         │                  │         │                 │
│             │────────▶│ Session Manager  │────────▶│ Issues JWTs     │
│ Stores      │         │                  │         │                 │
│ session     │         │ Generates UUIDs  │         │ Validates JWTs  │
│ tokens      │         │ as session IDs   │         │                 │
│             │         │                  │         │ Validates OAuth │
│             │         │ Stores encrypted │         │                 │
│             │         │ JWTs in Redis    │         │ Handles refresh │
│             │         │                  │         │                 │
└─────────────┘         └──────────────────┘         └─────────────────┘
                                │                            │
                                ▼                            ▼
                         ┌──────────────┐            ┌──────────────┐
                         │    Redis     │            │ OAuth        │
                         │  (Sessions)  │            │ Provider     │
                         └──────────────┘            └──────────────┘
                                                             │
                                                             ▼
                                                      ┌──────────────┐
                                                      │    Clerk     │
                                                      │ (User Store) │
                                                      └──────────────┘
```

## Session Token Architecture

### Why Session Tokens (Not Raw JWTs)

The adapter uses a **separate session token** (a UUID) instead of exposing the Gumnut JWT directly to clients:

1. **JWT Refresh Stability**: Gumnut may refresh the JWT, but the session token remains stable
2. **Checkpoint Preservation**: Sync checkpoints are tied to the stable session ID, not a changing JWT
3. **Session Revocation**: Deleting a session immediately revokes access
4. **Security**: Raw Gumnut JWTs never leave the adapter

### Authentication Flow Summary

1. User logs in via OAuth -> Gumnut returns JWT
2. Adapter generates a session token and stores the encrypted JWT in Redis
3. Client receives the session token as `accessToken`
4. On each request, client sends session token -> adapter looks up session -> retrieves stored JWT for Gumnut API calls

### Redis Data Model

For the complete Redis data model, including:

- Session storage schema
- Checkpoint storage (tied to sessions)
- User session indexes

See [`docs/references/session-checkpoint-reference.md`](../references/session-checkpoint-reference.md), which holds the field-level schema.

## Conclusion

This design implements a **session token architecture** for OAuth authentication. The Immich Adapter manages session tokens and stores encrypted Gumnut JWTs, while the Gumnut Backend handles all OAuth validation, user management, and JWT operations.

**Trade-offs:**

- Backend must implement full auth system (JWT generation, validation, refresh, revocation)
- Backend must determine JWT expiration policy
- Adapter requires Redis for session storage
- Extra Redis lookup per request (~1-2ms)

This design provides a **robust foundation** for authentication while maintaining full compatibility with the Immich client, supporting checkpoints for sync, and allowing the backend to implement sophisticated authentication logic as needed.
