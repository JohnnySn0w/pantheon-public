"""Tests for pgrep_predicate_lint, plus the live-tree regression guard (T-1930).

    python3 -m unittest tools.test_pgrep_predicate_lint -v

The EXCLUSION tests carry most of the weight here. A lint that flags
`pkill -P` (parent-pid, a predicate the lesson actively prefers), or the word
"ripgrep", or prose describing the trap, becomes noise and gets ignored — which
is worse than no lint. Each exclusion below corresponds to a real construct
found in this repo during the T-1930 baseline sweep.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import tools.pgrep_predicate_lint as ppl

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestDetectorFlags(unittest.TestCase):
    """Things that ARE cmdline-substring predicates."""

    def test_flags_bare_pgrep_f_in_shell(self) -> None:
        f = ppl.scan_text('if pgrep -f "some pattern"; then\n', "x.sh")
        self.assertEqual(len(f), 1)

    def test_flags_pgrep_f_in_python_argv(self) -> None:
        f = ppl.scan_text('subprocess.run(["pgrep", "-f", PATTERN])\n', "x.py")
        self.assertEqual(len(f), 1)

    def test_flags_bundled_short_flag(self) -> None:
        """`-af` bundles -f; the cluster must still match."""
        self.assertEqual(len(ppl.scan_text('pgrep -af "pat"\n', "x.sh")), 1)

    def test_flags_long_form_full(self) -> None:
        self.assertEqual(len(ppl.scan_text('pgrep --full "pat"\n', "x.sh")), 1)

    def test_flags_pkill_f(self) -> None:
        self.assertEqual(len(ppl.scan_text('pkill -f "pat"\n', "x.sh")), 1)

    def test_flags_uid_scoped_use_too(self) -> None:
        """Scoping is what makes a use ALLOWLISTABLE, not undetectable — the
        detector still flags it; the allowlist is where the judgement lives."""
        self.assertEqual(len(ppl.scan_text('pgrep -u "$u" -f "$p"\n', "x.sh")), 1)

    def test_flags_the_self_matching_probe(self) -> None:
        """Reproduced live during T-1930: a probe whose own shell argv contains
        the pattern literal matches itself. Exactly what the lint must catch."""
        self.assertEqual(
            len(ppl.scan_text('pgrep -af "claude.*--resume"\n', "x.sh")), 1
        )


class TestDetectorExclusions(unittest.TestCase):
    """Things that are NOT this class — each found in the live tree."""

    def test_does_not_flag_pkill_dash_P(self) -> None:
        """Parent-PID is kernel ground truth — a SOUND predicate the lesson
        prefers. tools/handoff_and_restart.sh uses it four times, correctly."""
        self.assertEqual(ppl.scan_text('pkill -TERM -P "$PID"\n', "x.sh"), [])

    def test_does_not_flag_pgrep_dash_x(self) -> None:
        """Exact process-name match — also sound."""
        self.assertEqual(ppl.scan_text('pgrep -x claude\n', "x.sh"), [])

    def test_does_not_flag_ripgrep_substring(self) -> None:
        r"""'ripgrep' literally CONTAINS 'pgrep' (r-i-pgrep). tools/memory_search.py
        trips a naive scan; word-boundary matching is mandatory."""
        self.assertEqual(ppl.scan_text('# a thin ripgrep wrapper\n', "x.py"), [])
        self.assertEqual(ppl.scan_text('cmd = ["ripgrep", "-f", x]\n', "x.py"), [])

    def test_does_not_flag_shell_comment(self) -> None:
        self.assertEqual(ppl.scan_text('# uses pgrep -f to find it\n', "x.sh"), [])

    def test_does_not_flag_python_prose(self) -> None:
        """A docstring sentence mentioning the flag is not an invocation. This
        file and the lint's own docstring both do it."""
        self.assertEqual(
            ppl.scan_text('    4. Bails if running (`pgrep -f \'claude\'` non-empty).\n', "x.py"),
            [],
        )

    def test_does_not_flag_pgrep_without_f(self) -> None:
        self.assertEqual(ppl.scan_text('pgrep claude\n', "x.sh"), [])


