#!/usr/bin/env python3
"""Tests for tools/lease_audit.py (T-4884 step B4b, the detective lease check).

Hermetic: the journal is a fake source fed with fixture records shaped like
real `journalctl _COMM=sudo -o json` output; the lease-event export, manifest and state
file live in a tempfile directory. Nothing reads the real journal, /var/lib or
the network. The inertness tests patch the process-spawning, socket and HTTP
primitives plus the live-sender factory and assert dry-run touches none of them.
"""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import lease_audit as la  # noqa: E402
from tools.codex_recovery import LEASE_EXPORT_DIR_MODE, LEASE_TTL_SECS, LeaseExportSink  # noqa: E402

NOW = 1_800_000_000
FLEET = ("claude", "claude-research", "suborch1", "suborch2", "exocortex", "codex")
RECOVERY, SEAT = "claude-fallback", "codex-fallback"
MARKER = "ZZ-UNTRUSTED-MARKER"


def classify(cmd: str) -> la.Classification:
    return la.classify_command(cmd)


def msg(actor: str, user: str, command: str, pwd: str = "/home/x") -> str:
    return f"{actor} : PWD={pwd} ; USER={user} ; COMMAND={command}"


class FakeJournal:
    def __init__(self, records=(), readable=True):
        self._records = list(records)
        self._readable = readable
        self.since = None

    def readable(self) -> bool:
        return self._readable

    def records(self, since):
        self.since = since
        return iter(self._records)


class Fixture:
    def __init__(self, tc: unittest.TestCase):
        self._tmp = tempfile.TemporaryDirectory()
        tc.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.root = root
        self.state = root / "state" / "lease_audit.json"
        self.events_path = root / "lease-events.jsonl"
        self.manifest = root / "manifest.json"
        self.records: list[dict] = []
        self._pid = 1000

    def sudo(self, actor: str, user: str, command: str, ts: int, *, pid: int | None = None) -> None:
        self._pid += 1
        self.records.append({"MESSAGE": msg(actor, user, command), "_PID": str(pid or self._pid),
                             "__REALTIME_TIMESTAMP": str(ts * 1_000_000)})

    def lease_events(self, *events: dict) -> None:
        self.events_path.write_text("".join(json.dumps(e) + "\n" for e in events))

    def run(self, *, now: int = NOW, source=None) -> tuple[int, str]:
        out = io.StringIO()
        rc = la.run(since="-15min", state_file=self.state, lease_events=self.events_path,
                    manifest=self.manifest, source=source or FakeJournal(self.records),
                    fleet=FLEET, now=now, out=out)
        return rc, out.getvalue()


def _event(op: str, user: str, target: str, ts: int, expires: int) -> dict:
    """One line in the broker's export format (codex_recovery.LeaseExportSink)."""
    return {"op": op, "target": target, "holder_user": user, "holder_uid": 1001,
            "ts_wall": ts, "ts_mono": 5, "expires_at_wall": expires}


def acquire(user: str, target: str, start: int, renew: bool = False) -> dict:
    return _event("renew" if renew else "acquire", user, target, start, start + LEASE_TTL_SECS)


def release(user: str, target: str, ts: int, expires: int) -> dict:
    return _event("release", user, target, ts, expires)


def findings(out: str, cls: str | None = None) -> list[dict]:
    rows = [json.loads(ln) for ln in out.splitlines() if ln.startswith("{")]
    return [r for r in rows if cls is None or r["finding"] == cls]


def summary(out: str) -> dict:
    line = [ln for ln in out.splitlines() if " summary " in ln][-1]
    return dict(tok.split("=", 1) for tok in line.split() if "=" in tok)


KILL = "/usr/bin/tmux kill-session -t claude-research-bot"


