"""Tests for tools/memex_failed_unit_check.py (T-4598).

Synthetic only: the ssh runner, the clock, the mail sender and the state path are all
injected, so nothing here reaches 203 or the mailbox hub. The one shell test runs the
remote command under a local `sh` against a FAKE `systemctl` on PATH. The real path is
proven by archivist after install.
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tools import memex_failed_unit_check as chk

NOW = datetime(2026, 10, 6, 5, 52, tzinfo=timezone.utc)
SYSTEMD = Path(__file__).resolve().parent / "systemd"
SERVICE = SYSTEMD / "pantheon-memex-failed-unit-check.service"
TIMER = SYSTEMD / "pantheon-memex-failed-unit-check.timer"
END_OK = "__MEMEX_FUC_END rc=0 jrc=0 canary=active"
MARKER = "__MEMEX_FUC_JOURNAL"
# T-5602: the ONE unit-failed journal entry captured from 203 on 2026-10-26 (the 14:47Z
# supernote-ingest failure), verbatim. Fixtures vary only USER_UNIT / UNIT_RESULT /
# __REALTIME_TIMESTAMP and keep every other field journald adds.
REAL_EVENT = (
    '{"__CURSOR":"s=07b11fee3ce54e4d88a149c2b8390432;i=2745c;b=0edf15c586b841e186fc3f5def3f5ed9;'
    'm=6488239942;t=65ebf699b3afe;x=23607f2a2a866e1c","__MONOTONIC_TIMESTAMP":"431780763970",'
    '"USER_UNIT":"supernote-ingest.service","__REALTIME_TIMESTAMP":"1793026063809278",'
    '"__SEQNUM_ID":"07b11fee3ce54e4d88a149c2b8390432","_BOOT_ID":"0edf15c586b841e186fc3f5def3f5ed9",'
    '"__SEQNUM":"160860","UNIT_RESULT":"exit-code"}'
)
REAL_TS = 1793026063809278  # 2026-10-26T14:47:43.809278Z
# A poll after the real failure (the 17:52Z slot that day); NOW predates it.
EV_NOW = datetime(2026, 10, 26, 17, 52, tzinfo=timezone.utc)
# The event floor a cursor-less run queries (now - 7h, whole seconds) and then persists.
FLOOR = int((NOW - timedelta(hours=7)).timestamp()) * 1_000_000
EV_FLOOR_S = int((EV_NOW - timedelta(hours=7)).timestamp())
EV_FLOOR = EV_FLOOR_S * 1_000_000


def _line(unit: str, desc: str = "memex drop leg") -> str:
    return f"{unit} loaded failed failed {desc}"


def _event(unit: str | None = None, result: str | None = None, ts: int | str | None = None) -> str:
    """A journal line in the captured real shape, with only the named fields varied."""
    obj = json.loads(REAL_EVENT)
    if unit is not None:
        obj["USER_UNIT"] = unit
    if result is not None:
        obj["UNIT_RESULT"] = result
    if ts is not None:
        obj["__REALTIME_TIMESTAMP"] = str(ts)
    return json.dumps(obj, separators=(",", ":"))


def _out(*lines: str, journal: tuple[str, ...] | list[str] = (), end: str = END_OK) -> str:
    """Remote stdout as the real command prints it: list-units lines, the journal marker,
    the journal lines, then the end sentinel."""
    return "\n".join([*lines, MARKER, *journal, end]) + "\n"


class FakeRunner:
    """Stands in for subprocess.run: records argv, returns canned output or raises."""

    def __init__(self, stdout: str = "", rc: int = 0, exc: BaseException | None = None):
        self.stdout, self.rc, self.exc = stdout, rc, exc
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.rc, stdout=self.stdout, stderr="remote noise")


class FakeSend:
    def __init__(self, exc: BaseException | None = None):
        self.exc = exc
        self.calls: list[tuple[str, str]] = []

    def __call__(self, subject: str, payload: str) -> None:
        self.calls.append((subject, payload))
        if self.exc is not None:
            raise self.exc


class FakeTailnet:
    """Stands in for tools.tailnet_self (T-5388). `state` is what off_tailnet() returns:
    None = on the tailnet or unknown (never suppresses), a string = definitely off."""

    def __init__(self, state: str | None = None):
        self.state = state
        self.reports: list[tuple[str, str, str]] = []
        self.reachable = 0

    def off_tailnet(self):
        return self.state

    def report_off_tailnet(self, monitor: str, state: str, *, detail: str = "") -> bool:
        self.reports.append((monitor, state, detail))
        return True

    def note_reachable(self) -> None:
        self.reachable += 1


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Never read this host's real tailscale state from a test (it may be off the tailnet).
        self.tailnet = FakeTailnet()
        p = patch.object(chk, "_default_tailnet", lambda: self.tailnet)
        p.start()
        self.addCleanup(p.stop)
        self.state = Path(self._tmp.name) / "sub" / "memex-failed-unit-check.json"

    def run_check(self, stdout: str = "", *, rc: int = 0, exc=None, send=None, now=NOW,
                  extra: list[str] | None = None) -> tuple[int, FakeRunner, FakeSend, str]:
        runner = FakeRunner(stdout, rc, exc)
        send = send if send is not None else FakeSend()
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = chk.main(["--host", "memex203", "--state", str(self.state), *(extra or [])],
                            runner=runner, clock=lambda: now, send=send)
        return code, runner, send, buf.getvalue()

    def write_state(self, failed: list[str], mailed_at: datetime) -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(json.dumps(
            {"failed": failed, "mailed_at": mailed_at.strftime("%Y-%m-%dT%H:%M:%SZ")}))

    def read_state(self) -> dict:
        return json.loads(self.state.read_text())


class TestParsing(_Base):
    def test_sentinel_only_is_clean(self) -> None:
        self.assertEqual(chk.parse(_out()), [])
        self.assertEqual(chk.parse("\n  \n" + MARKER + "\n\n" + END_OK + "\n\n"), [])
        code, _, send, out = self.run_check(_out())
        self.assertEqual(code, 0)
        self.assertEqual(send.calls, [])
        self.assertEqual(len(out.strip().splitlines()), 1, "one log line per run")

    def test_one_failed_line(self) -> None:
        self.assertEqual(chk.parse(_out(_line("memex-drop.service"))), ["memex-drop.service"])

    def test_several_failed_lines_sorted_and_deduped(self) -> None:
        text = _out(_line("z-leg.service"), _line("memex-backup.timer"),
                    _line("a@b.service"), _line("z-leg.service"))
        self.assertEqual(chk.parse(text), ["a@b.service", "memex-backup.timer", "z-leg.service"])

    def test_failed_marker_prefix_is_stripped(self) -> None:
        text = _out("● memex-drop.service loaded failed failed x",
                    "* memex-leg.service loaded failed failed y")
        self.assertEqual(chk.parse(text), ["memex-drop.service", "memex-leg.service"])

    def test_garbage_line_fails_closed(self) -> None:
        text = _out(_line("memex-drop.service"), "Failed to connect to bus: No medium found")
        with self.assertRaises(chk.CheckUnreachable):
            chk.parse(text)
        code, _, send, out = self.run_check(text)
        self.assertEqual(code, 3)
        self.assertEqual(send.calls, [], "a partial read must not be mailed as the failed set")
        self.assertNotIn("No medium found", out, "unparseable lines are not echoed")

    def test_non_unit_suffix_is_garbage(self) -> None:
        with self.assertRaises(chk.CheckUnreachable):
            chk.parse(_out("0 loaded units listed."))

    def test_ssh_nonzero_rc_fails_closed(self) -> None:
        code, _, send, out = self.run_check(_out(_line("memex-drop.service")), rc=255)
        self.assertEqual(code, 3)
        self.assertEqual(send.calls, [])
        self.assertIn("rc=255", out)
        self.assertNotIn("remote noise", out, "remote stderr is not echoed")

    def test_ssh_timeout_fails_closed(self) -> None:
        code, _, send, _ = self.run_check(exc=subprocess.TimeoutExpired(["ssh"], 90))
        self.assertEqual((code, send.calls), (3, []))

    def test_ssh_missing_fails_closed(self) -> None:
        code, _, _, _ = self.run_check(exc=FileNotFoundError("ssh"))
        self.assertEqual(code, 3)

    def test_one_ssh_invocation_with_the_sentinel_command(self) -> None:
        _, runner, _, _ = self.run_check(_out())
        self.assertEqual(len(runner.calls), 1)
        argv, kw = runner.calls[0]
        self.assertEqual(argv, [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "memex203",
            "systemctl --user list-units --state=failed --no-legend --plain; r=$?; "
            "printf '__MEMEX_FUC_JOURNAL\\n'; "
            "journalctl --user -o json --output-fields=USER_UNIT,UNIT_RESULT --no-pager "
            f"--since @{int((NOW - timedelta(hours=7)).timestamp())} "
            "MESSAGE_ID=d9b373ed55a64feb8242e02dbe79a49c; j=$?; "
            "printf '__MEMEX_FUC_END rc=%s jrc=%s canary=%s\\n' \"$r\" \"$j\" "
            "\"$(systemctl --user is-active supernote-ingest.path)\"",
        ])
        self.assertEqual(kw.get("timeout"), 90)


class TestEndSentinel(_Base):
    """REBOUND-1: rc 0 + empty stdout is NOT proof that systemctl ran."""

    def _fails_closed(self, stdout: str, says: str) -> str:
        code, _, send, out = self.run_check(stdout)
        self.assertEqual(code, 3, out)
        self.assertEqual(send.calls, [])
        self.assertIn(says, out)
        self.assertEqual(len(out.strip().splitlines()), 1, "one log line naming the condition")
        return out

    def test_empty_rc0_without_sentinel_fails_closed(self) -> None:
        # The reviewer's a1 shape: authorized_keys command=, `exit 0` rc, fresh manager.
        for stdout in ("", "\n \n"):
            with self.subTest(stdout=stdout):
                self._fails_closed(stdout, "missing end sentinel")

    def test_unit_lines_without_sentinel_fail_closed(self) -> None:
        self._fails_closed(_line("memex-drop.service") + "\n", "missing end sentinel")

    def test_sentinel_with_nonzero_rc_fails_closed(self) -> None:
        self._fails_closed(_out(end="__MEMEX_FUC_END rc=1 jrc=0 canary=active"), "list-units rc=1")

    def test_canary_inactive_fails_closed(self) -> None:
        self._fails_closed(_out(end="__MEMEX_FUC_END rc=0 jrc=0 canary=inactive"),
                           "canary not active (state=inactive)")

    def test_canary_failed_fails_closed(self) -> None:
        self._fails_closed(_out(_line("memex-drop.service"),
                                end="__MEMEX_FUC_END rc=0 jrc=0 canary=failed"),
                           "canary not active (state=failed)")

    def test_canary_empty_fails_closed(self) -> None:
        self._fails_closed(_out(end="__MEMEX_FUC_END rc=0 jrc=0 canary="), "canary not active (state=empty)")

    def test_lines_after_the_sentinel_fail_closed(self) -> None:
        self._fails_closed(END_OK + "\n" + _line("memex-drop.service") + "\n",
                           "1 line(s) after the end sentinel")

    def test_malformed_sentinel_fails_closed_without_echoing_it(self) -> None:
        for end in ("__MEMEX_FUC_END rc=0", "__MEMEX_FUC_END rc=0 jrc=0 canary=active extra",
                    "__MEMEX_FUC_END rc=0 jrc=0 canary=LEAK-dear-diary",
                    "__MEMEX_FUC_END rc=0 canary=active",  # the pre-T-5602 shape: no jrc
                    "__MEMEX_FUC_END rc=0 jrc=LEAK canary=active"):
            with self.subTest(end=end):
                out = self._fails_closed(_out(end=end), "malformed end sentinel")
                self.assertNotIn("LEAK", out)
                self.assertNotIn("diary", out)

    def test_failed_units_with_sentinel_take_the_mail_path(self) -> None:
        code, _, send, _ = self.run_check(_out(_line("memex-drop.service")))
        self.assertEqual((code, len(send.calls)), (0, 1))
        self.assertEqual(self.read_state()["failed"], ["memex-drop.service"])

    def test_remote_command_composes_under_a_real_shell(self) -> None:
        """Run the exact remote command under a real `bash -c` (and `sh -c`) with FAKE
        systemctl + journalctl on PATH: proves the quoting + printf + $? composition of both
        legs and the journal marker, not just the string."""
        bindir = Path(self._tmp.name) / "bin"
        bindir.mkdir()
        fake = bindir / "systemctl"
        fake.write_text(
            "#!/bin/sh\n"
            'case "$2" in\n'
            '  list-units) [ -n "$FAKE_LIST" ] && printf \'%s\\n\' "$FAKE_LIST"; exit "$FAKE_RC";;\n'
            '  is-active) printf \'%s\' "$3" > "$FAKE_ARGLOG"; printf \'%s\\n\' "$FAKE_STATE";'
            ' [ "$FAKE_STATE" = active ];;\n'
            "esac\n")
        fake.chmod(0o755)
        fakej = bindir / "journalctl"
        # Records its argv one per line, prints the canned journal (nothing when empty, as the
        # real one does with no match), exits with the canned rc.
        fakej.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$@" > "$FAKE_JARGLOG"\n'
            '[ -n "$FAKE_JOURNAL" ] && printf \'%s\\n\' "$FAKE_JOURNAL"\n'
            'exit "$FAKE_JRC"\n')
        fakej.chmod(0o755)
        arglog = Path(self._tmp.name) / "canary-arg"
        jarglog = Path(self._tmp.name) / "journal-args"

        def remote(canary: str, listed: str, rc: str, state: str, *, journal: str = "",
                   jrc: str = "0", since: int = 1793000000, shell: str = "bash") -> str:
            r = subprocess.run(
                [shell, "-c", chk.remote_command(canary, since)], capture_output=True, text=True,
                timeout=10, env={"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_LIST": listed,
                                 "FAKE_RC": rc, "FAKE_STATE": state, "FAKE_ARGLOG": str(arglog),
                                 "FAKE_JOURNAL": journal, "FAKE_JRC": jrc,
                                 "FAKE_JARGLOG": str(jarglog)})
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout

        for shell in ("bash", "sh"):
            with self.subTest(shell=shell):
                out = remote("supernote-ingest.path", _line("memex-drop.service"), "0", "active",
                             shell=shell)
                self.assertEqual(chk.parse(out), ["memex-drop.service"])
                self.assertEqual(chk.parse(remote("supernote-ingest.path", "", "0", "active",
                                                  shell=shell)), [])
                with self.assertRaisesRegex(chk.CheckUnreachable, "list-units rc=1"):
                    chk.parse(remote("supernote-ingest.path", "", "1", "active", shell=shell))
                with self.assertRaisesRegex(chk.CheckUnreachable, r"state=inactive"):
                    chk.parse(remote("supernote-ingest.path", "", "0", "inactive", shell=shell))
                # The journal leg: the real captured line comes through as one event...
                failed, events = chk.parse_all(remote(
                    "supernote-ingest.path", "", "0", "active", journal=REAL_EVENT, shell=shell))
                self.assertEqual(failed, [])
                self.assertEqual(events, [(REAL_TS, "supernote-ingest.service", "exit-code")])
                # ...journalctl's own rc reaches the sentinel and fails closed...
                with self.assertRaisesRegex(chk.CheckUnreachable, "journalctl rc=1"):
                    chk.parse_all(remote("supernote-ingest.path", "", "0", "active", jrc="1",
                                         shell=shell))
                # ...and journalctl got exactly the argv the brief names, --since as one token.
                remote("supernote-ingest.path", "", "0", "active", since=1793026063, shell=shell)
                self.assertEqual(jarglog.read_text().splitlines(), [
                    "--user", "-o", "json", "--output-fields=USER_UNIT,UNIT_RESULT", "--no-pager",
                    "--since", "@1793026063", "MESSAGE_ID=d9b373ed55a64feb8242e02dbe79a49c"])
        remote(r"memex-a\x2db.path", "", "0", "active")
        self.assertEqual(arglog.read_text(), r"memex-a\x2db.path", "canary reaches is-active intact")

    def test_since_must_be_a_plain_int(self) -> None:
        # S is formatted by this tool from an int; nothing else may reach the shell.
        for bad in ("1; rm -rf ~", 1.5, -1, True, None):
            with self.subTest(bad=bad), self.assertRaises((TypeError, ValueError)):
                chk.remote_command("supernote-ingest.path", bad)


class TestHostAndCanary(_Base):
    def _argv_used(self, argv: list[str], env: dict) -> list[str]:
        runner = FakeRunner(_out())
        with patch.dict(os.environ, env, clear=False), redirect_stdout(io.StringIO()):
            for key in (chk.HOST_ENV, chk.CANARY_ENV):
                if key not in env:
                    os.environ.pop(key, None)
            chk.main([*argv, "--state", str(self.state)], runner=runner, clock=lambda: NOW,
                     send=FakeSend())
        return runner.calls[0][0]

    def test_host_default_env_and_flag_precedence(self) -> None:
        self.assertEqual(self._argv_used([], {})[5], "memex203")
        self.assertEqual(self._argv_used([], {chk.HOST_ENV: "envhost"})[5], "envhost")
        self.assertEqual(self._argv_used(["--host", "flaghost"], {chk.HOST_ENV: "envhost"})[5],
                         "flaghost")

    def test_canary_default_env_and_flag_precedence(self) -> None:
        def used(argv, env):
            return self._argv_used(argv, env)[6].rsplit("is-active ", 1)[1]
        self.assertEqual(used([], {}), 'supernote-ingest.path)"')
        self.assertEqual(used([], {chk.CANARY_ENV: "env-canary.timer"}), 'env-canary.timer)"')
        self.assertEqual(used(["--canary", "flag-canary.service"], {chk.CANARY_ENV: "env-canary.timer"}),
                         'flag-canary.service)"')

    def test_canary_is_shell_quoted(self) -> None:
        self.assertIn(r"is-active 'memex-a\x2db.path')", chk.remote_command(r"memex-a\x2db.path", 0))

    def test_option_shaped_host_is_refused_before_ssh(self) -> None:
        for argv, env in ((["--host=-oProxyCommand=x"], {}),
                          ([], {chk.HOST_ENV: "-oProxyCommand=x"})):
            with self.subTest(argv=argv, env=env):
                runner = FakeRunner(_out())
                with patch.dict(os.environ, env), redirect_stdout(io.StringIO()):
                    code = chk.main([*argv, "--state", str(self.state)],
                                    runner=runner, clock=lambda: NOW, send=FakeSend())
                self.assertEqual((code, runner.calls), (3, []))

    def test_non_unit_canary_is_refused_before_ssh(self) -> None:
        for argv, env in ((["--canary", "x.path; rm -rf ~"], {}),
                          (["--canary=$(id).path"], {}),
                          ([], {chk.CANARY_ENV: "supernote-ingest"})):
            with self.subTest(argv=argv, env=env):
                runner = FakeRunner(_out())
                buf = io.StringIO()
                with patch.dict(os.environ, env), redirect_stdout(buf):
                    code = chk.main([*argv, "--state", str(self.state)],
                                    runner=runner, clock=lambda: NOW, send=FakeSend())
                self.assertEqual((code, runner.calls), (3, []))
                self.assertIn("invalid canary", buf.getvalue())


class TestMailAndExit(_Base):
    ONE = _out(_line("memex-drop.service"))
    TWO = _out(_line("memex-drop.service"), _line("memex-backup.service"))

    def test_first_sight_mails_exits_zero_and_writes_state(self) -> None:
        code, _, send, out = self.run_check(self.ONE)
        self.assertEqual(code, 0)
        self.assertEqual(len(send.calls), 1)
        self.assertEqual(self.read_state(),
                         {"failed": ["memex-drop.service"], "mailed_at": "2026-10-06T05:52:00Z",
                          "events_seen_until": FLOOR})
        self.assertIn("mailed archivist", out)
        self.assertEqual(len(out.strip().splitlines()), 1, "one log line per run")
        self.assertEqual([p.name for p in self.state.parent.iterdir()], [self.state.name],
                         "atomic write leaves no temp file behind")

    def test_same_set_within_20h_is_not_remailed(self) -> None:
        self.write_state(["memex-drop.service"], NOW - timedelta(hours=19, minutes=59))
        code, _, send, out = self.run_check(self.ONE)
        self.assertEqual((code, send.calls), (0, []))
        self.assertIn("already mailed", out)

    def test_changed_set_mails(self) -> None:
        self.write_state(["memex-drop.service"], NOW - timedelta(hours=1))
        code, _, send, _ = self.run_check(self.TWO)
        self.assertEqual((code, len(send.calls)), (0, 1))
        self.assertEqual(self.read_state()["failed"], ["memex-backup.service", "memex-drop.service"])

    def test_shrunk_set_mails(self) -> None:
        self.write_state(["memex-backup.service", "memex-drop.service"], NOW - timedelta(hours=1))
        code, _, send, _ = self.run_check(self.ONE)
        self.assertEqual((code, len(send.calls)), (0, 1))

    def test_20h_reminder_mails(self) -> None:
        self.write_state(["memex-drop.service"], NOW - timedelta(hours=20))
        code, _, send, _ = self.run_check(self.ONE)
        self.assertEqual((code, len(send.calls)), (0, 1))
        self.assertEqual(self.read_state()["mailed_at"], "2026-10-06T05:52:00Z")

    def test_future_mailed_at_is_not_trusted(self) -> None:
        # A clock-skewed or corrupted future stamp would otherwise suppress the set forever.
        self.write_state(["memex-drop.service"], NOW + timedelta(days=365))
        code, _, send, _ = self.run_check(self.ONE)
        self.assertEqual((code, len(send.calls)), (0, 1))

    def test_empty_set_clears_state_so_a_recurrence_mails_again(self) -> None:
        self.write_state(["memex-drop.service"], NOW - timedelta(hours=1))
        code, _, send, _ = self.run_check(_out())
        self.assertEqual((code, send.calls), (0, []))
        # The failed-set dedupe is gone; only the queried event floor remains (T-5602).
        self.assertEqual(self.read_state(), {"events_seen_until": FLOOR})
        code, _, send, _ = self.run_check(self.ONE, now=NOW + timedelta(hours=1))
        self.assertEqual((code, len(send.calls)), (0, 1))

    def test_unverified_empty_read_does_not_clear_state(self) -> None:
        # A sentinel-less "empty" read is not proof the set emptied: the dedupe must survive it.
        self.write_state(["memex-drop.service"], NOW - timedelta(hours=1))
        code, _, _, _ = self.run_check("")
        self.assertEqual(code, 3)
        self.assertTrue(self.state.exists())

    def test_send_raising_exits_1_and_writes_no_state(self) -> None:
        code, _, send, out = self.run_check(self.ONE, send=FakeSend(exc=PermissionError("hub/new")))
        self.assertEqual(code, 1)
        self.assertEqual(len(send.calls), 1)
        self.assertFalse(self.state.exists(), "state must be written only after a successful send")
        self.assertIn("mail to archivist FAILED", out)

    def test_send_raising_leaves_prior_state_untouched(self) -> None:
        self.write_state(["memex-drop.service"], NOW - timedelta(hours=1))
        before = self.state.read_text()
        code, _, _, _ = self.run_check(self.TWO, send=FakeSend(exc=OSError("hub gone")))
        self.assertEqual(code, 1)
        self.assertEqual(self.state.read_text(), before)
        # ...so the NEXT run retries the mail rather than believing it was delivered.
        code, _, send, _ = self.run_check(self.TWO)
        self.assertEqual((code, len(send.calls)), (0, 1))

    def test_corrupt_state_is_treated_as_never_mailed(self) -> None:
        for junk in ("{not json", json.dumps({"failed": "x"}), json.dumps({"failed": ["a.service"]}),
                     json.dumps({"failed": ["memex-drop.service"], "mailed_at": "yesterday"}), "[]"):
            with self.subTest(junk=junk):
                self.state.parent.mkdir(parents=True, exist_ok=True)
                self.state.write_text(junk)
                code, _, send, out = self.run_check(self.ONE)
                self.assertEqual((code, len(send.calls)), (0, 1))
                self.assertIn("treating as never mailed", out)
                self.assertEqual(self.read_state()["failed"], ["memex-drop.service"])

    def test_unreadable_state_is_treated_as_never_mailed(self) -> None:
        self.state.mkdir(parents=True)  # a directory where the file should be: read raises
        code, _, send, out = self.run_check(self.ONE)
        self.assertEqual(len(send.calls), 1)
        self.assertIn("treating as never mailed", out)
        # T-5602 review: mail delivered, but a state that cannot be written is a broken check.
        self.assertEqual(code, 1, "state not persisted -> exit 1 -> OnFailure= -> Recovery")
        self.assertIn("state not persisted", out)

    def test_state_write_failing_after_a_delivered_mail_exits_1(self) -> None:
        for stdout, now in ((self.ONE, NOW), (_out(journal=[REAL_EVENT]), EV_NOW)):
            with self.subTest(stdout=stdout), \
                    patch.object(chk, "write_state", side_effect=OSError("disk full")):
                code, _, send, out = self.run_check(stdout, now=now)
                self.assertEqual((code, len(send.calls)), (1, 1), "mailed once, then exit 1")
                self.assertIn("mailed archivist, but state write", out)
                self.assertIn("state not persisted", out)
                self.assertFalse(self.state.exists())

    def test_state_write_failing_on_an_unmailed_run_exits_1(self) -> None:
        # Both unmailed-state paths: the empty-set clear (writing the floor/cursor) and the
        # deduped failed set gaining the floor.
        cases = (
            ("empty set, no state", None, _out()),
            ("empty set, dedupe to clear", ["memex-drop.service"], _out()),
            ("deduped set gains the floor", ["memex-drop.service"], self.ONE),
        )
        for name, prior, stdout in cases:
            with self.subTest(case=name):
                if prior:
                    self.write_state(prior, NOW - timedelta(hours=1))
                elif self.state.exists():
                    self.state.unlink()
                before = self.state.read_text() if self.state.exists() else None
                with patch.object(chk, "write_state", side_effect=PermissionError("ro")):
                    code, _, send, out = self.run_check(stdout)
                self.assertEqual((code, send.calls), (1, []))
                self.assertIn("state not persisted", out)
                after = self.state.read_text() if self.state.exists() else None
                self.assertEqual(after, before)

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_really_unwritable_state_dir_exits_1_on_both_paths(self) -> None:
        # No patching: a read-only state directory, as the real filesystem refuses it.
        self.state.parent.mkdir(parents=True)
        self.state.parent.chmod(0o500)
        self.addCleanup(self.state.parent.chmod, 0o700)
        code, _, send, out = self.run_check(self.ONE)  # mail path
        self.assertEqual((code, len(send.calls)), (1, 1))
        self.assertIn("mailed archivist, but state write", out)
        code, _, send, out = self.run_check(_out())  # unmailed path (writes the floor)
        self.assertEqual((code, send.calls), (1, []))
        self.assertIn("state not persisted", out)

    def test_no_mail_never_sends_and_exits_1_on_failures(self) -> None:
        code, _, send, out = self.run_check(self.ONE, extra=["--no-mail"])
        self.assertEqual((code, send.calls), (1, []))
        self.assertFalse(self.state.exists(), "report mode never writes state")
        self.assertIn("--no-mail", out)

    def test_no_mail_clean_exits_0_and_leaves_state(self) -> None:
        self.write_state(["memex-drop.service"], NOW - timedelta(hours=1))
        code, _, send, _ = self.run_check(_out(), extra=["--no-mail"])
        self.assertEqual((code, send.calls), (0, []))
        self.assertTrue(self.state.exists(), "report mode does not touch the timer's dedupe")

    def test_no_mail_still_fails_closed_when_unreachable(self) -> None:
        self.assertEqual(self.run_check(rc=255, extra=["--no-mail"])[0], 3)
        self.assertEqual(self.run_check("", extra=["--no-mail"])[0], 3)


class TestFailureEvents(_Base):
    """T-5602: a unit that failed and was healed by a later run before the poll still mails."""

    EV = _out(journal=[REAL_EVENT])
    LATER = REAL_TS + 3_600_000_000  # one hour after the real failure

    def run_ev(self, stdout: str, **kw):
        kw.setdefault("now", EV_NOW)
        return self.run_check(stdout, **kw)

    def write_raw_state(self, obj) -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(json.dumps(obj))

    def since_token(self, runner: FakeRunner) -> str:
        """The value of the --since TOKEN in the remote command (shell-tokenised, never a
        substring of the joined line)."""
        tokens = shlex.split(runner.calls[0][0][6])
        self.assertEqual(tokens.count("--since"), 1)
        return tokens[tokens.index("--since") + 1]

    def test_parse_all_reads_the_real_captured_line(self) -> None:
        self.assertEqual(chk.parse_all(self.EV),
                         ([], [(REAL_TS, "supernote-ingest.service", "exit-code")]))
        self.assertEqual(chk.parse_all(_out()), ([], []))

    def test_healed_failure_mails_and_the_inclusive_rerun_does_not_remail(self) -> None:
        code, _, send, out = self.run_ev(self.EV)
        self.assertEqual((code, len(send.calls)), (0, 1))
        (subject, payload), = send.calls
        self.assertEqual(subject, "memex 203: 1 failure event(s) since last check: "
                                  "supernote-ingest.service (exit-code)")
        self.assertIn("2026-10-26T14:47:43Z supernote-ingest.service (exit-code)", payload)
        self.assertIn("ssh memex203 'journalctl --user -u supernote-ingest.service -n 50'", payload)
        self.assertIn("this unit may already be healthy again: a later run can clear the failed "
                      "state", payload)
        self.assertEqual(self.read_state(), {"events_seen_until": REAL_TS})
        self.assertIn("mailed archivist", out)
        self.assertEqual(len(out.strip().splitlines()), 1, "one log line per run")
        # The next poll: --since is the cursor's SECOND (inclusive), so 203 returns the same
        # line again. It must not re-mail, and the cursor must not move.
        code, runner, send, out = self.run_ev(self.EV, now=EV_NOW + timedelta(hours=6))
        self.assertEqual(self.since_token(runner), f"@{REAL_TS // 1_000_000}")
        self.assertEqual((code, send.calls), (0, []))
        self.assertEqual(self.read_state(), {"events_seen_until": REAL_TS})

    def test_only_events_after_the_cursor_are_mailed(self) -> None:
        self.write_raw_state({"events_seen_until": REAL_TS})
        same_second = REAL_TS + 1  # same --since second, a later microsecond: new
        code, _, send, _ = self.run_ev(_out(journal=[
            REAL_EVENT, _event(unit="memex-backup.service", result="timeout", ts=same_second)]))
        self.assertEqual((code, len(send.calls)), (0, 1))
        (subject, payload), = send.calls
        self.assertEqual(subject, "memex 203: 1 failure event(s) since last check: "
                                  "memex-backup.service (timeout)")
        self.assertNotIn("supernote-ingest.service", payload, "the already-mailed event is not repeated")
        self.assertEqual(self.read_state(), {"events_seen_until": same_second})

    def test_several_events_sorted_and_cursor_is_their_max(self) -> None:
        code, _, send, _ = self.run_ev(_out(journal=[
            _event(ts=self.LATER, result="signal"), REAL_EVENT]))
        self.assertEqual((code, len(send.calls)), (0, 1))
        (subject, payload), = send.calls
        self.assertEqual(subject, "memex 203: 2 failure event(s) since last check: "
                                  "supernote-ingest.service (exit-code), "
                                  "supernote-ingest.service (signal)")
        self.assertLess(payload.index("14:47:43Z"), payload.index("15:47:43Z"))
        self.assertEqual(payload.count("journalctl --user -u supernote-ingest.service"), 1,
                         "one inspect hint per unit")
        self.assertEqual(self.read_state()["events_seen_until"], self.LATER)

    def test_failed_set_and_event_in_one_run_is_one_mail(self) -> None:
        code, _, send, _ = self.run_ev(_out(_line("memex-drop.service"), journal=[REAL_EVENT]))
        self.assertEqual((code, len(send.calls)), (0, 1))
        (subject, payload), = send.calls
        self.assertEqual(subject, "memex 203: 1 failed unit(s): memex-drop.service; "
                                  "1 failure event(s) since last check: "
                                  "supernote-ingest.service (exit-code)")
        self.assertIn("- memex-drop.service", payload)
        self.assertIn("supernote-ingest.service (exit-code)", payload)
        self.assertEqual(self.read_state(), {"failed": ["memex-drop.service"],
                                             "mailed_at": "2026-10-26T17:52:00Z",
                                             "events_seen_until": REAL_TS})

    def test_new_event_mails_even_when_the_failed_set_is_deduped(self) -> None:
        self.write_raw_state({"failed": ["memex-drop.service"], "mailed_at": "2026-10-26T16:00:00Z",
                              "events_seen_until": REAL_TS - 1})
        code, _, send, _ = self.run_ev(_out(_line("memex-drop.service"), journal=[REAL_EVENT]))
        self.assertEqual((code, len(send.calls)), (0, 1))
        self.assertEqual(self.read_state()["events_seen_until"], REAL_TS)

    def test_send_raising_exits_1_and_does_not_advance_the_cursor(self) -> None:
        self.write_raw_state({"events_seen_until": REAL_TS - 10})
        before = self.state.read_text()
        code, _, send, out = self.run_ev(self.EV, send=FakeSend(exc=OSError("hub gone")))
        self.assertEqual((code, len(send.calls)), (1, 1))
        self.assertEqual(self.state.read_text(), before)
        self.assertIn("mail to archivist FAILED", out)
        # ...so the next run re-reads the same event and retries the mail.
        code, _, send, _ = self.run_ev(self.EV)
        self.assertEqual((code, len(send.calls)), (0, 1))
        self.assertEqual(self.read_state()["events_seen_until"], REAL_TS)

    def test_send_raising_with_no_state_writes_none(self) -> None:
        code, _, _, _ = self.run_ev(self.EV, send=FakeSend(exc=OSError("hub gone")))
        self.assertEqual(code, 1)
        self.assertFalse(self.state.exists())

    def test_empty_set_and_no_events_clears_the_dedupe_but_keeps_the_cursor(self) -> None:
        self.write_raw_state({"failed": ["memex-drop.service"], "mailed_at": "2026-10-26T16:00:00Z",
                              "events_seen_until": REAL_TS})
        code, runner, send, _ = self.run_ev(self.EV)  # inclusive re-read: no NEW event
        self.assertEqual((code, send.calls), (0, []))
        self.assertEqual(self.read_state(), {"events_seen_until": REAL_TS})
        self.assertEqual(self.since_token(runner), f"@{REAL_TS // 1_000_000}")
        # The failed-set dedupe really is cleared: the same set recurring mails again.
        code, _, send, _ = self.run_ev(_out(_line("memex-drop.service"), journal=[REAL_EVENT]),
                                       now=EV_NOW + timedelta(hours=1))
        self.assertEqual((code, len(send.calls)), (0, 1))
        self.assertEqual(self.read_state()["events_seen_until"], REAL_TS, "cursor survives")

    def test_cursor_only_state_survives_an_empty_run_untouched(self) -> None:
        self.write_raw_state({"events_seen_until": REAL_TS})
        code, _, send, _ = self.run_ev(_out())
        self.assertEqual((code, send.calls), (0, []))
        self.assertEqual(self.read_state(), {"events_seen_until": REAL_TS})

    def test_no_state_since_is_now_minus_7h(self) -> None:
        code, runner, _, _ = self.run_ev(_out())
        self.assertEqual(code, 0)
        self.assertEqual(self.since_token(runner),
                         f"@{int((EV_NOW - timedelta(hours=7)).timestamp())}")
        self.assertEqual(self.read_state(), {"events_seen_until": EV_FLOOR})

    def test_first_empty_read_persists_the_floor_for_the_next_since(self) -> None:
        # (1) The next run queries from the persisted floor, NOT its own now-7h: an outage
        # longer than the lookback no longer skips the events it missed.
        code, _, send, _ = self.run_ev(_out())
        self.assertEqual((code, send.calls), (0, []))
        self.assertEqual(self.read_state(), {"events_seen_until": EV_FLOOR})
        later = EV_NOW + timedelta(days=2)
        code, runner, send, _ = self.run_ev(_out(), now=later)
        self.assertEqual(self.since_token(runner), f"@{EV_FLOOR_S}")
        self.assertNotEqual(self.since_token(runner),
                            f"@{int((later - timedelta(hours=7)).timestamp())}")
        self.assertEqual(self.read_state(), {"events_seen_until": EV_FLOOR}, "cursor unchanged")
        # An event the outage would have skipped (older than the second run's now-7h) mails.
        code, _, send, _ = self.run_ev(self.EV, now=later)
        self.assertEqual((code, len(send.calls)), (0, 1))
        self.assertEqual(self.read_state(), {"events_seen_until": REAL_TS})

    def test_floor_never_moves_the_cursor_backwards(self) -> None:
        # A first-read event newer than the floor wins; the floor is the lower bound only.
        code, _, send, _ = self.run_ev(self.EV)
        self.assertEqual((code, len(send.calls)), (0, 1))
        self.assertGreater(REAL_TS, EV_FLOOR)
        self.assertEqual(self.read_state(), {"events_seen_until": REAL_TS})

    def test_deduped_failed_set_without_cursor_gains_the_floor_keeping_mailed_at(self) -> None:
        self.write_state(["memex-drop.service"], EV_NOW - timedelta(hours=1))
        code, _, send, _ = self.run_ev(_out(_line("memex-drop.service")))
        self.assertEqual((code, send.calls), (0, []))
        self.assertEqual(self.read_state(), {"failed": ["memex-drop.service"],
                                             "mailed_at": "2026-10-26T16:52:00Z",
                                             "events_seen_until": EV_FLOOR})

    def test_floor_is_not_written_when_the_send_fails(self) -> None:
        # (2) For both mail shapes: an event, and a failed set with no event.
        for stdout in (self.EV, _out(_line("memex-drop.service"))):
            with self.subTest(stdout=stdout):
                code, _, _, _ = self.run_ev(stdout, send=FakeSend(exc=OSError("hub gone")))
                self.assertEqual(code, 1)
                self.assertFalse(self.state.exists(), "a failed send writes nothing, not even the floor")
        self.write_state(["memex-drop.service"], EV_NOW - timedelta(hours=1))  # old, no cursor
        before = self.state.read_text()
        self.assertEqual(self.run_ev(_out(_line("memex-backup.service")),
                                     send=FakeSend(exc=OSError("hub gone")))[0], 1)
        self.assertEqual(self.state.read_text(), before)

    def test_no_mail_never_writes_the_floor(self) -> None:
        # (3) Report mode, with no state and with an old cursor-less state.
        for stdout in (_out(), self.EV, _out(_line("memex-drop.service"))):
            with self.subTest(stdout=stdout):
                self.assertIn(self.run_ev(stdout, extra=["--no-mail"])[0], (0, 1))
                self.assertFalse(self.state.exists())
        self.write_state(["memex-drop.service"], EV_NOW - timedelta(hours=1))
        before = self.state.read_text()
        for stdout in (_out(), _out(_line("memex-drop.service"))):
            with self.subTest(stdout=stdout, state="old"):
                self.run_ev(stdout, extra=["--no-mail"])
                self.assertEqual(self.state.read_text(), before)

    def test_old_state_without_cursor_still_loads(self) -> None:
        # A pre-T-5602 state file: dedupe still honoured, since falls back to now-7h.
        self.write_state(["memex-drop.service"], EV_NOW - timedelta(hours=1))
        code, runner, send, out = self.run_ev(_out(_line("memex-drop.service")))
        self.assertEqual((code, send.calls), (0, []))
        self.assertIn("already mailed", out)
        self.assertNotIn("treating as", out)
        self.assertEqual(self.since_token(runner),
                         f"@{int((EV_NOW - timedelta(hours=7)).timestamp())}")

    def test_corrupt_cursor_is_treated_as_missing(self) -> None:
        for junk in ("1793026063809278", -5, 1.5, True, None, [1]):
            with self.subTest(junk=junk):
                self.write_raw_state({"events_seen_until": junk})
                code, runner, send, out = self.run_ev(self.EV)
                self.assertEqual(self.since_token(runner),
                                 f"@{int((EV_NOW - timedelta(hours=7)).timestamp())}")
                self.assertEqual((code, len(send.calls)), (0, 1), "an extra mail, never a suppressed one")
                self.assertIn("treating as missing", out)
                self.assertEqual(self.read_state(), {"events_seen_until": REAL_TS})

    def test_future_cursor_is_not_trusted(self) -> None:
        # A cursor beyond now would suppress every event forever: treat it as missing.
        self.write_raw_state({"events_seen_until": REAL_TS + 10 * 86_400_000_000})
        code, runner, send, _ = self.run_ev(self.EV)
        self.assertEqual(self.since_token(runner),
                         f"@{int((EV_NOW - timedelta(hours=7)).timestamp())}")
        self.assertEqual((code, len(send.calls)), (0, 1))

    def test_no_mail_reports_events_exits_1_and_never_touches_state(self) -> None:
        code, _, send, out = self.run_ev(self.EV, extra=["--no-mail"])
        self.assertEqual((code, send.calls), (1, []))
        self.assertFalse(self.state.exists())
        self.assertIn("supernote-ingest.service (exit-code)", out)
        self.assertIn("--no-mail", out)
        self.write_raw_state({"failed": ["memex-drop.service"], "mailed_at": "2026-10-26T16:00:00Z",
                              "events_seen_until": REAL_TS - 1})
        before = self.state.read_text()
        self.assertEqual(self.run_ev(self.EV, extra=["--no-mail"])[0], 1)
        self.assertEqual(self.run_ev(_out(), extra=["--no-mail"])[0], 0)
        self.assertEqual(self.state.read_text(), before, "report mode never writes or clears")
        # An already-seen event is not "new" in report mode either.
        self.write_raw_state({"events_seen_until": REAL_TS})
        self.assertEqual(self.run_ev(self.EV, extra=["--no-mail"])[0], 0)


class TestJournalFailsClosed(_Base):
    LEAK = "LEAK-dear-diary-page-42"

    def _fails_closed(self, stdout: str, says: str) -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(json.dumps({"events_seen_until": REAL_TS - 1}))
        before = self.state.read_text()
        code, _, send, out = self.run_check(stdout, now=EV_NOW)
        self.assertEqual(code, 3, out)
        self.assertEqual(send.calls, [])
        self.assertIn(says, out)
        self.assertEqual(len(out.strip().splitlines()), 1, "one log line naming the condition")
        for frag in ("LEAK", "diary", "__CURSOR", "07b11fee", "{"):
            self.assertNotIn(frag, out, "no raw journal line is echoed")
        self.assertEqual(self.state.read_text(), before, "an unreadable answer touches no state")

    def test_nonzero_jrc_fails_closed(self) -> None:
        self._fails_closed(_out(end="__MEMEX_FUC_END rc=0 jrc=1 canary=active"), "journalctl rc=1")

    def test_missing_journal_marker_fails_closed(self) -> None:
        self._fails_closed("\n".join([_line("memex-drop.service"), END_OK]) + "\n",
                           "missing journal marker")
        self._fails_closed("\n".join([REAL_EVENT, END_OK]) + "\n", "missing journal marker")

    def test_duplicate_journal_marker_fails_closed(self) -> None:
        self._fails_closed(_out(journal=[MARKER, REAL_EVENT]), "repeated journal marker")

    def test_non_json_journal_line_fails_closed(self) -> None:
        for line in (f"-- No entries -- {self.LEAK}", "{" + self.LEAK, f'["{self.LEAK}"]',
                     json.dumps(self.LEAK)):
            with self.subTest(line=line):
                self._fails_closed(_out(journal=[REAL_EVENT, line]),
                                   "1 journal line(s) are not a unit-failed event")

    def test_bad_user_unit_fails_closed(self) -> None:
        for unit in (self.LEAK, "x.service; rm -rf ~", "", "memex-drop"):
            with self.subTest(unit=unit):
                self._fails_closed(_out(journal=[_event(unit=unit)]),
                                   "1 journal line(s) are not a unit-failed event")
        obj = json.loads(REAL_EVENT)
        del obj["USER_UNIT"]
        self._fails_closed(_out(journal=[json.dumps(obj)]), "are not a unit-failed event")
        obj["USER_UNIT"] = [104, 105]  # journald's array form for a binary field
        self._fails_closed(_out(journal=[json.dumps(obj)]), "are not a unit-failed event")

    def test_bad_unit_result_fails_closed(self) -> None:
        for result in (self.LEAK, "", "exit code", "x" * 33):
            with self.subTest(result=result):
                self._fails_closed(_out(journal=[_event(result=result)]),
                                   "1 journal line(s) are not a unit-failed event")

    def test_non_digit_timestamp_fails_closed(self) -> None:
        for ts in ("1793026063.809278", "-1", "", self.LEAK, "1" * 40):
            with self.subTest(ts=ts):
                self._fails_closed(_out(journal=[_event(ts=ts)]),
                                   "1 journal line(s) are not a unit-failed event")
        obj = json.loads(REAL_EVENT)
        obj["__REALTIME_TIMESTAMP"] = REAL_TS  # a JSON number, not journald's string
        self._fails_closed(_out(journal=[json.dumps(obj)]), "are not a unit-failed event")


class TestOffTailnet(_Base):
    """T-5388: while THIS host is off the tailnet, an ssh failure is not 203's fault."""

    def test_ssh_failure_while_off_tailnet_is_suppressed_and_reported_once(self) -> None:
        self.tailnet.state = "NeedsLogin"
        code, _, send, out = self.run_check(rc=255)
        self.assertEqual(code, 0)
        self.assertEqual(send.calls, [])
        self.assertEqual([(m, st) for m, st, _ in self.tailnet.reports],
                         [("memex-failed-unit-check", "NeedsLogin")])
        self.assertIn("ssh rc=255", self.tailnet.reports[0][2])
        self.assertNotIn("UNREACHABLE", out)

    def test_ssh_timeout_while_off_tailnet_is_suppressed(self) -> None:
        self.tailnet.state = "Stopped"
        code, *_ = self.run_check(exc=subprocess.TimeoutExpired("ssh", 90))
        self.assertEqual((code, len(self.tailnet.reports)), (0, 1))

    def test_unknown_tailnet_state_still_fails_closed(self) -> None:
        self.tailnet.state = None
        code, _, _, out = self.run_check(rc=255)
        self.assertEqual((code, self.tailnet.reports), (3, []))
        self.assertIn("UNREACHABLE", out)

    def test_parse_failure_is_never_suppressed(self) -> None:
        # ssh SUCCEEDED, so the tailnet carried it: a bad answer is 203's, whatever tailscale says.
        self.tailnet.state = "NeedsLogin"
        code, *_ = self.run_check("garbage\n")
        self.assertEqual((code, self.tailnet.reports), (3, []))

    def test_success_rearms_the_outage_alert(self) -> None:
        code, *_ = self.run_check(_out())
        self.assertEqual((code, self.tailnet.reachable), (0, 1))

    def test_failed_units_still_mail_and_rearm(self) -> None:
        code, _, send, _ = self.run_check(_out(_line("memex-drop.service")))
        self.assertEqual((code, len(send.calls), self.tailnet.reachable), (0, 1, 1))


