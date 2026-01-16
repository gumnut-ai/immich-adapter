"""Unit tests for MIME type utility functions."""

from routers.immich_models import AssetTypeEnum
from routers.utils.asset_conversion import mime_type_to_asset_type


class TestMimeTypeToAssetType:
    """Tests for mime_type_to_asset_type()."""

    def test_image_jpeg(self):
        """Test that image/jpeg returns IMAGE."""
        assert mime_type_to_asset_type("image/jpeg") == AssetTypeEnum.IMAGE

    def test_image_png(self):
        """Test that image/png returns IMAGE."""
        assert mime_type_to_asset_type("image/png") == AssetTypeEnum.IMAGE

    def test_image_heic(self):
        """Test that image/heic returns IMAGE."""
        assert mime_type_to_asset_type("image/heic") == AssetTypeEnum.IMAGE

    def test_image_gif(self):
        """Test that image/gif returns IMAGE."""
        assert mime_type_to_asset_type("image/gif") == AssetTypeEnum.IMAGE

    def test_video_mp4(self):
        """Test that video/mp4 returns VIDEO."""
        assert mime_type_to_asset_type("video/mp4") == AssetTypeEnum.VIDEO

    def test_video_quicktime(self):
        """Test that video/quicktime returns VIDEO."""
        assert mime_type_to_asset_type("video/quicktime") == AssetTypeEnum.VIDEO

    def test_video_webm(self):
        """Test that video/webm returns VIDEO."""
        assert mime_type_to_asset_type("video/webm") == AssetTypeEnum.VIDEO

    def test_audio_mp3(self):
        """Test that audio/mpeg returns AUDIO."""
        assert mime_type_to_asset_type("audio/mpeg") == AssetTypeEnum.AUDIO

    def test_audio_wav(self):
        """Test that audio/wav returns AUDIO."""
        assert mime_type_to_asset_type("audio/wav") == AssetTypeEnum.AUDIO

    def test_audio_aac(self):
        """Test that audio/aac returns AUDIO."""
        assert mime_type_to_asset_type("audio/aac") == AssetTypeEnum.AUDIO

    def test_application_octet_stream(self):
        """Test that application/octet-stream returns OTHER."""
        assert (
            mime_type_to_asset_type("application/octet-stream") == AssetTypeEnum.OTHER
        )

    def test_text_plain(self):
        """Test that text/plain returns OTHER."""
        assert mime_type_to_asset_type("text/plain") == AssetTypeEnum.OTHER

    def test_case_sensitive(self):
        """Test that MIME type matching is case-sensitive (lowercase expected)."""
        # Standard MIME types are lowercase, uppercase should return OTHER
        assert mime_type_to_asset_type("IMAGE/JPEG") == AssetTypeEnum.OTHER
        assert mime_type_to_asset_type("Video/mp4") == AssetTypeEnum.OTHER
