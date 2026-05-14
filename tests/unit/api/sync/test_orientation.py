"""Tests for dimension/orientation emission in sync converters.

After GUM-767, the adapter no longer compensates for orientation locally.
photos-api emits ``asset.width/height`` in display space (post-rotation) and
exposes raw (pre-rotation) sensor dims on ``metadata.raw_width/raw_height``,
which the adapter surfaces on ``exifInfo.exifImageWidth/Height`` for mobile
clients that re-derive display dims locally.
"""

from datetime import datetime, timezone

import pytest

from routers.api.sync.converters import (
    gumnut_asset_to_sync_asset_v1,
    gumnut_metadata_to_sync_exif_v1,
)
from tests.unit.api.sync.conftest import (
    create_mock_asset_data,
    create_mock_metadata_data,
)


OWNER_UUID = "22222222-2222-2222-2222-222222222222"
UPDATED_AT = datetime(2026, 5, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("orientation", [None, 1, 2, 3, 4, 5, 6, 7, 8])
def test_sync_asset_v1_emits_asset_dims_as_is(orientation):
    """SyncAssetV1.width/height pass through asset.width/height verbatim.

    photos-api now stores display-space dims at ingest (GUM-766), so the
    adapter must not re-swap. A portrait shot tagged orientation=6 has
    asset.width=2268, asset.height=4032 from the API and must emit those.
    """
    asset = create_mock_asset_data(UPDATED_AT)
    # Portrait dims as photos-api would return them post-GUM-766.
    asset.width = 2268
    asset.height = 4032
    if orientation is None:
        asset.metadata = None
    else:
        metadata = create_mock_metadata_data(UPDATED_AT)
        metadata.orientation = orientation
        asset.metadata = metadata

    result = gumnut_asset_to_sync_asset_v1(asset, owner_id=OWNER_UUID)

    assert result.width == 2268
    assert result.height == 4032


def test_sync_exif_v1_emits_raw_dims_and_unchanged_orientation():
    """exifInfo.exifImageWidth/Height carry raw (pre-rotation) sensor dims.

    Portrait shot with orientation=6: asset.width/height are display-space
    (2268×4032); raw_width/raw_height carry the pre-rotation values
    (4032×2268). Mobile clients re-derive display dims from raw + orientation.
    """
    asset = create_mock_asset_data(UPDATED_AT)
    asset.width = 2268
    asset.height = 4032
    metadata = create_mock_metadata_data(UPDATED_AT)
    metadata.orientation = 6
    metadata.raw_width = 4032
    metadata.raw_height = 2268
    asset.metadata = metadata

    result = gumnut_metadata_to_sync_exif_v1(asset)

    assert result.exifImageWidth == 4032
    assert result.exifImageHeight == 2268
    # Orientation is emitted as-is — mobile re-applies it locally.
    assert result.orientation == "6"


@pytest.mark.parametrize(
    "orientation,expected", [(1, "1"), (2, "2"), (3, "3"), (4, "4"), (8, "8")]
)
def test_sync_exif_v1_orientation_pass_through(orientation, expected):
    asset = create_mock_asset_data(UPDATED_AT)
    asset.width = 4032
    asset.height = 2268
    metadata = create_mock_metadata_data(UPDATED_AT)
    metadata.orientation = orientation
    metadata.raw_width = 4032
    metadata.raw_height = 2268
    asset.metadata = metadata

    result = gumnut_metadata_to_sync_exif_v1(asset)

    assert result.orientation == expected


def test_sync_exif_v1_falls_back_to_asset_dims_when_raw_dims_null():
    """Drift-cohort rows have NULL raw_width/raw_height — their asset.width/
    height is already display-space, so fall back to those values."""
    asset = create_mock_asset_data(UPDATED_AT)
    asset.width = 2268
    asset.height = 4032
    metadata = create_mock_metadata_data(UPDATED_AT)
    metadata.orientation = 6
    metadata.raw_width = None
    metadata.raw_height = None
    asset.metadata = metadata

    result = gumnut_metadata_to_sync_exif_v1(asset)

    assert result.exifImageWidth == 2268
    assert result.exifImageHeight == 4032
    assert result.orientation == "6"
