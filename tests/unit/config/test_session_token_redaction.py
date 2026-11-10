"""Unit tests for the session-token substitution rules in config.telemetry."""

from typing import Any
from uuid import UUID, uuid4

import pytest

from config.sentry import (
    _before_send_log,
    _before_send_transaction,
    _in_session_token_module,
    _redact_error_event,
    _scrub_session_tokens,
)
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
        (
            f"Command # 3 (SREM user:u1:sessions {TOKEN}) of pipeline",
            f"Command # 3 (SREM user:u1:sessions {REF}) of pipeline",
        ),
        (
            f"ZADD sessions:by_updated_at 1731.5 {TOKEN}",
            f"ZADD sessions:by_updated_at 1731.5 {REF}",
        ),
        (f"ZREM sessions:by_updated_at {TOKEN}", f"ZREM sessions:by_updated_at {REF}"),
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
        "environ": {"HTTP_X_IMMICH_USER_TOKEN": TOKEN},
        "scope": {"headers": [["b'x-immich-user-token'", f"b'{TOKEN}'"]]},
    }

    redacted = redact_session_tokens(payload)

    assert TOKEN not in repr(redacted)
    assert redacted["extra"]["session_id"] == REF
    assert redacted["scope"]["headers"][0] == ["b'x-immich-user-token'", REF]


def test_session_fields_holding_uuids_or_lists_are_substituted():
    """Tokens are usually handled as `UUID`s, so that is the likeliest shape
    for a future `extra={"session_id": ...}`."""
    payload = {
        "session_id": UUID(TOKEN),
        "session_token": [TOKEN, (UUID(TOKEN),)],
        "vars": {"session_id": f"UUID('{TOKEN}')"},
    }

    assert redact_session_tokens(payload) == {
        "session_id": REF,
        "session_token": [REF, [REF]],
        "vars": {"session_id": REF},
    }


def test_payload_is_not_mutated():
    """Log attributes reference the caller's own `extra` objects."""
    extra = {"session_id": TOKEN, "keys": [f"session:{TOKEN}"]}

    redacted = redact_session_tokens({"payload": extra})

    assert extra == {"session_id": TOKEN, "keys": [f"session:{TOKEN}"]}
    assert TOKEN not in repr(redacted)


def test_token_starting_with_b_is_not_mistaken_for_a_bytes_repr():
    """`b'...'` unquoting must not eat the leading hex digit of a bare token."""
    token = "b" + str(uuid4())[1:]
    payload = {"session_id": token, "headers": [["x-immich-user-token", token]]}

    assert redact_session_tokens(payload) == {
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
    assert _in_session_token_module("services.session_store")
    assert not _in_session_token_module("services.session_storex")
    assert not _in_session_token_module("routers.api.assets")
    assert not _in_session_token_module(None)


def test_frame_locals_survive_only_in_unlisted_adapter_modules():
    """Library frames (redis-py command arguments, the ASGI scope) hold tokens
    under names no field rule knows, so only in-app frames keep their locals."""

    def frame(module: str, in_app: object) -> dict[str, Any]:
        return {"module": module, "in_app": in_app, "vars": {"args": TOKEN}}

    frames = [
        frame("routers.api.assets", True),
        frame("services.session_store", True),
        frame("redis.asyncio.client", False),
        frame("asyncio.tasks", None),
    ]
    event = {"exception": {"values": [{"stacktrace": {"frames": frames}}]}}

    _scrub_session_tokens(event)

    assert [f["vars"] == {"args": TOKEN} for f in frames] == [
        True,
        False,
        False,
        False,
    ]


def test_session_token_modules_exist():
    """A renamed module would silently stop having its locals dropped."""
    import importlib

    from config.sentry import _SESSION_TOKEN_MODULES

    for name in _SESSION_TOKEN_MODULES:
        importlib.import_module(name)


@pytest.mark.parametrize(
    "hook, payload",
    [
        (_redact_error_event, {"extra": {"session_id": TOKEN}}),
        (_before_send_transaction, {"type": "transaction", "spans": []}),
        (_before_send_log, {"body": f"session:{TOKEN}", "attributes": {}}),
    ],
)
def test_hooks_drop_the_payload_when_scrubbing_fails(monkeypatch, hook, payload):
    """A session token is a live credential: unlike CDN redaction, which
    fails open, a scrubbing failure drops the event or log."""

    def fail(_value: Any) -> Any:
        raise RuntimeError("scrubber failed")

    monkeypatch.setattr("config.sentry.redact_session_tokens", fail)

    assert hook(payload, {}) is None
