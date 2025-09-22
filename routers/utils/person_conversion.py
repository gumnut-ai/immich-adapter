"""
Utility functions for converting Gumnut people to Immich format.

This module provides shared functionality for converting person data from the Gumnut API
to the Immich API format, including handling of datetime fields and person metadata.
"""

from datetime import datetime, date, timezone

from gumnut.types.person_response import PersonResponse
from routers.immich_models import PersonWithFacesResponseDto, PersonResponseDto
from routers.utils.gumnut_id_conversion import safe_uuid_from_person_id


def _extract_person_fields(
    gumnut_person: PersonResponse,
) -> tuple[str, str, date, bool, bool, datetime, str]:
    """
    Helper function to extract common fields from a Gumnut PersonResponse.

    Args:
        gumnut_person: The Gumnut PersonResponse object

    Returns:
        Tuple of (person_id, person_name, birth_date, is_favorite, is_hidden, updated_at, thumbnail_path)
    """
    person_id = gumnut_person.id
    person_name = gumnut_person.name or "Unknown Person"
    birth_date = gumnut_person.birth_date or datetime(1970, 1, 1, tzinfo=timezone.utc)  # Default date if None
    is_favorite = gumnut_person.is_favorite
    is_hidden = gumnut_person.is_hidden
    updated_at = gumnut_person.updated_at
    thumbnail_path = gumnut_person.thumbnail_face_url or ""

    # Ensure updated_at is a datetime object
    if updated_at is None:
        updated_at = datetime.now(tz=timezone.utc)
    elif not isinstance(updated_at, datetime):
        # If it's not already a datetime (e.g., it's a string), parse it
        try:
            if isinstance(updated_at, str):
                iso_string: str = updated_at.replace("Z", "+00:00")
                updated_at = datetime.fromisoformat(iso_string)
            else:
                updated_at = datetime.now(tz=timezone.utc)
        except (ValueError, AttributeError):
            updated_at = datetime.now(tz=timezone.utc)

    return (
        person_id,
        person_name,
        birth_date,
        is_favorite,
        is_hidden,
        updated_at,
        thumbnail_path,
    )


def convert_gumnut_person_to_immich(
    gumnut_person: PersonResponse,
) -> PersonResponseDto:
    """
    Convert a Gumnut person to PersonResponseDto format.

    Args:
        gumnut_person: The Gumnut PersonResponse object

    Returns:
        PersonResponseDto object with processed data
    """
    (
        person_id,
        person_name,
        birth_date,
        is_favorite,
        is_hidden,
        updated_at,
        thumbnail_path,
    ) = _extract_person_fields(gumnut_person)

    return PersonResponseDto(
        id=str(safe_uuid_from_person_id(person_id)),
        name=person_name,
        birthDate=birth_date,
        isFavorite=is_favorite,
        isHidden=is_hidden,
        thumbnailPath=thumbnail_path,
        updatedAt=updated_at,
        color=None,  # Gumnut doesn't have color field
    )


def convert_gumnut_person_to_immich_with_faces(
    gumnut_person: PersonResponse,
) -> PersonWithFacesResponseDto:
    """
    Convert a Gumnut person to PersonWithFacesResponseDto format.

    Args:
        gumnut_person: The Gumnut PersonResponse object

    Returns:
        PersonWithFacesResponseDto object with processed data
    """
    (
        person_id,
        person_name,
        birth_date,
        is_favorite,
        is_hidden,
        updated_at,
        thumbnail_path,
    ) = _extract_person_fields(gumnut_person)

    return PersonWithFacesResponseDto(
        id=str(safe_uuid_from_person_id(person_id)),
        name=person_name,
        birthDate=birth_date,
        isFavorite=is_favorite,
        isHidden=is_hidden,
        thumbnailPath=thumbnail_path,
        updatedAt=updated_at,
        color=None,  # Gumnut doesn't have color field
        faces=[],  # Empty faces list - would need separate API call
    )
