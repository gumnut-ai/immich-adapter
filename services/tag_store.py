"""Redis-backed store mapping synthetic tag IDs to their tag values.

The Gumnut API has no tag concept. To keep Immich clients working — notably
immich-go, which upserts a tag and then assigns assets to it by ID — the adapter
emulates tags: ``PUT /api/tags`` mints a deterministic synthetic ID per tag and
records the ``ID -> value`` mapping here, and ``PUT /api/tags/{id}/assets`` later
recovers the value to append it to each asset's description.

The mapping has to survive between those two requests and across worker
processes, so it lives in Redis (already a hard dependency for sessions and
checkpoints) rather than in process memory.
"""

from uuid import UUID, uuid5

from utils.redis_client import get_redis_client

# Fixed namespace for deriving synthetic tag IDs from (user, value). Chosen once
# at random; never change it — every previously-minted tag ID is derived from it,
# so a change would orphan tags mid-import.
_TAG_ID_NAMESPACE = UUID("6f3d8c2a-9b4e-4f1a-8c7d-2e1b0a9f8d6c")

# A tag mapping only needs to outlive a single import run (an upsert immediately
# followed by assignment), but a generous TTL keeps re-runs and slow imports
# working while ensuring keys eventually expire.
TAG_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days


def deterministic_tag_id(user_id: str, value: str) -> UUID:
    """Return a stable synthetic tag ID for a ``(user, tag value)`` pair.

    Deterministic so repeated upserts of the same tag return the same ID
    (idempotency) and every worker computes the same ID without coordination.
    Scoped by ``user_id`` so the same tag name for different users never
    collides.
    """
    return uuid5(_TAG_ID_NAMESPACE, f"{user_id}:{value}")


def _tag_key(user_id: str, tag_id: UUID) -> str:
    return f"immich_adapter:tag:{user_id}:{tag_id}"


async def remember_tag(user_id: str, tag_id: UUID, value: str) -> None:
    """Record ``tag_id -> value`` for this user, with a TTL."""
    client = await get_redis_client()
    await client.set(_tag_key(user_id, tag_id), value, ex=TAG_TTL_SECONDS)


async def lookup_tag_value(user_id: str, tag_id: UUID) -> str | None:
    """Return the tag value recorded for ``tag_id``, or ``None`` if unknown."""
    client = await get_redis_client()
    value = await client.get(_tag_key(user_id, tag_id))
    # The client is configured with decode_responses=True, so values come back
    # as ``str``; the decode guard just narrows the broad redis-py return type.
    if isinstance(value, bytes):
        return value.decode()
    return value
