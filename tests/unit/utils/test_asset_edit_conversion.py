"""Tests for routers/utils/asset_edit_conversion.py."""

import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from routers.immich_models import (
    AssetEditAction,
    AssetEditActionItemDto,
    AssetEditActionItemResponseDto,
    CropParameters,
    MirrorAxis,
    MirrorParameters,
    RotateParameters,
)
from routers.utils.asset_edit_conversion import (
    MAX_EDIT_ACTIONS,
    RECIPE_VERSION,
    AssetEditValidationError,
    CropBox,
    EditRecipe,
    UnsupportedEditRecipeError,
    immich_edits_to_recipe,
    parse_recipe_params,
    recipe_to_immich_edits,
)

SOURCE_W = 4000
SOURCE_H = 3000

ASSET_ID = "asset_01hzxyzexampleaaaaaaaaaaaa"
VERSION_ID = "asset_version_01hzxyzexamplebbbbbbbbbb"


def crop(x: int = 100, y: int = 200, width: int = 800, height: int = 600):
    return AssetEditActionItemDto(
        action=AssetEditAction.crop,
        parameters=CropParameters(x=x, y=y, width=width, height=height),
    )


def rotate(angle: float):
    return AssetEditActionItemDto(
        action=AssetEditAction.rotate, parameters=RotateParameters(angle=angle)
    )


def mirror(axis: MirrorAxis):
    return AssetEditActionItemDto(
        action=AssetEditAction.mirror, parameters=MirrorParameters(axis=axis)
    )


def to_recipe(edits, w: int = SOURCE_W, h: int = SOURCE_H) -> EditRecipe:
    return immich_edits_to_recipe(edits, w, h)


H = MirrorAxis.horizontal
V = MirrorAxis.vertical


class TestOrientationNormalization:
    EIGHT_STATES = [
        ([rotate(0)], 0, False),  # identity via explicit rotate 0
        ([rotate(90)], 90, False),
        ([rotate(180)], 180, False),
        ([rotate(270)], 270, False),
        ([mirror(H)], 0, True),
        ([mirror(H), rotate(90)], 90, True),
        ([mirror(V)], 180, True),  # vertical = horizontal mirror + 180
        ([mirror(V), rotate(90)], 270, True),
    ]

    @pytest.mark.parametrize("edits,angle,mirrored", EIGHT_STATES)
    def test_eight_states(self, edits, angle, mirrored):
        recipe = to_recipe(edits)
        assert recipe == EditRecipe(crop=None, angle=angle, mirror=mirrored)

    def test_identity_flag(self):
        assert to_recipe([rotate(0)]).is_identity
        assert not to_recipe([rotate(90)]).is_identity
        assert not to_recipe([mirror(H)]).is_identity
        assert not to_recipe([crop()]).is_identity

    @pytest.mark.parametrize(
        "equivalent,canonical",
        [
            # mirror vertical == mirror horizontal then rotate 180
            ([mirror(V)], [mirror(H), rotate(180)]),
            # both mirrors compose to a plain 180 rotation
            ([mirror(H), mirror(V)], [rotate(180)]),
            ([mirror(V), mirror(H)], [rotate(180)]),
            # full canonical web list: crop, mirrorH, mirrorV, rotate
            (
                [crop(), mirror(H), mirror(V), rotate(90)],
                [crop(), rotate(270)],
            ),
        ],
    )
    def test_equivalent_sequences_normalize_identically(self, equivalent, canonical):
        assert to_recipe(equivalent) == to_recipe(canonical)

    def test_list_order_is_semantically_honored(self):
        assert to_recipe([mirror(H), rotate(90)]) == EditRecipe(
            crop=None, angle=90, mirror=True
        )
        assert to_recipe([rotate(90), mirror(H)]) == EditRecipe(
            crop=None, angle=270, mirror=True
        )


