"""Integration tests for GET /api/server/features."""

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


class TestServerFeaturesEndpoint:
    def test_unimplemented_features_are_disabled(self, client):
        """Flags for stub features must be false so clients hide their UI."""
        response = client.get("/api/server/features")
        assert response.status_code == 200

        data = response.json()
        for flag in (
            "duplicateDetection",
            "map",
            "reverseGeocoding",
            "trash",
            "sidecar",
        ):
            assert data[flag] is False, f"{flag} must be False — endpoint is a stub"

    def test_implemented_features_are_enabled(self, client):
        """Flags for features backed by real implementations stay true."""
        response = client.get("/api/server/features")
        assert response.status_code == 200

        data = response.json()
        for flag in (
            "smartSearch",
            "facialRecognition",
            "search",
            "oauth",
            "oauthAutoLaunch",
        ):
            assert data[flag] is True, f"{flag} should be True"

    def test_password_login_disabled(self, client):
        """Password login is disabled — OAuth is the only login method."""
        response = client.get("/api/server/features")
        assert response.status_code == 200
        assert response.json()["passwordLogin"] is False
