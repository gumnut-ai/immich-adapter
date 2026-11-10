"""Pairs each OAuth sign-in's state with secrets its starting client holds.

``_callback_is_bound`` in ``routers/api/oauth.py`` owns why. Each pairing is a
Redis key named by a digest of the state and the secret, so Redis holds
neither value.
"""

import hashlib
from typing import Any

from utils.redis_client import get_redis_client
from utils.redis_protocols import AsyncRedisClient

# Only needs to outlive one trip through the identity provider's sign-in
# page; the Gumnut API rejects an expired state on its own.
OAUTH_SIGN_IN_TTL_SECONDS = 10 * 60

_KEY_PREFIX = "oauth_sign_in:"


def _key(server_state: str, secret: str) -> str:
    digest = hashlib.sha256(f"{server_state}\0{secret}".encode()).hexdigest()
    return f"{_KEY_PREFIX}{digest}"


class OAuthSignInStore:
    def __init__(self, redis_client: Any):
        # Typed Any like SessionStore: redis-py's client does not satisfy the
        # narrow protocol structurally.
        self._redis: AsyncRedisClient = redis_client

    async def bind(self, server_state: str, secret: str) -> None:
        """Record that the client holding ``secret`` started this sign-in."""
        await self._redis.set(
            _key(server_state, secret), "1", ex=OAUTH_SIGN_IN_TTL_SECONDS
        )

    async def is_bound(self, server_state: str, secret: str) -> bool:
        """Return whether ``secret`` was recorded for this sign-in."""
        return await self._redis.get(_key(server_state, secret)) is not None


async def get_oauth_sign_in_store() -> OAuthSignInStore:
    """FastAPI dependency providing the store on the shared Redis client."""
    return OAuthSignInStore(await get_redis_client())
