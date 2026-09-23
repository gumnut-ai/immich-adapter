"""Session sync epochs, run through the real Lua scripts on an in-memory Redis.

A session's checkpoints and client reset state belong to its sync epoch, and
only leaving a library advances it (see SessionStore.set_library).
"""

from datetime import datetime, timezone
from uuid import UUID

import fakeredis
import pytest

from routers.immich_models import SyncEntityType
from services.checkpoint_store import CheckpointStore
from services.session_store import Session, SessionStore, checkpoint_key

TOKEN = UUID("550e8400-e29b-41d4-a716-446655440000")
SESSION_KEY = f"session:{TOKEN}"


@pytest.fixture
def redis():
    return fakeredis.FakeAsyncRedis(decode_responses=True)


@pytest.fixture
def sessions(redis):
    return SessionStore(redis)


@pytest.fixture
def checkpoints(redis):
    return CheckpointStore(redis)


async def _put_session(redis, library_id: str = "lib_a", **fields: str) -> None:
    now = datetime.now(timezone.utc)
    data = Session(
        id=TOKEN,
        user_id="user_1",
        library_id=library_id,
        stored_jwt="enc",
        device_type="iOS",
        device_os="iOS 17",
        app_version="1.94.0",
        created_at=now,
        updated_at=now,
    ).to_dict()
    await redis.hset(SESSION_KEY, mapping={**data, **fields})


async def _session(sessions: SessionStore) -> Session:
    session = await sessions.get_by_id(str(TOKEN))
    assert session is not None
    return session


async def _ack(checkpoints: CheckpointStore, epoch: int, cursor: str) -> bool:
    return await checkpoints.set_many(TOKEN, epoch, [(SyncEntityType.AssetV1, cursor)])


async def _cursor(checkpoints: CheckpointStore, epoch: int) -> str | None:
    checkpoint = await checkpoints.get(TOKEN, epoch, SyncEntityType.AssetV1)
    return checkpoint.cursor if checkpoint else None


@pytest.mark.anyio
async def test_switch_advances_the_epoch_and_drops_old_checkpoints(
    redis, sessions, checkpoints
):
    await _put_session(redis)
    assert await _ack(checkpoints, 0, "a1")

    assert await sessions.set_library(str(TOKEN), "lib_a", "lib_b", from_choice=True)

    session = await _session(sessions)
    assert (session.library_id, session.sync_epoch) == ("lib_b", 1)
    assert session.library_from_choice is True
    assert session.is_pending_sync_reset is True
    assert not await redis.exists(checkpoint_key(TOKEN, 0))


@pytest.mark.anyio
@pytest.mark.parametrize(
    "previous,library_id",
    [
        pytest.param("", "lib_a", id="first-resolution"),
        pytest.param("lib_a", "lib_a", id="recheck-unchanged"),
    ],
)
async def test_recording_without_leaving_a_library_keeps_the_epoch(
    redis, sessions, checkpoints, previous, library_id
):
    await _put_session(redis, previous, library_checked_at="0")
    assert await _ack(checkpoints, 0, "a1")

    assert await sessions.set_library(str(TOKEN), previous, library_id)

    session = await _session(sessions)
    assert (session.library_id, session.sync_epoch) == (library_id, 0)
    assert session.library_checked_at > 0
    assert session.is_pending_sync_reset is False
    assert await _cursor(checkpoints, 0) == "a1"


@pytest.mark.anyio
async def test_drop_advances_the_epoch(redis, sessions):
    await _put_session(redis)

    assert await sessions.set_library(str(TOKEN), "lib_a", "")

    session = await _session(sessions)
    assert (session.library_id, session.sync_epoch) == ("", 1)
    assert session.is_pending_sync_reset is True


@pytest.mark.anyio
@pytest.mark.parametrize(
    "library_id,held", [("lib_b", True), ("lib_c", False), ("", False)]
)
async def test_stale_write_changes_nothing(redis, sessions, library_id, held):
    """A request that observed lib_a after the session moved to lib_b writes
    nothing; it holds its target only if that is where the session went."""
    await _put_session(redis, "lib_b", sync_epoch="1", client_epoch="1")

    assert await sessions.set_library(str(TOKEN), "lib_a", library_id) is held

    session = await _session(sessions)
    assert (session.library_id, session.sync_epoch) == ("lib_b", 1)


@pytest.mark.anyio
async def test_ack_for_an_epoch_the_session_left_is_dropped(
    redis, sessions, checkpoints
):
    await _put_session(redis)
    await sessions.set_library(str(TOKEN), "lib_a", "lib_b")

    assert not await _ack(checkpoints, 0, "a_late")
    assert not await redis.exists(checkpoint_key(TOKEN, 0))

    assert await _ack(checkpoints, 1, "b1")
    assert await _cursor(checkpoints, 1) == "b1"


@pytest.mark.anyio
async def test_checkpoints_expire_with_the_session(redis, checkpoints):
    await _put_session(redis)
    await redis.expire(SESSION_KEY, 600)

    assert await _ack(checkpoints, 0, "a1")

    assert 0 < await redis.ttl(checkpoint_key(TOKEN, 0)) <= 600


