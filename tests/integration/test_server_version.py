"""Integration tests for server version endpoints."""

import pytest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from config.immich_version import ImmichVersion


@pytest.fixture
def client():
    """Create a test client with mocked version."""
    from main import app

    # Mock get_settings() to return a settings object with our test version
    mock_settings = Mock()
    mock_settings.immich_version = ImmichVersion(major=2, minor=2, patch=2)

    with patch("routers.api.server.get_settings", return_value=mock_settings):
        with TestClient(app) as client:
            yield client


class TestServerVersionEndpoint:
    def test_get_server_version(self, client):
        """Test GET /api/server/version returns correct version."""
        response = client.get("/api/server/version")

        assert response.status_code == 200
        data = response.json()

        assert "major" in data
        assert "minor" in data
        assert "patch" in data

        assert data["major"] == 2
        assert data["minor"] == 2
        assert data["patch"] == 2

    def test_get_server_version_structure(self, client):
        """Test that version response has correct structure."""
        response = client.get("/api/server/version")

        assert response.status_code == 200
        data = response.json()

        # Verify all required fields are present
        assert set(data.keys()) == {"major", "minor", "patch"}

        # Verify all values are integers
        assert isinstance(data["major"], int)
        assert isinstance(data["minor"], int)
        assert isinstance(data["patch"], int)


class TestVersionCheckEndpoint:
    def test_get_version_check(self, client):
        """Test GET /api/server/version-check returns correct version."""
        response = client.get("/api/server/version-check")

        assert response.status_code == 200
        data = response.json()

        assert "checkedAt" in data
        assert "releaseVersion" in data

        # releaseVersion should be in semver format
        assert data["releaseVersion"] == "2.2.2"

    def test_get_version_check_timestamp(self, client):
        """Test that version-check includes a valid timestamp."""
        response = client.get("/api/server/version-check")

        assert response.status_code == 200
        data = response.json()

        # Verify checkedAt is a valid ISO timestamp string
        assert "checkedAt" in data
        assert isinstance(data["checkedAt"], str)
        assert len(data["checkedAt"]) > 0

        # Should be able to parse as datetime
        from datetime import datetime

        datetime.fromisoformat(data["checkedAt"].replace("Z", "+00:00"))
