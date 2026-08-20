"""Select the version an Immich edit is based on.

The Gumnut version chain is ordered by ``position``: 0 is the upload, the
highest is the current rendering. Each version's ``kind`` says what produced
it — ``original``, ``edit`` (a rendered Immich edit), ``external:<service>``,
or any future value, since the namespace is open.

Immich's editor assumes a single base image and expresses recipes against it,
so the adapter keeps one definition of the *edit base*, shared by the renderer
and by ``GET /api/assets/{id}/original?edited=false``: the highest-position
version that is not an edit. Basing on the latest non-edit rendering keeps
repeated adjustments non-cumulative (a prior edit is never a base) while
preserving whatever an external rendering layered onto the upload.
Until an external rendering exists, this is the position-0 original.

External renderings are produced from the full chain below them, so an
``edit`` sitting below an ``external:*`` version (``original -> edit ->
external``) is already baked into that rendering. The latest non-edit version
is therefore always the correct base; prior edits below it are intentionally
part of it, and versions need no provenance pointer for this selection.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

logger = logging.getLogger(__name__)


class VersionLike(Protocol):
    id: str
    position: int
    kind: str


class InvalidVersionChainError(Exception):
    """The chain has no unique root or no non-edit version to base on."""


def is_edit_version(version: VersionLike) -> bool:
    """True for renderings the adapter must never use as an edit base.

    Matches the documented ``edit`` kind and any namespaced ``edit:*`` variant.
    """
    kind = version.kind
    return kind == "edit" or kind.startswith("edit:")


def select_edit_base[V: VersionLike](versions: Sequence[V], *, asset_id: str) -> V:
    """Return the highest-position non-edit version.

    Raises :class:`InvalidVersionChainError` when the chain lacks a unique
    position-0 root or contains no non-edit version. Callers map that to their
    own error type.
    """
    root_count = sum(1 for version in versions if version.position == 0)
    if root_count != 1:
        logger.error(
            "Asset version chain has no unique root",
            extra={"asset_id": asset_id, "root_count": root_count},
        )
        raise InvalidVersionChainError("no unique root")

    ordered = sorted(versions, key=lambda version: version.position)
    bases = [version for version in ordered if not is_edit_version(version)]
    if not bases:
        logger.error(
            "Asset version chain has no non-edit version",
            extra={"asset_id": asset_id, "version_count": len(versions)},
        )
        raise InvalidVersionChainError("no non-edit version")
    base = bases[-1]
    return base