class TestRotationNormalization:
    @pytest.mark.parametrize(
        "angle,expected",
        [
            (0, 0),
            (90, 90),
            (180, 180),
            (270, 270),
            (90.0, 90),
            (-90, 270),
            (-180, 180),
            (-270, 90),
            (360, 0),
            (450, 90),
            (-450, 270),
            (720, 0),
        ],
    )
    def test_normalizes_modulo_360(self, angle, expected):
        assert to_recipe([rotate(angle)]).angle == expected

    @pytest.mark.parametrize(
        "angle",
        [45, 91, -1, 90.5, 89.999999, float("nan"), float("inf"), float("-inf")],
    )
    def test_rejects_invalid_angles(self, angle):
        with pytest.raises(AssetEditValidationError) as exc_info:
            to_recipe([rotate(angle)])
        assert exc_info.value.code == "invalid_angle"

    @pytest.mark.parametrize("bad_angle", [True, "90", 10**400])
    def test_rejects_smuggled_angle_values(self, bad_angle):
        # Bypass Pydantic to exercise the codec boundary.
        params = RotateParameters.model_construct(angle=bad_angle)
        edit = AssetEditActionItemDto.model_construct(
            action=AssetEditAction.rotate, parameters=params
        )
        with pytest.raises(AssetEditValidationError) as exc_info:
            to_recipe([edit])
        assert exc_info.value.code == "invalid_angle"


class TestCropValidation:
    def test_crop_only_preserves_exact_bounds(self):
        recipe = to_recipe([crop(x=10, y=20, width=300, height=400)])
        assert recipe.crop == CropBox(x=10, y=20, width=300, height=400)
        assert recipe.angle == 0 and recipe.mirror is False

    @pytest.mark.parametrize(
        "edits,angle,mirrored", TestOrientationNormalization.EIGHT_STATES
    )
    def test_crop_plus_each_orientation_preserves_bounds(self, edits, angle, mirrored):
        recipe = to_recipe([crop(x=5, y=6, width=70, height=80), *edits])
        assert recipe.crop == CropBox(x=5, y=6, width=70, height=80)
        assert recipe.angle == angle and recipe.mirror is mirrored

    def test_full_frame_crop_is_preserved(self):
        recipe = to_recipe([crop(x=0, y=0, width=SOURCE_W, height=SOURCE_H)])
        assert recipe.crop == CropBox(x=0, y=0, width=SOURCE_W, height=SOURCE_H)

    def test_crop_position_in_list_does_not_change_meaning(self):
        first = to_recipe([crop(), mirror(H), rotate(90)])
        last = to_recipe([mirror(H), rotate(90), crop()])
        assert first == last

    @pytest.mark.parametrize(
        "x,y,width,height,code",
        [
            (0, 0, SOURCE_W + 1, SOURCE_H, "crop_out_of_bounds"),
            (0, 0, SOURCE_W, SOURCE_H + 1, "crop_out_of_bounds"),
            (1, 0, SOURCE_W, SOURCE_H, "crop_out_of_bounds"),
            (0, 1, SOURCE_W, SOURCE_H, "crop_out_of_bounds"),
            (SOURCE_W, SOURCE_H, 1, 1, "crop_out_of_bounds"),
            (0, 0, 0, 100, "invalid_crop"),
            (0, 0, 100, 0, "invalid_crop"),
            (-1, 0, 100, 100, "invalid_crop"),
            (0, -1, 100, 100, "invalid_crop"),
            (0, 0, -5, 100, "invalid_crop"),
        ],
    )
    def test_rejects_bad_crops(self, x, y, width, height, code):
        # Bypass Pydantic to exercise the codec boundary.
        params = CropParameters.model_construct(x=x, y=y, width=width, height=height)
        edit = AssetEditActionItemDto.model_construct(
            action=AssetEditAction.crop, parameters=params
        )
        with pytest.raises(AssetEditValidationError) as exc_info:
            to_recipe([edit])
        assert exc_info.value.code == code

    @pytest.mark.parametrize("bad_value", [0.5, True])
    def test_rejects_non_integer_crop_values(self, bad_value):
        params = CropParameters.model_construct(x=bad_value, y=0, width=100, height=100)
        edit = AssetEditActionItemDto.model_construct(
            action=AssetEditAction.crop, parameters=params
        )
        with pytest.raises(AssetEditValidationError) as exc_info:
            to_recipe([edit])
        assert exc_info.value.code == "invalid_crop"

    def test_fold_accepts_source_dims_at_exact_upper_bound(self):
        bound = 2**53 - 1
        recipe = immich_edits_to_recipe(
            [crop(x=0, y=0, width=bound, height=1)], bound, 1
        )
        assert recipe.crop == CropBox(x=0, y=0, width=bound, height=1)

    @pytest.mark.parametrize(
        "bad_dim",
        [
            (0, SOURCE_H),
            (SOURCE_W, 0),
            (-1, SOURCE_H),
            (2**53, SOURCE_H),
            (4000.0, SOURCE_H),
            (True, SOURCE_H),
        ],
    )
    def test_invalid_source_dims_are_a_caller_bug(self, bad_dim):
        with pytest.raises(ValueError):
            immich_edits_to_recipe([crop()], *bad_dim)


