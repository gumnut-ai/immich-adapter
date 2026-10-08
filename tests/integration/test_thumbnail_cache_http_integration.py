import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from routers.api.assets import router
from routers.middleware.auth_middleware import AuthMiddleware
from routers.utils.gumnut_client import (
    LibraryScope,
    bind_library_scope,
    get_authenticated_gumnut_client,
)
from services.thumbnail_cache import ThumbnailCache


@pytest.mark.parametrize(
    "mutation_path,method",
    [
        ("/api/assets/example/edits", "put"),
        ("/api/assets/example/edits", "delete"),
        ("/api/assets", "delete"),
        ("/api/trash/restore/assets", "post"),
        ("/api/trash/empty", "post"),
    ],
)
def test_session_rechecked_and_mutations_invalidate_even_after_failure(
    mutation_path, method
):
    metadata = Mock(
        width=100, height=100, mime_type="image/jpeg", thumbhash="known-hash"
    )
    metadata.asset_urls = {
        "thumbnail": SimpleNamespace(
            url="https://cdn.example.com/old", mimetype="image/webp"
        )
    }
    sdk = Mock()
    sdk.assets.retrieve = AsyncMock(return_value=metadata)
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": time.time() + 120}).encode()
    ).decode()
    session = Mock(
        user_id=str(uuid4()),
        library_id="library",
        library_checked_at=time.time(),
        library_from_choice=False,
    )
    session.get_jwt.return_value = "header." + payload + ".signature"
    store = AsyncMock()
    store.get_by_id.return_value = session
    cache = ThumbnailCache(30, 100)
    settings = SimpleNamespace(
        thumbnail_metadata_cache_ttl_seconds=30,
        gumnut_api_base_url="https://api.example.com",
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    async def authenticated():
        bind_library_scope(LibraryScope("library", AsyncMock()))
        return sdk

    async def mutation():
        metadata.asset_urls["thumbnail"].url = "https://cdn.example.com/new"
        # A backend write can succeed before the adapter response fails.
        return Response(status_code=500)

    app.add_api_route(mutation_path, mutation, methods=[method.upper()])
    app.include_router(router)
    app.dependency_overrides[get_authenticated_gumnut_client] = authenticated
    with (
        patch(
            "routers.middleware.auth_middleware.get_session_store",
            AsyncMock(return_value=store),
        ),
        patch(
            "routers.middleware.auth_middleware.get_thumbnail_cache", return_value=cache
        ),
        patch("routers.api.assets.get_thumbnail_cache", return_value=cache),
        patch("routers.utils.thumbnail_cache.get_settings", return_value=settings),
        patch("services.thumbnail_cache.metrics.count") as count,
        patch(
            "routers.api.assets.stream_from_cdn",
            AsyncMock(side_effect=lambda *args, **kwargs: Response(content=args[0])),
        ),
    ):
        with TestClient(app) as client:
            path = f"/api/assets/{uuid4()}/thumbnail?edited=true&size=thumbnail"
            headers = {"authorization": "Bearer session-token"}
            assert (
                client.get(path, headers=headers).text == "https://cdn.example.com/old"
            )
            assert (
                client.get(path + "&c=known-hash", headers=headers).text
                == "https://cdn.example.com/old"
            )
            assert sdk.assets.retrieve.await_count == 1
            assert store.get_by_id.await_count == 2
            assert (
                client.request(method, mutation_path, headers=headers).status_code
                == 500
            )
            assert (
                client.get(path, headers=headers).text == "https://cdn.example.com/new"
            )
            assert sdk.assets.retrieve.await_count == 2
            store.get_by_id.return_value = None
            assert client.get(path, headers=headers).status_code == 401
            assert sdk.assets.retrieve.await_count == 2
            assert [call.args for call in count.call_args_list] == [
                ("thumbnail.cache.lookup", 1),
                ("thumbnail.cache.lookup", 1),
                ("thumbnail.cache.lookup", 1),
            ]
            assert [
                call.kwargs["attributes"]["cache.outcome"]
                for call in count.call_args_list
            ] == ["miss", "hit", "miss"]
