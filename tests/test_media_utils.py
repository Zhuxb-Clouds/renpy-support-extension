"""Tests for header-only image dimension probing (media_utils)."""

from __future__ import annotations

import struct
import zlib

import pytest

from media_utils import contained_dimensions, image_dimensions


def _png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">II5B", width, height, 8, 6, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
    )


def _jpeg(width: int, height: int, with_comment: bool = False) -> bytes:
    def sof_marker() -> bytes:
        payload = (
            b"\x08" + struct.pack(">HH", height, width) + b"\x01\x01\x00\x00\x00"
        )
        return b"\xff\xc0" + struct.pack(">H", len(payload) + 2) + payload

    out = b"\xff\xd8"
    if with_comment:
        comment = b"made-up comment"
        out += b"\xff\xfe" + struct.pack(">H", len(comment) + 2) + comment
    return out + sof_marker() + b"\xff\xd9"


def _gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00"


def _bmp(width: int, height: int) -> bytes:
    return b"BM" + b"\x00" * 16 + struct.pack("<ii", width, height) + b"\x00" * 8


def _webp_vp8(width: int, height: int) -> bytes:
    payload = (
        b"\x00\x00\x00\x9d\x01\x2a"
        + struct.pack("<HH", width | 0x8000, height | 0x8000)
    )
    return (
        b"RIFF\x00\x00\x00\x00WEBP"
        + b"VP8 "
        + struct.pack("<I", len(payload))
        + payload
    )


def _webp_vp8l(width: int, height: int) -> bytes:
    bits = ((height - 1) << 14) | (width - 1)
    payload = b"\x2f" + struct.pack("<I", bits)
    return (
        b"RIFF\x00\x00\x00\x00WEBP"
        + b"VP8L"
        + struct.pack("<I", len(payload))
        + payload
    )


def _webp_vp8x(width: int, height: int) -> bytes:
    payload = (
        b"\x00" * 4
        + struct.pack("<I", width - 1)[:3]
        + struct.pack("<I", height - 1)[:3]
    )
    return (
        b"RIFF\x00\x00\x00\x00WEBP"
        + b"VP8X"
        + struct.pack("<I", len(payload))
        + payload
    )


def test_png_dimensions(tmp_path) -> None:
    p = tmp_path / "a.png"
    p.write_bytes(_png(320, 200))
    assert image_dimensions(str(p)) == (320, 200)


def test_jpeg_dimensions(tmp_path) -> None:
    p = tmp_path / "a.jpg"
    p.write_bytes(_jpeg(640, 480))
    assert image_dimensions(str(p)) == (640, 480)

    p2 = tmp_path / "b.jpg"
    p2.write_bytes(_jpeg(64, 32, with_comment=True))
    assert image_dimensions(str(p2)) == (64, 32)


def test_gif_and_bmp_dimensions(tmp_path) -> None:
    p = tmp_path / "a.gif"
    p.write_bytes(_gif(100, 50))
    assert image_dimensions(str(p)) == (100, 50)

    p = tmp_path / "a.bmp"
    p.write_bytes(_bmp(150, 80))
    assert image_dimensions(str(p)) == (150, 80)

    # top-down BMP stores a negative height
    p = tmp_path / "b.bmp"
    p.write_bytes(_bmp(150, -80))
    assert image_dimensions(str(p)) == (150, 80)


def test_webp_dimensions_all_chunks(tmp_path) -> None:
    for builder in (_webp_vp8, _webp_vp8l, _webp_vp8x):
        p = tmp_path / "a.webp"
        p.write_bytes(builder(220, 140))
        assert image_dimensions(str(p)) == (220, 140), builder.__name__


def test_unknown_dimensions(tmp_path) -> None:
    p = tmp_path / "fake.png"
    p.write_bytes(b"\x89PNG\r\n")  # signature only, no IHDR
    assert image_dimensions(str(p)) is None

    p = tmp_path / "garbage.bin"
    p.write_bytes(b"not an image at all")
    assert image_dimensions(str(p)) is None

    assert image_dimensions(str(tmp_path / "missing.png")) is None


def test_contained_scales_down_only(tmp_path) -> None:
    p = tmp_path / "big.png"
    p.write_bytes(_png(800, 600))
    assert contained_dimensions(str(p), 400) == (400, 300)

    p = tmp_path / "tall.png"
    p.write_bytes(_png(4000, 10))
    assert contained_dimensions(str(p), 400) == (400, 1)

    # already within the box: untouched
    p = tmp_path / "small.png"
    p.write_bytes(_png(128, 64))
    assert contained_dimensions(str(p), 400) == (128, 64)
    assert contained_dimensions(str(p), 128) == (128, 64)

    assert contained_dimensions(str(tmp_path / "missing.png"), 400) is None
