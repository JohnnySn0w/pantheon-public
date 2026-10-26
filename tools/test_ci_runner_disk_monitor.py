"""T-4176: CT202 free-space monitor mails RECOVERY on a breach and fails LOUD on its own probe."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import ci_runner_disk_monitor as m

DF_HEADER = "Filesystem 1024-blocks Used Available Capacity Mounted on\n"


def _df(root_pct: int, cache_pct: int) -> str:
    return (DF_HEADER
            + f"/dev/mapper/pve-vm--202--disk--0 82447192 1 1 {root_pct}% /\n"
            + f"/dev/mapper/pve-ci--cache 102626232 1 1 {cache_pct}% /ci-cache\n")


def _runner(df_out=None, *, rc=0, raise_exc=None, du_out="11G\t/opt/actions-runner-pc/_work\n"):
    calls = []

    def run(*cmd, timeout):
        calls.append(cmd)
        if cmd[0] == "df":
            if raise_exc is not None:
                raise raise_exc
            return subprocess.CompletedProcess(cmd, rc, df_out or "", "ssh: connect refused" if rc else "")
        return subprocess.CompletedProcess(cmd, 0, du_out, "")
    run.calls = calls
    return run


_TAILNET_PATCHES = []


def setUpModule() -> None:
    # T-5385: the probe-failure path reads THIS host's tailscale state. Pin it to "on the
    # tailnet" so these tests never depend on the box they run on; the off case is tested below.
    for name, value in (("off_tailnet", None),):
        p = patch.object(m.tailnet_self, name, return_value=value)
        p.start()
        _TAILNET_PATCHES.append(p)
    for name in ("note_reachable", "report_off_tailnet"):
        p = patch.object(m.tailnet_self, name)
        p.start()
        _TAILNET_PATCHES.append(p)


def tearDownModule() -> None:
    for p in _TAILNET_PATCHES:
        p.stop()


class CiRunnerDiskMonitorTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch.object(m, "STATE_PATH", Path(tmp.name) / "state.json")
        p.start()
        self.addCleanup(p.stop)
        self.sent = []

    def _send(self, subject, body, severity):
        self.sent.append((subject, body, severity))

    def _check(self, run, now=1000.0, dry_run=False, send=None):
        return m.check(dry_run=dry_run, now=now, run=run, send=send or self._send)

    def test_parse_real_df_output(self) -> None:
        self.assertEqual(m.parse_df(_df(27, 16)), {"/": 27, "/ci-cache": 16})

    def test_ok_sends_nothing(self) -> None:
        self.assertEqual(self._check(_runner(_df(27, 16))), 0)
        self.assertEqual(self.sent, [])

    def test_warn_mails_recovery_with_top_consumers(self) -> None:
        self.assertEqual(self._check(_runner(_df(87, 16))), 0)
        [(subject, body, severity)] = self.sent
        self.assertIn("ci-runner-disk-warn", subject)
        self.assertIn("/opt/actions-runner-pc/_work", body)
        self.assertEqual(severity, "normal")

    def test_top_consumers_sizes_the_light_runners(self) -> None:
        # T-4355 (rev-t4355 R1): the light runners' dirs are in the du breakdown, so a
        # light lane filling CT202 shows up by name in the alert.
        run = _runner()
        m.top_consumers(run=run)
        [argv] = run.calls
        self.assertEqual(argv[:2], ("du", "-xsh"))
        for runner in ("actions-runner-pc-light", "actions-runner-echo-light"):
            for sub in ("_work", "_diag"):
                with self.subTest(runner=runner, sub=sub):
                    self.assertIn(f"/opt/{runner}/{sub}", argv)

    def test_urgent_on_either_mount_is_urgent(self) -> None:
        self._check(_runner(_df(40, 95)))
        self.assertEqual(self.sent[0][2], "urgent")
        self.assertIn("ci-runner-disk-urgent", self.sent[0][0])

    def test_cooldown_then_ok_clears_it(self) -> None:
        self._check(_runner(_df(87, 16)), now=1000)
        self._check(_runner(_df(87, 16)), now=2000)          # within cooldown
        self.assertEqual(len(self.sent), 1)
        self._check(_runner(_df(30, 16)), now=3000)          # recovers
        self._check(_runner(_df(87, 16)), now=4000)          # new breach alerts at once
        self.assertEqual(len(self.sent), 2)

    def test_ssh_failure_is_a_loud_probe_failure_not_ok(self) -> None:
        self.assertEqual(self._check(_runner(rc=255)), 0)
        [(subject, body, _)] = self.sent
        self.assertIn("PROBE FAILED", subject)
        self.assertIn("NOT an OK reading", body)

    def test_timeout_and_missing_mount_are_probe_failures(self) -> None:
        self._check(_runner(raise_exc=subprocess.TimeoutExpired("ssh", 60)), now=1000)
        self._check(_runner(DF_HEADER + "/dev/x 1 1 1 20% /\n"), now=1000 + m.COOLDOWN_SECONDS)
        self.assertEqual([s[0] for s in self.sent], ["ci-runner-disk: PROBE FAILED (CT202 disk is UNWATCHED)"] * 2)

    def test_unsendable_alert_exits_nonzero_for_onfailure(self) -> None:
        def broken(*a):
            raise OSError("hub unreachable")
        self.assertEqual(self._check(_runner(rc=255), send=broken), 2)
        self.assertEqual(self._check(_runner(_df(95, 1)), send=broken), 2)

    def test_dry_run_sends_nothing(self) -> None:
        self._check(_runner(_df(95, 95)), dry_run=True)
        self._check(_runner(rc=255), dry_run=True)
        self.assertEqual(self.sent, [])

    def test_flagless_run_refuses(self) -> None:
        self.assertEqual(m.main([]), 64)


class CtExecArgvT4484Test(unittest.TestCase):
    """T-4484: the probe enters CT202 with `nsenter -a` (never `pct exec`, which hangs under
    load), and the bound runs ON pve2. A local timeout kills only the ssh client."""

    def _argv(self, *cmd: str, timeout: int) -> list[str]:
        with patch.object(m.subprocess, "run") as run:
            m._ct_exec(*cmd, timeout=timeout)
        [call] = run.call_args_list
        self.assertEqual(call.kwargs["timeout"], timeout)  # the local backstop stays
        return call.args[0]

    def test_probe_argv_uses_nsenter_with_a_remote_timeout_never_pct_exec(self) -> None:
        argv = self._argv("df", "-P", *m.MOUNTS, timeout=m.PROBE_TIMEOUT)
        self.assertEqual(argv[:6], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", m.SSH_HOST])
        [remote] = argv[6:]  # one remote shell line, so the bound wraps the whole command
        self.assertNotIn("pct", remote.split())
        self.assertIn(f"lxc-info -n {m.CT_ID} -p -H", remote)
        self.assertIn('nsenter -t "$P" -a -- df -P / /ci-cache', remote)
        remote_bound = m.PROBE_TIMEOUT - m.REMOTE_TIMEOUT_MARGIN
        self.assertIn(f"exec timeout {remote_bound} nsenter", remote)
        self.assertLess(remote_bound, m.PROBE_TIMEOUT)

    def test_empty_pid_stops_before_nsenter(self) -> None:
        remote = m._remote_command(("df", "-P", "/"), timeout=60)
        self.assertLess(remote.index('[ -n "$P" ] &&'), remote.index("nsenter"))

    def test_remote_command_quotes_its_arguments(self) -> None:
        remote = m._remote_command(("du", "-xsh", "/path with space", "/x;rm -rf /"), timeout=150)
        self.assertIn("'/path with space'", remote)
        self.assertIn("'/x;rm -rf /'", remote)

    def test_remote_bound_never_drops_below_a_floor(self) -> None:
        self.assertIn("exec timeout 5 nsenter", m._remote_command(("df",), timeout=3))

    def test_probe_failed_mail_names_the_nsenter_check(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sent = []
        with patch.object(m, "STATE_PATH", Path(tmp.name) / "state.json"):
            m.check(dry_run=False, now=1000.0, run=_runner(rc=255),
                    send=lambda s, b, sev: sent.append(b))
        [body] = sent
        self.assertIn("nsenter", body)
        self.assertNotIn("pct exec", body)


class OffTailnetT5385Test(unittest.TestCase):
    def test_probe_failure_while_this_host_is_off_the_tailnet_sends_no_disk_alert(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sent = []
        with patch.object(m, "STATE_PATH", Path(tmp.name) / "state.json"), \
                patch.object(m.tailnet_self, "off_tailnet", return_value="NeedsLogin"), \
                patch.object(m.tailnet_self, "report_off_tailnet") as report:
            rc = m.check(dry_run=False, now=1000.0, run=_runner(rc=255),
                         send=lambda s, b, sev: sent.append(s))
        self.assertEqual(rc, 0)
        self.assertEqual(sent, [])
        report.assert_called_once()
        self.assertEqual(report.call_args.args[:2], ("ci-runner-disk", "NeedsLogin"))


if __name__ == "__main__":
    unittest.main()
