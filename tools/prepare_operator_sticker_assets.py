#!/usr/bin/env python3
"""Prepare the operator's six static sticker PNGs from the accepted source sheet."""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.postprocess_ai_sticker_png import read_png, write_png


Pixel = tuple[int, int, int, int]
Box = tuple[int, int, int, int]
Point = tuple[int, int]

SOURCE_SHA256 = "2626ee8e7b446d794b7e8fa09526e51a82ed79de81aa417ff13584f4b54ba096"
CANVAS_SIZE = 512
CORE_ALPHA_THRESHOLD = 128
HALO_ALPHA_THRESHOLD = 1
HALO_RADIUS = 6
MARGIN = 18
MAX_STATIC_STICKER_BYTES = 512 * 1024


@dataclass(frozen=True)
class StickerCrop:
    slug: str
    emoji: str
    source_box: Box
    seed: Point
    output_name: str


OPERATOR_CROPS = (
    StickerCrop("thoughtful", "\U0001F914", (0, 30, 398, 635), (215, 330), "operator-thoughtful.png"),
    StickerCrop("knowing", "\U0001F60F", (430, 15, 818, 620), (610, 335), "operator-knowing.png"),
    StickerCrop("wanderer", "\U0001F9ED", (858, 0, 1254, 610), (1030, 330), "operator-wanderer.png"),
    StickerCrop("focus", "\U0001F4BB", (0, 610, 418, 1225), (215, 930), "operator-focus.png"),
    StickerCrop("tangled", "\U0001F635\u200d\U0001F4AB", (432, 600, 818, 1230), (625, 910), "operator-tangled.png"),
    StickerCrop("serene", "\U0001F54A\ufe0f", (858, 646, 1254, 1220), (1040, 910), "operator-serene.png"),
)


