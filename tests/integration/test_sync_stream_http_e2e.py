"""
True end-to-end test for sync stream endpoint via HTTP.

This test makes actual HTTP requests through the full FastAPI stack:
- HTTP request -> Router -> AuthMiddleware -> Dependencies -> Handler -> Response

The only thing mocked is the Gumnut SDK (backend involves multiple servers).

Test data is derived from real Proxyman captures.
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from gumnut.types.album_asset_event_payload import AlbumAssetEventPayload
from gumnut.types.album_asset_response import AlbumAssetResponse
from gumnut.types.album_event_payload import AlbumEventPayload
from gumnut.types.album_response import AlbumResponse
from gumnut.types.asset_event_payload import AssetEventPayload
from gumnut.types.asset_response import AssetResponse
from gumnut.types.exif_event_payload import ExifEventPayload
from gumnut.types.exif_response import ExifResponse
from gumnut.types.face_event_payload import FaceEventPayload
from gumnut.types.face_response import FaceResponse
from gumnut.types.person_event_payload import PersonEventPayload
from gumnut.types.person_response import PersonResponse
from gumnut.types.user_response import UserResponse
from main import app
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from services.checkpoint_store import CheckpointStore, get_checkpoint_store
from services.session_store import Session, SessionStore, get_session_store

# Test constants
TEST_SESSION_UUID = UUID("550e8400-e29b-41d4-a716-446655440000")
TEST_USER_UUID = UUID("660e8400-e29b-41d4-a716-446655440001")
# Generated via: uuid_to_gumnut_user_id(TEST_USER_UUID)
TEST_GUMNUT_USER_ID = "intuser_LB2VTcVgieV6LNawiPgXG5"

# Test album data
TEST_ALBUMS_DATA = [
    {
        "id": "album_R99NCS4MdYwULi9UyBwy4z",
        "name": "Just Ten",
        "description": "",
        "asset_count": 10,
        "album_cover_asset_id": "asset_W6vgV5FDoWHuKN7Ao95VVJ",
        "start_date": "2011-05-07T16:19:38.900000Z",
        "end_date": "2024-02-25T06:57:27.660000-08:00",
        "created_at": "2026-01-21T18:01:55.158275Z",
        "updated_at": "2026-01-21T18:01:55.158275Z",
    }
]

# Test asset data
TEST_ASSETS_DATA = [
    {
        "id": "asset_EVDYwYYmiDoCVZiZnA5k5w",
        "device_asset_id": "web-DSC_0652.jpg-1767394994000",
        "device_id": "WEB",
        "mime_type": "image/jpeg",
        "original_file_name": "DSC_0652.jpg",
        "file_created_at": "2026-01-02T23:03:14Z",
        "file_modified_at": "2026-01-02T23:03:14Z",
        "local_datetime": "2024-02-25T06:57:27.660000-08:00",
        "checksum": "DR00pgYMC13XiSPf+jNy26nU7l/jzvMVLaB5EBRZQOA=",
        "checksum_sha1": "PaDX6+c+Lhjpm5/ciXUROL1ryaU=",
        "exif": {
            "make": "NIKON CORPORATION",
            "model": "NIKON Z 6_2",
            "lens_model": "NIKKOR Z 70-200mm f/2.8 VR S",
            "f_number": 8.0,
            "focal_length": 200.0,
            "iso": 100,
            "exposure_time": 0.00025,
        },
        "faces": [],
    },
    {
        "id": "asset_mgeB2TaTirHHP5HFZyspud",
        "device_asset_id": "web-DSC_0421.jpg-1767394992000",
        "device_id": "WEB",
        "mime_type": "image/jpeg",
        "original_file_name": "DSC_0421.jpg",
        "file_created_at": "2026-01-02T23:03:12Z",
        "file_modified_at": "2026-01-02T23:03:12Z",
        "local_datetime": "2024-02-24T08:03:10.290000-08:00",
        "checksum": "yPtguJL00ZTGGaaK0QN1uGQILEltlX4lZvZgzVowLZw=",
        "checksum_sha1": "brALG7dXmBNlyka47z1l2mBICXQ=",
        "exif": {
            "make": "NIKON CORPORATION",
            "model": "NIKON Z 6_2",
            "lens_model": "NIKKOR Z 70-200mm f/2.8 VR S",
            "f_number": 6.3,
            "focal_length": 200.0,
            "iso": 100,
            "exposure_time": 0.0015625,
        },
        "faces": [],
    },
    {
        "id": "asset_exqhM8woUg6HB6C9G2sLNq",
        "device_asset_id": "web-DSC_9471.jpg-1767397638000",
        "device_id": "WEB",
        "mime_type": "image/jpeg",
        "original_file_name": "DSC_9471.jpg",
        "file_created_at": "2026-01-02T23:47:18Z",
        "file_modified_at": "2026-01-02T23:47:18Z",
        "local_datetime": "2023-10-12T13:36:25.510000-07:00",
        "checksum": "nJ7tPpZocEqFkgfg8oVxdRkP76erAVaDuQdsZ2Vhwwo=",
        "checksum_sha1": "AO8u7EyZx+TpfVcc1ccu/BbXTBM=",
        "exif": {
            "make": "NIKON CORPORATION",
            "model": "NIKON Z 6_2",
            "lens_model": "NIKKOR Z 70-200mm f/2.8 VR S",
            "f_number": 2.8,
            "focal_length": 104.0,
            "iso": 1000,
            "exposure_time": 0.005,
        },
        "faces": [
            {
                "id": "face_nxS3BTriNXmqzFFW8ADKzf",
                "bounding_box": {"h": 245, "w": 175, "x": 665, "y": 122},
                "person_id": None,
            }
        ],
    },
    {
        "id": "asset_W6vgV5FDoWHuKN7Ao95VVJ",
        "device_asset_id": "web-DSC_4981-Edit.jpg-1767395034000",
        "device_id": "WEB",
        "mime_type": "image/jpeg",
        "original_file_name": "DSC_4981-Edit.jpg",
        "file_created_at": "2026-01-02T23:03:54Z",
        "file_modified_at": "2026-01-02T23:03:54Z",
        "local_datetime": "2011-05-07T16:19:38.900000Z",
        "checksum": "uUFqCn/vc8B8GiOtWRc+uRWjL4jg67nHNGw92WmFz+o=",
        "checksum_sha1": "Ry1EUP0D2lTMKQo+GlFrCKJFoDk=",
        "exif": {
            "make": "NIKON CORPORATION",
            "model": "NIKON D7000",
            "lens_model": "70.0-300.0 mm f/4.5-5.6",
            "f_number": 8.0,
            "focal_length": 300.0,
            "iso": 200,
            "exposure_time": 0.0015625,
        },
        "faces": [
            {
                "id": "face_FFPNgTzFWwM425xvoZjvjk",
                "bounding_box": {"h": 106, "w": 74, "x": 1443, "y": 528},
                "person_id": None,
            }
        ],
    },
]

# Expected exposure time outputs for Immich (input float -> output string)
# These are the known correct transformations for the test data above
EXPECTED_EXPOSURE_TIMES = {
    "asset_EVDYwYYmiDoCVZiZnA5k5w": "1/4000",  # 0.00025
    "asset_mgeB2TaTirHHP5HFZyspud": "1/640",  # 0.0015625
    "asset_exqhM8woUg6HB6C9G2sLNq": "1/200",  # 0.005
    "asset_W6vgV5FDoWHuKN7Ao95VVJ": "1/640",  # 0.0015625
}


def parse_datetime(dt_str: str) -> datetime:
    """Parse a datetime string handling various formats."""
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1] + "+00:00"
    return datetime.fromisoformat(dt_str)


def create_asset_response(asset_data: dict) -> AssetResponse:
    """Create an AssetResponse from test data."""
    return AssetResponse(
        id=asset_data["id"],
        device_asset_id=asset_data["device_asset_id"],
        device_id=asset_data["device_id"],
        mime_type=asset_data["mime_type"],
        original_file_name=asset_data["original_file_name"],
        file_created_at=parse_datetime(asset_data["file_created_at"]),
        file_modified_at=parse_datetime(asset_data["file_modified_at"]),
        local_datetime=parse_datetime(asset_data["local_datetime"]),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        checksum=asset_data["checksum"],
        checksum_sha1=asset_data.get("checksum_sha1"),
    )


def create_exif_response(asset_id: str, exif_data: dict) -> ExifResponse:
    """Create an ExifResponse from test data."""
    return ExifResponse(
        asset_id=asset_id,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        make=exif_data.get("make"),
        model=exif_data.get("model"),
        lens_model=exif_data.get("lens_model"),
        f_number=exif_data.get("f_number"),
        focal_length=exif_data.get("focal_length"),
        iso=exif_data.get("iso"),
        exposure_time=exif_data.get("exposure_time"),
    )


def create_face_response(asset_id: str, face_data: dict) -> FaceResponse:
    """Create a FaceResponse from test data."""
    return FaceResponse(
        id=face_data["id"],
        asset_id=asset_id,
        bounding_box=face_data["bounding_box"],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        person_id=face_data.get("person_id"),
    )


def create_album_response(album_data: dict) -> AlbumResponse:
    """Create an AlbumResponse from test data."""
    return AlbumResponse(
        id=album_data["id"],
        name=album_data["name"],
        asset_count=album_data["asset_count"],
        created_at=parse_datetime(album_data["created_at"]),
        updated_at=parse_datetime(album_data["updated_at"]),
        description=album_data.get("description", ""),
        album_cover_asset_id=album_data.get("album_cover_asset_id"),
        start_date=parse_datetime(album_data["start_date"]),
        end_date=parse_datetime(album_data["end_date"]),
    )


def create_album_asset_response(
    album_id: str,
    asset_id: str,
) -> AlbumAssetResponse:
    """Create an AlbumAssetResponse from test data."""
    return AlbumAssetResponse(
        id=f"album_asset_{album_id}_{asset_id}",
        album_id=album_id,
        asset_id=asset_id,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def create_person_response(
    person_id: str,
    name: str,
    thumbnail_face_id: str | None = None,
) -> PersonResponse:
    """Create a PersonResponse from test data."""
    return PersonResponse(
        id=person_id,
        name=name,
        is_favorite=False,
        is_hidden=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        thumbnail_face_id=thumbnail_face_id,
    )


@pytest.fixture
def mock_session():
    """Create a mock session that auth middleware will find."""
    now = datetime.now(timezone.utc)
    session = Session(
        id=TEST_SESSION_UUID,
        user_id=str(TEST_USER_UUID),
        library_id="",
        stored_jwt="encrypted-jwt-token",
        device_type="CLI",
        device_os="Test",
        app_version="1.0.0",
        created_at=now,
        updated_at=now,
        is_pending_sync_reset=False,
    )
    # Mock the get_jwt method to return a usable token
    session.get_jwt = Mock(return_value="real-jwt-token")
    return session


@pytest.fixture
def mock_session_store(mock_session):
    """Mock session store that returns our test session."""
    store = AsyncMock(spec=SessionStore)
    store.get_by_id.return_value = mock_session
    store.update_activity.return_value = None
    store.set_pending_sync_reset.return_value = None
    return store


@pytest.fixture
def mock_checkpoint_store():
    """Mock checkpoint store (empty checkpoints = full sync)."""
    store = AsyncMock(spec=CheckpointStore)
    store.get_all.return_value = []
    store.set_many.return_value = None
    store.delete_all.return_value = None
    return store


@pytest.fixture
def mock_gumnut_user():
    """Create a mock Gumnut user."""
    return UserResponse(
        id=TEST_GUMNUT_USER_ID,
        email="test@example.com",
        first_name="Test",
        last_name="User",
        is_superuser=False,
        is_active=True,
        is_verified=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def mock_gumnut_client(mock_gumnut_user):
    """
    Create mock Gumnut client with comprehensive test data.

    Includes: assets, exif, faces, albums, album_assets, persons
    """
    client = Mock()
    client.users.me.return_value = mock_gumnut_user

    # Build event data from test assets
    asset_events = []
    exif_events = []
    face_events = []

    for asset_data in TEST_ASSETS_DATA:
        # Asset event
        asset_response = create_asset_response(asset_data)
        asset_events.append(AssetEventPayload(data=asset_response, entity_type="asset"))

        # Exif event
        if asset_data.get("exif"):
            exif_response = create_exif_response(asset_data["id"], asset_data["exif"])
            exif_events.append(ExifEventPayload(data=exif_response, entity_type="exif"))

        # Face events
        for face_data in asset_data.get("faces", []):
            face_response = create_face_response(asset_data["id"], face_data)
            face_events.append(FaceEventPayload(data=face_response, entity_type="face"))

    # Album events (from Gumnut API)
    album_events = []
    for album_data in TEST_ALBUMS_DATA:
        album_response = create_album_response(album_data)
        album_events.append(AlbumEventPayload(data=album_response, entity_type="album"))

    # Album asset events (link assets to album)
    # Note: Using first album for test data since we only have one album
    album_asset_events = []
    for album_data in TEST_ALBUMS_DATA:
        for asset_data in TEST_ASSETS_DATA:
            album_asset_response = create_album_asset_response(
                album_id=album_data["id"],
                asset_id=asset_data["id"],
            )
            album_asset_events.append(
                AlbumAssetEventPayload(
                    data=album_asset_response, entity_type="album_asset"
                )
            )

    # Person events (create a test person with valid shortuuid-encoded ID)
    # Generated via: uuid_to_gumnut_person_id(UUID('770e8400-e29b-41d4-a716-446655440002'))
    person_response = create_person_response(
        person_id="person_PCRc9xU4DgqoA8btzKftNh",
        name="Test Person",
        thumbnail_face_id="face_nxS3BTriNXmqzFFW8ADKzf",
    )
    person_events = [PersonEventPayload(data=person_response, entity_type="person")]

    def mock_events_get(**kwargs):
        """Return appropriate events based on entity_types parameter."""
        entity_types = kwargs.get("entity_types", "")
        response = Mock()

        if entity_types == "asset":
            response.data = asset_events
        elif entity_types == "exif":
            response.data = exif_events
        elif entity_types == "face":
            response.data = face_events
        elif entity_types == "album":
            response.data = album_events
        elif entity_types == "album_asset":
            response.data = album_asset_events
        elif entity_types == "person":
            response.data = person_events
        else:
            response.data = []

        return response

    client.events.get.side_effect = mock_events_get
    return client


@pytest.fixture
def client(mock_gumnut_client, mock_checkpoint_store, mock_session_store):
    """
    Create test client with full middleware stack.

    Mocks:
    - Gumnut client (via dependency override)
    - Checkpoint store (via dependency override)
    - Session store (via dependency override AND patch for middleware)
    """
    # Override FastAPI dependencies
    app.dependency_overrides[get_authenticated_gumnut_client] = (
        lambda: mock_gumnut_client
    )
    app.dependency_overrides[get_checkpoint_store] = lambda: mock_checkpoint_store
    app.dependency_overrides[get_session_store] = lambda: mock_session_store

    # IMPORTANT: Auth middleware calls get_session_store() directly,
    # not via Depends(), so we need to patch it at the module level too
    try:
        with patch(
            "routers.middleware.auth_middleware.get_session_store",
            return_value=mock_session_store,
        ):
            yield TestClient(app, base_url="https://testserver")
    finally:
        app.dependency_overrides.clear()


def parse_jsonl_response(response_text: str) -> list[dict]:
    """Parse JSONL response text into list of event dicts."""
    return [json.loads(line) for line in response_text.strip().split("\n") if line]


def post_sync_stream(client: TestClient, types: list[str]) -> list[dict]:
    """POST to /api/sync/stream and return parsed events.

    Asserts status_code == 200 before parsing to provide actionable
    error output if the request fails (401/422/500).
    """
    response = client.post(
        "/api/sync/stream",
        json={"types": types},
        headers={"Authorization": f"Bearer {TEST_SESSION_UUID}"},
    )
    assert response.status_code == 200, response.text
    return parse_jsonl_response(response.text)


class TestSyncStreamHTTPE2E:
    """True E2E tests that make HTTP requests through the full stack.

    Note: Tests for streaming response format, auth methods (cookie/header/bearer),
    reset flag, and SyncCompleteV1 are covered by unit tests in:
    - tests/unit/api/sync/test_sync_stream.py
    - tests/unit/middleware/test_auth_middleware.py
    """

    def test_sync_stream_asset_data(self, client):
        """Test that asset data matches Immich expected format with correct values.

        Verifies:
        - originalFileName: preserved from source
        - checksum: uses checksum_sha1 from Gumnut
        - type: derived from mime_type (image/jpeg -> IMAGE)
        - visibility: default "timeline"
        - isFavorite: default False
        - deletedAt: null for non-deleted assets
        """
        events = post_sync_stream(client, ["AuthUsersV1", "AssetsV1"])
        asset_events = [e for e in events if e["type"] == "AssetV1"]

        # Build expected values from test data
        # Note: checksum uses checksum_sha1, type derived from mime_type
        expected_assets = {
            (
                a["original_file_name"],
                a["checksum_sha1"],  # Sync code prefers checksum_sha1
                "IMAGE",  # All test assets are image/jpeg
                "timeline",  # Default visibility
                False,  # isFavorite default
            )
            for a in TEST_ASSETS_DATA
        }
        actual_assets = {
            (
                e["data"]["originalFileName"],
                e["data"]["checksum"],
                e["data"]["type"],
                e["data"]["visibility"],
                e["data"]["isFavorite"],
            )
            for e in asset_events
        }

        assert actual_assets == expected_assets

        # Verify deletedAt is null for all assets (not deleted)
        for event in asset_events:
            assert event["data"]["deletedAt"] is None

    def test_sync_stream_exif_data(self, client):
        """Test that EXIF data is returned correctly.

        Uses explicit expected values to validate the contract rather than
        re-implementing the transformation logic.
        """
        events = post_sync_stream(client, ["AuthUsersV1", "AssetsV1", "AssetExifsV1"])
        exif_events = [e for e in events if e["type"] == "AssetExifV1"]

        assert len(exif_events) == len(TEST_ASSETS_DATA)

        # Build expected EXIF values from test data (all 7 fields)
        # Note: snake_case in test data maps to camelCase in Immich output
        # Note: exposure time uses EXPECTED_EXPOSURE_TIMES for known outputs
        expected_exif = {
            (
                a["exif"]["make"],
                a["exif"]["model"],
                a["exif"]["lens_model"],
                a["exif"]["f_number"],
                a["exif"]["focal_length"],
                a["exif"]["iso"],
                EXPECTED_EXPOSURE_TIMES[a["id"]],
            )
            for a in TEST_ASSETS_DATA
            if a.get("exif")
        }
        actual_exif = {
            (
                e["data"]["make"],
                e["data"]["model"],
                e["data"]["lensModel"],
                e["data"]["fNumber"],
                e["data"]["focalLength"],
                e["data"]["iso"],
                e["data"]["exposureTime"],
            )
            for e in exif_events
        }

        assert actual_exif == expected_exif

    def test_sync_stream_face_data(self, client):
        """Test that face data is returned with correct bounding box transformation.

        Gumnut uses {x, y, w, h} format, Immich expects {X1, Y1, X2, Y2}:
        - X1 = x
        - Y1 = y
        - X2 = x + w
        - Y2 = y + h
        """
        events = post_sync_stream(client, ["AuthUsersV1", "AssetsV1", "AssetFacesV1"])
        face_events = [e for e in events if e["type"] == "AssetFaceV1"]

        # Count faces in test data
        expected_face_count = sum(
            len(asset.get("faces", [])) for asset in TEST_ASSETS_DATA
        )
        assert len(face_events) == expected_face_count

        # Build expected bounding boxes from test data
        # Transform from {x, y, w, h} to (X1, Y1, X2, Y2)
        expected_bounding_boxes = set()
        for asset in TEST_ASSETS_DATA:
            for face in asset.get("faces", []):
                bb = face["bounding_box"]
                expected_bounding_boxes.add(
                    (
                        bb["x"],  # X1
                        bb["y"],  # Y1
                        bb["x"] + bb["w"],  # X2
                        bb["y"] + bb["h"],  # Y2
                    )
                )

        actual_bounding_boxes = {
            (
                e["data"]["boundingBoxX1"],
                e["data"]["boundingBoxY1"],
                e["data"]["boundingBoxX2"],
                e["data"]["boundingBoxY2"],
            )
            for e in face_events
        }

        assert actual_bounding_boxes == expected_bounding_boxes

    def test_sync_stream_album_data(self, client):
        """Test that album data is returned with correct values.

        Verifies:
        - name: preserved from source
        - description: preserved from source (empty string if not set)
        - thumbnailAssetId: UUID converted from album_cover_asset_id
        - isActivityEnabled: hardcoded True
        - order: hardcoded "desc"
        """
        events = post_sync_stream(client, ["AuthUsersV1", "AlbumsV1"])
        album_events = [e for e in events if e["type"] == "AlbumV1"]

        assert len(album_events) == len(TEST_ALBUMS_DATA)

        # Build expected values from test data
        expected_albums = {
            (
                album["name"],
                album.get("description", ""),
                True,  # isActivityEnabled hardcoded
                "desc",  # order hardcoded
            )
            for album in TEST_ALBUMS_DATA
        }
        actual_albums = {
            (
                e["data"]["name"],
                e["data"]["description"],
                e["data"]["isActivityEnabled"],
                e["data"]["order"],
            )
            for e in album_events
        }

        assert actual_albums == expected_albums

        # Verify thumbnailAssetId is present for albums with cover
        for event in album_events:
            album_data = event["data"]
            # Find matching test album by name
            test_album = next(
                a for a in TEST_ALBUMS_DATA if a["name"] == album_data["name"]
            )
            if test_album.get("album_cover_asset_id"):
                assert album_data["thumbnailAssetId"] is not None
                # Verify it's a valid UUID
                UUID(album_data["thumbnailAssetId"])
            else:
                assert album_data["thumbnailAssetId"] is None

    def test_sync_stream_full_sync_request(self, client):
        """Test a full sync request with all entity types.

        Verifies:
        - All requested entity types are present
        - Correct count for each entity type
        - SyncCompleteV1 is last with correct format
        """
        events = post_sync_stream(
            client,
            [
                "AuthUsersV1",
                "AssetsV1",
                "AssetExifsV1",
                "AssetFacesV1",
                "AlbumsV1",
                "AlbumToAssetsV1",
                "PeopleV1",
            ],
        )

        # Count events by type
        event_counts: dict[str, int] = {}
        for e in events:
            event_type = e["type"]
            event_counts[event_type] = event_counts.get(event_type, 0) + 1

        # Calculate expected counts from test data
        expected_face_count = sum(
            len(asset.get("faces", [])) for asset in TEST_ASSETS_DATA
        )

        # Verify counts match expected values
        # Compute expected album-to-asset count (albums × assets)
        expected_album_asset_count = len(TEST_ALBUMS_DATA) * len(TEST_ASSETS_DATA)

        assert event_counts.get("AuthUserV1") == 1
        assert event_counts.get("AssetV1") == len(TEST_ASSETS_DATA)
        assert event_counts.get("AssetExifV1") == len(TEST_ASSETS_DATA)
        assert event_counts.get("AssetFaceV1") == expected_face_count
        assert event_counts.get("AlbumV1") == len(TEST_ALBUMS_DATA)
        assert event_counts.get("AlbumToAssetV1") == expected_album_asset_count
        assert event_counts.get("PersonV1") == 1
        assert event_counts.get("SyncCompleteV1") == 1

        # Find SyncCompleteV1 event
        complete_events = [e for e in events if e["type"] == "SyncCompleteV1"]
        assert len(complete_events) == 1, "Expected exactly one SyncCompleteV1 event"

        complete_event = complete_events[0]

        # SyncCompleteV1 should be the last event
        assert events[-1]["type"] == "SyncCompleteV1"

        # Data should be empty
        assert complete_event["data"] == {}

        # Ack format: SyncCompleteV1|{timestamp}||
        ack = complete_event["ack"]
        ack_parts = ack.split("|")
        assert ack_parts[0] == "SyncCompleteV1", (
            f"Ack should start with SyncCompleteV1: {ack}"
        )
        assert len(ack_parts) >= 3, f"Ack should have at least 3 parts: {ack}"
        # Verify timestamp is valid ISO format
        datetime.fromisoformat(ack_parts[1])
        # Entity ID should be empty for SyncCompleteV1
        assert ack_parts[2] == "", (
            f"Entity ID should be empty for SyncCompleteV1: {ack}"
        )

    def test_sync_stream_ack_format(self, client):
        """Test that each entity has correct ack format with actual entity IDs.

        Ack format: {entity_type}|{timestamp}|{gumnut_entity_id}|
        - entity_type: matches the event type
        - timestamp: ISO 8601 format
        - gumnut_entity_id: the actual Gumnut ID from test data
        """
        events = post_sync_stream(
            client,
            [
                "AuthUsersV1",
                "AssetsV1",
                "AssetExifsV1",
                "AlbumsV1",
                "AlbumToAssetsV1",
                "PeopleV1",
                "AssetFacesV1",
            ],
        )

        # Build expected entity IDs from test data
        expected_entity_ids = {
            "AuthUserV1": {TEST_GUMNUT_USER_ID},
            "AssetV1": {a["id"] for a in TEST_ASSETS_DATA},
            "AssetExifV1": {a["id"] for a in TEST_ASSETS_DATA},  # Uses asset_id
            "AlbumV1": {album["id"] for album in TEST_ALBUMS_DATA},
            "AlbumToAssetV1": {
                f"album_asset_{album['id']}_{asset['id']}"
                for album in TEST_ALBUMS_DATA
                for asset in TEST_ASSETS_DATA
            },
            "PersonV1": {"person_PCRc9xU4DgqoA8btzKftNh"},
            "AssetFaceV1": {
                face["id"] for a in TEST_ASSETS_DATA for face in a.get("faces", [])
            },
            "SyncCompleteV1": {""},  # Empty entity_id
        }

        # Collect actual entity IDs from acks by event type
        actual_entity_ids: dict[str, set[str]] = {}
        for event in events:
            event_type = event["type"]
            ack = event["ack"]
            ack_parts = ack.split("|")

            # All acks should have at least 3 parts: type|timestamp|entity_id|
            assert len(ack_parts) >= 3, f"Ack should have at least 3 parts: {ack}"

            # First part should match event type
            assert ack_parts[0] == event_type, (
                f"Ack type mismatch: expected {event_type}, got {ack_parts[0]}"
            )

            # Second part should be valid ISO timestamp
            try:
                datetime.fromisoformat(ack_parts[1])
            except ValueError:
                pytest.fail(
                    f"Invalid timestamp in ack for {event_type}: {ack_parts[1]}"
                )

            # Collect entity_id
            entity_id = ack_parts[2]
            if event_type not in actual_entity_ids:
                actual_entity_ids[event_type] = set()
            actual_entity_ids[event_type].add(entity_id)

        # Verify entity IDs match expected values for each type
        for event_type, expected_ids in expected_entity_ids.items():
            actual_ids = actual_entity_ids.get(event_type, set())
            assert actual_ids == expected_ids, (
                f"{event_type} entity IDs mismatch:\n"
                f"  expected: {expected_ids}\n"
                f"  actual: {actual_ids}"
            )
