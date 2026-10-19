"""Lightweight image dimension probing without external dependencies.

Used to contain hover media previews to a configurable maximum size.  The
parsers read only the file header — enough for PNG, JPEG, GIF, WEBP and BMP,
the formats Ren'Py games actually ship.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple


def image_dimensions(path: str) -> Optional[Tuple[int, int]]:
    """Return ``(width, height)`` in pixels, or ``None`` when unknown."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
        if len(head) < 2:
            return None
        if head[:2] == b"\xff\xd8":
            return _jpeg_dimensions(path)
        if len(head) < 10:
            return None
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return _png_dimensions(head) if len(head) >= 24 else None
        if head[:3] == b"GIF":
            return _gif_dimensions(head)
        if head[:2] == b"BM":
            return _bmp_dimensions(head) if len(head) >= 26 else None
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return _webp_dimensions(head) if len(head) >= 20 else None
    except OSError:
        return None
    return None


def contained_dimensions(
    path: str, max_size: int
) -> Optional[Tuple[int, int]]:
    """``image_dimensions`` scaled down to fit ``max_size`` x ``max_size``.

    Aspect ratio is preserved and images already within the box are left
    untouched (never upscaled).  ``None`` when the dimensions are unknown.
    """
    dims = image_dimensions(path)
    if dims is None:
        return None
    width, height = dims
    if width <= 0 or height <= 0:
        return None
    scale = min(max_size / width, max_size / height, 1.0)
    return (max(1, int(width * scale)), max(1, int(height * scale)))


def _png_dimensions(head: bytes) -> Optional[Tuple[int, int]]:
    if head[12:16] != b"IHDR":
        return None
    width = int.from_bytes(head[16:20], "big")
    height = int.from_bytes(head[20:24], "big")
    return (width, height)


def _gif_dimensions(head: bytes) -> Optional[Tuple[int, int]]:
    width = int.from_bytes(head[6:8], "little")
    height = int.from_bytes(head[8:10], "little")
    return (width, height)


def _bmp_dimensions(head: bytes) -> Optional[Tuple[int, int]]:
    width = int.from_bytes(head[18:22], "little", signed=True)
    height = int.from_bytes(head[22:26], "little", signed=True)
    return (width, abs(height))


def _webp_dimensions(head: bytes) -> Optional[Tuple[int, int]]:
    chunk = head[12:16]
    if chunk == b"VP8 ":  # lossy
        if len(head) < 30:
            return None
        return (
            int.from_bytes(head[26:28], "little") & 0x3FFF,
            int.from_bytes(head[28:30], "little") & 0x3FFF,
        )
    if chunk == b"VP8L":  # lossless
        if len(head) < 25:
            return None
        bits = int.from_bytes(head[21:25], "little")
        return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if chunk == b"VP8X":  # extended
        if len(head) < 30:
            return None
        return (
            int.from_bytes(head[24:27], "little") + 1,
            int.from_bytes(head[27:30], "little") + 1,
        )
    return None


def _jpeg_dimensions(path: str) -> Optional[Tuple[int, int]]:
    """Scan SOFn markers for the frame dimensions."""
    try:
        with open(path, "rb") as fh:
            fh.seek(2)
            while True:
                head = fh.read(4)
                if len(head) < 4 or head[0] != 0xFF:
                    return None
                marker = head[1]
                if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
                    fh.seek(1, 1)  # standalone marker, no length field
                    continue
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    payload = fh.read(5)
                    if len(payload) < 5:
                        return None
                    height = int.from_bytes(payload[1:3], "big")
                    width = int.from_bytes(payload[3:5], "big")
                    return (width, height)
                seglen = int.from_bytes(head[2:4], "big")
                if seglen < 2:
                    return None
                fh.seek(seglen - 2, 1)
    except OSError:
        return None
