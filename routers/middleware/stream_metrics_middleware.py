"""Count completed ASGI streams independently of trace sampling."""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from utils import metrics


class StreamMetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 0
        complete = False

        async def observed_send(message: Message) -> None:
            nonlocal status, complete
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                complete = True

        try:
            await self.app(scope, receive, observed_send)
        finally:
            # FastAPI supplies the matched route template after routing. Never
            # emit the raw path, query, client headers or request identity.
            route = getattr(scope.get("route"), "path", None)
            if 200 <= status < 300:
                if scope["method"] == "GET" and route == "/api/assets/{id}/thumbnail":
                    metrics.count(
                        "thumbnail.delivery",
                        1,
                        attributes={
                            "stream.outcome": "complete" if complete else "abort"
                        },
                    )
                elif scope["method"] == "POST" and route == "/api/sync/stream":
                    state = scope.get("state", {})
                    outcome = "abort"
                    if complete:
                        if state.get("sync_stream_complete"):
                            outcome = "complete"
                        elif state.get("sync_stream_reset"):
                            outcome = "reset"
                    metrics.count(
                        "sync.stream", 1, attributes={"stream.outcome": outcome}
                    )