class TestRejections:
    def test_rejects_empty_list(self):
        with pytest.raises(AssetEditValidationError) as exc_info:
            to_recipe([])
        assert exc_info.value.code == "empty_edit_list"

    def test_rejects_oversized_list(self):
        edits = [crop(), mirror(H), mirror(V), rotate(90), rotate(180)]
        assert len(edits) > MAX_EDIT_ACTIONS
        with pytest.raises(AssetEditValidationError) as exc_info:
            to_recipe(edits)
        assert exc_info.value.code == "too_many_actions"

    @pytest.mark.parametrize(
        "edits",
        [
            [crop(), crop()],
            [rotate(90), rotate(90)],
            [rotate(90), rotate(180)],
            [mirror(H), mirror(H)],
            [mirror(V), mirror(V)],
            [crop(x=0, y=0, width=1, height=1), crop(x=1, y=1, width=2, height=2)],
        ],
    )
    def test_rejects_duplicate_actions(self, edits):
        with pytest.raises(AssetEditValidationError) as exc_info:
            to_recipe(edits)
        assert exc_info.value.code == "duplicate_action"

    def test_both_mirror_axes_together_are_legal(self):
        assert to_recipe([mirror(H), mirror(V)]) == EditRecipe(
            crop=None, angle=180, mirror=False
        )

    @pytest.mark.parametrize(
        "action,parameters",
        [
            (AssetEditAction.crop, RotateParameters(angle=90)),
            (AssetEditAction.crop, MirrorParameters(axis=H)),
            (AssetEditAction.rotate, CropParameters(x=0, y=0, width=1, height=1)),
            (AssetEditAction.rotate, MirrorParameters(axis=H)),
            (AssetEditAction.mirror, RotateParameters(angle=90)),
            (AssetEditAction.mirror, CropParameters(x=0, y=0, width=1, height=1)),
        ],
    )
    def test_rejects_action_parameter_mismatch(self, action, parameters):
        # The generated union is undiscriminated, so a mismatched pairing is
        # representable and must be rejected by the codec itself.
        edit = AssetEditActionItemDto.model_construct(
            action=action, parameters=parameters
        )
        with pytest.raises(AssetEditValidationError) as exc_info:
            to_recipe([edit])
        assert exc_info.value.code == "mismatched_parameters"

    def test_rejects_unknown_action(self):
        edit = AssetEditActionItemDto.model_construct(
            action="upscale", parameters=RotateParameters(angle=90)
        )
        with pytest.raises(AssetEditValidationError) as exc_info:
            to_recipe([edit])
        assert exc_info.value.code == "unsupported_action"

    def test_rejects_unknown_mirror_axis(self):
        params = MirrorParameters.model_construct(axis="diagonal")
        edit = AssetEditActionItemDto.model_construct(
            action=AssetEditAction.mirror, parameters=params
        )
        with pytest.raises(AssetEditValidationError) as exc_info:
            to_recipe([edit])
        assert exc_info.value.code == "invalid_mirror_axis"

    def test_errors_do_not_echo_client_values(self):
        with pytest.raises(AssetEditValidationError) as exc_info:
            to_recipe([rotate(123456789.5)])
        assert "123456789" not in str(exc_info.value)
        with pytest.raises(AssetEditValidationError) as exc_info:
            to_recipe([crop(x=987654, y=0, width=SOURCE_W, height=1)])
        assert "987654" not in str(exc_info.value)


