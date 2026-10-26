"""Tests for the operator's standalone sticker assets and mint path (T-4219)."""

from __future__ import annotations

import copy
import json
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.mint_telegram_sticker_set as mint
from tools.prepare_operator_sticker_assets import CORE_ALPHA_THRESHOLD, MAX_STATIC_STICKER_BYTES, SOURCE_SHA256
from tools.postprocess_ai_sticker_png import read_png


REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "assets" / "stickers" / "manifest.json"
STICKERS = REPO / "assets" / "stickers"
EXPECTED_MAPPING = [
    ("thoughtful", "\U0001F914"),
    ("knowing", "\U0001F60F"),
    ("wanderer", "\U0001F9ED"),
    ("focus", "\U0001F4BB"),
    ("tangled", "\U0001F635\u200d\U0001F4AB"),
    ("serene", "\U0001F54A\ufe0f"),
]


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _png_color_type(path: Path) -> int:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"{path} is not a PNG")
    length = struct.unpack(">I", data[8:12])[0]
    kind = data[12:16]
    if kind != b"IHDR" or length != 13:
        raise AssertionError(f"{path} has invalid PNG header")
    return data[25]


def _meaningful_alpha_components(width: int, height: int, pixels: list[tuple[int, int, int, int]]) -> list[tuple[int, tuple[int, int, int, int]]]:
    mask = [pixel[3] >= CORE_ALPHA_THRESHOLD for pixel in pixels]
    seen = [False] * len(mask)
    components: list[tuple[int, tuple[int, int, int, int]]] = []
    for start, selected in enumerate(mask):
        if not selected or seen[start]:
            continue
        stack = [start]
        seen[start] = True
        count = 0
        min_x = width
        min_y = height
        max_x = 0
        max_y = 0
        while stack:
            index = stack.pop()
            x = index % width
            y = index // width
            count += 1
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x)
            max_y = max(max_y, y)
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
                    if mask[neighbor] and not seen[neighbor]:
                        seen[neighbor] = True
                        stack.append(neighbor)
        components.append((count, (min_x, min_y, max_x + 1, max_y + 1)))
    return components


class OperatorStickerAssets(unittest.TestCase):
    def test_operator_sticker_assets_conform(self) -> None:
        manifest = _manifest()
        source = STICKERS / manifest["operator"]["source"]["file"]
        self.assertTrue(source.exists(), source)
        self.assertEqual(manifest["operator"]["source"]["sha256"], SOURCE_SHA256)

        for sticker in manifest["operator"]["stickers"]:
            path = STICKERS / sticker["file"]
            self.assertTrue(path.exists(), path)
            self.assertLessEqual(path.stat().st_size, MAX_STATIC_STICKER_BYTES, path)
            self.assertEqual(_png_color_type(path), 6, path)
            width, height, pixels = read_png(path)
            self.assertLessEqual(width, 512, path)
            self.assertLessEqual(height, 512, path)
            self.assertIn(512, (width, height), path)
            corners = [pixels[0], pixels[width - 1], pixels[(height - 1) * width], pixels[-1]]
            self.assertTrue(all(pixel[3] == 0 for pixel in corners), path)
            top = range(width)
            bottom = range((height - 1) * width, height * width)
            left = (y * width for y in range(height))
            right = (y * width + width - 1 for y in range(height))
            edge_indices = list(top) + list(bottom) + list(left) + list(right)
            self.assertFalse(any(pixels[index][3] != 0 for index in edge_indices), path)
            if sticker["reaction"] == "serene":
                components = _meaningful_alpha_components(width, height, pixels)
                self.assertEqual(len(components), 1, (path, components))
                occupied_rows = []
                for y in range(height):
                    xs = [x for x in range(width) if pixels[y * width + x][3] >= CORE_ALPHA_THRESHOLD]
                    if xs:
                        occupied_rows.append((y, len(xs), min(xs), max(xs)))
                first_rows = occupied_rows[:10]
                self.assertTrue(first_rows, "serene has no meaningful alpha")
                self.assertFalse(
                    any(count >= 240 or min_x < 140 for _y, count, min_x, _max_x in first_rows),
                    ("serene carries a broad detached/clipped top band", first_rows),
                )

    def test_operator_manifest_provenance_and_mapping(self) -> None:
        operator = _manifest()["operator"]
        self.assertEqual(operator.get("set_name_prefix"), "operator_after_midnight")
        self.assertEqual(operator["title"], "the operator After Midnight")
        self.assertEqual(operator["source"], {
            "file": "operator/operator-source-sheet.png",
            "sha256": SOURCE_SHA256,
        })
        self.assertEqual([(s["reaction"], s["emoji"]) for s in operator["stickers"]], EXPECTED_MAPPING)
        ids = [(s["file_id"], s["file_unique_id"]) for s in operator["stickers"]]
        if operator["set_name"]:
            self.assertRegex(
                operator["set_name"],
                r"^operator_after_midnight_by_[A-Za-z0-9_]+$",
            )
            self.assertTrue(all(file_id and unique_id for file_id, unique_id in ids))
        else:
            self.assertTrue(all(not file_id and not unique_id for file_id, unique_id in ids))


