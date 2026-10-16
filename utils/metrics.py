"""Best-effort native metrics: telemetry must never change request behavior."""

from typing import Any

from sentry_sdk import metrics


def count(name: str, value: float, *, attributes: dict[str, Any]) -> None:
    try:
        metrics.count(name, value, attributes=attributes)
    except Exception:
        pass


def distribution(
    name: str,
    value: float,
    *,
    attributes: dict[str, Any],
    unit: str | None = None,
) -> None:
    try:
        metrics.distribution(name, value, attributes=attributes, unit=unit)
    except Exception:
        pass
