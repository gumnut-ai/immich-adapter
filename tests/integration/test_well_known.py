"""Integration tests for well-known endpoints."""

import pytest
from fastapi.testclient import TestClient
from main import app


@pytest.fixture
def client():
    """Create a test client."""
    with TestClient(app) as client:
        yield client


class TestWellKnownImmich:
    def test_get_well_known_immich(self, client):
        """Test GET /.well-known/immich returns correct API endpoint."""
        response = client.get("/.well-known/immich")

        assert response.status_code == 200
        data = response.json()

        assert "api" in data
        assert "endpoint" in data["api"]
        assert data["api"]["endpoint"] == "/api"
