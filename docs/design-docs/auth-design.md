---
title: "Updated Authentication Design"
status: completed
created: 2025-12-06
last-updated: 2025-12-06
---

# Updated Authentication Design Document

Date: 2025-12-05

## Overview

This document describes the OAuth/OpenID Connect authentication architecture for the Immich Adapter system. The adapter manages **session tokens** for Immich clients while delegating OAuth validation and JWT management to the Gumnut backend.

## Current Implementation

The adapter passes the Gumnut JWT directly to clients, and session IDs are derived by hashing that JWT. When clients authenticate, they receive and store the raw JWT, sending it on every request. The middleware extracts the JWT and forwards it to the backend. When the backend refreshes the JWT, clients receive the new token and begin using it. However, because the session ID is a hash of the JWT, a refreshed token produces a different hash -- effectively creating a new session and orphaning the old one along with any associated sync checkpoints.

## Updated Implementation

The adapter will generate a stable UUID session token at login and return that to clients instead of the raw JWT. The Gumnut JWT is encrypted and stored in Redis, keyed by the session token. On each request, the middleware extracts the session token, looks up the stored JWT, and forwards it to the backend. When the backend refreshes the JWT, the adapter simply updates the stored value in Redis -- the client's session token never changes. This keeps sessions and checkpoints stable across JWT refresh cycles, enables immediate session revocation, and ensures raw JWTs are never exposed to clients.

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

### Component Responsibilities

### 1. Web Client (Browser)

- Initiates OAuth authentication flow
- Stores session token in cookies (managed by adapter)
- Sends requests with cookies
- **Does not handle token refresh** - adapter manages cookies automatically

### 2. Mobile Client (Mobile App)

- Initiates OAuth authentication flow
- Stores session token from OAuth response body
- Sends requests with `Authorization: Bearer` header
- Checks for `X-New-Access-Token` response header and updates stored token

### 3. Immich Adapter (Session Manager with Middleware)

- **Role**: Session management and request forwarding
- **Session Generation**: Creates UUID session tokens at login
- **JWT Storage**: Stores encrypted Gumnut JWTs in Redis, keyed by session token
- **Auth Middleware**: Handles session token extraction and JWT lookup for both client types
- **Client Type Detection**: Identifies web vs mobile clients by request format
- **Token Refresh Handling**:
  - Extracts session token from cookies (web) or Authorization header (mobile)
  - Looks up stored JWT from Redis
  - Forwards requests with JWT to backend
  - If backend returns new JWT, updates stored JWT in Redis
  - Session token remains stable across JWT refreshes
- **OAuth endpoints**: Forwards to backend authentication endpoints
- **No JWT validation**: Does not validate JWTs (backend responsibility)
- Maintains Immich API compatibility

### 4. Gumnut Backend (Authentication Server)

- **Role**: All authentication logic and JWT management
- Validates OAuth tokens with provider
- Manages user creation/lookup in Clerk
- Issues JWTs with appropriate expiration
- Validates JWTs on every API request
- Handles token refresh logic
- Handles token revocation
- Only component that communicates with OAuth Provider and Clerk

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

See the Redis data model documentation.

## Authentication Flows

### Flow 1: Initial Authentication & Token Exchange

```
User → Web Client → Adapter → Backend → OAuth Provider → Clerk
```

**Sequence:**

1. **User initiates login**
   - Web client calls adapter: `POST /api/oauth/authorize`
   - Body: `{redirectUri: "http://localhost:3000/auth/callback"}`

2. **Adapter forwards to backend**
   - Adapter calls backend: `GET /auth/auth-url`
   - Params: `redirect_uri`, optional PKCE params
   - Backend creates CSRF state token
   - Backend builds authorization URL for OAuth provider
   - Backend returns authorization URL to adapter
   - Adapter returns URL to web client

3. **OAuth provider authentication**
   - Web client redirects user to OAuth provider
   - User sees consent dialog and approves
   - OAuth provider redirects back with authorization code

4. **Token exchange**
   - Web client calls adapter: `POST /api/oauth/callback`
   - Body: `{url: "callback_url_with_code_and_state"}`
   - Adapter parses code, state, and error from callback URL
   - Adapter forwards to backend: `POST /auth/exchange`
   - Body: `{code: "code_from_provider", state: "state_from_provider", error: "error_if_any_from_provider"}`

5. **Backend processes OAuth token**
   - Backend validates CSRF state
   - Backend exchanges code for access token with OAuth provider
   - Backend validates ID token (if present)
   - Backend fetches user info from userinfo endpoint
   - Backend checks Clerk for existing user (by email)
   - If user doesn't exist, creates user in Clerk
   - Backend generates JWT containing Clerk user_id
   - Backend returns JWT + user info to adapter

6. **Adapter creates session and responds to client**
   - Adapter generates UUID session token
   - Adapter encrypts and stores Gumnut JWT in Redis, keyed by session token
   - Adapter sets cookies:
     - `immich_access_token=<session_token>` (httponly)
     - `immich_auth_type=oauth` (httponly)
     - `immich_is_authenticated=true`
   - Adapter returns session token and user info in response body:

     ```json
     {
       "accessToken": "session_token_uuid",
       "userId": "user_uuid",
       "userEmail": "user@example.com",
       "name": "User Name",
       "isAdmin": false,
       "isOnboarded": true,
       "profileImagePath": "https://...",
       "shouldChangePassword": false
     }
     ```

   - Client stores session token