class TestPayloadIsContentBlind(_Base):
    SENTINEL = "SENTINEL-journal-leak-7f3a Oct 05 05:17 drop: page 42 'dear diary'"

    def test_description_text_never_reaches_the_mail_or_log(self) -> None:
        text = _out(_line("memex-drop.service", self.SENTINEL),
                    f"● memex-leg.service loaded failed failed {self.SENTINEL}")
        code, _, send, out = self.run_check(text)
        self.assertEqual(code, 0)
        (subject, payload), = send.calls
        for frag in ("SENTINEL", "dear diary", "page 42", "7f3a"):
            self.assertNotIn(frag, subject)
            self.assertNotIn(frag, payload)
            self.assertNotIn(frag, out)
        self.assertEqual(subject, "memex 203: 2 failed unit(s): memex-drop.service, memex-leg.service")

    def test_payload_is_names_plus_fixed_text(self) -> None:
        subject, payload = chk.build_mail(["memex-drop.service", "memex-leg.service"], "memex203")
        self.assertIn("ssh memex203 'journalctl --user -u memex-drop.service -n 50'", payload)
        self.assertIn("ssh memex203 'journalctl --user -u memex-leg.service -n 50'", payload)
        self.assertIn("broken service => ticket (file first, diagnose after)", payload)
        # Everything left after removing the names is the same fixed template for any input.
        other_s, other_p = chk.build_mail(["x.service", "y.service"], "memex203")
        strip = lambda s, names: re.sub("|".join(map(re.escape, names)), "<U>", s)  # noqa: E731
        self.assertEqual(strip(payload, ["memex-drop.service", "memex-leg.service"]),
                         strip(other_p, ["x.service", "y.service"]))
        self.assertEqual(strip(subject, ["memex-drop.service", "memex-leg.service"]),
                         strip(other_s, ["x.service", "y.service"]))

    def test_subject_names_truncated_to_200_chars(self) -> None:
        names = [f"memex-leg-{i:03d}.service" for i in range(40)]
        subject, payload = chk.build_mail(names, "memex203")
        head = "memex 203: 40 failed unit(s): "
        self.assertTrue(subject.startswith(head))
        self.assertEqual(len(subject) - len(head), 200)
        self.assertTrue(subject.endswith("..."))
        for n in names:  # the payload is not truncated
            self.assertIn(f"- {n}", payload)


