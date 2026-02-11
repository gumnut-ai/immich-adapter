"""Shared fixtures and helpers for sync tests."""

import json
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from routers.utils.gumnut_id_conversion import (
    uuid_to_gumnut_album_id,
    uuid_to_gumnut_asset_id,
    uuid_to_gumnut_face_id,
    uuid_to_gumnut_person_id,
    uuid_to_gumnut_user_id,
)
from services.checkpoint_store import CheckpointStore
from services.session_store import Session, SessionStore

# Shared test constants
TEST_UUID = UUID("12345678-1234-1234-1234-123456789abc")
TEST_SESSION_UUID = UUID("550e8400-e29b-41d4-a716-446655440000")


@pytest.fixture
def test_uuid():
    """Provide the standard test UUID."""
    return TEST_UUID


@pytest.fixture
def test_session_uuid():
    """Provide the standard test session UUID."""
    return TEST_SESSION_UUID


def create_mock_user(updated_at: datetime) -> Mock:
    """Create a mock Gumnut user."""
    user = Mock()
    user.id = uuid_to_gumnut_user_id(TEST_UUID)
    user.email = "test@example.com"
    user.first_name = "Test"
    user.last_name = "User"
    user.is_superuser = False
    user.updated_at = updated_at
    return user


def create_mock_gumnut_client(user: Mock) -> Mock:
    """Create a mock Gumnut client with the given user."""
    client = Mock()
    client.users.me.return_value = user
    # Default: no v2 events
    events_response = Mock()
    events_response.data = []
    events_response.has_more = False
    client.events_v2.get.return_value = events_response
    # Default: empty entity list responses for batch fetching
    empty_page = Mock()
    empty_page.__iter__ = Mock(return_value=iter([]))
    client.assets.list.return_value = empty_page
    client.albums.list.return_value = empty_page
    client.people.list.return_value = empty_page
    client.faces.list.return_value = empty_page
    return client


def create_mock_session(
    session_uuid: UUID = TEST_SESSION_UUID,
    is_pending_sync_reset: bool = False,
) -> Session:
    """Create a mock Session object."""
    return Session(
        id=session_uuid,
        user_id="user-123",
        library_id="lib-123",
        stored_jwt="encrypted-jwt",
        device_type="iOS",
        device_os="iOS 17",
        app_version="1.94.0",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        is_pending_sync_reset=is_pending_sync_reset,
    )


def create_mock_checkpoint_store() -> AsyncMock:
    """Create a mock CheckpointStore."""
    store = AsyncMock(spec=CheckpointStore)
    store.get_all.return_value = []
    return store


def create_mock_session_store(session: Session | None = None) -> AsyncMock:
    """Create a mock SessionStore."""
    store = AsyncMock(spec=SessionStore)
    store.get_by_id.return_value = session
    return store


async def collect_stream(stream: AsyncGenerator[str, None]) -> list[dict]:
    """Collect all events from an async generator into a list of dicts."""
    events = []
    async for line in stream:
        events.append(json.loads(line.strip()))
    return events


def create_mock_v2_event(
    entity_type: str,
    entity_id: str,
    event_type: str,
    created_at: datetime,
    cursor: str = "cursor_1",
) -> Mock:
    """Create a mock v2 event."""
    event = Mock()
    event.entity_type = entity_type
    event.entity_id = entity_id
    event.event_type = event_type
    event.created_at = created_at
    event.cursor = cursor
    return event


def create_mock_v2_events_response(events: list, has_more: bool = False) -> Mock:
    """Create a mock v2 events response."""
    resp = Mock()
    resp.data = events
    resp.has_more = has_more
    return resp


def create_mock_asset_data(updated_at: datetime) -> Mock:
    """Create mock asset data for entity fetch."""
    asset = Mock()
    asset.id = uuid_to_gumnut_asset_id(TEST_UUID)
    asset.mime_type = "image/jpeg"
    asset.original_file_name = "test.jpg"
    asset.local_datetime = updated_at
    asset.file_created_at = updated_at
    asset.file_modified_at = updated_at
    asset.updated_at = updated_at
    asset.checksum = "abc123"
    asset.checksum_sha1 = "sha1checksum"
    asset.width = 1920
    asset.height = 1080
    asset.exif = None
    return asset


def create_mock_album_data(updated_at: datetime) -> Mock:
    """Create mock album data for entity fetch."""
    album = Mock()
    album.id = uuid_to_gumnut_album_id(TEST_UUID)
    album.name = "Test Album"
    album.description = "Test Description"
    album.created_at = updated_at
    album.updated_at = updated_at
    album.album_cover_asset_id = None
    return album


def create_mock_exif_data(updated_at: datetime) -> Mock:
    """Create mock exif data for entity fetch."""
    exif = Mock()
    exif.asset_id = uuid_to_gumnut_asset_id(TEST_UUID)
    exif.city = "San Francisco"
    exif.country = "USA"
    exif.state = "California"
    exif.description = None
    exif.original_datetime = updated_at
    exif.modified_datetime = None
    exif.exposure_time = 0.01
    exif.f_number = 2.8
    exif.focal_length = 50.0
    exif.iso = 100
    exif.latitude = 37.7749
    exif.longitude = -122.4194
    exif.lens_model = "50mm f/1.8"
    exif.make = "Canon"
    exif.model = "EOS R5"
    exif.orientation = 1
    exif.profile_description = None
    exif.projection_type = None
    exif.rating = None
    exif.fps = None
    exif.updated_at = updated_at
    return exif


def create_mock_person_data(updated_at: datetime) -> Mock:
    """Create mock person data for entity fetch."""
    person = Mock()
    person.id = uuid_to_gumnut_person_id(TEST_UUID)
    person.name = "Test Person"
    person.is_favorite = False
    person.is_hidden = False
    person.created_at = updated_at
    person.updated_at = updated_at
    return person


def create_mock_face_data(updated_at: datetime) -> Mock:
    """Create mock face data for entity fetch."""
    face = Mock()
    face.id = uuid_to_gumnut_face_id(TEST_UUID)
    face.asset_id = uuid_to_gumnut_asset_id(TEST_UUID)
    face.person_id = uuid_to_gumnut_person_id(TEST_UUID)
    face.bounding_box = {"x": 100, "y": 100, "w": 50, "h": 50}
    face.updated_at = updated_at
    return face


def create_mock_entity_page(entities: list) -> Mock:
    """Create a mock paginated entity response that is iterable."""
    page = Mock()
    page.__iter__ = Mock(return_value=iter(entities))
    return page


__all__ = [
    "TEST_UUID",
    "TEST_SESSION_UUID",
    "create_mock_user",
    "create_mock_gumnut_client",
    "create_mock_session",
    "create_mock_checkpoint_store",
    "create_mock_session_store",
    "collect_stream",
    "create_mock_v2_event",
    "create_mock_v2_events_response",
    "create_mock_asset_data",
    "create_mock_album_data",
    "create_mock_exif_data",
    "create_mock_person_data",
    "create_mock_face_data",
    "create_mock_entity_page",
]
