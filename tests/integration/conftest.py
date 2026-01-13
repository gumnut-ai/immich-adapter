"""Integration test configuration and shared fixtures."""

import pytest
from unittest.mock import AsyncMock, patch


@pytest.fixture(autouse=True)
def mock_redis_connection_check():
    """
    Mock Redis connection check for all integration tests.

    Integration tests don't require a real Redis instance - the Redis
    check is only needed for production startup validation.
    """
    with patch("main.check_redis_connection", new_callable=AsyncMock) as mock:
        yield mock
