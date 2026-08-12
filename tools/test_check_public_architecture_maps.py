"""Known-positive tests for public architecture derivative drift checks."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools import check_public_architecture_maps as checker


def _scene() -> dict:
    return {
        "type": "excalidraw",
        "version": 2,
        "elements": [
            {
                "id": "left",
                "type": "rectangle",
                "boundElements": [
                    {"id": "label", "type": "text"},
                    {"id": "flow", "type": "arrow"},
                ],
            },
            {
                "id": "label",
                "type": "text",
                "containerId": "left",
                "text": "Relationship Interface",
                "originalText": "Relationship Interface",
            },
            {
                "id": "right",
                "type": "rectangle",
                "boundElements": [{"id": "flow", "type": "arrow"}],
            },
            {
                "id": "flow",
                "type": "arrow",
                "startBinding": {"elementId": "left"},
                "endBinding": {"elementId": "right"},
            },
        ],
    }


class PublicArchitectureCheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.canonical = self.root / "canonical.excalidraw"
        self.public = self.root / "public.excalidraw"
        self.manifest = self.root / "manifest.json"
        self._write_json(self.canonical, _scene())
        self._write_json(self.public, _scene())

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")

    def _sha(self) -> str:
        return hashlib.sha256(self.canonical.read_bytes()).hexdigest()

    def _spec(self) -> dict:
        return {
            "id": "known-positive",
            "canonical": self.canonical.name,
            "canonical_sha256": self._sha(),
            "public": self.public.name,
            "text_classification": {"label": "translated"},
            "topology_exceptions": [],
        }

    def _write_manifest(self, spec: dict | None = None) -> None:
        self._write_json(
            self.manifest,
            {
                "version": 1,
                "forbidden_visible_patterns": [r"\bEcho\b"],
                "views": [spec or self._spec()],
            },
        )

    def _findings(self, spec: dict | None = None) -> list[str]:
        self._write_manifest(spec)
        return checker.validate_all(self.manifest, self.root)

    def test_green_fixture(self) -> None:
        self.assertEqual(self._findings(), [])

    def test_stale_source_hash_is_red(self) -> None:
        spec = self._spec()
        spec["canonical_sha256"] = "0" * 64
        self.assertTrue(any("source drift" in item for item in self._findings(spec)))

    def test_missing_classification_is_red(self) -> None:
        spec = self._spec()
        spec["text_classification"] = {}
        self.assertTrue(any("unclassified" in item for item in self._findings(spec)))

    def test_missing_public_visible_text_id_is_red(self) -> None:
        public = _scene()
        public["elements"] = [item for item in public["elements"] if item["id"] != "label"]
        self._write_json(self.public, public)
        self.assertTrue(any("visible-text ID missing" in item for item in self._findings()))

    def test_forbidden_vocabulary_is_red(self) -> None:
        public = _scene()
        label = next(item for item in public["elements"] if item["id"] == "label")
        label["text"] = label["originalText"] = "Echo"
        self._write_json(self.public, public)
        self.assertTrue(any("forbidden internal vocabulary" in item for item in self._findings()))

    def test_malformed_view_forbidden_patterns_is_red(self) -> None:
        spec = self._spec()
        spec["forbidden_visible_patterns"] = "not-an-array"
        self.assertTrue(
            any(
                "forbidden_visible_patterns must be an array of strings" in item
                for item in self._findings(spec)
            )
        )

    def test_arrow_binding_drift_is_red(self) -> None:
        public = _scene()
        arrow = next(item for item in public["elements"] if item["id"] == "flow")
        arrow["endBinding"] = {"elementId": "left"}
        self._write_json(self.public, public)
        self.assertTrue(any("arrow binding drift" in item for item in self._findings()))

    def test_container_binding_drift_is_red(self) -> None:
        public = _scene()
        label = next(item for item in public["elements"] if item["id"] == "label")
        label["containerId"] = "right"
        self._write_json(self.public, public)
        self.assertTrue(any("container binding drift" in item for item in self._findings()))

    def test_text_original_text_divergence_is_red(self) -> None:
        public = _scene()
        label = next(item for item in public["elements"] if item["id"] == "label")
        label["originalText"] = "stale"
        self._write_json(self.public, public)
        self.assertTrue(any("text/originalText diverge" in item for item in self._findings()))

    def test_invalid_json_load_is_red(self) -> None:
        self.public.write_text("{not-json", encoding="utf-8")
        self.assertTrue(any("scene load failed" in item for item in self._findings()))

    def test_topology_exception_is_explicit_and_bounded(self) -> None:
        public = _scene()
        arrow = next(item for item in public["elements"] if item["id"] == "flow")
        arrow["endBinding"] = {"elementId": "left"}
        self._write_json(self.public, public)

        rejected = self._findings()
        self.assertTrue(any("arrow binding drift" in item for item in rejected))
        allowed = self._spec()
        allowed["topology_exceptions"] = ["flow"]
        self.assertEqual(self._findings(allowed), [])


class RealTreePublicArchitectureGuard(unittest.TestCase):
    def test_committed_public_derivatives_match_canon(self) -> None:
        self.assertEqual(
            checker.validate_all(checker.DEFAULT_MANIFEST, checker.REPO_ROOT),
            [],
        )


if __name__ == "__main__":
    unittest.main()
