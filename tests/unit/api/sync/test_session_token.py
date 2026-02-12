"""Tests for _get_session_token helper function."""

from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi import HTTPException

from routers.api.sync.routes import _get_session_token
from tests.unit.api.sync.conftest import TEST_SESSION_UUID


class TestGetSessionToken:
    """Tests for _get_session_token helper function."""

    def test_valid_uuid_string_returns_uuid(self):
        """Valid UUID string in request.state returns UUID object."""
        mock_request = Mock()
        mock_request.state.session_token = str(TEST_SESSION_UUID)

        result = _get_session_token(mock_request)

        assert result == TEST_SESSION_UUID
        assert isinstance(result, UUID)

    def test_missing_session_token_raises_403(self):
        """Missing or None session_token raises 403."""
        mock_request = Mock()
        mock_request.state = Mock(spec=[])  # No session_token attribute

        with pytest.raises(HTTPException) as exc_info:
            _get_session_token(mock_request)

        assert exc_info.value.status_code == 403
        assert "Session required" in exc_info.value.detail

    def test_invalid_uuid_string_raises_403(self):
        """Invalid UUID string raises 403."""
        mock_request = Mock()
        mock_request.state.session_token = "not-a-valid-uuid"

        with pytest.raises(HTTPException) as exc_info:
            _get_session_token(mock_request)

        assert exc_info.value.status_code == 403
        assert "Invalid session token" in exc_info.value.detail
