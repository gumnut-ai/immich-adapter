"""Tests for the StreamingFormParser utility."""

from collections.abc import Mapping, Sequence

import pytest

from services.streaming_form_parser import (
    MAX_FIELD_BYTES,
    MAX_FORM_FIELDS,
    MAX_TOTAL_FIELD_BYTES,
    StreamingFormParser,
)
from services.streaming_pipe import StreamingPipe
from tests.conftest import IMMICH_GO_UPLOAD_FIELDS, MOBILE_UPLOAD_FIELDS

pytestmark = pytest.mark.anyio


def _build_multipart_body(
    fields: Mapping[str, str] | Sequence[tuple[str, str]],
    filename: str = "test.jpg",
    content_type: str = "image/jpeg",
    file_data: bytes = b"fake image data",
    boundary: str = "----TestBoundary123",
    after: Sequence[tuple[str, str]] = (),
) -> tuple[bytes, str]:
    """Build a multipart/form-data body for testing.

    ``fields`` precede the file part and may repeat a name when given as
    pairs; ``after`` follows it. Returns (body_bytes, content_type_header).
    """

    def field_part(name: str, value: str) -> str:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n'
            f"\r\n"
            f"{value}\r\n"
        )

    pairs = fields.items() if isinstance(fields, Mapping) else fields
    parts = [field_part(name, value) for name, value in pairs]

    # File part
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="assetData"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n"
        f"\r\n"
    )

    tail = "\r\n" + "".join(field_part(name, value) for name, value in after)
    body = "".join(parts).encode() + file_data + f"{tail}--{boundary}--\r\n".encode()
    ct_header = f"multipart/form-data; boundary={boundary}"
    return body, ct_header


