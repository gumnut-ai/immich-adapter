"""Tests for people.py endpoints."""

import pytest
from unittest.mock import AsyncMock, Mock, patch
from fastapi import HTTPException
from uuid import uuid4
from datetime import datetime, timezone, timedelta

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
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_person_id,
    uuid_to_gumnut_asset_id,
    uuid_to_gumnut_person_id,
)
from routers.immich_models import (
    AssetFaceUpdateDto,
    AssetFaceUpdateItem,
    BulkIdsDto,
    Error1,
    MergePersonDto,
    PeopleUpdateDto,
    PeopleUpdateItem,
    PersonCreateDto,
    PersonUpdateDto,
)


def call_get_all_people(**kwargs):
    """Helper function to call get_all_people with proper None defaults for Query parameters."""
    defaults = {
        "closestAssetId": None,
        "closestPersonId": None,
        "page": 1,
        "size": 500,
        "withHidden": None,
    }
    defaults.update(kwargs)
    return get_all_people(**defaults)


class TestCreatePerson:
    """Test the create_person endpoint."""

    @pytest.mark.anyio
    async def test_create_person_success(self, sample_gumnut_person):
        """Test successful person creation."""
        # Setup - mock only the Gumnut client
        mock_client = Mock()
        mock_client.people.create = AsyncMock(return_value=sample_gumnut_person)

        request = PersonCreateDto(
            name="John Doe",
            birthDate=datetime(1990, 1, 1).date(),
            isFavorite=True,
            isHidden=False,
        )

        # Execute
        result = await create_person(request, client=mock_client)

        # Assert
        # Result should be a converted PersonResponseDto
        assert hasattr(result, "id")
        assert hasattr(result, "name")
        assert result.name == "Test Person"  # From sample_gumnut_person
        mock_client.people.create.assert_called_once_with(
            name="John Doe",
            birth_date=datetime(1990, 1, 1).date(),
            is_favorite=True,
            is_hidden=False,
        )

    @pytest.mark.anyio
    async def test_create_person_propagates_auth_error(self):
        """Auth errors bubble up as AuthenticationError."""
        from gumnut import AuthenticationError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.people.create = AsyncMock(
            side_effect=make_sdk_status_error(
                401, "Invalid API key", cls=AuthenticationError
            )
        )

        request = PersonCreateDto(name="John Doe")
        with pytest.raises(AuthenticationError):
            await create_person(request, client=mock_client)

    @pytest.mark.anyio
    async def test_create_person_propagates_5xx(self):
        """Generic SDK errors bubble up to the global GumnutError handler."""
        from gumnut import APIStatusError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.people.create = AsyncMock(
            side_effect=make_sdk_status_error(500, "Unknown error")
        )

        request = PersonCreateDto(name="John Doe")
        with pytest.raises(APIStatusError):
            await create_person(request, client=mock_client)


class TestUpdatePeople:
    """Test the update_people endpoint."""

    @pytest.mark.anyio
    async def test_update_people_success(self):
        """Test successful bulk people update."""
        # Setup - mock only the Gumnut client
        mock_client = Mock()
        mock_client.people.update = AsyncMock(return_value=None)

        person_id1 = str(uuid4())
        person_id2 = str(uuid4())

        person_updates = [
            PeopleUpdateItem(id=person_id1, name="Updated Name 1"),
            PeopleUpdateItem(id=person_id2, name="Updated Name 2"),
        ]
        request = PeopleUpdateDto(people=person_updates)

        # Execute
        result = await update_people(request, client=mock_client)

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
        from gumnut import NotFoundError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()

        # First update succeeds, second fails
        mock_client.people.update = AsyncMock(
            side_effect=[
                None,
                make_sdk_status_error(404, "Person not found", cls=NotFoundError),
            ]
        )

        person_id1 = str(uuid4())
        person_id2 = str(uuid4())

        person_updates = [
            PeopleUpdateItem(id=person_id1, name="Updated Name 1"),
            PeopleUpdateItem(id=person_id2, name="Updated Name 2"),
        ]
        request = PeopleUpdateDto(people=person_updates)

        # Execute
        result = await update_people(request, client=mock_client)

        # Assert
        assert len(result) == 2
        assert result[0].success is True
        assert result[0].error is None
        assert result[0].id == person_id1
        assert result[1].success is False
        assert result[1].error == Error1.not_found
        assert result[1].id == person_id2

    @pytest.mark.anyio
    async def test_update_people_partial_data(self):
        """Test bulk people update with partial data."""
        # Setup - mock only the Gumnut client
        mock_client = Mock()
        mock_client.people.update = AsyncMock(return_value=None)

        person_id1 = str(uuid4())
        person_id2 = str(uuid4())

        # Only updating some fields
        person_updates = [
            PeopleUpdateItem(id=person_id1, name="New Name"),  # Only name
            PeopleUpdateItem(id=person_id2, isFavorite=True),  # Only favorite
        ]
        request = PeopleUpdateDto(people=person_updates)

        # Execute
        result = await update_people(request, client=mock_client)

        # Assert
        assert len(result) == 2
        assert all(item.success is True for item in result)
        assert result[0].id == person_id1
        assert result[1].id == person_id2
        # Check that only non-None fields were passed
        assert mock_client.people.update.call_count == 2

    @pytest.mark.anyio
    async def test_update_people_connection_error_does_not_abort_batch(self):
        """A transport error on one item must not abort the bulk operation."""
        from tests.conftest import make_sdk_connection_error

        mock_client = Mock()
        mock_client.people.update = AsyncMock(
            side_effect=[None, make_sdk_connection_error("PUT")]
        )

        person_id1 = str(uuid4())
        person_id2 = str(uuid4())
        request = PeopleUpdateDto(
            people=[
                PeopleUpdateItem(id=person_id1, name="Good"),
                PeopleUpdateItem(id=person_id2, name="Bad"),
            ]
        )

        result = await update_people(request, client=mock_client)

        assert len(result) == 2
        assert result[0].success is True
        assert result[0].id == person_id1
        assert result[1].success is False
        assert result[1].id == person_id2
        assert result[1].error == Error1.unknown

    @pytest.mark.anyio
    async def test_update_people_malformed_uuid_does_not_abort_batch(self):
        """A malformed UUID in one item must not abort the bulk operation.

        Immich's `PeopleUpdateItem.id` is typed as `str` (the OpenAPI spec
        switches between str and UUID for people ids), so `UUID(...)` can
        raise `ValueError` here even when other items in the batch are
        well-formed.
        """
        mock_client = Mock()
        mock_client.people.update = AsyncMock(return_value=None)

        valid_id = str(uuid4())
        person_updates = [
            PeopleUpdateItem(id="not-a-uuid", name="Bad"),
            PeopleUpdateItem(id=valid_id, name="Good"),
        ]
        request = PeopleUpdateDto(people=person_updates)

        result = await update_people(request, client=mock_client)

        assert len(result) == 2
        assert result[0].success is False
        assert result[0].id == "not-a-uuid"
        assert result[0].error == Error1.unknown
        assert result[1].success is True
        assert result[1].id == valid_id


