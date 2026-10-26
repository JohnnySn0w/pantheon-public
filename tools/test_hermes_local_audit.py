#!/usr/bin/env python3
"""Tests for tools/hermes_local_audit.py (T-2392).

Source cleanliness: the trigger sequence is assembled at RUNTIME by
concatenation in every fixture below, so it never appears as contiguous bytes in
this file. `grep -cF` for the unescaped form against this file must return 0.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hermes_local_audit as hla  # noqa: E402

TRIGGER = b"HERME" + b"S.md"          # runtime-assembled; never contiguous in source
SAFE = b"hermes.md"


class InstrumentValidation(unittest.TestCase):
    """Design decision 3: never believe a zero from an unproven scanner."""

    def test_real_pattern_passes_validation(self):
        hla.validate_instrument()  # must not raise against the live pattern

    def test_raises_when_pattern_cannot_match_the_positive(self):
        dead = mock.Mock()
        dead.search.return_value = None          # matches nothing, ever
        with mock.patch.object(hla, "_HERMES_PATTERN", dead):
            with self.assertRaises(hla.AuditError) as cm:
                hla.validate_instrument()
        self.assertIn("known-POSITIVE", str(cm.exception))

    def test_raises_when_pattern_is_over_broad(self):
        greedy = mock.Mock()
        greedy.search.return_value = True        # matches everything, including the safe form
        with mock.patch.object(hla, "_HERMES_PATTERN", greedy):
            with self.assertRaises(hla.AuditError) as cm:
                hla.validate_instrument()
        self.assertIn("known-NEGATIVE", str(cm.exception))


class BaseResolution(unittest.TestCase):
    """Design decision 2: a base off main's history yields a meaningless green."""

    def test_refuses_a_base_that_is_not_an_ancestor(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1)   # merge-base --is-ancestor fails
            with self.assertRaises(hla.AuditError) as cm:
                hla.resolve_base({"last_clean_sha": "deadbeef"}, None)
        self.assertIn("NOT an ancestor", str(cm.exception))

    def test_accepts_an_ancestor(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            self.assertEqual(hla.resolve_base({"last_clean_sha": "abc123"}, None), "abc123")

    def test_explicit_override_wins_over_state(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            self.assertEqual(hla.resolve_base({"last_clean_sha": "abc123"}, "override1"), "override1")

    def test_bootstrap_used_when_state_is_empty(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            self.assertEqual(hla.resolve_base({}, None), hla.BOOTSTRAP_BASE)


class Watermark(unittest.TestCase):
    """Design decision 4: a violation must never advance the watermark."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state.json"

    def _audit_with(self, surface: bytes, head="newhead", base="oldbase"):
        def fake_git(*args, **kw):
            if args[0] == "log":
                # The octopus pass (T-4229) finds no octopus merges here.
                return b"" if "--min-parents=3" in args else surface
            if args[0] == "rev-parse":
                return head + "\n"
            if args[0] == "rev-list":
                return "3\n"
            return ""
        with mock.patch.object(hla, "_git", side_effect=fake_git), \
             mock.patch.object(hla, "resolve_base", return_value=base):
            return hla.audit(self.state, None, dry_run=False)

    def test_clean_advances_the_watermark(self):
        out = self._audit_with(b"+ a perfectly ordinary added line\n")
        self.assertEqual(out["result"], "clean")
        self.assertEqual(json.loads(self.state.read_text())["last_clean_sha"], "newhead")

    def test_violation_does_NOT_advance_the_watermark(self):
        hla.save_state(self.state, {"last_clean_sha": "oldbase"})
        out = self._audit_with(b"+ see " + TRIGGER + b" for details\n")
        self.assertEqual(out["result"], "violation")
        self.assertEqual(out["violations"], 1)
        # The whole point: the window is NOT audited away by a failing run.
        self.assertEqual(json.loads(self.state.read_text())["last_clean_sha"], "oldbase")

    def test_lowercase_form_is_not_a_violation(self):
        out = self._audit_with(b"+ see " + SAFE + b" for details\n")
        self.assertEqual(out["result"], "clean")

    def test_dry_run_never_writes_state(self):
        def fake_git(*args, **kw):
            return b"" if args[0] == "log" else ("newhead\n" if args[0] == "rev-parse" else "0\n")
        with mock.patch.object(hla, "_git", side_effect=fake_git), \
             mock.patch.object(hla, "resolve_base", return_value="oldbase"):
            hla.audit(self.state, None, dry_run=True)
        self.assertFalse(self.state.exists())

    def test_noop_when_head_equals_base(self):
        def fake_git(*args, **kw):
            return "samesha\n" if args[0] == "rev-parse" else b""
        with mock.patch.object(hla, "_git", side_effect=fake_git), \
             mock.patch.object(hla, "resolve_base", return_value="samesha"):
            out = hla.audit(self.state, None, dry_run=False)
        self.assertEqual(out["result"], "noop")


class ExitCodes(unittest.TestCase):
    """'Did not ask' (2) must never be conflated with 'asked and found nothing' (0)."""

    def test_violation_exits_1(self):
        with mock.patch.object(hla, "audit", return_value={
                "result": "violation", "base": "a" * 40, "head": "b" * 40,
                "commits": 2, "surface_bytes": 10, "violations": 1}):
            self.assertEqual(hla.main(["--json"]), 1)

    def test_clean_exits_0(self):
        with mock.patch.object(hla, "audit", return_value={
                "result": "clean", "base": "a" * 40, "head": "b" * 40,
                "commits": 2, "surface_bytes": 10, "violations": 0}):
            self.assertEqual(hla.main(["--json"]), 0)

    def test_audit_error_exits_2_not_0(self):
        with mock.patch.object(hla, "audit", side_effect=hla.AuditError("network down")):
            self.assertEqual(hla.main([]), 2)

    def test_git_failure_raises_rather_than_returning_partial(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=128, stdout="", stderr="fatal: bad object")
            with self.assertRaises(hla.AuditError):
                hla._git("rev-parse", "nope")


# ---------------------------------------------------------------------------
# T-4229: the producer must see every committed byte (real temp git repos).
# The old command, `git log -p --no-color`, printed `Binary files ... differ`
# for a NUL / .gitattributes-binary file and no patch at all for a merge.
# Each fixture asserts that the old command is blind to it (the RED check)
# before asserting the audit counts it.
# ---------------------------------------------------------------------------

OLD_CMD = ("log", "-p", "--no-color")


class _Repo:
    """Throwaway repo with hermetic config (no global/system config, no hooks)."""

    ENV = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
    }

    def __init__(self, root: Path) -> None:
        self.root = root
        self("init", "-q", "-b", "main")

    def __call__(self, *args: str, check: bool = True) -> bytes:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false", *args],
            cwd=self.root, env=self.ENV, capture_output=True, check=check,
        ).stdout

    def commit(self, files: dict, msg: str) -> str:
        for name, data in files.items():
            (self.root / name).write_bytes(data)
        self("add", "-A")
        self("commit", "-q", "-m", msg)
        return self.head()

    def head(self) -> str:
        return self("rev-parse", "HEAD").decode().strip()


class ProducerSeesEveryAddedLine(unittest.TestCase):

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.r = _Repo(Path(tmp.name))
        # Hermetic config for the audit's own git calls too.
        env = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
        env.start()
        self.addCleanup(env.stop)
        self.base = self.r.commit(
            {".gitattributes": b"*.blob binary\n*.nd -diff\n", "f.txt": b"top\n- x\nbottom\n"}, "base",
        )

    def _scan(self) -> int:
        return hla.scan_window(self.base, self.r.head(), cwd=self.r.root)[0]

    def _old_blind(self) -> None:
        """RED check: the pre-T-4229 command does not see the trigger."""
        old = self.r(*OLD_CMD, f"{self.base}..HEAD")
        self.assertNotIn(TRIGGER, old)

    def _plant(self, files: dict) -> None:
        self.r.commit(files, "plant")
        self.assertIn(b"Binary files", self.r(*OLD_CMD, "-1"))
        self._old_blind()

    def _two_branches(self, side: dict, trunk: dict) -> str:
        r = self.r
        r("checkout", "-q", "-b", "side")
        side_sha = r.commit(side, "side")
        r("checkout", "-q", "main")
        r.commit(trunk, "trunk")
        return side_sha

    def test_nul_file_is_counted(self):
        self._plant({"bin.dat": b"\x00\x01" + TRIGGER + b"\x00"})
        self.assertEqual(self._scan(), 1)

    def test_gitattributes_binary_file_is_counted(self):
        self._plant({"x.blob": b"text " + TRIGGER + b"\n"})
        self.assertEqual(self._scan(), 1)

    def test_gitattributes_minus_diff_file_is_counted(self):
        self._plant({"y.nd": TRIGGER + b"\n"})
        self.assertEqual(self._scan(), 1)

    def test_clean_binary_is_not_counted(self):
        self.r.commit({"bin.dat": b"\x00\x01clean\x00" * 1000}, "plant")
        self.assertEqual(self._scan(), 0)

    def test_header_lookalike_evil_merge_is_counted(self):
        """Both branches clean; the merge resolution replaces `- x` with
        `+ <trigger>`, which the combined diff renders as a `--- x` /
        `+++ <trigger>` header-lookalike pair."""
        self._two_branches({"s.txt": b"side\n"}, {"m.txt": b"trunk\n"})
        self.r("merge", "-q", "--no-ff", "--no-commit", "side")
        self.r.commit({"f.txt": b"top\n+ " + TRIGGER + b"\nbottom\n"}, "merge")
        self.assertIn(b"\n--- x\n+++ " + TRIGGER, self.r("show", "HEAD", "-p"))  # fixture sanity
        self._old_blind()
        self.assertEqual(self._scan(), 1)

    def test_clean_merge_of_dirty_side_is_not_recounted(self):
        """Correct attribution: the side commit is counted once; the clean
        merge adds nothing (under `separate` it would be counted again)."""
        side_sha = self._two_branches({"s.txt": b"x " + TRIGGER + b"\n"}, {"m.txt": b"trunk\n"})
        self.r("merge", "-q", "--no-ff", "side", "-m", "merge")
        self.assertEqual(self._scan(), 1)
        # A window with the dirty side commit already behind the watermark:
        # the clean merge must not re-introduce it (it would pin the watermark).
        after_side = hla.scan_window(side_sha, self.r.head(), cwd=self.r.root)[0]
        self.assertEqual(after_side, 0)
        # Under `separate` that same window is dirty (the rejected alternative).
        sep = self.r("log", "-p", "--text", "--diff-merges=separate", f"{side_sha}..HEAD")
        self.assertIn(TRIGGER, sep)

    def test_clean_merge_is_not_a_false_positive(self):
        self._two_branches({"s.txt": b"side\n"}, {"m.txt": b"trunk\n"})
        self.r("merge", "-q", "--no-ff", "side", "-m", "merge")
        self.assertEqual(self._scan(), 0)

    def test_octopus_evil_merge_is_counted_via_separate_fallback(self):
        r = self.r
        for b in ("x", "y"):
            r("checkout", "-q", "-b", b, "main")
            r.commit({b + ".txt": b.encode() + b"\n"}, b)
        r("checkout", "-q", "main")
        r("merge", "-q", "--no-ff", "--no-commit", "x", "y")
        r.commit({"evil.txt": b"e " + TRIGGER + b"\n"}, "octopus")
        self._old_blind()
        self.assertGreaterEqual(self._scan(), 1)

    def test_git_failure_is_loud(self):
        with self.assertRaises(hla.AuditError):
            hla.scan_window(self.base, "0" * 40, cwd=self.r.root)

    def test_git_lacking_remerge_is_loud_end_to_end(self):
        """A git that rejects a producer flag must exit 2, never 0 (clean)."""
        # A bare origin, so audit()'s fetch + origin/main resolve for real.
        origin = self.r.root.parent / (self.r.root.name + "-origin.git")
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, env=_Repo.ENV)
        self.addCleanup(shutil.rmtree, origin, True)
        self.r.commit({"a.txt": b"clean\n"}, "work")
        self.r("remote", "add", "origin", str(origin))
        self.r("push", "-q", "origin", "main")
        state = self.r.root / ".audit-state.json"
        real_git = shutil.which("git")
        shim_dir = self.r.root.parent / (self.r.root.name + "-shim")
        shim_dir.mkdir()
        self.addCleanup(shutil.rmtree, shim_dir, True)
        shim = shim_dir / "git"
        shim.write_text(
            "#!/bin/sh\n"
            'for a in "$@"; do [ "$a" = "--diff-merges=remerge" ] && '
            "{ echo \"error: invalid value for '--diff-merges'\" >&2; exit 129; }; done\n"
            f'exec {real_git} "$@"\n'
        )
        shim.chmod(0o755)
        argv = ["--base", self.base, "--state", str(state), "--dry-run"]
        with mock.patch.object(hla, "REPO_ROOT", self.r.root):
            self.assertEqual(hla.main(argv), 0)  # control: the real git scans clean
            with mock.patch.dict(os.environ, {"PATH": f"{shim_dir}:{os.environ['PATH']}"}), \
                 mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(hla.main(argv), 2)
        self.assertIn("AUDIT COULD NOT RUN", err.getvalue())
        self.assertIn("--diff-merges", err.getvalue())

    def test_producer_flags_are_the_history_audits(self):
        self.assertEqual(hla._SHOW_ARGS[:4], ("-p", "--text", "--no-textconv", "--no-ext-diff"))
        self.assertIn("--diff-merges=remerge", hla._SHOW_ARGS)
        self.assertIn("--diff-merges=separate", hla._OCTOPUS_SHOW_ARGS)


class SourceCleanliness(unittest.TestCase):

    def test_no_contiguous_trigger_in_touched_files(self):
        here = Path(__file__).resolve().parent
        for name in ("hermes_local_audit.py", "test_hermes_local_audit.py"):
            self.assertNotIn(TRIGGER, (here / name).read_bytes(), name)


if __name__ == "__main__":
    unittest.main()
