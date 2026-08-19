"""Bake an Immich edit recipe into derived JPEG/PNG bytes, server-side.

Immich sends edit operations rather than baked bytes, so the adapter must
materialize the derived image before calling the Gumnut API's generic version
creation. Every bake starts from the asset's position-0 exact original bytes —
never the current rendering, a thumbnail, or a prior edit — so repeated
adjustments are non-cumulative and suffer no generational encode loss.

The bake pipeline, per the normalized v1 recipe
(`routers/utils/asset_edit_conversion.EditRecipe`):

1. List the asset's versions once and select the unique ``position == 0`` row.
2. Stream its signed exact-byte ``original`` URL to a spooled temp file,
   bounded before (Content-Length) and while streaming.
3. Decode into display orientation exactly once via the embedded EXIF
   orientation, under explicit dimension/pixel caps (Pillow's global
   decompression-bomb guard stays enabled as a backstop).
4. Apply crop (display-oriented frame), then clockwise rotation, then a
   horizontal mirror, asserting intermediate and output dimensions.
5. Encode PNG when transparency must survive, deterministic JPEG otherwise,
   with orientation baked into pixels and no orientation tag in the output.

The service performs no Gumnut version mutation and emits no websocket event.
The Gumnut API remains authoritative for declared-vs-detected format,
dimensions, byte size, and metadata finalization — the returned metadata is
for route decisions and diagnostics only. Signed URLs and raw image bytes are
never logged.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import IO, Any, cast

from gumnut import AsyncGumnut
from PIL import Image, ImageOps, UnidentifiedImageError

from config.settings import Settings, get_settings
from routers.utils.asset_edit_conversion import AssetEditError, EditRecipe
from routers.utils.cdn_client import open_cdn_response

logger = logging.getLogger(__name__)

# Deterministic JPEG encode settings: fixed quality with 4:4:4 chroma
# (no subsampling), matching upstream Immich's high-quality encode choice.
JPEG_QUALITY = 90
JPEG_SUBSAMPLING = 0

_DOWNLOAD_CHUNK_BYTES = 64 * 1024

# Recipe angles are clockwise in display space (pinned by the codec's golden
# tests against upstream Immich); PIL's ROTATE_* transposes are
# counterclockwise, so the mapping inverts.
_CLOCKWISE_ANGLE_TO_TRANSPOSE = {
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


class EditBakeError(AssetEditError):
    """A bake failed — carries a stable machine-readable code."""


class EditBakeSourceError(EditBakeError):
    """The asset's version chain or source bytes are unusable (server-side)."""


class EditBakeInputError(EditBakeError):
    """The source image or recipe cannot produce a valid bake (client-visible)."""


class EditBakeLimitError(EditBakeError):
    """A configured resource cap was exceeded."""


class EditBakeTimeoutError(EditBakeError):
    """The bake exceeded its wall-clock budget."""


@dataclass
class BakedEdit:
    """A baked derived rendering ready for upload.

    ``body`` is a seekable spooled temp file positioned at 0, valid only
    inside the ``bake_asset_edit`` context that produced it.
    """

    body: IO[bytes]
    mime_type: str
    width: int
    height: int
    size_bytes: int


_bake_semaphore: asyncio.Semaphore | None = None


def _get_bake_semaphore() -> asyncio.Semaphore:
    """Lazily create the process-wide bake concurrency bound."""
    global _bake_semaphore
    if _bake_semaphore is None:
        _bake_semaphore = asyncio.Semaphore(get_settings().edit_bake_max_concurrency)
    return _bake_semaphore


@asynccontextmanager
async def bake_asset_edit(
    client: AsyncGumnut,
    gumnut_asset_id: str,
    recipe: EditRecipe,
) -> AsyncIterator[BakedEdit]:
    """Bake ``recipe`` from the asset's position-0 original bytes.

    Yields a :class:`BakedEdit` whose ``body`` stays valid for the
    ``async with`` block; every temp file is deleted on exit regardless of
    outcome (success, validation failure, cancellation, or a caller failure
    such as version-create).

    Raises :class:`EditBakeError` subclasses for bake-domain failures. CDN
    failures propagate as the CDN client's ``HTTPException`` and SDK failures
    as the SDK's exceptions, both after temp cleanup.
    """
    settings = get_settings()
    try:
        async with asyncio.timeout(settings.edit_bake_timeout_seconds):
            async with _get_bake_semaphore():
                baked = await _bake(client, gumnut_asset_id, recipe, settings)
    except TimeoutError as exc:
        raise EditBakeTimeoutError(
            "bake_timeout", "Edit bake exceeded its time budget"
        ) from exc
    try:
        yield baked
    finally:
        baked.body.close()


