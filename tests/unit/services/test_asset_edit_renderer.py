"""Asset edit renderer tests using independent pixel-grid reference transforms."""

import asyncio
import io
import logging
import tempfile
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image, ImageOps

import services.asset_edit_renderer as renderer_module
from config.settings import Settings, get_settings
from routers.utils.asset_edit_conversion import CropBox, EditRecipe
from services.asset_edit_renderer import (
    RenderdEdit,
    EditRenderError,
    EditRenderInputError,
    EditRenderLimitError,
    EditRenderSourceError,
    EditRenderTimeoutError,
    render_asset_edit,
)

pytestmark = pytest.mark.anyio

ASSET_ID = "asset_editrendertest"
ORIGINAL_URL = "https://cdn.example.test/signed/original"

_EXIF_ORIENTATION_TAG = 0x0112

HEIC_FIXTURE_PATH = (
    Path(__file__).parents[2] / "fixtures" / "livephoto" / "IMG_1309.HEIC"
)

Pixel = float | tuple[int, ...] | None
Grid = list[list[Pixel]]


def rotate_cw(grid: Grid) -> Grid:
    h, w = len(grid), len(grid[0])
    return [[grid[h - 1 - x][y] for x in range(h)] for y in range(w)]


def mirror_h(grid: Grid) -> Grid:
    return [row[::-1] for row in grid]


def apply_recipe_reference(grid: Grid, recipe: EditRecipe) -> Grid:
    """Reference implementation of crop -> clockwise rotate -> mirror."""
    if recipe.crop is not None:
        c = recipe.crop
        grid = [row[c.x : c.x + c.width] for row in grid[c.y : c.y + c.height]]
    for _ in range(recipe.angle // 90):
        grid = rotate_cw(grid)
    if recipe.mirror:
        grid = mirror_h(grid)
    return grid


def grid_of(image: Image.Image) -> Grid:
    return [
        [image.getpixel((x, y)) for x in range(image.width)]
        for y in range(image.height)
    ]


def assert_grids_close(actual: Grid, expected: Grid, tolerance: int) -> None:
    """Per-channel tolerance comparison for lossy (JPEG) output grids."""
    assert len(actual) == len(expected)
    for actual_row, expected_row in zip(actual, expected):
        assert len(actual_row) == len(expected_row)
        for actual_pixel, expected_pixel in zip(actual_row, expected_row):
            assert isinstance(actual_pixel, tuple)
            assert isinstance(expected_pixel, tuple)
            assert all(
                abs(a - e) <= tolerance for a, e in zip(actual_pixel, expected_pixel)
            )


def make_rgba_image(width: int, height: int, alpha: int = 255) -> Image.Image:
    image = Image.new("RGBA", (width, height))
    for y in range(height):
        for x in range(width):
            image.putpixel((x, y), (10 + x * 30, 10 + y * 30, 200, alpha))
    return image


def encode_image(
    image: Image.Image, image_format: str, orientation: int | None = None
) -> bytes:
    buffer = io.BytesIO()
    save_kwargs: dict[str, Any] = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[_EXIF_ORIENTATION_TAG] = orientation
        save_kwargs["exif"] = exif
    image.save(buffer, format=image_format, **save_kwargs)
    return buffer.getvalue()


def rgba_png_bytes(width: int = 4, height: int = 2) -> bytes:
    return encode_image(make_rgba_image(width, height), "PNG")


def rgb_jpeg_bytes(width: int = 4, height: int = 2) -> bytes:
    return encode_image(make_rgba_image(width, height).convert("RGB"), "JPEG")


class FakeCdnResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = (
            headers if headers is not None else {"content-length": str(len(body))}
        )
        self.closed = False
        self.body_read = False

    async def aiter_bytes(self, chunk_size: int) -> AsyncIterator[bytes]:
        self.body_read = True
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]

    async def aclose(self) -> None:
        self.closed = True


def make_version(
    position: int = 0,
    mime_type: str = "image/jpeg",
    url: str | None = ORIGINAL_URL,
    version_id: str = "asset_version_root",
    kind: str | None = None,
) -> SimpleNamespace:
    if kind is None:
        kind = "original" if position == 0 else "edit"
    version_urls = {}
    if url is not None:
        version_urls["original"] = SimpleNamespace(url=url, mimetype=mime_type)
    version_urls["thumbnail"] = SimpleNamespace(
        url="https://cdn.example.test/signed/thumbnail", mimetype="image/jpeg"
    )
    return SimpleNamespace(
        id=version_id,
        position=position,
        kind=kind,
        mime_type=mime_type,
        version_urls=version_urls,
    )


def make_client(versions: list[SimpleNamespace]) -> Mock:
    client = Mock()
    client.assets.versions.list = AsyncMock(return_value=versions)
    return client


IDENTITY = EditRecipe(crop=None, angle=0, mirror=False)


@pytest.fixture(autouse=True)
def reset_render_globals() -> Iterator[None]:
    renderer_module._render_executor = None
    renderer_module._render_admission = None
    yield
    if renderer_module._render_executor is not None:
        renderer_module._render_executor.shutdown(wait=False)
    renderer_module._render_executor = None
    renderer_module._render_admission = None


@pytest.fixture
def render_settings() -> Settings:
    return get_settings().model_copy()


async def render(
    source_bytes: bytes,
    recipe: EditRecipe,
    settings: Settings,
    *,
    versions: list[SimpleNamespace] | None = None,
    cdn_response: FakeCdnResponse | None = None,
) -> tuple[RenderdEdit, bytes, Mock, AsyncMock]:
    """Run one render and return (metadata copy, output bytes, client, cdn mock)."""
    if versions is None:
        versions = [make_version()]
    client = make_client(versions)
    response = cdn_response or FakeCdnResponse(source_bytes)
    open_mock = AsyncMock(return_value=response)
    with (
        patch.object(renderer_module, "open_cdn_response", open_mock),
        patch.object(renderer_module, "get_settings", return_value=settings),
    ):
        async with render_asset_edit(client, ASSET_ID, recipe) as renderd:
            output_bytes = renderd.body.read()
            metadata = RenderdEdit(
                body=renderd.body,
                mime_type=renderd.mime_type,
                width=renderd.width,
                height=renderd.height,
                size_bytes=renderd.size_bytes,
            )
    return metadata, output_bytes, client, open_mock