class ClassifierTable(unittest.TestCase):
    REPAIRS = [
        (KILL, la.RepairClass.KILL_SESSION),
        ("/usr/bin/tmux -L default kill-session -t x", la.RepairClass.KILL_SESSION),
        ("/usr/bin/tmux kill-server", la.RepairClass.KILL_SESSION),
        ("/usr/bin/tmux send-keys -t x C-c", la.RepairClass.CONTROL_KEYS),
        ("/usr/bin/tmux send-keys -t x:0 Escape C-u", la.RepairClass.CONTROL_KEYS),
        ("/usr/bin/tmux send -t x Escape", la.RepairClass.CONTROL_KEYS),
        ("/usr/bin/tmux send-keys -t x escape", la.RepairClass.CONTROL_KEYS),
        ("/usr/bin/bash -c 'XDG_RUNTIME_DIR=/run/user/$(id -u) tmux send-keys -t s:0 Escape'",
         la.RepairClass.CONTROL_KEYS),
        ("/usr/bin/bash -c 'tmux send-keys -t x C-c 2>/dev/null'", la.RepairClass.CONTROL_KEYS),
        ("/usr/bin/bash -c 'tmux kill-session -t x 2>&1 >/dev/null || true'", la.RepairClass.KILL_SESSION),
        ("/usr/bin/bash -c 'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart "
         "pantheon-runtime-controller@specialist.service'", la.RepairClass.UNIT_RESTART),
        ("/bin/env XDG_RUNTIME_DIR=/run/user/1002 systemctl --user stop claude-telegram-research.service",
         la.RepairClass.UNIT_RESTART),
        ("/usr/bin/systemctl --user --no-block reset-failed codex-daemon.service", la.RepairClass.UNIT_RESTART),
        ("/usr/bin/bash -lc 'XDG_RUNTIME_DIR=/run/user/$(id -u) /home/e/pantheon-config/tools/"
         "handoff_and_restart.sh --service claude-telegram-exocortex.service --mode user'",
         la.RepairClass.RESTART_SCRIPT),
        ("/home/c/pantheon-config/tools/self_restart.sh", la.RepairClass.RESTART_SCRIPT),
        ("/usr/bin/bash /home/s/pantheon-config/tools/pantheon-seat-relaunch.sh --persona artisan",
         la.RepairClass.RESTART_SCRIPT),
        ("/usr/bin/bash -c 'for s in a b; do tmux send-keys -t $s C-c; done'", la.RepairClass.CONTROL_KEYS),
    ]
    NON_REPAIRS = [
        ("/usr/bin/tmux send-keys -t claude-research-bot -l 'Recovery: you have mail'", la.Kind.INTERVENTION),
        ("/usr/bin/tmux send-keys -t x hello Enter", la.Kind.INTERVENTION),
        ("/usr/bin/tmux send-keys -t x 12", la.Kind.INTERVENTION),
        ("/usr/bin/tmux send-keys -t x 0", la.Kind.INTERVENTION),
        ("/usr/bin/tmux send-keys -t x 1 2", la.Kind.INTERVENTION),
        ("/usr/bin/tmux send-keys -t x -H 03", la.Kind.INTERVENTION),
        ("/usr/bin/tmux capture-pane -p -t claude-research-bot", la.Kind.OTHER),
        ("/usr/bin/tmux send-keys -t x", la.Kind.OTHER),
        ("/usr/bin/grep kill-session /tmp/f", la.Kind.OTHER),
        ("/usr/bin/bash -c 'echo tmux kill-session'", la.Kind.OTHER),
        ("/usr/bin/bash -c 'grep \"tmux kill-session\" f'", la.Kind.OTHER),
        ("/usr/bin/cat /home/c/.local/state/pantheon/session_resets/handoff_restart.jsonl", la.Kind.OTHER),
        ("/usr/bin/bash -c 'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user show echo.service -p "
         "MemoryCurrent --value'", la.Kind.OTHER),
        ("/usr/bin/systemctl --user restart foo.service", la.Kind.OTHER),
        ("/usr/bin/systemctl restart pantheon-x.service", la.Kind.OTHER),  # not --user
        ("/usr/bin/ls -A /home/post-office/mailbox", la.Kind.OTHER),
        ("/usr/bin/bash -c 'unterminated", la.Kind.OPAQUE),
        ("/usr/bin/tmux -c 'kill-session'", la.Kind.OPAQUE),
    ]

    def test_repairs(self):
        for cmd, want in self.REPAIRS:
            with self.subTest(cmd=cmd):
                c = classify(cmd)
                self.assertEqual(c.kind, la.Kind.REPAIR)
                self.assertIn(want, c.repairs)

    def test_non_repairs(self):
        for cmd, want in self.NON_REPAIRS:
            with self.subTest(cmd=cmd):
                self.assertEqual(classify(cmd).kind, want)

    def test_send_keys_literal_control_key_name_is_intervention_not_control_keys(self):
        """`-l 'C-c'` types the three characters C, -, c: literal text, not a key."""
        for cmd in ("/usr/bin/tmux send-keys -t x -l 'C-c'", "/usr/bin/tmux send-keys -t x -l C-c",
                    "/usr/bin/tmux send-keys -lt x C-c", "/usr/bin/tmux send-keys -l -t x Escape"):
            with self.subTest(cmd=cmd):
                c = classify(cmd)
                self.assertEqual(c.kind, la.Kind.INTERVENTION)
                self.assertNotIn(la.RepairClass.CONTROL_KEYS, c.repairs)

    def test_modal_choice_digit_is_control_keys(self):
        """A single 1-9 (optionally + Enter) picks a dialog option: a repair."""
        for cmd in ("/usr/bin/tmux send-keys -t claude-telegram-bot-exocortex 1",
                    "/usr/bin/tmux send-keys -t x 9", "/usr/bin/tmux send-keys -t x 2 Enter",
                    "/usr/bin/bash -c 'tmux send-keys -t x:0 3 Enter'"):
            with self.subTest(cmd=cmd):
                c = classify(cmd)
                self.assertEqual(c.kind, la.Kind.REPAIR)
                self.assertEqual(c.repairs, frozenset({la.RepairClass.CONTROL_KEYS}))
                self.assertFalse(c.submit_only)  # never folded into a text nudge
        for cmd in ("/usr/bin/tmux send-keys -t x -l '1'", "/usr/bin/tmux send-keys -t x -l 1",
                    "/usr/bin/tmux send-keys -t x -l 2 Enter"):
            with self.subTest(cmd=cmd):
                self.assertEqual(classify(cmd).kind, la.Kind.INTERVENTION)

    def test_whitelist_is_on_argv_tokens_not_substrings(self):
        self.assertEqual(classify("/usr/bin/tmux-kill-session").kind, la.Kind.OTHER)
        self.assertEqual(classify("/usr/bin/tmux display -p kill-session").kind, la.Kind.OTHER)
        self.assertEqual(classify("/usr/bin/echo self_restart.sh").kind, la.Kind.OTHER)

    def test_sudo_quoting_decode(self):
        self.assertEqual(la.decode_sudo_command("/bin/true 'a b' 'it\\'s here' x\\\\y nl#012two"),
                         ["/bin/true", "a b", "it's here", "x\\y", "nl\ntwo"])
        self.assertIsNone(la.decode_sudo_command("/bin/true 'open"))
        self.assertIsNone(la.decode_sudo_command("/bin/true trailing\\"))


