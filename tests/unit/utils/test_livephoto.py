"""Unit tests for iOS live photo .MOV detection."""

import struct
from pathlib import Path

from utils.livephoto import is_live_photo_video, LIVE_PHOTO_KEY

# Real iOS live photo files for integration-style testing
FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "livephoto"
SAMPLE_MOV_PATH = FIXTURES_DIR / "IMG_1309.MOV"
SAMPLE_HEIC_PATH = FIXTURES_DIR / "IMG_1309.HEIC"


def _make_atom(atom_type: bytes, body: bytes) -> bytes:
    """Build a QuickTime atom: 4-byte big-endian size + 4-byte type + body."""
    size = 8 + len(body)
    return struct.pack(">I", size) + atom_type + body


def _make_keys_atom(keys: list[bytes]) -> bytes:
    """Build a 'keys' atom with the given key strings (all using 'mdta' namespace)."""
    # 4 bytes version/flags + 4 bytes entry count
    header = struct.pack(">II", 0, len(keys))
    entries = b""
    for key in keys:
        # entry: 4 bytes size + 4 bytes namespace + key string
        entry_size = 8 + len(key)
        entries += struct.pack(">I", entry_size) + b"mdta" + key
    return _make_atom(b"keys", header + entries)


def _make_qt_meta_atom(children: bytes) -> bytes:
    """Build a QuickTime-style 'meta' atom (no version/flags, plain container).

    Real iOS .MOV files use this format: meta contains hdlr + keys + ilst
    directly, without a version/flags prefix.
    """
    return _make_atom(b"meta", children)


def _make_isobmff_meta_atom(children: bytes) -> bytes:
    """Build an ISOBMFF-style 'meta' atom (full box with version/flags prefix)."""
    version_flags = struct.pack(">I", 0)
    return _make_atom(b"meta", version_flags + children)


def _make_hdlr_atom() -> bytes:
    """Build a minimal 'hdlr' atom like real iOS .MOV files have."""
    # hdlr body: 4 bytes version/flags + 4 bytes pre_defined + 4 bytes handler_type + 12 bytes reserved
    body = b"\x00" * 4 + b"\x00" * 4 + b"mdta" + b"\x00" * 12
    return _make_atom(b"hdlr", body)


def _make_live_photo_mov() -> bytes:
    """Build minimal QuickTime bytes representing an iOS live photo .MOV.

    Uses the real QuickTime .MOV structure: meta has no version/flags prefix,
    and starts with an hdlr atom followed by keys.
    """
    hdlr = _make_hdlr_atom()
    keys = _make_keys_atom([LIVE_PHOTO_KEY])
    meta = _make_qt_meta_atom(hdlr + keys)
    moov = _make_atom(b"moov", meta)
    ftyp = _make_atom(b"ftyp", b"qt  " + b"\x00" * 4)
    return ftyp + moov


def _make_regular_video_mov() -> bytes:
    """Build minimal QuickTime bytes for a regular video (no ContentIdentifier)."""
    hdlr = _make_hdlr_atom()
    keys = _make_keys_atom([b"com.apple.quicktime.make"])
    meta = _make_qt_meta_atom(hdlr + keys)
    moov = _make_atom(b"moov", meta)
    ftyp = _make_atom(b"ftyp", b"qt  " + b"\x00" * 4)
    return ftyp + moov


class TestIsLivePhotoVideo:
    """Tests for is_live_photo_video function."""

    def test_detects_live_photo_mov(self):
        """Live photo .MOV with ContentIdentifier key is detected."""
        data = _make_live_photo_mov()
        assert is_live_photo_video(data) is True

    def test_regular_video_returns_false(self):
        """Regular video with different QuickTime keys is not detected."""
        data = _make_regular_video_mov()
        assert is_live_photo_video(data) is False

    def test_non_quicktime_bytes_returns_false(self):
        """Random bytes that aren't QuickTime format return False."""
        data = b"this is not a quicktime file at all"
        assert is_live_photo_video(data) is False

    def test_empty_data_returns_false(self):
        """Empty bytes return False."""
        assert is_live_photo_video(b"") is False

    def test_truncated_moov_returns_false(self):
        """A moov atom whose declared size exceeds available data returns False."""
        # Create a moov header that claims to be 1000 bytes but only has 8
        data = struct.pack(">I", 1000) + b"moov"
        assert is_live_photo_video(data) is False

    def test_moov_without_meta_returns_false(self):
        """A valid moov atom with no meta child returns False."""
        # moov with only a dummy 'trak' atom inside
        trak = _make_atom(b"trak", b"\x00" * 8)
        moov = _make_atom(b"moov", trak)
        assert is_live_photo_video(moov) is False

    def test_meta_without_keys_returns_false(self):
        """A valid moov>meta with no keys child returns False."""
        # meta with only an hdlr atom (QuickTime-style, no version/flags)
        hdlr = _make_hdlr_atom()
        meta = _make_qt_meta_atom(hdlr)
        moov = _make_atom(b"moov", meta)
        assert is_live_photo_video(moov) is False

    def test_multiple_keys_with_content_identifier(self):
        """ContentIdentifier is found among multiple keys."""
        hdlr = _make_hdlr_atom()
        keys = _make_keys_atom(
            [
                b"com.apple.quicktime.make",
                LIVE_PHOTO_KEY,
                b"com.apple.quicktime.model",
            ]
        )
        meta = _make_qt_meta_atom(hdlr + keys)
        moov = _make_atom(b"moov", meta)
        assert is_live_photo_video(moov) is True

    def test_isobmff_meta_with_content_identifier(self):
        """ISOBMFF-style meta (with version/flags) is also handled."""
        keys = _make_keys_atom([LIVE_PHOTO_KEY])
        meta = _make_isobmff_meta_atom(keys)
        moov = _make_atom(b"moov", meta)
        assert is_live_photo_video(moov) is True


class TestIsLivePhotoVideoWithRealFile:
    """Tests using a real iOS live photo .MOV file.

    These tests require the sample file at ~/Downloads/IMG_1309/IMG_1309.MOV.
    They are skipped if the file is not present.
    """

    def test_real_live_photo_mov_detected(self):
        """Real iOS live photo .MOV is correctly identified."""
        if not SAMPLE_MOV_PATH.exists():
            import pytest

            pytest.skip(f"Sample file not found: {SAMPLE_MOV_PATH}")
        data = SAMPLE_MOV_PATH.read_bytes()
        assert is_live_photo_video(data) is True

    def test_real_heic_not_detected(self):
        """Real iOS .HEIC still image is not identified as live photo video."""
        if not SAMPLE_HEIC_PATH.exists():
            import pytest

            pytest.skip(f"Sample file not found: {SAMPLE_HEIC_PATH}")
        data = SAMPLE_HEIC_PATH.read_bytes()
        assert is_live_photo_video(data) is False
