"""Shared type aliases for the sync package."""

from dataclasses import dataclass
from typing import TypeAlias
from uuid import UUID

from gumnut.types.album_asset_response import AlbumAssetResponse
from gumnut.types.album_response import AlbumResponse
from gumnut.types.asset_response import AssetResponse
from gumnut.types.face_response import FaceResponse
from gumnut.types.person_response import PersonResponse
from gumnut.types.stack_list_stacks_response import StackListStacksResponse


@dataclass(frozen=True)
class FetchedStack:
    """A stack row paired with its resolved effective primary.

    Stacks are the one fetched entity whose sync conversion needs a value that
    isn't on the row: SyncStackV1 requires a non-null primaryAssetId, and an
    unpinned burst's cover is only knowable by reading its members. Resolving it
    is an async round-trip, but convert_entity_to_sync_event is deliberately
    I/O-free — so fetch_entities_map hydrates the members and carries the
    resolved primary here, keeping the converter pure.
    """

    row: StackListStacksResponse
    primary_asset_id: UUID

    @property
    def id(self) -> str:
        """The Gumnut stack id, so the generic stream loop can track it like any
        other fetched entity (see stats.streamed_ids)."""
        return self.row.id


EntityType: TypeAlias = (
    AssetResponse
    | AlbumResponse
    | AlbumAssetResponse
    | PersonResponse
    | FaceResponse
    | FetchedStack
)