class HostileInput(unittest.TestCase):
    def test_hostile_commands_no_crash_no_echo(self):
        f = Fixture(self)
        hostile = [
            f"/usr/bin/tmux kill-session -t x ; rm -rf / {MARKER}",
            f"/usr/bin/bash -c '; rm -rf / ; {MARKER}'",
            f"/usr/bin/tmux send-keys -t x {MARKER}\x1b[2J\x07\x00",
            f"/usr/bin/bash -c '{'(' * 5000}{MARKER}'",
            f"/usr/bin/tmux send-keys -t x '{MARKER * 10}' " + "A" * (1024 * 1024),
            f"/usr/bin/bash -c \"$(printf '{MARKER}')\" 'unbalanced",
        ]
        for i, cmd in enumerate(hostile):
            f.sudo(RECOVERY, "claude-research", cmd, NOW - 60 + i)
        f.records.append({"MESSAGE": [0xff, 0xfe], "_PID": "1", "__REALTIME_TIMESTAMP": "1"})
        f.records.append({"MESSAGE": f"{RECOVERY} : PWD=/x ; {MARKER}", "_PID": "2",
                          "__REALTIME_TIMESTAMP": str(NOW * 1_000_000)})
        rc, out = f.run()
        self.assertEqual(rc, 0)
        self.assertNotIn(MARKER, out)
        self.assertNotIn("rm -rf", out)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x00", out)
        self.assertNotIn(MARKER, f.state.read_text())
        self.assertEqual(summary(out)["unparseable"], "1")  # the non-string MESSAGE

    def test_scope_filters(self):
        f = Fixture(self)
        f.sudo(RECOVERY, RECOVERY, KILL, NOW - 10)            # actor's own account: ignored
        f.sudo(RECOVERY, "post-office", KILL, NOW - 9)     # non-fleet USER: ignored
        f.sudo(RECOVERY, "root", KILL, NOW - 8)            # non-fleet USER: ignored
        f.sudo("claude", "claude-research", KILL, NOW - 7)  # not a watched actor: counted only
        f.records.append({"MESSAGE": f"{RECOVERY} : command not allowed ; PWD=/x ; USER=claude ; "
                                     f"COMMAND={KILL}", "_PID": "9", "__REALTIME_TIMESTAMP":
                          str((NOW - 6) * 1_000_000)})  # denied: never ran
        rc, out = f.run()
        s = summary(out)
        self.assertEqual((rc, s["repairs"], s["actor_events"]), (0, "0", "0"))
        self.assertEqual(s["repairs_other_actors"], "1")
        self.assertEqual(s["denied"], "1")
        self.assertEqual(findings(out, "REPAIR_WITHOUT_LEASE"), [])

    def test_padded_actor_and_continuation_reassembly(self):
        f = Fixture(self)
        f.records += [
            {"MESSAGE": msg(RECOVERY, "suborch2", "/usr/bin/bash -c 'XDG_RUNTIME_DIR=/run/user/$(id -u) "
                            "/home/s/pantheon-config/tools/pantheon-seat-relaunch.sh"),
             "_PID": "77", "__REALTIME_TIMESTAMP": str((NOW - 5) * 1_000_000)},
            {"MESSAGE": f"{RECOVERY} : (command continued) --persona artisan'", "_PID": "77",
             "__REALTIME_TIMESTAMP": str((NOW - 5) * 1_000_000)},
            {"MESSAGE": msg("   codex", "claude", "/usr/bin/tmux send-keys -t s Escape"), "_PID": "78",
             "__REALTIME_TIMESTAMP": str((NOW - 4) * 1_000_000)},
        ]
        _, out = f.run()
        s = summary(out)
        self.assertEqual(s["repairs_by_class"].split(",")[3], "RESTART_SCRIPT:1")
        self.assertEqual(s["repairs_other_actors"], "1")
        self.assertEqual(s["unparseable"], "0")


