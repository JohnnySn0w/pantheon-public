"""Tests for memory_lint_sweep.py + memory_store_teardown.py (T-2888)."""
import tarfile
import tempfile
import unittest
from pathlib import Path

from tools import memory_lint_sweep as sweep_mod
from tools import memory_store_teardown as td

TODAY_ORD = 2026 * 372 + 8 * 31 + 12

GOOD_PIN = """---
name: reference_example_fact
description: an example fact for tests
metadata:
  type: reference
---

The example fact body.
"""

BROKEN_PIN = "no frontmatter at all\n"


def _mk_store(root: Path, slug: str, pins: dict[str, str]) -> Path:
    store = root / slug / "memory"
    store.mkdir(parents=True)
    for fname, content in pins.items():
        (store / fname).write_text(content, encoding="utf-8")
    index = "\n".join(f"- [{n}]({n})" for n in pins if n != "MEMORY.md")
    (store / "MEMORY.md").write_text(index + "\n", encoding="utf-8")
    return store


class SweepTest(unittest.TestCase):
    def test_all_clean_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mk_store(root, "proj-a", {"reference_example_fact.md": GOOD_PIN})
            self.assertEqual(sweep_mod.sweep(root, 90, TODAY_ORD, strict=False), 0)

    def test_error_in_any_store_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mk_store(root, "proj-a", {"reference_example_fact.md": GOOD_PIN})
            _mk_store(root, "proj-b", {"reference_broken.md": BROKEN_PIN})
            self.assertEqual(sweep_mod.sweep(root, 90, TODAY_ORD, strict=False), 1)

    def test_no_stores_is_a_failure_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sweep_mod.sweep(Path(tmp), 90, TODAY_ORD, strict=False), 1)


class TeardownTest(unittest.TestCase):
    def test_archives_then_deletes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _mk_store(root, "proj-a", {"reference_example_fact.md": GOOD_PIN})
            archive_root = root / "archives"
            archive = td.teardown(store, archive_root)
            self.assertTrue(archive.is_file())
            self.assertFalse(store.exists())
            with tarfile.open(archive) as tf:
                names = tf.getnames()
            self.assertIn("proj-a/memory/reference_example_fact.md", names)

    def test_refuses_non_store_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "not-a-store"
            plain.mkdir()
            (plain / "random.txt").write_text("hi", encoding="utf-8")
            rc = td.main([str(plain), "--yes", "--archive-root", str(Path(tmp) / "a")])
            self.assertEqual(rc, 2)
            self.assertTrue(plain.exists())  # nothing deleted

    def test_refuses_without_yes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _mk_store(root, "proj-a", {"reference_example_fact.md": GOOD_PIN})
            rc = td.main([str(store), "--archive-root", str(root / "a")])
            self.assertEqual(rc, 2)
            self.assertTrue(store.exists())


if __name__ == "__main__":
    unittest.main()
