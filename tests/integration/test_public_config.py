"""Integration tests for the public config endpoints.

Unit tests pin the DTO construction and the UNAUTHENTICATED_PATHS entries
separately; only a full-app request proves the router prefix and the
middleware exemption actually agree. A mismatch turns every v3.2 web login
into a 401, which is exactly the failure `/public/config` exists to prevent.
"""

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
        # Load-bearing for the web login flow: auto-launch into OAuth,
        # button label as the fallback, no password form.
        assert data["oauth"]["enabled"] is True
        assert data["oauth"]["autoLaunch"] is True
        assert data["oauth"]["buttonText"] == "Sign in with Gumnut"
        assert data["passwordLogin"]["enabled"] is False

    def test_defaults_are_served_without_credentials(self, client):
        response = client.get("/api/public/config/defaults")
        assert response.status_code == 200
        assert response.json() == client.get("/api/public/config").json()
