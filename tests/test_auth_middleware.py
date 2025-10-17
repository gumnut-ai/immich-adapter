"""Tests for the AuthMiddleware class."""

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from routers.middleware.auth_middleware import AuthMiddleware


@pytest.fixture
def app():
    """Create a test FastAPI app with AuthMiddleware."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    
    @app.get("/api/test/protected")
    async def protected_endpoint(request: Request):
        """Test endpoint that returns JWT token from request state."""
        return {
            "jwt_token": getattr(request.state, "jwt_token", None),
            "is_web_client": getattr(request.state, "is_web_client", None),
        }
    
    @app.get("/api/test/refresh")
    async def refresh_endpoint(request: Request):
        """Test endpoint that simulates token refresh."""
        response = Response(content='{"status": "ok"}', media_type="application/json")
        response.headers["x-new-access-token"] = "new-jwt-token-123"
        return response
    
    @app.get("/api/oauth/login")
    async def unauthenticated_endpoint():
        """Test endpoint that should bypass auth."""
        return {"message": "login page"}
    
    return app


@pytest.fixture
def client(app):
    """Create a test client."""
    return TestClient(app)


class TestAuthMiddleware:
    """Test cases for AuthMiddleware."""
    
    def test_mobile_client_with_bearer_token(self, client):
        """Test that mobile client with Authorization header is handled correctly."""
        headers = {"Authorization": "Bearer mobile-jwt-token-123"}
        
        response = client.get("/api/test/protected", headers=headers)
        
        assert response.status_code == 200
        data = response.json()
        assert data["jwt_token"] == "mobile-jwt-token-123"
        assert data["is_web_client"] is False
    
    def test_web_client_with_cookie(self, client):
        """Test that web client with cookie is handled correctly."""
        client.cookies = {"immich_access_token": "web-jwt-token-456"}
        
        response = client.get("/api/test/protected")
        
        assert response.status_code == 200
        data = response.json()
        assert data["jwt_token"] == "web-jwt-token-456"
        assert data["is_web_client"] is True
    
    def test_no_authentication(self, client):
        """Test request with no authentication."""
        response = client.get("/api/test/protected")
        
        assert response.status_code == 200
        data = response.json()
        assert data["jwt_token"] is None
        assert data["is_web_client"] is False  # Changed from None to False
    
    def test_malformed_authorization_header(self, client):
        """Test that malformed Authorization header is ignored."""
        headers = {"Authorization": "NotBearer malformed-token"}
        
        response = client.get("/api/test/protected", headers=headers)
        
        assert response.status_code == 200
        data = response.json()
        assert data["jwt_token"] is None
        assert data["is_web_client"] is False  # Changed from None to False
    
    def test_bearer_takes_precedence_over_cookie(self, client):
        """Test that Authorization header takes precedence over cookie."""
        headers = {"Authorization": "Bearer header-token-123"}
        client.cookies = {"immich_access_token": "cookie-token-456"}
        
        response = client.get("/api/test/protected", headers=headers)
        
        assert response.status_code == 200
        data = response.json()
        assert data["jwt_token"] == "header-token-123"
        assert data["is_web_client"] is False
    
    def test_unauthenticated_paths_bypass_auth(self, client):
        """Test that unauthenticated paths bypass authentication middleware."""
        response = client.get("/api/oauth/login")
        
        assert response.status_code == 200
        assert response.json() == {"message": "login page"}
    
    def test_token_refresh_for_web_client(self, client):
        """Test token refresh handling for web client."""
        client.cookies = {"immich_access_token": "old-web-token"}
        
        response = client.get("/api/test/refresh")
        
        assert response.status_code == 200
        # Check that cookie was updated
        assert "immich_access_token" in response.cookies
        assert response.cookies["immich_access_token"] == "new-jwt-token-123"
        # Check that refresh header was removed
        assert "x-new-access-token" not in response.headers
    
    def test_token_refresh_for_mobile_client(self, client):
        """Test token refresh handling for mobile client."""
        headers = {"Authorization": "Bearer old-mobile-token"}
        
        response = client.get("/api/test/refresh", headers=headers)
        
        assert response.status_code == 200
        # Check that refresh header is preserved for mobile client
        assert response.headers["x-new-access-token"] == "new-jwt-token-123"
        # Check that no cookie was set
        assert "immich_access_token" not in response.cookies
    
    def test_no_token_refresh_when_no_auth(self, client):
        """Test that token refresh header is handled even without initial auth."""
        response = client.get("/api/test/refresh")
        
        assert response.status_code == 200
        # Should preserve header since client type cannot be determined
        assert response.headers["x-new-access-token"] == "new-jwt-token-123"
    
    def test_cookie_properties(self, client):
        """Test that cookie is set with correct properties."""
        client.cookies = {"immich_access_token": "old-token"}
        
        response = client.get("/api/test/refresh")
        
        # Verify cookie properties (TestClient doesn't parse all cookie attributes)
        set_cookie_header = response.headers.get("set-cookie")
        assert "immich_access_token=new-jwt-token-123" in set_cookie_header
        assert "HttpOnly" in set_cookie_header
