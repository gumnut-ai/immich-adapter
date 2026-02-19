"""Unit tests for iOS live photo .MOV detection."""

import struct
from pathlib import Path

import pytest

from utils.livephoto import is_live_photo_video

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "livephoto"


@pytest.fixture
def sample_mov() -> bytes:
    return (FIXTURES_DIR / "IMG_1309.MOV").read_bytes()


@pytest.fixture
def sample_heic() -> bytes:
    return (FIXTURES_DIR / "IMG_1309.HEIC").read_bytes()


def _atom(atom_type: bytes, body: bytes) -> bytes:
    """Build a QuickTime atom: 4-byte big-endian size + 4-byte type + body."""
    return struct.pack(">I", 8 + len(body)) + atom_type + body


class TestIsLivePhotoVideo:
    """Tests for is_live_photo_video function."""

    def test_real_live_photo_mov_detected(self, sample_mov: bytes):
        """Real iOS live photo .MOV is correctly identified."""
        assert is_live_photo_video(sample_mov) is True

    def test_real_heic_not_detected(self, sample_heic: bytes):
        """Real iOS .HEIC still image is not identified as live photo video."""
        assert is_live_photo_video(sample_heic) is False

    def test_empty_data_returns_false(self):
        assert is_live_photo_video(b"") is False

    def test_non_quicktime_bytes_returns_false(self):
        assert is_live_photo_video(b"this is not a quicktime file") is False

    def test_truncated_moov_returns_false(self):
        """A moov atom whose declared size exceeds available data."""
        data = struct.pack(">I", 1000) + b"moov"
        assert is_live_photo_video(data) is False

    def test_moov_without_meta_returns_false(self):
        data = _atom(b"moov", _atom(b"trak", b"\x00" * 8))
        assert is_live_photo_video(data) is False

    def test_meta_without_keys_returns_false(self):
        hdlr = _atom(b"hdlr", b"\x00" * 24)
        data = _atom(b"moov", _atom(b"meta", hdlr))
        assert is_live_photo_video(data) is False

    def test_isobmff_meta_also_handled(self):
        """ISOBMFF-style meta (4-byte version/flags prefix) is also parsed."""
        # Build a keys atom with the live photo key
        key = b"com.apple.quicktime.content.identifier"
        key_entry = struct.pack(">I", 8 + len(key)) + b"mdta" + key
        keys_body = struct.pack(">II", 0, 1) + key_entry
        keys = _atom(b"keys", keys_body)
        # ISOBMFF meta: 4 zero bytes (version/flags) before children
        meta = _atom(b"meta", b"\x00\x00\x00\x00" + keys)
        data = _atom(b"moov", meta)
        assert is_live_photo_video(data) is True

    def test_null_terminated_key_detected(self):
        """Key string with trailing null bytes is still matched."""
        key = b"com.apple.quicktime.content.identifier\x00"
        key_entry = struct.pack(">I", 8 + len(key)) + b"mdta" + key
        keys_body = struct.pack(">II", 0, 1) + key_entry
        keys = _atom(b"keys", keys_body)
        meta = _atom(b"meta", keys)
        data = _atom(b"moov", meta)
        assert is_live_photo_video(data) is True
