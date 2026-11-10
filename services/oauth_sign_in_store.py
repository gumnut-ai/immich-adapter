"""Pairs each OAuth sign-in's state with secrets its starting client holds.

``_find_sign_in`` in ``routers/api/oauth.py`` owns why. Each pairing is a
Redis key named by a digest of the state and the secret, so Redis holds
neither value. The record's value carries the PKCE code verifier the adapter
generated for the sign-in, if any, so only a client holding one of the
sign-in's secrets can have it presented at the code exchange.
"""

import hashlib
import json
from dataclasses import dataclass
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


@dataclass(frozen=True)
class OAuthSignIn:
    """A sign-in started by the client holding the secret it was found by."""

    # The PKCE code verifier the adapter generated at authorize, or None when
    # the client supplied its own challenge and so presents its own verifier.
    code_verifier: str | None


class OAuthSignInStore:
    def __init__(self, redis_client: Any):
        # Typed Any like SessionStore: redis-py's client does not satisfy the
        # narrow protocol structurally.
        self._redis: AsyncRedisClient = redis_client

    async def bind(
        self, server_state: str, secret: str, code_verifier: str | None
    ) -> None:
        """Record that the client holding ``secret`` started this sign-in."""
        await self._redis.set(
            _key(server_state, secret),
            json.dumps({"code_verifier": code_verifier}),
            ex=OAUTH_SIGN_IN_TTL_SECONDS,
        )

    async def find(self, server_state: str, secret: str) -> OAuthSignIn | None:
        """Return the sign-in recorded for ``secret``, or None if there is none."""
        raw = await self._redis.get(_key(server_state, secret))
        if raw is None:
            return None
        record = json.loads(raw)
        # Records written before verifiers were stored hold "1": those
        # sign-ins sent no adapter-generated challenge, so need no verifier.
        if not isinstance(record, dict):
            return OAuthSignIn(code_verifier=None)
        return OAuthSignIn(code_verifier=record.get("code_verifier"))


async def get_oauth_sign_in_store() -> OAuthSignInStore:
    """FastAPI dependency providing the store on the shared Redis client."""
    return OAuthSignInStore(await get_redis_client())
