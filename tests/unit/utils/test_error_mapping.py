"""
Tests for error mapping utilities.
"""

import pytest
from fastapi import HTTPException

from routers.utils.error_mapping import check_for_error_by_code, map_gumnut_error


class TestCheckForErrorByCode:
    """Test the check_for_error_by_code function."""

    def test_check_error_with_status_code_attribute(self):
        """Test checking error when exception has status_code attribute."""

        class MockSDKError(Exception):
            def __init__(self, message, status_code):
                super().__init__(message)
                self.status_code = status_code

        # Test 404 error
        error_404 = MockSDKError("Not found", 404)
        assert check_for_error_by_code(error_404, 404) is True
        assert check_for_error_by_code(error_404, 401) is False
        assert check_for_error_by_code(error_404, 500) is False

        # Test 401 error
        error_401 = MockSDKError("Unauthorized", 401)
        assert check_for_error_by_code(error_401, 401) is True
        assert check_for_error_by_code(error_401, 404) is False
        assert check_for_error_by_code(error_401, 403) is False

        # Test 403 error
        error_403 = MockSDKError("Forbidden", 403)
        assert check_for_error_by_code(error_403, 403) is True
        assert check_for_error_by_code(error_403, 401) is False
        assert check_for_error_by_code(error_403, 404) is False

    def test_check_error_with_string_status_code(self):
        """Test checking error when status_code is a string."""

        class MockSDKError(Exception):
            def __init__(self, message, status_code):
                super().__init__(message)
                self.status_code = status_code

        # Test string status code
        error_str = MockSDKError("Not found", "404")
        assert check_for_error_by_code(error_str, 404) is True
        assert check_for_error_by_code(error_str, 401) is False

    def test_check_error_without_status_code_attribute(self):
        """Test checking error when exception doesn't have status_code attribute."""

        # Regular exception without status_code
        regular_error = Exception("Some error message")
        assert check_for_error_by_code(regular_error, 404) is False
        assert check_for_error_by_code(regular_error, 401) is False
        assert check_for_error_by_code(regular_error, 500) is False

        # ValueError without status_code
        value_error = ValueError("Invalid value")
        assert check_for_error_by_code(value_error, 400) is False

    def test_check_error_with_none_status_code(self):
        """Test checking error when status_code is None."""

        class MockSDKError(Exception):
            def __init__(self, message):
                super().__init__(message)
                self.status_code = None

        error_none = MockSDKError("Error with None status")
        # This should raise an error when trying to int(None)
        with pytest.raises((TypeError, ValueError)):
            check_for_error_by_code(error_none, 404)


