"""Integration tests for the jobs stub endpoints.

These exercise the HTTP/validation layer with a real ``TestClient``, unlike
``tests/unit/api/test_jobs.py`` which calls the stub functions directly and so
cannot catch the path-parameter validation regression these tests guard against
(the queue-name typing that broke immich-go's default pause/resume — see the
``send_job_command`` docstring).
"""

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


class TestSendJobCommand:
    """PUT /api/jobs/{name} must ack camelCase queue names with a benign 200."""

    @pytest.mark.parametrize("command", ["pause", "resume", "start", "empty"])
    def test_camelcase_queue_name_is_accepted(self, client, command):
        """immich-go's pause/resume must not 422 on legacy queue names."""
        response = client.put(
            "/api/jobs/thumbnailGeneration", json={"command": command}
        )

        assert response.status_code == 200, response.text
        data = response.json()
        # Benign no-op status: nothing is actually running or paused.
        assert data["queueStatus"] == {"isActive": False, "isPaused": False}
        assert data["jobCounts"]["active"] == 0

    def test_every_queue_name_is_accepted(self, client):
        """All legacy queue names immich-go might pause resolve, not just one."""
        for queue in ("metadataExtraction", "faceDetection", "videoConversion"):
            response = client.put(f"/api/jobs/{queue}", json={"command": "pause"})
            assert response.status_code == 200, f"{queue}: {response.text}"

    def test_unknown_queue_name_still_422s(self, client):
        """A name outside the QueueName enum is still rejected (contract fidelity)."""
        response = client.put("/api/jobs/notARealQueue", json={"command": "pause"})
        assert response.status_code == 422
