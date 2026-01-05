from fastapi import APIRouter

router = APIRouter(
    prefix="/.well-known",
    tags=["well-known"],
    responses={404: {"description": "Not found"}},
)


@router.get("/immich")
async def get_immich_well_known():
    """
    Returns the well-known configuration for Immich clients.

    This endpoint allows Immich mobile clients to discover the API endpoint
    without requiring users to specify "/api" in the server URL.
    """
    return {
        "api": {
            "endpoint": "/api",
        },
    }
