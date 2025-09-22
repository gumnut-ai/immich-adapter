"""Tests for people.py endpoints."""

import pytest
from unittest.mock import Mock, patch
from fastapi import HTTPException
from uuid import uuid4
from datetime import datetime, timezone

from routers.api.people import (
    create_person,
    update_people,
    update_person,
    get_all_people,
    delete_people,
    get_thumbnail,
    get_person,
    get_person_statistics,
    delete_person,
    merge_person,
    reassign_faces,
)
from routers.immich_models import (
    AssetFaceUpdateDto,
    BulkIdsDto,
    Error2,
    MergePersonDto,
    PeopleUpdateDto,
    PeopleUpdateItem,
    PersonCreateDto,
    PersonUpdateDto,
)


def call_get_all_people(**kwargs):
    """Helper function to call get_all_people with proper None defaults for Query parameters."""
    defaults = {
        'closestAssetId': None,
        'closestPersonId': None,
        'page': 1,
        'size': 500,
        'withHidden': None,
    }
    defaults.update(kwargs)
    return get_all_people(**defaults)


class TestCreatePerson:
    """Test the create_person endpoint."""

    @pytest.mark.anyio
    async def test_create_person_success(self, sample_gumnut_person):
        """Test successful person creation."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.create.return_value = sample_gumnut_person

            request = PersonCreateDto(
                name="John Doe",
                birthDate=datetime(1990, 1, 1).date(),
                isFavorite=True,
                isHidden=False
            )

            # Execute
            result = await create_person(request)

            # Assert
            # Result should be a converted PersonResponseDto
            assert hasattr(result, 'id')
            assert hasattr(result, 'name')
            assert result.name == "Test Person"  # From sample_gumnut_person
            mock_client.people.create.assert_called_once_with(
                name="John Doe",
                birth_date=datetime(1990, 1, 1).date(),
                is_favorite=True,
                is_hidden=False
            )

    @pytest.mark.anyio
    async def test_create_person_api_error(self):
        """Test person creation with API error."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.create.side_effect = Exception("401 Invalid API key")

            request = PersonCreateDto(name="John Doe")

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await create_person(request)

            assert exc_info.value.status_code == 401

    @pytest.mark.anyio
    async def test_create_person_general_error(self):
        """Test person creation with general error."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.create.side_effect = Exception("Unknown error")

            request = PersonCreateDto(name="John Doe")

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await create_person(request)

            assert exc_info.value.status_code == 500
            assert "Failed to create person" in str(exc_info.value.detail)


class TestUpdatePeople:
    """Test the update_people endpoint."""

    @pytest.mark.anyio
    async def test_update_people_success(self):
        """Test successful bulk people update."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.update.return_value = None

            person_id1 = str(uuid4())
            person_id2 = str(uuid4())

            person_updates = [
                PeopleUpdateItem(id=person_id1, name="Updated Name 1"),
                PeopleUpdateItem(id=person_id2, name="Updated Name 2")
            ]
            request = PeopleUpdateDto(people=person_updates)

            # Execute
            result = await update_people(request)

            # Assert
            assert len(result) == 2
            assert all(item.success is True for item in result)
            assert all(item.error is None for item in result)
            assert result[0].id == person_id1
            assert result[1].id == person_id2
            assert mock_client.people.update.call_count == 2

    @pytest.mark.anyio
    async def test_update_people_mixed_results(self):
        """Test bulk people update with some failures."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # First update succeeds, second fails
            mock_client.people.update.side_effect = [
                None,  # Success
                Exception("404 Person not found")  # Failure
            ]

            person_id1 = str(uuid4())
            person_id2 = str(uuid4())

            person_updates = [
                PeopleUpdateItem(id=person_id1, name="Updated Name 1"),
                PeopleUpdateItem(id=person_id2, name="Updated Name 2")
            ]
            request = PeopleUpdateDto(people=person_updates)

            # Execute
            result = await update_people(request)

            # Assert
            assert len(result) == 2
            assert result[0].success is True
            assert result[0].error is None
            assert result[0].id == person_id1
            assert result[1].success is False
            assert result[1].error == Error2.not_found
            assert result[1].id == person_id2

    @pytest.mark.anyio
    async def test_update_people_partial_data(self):
        """Test bulk people update with partial data."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.update.return_value = None

            person_id1 = str(uuid4())
            person_id2 = str(uuid4())

            # Only updating some fields
            person_updates = [
                PeopleUpdateItem(id=person_id1, name="New Name"),  # Only name
                PeopleUpdateItem(id=person_id2, isFavorite=True),  # Only favorite
            ]
            request = PeopleUpdateDto(people=person_updates)

            # Execute
            result = await update_people(request)

            # Assert
            assert len(result) == 2
            assert all(item.success is True for item in result)
            assert result[0].id == person_id1
            assert result[1].id == person_id2
            # Check that only non-None fields were passed
            assert mock_client.people.update.call_count == 2