def alpha_bbox(width: int, height: int, pixels: list[Pixel], *, threshold: int = 1) -> Box:
    xs: list[int] = []
    ys: list[int] = []
    for index, pixel in enumerate(pixels):
        if pixel[3] >= threshold:
            xs.append(index % width)
            ys.append(index // width)
    if not xs:
        raise ValueError("no visible pixels in selection")
    return min(xs), min(ys), max(xs) + 1, max(ys) + 1


def crop_rgba(width: int, height: int, pixels: list[Pixel], box: Box) -> tuple[int, int, list[Pixel]]:
    left, top, right, bottom = box
    if left < 0 or top < 0 or right > width or bottom > height or left >= right or top >= bottom:
        raise ValueError(f"invalid crop box {box} for {width}x{height}")
    out: list[Pixel] = []
    for y in range(top, bottom):
        row = y * width
        out.extend(pixels[row + left : row + right])
    return right - left, bottom - top, out


def seeded_component_mask(
    width: int,
    height: int,
    pixels: list[Pixel],
    seed: Point,
    *,
    alpha_threshold: int,
) -> list[bool]:
    sx, sy = seed
    if not (0 <= sx < width and 0 <= sy < height):
        raise ValueError(f"seed {seed} outside {width}x{height}")
    start = sy * width + sx
    if pixels[start][3] < alpha_threshold:
        raise ValueError(f"seed {seed} is below alpha threshold {alpha_threshold}")

    mask = [False] * (width * height)
    stack = [start]
    mask[start] = True
    while stack:
        index = stack.pop()
        x = index % width
        y = index // width
        for dy in (-1, 0, 1):
            yy = y + dy
            if yy < 0 or yy >= height:
                continue
            base = yy * width
            for dx in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                xx = x + dx
                if xx < 0 or xx >= width:
                    continue
                neighbor = base + xx
                if not mask[neighbor] and pixels[neighbor][3] >= alpha_threshold:
                    mask[neighbor] = True
                    stack.append(neighbor)
    return mask


def dilate_mask(width: int, height: int, mask: list[bool], *, radius: int) -> list[bool]:
    out = mask[:]
    queue = [(index, 0) for index, selected in enumerate(mask) if selected]
    head = 0
    while head < len(queue):
        index, distance = queue[head]
        head += 1
        if distance >= radius:
            continue
        x = index % width
        y = index // width
        for dy in (-1, 0, 1):
            yy = y + dy
            if yy < 0 or yy >= height:
                continue
            base = yy * width
            for dx in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                xx = x + dx
                if xx < 0 or xx >= width:
                    continue
                neighbor = base + xx
                if not out[neighbor]:
                    out[neighbor] = True
                    queue.append((neighbor, distance + 1))
    return out


def selected_sticker_pixels(
    width: int,
    height: int,
    pixels: list[Pixel],
    seed: Point,
) -> tuple[int, int, list[Pixel]]:
    core = seeded_component_mask(width, height, pixels, seed, alpha_threshold=CORE_ALPHA_THRESHOLD)
    halo = dilate_mask(width, height, core, radius=HALO_RADIUS)
    selected = [is_core or (in_halo and pixels[index][3] >= HALO_ALPHA_THRESHOLD) for index, (is_core, in_halo) in enumerate(zip(core, halo))]

    masked = [pixel if selected[index] else (0, 0, 0, 0) for index, pixel in enumerate(pixels)]
    left, top, right, bottom = alpha_bbox(width, height, masked, threshold=1)
    return crop_rgba(width, height, masked, (left, top, right, bottom))


def pad_to_square(width: int, height: int, pixels: list[Pixel], *, margin: int) -> tuple[int, int, list[Pixel]]:
    size = max(width, height) + margin * 2
    out = [(0, 0, 0, 0)] * (size * size)
    ox = (size - width) // 2
    oy = (size - height) // 2
    for y in range(height):
        for x in range(width):
            out[(oy + y) * size + ox + x] = pixels[y * width + x]
    return size, size, out


def _blend_samples(samples: list[tuple[Pixel, float]]) -> Pixel:
    red = green = blue = alpha = 0.0
    total_weight = 0.0
    for pixel, weight in samples:
        if weight <= 0:
            continue
        a = pixel[3] / 255.0
        red += pixel[0] * a * weight
        green += pixel[1] * a * weight
        blue += pixel[2] * a * weight
        alpha += a * weight
        total_weight += weight
    if total_weight <= 0 or alpha <= 0:
        return (0, 0, 0, 0)
    alpha /= total_weight
    return (
        max(0, min(255, round(red / (alpha * total_weight)))),
        max(0, min(255, round(green / (alpha * total_weight)))),
        max(0, min(255, round(blue / (alpha * total_weight)))),
        max(0, min(255, round(alpha * 255))),
    )


def resize_rgba_premultiplied(width: int, height: int, pixels: list[Pixel], out_size: int) -> list[Pixel]:
    out: list[Pixel] = []
    x_scale = width / out_size
    y_scale = height / out_size
    for y in range(out_size):
        sy = (y + 0.5) * y_scale - 0.5
        y0 = int(sy)
        y1 = y0 + 1
        wy1 = sy - y0
        wy0 = 1.0 - wy1
        for x in range(out_size):
            sx = (x + 0.5) * x_scale - 0.5
            x0 = int(sx)
            x1 = x0 + 1
            wx1 = sx - x0
            wx0 = 1.0 - wx1
            samples: list[tuple[Pixel, float]] = []
            for xx, wx in ((x0, wx0), (x1, wx1)):
                if xx < 0 or xx >= width:
                    continue
                for yy, wy in ((y0, wy0), (y1, wy1)):
                    if yy < 0 or yy >= height:
                        continue
                    samples.append((pixels[yy * width + xx], wx * wy))
            out.append(_blend_samples(samples))
    return out


def assert_telegram_static_png(path: Path) -> None:
    if path.stat().st_size > MAX_STATIC_STICKER_BYTES:
        raise ValueError(f"{path} exceeds 512 KB")
    width, height, pixels = read_png(path)
    if width > CANVAS_SIZE or height > CANVAS_SIZE:
        raise ValueError(f"{path} exceeds 512x512")
    if CANVAS_SIZE not in (width, height):
        raise ValueError(f"{path} must have one side exactly 512")
    corners = [pixels[0], pixels[width - 1], pixels[(height - 1) * width], pixels[-1]]
    if any(pixel[3] != 0 for pixel in corners):
        raise ValueError(f"{path} corners are not transparent")
    edge_indices = (
        list(range(width))
        + list(range((height - 1) * width, height * width))
        + [y * width for y in range(height)]
        + [y * width + width - 1 for y in range(height)]
    )
    if any(pixels[index][3] != 0 for index in edge_indices):
        raise ValueError(f"{path} has nontransparent canvas-edge pixels")


def _paste(dest: list[Pixel], dest_width: int, src: list[Pixel], src_width: int, src_height: int, left: int, top: int) -> None:
    for y in range(src_height):
        for x in range(src_width):
            dest[(top + y) * dest_width + left + x] = src[y * src_width + x]


def write_contact_sheet(paths: list[Path], output: Path) -> None:
    cols = 3
    rows = 2
    gap = 24
    preview_size = 256
    width = cols * preview_size + (cols + 1) * gap
    height = rows * preview_size + (rows + 1) * gap
    pixels = [(245, 245, 245, 255)] * (width * height)
    for index, path in enumerate(paths):
        sticker_width, sticker_height, sticker_pixels = read_png(path)
        sticker_pixels = resize_rgba_premultiplied(sticker_width, sticker_height, sticker_pixels, preview_size)
        col = index % cols
        row = index // cols
        left = gap + col * (preview_size + gap)
        top = gap + row * (preview_size + gap)
        _paste(pixels, width, sticker_pixels, preview_size, preview_size, left, top)
    write_png(output, width, height, pixels)


def build_operator_assets(source: Path, output_dir: Path, *, contact_sheet: Path | None = None) -> list[Path]:
    if hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise ValueError(f"{source} does not match locked source SHA-256")
    sheet_width, sheet_height, sheet_pixels = read_png(source)
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for crop in OPERATOR_CROPS:
        rel_seed = (crop.seed[0] - crop.source_box[0], crop.seed[1] - crop.source_box[1])
        crop_width, crop_height, crop_pixels = crop_rgba(sheet_width, sheet_height, sheet_pixels, crop.source_box)
        selected_width, selected_height, selected_pixels = selected_sticker_pixels(crop_width, crop_height, crop_pixels, rel_seed)
        padded_width, padded_height, padded_pixels = pad_to_square(selected_width, selected_height, selected_pixels, margin=MARGIN)
        sticker_pixels = resize_rgba_premultiplied(padded_width, padded_height, padded_pixels, CANVAS_SIZE)
        output_path = output_dir / crop.output_name
        write_png(output_path, CANVAS_SIZE, CANVAS_SIZE, sticker_pixels)
        assert_telegram_static_png(output_path)
        outputs.append(output_path)
    if contact_sheet is not None:
        contact_sheet.parent.mkdir(parents=True, exist_ok=True)
        write_contact_sheet(outputs, contact_sheet)
    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--contact-sheet", type=Path)
    args = parser.parse_args(argv)

    outputs = build_operator_assets(args.source, args.output_dir, contact_sheet=args.contact_sheet)
    for output in outputs:
        print(output)
    if args.contact_sheet:
        print(args.contact_sheet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