def make_blocking_render_sync(
    release: threading.Event, entered: list[str]
) -> Callable[[IO[bytes], EditRecipe, Settings], RenderdEdit]:
    """Worker stub that records its thread name, then blocks until ``release``."""

    def blocking_render_sync(
        input_file: IO[bytes], recipe: EditRecipe, settings_arg: Settings
    ) -> RenderdEdit:
        input_file.close()
        entered.append(threading.current_thread().name)
        release.wait(timeout=10)
        return RenderdEdit(
            body=Mock(), mime_type="image/jpeg", width=1, height=1, size_bytes=1
        )

    return blocking_render_sync


async def run_stub_render(*, patch_cdn: bool = True) -> None:
    """One identity render against a fresh mock client (for concurrency tests).

    With ``patch_cdn`` (the default) the CDN opener is patched per call with
    a fresh response; pass ``False`` when the test already patched it — e.g.
    with a shared mock that counts opens across renders.
    """
    client = make_client([make_version()])
    if patch_cdn:
        response = FakeCdnResponse(rgb_jpeg_bytes())
        with patch.object(
            renderer_module, "open_cdn_response", AsyncMock(return_value=response)
        ):
            async with render_asset_edit(client, ASSET_ID, IDENTITY):
                pass
    else:
        async with render_asset_edit(client, ASSET_ID, IDENTITY):
            pass


class TestGoldenTransforms:
    @pytest.mark.parametrize(
        "recipe",
        [
            EditRecipe(
                crop=CropBox(x=1, y=1, width=3, height=2), angle=0, mirror=False
            ),
            EditRecipe(crop=None, angle=90, mirror=False),
            EditRecipe(crop=None, angle=180, mirror=False),
            EditRecipe(crop=None, angle=270, mirror=False),
            EditRecipe(crop=None, angle=0, mirror=True),
            EditRecipe(crop=None, angle=180, mirror=True),
        ],
    )
    async def test_single_operations(
        self, render_settings: Settings, recipe: EditRecipe
    ) -> None:
        source = make_rgba_image(6, 4)
        metadata, output_bytes, _, _ = await render(
            encode_image(source, "PNG"), recipe, render_settings
        )
        expected = apply_recipe_reference(grid_of(source), recipe)
        renderd_image = Image.open(io.BytesIO(output_bytes))
        assert metadata.mime_type == "image/png"
        assert grid_of(renderd_image) == expected
        assert (metadata.width, metadata.height) == (
            len(expected[0]),
            len(expected),
        )

    @pytest.mark.parametrize("angle", [0, 90, 180, 270])
    @pytest.mark.parametrize("mirror", [False, True])
    async def test_all_eight_composed_orientation_states(
        self, render_settings: Settings, angle: int, mirror: bool
    ) -> None:
        source = make_rgba_image(4, 2)
        recipe = EditRecipe(crop=None, angle=angle, mirror=mirror)
        _, output_bytes, _, _ = await render(
            encode_image(source, "PNG"), recipe, render_settings
        )
        expected = apply_recipe_reference(grid_of(source), recipe)
        assert grid_of(Image.open(io.BytesIO(output_bytes))) == expected

    async def test_crop_then_rotate_pipeline_order(
        self, render_settings: Settings
    ) -> None:
        """Edge pixels prove crop happens in the pre-rotation frame."""
        source = make_rgba_image(6, 4)
        recipe = EditRecipe(
            crop=CropBox(x=0, y=0, width=3, height=2), angle=90, mirror=False
        )
        _, output_bytes, _, _ = await render(
            encode_image(source, "PNG"), recipe, render_settings
        )
        expected = rotate_cw([row[0:3] for row in grid_of(source)[0:2]])
        renderd_grid = grid_of(Image.open(io.BytesIO(output_bytes)))
        assert renderd_grid == expected
        assert renderd_grid[0][0] == source.getpixel((0, 1))

    async def test_crop_applies_to_display_oriented_frame(
        self, render_settings: Settings
    ) -> None:
        """With EXIF orientation 6, crop coordinates address display space."""
        stored = make_rgba_image(4, 6)
        source_bytes = encode_image(stored, "PNG", orientation=6)
        # Orientation 6 displays the stored image rotated 90 degrees CW.
        display_grid = rotate_cw(grid_of(stored))
        recipe = EditRecipe(
            crop=CropBox(x=1, y=0, width=2, height=3), angle=0, mirror=False
        )
        _, output_bytes, _, _ = await render(source_bytes, recipe, render_settings)
        expected = [row[1:3] for row in display_grid[0:3]]
        assert grid_of(Image.open(io.BytesIO(output_bytes))) == expected


class TestExifOrientation:
    @pytest.mark.parametrize("orientation", [2, 3, 4, 5, 6, 7, 8])
    async def test_orientation_renderd_exactly_once(
        self, render_settings: Settings, orientation: int
    ) -> None:
        stored = make_rgba_image(4, 2)
        source_bytes = encode_image(stored, "PNG", orientation=orientation)
        metadata, output_bytes, _, _ = await render(
            source_bytes, IDENTITY, render_settings
        )

        reference = ImageOps.exif_transpose(Image.open(io.BytesIO(source_bytes)))
        assert reference is not None
        renderd_image = Image.open(io.BytesIO(output_bytes))
        assert grid_of(renderd_image) == grid_of(reference)
        assert (metadata.width, metadata.height) == reference.size
        assert renderd_image.getexif().get(_EXIF_ORIENTATION_TAG) is None

    async def test_output_has_no_orientation_tag_for_jpeg(
        self, render_settings: Settings
    ) -> None:
        stored = make_rgba_image(4, 2).convert("RGB")
        source_bytes = encode_image(stored, "JPEG", orientation=6)
        metadata, output_bytes, _, _ = await render(
            source_bytes, IDENTITY, render_settings
        )
        renderd_image = Image.open(io.BytesIO(output_bytes))
        assert metadata.mime_type == "image/jpeg"
        assert (metadata.width, metadata.height) == (2, 4)
        assert renderd_image.getexif().get(_EXIF_ORIENTATION_TAG) is None