class TestUpdatePerson:
    """Test the update_person endpoint."""

    @pytest.mark.anyio
    async def test_update_person_success(self, sample_gumnut_person, sample_uuid):
        """Test successful person update."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            # Update the sample to have the updated name
            sample_gumnut_person.name = "Updated Name"
            mock_client.people.update.return_value = sample_gumnut_person

            request = PersonUpdateDto(name="Updated Name", isFavorite=True)

            # Execute
            result = await update_person(sample_uuid, request)

            # Assert
            # Result should be a converted PersonResponseDto
            assert hasattr(result, 'id')
            assert hasattr(result, 'name')
            assert result.name == "Updated Name"
            mock_client.people.update.assert_called_once()

    @pytest.mark.anyio
    async def test_update_person_not_found(self, sample_uuid):
        """Test updating non-existent person."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.update.side_effect = Exception("404 Person not found")

            request = PersonUpdateDto(name="Updated Name")

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await update_person(sample_uuid, request)

            assert exc_info.value.status_code == 500  # General error handling


class TestGetAllPeople:
    """Test the get_all_people endpoint."""

    @pytest.mark.anyio
    async def test_get_all_people_success(self, multiple_gumnut_people, mock_sync_cursor_page):
        """Test successful retrieval of all people."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.list.return_value = mock_sync_cursor_page(multiple_gumnut_people)

            # Execute
            result = await call_get_all_people()

            # Assert
            assert len(result.people) == 3
            assert result.total == 3
            assert result.hidden == 0
            assert result.hasNextPage is False  # No pagination in this case
            mock_client.people.list.assert_called_once()

    @pytest.mark.anyio
    async def test_get_all_people_with_pagination(self, multiple_gumnut_people, mock_sync_cursor_page):
        """Test people retrieval with pagination."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.list.return_value = mock_sync_cursor_page(multiple_gumnut_people)

            # Execute - get first page with size 2
            result = await call_get_all_people(page=1, size=2)

            # Assert
            assert len(result.people) == 2  # Only 2 out of 3
            assert result.total == 3  # But total is still 3
            assert result.hasNextPage is True  # Has next page

    @pytest.mark.anyio
    async def test_get_all_people_without_hidden(self, multiple_gumnut_people, mock_sync_cursor_page):
        """Test people retrieval excluding hidden people."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Mark one person as hidden
            people = multiple_gumnut_people
            people[0].is_hidden = False
            people[1].is_hidden = True  # Hidden
            people[2].is_hidden = False

            mock_client.people.list.return_value = mock_sync_cursor_page(people)

            # Execute
            result = await call_get_all_people(withHidden=False)

            # Assert
            assert len(result.people) == 2  # Only non-hidden people
            assert result.total == 2  # Total reflects filtered count
            assert result.hidden == 1  # One person was hidden

    @pytest.mark.anyio
    async def test_get_all_people_empty(self, mock_sync_cursor_page):
        """Test people retrieval with no people."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.list.return_value = mock_sync_cursor_page([])

            # Execute
            result = await call_get_all_people()

            # Assert
            assert len(result.people) == 0
            assert result.total == 0
            assert result.hidden == 0
            assert result.hasNextPage is False

    @pytest.mark.anyio
    async def test_get_all_people_gumnut_error(self):
        """Test handling of Gumnut API errors."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.list.side_effect = Exception("API Error")

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await call_get_all_people()

            assert exc_info.value.status_code == 500
            assert "Failed to fetch people" in str(exc_info.value.detail)


class TestDeletePeople:
    """Test the delete_people endpoint."""

    @pytest.mark.anyio
    async def test_delete_people_success(self):
        """Test successful bulk people deletion."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.delete.return_value = None

            person_ids = [uuid4(), uuid4()]
            request = BulkIdsDto(ids=person_ids)

            # Execute
            result = await delete_people(request)

            # Assert
            assert result.status_code == 204
            assert mock_client.people.delete.call_count == 2

    @pytest.mark.anyio
    async def test_delete_people_not_found(self):
        """Test deletion with person not found."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.delete.side_effect = Exception("404 Person not found")

            person_ids = [uuid4()]
            request = BulkIdsDto(ids=person_ids)

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await delete_people(request)

            assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_delete_people_api_error(self):
        """Test deletion with API error."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.delete.side_effect = Exception("401 Invalid API key")

            person_ids = [uuid4()]
            request = BulkIdsDto(ids=person_ids)

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await delete_people(request)

            assert exc_info.value.status_code == 401


