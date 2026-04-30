"""Sync-stream trash propagation: SyncAssetV1.deletedAt and state=all hydration.

The sync stream surfaces trash transitions (asset_trashed/asset_restored events)
to mobile clients as ``SyncAssetV1`` upserts with ``deletedAt`` populated. Two
pieces have to land together:

- ``gumnut_asset_to_sync_asset_v1`` reads ``trashed_at`` off the source asset.
- ``fetch_entities_map``'s asset branch fetches with ``state="all"`` so trashed
  rows are included; without this an ``asset_trashed`` event arrives, the live-
  only fetch returns no row, and the event is dropped on hydration with a
  "likely deleted between event and fetch" warning.
"""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from routers.api.sync.converters import gumnut_asset_to_sync_asset_v1
from routers.api.sync.entity_fetch import fetch_entities_map
from tests.unit.api.sync.conftest import (
    create_mock_asset_data,
    create_mock_entity_page,
)


class TestGumnutAssetToSyncAssetV1DeletedAt:
    def test_live_asset_deleted_at_none(self):
        """A live asset (trashed_at=None) produces SyncAssetV1.deletedAt=None."""
        updated_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
        asset = create_mock_asset_data(updated_at)
        asset.trashed_at = None

        sync_asset = gumnut_asset_to_sync_asset_v1(
            asset, owner_id="11111111-1111-1111-1111-111111111111"
        )

        assert sync_asset.deletedAt is None

    def test_trashed_asset_deleted_at_populated(self):
        """A trashed asset's trashed_at flows through to SyncAssetV1.deletedAt.

        Mobile clients key off this field to move the local file to the device
        trash; missing it would cause asset_trashed events to manifest as live
        assets with no trash signal.
        """
        updated_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
        trashed_at = datetime(2026, 4, 2, 15, 30, tzinfo=timezone.utc)
        asset = create_mock_asset_data(updated_at)
        asset.trashed_at = trashed_at

        sync_asset = gumnut_asset_to_sync_asset_v1(
            asset, owner_id="11111111-1111-1111-1111-111111111111"
        )

        assert sync_asset.deletedAt == trashed_at


class TestFetchEntitiesMapAssetStateAll:
    @pytest.mark.anyio
    async def test_asset_branch_passes_state_all(self):
        """The asset branch must fetch with state="all" to include trashed rows.

        Without state="all", an asset_trashed event arrives in the stream, the
        adapter tries to hydrate it via the default live-only filter, the
        backend returns no row, and the event is dropped — so the mobile client
        never learns the asset was trashed and skips the restore window.
        """
        client = Mock()
        client.assets.list = Mock(return_value=create_mock_entity_page([]))

        await fetch_entities_map(client, "asset", ["asset_abc"])

        client.assets.list.assert_called_once()
        kwargs = client.assets.list.call_args.kwargs
        assert kwargs.get("state") == "all"
        assert kwargs.get("ids") == ["asset_abc"]

    @pytest.mark.anyio
    async def test_metadata_branch_does_not_pass_state(self):
        """The metadata branch stays on the default (live-only) filter.

        Metadata events on trashed assets are rare, and the existing
        "explicitly missing" log path surfaces them if they ever arrive.
        Switching the metadata branch to state="all" would broaden the
        privileged fetch unnecessarily.
        """
        client = Mock()
        client.assets.list = Mock(return_value=create_mock_entity_page([]))

        await fetch_entities_map(client, "metadata", ["asset_abc"])

        client.assets.list.assert_called_once()
        kwargs = client.assets.list.call_args.kwargs
        assert "state" not in kwargs