class TestFormatSelection:
    async def test_jpeg_input_encodes_jpeg(self, render_settings: Settings) -> None:
        metadata, output_bytes, _, _ = await render(
            rgb_jpeg_bytes(), IDENTITY, render_settings
        )
        assert metadata.mime_type == "image/jpeg"
        assert Image.open(io.BytesIO(output_bytes)).format == "JPEG"

    async def test_opaque_png_input_encodes_jpeg(
        self, render_settings: Settings
    ) -> None:
        source_bytes = encode_image(make_rgba_image(4, 2).convert("RGB"), "PNG")
        metadata, output_bytes, _, _ = await render(
            source_bytes, IDENTITY, render_settings
        )
        assert metadata.mime_type == "image/jpeg"
        assert Image.open(io.BytesIO(output_bytes)).format == "JPEG"

    async def test_transparent_png_survives_as_png(
        self, render_settings: Settings
    ) -> None:
        source = make_rgba_image(4, 2, alpha=128)
        metadata, output_bytes, _, _ = await render(
            encode_image(source, "PNG"), IDENTITY, render_settings
        )
        renderd_image = Image.open(io.BytesIO(output_bytes))
        assert metadata.mime_type == "image/png"
        assert renderd_image.format == "PNG"
        corner = renderd_image.getpixel((0, 0))
        assert isinstance(corner, tuple)
        assert corner[3] == 128

    async def test_palette_transparency_survives_as_png(
        self, render_settings: Settings
    ) -> None:
        source = make_rgba_image(4, 2, alpha=0).quantize(colors=4)
        source_bytes = encode_image(source, "PNG")
        assert "transparency" in Image.open(io.BytesIO(source_bytes)).info
        metadata, _, _, _ = await render(source_bytes, IDENTITY, render_settings)
        assert metadata.mime_type == "image/png"

    async def test_grayscale_jpeg_preserves_l_mode(
        self, render_settings: Settings
    ) -> None:
        source = Image.new("L", (4, 2))
        for y in range(2):
            for x in range(4):
                source.putpixel((x, y), 20 + x * 30 + y * 40)
        source_bytes = encode_image(source, "JPEG")
        metadata, output_bytes, _, _ = await render(
            source_bytes, IDENTITY, render_settings
        )
        renderd_image = Image.open(io.BytesIO(output_bytes))
        assert metadata.mime_type == "image/jpeg"
        assert renderd_image.format == "JPEG"
        assert renderd_image.mode == "L"

    async def test_grayscale_alpha_png_preserves_la_mode(
        self, render_settings: Settings
    ) -> None:
        source = Image.new("LA", (4, 2), (100, 128))
        source_bytes = encode_image(source, "PNG")
        metadata, output_bytes, _, _ = await render(
            source_bytes, IDENTITY, render_settings
        )
        renderd_image = Image.open(io.BytesIO(output_bytes))
        assert metadata.mime_type == "image/png"
        assert renderd_image.format == "PNG"
        assert renderd_image.mode == "LA"
        pixel = renderd_image.getpixel((0, 0))
        assert isinstance(pixel, tuple)
        assert pixel[1] == 128

    async def test_deterministic_output(self, render_settings: Settings) -> None:
        recipe = EditRecipe(
            crop=CropBox(x=0, y=0, width=3, height=2), angle=90, mirror=True
        )
        source_bytes = rgb_jpeg_bytes(6, 4)
        _, first, _, _ = await render(source_bytes, recipe, render_settings)
        _, second, _, _ = await render(source_bytes, recipe, render_settings)
        assert first == second


class TestHeicSources:
    """Real-file HEIC coverage against a display-space reference decode."""

    async def test_real_iphone_heic_identity_render(
        self, render_settings: Settings
    ) -> None:
        source_bytes = HEIC_FIXTURE_PATH.read_bytes()
        metadata, output_bytes, _, _ = await render(
            source_bytes,
            IDENTITY,
            render_settings,
            versions=[make_version(mime_type="image/heic")],
        )
        reference = ImageOps.exif_transpose(Image.open(io.BytesIO(source_bytes)))
        assert reference is not None
        renderd_image = Image.open(io.BytesIO(output_bytes))
        assert metadata.mime_type == "image/jpeg"
        assert renderd_image.format == "JPEG"
        assert (metadata.width, metadata.height) == reference.size
        assert renderd_image.getexif().get(_EXIF_ORIENTATION_TAG) is None

    async def test_real_iphone_heic_crop_rotate_mirror(
        self, render_settings: Settings
    ) -> None:
        source_bytes = HEIC_FIXTURE_PATH.read_bytes()
        crop = CropBox(x=1500, y=2000, width=8, height=6)
        recipe = EditRecipe(crop=crop, angle=90, mirror=True)
        metadata, output_bytes, _, _ = await render(
            source_bytes,
            recipe,
            render_settings,
            versions=[make_version(mime_type="image/heic")],
        )
        assert (metadata.width, metadata.height) == (6, 8)
        reference = ImageOps.exif_transpose(Image.open(io.BytesIO(source_bytes)))
        assert reference is not None
        region: Grid = [
            [reference.getpixel((crop.x + dx, crop.y + dy)) for dx in range(crop.width)]
            for dy in range(crop.height)
        ]
        # The crop region was chosen for pixel structure so a wrong rotation
        # or mirror cannot pass within the JPEG tolerance below.
        assert len({pixel for row in region for pixel in row}) > 8
        expected = mirror_h(rotate_cw(region))
        renderd_grid = grid_of(Image.open(io.BytesIO(output_bytes)))
        assert_grids_close(renderd_grid, expected, tolerance=16)


