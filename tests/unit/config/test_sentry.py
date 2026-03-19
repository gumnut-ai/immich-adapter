from typing import Any

from config.sentry import _enrich_http_spans


def _span_data(result: dict[str, Any], index: int) -> dict[str, Any]:
    """Extract span data dict with proper typing for test assertions."""
    return result["spans"][index]["data"]


class TestEnrichHttpSpans:
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