class TestUpdatePerson:
    """Test the update_person endpoint."""

    @pytest.mark.anyio
    async def test_update_person_success(self, sample_gumnut_person, sample_uuid):
        """Test successful person update."""
        # Setup - mock only the Gumnut client
        mock_client = Mock()
        # Update the sample to have the updated name
        sample_gumnut_person.name = "Updated Name"
        mock_client.people.update = AsyncMock(return_value=sample_gumnut_person)

        request = PersonUpdateDto(name="Updated Name", isFavorite=True)

        # Execute
        result = await update_person(sample_uuid, request, client=mock_client)

        # Assert
        # Result should be a converted PersonResponseDto
        assert hasattr(result, "id")
        assert hasattr(result, "name")
        assert result.name == "Updated Name"
        mock_client.people.update.assert_called_once()

    @pytest.mark.anyio
    async def test_update_person_not_found(self, sample_uuid):
        """A NotFoundError on update bubbles up to the global handler."""
        from gumnut import NotFoundError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.people.update = AsyncMock(
            side_effect=make_sdk_status_error(
                404, "Person not found", cls=NotFoundError
            )
        )

        request = PersonUpdateDto(name="Updated Name")
        with pytest.raises(NotFoundError):
            await update_person(sample_uuid, request, client=mock_client)

    @pytest.mark.anyio
    async def test_update_person_with_feature_face_asset_id(
        self, sample_gumnut_person, sample_uuid
    ):
        """Test updating a person's feature face via featureFaceAssetId."""
        mock_client = Mock()
        sample_gumnut_person.name = "Test Person"
        mock_client.people.update = AsyncMock(return_value=sample_gumnut_person)

        asset_uuid = uuid4()
        mock_face = Mock()
        mock_face.id = "face_abc123"
        mock_faces_page = Mock()
        mock_faces_page.data = [mock_face]
        mock_client.faces.list = AsyncMock(return_value=mock_faces_page)

        request = PersonUpdateDto(featureFaceAssetId=asset_uuid)

        result = await update_person(sample_uuid, request, client=mock_client)

        assert result.name == "Test Person"
        mock_client.faces.list.assert_called_once_with(
            person_id=uuid_to_gumnut_person_id(sample_uuid),
            asset_id=uuid_to_gumnut_asset_id(asset_uuid),
            limit=1,
        )
        mock_client.people.update.assert_called_once_with(
            person_id=uuid_to_gumnut_person_id(sample_uuid),
            thumbnail_face_id="face_abc123",
        )

    @pytest.mark.anyio
    async def test_update_person_feature_face_no_face_found(self, sample_uuid):
        """Test updating feature face when no face exists on the asset."""
        mock_client = Mock()
        mock_faces_page = Mock()
        mock_faces_page.data = []
        mock_client.faces.list = AsyncMock(return_value=mock_faces_page)

        request = PersonUpdateDto(featureFaceAssetId=uuid4())

        with pytest.raises(HTTPException) as exc_info:
            await update_person(sample_uuid, request, client=mock_client)

        assert exc_info.value.status_code == 400
        assert "No face found" in str(exc_info.value.detail)


