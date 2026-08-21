"""Select versions from an asset's chain: root, current rendering, edit base.

The Gumnut version chain is ordered by ``position``: 0 is the upload, the
highest is the current rendering. Each version's ``kind`` says what produced
it — ``original``, ``edit`` (a rendered Immich edit), ``external:<service>``,
or any future value, since the namespace is open.

The selections serve distinct contracts:

* ``select_root`` returns the position-0 upload. ``GET
  /api/assets/{id}/original?edited=false`` — Immich's *Download original* and
  the path backups rely on — always streams these exact bytes, so anything
  Immich reports as edited (every non-``original`` kind) can be undone to the
  upload.
* ``select_current`` returns the highest-position version — the current
  rendering. The Immich edit routes read and mutate the chain through it.
* ``select_edit_base`` returns the highest-position version that is not an
  edit. Immich's editor expresses recipes against a single base image; basing
  on the latest non-edit rendering keeps repeated adjustments non-cumulative
  (a prior edit is never a base) while preserving whatever an external
  rendering layered onto the upload. Until an external rendering exists, this
  is the root.

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


def select_root[V: VersionLike](versions: Sequence[V], *, asset_id: str) -> V:
    """Return the unique position-0 version.

    Raises :class:`InvalidVersionChainError` when the chain has no root or more
    than one; callers map that to their own error type.
    """
    roots = [version for version in versions if version.position == 0]
    if len(roots) != 1:
        logger.error(
            "Asset version chain has no unique root",
            extra={"asset_id": asset_id, "root_count": len(roots)},
        )
        raise InvalidVersionChainError("no unique root")
    return roots[0]


def select_current[V: VersionLike](versions: Sequence[V], *, asset_id: str) -> V:
    """Return the current rendering: the highest-position version.

    Validates the chain has a unique root first, raising
    :class:`InvalidVersionChainError` otherwise (which also covers an empty
    chain); callers map that to their own error type.
    """
    select_root(versions, asset_id=asset_id)
    return max(versions, key=lambda version: version.position)


def is_edit_kind(kind: str) -> bool:
    """True for the documented ``edit`` kind and any namespaced ``edit:*`` variant.

    Also usable against ``AssetResponse.kind``, which names what produced the
    asset's *current* rendering.
    """
    return kind == "edit" or kind.startswith("edit:")


def is_edit_version(version: VersionLike) -> bool:
    """True for renderings the adapter must never use as an edit base."""
    return is_edit_kind(version.kind)


def select_edit_base[V: VersionLike](versions: Sequence[V], *, asset_id: str) -> V:
    """Return the highest-position non-edit version.

    Raises :class:`InvalidVersionChainError` when the chain lacks a unique
    position-0 root or contains no non-edit version. Callers map that to their
    own error type.
    """
    select_root(versions, asset_id=asset_id)

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
