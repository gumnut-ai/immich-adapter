"""Tests for faces.py endpoints."""

import pytest
from unittest.mock import AsyncMock, Mock
from uuid import uuid4
from datetime import datetime, timezone

from routers.api.faces import get_faces
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_person_id,
    uuid_to_gumnut_asset_id,
    uuid_to_gumnut_face_id,
    uuid_to_gumnut_person_id,
)


def _make_face(
    asset_id: str,
    person_id: str | None = None,
    bounding_box: dict | None = None,
) -> Mock:
    """Create a mock Gumnut FaceResponse."""
    face = Mock()
    face.id = uuid_to_gumnut_face_id(uuid4())
    face.asset_id = asset_id
    face.person_id = person_id
    face.bounding_box = bounding_box or {"x": 100, "y": 200, "w": 300, "h": 400}
    face.created_at = datetime.now(timezone.utc)
    face.updated_at = datetime.now(timezone.utc)
    face.thumbnail_url = None
    return face


def _make_asset(asset_id: str, width: int = 1920, height: int = 1080) -> Mock:
    """Create a mock Gumnut AssetResponse with dimensions."""
    asset = Mock()
    asset.id = asset_id
    asset.width = width
    asset.height = height
    return asset


def _make_person(person_id: str, name: str = "Test Person") -> Mock:
    """Create a mock Gumnut PersonResponse."""
    person = Mock()
    person.id = person_id
    person.name = name
    person.birth_date = datetime(1990, 1, 1).date()
    person.is_favorite = False
    person.is_hidden = False
    person.created_at = datetime.now(timezone.utc)
    person.updated_at = datetime.now(timezone.utc)
    return person


class TestGetFaces:
    """Test the get_faces endpoint."""

    @pytest.mark.anyio
    async def test_returns_faces_for_asset(self, mock_sync_cursor_page):
        """Test that faces are returned with correct bounding box conversion."""
        asset_uuid = uuid4()
        gumnut_asset_id = uuid_to_gumnut_asset_id(asset_uuid)

        face = _make_face(
            asset_id=gumnut_asset_id,
            bounding_box={"x": 100, "y": 200, "w": 300, "h": 400},
        )

        mock_client = Mock()
        mock_client.faces.list = Mock(return_value=mock_sync_cursor_page([face]))
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_asset(gumnut_asset_id, width=1920, height=1080)
        )

        result = await get_faces(id=asset_uuid, client=mock_client)

        assert len(result) == 1
        assert result[0].boundingBoxX1 == 100
        assert result[0].boundingBoxX2 == 400  # x + w
        assert result[0].boundingBoxY1 == 200
        assert result[0].boundingBoxY2 == 600  # y + h
        assert result[0].imageWidth == 1920
        assert result[0].imageHeight == 1080
        assert result[0].person is None

        mock_client.faces.list.assert_called_once_with(asset_id=gumnut_asset_id)

    @pytest.mark.anyio
    async def test_returns_empty_list_when_no_faces(self, mock_sync_cursor_page):
        """Test that an empty list is returned when asset has no faces."""
        asset_uuid = uuid4()
        gumnut_asset_id = uuid_to_gumnut_asset_id(asset_uuid)

        mock_client = Mock()
        mock_client.faces.list = Mock(return_value=mock_sync_cursor_page([]))
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_asset(gumnut_asset_id)
        )

        result = await get_faces(id=asset_uuid, client=mock_client)

        assert result == []

    @pytest.mark.anyio
    async def test_includes_person_data(self, mock_sync_cursor_page):
        """Test that person data is included when face has a person_id."""
        asset_uuid = uuid4()
        gumnut_asset_id = uuid_to_gumnut_asset_id(asset_uuid)
        person_id = uuid_to_gumnut_person_id(uuid4())

        face = _make_face(asset_id=gumnut_asset_id, person_id=person_id)
        person = _make_person(person_id, name="Calvin")

        mock_client = Mock()
        mock_client.faces.list = Mock(return_value=mock_sync_cursor_page([face]))
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_asset(gumnut_asset_id)
        )
        mock_client.people.retrieve = AsyncMock(return_value=person)

        result = await get_faces(id=asset_uuid, client=mock_client)

        assert len(result) == 1
        assert result[0].person is not None
        assert result[0].person.name == "Calvin"
        assert result[0].person.id == str(safe_uuid_from_person_id(person_id))

    @pytest.mark.anyio
    async def test_multiple_faces_same_person_fetches_once(self, mock_sync_cursor_page):
        """Test that the same person is only fetched once for multiple faces."""
        asset_uuid = uuid4()
        gumnut_asset_id = uuid_to_gumnut_asset_id(asset_uuid)
        person_id = uuid_to_gumnut_person_id(uuid4())

        face1 = _make_face(asset_id=gumnut_asset_id, person_id=person_id)
        face2 = _make_face(asset_id=gumnut_asset_id, person_id=person_id)
        person = _make_person(person_id)

        mock_client = Mock()
        mock_client.faces.list = Mock(
            return_value=mock_sync_cursor_page([face1, face2])
        )
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_asset(gumnut_asset_id)
        )
        mock_client.people.retrieve = AsyncMock(return_value=person)

        result = await get_faces(id=asset_uuid, client=mock_client)

        assert len(result) == 2
        # Person should be fetched only once
        mock_client.people.retrieve.assert_called_once_with(person_id)
        assert result[0].person is not None
        assert result[1].person is not None

    @pytest.mark.anyio
    async def test_person_fetch_failure_returns_null_person(
        self, mock_sync_cursor_page
    ):
        """Test that a failed person fetch results in null person, not an error."""
        asset_uuid = uuid4()
        gumnut_asset_id = uuid_to_gumnut_asset_id(asset_uuid)
        person_id = uuid_to_gumnut_person_id(uuid4())

        face = _make_face(asset_id=gumnut_asset_id, person_id=person_id)

        mock_client = Mock()
        mock_client.faces.list = Mock(return_value=mock_sync_cursor_page([face]))
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_asset(gumnut_asset_id)
        )
        mock_client.people.retrieve = AsyncMock(
            side_effect=Exception("Person not found")
        )

        result = await get_faces(id=asset_uuid, client=mock_client)

        assert len(result) == 1
        assert result[0].person is None

    @pytest.mark.anyio
    async def test_face_id_converted_to_uuid(self, mock_sync_cursor_page):
        """Test that Gumnut face IDs are converted to UUIDs."""
        asset_uuid = uuid4()
        gumnut_asset_id = uuid_to_gumnut_asset_id(asset_uuid)
        face_uuid = uuid4()

        face = _make_face(asset_id=gumnut_asset_id)
        face.id = uuid_to_gumnut_face_id(face_uuid)

        mock_client = Mock()
        mock_client.faces.list = Mock(return_value=mock_sync_cursor_page([face]))
        mock_client.assets.retrieve = AsyncMock(
            return_value=_make_asset(gumnut_asset_id)
        )

        result = await get_faces(id=asset_uuid, client=mock_client)

        assert result[0].id == face_uuid

    @pytest.mark.anyio
    async def test_sdk_error_mapped_to_http_exception(self):
        """Test that SDK errors from faces.list are mapped via map_gumnut_error."""
        from fastapi import HTTPException

        asset_uuid = uuid4()

        mock_client = Mock()
        mock_client.faces.list = Mock(side_effect=Exception("Something went wrong"))

        with pytest.raises(HTTPException) as exc_info:
            await get_faces(id=asset_uuid, client=mock_client)

        assert exc_info.value.status_code == 500
        assert "Failed to fetch faces" in exc_info.value.detail
