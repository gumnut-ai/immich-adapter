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


def _attach_metadata(
    asset: Mock,
    *,
    orientation: int | None,
    raw_width: int | None = None,
    raw_height: int | None = None,
) -> None:
    """Attach a minimal metadata mock to a sample asset."""
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
    metadata.raw_width = raw_width
    metadata.raw_height = raw_height
    asset.metadata = metadata


class TestDimensionEmission:
    """Adapter passes through display-space dims from photos-api as-is.

    Post-GUM-767, photos-api stores ``asset.width/height`` in display space
    and exposes pre-rotation raw dims on ``metadata.raw_width/raw_height``.
    The adapter no longer compensates for orientation locally — it surfaces
    display dims on ``asset.width/height`` and raw dims on
    ``exifInfo.exifImageWidth/Height``, with NULL fallback for drift-cohort
    rows whose ``raw_width/raw_height`` were never captured.
    """

    def test_portrait_emits_raw_on_exif_and_display_on_asset(
        self, sample_gumnut_asset, mock_current_user
    ):
        """Portrait shot (orientation=6): asset.width/height are display dims,
        exifInfo carries raw (pre-rotation) sensor dims, and the orientation
        tag is emitted unchanged."""
        # As photos-api returns post-GUM-766: dims already display-space.
        sample_gumnut_asset.width = 2268
        sample_gumnut_asset.height = 4032
        _attach_metadata(
            sample_gumnut_asset, orientation=6, raw_width=4032, raw_height=2268
        )

        result = convert_gumnut_asset_to_immich(sample_gumnut_asset, mock_current_user)

        assert result.width == 2268.0
        assert result.height == 4032.0
        assert result.exifInfo is not None
        assert result.exifInfo.exifImageWidth == 4032
        assert result.exifInfo.exifImageHeight == 2268
        # Orientation tag emitted as-is; mobile re-derives display dims locally.
        assert result.exifInfo.orientation == "6"

    @pytest.mark.parametrize("orientation", [None, 1, 2, 3, 4, 5, 6, 7, 8])
    def test_asset_dims_pass_through_for_all_orientations(
        self, sample_gumnut_asset, mock_current_user, orientation
    ):
        """Regardless of orientation, asset.width/height are emitted verbatim.

        photos-api owns display-space dims at ingest; the adapter must not
        second-guess them.
        """
        sample_gumnut_asset.width = 4032
        sample_gumnut_asset.height = 2268
        if orientation is None:
            sample_gumnut_asset.metadata = None
        else:
            _attach_metadata(sample_gumnut_asset, orientation=orientation)

        result = convert_gumnut_asset_to_immich(sample_gumnut_asset, mock_current_user)

        assert result.width == 4032.0
        assert result.height == 2268.0

    def test_extract_exif_info_falls_back_to_asset_dims_when_raw_null(
        self, sample_gumnut_asset
    ):
        """Drift-cohort rows (raw_width/height NULL) fall back to
        asset.width/height, which is already display-space for that cohort."""
        sample_gumnut_asset.width = 2268
        sample_gumnut_asset.height = 4032
        _attach_metadata(
            sample_gumnut_asset, orientation=6, raw_width=None, raw_height=None
        )

        result = extract_exif_info(sample_gumnut_asset)

        assert result.exifImageWidth == 2268
        assert result.exifImageHeight == 4032
        assert result.orientation == "6"

    def test_extract_sync_exif_uses_raw_dims(self, sample_gumnut_asset):
        sample_gumnut_asset.width = 2268
        sample_gumnut_asset.height = 4032
        _attach_metadata(
            sample_gumnut_asset, orientation=6, raw_width=4032, raw_height=2268
        )

        result = extract_sync_exif(sample_gumnut_asset, asset_uuid="x")

        assert result.exifImageWidth == 4032
        assert result.exifImageHeight == 2268
        assert result.orientation == "6"

    def test_extract_sync_exif_falls_back_when_raw_null(self, sample_gumnut_asset):
        sample_gumnut_asset.width = 2268
        sample_gumnut_asset.height = 4032
        _attach_metadata(
            sample_gumnut_asset, orientation=6, raw_width=None, raw_height=None
        )

        result = extract_sync_exif(sample_gumnut_asset, asset_uuid="x")

        assert result.exifImageWidth == 2268
        assert result.exifImageHeight == 4032

    def test_build_asset_upload_ready_payload_emits_raw_and_display_dims(
        self, sample_gumnut_asset
    ):
        sample_gumnut_asset.width = 2268
        sample_gumnut_asset.height = 4032
        _attach_metadata(
            sample_gumnut_asset, orientation=6, raw_width=4032, raw_height=2268
        )

        payload = build_asset_upload_ready_payload(
            sample_gumnut_asset, owner_id="22222222-2222-2222-2222-222222222222"
        )

        assert payload.asset.width == 2268
        assert payload.asset.height == 4032
        assert payload.exif.exifImageWidth == 4032
        assert payload.exif.exifImageHeight == 2268
        assert payload.exif.orientation == "6"
