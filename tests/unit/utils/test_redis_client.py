"""Unit tests for Redis client utilities."""

import pytest
from unittest.mock import AsyncMock, patch

import redis.exceptions

from utils.redis_client import check_redis_connection, get_redis_client


class TestGetRedisClient:
    """Tests for get_redis_client()."""

    @pytest.fixture(autouse=True)
    def reset_redis_client(self):
        """Reset the module-level singleton before each test."""
        import utils.redis_client as mod

        mod._redis_client = None
        yield
        mod._redis_client = None

    @pytest.mark.anyio
    async def test_get_redis_client_pool_configuration(self):
        """Test that Redis client is created with connection pool parameters."""
        mock_client = AsyncMock()

        with patch(
            "utils.redis_client.redis.from_url", return_value=mock_client
        ) as mock_from_url:
            client = await get_redis_client()

            mock_from_url.assert_called_once()
            call_kwargs = mock_from_url.call_args.kwargs
            assert call_kwargs["decode_responses"] is True
            assert call_kwargs["max_connections"] == 20
            assert call_kwargs["socket_connect_timeout"] == 5
            assert call_kwargs["socket_timeout"] == 5
            assert call_kwargs["health_check_interval"] == 30
            assert client is mock_client


class TestCheckRedisConnection:
    """Tests for check_redis_connection()."""

    @pytest.mark.anyio
    async def test_check_redis_connection_success(self):
        """Test successful Redis connection check."""
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)

        with patch("utils.redis_client.get_redis_client", return_value=mock_client):
            await check_redis_connection()
            mock_client.ping.assert_called_once()

    @pytest.mark.anyio
    async def test_check_redis_connection_failure(self):
        """Test Redis connection check raises RedisError on connection failure."""
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(
            side_effect=redis.exceptions.ConnectionError(
                "Error 111 connecting to localhost:6379. Connection refused."
            )
        )

        with patch("utils.redis_client.get_redis_client", return_value=mock_client):
            with pytest.raises(redis.exceptions.ConnectionError) as exc_info:
                await check_redis_connection()

            assert "Connection refused" in str(exc_info.value)

    @pytest.mark.anyio
    async def test_check_redis_connection_timeout(self):
        """Test Redis connection check raises RedisError on timeout."""
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(
            side_effect=redis.exceptions.TimeoutError("Connection timed out")
        )

        with patch("utils.redis_client.get_redis_client", return_value=mock_client):
            with pytest.raises(redis.exceptions.TimeoutError) as exc_info:
                await check_redis_connection()

            assert "timed out" in str(exc_info.value)
