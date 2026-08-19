"""Test configuration and shared fixtures."""

# IMPORTANT: Set TESTING env var before any imports that might trigger settings loading.
# This ensures TestSettings (which loads .env.test) is used instead of DefaultSettings
# when main.py is imported during test collection.
import os

os.environ["TESTING"] = "1"

import base64
import hashlib

import pytest
from unittest.mock import AsyncMock, Mock
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from typing import List, Any

import httpx
from gumnut import APIConnectionError, APIStatusError, NotFoundError

from routers.immich_models import UserResponseDto, UserAvatarColor
from routers.utils.gumnut_id_conversion import (
    uuid_to_gumnut_album_id,
    uuid_to_gumnut_asset_id,
    uuid_to_gumnut_person_id,
    uuid_to_gumnut_stack_id,
)


def make_sdk_status_error(
    status_code: int,
    message: str = "upstream error",
    body: object | None = None,
    *,
    cls: type[APIStatusError] = APIStatusError,
) -> APIStatusError:
    """Construct a Gumnut SDK APIStatusError for tests.

    Use a typed subclass (e.g. NotFoundError) when isinstance dispatch matters.
    """
    request = httpx.Request("GET", "http://test.local/")
    response = httpx.Response(status_code, request=request)
    return cls(message, response=response, body=body)


def make_sdk_connection_error(method: str = "GET") -> APIConnectionError:
    """Construct an SDK APIConnectionError for tests (transport-failure path)."""
    return APIConnectionError(request=httpx.Request(method, "http://test.local/"))


@pytest.fixture
def sdk_not_found_error():
    """A NotFoundError instance suitable for `side_effect=` in mocks."""
    return make_sdk_status_error(404, "Not found", cls=NotFoundError)


# Configure anyio to use only asyncio backend
pytest_plugins = ("anyio",)


@pytest.fixture(scope="session")
def anyio_backend():
    """Force asyncio backend for all tests."""
    return "asyncio"


@pytest.fixture
def mock_gumnut_client():
    """Mock the Gumnut client to avoid actual API calls."""
    client = Mock()
    return client


@pytest.fixture
def sample_uuid():
    """Generate a sample UUID for testing."""
    return uuid4()


@pytest.fixture
def sample_gumnut_album():
    """Create a sample Gumnut album object with proper date fields."""
    album = Mock()
    album.id = uuid_to_gumnut_album_id(uuid4())
    album.name = "Test Album"
    album.description = "Test Description"
    album.created_at = datetime.now(timezone.utc)
    album.updated_at = datetime.now(timezone.utc)
    album.asset_count = 5
    album.album_cover_asset_id = None
    album.start_date = None
    album.end_date = None
    return album


