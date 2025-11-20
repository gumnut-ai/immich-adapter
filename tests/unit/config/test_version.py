import pytest
from config.immich_version import (
    ImmichVersion,
    parse_immich_version,
    load_immich_version,
)


class TestImmichVersion:
    def test_create_version(self):
        """Test creating an ImmichVersion instance."""
        version = ImmichVersion(major=2, minor=2, patch=3)
        assert version.major == 2
        assert version.minor == 2
        assert version.patch == 3

    def test_version_string_representation(self):
        """Test string representation of ImmichVersion."""
        version = ImmichVersion(major=2, minor=2, patch=3)
        assert str(version) == "2.2.3"

    def test_version_is_frozen(self):
        """Test that ImmichVersion is immutable."""
        version = ImmichVersion(major=2, minor=2, patch=3)
        with pytest.raises(Exception):  # FrozenInstanceError
            version.major = 3  # type: ignore


class TestParseImmichVersion:
    """Test the version parsing logic."""

    def test_parse_version_basic(self):
        """Test parsing a basic version string."""
        version = parse_immich_version("2.2.2")
        assert version.major == 2
        assert version.minor == 2
        assert version.patch == 2

    def test_parse_version_with_v_prefix(self):
        """Test parsing version with 'v' prefix (e.g., v2.2.2)."""
        version = parse_immich_version("v2.2.2")
        assert version.major == 2
        assert version.minor == 2
        assert version.patch == 2

    def test_parse_version_with_whitespace(self):
        """Test parsing version with surrounding whitespace."""
        version = parse_immich_version("  v1.2.3  \n")
        assert version.major == 1
        assert version.minor == 2
        assert version.patch == 3

    def test_parse_version_large_numbers(self):
        """Test parsing version with large version numbers."""
        version = parse_immich_version("142.99.1000")
        assert version.major == 142
        assert version.minor == 99
        assert version.patch == 1000

    def test_parse_version_rejects_invalid_format(self):
        """Test that invalid version format raises ValueError."""
        with pytest.raises(ValueError, match="Invalid version format"):
            parse_immich_version("invalid-version")

    def test_parse_version_rejects_semver_with_prerelease(self):
        """Test that semver with prerelease suffix is rejected (e.g., 2.2.2-beta)."""
        with pytest.raises(ValueError, match="Invalid version format"):
            parse_immich_version("2.2.2-beta")

    def test_parse_version_rejects_four_part_version(self):
        """Test that four-part version is rejected (e.g., 2.2.2.1)."""
        with pytest.raises(ValueError, match="Invalid version format"):
            parse_immich_version("2.2.2.1")

    def test_parse_version_rejects_incomplete_version(self):
        """Test that incomplete version is rejected (e.g., 2.2)."""
        with pytest.raises(ValueError, match="Invalid version format"):
            parse_immich_version("2.2")

    def test_parse_version_rejects_text_in_version(self):
        """Test that text in version numbers is rejected."""
        with pytest.raises(ValueError, match="Invalid version format"):
            parse_immich_version("2.two.2")


class TestLoadImmichVersion:
    """Test loading version from file (integration tests)."""

    def test_load_version_from_file(self):
        """Test loading version from actual .immich-container-tag file."""
        version = load_immich_version()

        # Verify it returns an ImmichVersion instance
        assert isinstance(version, ImmichVersion)

        # Verify all fields are integers
        assert isinstance(version.major, int)
        assert isinstance(version.minor, int)
        assert isinstance(version.patch, int)

        # Sanity check: major version should be positive
        assert version.major > 0
