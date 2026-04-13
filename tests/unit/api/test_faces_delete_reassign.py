"""Tests for face delete and reassign endpoints."""

import pytest
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from routers.api.faces import delete_face, reassign_faces_by_id
from routers.immich_models import AssetFaceDeleteDto, FaceDto
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_person_id,
    uuid_to_gumnut_face_id,
    uuid_to_gumnut_person_id,
)


def _make_person(person_id: str, name: str = "Test Person") -> Mock:
    """Create a mock Gumnut PersonResponse."""
    from datetime import datetime, timezone

    person = Mock()
    person.id = person_id
    person.name = name
    person.birth_date = datetime(1990, 1, 1).date()
    person.is_favorite = False
    person.is_hidden = False
    person.created_at = datetime.now(timezone.utc)
    person.updated_at = datetime.now(timezone.utc)
    return person


class TestDeleteFace:
    """Test the delete_face endpoint."""

    @pytest.mark.anyio
    async def test_deletes_face_via_sdk(self):
        """Test that delete_face calls client.faces.delete with converted ID."""
        face_uuid = uuid4()
        gumnut_face_id = uuid_to_gumnut_face_id(face_uuid)

        mock_client = Mock()
        mock_client.faces.delete = AsyncMock()

        request = AssetFaceDeleteDto(force=False)
        await delete_face(id=face_uuid, request=request, client=mock_client)

        mock_client.faces.delete.assert_called_once_with(gumnut_face_id)

    @pytest.mark.anyio
    async def test_sdk_error_mapped_to_http_exception(self):
        """Test that SDK errors are mapped via map_gumnut_error."""
        from fastapi import HTTPException

        face_uuid = uuid4()
        mock_client = Mock()
        mock_client.faces.delete = AsyncMock(
            side_effect=Exception("Something went wrong")
        )

        request = AssetFaceDeleteDto(force=False)
        with pytest.raises(HTTPException) as exc_info:
            await delete_face(id=face_uuid, request=request, client=mock_client)

        assert exc_info.value.status_code == 500
        assert "Failed to delete face" in exc_info.value.detail


class TestReassignFace:
    """Test the reassign_faces_by_id endpoint."""

    @pytest.mark.anyio
    async def test_reassigns_face_to_person(self):
        """Test that reassign updates face and returns converted person."""
        face_uuid = uuid4()
        person_uuid = uuid4()
        gumnut_face_id = uuid_to_gumnut_face_id(face_uuid)
        gumnut_person_id = uuid_to_gumnut_person_id(person_uuid)

        person = _make_person(gumnut_person_id, name="Calvin")

        mock_client = Mock()
        mock_client.faces.update = AsyncMock()
        mock_client.people.retrieve = AsyncMock(return_value=person)

        request = FaceDto(id=person_uuid)
        result = await reassign_faces_by_id(
            id=face_uuid, request=request, client=mock_client
        )

        mock_client.faces.update.assert_called_once_with(
            gumnut_face_id, person_id=gumnut_person_id
        )
        mock_client.people.retrieve.assert_called_once_with(gumnut_person_id)
        assert result.name == "Calvin"
        assert result.id == str(safe_uuid_from_person_id(gumnut_person_id))

    @pytest.mark.anyio
    async def test_sdk_error_mapped_to_http_exception(self):
        """Test that SDK errors are mapped via map_gumnut_error."""
        from fastapi import HTTPException

        face_uuid = uuid4()
        person_uuid = uuid4()

        mock_client = Mock()
        mock_client.faces.update = AsyncMock(
            side_effect=Exception("Something went wrong")
        )

        request = FaceDto(id=person_uuid)
        with pytest.raises(HTTPException) as exc_info:
            await reassign_faces_by_id(
                id=face_uuid, request=request, client=mock_client
            )

        assert exc_info.value.status_code == 500
        assert "Failed to reassign face" in exc_info.value.detail