def make_gumnut_asset(
    *,
    asset_id: str | None = None,
    original_file_name: str = "test.jpg",
    device_asset_id: str = "device-123",
    device_id: str = "device-456",
    checksum: str = "abc123",
    checksum_sha1: str = "PaDX6+c+Lhjpm5/ciXUROL1ryaU=",
    trashed_at: datetime | None = None,
    stack_id: str | None = None,
    local_datetime: datetime | None = None,
    kind: str = "original",
) -> Mock:
    """Build a Mock Gumnut asset carrying every field the converters read.

    One builder rather than a copy per fixture: an SDK field addition (or a
    converter that starts reading an existing one) otherwise has to be applied
    to each copy, and an unset `Mock` attribute is neither `None` nor a real
    value — it silently flips a branch or fails DTO validation far from the
    fixture that missed it.

    `checksum_sha1` is the base64 SHA-1 that reaches Immich (`checksum` is the
    SHA-256, which never does), so vary it — via `fake_sha1_checksum` — whenever
    a test builds several assets whose identities must stay distinguishable;
    otherwise a dedup-aware assertion passes because every asset carries the
    same value. `stack_id` takes an `asset_stack_`-prefixed Gumnut ID, not a
    UUID, and is `None` for an unstacked asset. `local_datetime` is the capture
    time every ordering rule in the adapter sorts on; pass distinct values
    whenever a test depends on which asset comes first.
    """
    now = datetime.now(timezone.utc)
    asset = Mock()
    asset.id = asset_id or uuid_to_gumnut_asset_id(uuid4())
    asset.local_datetime = local_datetime or now
    asset.created_at = now
    asset.updated_at = now
    asset.mime_type = "image/jpeg"
    asset.original_file_name = original_file_name
    asset.duration = None
    asset.library_id = "library-789"
    # File/provenance scalars live on the nested ``file_data`` group
    # (requested via ``include=file_data``); the adapter reads them from there.
    asset.file_data = Mock()
    asset.file_data.device_asset_id = device_asset_id
    asset.file_data.device_id = device_id
    asset.file_data.file_created_at = now
    asset.file_data.file_modified_at = now
    asset.file_data.checksum = checksum
    # Base64-encoded SHA-1 (28 chars), the Immich-facing checksum format.
    asset.file_data.checksum_sha1 = checksum_sha1
    asset.file_data.file_size_bytes = 1059218
    # Default to "not yet generated"; thumbhash tests set an explicit value.
    # Without this, the Mock would yield a Mock (not None) for asset.thumbhash.
    asset.thumbhash = None
    asset.width = 1920
    asset.height = 1080
    asset.people = []  # Empty list for people
    asset.metadata = None  # No metadata
    asset.trashed_at = trashed_at
    asset.stack_id = stack_id
    # What produced the current rendering. `"original"` (the default) means the
    # asset is unedited; anything else maps to Immich `isEdited=True` via
    # `is_asset_edited`. Set explicitly so a Mock attribute (truthy, never equal
    # to "original") doesn't make every fixture asset read as edited.
    asset.kind = kind
    asset.current_version_id = f"asset_version_{asset.id}"
    return asset


def fake_sha1_checksum(seed: str) -> str:
    """A distinct but well-formed Immich checksum (base64 SHA-1, 28 chars)."""
    return base64.b64encode(hashlib.sha1(seed.encode()).digest()).decode()


def make_gumnut_stack(
    *,
    stack_id: str | None = None,
    primary_asset_id: str | None = None,
    asset_count: int = 2,
    origin: str = "auto_burst",
) -> Mock:
    """Build a Mock Gumnut stack row (the shape every stacks endpoint returns).

    Defaults to an unpinned `auto_burst`. Set `asset_count` below the member
    count to model trashed members. Both live in `routers/utils/stack_conversion.py`:
    `resolve_effective_primary` for how the pin is used, and
    `HydratedStack.live_asset_count` for why the count can sit below the member
    count.
    """
    stack = Mock()
    stack.id = stack_id or uuid_to_gumnut_stack_id(uuid4())
    stack.primary_asset_id = primary_asset_id
    stack.asset_count = asset_count
    stack.origin = origin
    now = datetime.now(timezone.utc)
    stack.created_at = now
    stack.updated_at = now
    return stack


def make_gumnut_stack_members(
    count: int,
    *,
    stack_id: str,
    trashed: set[int] | None = None,
    first_captured_at: datetime | None = None,
) -> list[Mock]:
    """Build `count` Mock assets belonging to `stack_id`, in member order.

    `trashed` holds the positional indices to mark trashed, so a test can spell
    out a mixed live/trashed stack in one call. Every per-asset identity field —
    filename, device IDs, and both checksums — varies by index, so a member
    that leaks into the wrong position is visible in failure output rather than
    matching its neighbours.

    Capture times ascend one second per index from `first_captured_at`, so
    member order and capture order agree and "the earliest frame" is a single
    unambiguous asset. Identical timestamps would leave any capture-time sort
    deciding on its tiebreaker, which is not what such a test means to assert.
    A real burst is sub-second, but only the ordering matters here.
    """
    trashed = trashed or set()
    now = datetime.now(timezone.utc)
    first_captured_at = first_captured_at or now
    return [
        make_gumnut_asset(
            original_file_name=f"burst-{i}.jpg",
            # Distinct series, so an assertion reading both can catch a swap.
            device_asset_id=f"device-asset-{i}",
            device_id=f"device-id-{i}",
            checksum=f"checksum-{i}",
            checksum_sha1=fake_sha1_checksum(f"burst-{i}"),
            trashed_at=now if i in trashed else None,
            stack_id=stack_id,
            local_datetime=first_captured_at + timedelta(seconds=i),
        )
        for i in range(count)
    ]


