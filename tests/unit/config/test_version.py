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

    def test_load_version_with_v_prefix(self, tmp_path: Path, monkeypatch):
        """Test loading version with 'v' prefix (e.g., v2.2.2)."""
        # Create a fake config module directory with version file
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        version_file = tmp_path / ".immich-container-tag"
        version_file.write_text("v2.2.2")

        # Monkeypatch __file__ in the immich_version module to point to our test location
        import config.immich_version as version_module

        monkeypatch.setattr(
            version_module, "__file__", str(config_dir / "immich_version.py")
        )

        # Now call the actual function
        version = load_immich_version()
        assert version.major == 2
        assert version.minor == 2
        assert version.patch == 2

    def test_load_version_without_v_prefix(self, tmp_path: Path, monkeypatch):
        """Test loading version without 'v' prefix (e.g., 2.2.2)."""
        # Create a fake config module directory with version file
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        version_file = tmp_path / ".immich-container-tag"
        version_file.write_text("2.2.2")

        # Monkeypatch __file__ in the immich_version module
        import config.immich_version as version_module

        monkeypatch.setattr(
            version_module, "__file__", str(config_dir / "immich_version.py")
        )

        # Call the actual function
        version = load_immich_version()
        assert version.major == 2
        assert version.minor == 2
        assert version.patch == 2

    def test_load_version_invalid_format(self, tmp_path: Path, monkeypatch):
        """Test that invalid version format raises ValueError."""
        # Create a fake config module directory with invalid version file
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        version_file = tmp_path / ".immich-container-tag"
        version_file.write_text("invalid-version")

        # Monkeypatch __file__ in the immich_version module
        import config.immich_version as version_module

        monkeypatch.setattr(
            version_module, "__file__", str(config_dir / "immich_version.py")
        )

        # Call the actual function and expect ValueError
        with pytest.raises(ValueError, match="Invalid version format"):
            load_immich_version()

    def test_load_version_missing_file(self, tmp_path: Path, monkeypatch):
        """Test that missing version file raises FileNotFoundError."""
        # Create a fake config module directory but NO version file
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        # Note: deliberately NOT creating .immich-container-tag file

        # Monkeypatch __file__ in the immich_version module
        import config.immich_version as version_module

        monkeypatch.setattr(
            version_module, "__file__", str(config_dir / "immich_version.py")
        )

        # Call the actual function and expect FileNotFoundError
        with pytest.raises(FileNotFoundError, match="Version file not found"):
            load_immich_version()

    def test_load_version_with_whitespace(self, tmp_path: Path, monkeypatch):
        """Test loading version with surrounding whitespace."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        version_file = tmp_path / ".immich-container-tag"
        version_file.write_text("  v1.2.3  \n")

        import config.immich_version as version_module

        monkeypatch.setattr(
            version_module, "__file__", str(config_dir / "immich_version.py")
        )

        version = load_immich_version()
        assert version.major == 1
        assert version.minor == 2
        assert version.patch == 3

    def test_load_version_rejects_semver_with_prerelease(
        self, tmp_path: Path, monkeypatch
    ):
        """Test that semver with prerelease suffix is rejected (e.g., 2.2.2-beta)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        version_file = tmp_path / ".immich-container-tag"
        version_file.write_text("2.2.2-beta")

        import config.immich_version as version_module

        monkeypatch.setattr(
            version_module, "__file__", str(config_dir / "immich_version.py")
        )

        # Should reject because we only accept strict major.minor.patch
        with pytest.raises(ValueError, match="Invalid version format"):
            load_immich_version()

    def test_load_version_rejects_four_part_version(self, tmp_path: Path, monkeypatch):
        """Test that four-part version is rejected (e.g., 2.2.2.1)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        version_file = tmp_path / ".immich-container-tag"
        version_file.write_text("2.2.2.1")

        import config.immich_version as version_module

        monkeypatch.setattr(
            version_module, "__file__", str(config_dir / "immich_version.py")
        )

        # Should reject because we only accept strict major.minor.patch
        with pytest.raises(ValueError, match="Invalid version format"):
            load_immich_version()
