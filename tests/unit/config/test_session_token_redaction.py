"""Unit tests for the session-token substitution rules in config.telemetry."""

from uuid import uuid4

from config.sentry import _in_session_token_module
from config.telemetry import (
    redact_session_tokens,
    redact_session_tokens_text,
    session_telemetry_id,
)

TOKEN = str(uuid4())
REF = session_telemetry_id(TOKEN)


def test_telemetry_id_is_stable_short_and_not_the_token():
    assert session_telemetry_id(TOKEN) == REF
    assert len(REF) == 16
    assert TOKEN not in REF
    assert session_telemetry_id(str(uuid4())) != REF


def test_text_marked_as_session_is_substituted():
    for raw, expected in [
        (f"HGETALL 'session:{TOKEN}'", f"HGETALL 'session:{REF}'"),
        (f"session:{TOKEN}:checkpoints:2", f"session:{REF}:checkpoints:2"),
        (f"PUT /api/sessions/{TOKEN} HTTP/1.1", f"PUT /api/sessions/{REF} HTTP/1.1"),
        (f"Bearer {TOKEN}", f"Bearer {REF}"),
        (f"a=1; immich_access_token={TOKEN}", f"a=1; immich_access_token={REF}"),
    ]:
        assert redact_session_tokens_text(raw) == expected


def test_unmarked_uuids_are_left_alone():
    """Asset, album and user ids share the token's shape; only context marks a
    session."""
    text = f"asset {TOKEN} in /api/assets/{TOKEN} sessions:by_updated_at"
    assert redact_session_tokens_text(text) == text
    payload = {"asset_id": TOKEN, "ids": [TOKEN], "user_id": TOKEN}
    assert redact_session_tokens(payload) == {
        "asset_id": TOKEN,
        "ids": [TOKEN],
        "user_id": TOKEN,
    }


def test_session_fields_are_substituted_in_every_shape():
    payload = {
        "extra": {"session_id": TOKEN, "session_token": f"'{TOKEN}'"},
        "request": {"headers": {"X-Immich-User-Token": TOKEN}},
        "data": {"http.request.header.x-immich-user-token": TOKEN},
        "environ": {"HTTP_X_IMMICH_USER_TOKEN": TOKEN},
        "scope": {"headers": [["b'x-immich-user-token'", f"b'{TOKEN}'"]]},
    }

    redact_session_tokens(payload)

    assert TOKEN not in repr(payload)
    assert payload["extra"]["session_id"] == REF
    assert payload["scope"]["headers"][0] == ["b'x-immich-user-token'", REF]


def test_token_starting_with_b_is_not_mistaken_for_a_bytes_repr():
    """`b'...'` unquoting must not eat the leading hex digit of a bare token."""
    token = "b" + str(uuid4())[1:]
    payload = {"session_id": token, "headers": [["x-immich-user-token", token]]}

    redact_session_tokens(payload)

    assert payload == {
        "session_id": session_telemetry_id(token),
        "headers": [["x-immich-user-token", session_telemetry_id(token)]],
    }


def test_substitution_is_idempotent():
    """Call sites already log the telemetry id; the backstops must not rehash
    it, or records and spans of one session would stop correlating."""
    payload = {"session_id": REF, "key": f"session:{REF}"}
    assert redact_session_tokens(payload) == {
        "session_id": REF,
        "key": f"session:{REF}",
    }


def test_session_token_modules_match_by_package_prefix():
    assert _in_session_token_module("routers.middleware.auth_middleware")
    assert _in_session_token_module("starlette.middleware.base")
    assert _in_session_token_module("services.session_store")
    assert not _in_session_token_module("services.session_storex")
    assert not _in_session_token_module("routers.api.assets")
    assert not _in_session_token_module(None)


def test_session_token_modules_exist():
    """A renamed module would silently stop having its locals dropped."""
    import importlib

    from config.sentry import _SESSION_TOKEN_MODULES

    for name in _SESSION_TOKEN_MODULES:
        importlib.import_module(name)
