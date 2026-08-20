"""Render normalized Immich edits into derived JPEG/PNG bytes.

Every render starts from the asset's edit base — the latest non-edit version,
selected by ``select_edit_base`` — so repeated adjustments are non-cumulative.
The service returns upload-ready bytes and metadata but does not create a
Gumnut version or emit a websocket event.
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
from routers.utils.asset_version_chain import (
    InvalidVersionChainError,
    select_edit_base,
)
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


class EditRenderError(AssetEditError):
    """A render failed — carries a stable machine-readable code."""


class EditRenderSourceError(EditRenderError):
    """The asset's version chain or source bytes are unusable (server-side)."""


class EditRenderInputError(EditRenderError):
    """The source image or recipe cannot produce a valid render (client-visible)."""


class EditRenderLimitError(EditRenderError):
    """A configured resource cap was exceeded."""


class EditRenderTimeoutError(EditRenderError):
    """The render exceeded its wall-clock budget."""


@dataclass
class RenderdEdit:
    """A renderd derived rendering ready for upload.

    ``body`` is a seekable spooled temp file positioned at 0, valid only
    inside the ``render_asset_edit`` context that produced it.
    """

    body: IO[bytes]
    mime_type: str
    width: int
    height: int
    size_bytes: int


_render_executor: ThreadPoolExecutor | None = None
_render_admission: asyncio.Semaphore | None = None


def _get_render_executor() -> ThreadPoolExecutor:
    """Return the dedicated pool that bounds CPU and decoded-pixel memory.

    Timed-out work keeps its worker until it exits, preventing oversubscription
    without consuming the shared asyncio executor.
    """
    global _render_executor
    if _render_executor is None:
        _render_executor = ThreadPoolExecutor(
            max_workers=get_settings().edit_render_max_concurrency,
            thread_name_prefix="edit-render",
        )
    return _render_executor


def _get_render_admission() -> asyncio.Semaphore:
    """Bound source inputs retained before executor submission.

    Admission precedes download because the executor queue is unbounded.
    Timed-out work retains its slot until its worker exits; requests waiting
    for admission hold no downloaded source.
    """
    global _render_admission
    if _render_admission is None:
        _render_admission = asyncio.Semaphore(
            get_settings().edit_render_max_concurrency
        )
    return _render_admission


@asynccontextmanager
async def render_asset_edit(
    client: AsyncGumnut,
    gumnut_asset_id: str,
    recipe: EditRecipe,
) -> AsyncIterator[RenderdEdit]:
    """Yield a render from the asset's edit base, closing its body on exit.

    Render failures use :class:`EditRenderError`; CDN-open and SDK failures retain
    their original exception types.
    """
    settings = get_settings()
    try:
        # Include admission waits so saturation times out instead of queueing.
        async with asyncio.timeout(settings.edit_render_timeout_seconds):
            renderd = await _render(client, gumnut_asset_id, recipe, settings)
    except TimeoutError as exc:
        raise EditRenderTimeoutError(
            "render_timeout", "Edit render exceeded its time budget"
        ) from exc
    try:
        yield renderd
    finally:
        renderd.body.close()


async def _render(
    client: AsyncGumnut,
    gumnut_asset_id: str,
    recipe: EditRecipe,
    settings: Settings,
) -> RenderdEdit:
    source_url = await _select_edit_base_url(client, gumnut_asset_id)

    # Waiting requests must not retain a spool or downloaded source.
    loop = asyncio.get_running_loop()
    admission = _get_render_admission()
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

    def runner(source: IO[bytes]) -> RenderdEdit | None:
        with state_lock:
            if state["abandoned"]:
                # Cancellation won the submit-to-start race; the awaiter owns cleanup.
                return None
            state["started"] = True
        try:
            try:
                result = _render_sync(source, recipe, settings)
            except BaseException:
                with state_lock:
                    abandoned = state["abandoned"]
                if abandoned:
                    # Avoid an unretrieved exception after the awaiter left.
                    logger.warning(
                        "Abandoned edit render failed",
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
            max_size=settings.edit_render_spool_max_bytes
        )
        await _download_source(source_url, input_file, settings, gumnut_asset_id)
        result = await loop.run_in_executor(_get_render_executor(), runner, input_file)
    except BaseException:
        with state_lock:
            state["abandoned"] = True
            started: bool = state["started"]
            orphan: RenderdEdit | None = state["result"]
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
        raise EditRenderTimeoutError("render_timeout", "Edit render was abandoned")
    return result


