---
title: "Immich Authentication Architecture"
status: deprecated
superseded-by: auth-design.md
created: 2025-10-21
last-updated: 2026-05-11
---

# Immich Authentication Architecture

> Deprecated: this document is preserved as historical design context. For the
> current auth/session design, see [auth-design.md](auth-design.md) and the auth
> summary in [adapter-architecture.md](../architecture/adapter-architecture.md).

## Context

Gumnut wanted unmodified Immich web and mobile clients to authenticate against
Gumnut while using the authentication options already configured in Clerk:
social login, email-based login, and username/password flows where available.
Immich supports OpenID Connect (OIDC), but its clients expect an Immich-shaped
login flow and a long-lived access token they can store and reuse.

The original design explored how to make the adapter fit between those two
systems:

```text
Immich client -> immich-adapter -> Gumnut backend -> Clerk/OAuth provider
```

## Product Goals

- Users should authenticate through the hosted Gumnut/Clerk login experience,
  not a separate Immich-specific account system.
- Existing browser sessions should avoid unnecessary re-login where the OAuth
  provider allows it.
- Mobile and web clients should stay logged in across normal usage patterns.
- The adapter should preserve Immich API compatibility without requiring client
  changes.
- Backend resource authorization should remain a Gumnut backend concern.

## Design Constraints

### Immich Client Compatibility

- We cannot change Immich client endpoint signatures.
- We cannot add Gumnut-specific client flows outside the Immich API surface.
- Mobile clients need a token they can persist across app launches.
- Web clients need cookie behavior compatible with the Immich web app.

### Gumnut Auth Boundary

- Clerk/OAuth validation belongs in the Gumnut backend.
- JWT claim validation and resource authorization belong in the backend.
- The adapter should not become an independent auth authority for Gumnut data.
- Any adapter token behavior must exist only to bridge Immich client
  expectations to Gumnut's backend auth model.

## Original Design Direction

The original proposal treated the adapter as a mostly stateless auth proxy:

- The adapter forwards OAuth authorization and callback requests to the backend.
- The backend validates OAuth state, exchanges authorization codes, checks Clerk,
  and issues Gumnut JWTs.
- The adapter returns the JWT through Immich-compatible response shapes and
  cookies.
- Subsequent requests carry that JWT back through the adapter to the backend.
- JWT refresh remains a backend responsibility; the adapter only forwards the
  refreshed token to the client.

The appeal of this design was a clean boundary: the backend owned all auth
logic, while the adapter only translated Immich-shaped requests.

## Why This Was Superseded

The stateless proxy model exposed a lifecycle mismatch:

- Immich clients expect stable, long-lived access tokens.
- Gumnut JWTs are intentionally shorter-lived and may be refreshed.
- Sync checkpoints and device sessions need a stable session identity.
- Exposing raw backend JWTs to clients made revocation and refresh behavior
  harder to control.

The current design introduced adapter-generated session tokens backed by Redis.
Clients receive stable session tokens; the adapter stores encrypted backend JWTs
behind those session tokens and updates the stored JWT when the backend refreshes
it. This preserves Immich compatibility while keeping raw Gumnut JWTs inside the
adapter/backend boundary.

## Decisions That Carried Forward

The current design kept several principles from this original design:

- OAuth and JWT validation remain backend responsibilities.
- The adapter conforms to Immich endpoint contracts.
- The adapter detects client type only to present auth state in the shape each
  client expects.
- Backend JWTs are used for Gumnut API calls; Immich clients are not granted
  independent authorization authority.
- Cookie security matters for web compatibility and must stay explicit.

## Historical Security Notes

The original design called out common cookie and OAuth security concerns that
remain relevant:

- Auth-bearing cookies should be `HttpOnly`.
- Cookies should use `Secure` outside local HTTP development.
- Cookies need an explicit `SameSite` policy.
- Credentialed CORS should not use broad wildcard origins.
- OAuth state/nonce validation belongs in the backend OAuth exchange.

Current cookie behavior is documented in
[Adapter Architecture](../architecture/adapter-architecture.md#session-lifecycle).

## Current Documentation

Use these docs for implementation work:

- [Authentication Design](auth-design.md) - current OAuth/session-token design
- [Adapter Architecture](../architecture/adapter-architecture.md) - operational
  auth/session summary
- [Session & Checkpoint Implementation](../architecture/session-checkpoint-implementation.md)
  - Redis session and checkpoint storage details
