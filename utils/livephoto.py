"""Detection of iOS live photo .MOV files via QuickTime metadata parsing."""

import logging
import struct

logger = logging.getLogger(__name__)

LIVE_PHOTO_KEY = b"com.apple.quicktime.content.identifier"


def _find_atom(
    data: bytes, target: bytes, offset: int, end: int
) -> tuple[int, int] | None:
    """
    Find a QuickTime atom by type within data[offset:end].

    Returns (body_offset, body_end) of the atom's body, or None if not found.
    """
    pos = offset
    while pos + 8 <= end:
        size, atom_type = struct.unpack(">I4s", data[pos : pos + 8])
        if size < 8:
            # Invalid atom size
            return None
        atom_end = pos + size
        if atom_end > end:
            return None
        if atom_type == target:
            return (pos + 8, atom_end)
        pos = atom_end
    return None


def is_live_photo_video(data: bytes) -> bool:
    """
    Detect iOS live photo .MOV files by checking for
    com.apple.quicktime.content.identifier in QuickTime mdta/keys metadata.

    iOS live photos upload as two separate files: a .MOV (2-3 second video)
    and a .HEIC (still image). The .MOV contains a ContentIdentifier in its
    QuickTime metadata that links it to the paired still image. Regular user
    videos do not have this field.

    Args:
        data: Raw bytes of the uploaded file.

    Returns:
        True if the file is an iOS live photo video, False otherwise
        (including on any parse error).
    """
    try:
        # Step 1: Find the 'moov' atom at the top level
        moov = _find_atom(data, b"moov", 0, len(data))
        if moov is None:
            return False
        moov_start, moov_end = moov

        # Step 2: Find the 'meta' atom inside 'moov'
        meta = _find_atom(data, b"meta", moov_start, moov_end)
        if meta is None:
            return False
        meta_start, meta_end = meta

        # The 'meta' atom may or may not have a 4-byte version/flags prefix.
        # ISOBMFF (MP4) treats 'meta' as a full box with version/flags,
        # but QuickTime .MOV files use a plain container (no version/flags).
        # Try both interpretations instead of guessing the format.
        keys = _find_atom(data, b"keys", meta_start, meta_end)
        if keys is None and meta_start + 4 < meta_end:
            keys = _find_atom(data, b"keys", meta_start + 4, meta_end)
        if keys is None:
            return False
        keys_start, keys_end = keys

        # Step 4: Parse 'keys' entries
        # Format: 4 bytes version/flags, 4 bytes entry count,
        # then entries of: 4 bytes size, 4 bytes namespace, (size-8) bytes key string
        if keys_start + 8 > keys_end:
            return False
        _version_flags, entry_count = struct.unpack(
            ">II", data[keys_start : keys_start + 8]
        )
        pos = keys_start + 8

        for _ in range(entry_count):
            if pos + 8 > keys_end:
                return False
            key_size, _namespace = struct.unpack(">I4s", data[pos : pos + 8])
            if key_size < 8:
                return False
            key_data_end = pos + key_size
            if key_data_end > keys_end:
                return False
            key_string = data[pos + 8 : key_data_end].rstrip(b"\x00")
            if key_string == LIVE_PHOTO_KEY:
                return True
            pos = key_data_end

        return False

    except Exception:
        # Any parse error means this isn't a valid live photo .MOV
        return False
