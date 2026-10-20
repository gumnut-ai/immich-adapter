"""Best-effort native metrics: telemetry must never change request behavior."""

import contextlib
from typing import Any

from sentry_sdk import metrics


def count(name: str, value: float, *, attributes: dict[str, Any]) -> None:
    with contextlib.suppress(Exception):
        metrics.count(name, value, attributes=attributes)


def distribution(
    name: str,
    value: float,
    *,
    attributes: dict[str, Any],
    unit: str | None = None,
) -> None:
    with contextlib.suppress(Exception):
        metrics.distribution(name, value, attributes=attributes, unit=unit)
