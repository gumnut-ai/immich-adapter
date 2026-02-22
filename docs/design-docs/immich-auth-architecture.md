---
title: "Immich Authentication Architecture"
status: deprecated
superseded-by: auth-design.md
created: 2025-10-21
last-updated: 2025-10-21
---

# Immich Authentication Architecture

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
   - Adapter calls backend (`photos-api.gumnut.ai`): `GET /api/oauth/auth-url`
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
   - Adapter calls backend (`photos-api.gumnut.ai`): `POST /api/oauth/exchange`
     - Body: `{code: "code_from_provider", state: "state_from_provider", error: "error_if_any_from_provider", provider: "clerk"}`

5. **Backend processes OAuth token**
   - Backend (`photos-api.gumnut.ai`) validates CSRF state generated in step 2
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

### API Endpoints

#### Immich Adapter Endpoints

The adapter implements Immich API endpoints as simple proxies to the backend. **All token handling is performed by middleware**, not by individual endpoints.

#### `POST /api/oauth/authorize`

**Purpose**: Start OAuth authentication flow

**Request:**

```json
{
  "redirectUri": "http://localhost:3000/auth/callback",
  "codeChallenge": "PKCE_challenge_string",
  "codeChallengeMethod": "S256"
}
```

**Backend Call:**

```
GET /api/oauth/auth-url?redirect_uri={redirectUri}&code_challenge={codeChallenge}...
```

**Response:**

```json
{
  "url": "https://provider.com/oauth/authorize?client_id=...&state=...&redirect_uri=..."
}
```

**Implementation:**

- Extract redirectUri and optional PKCE params
- Forward to backend `/api/oauth/auth-url` endpoint
- Return authorization URL from backend

#### `POST /api/oauth/callback`

**Purpose**: Complete OAuth flow and get JWT

**Request:**

```json
{
  "url": "http://localhost:3000/auth/callback?code=xxx&state=yyy",
  "codeVerifier": "PKCE_verifier_string"
}
```

**Backend Call:**

```
POST /api/oauth/exchange
Body: {
  "oauth_token": "authorization_code",
  "provider": "google",
  "code_verifier": "pkce_verifier"
}
```

**Response:**

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

**Implementation:**

- Parse callback URL for code and state
- Forward to backend `/api/oauth/exchange`
- Receive JWT from backend
- Set authentication cookies with JWT
- Return JWT and user info to client

#### All Other API Endpoints

**Purpose**: Forward authenticated requests to backend

**Examples:**

- `GET /api/albums`
- `GET /api/assets`
- `POST /api/albums`
- etc. (any Immich API endpoint)

**Request (from client):**

- **Web client**: Request with `immich_access_token` cookie
- **Mobile client**: Request with `Authorization: Bearer {jwt}` header

**Middleware Processing:**

- Detects client type
- Extracts JWT from cookie or header
- Adds `Authorization: Bearer {jwt}` header for backend

**Endpoint Handler:**

- Forwards request to backend with JWT
- Returns backend response

**Middleware Response Processing:**

- Checks for `X-New-Access-Token` header from backend
- Updates cookie (web) or passes header (mobile)
- Forwards response to client

**No special endpoint code needed** - middleware handles all token logic.

#### OAuth Support Endpoints

The following endpoints are part of Immich's OAuth API:

**`POST /api/oauth/link`** - Link OAuth account to existing user

- Status: Forward to backend (if backend implements)
- Backend handles OAuth linking logic

**`POST /api/oauth/unlink`** - Unlink OAuth account from user

- Status: Forward to backend (if backend implements)
- Backend handles OAuth unlinking logic

**`GET /api/oauth/mobile-redirect`** - Handle OAuth redirects for mobile apps

- Status: Forward to backend (if backend implements)
- Mobile app redirect handling in backend

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

### Testing Strategy

#### Unit Tests

**Adapter Tests:**

