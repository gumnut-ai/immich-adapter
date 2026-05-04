"""Tests for orientation-aware width/height normalization in sync converters.

The sync stream's ``SyncAssetV1.width``/``height`` and
``SyncAssetExifV1.exifImageWidth``/``exifImageHeight`` must reflect display
orientation (post-rotation), matching upstream Immich's wire contract — clients
read these directly without consulting the orientation tag.
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


@pytest.mark.parametrize("orientation", [None, 1, 2, 3, 4])
def test_sync_asset_v1_passes_through_unflipped_orientations(orientation):
    asset = create_mock_asset_data(UPDATED_AT)
    asset.width = 4032
    asset.height = 2268
    if orientation is None:
        asset.metadata = None
    else:
        metadata = create_mock_metadata_data(UPDATED_AT)
        metadata.orientation = orientation
        asset.metadata = metadata

    result = gumnut_asset_to_sync_asset_v1(asset, owner_id=OWNER_UUID)

    assert result.width == 4032
    assert result.height == 2268


@pytest.mark.parametrize("orientation", [5, 6, 7, 8])
def test_sync_asset_v1_swaps_for_flipped_orientations(orientation):
    """Regression: GUM-688 — Pixel-shot landscape buffers tagged orientation=6
    were emitted unswapped, causing immich web to render portrait pixels into a
    landscape layout box."""
    asset = create_mock_asset_data(UPDATED_AT)
    asset.width = 4032
    asset.height = 2268
    metadata = create_mock_metadata_data(UPDATED_AT)
    metadata.orientation = orientation
    asset.metadata = metadata

    result = gumnut_asset_to_sync_asset_v1(asset, owner_id=OWNER_UUID)

    assert result.width == 2268
    assert result.height == 4032


def test_sync_exif_v1_swaps_for_flipped_orientation():
    asset = create_mock_asset_data(UPDATED_AT)
    asset.width = 4032
    asset.height = 2268
    metadata = create_mock_metadata_data(UPDATED_AT)
    metadata.orientation = 6
    asset.metadata = metadata

    result = gumnut_metadata_to_sync_exif_v1(asset)

    assert result.exifImageWidth == 2268
    assert result.exifImageHeight == 4032


def test_sync_exif_v1_passes_through_unflipped_orientation():
    asset = create_mock_asset_data(UPDATED_AT)
    asset.width = 4032
    asset.height = 2268
    metadata = create_mock_metadata_data(UPDATED_AT)
    metadata.orientation = 1
    asset.metadata = metadata

    result = gumnut_metadata_to_sync_exif_v1(asset)

    assert result.exifImageWidth == 4032
    assert result.exifImageHeight == 2268