async def _select_edit_base_url(client: AsyncGumnut, gumnut_asset_id: str) -> str:
    """List versions once and return the edit base's exact-byte URL."""
    versions = await client.assets.versions.list(gumnut_asset_id, include=["variants"])
    try:
        base = select_edit_base(versions, asset_id=gumnut_asset_id)
    except InvalidVersionChainError as exc:
        raise EditRenderSourceError(
            "invalid_version_chain", "Asset version chain is invalid"
        ) from exc

    if not base.mime_type.startswith("image/"):
        raise EditRenderInputError(
            "unsupported_image", "Only image assets can be edited"
        )

    original = (base.version_urls or {}).get("original")
    if original is None:
        logger.warning(
            "Asset edit base bytes not available",
            extra={"asset_id": gumnut_asset_id, "version_id": base.id},
        )
        raise EditRenderSourceError(
            "source_bytes_unavailable", "Edit base version bytes are not available"
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
    max_bytes = settings.edit_render_max_input_bytes
    response = await open_cdn_response(source_url)
    try:
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            raise EditRenderLimitError(
                "input_too_large", "Source image exceeds the input byte cap"
            )
        received = 0
        try:
            async for chunk in response.aiter_bytes(chunk_size=_DOWNLOAD_CHUNK_BYTES):
                received += len(chunk)
                if received > max_bytes:
                    raise EditRenderLimitError(
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
            raise EditRenderSourceError(
                "source_fetch_failed", "Failed to fetch source image bytes"
            ) from exc
    finally:
        await response.aclose()
    destination.seek(0)


def _render_sync(
    input_file: IO[bytes], recipe: EditRecipe, settings: Settings
) -> RenderdEdit:
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
        raise EditRenderLimitError(
            "image_too_large", "Source image exceeds the decoded pixel cap"
        ) from exc
    except UnidentifiedImageError as exc:
        raise EditRenderInputError(
            "unsupported_image", "Source bytes are not a supported image"
        ) from exc
    except (OSError, SyntaxError, ValueError) as exc:
        # Recognized but malformed containers can fail during header parsing.
        raise EditRenderInputError(
            "corrupt_image", "Source image could not be decoded"
        ) from exc

    if getattr(image, "is_animated", False):
        # Pillow would silently flatten the source to frame 0.
        raise EditRenderInputError(
            "unsupported_image", "Animated images cannot be edited"
        )

    width, height = image.size
    if width < 1 or height < 1:
        raise EditRenderInputError(
            "corrupt_image", "Source image reports invalid dimensions"
        )
    if (
        width > settings.edit_render_max_dimension
        or height > settings.edit_render_max_dimension
        or width * height > settings.edit_render_max_pixels
    ):
        raise EditRenderLimitError(
            "image_too_large", "Source image exceeds the dimension or pixel caps"
        )

    try:
        # Decode now so corrupt streams receive a stable error code.
        image.load()
        oriented = ImageOps.exif_transpose(image)
    except Image.DecompressionBombError as exc:
        raise EditRenderLimitError(
            "image_too_large", "Source image exceeds the decoded pixel cap"
        ) from exc
    except (OSError, SyntaxError, ValueError) as exc:
        raise EditRenderInputError(
            "corrupt_image", "Source image could not be decoded"
        ) from exc
    return oriented


def _require_dimensions(
    image: Image.Image, width: int, height: int, stage: str
) -> None:
    if image.size != (width, height):
        raise EditRenderError(
            "dimension_mismatch",
            f"Renderd image dimensions diverged from the recipe at {stage}",
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
            raise EditRenderInputError(
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
            raise EditRenderLimitError(
                "output_too_large", "Encoded output exceeds the output byte cap"
            )
        return written

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._file.seek(offset, whence)

    def tell(self) -> int:
        return self._file.tell()

    def flush(self) -> None:
        self._file.flush()


def _encode(image: Image.Image, settings: Settings) -> RenderdEdit:
    """Encode transparency as PNG and opaque pixels as JPEG, without EXIF."""
    output: IO[bytes] = tempfile.SpooledTemporaryFile(
        max_size=settings.edit_render_spool_max_bytes
    )
    try:
        # PIL only needs write/seek/tell/flush from its fp; the wrapper
        # satisfies that at runtime but not the full IO[bytes] type.
        capped = cast(
            "IO[bytes]", _CappedFile(output, settings.edit_render_max_output_bytes)
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
        return RenderdEdit(
            body=output,
            mime_type=mime_type,
            width=encoded.width,
            height=encoded.height,
            size_bytes=size_bytes,
        )
    except BaseException:
        output.close()
        raise
