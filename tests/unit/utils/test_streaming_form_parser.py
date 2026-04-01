"""Tests for the StreamingFormParser utility."""

import pytest

from utils.streaming_form_parser import StreamingFormParser
from utils.streaming_pipe import StreamingPipe


def _build_multipart_body(
    fields: dict[str, str],
    filename: str = "test.jpg",
    content_type: str = "image/jpeg",
    file_data: bytes = b"fake image data",
    boundary: str = "----TestBoundary123",
) -> tuple[bytes, str]:
    """Build a multipart/form-data body for testing.

    Returns (body_bytes, content_type_header).
    """
    parts = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n'
            f"\r\n"
            f"{value}\r\n"
        )

    # File part
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="assetData"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n"
        f"\r\n"
    )

    body = "".join(parts).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()
    ct_header = f"multipart/form-data; boundary={boundary}"
    return body, ct_header


class TestStreamingFormParser:
    def test_parses_form_fields_and_file(self):
        """Test that form fields and file data are correctly parsed."""
        pipe = StreamingPipe(maxsize=64)
        parser_handler = StreamingFormParser(pipe)

        fields = {
            "deviceAssetId": "device-123",
            "deviceId": "device-456",
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

    def test_headers_ready_event_fires(self):
        """Test that headers_ready is set when file part headers are parsed."""
        pipe = StreamingPipe(maxsize=64)
        parser_handler = StreamingFormParser(pipe)

        assert not parser_handler.headers_ready.is_set()

        fields = {
            "deviceAssetId": "dev-123",
            "deviceId": "dev-456",
            "fileCreatedAt": "2023-01-01T12:00:00Z",
        }
        body, ct_header = _build_multipart_body(fields)
        parser = parser_handler.create_parser(ct_header)
        parser.write(body)
        parser.finalize()

        assert parser_handler.headers_ready.is_set()
        assert parser_handler.filename == "test.jpg"

    def test_rejects_multiple_file_parts(self):
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

    def test_rejects_file_before_required_fields(self):
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

    def test_field_size_limit(self):
        """Test that oversized form fields are rejected."""
        pipe = StreamingPipe(maxsize=64)
        parser_handler = StreamingFormParser(pipe)

        # Create a field larger than 64KB
        big_value = "x" * (65 * 1024)
        body, ct_header = _build_multipart_body({"bigfield": big_value})
        parser = parser_handler.create_parser(ct_header)

        with pytest.raises(ValueError, match="exceeds .* byte limit"):
            parser.write(body)

    def test_missing_boundary_raises(self):
        """Test that missing boundary raises ValueError."""
        pipe = StreamingPipe(maxsize=64)
        parser_handler = StreamingFormParser(pipe)

        with pytest.raises(ValueError, match="Missing multipart boundary"):
            parser_handler.create_parser("multipart/form-data")

    def test_mark_finalized_missing_file_part(self):
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

    def test_rejects_wrong_file_field_name(self):
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

    def test_chunked_parsing(self):
        """Test that parsing works when body arrives in small chunks."""
        pipe = StreamingPipe(maxsize=64)
        parser_handler = StreamingFormParser(pipe)

        fields = {
            "deviceAssetId": "dev-123",
            "deviceId": "dev-456",
            "fileCreatedAt": "2023-01-01T12:00:00Z",
        }
        file_data = b"A" * 1000
        body, ct_header = _build_multipart_body(fields, file_data=file_data)

        parser = parser_handler.create_parser(ct_header)

        # Feed in 100-byte chunks
        for i in range(0, len(body), 100):
            parser.write(body[i : i + 100])
        parser.finalize()

        assert parser_handler.form_fields["deviceAssetId"] == "dev-123"
        assert parser_handler.filename == "test.jpg"

        # Read all file data from pipe
        received = b""
        while True:
            chunk = pipe.read(1024)
            if not chunk:
                break
            received += chunk
        assert received == file_data