class TestStreamingFormParser:
    async def test_parses_form_fields_and_file(self):
        """Test that form fields and file data are correctly parsed."""
        pipe = StreamingPipe(maxsize=64)
        parser_handler = StreamingFormParser(pipe)

        # Immich v3 no longer sends device fields; fileCreatedAt is the only
        # pre-file field the upload path requires.
        fields = {
            "fileCreatedAt": "2023-01-01T12:00:00Z",
        }
        file_data = b"fake image content here"
        body, ct_header = _build_multipart_body(fields, file_data=file_data)

        parser = parser_handler.create_parser(ct_header)
        parser.write(body)
        parser.finalize()

        # Form fields should be collected
        assert parser_handler.form_fields == fields
        assert parser_handler.filename == "test.jpg"
        assert parser_handler.content_type == "image/jpeg"

        # File data should be in the pipe
        result = pipe.read(1024)
        assert result == file_data
        # Pipe should be closed (EOF)
        assert pipe.read(1024) == b""

    async def test_headers_ready_event_fires(self):
        """Test that headers_ready is set when file part headers are parsed."""
        pipe = StreamingPipe(maxsize=64)
        parser_handler = StreamingFormParser(pipe)

        assert not parser_handler.headers_ready.is_set()

        fields = {
            "fileCreatedAt": "2023-01-01T12:00:00Z",
        }
        body, ct_header = _build_multipart_body(fields)
        parser = parser_handler.create_parser(ct_header)
        parser.write(body)
        parser.finalize()

        assert parser_handler.headers_ready.is_set()
        assert parser_handler.filename == "test.jpg"

    async def test_rejects_multiple_file_parts(self):
        """Test that multiple file parts raise an error."""
        pipe = StreamingPipe(maxsize=64)
        parser_handler = StreamingFormParser(pipe)

        boundary = "----TestBoundary123"
        # Include required fields before the first file part
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="deviceAssetId"\r\n'
            f"\r\n"
            f"dev-123\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="deviceId"\r\n'
            f"\r\n"
            f"dev-456\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="fileCreatedAt"\r\n'
            f"\r\n"
            f"2023-01-01T12:00:00Z\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="assetData"; filename="file1.jpg"\r\n'
            f"Content-Type: image/jpeg\r\n"
            f"\r\n"
            f"data1\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="assetData"; filename="file2.jpg"\r\n'
            f"Content-Type: image/jpeg\r\n"
            f"\r\n"
            f"data2\r\n"
            f"--{boundary}--\r\n"
        ).encode()

        parser = parser_handler.create_parser(
            f"multipart/form-data; boundary={boundary}"
        )

        with pytest.raises(ValueError, match="Multiple file parts"):
            parser.write(body)

    async def test_rejects_file_before_required_fields(self):
        """Test that file part before required fields raises an error."""
        pipe = StreamingPipe(maxsize=64)
        parser_handler = StreamingFormParser(pipe)

        boundary = "----TestBoundary123"
        # File part without preceding required fields
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="assetData"; filename="test.jpg"\r\n'
            f"Content-Type: image/jpeg\r\n"
            f"\r\n"
            f"data\r\n"
            f"--{boundary}--\r\n"
        ).encode()

        parser = parser_handler.create_parser(
            f"multipart/form-data; boundary={boundary}"
        )

        with pytest.raises(ValueError, match="Required fields must precede"):
            parser.write(body)

    async def test_field_size_limit(self):
        """Test that oversized form fields are rejected."""
        pipe = StreamingPipe(maxsize=64)
        parser_handler = StreamingFormParser(pipe)

        # Create a field larger than 64KB
        big_value = "x" * (65 * 1024)
        body, ct_header = _build_multipart_body({"bigfield": big_value})
        parser = parser_handler.create_parser(ct_header)

        with pytest.raises(ValueError, match="exceeds .* byte limit"):
            parser.write(body)

    async def test_missing_boundary_raises(self):
        """Test that missing boundary raises ValueError."""
        pipe = StreamingPipe(maxsize=64)
        parser_handler = StreamingFormParser(pipe)

        with pytest.raises(ValueError, match="Missing multipart boundary"):
            parser_handler.create_parser("multipart/form-data")

    async def test_mark_finalized_missing_file_part(self):
        """Test that mark_finalized sets error and wakes waiter when no file part seen."""
        pipe = StreamingPipe(maxsize=64)
        parser_handler = StreamingFormParser(pipe)

        boundary = "----TestBoundary123"
        # Body with only form fields, no file part
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="deviceAssetId"\r\n'
            f"\r\n"
            f"dev-123\r\n"
            f"--{boundary}--\r\n"
        ).encode()

        parser = parser_handler.create_parser(
            f"multipart/form-data; boundary={boundary}"
        )
        parser.write(body)
        parser.finalize()
        parser_handler.mark_finalized()

        # headers_ready should be set so the upload thread wakes immediately
        assert parser_handler.headers_ready.is_set()

        # Reading from the pipe should raise the error
        with pytest.raises(ValueError, match="Missing file part"):
            pipe.read(1)

    async def test_rejects_wrong_file_field_name(self):
        """Test that a file part with the wrong field name is rejected."""
        pipe = StreamingPipe(maxsize=64)
        parser_handler = StreamingFormParser(pipe)

        boundary = "----TestBoundary123"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="deviceAssetId"\r\n'
            f"\r\n"
            f"dev-123\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="deviceId"\r\n'
            f"\r\n"
            f"dev-456\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="fileCreatedAt"\r\n'
            f"\r\n"
            f"2023-01-01T12:00:00Z\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="wrongName"; filename="test.jpg"\r\n'
            f"Content-Type: image/jpeg\r\n"
            f"\r\n"
            f"data\r\n"
            f"--{boundary}--\r\n"
        ).encode()

        parser = parser_handler.create_parser(
            f"multipart/form-data; boundary={boundary}"
        )

        with pytest.raises(ValueError, match="File field name must be 'assetData'"):
            parser.write(body)

    async def test_chunked_parsing(self):
        """Test that parsing works when body arrives in small chunks."""
        pipe = StreamingPipe(maxsize=64)
        parser_handler = StreamingFormParser(pipe)

        fields = {
            "fileCreatedAt": "2023-01-01T12:00:00Z",
        }
        file_data = b"A" * 1000
        body, ct_header = _build_multipart_body(fields, file_data=file_data)

        parser = parser_handler.create_parser(ct_header)

        # Feed in 100-byte chunks
        for i in range(0, len(body), 100):
            parser.write(body[i : i + 100])
        parser.finalize()

        assert parser_handler.form_fields["fileCreatedAt"] == "2023-01-01T12:00:00Z"
        assert parser_handler.filename == "test.jpg"

        # Read all file data from pipe
        received = b""
        while True:
            chunk = pipe.read(1024)
            if not chunk:
                break
            received += chunk
        assert received == file_data


