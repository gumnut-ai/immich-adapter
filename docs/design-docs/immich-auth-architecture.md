---
title: "Immich Authentication Architecture"
status: deprecated
superseded-by: auth-design.md
created: 2025-10-21
last-updated: 2026-05-19
---

# Immich Authentication Architecture

> **Note —** This doc was pruned on 2026-05-19 to its stable historical
> context: problem, goals, architecture, alternatives, and outcome.
> Implementation-specific detail (sample code, endpoint specs, test plans,
> and rollout sequencing) was removed because it is now owned by the code and
> was drifting from the live implementation. For the current design, see
> [`auth-design.md`](auth-design.md).

## Overview

This deprecated design explored how Immich Web and Immich Mobile clients could authenticate to Gumnut while preserving compatibility with Immich's client expectations.

The central product goal was to let users sign in through Gumnut while still using Immich clients that expect Immich-style auth flows, cookies, bearer tokens, OAuth callbacks, and authenticated API requests.

## Background

Immich supports username/password authentication and third-party authentication through OpenID Connect. Gumnut's product direction was to rely on Gumnut-owned authentication and authorization rather than running a standalone Immich identity system.

That created an adapter boundary:

- Immich clients should continue speaking Immich-compatible API shapes.
- Gumnut should own user identity, token issuance, provider validation, and account lifecycle.
- The adapter should translate client expectations without becoming a second authentication authority.

## Product Considerations

The design considered two broad user experiences:

- Keep Immich's native login screens and map those requests through the adapter.
- Send users through Gumnut-owned authentication and return Immich-compatible session material to the clients.

The second direction won because it kept identity policy centralized in Gumnut and avoided maintaining a parallel Immich user database.

## Adapter-Side Design

The adapter was originally framed as a simple proxy with middleware:

1. Immich clients call adapter endpoints.
2. Middleware extracts credentials from cookies or `Authorization` headers.
3. The adapter forwards authenticated API requests to the Gumnut backend.
4. Middleware handles token refresh material from backend responses and shapes it for the client type.

The adapter intentionally did not validate OAuth provider tokens, own user provisioning, or store long-lived authentication state. Those responsibilities belonged to the backend.

### Design Constraints

- Immich Web uses cookies and browser redirects.
- Immich Mobile uses bearer-token style API requests.
- OAuth flows must preserve redirect URI and PKCE compatibility where clients require it.
- API forwarding should not require every route handler to implement its own auth logic.

### Architecture

The design split responsibility across four components:

| Component | Responsibility |
|-----------|----------------|
| Web client | Starts OAuth flow, follows browser redirects, stores browser session cookies |
| Mobile client | Starts OAuth flow, stores bearer token material, sends authenticated API requests |
| Immich adapter | Preserves Immich-compatible endpoint behavior and translates auth material at the edge |
| Gumnut backend | Owns OAuth provider validation, user provisioning, JWT issuance, refresh policy, and protected resource authorization |

## Authentication Flows

The design covered four major flows:

1. **Initial authentication and token exchange**: the client starts OAuth through the adapter; the backend produces Gumnut-issued token material; the adapter returns Immich-compatible response fields.
2. **Subsequent API requests**: the adapter extracts credentials from browser cookies or mobile bearer headers and forwards requests to the backend.
3. **Token refresh**: refresh responsibility stays with the backend; the adapter updates cookies for web clients or forwards refresh headers for mobile clients.
4. **Token revocation/logout**: logout clears client-side session material and delegates invalidation to backend-owned auth state where applicable.

These flows were kept at the adapter boundary so the adapter could remain mostly stateless.

## Backend-Side Design

The backend was responsible for:

- Creating OAuth authorization URLs.
- Exchanging provider authorization codes for Gumnut token material.
- Validating OAuth provider identity claims.
- Provisioning or linking Gumnut users.
- Signing and validating Gumnut-issued JWTs.
- Enforcing authorization on protected API endpoints.

This division kept OAuth provider trust decisions out of the adapter.

## Security Considerations

The design emphasized:

- **CSRF/state validation**: OAuth state should be generated, stored, validated, and consumed by the backend.
- **PKCE support**: public clients can include PKCE challenge material during authorization and verification material during token exchange.
- **JWT handling**: the backend owns token signing, expiration policy, validation, and refresh behavior.
- **Adapter statelessness**: the adapter should not become a durable token store.
- **Provider validation**: OAuth token and identity validation belongs to the backend, not the adapter.
- **Cookie security**: browser session cookies need secure attributes appropriate for cross-site OAuth redirects and production HTTPS.
- **CORS policy**: production CORS should be narrowly scoped to known client origins.

## Outcome

This document was superseded by [`auth-design.md`](auth-design.md), which captures the updated session-token architecture. The durable historical value of this doc is the responsibility split: Immich clients remain compatibility targets, the adapter translates at the API edge, and Gumnut backend services own identity and trust decisions.
