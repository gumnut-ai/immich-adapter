"""Tests for asset conversion utilities — focused on trash-state propagation.

The DTO conversion sites in ``routers/utils/asset_conversion.py`` must surface
``trashed_at`` to Immich clients via:

- ``AssetResponseDto.isTrashed`` (boolean) — gates the "In trash" indicator and
  the restore-vs-delete action bar in the UI.
- ``SyncAssetV1.deletedAt`` (nullable datetime) — the trash-state signal the
  sync stream ships to mobile clients.
"""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from routers.utils.asset_conversion import (
    build_asset_upload_ready_payload,
    convert_gumnut_asset_to_immich,
    display_dimensions,
    extract_exif_info,
    extract_sync_exif,
)


class TestConvertGumnutAssetToImmichTrashState:
    def test_live_asset_has_is_trashed_false(
        self, sample_gumnut_asset, mock_current_user
    ):
        """A live asset (trashed_at=None) maps to isTrashed=False."""
        sample_gumnut_asset.trashed_at = None

        result = convert_gumnut_asset_to_immich(sample_gumnut_asset, mock_current_user)

        assert result.isTrashed is False

    def test_trashed_asset_has_is_trashed_true(
        self, sample_gumnut_asset, mock_current_user
    ):
        """A trashed asset (trashed_at set) maps to isTrashed=True.

        The Immich UI keys "In trash" indicator off this flag, so it must
        reflect the live trashed_at state on every read path.
        """
        sample_gumnut_asset.trashed_at = datetime(2026, 4, 1, tzinfo=timezone.utc)

        result = convert_gumnut_asset_to_immich(sample_gumnut_asset, mock_current_user)

        assert result.isTrashed is True


class TestBuildAssetUploadReadyPayloadTrashState:
    def test_live_asset_payload_has_deleted_at_none(self, sample_gumnut_asset):
        """The upload-ready WebSocket payload mirrors trashed_at on the asset."""
        sample_gumnut_asset.trashed_at = None

        payload = build_asset_upload_ready_payload(
            sample_gumnut_asset, owner_id="22222222-2222-2222-2222-222222222222"
        )

        assert payload.asset.deletedAt is None

    def test_trashed_asset_payload_has_deleted_at_set(self, sample_gumnut_asset):
        """If a re-uploaded checksum matches a trashed asset, deletedAt is non-null.

        Re-uploads do not auto-restore on the backend, so ``trashed_at`` may be
        non-null on the asset returned by the upload pipeline. The wire payload
        must reflect the truth from the source rather than a hardcoded None.
        """
        trashed_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
        sample_gumnut_asset.trashed_at = trashed_at

        payload = build_asset_upload_ready_payload(
            sample_gumnut_asset, owner_id="22222222-2222-2222-2222-222222222222"
        )

        assert payload.asset.deletedAt == trashed_at


def _attach_metadata(asset: Mock, *, orientation: int | None) -> None:
    """Attach a minimal metadata mock with a given orientation to a sample asset."""
    metadata = Mock()
    metadata.make = None
    metadata.model = None
    metadata.lens_model = None
    metadata.f_number = None
    metadata.focal_length = None
    metadata.iso = None
    metadata.exposure_time = None
    metadata.latitude = None
    metadata.longitude = None
    metadata.city = None
    metadata.state = None
    metadata.country = None
    metadata.description = None
    metadata.orientation = orientation
    metadata.rating = None
    metadata.projection_type = None
    metadata.original_datetime = None
    metadata.modified_datetime = None
    asset.metadata = metadata


class TestDisplayDimensions:
    """display_dimensions normalizes raw sensor dims to display orientation."""

    @pytest.mark.parametrize("orientation", [None, 1, 2, 3, 4])
    def test_unflipped_orientations_pass_through(self, orientation):
        assert display_dimensions(4032, 2268, orientation) == (4032, 2268)

    @pytest.mark.parametrize("orientation", [5, 6, 7, 8])
    def test_flipped_orientations_swap(self, orientation):
        assert display_dimensions(4032, 2268, orientation) == (2268, 4032)

    def test_none_dimensions_pass_through(self):
        assert display_dimensions(None, None, 6) == (None, None)
        assert display_dimensions(None, 1080, 6) == (None, 1080)
        assert display_dimensions(1920, None, 6) == (1920, None)


class TestOrientationNormalization:
    """All asset-conversion sites must emit width/height in display orientation.

    Regression: an asset with raw landscape sensor dims (4032×2268) and EXIF
    orientation=6 (rotate 90° CW) was emitted unswapped, causing immich web's
    ``getAssetRatio`` to size the layout box as landscape while the served
    pixels were portrait — the image rendered stretched.
    """

    def test_extract_exif_info_swaps_for_orientation_6(self, sample_gumnut_asset):
        sample_gumnut_asset.width = 4032
        sample_gumnut_asset.height = 2268
        _attach_metadata(sample_gumnut_asset, orientation=6)

        result = extract_exif_info(sample_gumnut_asset)

        assert result.exifImageWidth == 2268
        assert result.exifImageHeight == 4032

    def test_extract_exif_info_passes_through_for_orientation_1(
        self, sample_gumnut_asset
    ):
        sample_gumnut_asset.width = 4032
        sample_gumnut_asset.height = 2268
        _attach_metadata(sample_gumnut_asset, orientation=1)

        result = extract_exif_info(sample_gumnut_asset)

        assert result.exifImageWidth == 4032
        assert result.exifImageHeight == 2268

    def test_extract_sync_exif_swaps_for_orientation_6(self, sample_gumnut_asset):
        sample_gumnut_asset.width = 4032
        sample_gumnut_asset.height = 2268
        _attach_metadata(sample_gumnut_asset, orientation=6)

        result = extract_sync_exif(sample_gumnut_asset, asset_uuid="x")

        assert result.exifImageWidth == 2268
        assert result.exifImageHeight == 4032

    def test_convert_gumnut_asset_to_immich_swaps_top_level_dims(
        self, sample_gumnut_asset, mock_current_user
    ):
        """Top-level width/height are what ``getAssetRatio`` reads."""
        sample_gumnut_asset.width = 4032
        sample_gumnut_asset.height = 2268
        _attach_metadata(sample_gumnut_asset, orientation=6)

        result = convert_gumnut_asset_to_immich(sample_gumnut_asset, mock_current_user)

        assert result.width == 2268.0
        assert result.height == 4032.0
        assert result.exifInfo is not None
        assert result.exifInfo.exifImageWidth == 2268
        assert result.exifInfo.exifImageHeight == 4032

    def test_convert_gumnut_asset_to_immich_no_metadata_passes_through(
        self, sample_gumnut_asset, mock_current_user
    ):
        sample_gumnut_asset.width = 4032
        sample_gumnut_asset.height = 2268
        sample_gumnut_asset.metadata = None

        result = convert_gumnut_asset_to_immich(sample_gumnut_asset, mock_current_user)

        assert result.width == 4032.0
        assert result.height == 2268.0

    def test_build_asset_upload_ready_payload_swaps_top_level_dims(
        self, sample_gumnut_asset
    ):
        sample_gumnut_asset.width = 4032
        sample_gumnut_asset.height = 2268
        _attach_metadata(sample_gumnut_asset, orientation=6)

        payload = build_asset_upload_ready_payload(
            sample_gumnut_asset, owner_id="22222222-2222-2222-2222-222222222222"
        )

        assert payload.asset.width == 2268
        assert payload.asset.height == 4032
        assert payload.exif.exifImageWidth == 2268
        assert payload.exif.exifImageHeight == 4032