class TestUpdatePeopleFeatureFace:
    """Test featureFaceAssetId handling in bulk update."""

    @pytest.mark.anyio
    async def test_update_people_with_feature_face_asset_id(self):
        """Test bulk update with featureFaceAssetId."""
        mock_client = Mock()
        mock_client.people.update = AsyncMock(return_value=None)

        person_uuid = uuid4()
        asset_uuid = uuid4()
        mock_face = Mock()
        mock_face.id = "face_xyz789"
        mock_faces_page = Mock()
        mock_faces_page.data = [mock_face]
        mock_client.faces.list = AsyncMock(return_value=mock_faces_page)

        person_updates = [
            PeopleUpdateItem(id=str(person_uuid), featureFaceAssetId=asset_uuid),
        ]
        request = PeopleUpdateDto(people=person_updates)

        result = await update_people(request, client=mock_client)

        assert len(result) == 1
        assert result[0].success is True
        mock_client.faces.list.assert_called_once_with(
            person_id=uuid_to_gumnut_person_id(person_uuid),
            asset_id=uuid_to_gumnut_asset_id(asset_uuid),
            limit=1,
        )
        mock_client.people.update.assert_called_once_with(
            person_id=uuid_to_gumnut_person_id(person_uuid),
            thumbnail_face_id="face_xyz789",
        )

    @pytest.mark.anyio
    async def test_update_people_feature_face_no_face_found(self):
        """Test bulk update with featureFaceAssetId when no face exists on the asset."""
        mock_client = Mock()
        mock_client.people.update = AsyncMock(return_value=None)
        mock_faces_page = Mock()
        mock_faces_page.data = []
        mock_client.faces.list = AsyncMock(return_value=mock_faces_page)

        person_uuid = uuid4()
        asset_uuid = uuid4()
        request = PeopleUpdateDto(
            people=[
                PeopleUpdateItem(id=str(person_uuid), featureFaceAssetId=asset_uuid)
            ]
        )

        result = await update_people(request, client=mock_client)

        mock_client.faces.list.assert_called_once_with(
            person_id=uuid_to_gumnut_person_id(person_uuid),
            asset_id=uuid_to_gumnut_asset_id(asset_uuid),
            limit=1,
        )
        mock_client.people.update.assert_not_called()
        assert len(result) == 1
        assert result[0].success is False
        assert result[0].error == Error1.unknown


class TestGetAllPeople:
    """Test the get_all_people endpoint."""

    @pytest.mark.anyio
    async def test_get_all_people_success(
        self, multiple_gumnut_people, mock_sync_cursor_page
    ):
        """Test successful retrieval of all people."""
        # Setup - mock only the Gumnut client
        mock_client = Mock()
        mock_client.people.list.return_value = mock_sync_cursor_page(
            multiple_gumnut_people
        )

        # Execute
        result = await call_get_all_people(client=mock_client)

        # Assert
        assert len(result.people) == 3
        assert result.total == 3
        assert result.hidden == 0
        assert result.hasNextPage is False  # No pagination in this case
        mock_client.people.list.assert_called_once_with(name_filter="all")

    @pytest.mark.anyio
    async def test_get_all_people_with_pagination(
        self, multiple_gumnut_people, mock_sync_cursor_page
    ):
        """Test people retrieval with pagination."""
        # Setup - mock only the Gumnut client
        mock_client = Mock()
        mock_client.people.list.return_value = mock_sync_cursor_page(
            multiple_gumnut_people
        )

        # Execute - get first page with size 2
        result = await call_get_all_people(page=1, size=2, client=mock_client)

        # Assert
        assert len(result.people) == 2  # Only 2 out of 3
        assert result.total == 3  # But total is still 3
        assert result.hasNextPage is True  # Has next page

    @pytest.mark.anyio
    async def test_get_all_people_without_hidden(
        self, multiple_gumnut_people, mock_sync_cursor_page
    ):
        """Test people retrieval excluding hidden people."""
        # Setup - mock only the Gumnut client
        mock_client = Mock()

        # Mark one person as hidden
        people = multiple_gumnut_people
        people[0].is_hidden = False
        people[1].is_hidden = True  # Hidden
        people[2].is_hidden = False

        mock_client.people.list.return_value = mock_sync_cursor_page(people)

        # Execute
        result = await call_get_all_people(withHidden=False, client=mock_client)

        # Assert
        assert len(result.people) == 2  # Only non-hidden people
        assert result.total == 2  # Total reflects filtered count
        assert result.hidden == 1  # One person was hidden

    @pytest.mark.anyio
    async def test_get_all_people_empty(self, mock_sync_cursor_page):
        """Test people retrieval with no people."""
        # Setup - mock only the Gumnut client
        mock_client = Mock()
        mock_client.people.list.return_value = mock_sync_cursor_page([])

        # Execute
        result = await call_get_all_people(client=mock_client)

        # Assert
        assert len(result.people) == 0
        assert result.total == 0
        assert result.hidden == 0
        assert result.hasNextPage is False

    @pytest.mark.anyio
    async def test_get_all_people_propagates_sdk_error(self):
        """SDK errors bubble up to the global GumnutError handler."""
        from gumnut import APIStatusError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.people.list.side_effect = make_sdk_status_error(500, "boom")

        with pytest.raises(APIStatusError):
            await call_get_all_people(client=mock_client)


def _make_person(
    *,
    name: str | None = None,
    is_hidden: bool = False,
    is_favorite: bool = False,
    asset_count: int | None = 0,
    created_at: datetime | None = None,
) -> Mock:
    """Helper to create a mock Gumnut person with controlled fields for sort tests."""
    person = Mock()
    person.id = uuid_to_gumnut_person_id(uuid4())
    person.name = name
    person.birth_date = None
    person.is_hidden = is_hidden
    person.is_favorite = is_favorite
    person.thumbnail_face_id = None
    person.thumbnail_face_url = None
    person.asset_urls = None
    person.asset_count = asset_count
    person.created_at = created_at or datetime.now(timezone.utc)
    person.updated_at = datetime.now(timezone.utc)
    return person