class TestRecipeParams:
    def test_serialization_shape(self):
        recipe = EditRecipe(
            crop=CropBox(x=1, y=2, width=3, height=4), angle=90, mirror=True
        )
        assert recipe.to_params() == {
            "version": RECIPE_VERSION,
            "crop": {"x": 1, "y": 2, "width": 3, "height": 4},
            "angle": 90,
            "mirror": True,
        }
        assert EditRecipe(crop=None, angle=0, mirror=False).to_params() == {
            "version": RECIPE_VERSION,
            "angle": 0,
            "mirror": False,
        }

    def test_canonical_json_is_stable(self):
        recipe = EditRecipe(
            crop=CropBox(x=1, y=2, width=3, height=4), angle=270, mirror=False
        )
        expected = (
            '{"angle":270,"crop":{"height":4,"width":3,"x":1,"y":2},'
            '"mirror":false,"version":1}'
        )
        assert recipe.to_params_json() == expected

    @pytest.mark.parametrize(
        "recipe",
        [
            EditRecipe(crop=None, angle=0, mirror=False),
            EditRecipe(crop=None, angle=90, mirror=True),
            EditRecipe(
                crop=CropBox(x=0, y=0, width=1, height=1), angle=180, mirror=False
            ),
            EditRecipe(
                crop=CropBox(x=7, y=9, width=11, height=13), angle=270, mirror=True
            ),
        ],
    )
    def test_parse_serialize_idempotent(self, recipe):
        params = recipe.to_params()
        assert parse_recipe_params(params) == recipe
        assert parse_recipe_params(params).to_params() == params

    def test_explicit_null_crop_is_treated_as_absent(self):
        params = {"version": 1, "crop": None, "angle": 0, "mirror": False}
        assert parse_recipe_params(params).crop is None

    def test_accepts_crop_values_at_exact_upper_bound(self):
        bound = 2**53 - 1
        params = {
            "version": 1,
            "crop": {"x": 0, "y": 0, "width": bound, "height": bound},
            "angle": 0,
            "mirror": False,
        }
        recipe = parse_recipe_params(params)
        assert recipe.crop == CropBox(x=0, y=0, width=bound, height=bound)
        rows = recipe_to_immich_edits(ASSET_ID, VERSION_ID, params)
        assert isinstance(rows[0].parameters, CropParameters)
        assert rows[0].parameters.width == bound

    @pytest.mark.parametrize(
        "params,code",
        [
            (None, "malformed_recipe"),
            ("not a dict", "malformed_recipe"),
            ([], "malformed_recipe"),
            ({}, "malformed_recipe"),
            ({"angle": 0, "mirror": False}, "malformed_recipe"),
            ({"version": "1", "angle": 0, "mirror": False}, "malformed_recipe"),
            ({"version": True, "angle": 0, "mirror": False}, "malformed_recipe"),
            ({"version": 2, "angle": 0, "mirror": False}, "unsupported_recipe_version"),
            ({"version": 0, "angle": 0, "mirror": False}, "unsupported_recipe_version"),
            # Unsupported version takes precedence over malformed fields.
            (
                {"version": 2, "angle": 45, "mirror": "no", "exposure": 0.5},
                "unsupported_recipe_version",
            ),
            (
                {"version": 1, "angle": 0, "mirror": False, "exposure": 0.5},
                "unsupported_recipe_field",
            ),
            ({"version": 1, "mirror": False}, "malformed_recipe"),
            ({"version": 1, "angle": 45, "mirror": False}, "malformed_recipe"),
            ({"version": 1, "angle": True, "mirror": False}, "malformed_recipe"),
            ({"version": 1, "angle": 90.0, "mirror": False}, "malformed_recipe"),
            ({"version": 1, "angle": "90", "mirror": False}, "malformed_recipe"),
            ({"version": 1, "angle": 0}, "malformed_recipe"),
            ({"version": 1, "angle": 0, "mirror": "no"}, "malformed_recipe"),
            ({"version": 1, "angle": 0, "mirror": 0}, "malformed_recipe"),
            (
                {"version": 1, "angle": 0, "mirror": False, "crop": []},
                "malformed_recipe",
            ),
            (
                {"version": 1, "angle": 0, "mirror": False, "crop": {"x": 0}},
                "malformed_recipe",
            ),
            (
                {
                    "version": 1,
                    "angle": 0,
                    "mirror": False,
                    "crop": {"x": 0, "y": 0, "width": 0, "height": 1},
                },
                "malformed_recipe",
            ),
            (
                {
                    "version": 1,
                    "angle": 0,
                    "mirror": False,
                    "crop": {"x": -1, "y": 0, "width": 1, "height": 1},
                },
                "malformed_recipe",
            ),
            (
                {
                    "version": 1,
                    "angle": 0,
                    "mirror": False,
                    "crop": {"x": True, "y": 0, "width": 1, "height": 1},
                },
                "malformed_recipe",
            ),
            (
                {
                    "version": 1,
                    "angle": 0,
                    "mirror": False,
                    "crop": {"x": 0, "y": 0, "width": 2**53, "height": 1},
                },
                "malformed_recipe",
            ),
            (
                {
                    "version": 1,
                    "angle": 0,
                    "mirror": False,
                    "crop": {"x": 0, "y": 0, "width": 1, "height": 1, "unit": "px"},
                },
                "malformed_recipe",
            ),
        ],
    )
    def test_rejects_unsupported_or_malformed_params(self, params, code):
        with pytest.raises(UnsupportedEditRecipeError) as exc_info:
            parse_recipe_params(params)
        assert exc_info.value.code == code


