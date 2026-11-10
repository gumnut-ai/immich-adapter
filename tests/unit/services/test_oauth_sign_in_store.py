import fakeredis
import pytest

from services.oauth_sign_in_store import OAUTH_SIGN_IN_TTL_SECONDS, OAuthSignInStore


@pytest.fixture
def redis():
    return fakeredis.FakeAsyncRedis(decode_responses=True)


@pytest.fixture
def store(redis):
    return OAuthSignInStore(redis)


@pytest.mark.anyio
async def test_recorded_secret_is_bound_to_its_state_only(store):
    await store.bind("server-state", "secret")

    assert await store.is_bound("server-state", "secret")
    assert not await store.is_bound("server-state", "other-secret")
    assert not await store.is_bound("other-state", "secret")


@pytest.mark.anyio
async def test_several_secrets_can_be_bound_to_one_state(store):
    await store.bind("server-state", "browser-secret")
    await store.bind("server-state", "client-state")

    assert await store.is_bound("server-state", "browser-secret")
    assert await store.is_bound("server-state", "client-state")


@pytest.mark.anyio
async def test_record_expires_and_holds_neither_value(store, redis):
    await store.bind("server-state", "secret")

    [key] = await redis.keys("*")
    value = await redis.get(key)
    for raw in ("server-state", "secret"):
        assert raw not in key
        assert raw not in value
    assert 0 < await redis.ttl(key) <= OAUTH_SIGN_IN_TTL_SECONDS