```python
# Test OAuth authorization flow
async def test_oauth_authorize_forwards_to_backend()
async def test_oauth_authorize_returns_backend_url()
async def test_oauth_authorize_with_pkce_params()

# Test OAuth callback handling
async def test_oauth_callback_forwards_to_backend()
async def test_oauth_callback_sets_cookies()
async def test_oauth_callback_returns_jwt()
async def test_oauth_callback_handles_backend_errors()

# Test middleware - client type detection
async def test_middleware_detects_web_client_from_cookie()
async def test_middleware_detects_mobile_client_from_auth_header()
async def test_middleware_returns_401_if_no_auth()

# Test middleware - JWT extraction
async def test_middleware_extracts_jwt_from_cookie()
async def test_middleware_extracts_jwt_from_auth_header()
async def test_middleware_adds_auth_header_for_backend()

# Test middleware - token refresh for web clients
async def test_middleware_updates_cookie_on_token_refresh()
async def test_middleware_removes_refresh_header_for_web_client()
async def test_middleware_does_not_update_cookie_without_refresh()

# Test middleware - token refresh for mobile clients
async def test_middleware_passes_refresh_header_to_mobile_client()
async def test_middleware_does_not_modify_response_without_refresh()

# Test API forwarding
async def test_api_request_forwards_jwt_to_backend()
async def test_api_request_forwards_response_from_backend()
async def test_api_request_handles_401_from_backend()
```

#### Integration Tests

**End-to-End OAuth Flow:**

1. Start OAuth authorization
2. Simulate OAuth provider callback
3. Verify adapter forwards to backend
4. Verify JWT returned to client
5. Verify cookies set for web client
6. Make authenticated API request
7. Verify middleware extracts and forwards JWT
8. Verify response returned to client

**Token Refresh Flow - Web Client:**

1. Login and receive JWT
2. Make API request with cookie
3. Backend returns response with `X-New-Access-Token`
4. Verify middleware updates cookie
5. Verify refresh header removed from response
6. Make another request
7. Verify updated JWT used

**Token Refresh Flow - Mobile Client:**

1. Login and receive JWT
2. Make API request with Authorization header
3. Backend returns response with `X-New-Access-Token`
4. Verify middleware passes header to client
5. Verify client can update stored JWT
6. Make another request with new JWT

**Backend Integration:**

- Test adapter -> backend communication
- Test error handling from backend
- Test timeout handling
- Mock backend for testing

#### Future Enhancements

- OAuth account linking (`/api/oauth/link`)
- OAuth account unlinking (`/api/oauth/unlink`)
- Mobile redirect handling

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

### Authentication Flows

#### Flow 1: OAuth Authorization URL Generation

**Endpoint**: `GET /api/oauth/auth-url`

**Query Parameters**:

- `redirect_uri`: URL to redirect after OAuth consent
- `code_challenge`: PKCE code challenge (optional)
- `code_challenge_method`: PKCE method, typically S256 (optional)

**Process**:

1. Generate random CSRF state token
2. Store state token temporarily with expiration (5 minutes)
3. Build Clerk OAuth authorization URL with:
   - client_id from settings
   - redirect_uri from request
   - state token
   - scope (openid, email, profile)
   - PKCE parameters if provided
4. Return authorization URL

**Response**:

```json
{
  "url": "https://clerk.example.com/oauth/authorize?client_id=...&state=...&redirect_uri=..."
}
```

**State Storage**:

- Use Redis with TTL for state token storage
- Key: `oauth_state:{token}`
- Value: JSON with redirect_uri, created timestamp
- TTL: 5 minutes

#### Flow 2: OAuth Token Exchange

**Endpoint**: `POST /api/oauth/exchange`

**Request Body**:

```json
{
  "code": "authorization_code",
  "state": "state_token",
  "error": "error_code",
  "provider": "clerk",
  "code_verifier": "pkce_verifier_string"
}
```

**Process**:

1. Check for OAuth error parameter
2. Validate authorization code is present
3. Validate state token is present
4. Validate state token exists in Redis
5. Remove state token from Redis (one-time use)
6. Exchange authorization code for access token with Clerk:
   - POST to Clerk token endpoint
   - Include client_id, client_secret, code, redirect_uri
   - Include code_verifier if PKCE was used
7. Validate ID token signature using Clerk JWKS
8. Extract user info from ID token claims
9. Get or create user in internal database:
   - Query by clerk_user_id from ID token sub claim
   - If user exists, return existing user
   - If user doesn't exist, create with OAuth info
10. Generate JWT with claims:
    - sub: internal user_id
    - clerk_user_id: Clerk user ID
    - exp: current time + JWT expiration (configurable)
    - iat: current time
11. Sign JWT with secret key
12. Return JWT and user info

**Response**:

```json
{
  "access_token": "jwt_string",
  "user": {
    "id": "intuser_...",
    "email": "user@example.com",
    "first_name": "John",
    "last_name": "Doe",
    "clerk_user_id": "user_clerk123",
    "is_active": true,
    "is_verified": true
  }
}
```

**Error Handling**:

- Invalid state: 401 Unauthorized
- Expired state: 401 Unauthorized
- OAuth exchange failure: 502 Bad Gateway
- ID token validation failure: 401 Unauthorized
- User creation failure: 500 Internal Server Error