class TestGetAllPeopleSorting:
    """Test that get_all_people sorts in Immich's expected order."""

    @pytest.mark.anyio
    async def test_visible_people_before_hidden(self, mock_sync_cursor_page):
        """Hidden people should sort after visible ones."""
        hidden = _make_person(name="Alice", is_hidden=True, asset_count=100)
        visible = _make_person(name="Bob", is_hidden=False, asset_count=1)

        mock_client = Mock()
        mock_client.people.list.return_value = mock_sync_cursor_page([hidden, visible])

        result = await call_get_all_people(withHidden=True, client=mock_client)

        assert result.people[0].name == "Bob"
        assert result.people[1].name == "Alice"

    @pytest.mark.anyio
    async def test_favorites_before_non_favorites(self, mock_sync_cursor_page):
        """Favorites should sort before non-favorites."""
        non_fav = _make_person(name="Alice", is_favorite=False, asset_count=100)
        fav = _make_person(name="Bob", is_favorite=True, asset_count=1)

        mock_client = Mock()
        mock_client.people.list.return_value = mock_sync_cursor_page([non_fav, fav])

        result = await call_get_all_people(client=mock_client)

        assert result.people[0].name == "Bob"
        assert result.people[1].name == "Alice"

    @pytest.mark.anyio
    async def test_named_before_unnamed(self, mock_sync_cursor_page):
        """Named people should sort before unnamed ones."""
        unnamed = _make_person(name=None, asset_count=100)
        empty_name = _make_person(name="", asset_count=50)
        named = _make_person(name="Alice", asset_count=1)

        mock_client = Mock()
        mock_client.people.list.return_value = mock_sync_cursor_page(
            [unnamed, empty_name, named]
        )

        result = await call_get_all_people(client=mock_client)

        assert result.people[0].name == "Alice"

    @pytest.mark.anyio
    async def test_higher_asset_count_first(self, mock_sync_cursor_page):
        """People with more assets should sort first within the same tier."""
        few = _make_person(name="Alice", asset_count=5)
        many = _make_person(name="Bob", asset_count=50)

        mock_client = Mock()
        mock_client.people.list.return_value = mock_sync_cursor_page([few, many])

        result = await call_get_all_people(client=mock_client)

        assert result.people[0].name == "Bob"
        assert result.people[1].name == "Alice"

    @pytest.mark.anyio
    async def test_alphabetical_by_name(self, mock_sync_cursor_page):
        """People with the same asset count should sort alphabetically."""
        charlie = _make_person(name="Charlie", asset_count=10)
        alice = _make_person(name="Alice", asset_count=10)
        bob = _make_person(name="Bob", asset_count=10)

        mock_client = Mock()
        mock_client.people.list.return_value = mock_sync_cursor_page(
            [charlie, alice, bob]
        )

        result = await call_get_all_people(client=mock_client)

        assert [p.name for p in result.people] == ["Alice", "Bob", "Charlie"]

    @pytest.mark.anyio
    async def test_created_at_tiebreaker(self, mock_sync_cursor_page):
        """When all other fields match, older people should sort first."""
        now = datetime.now(timezone.utc)

        # Same name and asset count — created_at is the tiebreaker
        newer = _make_person(name="Alice", asset_count=10, created_at=now)
        older = _make_person(
            name="Alice", asset_count=10, created_at=now - timedelta(days=1)
        )

        mock_client = Mock()
        mock_client.people.list.return_value = mock_sync_cursor_page([newer, older])

        result = await call_get_all_people(client=mock_client)

        # Older should come first — identify by person ID
        older_id = str(safe_uuid_from_person_id(older.id))
        newer_id = str(safe_uuid_from_person_id(newer.id))
        assert result.people[0].id == older_id
        assert result.people[1].id == newer_id

    @pytest.mark.anyio
    async def test_full_immich_ordering(self, mock_sync_cursor_page):
        """Test the complete Immich sort order with mixed attributes."""
        # Expected final order (top to bottom):
        # 1. Visible favorite with name and high asset count
        # 2. Visible favorite with name and low asset count
        # 3. Visible non-favorite with name (alphabetically: Alice before Bob)
        # 4. Visible non-favorite unnamed
        # 5. Hidden person
        visible_fav_many = _make_person(name="Zara", is_favorite=True, asset_count=50)
        visible_fav_few = _make_person(name="Yuki", is_favorite=True, asset_count=5)
        visible_alice = _make_person(name="Alice", asset_count=10)
        visible_bob = _make_person(name="Bob", asset_count=10)
        visible_unnamed = _make_person(name=None, asset_count=100)
        hidden_person = _make_person(name="Hidden", is_hidden=True, asset_count=200)

        # Shuffle input order
        shuffled = [
            visible_unnamed,
            hidden_person,
            visible_bob,
            visible_fav_few,
            visible_alice,
            visible_fav_many,
        ]

        mock_client = Mock()
        mock_client.people.list.return_value = mock_sync_cursor_page(shuffled)

        result = await call_get_all_people(withHidden=True, client=mock_client)

        names = [p.name for p in result.people]
        assert names == [
            "Zara",  # visible, favorite, most assets
            "Yuki",  # visible, favorite, fewer assets
            "Alice",  # visible, non-fav, named, 10 assets, alphabetical
            "Bob",  # visible, non-fav, named, 10 assets, alphabetical
            # unnamed person (name is None → converted to "Unknown Person")
            "Unknown Person",
            "Hidden",  # hidden person always last
        ]

    @pytest.mark.anyio
    async def test_hidden_filter_before_pagination(self, mock_sync_cursor_page):
        """Filtering hidden people should happen before pagination slicing."""
        # Create 4 people: 2 hidden, 2 visible
        people = [
            _make_person(name="Visible 1", is_hidden=False, asset_count=20),
            _make_person(name="Hidden 1", is_hidden=True, asset_count=15),
            _make_person(name="Visible 2", is_hidden=False, asset_count=10),
            _make_person(name="Hidden 2", is_hidden=True, asset_count=5),
        ]

        mock_client = Mock()
        mock_client.people.list.return_value = mock_sync_cursor_page(people)

        # Request page 1, size 2 with hidden filtered out
        result = await call_get_all_people(
            page=1, size=2, withHidden=False, client=mock_client
        )

        # Should get both visible people (only 2 exist after filtering)
        assert len(result.people) == 2
        assert result.total == 2
        assert result.hidden == 2
        assert result.hasNextPage is False

    @pytest.mark.anyio
    async def test_none_asset_count_treated_as_zero(self, mock_sync_cursor_page):
        """People with None asset_count should sort as if they have 0."""
        with_count = _make_person(name="Alice", asset_count=5)
        no_count = _make_person(name="Bob", asset_count=None)

        mock_client = Mock()
        mock_client.people.list.return_value = mock_sync_cursor_page(
            [no_count, with_count]
        )

        result = await call_get_all_people(client=mock_client)

        assert result.people[0].name == "Alice"
        assert result.people[1].name == "Bob"


