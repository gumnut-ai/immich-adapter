"""Regression tests for the generated Immich models.

Guards the pattern-strip fix in ``tools/generate_immich_models.py`` (see
``_strip_non_string_patterns`` for the mechanism): the v3 spec puts string ``pattern``
constraints on ``format: uuid`` / ``date-time`` fields, which pydantic-core rejects
with a ``TypeError`` when a value for such a field is validated — so every DTO carrying
a populated id or timestamp is un-constructable. These tests lock the committed
artifact: uuid/date-time DTOs must construct, and patterns on genuine string fields
must survive.

We assert against the committed generated module rather than importing the generator,
whose PEP-723 inline deps (click/requests/pyyaml) are not installed in the test venv.
"""

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from routers.immich_models import SessionResponseDto, SharedLinkEditDto

MODELS_PATH = Path(__file__).resolve().parents[2] / "routers" / "immich_models.py"


def _session_dto(session_id: UUID) -> SessionResponseDto:
    return SessionResponseDto(
        appVersion=None,
        createdAt="2024-01-01T00:00:00.000Z",
        current=True,
        deviceOS="iOS",
        deviceType="mobile",
        id=session_id,
        isPendingSyncReset=False,
        updatedAt="2024-01-01T00:00:00.000Z",
    )


def test_uuid_id_dto_instantiates():
    """A DTO with a ``UUID`` id builds (it formerly raised the pattern ``TypeError``)."""
    dto = _session_dto(UUID("12345678-1234-4123-8123-123456789012"))
    assert isinstance(dto.id, UUID)


def test_uuid_field_accepts_non_v4_uuid():
    """The id field maps to plain ``UUID``, not ``UUID4``.

    The adapter reproduces Gumnut's UUIDs verbatim and does not control their version,
    so any UUID version must be accepted — a ``UUID4`` constraint could reject the
    adapter's own responses. This id has version nibble ``1`` (a v1 UUID), not ``4``.
    """
    dto = _session_dto(UUID("12345678-1234-1123-8123-123456789012"))
    assert dto.id.version == 1


def test_datetime_field_dto_instantiates():
    """A DTO with an ``AwareDatetime`` field validates a real timestamp.

    The pattern constraint is applied when a datetime *value* is validated, so passing
    a real timestamp to ``SharedLinkEditDto.expiresAt`` is what exercises (and formerly
    tripped) it. The no-argument construction is only a default-value smoke check.
    """
    assert SharedLinkEditDto().expiresAt is None
    dto = SharedLinkEditDto(expiresAt=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert dto.expiresAt is not None


def test_patterns_stripped_from_uuid_and_datetime_but_kept_on_strings():
    """Generated source drops uuid/date-time patterns and keeps genuine string ones."""
    source = MODELS_PATH.read_text()
    # Distinctive, backslash-free fragments of the stripped patterns.
    assert "4[0-9a-fA-F]{3}-[89abAB]" not in source, "v4-UUID pattern was not stripped"
    assert "-02-29" not in source, "RFC3339 date-time pattern was not stripped"
    # A pattern on a genuine string field (email) must be retained.
    assert "~-]+@" in source, "string-field pattern was wrongly stripped"
