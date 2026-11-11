"""Limits on the buffered upload path, parsed from a real request body."""

import weakref
from tempfile import SpooledTemporaryFile
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.requests import Request

from routers.api.assets import upload_asset
from routers.utils.gumnut_id_conversion import uuid_to_gumnut_asset_id
from services.streaming_form_parser import MAX_FIELD_BYTES, MAX_FORM_FIELDS
from tests.conftest import IMMICH_GO_UPLOAD_FIELDS

pytestmark = pytest.mark.anyio

_BOUNDARY = "----TestBoundary123"
_ASSET_UUID = UUID("00000000-0000-4000-8000-000000000001")
_ASSET = ("assetData", "IMG_0001.JPG", b"fake image data")
_SIDECAR = ("sidecarData", "IMG_0001.JPG.xmp", b"<x:xmpmeta/>")


def _body(fields: list[tuple[str, str]], files: list[tuple[str, str, bytes]]) -> bytes:
    """Build a multipart body: fields, then files, in immich-go's order."""
    out = b""
    for name, value in fields:
        out += (
            f"--{_BOUNDARY}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()
    for name, filename, data in files:
        out += (
            (
                f"--{_BOUNDARY}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + data
            + b"\r\n"
        )
    return out + f"--{_BOUNDARY}--\r\n".encode()


def _request(body: bytes, with_length: bool) -> Request:
    """A request carrying `body`; immich-go sends it without a Content-Length."""
    headers = [(b"content-type", f"multipart/form-data; boundary={_BOUNDARY}".encode())]
    if with_length:
        headers.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/assets",
        "headers": headers,
        "app": Mock(),  # Starlette maps form errors to 400 only inside an app.
    }
    chunks = [body[i : i + 1024] for i in range(0, len(body), 1024)]
    messages = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]

    async def receive():
        return messages.pop(0)

    return Request(scope, receive)


async def _upload(body: bytes, mock_current_user, with_length: bool = False) -> Mock:
    """Run the buffered upload and return the SDK create mock."""
    asset = Mock()
    asset.id = uuid_to_gumnut_asset_id(_ASSET_UUID)
    raw_response = Mock()
    raw_response.status_code = 200  # duplicate: no event emission needed
    raw_response.parse = AsyncMock(return_value=asset)
    client = Mock()
    client.assets.with_raw_response.create = AsyncMock(return_value=raw_response)
    settings = Mock()
    settings.streaming_upload_threshold_bytes = 100 * 1024 * 1024
    settings.gumnut_api_base_url = "http://localhost:8000"

    response = await upload_asset(
        request=_request(body, with_length),
        client=client,
        current_user=mock_current_user,
        settings=settings,
        library_id=None,
    )
    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    return client.assets.with_raw_response.create


class TestFormLimits:
    async def test_accepts_immich_go_upload_with_sidecar(self, mock_current_user):
        body = _body(IMMICH_GO_UPLOAD_FIELDS, [_ASSET, _SIDECAR])

        create = await _upload(body, mock_current_user)

        create.assert_awaited_once()
        assert create.call_args.kwargs["asset_data"][0] == "IMG_0001.JPG"

    async def test_rejects_third_file_part(self, mock_current_user):
        extra = ("extraData", "extra.bin", b"x")
        body = _body(IMMICH_GO_UPLOAD_FIELDS, [_ASSET, _SIDECAR, extra])

        with pytest.raises(HTTPException) as exc_info:
            await _upload(body, mock_current_user)

        assert exc_info.value.status_code == 400
        assert "files" in exc_info.value.detail

    @staticmethod
    def _fields(count: int) -> list[tuple[str, str]]:
        padding = count - len(IMMICH_GO_UPLOAD_FIELDS)
        return IMMICH_GO_UPLOAD_FIELDS + [(f"f{i}", "v") for i in range(padding)]

    async def test_accepts_field_count_at_limit(self, mock_current_user):
        body = _body(self._fields(MAX_FORM_FIELDS), [_ASSET])

        create = await _upload(body, mock_current_user)

        create.assert_awaited_once()

    async def test_rejects_field_count_over_limit(self, mock_current_user):
        body = _body(self._fields(MAX_FORM_FIELDS + 1), [_ASSET])

        with pytest.raises(HTTPException) as exc_info:
            await _upload(body, mock_current_user)

        assert exc_info.value.status_code == 400
        assert "fields" in exc_info.value.detail

    async def test_accepts_field_at_size_limit(self, mock_current_user):
        fields = IMMICH_GO_UPLOAD_FIELDS + [("big", "x" * MAX_FIELD_BYTES)]

        create = await _upload(_body(fields, [_ASSET]), mock_current_user)

        create.assert_awaited_once()

    async def test_rejects_field_over_size_limit(self, mock_current_user):
        fields = IMMICH_GO_UPLOAD_FIELDS + [("big", "x" * (MAX_FIELD_BYTES + 1))]

        with pytest.raises(HTTPException) as exc_info:
            await _upload(_body(fields, [_ASSET]), mock_current_user)

        assert exc_info.value.status_code == 400


_LIMIT_TARGET = "routers.api.assets._UPLOAD_WITHOUT_LENGTH_MAX_BYTES"


class TestBodyWithoutLengthLimit:
    _BODY = _body(IMMICH_GO_UPLOAD_FIELDS, [_ASSET, _SIDECAR])

    async def test_accepts_body_at_limit(self, mock_current_user, monkeypatch):
        monkeypatch.setattr(_LIMIT_TARGET, len(self._BODY))

        create = await _upload(self._BODY, mock_current_user)

        create.assert_awaited_once()

    async def test_rejects_body_over_limit(self, mock_current_user, monkeypatch):
        monkeypatch.setattr(_LIMIT_TARGET, len(self._BODY) - 1)

        with pytest.raises(HTTPException) as exc_info:
            await _upload(self._BODY, mock_current_user)

        assert exc_info.value.status_code == 413
        assert "Content-Length" in exc_info.value.detail

    async def test_ignores_body_with_content_length(
        self, mock_current_user, monkeypatch
    ):
        # A declared length is bounded by the streaming threshold instead.
        monkeypatch.setattr(_LIMIT_TARGET, len(self._BODY) - 1)

        create = await _upload(self._BODY, mock_current_user, with_length=True)

        create.assert_awaited_once()

    async def test_rejected_body_releases_spooled_file(
        self, mock_current_user, monkeypatch
    ):
        opened: list[weakref.ref[SpooledTemporaryFile]] = []

        class TrackedSpooledFile(SpooledTemporaryFile):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                opened.append(weakref.ref(self))

        monkeypatch.setattr(
            "starlette.formparsers.SpooledTemporaryFile", TrackedSpooledFile
        )
        # Roll the file over to disk, as a large upload would.
        monkeypatch.setattr("starlette.formparsers.MultiPartParser.spool_max_size", 8)
        body = _body(IMMICH_GO_UPLOAD_FIELDS, [("assetData", "big.jpg", b"x" * 4096)])
        monkeypatch.setattr(_LIMIT_TARGET, len(body) - 1)

        # Plain try/except: pytest.raises would keep the traceback, and with it
        # the parser's files, alive.
        rejected = False
        try:
            await _upload(body, mock_current_user)
        except HTTPException as exc:
            rejected = exc.status_code == 413
        assert rejected

        assert opened
        # Starlette closes spooled files only on its own parse errors; this
        # one is released by reference counting, which closes it.
        assert all((f := ref()) is None or f.closed for ref in opened)