def make_gumnut_stack_with_members(
    *, count: int, trashed: set[int] | None = None, **stack_kwargs: Any
) -> tuple[Mock, list[Mock]]:
    """Build a (stack, members) pair whose members all point at the stack.

    The pairing is the point: `make_gumnut_stack_members` needs a stack ID, and
    a member list built against the wrong one produces a stack that hydrates
    empty — a failure that reads as a routing bug rather than a fixture typo.

    `asset_count` is paired too, defaulting to the live member count rather than
    `make_gumnut_stack`'s standalone default. It stopped being cosmetic once
    `search_stacks` began spending it against `SEARCH_STACKS_MEMBER_BUDGET`: a
    row that claims two members while carrying eight would budget a number the
    response contradicts, and the test would pass for the wrong reason. Pass it
    explicitly to model the row and the member read disagreeing on purpose.
    """
    stack_kwargs.setdefault("asset_count", count - len(trashed or ()))
    stack = make_gumnut_stack(**stack_kwargs)
    members = make_gumnut_stack_members(count, stack_id=stack.id, trashed=trashed)
    return stack, members


def mock_list_stacks(rows):
    """A `client.stacks.list_stacks` mock returning the requested subset of `rows`.

    Filters by the `ids` each call asks for rather than replaying `rows`
    wholesale, so chunked reads get their own chunk back — and so a test can
    model a row that vanished between the asset read and the stack read simply
    by leaving that stack out of `rows`.

    The matched rows come back **reversed**, deliberately not in the order the
    caller requested: the Gumnut API promises no row order and callers index by
    `row.id`, so replaying the request order would let a consumer that walked
    the response instead of its own ID list pass. See the comment above
    `complete_ids` in `resolve_timeline_stacks` for why that matters there.
    """
    rows_by_id = {row.id: row for row in rows}
    return Mock(
        side_effect=lambda **kwargs: MockSyncCursorPage(
            [
                rows_by_id[stack_id]
                for stack_id in reversed(kwargs["ids"])
                if stack_id in rows_by_id
            ]
        )
    )


@pytest.fixture
def sample_gumnut_asset():
    """Create a sample Gumnut asset object with proper date fields."""
    return make_gumnut_asset()


@pytest.fixture
def multiple_gumnut_albums():
    """Create multiple Gumnut albums for list testing with proper date fields."""
    albums = []
    for i in range(3):
        album = Mock()
        album.id = uuid_to_gumnut_album_id(uuid4())
        album.name = f"Test Album {i}"
        album.description = f"Test Description {i}"
        album.created_at = datetime.now(timezone.utc)
        album.updated_at = datetime.now(timezone.utc)
        album.asset_count = i + 1
        album.album_cover_asset_id = None
        album.start_date = None
        album.end_date = None
        albums.append(album)
    return albums


@pytest.fixture
def multiple_gumnut_assets():
    """Create multiple Gumnut assets for list testing with proper date fields."""
    return [
        make_gumnut_asset(
            original_file_name=f"test{i}.jpg",
            device_asset_id=f"device-{i}",
            device_id=f"device-{i}",
            checksum=f"checksum-{i}",
            checksum_sha1=fake_sha1_checksum(f"test{i}"),
        )
        for i in range(3)
    ]


def make_mock_streaming_context(
    headers: dict[str, str],
    chunks: tuple[bytes, ...] = (b"fake image data",),
    *,
    method: str,
) -> Mock:
    """Create a mock streaming response context manager.

    Returns a mock context manager whose response exposes only the specified
    iterator method (``iter_bytes`` or ``aiter_bytes``), so tests fail loudly
    if the code under test calls the wrong one.
    """
    mock_response = Mock()
    mock_response.headers = headers

    async def _iter(chunk_size: int | None = None):
        for chunk in chunks:
            yield chunk

    setattr(mock_response, method, _iter)

    mock_context = Mock()
    mock_context.__aenter__ = AsyncMock(return_value=mock_response)
    mock_context.__aexit__ = AsyncMock(return_value=None)
    return mock_context


