"""Unit tests for user agent parsing utilities."""

from utils.user_agent import extract_device_info, DeviceInfo


class TestExtractDeviceInfo:
    """Tests for extract_device_info function."""

    def test_extracts_device_info_from_browser_user_agent(self):
        """Test that device info is correctly extracted from browser User-Agent."""
        ua_string = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

        result = extract_device_info(ua_string)

        assert isinstance(result, DeviceInfo)
        assert "Chrome" in result.device_type
        # Mac OS X should be normalized to macOS for Immich frontend icons
        assert result.device_os == "macOS"
        assert result.app_version == ""

    def test_extracts_device_info_from_safari_user_agent(self):
        """Test Safari browser detection and macOS normalization."""
        ua_string = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"

        result = extract_device_info(ua_string)

        assert "Safari" in result.device_type
        assert result.device_os == "macOS"
        assert result.app_version == ""

    # Immich mobile app User-Agent format: Immich_{platform}_{version}

    def test_extracts_app_version_from_immich_ios_user_agent(self):
        """Test that app version is extracted from Immich iOS User-Agent."""
        ua_string = "Immich_iOS_1.94.0"

        result = extract_device_info(ua_string)

        assert result.app_version == "1.94.0"

    def test_extracts_app_version_from_immich_android_user_agent(self):
        """Test that app version is extracted from Immich Android User-Agent."""
        ua_string = "Immich_Android_1.95.1"

        result = extract_device_info(ua_string)

        assert result.app_version == "1.95.1"

    def test_handles_empty_user_agent(self):
        """Test that empty User-Agent is handled gracefully."""
        result = extract_device_info("")

        assert isinstance(result, DeviceInfo)
        # user-agents library returns "Other" for unknown browsers/OS
        assert result.device_type == "Other"
        assert result.device_os == "Other"
        assert result.app_version == ""

    def test_handles_windows_user_agent(self):
        """Test Windows OS detection."""
        ua_string = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

        result = extract_device_info(ua_string)

        assert "Chrome" in result.device_type
        assert result.device_os == "Windows"
        assert result.app_version == ""

    def test_handles_linux_user_agent(self):
        """Test Linux OS detection."""
        ua_string = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

        result = extract_device_info(ua_string)

        assert "Chrome" in result.device_type
        assert result.device_os == "Linux"
        assert result.app_version == ""

    def test_handles_ios_safari_user_agent(self):
        """Test iOS Safari detection."""
        ua_string = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"

        result = extract_device_info(ua_string)

        assert result.device_os == "iOS"

    def test_handles_android_chrome_user_agent(self):
        """Test Android Chrome detection."""
        ua_string = "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"

        result = extract_device_info(ua_string)

        assert result.device_os == "Android"

    def test_app_version_not_extracted_from_non_immich_user_agent(self):
        """Test that app_version remains empty for non-Immich User-Agents."""
        ua_string = "SomeOtherApp_iOS_2.0.0"

        result = extract_device_info(ua_string)

        assert result.app_version == ""
