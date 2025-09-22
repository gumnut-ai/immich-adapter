from fastapi import APIRouter
from routers.immich_models import (
    AdminOnboardingUpdateDto,
    ReverseGeocodingStateResponseDto,
    VersionCheckStateResponseDto,
)


router = APIRouter(
    prefix="/api/system-metadata",
    tags=["system-metadata"],
    responses={404: {"description": "Not found"}},
)


@router.get("/admin-onboarding")
async def get_admin_onboarding() -> AdminOnboardingUpdateDto:
    """
    Get admin onboarding status.
    This is a stub implementation that returns onboarded status.
    """
    return AdminOnboardingUpdateDto(isOnboarded=True)


@router.post("/admin-onboarding", status_code=204)
async def update_admin_onboarding(request: AdminOnboardingUpdateDto):
    """
    Update admin onboarding status.
    This is a stub implementation that does not perform any action.
    """
    return


@router.get("/reverse-geocoding-state")
async def get_reverse_geocoding_state() -> ReverseGeocodingStateResponseDto:
    """
    Get reverse geocoding state.
    This is a stub implementation that returns dummy geocoding state.
    """
    return ReverseGeocodingStateResponseDto(
        lastImportFileName="",
        lastUpdate="",
    )


@router.get("/version-check-state")
async def get_version_check_state() -> VersionCheckStateResponseDto:
    """
    Get version check state.
    This is a stub implementation that returns dummy version check state.
    """
    return VersionCheckStateResponseDto(
        checkedAt="",
        releaseVersion="",
    )
