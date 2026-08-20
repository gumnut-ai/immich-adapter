"""Bake normalized Immich edits into derived JPEG/PNG bytes.

Every bake starts from the asset's position-0 exact original, keeping repeated
adjustments non-cumulative. The service returns upload-ready bytes and metadata
but does not create a Gumnut version or emit a websocket event.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import IO, Any, cast

import httpx
from gumnut import AsyncGumnut
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

from config.settings import Settings, get_settings
from routers.utils.asset_edit_conversion import AssetEditError, EditRecipe
from routers.utils.cdn_client import open_cdn_response

# Pillow needs this opener to decode HEIC/HEIF originals.
register_heif_opener()

logger = logging.getLogger(__name__)

# Deterministic JPEG settings: quality 90 with 4:4:4 chroma.
JPEG_QUALITY = 90
JPEG_SUBSAMPLING = 0

_DOWNLOAD_CHUNK_BYTES = 64 * 1024

# Recipe angles are clockwise; Pillow's ROTATE_* values are counterclockwise.
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


_bake_executor: ThreadPoolExecutor | None = None
_bake_admission: asyncio.Semaphore | None = None


def _get_bake_executor() -> ThreadPoolExecutor:
    """Return the dedicated pool that bounds CPU and decoded-pixel memory.

    Timed-out work keeps its worker until it exits, preventing oversubscription
    without consuming the shared asyncio executor.
    """
    global _bake_executor
    if _bake_executor is None:
        _bake_executor = ThreadPoolExecutor(
            max_workers=get_settings().edit_bake_max_concurrency,
            thread_name_prefix="edit-bake",
        )
    return _bake_executor


def _get_bake_admission() -> asyncio.Semaphore:
    """Bound source inputs retained before executor submission.

    Admission precedes download because the executor queue is unbounded.
    Timed-out work retains its slot until its worker exits; requests waiting
    for admission hold no downloaded source.
    """
    global _bake_admission
    if _bake_admission is None:
        _bake_admission = asyncio.Semaphore(get_settings().edit_bake_max_concurrency)
    return _bake_admission


@asynccontextmanager
async def bake_asset_edit(
    client: AsyncGumnut,
    gumnut_asset_id: str,
    recipe: EditRecipe,
) -> AsyncIterator[BakedEdit]:
    """Yield a bake from the position-0 original, closing its body on exit.

    Bake failures use :class:`EditBakeError`; CDN-open and SDK failures retain
    their original exception types.
    """
    settings = get_settings()
    try:
        # Include admission waits so saturation times out instead of queueing.
        async with asyncio.timeout(settings.edit_bake_timeout_seconds):
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

    # Waiting requests must not retain a spool or downloaded source.
    loop = asyncio.get_running_loop()
    admission = _get_bake_admission()
    await admission.acquire()

    # Once started, the worker owns the input and admission slot. The lock
    # ensures that cancellation closes any orphaned result and that either the
    # worker or awaiter—not both—closes the input and releases admission.
    state_lock = threading.Lock()
    state: dict[str, Any] = {"abandoned": False, "started": False, "result": None}
    input_file: IO[bytes] | None = None

    def release_admission_threadsafe() -> None:
        try:
            loop.call_soon_threadsafe(admission.release)
        except RuntimeError:
            # The loop is already shutting down.
            pass

    def runner() -> BakedEdit | None:
        with state_lock:
            if state["abandoned"]:
                # Cancellation won the submit-to-start race; the awaiter owns cleanup.
                return None
            state["started"] = True
        try:
            assert input_file is not None
            try:
                result = _bake_sync(input_file, recipe, settings)
            except BaseException:
                with state_lock:
                    abandoned = state["abandoned"]
                if abandoned:
                    # Avoid an unretrieved exception after the awaiter left.
                    logger.warning(
                        "Abandoned edit bake failed",
                        exc_info=True,
                        extra={"asset_id": gumnut_asset_id},
                    )
                    return None
                raise
            with state_lock:
                if state["abandoned"]:
                    result.body.close()
                    return None
                state["result"] = result
            return result
        finally:
            # Timed-out work retains its slot until the worker exits.
            release_admission_threadsafe()

    try:
        input_file = tempfile.SpooledTemporaryFile(
            max_size=settings.edit_bake_spool_max_bytes
        )
        await _download_source(source_url, input_file, settings, gumnut_asset_id)
        result = await loop.run_in_executor(_get_bake_executor(), runner)
    except BaseException:
        with state_lock:
            state["abandoned"] = True
            started: bool = state["started"]
            orphan: BakedEdit | None = state["result"]
            state["result"] = None
        if orphan is not None:
            orphan.body.close()
        if not started:
            # No worker will run its cleanup for this input or slot.
            if input_file is not None:
                input_file.close()
            admission.release()
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
    source_url: str, destination: IO[bytes], settings: Settings, gumnut_asset_id: str
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
        try:
            async for chunk in response.aiter_bytes(chunk_size=_DOWNLOAD_CHUNK_BYTES):
                received += len(chunk)
                if received > max_bytes:
                    raise EditBakeLimitError(
                        "input_too_large", "Source image exceeds the input byte cap"
                    )
                destination.write(chunk)
        except httpx.HTTPError as exc:
            # Classify failures after the initial response without logging the URL.
            logger.warning(
                "CDN stream failed while downloading source bytes",
                extra={
                    "error_type": type(exc).__name__,
                    "asset_id": gumnut_asset_id,
                    "cdn_host": httpx.URL(source_url).host,
                },
            )
            raise EditBakeSourceError(
                "source_fetch_failed", "Failed to fetch source image bytes"
            ) from exc
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
    """Decode once into display orientation after header-level size checks."""
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
        # Recognized but malformed containers can fail during header parsing.
        raise EditBakeInputError(
            "corrupt_image", "Source image could not be decoded"
        ) from exc

    if getattr(image, "is_animated", False):
        # Pillow would silently flatten the source to frame 0.
        raise EditBakeInputError(
            "unsupported_image", "Animated images cannot be edited"
        )

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
            "image_too_large", "Source image exceeds the dimension or pixel caps"
        )

    try:
        # Decode now so corrupt streams receive a stable error code.
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
        # Pillow pads out-of-frame crops, so validate against decoded dimensions.
        if (
            crop.x < 0
            or crop.y < 0
            or crop.x + crop.width > width
            or crop.y + crop.height > height
        ):
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
    """Write-through file wrapper that aborts once the byte cap is exceeded.

    Deliberately exposes no ``fileno()``: a spool rolled to disk would
    otherwise hand PIL a file descriptor to write through directly,
    bypassing the cap.
    """

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
    """Encode transparency as PNG and opaque pixels as JPEG, without EXIF."""
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
