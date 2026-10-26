"""Tests for tools/mailbox_traffic_report.py — who sends mail to whom, and how often."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import tools.mailbox_traffic_report as mtr
import tools.seat_exec as se

NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)


def _name(when: datetime, sender: str, suffix: str = "") -> str:
    return f"{int(when.timestamp() * 1e9):020d}.123.000000.{sender}{suffix}"


PERSONAS = {"claude": "orchestrator", "claude-research": "specialist", "claude-fallback": "recovery"}


class Parse(unittest.TestCase):
    def test_parses_sender_recipient_time_and_unread(self):
        ds, bad = mtr.parse([f"orchestrator/new/{_name(NOW, 'recovery')}"], account_personas={})
        self.assertEqual(bad, 0)
        self.assertEqual((ds[0].sender, ds[0].recipient, ds[0].unread, ds[0].ts), ("recovery", "orchestrator", True, NOW))

    def test_maildir_info_suffix_is_not_part_of_the_sender(self):
        ds, _ = mtr.parse([f"orchestrator/cur/{_name(NOW, 'recovery', ':2,S')}"], account_personas={})
        self.assertEqual(ds[0].sender, "recovery")
        self.assertFalse(ds[0].unread)

    def test_accounts_fold_to_personas_and_workers_to_role(self):
        ds, _ = mtr.parse([f"recovery/cur/{_name(NOW, 'claude')}",
                           f"recovery/cur/{_name(NOW, 'implementer-T3328')}"], account_personas=PERSONAS)
        self.assertEqual([d.sender for d in ds], ["orchestrator", "implementer-*"])

    def test_raw_senders_kept_when_no_map(self):
        ds, _ = mtr.parse([f"recovery/cur/{_name(NOW, 'claude')}"], account_personas=None)
        self.assertEqual(ds[0].sender, "claude")

    def test_unparseable_names_are_counted_not_dropped_silently(self):
        ds, bad = mtr.parse(["orchestrator/cur/garbage", "orchestrator/tmp/" + _name(NOW, "recovery"), "x/cur"],
                            account_personas={})
        self.assertEqual((len(ds), bad), (0, 3))


class Summarize(unittest.TestCase):
    def test_windows_pairs_daily_and_unread(self):
        ds = [mtr.Delivery(NOW - timedelta(hours=1), "recovery", "orchestrator", True),
              mtr.Delivery(NOW - timedelta(days=2), "recovery", "orchestrator", False),
              mtr.Delivery(NOW - timedelta(days=40), "orchestrator", "recovery", False)]
        s = mtr.summarize(ds, now=NOW, days=3)
        self.assertEqual([s["windows"][k]["total"] for k in ("24h", "7d", "30d", "all")], [1, 2, 2, 3])
        self.assertEqual(s["windows"]["all"]["pairs"][0], {"sender": "recovery", "recipient": "orchestrator", "count": 2})
        self.assertEqual([d["count"] for d in s["daily"]], [1, 0, 1])
        self.assertEqual(s["unread_by_recipient"], {"orchestrator": 1})
        self.assertEqual(s["first_message"], (NOW - timedelta(days=40)).strftime("%Y-%m-%d"))


class ListHub(unittest.TestCase):
    def test_failed_listing_raises_never_empty(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="denied"))
        with self.assertRaises(mtr.ListingFailed):
            mtr.list_hub("/home/post-office/mailbox", "post-office", run=run)

    def test_own_account_lists_directly_other_via_sudo(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout="a/cur/x\n", stderr=""))
        with mock.patch.object(se.getpass, "getuser", return_value="post-office"):
            mtr.list_hub("/h", "post-office", run=run)
        self.assertEqual(run.call_args.args[0][0], "find")
        with mock.patch.object(se.getpass, "getuser", return_value="claude-fallback"):
            mtr.list_hub("/h", "post-office", run=run)
        self.assertEqual(run.call_args.args[0][:4], ["sudo", "-n", "-u", "post-office"])

    def test_real_listing_skips_an_unreadable_dot_dir_and_keeps_depth_three(self):
        """T-4370: mason/.urgent_rate (the URGENT rate limiter's dir, no post-office ACL)
        gave find EACCES, exit 1, and the whole report failed. Dot-entries are pruned,
        never entered, and only <recipient>/<new|cur>/<name> is listed, through the
        real find as this account."""
        import getpass
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / "mailbox"
            for rel in ("mason/new/m1.orchestrator", "mason/cur/m2.recovery", "recovery/cur/m3.specialist",
                        "mason/.urgent_rate/recovery.json", "mason/.inflight/m4.orchestrator",
                        "new/decoy-at-depth-2", "recovery/tmp/m5.half-written"):
                (hub / rel).parent.mkdir(parents=True, exist_ok=True)
                (hub / rel).write_text("{}", encoding="utf-8")
            (hub / "mason" / ".acks.jsonl").write_text("", encoding="utf-8")
            # T-4386 N1: two unreadable dot-dirs, so a prune narrowed to .urgent_rate fails.
            locked = [hub / "mason" / ".urgent_rate", hub / "mason" / ".inflight"]
            for d in locked:
                d.chmod(0)
            try:
                if os.access(locked[0], os.R_OK | os.X_OK):
                    self.skipTest("running as a user that can read a mode-000 dir (root)")
                listed = mtr.list_hub(str(hub) + "/", getpass.getuser())
            finally:
                for d in locked:
                    d.chmod(0o700)
        self.assertEqual(sorted(listed), ["mason/cur/m2.recovery", "mason/new/m1.orchestrator", "recovery/cur/m3.specialist"])

    def _small_hub(self, hub: Path) -> None:
        for rel in ("mason/new/m1.orchestrator", "recovery/cur/m3.specialist", "mason/.inflight/m4.orchestrator"):
            (hub / rel).parent.mkdir(parents=True, exist_ok=True)
            (hub / rel).write_text("{}", encoding="utf-8")

    def test_a_dot_named_hub_root_is_listed_not_pruned(self):
        """T-4386 N2: the prune must never apply to the hub root itself. A dot-named hub
        pruned at depth 0 listed [] with exit 0, a silent empty report."""
        import getpass
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / ".mailbox"
            self._small_hub(hub)
            for given in (str(hub), str(hub) + "/"):
                with self.subTest(hub=given):
                    self.assertEqual(sorted(mtr.list_hub(given, getpass.getuser())),
                                     ["mason/new/m1.orchestrator", "recovery/cur/m3.specialist"])

    def test_a_symlinked_hub_is_followed_with_or_without_a_trailing_slash(self):
        """T-4386: find -P does not descend a symlinked starting point unless it ends in
        "/", and the old rstrip removed that "/". Both spellings must list the hub."""
        import getpass
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real-mailbox"
            self._small_hub(real)
            link = Path(tmp) / "mailbox"
            link.symlink_to(real)
            for given in (str(link), str(link) + "/"):
                with self.subTest(hub=given):
                    self.assertEqual(sorted(mtr.list_hub(given, getpass.getuser())),
                                     ["mason/new/m1.orchestrator", "recovery/cur/m3.specialist"])

    def test_hub_owner_from_path(self):
        self.assertEqual(mtr.hub_owner("/home/post-office/mailbox"), "post-office")
        self.assertEqual(mtr.hub_owner("/srv/mailbox"), "post-office")


class Modes(unittest.TestCase):
    def _main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mtr.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_no_mode_refuses_64(self):
        self.assertEqual(self._main()[0], 64)
        self.assertEqual(self._main("--collect")[0], 64)
        self.assertEqual(self._main("--collect", "--dry-run", "--commit-prod")[0], 64)

    def test_serve_without_snapshot_is_renderable_empty_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, _ = self._main("--format", "json", "--repo-root", tmp)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["error"], "no_snapshot")

    def test_listing_failure_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(mtr, "list_hub", side_effect=mtr.ListingFailed("denied")):
            rc, _, err = self._main("--collect", "--commit-prod", "--repo-root", tmp, "--no-push")
            self.assertEqual(rc, 2)
            self.assertIn("nothing written", err)
            self.assertFalse((Path(tmp) / mtr.SNAPSHOT_DIR).exists())


class WriteSnapshot(unittest.TestCase):
    def test_latest_plus_history_and_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            hist = Path(tmp) / mtr.SNAPSHOT_DIR / "history"
            hist.mkdir(parents=True)
            (hist / "2020-01-01.json").write_text("{}")
            scope = mtr.write_snapshot({"generated_at": "2026-09-28T12:00:00Z"}, tmp)
            self.assertEqual(scope, [mtr.SNAPSHOT_DIR + "/"])
            self.assertTrue((Path(tmp) / mtr.SNAPSHOT_DIR / "latest.json").exists())
            self.assertEqual(sorted(p.name for p in hist.iterdir()), ["2026-09-28.json"])


if __name__ == "__main__":
    unittest.main()
