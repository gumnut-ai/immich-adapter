"""Immich v3 Sync-v2 conformance for the sync stream.

At server version >= 3.0.0 the mobile client requests the V2 sync entity types
(`assetsV2`, `albumsV2`, `assetFacesV2`) plus the new `assetOcrV1`, and swaps
`partnerAssetsV2` / `albumAssetsV2` in for their V1 forms. These guard the thin
V2 layer that extends the existing faces V1/V2 dual pattern:

- `SyncAssetV2` is `SyncAssetV1` with int-ms `duration`.
- `SyncAlbumV2` is `SyncAlbumV1` minus `ownerId` (the GA model dropped it; the
  design doc's "byte-identical" predates that drop).
- `SyncAssetV1.createdAt` is now required — both construction sites must set it.
- OCR / partner-asset / album-asset V2 request types are accepted but emit
  nothing (Gumnut has no such data); no V1/V2 double-emission.

Assertions use `model_fields`, module-level constants, and `inspect.getsource`
rather than constructing the DTOs: on the `migration/immichv3` branch the
regenerated models carry `pattern`-constrained UUID/datetime fields that raise
under the pinned pydantic, so instantiating a sync DTO is impossible here.
Class/field/source inspection does not instantiate anything and runs cleanly.
"""

import inspect

from routers.api.sync import converters, events, stream
from routers.api.sync.stream import (
    _NOOP_REQUEST_TYPES,
    _SUPPORTED_REQUEST_TYPES,
    _SYNC_TYPE_ORDER,
    _V1_SUPERSEDED_BY_V2,
)
from routers.immich_models import (
    SyncAlbumV1,
    SyncAlbumV2,
    SyncAssetV1,
    SyncAssetV2,
    SyncEntityType,
    SyncRequestType,
)
from routers.utils import asset_conversion


# --- Payload shape (model_fields only — no instantiation) --------------------


def test_sync_asset_v2_is_v1_with_int_ms_duration():
    """SyncAssetV2 has the same fields as V1, but duration is int-ms not string."""
    assert set(SyncAssetV2.model_fields) == set(SyncAssetV1.model_fields)
    assert SyncAssetV1.model_fields["duration"].annotation == (str | None)
    assert SyncAssetV2.model_fields["duration"].annotation == (int | None)


def test_sync_album_v2_is_v1_minus_owner_id():
    """The GA SyncAlbumV2 drops ownerId; otherwise identical to V1."""
    assert set(SyncAlbumV1.model_fields) - set(SyncAlbumV2.model_fields) == {"ownerId"}
    assert set(SyncAlbumV2.model_fields) - set(SyncAlbumV1.model_fields) == set()


def test_sync_asset_v1_created_at_now_required():
    """v3 made SyncAssetV1.createdAt required (no default)."""
    assert SyncAssetV1.model_fields["createdAt"].is_required()


# --- Converters (source inspection — instantiation hits the pattern blocker) --


def test_both_sync_asset_v1_sites_set_created_at():
    """Both SyncAssetV1 construction sites populate the now-required createdAt."""
    assert "createdAt=" in inspect.getsource(converters.gumnut_asset_to_sync_asset_v1)
    assert "createdAt=" in inspect.getsource(
        asset_conversion.build_asset_upload_ready_payload
    )


def test_asset_v2_converter_emits_int_ms_duration():
    """The V2 asset converter swaps duration to int-ms via duration_ms."""
    src = inspect.getsource(converters.gumnut_asset_to_sync_asset_v2)
    assert "duration_ms" in src
    assert 'fields["duration"]' in src


def test_album_v2_converter_drops_owner_id():
    """The V2 album converter drops ownerId (absent from SyncAlbumV2)."""
    src = inspect.getsource(converters.gumnut_album_to_sync_album_v2)
    assert 'fields.pop("ownerId"' in src


# --- Stream wiring (module constants) ----------------------------------------


def test_stream_order_maps_assets_v2_and_albums_v2():
    """AssetsV2/AlbumsV2 stream through the asset/album entities as V2 events."""
    assert (
        SyncRequestType.AssetsV2,
        "asset",
        SyncEntityType.AssetV2,
    ) in _SYNC_TYPE_ORDER
    assert (
        SyncRequestType.AlbumsV2,
        "album",
        SyncEntityType.AlbumV2,
    ) in _SYNC_TYPE_ORDER


def test_v1_superseded_by_v2_covers_assets_albums_faces():
    """Requesting a V2 type skips its V1 counterpart — no double-emission."""
    assert _V1_SUPERSEDED_BY_V2 == {
        SyncRequestType.AssetsV1: SyncRequestType.AssetsV2,
        SyncRequestType.AlbumsV1: SyncRequestType.AlbumsV2,
        SyncRequestType.AssetFacesV1: SyncRequestType.AssetFacesV2,
    }
    # The stream loop must actually consult the table.
    assert "_V1_SUPERSEDED_BY_V2" in inspect.getsource(stream.generate_sync_stream)


def test_ocr_partner_album_asset_v2_are_accepted_noops():
    """New v3 request types with no Gumnut data are accepted but emit nothing."""
    noop_types = {
        SyncRequestType.AssetOcrV1,
        SyncRequestType.PartnerAssetsV2,
        SyncRequestType.AlbumAssetsV2,
    }
    # Accepted (so they are not logged as "unsupported")...
    assert noop_types <= set(_NOOP_REQUEST_TYPES)
    assert noop_types <= _SUPPORTED_REQUEST_TYPES
    # ...but never streamed (absent from the entity-producing order).
    streamed_request_types = {rt for rt, _, _ in _SYNC_TYPE_ORDER}
    assert noop_types.isdisjoint(streamed_request_types)


def test_event_conversion_routes_v2_entities():
    """convert_entity_to_sync_event dispatches AssetV2/AlbumV2 to the V2 converters."""
    src = inspect.getsource(events.convert_entity_to_sync_event)
    assert "SyncEntityType.AssetV2" in src
    assert "gumnut_asset_to_sync_asset_v2" in src
    assert "SyncEntityType.AlbumV2" in src
    assert "gumnut_album_to_sync_album_v2" in src
