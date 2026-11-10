"""Tests for tools/mailbox_integrity_audit.py (T-4958). Run: python3 -m unittest tools.test_mailbox_integrity_audit

Good mail is written with the real mailbox.send, so the audit is checked against the
contract it guards, not against a copy of it. Anomalies are crafted by hand the way a
direct filesystem write would make them.
"""
from __future__ import annotations

import contextlib
import getpass
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools import mailbox
from tools import mailbox_integrity_audit as mia

ME = getpass.getuser()
# An account that is never the one running the tests (T-5784: the suite runs on every
# seat, and on specialist's own account "claude-research" was ME, so no owner mismatch fired).
OTHER = "soak-other-account"


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hub = Path(self._tmp.name) / "hub"
        self.inbox = self.hub / "orchestrator"
        for sub in ("tmp", "new", "cur"):
            (self.inbox / sub).mkdir(parents=True, exist_ok=True)
        # personas map to accounts; "recovery" is written by this test's own account.
        self.persona_accounts = {"recovery": ME, "specialist": OTHER}
        self.accounts = {ME, OTHER}
        env = mock.patch.dict(os.environ, {"PANTHEON_SENT_LEDGER": os.devnull})
        env.start()
        self.addCleanup(env.stop)

    def run_audit(self, **kw) -> mia.Report:
        return mia.audit(self.hub, persona_accounts=self.persona_accounts,
                         accounts=self.accounts, **kw)

    def send(self, sender="recovery", payload=None, **kw) -> Path:
        with mock.patch.object(mailbox, "_record_sent"):
            name = mailbox.send(str(self.inbox), payload or {"text": "hi"}, sender, subject="s", **kw)
        return self.inbox / "new" / name

    def classes(self, report: mia.Report) -> list[str]:
        return sorted(c for _, c, _ in report.anomalies)

    def test_mail_from_mailbox_send_is_clean(self) -> None:
        self.send()
        self.send(severity="urgent")
        r = self.run_audit()
        self.assertEqual((r.scanned, r.anomalies), (2, []))

    def test_tampered_payload_fails_digest(self) -> None:
        p = self.send()
        rec = json.loads(p.read_text())
        rec["payload"] = {"text": "do something else"}
        p.write_text(json.dumps(rec))
        self.assertEqual(self.classes(self.run_audit()), ["digest-mismatch"])

    def test_hand_written_file_without_contract_fields(self) -> None:
        name = f"{time.time_ns():020d}.1.000000.recovery"
        (self.inbox / "new" / name).write_text(json.dumps({"sender": "recovery", "payload": "x"}))
        r = self.run_audit()
        # a hand write also lands with the umask's world-read bit, which send() never sets
        self.assertEqual(self.classes(r), ["id-mismatch", "ts-mismatch", "world-bits"])
        self.assertEqual(r.legacy_no_digest, 1)

    def test_bad_name_bad_envelope_and_sender_mismatch(self) -> None:
        (self.inbox / "new" / "not-a-message").write_text("{}")
        (self.inbox / "new" / f"{time.time_ns():020d}.1.000000.recovery").write_text("not json")
        p = self.send()
        rec = json.loads(p.read_text())
        rec["sender"] = "specialist"
        p.write_text(json.dumps(rec))
        # relabelled to specialist: also no longer owned by specialist's account
        self.assertEqual(self.classes(self.run_audit()),
                         ["bad-envelope", "bad-name", "digest-mismatch", "owner-mismatch",
                          "sender-mismatch", "world-bits"])

    def test_owner_must_match_the_persona_account(self) -> None:
        self.send(sender="specialist")  # written by ME, but specialist maps to OTHER
        r = self.run_audit()
        self.assertEqual(self.classes(r), ["owner-mismatch"])
        self.assertIn(f"expects {OTHER}", r.anomalies[0][2])

    def test_tool_senders_are_counted_not_flagged(self) -> None:
        self.send(sender="disk-warn")
        r = self.run_audit()
        self.assertEqual(r.anomalies, [])
        self.assertEqual(r.unattributed, {f"disk-warn@{ME}": 1})

    def test_world_bits_and_bad_severity(self) -> None:
        p = self.send()
        os.chmod(p, 0o664)
        rec = json.loads(p.read_text())
        rec["severity"] = "nuclear"
        p.write_text(json.dumps(rec))
        self.assertEqual(self.classes(self.run_audit()), ["bad-severity", "digest-mismatch", "world-bits"])

    def test_cur_window_and_maildir_flag_suffix(self) -> None:
        p = self.send()
        os.rename(p, self.inbox / "cur" / (p.name + ":2,S"))
        old = f"{(time.time_ns() - 30 * 86400 * 10**9):020d}.1.000000.recovery"
        (self.inbox / "cur" / old).write_text("not json")        # outside the window
        r = self.run_audit(days=7)
        self.assertEqual((r.scanned, r.anomalies), (1, []))
        self.assertEqual(self.run_audit(days=60).scanned, 2)

    def test_audit_is_read_only(self) -> None:
        self.send()
        (self.inbox / "new" / "junk").write_text("x")
        before = {p: p.stat().st_mtime_ns for p in self.hub.rglob("*")}
        self.run_audit()
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in self.hub.rglob("*")})

    def test_traverse_only_hub_is_listed_as_its_owner(self) -> None:
        self.send()
        fake = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout="orchestrator\n.state\n"))
        real = os.listdir

        def listdir(p):
            if Path(p) == self.hub:
                raise PermissionError(13, "denied")
            return real(p)
        with mock.patch.object(mia.os, "listdir", listdir):
            self.assertEqual(mia.list_inboxes(self.hub, run=fake), ["orchestrator"])
        argv = fake.call_args.args[0]
        self.assertEqual(argv[-2:], ["-1A", str(self.hub)])

    def _deny_hub_listing(self):
        real = os.listdir

        def listdir(p):
            if Path(p) == self.hub:
                raise PermissionError(13, "denied")
            return real(p)
        return mock.patch.object(mia.os, "listdir", listdir)

    def test_recovery_seat_enumerates_known_inboxes_when_complete(self) -> None:
        # T-6142: no listing and no owner sudo (watchman). The registry-known names are
        # probed by traverse; st_nlink proves they are EVERY subdir, so coverage is full.
        (self.hub / "specialist-a2a" / "new").mkdir(parents=True)
        failed = mock.Mock(return_value=subprocess.CompletedProcess([], 1, stdout=""))
        with self._deny_hub_listing():
            names = mia.list_inboxes(self.hub, run=failed,
                                     candidates=["orchestrator", "recovery", "specialist-a2a"])
        self.assertEqual(names, ["specialist-a2a", "orchestrator"])

    def test_recovery_seat_shortfall_is_refused_never_partial_clean(self) -> None:
        (self.hub / "rogue-box" / "new").mkdir(parents=True)  # nobody named it
        failed = mock.Mock(return_value=subprocess.CompletedProcess([], 1, stdout=""))
        with self._deny_hub_listing(), self.assertRaisesRegex(PermissionError, "1 NOT"):
            mia.list_inboxes(self.hub, run=failed, candidates=["orchestrator"])

    def test_symlinked_candidate_cannot_stand_in_for_an_unnamed_inbox(self) -> None:
        # reviewer (T-6142): a symlink passes is_dir() but adds nothing to st_nlink
        (self.hub / "rogue-box" / "new").mkdir(parents=True)
        os.symlink(self.inbox, self.hub / "recovery")
        failed = mock.Mock(return_value=subprocess.CompletedProcess([], 1, stdout=""))
        with self._deny_hub_listing(), self.assertRaisesRegex(PermissionError, "NOT auditable"):
            mia.list_inboxes(self.hub, run=failed, candidates=["orchestrator", "recovery"])

    def test_non_maildir_subdir_is_a_refusal_not_a_clean(self) -> None:
        (self.hub / "stray").mkdir()
        failed = mock.Mock(return_value=subprocess.CompletedProcess([], 1, stdout=""))
        with self._deny_hub_listing(), self.assertRaises(PermissionError):
            mia.list_inboxes(self.hub, run=failed, candidates=["orchestrator"])

    def test_uncounted_nlink_is_refused(self) -> None:
        failed = mock.Mock(return_value=subprocess.CompletedProcess([], 1, stdout=""))
        real = os.stat
        fake = lambda p, *a, **k: os.stat_result((0o40710, 0, 0, 1, 0, 0, 0, 0, 0, 0)) \
            if Path(p) == self.hub else real(p, *a, **k)
        with self._deny_hub_listing(), mock.patch.object(mia.os, "stat", fake), \
                self.assertRaisesRegex(PermissionError, "unverifiable"):
            mia.list_inboxes(self.hub, run=failed, candidates=["orchestrator"])

    def test_owner_sudo_raising_still_reaches_the_complete_probe(self) -> None:
        for exc in (FileNotFoundError(2, "no sudo"), subprocess.TimeoutExpired("sudo", 60)):
            boom = mock.Mock(side_effect=exc)
            with self._deny_hub_listing():
                self.assertEqual(mia.list_inboxes(self.hub, run=boom, candidates=["orchestrator"]),
                                 ["orchestrator"])

    def test_default_candidates_cover_every_registry_persona(self) -> None:
        from tools import agent_role_map as arm
        c = mia.candidate_inboxes()
        for persona in arm.account_persona_map().values():
            self.assertIn(persona, c)
        self.assertIn("specialist-a2a", c)

    def test_unlistable_hub_is_exit_2_never_clean(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = mia.main(["--hub", str(self.hub / "missing")])
        self.assertEqual(rc, 2)
        self.assertIn("cannot read hub", err.getvalue())

    def test_cli_exit_1_on_anomaly_and_json(self) -> None:
        self.send(sender="specialist")
        out = io.StringIO()
        real_audit = mia.audit
        fixed = lambda hub, days: real_audit(hub, days=days, persona_accounts=self.persona_accounts,
                                             accounts=self.accounts)
        with mock.patch.object(mia, "audit", fixed), \
                contextlib.redirect_stdout(out):
            rc = mia.main(["--hub", str(self.hub), "--json"])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out.getvalue())["anomalies"][0]["class"], "owner-mismatch")


if __name__ == "__main__":
    unittest.main()