class LeaseIntervals(unittest.TestCase):
    def setUp(self):
        self.f = Fixture(self)
        self.start = NOW - 1000

    def test_repair_inside_own_lease_no_finding(self):
        self.f.lease_events(acquire(RECOVERY, "claude-research", self.start))
        self.f.sudo(RECOVERY, "claude-research", KILL, self.start + 60)
        rc, out = self.f.run()
        self.assertEqual(rc, 0)
        self.assertEqual(findings(out), [])
        self.assertEqual(summary(out)["lease_state"], "known")

    def test_repair_outside_lease_is_a_finding(self):
        self.f.lease_events(acquire(RECOVERY, "claude-research", self.start))
        self.f.sudo(RECOVERY, "claude-research", KILL, self.start + LEASE_TTL_SECS + 5)
        _, out = self.f.run()
        rows = findings(out, "REPAIR_WITHOUT_LEASE")
        self.assertEqual(rows, [{"finding": "REPAIR_WITHOUT_LEASE", "actor_account": RECOVERY,
                                 "target_account": "claude-research", "repair_class": "KILL_SESSION",
                                 "ts_epoch": self.start + LEASE_TTL_SECS + 5}])

    def test_lease_held_by_the_other_actor_is_a_finding(self):
        self.f.lease_events(acquire(SEAT, "claude-research", self.start))
        self.f.sudo(RECOVERY, "claude-research", KILL, self.start + 60)
        _, out = self.f.run()
        self.assertEqual(len(findings(out, "REPAIR_WITHOUT_LEASE")), 1)

    def test_lease_on_another_target_is_a_finding(self):
        self.f.lease_events(acquire(RECOVERY, "suborch1", self.start))
        self.f.sudo(RECOVERY, "claude-research", KILL, self.start + 60)
        _, out = self.f.run()
        self.assertEqual(len(findings(out, "REPAIR_WITHOUT_LEASE")), 1)

    def test_early_release_ends_the_interval_at_its_ts_wall(self):
        self.f.lease_events(acquire(RECOVERY, "claude-research", self.start),
                            release(RECOVERY, "claude-research", self.start + 300,
                                    self.start + LEASE_TTL_SECS))
        self.f.sudo(RECOVERY, "claude-research", KILL, self.start + 200)  # held
        self.f.sudo(RECOVERY, "claude-research", KILL, self.start + 500)  # released at +300
        _, out = self.f.run()
        rows = findings(out, "REPAIR_WITHOUT_LEASE")
        self.assertEqual([r["ts_epoch"] for r in rows], [self.start + 500])

    def test_t5048_send_in_the_release_second_is_held(self):
        # B13 attempt 5 (10-15): acquire 1792088203, Escape sent at 1792088204, release at
        # 1792088204. Both clocks are whole seconds, so the release second is ambiguous; the
        # honest reading is held.
        esc = "/usr/bin/tmux send-keys -t claude-telegram-bot-fallback Escape"
        self.f.lease_events(acquire(SEAT, "claude-fallback", 1792088203),
                            release(SEAT, "claude-fallback", 1792088204,
                                    1792088203 + LEASE_TTL_SECS))
        self.f.sudo(SEAT, "claude-fallback", esc, 1792088204)
        _, out = self.f.run(now=1792088204 + 225)
        self.assertEqual(findings(out, "REPAIR_WITHOUT_LEASE"), [])

    def test_t5048_send_one_second_after_release_is_still_flagged(self):
        esc = "/usr/bin/tmux send-keys -t claude-telegram-bot-fallback Escape"
        self.f.lease_events(acquire(SEAT, "claude-fallback", 1792088203),
                            release(SEAT, "claude-fallback", 1792088204,
                                    1792088203 + LEASE_TTL_SECS))
        self.f.sudo(SEAT, "claude-fallback", esc, 1792088205)
        _, out = self.f.run(now=1792088205 + 225)
        self.assertEqual([r["ts_epoch"] for r in findings(out, "REPAIR_WITHOUT_LEASE")],
                         [1792088205])

    def test_t5048_expiry_end_stays_exclusive(self):
        # Only a RELEASE is widened: an expired lease does not cover its expiry second.
        self.f.lease_events(acquire(RECOVERY, "claude-research", self.start))
        self.f.sudo(RECOVERY, "claude-research", KILL, self.start + LEASE_TTL_SECS)
        _, out = self.f.run()
        self.assertEqual(len(findings(out, "REPAIR_WITHOUT_LEASE")), 1)

    def test_expire_observed_closes_and_reacquire_by_other_holder(self):
        self.f.lease_events(
            acquire(RECOVERY, "claude-research", self.start),
            _event("expire_observed", RECOVERY, "claude-research", self.start + 1000,
                   self.start + LEASE_TTL_SECS),
            acquire(SEAT, "claude-research", self.start + 1000))
        self.f.sudo(RECOVERY, "claude-research", KILL, self.start + 1100)  # recovery's expired
        self.f.sudo(SEAT, "claude-research", KILL, self.start + 1101)  # the recovery seat's live
        _, out = self.f.run()
        rows = findings(out, "REPAIR_WITHOUT_LEASE")
        self.assertEqual([r["actor_account"] for r in rows], [RECOVERY])

    def test_reads_lines_written_by_the_real_broker_export_sink(self):
        """Contract: the reader consumes exactly what codex_recovery writes."""
        pub = self.f.root / "pub"
        pub.mkdir()
        os.chmod(pub, LEASE_EXPORT_DIR_MODE)
        path = pub / "lease-events.jsonl"
        sink = LeaseExportSink(path, expected_owner_uid=os.geteuid(), group_gid=os.getegid())
        self.assertTrue(sink.write(op="acquire", target="claude-research", holder_user=RECOVERY,
                                   holder_uid=1001, ts_wall=self.start,
                                   expires_at_wall=self.start + LEASE_TTL_SECS))
        self.assertTrue(sink.write(op="release", target="claude-research", holder_user=RECOVERY,
                                   holder_uid=1001, ts_wall=self.start + 100,
                                   expires_at_wall=self.start + LEASE_TTL_SECS))
        self.f.events_path = path
        self.f.sudo(RECOVERY, "claude-research", KILL, self.start + 50)   # held
        self.f.sudo(RECOVERY, "claude-research", KILL, self.start + 150)  # released
        _, out = self.f.run()
        self.assertEqual(summary(out)["lease_state"], "known")
        self.assertEqual([r["ts_epoch"] for r in findings(out, "REPAIR_WITHOUT_LEASE")],
                         [self.start + 150])

    def test_malformed_export_lines_are_skipped(self):
        self.f.events_path.write_text(
            "not json\n" + json.dumps({"op": "acquire", "target": "claude-research"}) + "\n"
            + json.dumps(acquire(RECOVERY, "claude-research", self.start)) + "\n")
        self.f.sudo(RECOVERY, "claude-research", KILL, self.start + 10)
        _, out = self.f.run()
        self.assertEqual(findings(out), [])

    def test_renew_extends_the_interval(self):
        self.f.lease_events(acquire(RECOVERY, "claude-research", self.start),
                            acquire(RECOVERY, "claude-research", self.start + 600, renew=True))
        self.f.sudo(RECOVERY, "claude-research", KILL, self.start + LEASE_TTL_SECS + 100)
        _, out = self.f.run()
        self.assertEqual(findings(out, "REPAIR_WITHOUT_LEASE"), [])

    def test_manifest_maps_account_to_target_key(self):
        self.f.manifest.write_text(json.dumps({
            "targets": {"specialist": {"account": "claude-research", "uid": 1002, "tmux_session": "s",
                                  "user_unit": "u.service", "foreground_exe_allowlist": ["/bin/x"]}},
            "invokers": {RECOVERY: {"verbs": ["lease"]}}}))
        self.f.lease_events(acquire(RECOVERY, "specialist", self.start))
        self.f.sudo(RECOVERY, "claude-research", KILL, self.start + 60)   # leased under key "specialist"
        self.f.sudo(RECOVERY, "suborch1", KILL, self.start + 61)          # not a manifest target
        _, out = self.f.run()
        s = summary(out)
        self.assertEqual(findings(out, "REPAIR_WITHOUT_LEASE"), [])
        self.assertEqual((s["targets"], s["repairs_unleasable"]), ("manifest", "1"))