#### Flow 3: Authenticated API Requests

**All API Endpoints**: `GET/POST/PATCH/DELETE /api/*`

**Request**:

- Authorization header: `Bearer {jwt}`

**Process**:

1. Extract JWT from Authorization header
2. Verify JWT signature using secret key
3. Check JWT expiration
4. Extract user_id from JWT sub claim
5. Process API request with authenticated user_id
6. Generate new JWT with same claims but updated exp/iat
7. Add `X-New-Access-Token` header to response
8. Return API response with refreshed JWT

**JWT Validation**:

- Verify signature matches secret key
- Verify exp claim is in the future
- Verify JWT structure is valid
- Extract sub claim as user_id

**Token Refresh Logic**:

- Always generate new JWT on every authenticated request
- New JWT has extended expiration (7 days from current time)
- Include in `X-New-Access-Token` response header
- Client should use new token for subsequent requests

**Response**:

- Standard API response body
- Header: `X-New-Access-Token: {new_jwt}` (always present for JWT-authenticated requests)

**Error Handling**:

- Missing Authorization header: 401 Unauthorized
- Invalid JWT signature: 401 Unauthorized
- Expired JWT: 401 Unauthorized
- Malformed JWT: 401 Unauthorized

### API Endpoints

#### Authentication Endpoints

#### `GET /api/oauth/auth-url`

**Purpose**: Generate OAuth authorization URL

**Query Parameters**:

- `redirect_uri` (required): OAuth callback URL
- `code_challenge` (optional): PKCE code challenge
- `code_challenge_method` (optional): PKCE method (S256)

**Response**: 200 OK

```json
{
  "url": "https://clerk.example.com/oauth/authorize?..."
}
```

**Implementation**:

- Generate CSRF state token (random 32-byte string)
- Store state in Redis with 5 minute TTL
- Build Clerk authorization URL
- Return URL to adapter

#### `POST /api/oauth/exchange`

**Purpose**: Exchange OAuth code for JWT

**Request Body**:

```json
{
  "code": "auth_code",
  "state": "state_token",
  "error": "error_code",
  "provider": "clerk",
  "code_verifier": "pkce_verifier"
}
```

**Parameters**:

- `code` (optional): Authorization code from OAuth provider
- `state` (optional): State token for CSRF protection
- `error` (optional): Error code if OAuth failed
- `provider` (required): OAuth provider name (e.g., "clerk")
- `code_verifier` (optional): PKCE code verifier

**Response**: 200 OK

```json
{
  "access_token": "jwt_string",
  "user": {
    "id": "intuser_...",
    "email": "user@example.com",
    "first_name": "John",
    "last_name": "Doe",
    "clerk_user_id": "user_clerk123",
    "is_active": true,
    "is_verified": true
  }
}
```

**Implementation**:

- Check for OAuth errors from provider
- Validate authorization code is present
- Validate state token is present
- Validate and consume state token (one-time use)
- Exchange code with Clerk token endpoint
- Validate ID token from Clerk
- Get or create user in database
- Generate and sign JWT
- Return JWT and user data

#### Protected API Endpoints

All existing API endpoints (`/api/*`) automatically support JWT authentication through the existing `authenticated_request` dependency.

**Modifications to `authenticated_request`**:

1. Check for JWT in Authorization header (Bearer token)
2. If JWT present and valid:
   - Verify signature and expiration
   - Extract user_id from claims
   - Store JWT claims and service in request state for middleware
3. If JWT not present, fall back to existing Clerk session validation
4. If neither valid, return 401 Unauthorized

**No changes required to individual endpoint handlers** - authentication logic is centralized in the dependency.

### Implementation Details

#### JWT Structure

**Claims**:

```json
{
  "sub": "intuser_abc123",
  "clerk_user_id": "user_clerk456",
  "iat": 1234567890,
  "exp": 1234571490
}
```

**Fields**:

- `sub`: Internal user ID from database
- `clerk_user_id`: Clerk user identifier
- `iat`: Issued at timestamp
- `exp`: Expiration timestamp

**Expiration Policy**:

- Default: 7 days (604800 seconds)
- Configurable via JWT_EXPIRATION_SECONDS environment variable
- New JWT generated on every authenticated request

**Signing**:

- Algorithm: HS256
- Secret key from environment variable
- Verify signature on every request

#### State Token Management

**Storage**: Redis

**Key Format**: `oauth_state:{token}`

**Value Structure**:

```json
{
  "redirect_uri": "http://...",
  "created_at": 1234567890
}
```

