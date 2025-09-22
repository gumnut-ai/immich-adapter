"""Test configuration and shared fixtures."""

import pytest
from unittest.mock import Mock, patch
from datetime import datetime, timezone
from uuid import uuid4
from typing import List, Any

# Configure anyio to use only asyncio backend
pytest_plugins = ("anyio",)


@pytest.fixture(scope="session")
def anyio_backend():
    """Force asyncio backend for all tests."""
    return "asyncio"


@pytest.fixture
def mock_gumnut_client():
    """Mock the Gumnut client to avoid actual API calls."""
    with patch('routers.api.albums.get_gumnut_client') as mock_get_client:
        client = Mock()
        mock_get_client.return_value = client
        yield client


@pytest.fixture
def sample_uuid():
    """Generate a sample UUID for testing."""
    return uuid4()


@pytest.fixture
def sample_gumnut_album():
    """Create a sample Gumnut album object with proper date fields."""
    album = Mock()
    album.id = "gumnut-album-123"
    album.name = "Test Album"
    album.description = "Test Description"
    album.created_at = datetime.now(timezone.utc)
    album.updated_at = datetime.now(timezone.utc)
    album.asset_count = 5
    return album


@pytest.fixture
def sample_gumnut_asset():
    """Create a sample Gumnut asset object with proper date fields."""
    asset = Mock()
    asset.id = "gumnut-asset-456"
    asset.device_asset_id = "device-123"
    asset.device_id = "device-456"
    asset.file_created_at = datetime.now(timezone.utc)
    asset.file_modified_at = datetime.now(timezone.utc)
    asset.created_at = datetime.now(timezone.utc)
    asset.updated_at = datetime.now(timezone.utc)
    asset.mime_type = "image/jpeg"
    asset.original_filename = "test.jpg"
    asset.original_file_name = "test.jpg"
    asset.duration_in_seconds = None
    asset.library_id = "library-789"
    asset.checksum = "abc123"
    asset.people = []  # Empty list for people
    asset.exif = None  # No EXIF data
    return asset


@pytest.fixture
def multiple_gumnut_albums():
    """Create multiple Gumnut albums for list testing with proper date fields."""
    albums = []
    for i in range(3):
        album = Mock()
        album.id = f"gumnut-album-{i}"
        album.name = f"Test Album {i}"
        album.description = f"Test Description {i}"
        album.created_at = datetime.now(timezone.utc)
        album.updated_at = datetime.now(timezone.utc)
        album.asset_count = i + 1
        albums.append(album)
    return albums


@pytest.fixture
def multiple_gumnut_assets():
    """Create multiple Gumnut assets for list testing with proper date fields."""
    assets = []
    for i in range(3):
        asset = Mock()
        asset.id = f"gumnut-asset-{i}"
        asset.device_asset_id = f"device-{i}"
        asset.device_id = f"device-{i}"
        asset.file_created_at = datetime.now(timezone.utc)
        asset.file_modified_at = datetime.now(timezone.utc)
        asset.mime_type = "image/jpeg"
        asset.original_filename = f"test{i}.jpg"
        asset.duration_in_seconds = None
        asset.library_id = "library-789"
        assets.append(asset)
    return assets


class MockSyncCursorPage:
    """Mock for Gumnut SyncCursorPage response."""
    def __init__(self, items: List[Any]):
        self.items = items

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)


@pytest.fixture
def mock_sync_cursor_page():
    """Factory for creating mock SyncCursorPage objects."""
    def _create_page(items: List[Any]):
        return MockSyncCursorPage(items)
    return _create_page


@pytest.fixture
def sample_gumnut_person():
    """Create a sample Gumnut person object with proper fields."""
    person = Mock()
    person.id = "gumnut-person-123"
    person.name = "Test Person"
    person.birth_date = datetime(1990, 1, 1).date()
    person.is_favorite = False
    person.is_hidden = False
    person.thumbnail_face_id = "face-456"
    person.thumbnail_face_url = "https://example.com/thumbnail.jpg"
    person.created_at = datetime.now(timezone.utc)
    person.updated_at = datetime.now(timezone.utc)
    return person


@pytest.fixture
def multiple_gumnut_people():
    """Create multiple Gumnut people for list testing."""
    people = []
    for i in range(3):
        person = Mock()
        person.id = f"gumnut-person-{i}"
        person.name = f"Test Person {i}"
        person.birth_date = datetime(1990 + i, 1, 1).date()
        person.is_favorite = i % 2 == 0  # Alternate favorites
        person.is_hidden = False  # Default to not hidden
        person.thumbnail_face_id = f"face-{i}"
        person.thumbnail_face_url = f"https://example.com/thumbnail-{i}.jpg"
        person.created_at = datetime.now(timezone.utc)
        person.updated_at = datetime.now(timezone.utc)
        people.append(person)
    return people