class UnknownLeaseState(unittest.TestCase):
    def test_missing_export_one_unknown_per_run(self):
        f = Fixture(self)
        for i in range(3):
            f.sudo(RECOVERY, "claude-research", KILL, NOW - 100 + i)
        rc, out = f.run()
        self.assertEqual(rc, 0)
        self.assertEqual(findings(out), [{"finding": "LEASE_STATE_UNKNOWN"}])
        self.assertEqual(summary(out)["repairs_unadjudicated"], "3")

    def test_unreadable_export_is_unknown(self):
        f = Fixture(self)
        f.events_path.mkdir()  # not a regular file
        f.sudo(RECOVERY, "claude-research", KILL, NOW - 1)
        _, out = f.run()
        self.assertEqual(findings(out), [{"finding": "LEASE_STATE_UNKNOWN"}])

    def test_unknown_emitted_even_with_no_repairs(self):
        _, out = Fixture(self).run()
        self.assertEqual(findings(out), [{"finding": "LEASE_STATE_UNKNOWN"}])


class SubmitPairing(unittest.TestCase):
    def test_enter_after_text_nudge_is_intervention_lone_enter_is_repair(self):
        f = Fixture(self)
        f.lease_events()  # known, empty lease state
        f.sudo(RECOVERY, "claude-research", "/usr/bin/tmux send-keys -t b -l 'hello specialist'", NOW - 100)
        f.sudo(RECOVERY, "claude-research", "/usr/bin/tmux send-keys -t b Enter", NOW - 99)
        f.sudo(RECOVERY, "claude-research", "/usr/bin/tmux send-keys -t b Enter", NOW - 10)
        _, out = f.run()
        s = summary(out)
        self.assertEqual((s["interventions"], s["submits_paired"], s["repairs"]), ("2", "1", "1"))
        self.assertEqual([r["ts_epoch"] for r in findings(out, "REPAIR_WITHOUT_LEASE")], [NOW - 10])

    def test_text_and_enter_in_one_shell_is_intervention(self):
        c = classify("/usr/bin/bash -c 'tmux send-keys -t b -l hi; tmux send-keys -t b Enter'")
        self.assertEqual(c.kind, la.Kind.INTERVENTION)
        c = classify("/usr/bin/bash -c 'tmux send-keys -t b -l hi; tmux send-keys -t b C-c'")
        self.assertEqual(c.kind, la.Kind.REPAIR)