**TTL**: 5 minutes (300 seconds)

**Generation**: 32 random bytes, base64url encoded

**Validation**:

- Check token exists in Redis
- Check not expired (handled by TTL)
- Delete after successful validation (one-time use)

#### User Provisioning

**User Lookup**:

1. Extract user info from Clerk ID token
2. Query database for user with matching clerk_user_id
3. If found, return existing user
4. If not found, create new user

**User Creation**:

```python
User(
    clerk_user_id=id_token["sub"],
    email=id_token.get("email"),
    first_name=id_token.get("given_name"),
    last_name=id_token.get("family_name"),
    is_active=True,
    is_verified=True
)
```

**Race Condition Handling**:

- Use existing `get_or_create_user_from_clerk` function
- Handles concurrent user creation attempts
- Uses database unique constraint and exception handling

#### Clerk Integration

**OAuth Configuration**:

- Client ID: from environment variable
- Client Secret: from environment variable
- Authorization endpoint: Clerk OAuth authorize URL
- Token endpoint: Clerk OAuth token URL
- JWKS endpoint: Clerk JWKS URL for ID token validation

**Required Scopes**:

- `openid`: Basic OAuth functionality
- `email`: User email address
- `profile`: User name and profile info

**ID Token Validation**:

1. Fetch Clerk JWKS from public endpoint
2. Verify ID token signature using JWKS
3. Verify issuer matches Clerk
4. Verify audience matches client_id
5. Verify expiration is in future
6. Extract user claims

**User Info Extraction**:

- Email: from `email` claim
- First name: from `given_name` claim
- Last name: from `family_name` claim
- Clerk user ID: from `sub` claim

### Configuration

#### Environment Variables

**New Variables**:

- `JWT_SECRET_KEY`: Secret key for signing JWTs (required)
- `JWT_EXPIRATION_SECONDS`: JWT lifetime in seconds (default: 604800 = 7 days)
- `JWT_REFRESH_THRESHOLD_SECONDS`: Refresh when this many seconds remain (default: 300)
- `OAUTH_STATE_TTL_SECONDS`: State token TTL (default: 300)
- `CLERK_OAUTH_DISCOVERY_URL`: Clerk OIDC discovery endpoint (required)
- `CLERK_OAUTH_CLIENT_ID`: Clerk OAuth client identifier (required)
- `CLERK_OAUTH_CLIENT_SECRET`: Clerk OAuth client secret (required)

**Existing Variables** (reused):

- `CLERK_SECRET_KEY`: Clerk API secret key
- `CLERK_PUBLISHABLE_KEY`: Clerk publishable key
- `REDIS_URL`: Redis connection URL for state storage

#### Settings Updates

Add to `Settings` class in `config/settings.py`:

```python
jwt_secret_key: str | None = None
jwt_expiration_seconds: int = 604800
jwt_refresh_threshold_seconds: int = 300
oauth_state_ttl_seconds: int = 300
clerk_oauth_discovery_url: str | None = None
clerk_oauth_client_id: str | None = None
clerk_oauth_client_secret: str | None = None
```

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

### Testing Strategy

#### Unit Tests

**OAuth URL Generation** (`test_auth_url_generation.py`):

```python
async def test_generate_auth_url_creates_state_token()
async def test_generate_auth_url_stores_state_in_redis()
async def test_generate_auth_url_includes_pkce_parameters()
async def test_generate_auth_url_returns_clerk_authorization_url()
```

**Token Exchange** (`test_token_exchange.py`):

```python
async def test_exchange_validates_state_token()
async def test_exchange_calls_clerk_token_endpoint()
async def test_exchange_validates_id_token_signature()
async def test_exchange_creates_user_if_not_exists()
async def test_exchange_returns_existing_user()
async def test_exchange_generates_valid_jwt()
async def test_exchange_returns_user_info()
async def test_exchange_rejects_invalid_state()
async def test_exchange_rejects_expired_state()
async def test_exchange_validates_pkce_verifier()
```

**JWT Validation** (`test_jwt_validation.py`):

```python
async def test_validate_jwt_verifies_signature()
async def test_validate_jwt_checks_expiration()
async def test_validate_jwt_extracts_user_id()
async def test_validate_jwt_rejects_expired_token()
async def test_validate_jwt_rejects_invalid_signature()
async def test_validate_jwt_rejects_malformed_token()
```

**Token Refresh** (`test_token_refresh.py`):