class TestSourceAcquisition:
    async def test_lists_versions_once_and_fetches_root_original(
        self, render_settings: Settings
    ) -> None:
        metadata, output_bytes, client, open_mock = await render(
            rgb_jpeg_bytes(), IDENTITY, render_settings
        )
        client.assets.versions.list.assert_awaited_once_with(
            ASSET_ID, include=["variants"]
        )
        open_mock.assert_awaited_once_with(ORIGINAL_URL)
        client.assets.versions.delete.assert_not_called()
        client.assets.versions.revert.assert_not_called()
        client.assets.retrieve.assert_not_called()
        renderd_image = Image.open(io.BytesIO(output_bytes))
        assert (metadata.width, metadata.height) == renderd_image.size
        assert metadata.size_bytes == len(output_bytes)
        assert metadata.mime_type == "image/jpeg"

    async def test_supplied_snapshot_skips_listing(
        self, render_settings: Settings
    ) -> None:
        """A pre-listed chain snapshot is honored: no second list call."""
        snapshot_url = "https://cdn.example.test/signed/from-snapshot"
        snapshot: Any = [make_version(url=snapshot_url)]
        # The client's own list would resolve a different chain; the renderer
        # must never consult it when a snapshot is supplied.
        client = make_client([make_version(url="https://cdn.example.test/wrong")])
        response = FakeCdnResponse(rgb_jpeg_bytes())
        open_mock = AsyncMock(return_value=response)
        with (
            patch.object(renderer_module, "open_cdn_response", open_mock),
            patch.object(renderer_module, "get_settings", return_value=render_settings),
        ):
            async with render_asset_edit(client, ASSET_ID, IDENTITY, snapshot):
                pass
        client.assets.versions.list.assert_not_called()
        open_mock.assert_awaited_once_with(snapshot_url)

    async def test_renders_from_root_not_prior_edit(
        self, render_settings: Settings
    ) -> None:
        root = make_version(position=0, url=ORIGINAL_URL)
        current = make_version(
            position=1,
            url="https://cdn.example.test/signed/prior-edit",
            version_id="asset_version_edit",
        )
        _, _, _, open_mock = await render(
            rgb_jpeg_bytes(),
            IDENTITY,
            render_settings,
            versions=[root, current],
        )
        open_mock.assert_awaited_once_with(ORIGINAL_URL)

    async def test_renders_from_latest_external_rendering(
        self, render_settings: Settings
    ) -> None:
        external_url = "https://cdn.example.test/signed/external"
        versions = [
            make_version(position=0, url=ORIGINAL_URL),
            make_version(
                position=1,
                url=external_url,
                version_id="asset_version_external",
                kind="external:enhancer",
            ),
            make_version(
                position=2,
                url="https://cdn.example.test/signed/prior-edit",
                version_id="asset_version_edit",
            ),
        ]
        _, _, _, open_mock = await render(
            rgb_jpeg_bytes(), IDENTITY, render_settings, versions=versions
        )
        open_mock.assert_awaited_once_with(external_url)

    async def test_edit_below_external_base_still_renders_from_external(
        self, render_settings: Settings
    ) -> None:
        external_url = "https://cdn.example.test/signed/external"
        versions = [
            make_version(position=0, url=ORIGINAL_URL),
            make_version(
                position=1,
                url="https://cdn.example.test/signed/prior-edit",
                version_id="asset_version_edit",
            ),
            make_version(
                position=2,
                url=external_url,
                version_id="asset_version_external",
                kind="external:enhancer",
            ),
        ]
        _, _, _, open_mock = await render(
            rgb_jpeg_bytes(), IDENTITY, render_settings, versions=versions
        )
        open_mock.assert_awaited_once_with(external_url)

    @pytest.mark.parametrize("positions", [[], [1], [0, 0]])
    async def test_missing_or_duplicate_root_rejected(
        self, render_settings: Settings, positions: list[int]
    ) -> None:
        versions = [make_version(position=p) for p in positions]
        with pytest.raises(EditRenderSourceError) as exc_info:
            await render(rgb_jpeg_bytes(), IDENTITY, render_settings, versions=versions)
        assert exc_info.value.code == "invalid_version_chain"

    async def test_chain_without_non_edit_version_rejected(
        self, render_settings: Settings
    ) -> None:
        with pytest.raises(EditRenderSourceError) as exc_info:
            await render(
                rgb_jpeg_bytes(),
                IDENTITY,
                render_settings,
                versions=[make_version(position=0, kind="edit")],
            )
        assert exc_info.value.code == "invalid_version_chain"

    async def test_root_without_original_url_rejected(
        self, render_settings: Settings
    ) -> None:
        with pytest.raises(EditRenderSourceError) as exc_info:
            await render(
                rgb_jpeg_bytes(),
                IDENTITY,
                render_settings,
                versions=[make_version(url=None)],
            )
        assert exc_info.value.code == "source_bytes_unavailable"

    async def test_non_image_root_rejected_before_download(
        self, render_settings: Settings
    ) -> None:
        client = make_client([make_version(mime_type="video/mp4")])
        open_mock = AsyncMock()
        with (
            patch.object(renderer_module, "open_cdn_response", open_mock),
            patch.object(renderer_module, "get_settings", return_value=render_settings),
        ):
            with pytest.raises(EditRenderInputError) as exc_info:
                async with render_asset_edit(client, ASSET_ID, IDENTITY):
                    pass
        assert exc_info.value.code == "unsupported_image"
        open_mock.assert_not_awaited()

    async def test_repeat_adjustment_rerenders_from_position_zero(
        self, render_settings: Settings
    ) -> None:
        source_bytes = rgb_jpeg_bytes(6, 4)
        first_recipe = EditRecipe(
            crop=CropBox(x=1, y=1, width=4, height=2), angle=90, mirror=False
        )
        second_recipe = EditRecipe(
            crop=CropBox(x=1, y=1, width=4, height=2), angle=0, mirror=False
        )
        _, _, _, first_open = await render(source_bytes, first_recipe, render_settings)
        _, adjusted, _, second_open = await render(
            source_bytes, second_recipe, render_settings
        )
        _, fresh, _, _ = await render(source_bytes, second_recipe, render_settings)
        assert adjusted == fresh
        first_open.assert_awaited_once_with(ORIGINAL_URL)
        second_open.assert_awaited_once_with(ORIGINAL_URL)