class Routing(unittest.TestCase):
    def _lines(self, out, action):
        return [ln for ln in out.splitlines() if f"would_act action={action}" in ln]

    def test_route_by_actor(self):
        f = Fixture(self)
        f.lease_events()
        f.sudo(SEAT, "claude-research", KILL, NOW - 20)
        f.sudo(RECOVERY, "suborch1", KILL, NOW - 10)
        _, out = f.run()
        self.assertEqual(len(self._lines(out, "MAIL_RECOVERY")), 1)
        self.assertIn(f"actor={SEAT}", self._lines(out, "MAIL_RECOVERY")[0])
        self.assertEqual(len(self._lines(out, "MAIL_RECOVERY_SEAT")), 1)
        self.assertIn(f"actor={RECOVERY}", self._lines(out, "MAIL_RECOVERY_SEAT")[0])
        self.assertEqual(self._lines(out, "PAGE_OPERATOR"), [])

    def test_mail_recipient_resolved_from_manifest_by_account(self):
        """The recovery seat self-names (TG 11753): its mail target is the manifest's
        persona for codex-fallback, so a rename needs no code change."""
        from tools import agent_role_map
        f = Fixture(self)
        f.lease_events()
        f.sudo(SEAT, "claude-research", KILL, NOW - 20)
        f.sudo(RECOVERY, "suborch1", KILL, NOW - 10)
        with mock.patch.dict(agent_role_map._ACCOUNT_TO_PERSONA, {SEAT: "foo", RECOVERY: "recovery"}):
            self.assertEqual(la.mail_recipient(la.Action.MAIL_RECOVERY_SEAT), "foo")
            self.assertEqual(la.mail_recipient(la.Action.MAIL_RECOVERY), "recovery")
            _, out = f.run()
        self.assertIn(" to=foo ", self._lines(out, "MAIL_RECOVERY_SEAT")[0])
        self.assertIn(" to=recovery ", self._lines(out, "MAIL_RECOVERY")[0])

    def test_mail_recipient_unmapped_seat_uses_neutral_placeholder(self):
        from tools import agent_role_map
        unmapped = {k: v for k, v in agent_role_map._ACCOUNT_TO_PERSONA.items() if k != SEAT}
        with mock.patch.object(agent_role_map, "_ACCOUNT_TO_PERSONA", unmapped):
            self.assertEqual(la.mail_recipient(la.Action.MAIL_RECOVERY_SEAT), "recovery-seat")
        with self.assertRaises(ValueError):
            la.mail_recipient(la.Action.PAGE_OPERATOR)

    def test_dedupe_across_overlapping_runs(self):
        f = Fixture(self)
        f.lease_events()
        f.sudo(RECOVERY, "suborch1", KILL, NOW - 10)
        _, first = f.run()
        _, second = f.run(now=NOW + 300)
        self.assertEqual(len(findings(first, "REPAIR_WITHOUT_LEASE")), 1)
        self.assertEqual(findings(second, "REPAIR_WITHOUT_LEASE"), [])
        self.assertEqual(summary(second)["findings"], "1")
        self.assertEqual(summary(second)["new_findings"], "0")

    def test_three_in_24h_pages_once_then_cooldown(self):
        f = Fixture(self)
        f.lease_events()
        outs = []
        for i, t in enumerate((NOW, NOW + 7200, NOW + 14400, NOW + 21600)):
            f.records = []
            f.sudo(RECOVERY, "suborch1", KILL, t - 5, pid=500 + i)
            outs.append(f.run(now=t)[1])
        pages = [len(self._lines(o, "PAGE_OPERATOR")) for o in outs]
        self.assertEqual(pages, [0, 0, 1, 0])
        self.assertIn("count_24h=3", self._lines(outs[2], "PAGE_OPERATOR")[0])
        # a different (actor, target, class) key counts separately
        f.records = []
        f.sudo(RECOVERY, "suborch2", KILL, NOW + 21700, pid=900)
        self.assertEqual(self._lines(f.run(now=NOW + 21705)[1], "PAGE_OPERATOR"), [])

    def test_page_again_after_cooldown(self):
        f = Fixture(self)
        f.lease_events()
        t0 = NOW
        for i in range(3):
            f.records = []
            f.sudo(RECOVERY, "suborch1", KILL, t0 + i * 7200, pid=600 + i)
            f.run(now=t0 + i * 7200 + 1)
        t1 = t0 + 14400 + la.PAGE_COOLDOWN_S + 3600  # past the cooldown and the 24h window
        outs = []
        for i in range(3):
            f.records = []
            f.sudo(RECOVERY, "suborch1", KILL, t1 + i * 60, pid=700 + i)
            outs.append(f.run(now=t1 + i * 60 + 1)[1])
        self.assertEqual([len(self._lines(o, "PAGE_OPERATOR")) for o in outs], [0, 0, 1])

    def test_mail_cooldown_suppresses_repeat(self):
        f = Fixture(self)
        f.lease_events()
        f.sudo(RECOVERY, "suborch1", KILL, NOW - 30)
        f.sudo(RECOVERY, "suborch1", KILL, NOW - 20)
        _, out = f.run()
        self.assertEqual(len(self._lines(out, "MAIL_RECOVERY_SEAT")), 1)
        self.assertEqual(summary(out)["suppressed"], "1")
        self.assertEqual(len(findings(out, "REPAIR_WITHOUT_LEASE")), 2)

    def test_corrupt_state_file_starts_fresh(self):
        f = Fixture(self)
        f.state.parent.mkdir(parents=True)
        f.state.write_text("{not json")
        f.lease_events()
        f.sudo(RECOVERY, "suborch1", KILL, NOW - 5)
        rc, out = f.run()
        self.assertEqual(rc, 0)
        self.assertEqual(len(findings(out, "REPAIR_WITHOUT_LEASE")), 1)
        self.assertEqual(json.loads(f.state.read_text())["schema_version"], 1)


