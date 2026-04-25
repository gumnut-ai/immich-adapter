"""Streaming upload pipeline for forwarding large files to photos-api.

Uses a three-thread pipeline to stream multipart uploads without buffering
the entire file to disk or memory:
- Event loop thread: reads request.stream() and dispatches chunks
- Parser thread: runs python-multipart, pushes file data to pipe
- Upload thread: runs sync httpx POST, reads file data from pipe

iOS live photo .MOV detection is skipped here because it requires file seeks
(random access), which is incompatible with streaming. Live photo videos are
always small (1-5MB), well below the default 100MB threshold, so they take
the buffered path where the check runs.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections.abc import Callable
from typing import IO, Any, cast

import httpx
import sentry_sdk
from fastapi import HTTPException, Request, status

from routers.utils.error_mapping import log_upstream_response
from routers.utils.gumnut_client import set_refreshed_token
from services.streaming_form_parser import StreamingFormParser
from services.streaming_pipe import StreamingPipe

logger = logging.getLogger(__name__)

# --- Shared httpx client (lazy singleton) ---

_streaming_http_client: httpx.Client | None = None
_streaming_client_lock = threading.Lock()


def _get_streaming_http_client() -> httpx.Client:
    global _streaming_http_client
    if _streaming_http_client is None:
        with _streaming_client_lock:
            if _streaming_http_client is None:
                _streaming_http_client = httpx.Client(
                    timeout=httpx.Timeout(
                        connect=30.0, read=600.0, write=600.0, pool=30.0
                    ),
                    limits=httpx.Limits(
                        max_connections=10, max_keepalive_connections=5
                    ),
                )
    return _streaming_http_client


def close_streaming_http_client() -> None:
    """Close the shared streaming httpx client. Call on application shutdown."""
    global _streaming_http_client
    with _streaming_client_lock:
        if _streaming_http_client is not None:
            _streaming_http_client.close()
            _streaming_http_client = None


class StreamingUploadPipeline:
    """Coordinates the three-thread streaming upload pipeline.

    Usage:
        pipeline = StreamingUploadPipeline(request, api_base_url, jwt_token)
        result = await pipeline.execute(extract_fields_fn)
        # result is the JSON dict from photos-api
        # pipeline.last_status_code has the HTTP status (200=duplicate, 201=created)
        # pipeline.refreshed_token has the JWT if photos-api refreshed it
    """

    def __init__(
        self,
        request: Request,
        api_base_url: str,
        jwt_token: str,
    ) -> None:
        self._request = request
        self._api_base_url = api_base_url
        self._jwt_token = jwt_token

        self._pipe = StreamingPipe(maxsize=64)
        self._form_parser = StreamingFormParser(self._pipe)
        self._parser = self._form_parser.create_parser(
            request.headers.get("content-type", "")
        )
        self._chunk_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
        self._parse_error: BaseException | None = None

        # Populated after successful upload
        self.refreshed_token: str | None = None
        self.last_status_code: int | None = None

    @property
    def form_parser(self) -> StreamingFormParser:
        return self._form_parser

    # --- Thread workers ---

    def _run_parser(self) -> None:
        """Run the multipart parser, reading chunks from the queue."""
        try:
            while True:
                try:
                    chunk = self._chunk_queue.get(timeout=300)
                except queue.Empty:
                    raise TimeoutError("Timed out waiting for request body chunks")
                if chunk is None:
                    break
                self._parser.write(chunk)
            self._parser.finalize()
            self._form_parser.mark_finalized()
        except Exception as e:
            self._parse_error = e
            self._pipe.set_error(e)
            self._form_parser.headers_ready.set()
            raise

    def _put_chunk_blocking(self, chunk_data: bytes | None) -> None:
        """Put a chunk into the queue with 1s timeout loop.

        Checks parse_error between attempts so a failed parser thread is
        detected quickly rather than blocking a thread pool worker forever.
        """
        elapsed = 0.0
        while True:
            if self._parse_error is not None:
                raise self._parse_error
            try:
                self._chunk_queue.put(chunk_data, timeout=1.0)
                return
            except queue.Full:
                elapsed += 1.0
                if elapsed >= 300:
                    raise TimeoutError("chunk_queue stalled — full for 300s")

    def _drain_and_signal_parser_exit(self) -> None:
        """Drain chunk_queue and send sentinel to unblock _run_parser."""
        try:
            while True:
                self._chunk_queue.get_nowait()
        except queue.Empty:
            pass
        for _ in range(10):
            try:
                self._chunk_queue.put(None, timeout=0.1)
                return
            except queue.Full:
                try:
                    self._chunk_queue.get_nowait()
                except queue.Empty:
                    pass

    async def _feed_chunks(self) -> None:
        """Read request body and enqueue chunks for the parser thread."""
        try:
            async for chunk in self._request.stream():
                await asyncio.to_thread(self._put_chunk_blocking, chunk)
            await asyncio.to_thread(self._put_chunk_blocking, None)
        except asyncio.CancelledError:
            # Unblock parser thread on cancellation; pipe error is already
            # set by the caller before cancel.
            self._drain_and_signal_parser_exit()
            raise
        except Exception as e:
            if self._parse_error is None:
                self._parse_error = e
            self._pipe.set_error(e)
            self._drain_and_signal_parser_exit()
            raise

    def _sync_upload(
        self,
        extract_fields_fn: Callable[[dict[str, str]], Any],
    ) -> dict[str, Any]:
        """Run the sync httpx POST to photos-api.

        Args:
            extract_fields_fn: Callable that extracts upload fields from
                form_parser.form_fields. Passed as argument to avoid
                circular imports with assets.py.
        """
        if not self._form_parser.headers_ready.wait(timeout=30):
            raise TimeoutError("Timed out waiting for file part headers")

        if self._parse_error is not None:
            raise self._parse_error

        filename = self._form_parser.filename
        content_type = self._form_parser.content_type
        if not filename or not content_type:
            raise ValueError("Missing file part 'assetData'")

        device_asset_id, device_id, file_created_at, file_modified_at = (
            extract_fields_fn(self._form_parser.form_fields)
        )

        content_length = self._request.headers.get("content-length", "unknown")
        logger.info(
            "Streaming %s to photos-api (%s bytes)",
            filename,
            content_length,
            extra={
                "upload_filename": filename,
                "content_type": content_type,
                "content_length": content_length,
                "device_asset_id": device_asset_id,
            },
        )

        # Bypasses the Gumnut SDK because the pipe-backed body is non-replayable,
        # making SDK-level retry impossible. Token refresh is captured from the
        # response headers after the POST completes, but if the JWT expires during
        # a multi-minute upload, the request will fail with no recovery path.
        try:
            response = _get_streaming_http_client().post(
                f"{self._api_base_url}/api/assets",
                headers={"Authorization": f"Bearer {self._jwt_token}"},
                files={
                    "asset_data": (
                        filename,
                        cast(IO[bytes], self._pipe),
                        content_type,
                    ),
                },
                data={
                    "device_asset_id": device_asset_id,
                    "device_id": device_id,
                    "file_created_at": file_created_at.isoformat(),
                    "file_modified_at": file_modified_at.isoformat(),
                },
            )
        except httpx.TimeoutException as e:
            raise TimeoutError("Upstream upload timed out") from e
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Upload failed",
            ) from e

        detail: str | None = None
        if response.status_code in (200, 201):
            logger.info(
                "photos-api responded %d for %s",
                response.status_code,
                filename,
                extra={
                    "status_code": response.status_code,
                    "upload_filename": filename,
                },
            )
        else:
            try:
                body = response.json()
                detail = str(body.get("detail", response.text))
            except Exception:
                detail = response.text
            log_upstream_response(
                logger,
                context="streaming_upload",
                status_code=response.status_code,
                message=f"photos-api upload error for {filename}",
                extra={
                    "upload_filename": filename,
                    "error_detail": detail[:500],
                },
            )

        if response.status_code == 429:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Service temporarily unavailable",
            )

        if response.status_code not in (200, 201):
            if detail is None:
                detail = response.text
            # Map upstream 5xx and 401 to 502: a 401 from photos-api means
            # the adapter's internal JWT expired, not the client's session.
            # Forwarding 401 would cause Immich clients to clear their session.
            client_status = (
                status.HTTP_502_BAD_GATEWAY
                if response.status_code >= 500 or response.status_code == 401
                else response.status_code
            )
            raise HTTPException(
                status_code=client_status,
                detail="Upload failed",
            )

        # Capture refreshed token for propagation to event loop
        new_token = response.headers.get("x-new-access-token")
        if new_token:
            self.refreshed_token = new_token

        self.last_status_code = response.status_code
        return response.json()

    # --- Main orchestration ---

    async def execute(
        self, extract_fields_fn: Callable[[dict[str, str]], Any]
    ) -> dict[str, Any]:
        """Run the full streaming upload pipeline.

        Args:
            extract_fields_fn: Callable that extracts UploadFields from a
                dict of form field strings. Passed to avoid circular imports.

        Returns:
            The JSON response dict from photos-api.

        Raises:
            HTTPException: On auth, validation, timeout, or upstream errors.
        """
        feed_task: asyncio.Task[None] | None = None
        parser_future: asyncio.Future[None] | None = None
        try:
            with sentry_sdk.start_span(
                op="http.client", name="gumnut.assets.create.streaming"
            ) as span:
                span.set_data("upload.strategy", "streaming")

                feed_task = asyncio.create_task(self._feed_chunks())
                parser_future = asyncio.get_running_loop().run_in_executor(
                    None, self._run_parser
                )
                result = await asyncio.to_thread(self._sync_upload, extract_fields_fn)

                # Propagate refreshed token to event loop context (nonlocal
                # from _sync_upload since ContextVar copies don't propagate)
                if self.refreshed_token:
                    set_refreshed_token(self.refreshed_token)

                # Wait for feed + parser to complete
                try:
                    await feed_task
                    await parser_future
                except Exception:
                    if self._parse_error is not None:
                        logger.error(
                            "Parser error after upload completed",
                            extra={"error": str(self._parse_error)},
                            exc_info=True,
                        )

                span.set_data("upload.filename", self._form_parser.filename)
                span.set_data("upload.content_type", self._form_parser.content_type)

            return result

        finally:
            if self._parse_error is not None:
                self._pipe.set_error(self._parse_error)

            if feed_task is not None and not feed_task.done():
                if not self._pipe.has_error:
                    self._pipe.set_error(Exception("Upload aborted"))
                feed_task.cancel()
                try:
                    await feed_task
                except (asyncio.CancelledError, Exception):
                    pass

            self._drain_and_signal_parser_exit()

            if parser_future is not None:
                try:
                    await asyncio.wait_for(parser_future, timeout=5)
                except (asyncio.TimeoutError, Exception):
                    pass
