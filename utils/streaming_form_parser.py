"""Streaming multipart form parser that pipes file data to a StreamingPipe.

Parses multipart/form-data from the raw request stream using python-multipart's
callback-based MultipartParser. File data is piped to a StreamingPipe for
concurrent forwarding to photos-api. Form fields are collected into a dict.

This is a simplified version of photos-api's StreamingFormHandler, adapted for
the proxy use case (no checksums, no S3, just form fields + pipe).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from python_multipart.multipart import MultipartParser, parse_options_header

if TYPE_CHECKING:
    from python_multipart.multipart import MultipartCallbacks

from utils.streaming_pipe import StreamingPipe

logger = logging.getLogger(__name__)

MAX_FIELD_BYTES = 64 * 1024  # 64KB — cap for non-file form fields

# Fields that must be present before the file part starts.
_REQUIRED_FIELDS = {"deviceAssetId", "deviceId", "fileCreatedAt"}


class StreamingFormParser:
    """Parses multipart form data, streaming file content to a pipe.

    Usage:
        pipe = StreamingPipe()
        parser_handler = StreamingFormParser(pipe)
        mp_parser = parser_handler.create_parser(content_type)

        # In a thread (callbacks may block on pipe.put):
        for chunk in body_chunks:
            mp_parser.write(chunk)
        mp_parser.finalize()
    """

    def __init__(self, pipe: StreamingPipe) -> None:
        self._pipe = pipe

        # Parser state tracking
        self._current_header_name = b""
        self._current_header_value = b""
        self._current_headers: dict[str, str] = {}
        self._current_is_file = False
        self._current_field_name = ""
        self._current_field_data = bytearray()

        # File info
        self._filename: str | None = None
        self._content_type: str | None = None
        self._file_seen = False

        # Results
        self._form_fields: dict[str, str] = {}

        # Signaling: set when file part headers are parsed (filename + content_type
        # are available). The upload thread waits on this before starting the request.
        self._headers_ready = threading.Event()

    @property
    def form_fields(self) -> dict[str, str]:
        return self._form_fields

    @property
    def filename(self) -> str | None:
        return self._filename

    @property
    def content_type(self) -> str | None:
        return self._content_type

    @property
    def headers_ready(self) -> threading.Event:
        return self._headers_ready

    def create_parser(self, content_type: str) -> MultipartParser:
        """Create a MultipartParser wired to this handler's callbacks."""
        _, params = parse_options_header(content_type.encode())
        boundary = params.get(b"boundary", b"")
        if not boundary:
            raise ValueError("Missing multipart boundary in Content-Type header")

        callbacks: MultipartCallbacks = {
            "on_part_begin": self._on_part_begin,
            "on_part_data": self._on_part_data,
            "on_part_end": self._on_part_end,
            "on_header_field": self._on_header_field,
            "on_header_value": self._on_header_value,
            "on_header_end": self._on_header_end,
            "on_headers_finished": self._on_headers_finished,
        }

        return MultipartParser(boundary, callbacks)

    # --- Synchronous callbacks (called by MultipartParser.write) ---

    def _on_part_begin(self) -> None:
        self._current_headers = {}
        self._current_header_name = b""
        self._current_header_value = b""
        self._current_is_file = False
        self._current_field_name = ""
        self._current_field_data = bytearray()

    def _on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._current_header_name += data[start:end]

    def _on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._current_header_value += data[start:end]

    def _on_header_end(self) -> None:
        name = self._current_header_name.decode("latin-1").lower()
        value = self._current_header_value.decode("latin-1")
        self._current_headers[name] = value
        self._current_header_name = b""
        self._current_header_value = b""

    def _on_headers_finished(self) -> None:
        content_disposition = self._current_headers.get("content-disposition", "")
        _, params = parse_options_header(content_disposition)

        field_name = params.get(b"name", b"").decode("utf-8")
        filename = params.get(b"filename", b"").decode("utf-8")

        self._current_field_name = field_name

        if filename:
            if self._file_seen:
                raise ValueError("Multiple file parts are not supported")
            if field_name != "assetData":
                raise ValueError("File field name must be 'assetData'")
            # Verify required fields arrived before the file part
            missing = [k for k in _REQUIRED_FIELDS if k not in self._form_fields]
            if missing:
                raise ValueError(
                    "Required fields must precede file part in streaming mode: "
                    + ", ".join(sorted(missing))
                )
            # This is a file part
            self._current_is_file = True
            self._file_seen = True
            self._filename = filename
            self._content_type = self._current_headers.get(
                "content-type", "application/octet-stream"
            )
            # Signal that file info is available for the upload thread
            self._headers_ready.set()

    def _on_part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]

        if self._current_is_file:
            self._pipe.put(chunk)
        else:
            if len(self._current_field_data) + len(chunk) > MAX_FIELD_BYTES:
                raise ValueError(
                    f"Form field '{self._current_field_name}' exceeds {MAX_FIELD_BYTES} byte limit"
                )
            self._current_field_data.extend(chunk)

    def _on_part_end(self) -> None:
        if self._current_is_file:
            self._pipe.close_writer()
        else:
            self._form_fields[self._current_field_name] = (
                self._current_field_data.decode("utf-8")
            )

    def mark_finalized(self) -> None:
        """Call after parser.finalize() to handle missing file part.

        If no file part was seen, sets an error on the pipe and signals
        headers_ready so the upload thread fails immediately.
        """
        if not self._file_seen:
            error = ValueError("Missing file part 'assetData'")
            self._pipe.set_error(error)
            self._headers_ready.set()
