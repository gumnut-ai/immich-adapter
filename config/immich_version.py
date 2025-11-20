from pathlib import Path
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ImmichVersion:
    """Represents an Immich semantic version."""

    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def load_immich_version() -> ImmichVersion:
    """
    Load Immich version from .immich-container-tag file.

    Returns:
        ImmichVersion instance with major, minor, patch

    Raises:
        FileNotFoundError: If .immich-container-tag file doesn't exist
        ValueError: If version format is invalid
    """
    version_file = Path(__file__).parent.parent / ".immich-container-tag"

    if not version_file.exists():
        raise FileNotFoundError(f"Version file not found: {version_file}")

    version_string = version_file.read_text().strip()

    # Remove 'v' prefix if present (e.g., "v2.2.2" -> "2.2.2")
    version_string = version_string.lstrip("v")

    # Parse version using regex: strictly major.minor.patch
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version_string)

    if not match:
        raise ValueError(f"Invalid version format in {version_file}: {version_string}")

    major, minor, patch = match.groups()
    return ImmichVersion(major=int(major), minor=int(minor), patch=int(patch))
