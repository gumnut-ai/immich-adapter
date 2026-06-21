"""Tests for asset conversion utilities — focused on trash-state propagation.

The DTO conversion sites in ``routers/utils/asset_conversion.py`` must surface
``trashed_at`` to Immich clients via:

- ``AssetResponseDto.isTrashed`` (boolean) — gates the "In trash" indicator and
  the restore-vs-delete action bar in the UI.
- ``SyncAssetV1.deletedAt`` (nullable datetime) — the trash-state signal the
  sync stream ships to mobile clients.
"""

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from gumnut.types.asset_response import AssetResponse
from gumnut.types.file_data_response import FileDataResponse

from routers.api.sync.converters import gumnut_asset_to_sync_asset_v1
from routers.utils.asset_conversion import (
    build_asset_upload_ready_payload,
    convert_gumnut_asset_to_immich,
    extract_exif_info,
    extract_sync_exif,
    format_duration,
    resolve_capture_datetime,
    resolve_file_modified_at,
    resolve_immich_checksum,
)
from routers.utils.datetime_utils import to_actual_utc


class TestDateResolution:
    """Immich capture-date fields use the Gumnut API's resolved ``local_datetime``;
    ``fileModifiedAt`` prefers EXIF ``metadata.modified_datetime`` because
    the Gumnut API does not resolve a separate modify-time field."""

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
        # ``file_created_at`` / ``file_modified_at`` live on the nested
        # ``file_data`` group (requested via ``include=file_data``).
        asset.file_data.file_created_at = file_created
        asset.file_data.file_modified_at = file_modified
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

    def test_capture_fields_use_local_datetime_and_modify_field_uses_metadata(
        self, sample_gumnut_asset, mock_current_user
    ):
        """When all date sources are populated, capture-time fields collapse to
        ``local_datetime`` and ``fileModifiedAt`` collapses to the EXIF
        ``metadata.modified_datetime`` (not the raw ``file_modified_at``)."""
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
        assert rest.fileModifiedAt == self.METADATA_DT
        assert rest.localDateTime == self.LOCAL_DT

        payload = build_asset_upload_ready_payload(
            sample_gumnut_asset, owner_id="22222222-2222-2222-2222-222222222222"
        )
        assert payload.asset.fileCreatedAt == self.LOCAL_DT
        assert payload.asset.fileModifiedAt == self.METADATA_DT
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

    def test_file_modified_at_falls_back_to_capture_when_both_modify_times_absent(
        self, sample_gumnut_asset, mock_current_user
    ):
        """``file_modified_at`` is nullable (part of the ``file_data`` include
        group), so the modify-time cascade can bottom out with no source. When
        neither ``metadata.modified_datetime`` nor ``file_modified_at`` is
        present, ``fileModifiedAt`` falls back to the capture time rather than
        ``None`` — Immich requires a non-null ``fileModifiedAt``."""
        self._set_dates(
            sample_gumnut_asset,
            local_datetime=self.LOCAL_DT,
            metadata_original=None,
            metadata_modified=None,
            file_created=None,
            file_modified=None,
        )

        rest = convert_gumnut_asset_to_immich(sample_gumnut_asset, mock_current_user)
        assert rest.fileModifiedAt == self.LOCAL_DT
        assert rest.fileCreatedAt == self.LOCAL_DT

        payload = build_asset_upload_ready_payload(
            sample_gumnut_asset, owner_id="22222222-2222-2222-2222-222222222222"
        )
        assert payload.asset.fileModifiedAt == self.LOCAL_DT


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
    """Adapter passes through display-space dims from the Gumnut API as-is.

    The Gumnut API stores ``asset.width/height`` in display space at ingest and
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
        # As the Gumnut API returns the asset: dims already in display space.
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

        The Gumnut API owns display-space dims at ingest; the adapter must not
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

    def test_extract_sync_exif_without_metadata_uses_asset_dims(
        self, sample_gumnut_asset
    ):
        """Without metadata, the upload-ready payload still carries asset dims.

        The WebSocket upload path builds ``exifInfo`` from the full asset even
        when EXIF extraction has not populated ``metadata`` yet.
        """
        sample_gumnut_asset.width = 1920
        sample_gumnut_asset.height = 1080
        sample_gumnut_asset.metadata = None

        result = extract_sync_exif(sample_gumnut_asset, asset_uuid="x")

        assert result.exifImageWidth == 1920
        assert result.exifImageHeight == 1080
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

    def test_zero_raw_dims_fall_back_to_asset_dims(self, sample_gumnut_asset):
        """Zero from the Gumnut API on raw dims is treated as unknown: fall back
        to asset.width/height and null orientation, mirroring the NULL case.

        The Immich mobile asset viewer computes ``width / height`` to size
        its viewport and only guards against ``null``; a ``0/0`` ratio yields
        ``NaN`` and crashes the viewer on tap.
        """
        sample_gumnut_asset.width = 1920
        sample_gumnut_asset.height = 1080
        _attach_metadata(sample_gumnut_asset, orientation=6, raw_width=0, raw_height=0)

        sync_result = extract_sync_exif(sample_gumnut_asset, asset_uuid="x")
        assert sync_result.exifImageWidth == 1920
        assert sync_result.exifImageHeight == 1080
        assert sync_result.orientation is None

        rest_result = extract_exif_info(sample_gumnut_asset)
        assert rest_result.exifImageWidth == 1920
        assert rest_result.exifImageHeight == 1080
        assert rest_result.orientation is None

    def test_zero_dims_everywhere_emit_none(self, sample_gumnut_asset):
        """When both raw and asset dims are 0, both wires must emit None —
        not 0. This is the videos-without-EXIF cohort that was crashing the
        mobile asset viewer.
        """
        sample_gumnut_asset.width = 0
        sample_gumnut_asset.height = 0
        _attach_metadata(
            sample_gumnut_asset, orientation=None, raw_width=0, raw_height=0
        )

        sync_result = extract_sync_exif(sample_gumnut_asset, asset_uuid="x")
        assert sync_result.exifImageWidth is None
        assert sync_result.exifImageHeight is None
        assert sync_result.orientation is None

        rest_result = extract_exif_info(sample_gumnut_asset)
        assert rest_result.exifImageWidth is None
        assert rest_result.exifImageHeight is None
        assert rest_result.orientation is None


class TestChecksumEmission:
    """Every outbound converter emits the Immich-facing ``checksum_sha1``, never
    Gumnut's SHA-256 ``checksum`` or the legacy ``"placeholder-checksum"``.

    See ``resolve_immich_checksum`` for why the format distinction matters.
    """

    # 28-char base64 SHA-1 (the correct Immich wire value) vs. the SHA-256
    # placeholder the fixture carries on ``.checksum``.
    SHA1_B64 = "PaDX6+c+Lhjpm5/ciXUROL1ryaU="
    OWNER_ID = "22222222-2222-2222-2222-222222222222"

    def test_rest_converter_emits_sha1(self, sample_gumnut_asset, mock_current_user):
        sample_gumnut_asset.file_data.checksum = "base64-sha256-value-not-this"
        sample_gumnut_asset.file_data.checksum_sha1 = self.SHA1_B64

        result = convert_gumnut_asset_to_immich(sample_gumnut_asset, mock_current_user)

        assert result.checksum == self.SHA1_B64

    def test_websocket_payload_emits_sha1(self, sample_gumnut_asset):
        sample_gumnut_asset.file_data.checksum = "base64-sha256-value-not-this"
        sample_gumnut_asset.file_data.checksum_sha1 = self.SHA1_B64

        payload = build_asset_upload_ready_payload(
            sample_gumnut_asset, owner_id=self.OWNER_ID
        )

        assert payload.asset.checksum == self.SHA1_B64

    def test_sync_converter_emits_sha1(self, sample_gumnut_asset):
        sample_gumnut_asset.file_data.checksum = "base64-sha256-value-not-this"
        sample_gumnut_asset.file_data.checksum_sha1 = self.SHA1_B64

        result = gumnut_asset_to_sync_asset_v1(
            sample_gumnut_asset, owner_id=self.OWNER_ID
        )

        assert result.checksum == self.SHA1_B64

    def test_null_sha1_emits_empty_not_sha256_or_placeholder(
        self, sample_gumnut_asset, mock_current_user
    ):
        """When ``checksum_sha1`` is null, every converter emits ``""`` — a
        clean dedup no-match — rather than the SHA-256 or
        ``"placeholder-checksum"``, which look valid but never match."""
        sample_gumnut_asset.file_data.checksum = "base64-sha256-value-not-this"
        sample_gumnut_asset.file_data.checksum_sha1 = None

        rest = convert_gumnut_asset_to_immich(sample_gumnut_asset, mock_current_user)
        ws = build_asset_upload_ready_payload(
            sample_gumnut_asset, owner_id=self.OWNER_ID
        )
        sync = gumnut_asset_to_sync_asset_v1(
            sample_gumnut_asset, owner_id=self.OWNER_ID
        )

        for emitted in (rest.checksum, ws.asset.checksum, sync.checksum):
            assert emitted == ""
            assert emitted != sample_gumnut_asset.file_data.checksum
            assert emitted != "placeholder-checksum"

    def test_null_sha1_logs_warning_with_asset_id(
        self, sample_gumnut_asset, mock_current_user, caplog
    ):
        """The null path is an explicit operator-facing diagnostic, not a
        silent fallback: it must log a WARNING carrying the asset id so the
        rare legacy-row cohort stays observable."""
        sample_gumnut_asset.file_data.checksum_sha1 = None

        with caplog.at_level(logging.WARNING, logger="routers.utils.asset_conversion"):
            convert_gumnut_asset_to_immich(sample_gumnut_asset, mock_current_user)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("checksum_sha1" in r.message for r in warnings)
        assert any(
            getattr(r, "asset_id", None) == sample_gumnut_asset.id for r in warnings
        )

    def test_non_null_sha1_emits_no_warning(
        self, sample_gumnut_asset, mock_current_user, caplog
    ):
        """The common path (a populated ``checksum_sha1``) must stay silent.

        Locks in "no log spam on the happy path": a refactor that moved the
        WARNING out of the ``is None`` branch would fire here and fail.
        """
        sample_gumnut_asset.file_data.checksum_sha1 = self.SHA1_B64

        with caplog.at_level(logging.WARNING, logger="routers.utils.asset_conversion"):
            convert_gumnut_asset_to_immich(sample_gumnut_asset, mock_current_user)

        assert not [r for r in caplog.records if r.levelno == logging.WARNING]


class TestThumbhashEmission:
    """Every outbound converter passes the upstream ``thumbhash`` straight
    through to its Immich DTO, and emits ``None`` when it has not been generated
    yet.

    thumbhash is a plain 1:1 passthrough (no format remap, no null warning),
    unlike ``resolve_immich_checksum`` — so it is emitted inline at each site
    rather than through a resolver helper.
    """

    # A representative base64 ThumbHash value.
    THUMBHASH = "1QcSHQRnh493V4dIh4eXh1h4kJUI"
    OWNER_ID = "22222222-2222-2222-2222-222222222222"

    def test_rest_converter_emits_thumbhash(
        self, sample_gumnut_asset, mock_current_user
    ):
        sample_gumnut_asset.thumbhash = self.THUMBHASH

        result = convert_gumnut_asset_to_immich(sample_gumnut_asset, mock_current_user)

        assert result.thumbhash == self.THUMBHASH

    def test_websocket_payload_emits_thumbhash(self, sample_gumnut_asset):
        sample_gumnut_asset.thumbhash = self.THUMBHASH

        payload = build_asset_upload_ready_payload(
            sample_gumnut_asset, owner_id=self.OWNER_ID
        )

        assert payload.asset.thumbhash == self.THUMBHASH

    def test_sync_converter_emits_thumbhash(self, sample_gumnut_asset):
        sample_gumnut_asset.thumbhash = self.THUMBHASH

        result = gumnut_asset_to_sync_asset_v1(
            sample_gumnut_asset, owner_id=self.OWNER_ID
        )

        assert result.thumbhash == self.THUMBHASH

    def test_null_thumbhash_passes_through_as_none(
        self, sample_gumnut_asset, mock_current_user
    ):
        """When upstream ``thumbhash`` is null (not yet generated), every
        converter emits ``None`` — the nullable Immich placeholder — rather than
        the old hardcoded ``""`` / constant-string stand-ins."""
        sample_gumnut_asset.thumbhash = None

        rest = convert_gumnut_asset_to_immich(sample_gumnut_asset, mock_current_user)
        ws = build_asset_upload_ready_payload(
            sample_gumnut_asset, owner_id=self.OWNER_ID
        )
        sync = gumnut_asset_to_sync_asset_v1(
            sample_gumnut_asset, owner_id=self.OWNER_ID
        )

        assert rest.thumbhash is None
        assert ws.asset.thumbhash is None
        assert sync.thumbhash is None


class TestFormatDuration:
    """``format_duration`` turns upstream float seconds into Immich's
    ``HH:MM:SS.ffffff`` interval string, and ``None`` into ``None`` so callers
    can preserve their existing absent-duration behavior."""

    def test_none_passes_through(self):
        assert format_duration(None) is None

    @pytest.mark.parametrize(
        "seconds, expected",
        [
            (0.0, "00:00:00.000000"),
            (5.5, "00:00:05.500000"),
            (65.25, "00:01:05.250000"),
            (3661.5, "01:01:01.500000"),
            (7200, "02:00:00.000000"),
            # Long clip past 24h still renders hours without wrapping.
            (90061.0, "25:01:01.000000"),
            # Just under a minute/hour boundary must carry up, never render
            # an out-of-range :60 seconds field.
            (59.9999999, "00:01:00.000000"),
            (3599.9999999, "01:00:00.000000"),
        ],
    )
    def test_formats_seconds_as_interval(self, seconds, expected):
        assert format_duration(seconds) == expected


class TestDurationEmission:
    """Every outbound converter forwards the upstream ``duration`` (float
    seconds) as an ``HH:MM:SS.ffffff`` string. When upstream is NULL each site
    preserves the value it emitted before this field existed — never a
    fabricated length. Mirrors the width/height staged-rollout precedent."""

    OWNER_ID = "22222222-2222-2222-2222-222222222222"

    def test_rest_converter_formats_populated_duration(
        self, sample_gumnut_asset, mock_current_user
    ):
        sample_gumnut_asset.mime_type = "video/mp4"
        sample_gumnut_asset.duration = 12.5

        result = convert_gumnut_asset_to_immich(sample_gumnut_asset, mock_current_user)

        assert result.duration == "00:00:12.500000"

    def test_websocket_payload_formats_populated_duration(self, sample_gumnut_asset):
        sample_gumnut_asset.mime_type = "video/mp4"
        sample_gumnut_asset.duration = 30.0

        payload = build_asset_upload_ready_payload(
            sample_gumnut_asset, owner_id=self.OWNER_ID
        )

        assert payload.asset.duration == "00:00:30.000000"

    def test_sync_converter_formats_populated_duration(self, sample_gumnut_asset):
        sample_gumnut_asset.mime_type = "video/mp4"
        sample_gumnut_asset.duration = 30.0

        result = gumnut_asset_to_sync_asset_v1(
            sample_gumnut_asset, owner_id=self.OWNER_ID
        )

        assert result.duration == "00:00:30.000000"

    def test_null_duration_preserves_prior_behavior(
        self, sample_gumnut_asset, mock_current_user
    ):
        """Upstream NULL: the REST single-asset DTO keeps its zero placeholder
        for video, while the WebSocket and sync converters keep ``None``."""
        sample_gumnut_asset.mime_type = "video/mp4"
        sample_gumnut_asset.duration = None

        rest = convert_gumnut_asset_to_immich(sample_gumnut_asset, mock_current_user)
        ws = build_asset_upload_ready_payload(
            sample_gumnut_asset, owner_id=self.OWNER_ID
        )
        sync = gumnut_asset_to_sync_asset_v1(
            sample_gumnut_asset, owner_id=self.OWNER_ID
        )

        assert rest.duration == "00:00:00.000000"
        assert ws.asset.duration is None
        assert sync.duration is None

    def test_null_duration_image_emits_empty_string_in_rest_dto(
        self, sample_gumnut_asset, mock_current_user
    ):
        """For images (no duration concept) the REST DTO keeps its empty-string
        placeholder rather than the video zero or a fabricated value."""
        sample_gumnut_asset.mime_type = "image/jpeg"
        sample_gumnut_asset.duration = None

        result = convert_gumnut_asset_to_immich(sample_gumnut_asset, mock_current_user)

        assert result.duration == ""


class TestFileDataSourcing:
    """The file/provenance scalars (``checksum_sha1`` / ``file_modified_at`` /
    ``file_size_bytes``) are read from the nested ``file_data`` group, never the
    deprecated top-level scalars.

    Uses real SDK models (not Mocks): the top-level scalars default to ``None`` on
    a real ``AssetResponse``, so a regression that read them instead of
    ``file_data`` would surface here as an empty checksum / wrong modify-time.
    """

    DT = datetime(2024, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    MTIME = datetime(2024, 3, 2, 8, 30, 0, tzinfo=timezone.utc)
    SHA1_B64 = "PaDX6+c+Lhjpm5/ciXUROL1ryaU="

    def _file_data(self) -> FileDataResponse:
        return FileDataResponse(
            device_asset_id="dev-asset",
            device_id="dev",
            file_created_at=self.DT,
            file_modified_at=self.MTIME,
            checksum="base64-sha256-not-this",
            checksum_sha1=self.SHA1_B64,
            file_size_bytes=4242,
        )

    def _asset(self, file_data: FileDataResponse | None) -> AssetResponse:
        return AssetResponse(
            id="asset_test",
            mime_type="image/jpeg",
            original_file_name="test.jpg",
            local_datetime=self.DT,
            created_at=self.DT,
            updated_at=self.DT,
            metadata=None,
            file_data=file_data,
        )

    def test_checksum_read_from_file_data(self):
        assert resolve_immich_checksum(self._asset(self._file_data())) == self.SHA1_B64

    def test_checksum_empty_when_file_data_absent(self):
        # No file_data (e.g. include=file_data not requested) → empty Immich
        # checksum (the legacy null-SHA1 fallback), never the SHA-256.
        assert resolve_immich_checksum(self._asset(None)) == ""

    def test_file_modified_at_read_from_file_data(self):
        result = resolve_file_modified_at(self._asset(self._file_data()))
        assert result == to_actual_utc(self.MTIME)

    def test_file_modified_at_falls_back_to_capture_when_file_data_absent(self):
        # No file_data and no metadata → the modify-time cascade bottoms out at
        # the capture time, never ``None`` (Immich requires fileModifiedAt).
        result = resolve_file_modified_at(self._asset(None))
        assert result == to_actual_utc(self.DT)
