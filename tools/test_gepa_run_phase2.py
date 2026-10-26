"""Tests for tools/gepa/run_phase2.py (T-2809 Phase 2).

Run under system python3: python3 -m unittest tools.test_gepa_run_phase2 -v
Full coverage requires the gepa venv (a2a-style carve-out — this module
imports `gepa` at top level, so importing `tools.gepa.run_phase2` transitively
requires the real package):
    /home/claude/.venv-gepa/bin/python -m unittest tools.test_gepa_run_phase2 -v

No test here calls `gepa.optimize(...)` — that would be running the
optimization, which is explicitly out of this ticket's scope. Only argparse
wiring and the pure preflight-check helpers are exercised.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    from tools.gepa import run_phase2

    _HAVE_GEPA = True
except ImportError:
    _HAVE_GEPA = False


@unittest.skipUnless(_HAVE_GEPA, "gepa not installed under this interpreter (see /home/claude/.venv-gepa)")
class TestParseArgsDefaults(unittest.TestCase):
    def test_defaults_match_the_poc_plan_placeholders(self):
        args = run_phase2.parse_args([])
        self.assertEqual(args.max_rollouts, 150)
        self.assertEqual(args.wall_clock_seconds, 21600)
        self.assertEqual(args.executor_model, "claude-sonnet-5")
        self.assertEqual(args.reflector_model, "claude-opus-5")
        self.assertIsNone(args.judge_model)  # None sentinel -> defaults to executor model at wiring time
        self.assertFalse(args.keep_sessions)

    def test_default_run_dir_and_out_dir_are_under_tmp(self):
        args = run_phase2.parse_args([])
        self.assertTrue(str(args.run_dir).startswith("/tmp"))
        self.assertTrue(str(args.out_dir).startswith("/tmp"))

    def test_default_seed_candidate_path_resolves_to_the_real_reviewer_persona(self):
        args = run_phase2.parse_args([])
        # T-4763: the in-repo harness copy, resolved from this checkout, not a home path.
        repo = Path(__file__).resolve().parent.parent
        self.assertEqual(Path(args.seed_candidate_path), repo / "harness" / "claude" / "agents" / "reviewer.md")
        self.assertTrue(Path(args.seed_candidate_path).is_file())

    def test_overrides_apply(self):
        args = run_phase2.parse_args(["--max-rollouts", "10", "--wall-clock-seconds", "60", "--keep-sessions"])
        self.assertEqual(args.max_rollouts, 10)
        self.assertEqual(args.wall_clock_seconds, 60)
        self.assertTrue(args.keep_sessions)

    def test_help_does_not_raise(self):
        with self.assertRaises(SystemExit) as ctx:
            run_phase2.parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)


@unittest.skipUnless(_HAVE_GEPA, "gepa not installed under this interpreter (see /home/claude/.venv-gepa)")
class TestComputeEvalSetDigest(unittest.TestCase):
    def test_digest_is_deterministic_for_identical_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            eval_dir = self._make_fixture_eval_set(Path(tmp))
            d1 = run_phase2.compute_eval_set_digest(eval_dir)
            d2 = run_phase2.compute_eval_set_digest(eval_dir)
            self.assertEqual(d1, d2)

    def test_digest_changes_when_a_case_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            eval_dir = self._make_fixture_eval_set(Path(tmp))
            before = run_phase2.compute_eval_set_digest(eval_dir)
            (eval_dir / "cases" / "T-1.json").write_text('{"case_id": "T-1", "changed": true}', encoding="utf-8")
            after = run_phase2.compute_eval_set_digest(eval_dir)
            self.assertNotEqual(before, after)

    @staticmethod
    def _make_fixture_eval_set(root: Path) -> Path:
        eval_dir = root / "reviewer-v1"
        (eval_dir / "cases").mkdir(parents=True)
        (eval_dir / "manifest.json").write_text('{"counts": {"total": 1}}', encoding="utf-8")
        (eval_dir / "cases" / "T-1.json").write_text('{"case_id": "T-1"}', encoding="utf-8")
        return eval_dir


@unittest.skipUnless(_HAVE_GEPA, "gepa not installed under this interpreter (see /home/claude/.venv-gepa)")
class TestCheckIsolationFlagsPresent(unittest.TestCase):
    def test_passes_when_both_flags_present(self):
        argv = ["claude", "-p", "--setting-sources", "", "--strict-mcp-config", "prompt"]
        run_phase2.check_isolation_flags_present(argv)  # must not raise

    def test_raises_when_setting_sources_missing(self):
        argv = ["claude", "-p", "--strict-mcp-config", "prompt"]
        with self.assertRaises(run_phase2.IsolationFlagCheckError):
            run_phase2.check_isolation_flags_present(argv)

    def test_raises_when_strict_mcp_config_missing(self):
        argv = ["claude", "-p", "--setting-sources", "", "prompt"]
        with self.assertRaises(run_phase2.IsolationFlagCheckError):
            run_phase2.check_isolation_flags_present(argv)

    def test_raises_when_bare_present(self):
        argv = ["claude", "-p", "--setting-sources", "", "--strict-mcp-config", "--bare", "prompt"]
        with self.assertRaises(run_phase2.IsolationFlagCheckError):
            run_phase2.check_isolation_flags_present(argv)


@unittest.skipUnless(_HAVE_GEPA, "gepa not installed under this interpreter (see /home/claude/.venv-gepa)")
class TestBuildSeedCandidate(unittest.TestCase):
    def test_reads_persona_file_into_the_expected_component_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            persona_path = Path(tmp) / "reviewer.md"
            persona_path.write_text("persona text fixture\n", encoding="utf-8")
            candidate = run_phase2.build_seed_candidate(persona_path)
        from tools.gepa.adapter import ReviewerGEPAAdapter

        self.assertIn(ReviewerGEPAAdapter.PERSONA_KEY, candidate)
        self.assertEqual(candidate[ReviewerGEPAAdapter.PERSONA_KEY], "persona text fixture\n")

    def test_missing_file_raises_clear_error(self):
        with self.assertRaises(FileNotFoundError):
            run_phase2.build_seed_candidate(Path("/tmp/definitely-does-not-exist-gepa-fixture.md"))


if __name__ == "__main__":
    unittest.main()
