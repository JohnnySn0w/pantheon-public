"""Tests for _tree_walk.walk_paths and the vanish-mid-walk regression (T-4450).

    python3 -m unittest tools.test_tree_walk -v

The vanish is reproduced deterministically: os.scandir is wrapped so that a
chosen directory is removed just before it is scanned, i.e. after its parent
listed it. That is the exact window a sibling xdist test's TemporaryDirectory
cleanup hits during a live-tree rglob.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from fnmatch import fnmatchcase
from pathlib import Path
from unittest.mock import patch

import tools.pgrep_predicate_lint as ppl
from tools._tree_walk import walk_paths

_REAL_SCANDIR = os.scandir


@contextmanager
def vanish_on_scan(victim: Path):
    """Remove ``victim`` the first time anything tries to scandir it."""
    victim = Path(victim)

    def scandir(path="."):
        # rmtree itself scandirs by fd (an int); only path arguments can match.
        if not isinstance(path, int) and Path(os.fsdecode(path)) == victim and victim.exists():
            shutil.rmtree(victim)
        return _REAL_SCANDIR(path)

    with patch("os.scandir", scandir):
        yield


def _rglob_py311(self: Path, pattern: str):
    """Model of py3.11 ``Path.rglob``: recursive scandir that lets a
    FileNotFoundError from a vanished directory escape the generator (3.11
    swallows only PermissionError; 3.12+ walks via the tolerant Path.walk, so
    the bug only reproduced on the nightly py3.11 leg). Patched over
    ``Path.rglob`` so the regression goes red on any interpreter."""

    def dirs(p: Path):
        yield p
        with os.scandir(p) as it:
            entries = list(it)
        for e in entries:
            if e.is_dir() and not e.is_symlink():
                yield from dirs(p / e.name)

    for d in dirs(self):
        with os.scandir(d) as it:
            entries = list(it)
        for e in entries:
            if fnmatchcase(e.name, pattern):
                yield d / e.name


class _TreeCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def _write(self, rel: str, text: str = "x\n") -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p


class TestWalkPathsMatchesRglob(_TreeCase):
    """On a stable tree walk_paths must equal sorted(rglob(pattern))."""

    def _build(self) -> None:
        self._write("a.md")
        self._write("b.sh")
        self._write(".hidden/c.md")
        self._write("d/e/f.md")
        self._write("d/e/g.py")
        self._write("dir.md/inner.md")  # a directory whose name matches *.md
        (self.root / "empty").mkdir()
        (self.root / "link-to-d").symlink_to(self.root / "d", target_is_directory=True)
        (self.root / "link-to-a.md").symlink_to(self.root / "a.md")
        (self.root / "dangling.md").symlink_to(self.root / "nowhere.md")

    def test_star(self) -> None:
        self._build()
        self.assertEqual(walk_paths(self.root), sorted(self.root.rglob("*")))

    def test_suffix_pattern(self) -> None:
        self._build()
        self.assertEqual(walk_paths(self.root, "*.md"), sorted(self.root.rglob("*.md")))

    def test_missing_root_is_empty(self) -> None:
        self.assertEqual(walk_paths(self.root / "absent"), [])


class TestVanishMidWalk(_TreeCase):
    def _build(self) -> Path:
        self._write("keep/a.sh", 'pgrep -f "pat"\n')
        self._write("tmpgone/nested/b.sh", 'pgrep -f "pat"\n')
        self._write("z/c.sh", 'pgrep -f "pat"\n')
        return self.root / "tmpgone"

    def test_walk_paths_survives_vanished_dir(self) -> None:
        victim = self._build()
        with vanish_on_scan(victim):
            got = walk_paths(self.root)
        rel = [p.relative_to(self.root).as_posix() for p in got]
        self.assertEqual(rel, ["keep", "keep/a.sh", "tmpgone", "z", "z/c.sh"])

    def test_py311_rglob_model_raises_on_vanish(self) -> None:
        """Guards the model: without it the pgrep regression would be hollow."""
        victim = self._build()
        self.assertEqual(sorted(_rglob_py311(self.root, "*")), sorted(self.root.rglob("*")))
        with vanish_on_scan(victim):
            with self.assertRaises(FileNotFoundError):
                list(_rglob_py311(self.root, "*"))

    def test_pgrep_scan_survives_vanished_dir(self) -> None:
        """The T-4450 nightly failure: scan() aborted with FileNotFoundError."""
        victim = self._build()
        with vanish_on_scan(victim), patch.object(Path, "rglob", _rglob_py311):
            findings = ppl.scan(self.root, allowlist=())
        self.assertEqual([f[0] for f in findings], ["keep/a.sh", "z/c.sh"])

    def test_other_oserror_still_raises(self) -> None:
        """Only vanish/permission-class errors are tolerated."""
        self._build()

        def scandir(path="."):
            raise OSError(5, "EIO")

        with patch("os.scandir", scandir):
            with self.assertRaises(OSError):
                walk_paths(self.root)


if __name__ == "__main__":
    unittest.main()
