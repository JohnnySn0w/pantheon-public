#!/usr/bin/env python3
"""Unit tests for tools/mirror_state.py (T-1994).

Run: python3 -m unittest tools.test_mirror_state
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import mirror_state as ms


class TestLoadSave(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "state.yaml"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_is_empty_state(self):
        state = ms.load_state(self.path)
        self.assertEqual(state, {"version": ms.STATE_VERSION, "pairs": {}})

    def test_roundtrip_save_load(self):
        state = ms._empty_state()
        ms.record_published(state, "pwa-release-mirror", "a" * 40, by="orchestrator")
        ms.save_state(state, self.path)
        self.assertTrue(self.path.exists())
        reloaded = ms.load_state(self.path)
        self.assertEqual(
            reloaded["pairs"]["pwa-release-mirror"]["source_head_at_last_publish"],
            "a" * 40,
        )

    def test_malformed_yaml_raises(self):
        self.path.write_text("::: not valid : : yaml : [", encoding="utf-8")
        with self.assertRaises(ms.MirrorStateError):
            ms.load_state(self.path)

    def test_non_mapping_raises(self):
        self.path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with self.assertRaises(ms.MirrorStateError):
            ms.load_state(self.path)

    def test_empty_file_is_empty_state(self):
        self.path.write_text("", encoding="utf-8")
        state = ms.load_state(self.path)
        self.assertEqual(state["pairs"], {})

    def test_save_is_atomic_no_tmp_left(self):
        state = ms._empty_state()
        ms.save_state(state, self.path)
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])


class TestRecordPublished(unittest.TestCase):
    def test_sets_anchor_and_metadata(self):
        state = ms._empty_state()
        entry = ms.record_published(
            state, "claude-harness-mirror", "8af520d66da09180d77d54db88db612fd5208205",
            mirror_head="2e8bd87", by="orchestrator", now_iso="2026-07-23T00:00:00Z",
        )
        self.assertEqual(entry["source_head_at_last_publish"],
                         "8af520d66da09180d77d54db88db612fd5208205")
        self.assertEqual(entry["published_mirror_head"], "2e8bd87")
        self.assertEqual(entry["published_by"], "orchestrator")
        self.assertEqual(entry["published_at"], "2026-07-23T00:00:00Z")

    def test_clears_matching_staged_bundle(self):
        state = ms._empty_state()
        ms.record_staged(state, "codex-harness-mirror", "b" * 40)
        self.assertIsNotNone(state["pairs"]["codex-harness-mirror"]["staged_bundle"])
        ms.record_published(state, "codex-harness-mirror", "b" * 40, by="orchestrator")
        # staged head == published head -> cleared.
        self.assertIsNone(state["pairs"]["codex-harness-mirror"]["staged_bundle"])

    def test_leaves_nonmatching_staged_bundle(self):
        state = ms._empty_state()
        ms.record_staged(state, "codex-harness-mirror", "c" * 40)  # newer build
        ms.record_published(state, "codex-harness-mirror", "d" * 40, by="orchestrator")  # older publish
        # A staged build for a DIFFERENT (newer) head survives — it's a real
        # built-but-unpushed condition the sweep should still flag.
        self.assertEqual(
            state["pairs"]["codex-harness-mirror"]["staged_bundle"]["source_head_at_build"],
            "c" * 40,
        )

    def test_unknown_slug_rejected(self):
        state = ms._empty_state()
        with self.assertRaises(ms.MirrorStateError):
            ms.record_published(state, "not-a-real-slug", "a" * 40)

    def test_bad_sha_rejected(self):
        state = ms._empty_state()
        with self.assertRaises(ms.MirrorStateError):
            ms.record_published(state, "pwa-release-mirror", "nothex!!")
        with self.assertRaises(ms.MirrorStateError):
            ms.record_published(state, "pwa-release-mirror", "abc")  # too short

    def test_sha_normalized_lowercase(self):
        state = ms._empty_state()
        entry = ms.record_published(state, "pwa-release-mirror", "ABCDEF1234")
        self.assertEqual(entry["source_head_at_last_publish"], "abcdef1234")


class TestRecordStaged(unittest.TestCase):
    def test_records_staged_bundle(self):
        state = ms._empty_state()
        entry = ms.record_staged(
            state, "pwa-release-mirror", "e" * 40,
            bundle_path="/tmp/pwa.bundle", now_iso="2026-07-23T01:00:00Z",
        )
        sb = entry["staged_bundle"]
        self.assertEqual(sb["source_head_at_build"], "e" * 40)
        self.assertEqual(sb["bundle_path"], "/tmp/pwa.bundle")
        self.assertEqual(sb["built_at"], "2026-07-23T01:00:00Z")

    def test_clear_staged(self):
        state = ms._empty_state()
        ms.record_staged(state, "pwa-release-mirror", "f" * 40)
        ms.clear_staged(state, "pwa-release-mirror")
        self.assertIsNone(state["pairs"]["pwa-release-mirror"]["staged_bundle"])


class TestCLI(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "state.yaml"

    def tearDown(self):
        self._tmp.cleanup()

    def test_cli_record_and_get(self):
        rc = ms.main([
            "--state-path", str(self.path), "record-published",
            "pwa-release-mirror", "--source-head", "a" * 40, "--by", "orchestrator",
        ])
        self.assertEqual(rc, 0)
        reloaded = ms.load_state(self.path)
        self.assertEqual(
            reloaded["pairs"]["pwa-release-mirror"]["source_head_at_last_publish"],
            "a" * 40,
        )

    def test_cli_unknown_slug_exit_2(self):
        rc = ms.main([
            "--state-path", str(self.path), "record-published",
            "bogus", "--source-head", "a" * 40,
        ])
        self.assertEqual(rc, 2)

    def test_cli_payload_tree_roundtrip(self):
        base = ["--state-path", str(self.path)]
        self.assertEqual(ms.main(base + ["record-staged", "codex-harness-mirror",
                                         "--source-head", "b" * 40,
                                         "--payload-tree", "1" * 40]), 0)
        st = ms.load_state(self.path)["pairs"]["codex-harness-mirror"]
        self.assertEqual(st["staged_bundle"]["payload_tree"], "1" * 40)
        self.assertEqual(ms.main(base + ["record-published", "codex-harness-mirror",
                                         "--source-head", "c" * 40,
                                         "--payload-tree", "1" * 40]), 0)
        st = ms.load_state(self.path)["pairs"]["codex-harness-mirror"]
        self.assertEqual(st["published_payload_tree"], "1" * 40)
        self.assertIsNone(st["staged_bundle"])  # cleared by tree, not by source head

    def test_cli_bad_payload_tree_exit_2(self):
        for cmd in (["record-published", "codex-harness-mirror", "--source-head", "a" * 40],
                    ["record-staged", "codex-harness-mirror", "--source-head", "a" * 40],
                    ["rebase-anchor", "codex-harness-mirror", "--source-head", "a" * 40]):
            rc = ms.main(["--state-path", str(self.path), *cmd, "--payload-tree", "nothex!!"])
            self.assertEqual(rc, 2, cmd)
        self.assertFalse(self.path.exists())  # nothing written on a refusal

    def test_cli_rebase_anchor(self):
        base = ["--state-path", str(self.path)]
        ms.main(base + ["record-published", "claude-harness-mirror", "--source-head", "a" * 40,
                        "--mirror-head", "6" * 40, "--by", "orchestrator"])
        before = dict(ms.load_state(self.path)["pairs"]["claude-harness-mirror"])
        rc = ms.main(base + ["rebase-anchor", "claude-harness-mirror", "--source-head", "d" * 40,
                             "--payload-tree", "2" * 40, "--match", "allowlisted"])
        self.assertEqual(rc, 0)
        e = ms.load_state(self.path)["pairs"]["claude-harness-mirror"]
        self.assertEqual(e["source_head_at_last_publish"], "d" * 40)
        self.assertEqual(e["published_payload_tree"], "2" * 40)
        self.assertEqual(e["rebased_from"]["source_head"], "a" * 40)
        self.assertEqual(e["rebased_from"]["match"], "allowlisted")
        for k in ("published_at", "published_mirror_head", "published_by"):
            self.assertEqual(e[k], before[k], k)  # a rebase is not a publish

    def test_cli_rebase_anchor_bad_match_rejected(self):
        with self.assertRaises(SystemExit):
            ms.main(["--state-path", str(self.path), "rebase-anchor", "claude-harness-mirror",
                     "--source-head", "d" * 40, "--payload-tree", "2" * 40, "--match", "close"])


class TestPayloadTree(unittest.TestCase):
    def test_publish_without_tree_nulls_an_old_tree(self):
        state = ms._empty_state()
        ms.record_published(state, "codex-harness-mirror", "a" * 40, payload_tree="1" * 40)
        ms.record_published(state, "codex-harness-mirror", "b" * 40)
        self.assertIsNone(state["pairs"]["codex-harness-mirror"]["published_payload_tree"])

    def test_legacy_publish_gains_no_tree_key(self):
        state = ms._empty_state()
        e = ms.record_published(state, "pwa-release-mirror", "a" * 40)
        self.assertNotIn("published_payload_tree", e)

    def test_publish_clears_rebase_and_matching_staged_tree(self):
        state = ms._empty_state()
        ms.rebase_anchor(state, "codex-harness-mirror", "a" * 40, "1" * 40)
        ms.record_staged(state, "codex-harness-mirror", "b" * 40, payload_tree="3" * 40)
        ms.record_published(state, "codex-harness-mirror", "c" * 40, payload_tree="3" * 40)
        e = state["pairs"]["codex-harness-mirror"]
        self.assertNotIn("rebased_from", e)
        self.assertIsNone(e["staged_bundle"])

    def test_nonmatching_staged_tree_survives(self):
        state = ms._empty_state()
        ms.record_staged(state, "codex-harness-mirror", "b" * 40, payload_tree="3" * 40)
        ms.record_published(state, "codex-harness-mirror", "c" * 40, payload_tree="4" * 40)
        self.assertIsNotNone(state["pairs"]["codex-harness-mirror"]["staged_bundle"])


if __name__ == "__main__":
    unittest.main()
