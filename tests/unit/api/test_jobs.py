"""Unit tests for Jobs API endpoints.

The jobs stubs construct generated DTOs with a fixed set of queues, so a
model regeneration that adds a required queue breaks them at construction
time (pydantic ValidationError -> 500). The tests here exercise that
construction path end to end.
"""

import pytest

from routers.api.jobs import get_all_jobs_status
from routers.immich_models import QueuesResponseLegacyDto


class TestGetAllJobsStatus:
    """Test the get_all_jobs_status endpoint."""

    @pytest.mark.anyio
    async def test_constructs_valid_dto(self):
        """The stub must supply every queue the generated model requires."""
        status = await get_all_jobs_status()

        assert isinstance(status, QueuesResponseLegacyDto)
