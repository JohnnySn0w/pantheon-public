"""Tests for tools/mutation_gate.py — the mutation-testing falsification gate.

T-3462 focus: bulk mode (--auto and --diff) must produce results IDENTICAL to
--lines mode for the same targeted line set. The historical bug: __pycache__
staleness interacted with shutil.copy2's mtime-preserving restore, so under a
sequential bulk run an earlier iteration's mutated .pyc could be loaded when
a later iteration's source was technically restored — reporting false SURVIVED
where --lines mode correctly reported KILLED. Root cause: shutil.copy2 preserves
mtime → restored source.mtime == original mtime == some cached mutated pyc's
source_mtime → import system reuses the stale pyc.

Fix (belt-and-suspenders, both required):
 - PYTHONDONTWRITEBYTECODE=1 in run_test's subprocess env → no new .pyc writes
   accumulate across iterations (prevents accumulation).
 - os.utime(source, None) in restore() → source.mtime monotonically advances
   past any historical cached pyc's source_mtime (invalidates existing).

These tests bind the invariant: bulk-mode targeting mode must produce the same
KILLED/SURVIVED verdict as --lines mode. If pycache staleness returns, these
tests fail.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


MUTATION_GATE_PY = Path(__file__).parent / "mutation_gate.py"


def _run_gate(source: Path, test_cmd: str, target_flag: str, target_value: str | None) -> tuple[int, str]:
    """Run mutation_gate.py, return (exit_code, stdout+stderr)."""
    cmd = [sys.executable, str(MUTATION_GATE_PY), "--source", str(source), "--test-cmd", test_cmd]
    if target_value is None:
        cmd.append(target_flag)
    else:
        cmd.extend([target_flag, target_value])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return result.returncode, result.stdout + "\n" + result.stderr


def _extract_verdicts(output: str) -> dict[int, str]:
    """Parse `[i/N] line K: 🟢-SURVIVED — ...` or `🔴-KILLED — ...` lines into {line_num: 'SURVIVED'|'KILLED'}."""
    verdicts: dict[int, str] = {}
    for raw in output.splitlines():
        # Look for the "line <N>: <SYMBOL>" pattern
        if "line " not in raw:
            continue
        try:
            after_line = raw.split("line ", 1)[1]
            num_str, rest = after_line.split(":", 1)
            line_num = int(num_str.strip())
        except (IndexError, ValueError):
            continue
        if "SURVIVED" in rest:
            verdicts[line_num] = "SURVIVED"
        elif "KILLED" in rest:
            verdicts[line_num] = "KILLED"
    return verdicts


class TestBulkModeMatchesLinesMode(unittest.TestCase):
    """T-3462 regression guard: --auto and --lines must agree on the same targets."""

    def setUp(self) -> None:
        self._tmpdir_ctx = TemporaryDirectory()
        self.tmpdir = Path(self._tmpdir_ctx.name)
        # Fixture module: a validation function whose lines the test genuinely
        # depends on. Every non-trivial statement line must show as KILLED when
        # mutated (its removal makes the test fail).
        self.fixture = self.tmpdir / "fixture_validator.py"
        self.fixture.write_text(textwrap.dedent("""\
            def validate(x):
                if x <= 0:
                    return False
                if x > 100:
                    return False
                return True
        """))
        # Test module: pins every branch of the validator, so mutating ANY
        # non-trivial line in the validator must break at least one assertion.
        self.testfile = self.tmpdir / "test_fixture.py"
        self.testfile.write_text(textwrap.dedent("""\
            import sys
            sys.path.insert(0, ".")
            from fixture_validator import validate

            def test_zero_and_negative_rejected():
                assert validate(0) is False
                assert validate(-1) is False

            def test_over_100_rejected():
                assert validate(101) is False
                assert validate(200) is False

            def test_in_range_accepted():
                assert validate(1) is True
                assert validate(50) is True
                assert validate(100) is True
        """))
        self.test_cmd = f"cd {self.tmpdir} && {sys.executable} -m pytest -q test_fixture.py"

    def tearDown(self) -> None:
        self._tmpdir_ctx.cleanup()

    def _mutable_lines(self) -> list[int]:
        """Every non-trivial line in the fixture (mirrors mutation_gate's is_mutable_line)."""
        lines = self.fixture.read_text().splitlines()
        return [
            i + 1
            for i, line in enumerate(lines)
            if line.strip() and not line.strip().startswith("#") and line.strip() not in ("...", "pass", '"""', "'''", ")", "}", "]")
        ]

    def test_lines_mode_kills_every_mutated_line(self) -> None:
        """Baseline: --lines mode should report every LINE-THE-GATE-MUTATES as
        KILLED (some structural lines like `def` / compound `if` are UNMUTABLE
        and correctly skipped — those don't appear in the verdict map). If any
        line the gate DID mutate reports SURVIVED here, the fixture is under-
        testing and the bulk-vs-lines invariant test is meaningless.
        """
        targets = self._mutable_lines()
        exit_code, output = _run_gate(self.fixture, self.test_cmd, "--lines", ",".join(str(n) for n in targets))
        verdicts = _extract_verdicts(output)
        self.assertGreater(len(verdicts), 0, f"--lines mode mutated NOTHING; fixture broken:\n{output}")
        for line_num, verdict in verdicts.items():
            self.assertEqual(
                verdict, "KILLED",
                f"--lines mode reported line {line_num} as SURVIVED; fixture is under-tested (test doesn't bind this line):\n{output}",
            )

    def test_auto_mode_matches_lines_mode(self) -> None:
        """T-3462 invariant guard: --auto mode must produce the same KILLED/
        SURVIVED verdicts as --lines mode for the same targeted line set.

        HONEST SCOPE — the historical false-SURVIVED bug (T-3462) was reported
        against tools/continuity_response_capture.py:239 at commit 7fedebb8a7
        under --diff bulk mode; a minimum small-fixture repro was NOT found
        during the T-3462 fix session (bug requires specific accumulation
        conditions that don't manifest on a ~20-line fixture). This test
        binds the INVARIANT (bulk-vs-lines equivalence must always hold)
        rather than independently reproducing the specific pycache-staleness
        failure. The fix (PYTHONDONTWRITEBYTECODE + os.utime bump in restore)
        is diagnosis-grounded defense-in-depth, not observationally proven
        by this test alone; the definitive repro would need re-checkout of
        7fedebb8a7 + a bulk --diff run against that specific file. If a
        pycache-related regression ever manifests small enough for this
        fixture to catch, this test catches it.
        """
        lines_targets = self._mutable_lines()
        lines_exit, lines_output = _run_gate(self.fixture, self.test_cmd, "--lines", ",".join(str(n) for n in lines_targets))
        lines_verdicts = _extract_verdicts(lines_output)

        auto_exit, auto_output = _run_gate(self.fixture, self.test_cmd, "--auto", None)
        auto_verdicts = _extract_verdicts(auto_output)

        # Both modes should have targeted the same mutable lines (is_mutable_line
        # heuristic is deterministic on the same source).
        self.assertEqual(
            set(lines_verdicts.keys()), set(auto_verdicts.keys()),
            f"--auto and --lines targeted different line sets:\nlines={sorted(lines_verdicts.keys())}\nauto={sorted(auto_verdicts.keys())}",
        )
        # And every verdict must match.
        for line_num in sorted(lines_verdicts.keys()):
            self.assertEqual(
                lines_verdicts[line_num], auto_verdicts[line_num],
                f"--auto mode disagreed with --lines mode on line {line_num}: "
                f"lines said {lines_verdicts[line_num]}, auto said {auto_verdicts[line_num]}. "
                f"This is the T-3462 pycache-staleness regression class.",
            )


if __name__ == "__main__":
    unittest.main()
