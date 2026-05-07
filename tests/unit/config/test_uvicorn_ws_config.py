"""Static guard: uvicorn must resolve `--ws websockets-sansio` to the
modern sans-I/O implementation.

The Dockerfile invokes uvicorn with `--ws websockets-sansio` to avoid the
deprecated legacy `websockets` API, whose receive loop wraps
`asyncio.shield(self.transfer_data_task)` and leaks
`"exception in shielded future"` records on peer close. This test fails
loudly if a future uvicorn version renames or removes the sansio impl key.
See `docs/references/uvicorn-settings.md` § "ws (WebSocket protocol
implementation)".
"""

import uvicorn
from uvicorn.protocols.websockets.websockets_sansio_impl import (
    WebSocketsSansIOProtocol,
)


async def _noop_app(scope, receive, send):  # pragma: no cover
    """Minimal ASGI app, only needed to satisfy `uvicorn.Config`."""


def test_websockets_sansio_resolves_to_modern_protocol():
    """`ws='websockets-sansio'` must select the sans-I/O server protocol class."""
    config = uvicorn.Config(_noop_app, ws="websockets-sansio")
    config.load()
    assert config.ws_protocol_class is WebSocketsSansIOProtocol


def test_websockets_sansio_does_not_import_legacy():
    """The sansio impl must not pull in the deprecated `websockets.legacy` tree.

    The legacy module is what produces the shielded-future leak. If a future
    refactor of the sansio impl reaches into `websockets.legacy.*`, this
    test catches it before deploy.
    """
    import inspect

    from uvicorn.protocols.websockets import websockets_sansio_impl

    source = inspect.getsource(websockets_sansio_impl)
    assert "websockets.legacy" not in source
