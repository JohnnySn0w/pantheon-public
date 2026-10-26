#!/usr/bin/env python3
"""PMB-1 CLI (T-2518): ``python3 -m tools.pmb1 run`` and ``... gate --tier 1``."""
from __future__ import annotations

import contextlib
import glob
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.pmb1 import cli, offline

REPO = Path(__file__).resolve().parent.parent


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


class TestCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = cls._tmp.name
        cls.rc, cls.stdout, _ = call([
            "run", "--substrate", "rag,random,full,amnesiac", "--backbone", "stub,stub-lenient",
            "--fixture", "seed=1,size=tiny", "--gate", "--out", cls.out,
        ])

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_run_writes_one_record_per_pair_and_lists_the_not_run_tracks(self) -> None:
        self.assertEqual(self.rc, 0)
        files = glob.glob(os.path.join(self.out, "*__*.json"))
        self.assertEqual(len(files), 8)
        self.assertIn("not run (named stubs): A4", self.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.out, "rank_stability.json")))

    def test_gate_without_a_candidate_names_the_failing_criterion(self) -> None:
        rc, out, _ = call(["gate", "--tier", "1", *sorted(glob.glob(os.path.join(self.out, "*__*.json")))])
        self.assertEqual(rc, 1)
        self.assertIn("FAIL", out)
        self.assertIn("candidate-present", out)

    def test_gate_json_output_has_every_criterion(self) -> None:
        rc, out, _ = call(["gate", "--tier", "1", "--json", *sorted(glob.glob(os.path.join(self.out, "*__*.json")))])
        d = json.loads(out)
        self.assertFalse(d["passed"])
        self.assertTrue(all({"name", "passed", "detail"} <= set(c) for c in d["criteria"]))

    def test_gate_for_an_unbuilt_tier_is_refused(self) -> None:
        rc, _, err = call(["gate", "--tier", "3", *glob.glob(os.path.join(self.out, "*__*.json"))])
        self.assertEqual(rc, 2)
        self.assertIn("tiers 1 and 2 only", err)  # tier 2 exists since T-2518 tier 2 (fixture v4)

    def test_one_family_gate_run_is_refused_with_exit_2(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rc, _, err = call(["run", "--substrate", "rag", "--backbone", "stub", "--fixture", "seed=1,size=tiny",
                               "--gate", "--out", d])
            self.assertEqual(rc, 2)
            self.assertIn("2 different families", err)
            self.assertEqual(glob.glob(os.path.join(d, "*.json")), [])

    def test_make_backbone_specs(self) -> None:
        self.assertEqual(cli.make_backbone("stub").family, "stub-strict")
        self.assertEqual(cli.make_backbone("stub-lenient").family, "stub-lenient")
        with self.assertRaises(ValueError):
            cli.make_backbone("claude")

    def test_module_entrypoint_runs_from_the_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run(
                [sys.executable, "-m", "tools.pmb1", "run", "--substrate", "amnesiac", "--backbone", "stub",
                 "--fixture", "seed=1,size=tiny", "--out", d],
                cwd=REPO, capture_output=True, text=True, timeout=120,
                env={k: v for k, v in os.environ.items() if k != "PMB1_OLLAMA_URL"},
            )
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("amnesiac-floor", p.stdout)


if __name__ == "__main__":
    unittest.main()
