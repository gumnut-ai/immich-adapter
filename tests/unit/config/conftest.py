"""Shared fixtures for the telemetry tests.

`telemetry` initializes Sentry from the production options (traces sampled at
100%) with a capturing transport, and attaches a stdout handler built from the
production logging config plus a record collector, so a test can assert a
secret appears nowhere in the serialized envelopes, the formatted log output
or the log records themselves.
"""

import copy
import io
import logging
import logging.config
import socket
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
import redis.asyncio
import sentry_sdk
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from config.logging import LOGGING_CONFIG
from config.sentry import sentry_init_options


class _CaptureTransport(Transport):
    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self.envelopes: list[Envelope] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        self.envelopes.append(envelope)


class _RecordCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@dataclass
class Telemetry:
    transport: _CaptureTransport
    stdout: io.StringIO
    collector: _RecordCollector
    item_types: set[str] = field(default_factory=set)

    def flush(self) -> str:
        """Flush Sentry and return everything emitted so far as one string."""
        sentry_sdk.flush()
        rendered = []
        for envelope in self.transport.envelopes:
            for item in envelope.items:
                self.item_types.add(item.type or "")
            rendered.append(envelope.serialize().decode("utf-8", "replace"))
        return "\n".join(rendered) + "\n" + self.stdout.getvalue()

    def records_with(self, secret: str) -> list[str]:
        """Log records emitted so far whose attributes contain the secret.

        The Sentry and stdout backstops would hide a call site that still puts
        a secret on its record, so the records are checked at the source too.
        """
        return [
            record.getMessage()
            for record in self.collector.records
            if secret in repr(vars(record)) or secret in record.getMessage()
        ]


@pytest.fixture
def telemetry() -> Iterator[Telemetry]:
    transport = _CaptureTransport()
    options = sentry_init_options("https://public@example.invalid/1")
    options.update(
        traces_sample_rate=1.0, profiles_sample_rate=0.0, transport=transport
    )
    prior_client = sentry_sdk.get_client()
    sentry_sdk.init(**options)

    stdout = io.StringIO()
    handler = logging.StreamHandler(stdout)
    configurator = logging.config.DictConfigurator({"version": 1})
    handler.setFormatter(
        configurator.configure_formatter(
            copy.deepcopy(LOGGING_CONFIG["formatters"]["default"])
        )
    )
    collector = _RecordCollector()
    # The root logger plus the configured loggers that do not propagate to it.
    loggers = [logging.getLogger()] + [
        logging.getLogger(name) for name in LOGGING_CONFIG["loggers"]
    ]
    prior_level = loggers[0].level
    for logger in loggers:
        logger.addHandler(handler)
        logger.addHandler(collector)
    loggers[0].setLevel(logging.INFO)
    try:
        yield Telemetry(transport, stdout, collector)
    finally:
        for logger in loggers:
            logger.removeHandler(collector)
            logger.removeHandler(handler)
        loggers[0].setLevel(prior_level)
        sentry_sdk.get_client().close()
        sentry_sdk.get_global_scope().set_client(prior_client)


@pytest.fixture
async def unreachable_redis() -> AsyncIterator[redis.asyncio.Redis]:
    """A real redis-py client whose every command fails to connect."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    client = redis.asyncio.Redis(
        port=closed_port,
        socket_connect_timeout=0.2,
        retry=Retry(NoBackoff(), retries=0),
        decode_responses=True,
    )
    yield client
    await client.aclose()
