import logging
from typing import List
from uuid import UUID

from fastapi import APIRouter, Body, Depends
from gumnut import AsyncGumnut

from routers.immich_models import (
    AssetFaceCreateDto,
    AssetFaceDeleteDto,
    AssetFaceResponseDto,
    FaceDto,
    PersonResponseDto,
    SourceType,
)
from routers.utils.gumnut_client import get_authenticated_gumnut_client
from routers.utils.gumnut_id_conversion import (
    safe_uuid_from_face_id,
    uuid_to_gumnut_asset_id,
    uuid_to_gumnut_face_id,
    uuid_to_gumnut_person_id,
)
from routers.utils.person_conversion import convert_gumnut_person_to_immich


router = APIRouter(
    prefix="/api/faces",
    tags=["faces"],
    responses={404: {"description": "Not found"}},
)

logger = logging.getLogger(__name__)


def _to_immich_source_type(source: str | None) -> SourceType:
    """Map a Gumnut face ``source`` to Immich's ``sourceType``.

    Gumnut reports ``manual`` for user-drawn boxes and ``automatic`` for
    detector-found faces; Immich splits these into ``manual`` vs
    ``machine-learning``. Kept in one place so ``create_face`` and ``get_faces``
    agree — otherwise a manually created face reports ``manual`` on create but
    flips to ``machine-learning`` when re-read via ``GET /faces``.
    """
    return SourceType.manual if source == "manual" else SourceType.machine_learning


@router.delete("/{id}", status_code=204)
async def delete_face(
    id: UUID,
    request: AssetFaceDeleteDto | None = Body(default=None),
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
):
    """Deletes a specific face by ID."""
    gumnut_face_id = uuid_to_gumnut_face_id(id)
    await client.faces.delete(gumnut_face_id)


@router.put("/{id}", response_model=PersonResponseDto)
async def reassign_faces_by_id(
    id: UUID,
    request: FaceDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
):
    """Re-assign the face provided in the body to the person identified by the id in the path parameter.

    Despite the URL collection being /faces, Immich's contract is: path `{id}`
    is the target person, body `id` is the face being reassigned. Verified
    against Immich's face.controller.ts and person.service.reassignFacesById.
    """
    gumnut_person_id = uuid_to_gumnut_person_id(id)
    gumnut_face_id = uuid_to_gumnut_face_id(request.id)
    await client.faces.update(gumnut_face_id, person_id=gumnut_person_id)
    gumnut_person = await client.people.retrieve(gumnut_person_id)
    return convert_gumnut_person_to_immich(gumnut_person)


