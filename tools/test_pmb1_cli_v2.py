#!/usr/bin/env python3
"""PMB-1 CLI on the v2 fixture (T-2518 step 2a/2b): version=2 in the spec, per-seed runs, the stub-embedder declaration."""
from __future__ import annotations

import contextlib
import glob
import io
import os
import tempfile
import unittest

from tools.pmb1 import cli, offline

_GUARD = None


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install()


def tearDownModule() -> None:
    _GUARD.close()


def call(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


RUN = ["run", "--substrate", "rag,random,full,amnesiac", "--backbone", "stub,stub-lenient",
       "--fixture", "seed=1,size=tiny,version=2", "--seeds", "1,2,3", "--gate"]


class TestCliV2(unittest.TestCase):
    def test_a_v2_gate_run_without_the_stub_declaration_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            rc, _, err = call(RUN + ["--out", out])
        self.assertEqual(rc, 2)
        self.assertIn("lexical stub", err)

    def test_declared_run_writes_one_dir_per_seed_and_the_gate_reads_them(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            rc, stdout, _ = call(RUN + ["--lexical-stub-embedder", "--out", out])
            self.assertEqual(rc, 0)
            self.assertIn("fixture seed=3,size=tiny,version=2", stdout)
            self.assertIn("A2", stdout)  # v2 reports A2 / A7 / E4 as not run
            files = sorted(glob.glob(os.path.join(out, "seed-*", "*__*.json")))
            self.assertEqual(len(files), 3 * 8)
            rc, gout, _ = call(["gate", "--tier", "1", "--candidate", "verbatim-rag"] + files)
            self.assertEqual(rc, 1)  # rag is a control, not a candidate: refused by name
            self.assertIn("candidate-present", gout)

    def test_the_default_spec_is_still_v1(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            rc, stdout, _ = call(["run", "--substrate", "rag", "--backbone", "stub", "--fixture", "seed=1,size=tiny", "--out", out])
        self.assertEqual(rc, 0)
        self.assertIn("fixture seed=1,size=tiny,version=1", stdout)


if __name__ == "__main__":
    unittest.main()
