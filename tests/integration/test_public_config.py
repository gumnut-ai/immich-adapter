"""Verify that public config routes bypass authentication end to end."""

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


class TestPublicConfigEndpoint:
    def test_config_is_served_without_credentials(self, client):
        response = client.get("/api/public/config")
        assert response.status_code == 200

        data = response.json()
        assert data["oauth"]["enabled"] is True
        assert data["oauth"]["autoLaunch"] is True
        assert data["oauth"]["buttonText"] == "Sign in with Gumnut"
        assert data["passwordLogin"]["enabled"] is False

    def test_defaults_are_served_without_credentials(self, client):
        response = client.get("/api/public/config/defaults")
        assert response.status_code == 200
        assert response.json() == client.get("/api/public/config").json()
