---
title: "Immich Authentication Architecture"
status: deprecated
superseded-by: auth-design.md
created: 2025-10-21
last-updated: 2026-07-11
---

# Immich Authentication Architecture

> **Note —** Pruned 2026-07-11. This is a **deprecated** design for an abandoned *stateless-proxy* auth approach; the shipped design is the session-token architecture in [`auth-design.md`](auth-design.md). Retained here for historical context: the problem framing, the stateless-proxy decision, the architecture diagrams and component responsibilities, the authentication flows, the security-design rationale, and the appendix reasoning on OAuth client ownership and mobile Universal Links. Removed because it described an approach that never shipped this way: the per-endpoint request/response specs, the test-name lists, the backend JWT/state/user-provisioning implementation detail, the environment-variable and settings inventories, and the BAD/GOOD code samples — each reduced to its takeaway.

## Overview

One of the strategic initiatives for Gumnut is leverage Immich and the Immich ecosystem, and position Gumnut as a cloud-based alternative to self-hosting Immich. As part of making this work, we need to figure out how to enable the Immich Web and Immich Mobile (Android, iOS) clients to authenticate to Gumnut using either (a) social login providers like Google or (b) their Gumnut username and password.

## Background

Immich supports both username/password authentication as well as 3rd party authentication via [OpenID Connect](https://openid.net/connect/) (OIDC), an identity layer built on top of OAuth2.

How OAuth works: https://clerk.com/blog/how-oauth-works

### Username/password authentication

This is the Immich login screen when OAuth is not enabled.

This is the Immich login screen when OAuth is enabled.

For reference, here's the corresponding Gumnut login screen for the dashboard app (accounts.gumnut.ai, hosted by Clerk).

### OAuth authentication

[OAuth Authentication | Immich](https://docs.immich.app/administration/oauth/)

Immich only supports the ability to show one OAuth provider on its login screen. Here's the configuration screen in Immich for OAuth.

For reference, here's the corresponding Clerk configuration screen for Google OAuth.

## Product thoughts

When a user arrives at the Immich login screen:

- If they already have a valid session cookie, they should automatically get logged in and see their dashboard.
- If they do not have a valid session cookie, they should see the Gumnut login screen, hosted by Clerk. Gumnut users may be using username/password authentication, email-based authentication, social login, or may have multiple options enabled. We show all of the authentication options that Gumnut has configured in Clerk.

A user using a particular client/device should stay logged in to Immich as long as they've used Gumnut within the past week.

- If a user checks their photos on Friday, then again sometime on Monday, they shouldn't have to log in again on Monday -- the authentication credentials haven't expired.
- If a user uses Gumnut once per day, each day of the week, they should never have to log in, even after a week -- the authentication credentials get renewed automatically.
- If a user logs into Gumnut to check their photos (photos.gumnut.ai), then goes to the Gumnut dashboard (app.gumnut.ai), they shouldn't have to log in again.

## Technical Design

### Overview

This document describes the OAuth/OpenID Connect authentication architecture for the Immich Adapter system. The adapter acts as a **simple proxy** between the Immich clients and the Gumnut backend, forwarding authentication requests and responses without implementing authentication logic itself.

Configure Clerk as Immich's Identity Provider ([Clerk SSO documentation](https://clerk.com/docs/guides/configure/auth-strategies/oauth/single-sign-on#option-2-let-users-authenticate-into-third-party-applications-using-clerk-as-an-identity-provider-id-p))

## Adapter-side Design

### Design Constraints

#### Immich Client Compatibility Requirements

- **Cannot modify the Immich clients** - We use the third-party client as-is
- **Cannot change API endpoint signatures** - Must conform to Immich OpenAPI spec
- **Cannot add new endpoints** - Limited to existing Immich API surface
- **Must support long-lived tokens** - Immich client expects persistent access tokens

#### Architectural Principles

- **Backend handles all auth logic** - Token generation, validation, refresh, and revocation
- **Adapter is a proxy** - Simple forwarding with no business logic
- **Stateless adapter** - No token storage or session management in adapter
- **Security in backend** - All OAuth validation and JWT operations in backend

### Architecture

#### System Components

```
┌─────────────┐         ┌──────────────────┐         ┌─────────────────┐
│   Immich    │         │ Immich Adapter   │         │ Gumnut Backend  │
│   Client    │         │                  │         │                 │
│ (Mobile/Web)│         │                  │         │                 │
│             │────────▶│  Simple Proxy    │────────▶│ Issues JWTs     │
│ Stores      │         │                  │         │                 │
│ access      │         │  Forwards all    │         │ Validates JWTs  │
│ tokens      │         │  requests        │         │                 │
│             │         │                  │         │ Validates OAuth │
│             │         │                  │         │                 │
│             │         │                  │         │ Handles refresh │
│             │         │                  │         │                 │
└─────────────┘         └──────────────────┘         └─────────────────┘
                                                              │
                                                              ▼
                                                       ┌──────────────┐
                                                       │ OAuth        │
                                                       │ Provider     │
                                                       └──────────────┘
                                                              │
                                                              ▼
                                                       ┌──────────────┐
                                                       │    Clerk     │
                                                       │ (User Store) │
                                                       └──────────────┘
```

#### Component Responsibilities

#### 1. Web Client (Browser)

- Initiates OAuth authentication flow
- Stores JWT in cookies (managed by adapter)
- Sends requests with cookies
- **Does not handle token refresh** - adapter manages cookies automatically

#### 2. Mobile Client (Mobile App)

- Initiates OAuth authentication flow
- Stores JWT from OAuth response body
- Sends requests with `Authorization: Bearer` header
- Checks for `X-New-Access-Token` response header and updates stored token

#### 3. Immich Adapter (Simple Proxy with Middleware)

- **Role**: Stateless proxy that forwards requests
- **Auth Middleware**: Handles token extraction and refresh for both client types
- **Client Type Detection**: Identifies web vs mobile clients by request format
- **Token Refresh Handling**:
  - Extracts JWT from cookies (web) or Authorization header (mobile)
  - Forwards requests with JWT to backend
  - Processes token refresh responses from backend
  - Updates cookies for web clients
  - Passes refresh headers to mobile clients
- **OAuth endpoints**: Forwards to backend authentication endpoints
- **No validation**: Does not validate JWTs or OAuth tokens
- **No storage**: Does not store tokens or session data
- Maintains Immich API compatibility

#### 4. Gumnut Backend (Authentication Server)

- **Role**: All authentication logic and token management
- Validates OAuth tokens with provider
- Manages user creation/lookup in Clerk
- Issues JWTs with appropriate expiration
- Validates JWTs on every API request
- Handles token refresh logic
- Handles token revocation
- Only component that communicates with OAuth Provider and Clerk

### Authentication Flows

#### Flow 1: Initial Authentication & Token Exchange

```
User → Web Client → Adapter → Backend → OAuth Provider → Clerk
```

**Sequence:**

1. **User initiates login**
   - Web client (`photos.gumnut.ai`) calls adapter (`immich-api.gumnut.ai`): `POST /api/oauth/authorize`
   - Body: `{redirectUri: "https://photos.gumnut.ai/auth/login"}`

2. **Adapter forwards to backend**
   - Adapter calls backend (`api.gumnut.ai`): `GET /api/oauth/auth-url`
   - Params: `redirect_uri`, optional PKCE params
   - Backend creates CSRF state token
   - Backend creates nonce to prevent replay attacks
   - Backend saves state token and nonce for comparison in step 5
   - Backend builds authorization URL for OAuth provider (in our case, Clerk)
   - Backend returns authorization URL to adapter
   - Adapter returns authorization URL to web client

3. **OAuth provider authentication**
   - Web client (`photos.gumnut.ai`) redirects user to OAuth provider (in our case, Clerk) via authorization URL from previous step.
   - OAuth provider verifies provided `redirectUri` is in its list of valid URIs for the client ID.
   - OAuth provider's web site starts authorization process
     - In our present case of using Clerk, a dialog asks for email address or allows user to select authorization via an additional provider (configured by us with Clerk), such as "Continue with Google"
   - User continues through authorization flow
     - In the case of entering an email address, then user is prompted for password.
     - In the case of selecting an additional authorization provider, user is then redirected to that provider's website and shown the appropriate flow for authenticating with the provider.
       - If not already authenticated with the provider, the user will enter an email address and password.
       - After authentication, the user will be presented with the additional provider's consent dialog, along the lines of "Google will allow Clerk to access this info about you..." and approves.
       - The additional provider then redirects back to the primary OAuth provider's flow.
   - After authorization is complete, the user sees the primary OAuth provider's (in our case - Clerk) consent dialog and approves
   - OAuth provider redirects client back to `redirectUri` with authorization code

4. **Token exchange**
   - Web client (`photos.gumnut.ai`) calls the adapter (`immich-api.gumnut.ai`): `POST /api/oauth/callback`
     - Body: `{url: "callback_url_with_parameters"}`
       - `callback_url_with_parameters` is built by the OAuth provider, starting with the `redirectUri` and includes the following parameters (according to OAuth spec)
         - code
         - state
         - error (if present)
   - Adapter parses parameters from callback URL
   - Adapter calls backend (`api.gumnut.ai`): `POST /api/oauth/exchange`
     - Body: `{code: "code_from_provider", state: "state_from_provider", error: "error_if_any_from_provider", provider: "clerk"}`

5. **Backend processes OAuth token**
   - Backend (`api.gumnut.ai`) validates CSRF state generated in step 2
   - Backend calls OAuth provider to exchange code for token response
   - Backend validates ID token
   - Backend validates that nonce contained in ID token matches nonce generated in step 2
   - Backend checks Clerk for existing user (by Clerk user_id)
   - If user doesn't exist, creates user in Clerk
   - Backend generates JWT containing Clerk user_id
   - Backend returns JWT and user info to adapter (`immich-api.gumnut.ai`)

6. **Response to client**
   - Adapter (`immich-api.gumnut.ai`) sets cookies:
     - `immich_access_token=<jwt>` (httponly)
     - `immich_auth_type=oauth` (httponly)
     - `immich_is_authenticated=true`
   - Adapter returns JWT and user info in response body:

     ```json
     {
       "accessToken": "jwt_token_string",
       "userId": "user_uuid",
       "userEmail": "user@example.com",
       "name": "User Name",
       "isAdmin": false,
       "isOnboarded": true,
       "profileImagePath": "https://...",
       "shouldChangePassword": false
     }
     ```

**Key Points:**

- Backend determines JWT expiration policy
- Adapter simply forwards JWT from backend to client
- Client stores and manages JWT
- CSRF protection handled by backend

#### Flow 2: Subsequent API Requests (with Token Refresh)

```
User → Web/Mobile Client → Adapter Middleware → Backend → Gumnut API
```

**Sequence:**

1. **User makes API request**
   - **Web client**: Sends request with `immich_access_token` cookie
   - **Mobile client**: Sends request with `Authorization: Bearer {jwt}` header
   - Request goes to adapter API endpoint (e.g., `GET /api/albums`)

2. **Adapter middleware processes request**
   - Detects client type:
     - If `Authorization` header present -> mobile client
     - If `immich_access_token` cookie present -> web client
   - Extracts JWT from appropriate source
   - Adds `Authorization: Bearer {jwt}` header for backend
   - Request forwarded to endpoint handler

3. **Adapter forwards to backend**
   - Endpoint handler forwards request to backend
   - Includes JWT in `Authorization` header
   - Same method, path, and body

4. **Backend validates JWT and processes request**
   - Backend verifies JWT signature
   - Backend checks JWT expiration
   - Backend extracts user_id from JWT claims
   - Backend processes request for authenticated user
   - **If JWT is close to expiration**: Backend generates new JWT

5. **Backend response**
   - Backend returns API response
   - **If token refreshed**: Includes `X-New-Access-Token: {new_jwt}` header

6. **Adapter middleware processes response**
   - Checks for `X-New-Access-Token` header
   - If present and **web client**:
     - Updates `immich_access_token` cookie with new JWT
     - Removes `X-New-Access-Token` header from response
   - If present and **mobile client**:
     - Keeps `X-New-Access-Token` header in response
     - Mobile client will read header and update stored token

7. **Response to client**
   - Adapter forwards response to client
   - **Web client**: Unaware of token refresh (cookie updated automatically)
   - **Mobile client**: Reads `X-New-Access-Token` header and updates stored JWT

**Key Points:**

- **Middleware handles all token logic** - endpoint code unchanged
- Adapter performs **no JWT validation**
- All authorization logic in backend
- **Token refresh is transparent for web clients**
- **Token refresh is standard header pattern for mobile clients**
- Backend controls when to refresh tokens

#### Flow 3: Token Refresh (Backend Responsibility)

Token refresh is **handled entirely by the backend** as part of normal API requests (Flow 2 above). The backend determines:

- JWT expiration policy
- When tokens should be refreshed (e.g., when < 5 minutes until expiration)
- Whether to include `X-New-Access-Token` header in response

The adapter middleware automatically handles the refresh response:

- **Web clients**: Cookie updated transparently
- **Mobile clients**: Header passed through for client to update stored token

No separate refresh endpoint needed - refresh happens during normal API requests.

#### Flow 4: Token Revocation (Backend Responsibility)

Token revocation is **handled entirely by the backend**. The backend can:

- Maintain a token blacklist
- Mark users as inactive in Clerk
- Implement any revocation strategy

The adapter does not participate in revocation logic.

### Adapter Architecture

#### Middleware-Based Token Handling

The adapter uses **FastAPI middleware** to handle token extraction and refresh logic for all endpoints automatically.

**Key Components:**

1. **Auth Middleware** (`routers/middleware/auth_middleware.py`)
   - Runs on every request/response
   - Client type detection
   - JWT extraction from cookies or headers
   - Token refresh response handling
   - Cookie management for web clients

2. **Endpoint Handlers** (`routers/api/*.py`)
   - No token handling code
   - Simple forwarding to backend
   - Unchanged by auth logic

**Benefits:**

- **Centralized logic**: All token handling in one place
- **Zero endpoint changes**: Works with all existing and future endpoints
- **Easy to test**: Test middleware independently
- **Easy to maintain**: Single point of change for auth logic

**Request Flow:**

```
Request → Middleware (extract token) → Endpoint Handler (forward to backend)
→ Backend Response → Middleware (handle refresh) → Response to Client
```

The middleware intercepts all requests before they reach endpoint handlers and all responses before they reach clients, providing a clean separation of concerns.

### Security Considerations

#### CSRF Protection

- **Responsibility**: Backend
- State parameter used in OAuth flow
- Backend creates and validates state tokens
- State stored temporarily in backend
- State consumed after validation (one-time use)

#### PKCE (Proof Key for Code Exchange)

- **Optional**: For public clients (mobile apps, SPAs)
- Code challenge sent in authorization request
- Code verifier sent in token exchange
- Prevents authorization code interception attacks
- Backend validates PKCE flow

#### JWT Security

- **Responsibility**: Backend
- Backend signs JWTs with secret key
- Backend determines JWT expiration policy
- Backend validates signature and expiration on every request
- Client stores JWT (cookies or localStorage)
- Adapter never validates or inspects JWTs

#### Stateless Adapter

- **No token storage**: Adapter does not store JWTs or tokens
- **No session management**: No session state in adapter
- **Simple proxy**: Just forwards requests and responses
- **Benefits**:
  - Easy horizontal scaling
  - No state synchronization needed
  - Simplified deployment
  - Clear separation of concerns

#### OAuth Provider Validation

- **Responsibility**: Backend only
- Backend validates OAuth tokens directly with provider
- Backend verifies ID token signatures using provider's JWKS
- Adapter never validates OAuth tokens

### Conclusion

This design implements a **simple, stateless proxy** pattern for OAuth authentication. The Immich Adapter acts as a thin forwarding layer with no authentication logic, while the Gumnut Backend handles all OAuth validation, user management, JWT operations, token refresh, and token revocation.

**Key Benefits:**

- **Simplicity**: Adapter is just a proxy with minimal logic
- **Stateless**: No token storage or session management in adapter
- **Clear separation**: Backend owns all auth logic
- **Easy scaling**: Stateless adapter scales horizontally
- **Immich compatible**: Conforms to Immich API without modification
- **Flexible**: Backend can change auth implementation independently
- **Maintainable**: Simple adapter code is easy to understand and test

**Trade-offs:**

- Backend must implement full auth system (JWT generation, validation, refresh, revocation)
- Backend must determine JWT expiration policy
- Extra network hop (client -> adapter -> backend)

This design provides a **clean, simple foundation** for authentication while maintaining full compatibility with the Immich client and allowing the backend to implement sophisticated authentication logic as needed.

## Backend-side Design

### Design Constraints

#### Adapter API Requirements

- Must implement endpoints specified by the Immich Adapter design
- Must accept OAuth authorization codes and return JWTs
- Must support token refresh through standard API responses
- Must validate JWTs on every authenticated API request

#### Backend Responsibilities

- Complete OAuth token validation with Clerk
- User creation and lookup in Clerk
- JWT generation with appropriate expiration
- JWT validation on every API request
- Token refresh logic when JWT nears expiration
- CSRF protection using state parameter

### Architecture

#### System Components

```
┌─────────────────┐         ┌──────────────┐
│ Immich Adapter  │         │   Backend    │
│                 │────────▶│              │
│ Forwards OAuth  │         │ Validates    │
│ requests        │         │ OAuth tokens │
│                 │         │              │
│ Forwards API    │         │ Issues JWTs  │
│ requests with   │         │              │
│ JWT             │         │ Validates    │
│                 │         │ JWTs         │
└─────────────────┘         └──────────────┘
                                    │
                                    │ OAuth Protocol
                                    ▼
                            ┌──────────────┐
                            │    Clerk     │
                            │              │
                            │ OAuth        │
                            │ Provider     │
                            │              │
                            │ User Store   │
                            └──────────────┘
```

#### Component Responsibilities

#### Backend Server

- Generates OAuth authorization URLs with CSRF state tokens
- Exchanges authorization codes for access tokens with Clerk
- Validates ID tokens from Clerk
- Fetches user information from Clerk
- Creates or retrieves users in internal database
- Generates signed JWTs containing user_id
- Validates JWT signature and expiration on API requests
- Refreshes JWTs when close to expiration
- Provides user authentication for all API endpoints

#### Clerk

- OAuth provider for authentication
- User identity store
- Provides OAuth authorization endpoints
- Issues access tokens and ID tokens
- Provides user information via userinfo endpoint
- Validates authentication sessions

### Security Considerations

#### CSRF Protection

- State parameter used in OAuth flow
- State tokens are random, unpredictable values
- Stored in Redis with short TTL
- One-time use (deleted after validation)
- Prevents authorization code injection attacks

#### PKCE Support

- Optional for public clients
- Code challenge sent in authorization request
- Code verifier sent in token exchange
- Prevents authorization code interception
- Validates code_verifier matches code_challenge

#### JWT Security

- Signed with secret key (HS256 algorithm)
- Secret key stored in environment variables
- Signature verified on every API request
- Short expiration time (1 hour)
- Automatic refresh near expiration
- No sensitive data in JWT claims

#### Token Storage

- JWTs stored client-side only
- State tokens stored server-side in Redis
- State tokens have short TTL (5 minutes)
- No long-term token storage in backend
- Refresh tokens not needed (short-lived JWTs)

#### Clerk Integration

- All OAuth communication over HTTPS
- ID tokens validated using JWKS
- Client secret never exposed to clients
- OAuth flows follow standard specifications

### Conclusion

This design implements OAuth authentication in the Gumnut API backend using Clerk as the OAuth provider. The backend handles all authentication logic including OAuth token validation, user provisioning, JWT generation and validation, and token refresh.

The implementation reuses existing infrastructure (Clerk integration, user model, authentication dependencies) and adds minimal new code focused on OAuth-specific logic. The design maintains security through CSRF protection, PKCE support, JWT signing, and proper token expiration handling.

## 2025-10-28 | Additional Notes on Immich Authentication

https://clerk.com/blog/how-oauth-works#does-the-access-token-expire-what-happens-if-when-it-does

- Seems like we should have short-lived JWT access tokens and long-lived refresh tokens. And when access tokens expire, we automatically get a new one with the refresh token? What do we do now?

https://clerk.com/blog/how-oauth-works#common-o-auth-terminology

- Seems like Client really should be the program that the end user is interacting with. E.g. ChatGPT, Claude Web, Immich web, Immich mobile.
- From a product perspective, we want the Gumnut Dashboard, the Photos Web app, and the Photos Mobile app, to all feel like first-party products. Like, if you log into Google, you don't have to log in separately for Google Mail, Google Calendar, etc.
- We SHOULD NOT have OAuth consent for first-party products. It should look just like login, then you're in.
- We SHOULD have OAuth consent for third-party products that want to connect to Gumnut. Like ChatGPT, Claude, etc. Also Claude Code and any other dynamic clients. And anyone else that want to connect via MCP.
- The Gumnut Dashboard doesn't use OAuth. That should be fine.
- Clients identify themselves using their `client_id`, and be registered in Clerk. The server verifies their `redirect_uri` to know that the auth code is going back to a legitimate client. That ties each known client to a specific set of `redirect_uri`
- Each Client will have a different set of valid `redirect_uri`.
  - For ChatGPT, it's `https://chatgpt.com/connector_platform_oauth_redirect`
  - For Claude, it's `https://claude.ai/api/mcp/auth_callback`
  - For Immich on Gumnut, it's `https://immich-api.gumnut.ai/auth/callback`.
  - For clients that are dynamically registering, e.g. to use MCP on Gumnut, it'll be something they define. Each one of these clients has their own `client_id`, there's no shared ID for MCP clients.
  - For clients that want to use the Gumnut REST API for Immich API... these are M2M flows that should use API keys?
  - But if someone has, let's say a script that piggybacks on the Immich API. Could they theoretically go through the same OAuth flow and present to Gumnut that they're Immich? Yes, I think so. What in the OAuth spec is supposed to prevent this?
- This means to me that architecturally, the `client_id` and `client_secret` should be part of `immich-adapter`, not the Gumnut API. They have a specific `redirect_url` that is specific to `immich-adapter`.

**Web client authentication (from a security review):** the OAuth-callback session cookies (`immich_access_token`, `immich_auth_type`, `immich_is_authenticated`) are protected by the browser Same-Origin Policy only if configured correctly — each must set `httponly`, `secure`, and `samesite=Lax` (or `Strict`), plus an explicit `domain` (e.g. `photos.gumnut.ai`), `path=/`, and a `max_age` matching the JWT lifetime. See the Cookie Security Configuration list below.

Notes from Claude on mobile client authentication:

```
The Missing Protection: Universal Links / App Links

The design document doesn't adequately address mobile client authentication. Here's what it should specify:

For Mobile Apps - Use Universal Links (iOS) / App Links (Android)

Instead of custom URL schemes like myapp://oauth/callback, use:

iOS Universal Links:
Redirect URI: https://photos.gumnut.ai/oauth/mobile-callback

Requires:
- Hosting apple-app-site-association file at https://photos.gumnut.ai/.well-known/apple-app-site-association
- Apple verifies you control the domain
- Only your app (with your team ID) can handle this URL

Android App Links:
Redirect URI: https://photos.gumnut.ai/oauth/mobile-callback

Requires:
- Hosting assetlinks.json at https://photos.gumnut.ai/.well-known/assetlinks.json
- Contains SHA-256 fingerprint of your app signing certificate
- Only apps signed with your certificate can handle this URL

Why This Works

With Universal Links/App Links:
Attacker builds malicious app with stolen client_id
  -> tries to register redirect URI https://photos.gumnut.ai/oauth/mobile-callback
  ->
iOS/Android checks domain verification
  -> Attacker doesn't control photos.gumnut.ai domain
  -> Attacker can't host verification files
  -> OS REJECTS registration
  ->
Malicious app cannot receive OAuth callbacks

Additional Protections

1. Custom URL Schemes are Vulnerable

The design should explicitly warn against using schemes like:
immich://oauth/callback  -- INSECURE - any app can register
gumnut://callback        -- INSECURE - no ownership verification

These are first-come-first-served (iOS) or ambiguous (Android - multiple apps can claim).
```

### Cookie Security Configuration

A security review also flagged a CORS-misconfiguration risk: `allow_origins=["*"]` combined with `allow_credentials=True` lets a malicious origin issue credentialed `fetch()` calls and read authenticated responses. When setting cookies in the OAuth callback, the adapter must:

1. **SameSite:** `SameSite=Lax` minimum (`Strict` for highest security) — blocks cookies on cross-site requests, preventing CSRF.
2. **Secure:** `secure=True` — cookies only sent over HTTPS.
3. **Domain:** scope to a specific domain (`domain=photos.gumnut.ai`), or `.gumnut.ai` to share across subdomains.
4. **HttpOnly:** keep it (already specified in the design) — prevents XSS cookie theft.
5. **CORS:** whitelist only trusted origins; if `allow_credentials=True`, never use `allow_origins=["*"]` — for Gumnut, only `https://photos.gumnut.ai` and `https://app.gumnut.ai`.