async def _bake(
    client: AsyncGumnut,
    gumnut_asset_id: str,
    recipe: EditRecipe,
    settings: Settings,
) -> BakedEdit:
    source_url = await _select_root_original_url(client, gumnut_asset_id)

    input_file: IO[bytes] = tempfile.SpooledTemporaryFile(
        max_size=settings.edit_bake_spool_max_bytes
    )
    try:
        await _download_source(source_url, input_file, settings)
    except BaseException:
        input_file.close()
        raise

    # The decode/transform/encode thread owns input_file from here. Shared
    # state (guarded by the lock) makes cleanup airtight when the awaiting
    # task is cancelled or times out in any interleaving: whichever side sees
    # both "abandoned" and a produced result closes it.
    state_lock = threading.Lock()
    state: dict[str, Any] = {"abandoned": False, "result": None}

    def runner() -> BakedEdit | None:
        try:
            result = _bake_sync(input_file, recipe, settings)
        except BaseException:
            with state_lock:
                abandoned = state["abandoned"]
            if abandoned:
                # Nobody is awaiting this thread; swallow so the executor
                # future doesn't surface an unretrieved exception.
                logger.warning("Abandoned edit bake failed", exc_info=True)
                return None
            raise
        with state_lock:
            if state["abandoned"]:
                result.body.close()
                return None
            state["result"] = result
        return result

    try:
        result = await asyncio.to_thread(runner)
    except BaseException:
        with state_lock:
            state["abandoned"] = True
            orphan: BakedEdit | None = state["result"]
            state["result"] = None
        if orphan is not None:
            orphan.body.close()
        raise
    if result is None:  # pragma: no cover - only reachable via the race above
        raise EditBakeTimeoutError("bake_timeout", "Edit bake was abandoned")
    return result


async def _select_root_original_url(client: AsyncGumnut, gumnut_asset_id: str) -> str:
    """List versions once and return the unique root's exact-byte URL."""
    versions = await client.assets.versions.list(gumnut_asset_id, include=["variants"])
    roots = [version for version in versions if version.position == 0]
    if len(roots) != 1:
        logger.error(
            "Asset version chain has no unique root",
            extra={"asset_id": gumnut_asset_id, "root_count": len(roots)},
        )
        raise EditBakeSourceError(
            "invalid_version_chain", "Asset version chain is invalid"
        )
    root = roots[0]

    if not root.mime_type.startswith("image/"):
        raise EditBakeInputError("unsupported_image", "Only image assets can be edited")

    original = (root.version_urls or {}).get("original")
    if original is None:
        logger.warning(
            "Asset original version bytes not available",
            extra={"asset_id": gumnut_asset_id, "version_id": root.id},
        )
        raise EditBakeSourceError(
            "source_bytes_unavailable", "Original version bytes are not available"
        )
    return original.url


async def _download_source(
    source_url: str, destination: IO[bytes], settings: Settings
) -> None:
    """Stream the source bytes into ``destination`` under the input byte cap.

    The cap is enforced against a declared Content-Length before any body
    bytes are read, and re-enforced while streaming so a lying or absent
    header cannot bypass it.
    """
    max_bytes = settings.edit_bake_max_input_bytes
    response = await open_cdn_response(source_url)
    try:
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            raise EditBakeLimitError(
                "input_too_large", "Source image exceeds the input byte cap"
            )
        received = 0
        async for chunk in response.aiter_bytes(chunk_size=_DOWNLOAD_CHUNK_BYTES):
            received += len(chunk)
            if received > max_bytes:
                raise EditBakeLimitError(
                    "input_too_large", "Source image exceeds the input byte cap"
                )
            destination.write(chunk)
    finally:
        await response.aclose()
    destination.seek(0)


def _bake_sync(
    input_file: IO[bytes], recipe: EditRecipe, settings: Settings
) -> BakedEdit:
    """Decode, transform, and encode synchronously (runs in a worker thread)."""
    try:
        image = _decode_display_oriented(input_file, settings)
        transformed = _apply_recipe(image, recipe)
        return _encode(transformed, settings)
    finally:
        input_file.close()


