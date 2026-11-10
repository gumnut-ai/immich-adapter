"""Signed CDN URL credentials never reach Sentry.

Sentry is initialized from the production options (traces sampled at 100%)
with a capturing transport. A fetch of a synthetic signed URL fails, and the
failure is logged and captured, so the URL reaches a transaction span, a log,
a breadcrumb, the exception message and frame locals.
"""

import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from config.sentry import sentry_init_options

TOKEN = "SYNTHETICverifyTOKEN0123456789abcdefghijklm"
SIGNED_URL = (
    "https://assets.example.test/derived/asset.jpg"
    f"?f=webp&w=720&verify={TOKEN}&dl=family-photo.jpg"
)

logger = logging.getLogger(__name__)


class _CaptureTransport(Transport):
    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self.envelopes: list[Envelope] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        self.envelopes.append(envelope)


@pytest.fixture
def transport() -> Iterator[_CaptureTransport]:
    transport = _CaptureTransport()
    options = sentry_init_options("https://public@example.invalid/1")
    options.update(
        traces_sample_rate=1.0, profiles_sample_rate=0.0, transport=transport
    )
    prior_client = sentry_sdk.get_client()
    sentry_sdk.init(**options)
    try:
        yield transport
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.get_global_scope().set_client(prior_client)


async def _fetch(url: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404))
    ) as client:
        response = await client.get(url)
        response.raise_for_status()


@pytest.mark.anyio
async def test_failed_signed_fetch_sends_no_credential(transport):
    with sentry_sdk.start_transaction(op="http.server", name="thumbnail"):
        try:
            await _fetch(SIGNED_URL)
        except httpx.HTTPStatusError as exc:
            logger.warning("CDN fetch failed: %s", exc)
            sentry_sdk.capture_exception(exc)
    sentry_sdk.flush()

    payloads: dict[str, list[Any]] = {}
    rendered = []
    for envelope in transport.envelopes:
        for item in envelope.items:
            payloads.setdefault(item.type or "", []).append(item.payload.json)
        rendered.append(envelope.serialize().decode("utf-8", "replace"))
    emitted = "\n".join(rendered)

    assert sorted(payloads) == ["event", "log", "transaction"]
    assert TOKEN not in emitted
    assert "family-photo.jpg" not in emitted
    ((log,),) = [payload["items"] for payload in payloads["log"]]
    assert "w=720&verify=REDACTED&dl=REDACTED" in log["body"]
    (event,) = payloads["event"]
    assert "verify=REDACTED" in event["exception"]["values"][0]["value"]