class JournalPort(unittest.TestCase):
    def test_unreadable_journal_is_a_finding_and_exit_2(self):
        f = Fixture(self)
        rc, out = f.run(source=FakeJournal([], readable=False))
        self.assertEqual(rc, 2)
        self.assertEqual(findings(out), [{"finding": "JOURNAL_UNREADABLE"}])

    def test_journalctl_argv(self):
        calls = []

        def runner(argv, **kw):
            calls.append(argv)
            line = json.dumps({"MESSAGE": msg(RECOVERY, "claude", KILL), "_PID": "5",
                               "__REALTIME_TIMESTAMP": "1"})
            return subprocess.CompletedProcess(argv, 0, stdout=line + "\n", stderr="")
        src = la.JournalctlSource(runner=runner)
        self.assertTrue(src.readable())
        self.assertEqual(len(list(src.records("-15min"))), 1)
        self.assertIn("--system", calls[0])
        self.assertIn("_COMM=sudo", calls[1])
        self.assertIn("--since=-15min", calls[1])

    def test_journalctl_failure_raises_unreadable(self):
        def runner(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        with self.assertRaises(la.JournalUnreadable):
            la.JournalctlSource(runner=runner).readable()

    def test_since_validation(self):
        for ok in ("-15min", "-2h", "@1800000000", "-30s"):
            self.assertTrue(la._since_ok(ok), ok)
        for bad in ("--help", "-2h; rm", "yesterday", "", "-1x"):
            self.assertFalse(la._since_ok(bad), bad)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(la.main(["--since", "--help"]), 64)


class Inertness(unittest.TestCase):
    def test_dry_run_spawns_nothing_and_builds_no_sender(self):
        f = Fixture(self)
        f.lease_events()
        f.sudo(SEAT, "claude-research", KILL, NOW - 20)
        f.sudo(RECOVERY, "suborch1", "/usr/bin/systemctl --user restart pantheon-x.service", NOW - 10)
        boom = mock.Mock(side_effect=AssertionError("side effect in dry-run"))
        with mock.patch.object(subprocess, "run", boom), \
                mock.patch.object(subprocess, "Popen", boom), \
                mock.patch.object(socket, "socket", boom), \
                mock.patch.object(urllib.request, "urlopen", boom), \
                mock.patch.object(la, "make_live_senders", boom):
            for t in (NOW, NOW + 7200, NOW + 14400):  # through the page path too
                f.records = [dict(r, _PID=f"{r['_PID']}{t}") for r in f.records]
                rc, out = f.run(now=t)
                self.assertEqual(rc, 0)
        boom.assert_not_called()
        self.assertIn("would_act action=PAGE_OPERATOR", out)

    def test_live_is_refused(self):
        boom = mock.Mock(side_effect=AssertionError("live sender built"))
        err = io.StringIO()
        with mock.patch.object(la, "make_live_senders", boom), \
                mock.patch.object(la, "run", boom), redirect_stderr(err):
            self.assertEqual(la.main(["--live"]), 3)
        self.assertIn("not enabled until T-4884 B13", err.getvalue())
        boom.assert_not_called()

    def test_live_senders_are_stubs(self):
        with self.assertRaises(NotImplementedError):
            la.make_live_senders()


if __name__ == "__main__":
    unittest.main()