```python
async def test_refresh_header_always_added_to_jwt_response()
async def test_refreshed_token_has_extended_expiration()
async def test_refreshed_token_preserves_claims()
async def test_no_refresh_for_api_key_auth()
async def test_no_refresh_for_clerk_session_auth()
```

**State Management** (`test_state_management.py`):

```python
async def test_state_token_stored_in_redis()
async def test_state_token_expires_after_ttl()
async def test_state_token_deleted_after_use()
async def test_concurrent_state_validation_only_succeeds_once()
```

#### Integration Tests

**End-to-End OAuth Flow**:

1. Request OAuth authorization URL
2. Verify state token created in Redis
3. Simulate OAuth provider callback
4. Exchange authorization code for JWT
5. Verify state token deleted from Redis
6. Verify JWT returned with valid signature
7. Verify user created in database
8. Make authenticated API request with JWT
9. Verify request succeeds with user context

**Token Refresh Flow**:

1. Create JWT close to expiration
2. Make authenticated API request
3. Verify response includes new JWT in header
4. Verify new JWT has extended expiration
5. Make another request with new JWT
6. Verify request succeeds

**Clerk Integration**:

- Mock Clerk OAuth endpoints
- Test token exchange with mock responses
- Test ID token validation with test keys
- Test error handling for Clerk failures

### Conclusion

This design implements OAuth authentication in the Gumnut Photos API backend using Clerk as the OAuth provider. The backend handles all authentication logic including OAuth token validation, user provisioning, JWT generation and validation, and token refresh.

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
- This means to me that architecturally, the `client_id` and `client_secret` should be part of `immich-adapter`, not `photos-api`. They have a specific `redirect_url` that is specific to `immich-adapter`.

Notes from Claude on web client authentication:

```
Attack Vector 2: Can evil.com steal cookies after OAuth completes?

NO - Protected by browser Same-Origin Policy, but only if cookies are configured correctly.

The design document says (line 213-216):
immich_access_token=<jwt>  (httponly)
immich_auth_type=oauth     (httponly)
immich_is_authenticated=true

But it DOESN'T specify critical cookie attributes. Here's the problem:

If cookies are configured incorrectly:

# BAD - Missing security attributes
response.set_cookie(
    key="immich_access_token",
    value=jwt,
    httponly=True  # Good - prevents JS access
    # Missing: secure=True
    # Missing: samesite="Lax" or "Strict"
    # Missing: domain specification
)

Correct configuration:

# GOOD - All security attributes
response.set_cookie(
    key="immich_access_token",
    value=jwt,
    httponly=True,      # Prevents JavaScript access
    secure=True,        # HTTPS only
    samesite="Lax",     # Prevents CSRF attacks
    domain="photos.gumnut.ai",  # Scope to specific domain
    path="/",
    max_age=604800      # 7 days
)
```

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

```
Attack Vector 4: CORS Misconfiguration

This is a POTENTIAL vulnerability - I don't see CORS middleware in your current main.py.

The Attack (if CORS is misconfigured):

# BAD - Overly permissive CORS
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # Allows ANY origin
    allow_credentials=True,        # Sends cookies!
    allow_methods=["*"],
    allow_headers=["*"],
)

// evil.com runs this JavaScript
fetch('https://photos.gumnut.ai/api/albums', {
  credentials: 'include'  // Include victim's cookies
})
.then(r => r.json())
.then(data => {
  // Steal victim's photo albums
  exfiltrate(data);
});

The Protection:

# GOOD - Restrictive CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://photos.gumnut.ai",
        "https://app.gumnut.ai"
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

What the Design Document is Missing

The design should include a "Cookie Security" section:

### Cookie Security Configuration

When setting cookies in the OAuth callback (line 213-216), the adapter MUST:

1. **Set SameSite attribute:**
   - Use `SameSite=Lax` minimum
   - Prevents CSRF by blocking cookies on cross-site requests
   - Use `SameSite=Strict` for highest security

2. **Set Secure attribute:**
   - Use `secure=True`
   - Ensures cookies only sent over HTTPS
   - Prevents man-in-the-middle attacks

3. **Set Domain attribute:**
   - Scope to specific domain: `domain=photos.gumnut.ai`
   - Prevents subdomain cookie theft
   - Or use `.gumnut.ai` if sharing across subdomains

4. **Keep HttpOnly:**
   - Already specified in design
   - Prevents XSS cookie theft

5. **CORS Configuration:**
   - Whitelist only trusted origins
   - If `allow_credentials=True`, NEVER use `allow_origins=["*"]`
   - For Gumnut: Only allow `https://photos.gumnut.ai`, `https://app.gumnut.ai`
```