@pytest.mark.anyio
async def test_no_checkpoints_are_written_for_a_missing_session(redis, checkpoints):
    assert not await _ack(checkpoints, 0, "a1")
    assert not await redis.exists(checkpoint_key(TOKEN, 0))


@pytest.mark.anyio
async def test_reset_ack_brings_the_client_to_the_epoch(redis, sessions, checkpoints):
    await _put_session(redis)
    await sessions.set_library(str(TOKEN), "lib_a", "lib_b")

    # A reset issued for the epoch the session has since left changes nothing.
    assert not await sessions.acknowledge_sync_reset(str(TOKEN), 0)
    assert (await _session(sessions)).is_pending_sync_reset is True

    assert await sessions.acknowledge_sync_reset(str(TOKEN), 1)
    session = await _session(sessions)
    assert session.is_pending_sync_reset is False
    assert await _ack(checkpoints, 1, "b1")

    # The client wipes its local copy before every reset ack, so its
    # checkpoints go too.
    assert await sessions.acknowledge_sync_reset(str(TOKEN), 1)
    assert await _cursor(checkpoints, 1) is None


@pytest.mark.anyio
async def test_legacy_pending_reset_is_owed_until_acked(redis, sessions, checkpoints):
    """A session written before sync epochs keeps its checkpoints under the
    old key, and a reset it already owed still happens."""
    await _put_session(redis)
    await redis.hdel(SESSION_KEY, "sync_epoch", "client_epoch")
    await redis.hset(SESSION_KEY, "is_pending_sync_reset", "1")
    await redis.hset(f"{SESSION_KEY}:checkpoints", "AssetV1", "2025-01-01T00:00:00|a1")

    session = await _session(sessions)
    assert (session.sync_epoch, session.is_pending_sync_reset) == (0, True)
    assert await _cursor(checkpoints, 0) == "a1"

    assert await sessions.acknowledge_sync_reset(str(TOKEN), 0)

    session = await _session(sessions)
    assert session.is_pending_sync_reset is False
    assert await redis.hget(SESSION_KEY, "is_pending_sync_reset") == "0"
    assert await _cursor(checkpoints, 0) is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "advance",
    [
        pytest.param(
            lambda s: s.set_library(str(TOKEN), "lib_a", "lib_b"), id="switch"
        ),
        pytest.param(lambda s: s.set_pending_sync_reset(str(TOKEN), True), id="reset"),
    ],
)
async def test_legacy_session_resets_when_its_epoch_first_advances(
    redis, sessions, advance
):
    """A session written before sync epochs has a client copy of epoch 0."""
    await _put_session(redis)
    await redis.hdel(SESSION_KEY, "sync_epoch", "client_epoch")

    assert await advance(sessions)

    session = await _session(sessions)
    assert session.sync_epoch == 1
    assert session.is_pending_sync_reset is True


@pytest.mark.anyio
async def test_legacy_reset_flag_follows_the_epochs(redis, sessions):
    """An adapter that predates epochs reads is_pending_sync_reset, so every
    epoch change keeps it in step."""
    await _put_session(redis)

    await sessions.set_library(str(TOKEN), "lib_a", "lib_b")
    assert await redis.hget(SESSION_KEY, "is_pending_sync_reset") == "1"

    await sessions.acknowledge_sync_reset(str(TOKEN), 1)
    assert await redis.hget(SESSION_KEY, "is_pending_sync_reset") == "0"

    await sessions.set_pending_sync_reset(str(TOKEN), True)
    assert await redis.hget(SESSION_KEY, "is_pending_sync_reset") == "1"

    await sessions.set_pending_sync_reset(str(TOKEN), False)
    assert await redis.hget(SESSION_KEY, "is_pending_sync_reset") == "0"


@pytest.mark.anyio
async def test_requested_reset_advances_the_epoch_and_clearing_it_catches_up(
    redis, sessions, checkpoints
):
    await _put_session(redis)
    assert await _ack(checkpoints, 0, "a1")

    assert await sessions.set_pending_sync_reset(str(TOKEN), True)
    session = await _session(sessions)
    assert (session.library_id, session.sync_epoch) == ("lib_a", 1)
    assert session.is_pending_sync_reset is True
    assert not await redis.exists(checkpoint_key(TOKEN, 0))

    assert await sessions.set_pending_sync_reset(str(TOKEN), False)
    assert (await _session(sessions)).is_pending_sync_reset is False


@pytest.mark.anyio
async def test_requested_reset_needs_a_session(sessions):
    assert not await sessions.set_pending_sync_reset(str(TOKEN), True)
    assert not await sessions.set_pending_sync_reset(str(TOKEN), False)


@pytest.mark.anyio
async def test_delete_removes_the_current_epoch_checkpoints(
    redis, sessions, checkpoints
):
    await _put_session(redis, "lib_b", sync_epoch="2", client_epoch="2")
    assert await _ack(checkpoints, 2, "b1")

    assert await sessions.delete_by_id(str(TOKEN))

    assert not await redis.exists(checkpoint_key(TOKEN, 2))
