"""Bounded HTTP-attempt observations for the shared Gumnut API client."""

import asyncio
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx

from utils import metrics

_OPERATION_FAMILIES = frozenset(
    {
        "assets",
        "albums",
        "users",
        "libraries",
        "people",
        "faces",
        "stacks",
        "events",
        "auth",
    }
)


@dataclass
class _Attempt:
    started: float
    operation: str


@dataclass
class _SendState:
    attempt: _Attempt | None = None


class GumnutHTTPClient(httpx.AsyncClient):
    """Observe every SDK retry and redirect hop without changing transports.

    Duration runs from the last request hook to the first response hook (headers),
    or to an exception before headers. It includes pool wait, connection setup
    and request upload, but excludes response-body consumption, later response
    hooks and SDK retry backoff. A body/hook failure after headers retains the
    single response observation; it never adds a second failed attempt.
    """

    def __init__(self, **kwargs: Any) -> None:
        hooks = dict(kwargs.pop("event_hooks", None) or {})
        # Starting last excludes failures in caller request hooks. Finishing
        # first preserves the existing token/library hook's ordering and errors.
        hooks["request"] = [*hooks.get("request", []), self._start_attempt]
        hooks["response"] = [self._response_received, *hooks.get("response", [])]
        super().__init__(event_hooks=hooks, **kwargs)
        self._send_state: ContextVar[_SendState | None] = ContextVar(
            "gumnut_http_send", default=None
        )

    async def _start_attempt(self, request: httpx.Request) -> None:
        state = self._send_state.get()
        if state is not None:
            path = request.url.path.removeprefix("/api/")
            family = path.strip("/").split("/", 1)[0]
            state.attempt = _Attempt(
                time.perf_counter(),
                family if family in _OPERATION_FAMILIES else "other",
            )

    def _finish_attempt(self, outcome: str, status_code: int | None = None) -> None:
        state = self._send_state.get()
        if state is None or state.attempt is None:
            return
        attempt, state.attempt = state.attempt, None
        elapsed = time.perf_counter() - attempt.started
        attributes = {
            "http.operation": attempt.operation,
            "http.status_class": (
                f"{status_code // 100}xx"
                if status_code is not None and 100 <= status_code < 600
                else "none"
                if status_code is None
                else "other"
            ),
            "http.outcome": outcome,
        }
        metrics.count("gumnut.http.attempt", 1, attributes=attributes)
        metrics.distribution(
            "gumnut.http.attempt.duration",
            elapsed,
            attributes=attributes,
            unit="second",
        )

    async def _response_received(self, response: httpx.Response) -> None:
        self._finish_attempt("response", response.status_code)

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        token = self._send_state.set(_SendState())
        try:
            return await super().send(request, **kwargs)
        except asyncio.CancelledError:
            self._finish_attempt("cancelled")
            raise
        except httpx.TimeoutException:
            self._finish_attempt("timeout")
            raise
        except Exception:
            self._finish_attempt("error")
            raise
        finally:
            self._send_state.reset(token)
