import fakeredis
import pytest

from services.oauth_sign_in_store import (
    OAUTH_SIGN_IN_TTL_SECONDS,
    OAuthSignIn,
    OAuthSignInStore,
    _key,
)


@pytest.fixture
def redis():
    return fakeredis.FakeAsyncRedis(decode_responses=True)


@pytest.fixture
def store(redis):
    return OAuthSignInStore(redis)


@pytest.mark.anyio
async def test_recorded_secret_is_bound_to_its_state_only(store):
    await store.bind("server-state", "secret", None)

    assert await store.find("server-state", "secret") == OAuthSignIn(None)
    assert await store.find("server-state", "other-secret") is None
    assert await store.find("other-state", "secret") is None


@pytest.mark.anyio
async def test_several_secrets_can_be_bound_to_one_state(store):
    await store.bind("server-state", "browser-secret", "verifier")
    await store.bind("server-state", "client-state", "verifier")

    assert await store.find("server-state", "browser-secret") == OAuthSignIn("verifier")
    assert await store.find("server-state", "client-state") == OAuthSignIn("verifier")


@pytest.mark.anyio
async def test_record_expires_and_holds_neither_state_nor_secret(store, redis):
    await store.bind("server-state", "secret", "verifier")

    [key] = await redis.keys("*")
    value = await redis.get(key)
    for raw in ("server-state", "secret"):
        assert raw not in key
        assert raw not in value
    assert 0 < await redis.ttl(key) <= OAUTH_SIGN_IN_TTL_SECONDS


@pytest.mark.anyio
async def test_record_from_before_verifiers_were_stored_has_no_verifier(store, redis):
    # A sign-in started before an upgrade and finished after it.
    await redis.set(_key("server-state", "secret"), "1")

    assert await store.find("server-state", "secret") == OAuthSignIn(None)
