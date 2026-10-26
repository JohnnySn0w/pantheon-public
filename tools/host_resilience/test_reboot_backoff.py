#!/usr/bin/env python3
"""Tests for pantheon_reboot_backoff.py (T-5237): the panic-delay ladder, the loop hold, log
handling, and the Telegram boot alert. Fake root, fake clock, fake systemctl and curl, and a fake
repo checkout for the best-effort mailbox import. Nothing touches the real system."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "pantheon_reboot_backoff.py"
NOW = 1_800_000_000
TOKEN = "123456:SECRETTOKEN"

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
echo "systemctl $*" >> "$FAKE_DIR/calls"
[ -z "${FAKE_REEXEC_FAILS:-}" ] || exit 1
if [ "$1" = daemon-reexec ]; then
  mkdir -p "$HR_INSTALL_ROOT/proc/1/fd"
  if [ -f "$HR_INSTALL_ROOT/run/systemd/system.conf.d/90-pantheon-watchdog-hold.conf" ] || [ -n "${FAKE_NO_PID1_FD:-}" ]; then
    rm -f "$HR_INSTALL_ROOT/proc/1/fd/3"
  else
    ln -sfn /dev/watchdog "$HR_INSTALL_ROOT/proc/1/fd/3"
  fi
fi
exit 0
"""

