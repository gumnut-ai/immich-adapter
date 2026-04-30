"""Tests for asset conversion utilities — focused on trash-state propagation.

The DTO conversion sites in ``routers/utils/asset_conversion.py`` must surface
``trashed_at`` to Immich clients via:

- ``AssetResponseDto.isTrashed`` (boolean) — gates the "In trash" indicator and
  the restore-vs-delete action bar in the UI.
- ``SyncAssetV1.deletedAt`` (nullable datetime) — the trash-state signal the
  sync stream ships to mobile clients.
"""

from datetime import datetime, timezone

from routers.utils.asset_conversion import (
    build_asset_upload_ready_payload,
    convert_gumnut_asset_to_immich,
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