@router.get("")
async def get_faces(
    id: UUID,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> List[AssetFaceResponseDto]:
    """Get all faces detected in an asset."""
    gumnut_asset_id = uuid_to_gumnut_asset_id(id)

    faces = [f async for f in client.faces.list(asset_id=gumnut_asset_id)]
    if not faces:
        return []

    asset = await client.assets.retrieve(gumnut_asset_id)

    image_width = asset.width or 0
    image_height = asset.height or 0

    # Batch-fetch unique people referenced by faces
    person_ids = {f.person_id for f in faces if f.person_id}
    people_by_id: dict[str, PersonResponseDto] = {}
    for person_id in person_ids:
        try:
            gumnut_person = await client.people.retrieve(person_id)
            people_by_id[person_id] = convert_gumnut_person_to_immich(gumnut_person)
        except Exception:
            logger.warning(
                "Failed to fetch person for face",
                extra={"person_id": person_id, "asset_id": gumnut_asset_id},
            )

    result: List[AssetFaceResponseDto] = []
    for face in faces:
        bb = face.bounding_box or {}
        person = people_by_id.get(face.person_id) if face.person_id else None

        result.append(
            AssetFaceResponseDto(
                id=safe_uuid_from_face_id(face.id),
                boundingBoxX1=bb.get("x", 0),
                boundingBoxX2=bb.get("x", 0) + bb.get("w", 0),
                boundingBoxY1=bb.get("y", 0),
                boundingBoxY2=bb.get("y", 0) + bb.get("h", 0),
                imageWidth=image_width,
                imageHeight=image_height,
                person=person,
                sourceType=_to_immich_source_type(face.source),
            )
        )

    return result


@router.post("", status_code=201)
async def create_face(
    request: AssetFaceCreateDto,
    client: AsyncGumnut = Depends(get_authenticated_gumnut_client),
) -> AssetFaceResponseDto:
    """Draw a user-specified face box on an asset and assign it to a person.

    Backs Immich's "create a face on-the-fly" flow in the face tag editor: the
    client creates the person first (POST /people), then calls this to draw the
    box and link it to that person. Both `assetId` and `personId` are required
    by the Immich request DTO.
    """
    gumnut_asset_id = uuid_to_gumnut_asset_id(request.assetId)
    gumnut_person_id = uuid_to_gumnut_person_id(request.personId)

    # Immich's face editor reports the box in the pixel space of the *preview*
    # it rendered (request.imageWidth/imageHeight — the downscaled image the
    # user drew on), but Gumnut stores and returns face boxes in the asset's
    # full-resolution pixel space (the frame get_faces pairs boxes with via
    # asset.width/height). Without rescaling, a box drawn at the center of a
    # 1440-wide preview is stored verbatim and later read back against the
    # 3024-wide asset, landing shrunk in the top-left corner. Scale the box up
    # to the asset's real dimensions before storing.
    asset = await client.assets.retrieve(gumnut_asset_id)
    scale_x = (
        asset.width / request.imageWidth if asset.width and request.imageWidth else 1.0
    )
    scale_y = (
        asset.height / request.imageHeight
        if asset.height and request.imageHeight
        else 1.0
    )

    # Scale by the box's *endpoints*, not its width/height independently:
    # rounding x and w (or y and h) separately can push x+w one pixel past the
    # asset's right/bottom edge for a box drawn flush to that edge, and the
    # Gumnut API *rejects* a box whose x+w exceeds asset.width (it validates the
    # bound rather than clamping). Deriving the far edge from the scaled endpoint
    # and clamping it to the asset bound keeps an edge-flush box in-bounds while
    # leaving interior boxes unchanged.
    x1 = round(request.x * scale_x)
    y1 = round(request.y * scale_y)
    x2 = round((request.x + request.width) * scale_x)
    y2 = round((request.y + request.height) * scale_y)
    if asset.width:
        x2 = min(x2, asset.width)
    if asset.height:
        y2 = min(y2, asset.height)

    face = await client.faces.create(
        asset_id=gumnut_asset_id,
        bounding_box={"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1},
        person_id=gumnut_person_id,
    )

    # The create response carries only person_id, so fetch the full person to
    # populate the Immich response. Degrade to person=None on failure rather
    # than failing the request — the face was created successfully either way
    # (mirrors get_faces).
    person: PersonResponseDto | None = None
    try:
        gumnut_person = await client.people.retrieve(gumnut_person_id)
        person = convert_gumnut_person_to_immich(gumnut_person)
    except Exception:
        logger.warning(
            "Failed to fetch person for created face",
            extra={"person_id": gumnut_person_id, "asset_id": gumnut_asset_id},
        )

    bb = face.bounding_box or {}
    return AssetFaceResponseDto(
        id=safe_uuid_from_face_id(face.id),
        boundingBoxX1=bb.get("x", 0),
        boundingBoxX2=bb.get("x", 0) + bb.get("w", 0),
        boundingBoxY1=bb.get("y", 0),
        boundingBoxY2=bb.get("y", 0) + bb.get("h", 0),
        # Report the asset's full-resolution frame the scaled box now lives in,
        # matching get_faces so a re-fetch renders the box identically.
        imageWidth=asset.width or request.imageWidth,
        imageHeight=asset.height or request.imageHeight,
        person=person,
        sourceType=_to_immich_source_type(face.source),
    )