class OperatorMintPath(unittest.TestCase):
    def test_standalone_mint_never_targets_pantheon(self) -> None:
        manifest = {
            "orchestrator": {
                "set_name": "pantheon_pack_by_example_bot",
                "stickers": [{"reaction": "approved", "file": "orchestrator/orchestrator-approved.png", "file_id": "ORCHESTRATOR"}],
            },
            "specialist": {
                "set_name": "pantheon_pack_by_example_bot",
                "stickers": [{"reaction": "thinking", "file": "specialist/specialist-thinking.png", "file_id": "SPECIALIST"}],
            },
            "operator": {
                "set_name": "",
                "set_name_prefix": "operator_after_midnight",
                "title": "the operator After Midnight",
                "source": {"file": "operator/operator-source-sheet.png", "sha256": SOURCE_SHA256},
                "stickers": [
                    {
                        "reaction": reaction,
                        "emoji": emoji,
                        "file": f"operator/operator-{reaction}.png",
                        "file_id": "",
                        "file_unique_id": "",
                    }
                    for reaction, emoji in EXPECTED_MAPPING
                ],
            },
        }
        non_operator_before = {key: copy.deepcopy(value) for key, value in manifest.items() if key != "operator"}
        calls: list[tuple[str, dict, dict]] = []

        def fake_call(token, method, fields=None, files=None):
            calls.append((method, dict(fields or {}), dict(files or {})))
            if method == "getMe":
                return {"ok": True, "result": {"username": "example_bot"}}
            if method == "uploadStickerFile":
                return {"ok": True, "result": {"file_id": f"UPLOADED_{len([c for c in calls if c[0] == 'uploadStickerFile'])}"}}
            if method == "createNewStickerSet":
                return {"ok": True, "result": True}
            if method == "getStickerSet":
                return {
                    "ok": True,
                    "result": {
                        "stickers": [
                            {"file_id": f"FILE_{index}", "file_unique_id": f"UNIQUE_{index}"}
                            for index, _mapping in enumerate(EXPECTED_MAPPING, start=1)
                        ]
                    },
                }
            self.fail(f"unexpected Bot API method: {method}")

        with mock.patch.object(mint, "call", side_effect=fake_call):
            link = mint.mint_operator_standalone(manifest, token="TOKEN", owner="424242424")

        self.assertEqual(link, "https://t.me/addstickers/operator_after_midnight_by_example_bot")
        self.assertEqual([method for method, _fields, _files in calls], [
            "getMe",
            "uploadStickerFile",
            "uploadStickerFile",
            "uploadStickerFile",
            "uploadStickerFile",
            "uploadStickerFile",
            "uploadStickerFile",
            "createNewStickerSet",
            "getStickerSet",
        ])
        forbidden = {"addStickerToSet", "replaceStickerInSet", "deleteStickerFromSet", "setStickerPositionInSet"}
        self.assertTrue(forbidden.isdisjoint({method for method, _fields, _files in calls}))
        serialized_calls = json.dumps(calls, default=str, ensure_ascii=False)
        self.assertNotIn("pantheon_pack_by_", serialized_calls)

        create_fields = [fields for method, fields, _files in calls if method == "createNewStickerSet"][0]
        self.assertEqual(create_fields["user_id"], "424242424")
        self.assertEqual(create_fields["name"], "operator_after_midnight_by_example_bot")
        self.assertEqual(create_fields["title"], "the operator After Midnight")
        self.assertEqual(create_fields["sticker_type"], "regular")
        input_stickers = json.loads(create_fields["stickers"])
        self.assertEqual([s["format"] for s in input_stickers], ["static"] * 6)
        self.assertEqual([s["emoji_list"] for s in input_stickers], [[emoji] for _reaction, emoji in EXPECTED_MAPPING])

        self.assertEqual({key: value for key, value in manifest.items() if key != "operator"}, non_operator_before)
        self.assertEqual(manifest["operator"]["set_name"], "operator_after_midnight_by_example_bot")
        self.assertEqual([s["file_id"] for s in manifest["operator"]["stickers"]], [f"FILE_{i}" for i in range(1, 7)])
        self.assertEqual([s["file_unique_id"] for s in manifest["operator"]["stickers"]], [f"UNIQUE_{i}" for i in range(1, 7)])


if __name__ == "__main__":
    unittest.main()