class TestDeletePeople:
    """Test the delete_people endpoint."""

    @pytest.mark.anyio
    async def test_delete_people_success(self):
        """Test successful bulk people deletion."""
        # Setup - mock only the Gumnut client
        mock_client = Mock()
        mock_client.people.delete = AsyncMock(return_value=None)

        person_ids = [uuid4(), uuid4()]
        request = BulkIdsDto(ids=person_ids)

        # Execute
        result = await delete_people(request, client=mock_client)

        # Assert
        assert result.status_code == 204
        assert mock_client.people.delete.call_count == 2

    @pytest.mark.anyio
    async def test_delete_people_not_found_propagates(self):
        """A NotFoundError on delete bubbles up to the global handler."""
        from gumnut import NotFoundError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.people.delete = AsyncMock(
            side_effect=make_sdk_status_error(
                404, "Person not found", cls=NotFoundError
            )
        )

        request = BulkIdsDto(ids=[uuid4()])
        with pytest.raises(NotFoundError):
            await delete_people(request, client=mock_client)

    @pytest.mark.anyio
    async def test_delete_people_propagates_auth_error(self):
        """Auth errors bubble up to the global handler."""
        from gumnut import AuthenticationError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.people.delete = AsyncMock(
            side_effect=make_sdk_status_error(
                401, "Invalid API key", cls=AuthenticationError
            )
        )

        request = BulkIdsDto(ids=[uuid4()])
        with pytest.raises(AuthenticationError):
            await delete_people(request, client=mock_client)


class TestGetThumbnail:
    """Test the get_thumbnail endpoint."""

    @pytest.mark.anyio
    async def test_get_thumbnail_success(self, sample_gumnut_person, sample_uuid):
        """Test successful thumbnail retrieval via CDN."""
        mock_client = Mock()
        mock_client.people.retrieve = AsyncMock(return_value=sample_gumnut_person)
        mock_streaming_response = Mock()

        with patch(
            "routers.api.people.stream_from_cdn", new_callable=AsyncMock
        ) as mock_cdn:
            mock_cdn.return_value = mock_streaming_response
            result = await get_thumbnail(sample_uuid, client=mock_client)

        assert result is mock_streaming_response
        mock_client.people.retrieve.assert_called_once()
        mock_cdn.assert_called_once_with(
            "https://cdn.example.com/person-thumbnail.jpg", "image/jpeg"
        )

    @pytest.mark.anyio
    async def test_get_thumbnail_no_thumbnail(self, sample_gumnut_person, sample_uuid):
        """Test thumbnail retrieval when person has no asset_urls."""
        mock_client = Mock()
        sample_gumnut_person.asset_urls = None
        mock_client.people.retrieve = AsyncMock(return_value=sample_gumnut_person)

        with pytest.raises(HTTPException) as exc_info:
            await get_thumbnail(sample_uuid, client=mock_client)

        assert exc_info.value.status_code == 404
        assert "Person thumbnail not available" in str(exc_info.value.detail)

    @pytest.mark.anyio
    async def test_get_thumbnail_no_thumbnail_key(
        self, sample_gumnut_person, sample_uuid
    ):
        """Test thumbnail retrieval when asset_urls has no thumbnail key."""
        mock_client = Mock()
        sample_gumnut_person.asset_urls = {
            "original": {
                "url": "https://cdn.example.com/orig.jpg",
                "mimetype": "image/jpeg",
            }
        }
        mock_client.people.retrieve = AsyncMock(return_value=sample_gumnut_person)

        with pytest.raises(HTTPException) as exc_info:
            await get_thumbnail(sample_uuid, client=mock_client)

        assert exc_info.value.status_code == 404
        assert "Person thumbnail not available" in str(exc_info.value.detail)

    @pytest.mark.anyio
    async def test_get_thumbnail_person_not_found(self, sample_uuid):
        """A NotFoundError bubbles up to the global handler."""
        from gumnut import NotFoundError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.people.retrieve = AsyncMock(
            side_effect=make_sdk_status_error(
                404, "Person not found", cls=NotFoundError
            )
        )

        with pytest.raises(NotFoundError):
            await get_thumbnail(sample_uuid, client=mock_client)


