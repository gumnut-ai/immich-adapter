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
                    # Cap the pool so we fail fast when all connections are
                    # in use, instead of silently creating thousands of
                    # connections. Default is 2**31 (effectively unlimited).
                    max_connections=20,
                    # Bound the TCP handshake so a DNS or network issue
                    # surfaces quickly rather than blocking indefinitely.
                    # Default is None (no timeout).
                    socket_connect_timeout=5,
                    # Bound individual read/write operations so a stalled
                    # Redis command doesn't pin a connection forever.
                    # Default is None (no timeout).
                    socket_timeout=5,
                    # Proactively verify idle connections before reuse,
                    # avoiding errors from connections silently closed by
                    # the server or a proxy. Default is 0 (disabled).
                    health_check_interval=30,
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
