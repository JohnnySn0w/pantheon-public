"""Tests for tools/systemd_drift_sweep.py — the cross-account drift sweep (T-2444).

Hermetic: a tempdir plays the repo unit tree, and a FakeOps plays the cross-account
sudo reader. Nothing here shells out or touches a real account, so the three traps the
module exists to honour are asserted as LOGIC, on every seat and on a CI runner:

  (a) a could-not-ask account must never read as clean-empty,
  (b) reachable-but-zero-tracked-units is UNVERIFIED, not CLEAN,
  (c) drift is a CONTENT difference (cp's, not symlinks).

Plus the severity split that keeps a doc-path reshuffle from paging (correction #2) and
the sudo-to-self carve-out (trap a's mechanism).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import systemd_drift_sweep as sweep


class FakeOps:
    """Plays Ops.read_installed_dir from a canned {account: (reason, files|None)} map.
    files is None to model an unreachable account; {} to model asked-found-nothing."""

    def __init__(self, per_account: dict):
        self._map = per_account

    def read_installed_dir(self, account: str):
        return self._map.get(account, ("unknown account", None))


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "units"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _repo_unit(self, name: str, text: str) -> None:
        (self.repo / name).write_text(text, encoding="utf-8")

    def _sweep(self, account: str, files, reason=""):
        ops = FakeOps({account: (reason, files)})
        return sweep.sweep_account(account, ops=ops, unit_dir=self.repo)


class TestPerAccountStatus(_Base):
    def test_matching_installed_is_clean(self):
        body = "[Service]\nExecStart=/bin/true\n"
        self._repo_unit("a.service", body)
        r = self._sweep("codex", {"a.service": body})
        self.assertEqual(r["status"], sweep.CLEAN)
        self.assertEqual(r["compared"], 1)
        self.assertEqual(r["substantive"], [])

    def test_content_difference_is_drift(self):
        # trap (c): a cp drifts by CONTENT. Repo ahead of the installed copy.
        self._repo_unit("b.timer", "[Timer]\nOnBootSec=2min\nOnActiveSec=5min\n")
        r = self._sweep("codex", {"b.timer": "[Timer]\nOnBootSec=2min\n"})
        self.assertEqual(r["status"], sweep.DRIFT)
        self.assertEqual([d["unit"] for d in r["substantive"]], ["b.timer"])

    def test_documentation_only_drift_is_cosmetic_and_clean_status(self):
        # correction #2: a Documentation= reshuffle is real drift but must NOT flip the
        # account to DRIFT (which alerts) — it is reported as cosmetic.
        self._repo_unit("c.service",
                        "[Unit]\nDocumentation=file:///new\n[Service]\nExecStart=/bin/true\n")
        r = self._sweep("claude-research",
                        {"c.service": "[Unit]\nDocumentation=file:///old\n[Service]\nExecStart=/bin/true\n"})
        self.assertEqual(r["status"], sweep.CLEAN)
        self.assertEqual([d["unit"] for d in r["cosmetic"]], ["c.service"])
        self.assertEqual(r["substantive"], [])

    def test_unreachable_account_is_never_clean(self):
        # trap (a): a denied sudo leaves empty stdout — Ops returns files=None, and the
        # sweep must surface UNREACHABLE, never a clean empty seat.
        self._repo_unit("d.service", "[Service]\nExecStart=/bin/true\n")
        r = self._sweep("exocortex", None, reason="query failed rc=1")
        self.assertEqual(r["status"], sweep.UNREACHABLE)
        self.assertEqual(r["compared"], 0)
        self.assertIn("query failed", r["reason"])

    def test_reachable_but_no_tracked_units_is_unverified_not_clean(self):
        # trap (b): an account we CAN read but that has none of our tracked units is
        # NO_UNITS, not CLEAN — else a wiped seat reads green.
        self._repo_unit("e.service", "[Service]\nExecStart=/bin/true\n")
        r = self._sweep("the-ancestor", {})  # read fine, zero tracked units
        self.assertEqual(r["status"], sweep.NO_UNITS)
        self.assertEqual(r["compared"], 0)

    def test_persona_is_resolved(self):
        self._repo_unit("f.service", "[Service]\nExecStart=/bin/true\n")
        r = self._sweep("claude-research", {"f.service": "[Service]\nExecStart=/bin/true\n"})
        self.assertEqual(r["persona"], "specialist")


class TestAlerting(_Base):
    def test_only_drift_and_unreachable_alert(self):
        self._repo_unit("real.service", "[Service]\nExecStart=/bin/true serve\n")
        self._repo_unit("doc.service",
                        "[Unit]\nDocumentation=file:///new\n[Service]\nExecStart=/bin/true\n")
        ops = FakeOps({
            "codex": ("", {"real.service": "[Service]\nExecStart=/bin/true\n",         # substantive
                           "doc.service": "[Unit]\nDocumentation=file:///new\n[Service]\nExecStart=/bin/true\n"}),
            "exocortex": ("sudo denied", None),                                          # unreachable
            "the-ancestor": ("", {}),                                                    # no units
            "suborch1": ("", {"real.service": "[Service]\nExecStart=/bin/true serve\n",  # clean
                              "doc.service": "[Unit]\nDocumentation=file:///new\n[Service]\nExecStart=/bin/true\n"}),
        })
        results = sweep.sweep_fleet(
            ["codex", "exocortex", "the-ancestor", "suborch1"], ops=ops, unit_dir=self.repo)
        by = {r["account"]: r["status"] for r in results}
        self.assertEqual(by["codex"], sweep.DRIFT)
        self.assertEqual(by["exocortex"], sweep.UNREACHABLE)
        self.assertEqual(by["the-ancestor"], sweep.NO_UNITS)
        self.assertEqual(by["suborch1"], sweep.CLEAN)

        alerting = [r for r in results if r["status"] in sweep.ALERTING]
        self.assertEqual({r["account"] for r in alerting}, {"codex", "exocortex"})
        text = sweep._alert_text(alerting)
        self.assertIn("codex", text)
        self.assertIn("exocortex", text)
        self.assertNotIn("the-ancestor", text)   # NO_UNITS reported, not alerted
        self.assertNotIn("suborch1", text)        # CLEAN


class TestOpsReader(unittest.TestCase):
    """Ops.read_installed_dir mechanics — sudo-to-self carve-out and rc handling."""

    def _run_with_fake_subprocess(self, account, returncode, stdout, stderr=""):
        import subprocess as _sp

        class _R:
            pass

        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            r = _R()
            r.returncode = returncode
            r.stdout = stdout
            r.stderr = stderr
            return r

        orig = _sp.run
        _sp.run = fake_run
        try:
            reason, files = sweep.Ops().read_installed_dir(account)
        finally:
            _sp.run = orig
        return captured["argv"], reason, files

    def test_self_account_skips_sudo(self):
        me = sweep._me()
        argv, _, _ = self._run_with_fake_subprocess(me, 0, "")
        self.assertEqual(argv[0], "bash",
                         "sudo-to-self is a grant nobody made (trap a) — self must not be wrapped")

    def test_other_account_uses_sudo(self):
        me = sweep._me()
        other = "claude" if me != "claude" else "codex"
        argv, _, _ = self._run_with_fake_subprocess(other, 0, "")
        self.assertEqual(argv[:3], ["sudo", "-n", "-u"])
        self.assertEqual(argv[3], other)

    def test_nonzero_rc_with_empty_stdout_is_unverified_not_empty(self):
        # THE trap: a denied sudo returns rc!=0 and EMPTY stdout, byte-identical to a
        # clean empty read. Only the rc separates them.
        me = sweep._me()
        other = "claude" if me != "claude" else "codex"
        _, reason, files = self._run_with_fake_subprocess(other, 1, "", stderr="not permitted")
        self.assertIsNone(files, "non-zero rc must be UNVERIFIED (files=None), never clean-empty")
        self.assertIn("rc=1", reason)

    def test_nodir_is_reachable_empty_not_unreachable(self):
        me = sweep._me()
        other = "claude" if me != "claude" else "codex"
        _, reason, files = self._run_with_fake_subprocess(other, 0, sweep._NODIR)
        self.assertEqual(files, {}, "NODIR means asked-and-found-nothing, not could-not-ask")


class TestStreamParse(unittest.TestCase):
    def test_roundtrips_content_with_newlines_equals_and_hash(self):
        content_a = "[Service]\nExecStart=/bin/true --flag=1\n# a comment\nEnvironment=X=Y=Z\n"
        content_b = "[Timer]\nOnCalendar=*-*-* 04:00:00\n"
        stream = (f"{sweep._REC}a.service\0{content_a}"
                  f"{sweep._REC}b.timer\0{content_b}")
        files = sweep._parse_installed_stream(stream)
        self.assertEqual(files, {"a.service": content_a, "b.timer": content_b})

    def test_empty_stream_is_empty_dict(self):
        self.assertEqual(sweep._parse_installed_stream(""), {})


if __name__ == "__main__":
    unittest.main()
