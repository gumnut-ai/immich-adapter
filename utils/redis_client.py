import asyncio

import redis.asyncio as redis

from config.settings import get_settings

_redis_client: redis.Redis | None = None
_redis_lock = asyncio.Lock()


async def get_redis_client() -> redis.Redis:
    """
    Get or create singleton async Redis client.

    Uses double-checked locking to prevent race conditions when
    multiple coroutines attempt to create the client simultaneously.

    Returns:
        Configured Redis client instance with decode_responses=True
    """
    global _redis_client
    if _redis_client is None:
        async with _redis_lock:
            # Double-check after acquiring lock
            if _redis_client is None:
                settings = get_settings()
                _redis_client = redis.from_url(
                    settings.redis_url,
                    decode_responses=True,
                    max_connections=settings.redis_max_connections,
                    socket_connect_timeout=settings.redis_socket_connect_timeout,
                    socket_timeout=settings.redis_socket_timeout,
                    health_check_interval=settings.redis_health_check_interval,
                )
    return _redis_client


async def check_redis_connection() -> None:
    """
    Verify Redis is reachable by sending a PING command.

    Raises:
        redis.exceptions.RedisError: If Redis is unreachable
    """
    client = await get_redis_client()
    await client.ping()


async def close_redis_client() -> None:
    """Close the singleton Redis client if it exists."""
    global _redis_client
    if _redis_client is not None:
        try:
            await _redis_client.aclose()
        except Exception:
            pass
        finally:
            _redis_client = None


def _reset_for_testing() -> None:
    """Reset module state. Only for use in tests."""
    global _redis_client, _redis_lock
    _redis_client = None
    _redis_lock = asyncio.Lock()
