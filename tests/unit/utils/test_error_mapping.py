"""
Tests for error mapping utilities.
"""

import logging

import httpx
import pytest
from fastapi import HTTPException
from gumnut import (
    APIResponseValidationError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
)

import routers.utils.error_mapping as error_mapping_module
from routers.utils.error_mapping import (
    ERROR_DETAIL_MAX_CHARS,
    log_bulk_status_error,
    log_upstream_response,
    map_gumnut_error,
    upstream_status_log_level,
)
from tests.conftest import make_sdk_connection_error, make_sdk_status_error


class TestUpstreamStatusLogLevel:
    """Test centralized status -> log level policy for upstream responses."""

    @pytest.mark.parametrize(
        ("status_code", "expected_level"),
        [
            (400, logging.WARNING),
            (401, logging.WARNING),
            (403, logging.WARNING),
            (404, logging.INFO),
            (422, logging.WARNING),
            (429, logging.WARNING),
            (500, logging.ERROR),
            (503, logging.ERROR),
        ],
    )
    def test_upstream_status_log_level_policy(self, status_code, expected_level):
        assert upstream_status_log_level(status_code) == expected_level


class TestLogUpstreamResponse:
    """Test shared upstream logging helper behavior."""

    def test_helper_fields_override_conflicting_extra(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        caplog.set_level(logging.INFO, logger="routers.utils.error_mapping")

        log_upstream_response(
            error_mapping_module.logger,
            context="authoritative-context",
            status_code=404,
            message="upstream response",
            extra={
                "context": "caller-context",
                "status_code": 999,
                "custom_field": "kept",
            },
        )

        matching_records = [
            record
            for record in caplog.records
            if record.getMessage() == "upstream response"
        ]
        assert matching_records

        record = matching_records[-1]
        assert getattr(record, "context", None) == "authoritative-context"
        assert getattr(record, "status_code", None) == 404
        assert getattr(record, "custom_field", None) == "kept"

    def test_helper_propagates_exc_info(self, caplog: pytest.LogCaptureFixture):
        caplog.set_level(logging.INFO, logger="routers.utils.error_mapping")

        try:
            raise ValueError("boom")
        except ValueError:
            log_upstream_response(
                error_mapping_module.logger,
                context="ctx",
                status_code=500,
                message="upstream traceback",
                exc_info=True,
            )

        matching_records = [
            record
            for record in caplog.records
            if record.getMessage() == "upstream traceback"
        ]
        assert matching_records

        record = matching_records[-1]
        assert record.exc_info is not None
        assert record.exc_info[0] is ValueError


class TestMapGumnutError:
    """Test the map_gumnut_error function for typed SDK exceptions."""

    @pytest.mark.parametrize(
        ("cls", "status_code"),
        [
            (NotFoundError, 404),
            (AuthenticationError, 401),
            (PermissionDeniedError, 403),
            (BadRequestError, 400),
        ],
    )
    def test_typed_status_errors_map_to_their_status(self, cls, status_code):
        err = make_sdk_status_error(status_code, "upstream said no", cls=cls)
        result = map_gumnut_error(err, "Failed to fetch resource")

        assert isinstance(result, HTTPException)
        assert result.status_code == status_code
        # No body → falls back to e.message
        assert result.detail == "upstream said no"

    def test_extracts_detail_from_body_dict(self):
        err = make_sdk_status_error(
            401,
            "raw error",
            body={"detail": "JWT has expired"},
            cls=AuthenticationError,
        )
        result = map_gumnut_error(err, "Failed to fetch user details")

        assert result.status_code == 401
        assert result.detail == "JWT has expired"

    def test_extracts_message_from_body_when_no_detail(self):
        err = make_sdk_status_error(
            404,
            "raw",
            body={"message": "Asset not found"},
            cls=NotFoundError,
        )
        result = map_gumnut_error(err, "Failed to fetch asset")

        assert result.status_code == 404
        assert result.detail == "Asset not found"

    def test_extracts_error_from_body_when_no_detail_or_message(self):
        err = make_sdk_status_error(
            403,
            "raw",
            body={"error": "Access denied"},
            cls=PermissionDeniedError,
        )
        result = map_gumnut_error(err, "Failed to access resource")

        assert result.status_code == 403
        assert result.detail == "Access denied"

    def test_falls_back_to_message_when_body_not_dict(self):
        err = make_sdk_status_error(
            500,
            "Plain error message",
            body="not a dict",
        )
        result = map_gumnut_error(err, "Failed to process")

        assert result.status_code == 500
        assert result.detail == "Plain error message"

    @pytest.mark.parametrize(
        ("status_code", "expected_level"),
        [
            (401, logging.WARNING),
            (403, logging.WARNING),
            (404, logging.INFO),
            (422, logging.WARNING),
            (429, logging.WARNING),
            (500, logging.ERROR),
        ],
    )
    def test_logs_with_status_policy(
        self,
        caplog: pytest.LogCaptureFixture,
        status_code: int,
        expected_level: int,
    ):
        """Status-based log level should follow upstream response policy."""
        caplog.set_level(logging.INFO, logger="routers.utils.error_mapping")

        result = map_gumnut_error(
            make_sdk_status_error(status_code, "Upstream request failed"),
            "Failed to upload asset",
        )

        assert result.status_code == status_code

        status_records = [
            record
            for record in caplog.records
            if getattr(record, "status_code", None) == status_code
        ]
        assert status_records
        assert status_records[-1].levelno == expected_level

    def test_propagates_extra_to_log_record(self, caplog: pytest.LogCaptureFixture):
        """Caller-supplied extra fields should land on the upstream log record."""
        caplog.set_level(logging.INFO, logger="routers.utils.error_mapping")

        map_gumnut_error(
            make_sdk_status_error(500, "Upstream request failed"),
            "Failed to upload asset",
            extra={"upload_filename": "img.jpg", "device_asset_id": "abc"},
        )

        matching_records = [
            record
            for record in caplog.records
            if getattr(record, "upload_filename", None) == "img.jpg"
        ]
        assert matching_records
        assert getattr(matching_records[-1], "device_asset_id", None) == "abc"

    def test_rate_limit_error_maps_to_502_and_logs_warning(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """RateLimitError should be mapped to 502 and logged at WARNING."""

        # Patch the imported reference so isinstance() inside map_gumnut_error
        # matches our fake class (otherwise we'd need the real RateLimitError
        # constructor, which requires a real httpx.Response).
        class FakeRateLimitError(Exception):
            pass

        monkeypatch.setattr(error_mapping_module, "RateLimitError", FakeRateLimitError)
        caplog.set_level(logging.INFO, logger="routers.utils.error_mapping")

        result = error_mapping_module.map_gumnut_error(
            FakeRateLimitError("429 Too many requests"),
            "Failed to upload asset",
        )

        assert result.status_code == 502
        assert (
            result.detail == "Failed to upload asset: Upstream temporarily unavailable"
        )

        matching_records = [
            record
            for record in caplog.records
            if "rate-limited request" in record.getMessage()
        ]
        assert matching_records
        assert matching_records[-1].levelno == logging.WARNING

    def test_non_sdk_exception_maps_to_500(self, caplog: pytest.LogCaptureFixture):
        """A plain Exception (e.g. programmer error) should map to 500."""
        caplog.set_level(logging.INFO, logger="routers.utils.error_mapping")

        result = map_gumnut_error(
            ValueError("Some unknown error"),
            "Failed to process",
        )

        assert isinstance(result, HTTPException)
        assert result.status_code == 500
        assert "Some unknown error" in result.detail

    def test_helper_fields_override_caller_extra_in_map_gumnut_error(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        """Caller `extra` cannot override authoritative context/status_code."""
        caplog.set_level(logging.INFO, logger="routers.utils.error_mapping")

        map_gumnut_error(
            make_sdk_status_error(404, "Not found", cls=NotFoundError),
            "Failed to fetch resource",
            extra={"context": "spoof", "status_code": 999, "custom_field": "kept"},
        )

        records = [
            r
            for r in caplog.records
            if getattr(r, "context", None) == "Failed to fetch resource"
        ]
        assert records
        assert getattr(records[-1], "status_code", None) == 404
        assert getattr(records[-1], "custom_field", None) == "kept"

    def test_response_validation_error_maps_to_502_and_logs_error(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        """APIResponseValidationError → 502 ERROR, regardless of upstream status."""
        caplog.set_level(logging.INFO, logger="routers.utils.error_mapping")

        request = httpx.Request("GET", "http://test.local/")
        # Schema mismatch on a 200 — the upstream HTTP status is fine; the bug
        # is in the body. Severity must come from the 502 we surface, not 200.
        response = httpx.Response(200, request=request)
        err = APIResponseValidationError(response, body=None, message="bad schema")

        result = map_gumnut_error(err, "Failed to upload asset")

        assert result.status_code == 502
        assert (
            result.detail
            == "Failed to upload asset: Upstream returned invalid response"
        )

        matching = [
            r
            for r in caplog.records
            if getattr(r, "context", None) == "Failed to upload asset"
        ]
        assert matching
        assert matching[-1].levelno == logging.ERROR
        assert getattr(matching[-1], "status_code", None) == 502

    def test_connection_error_maps_to_502_and_logs_error(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        """APIConnectionError → 502 ERROR with `Upstream unreachable` detail."""
        caplog.set_level(logging.INFO, logger="routers.utils.error_mapping")

        result = map_gumnut_error(
            make_sdk_connection_error(),
            "Failed to upload asset",
        )

        assert result.status_code == 502
        assert result.detail == "Failed to upload asset: Upstream unreachable"

        matching = [
            r
            for r in caplog.records
            if getattr(r, "context", None) == "Failed to upload asset"
        ]
        assert matching
        assert matching[-1].levelno == logging.ERROR


class TestLogBulkStatusError:
    """Test the per-item bulk-endpoint status-error log helper."""

    def test_uses_status_code_for_severity_and_includes_truncated_detail(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        """404 → INFO, error_detail folded into the record and truncated."""
        caplog.set_level(logging.INFO, logger="routers.utils.error_mapping")

        long_msg = "x" * (ERROR_DETAIL_MAX_CHARS + 50)
        log_bulk_status_error(
            error_mapping_module.logger,
            context="delete_assets",
            exc=make_sdk_status_error(404, long_msg, cls=NotFoundError),
            message="Asset not found during deletion",
            extra={"asset_id": "abc"},
        )

        matching = [
            r
            for r in caplog.records
            if r.getMessage() == "Asset not found during deletion"
        ]
        assert matching
        record = matching[-1]
        assert record.levelno == logging.INFO
        assert getattr(record, "context", None) == "delete_assets"
        assert getattr(record, "status_code", None) == 404
        assert getattr(record, "asset_id", None) == "abc"
        assert len(getattr(record, "error_detail", "")) == ERROR_DETAIL_MAX_CHARS

    def test_500_status_logs_at_error(self, caplog: pytest.LogCaptureFixture):
        caplog.set_level(logging.INFO, logger="routers.utils.error_mapping")

        log_bulk_status_error(
            error_mapping_module.logger,
            context="add_assets_to_album",
            exc=make_sdk_status_error(500, "boom"),
            message="Failed to add asset",
        )

        matching = [
            r for r in caplog.records if r.getMessage() == "Failed to add asset"
        ]
        assert matching
        assert matching[-1].levelno == logging.ERROR