class TestAllowlistAndScan(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, rel: str, text: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def test_scan_reports_unallowlisted(self) -> None:
        self._write("tools/x.sh", 'pgrep -f "pat"\n')
        self.assertEqual(len(ppl.scan(self.root, allowlist=())), 1)

    def test_allowlist_suppresses_by_path(self) -> None:
        self._write("tools/x.sh", 'pgrep -f "pat"\n')
        out = ppl.scan(self.root, allowlist=(("tools/x.sh", "justified: reason"),))
        self.assertEqual(out, [])

    def test_test_files_are_out_of_scope(self) -> None:
        """Structural requirement, not preference: this lint's own tests must
        contain synthetic samples, so scanning tests makes the live-tree guard
        unsatisfiable."""
        self._write("tools/test_thing.py", 'subprocess.run(["pgrep", "-f", P])\n')
        self.assertEqual(ppl.scan(self.root, allowlist=()), [])

    def test_nested_agent_worktrees_are_out_of_scope(self) -> None:
        """T-4513: a gitignored .claude/worktrees/<agent> checkout is another
        commit, not this tree; only that exact prefix is skipped."""
        self._write(".claude/worktrees/agent-x/tools/x.sh", 'pgrep -f "pat"\n')
        self.assertEqual(ppl.scan(self.root, allowlist=()), [])
        self._write("tools/worktrees/x.sh", 'pgrep -f "pat"\n')
        self.assertEqual(len(ppl.scan(self.root, allowlist=())), 1)

    def test_prose_dirs_are_out_of_scope(self) -> None:
        self._write("lessons/a-lesson.sh", 'pgrep -f "pat"\n')
        self.assertEqual(ppl.scan(self.root, allowlist=()), [])

    def test_exit_codes(self) -> None:
        self._write("tools/x.sh", 'pgrep -f "pat"\n')
        self.assertEqual(ppl.main(["--root", str(self.root), "--quiet"]), 1)
        (self.root / "tools" / "x.sh").write_text("pgrep -x claude\n")
        self.assertEqual(ppl.main(["--root", str(self.root), "--quiet"]), 0)


class TestAllowlistIntegrity(unittest.TestCase):
    """The reason field is what keeps the allowlist honest (Orchestrator, T-1930)."""

    def test_every_entry_has_a_substantive_reason(self) -> None:
        for path, reason in ppl.ALLOWLIST:
            with self.subTest(path=path):
                self.assertGreater(len(reason), 80, "reason must justify, not label")

    #: The machine-readable tracker marker on a KNOWN-VIOLATION entry. Parsed,
    #: not merely pattern-matched: a bare "T-NNNN" mentioned in passing must not
    #: be mistaken for the tracker (a reason citing some other, possibly
    #: long-resolved, ticket would otherwise trip the resolution check below).
    _TRACKER_RE = re.compile(r"tracked:\s*(T-\d{4,})", re.IGNORECASE)  # T-3215

    def _known_violations(self):
        return [(p, r) for p, r in ppl.ALLOWLIST if "KNOWN VIOLATION" in r]

    def test_known_violations_are_marked_and_ticketed(self) -> None:
        """A defect parked for born-green must be distinguishable from a genuine
        justification, and must carry a parseable tracker marker."""
        for path, reason in self._known_violations():
            with self.subTest(path=path):
                self.assertRegex(
                    reason, self._TRACKER_RE, "must carry a `tracked: T-NNNN` marker"
                )
                self.assertIn("not a blessing", reason)

    @classmethod
    def _resolved_trackers(cls, allowlist) -> list:
        """Return [(path, ticket)] for KNOWN-VIOLATION entries whose tracker has
        already resolved. Detection is separated from assertion so the check can
        itself be tested — the same scan-vs-main split the lint uses."""
        out = []
        for path, reason in allowlist:
            if "KNOWN VIOLATION" not in reason:
                continue
            m = cls._TRACKER_RE.search(reason)
            if m and (REPO_ROOT / "tickets" / "done" / f"{m.group(1)}.md").exists():
                out.append((path, m.group(1)))
        return out

    def test_known_violation_tracker_is_not_already_resolved(self) -> None:
        """T-1958: a debt entry must not outlive its tracking ticket.

        The previous version asserted only that a ticket was CITED — it read no
        ticket state, so the tracker resolving while the entry survived passed
        silently and the debt became permanent and unlabelled. That is the exact
        silent-widening this guard exists to prevent
        (lessons/2026-07-22-allowlist-needs-its-own-guard.md).

        A failure means one of two things, both needing a human: the defect was
        FIXED (remove the entry) or the ticket closed WITHOUT the fix (re-open
        it, or repoint the marker at its successor).
        """
        stale = self._resolved_trackers(ppl.ALLOWLIST)
        self.assertEqual(
            stale, [],
            "KNOWN-VIOLATION entries whose tracking ticket has RESOLVED while the "
            "entry survives: "
            + ", ".join(f"{p} (tracked: {t})" for p, t in stale),
        )

    def test_resolved_tracker_IS_detected(self) -> None:
        """Non-vacuity: the check must FIRE, not pass because nothing matched.

        Uses T-1930 — genuinely resolved — as a synthetic debt entry. If this
        ever stops detecting, the guard has become decorative.
        """
        self.assertTrue((REPO_ROOT / "tickets" / "done" / "T-1930.md").exists())
        synthetic = (("tools/whatever.py",
                      "KNOWN VIOLATION — defect, NOT justified. tracked: T-1930. "
                      "This entry is a debt marker, not a blessing."),)
        self.assertEqual(
            self._resolved_trackers(synthetic), [("tools/whatever.py", "T-1930")]
        )

    def test_open_tracker_passes(self) -> None:
        """Complement: an entry whose tracker is still open must NOT fire."""
        self.assertFalse((REPO_ROOT / "tickets" / "done" / "T-9999.md").exists())
        synthetic = (("tools/whatever.py",
                      "KNOWN VIOLATION — defect, NOT justified. tracked: T-9999. "
                      "This entry is a debt marker, not a blessing."),)
        self.assertEqual(self._resolved_trackers(synthetic), [])

    def test_bare_ticket_mention_is_not_treated_as_the_tracker(self) -> None:
        """Only the `tracked:` marker counts. A reason mentioning a resolved
        ticket in passing must not trip the check — otherwise every entry that
        referenced history would rot into a false failure."""
        synthetic = (("tools/whatever.py",
                      "KNOWN VIOLATION — defect, NOT justified. tracked: T-9999. "
                      "Unlike the T-1930 case, this one is unfixed. "
                      "This entry is a debt marker, not a blessing."),)
        self.assertEqual(self._resolved_trackers(synthetic), [])

    def test_allowlisted_paths_exist(self) -> None:
        """A stale entry silently widens the lint's blind spot."""
        for path, _reason in ppl.ALLOWLIST:
            with self.subTest(path=path):
                self.assertTrue((REPO_ROOT / path).exists(), f"{path} no longer exists")


class TestLiveTree(unittest.TestCase):
    """Born-green guard: the real tree must hold no unallowlisted predicate.

    Sibling precedent: test_tempfile_hygiene_lint.test_live_tools_tree_is_clean
    (T-1309).
    """

    def test_live_tree_is_clean(self) -> None:
        findings = ppl.scan(REPO_ROOT)
        self.assertEqual(
            findings,
            [],
            "unallowlisted cmdline-substring process predicate(s):\n  "
            + "\n  ".join(f"{r}:{n}  {ln}" for r, n, ln in findings),
        )


if __name__ == "__main__":
    unittest.main()
