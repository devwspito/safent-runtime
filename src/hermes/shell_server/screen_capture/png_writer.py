"""Minimal pure-Python PNG encoder for RGBA frames (no Pillow required).

Writes an 8-bit RGBA PNG from raw bytes compatible with Frame.data.
Implemented per the PNG spec (RFC 2083): IHDR + IDAT (zlib-deflated
filtered scanlines) + IEND.  Filter type 0 (None) per scanline — fast
and sufficient for screenshot archives.
"""

from __future__ import annotations

import struct
import zlib


def _chunk(tag: bytes, data: bytes) -> bytes:
    length = struct.pack(">I", len(data))
    crc = struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return length + tag + data + crc


def encode_rgba_png(width: int, height: int, data: bytes) -> bytes:
    """Return a PNG-encoded byte string from raw RGBA bytes.

    Args:
        width:  image width in pixels.
        height: image height in pixels.
        data:   raw RGBA bytes, length == width * height * 4.

    Returns:
        PNG file content as bytes.

    Raises:
        ValueError: if data length does not match width * height * 4.
    """
    expected = width * height * 4
    if len(data) != expected:
        raise ValueError(
            f"expected {expected} bytes for {width}×{height} RGBA, got {len(data)}"
        )

    # PNG signature.
    sig = b"\x89PNG\r\n\x1a\n"

    # IHDR: width, height, bit_depth=8, color_type=6 (RGBA), rest zeros.
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    ihdr = _chunk(b"IHDR", ihdr_data)

    # IDAT: filter each scanline with type 0 (none), then zlib-compress.
    raw_scanlines = bytearray()
    row_bytes = width * 4
    for y in range(height):
        raw_scanlines.append(0)  # filter byte: no filter
        raw_scanlines += data[y * row_bytes : (y + 1) * row_bytes]

    compressed = zlib.compress(bytes(raw_scanlines), level=6)
    idat = _chunk(b"IDAT", compressed)

    iend = _chunk(b"IEND", b"")

    return sig + ihdr + idat + iend