def _unit_sections(path: Path) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    cur = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = sections.setdefault(line[1:-1], [])
        else:
            assert cur is not None, f"{path.name}: directive outside a section: {line}"
            cur.append(line)
    return sections


class TestUnitFiles(unittest.TestCase):
    def test_service(self) -> None:
        s = _unit_sections(SERVICE)
        self.assertIn("OnFailure=pantheon-unit-failure-alert@%n.service", s["Unit"])
        # REBOUND-1: network-online.target is a no-op in a --user manager; not carried.
        self.assertFalse([d for d in s["Unit"] if "network-online.target" in d])
        self.assertTrue(any(d.startswith("Description=") for d in s["Unit"]))
        self.assertIn("Documentation=file:///home/%u/pantheon-config/tools/memex_failed_unit_check.py",
                      s["Unit"])
        svc = s["Service"]
        self.assertIn("Type=oneshot", svc)
        self.assertIn('ExecStart=/usr/bin/sg pantheon-mailbox -c '
                      '"/usr/bin/python3 %h/pantheon-config/tools/memex_failed_unit_check.py"', svc)
        self.assertEqual(len([d for d in svc if d.startswith("ExecStart=")]), 1)
        self.assertIn("WorkingDirectory=%h/pantheon-config", svc)
        self.assertIn("Environment=PATH=/usr/local/bin:/usr/bin:/bin", svc)
        self.assertIn("TimeoutStartSec=5min", svc)
        self.assertIn("StandardOutput=journal", svc)
        # A --user unit cannot take SupplementaryGroups= (fails 216); the sg wrap replaces it.
        self.assertFalse([d for d in svc if d.startswith("SupplementaryGroups=")])

    def test_timer(self) -> None:
        sections = _unit_sections(TIMER)
        t = sections["Timer"]
        self.assertIn("OnCalendar=*-*-* 05,11,17,23:52:00 UTC", t)
        # REBOUND-1: no Persistent= — a boot catch-up fire would race tailscale into a false exit 3.
        self.assertFalse([d for d in t if d.startswith("Persistent=")])
        self.assertIn(f"Unit={SERVICE.name}", t)
        self.assertIn("WantedBy=timers.target", sections["Install"])


if __name__ == "__main__":
    unittest.main()
