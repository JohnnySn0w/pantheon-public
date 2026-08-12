#!/usr/bin/env python3
"""Convert built-in image_gen chroma-key PNGs into 512x512 RGBA stickers."""

from __future__ import annotations

import argparse
import struct
import zlib
from pathlib import Path


PNG_SIG = b"\x89PNG\r\n\x1a\n"


def paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def read_png(path: Path) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    data = path.read_bytes()
    if not data.startswith(PNG_SIG):
        raise ValueError(f"{path} is not a PNG")
    pos = len(PNG_SIG)
    width = height = bit_depth = color_type = None
    idat = bytearray()
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        kind = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            if bit_depth != 8 or compression != 0 or filtering != 0 or interlace != 0:
                raise ValueError("unsupported PNG format")
            if color_type not in (2, 6):
                raise ValueError(f"unsupported PNG color type {color_type}")
        elif kind == b"IDAT":
            idat.extend(payload)
        elif kind == b"IEND":
            break
    if width is None or height is None or color_type is None:
        raise ValueError("missing PNG header")

    channels = 4 if color_type == 6 else 3
    stride = width * channels
    raw = zlib.decompress(bytes(idat))
    rows: list[bytes] = []
    i = 0
    prev = bytearray(stride)
    for _ in range(height):
        filt = raw[i]
        i += 1
        row = bytearray(raw[i : i + stride])
        i += stride
        for x in range(stride):
            left = row[x - channels] if x >= channels else 0
            up = prev[x]
            up_left = prev[x - channels] if x >= channels else 0
            if filt == 1:
                row[x] = (row[x] + left) & 0xFF
            elif filt == 2:
                row[x] = (row[x] + up) & 0xFF
            elif filt == 3:
                row[x] = (row[x] + ((left + up) >> 1)) & 0xFF
            elif filt == 4:
                row[x] = (row[x] + paeth(left, up, up_left)) & 0xFF
            elif filt != 0:
                raise ValueError(f"unsupported PNG filter {filt}")
        rows.append(bytes(row))
        prev = row

    pixels: list[tuple[int, int, int, int]] = []
    for row in rows:
        for x in range(0, len(row), channels):
            if channels == 4:
                pixels.append((row[x], row[x + 1], row[x + 2], row[x + 3]))
            else:
                pixels.append((row[x], row[x + 1], row[x + 2], 255))
    return width, height, pixels


def write_png(path: Path, width: int, height: int, pixels: list[tuple[int, int, int, int]]) -> None:
    channels = 4
    raw = bytearray()
    prev = bytearray(width * channels)
    for y in range(height):
        row = bytearray()
        for x in range(width):
            row.extend(pixels[y * width + x])
        candidates = [bytes([0]) + bytes(row)]

        sub = bytearray(row)
        for i in range(len(sub) - 1, -1, -1):
            left = row[i - channels] if i >= channels else 0
            sub[i] = (row[i] - left) & 0xFF
        candidates.append(bytes([1]) + bytes(sub))

        up = bytearray(row)
        for i in range(len(up)):
            up[i] = (row[i] - prev[i]) & 0xFF
        candidates.append(bytes([2]) + bytes(up))

        average = bytearray(row)
        for i in range(len(average)):
            left = row[i - channels] if i >= channels else 0
            average[i] = (row[i] - ((left + prev[i]) >> 1)) & 0xFF
        candidates.append(bytes([3]) + bytes(average))

        paeth_row = bytearray(row)
        for i in range(len(paeth_row)):
            left = row[i - channels] if i >= channels else 0
            up_pixel = prev[i]
            up_left = prev[i - channels] if i >= channels else 0
            paeth_row[i] = (row[i] - paeth(left, up_pixel, up_left)) & 0xFF
        candidates.append(bytes([4]) + bytes(paeth_row))

        def score(candidate: bytes) -> int:
            return sum(abs(b - 256 if b > 127 else b) for b in candidate[1:])

        raw.extend(min(candidates, key=score))
        prev = row

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        PNG_SIG
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def resize_nearest(width: int, height: int, pixels: list[tuple[int, int, int, int]], size: int) -> list[tuple[int, int, int, int]]:
    out: list[tuple[int, int, int, int]] = []
    for y in range(size):
        sy = min(height - 1, int(y * height / size))
        for x in range(size):
            sx = min(width - 1, int(x * width / size))
            out.append(pixels[sy * width + sx])
    return out


def key_to_alpha(pixels: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    # Sample the border to tolerate generated key colors that are near, not exactly, #00ff00.
    key = (0, 255, 0)
    out: list[tuple[int, int, int, int]] = []
    for red, green, blue, alpha in pixels:
        distance = max(abs(red - key[0]), abs(green - key[1]), abs(blue - key[2]))
        dominance = green - max(red, blue)
        if distance <= 34 or dominance >= 72:
            out.append((red, min(red, blue), blue, 0))
        elif dominance >= 24:
            out_alpha = max(0, min(alpha, int(255 * (72 - dominance) / 48)))
            out.append((red, min(green, max(red, blue)), blue, out_alpha))
        else:
            out.append((red, green, blue, alpha))
    return out


def transparent_corners(width: int, height: int, pixels: list[tuple[int, int, int, int]]) -> bool:
    corners = [pixels[0], pixels[width - 1], pixels[(height - 1) * width], pixels[-1]]
    return all(pixel[3] == 0 for pixel in corners)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    width, height, pixels = read_png(args.input)
    pixels = key_to_alpha(pixels)
    if width != 512 or height != 512:
        pixels = resize_nearest(width, height, pixels, 512)
        width = height = 512
    if not transparent_corners(width, height, pixels):
        raise SystemExit("corner transparency validation failed")
    write_png(args.output, width, height, pixels)


if __name__ == "__main__":
    main()
