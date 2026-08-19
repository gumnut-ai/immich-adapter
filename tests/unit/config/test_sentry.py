from typing import Any

from config.sentry import _enrich_http_spans, _redact_error_event


def _span_data(result: dict[str, Any], index: int) -> dict[str, Any]:
    """Extract span data dict with proper typing for test assertions."""
    return result["spans"][index]["data"]


def test_error_events_redact_signed_cdn_values_without_touching_other_queries():
    event: Any = {
        "exception": {
            "values": [
                {
                    "stacktrace": {
                        "frames": [
                            {
                                "vars": {
                                    "url": (
                                        "'https://assets.gumnut.ai/asset.jpg"
                                        "?w=720&verify=secret-token"
                                        "&dl=family-photo.jpg&f=webp'"
                                    ),
                                    "safe_url": (
                                        "'https://api.example.com/search"
                                        "?page=2&dl=report.csv&query=cats'"
                                    ),
                                }
                            }
                        ]
                    }
                }
            ]
        },
        "breadcrumbs": {
            "values": [
                {
                    "message": (
                        "GET https://assets.gumnut.ai/asset.jpg"
                        "?verify=secret-token&dl=family-photo.jpg&w=720"
                    )
                }
            ]
        },
        "request": {"query_string": "page=2&dl=report.csv&query=cats"},
    }

    result = _redact_error_event(event, {})
    rendered = repr(result)
    assert "secret-token" not in rendered
    assert "family-photo.jpg" not in rendered
    assert "verify=REDACTED&dl=REDACTED&w=720" in rendered
    assert "page=2&dl=report.csv&query=cats" in rendered