class TestInputValidationAndLimits:
    async def test_corrupt_bytes_rejected(self, render_settings: Settings) -> None:
        with pytest.raises(EditRenderInputError) as exc_info:
            await render(b"definitely not an image", IDENTITY, render_settings)
        assert exc_info.value.code == "unsupported_image"

    async def test_animated_source_rejected(self, render_settings: Settings) -> None:
        frames = [Image.new("RGB", (4, 2), color) for color in ("red", "blue")]
        buffer = io.BytesIO()
        frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:])
        with pytest.raises(EditRenderInputError) as exc_info:
            await render(
                buffer.getvalue(),
                IDENTITY,
                render_settings,
                versions=[make_version(mime_type="image/gif")],
            )
        assert exc_info.value.code == "unsupported_image"

    async def test_negative_crop_origin_rejected(
        self, render_settings: Settings
    ) -> None:
        recipe = EditRecipe(
            crop=CropBox(x=-2, y=0, width=4, height=2), angle=0, mirror=False
        )
        with pytest.raises(EditRenderInputError) as exc_info:
            await render(rgb_jpeg_bytes(8, 4), recipe, render_settings)
        assert exc_info.value.code == "crop_out_of_bounds"

    async def test_truncated_image_rejected(self, render_settings: Settings) -> None:
        source_bytes = rgb_jpeg_bytes(32, 32)
        with pytest.raises(EditRenderInputError) as exc_info:
            await render(
                source_bytes[: len(source_bytes) // 2], IDENTITY, render_settings
            )
        assert exc_info.value.code == "corrupt_image"

    async def test_declared_content_length_over_cap_rejected_before_read(
        self, render_settings: Settings
    ) -> None:
        settings = render_settings.model_copy(
            update={"edit_render_max_input_bytes": 1000}
        )
        response = FakeCdnResponse(rgb_jpeg_bytes(), headers={"content-length": "5000"})
        with pytest.raises(EditRenderLimitError) as exc_info:
            await render(b"", IDENTITY, settings, cdn_response=response)
        assert exc_info.value.code == "input_too_large"
        assert not response.body_read
        assert response.closed

    async def test_content_length_lie_caught_while_streaming(
        self, render_settings: Settings
    ) -> None:
        settings = render_settings.model_copy(
            update={"edit_render_max_input_bytes": 1000}
        )
        response = FakeCdnResponse(b"x" * 5000, headers={"content-length": "10"})
        with pytest.raises(EditRenderLimitError) as exc_info:
            await render(b"", IDENTITY, settings, cdn_response=response)
        assert exc_info.value.code == "input_too_large"
        assert response.closed

    async def test_mid_stream_cdn_failure_classified(
        self, render_settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        class DyingCdnResponse:
            def __init__(self) -> None:
                self.headers: dict[str, str] = {}
                self.closed = False

            async def aiter_bytes(self, chunk_size: int) -> AsyncIterator[bytes]:
                yield b"partial"
                raise httpx.ReadError("connection lost")

            async def aclose(self) -> None:
                self.closed = True

        response = DyingCdnResponse()
        client = make_client([make_version()])
        with (
            patch.object(
                renderer_module, "open_cdn_response", AsyncMock(return_value=response)
            ),
            patch.object(renderer_module, "get_settings", return_value=render_settings),
            caplog.at_level(logging.WARNING, logger="services.asset_edit_renderer"),
        ):
            with pytest.raises(EditRenderSourceError) as exc_info:
                async with render_asset_edit(client, ASSET_ID, IDENTITY):
                    pass  # pragma: no cover
        assert exc_info.value.code == "source_fetch_failed"
        assert response.closed
        record = next(
            r
            for r in caplog.records
            if r.getMessage() == "CDN stream failed while downloading source bytes"
        )
        assert getattr(record, "asset_id") == ASSET_ID
        assert getattr(record, "cdn_host") == "cdn.example.test"
        assert ORIGINAL_URL not in str(record.__dict__)

    async def test_stream_over_cap_without_content_length(
        self, render_settings: Settings
    ) -> None:
        settings = render_settings.model_copy(
            update={"edit_render_max_input_bytes": 1000}
        )
        response = FakeCdnResponse(b"x" * 5000, headers={})
        with pytest.raises(EditRenderLimitError) as exc_info:
            await render(b"", IDENTITY, settings, cdn_response=response)
        assert exc_info.value.code == "input_too_large"
        assert response.closed

    async def test_decompression_bomb_pixel_count_rejected(
        self, render_settings: Settings
    ) -> None:
        settings = render_settings.model_copy(update={"edit_render_max_pixels": 100})
        with pytest.raises(EditRenderLimitError) as exc_info:
            await render(rgb_jpeg_bytes(20, 20), IDENTITY, settings)
        assert exc_info.value.code == "image_too_large"

    async def test_oversized_dimension_rejected(
        self, render_settings: Settings
    ) -> None:
        settings = render_settings.model_copy(update={"edit_render_max_dimension": 10})
        with pytest.raises(EditRenderLimitError) as exc_info:
            await render(rgb_jpeg_bytes(20, 4), IDENTITY, settings)
        assert exc_info.value.code == "image_too_large"

    async def test_output_over_cap_rejected(self, render_settings: Settings) -> None:
        settings = render_settings.model_copy(
            update={"edit_render_max_output_bytes": 64}
        )
        with pytest.raises(EditRenderLimitError) as exc_info:
            await render(rgb_jpeg_bytes(32, 32), IDENTITY, settings)
        assert exc_info.value.code == "output_too_large"

    async def test_crop_beyond_decoded_frame_rejected(
        self, render_settings: Settings
    ) -> None:
        recipe = EditRecipe(
            crop=CropBox(x=0, y=0, width=10, height=10), angle=0, mirror=False
        )
        with pytest.raises(EditRenderInputError) as exc_info:
            await render(rgb_jpeg_bytes(4, 2), recipe, render_settings)
        assert exc_info.value.code == "crop_out_of_bounds"

    async def test_dimension_mismatch_is_internal_error(self) -> None:
        image = Image.new("RGB", (4, 2))
        with pytest.raises(EditRenderError) as exc_info:
            renderer_module._require_dimensions(image, 2, 4, "rotate")
        assert exc_info.value.code == "dimension_mismatch"

    async def test_inputs_exactly_at_each_cap_succeed(
        self, render_settings: Settings
    ) -> None:
        source_bytes = rgb_jpeg_bytes(20, 10)
        settings = render_settings.model_copy(
            update={
                "edit_render_max_input_bytes": len(source_bytes),
                "edit_render_max_pixels": 20 * 10,
                "edit_render_max_dimension": 20,
            }
        )
        metadata, _, _, _ = await render(source_bytes, IDENTITY, settings)
        assert (metadata.width, metadata.height) == (20, 10)

    async def test_output_exactly_at_cap_succeeds(
        self, render_settings: Settings
    ) -> None:
        source_bytes = rgb_jpeg_bytes(20, 10)
        first, _, _, _ = await render(source_bytes, IDENTITY, render_settings)
        settings = render_settings.model_copy(
            update={"edit_render_max_output_bytes": first.size_bytes}
        )
        second, _, _, _ = await render(source_bytes, IDENTITY, settings)
        assert second.size_bytes == first.size_bytes


class RecordingTempFactory:
    """tempfile shim that records every spooled temp file it hands out."""

    def __init__(self) -> None:
        self.files: list[IO[bytes]] = []

    def SpooledTemporaryFile(self, **kwargs: Any) -> IO[bytes]:
        file: IO[bytes] = tempfile.SpooledTemporaryFile(**kwargs)
        self.files.append(file)
        return file


class TestLifetimeAndCleanup:
    async def test_temp_files_cleaned_on_success(
        self, render_settings: Settings
    ) -> None:
        factory = RecordingTempFactory()
        client = make_client([make_version()])
        response = FakeCdnResponse(rgb_jpeg_bytes())
        with (
            patch.object(renderer_module, "tempfile", factory),
            patch.object(
                renderer_module, "open_cdn_response", AsyncMock(return_value=response)
            ),
            patch.object(renderer_module, "get_settings", return_value=render_settings),
        ):
            async with render_asset_edit(client, ASSET_ID, IDENTITY) as renderd:
                assert not renderd.body.closed
        assert len(factory.files) == 2  # input spool + output spool
        assert all(file.closed for file in factory.files)

    async def test_temp_files_cleaned_when_caller_fails(
        self, render_settings: Settings
    ) -> None:
        factory = RecordingTempFactory()
        client = make_client([make_version()])
        response = FakeCdnResponse(rgb_jpeg_bytes())
        with (
            patch.object(renderer_module, "tempfile", factory),
            patch.object(
                renderer_module, "open_cdn_response", AsyncMock(return_value=response)
            ),
            patch.object(renderer_module, "get_settings", return_value=render_settings),
        ):
            with pytest.raises(RuntimeError, match="version create failed"):
                async with render_asset_edit(client, ASSET_ID, IDENTITY):
                    raise RuntimeError("version create failed")
        assert all(file.closed for file in factory.files)

    async def test_temp_files_cleaned_on_validation_failure(
        self, render_settings: Settings
    ) -> None:
        factory = RecordingTempFactory()
        client = make_client([make_version()])
        response = FakeCdnResponse(b"definitely not an image")
        with (
            patch.object(renderer_module, "tempfile", factory),
            patch.object(
                renderer_module, "open_cdn_response", AsyncMock(return_value=response)
            ),
            patch.object(renderer_module, "get_settings", return_value=render_settings),
        ):
            with pytest.raises(EditRenderInputError):
                async with render_asset_edit(client, ASSET_ID, IDENTITY):
                    pass
        assert all(file.closed for file in factory.files)

    async def test_timeout_raises_and_reaps_abandoned_result(
        self, render_settings: Settings
    ) -> None:
        settings = render_settings.model_copy(
            update={"edit_render_timeout_seconds": 0.25}
        )
        client = make_client([make_version()])
        response = FakeCdnResponse(rgb_jpeg_bytes())
        abandoned_body = Mock()
        release = threading.Event()

        def slow_render_sync(
            input_file: IO[bytes], recipe: EditRecipe, settings_arg: Settings
        ) -> RenderdEdit:
            input_file.close()
            # Event-gated (not a sleep) so the worker can never finish
            # before the render timeout fires.
            release.wait(timeout=10)
            return RenderdEdit(
                body=abandoned_body,
                mime_type="image/jpeg",
                width=1,
                height=1,
                size_bytes=1,
            )

        with (
            patch.object(renderer_module, "_render_sync", slow_render_sync),
            patch.object(
                renderer_module, "open_cdn_response", AsyncMock(return_value=response)
            ),
            patch.object(renderer_module, "get_settings", return_value=settings),
        ):
            try:
                with pytest.raises(EditRenderTimeoutError) as exc_info:
                    async with render_asset_edit(client, ASSET_ID, IDENTITY):
                        pass
                assert exc_info.value.code == "render_timeout"
            finally:
                release.set()
            # The abandoned worker thread must close its orphaned output.
            async with asyncio.timeout(5):
                while not abandoned_body.close.called:
                    await asyncio.sleep(0.01)

    async def test_cdn_failure_propagates_after_cleanup(
        self, render_settings: Settings
    ) -> None:
        factory = RecordingTempFactory()
        client = make_client([make_version()])
        cdn_error = HTTPException(status_code=502, detail="CDN upstream error")
        with (
            patch.object(renderer_module, "tempfile", factory),
            patch.object(
                renderer_module, "open_cdn_response", AsyncMock(side_effect=cdn_error)
            ),
            patch.object(renderer_module, "get_settings", return_value=render_settings),
        ):
            with pytest.raises(HTTPException) as exc_info:
                async with render_asset_edit(client, ASSET_ID, IDENTITY):
                    pass  # pragma: no cover
        assert exc_info.value is cdn_error
        assert len(factory.files) == 1  # only the input spool was created
        assert all(file.closed for file in factory.files)

    async def test_timeout_swallows_abandoned_render_failure(
        self, render_settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An abandoned worker that then fails logs a warning, not a crash.

        The warning is the swallow mechanism's whole observable contract
        (the exception must not surface anywhere), so asserting on it is
        the log-level-as-contract exception to the no-log-assertions rule.
        """
        settings = render_settings.model_copy(
            update={"edit_render_timeout_seconds": 0.25}
        )
        client = make_client([make_version()])
        response = FakeCdnResponse(rgb_jpeg_bytes())
        release = threading.Event()

        def failing_render_sync(
            input_file: IO[bytes], recipe: EditRecipe, settings_arg: Settings
        ) -> RenderdEdit:
            input_file.close()
            # Event-gated (not a sleep) so the failure can never surface
            # before the render timeout abandons this worker.
            release.wait(timeout=10)
            raise RuntimeError("decode blew up after abandonment")

        with (
            patch.object(renderer_module, "_render_sync", failing_render_sync),
            patch.object(
                renderer_module, "open_cdn_response", AsyncMock(return_value=response)
            ),
            patch.object(renderer_module, "get_settings", return_value=settings),
            caplog.at_level(logging.WARNING, logger="services.asset_edit_renderer"),
        ):
            try:
                with pytest.raises(EditRenderTimeoutError):
                    async with render_asset_edit(client, ASSET_ID, IDENTITY):
                        pass  # pragma: no cover
            finally:
                release.set()
            # The abandoned worker must reach the swallow branch and emit
            # its warning rather than surfacing the RuntimeError.
            async with asyncio.timeout(5):
                while not any(
                    record.getMessage() == "Abandoned edit render failed"
                    for record in caplog.records
                ):
                    await asyncio.sleep(0.01)

    async def test_cancellation_while_queued_for_a_worker_cleans_up(
        self, render_settings: Settings
    ) -> None:
        """A render cancelled while queued behind a full pool closes its spool.

        The queued runner never starts, so _render_sync's finally can never
        close the input file — the awaiting side must. Admission normally
        keeps renders out of the executor queue entirely (slots == workers),
        so the bound is deliberately widened here to pin the submit-to-start
        cancellation window's cleanup path.
        """
        factory = RecordingTempFactory()
        settings = render_settings.model_copy(
            update={
                "edit_render_max_concurrency": 1,
                "edit_render_timeout_seconds": 30.0,
            }
        )
        release = threading.Event()
        entered: list[str] = []

        with (
            patch.object(renderer_module, "tempfile", factory),
            patch.object(
                renderer_module,
                "_render_sync",
                make_blocking_render_sync(release, entered),
            ),
            patch.object(renderer_module, "get_settings", return_value=settings),
        ):
            renderer_module._render_admission = asyncio.Semaphore(2)
            try:
                first = asyncio.create_task(run_stub_render())
                async with asyncio.timeout(10):
                    while not entered:
                        await asyncio.sleep(0.01)
                # The single worker is now blocked; the second render queues.
                second = asyncio.create_task(run_stub_render())
                async with asyncio.timeout(10):
                    # Wait until the second render's work item is actually
                    # sitting in the executor queue, so the cancel provably
                    # lands on the queued-future await and not mid-download.
                    executor = renderer_module._render_executor
                    assert executor is not None
                    while executor._work_queue.qsize() < 1:  # pyright: ignore[reportAttributeAccessIssue]
                        await asyncio.sleep(0.01)
                second.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await second
                assert factory.files[1].closed
            finally:
                release.set()
            async with asyncio.timeout(10):
                await first
        # The queued runner must never have started.
        assert len(entered) == 1
        assert all(file.closed for file in factory.files)

    async def test_cancellation_during_download_cleans_up(
        self, render_settings: Settings
    ) -> None:
        factory = RecordingTempFactory()
        client = make_client([make_version()])
        download_started = asyncio.Event()

        class HangingCdnResponse:
            def __init__(self) -> None:
                self.headers: dict[str, str] = {}
                self.closed = False

            async def aiter_bytes(self, chunk_size: int) -> AsyncIterator[bytes]:
                download_started.set()
                await asyncio.Event().wait()
                yield b""  # pragma: no cover

            async def aclose(self) -> None:
                self.closed = True

        response = HangingCdnResponse()
        with (
            patch.object(renderer_module, "tempfile", factory),
            patch.object(
                renderer_module, "open_cdn_response", AsyncMock(return_value=response)
            ),
            patch.object(renderer_module, "get_settings", return_value=render_settings),
        ):

            async def run() -> None:
                async with render_asset_edit(client, ASSET_ID, IDENTITY):
                    pass  # pragma: no cover

            task = asyncio.create_task(run())
            async with asyncio.timeout(5):
                await download_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert response.closed
        assert all(file.closed for file in factory.files)


class TestConcurrencyBound:
    async def test_executor_sized_from_settings(
        self, render_settings: Settings
    ) -> None:
        settings = render_settings.model_copy(update={"edit_render_max_concurrency": 2})
        with patch.object(renderer_module, "get_settings", return_value=settings):
            executor = renderer_module._get_render_executor()
        assert executor._max_workers == 2  # pyright: ignore[reportAttributeAccessIssue]

    async def test_at_most_max_concurrency_renders_run_at_once(
        self, render_settings: Settings
    ) -> None:
        settings = render_settings.model_copy(
            update={
                "edit_render_max_concurrency": 1,
                "edit_render_timeout_seconds": 30.0,
            }
        )
        release = threading.Event()
        entered: list[str] = []

        with patch.object(
            renderer_module, "_render_sync", make_blocking_render_sync(release, entered)
        ):
            with patch.object(renderer_module, "get_settings", return_value=settings):
                try:
                    first = asyncio.create_task(run_stub_render())
                    second = asyncio.create_task(run_stub_render())
                    async with asyncio.timeout(10):
                        while not entered:
                            await asyncio.sleep(0.01)
                        # Give the second render every chance to start; the
                        # single-worker pool must hold it back.
                        await asyncio.sleep(0.1)
                        assert len(entered) == 1
                finally:
                    release.set()
                async with asyncio.timeout(10):
                    await asyncio.gather(first, second)
        assert len(entered) == 2

    async def test_waiting_render_downloads_nothing_until_admitted(
        self, render_settings: Settings
    ) -> None:
        factory = RecordingTempFactory()
        settings = render_settings.model_copy(
            update={
                "edit_render_max_concurrency": 1,
                "edit_render_timeout_seconds": 30.0,
            }
        )
        release = threading.Event()
        entered: list[str] = []
        open_mock = AsyncMock(side_effect=lambda url: FakeCdnResponse(rgb_jpeg_bytes()))

        with (
            patch.object(renderer_module, "tempfile", factory),
            patch.object(
                renderer_module,
                "_render_sync",
                make_blocking_render_sync(release, entered),
            ),
            patch.object(renderer_module, "open_cdn_response", open_mock),
            patch.object(renderer_module, "get_settings", return_value=settings),
        ):
            try:
                first = asyncio.create_task(run_stub_render(patch_cdn=False))
                async with asyncio.timeout(10):
                    while not entered:
                        await asyncio.sleep(0.01)
                second = asyncio.create_task(run_stub_render(patch_cdn=False))
                # Give the second render every chance to run ahead; admission
                # must hold it back before any spool or download exists.
                await asyncio.sleep(0.1)
                assert open_mock.await_count == 1
                assert len(factory.files) == 1
            finally:
                release.set()
            async with asyncio.timeout(10):
                await asyncio.gather(first, second)
        assert open_mock.await_count == 2
        assert len(entered) == 2

    async def test_timed_out_renders_hold_then_free_their_admission_slot(
        self, render_settings: Settings
    ) -> None:
        """A timed-out running render retains its slot until its worker exits;
        a request timing out while waiting for admission downloads nothing;
        and the slot is recovered afterwards rather than leaked."""
        factory = RecordingTempFactory()
        settings = render_settings.model_copy(
            update={
                "edit_render_max_concurrency": 1,
                "edit_render_timeout_seconds": 0.1,
            }
        )
        release = threading.Event()
        entered: list[str] = []
        open_mock = AsyncMock(side_effect=lambda url: FakeCdnResponse(rgb_jpeg_bytes()))

        with (
            patch.object(renderer_module, "tempfile", factory),
            patch.object(
                renderer_module,
                "_render_sync",
                make_blocking_render_sync(release, entered),
            ),
            patch.object(renderer_module, "open_cdn_response", open_mock),
            patch.object(renderer_module, "get_settings", return_value=settings),
        ):
            try:
                first = asyncio.create_task(run_stub_render(patch_cdn=False))
                async with asyncio.timeout(10):
                    while not entered:
                        await asyncio.sleep(0.01)
                # The first render times out but its worker is still running,
                # so the slot must remain occupied...
                with pytest.raises(EditRenderTimeoutError):
                    await first
                # ...which forces the second to time out while waiting for
                # admission, without downloading or spooling anything.
                with pytest.raises(EditRenderTimeoutError) as exc_info:
                    await run_stub_render(patch_cdn=False)
                assert exc_info.value.code == "render_timeout"
                assert open_mock.await_count == 1
                assert len(factory.files) == 1
            finally:
                release.set()
            # Once the abandoned worker exits it releases the slot, so a
            # fresh render is admitted and completes.
            async with asyncio.timeout(10):
                await run_stub_render(patch_cdn=False)
        assert open_mock.await_count == 2
        assert len(entered) == 2

    async def test_failed_download_releases_admission_slot(
        self, render_settings: Settings
    ) -> None:
        settings = render_settings.model_copy(
            update={
                "edit_render_max_concurrency": 1,
                "edit_render_timeout_seconds": 5.0,
            }
        )
        cdn_error = HTTPException(status_code=502, detail="CDN upstream error")
        open_mock = AsyncMock(
            side_effect=[cdn_error, FakeCdnResponse(rgb_jpeg_bytes())]
        )
        client = make_client([make_version()])
        with (
            patch.object(renderer_module, "open_cdn_response", open_mock),
            patch.object(renderer_module, "get_settings", return_value=settings),
        ):
            with pytest.raises(HTTPException):
                async with render_asset_edit(client, ASSET_ID, IDENTITY):
                    pass  # pragma: no cover
            async with render_asset_edit(client, ASSET_ID, IDENTITY) as renderd:
                assert renderd.mime_type == "image/jpeg"