class TestReverseTranslation:
    def test_canonical_order_and_shapes(self):
        params = EditRecipe(
            crop=CropBox(x=1, y=2, width=30, height=40), angle=90, mirror=True
        ).to_params()
        rows = recipe_to_immich_edits(ASSET_ID, VERSION_ID, params)
        assert [row.action for row in rows] == [
            AssetEditAction.crop,
            AssetEditAction.mirror,
            AssetEditAction.rotate,
        ]
        assert isinstance(rows[0].parameters, CropParameters)
        assert rows[0].parameters.x == 1 and rows[0].parameters.height == 40
        assert isinstance(rows[1].parameters, MirrorParameters)
        assert rows[1].parameters.axis == MirrorAxis.horizontal
        assert isinstance(rows[2].parameters, RotateParameters)
        assert rows[2].parameters.angle == 90.0
        assert all(isinstance(row, AssetEditActionItemResponseDto) for row in rows)

    def test_identity_recipe_yields_no_rows(self):
        params = EditRecipe(crop=None, angle=0, mirror=False).to_params()
        assert recipe_to_immich_edits(ASSET_ID, VERSION_ID, params) == []

    def test_omits_absent_operations(self):
        params = EditRecipe(crop=None, angle=180, mirror=False).to_params()
        rows = recipe_to_immich_edits(ASSET_ID, VERSION_ID, params)
        assert [row.action for row in rows] == [AssetEditAction.rotate]

    @pytest.mark.parametrize(
        "recipe",
        [
            EditRecipe(crop=None, angle=angle, mirror=mirrored)
            for angle in (0, 90, 180, 270)
            for mirrored in (False, True)
        ]
        + [
            EditRecipe(
                crop=CropBox(x=10, y=20, width=100, height=200),
                angle=angle,
                mirror=mirrored,
            )
            for angle in (0, 90, 180, 270)
            for mirrored in (False, True)
        ],
    )
    def test_round_trip_every_supported_recipe(self, recipe):
        rows = recipe_to_immich_edits(ASSET_ID, VERSION_ID, recipe.to_params())
        if recipe.is_identity:
            assert rows == []
            return
        assert immich_edits_to_recipe(rows, SOURCE_W, SOURCE_H) == recipe

    def test_unsupported_recipe_raises_not_partial_state(self):
        with pytest.raises(UnsupportedEditRecipeError):
            recipe_to_immich_edits(ASSET_ID, VERSION_ID, {"version": 2})

    def test_empty_identity_is_a_caller_bug(self):
        params = EditRecipe(crop=None, angle=90, mirror=False).to_params()
        with pytest.raises(ValueError):
            recipe_to_immich_edits("", VERSION_ID, params)
        with pytest.raises(ValueError):
            recipe_to_immich_edits(ASSET_ID, "", params)