class TestGetThumbnail:
    """Test the get_thumbnail endpoint."""

    @pytest.mark.anyio
    async def test_get_thumbnail_success(self, sample_gumnut_person, sample_uuid):
        """Test successful thumbnail retrieval."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Setup person with thumbnail
            sample_gumnut_person.thumbnail_face_id = "face-123"
            mock_client.people.retrieve.return_value = sample_gumnut_person

            # Mock the thumbnail download response
            mock_response = Mock()
            mock_response.read.return_value = b"fake image data"
            mock_response.headers = {"content-type": "image/jpeg"}
            mock_client.faces.download_thumbnail.return_value = mock_response

            # Execute
            result = await get_thumbnail(sample_uuid)

            # Assert
            assert result.media_type == "image/jpeg"
            assert result.body == b"fake image data"
            mock_client.people.retrieve.assert_called_once()
            mock_client.faces.download_thumbnail.assert_called_once_with("face-123")

    @pytest.mark.anyio
    async def test_get_thumbnail_no_thumbnail(self, sample_gumnut_person, sample_uuid):
        """Test thumbnail retrieval when person has no thumbnail."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            # Setup person without thumbnail
            sample_gumnut_person.thumbnail_face_id = None
            mock_client.people.retrieve.return_value = sample_gumnut_person

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await get_thumbnail(sample_uuid)

            assert exc_info.value.status_code == 404
            assert "Asset not found" in str(exc_info.value.detail)

    @pytest.mark.anyio
    async def test_get_thumbnail_person_not_found(self, sample_uuid):
        """Test thumbnail retrieval when person doesn't exist."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.retrieve.side_effect = Exception("404 Person not found")

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await get_thumbnail(sample_uuid)

            assert exc_info.value.status_code == 404


class TestGetPerson:
    """Test the get_person endpoint."""

    @pytest.mark.anyio
    async def test_get_person_success(self, sample_gumnut_person, sample_uuid):
        """Test successful person retrieval."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.retrieve.return_value = sample_gumnut_person

            # Execute
            result = await get_person(sample_uuid)

            # Assert
            # Result should be a converted PersonResponseDto
            assert hasattr(result, 'id')
            assert hasattr(result, 'name')
            assert result.name == "Test Person"  # From sample_gumnut_person
            mock_client.people.retrieve.assert_called_once()

    @pytest.mark.anyio
    async def test_get_person_not_found(self, sample_uuid):
        """Test person retrieval when person doesn't exist."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.retrieve.side_effect = Exception("404 Person not found")

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await get_person(sample_uuid)

            assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_get_person_api_error(self, sample_uuid):
        """Test person retrieval with API error."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.retrieve.side_effect = Exception("401 Invalid API key")

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await get_person(sample_uuid)

            assert exc_info.value.status_code == 401