class TestGetPerson:
    """Test the get_person endpoint."""

    @pytest.mark.anyio
    async def test_get_person_success(self, sample_gumnut_person, sample_uuid):
        """Test successful person retrieval."""
        # Setup - mock only the Gumnut client
        mock_client = Mock()
        mock_client.people.retrieve = AsyncMock(return_value=sample_gumnut_person)

        # Execute
        result = await get_person(sample_uuid, client=mock_client)

        # Assert
        # Result should be a converted PersonResponseDto
        assert hasattr(result, "id")
        assert hasattr(result, "name")
        assert result.name == "Test Person"  # From sample_gumnut_person
        mock_client.people.retrieve.assert_called_once()

    @pytest.mark.anyio
    async def test_get_person_not_found_propagates(self, sample_uuid):
        """A NotFoundError on retrieve bubbles up to the global handler."""
        from gumnut import NotFoundError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.people.retrieve = AsyncMock(
            side_effect=make_sdk_status_error(
                404, "Person not found", cls=NotFoundError
            )
        )

        with pytest.raises(NotFoundError):
            await get_person(sample_uuid, client=mock_client)

    @pytest.mark.anyio
    async def test_get_person_propagates_auth_error(self, sample_uuid):
        """Auth errors bubble up to the global handler."""
        from gumnut import AuthenticationError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.people.retrieve = AsyncMock(
            side_effect=make_sdk_status_error(
                401, "Invalid API key", cls=AuthenticationError
            )
        )

        with pytest.raises(AuthenticationError):
            await get_person(sample_uuid, client=mock_client)


class TestGetPersonStatistics:
    """Test the get_person_statistics endpoint."""

    @pytest.mark.anyio
    async def test_get_person_statistics_success(
        self, multiple_gumnut_assets, mock_sync_cursor_page, sample_uuid
    ):
        """Test successful person statistics retrieval."""
        # Setup - mock only the Gumnut client
        mock_client = Mock()
        mock_client.assets.list.return_value = mock_sync_cursor_page(
            multiple_gumnut_assets
        )

        # Execute
        result = await get_person_statistics(sample_uuid, client=mock_client)

        # Assert
        assert result.assets == 3  # Number of assets from multiple_gumnut_assets
        mock_client.assets.list.assert_called_once()

    @pytest.mark.anyio
    async def test_get_person_statistics_no_assets(
        self, mock_sync_cursor_page, sample_uuid
    ):
        """Test person statistics with no assets."""
        # Setup - mock only the Gumnut client
        mock_client = Mock()
        mock_client.assets.list.return_value = None

        # Execute
        result = await get_person_statistics(sample_uuid, client=mock_client)

        # Assert
        assert result.assets == 0

    @pytest.mark.anyio
    async def test_get_person_statistics_empty_assets(
        self, mock_sync_cursor_page, sample_uuid
    ):
        """Test person statistics with empty asset list."""
        # Setup - mock only the Gumnut client
        mock_client = Mock()
        mock_client.assets.list.return_value = mock_sync_cursor_page([])

        # Execute
        result = await get_person_statistics(sample_uuid, client=mock_client)

        # Assert
        assert result.assets == 0

    @pytest.mark.anyio
    async def test_get_person_statistics_not_found_propagates(self, sample_uuid):
        """A NotFoundError on assets.list bubbles up to the global handler."""
        from gumnut import NotFoundError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.assets.list.side_effect = make_sdk_status_error(
            404, "Person not found", cls=NotFoundError
        )

        with pytest.raises(NotFoundError):
            await get_person_statistics(sample_uuid, client=mock_client)


class TestDeletePerson:
    """Test the delete_person endpoint."""

    @pytest.mark.anyio
    async def test_delete_person_success(self, sample_uuid):
        """Test successful person deletion."""
        # Setup - mock only the Gumnut client
        mock_client = Mock()
        mock_client.people.delete = AsyncMock(return_value=None)

        # Execute
        result = await delete_person(sample_uuid, client=mock_client)

        # Assert
        assert result.status_code == 204
        mock_client.people.delete.assert_called_once()

    @pytest.mark.anyio
    async def test_delete_person_not_found_propagates(self, sample_uuid):
        """A NotFoundError on delete bubbles up to the global handler."""
        from gumnut import NotFoundError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.people.delete = AsyncMock(
            side_effect=make_sdk_status_error(
                404, "Person not found", cls=NotFoundError
            )
        )

        with pytest.raises(NotFoundError):
            await delete_person(sample_uuid, client=mock_client)

    @pytest.mark.anyio
    async def test_delete_person_propagates_auth_error(self, sample_uuid):
        """Auth errors bubble up to the global handler."""
        from gumnut import AuthenticationError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.people.delete = AsyncMock(
            side_effect=make_sdk_status_error(
                401, "Invalid API key", cls=AuthenticationError
            )
        )

        with pytest.raises(AuthenticationError):
            await delete_person(sample_uuid, client=mock_client)