class TestSynthesizedRowIds:
    PARAMS = EditRecipe(
        crop=CropBox(x=1, y=2, width=30, height=40), angle=90, mirror=True
    ).to_params()

    def rows(self, asset_id: str = ASSET_ID, version_id: str = VERSION_ID):
        return recipe_to_immich_edits(asset_id, version_id, self.PARAMS)

    def test_ids_are_stable_across_repeated_reads(self):
        first = self.rows()
        second = self.rows()
        assert [row.id for row in first] == [row.id for row in second]

    def test_ids_are_valid_uuids(self):
        for row in self.rows():
            assert isinstance(row.id, uuid.UUID)

    def test_ids_are_uuid_version_4(self):
        for row in self.rows():
            assert row.id.version == 4
            assert row.id.variant == uuid.RFC_4122

    def test_ids_match_golden_values(self):
        golden = {
            AssetEditAction.crop: uuid.UUID("2265abeb-0285-4476-93c0-f245091c6255"),
            AssetEditAction.mirror: uuid.UUID("d8c22012-6ed5-406e-9cef-2e78ddb5bf01"),
            AssetEditAction.rotate: uuid.UUID("b1e27e4d-257c-4200-965f-a3be45979fcb"),
        }
        assert {row.action: row.id for row in self.rows()} == golden

    def test_ids_differ_by_action(self):
        ids = [row.id for row in self.rows()]
        assert len(set(ids)) == len(ids)

    def test_ids_differ_by_version_and_asset(self):
        base = {row.action: row.id for row in self.rows()}
        other_version = {
            row.action: row.id for row in self.rows(version_id="asset_version_other")
        }
        other_asset = {row.action: row.id for row in self.rows(asset_id="asset_other")}
        for action, row_id in base.items():
            assert other_version[action] != row_id
            assert other_asset[action] != row_id


class TestPurity:
    def test_module_pulls_in_no_network_or_pixel_io_dependencies(self):
        repo_root = Path(__file__).resolve().parents[3]
        code = (
            "import sys\n"
            "import routers.utils.asset_edit_conversion\n"
            "forbidden = {'gumnut', 'fastapi', 'starlette', 'httpx', 'PIL',\n"
            "             'aiohttp', 'requests', 'anyio', 'urllib3'}\n"
            "loaded = {name.split('.')[0] for name in sys.modules}\n"
            "bad = sorted(loaded & forbidden)\n"
            "assert not bad, f'forbidden imports: {bad}'\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
