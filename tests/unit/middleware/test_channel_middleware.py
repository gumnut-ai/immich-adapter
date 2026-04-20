"""Unit tests for ChannelTaggingMiddleware."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.middleware.channel_middleware import (
    CHANNEL_TAG,
    ChannelTaggingMiddleware,
    resolve_channel,
)


class TestResolveChannel:
    @pytest.mark.parametrize(
        "user_agent,expected",
        [
            # Immich mobile apps (actual client UA format)
            ("Immich_iOS_1.94.0", "immich-mobile-ios"),
            ("Immich_Android_1.95.1", "immich-mobile-android"),
            # Lower-case spec format
            ("immich-ios/1.94.0", "immich-mobile-ios"),
            ("immich-android/1.95.1", "immich-mobile-android"),
            # Browsers
            (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
                "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
                "immich-web",
            ),
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "immich-web",
            ),
            (
                "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 "
                "Firefox/125.0",
                "immich-web",
            ),
            # Unknown UAs fall back to generic immich-adapter tag
            ("", "immich-adapter"),
            ("curl/8.4.0", "immich-adapter"),
            ("Mozilla/5.0 (unknown bot)", "immich-adapter"),
            ("Python/3.13 requests/2.31", "immich-adapter"),
        ],
    )
    def test_classification(self, user_agent: str, expected: str):
        assert resolve_channel(user_agent) == expected


class TestChannelTaggingMiddleware:
    @pytest.fixture
    def client(self):
        app = FastAPI()
        app.add_middleware(ChannelTaggingMiddleware)

        @app.get("/api/ping")
        async def _ping():
            return {"ok": True}

        return TestClient(app)

    @pytest.mark.parametrize(
        "user_agent,expected",
        [
            ("Immich_iOS_1.94.0", "immich-mobile-ios"),
            ("Immich_Android_1.95.1", "immich-mobile-android"),
            (
                "Mozilla/5.0 (Macintosh) AppleWebKit/605 Chrome/122",
                "immich-web",
            ),
            ("curl/8.4.0", "immich-adapter"),
            ("", "immich-adapter"),
        ],
    )
    def test_tag_set_on_active_scope(
        self, client: TestClient, user_agent: str, expected: str
    ):
        mock_span = MagicMock()
        with (
            patch(
                "routers.middleware.channel_middleware.sentry_sdk.set_tag"
            ) as mock_set_tag,
            patch(
                "routers.middleware.channel_middleware.sentry_sdk.get_current_span",
                return_value=mock_span,
            ),
        ):
            headers = {"user-agent": user_agent} if user_agent else {}
            response = client.get("/api/ping", headers=headers)

        assert response.status_code == 200
        mock_set_tag.assert_called_once_with(CHANNEL_TAG, expected)
        mock_span.set_data.assert_called_once_with(CHANNEL_TAG, expected)

    def test_tag_still_set_when_no_active_span(self, client: TestClient):
        with (
            patch(
                "routers.middleware.channel_middleware.sentry_sdk.set_tag"
            ) as mock_set_tag,
            patch(
                "routers.middleware.channel_middleware.sentry_sdk.get_current_span",
                return_value=None,
            ),
        ):
            response = client.get(
                "/api/ping", headers={"user-agent": "Immich_iOS_1.94.0"}
            )

        assert response.status_code == 200
        mock_set_tag.assert_called_once_with(CHANNEL_TAG, "immich-mobile-ios")
