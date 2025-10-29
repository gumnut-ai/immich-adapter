from urllib.parse import parse_qs, urlparse, urlunparse


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
        if not state:
            raise ValueError("Missing required 'state' parameter in callback URL")

        return {
            "code": code,
            "state": state,
            "error": error,
        }

    except Exception as e:
        raise ValueError(f"Failed to parse OAuth callback URL: {str(e)}")


def normalize_redirect_uri(uri: str) -> str:
    """
    Normalize a redirect URI for consistent comparison in OAuth security checks.

    This function applies several transformations to ensure URIs can be compared
    reliably regardless of formatting variations. Query parameters are preserved
    as they are valid in OAuth redirect URIs, but fragments are stripped.

    Transformations applied:
    1. Scheme & hostname converted to lowercase (path remains case-sensitive)
    2. Default ports stripped (`:80` for HTTP, `:443` for HTTPS)
    3. Non-default ports preserved (`:8080`, `:8443`, etc.)
    4. Trailing slashes removed from paths (except root `/`)
    5. Query parameters preserved (allowed in OAuth redirect URIs)
    6. Fragments stripped
    7. Empty paths normalized to `/`

    Examples:
        - "HTTP://LocalHost:80/auth/callback/" → "http://localhost/auth/callback"
        - "https://app.example.com:443/oauth" → "https://app.example.com/oauth"
        - "http://localhost:8080/callback/" → "http://localhost:8080/callback"
        - "http://example.com?state=abc" → "http://example.com/?state=abc"
        - "http://example.com?state=abc#frag" → "http://example.com/?state=abc"
        - "http://example.com#fragment" → "http://example.com/"

    Args:
        uri: The redirect URI to normalize

    Returns:
        Normalized URI string suitable for comparison
    """
    p = urlparse(uri)
    scheme = (p.scheme or "").lower()
    host = (p.hostname or "").lower()
    port = f":{p.port}" if p.port else ""
    # Strip default ports
    if (scheme == "http" and port == ":80") or (scheme == "https" and port == ":443"):
        port = ""
    path = (p.path or "/").rstrip("/") or "/"
    query = p.query
    return urlunparse((scheme, f"{host}{port}", path, "", query, ""))
