"""Integration tests for GET /api/server/config.

The web trash page renders ``serverConfigManager.value.trashDays`` as
"trashed items will be permanently deleted after N days". This must reflect
the deploy-time TRASH_RETENTION_DAYS env var so the message is truthful.
"""

import pytest
from fastapi.testclient import TestClient

from config.settings import get_settings
from main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Settings are lru_cached; clear before/after so env mutations take effect."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestServerConfigTrashDays:
    def test_default_trash_days_is_ninety(self, client):
        """Default retention is 90 days, matching the photos-api default.

        The previous hardcoded 30 was wrong and rendered a misleading message
        on the web trash page.
        """
        response = client.get("/api/server/config")
        assert response.status_code == 200
        assert response.json()["trashDays"] == 90

    def test_trash_days_overridden_by_env(self, client, monkeypatch):
        """TRASH_RETENTION_DAYS env var overrides the default."""
        monkeypatch.setenv("TRASH_RETENTION_DAYS", "45")
        get_settings.cache_clear()

        response = client.get("/api/server/config")
        assert response.status_code == 200
        assert response.json()["trashDays"] == 45
