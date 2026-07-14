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
— shape and construction-site inspection that pins the sync DTO surface and the
converter call sites directly, without needing to construct the DTOs.
"""

import inspect

from routers.api.sync import converters, events, stream
from routers.api.sync.fk_integrity import _GUMNUT_TYPE_TO_SYNC_TYPES
from routers.api.sync.stream import (
    _DERIVED_UPSERT_ONLY_TYPES,
    _NOOP_REQUEST_TYPES,
    _SUPPORTED_REQUEST_TYPES,
    _SYNC_TYPE_ORDER,
    _V1_SUPERSEDED_BY_V2,
)
from routers.immich_models import (
    SyncAlbumUserV1,
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


# --- Converters (construction sites asserted via source inspection) ----------


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
    # The stream loop must actually consult the table — match the call site,
    # not the bare identifier (which also appears in a nearby comment).
    assert "_V1_SUPERSEDED_BY_V2.get(" in inspect.getsource(stream.generate_sync_stream)


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


# --- Invariants: keep the V2 rollout consistent across the sync package ------
# Adding a V2 to _SYNC_TYPE_ORDER means it must also be routed in events, kept in
# the FK-checkpoint map, and (for the album/face causal-consistency overrides)
# covered by those overrides — these guard against the half-wired V2 rollout.


def test_fk_checkpoint_map_covers_every_streamed_type():
    """Every _SYNC_TYPE_ORDER entity type is in the FK checkpoint map for its
    gumnut type, so a client that synced under V2 still matches on checkpoint
    lookups (else FK reference checks emit spurious warnings)."""
    for _req, gumnut_type, sync_type in _SYNC_TYPE_ORDER:
        assert sync_type in _GUMNUT_TYPE_TO_SYNC_TYPES.get(gumnut_type, []), (
            f"{sync_type} missing from _GUMNUT_TYPE_TO_SYNC_TYPES[{gumnut_type!r}]"
        )


def test_events_routing_references_every_streamed_v2_type():
    """Every V2 entity type in _SYNC_TYPE_ORDER is dispatched in the event
    converter — a V2 added to the order but not routed would silently stream
    as V1 (the converter falls through to the V1 branch)."""
    routing_src = inspect.getsource(events.convert_entity_to_sync_event)
    v2_types = [st for _r, _g, st in _SYNC_TYPE_ORDER if st.value.endswith("V2")]
    assert v2_types  # guard against the filter silently matching nothing
    for sync_type in v2_types:
        assert f"SyncEntityType.{sync_type.name}" in routing_src, (
            f"{sync_type} not routed in convert_entity_to_sync_event"
        )


def test_album_cover_override_applies_to_album_v2():
    """The album cover causal-consistency override must run on the AlbumV2 path
    (the v3 client streams albums as AlbumV2), not just AlbumV1. The override
    lives in _stream_entity_type, where AlbumV2 appears only in that gate."""
    assert "SyncEntityType.AlbumV2" in inspect.getsource(stream._stream_entity_type)


# --- AlbumUsersV1: owner album-user link (required for v3 album display) ------
# SyncAlbumV2 dropped ownerId, so the v3 client no longer derives the owner from
# the album event. Its album-list query inner-joins on an owner-role album-user
# row, so the adapter must emit AlbumUserV1 (derived from the same album events)
# or every album is filtered out of the list and never displayed.


def test_album_users_v1_streamed_from_album_entity():
    """AlbumUsersV1 streams the owner link off the "album" gumnut entity, after
    the album itself (FK parent) — placed before AlbumToAssetsV1 in the order."""
    assert (
        SyncRequestType.AlbumUsersV1,
        "album",
        SyncEntityType.AlbumUserV1,
    ) in _SYNC_TYPE_ORDER
    # Requested by the client, so it must not be logged as unsupported.
    assert SyncRequestType.AlbumUsersV1 in _SUPPORTED_REQUEST_TYPES
    # FK ordering: album (AlbumsV2) must be streamed before its owner link.
    order = [st for _r, _g, st in _SYNC_TYPE_ORDER]
    assert order.index(SyncEntityType.AlbumV2) < order.index(SyncEntityType.AlbumUserV1)


def test_album_user_v1_is_derived_upsert_only():
    """AlbumUserV1's deletes are handled by the album pass + client-side FK
    cascade, so its own pass must be upsert-only (no duplicate AlbumDeleteV1)."""
    assert SyncEntityType.AlbumUserV1 in _DERIVED_UPSERT_ONLY_TYPES
    # The stream loop must actually gate emit_deletes on the set.
    assert "_DERIVED_UPSERT_ONLY_TYPES" in inspect.getsource(
        stream.generate_sync_stream
    )
    assert "emit_deletes" in inspect.getsource(stream._stream_entity_type)


def test_album_user_converter_sets_owner_role():
    """The owner link is single-user: albumId + owner userId + role=owner."""
    src = inspect.getsource(converters.gumnut_album_to_sync_album_user_v1)
    assert "AlbumUserRole.owner" in src
    # AlbumUserV1 must be routed in the event converter (else it would fall
    # through to the AlbumV1 branch and emit the wrong entity).
    assert "SyncEntityType.AlbumUserV1" in inspect.getsource(
        events.convert_entity_to_sync_event
    )
    # Role field exists on the DTO the converter builds.
    assert "role" in SyncAlbumUserV1.model_fields