class TestEnrichHttpSpans:
    def test_redacts_only_sensitive_cdn_query_metadata(self):
        event = {
            "spans": [
                {
                    "op": "http.client",
                    "description": (
                        "GET https://assets.gumnut.ai/asset.jpg"
                        "?w=720&verify=secret-token&dl=family-photo.jpg&f=webp"
                    ),
                    "data": {
                        "url": (
                            "https://assets.gumnut.ai/asset.jpg"
                            "?w=720&verify=secret-token&dl=family-photo.jpg&f=webp"
                        ),
                        "http.query": (
                            "w=720&verify=secret-token&dl=family-photo.jpg&f=webp"
                        ),
                    },
                }
            ]
        }

        result = _enrich_http_spans(event, {})
        span = result["spans"][0]
        assert span["description"].endswith("?w=720&verify=REDACTED&dl=REDACTED&f=webp")
        assert _span_data(result, 0)["url"].endswith(
            "?w=720&verify=REDACTED&dl=REDACTED&f=webp"
        )
        assert _span_data(result, 0)["http.query"] == (
            "w=720&verify=REDACTED&dl=REDACTED&f=webp"
        )

    def test_preserves_unrelated_query_metadata(self):
        event = {
            "spans": [
                {
                    "op": "http.client",
                    "description": (
                        "GET https://api.example.com/search"
                        "?page=2&dl=report.csv&query=cats"
                    ),
                    "data": {
                        "url": (
                            "https://api.example.com/search"
                            "?page=2&dl=report.csv&query=cats"
                        ),
                        "http.query": "page=2&dl=report.csv&query=cats",
                    },
                }
            ]
        }

        result = _enrich_http_spans(event, {})
        assert _span_data(result, 0)["http.query"] == (
            "page=2&dl=report.csv&query=cats"
        )
        assert _span_data(result, 0)["url"].endswith("?page=2&dl=report.csv&query=cats")

    def test_adds_server_address_from_url(self):
        event = {
            "spans": [
                {
                    "op": "http.client",
                    "description": "GET https://api.example.com/v1/users",
                    "data": {},
                }
            ]
        }
        result = _enrich_http_spans(event, {})
        assert _span_data(result, 0)["server.address"] == "api.example.com"

    def test_adds_server_port_when_present(self):
        event = {
            "spans": [
                {
                    "op": "http.client",
                    "description": "POST http://localhost:8000/api/assets",
                    "data": {},
                }
            ]
        }
        result = _enrich_http_spans(event, {})
        assert _span_data(result, 0)["server.address"] == "localhost"
        assert _span_data(result, 0)["server.port"] == 8000

    def test_skips_non_http_client_spans(self):
        event = {
            "spans": [
                {
                    "op": "db",
                    "description": "SELECT * FROM users",
                    "data": {},
                }
            ]
        }
        result = _enrich_http_spans(event, {})
        assert "server.address" not in _span_data(result, 0)

    def test_skips_spans_that_already_have_server_address(self):
        event = {
            "spans": [
                {
                    "op": "http.client",
                    "description": "GET https://api.example.com/v1/users",
                    "data": {"server.address": "existing.example.com"},
                }
            ]
        }
        result = _enrich_http_spans(event, {})
        assert _span_data(result, 0)["server.address"] == "existing.example.com"

    def test_skips_spans_without_url_in_description(self):
        event = {
            "spans": [
                {
                    "op": "http.client",
                    "description": "aws.s3.PutObject",
                    "data": {},
                }
            ]
        }
        result = _enrich_http_spans(event, {})
        assert "server.address" not in _span_data(result, 0)

    def test_handles_missing_data_dict(self):
        event = {
            "spans": [
                {
                    "op": "http.client",
                    "description": "GET https://api.example.com/v1/users",
                }
            ]
        }
        result = _enrich_http_spans(event, {})
        assert _span_data(result, 0)["server.address"] == "api.example.com"

    def test_handles_no_spans(self):
        event: dict[str, Any] = {"spans": []}
        result = _enrich_http_spans(event, {})
        assert result["spans"] == []

    def test_omits_port_for_default_https(self):
        event = {
            "spans": [
                {
                    "op": "http.client",
                    "description": "GET https://api.clerk.com/v1/jwks",
                    "data": {},
                }
            ]
        }
        result = _enrich_http_spans(event, {})
        assert _span_data(result, 0)["server.address"] == "api.clerk.com"
        assert "server.port" not in _span_data(result, 0)

    def test_enriches_multiple_spans(self):
        event = {
            "spans": [
                {
                    "op": "http.client",
                    "description": "GET https://a.example.com/foo",
                    "data": {},
                },
                {
                    "op": "db",
                    "description": "SELECT 1",
                    "data": {},
                },
                {
                    "op": "http.client",
                    "description": "POST https://b.example.com/bar",
                    "data": {},
                },
            ]
        }
        result = _enrich_http_spans(event, {})
        assert _span_data(result, 0)["server.address"] == "a.example.com"
        assert "server.address" not in _span_data(result, 1)
        assert _span_data(result, 2)["server.address"] == "b.example.com"

    def test_handles_data_none(self):
        event = {
            "spans": [
                {
                    "op": "http.client",
                    "description": "GET https://api.example.com/v1/users",
                    "data": None,
                }
            ]
        }
        result = _enrich_http_spans(event, {})
        assert _span_data(result, 0)["server.address"] == "api.example.com"

    def test_prefers_data_url_over_description(self):
        event = {
            "spans": [
                {
                    "op": "http.client",
                    "description": "GET https://description.example.com/foo",
                    "data": {"url": "https://data.example.com/bar"},
                }
            ]
        }
        result = _enrich_http_spans(event, {})
        assert _span_data(result, 0)["server.address"] == "data.example.com"

    def test_skips_non_http_scheme(self):
        event = {
            "spans": [
                {
                    "op": "http.client",
                    "description": "CONNECT ftp://files.example.com/data",
                    "data": {},
                }
            ]
        }
        result = _enrich_http_spans(event, {})
        assert "server.address" not in _span_data(result, 0)

    def test_handles_spans_none(self):
        event: dict[str, Any] = {"spans": None}
        result = _enrich_http_spans(event, {})
        assert result["spans"] is None

    def test_ignores_non_dict_span_element(self):
        event: dict[str, Any] = {
            "spans": [
                None,
                {
                    "op": "http.client",
                    "description": "GET https://api.example.com/v1",
                    "data": {},
                },
            ]
        }
        result = _enrich_http_spans(event, {})
        assert _span_data(result, 1)["server.address"] == "api.example.com"

    def test_ignores_non_string_url(self):
        event = {
            "spans": [
                {
                    "op": "http.client",
                    "description": "GET https://api.example.com/v1",
                    "data": {"url": {"not": "a string"}},
                }
            ]
        }
        result = _enrich_http_spans(event, {})
        assert _span_data(result, 0)["server.address"] == "api.example.com"

    def test_malformed_port_does_not_raise(self):
        event = {
            "spans": [
                {
                    "op": "http.client",
                    "description": "GET http://localhost:badport/foo",
                    "data": {},
                }
            ]
        }
        result = _enrich_http_spans(event, {})
        assert _span_data(result, 0)["server.address"] == "localhost"
        assert "server.port" not in _span_data(result, 0)

    def test_non_string_description_does_not_raise(self):
        event = {
            "spans": [
                {
                    "op": "http.client",
                    "description": 123,
                    "data": {},
                }
            ]
        }
        result = _enrich_http_spans(event, {})
        assert "server.address" not in _span_data(result, 0)