class TestMergePerson:
    """Test the merge_person endpoint."""

    @pytest.mark.anyio
    async def test_merge_person_empty_ids(self, sample_uuid):
        """Empty ids list short-circuits without an upstream call."""
        mock_client = Mock()
        mock_client.people.merge = AsyncMock()

        request = MergePersonDto(ids=[])

        result = await merge_person(sample_uuid, request, client=mock_client)

        assert result == []
        mock_client.people.merge.assert_not_called()

    @pytest.mark.anyio
    async def test_merge_person_self_merge_rejected(self, sample_uuid):
        """Including the target id in the source list returns 400 before any upstream call."""
        mock_client = Mock()
        mock_client.people.merge = AsyncMock()

        request = MergePersonDto(ids=[uuid4(), sample_uuid])

        with pytest.raises(HTTPException) as exc_info:
            await merge_person(sample_uuid, request, client=mock_client)

        assert exc_info.value.status_code == 400
        mock_client.people.merge.assert_not_called()

    @pytest.mark.anyio
    async def test_merge_person_single_source_success(
        self, sample_uuid, sample_gumnut_person
    ):
        """A single source: one upstream merge call, all-success result."""
        target_uuid = sample_uuid
        source_uuid = uuid4()

        mock_client = Mock()
        mock_client.people.merge = AsyncMock(return_value=sample_gumnut_person)

        request = MergePersonDto(ids=[source_uuid])

        result = await merge_person(target_uuid, request, client=mock_client)

        assert len(result) == 1
        assert result[0].id == str(source_uuid)
        assert result[0].success is True
        assert result[0].error is None

        mock_client.people.merge.assert_called_once_with(
            uuid_to_gumnut_person_id(target_uuid),
            source_person_ids=[uuid_to_gumnut_person_id(source_uuid)],
        )

    @pytest.mark.anyio
    async def test_merge_person_multiple_sources_success(
        self, sample_uuid, sample_gumnut_person
    ):
        """Multiple sources go through a single atomic upstream merge call."""
        target_uuid = sample_uuid
        source_1 = uuid4()
        source_2 = uuid4()

        mock_client = Mock()
        mock_client.people.merge = AsyncMock(return_value=sample_gumnut_person)

        request = MergePersonDto(ids=[source_1, source_2])

        result = await merge_person(target_uuid, request, client=mock_client)

        assert len(result) == 2
        assert all(r.success for r in result)
        assert [r.id for r in result] == [str(source_1), str(source_2)]

        mock_client.people.merge.assert_called_once_with(
            uuid_to_gumnut_person_id(target_uuid),
            source_person_ids=[
                uuid_to_gumnut_person_id(source_1),
                uuid_to_gumnut_person_id(source_2),
            ],
        )

    @pytest.mark.anyio
    async def test_merge_person_upstream_error_bubbles(self, sample_uuid):
        """Upstream errors bubble to the global GumnutError handler."""
        from gumnut import NotFoundError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.people.merge = AsyncMock(
            side_effect=make_sdk_status_error(
                404, "Person not found", cls=NotFoundError
            )
        )

        request = MergePersonDto(ids=[uuid4()])

        with pytest.raises(NotFoundError):
            await merge_person(sample_uuid, request, client=mock_client)


