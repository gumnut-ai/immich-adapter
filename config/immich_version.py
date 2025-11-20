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


def parse_immich_version(version_string: str) -> ImmichVersion:
    """
    Parse a version string into an ImmichVersion instance.

    Args:
        version_string: Version string (e.g., "2.2.2", "v2.2.2")

    Returns:
        ImmichVersion instance with major, minor, patch

    Raises:
        ValueError: If version format is invalid
    """
    # Strip whitespace and parse version using regex: strictly major.minor.patch
    # Optional 'v' prefix is allowed
    version_string = version_string.strip()
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version_string)

    if not match:
        raise ValueError(f"Invalid version format: {version_string}")

    major, minor, patch = match.groups()
    return ImmichVersion(major=int(major), minor=int(minor), patch=int(patch))


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

    version_string = version_file.read_text()
    return parse_immich_version(version_string)
