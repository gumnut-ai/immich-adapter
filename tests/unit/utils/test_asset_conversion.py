"""Tests for asset conversion utilities — focused on trash-state propagation.

The DTO conversion sites in ``routers/utils/asset_conversion.py`` must surface
``trashed_at`` to Immich clients via:

- ``AssetResponseDto.isTrashed`` (boolean) — gates the "In trash" indicator and
  the restore-vs-delete action bar in the UI.
- ``SyncAssetV1.deletedAt`` (nullable datetime) — the trash-state signal the
  sync stream ships to mobile clients.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from routers.utils.asset_conversion import (
    build_asset_upload_ready_payload,
    convert_gumnut_asset_to_immich,
    extract_exif_info,
    extract_sync_exif,
    resolve_capture_datetime,
)


class TestDateResolution:
    """Immich capture-date fields use Photos API's resolved ``local_datetime``."""

    LOCAL_DT = datetime(2017, 6, 3, 9, 15, 0, tzinfo=timezone.utc)
    METADATA_DT = datetime(2018, 7, 4, 10, 30, 0, tzinfo=timezone.utc)
    FILE_CREATED_DT = datetime(2019, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
    FILE_MODIFIED_DT = datetime(2019, 8, 6, 13, 0, 0, tzinfo=timezone.utc)
    CREATED_DT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    UPDATED_DT = datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc)

    def _set_dates(
        self,
        asset: Mock,
        *,
        local_datetime: datetime | None,
        metadata_original: datetime | None,
        metadata_modified: datetime | None,
        file_created: datetime | None,
        file_modified: datetime | None,
    ) -> None:
        asset.created_at = self.CREATED_DT
        asset.updated_at = self.UPDATED_DT
        asset.local_datetime = local_datetime
        asset.file_created_at = file_created
        asset.file_modified_at = file_modified
        if metadata_original is None and metadata_modified is None:
            asset.metadata = None
            return
        metadata = Mock()
        metadata.original_datetime = metadata_original
        metadata.modified_datetime = metadata_modified
        # Avoid AttributeError on the dims/orientation path.
        metadata.raw_width = None
        metadata.raw_height = None
        metadata.orientation = None
        for attr in (
            "make",
            "model",
            "lens_model",
            "f_number",
            "focal_length",
            "iso",
            "exposure_time",
            "latitude",
            "longitude",
            "city",
            "state",
            "country",
            "description",
            "rating",
            "projection_type",
        ):
            setattr(metadata, attr, None)
        asset.metadata = metadata

    def test_capture_fields_use_local_datetime_not_metadata_or_file_dates(
        self, sample_gumnut_asset, mock_current_user
    ):
        self._set_dates(
            sample_gumnut_asset,
            local_datetime=self.LOCAL_DT,
            metadata_original=self.METADATA_DT,
            metadata_modified=self.METADATA_DT,
            file_created=self.FILE_CREATED_DT,
            file_modified=self.FILE_MODIFIED_DT,
        )

        assert resolve_capture_datetime(sample_gumnut_asset) == self.LOCAL_DT

        rest = convert_gumnut_asset_to_immich(sample_gumnut_asset, mock_current_user)
        assert rest.fileCreatedAt == self.LOCAL_DT
        assert rest.fileModifiedAt == self.FILE_MODIFIED_DT
        assert rest.localDateTime == self.LOCAL_DT

        payload = build_asset_upload_ready_payload(
            sample_gumnut_asset, owner_id="22222222-2222-2222-2222-222222222222"
        )
        assert payload.asset.fileCreatedAt == self.LOCAL_DT
        assert payload.asset.fileModifiedAt == self.FILE_MODIFIED_DT
        assert payload.asset.localDateTime == self.LOCAL_DT

    def test_capture_fields_keep_local_datetime_timezone_semantics(
        self, sample_gumnut_asset, mock_current_user
    ):
        tokyo = timezone(timedelta(hours=9))
        local_datetime = datetime(2024, 6, 20, 15, 0, 0, tzinfo=tokyo)
        expected_file_created_at = datetime(2024, 6, 20, 6, 0, 0, tzinfo=timezone.utc)
        expected_local_date_time = datetime(2024, 6, 20, 15, 0, 0, tzinfo=timezone.utc)
        self._set_dates(
            sample_gumnut_asset,
            local_datetime=local_datetime,
            metadata_original=None,
            metadata_modified=None,
            file_created=self.FILE_CREATED_DT,
            file_modified=self.FILE_MODIFIED_DT,
        )

        rest = convert_gumnut_asset_to_immich(sample_gumnut_asset, mock_current_user)
        assert rest.fileCreatedAt == expected_file_created_at
        assert rest.fileModifiedAt == self.FILE_MODIFIED_DT
        assert rest.localDateTime == expected_local_date_time

        payload = build_asset_upload_ready_payload(
            sample_gumnut_asset, owner_id="22222222-2222-2222-2222-222222222222"
        )
        assert payload.asset.fileCreatedAt == expected_file_created_at
        assert payload.asset.fileModifiedAt == self.FILE_MODIFIED_DT
        assert payload.asset.localDateTime == expected_local_date_time

    def test_required_rest_fields_fall_back_to_upload_dates_when_resolved_dates_missing(
        self, sample_gumnut_asset, mock_current_user
    ):
        self._set_dates(
            sample_gumnut_asset,
            local_datetime=None,
            metadata_original=None,
            metadata_modified=None,
            file_created=None,
            file_modified=None,
        )

        rest = convert_gumnut_asset_to_immich(sample_gumnut_asset, mock_current_user)
        assert rest.fileCreatedAt == self.CREATED_DT
        assert rest.fileModifiedAt == self.UPDATED_DT
        assert rest.localDateTime == self.CREATED_DT

        payload = build_asset_upload_ready_payload(
            sample_gumnut_asset, owner_id="22222222-2222-2222-2222-222222222222"
        )
        assert payload.asset.fileCreatedAt == self.CREATED_DT
        assert payload.asset.fileModifiedAt == self.UPDATED_DT
        assert payload.asset.localDateTime == self.CREATED_DT


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

    photos-api stores ``asset.width/height`` in display space at ingest and
    exposes pre-rotation raw dims on ``metadata.raw_width/raw_height``. The
    adapter no longer compensates for orientation locally — it surfaces
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
        # As photos-api returns the asset: dims already in display space.
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
        asset.width/height — display-space for that cohort. Orientation must
        be nulled on the wire so mobile doesn't double-rotate display dims."""
        sample_gumnut_asset.width = 2268
        sample_gumnut_asset.height = 4032
        _attach_metadata(
            sample_gumnut_asset, orientation=6, raw_width=None, raw_height=None
        )

        result = extract_exif_info(sample_gumnut_asset)

        assert result.exifImageWidth == 2268
        assert result.exifImageHeight == 4032
        # Fallback path nulls orientation: dims are display-space; emitting
        # orientation=6 here would make mobile re-apply the 5–8 swap and
        # derive landscape dims for a portrait shot.
        assert result.orientation is None

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
        # Fallback path nulls orientation to prevent mobile double-rotation.
        assert result.orientation is None

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