def _parse(
    fields: Sequence[tuple[str, str]], after: Sequence[tuple[str, str]] = ()
) -> StreamingFormParser:
    body, ct_header = _build_multipart_body(fields, after=after)
    parser_handler = StreamingFormParser(StreamingPipe(maxsize=64))
    parser = parser_handler.create_parser(ct_header)
    parser.write(body)
    parser.finalize()
    return parser_handler


_CREATED: tuple[str, str] = ("fileCreatedAt", "2023-01-01T12:00:00Z")


class TestStreamingFormParserLimits:
    @pytest.mark.parametrize("fields", [MOBILE_UPLOAD_FIELDS, IMMICH_GO_UPLOAD_FIELDS])
    async def test_accepts_client_field_sets(self, fields):
        parser_handler = _parse(fields)

        assert parser_handler.form_fields == dict(fields)

    async def test_keeps_field_after_file_part(self):
        """Immich web appends `visibility` after `assetData` for a locked upload."""
        parser_handler = _parse([_CREATED], after=[("visibility", "locked")])

        assert parser_handler.form_fields["visibility"] == "locked"

    async def test_accepts_field_count_at_limit(self):
        fields = [_CREATED] + [(f"f{i}", "v") for i in range(MAX_FORM_FIELDS - 1)]

        parser_handler = _parse(fields)

        assert len(parser_handler.form_fields) == MAX_FORM_FIELDS

    async def test_rejects_field_count_over_limit(self):
        fields = [_CREATED] + [(f"f{i}", "v") for i in range(MAX_FORM_FIELDS)]

        with pytest.raises(ValueError, match="more than .* fields"):
            _parse(fields)

    async def test_counts_fields_after_file_part(self):
        before = [_CREATED] + [(f"f{i}", "v") for i in range(MAX_FORM_FIELDS - 1)]

        with pytest.raises(ValueError, match="more than .* fields"):
            _parse(before, after=[("visibility", "locked")])

    async def test_counts_repeated_field_names(self):
        fields = [_CREATED] + [("repeated", "v")] * MAX_FORM_FIELDS

        with pytest.raises(ValueError, match="more than .* fields"):
            _parse(fields)

    @staticmethod
    def _fields_totalling(total: int) -> list[tuple[str, str]]:
        fields = [_CREATED]
        remaining = total - len(_CREATED[1])
        index = 0
        while remaining:
            size = min(remaining, MAX_FIELD_BYTES)
            fields.append((f"f{index}", "x" * size))
            remaining -= size
            index += 1
        return fields

    async def test_accepts_field_bytes_at_limit(self):
        parser_handler = _parse(self._fields_totalling(MAX_TOTAL_FIELD_BYTES))

        total = sum(len(value) for value in parser_handler.form_fields.values())
        assert total == MAX_TOTAL_FIELD_BYTES

    async def test_rejects_field_bytes_over_limit(self):
        with pytest.raises(ValueError, match="in total"):
            _parse(self._fields_totalling(MAX_TOTAL_FIELD_BYTES + 1))