**Key Points:**

- Backend determines JWT expiration policy
- Adapter generates session token and stores encrypted JWT
- Client stores and manages session token (not the raw JWT)
- CSRF protection handled by backend

### Flow 2: Subsequent API Requests (with Token Refresh)

```
User → Web/Mobile Client → Adapter Middleware → Backend → Gumnut API
```

**Sequence:**

1. **User makes API request**
   - **Web client**: Sends request with `immich_access_token` cookie (contains session token)
   - **Mobile client**: Sends request with `Authorization: Bearer {session_token}` header
   - Request goes to adapter API endpoint (e.g., `GET /api/albums`)

2. **Adapter middleware processes request**
   - Detects client type:
     - If `Authorization` header present -> mobile client
     - If `immich_access_token` cookie present -> web client
   - Extracts session token from appropriate source
   - Looks up session in Redis by session token
   - Retrieves and decrypts stored Gumnut JWT
   - Adds `Authorization: Bearer {jwt}` header for backend
   - Request forwarded to endpoint handler

3. **Adapter forwards to backend**
   - Endpoint handler forwards request to backend
   - Includes Gumnut JWT in `Authorization` header
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
   - If present:
     - Updates stored JWT in Redis (encrypted)
     - **Session token remains unchanged**
     - Removes `X-New-Access-Token` header from response (client doesn't need it)

7. **Response to client**
   - Adapter forwards response to client
   - **Web client**: Unaware of JWT refresh (cookie unchanged, same session token)
   - **Mobile client**: Unaware of JWT refresh (same session token)

**Key Points:**

- **Middleware handles all token logic** - endpoint code unchanged
- Adapter performs **no JWT validation** (only session lookup)
- All authorization logic in backend
- **Token refresh is transparent to all clients** - session token is stable
- Backend controls when to refresh tokens

### Flow 3: Token Refresh (Backend Responsibility)

Token refresh is **handled entirely by the backend** as part of normal API requests (Flow 2 above). The backend determines:

- JWT expiration policy
- When tokens should be refreshed (e.g., when < 5 minutes until expiration)
- Whether to include `X-New-Access-Token` header in response

The adapter middleware automatically handles the refresh response:

- Updates stored JWT in Redis
- Session token remains stable
- Client is unaware of refresh

No separate refresh endpoint needed - refresh happens during normal API requests.

### Flow 4: Logout (Session Deletion)

```
User → Client → Adapter
```

**Sequence:**

1. **User initiates logout**
   - Client calls adapter: `POST /api/auth/logout`

2. **Adapter deletes session**
   - Extracts session token from cookie or header
   - Deletes session from Redis (including stored JWT)
   - Clears authentication cookies
   - Returns success response

3. **Session cleanup**
   - Session deletion also clears any associated checkpoints
   - User session index is updated
   - Next request with this session token will fail authentication

**Key Points:**

- Session deletion is immediate
- Stored JWT is deleted from Redis
- Client must re-authenticate to get a new session

## Adapter Architecture

### Middleware-Based Token Handling

The adapter uses **FastAPI middleware** to handle session lookup and JWT management for all endpoints automatically.

**Key Components:**

1. **Auth Middleware** (`routers/middleware/auth_middleware.py`)
   - Runs on every request/response
   - Client type detection
   - Session token extraction from cookies or headers
   - Session lookup in Redis
   - JWT retrieval and decryption
   - Token refresh response handling (update stored JWT)

2. **Session Store** (`services/session_store.py`)
   - Redis-based session storage
   - JWT encryption/decryption
   - Session CRUD operations
   - User session index management

3. **Endpoint Handlers** (`routers/api/*.py`)
   - No token handling code
   - Simple forwarding to backend
   - Unchanged by auth logic

**Benefits:**

- **Centralized logic**: All token handling in one place
- **Zero endpoint changes**: Works with all existing and future endpoints
- **Easy to test**: Test middleware independently
- **Easy to maintain**: Single point of change for auth logic
- **Checkpoint stability**: Session IDs are stable across JWT refreshes

**Request Flow:**

```
Request → Middleware (extract session token, lookup JWT)
  → Endpoint Handler (forward to backend) → Backend Response
  → Middleware (handle JWT refresh, update stored JWT) → Response to Client
```

The middleware intercepts all requests before they reach endpoint handlers and all responses before they reach clients, providing a clean separation of concerns.

## Conclusion

This design implements a **session token architecture** for OAuth authentication. The Immich Adapter manages session tokens and stores encrypted Gumnut JWTs, while the Gumnut Backend handles all OAuth validation, user management, and JWT operations.

**Key Benefits:**

- **Stable sessions**: Session tokens survive JWT refreshes
- **Checkpoint support**: Sync checkpoints tied to stable session IDs
- **Immediate revocation**: Delete session = revoke access instantly
- **Security**: Raw JWTs never exposed to clients
- **Immich compatible**: Conforms to Immich API without modification
- **Transparent refresh**: Clients unaware of JWT refresh cycles
- **Easy scaling**: Redis enables horizontal scaling

**Trade-offs:**

- Backend must implement full auth system (JWT generation, validation, refresh, revocation)
- Backend must determine JWT expiration policy
- Adapter requires Redis for session storage
- Extra Redis lookup per request (~1-2ms)

This design provides a **robust foundation** for authentication while maintaining full compatibility with the Immich client, supporting checkpoints for sync, and allowing the backend to implement sophisticated authentication logic as needed.