FAKE_CURL = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_DIR/curl_argv"
cat >> "$FAKE_DIR/curl_stdin"
n=$(( $(cat "$FAKE_DIR/curl_n" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$FAKE_DIR/curl_n"
if [ -n "${FAKE_CURL_ALWAYS_FAILS:-}" ]; then
  echo "curl: (6) Could not resolve host; url https://api.telegram.org/bot123456:SECRETTOKEN/sendMessage" >&2
  exit 6
fi
[ "$n" -gt "${FAKE_CURL_FAIL_FIRST:-0}" ] || exit 7
exit 0
"""

FAKE_ARM = "def current_mailbox_hub():\n    return '/fake/hub'\n"
FAKE_MAILBOX = (
    "import json, os\n"
    "def send(rcpt, payload, sender, *, subject=None, severity='normal', **kw):\n"
    "    open(os.environ['FAKE_DIR'] + '/state_at_mail', 'w').write(str(os.path.exists(os.environ['HR_INSTALL_ROOT'] + '/var/lib/pantheon/host-resilience/state.json')))\n"
    "    with open(os.environ['FAKE_DIR'] + '/mail', 'a') as f:\n"
    "        f.write(json.dumps([rcpt, sender, subject, severity, payload]) + '\\n')\n"
)


class Case(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        t = Path(td.name)
        self.root, self.fake, self.bin, self.repo = t / "root", t / "fake", t / "bin", t / "repo"
        for d in (self.root, self.fake, self.bin, self.repo / "tools"):
            d.mkdir(parents=True)
        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("curl", FAKE_CURL)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        (self.repo / "tools/agent_role_map.py").write_text(FAKE_ARM)
        (self.repo / "tools/mailbox.py").write_text(FAKE_MAILBOX)
        (self.root / "proc/sys/kernel").mkdir(parents=True)
        self.panic_file.write_text("10\n")
        self.state = self.root / "var/lib/pantheon/host-resilience"

    @property
    def panic_file(self):
        return self.root / "proc/sys/kernel/panic"

    def panic(self):
        return self.panic_file.read_text().strip()

    def run_cmd(self, cmd, boot_id="bootNEW", now=NOW, **extra):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", HR_INSTALL_ROOT=str(self.root),
                   HR_NOW=str(now), HR_BOOT_ID=boot_id, HR_RETRY_SCALE="0", FAKE_DIR=str(self.fake))
        env.setdefault("PANTHEON_CONFIG_DIR", str(self.repo))
        env.update(extra)
        return subprocess.run([sys.executable, str(SCRIPT), cmd], env=env, capture_output=True,
                              text=True, timeout=60)

    def seed_boots(self, prior, spacing=100):
        """`prior` earlier boots, all inside the hour, so the new boot makes N = prior + 1."""
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "boots.log").write_text(
            "".join(f"{NOW - spacing * (prior - i)} old{i}\n" for i in range(prior)))

    def calls(self):
        p = self.fake / "calls"
        return p.read_text().splitlines() if p.exists() else []

    def dropin(self):
        return self.root / "run/systemd/system.conf.d/90-pantheon-watchdog-hold.conf"

    def mails(self):
        p = self.fake / "mail"
        return p.read_text().splitlines() if p.exists() else []


class LadderTests(Case):
    def test_delay_ladder_n1_to_n5(self):
        expect = {1: "10", 2: "60", 3: "300", 4: "0", 5: "0"}  # N>=4 is the hold: panic=0
        for n, want in expect.items():
            with self.subTest(n=n):
                self.setUp()
                self.seed_boots(n - 1)
                r = self.run_cmd("backoff")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.panic(), want)

    def test_old_boots_outside_the_hour_do_not_count(self):
        self.state.mkdir(parents=True)
        (self.state / "boots.log").write_text("".join(f"{NOW - 4000 - i} old{i}\n" for i in range(10)))
        self.assertEqual(self.run_cmd("backoff").returncode, 0)
        self.assertEqual(self.panic(), "10")

    def test_same_boot_id_is_counted_once(self):
        self.seed_boots(1)
        self.run_cmd("backoff")
        self.run_cmd("backoff")  # e.g. a manual `systemctl restart` of the unit
        lines = (self.state / "boots.log").read_text().splitlines()
        self.assertEqual(sum(1 for ln in lines if ln.endswith("bootNEW")), 1)
        self.assertEqual(self.panic(), "60")

    def test_below_hold_no_dropin_no_reexec_and_stale_marker_cleared(self):
        self.state.mkdir(parents=True)
        (self.state / "LOOP_HOLD").write_text("old\n")
        self.seed_boots(2)
        self.assertEqual(self.run_cmd("backoff").returncode, 0)
        self.assertFalse(self.dropin().exists())
        self.assertFalse(any("daemon-reexec" in c for c in self.calls()))
        self.assertFalse((self.state / "LOOP_HOLD").exists())
        self.assertEqual(self.mails(), [])


class HoldTests(Case):
    def test_hold_at_n4(self):
        self.seed_boots(3)
        r = self.run_cmd("backoff")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RuntimeWatchdogSec=0", self.dropin().read_text())
        self.assertIn("systemctl daemon-reexec", self.calls())
        self.assertEqual(self.panic(), "0")
        self.assertIn("boots_in_hour=4", (self.state / "LOOP_HOLD").read_text())
        self.assertIn("REBOOT LOOP", r.stderr)
        self.assertIn("<2>", r.stderr)  # journal crit priority

    def test_hold_drop_in_is_runtime_only_never_persistent(self):
        self.seed_boots(4)
        self.run_cmd("backoff")
        persistent = [p for p in self.root.rglob("*") if p.is_file() and "RuntimeWatchdogSec" in p.read_text(errors="replace")]
        self.assertEqual(persistent, [self.dropin()])
        self.assertTrue(str(self.dropin()).startswith(str(self.root / "run/")))
        self.assertFalse((self.root / "etc").exists())

    def test_hold_sends_one_urgent_mail_to_recovery_and_mail_failure_never_blocks(self):
        self.seed_boots(3)
        self.run_cmd("backoff")
        mails = self.mails()
        self.assertEqual(len(mails), 1)
        self.assertIn('"/fake/hub/recovery"', mails[0])
        self.assertIn('"urgent"', mails[0])
        # Mailbox unreachable: still exit 0, still held.
        self.setUp()
        self.seed_boots(3)
        r = self.run_cmd("backoff", PANTHEON_CONFIG_DIR=str(self.repo / "nowhere"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("mail to recovery failed", r.stderr)
        self.assertEqual(self.panic(), "0")
        self.assertTrue((self.state / "LOOP_HOLD").exists())

    def test_hold_not_fully_applied_falls_back_to_1800_not_to_nothing(self):
        self.seed_boots(3)
        r = self.run_cmd("backoff", FAKE_REEXEC_FAILS="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.panic(), "1800")
        self.assertTrue((self.state / "LOOP_HOLD").exists())
        self.assertEqual(len(self.mails()), 1)


class RearmTests(Case):
    def hold(self):
        self.seed_boots(3)
        self.assertEqual(self.run_cmd("backoff").returncode, 0)
        self.assertTrue(self.dropin().exists())

    def test_hold_state_is_written_before_the_mail_and_wording_names_rearm(self):
        self.hold()
        self.assertEqual((self.fake / "state_at_mail").read_text(), "True")
        mail = self.mails()[0]
        self.assertIn("rearm (or reboot)", mail)
        self.assertNotIn("rm /var/lib", mail)

    def test_rearm_ends_the_hold_without_a_reboot(self):
        self.hold()
        r = self.run_cmd("rearm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.dropin().exists())
        self.assertFalse((self.state / "LOOP_HOLD").exists())
        self.assertEqual(self.panic(), "10")
        self.assertEqual(self.calls().count("systemctl daemon-reexec"), 2)  # the hold's, then rearm's
        self.assertIn('"hold": false', (self.state / "state.json").read_text())

    def test_rearm_is_idempotent(self):
        self.hold()
        self.run_cmd("rearm")
        n = len(self.calls())
        r = self.run_cmd("rearm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.calls()), n)
        self.assertEqual(self.panic(), "10")

    def test_rearm_on_a_non_held_box_changes_nothing(self):
        self.seed_boots(1)
        self.run_cmd("backoff")  # N=2 -> panic 60, no hold
        r = self.run_cmd("rearm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not held", r.stdout)
        self.assertEqual(self.panic(), "60")
        self.assertFalse(any("daemon-reexec" in c for c in self.calls()))

    def test_rearm_works_from_the_failed_hold_fallback_state(self):
        self.seed_boots(3)
        self.run_cmd("backoff", FAKE_REEXEC_FAILS="1")  # marker + panic 1800, drop-in written
        self.assertEqual(self.panic(), "1800")
        self.assertEqual(self.run_cmd("rearm").returncode, 0)
        self.assertEqual(self.panic(), "10")
        self.assertFalse((self.state / "LOOP_HOLD").exists())

    def test_rearm_keeps_the_marker_when_pid1_does_not_hold_the_device(self):
        self.hold()
        r = self.run_cmd("rearm", FAKE_NO_PID1_FD="1", HR_WAIT_SECS="0")
        self.assertEqual(r.returncode, 1)
        self.assertIn("does NOT hold", r.stderr)
        self.assertTrue((self.state / "LOOP_HOLD").exists())


class LogTests(Case):
    def test_trim_is_oldest_first_and_bounded(self):
        self.state.mkdir(parents=True)
        (self.state / "boots.log").write_text("".join(f"{NOW - 90000 + i} old{i}\n" for i in range(250)))
        self.run_cmd("backoff")
        lines = (self.state / "boots.log").read_text().splitlines()
        self.assertEqual(len(lines), 200)
        self.assertEqual(lines[-1], f"{NOW} bootNEW")
        self.assertEqual(lines[0], f"{NOW - 90000 + 51} old51")  # 251 -> keep newest 200
        self.assertEqual(self.panic(), "10")

    def test_malformed_log_is_n1_fail_open(self):
        self.state.mkdir(parents=True)
        (self.state / "boots.log").write_bytes(b"garbage\n\x00\xff\xfe\nabc def\n1 2 3\n-5 x\n")
        r = self.run_cmd("backoff")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.panic(), "10")
        self.assertFalse(self.dropin().exists())

    def test_unreadable_log_is_n1_fail_open(self):
        self.state.mkdir(parents=True)
        (self.state / "boots.log").mkdir()  # reading it as a file raises
        r = self.run_cmd("backoff")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.panic(), "10")
        self.assertIn("fail-open", r.stderr)

    def test_count_subcommand_is_read_only(self):
        self.seed_boots(2)
        before = (self.state / "boots.log").read_text()
        r = self.run_cmd("count")
        self.assertEqual(r.stdout.strip(), "2")
        self.assertEqual((self.state / "boots.log").read_text(), before)

    def test_usage_error(self):
        self.assertEqual(self.run_cmd("bogus").returncode, 2)


class AlertTests(Case):
    def setUp(self):
        super().setUp()
        env = self.root / "etc/pantheon/boot-alert.env"
        env.parent.mkdir(parents=True)
        env.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=4242\n")

    def sent_text(self):
        for ln in (self.fake / "curl_stdin").read_text().splitlines():
            if ln.startswith('data-urlencode = "text='):
                return ln[len('data-urlencode = "text='):-1]
        self.fail("no text in curl config")

    def stamp(self):
        return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(NOW))

    def test_normal_boot_message(self):
        self.run_cmd("backoff")
        r = self.run_cmd("alert")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.sent_text(),
                         f"VPS rebooted at {self.stamp()}. Reboots in last hour: 1. Panic delay now 10s.")
        self.assertIn('chat_id=4242', (self.fake / "curl_stdin").read_text())

    def test_repeat_boot_message(self):
        self.seed_boots(2)
        self.run_cmd("backoff")
        self.run_cmd("alert")
        self.assertEqual(self.sent_text(),
                         f"VPS rebooted at {self.stamp()}. Reboots in last hour: 3. Panic delay now 300s.")

    def test_hold_message(self):
        self.seed_boots(3)
        self.run_cmd("backoff")
        self.run_cmd("alert")
        self.assertEqual(self.sent_text(),
                         "REBOOT LOOP: 4 boots in 1h. Auto-reboot and watchdog DISABLED for this boot; "
                         "box staying up for inspection. To re-arm with: sudo python3 "
                         "/usr/local/sbin/pantheon_reboot_backoff.py rearm (or reboot).")

    def test_retry_is_bounded_and_exit_is_zero_even_on_total_failure(self):
        self.run_cmd("backoff")
        r = self.run_cmd("alert", FAKE_CURL_ALWAYS_FAILS="1")
        self.assertEqual(r.returncode, 0)
        self.assertEqual((self.fake / "curl_n").read_text().strip(), "5")
        self.assertIn("GAVE UP", r.stderr)
        self.assertIn("<2>", r.stderr)

    def test_succeeds_on_a_later_try_then_stops(self):
        self.run_cmd("backoff")
        r = self.run_cmd("alert", FAKE_CURL_FAIL_FIRST="2")
        self.assertEqual(r.returncode, 0)
        self.assertEqual((self.fake / "curl_n").read_text().strip(), "3")

    def test_token_never_in_argv_or_logs_but_is_used_via_stdin_config(self):
        self.run_cmd("backoff")
        for extra in ({}, {"FAKE_CURL_ALWAYS_FAILS": "1"}):
            self.setUp()
            self.run_cmd("backoff")
            r = self.run_cmd("alert", **extra)
            argv = (self.fake / "curl_argv").read_text()
            self.assertNotIn("SECRETTOKEN", argv)
            self.assertNotIn("api.telegram.org", argv)
            self.assertIn("-K -", argv)
            self.assertNotIn("SECRETTOKEN", r.stdout + r.stderr)
            self.assertIn(f"bot{TOKEN}/sendMessage", (self.fake / "curl_stdin").read_text())

    def test_missing_credentials_is_loud_and_exit_zero(self):
        (self.root / "etc/pantheon/boot-alert.env").unlink()
        r = self.run_cmd("alert")
        self.assertEqual(r.returncode, 0)
        self.assertIn("NOT SENT", r.stderr)
        self.assertFalse((self.fake / "curl_n").exists())

    def test_alert_without_backoff_state_still_sends(self):
        r = self.run_cmd("alert")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Reboots in last hour: 1.", self.sent_text())


if __name__ == "__main__":
    unittest.main()