class TestMapGumnutError:
    """Test the map_gumnut_error function."""

    def test_map_error_with_status_code_attribute(self):
        """Test mapping error when exception has status_code attribute."""

        class MockSDKError(Exception):
            def __init__(self, message, status_code):
                super().__init__(message)
                self.status_code = status_code

        # Test 404 error - context is not included, just the error message
        error_404 = MockSDKError("Resource not found", 404)
        result = map_gumnut_error(error_404, "Failed to fetch resource")
        assert isinstance(result, HTTPException)
        assert result.status_code == 404
        assert result.detail == "Resource not found"

        # Test 401 error
        error_401 = MockSDKError("Invalid credentials", 401)
        result = map_gumnut_error(error_401, "Failed to authenticate")
        assert isinstance(result, HTTPException)
        assert result.status_code == 401
        assert result.detail == "Invalid credentials"

        # Test 403 error
        error_403 = MockSDKError("Access denied", 403)
        result = map_gumnut_error(error_403, "Failed to access resource")
        assert isinstance(result, HTTPException)
        assert result.status_code == 403
        assert result.detail == "Access denied"

    def test_map_error_with_string_patterns(self):
        """Test mapping error using string pattern matching fallback."""

        # Test 404 string patterns
        error_404_1 = Exception("404 Not found")
        result = map_gumnut_error(error_404_1, "Failed to fetch")
        assert isinstance(result, HTTPException)
        assert result.status_code == 404
        assert result.detail == "Failed to fetch: Not found"

        error_404_2 = Exception("Resource Not found")
        result = map_gumnut_error(error_404_2, "Failed to fetch")
        assert isinstance(result, HTTPException)
        assert result.status_code == 404
        assert result.detail == "Failed to fetch: Not found"

        error_404_3 = Exception("asset not found")
        result = map_gumnut_error(error_404_3, "Failed to fetch")
        assert isinstance(result, HTTPException)
        assert result.status_code == 404
        assert result.detail == "Failed to fetch: Not found"

    def test_map_error_401_patterns(self):
        """Test mapping 401 error patterns."""

        # Test 401 string patterns
        error_401_1 = Exception("401 Unauthorized")
        result = map_gumnut_error(error_401_1, "Failed to authenticate")
        assert isinstance(result, HTTPException)
        assert result.status_code == 401
        assert result.detail == "Failed to authenticate: Invalid API key"

        error_401_2 = Exception("Invalid API key provided")
        result = map_gumnut_error(error_401_2, "Failed to authenticate")
        assert isinstance(result, HTTPException)
        assert result.status_code == 401
        assert result.detail == "Failed to authenticate: Invalid API key"

        error_401_3 = Exception("Unauthorized access")
        result = map_gumnut_error(error_401_3, "Failed to authenticate")
        assert isinstance(result, HTTPException)
        assert result.status_code == 401
        assert result.detail == "Failed to authenticate: Invalid API key"

    def test_map_error_403_patterns(self):
        """Test mapping 403 error patterns."""

        # Test 403 string patterns
        error_403_1 = Exception("403 Forbidden")
        result = map_gumnut_error(error_403_1, "Failed to access")
        assert isinstance(result, HTTPException)
        assert result.status_code == 403
        assert result.detail == "Failed to access: Access denied"

        error_403_2 = Exception("Access Forbidden")
        result = map_gumnut_error(error_403_2, "Failed to access")
        assert isinstance(result, HTTPException)
        assert result.status_code == 403
        assert result.detail == "Failed to access: Access denied"

    def test_map_error_400_patterns(self):
        """Test mapping 400 error patterns."""

        # Test 400 string patterns
        error_400_1 = Exception("400 Bad request")
        result = map_gumnut_error(error_400_1, "Failed to process")
        assert isinstance(result, HTTPException)
        assert result.status_code == 400
        assert result.detail == "Failed to process: Bad request"

        error_400_2 = Exception("Invalid Bad request format")
        result = map_gumnut_error(error_400_2, "Failed to process")
        assert isinstance(result, HTTPException)
        assert result.status_code == 400
        assert result.detail == "Failed to process: Bad request"

    def test_map_error_fallback_to_500(self):
        """Test mapping unknown errors falls back to 500."""

        # Test unknown error
        unknown_error = Exception("Some unknown error")
        result = map_gumnut_error(unknown_error, "Failed to process")
        assert isinstance(result, HTTPException)
        assert result.status_code == 500
        assert result.detail == "Failed to process: Some unknown error"

        # Test empty error message
        empty_error = Exception("")
        result = map_gumnut_error(empty_error, "Failed to process")
        assert isinstance(result, HTTPException)
        assert result.status_code == 500
        assert result.detail == "Failed to process: "

    def test_map_error_with_different_contexts(self):
        """Test mapping errors with different context messages."""

        error = Exception("404 Not found")

        # Test different contexts
        result1 = map_gumnut_error(error, "Failed to fetch album")
        assert result1.detail == "Failed to fetch album: Not found"

        result2 = map_gumnut_error(error, "Failed to fetch asset")
        assert result2.detail == "Failed to fetch asset: Not found"

        result3 = map_gumnut_error(error, "Failed to fetch person")
        assert result3.detail == "Failed to fetch person: Not found"

    def test_map_error_case_insensitive_patterns(self):
        """Test that string pattern matching is case insensitive where appropriate."""

        # Test lowercase patterns
        error_bad_request = Exception("bad request")
        result = map_gumnut_error(error_bad_request, "Failed to process")
        assert isinstance(result, HTTPException)
        assert result.status_code == 400

        error_forbidden = Exception("forbidden")
        result = map_gumnut_error(error_forbidden, "Failed to access")
        assert isinstance(result, HTTPException)
        assert result.status_code == 403

        error_unauthorized = Exception("unauthorized")
        result = map_gumnut_error(error_unauthorized, "Failed to authenticate")
        assert isinstance(result, HTTPException)
        assert result.status_code == 401

    def test_map_error_priority_status_code_over_string(self):
        """Test that status_code attribute takes priority over string matching."""

        class MockSDKError(Exception):
            def __init__(self, message, status_code):
                super().__init__(message)
                self.status_code = status_code

        # Error has status_code 500 but message contains "404"
        error = MockSDKError("404 Not found in server error", 500)
        result = map_gumnut_error(error, "Failed to process")
        assert isinstance(result, HTTPException)
        assert result.status_code == 500  # Should use status_code, not string pattern
        assert result.detail == "404 Not found in server error"

        # Error has status_code 401 but message contains "403"
        error = MockSDKError("403 Forbidden but auth issue", 401)
        result = map_gumnut_error(error, "Failed to authenticate")
        assert isinstance(result, HTTPException)
        assert result.status_code == 401  # Should use status_code, not string pattern

    def test_map_error_extracts_message_from_body(self):
        """Test that clean messages are extracted from SDK exception body attribute."""

        class MockSDKErrorWithBody(Exception):
            def __init__(self, message, status_code, body):
                super().__init__(message)
                self.status_code = status_code
                self.body = body

        # Test extracting 'detail' from body (like Gumnut SDK responses)
        error_with_detail = MockSDKErrorWithBody(
            "Error code: 401 - {'detail': 'JWT has expired'}",
            401,
            {"detail": "JWT has expired"},
        )
        result = map_gumnut_error(error_with_detail, "Failed to fetch user details")
        assert isinstance(result, HTTPException)
        assert result.status_code == 401
        assert result.detail == "JWT has expired"

        # Test extracting 'message' from body
        error_with_message = MockSDKErrorWithBody(
            "Error code: 404 - {'message': 'Asset not found'}",
            404,
            {"message": "Asset not found"},
        )
        result = map_gumnut_error(error_with_message, "Failed to fetch asset")
        assert isinstance(result, HTTPException)
        assert result.status_code == 404
        assert result.detail == "Asset not found"

        # Test extracting 'error' from body
        error_with_error = MockSDKErrorWithBody(
            "Error code: 403 - {'error': 'Access denied'}",
            403,
            {"error": "Access denied"},
        )
        result = map_gumnut_error(error_with_error, "Failed to access resource")
        assert isinstance(result, HTTPException)
        assert result.status_code == 403
        assert result.detail == "Access denied"

        # Test fallback when body is not a dict
        error_without_dict_body = MockSDKErrorWithBody(
            "Plain error message",
            500,
            "not a dict",
        )
        result = map_gumnut_error(error_without_dict_body, "Failed to process")
        assert isinstance(result, HTTPException)
        assert result.status_code == 500
        assert result.detail == "Plain error message"
