"""
Error mapping utilities for handling Gumnut SDK exceptions.
"""

import logging
from fastapi import HTTPException

logger = logging.getLogger(__name__)


def check_for_error_by_code(e: Exception, code: int) -> bool:
    """
    Check if an exception represents a specific HTTP error code.

    Args:
        e: The exception to check
        code: The HTTP status code to check for

    Returns:
        True if the exception represents the specified error code, False otherwise
    """
    # Check if the SDK exposes HTTP status code
    if hasattr(e, "status_code"):
        status_code = int(getattr(e, "status_code"))
        return status_code == code

    return False


def map_gumnut_error(e: Exception, context: str) -> HTTPException:
    """
    Map Gumnut SDK exceptions to appropriate HTTP exceptions.

    Args:
        e: The exception from the Gumnut SDK
        context: Context string describing what operation failed

    Returns:
        HTTPException with appropriate status code and detail message
    """
    msg = str(e)

    # Log the original error for debugging
    logger.warning(f"Gumnut SDK error in {context}: {msg}")

    # Try to extract clean message from SDK exception body
    detail = None
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("message") or body.get("error")

    if not detail:
        detail = msg

    # If the SDK exposes HTTP status, use it
    if hasattr(e, "status_code"):
        code = int(getattr(e, "status_code"))
        return HTTPException(status_code=code, detail=detail)

    # Fallback to string matching for common HTTP errors
    # This is still brittle but better than duplicating everywhere
    msg_lower = msg.lower()
    if "404" in msg or "not found" in msg_lower:
        return HTTPException(status_code=404, detail=f"{context}: Not found")
    elif "401" in msg or "invalid api key" in msg_lower or "unauthorized" in msg_lower:
        return HTTPException(status_code=401, detail=f"{context}: Invalid API key")
    elif "403" in msg or "forbidden" in msg_lower:
        return HTTPException(status_code=403, detail=f"{context}: Access denied")
    elif "400" in msg or "bad request" in msg_lower:
        return HTTPException(status_code=400, detail=f"{context}: Bad request")

    # Final fallback to 500
    return HTTPException(status_code=500, detail=f"{context}: {msg}")