def _decode_display_oriented(input_file: IO[bytes], settings: Settings) -> Image.Image:
    """Decode the source into display orientation, exactly once.

    Dimension and pixel caps are checked from the header before any pixel
    data is decoded. EXIF orientation is applied to pixels here and nowhere
    else; stale embedded thumbnails are irrelevant to the transform because
    only the primary image's pixels are read, and the Gumnut API performs
    authoritative metadata finalization after upload.
    """
    try:
        image = Image.open(input_file)
    except Image.DecompressionBombError as exc:
        raise EditBakeLimitError(
            "image_too_large", "Source image exceeds the decoded pixel cap"
        ) from exc
    except UnidentifiedImageError as exc:
        raise EditBakeInputError(
            "unsupported_image", "Source bytes are not a supported image"
        ) from exc
    except (OSError, SyntaxError, ValueError) as exc:
        # A recognized container that fails while its header/segments are
        # parsed (e.g. a truncated stream) surfaces here rather than as
        # UnidentifiedImageError, which the clause above already consumed.
        raise EditBakeInputError(
            "corrupt_image", "Source image could not be decoded"
        ) from exc

    width, height = image.size
    if width < 1 or height < 1:
        raise EditBakeInputError(
            "corrupt_image", "Source image reports invalid dimensions"
        )
    if (
        width > settings.edit_bake_max_dimension
        or height > settings.edit_bake_max_dimension
        or width * height > settings.edit_bake_max_pixels
    ):
        raise EditBakeLimitError(
            "image_too_large", "Source image exceeds the decoded pixel cap"
        )

    try:
        # Force the full pixel decode here so corrupt streams fail with a
        # stable code instead of surfacing mid-transform or mid-encode.
        image.load()
        oriented = ImageOps.exif_transpose(image)
    except Image.DecompressionBombError as exc:
        raise EditBakeLimitError(
            "image_too_large", "Source image exceeds the decoded pixel cap"
        ) from exc
    except (OSError, SyntaxError, ValueError) as exc:
        raise EditBakeInputError(
            "corrupt_image", "Source image could not be decoded"
        ) from exc
    assert oriented is not None
    return oriented


def _require_dimensions(
    image: Image.Image, width: int, height: int, stage: str
) -> None:
    if image.size != (width, height):
        raise EditBakeError(
            "dimension_mismatch",
            f"Baked image dimensions diverged from the recipe at {stage}",
        )


def _apply_recipe(image: Image.Image, recipe: EditRecipe) -> Image.Image:
    """Apply crop, then clockwise rotation, then horizontal mirror."""
    width, height = image.size

    if recipe.crop is not None:
        crop = recipe.crop
        if crop.x + crop.width > width or crop.y + crop.height > height:
            raise EditBakeInputError(
                "crop_out_of_bounds", "Crop exceeds the source image frame"
            )
        image = image.crop((crop.x, crop.y, crop.x + crop.width, crop.y + crop.height))
        _require_dimensions(image, crop.width, crop.height, "crop")
        width, height = crop.width, crop.height

    if recipe.angle != 0:
        image = image.transpose(_CLOCKWISE_ANGLE_TO_TRANSPOSE[recipe.angle])
        if recipe.angle in (90, 270):
            width, height = height, width
        _require_dimensions(image, width, height, "rotate")

    if recipe.mirror:
        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        _require_dimensions(image, width, height, "mirror")

    return image


def _has_transparency(image: Image.Image) -> bool:
    return "A" in image.getbands() or "transparency" in image.info


class _CappedFile:
    """Write-through file wrapper that aborts once the byte cap is exceeded."""

    def __init__(self, file: IO[bytes], max_bytes: int) -> None:
        self._file = file
        self._max_bytes = max_bytes

    def write(self, data: bytes) -> int:
        written = self._file.write(data)
        if self._file.tell() > self._max_bytes:
            raise EditBakeLimitError(
                "output_too_large", "Encoded output exceeds the output byte cap"
            )
        return written

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._file.seek(offset, whence)

    def tell(self) -> int:
        return self._file.tell()

    def flush(self) -> None:
        self._file.flush()


def _encode(image: Image.Image, settings: Settings) -> BakedEdit:
    """Encode the transformed image to PNG (transparency) or JPEG (otherwise).

    The alpha policy is fixed: any transparency routes to PNG, so the JPEG
    path never flattens — there is no implicit background composite. The
    output container carries no orientation tag (orientation is baked into
    pixels) and no copied EXIF; the Gumnut API finalizes metadata after
    upload.
    """
    output: IO[bytes] = tempfile.SpooledTemporaryFile(
        max_size=settings.edit_bake_spool_max_bytes
    )
    try:
        # PIL only needs write/seek/tell/flush from its fp; the wrapper
        # satisfies that at runtime but not the full IO[bytes] type.
        capped = cast(
            "IO[bytes]", _CappedFile(output, settings.edit_bake_max_output_bytes)
        )
        if _has_transparency(image):
            encoded = image if image.mode in ("RGBA", "LA") else image.convert("RGBA")
            encoded.save(capped, format="PNG")
            mime_type = "image/png"
        else:
            encoded = image if image.mode in ("RGB", "L") else image.convert("RGB")
            encoded.save(
                capped,
                format="JPEG",
                quality=JPEG_QUALITY,
                subsampling=JPEG_SUBSAMPLING,
            )
            mime_type = "image/jpeg"
        output.seek(0, 2)
        size_bytes = output.tell()
        output.seek(0)
        return BakedEdit(
            body=output,
            mime_type=mime_type,
            width=encoded.width,
            height=encoded.height,
            size_bytes=size_bytes,
        )
    except BaseException:
        output.close()
        raise