class TestReassignFaces:
    """Test the reassign_faces endpoint."""

    @pytest.mark.anyio
    async def test_reassign_faces_empty_data(self, sample_uuid):
        """Test reassign with no face items returns empty list without API calls."""
        mock_client = Mock()
        request = AssetFaceUpdateDto(data=[])

        result = await reassign_faces(sample_uuid, request, client=mock_client)

        assert result == []
        # No API calls should be made for empty data
        mock_client.people.retrieve.assert_not_called()

    @pytest.mark.anyio
    async def test_reassign_faces_success(
        self, sample_uuid, sample_gumnut_person, mock_sync_cursor_page
    ):
        """Test successful face reassignment.

        URL {id} (sample_uuid) is the target person.
        Body personId (source_uuid) is the source person (current face owner).
        """
        target_uuid = sample_uuid  # URL param = target
        source_uuid = uuid4()  # body personId = source
        asset_uuid = uuid4()

        # Mock face found on source person's asset
        mock_face = Mock()
        mock_face.id = "face_abc123"
        mock_face.person_id = uuid_to_gumnut_person_id(source_uuid)

        mock_client = Mock()
        mock_client.faces.list = Mock(return_value=mock_sync_cursor_page([mock_face]))
        mock_client.faces.update = AsyncMock()
        mock_client.people.retrieve = AsyncMock(return_value=sample_gumnut_person)

        request = AssetFaceUpdateDto(
            data=[AssetFaceUpdateItem(assetId=asset_uuid, personId=source_uuid)]
        )

        result = await reassign_faces(target_uuid, request, client=mock_client)

        # Verify face was looked up by source person (body) + asset
        mock_client.faces.list.assert_called_once_with(
            person_id=uuid_to_gumnut_person_id(source_uuid),
            asset_id=uuid_to_gumnut_asset_id(asset_uuid),
        )
        # Verify face was reassigned to target person (URL)
        mock_client.faces.update.assert_called_once_with(
            "face_abc123", person_id=uuid_to_gumnut_person_id(target_uuid)
        )
        # Verify target person was fetched and returned
        assert len(result) == 1
        assert hasattr(result[0], "name")

    @pytest.mark.anyio
    async def test_reassign_faces_no_face_found_skips(
        self, sample_uuid, sample_gumnut_person, mock_sync_cursor_page
    ):
        """Test that missing faces are skipped without error."""
        target_uuid = sample_uuid  # URL param = target
        source_uuid = uuid4()  # body personId = source
        asset_uuid = uuid4()

        mock_client = Mock()
        mock_client.people.retrieve = AsyncMock(return_value=sample_gumnut_person)
        mock_client.faces.list = Mock(
            return_value=mock_sync_cursor_page([])  # No face found
        )
        mock_client.faces.update = AsyncMock()

        request = AssetFaceUpdateDto(
            data=[AssetFaceUpdateItem(assetId=asset_uuid, personId=source_uuid)]
        )

        result = await reassign_faces(target_uuid, request, client=mock_client)

        # Face update should not have been called
        mock_client.faces.update.assert_not_called()
        assert result == []

    @pytest.mark.anyio
    async def test_reassign_faces_api_error(self, sample_uuid, sample_gumnut_person):
        """Test error handling during reassignment."""
        target_uuid = sample_uuid  # URL param = target
        source_uuid = uuid4()  # body personId = source
        asset_uuid = uuid4()

        from gumnut import APIStatusError
        from tests.conftest import make_sdk_status_error

        mock_client = Mock()
        mock_client.people.retrieve = AsyncMock(return_value=sample_gumnut_person)
        mock_client.faces.list = Mock(
            side_effect=make_sdk_status_error(500, "Internal Server Error")
        )

        request = AssetFaceUpdateDto(
            data=[AssetFaceUpdateItem(assetId=asset_uuid, personId=source_uuid)]
        )

        with pytest.raises(APIStatusError):
            await reassign_faces(target_uuid, request, client=mock_client)

    @pytest.mark.anyio
    async def test_reassign_faces_multiple_faces_on_asset(
        self, sample_uuid, sample_gumnut_person, mock_sync_cursor_page
    ):
        """All faces for (source person, asset) should be reassigned, not just the first."""
        target_uuid = sample_uuid  # URL param = target
        source_uuid = uuid4()  # body personId = source
        asset_uuid = uuid4()

        mock_face_1 = Mock()
        mock_face_1.id = "face_aaa"
        mock_face_2 = Mock()
        mock_face_2.id = "face_bbb"

        mock_client = Mock()
        mock_client.faces.list = Mock(
            return_value=mock_sync_cursor_page([mock_face_1, mock_face_2])
        )
        mock_client.faces.update = AsyncMock()
        mock_client.people.retrieve = AsyncMock(return_value=sample_gumnut_person)

        request = AssetFaceUpdateDto(
            data=[AssetFaceUpdateItem(assetId=asset_uuid, personId=source_uuid)]
        )

        result = await reassign_faces(target_uuid, request, client=mock_client)

        # Both faces should be updated to the target person (URL)
        assert mock_client.faces.update.call_count == 2
        mock_client.faces.update.assert_any_call(
            "face_aaa", person_id=uuid_to_gumnut_person_id(target_uuid)
        )
        mock_client.faces.update.assert_any_call(
            "face_bbb", person_id=uuid_to_gumnut_person_id(target_uuid)
        )
        assert len(result) == 1

    @pytest.mark.anyio
    async def test_reassign_faces_multiple_sources_to_single_target(
        self, sample_uuid, sample_gumnut_person, mock_sync_cursor_page
    ):
        """Multiple items with different source persons all reassign to the URL target."""
        target_uuid = sample_uuid  # URL param = single target
        source_1 = uuid4()
        source_2 = uuid4()
        asset_a = uuid4()
        asset_b = uuid4()
        asset_c = uuid4()

        mock_face_a = Mock(id="face_a")
        mock_face_b = Mock(id="face_b")
        mock_face_c = Mock(id="face_c")

        # Return one face per asset
        mock_client = Mock()
        mock_client.people.retrieve = AsyncMock(return_value=sample_gumnut_person)
        mock_client.faces.list = Mock(
            side_effect=[
                mock_sync_cursor_page([mock_face_a]),
                mock_sync_cursor_page([mock_face_b]),
                mock_sync_cursor_page([mock_face_c]),
            ]
        )
        mock_client.faces.update = AsyncMock()

        # Request: faces from source_1 and source_2 across 3 assets
        request = AssetFaceUpdateDto(
            data=[
                AssetFaceUpdateItem(assetId=asset_a, personId=source_1),
                AssetFaceUpdateItem(assetId=asset_b, personId=source_2),
                AssetFaceUpdateItem(assetId=asset_c, personId=source_1),
            ]
        )

        result = await reassign_faces(target_uuid, request, client=mock_client)

        # All 3 faces updated to the target
        assert mock_client.faces.update.call_count == 3
        for call in mock_client.faces.update.call_args_list:
            assert call[1]["person_id"] == uuid_to_gumnut_person_id(target_uuid)

        # Faces looked up by source person IDs
        list_calls = mock_client.faces.list.call_args_list
        assert list_calls[0][1]["person_id"] == uuid_to_gumnut_person_id(source_1)
        assert list_calls[1][1]["person_id"] == uuid_to_gumnut_person_id(source_2)
        assert list_calls[2][1]["person_id"] == uuid_to_gumnut_person_id(source_1)

        # Result: target person returned once (not per item)
        assert len(result) == 1
        assert hasattr(result[0], "name")

        # Target person fetched once upfront
        assert mock_client.people.retrieve.call_count == 1
