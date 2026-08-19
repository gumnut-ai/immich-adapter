"""Pure codec between Immich edit-action lists and the Gumnut v1 edit recipe.

Immich's editor submits an ordered list of crop / rotate / mirror actions and
expects the server to produce the pixels. Gumnut stores one consolidated,
normalized recipe on the `edit` version's `params` — the Gumnut API treats
`params` as opaque JSON whose schema is defined by the producer, so this module
*is* the schema definition for `kind="edit"`. Nothing in the adapter calls
this module yet: both directions live here so that the planned pixel-rendering
pipeline and the planned edit routes can share a single tested semantic
contract when they land.

The v1 recipe JSON object::

    {
        "version": 1,
        "crop": {"x": 0, "y": 0, "width": 100, "height": 100},  # optional
        "angle": 0 | 90 | 180 | 270,
        "mirror": true | false
    }

Pipeline semantics (fixed, independent of Immich's serialization order):
crop is applied to the display-oriented source frame first, then rotation by
`angle` (Immich's `RotateParameters.angle` convention), then a horizontal
mirror when `mirror` is true. This matches the upstream Immich server, which
extracts the crop first regardless of its list position and composes the
remaining actions into one affine matrix in list order, applying the *last*
list element to the pixels *first* (`server/src/repositories/media.repository.ts`
and `web/src/lib/utils/editor.ts` in upstream Immich). Every duplicate-free
rotate/mirror sequence composes to exactly one of the eight display-orientation
states, uniquely representable as `(mirror, angle)` — so "ambiguous ordering"
reduces to the duplicate and mismatch rejections below.

Purity contract: no Gumnut SDK, FastAPI, imaging, filesystem, or network
imports. Inputs and outputs are the generated Immich DTOs plus the small typed
recipe value, so callers and tests need no app or client fixtures. Error
messages carry stable codes and never echo client-supplied values.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from routers.immich_models import (
    AssetEditAction,
    AssetEditActionItemDto,
    AssetEditActionItemResponseDto,
    CropParameters,
    MirrorAxis,
    MirrorParameters,
    RotateParameters,
)

RECIPE_VERSION = 1

# A valid list holds at most one crop, one mirror per axis, and one rotate.
MAX_EDIT_ACTIONS = 4

# Upper bound the generated Immich DTOs enforce on crop fields (2**53 - 1);
# revalidated here so a stored recipe outside it fails with this module's
# stable error instead of a pydantic ValidationError at DTO construction.
_MAX_CROP_VALUE = 9_007_199_254_740_991

# Opaque domain-separation prefix for row-ID hashing; see _synthesized_row_id.
_EDIT_ROW_ID_KEY_PREFIX = "9c1d1f2e-5a4b-4d3c-8e6f-2b7a9d0c4e51"

_RECIPE_KEYS = frozenset({"version", "crop", "angle", "mirror"})
_CROP_KEYS = frozenset({"x", "y", "width", "height"})
_VALID_ANGLES = frozenset({0, 90, 180, 270})


class AssetEditError(Exception):
    """Base for adapter-domain edit codec failures.

    Carries a stable machine-readable `code` so route code can map failures to
    Immich-shaped responses without string-matching messages.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AssetEditValidationError(AssetEditError):
    """A client-supplied Immich edit list is invalid — maps to a 4xx."""


class UnsupportedEditRecipeError(AssetEditError):
    """A stored recipe cannot be represented as Immich edits.

    Raised for unknown recipe versions, unsupported fields, and malformed
    stored params. Future edit routes are expected to surface this as "edits
    unavailable/unsupported" rather than returning misleading partial state.
    """


