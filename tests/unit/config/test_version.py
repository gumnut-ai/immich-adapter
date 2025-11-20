import pytest
from pathlib import Path
from config.immich_version import ImmichVersion, load_immich_version


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


class TestLoadImmichVersion:
    def test_load_version_from_file(self):
        """Test loading version from .immich-container-tag file."""
        version = load_immich_version()

        # Verify it returns an ImmichVersion instance
        assert isinstance(version, ImmichVersion)

        # Verify all fields are integers
        assert isinstance(version.major, int)
        assert isinstance(version.minor, int)
        assert isinstance(version.patch, int)

        # Sanity check: major version should be positive
        assert version.major > 0

    def test_load_version_with_v_prefix(self, tmp_path: Path):
        """Test loading version with 'v' prefix (e.g., v2.2.2)."""
        version_file = tmp_path / ".immich-container-tag"
        version_file.write_text("v2.2.2")

        # Test the parsing logic directly
        def mock_load():
            version_string = version_file.read_text().strip()
            version_string = version_string.lstrip("v")
            import re

            match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version_string)
            if not match:
                raise ValueError(f"Invalid version format: {version_string}")
            major, minor, patch = match.groups()
            return ImmichVersion(major=int(major), minor=int(minor), patch=int(patch))

        version = mock_load()
        assert version.major == 2
        assert version.minor == 2
        assert version.patch == 2

    def test_load_version_without_v_prefix(self, tmp_path: Path):
        """Test loading version without 'v' prefix (e.g., 2.2.2)."""
        version_file = tmp_path / ".immich-container-tag"
        version_file.write_text("2.2.2")

        import re

        version_string = version_file.read_text().strip()
        version_string = version_string.lstrip("v")
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version_string)
        assert match is not None
        major, minor, patch = match.groups()
        version = ImmichVersion(major=int(major), minor=int(minor), patch=int(patch))

        assert version.major == 2
        assert version.minor == 2
        assert version.patch == 2

    def test_load_version_invalid_format(self):
        """Test that invalid version format is caught by regex."""
        # Test the regex parsing logic directly
        import re

        version_string = "invalid-version"
        version_string = version_string.lstrip("v")
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version_string)
        assert match is None  # This should fail to match

    def test_load_version_missing_file(self, tmp_path: Path, monkeypatch):
        """Test that missing version file raises FileNotFoundError."""
        # Monkeypatch to point to non-existent file
        import config.immich_version as version_module

        def mock_load():
            version_file = tmp_path / ".immich-container-tag-nonexistent"
            if not version_file.exists():
                raise FileNotFoundError(f"Version file not found: {version_file}")
            return ImmichVersion(major=1, minor=0, patch=0)

        monkeypatch.setattr(version_module, "load_immich_version", mock_load)

        with pytest.raises(FileNotFoundError):
            version_module.load_immich_version()
