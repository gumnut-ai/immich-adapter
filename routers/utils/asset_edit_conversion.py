"""Convert Immich edit actions to and from the Gumnut v1 edit recipe.

The Gumnut API stores producer-defined `params` JSON on `edit` versions, so
this module owns that schema:

The v1 recipe JSON object::

    {
        "version": 1,
        "crop": {"x": 0, "y": 0, "width": 100, "height": 100},  # optional
        "angle": 0 | 90 | 180 | 270,
        "mirror": true | false
    }

Recipes apply crop to the display-oriented source frame, then rotation, then a
horizontal mirror. Immich actions compose in list order, with the last action
applied first; every valid rotate/mirror sequence reduces to one of eight
`(mirror, angle)` orientations.

The module has no SDK, framework, imaging, filesystem, or network dependencies.
Errors expose stable codes and never echo client values.
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

# Immich's Zod/OpenAPI integer fields use JavaScript's maximum safe integer.
# This is a wire-format ceiling, not a supported image dimension; crop bounds
# must still be checked against the actual source dimensions.
_IMMICH_MAX_INTEGER = 9_007_199_254_740_991

# Fixed, arbitrary domain separator for the asset/version/action SHA-256 key.
# It originated as this codec's UUID namespace, isolates these hashes from
# other deterministic IDs, and is now part of the stable row-ID contract:
# changing it changes every synthesized row ID.
_EDIT_ROW_ID_KEY_PREFIX = "9c1d1f2e-5a4b-4d3c-8e6f-2b7a9d0c4e51"

_RECIPE_KEYS = frozenset({"version", "crop", "angle", "mirror"})
_CROP_KEYS = frozenset({"x", "y", "width", "height"})
_VALID_ANGLES = frozenset({0, 90, 180, 270})


class AssetEditError(Exception):
    """Adapter-domain failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AssetEditValidationError(AssetEditError):
    """A client-supplied Immich edit list is invalid — maps to a 4xx."""


class UnsupportedEditRecipeError(AssetEditError):
    """A stored recipe cannot be represented as Immich edits."""


@dataclass(frozen=True)
class CropBox:
    """Crop in the display-oriented source frame."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class EditRecipe:
    """V1 edit recipe: crop, then rotate, then horizontal mirror."""

    crop: CropBox | None
    angle: int
    mirror: bool

    @property
    def is_identity(self) -> bool:
        """Whether the recipe performs no transformation."""
        return self.crop is None and self.angle == 0 and not self.mirror

    def to_params(self) -> dict[str, object]:
        """Serialize to `params`, omitting an absent crop."""
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
        """Serialize `params` in a byte-stable canonical form."""
        return json.dumps(self.to_params(), sort_keys=True, separators=(",", ":"))


def _require_positive_int(value: int, name: str) -> None:
    if type(value) is not int or value <= 0 or value > _IMMICH_MAX_INTEGER:
        raise ValueError(f"{name} must be a positive int within Immich's integer range")


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
    # Avoid converting arbitrarily large ints inside isfinite().
    if isinstance(angle, float) and not math.isfinite(angle):
        raise AssetEditValidationError("invalid_angle", "Rotation angle must be finite")
    # The recipe contract accepts any finite multiple of 90, deliberately wider
    # than Immich's literal input validator, and normalizes it.
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

    Source dimensions describe the parent version's display-oriented frame.
    Invalid source dimensions are a caller bug and raise
    `ValueError`; invalid client input raises `AssetEditValidationError`.

    The `(mirror, angle)` state means rotate, then mirror horizontally. Each
    action's matrix is multiplied on the right to preserve Immich list order.
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
    # Immich permits one mirror per axis, including both axes together.
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
        if type(field) is not int or field < 0 or field > _IMMICH_MAX_INTEGER:
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

    Version is checked first. Invalid or unsupported params fail as a whole.
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
    # Newlines prevent concatenation collisions. Hashing gives stable IDs;
    # stamping UUIDv4 bits satisfies Immich's validator. The prefix and key
    # encoding are pinned by golden tests. These IDs represent current state,
    # not user-action history.
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

    Rows use the editor's crop, mirror, rotate order. IDs are stable for an
    asset/version/action tuple, so changed params require a new version ID.
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