class MockSyncCursorPage:
    """Mock for Gumnut SyncCursorPage / AsyncPaginator response.

    Supports both sync and async iteration patterns:
    - ``for item in page`` (sync iteration, used in tests)
    - ``async for item in page`` (async iteration, used by AsyncGumnut list calls)
    - ``page = await paginator`` (returns self with .data, used by entity_fetch)
    """

    def __init__(self, items: List[Any]):
        self.items = items
        self.data = items
        self.has_more = False

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    async def _async_iter(self):
        for item in self.items:
            yield item

    def __aiter__(self):
        return self._async_iter()

    def __await__(self):
        return self._await_impl().__await__()

    async def _await_impl(self):
        return self


class MockPaginatedListing:
    """Mock paginator that fakes the SDK's auto-pagination contract.

    ``MockSyncCursorPage`` yields a flat list, so it can't distinguish "the
    SDK's ``limit`` is a result cap" from "the SDK's ``limit`` is per-page" —
    both behaviors return the same thing. This one yields one item at a time
    across pages of ``page_size`` and counts the boundaries it crosses, so a
    regression that drops an explicit ``break`` out of ``async for`` visibly
    walks extra pages, and a walk that must consume every page can assert it
    crossed more than one.

    ``page_size`` must equal the ``limit`` the code actually sends, or
    ``pages_fetched`` counts boundaries that never occur. Either read it off the
    call (a ``side_effect`` capturing ``kwargs["limit"]``) or pin it with a
    separate ``call_args.kwargs["limit"] == page_size`` assertion — a
    test-picked number with neither is what lets the two silently drift.
    """

    def __init__(self, items: List[Any], page_size: int):
        self._items = items
        self._page_size = page_size
        self.pages_fetched = 0

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for i, item in enumerate(self._items):
            if i % self._page_size == 0:
                self.pages_fetched += 1
            yield item


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
    person.id = uuid_to_gumnut_person_id(uuid4())
    person.name = "Test Person"
    person.birth_date = datetime(1990, 1, 1).date()
    person.is_favorite = False
    person.is_hidden = False
    person.thumbnail_face_id = "face-456"
    person.thumbnail_face_url = "https://example.com/thumbnail.jpg"
    thumbnail_variant = Mock()
    thumbnail_variant.url = "https://cdn.example.com/person-thumbnail.jpg"
    thumbnail_variant.mimetype = "image/jpeg"
    person.asset_urls = {"thumbnail": thumbnail_variant}
    person.asset_count = 5
    person.created_at = datetime.now(timezone.utc)
    person.updated_at = datetime.now(timezone.utc)
    return person


@pytest.fixture
def multiple_gumnut_people():
    """Create multiple Gumnut people for list testing."""
    people = []
    for i in range(3):
        person = Mock()
        person.id = uuid_to_gumnut_person_id(uuid4())
        person.name = f"Test Person {i}"
        person.birth_date = datetime(1990 + i, 1, 1).date()
        person.is_favorite = i % 2 == 0  # Alternate favorites
        person.is_hidden = False  # Default to not hidden
        person.thumbnail_face_id = f"face-{i}"
        person.thumbnail_face_url = f"https://example.com/thumbnail-{i}.jpg"
        thumbnail_variant = Mock()
        thumbnail_variant.url = f"https://cdn.example.com/person-thumbnail-{i}.jpg"
        thumbnail_variant.mimetype = "image/jpeg"
        person.asset_urls = {"thumbnail": thumbnail_variant}
        person.asset_count = (i + 1) * 5
        person.created_at = datetime.now(timezone.utc)
        person.updated_at = datetime.now(timezone.utc)
        people.append(person)
    return people


@pytest.fixture
def mock_current_user():
    """Create a mock current user for testing."""
    return UserResponseDto(
        id=uuid4(),
        email="test@example.com",
        name="Test User",
        avatarColor=UserAvatarColor.primary,
        profileImagePath="",
        profileChangedAt=datetime.now(timezone.utc),
    )
