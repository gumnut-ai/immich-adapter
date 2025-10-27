from urllib.parse import urlparse, parse_qs


def parse_callback_url(callback_url: str) -> dict[str, str | None]:
    """
    Parse OAuth callback URL to extract authorization code, state, and error.

    The callback URL from the OAuth provider contains query parameters with
    the authorization code (on success) or error (on failure), plus the CSRF
    state token.

    Args:
        callback_url: Full callback URL from OAuth provider
                     Example: "http://localhost:3000/auth/callback?code=abc123&state=xyz789"

    Returns:
        Dictionary with parsed values:
        - code: Authorization code (None if error occurred)
        - state: CSRF state token
        - error: Error code if OAuth failed (None on success)

    Raises:
        ValueError: If URL cannot be parsed or is missing required parameters
    """
    try:
        parsed = urlparse(callback_url)
        query_params = parse_qs(parsed.query)

        # Extract parameters (parse_qs returns lists, we want first value)
        code = query_params.get("code", [None])[0]
        state = query_params.get("state", [None])[0]
        error = query_params.get("error", [None])[0]

        # State is required in OAuth flow
        if state is None:
            raise ValueError("Missing required 'state' parameter in callback URL")

        return {
            "code": code,
            "state": state,
            "error": error,
        }

    except Exception as e:
        raise ValueError(f"Failed to parse OAuth callback URL: {str(e)}")