class TestGetPersonStatistics:
    """Test the get_person_statistics endpoint."""

    @pytest.mark.anyio
    async def test_get_person_statistics_success(self, multiple_gumnut_assets, mock_sync_cursor_page, sample_uuid):
        """Test successful person statistics retrieval."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.assets.list.return_value = mock_sync_cursor_page(multiple_gumnut_assets)

            # Execute
            result = await get_person_statistics(sample_uuid)

            # Assert
            assert result.assets == 3  # Number of assets from multiple_gumnut_assets
            mock_client.assets.list.assert_called_once()

    @pytest.mark.anyio
    async def test_get_person_statistics_no_assets(self, mock_sync_cursor_page, sample_uuid):
        """Test person statistics with no assets."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.assets.list.return_value = None

            # Execute
            result = await get_person_statistics(sample_uuid)

            # Assert
            assert result.assets == 0

    @pytest.mark.anyio
    async def test_get_person_statistics_empty_assets(self, mock_sync_cursor_page, sample_uuid):
        """Test person statistics with empty asset list."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.assets.list.return_value = mock_sync_cursor_page([])

            # Execute
            result = await get_person_statistics(sample_uuid)

            # Assert
            assert result.assets == 0

    @pytest.mark.anyio
    async def test_get_person_statistics_not_found(self, sample_uuid):
        """Test person statistics when person doesn't exist."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.assets.list.side_effect = Exception("404 Person not found")

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await get_person_statistics(sample_uuid)

            assert exc_info.value.status_code == 404


class TestDeletePerson:
    """Test the delete_person endpoint."""

    @pytest.mark.anyio
    async def test_delete_person_success(self, sample_uuid):
        """Test successful person deletion."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.delete.return_value = None

            # Execute
            result = await delete_person(sample_uuid)

            # Assert
            assert result.status_code == 204
            mock_client.people.delete.assert_called_once()

    @pytest.mark.anyio
    async def test_delete_person_not_found(self, sample_uuid):
        """Test deletion of non-existent person."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.delete.side_effect = Exception("404 Person not found")

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await delete_person(sample_uuid)

            assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_delete_person_api_error(self, sample_uuid):
        """Test deletion with API error."""
        # Setup - mock only the Gumnut client
        with patch('routers.api.people.get_gumnut_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client
            mock_client.people.delete.side_effect = Exception("401 Invalid API key")

            # Execute & Assert
            with pytest.raises(HTTPException) as exc_info:
                await delete_person(sample_uuid)

            assert exc_info.value.status_code == 401


class TestMergePerson:
    """Test the merge_person endpoint."""

    @pytest.mark.anyio
    async def test_merge_person_stub(self, sample_uuid):
        """Test merge person stub implementation."""
        # Setup
        request = MergePersonDto(ids=[uuid4(), uuid4()])

        # Execute
        result = await merge_person(sample_uuid, request)

        # Assert
        assert result == []  # Stub implementation returns empty list


class TestReassignFaces:
    """Test the reassign_faces endpoint."""

    @pytest.mark.anyio
    async def test_reassign_faces_stub(self, sample_uuid):
        """Test reassign faces stub implementation."""
        # Setup
        request = AssetFaceUpdateDto(data=[])

        # Execute
        result = await reassign_faces(sample_uuid, request)

        # Assert
        assert result == []  # Stub implementation returns empty list