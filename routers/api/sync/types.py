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
    """A stack row paired with the member-derived primary required by sync."""

    row: StackListStacksResponse
    primary_asset_id: UUID

    @property
    def id(self) -> str:
        """Return the Gumnut stack ID used by the generic stream loop."""
        return self.row.id


EntityType: TypeAlias = (
    AssetResponse
    | AlbumResponse
    | AlbumAssetResponse
    | PersonResponse
    | FaceResponse
    | FetchedStack
)