@dataclass(frozen=True)
class CropBox:
    """Validated crop in the display-oriented source frame."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class EditRecipe:
    """Normalized v1 edit recipe: crop, then rotate, then horizontal mirror."""

    crop: CropBox | None
    angle: int
    mirror: bool

    @property
    def is_identity(self) -> bool:
        """True when the recipe performs no transformation at all."""
        return self.crop is None and self.angle == 0 and not self.mirror

    def to_params(self) -> dict[str, object]:
        """Serialize to the v1 `params` JSON object (crop omitted when None)."""
        params: dict[str, object] = {"version": RECIPE_VERSION}
        if self.crop is not None:
            params["crop"] = {
                "x": self.crop.x,
                "y": self.crop.y,
                "width": self.crop.width,
                "height": self.crop.height,
            }
        params["angle"] = self.angle
        params["mirror"] = self.mirror
        return params

    def to_params_json(self) -> str:
        """Canonical stable serialization (sorted keys, compact separators).

        Byte-stable so stored `params` can be compared for equality across
        reads and writers without semantic JSON diffing.
        """
        return json.dumps(self.to_params(), sort_keys=True, separators=(",", ":"))


def _require_positive_int(value: int, name: str) -> None:
    # The upper bound keeps the forward/reverse contract symmetric: any crop
    # the fold accepts fits the recipe parser's (and the DTO's) field bounds.
    if type(value) is not int or value <= 0 or value > _MAX_CROP_VALUE:
        raise ValueError(f"{name} must be a positive int within the crop bounds")


def _validate_crop(
    parameters: CropParameters, source_width: int, source_height: int
) -> CropBox:
    x, y, width, height = (
        parameters.x,
        parameters.y,
        parameters.width,
        parameters.height,
    )
    for value in (x, y, width, height):
        # `type is int` also excludes bool; model_construct can bypass pydantic.
        if type(value) is not int:
            raise AssetEditValidationError(
                "invalid_crop", "Crop x/y/width/height must be integers"
            )
    if x < 0 or y < 0 or width < 1 or height < 1:
        raise AssetEditValidationError(
            "invalid_crop",
            "Crop requires non-negative x/y and strictly positive width/height",
        )
    if x + width > source_width or y + height > source_height:
        raise AssetEditValidationError(
            "crop_out_of_bounds", "Crop exceeds the source image frame"
        )
    return CropBox(x=x, y=y, width=width, height=height)


def _normalize_angle(angle: float) -> int:
    if isinstance(angle, bool) or not isinstance(angle, (int, float)):
        raise AssetEditValidationError(
            "invalid_angle", "Rotation angle must be a number"
        )
    # Only floats can be non-finite; calling isfinite on a smuggled int too
    # large for a float would raise OverflowError instead of the stable code.
    if isinstance(angle, float) and not math.isfinite(angle):
        raise AssetEditValidationError("invalid_angle", "Rotation angle must be finite")
    # Deliberately more lenient than upstream Immich's literal {0, 90, 180,
    # 270}: any finite multiple of 90 (signed or overflowing) normalizes
    # modulo 360, per the accepted recipe contract.
    if angle % 90 != 0:
        raise AssetEditValidationError(
            "invalid_angle", "Rotation angle must be a multiple of 90 degrees"
        )
    return int(angle) % 360


def immich_edits_to_recipe(
    edits: Sequence[AssetEditActionItemDto | AssetEditActionItemResponseDto],
    source_width: int,
    source_height: int,
) -> EditRecipe:
    """Fold an ordered Immich edit-action list into the normalized v1 recipe.

    `source_width` / `source_height` are the display-oriented dimensions of the
    frame the edits apply to (the parent version's display dims); crop bounds
    are validated against them. Invalid source dims are a caller bug and raise
    `ValueError`; invalid client input raises `AssetEditValidationError`.

    The rotate/mirror composition replicates upstream Immich's matrix compose:
    with list `[e1, ..., en]` the total transform is `M(e1)·...·M(en)`, i.e.
    the last list element applies to the pixels first. The fold keeps the state
    `(mirror, angle)` meaning "rotate by `angle`, then mirror horizontally if
    `mirror`" and multiplies each action's matrix on the right.
    """
    _require_positive_int(source_width, "source_width")
    _require_positive_int(source_height, "source_height")

    if len(edits) == 0:
        raise AssetEditValidationError(
            "empty_edit_list", "At least one edit action is required"
        )
    if len(edits) > MAX_EDIT_ACTIONS:
        raise AssetEditValidationError(
            "too_many_actions",
            f"An edit list holds at most {MAX_EDIT_ACTIONS} actions",
        )

    crop: CropBox | None = None
    # Duplicate keys mirror upstream Immich's uniqueness rule: one crop, one
    # rotate, one mirror *per axis* — horizontal + vertical together is legal.
    seen: set[str] = set()
    mirror = False
    angle = 0

    for edit in edits:
        action = edit.action
        parameters = edit.parameters
        if action is AssetEditAction.crop:
            if not isinstance(parameters, CropParameters):
                raise AssetEditValidationError(
                    "mismatched_parameters", "Crop action requires crop parameters"
                )
            if "crop" in seen:
                raise AssetEditValidationError(
                    "duplicate_action", "Duplicate crop action"
                )
            seen.add("crop")
            crop = _validate_crop(parameters, source_width, source_height)
        elif action is AssetEditAction.rotate:
            if not isinstance(parameters, RotateParameters):
                raise AssetEditValidationError(
                    "mismatched_parameters", "Rotate action requires rotate parameters"
                )
            if "rotate" in seen:
                raise AssetEditValidationError(
                    "duplicate_action", "Duplicate rotate action"
                )
            seen.add("rotate")
            angle = (angle + _normalize_angle(parameters.angle)) % 360
        elif action is AssetEditAction.mirror:
            if not isinstance(parameters, MirrorParameters):
                raise AssetEditValidationError(
                    "mismatched_parameters", "Mirror action requires mirror parameters"
                )
            axis = parameters.axis
            if axis is not MirrorAxis.horizontal and axis is not MirrorAxis.vertical:
                raise AssetEditValidationError(
                    "invalid_mirror_axis", "Mirror axis must be horizontal or vertical"
                )
            key = f"mirror-{axis.value}"
            if key in seen:
                raise AssetEditValidationError(
                    "duplicate_action", "Duplicate mirror action for the same axis"
                )
            seen.add(key)
            if axis is MirrorAxis.horizontal:
                angle = (-angle) % 360
            else:
                angle = (180 - angle) % 360
            mirror = not mirror
        else:
            raise AssetEditValidationError(
                "unsupported_action", "Only crop, rotate, and mirror are supported"
            )

    return EditRecipe(crop=crop, angle=angle, mirror=mirror)


def _crop_from_recipe_value(value: object) -> CropBox:
    if not isinstance(value, Mapping):
        raise UnsupportedEditRecipeError("malformed_recipe", "Recipe crop is malformed")
    if set(value.keys()) != _CROP_KEYS:
        raise UnsupportedEditRecipeError("malformed_recipe", "Recipe crop is malformed")
    fields: dict[str, int] = {}
    for key in ("x", "y", "width", "height"):
        field = value[key]
        if type(field) is not int or field < 0 or field > _MAX_CROP_VALUE:
            raise UnsupportedEditRecipeError(
                "malformed_recipe", "Recipe crop is malformed"
            )
        fields[key] = field
    if fields["width"] < 1 or fields["height"] < 1:
        raise UnsupportedEditRecipeError("malformed_recipe", "Recipe crop is malformed")
    return CropBox(
        x=fields["x"], y=fields["y"], width=fields["width"], height=fields["height"]
    )


def parse_recipe_params(params: object) -> EditRecipe:
    """Parse a stored `params` object into a validated `EditRecipe`.

    The version marker is checked before any other field. Unknown versions,
    unknown fields, and malformed values raise `UnsupportedEditRecipeError`
    rather than yielding partial state.
    """
    if not isinstance(params, Mapping):
        raise UnsupportedEditRecipeError(
            "malformed_recipe", "Edit recipe params must be a JSON object"
        )
    version = params.get("version")
    if type(version) is not int:
        raise UnsupportedEditRecipeError(
            "malformed_recipe", "Edit recipe has no valid version marker"
        )
    if version != RECIPE_VERSION:
        raise UnsupportedEditRecipeError(
            "unsupported_recipe_version", "Edit recipe version is not supported"
        )
    unknown = set(params.keys()) - _RECIPE_KEYS
    if unknown:
        raise UnsupportedEditRecipeError(
            "unsupported_recipe_field", "Edit recipe contains unsupported fields"
        )

    angle = params.get("angle")
    if type(angle) is not int or angle not in _VALID_ANGLES:
        raise UnsupportedEditRecipeError(
            "malformed_recipe", "Edit recipe angle is malformed"
        )
    mirror = params.get("mirror")
    if type(mirror) is not bool:
        raise UnsupportedEditRecipeError(
            "malformed_recipe", "Edit recipe mirror flag is malformed"
        )
    crop_value = params.get("crop")
    crop = None if crop_value is None else _crop_from_recipe_value(crop_value)
    return EditRecipe(crop=crop, angle=angle, mirror=mirror)


def _synthesized_row_id(
    asset_id: str, version_id: str, action: AssetEditAction
) -> uuid.UUID:
    # Newline-joined to keep the key unambiguous under concatenation. The IDs
    # identify the synthesized current state deterministically; they do not
    # claim to preserve user-action history.
    #
    # The hash digest is stamped with RFC 4122 version-4 bits: upstream Immich
    # validates edit-row IDs as UUIDv4 specifically, and adapter-generated IDs
    # are v4 everywhere else in this repo, so a name-based v5 UUID would fail
    # strict clients and break the local convention. Determinism (identical
    # rows across repeated reads) still matters, hence hashing instead of
    # `uuid.uuid4()`. Never change the prefix or key encoding: ID stability
    # across deployments is part of the contract, pinned by the golden-ID test.
    key = "\n".join((_EDIT_ROW_ID_KEY_PREFIX, asset_id, version_id, action.value))
    digest = bytearray(hashlib.sha256(key.encode("utf-8")).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40  # version 4
    digest[8] = (digest[8] & 0x3F) | 0x80  # RFC 4122 variant
    return uuid.UUID(bytes=bytes(digest))


def recipe_to_immich_edits(
    asset_id: str,
    version_id: str,
    params: object,
) -> list[AssetEditActionItemResponseDto]:
    """Translate a stored recipe into the canonical Immich operation list.

    Emits at most one crop, one mirror, and one rotate row, in the order the
    Immich web editor serializes them (crop, mirror, rotate) — which, under
    upstream's compose semantics, applies rotate first and mirror second,
    matching the recipe pipeline exactly. Row IDs are synthesized
    deterministically from the immutable asset/version identity plus action
    kind, so repeated reads return identical rows. That identity assumes a
    version's `params` are write-once: a changed recipe must arrive as a new
    version ID, or rows would reuse IDs while their parameters changed.
    """
    if not asset_id or not version_id:
        raise ValueError("asset_id and version_id must be non-empty")
    recipe = parse_recipe_params(params)

    rows: list[AssetEditActionItemResponseDto] = []
    if recipe.crop is not None:
        rows.append(
            AssetEditActionItemResponseDto(
                action=AssetEditAction.crop,
                id=_synthesized_row_id(asset_id, version_id, AssetEditAction.crop),
                parameters=CropParameters(
                    x=recipe.crop.x,
                    y=recipe.crop.y,
                    width=recipe.crop.width,
                    height=recipe.crop.height,
                ),
            )
        )
    if recipe.mirror:
        rows.append(
            AssetEditActionItemResponseDto(
                action=AssetEditAction.mirror,
                id=_synthesized_row_id(asset_id, version_id, AssetEditAction.mirror),
                parameters=MirrorParameters(axis=MirrorAxis.horizontal),
            )
        )
    if recipe.angle != 0:
        rows.append(
            AssetEditActionItemResponseDto(
                action=AssetEditAction.rotate,
                id=_synthesized_row_id(asset_id, version_id, AssetEditAction.rotate),
                parameters=RotateParameters(angle=float(recipe.angle)),
            )
        )
    return rows
