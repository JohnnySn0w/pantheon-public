#!/usr/bin/env python3
"""Tests for tools/host_resilience/install.sh and uninstall.sh (T-5237).

The scripts run for real under bash against a temp HR_INSTALL_ROOT with FAKE systemctl, sysctl,
modprobe and rmmod first on PATH. Nothing touches the real system.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
D_SYSCTL = "etc/sysctl.d/60-pantheon-panic-reboot.conf"
D_MODLOAD = "etc/modules-load.d/pantheon-softdog.conf"
D_WD = "etc/systemd/system.conf.d/10-pantheon-watchdog.conf"
D_BIN = "usr/local/sbin/pantheon_reboot_backoff.py"
D_UNIT1 = "etc/systemd/system/pantheon-reboot-backoff.service"
D_UNIT2 = "etc/systemd/system/pantheon-boot-alert.service"
D_UNIT3 = "etc/systemd/system/pantheon-softdog.service"
W_UNIT3 = "etc/systemd/system/sysinit.target.wants/pantheon-softdog.service"
D_ALERTENV = "etc/pantheon/boot-alert.env"
TOKEN = "123456:SECRETTOKEN"
KEYS = ["kernel.panic", "kernel.panic_on_oops"]
WANT = {"kernel.panic": "10", "kernel.panic_on_oops": "1"}

FAKE_SYSCTL = r"""#!/usr/bin/env bash
echo "sysctl $*" >> "$FAKE_DIR/calls"
case "$1" in
  -w)
    [ -z "${FAKE_SYSCTL_W_FAILS:-}" ] || exit 1
    k="${2%%=*}"; echo "${2#*=}" > "$HR_INSTALL_ROOT/proc/sys/${k//.//}" ;;
esac
exit 0
"""

FAKE_MODPROBE = r"""#!/usr/bin/env bash
echo "modprobe $*" >> "$FAKE_DIR/calls"
if [ "$1" = "-n" ]; then [ -z "${FAKE_MODPROBE_N_FAILS:-}" ]; exit; fi
[ -z "${FAKE_MODPROBE_FAILS:-}" ] || exit 1
mkdir -p "$HR_INSTALL_ROOT/sys/module/softdog/parameters" "$HR_INSTALL_ROOT/dev"
echo "${FAKE_NOWAYOUT:-N}" > "$HR_INSTALL_ROOT/sys/module/softdog/parameters/nowayout"
[ -n "${FAKE_NO_DEVWATCHDOG:-}" ] || : > "$HR_INSTALL_ROOT/dev/watchdog"
exit 0
"""

FAKE_CP = r"""#!/usr/bin/env bash
[ -z "${FAKE_CP_FAILS:-}" ] || exit 1
exec /bin/cp "$@"
"""

FAKE_RMMOD = r"""#!/usr/bin/env bash
echo "rmmod $*" >> "$FAKE_DIR/calls"
[ -z "${FAKE_RMMOD_FAILS:-}" ] || exit 1
rm -rf "$HR_INSTALL_ROOT/sys/module/softdog" "$HR_INSTALL_ROOT/dev/watchdog"
exit 0
"""

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
echo "systemctl $*" >> "$FAKE_DIR/calls"
case "$1" in
  enable)
    for u in "${@:2}"; do
      case "$u" in pantheon-softdog.service)
        mkdir -p "$HR_INSTALL_ROOT/etc/systemd/system/sysinit.target.wants"
        ln -sfn /etc/systemd/system/pantheon-softdog.service "$HR_INSTALL_ROOT/etc/systemd/system/sysinit.target.wants/$u" ;;
      esac
    done ;;
  disable)
    for u in "${@:2}"; do rm -f "$HR_INSTALL_ROOT/etc/systemd/system/sysinit.target.wants/$u"; done ;;
  show)
    if [ -f "$HR_INSTALL_ROOT/etc/systemd/system.conf.d/10-pantheon-watchdog.conf" ]; then
      echo "RuntimeWatchdogUSec=${FAKE_WD_USEC:-2min}"
    else
      echo "RuntimeWatchdogUSec=0"
    fi ;;
  daemon-reexec)
    [ -z "${FAKE_REEXEC_FAILS:-}" ] || exit 1
    mkdir -p "$HR_INSTALL_ROOT/proc/1/fd"
    if [ -f "$HR_INSTALL_ROOT/etc/systemd/system.conf.d/10-pantheon-watchdog.conf" ] && [ -z "${FAKE_NO_PID1_FD:-}" ]; then
      ln -sfn /dev/watchdog "$HR_INSTALL_ROOT/proc/1/fd/3"
    else
      rm -f "$HR_INSTALL_ROOT/proc/1/fd/3"
    fi ;;
esac
exit 0
"""


class Case(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        td = Path(self._td.name)
        self.root, self.fake, self.bin = td / "root", td / "fake", td / "bin"
        for d in (self.root, self.fake, self.bin):
            d.mkdir()
        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("sysctl", FAKE_SYSCTL),
                           ("modprobe", FAKE_MODPROBE), ("rmmod", FAKE_RMMOD), ("cp", FAKE_CP)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        # Stock values, as on the real host (read via the rooted /proc/sys).
        (self.root / "proc/sys/kernel").mkdir(parents=True)
        for k, v in (("panic", "0"), ("panic_on_oops", "0")):
            (self.root / "proc/sys/kernel" / k).write_text(v + "\n")
        self.alert_src = td / "bot.env"
        self.alert_src.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=4242\nOTHER_SECRET=zzz\n")
        self.base = self.tree()

    def sh(self, script, *args, uid="0", path=None, **extra):
        env = dict(os.environ, PATH=path or f"{self.bin}:{os.environ['PATH']}", FAKE_DIR=str(self.fake),
                   HR_INSTALL_ROOT=str(self.root), HR_TEST_UID=uid, HR_WAIT_SECS="0",
                   HR_ALERT_ENV_SRC=str(self.alert_src))
        env.update(extra)
        return subprocess.run(["bash", str(HERE / script), *args], env=env, capture_output=True,
                              text=True, timeout=60)

    def calls(self):
        p = self.fake / "calls"
        return p.read_text().splitlines() if p.exists() else []

    def idx(self, needle, calls=None):
        calls = self.calls() if calls is None else calls
        for i, c in enumerate(calls):
            if needle in c:
                return i
        self.fail(f"no call containing {needle!r}: {calls}")

    def live(self, k):
        return (self.root / "proc/sys" / k.replace(".", "/")).read_text().strip()

    def p(self, rel):
        return self.root / rel

    def backup(self):
        return self.root / "var/lib/pantheon/host-resilience/install-backup"

    def assertNoBootUnit(self):
        self.assertFalse(self.p(D_UNIT3).exists())
        self.assertFalse(self.p(W_UNIT3).is_symlink())

    def tree(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))


class InstallTests(Case):
    def test_ordering_and_end_state(self):
        r = self.sh("install.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        c = self.calls()
        sysctl_w = self.idx("sysctl -w kernel.panic_on_oops=1", c)
        modprobe = self.idx("modprobe softdog", c)
        reexec = self.idx("systemctl daemon-reexec", c)
        self.assertTrue(any("show -p RuntimeWatchdogUSec" in x for x in c[reexec:]), "verify must follow reexec")
        self.assertLess(self.idx("modprobe -n softdog", c), sysctl_w)
        self.assertLess(sysctl_w, modprobe)
        self.assertLess(modprobe, reexec)
        for k in KEYS:
            self.assertEqual(self.live(k), WANT[k])
        for rel in (D_SYSCTL, D_UNIT3, D_WD):
            self.assertTrue(self.p(rel).is_file(), rel)
        self.assertTrue(self.p(W_UNIT3).is_symlink())
        self.assertFalse(self.p(D_MODLOAD).exists())
        self.assertIn("RuntimeWatchdogSec=5min", self.p(D_WD).read_text())
        self.assertNotIn("softlockup_panic =", self.p(D_SYSCTL).read_text())
        self.assertNotIn("hung_task_panic =", self.p(D_SYSCTL).read_text())
        self.assertIn("softdog_preloaded=0", (self.backup() / "state").read_text())

    def test_drop_in_is_in_place_before_reexec(self):
        # The drop-in must exist when daemon-reexec runs: assert via a hook on the fake.
        (self.bin / "systemctl").write_text(FAKE_SYSTEMCTL.replace(
            "daemon-reexec)", 'daemon-reexec) [ -f "$HR_INSTALL_ROOT/etc/systemd/system.conf.d/10-pantheon-watchdog.conf" ] && touch "$FAKE_DIR/dropin_seen_by_reexec";'))
        self.assertEqual(self.sh("install.sh").returncode, 0)
        self.assertTrue((self.fake / "dropin_seen_by_reexec").exists())

    def test_dry_run_is_pure_and_prints_every_action(self):
        r = self.sh("install.sh", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.tree(), self.base)
        for needle in ("modprobe -n softdog", "sysctl -w kernel.panic=10", "install -D", "modprobe softdog",
                       "systemctl daemon-reexec", "verify"):
            self.assertIn(needle, r.stdout)

    def test_dry_run_works_as_non_root(self):
        r = self.sh("install.sh", "--dry-run", uid="1000")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.tree(), self.base)

    def test_refuses_non_root(self):
        r = self.sh("install.sh", uid="1000")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.tree(), self.base)

    def test_usage_error(self):
        self.assertEqual(self.sh("install.sh", "--bogus").returncode, 2)

    def test_refuses_when_softdog_not_loadable(self):
        r = self.sh("install.sh", FAKE_MODPROBE_N_FAILS="1")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.tree(), self.base)
        self.assertFalse(any(c.startswith("sysctl -w") for c in self.calls()))

    def test_idempotent_rerun_keeps_first_backup(self):
        self.assertEqual(self.sh("install.sh").returncode, 0)
        state1 = (self.backup() / "state").read_text()
        snap = {rel: self.p(rel).read_text() for rel in (D_SYSCTL, D_UNIT3, D_WD)}
        r = self.sh("install.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("keeping it", r.stdout)
        self.assertEqual((self.backup() / "state").read_text(), state1)
        self.assertIn("sysctl.kernel.panic=0", state1)
        for rel, txt in snap.items():
            self.assertEqual(self.p(rel).read_text(), txt)

    def test_nowayout_set_refuses_watchdog_keeps_sysctl(self):
        r = self.sh("install.sh", FAKE_NOWAYOUT="Y")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("REFUSED", r.stderr)
        for k in KEYS:
            self.assertEqual(self.live(k), WANT[k])
        self.assertFalse(self.p(D_WD).exists())
        self.assertFalse(self.p(D_MODLOAD).exists())
        self.assertNoBootUnit()
        self.assertFalse(any("daemon-reexec" in c for c in self.calls()))
        self.assertTrue(any(c == "rmmod softdog" for c in self.calls()))

    def test_nowayout_unreadable_refuses(self):
        (self.bin / "modprobe").write_text(FAKE_MODPROBE.replace(
            'echo "${FAKE_NOWAYOUT:-N}" > "$HR_INSTALL_ROOT/sys/module/softdog/parameters/nowayout"', ":"))
        r = self.sh("install.sh")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertFalse(self.p(D_WD).exists())

    def _no_sysfs_param(self):
        (self.bin / "modprobe").write_text(FAKE_MODPROBE.replace(
            'echo "${FAKE_NOWAYOUT:-N}" > "$HR_INSTALL_ROOT/sys/module/softdog/parameters/nowayout"', ":"))
        (self.root / "boot").mkdir(parents=True, exist_ok=True)
        (self.root / "proc").mkdir(parents=True, exist_ok=True)
        (self.root / "proc/cmdline").write_text("BOOT_IMAGE=/vmlinuz ro console=ttyS0\n")

    def test_nowayout_from_kernel_config_when_sysfs_hides_it(self):
        self._no_sysfs_param()
        (self.root / "boot/config-testkernel").write_text("CONFIG_WATCHDOG=y\n# CONFIG_WATCHDOG_NOWAYOUT is not set\n")
        r = self.sh("install.sh", HR_KERNEL_RELEASE="testkernel")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.p(D_WD).exists())

    def test_nowayout_config_set_or_override_refuses(self):
        self._no_sysfs_param()
        (self.root / "boot/config-testkernel").write_text("CONFIG_WATCHDOG_NOWAYOUT=y\n")
        r = self.sh("install.sh", HR_KERNEL_RELEASE="testkernel")
        self.assertEqual(r.returncode, 4, r.stderr)
        (self.root / "boot/config-testkernel").write_text("# CONFIG_WATCHDOG_NOWAYOUT is not set\n")
        (self.root / "proc/cmdline").write_text("ro softdog.nowayout=1\n")
        r = self.sh("install.sh", HR_KERNEL_RELEASE="testkernel")
        self.assertEqual(r.returncode, 4, r.stderr)

    def test_verify_failure_zero_usec_rolls_back_watchdog(self):
        r = self.sh("install.sh", FAKE_WD_USEC="0")
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("rolling the watchdog part back", r.stderr)
        self.assertFalse(self.p(D_WD).exists())
        self.assertFalse(self.p(D_MODLOAD).exists())
        self.assertNoBootUnit()
        self.assertFalse((self.root / "sys/module/softdog").exists())
        c = self.calls()
        self.assertGreater(self.idx("rmmod softdog", c), self.idx("show -p", c))
        for k in KEYS:  # sysctl part is independent and stays applied
            self.assertEqual(self.live(k), WANT[k])

    def test_verify_failure_missing_dev_watchdog_rolls_back(self):
        r = self.sh("install.sh", FAKE_NO_DEVWATCHDOG="1")
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertFalse(self.p(D_WD).exists())
        self.assertFalse(self.p(D_MODLOAD).exists())
        self.assertNoBootUnit()

    def test_verify_failure_pid1_not_holding_device_rolls_back(self):
        r = self.sh("install.sh", FAKE_NO_PID1_FD="1")
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("held by PID 1: no", r.stderr)
        self.assertFalse(self.p(D_WD).exists())
        self.assertNoBootUnit()
        self.assertIn("systemctl disable pantheon-softdog.service", self.calls())
        self.assertTrue(any(c == "rmmod softdog" for c in self.calls()))

    def test_device_node_must_exist_before_reexec(self):
        r = self.sh("install.sh", FAKE_NO_DEVWATCHDOG="1")
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("did not appear", r.stderr)
        self.assertFalse(self.p(D_WD).exists())
        c = self.calls()
        self.assertEqual([x for x in c[c.index("modprobe softdog"):] if "daemon-reexec" in x][:1],
                         ["systemctl daemon-reexec"])  # only the rollback's reexec, drop-in already gone

    def test_sysctl_write_failure_is_loud_and_stops_before_watchdog(self):
        r = self.sh("install.sh", FAKE_SYSCTL_W_FAILS="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ERROR sysctl -w kernel.panic=10 failed", r.stderr)
        self.assertFalse(any(c == "modprobe softdog" for c in self.calls()))
        self.assertFalse(self.p(D_WD).exists())

    def test_reexec_failure_rolls_back(self):
        r = self.sh("install.sh", FAKE_REEXEC_FAILS="1")
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertFalse(self.p(D_WD).exists())
        self.assertNoBootUnit()

    def test_boot_unit_content_and_install_order(self):
        txt = (HERE / "pantheon-softdog.service").read_text()
        for needle in ("Type=oneshot", "RemainAfterExit=yes", "ExecStart=/sbin/modprobe softdog",
                       "DefaultDependencies=no", "After=systemd-modules-load.service",
                       "Before=sysinit.target", "WantedBy=sysinit.target"):
            self.assertIn(needle, txt)
        self.assertFalse((HERE / "pantheon-softdog.conf").exists())
        r = self.sh("install.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(oct(self.p(D_UNIT3).stat().st_mode & 0o777), "0o644")
        c = self.calls()
        en = self.idx("systemctl enable pantheon-softdog.service", c)
        self.assertLess(self.idx("modprobe softdog", c), en)
        self.assertLess(max(i for i, x in enumerate(c) if x == "systemctl daemon-reload"), en)
        self.assertLess(en, self.idx("systemctl daemon-reexec", c))

    def test_stale_modules_load_file_is_removed_and_not_restored(self):
        self.p(D_MODLOAD).parent.mkdir(parents=True)
        self.p(D_MODLOAD).write_text("softdog\n")
        r = self.sh("install.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.p(D_MODLOAD).exists())
        self.assertEqual(self.sh("uninstall.sh").returncode, 0)
        self.assertFalse(self.p(D_MODLOAD).exists())
        self.assertNoBootUnit()

    def test_rollback_restores_preexisting_boot_unit(self):
        self.p(D_UNIT3).parent.mkdir(parents=True, exist_ok=True)
        self.p(D_UNIT3).write_text("[Unit]\nDescription=mine\n")
        r = self.sh("install.sh", FAKE_WD_USEC="0")
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertEqual(self.p(D_UNIT3).read_text(), "[Unit]\nDescription=mine\n")
        self.assertFalse(self.p(W_UNIT3).is_symlink())

    def test_rollback_restores_preexisting_dropin(self):
        self.p(D_WD).parent.mkdir(parents=True)
        self.p(D_WD).write_text("[Manager]\nRuntimeWatchdogSec=0\n")
        r = self.sh("install.sh", FAKE_WD_USEC="0")
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertEqual(self.p(D_WD).read_text(), "[Manager]\nRuntimeWatchdogSec=0\n")

    def test_rollback_leaves_preloaded_softdog_loaded(self):
        (self.root / "sys/module/softdog").mkdir(parents=True)
        r = self.sh("install.sh", FAKE_WD_USEC="0")
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertFalse(any(c == "rmmod softdog" for c in self.calls()))


class BackoffInstallTests(Case):
    def test_units_installed_enabled_not_started_and_alert_env_filtered_0600(self):
        r = self.sh("install.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        for rel in (D_BIN, D_UNIT1, D_UNIT2):
            self.assertTrue(self.p(rel).is_file(), rel)
        c = self.calls()
        self.assertIn("systemctl enable pantheon-reboot-backoff.service pantheon-boot-alert.service", c)
        self.assertFalse(any("--now" in x or x.startswith("systemctl start") for x in c))
        env = self.p(D_ALERTENV)
        self.assertEqual(oct(env.stat().st_mode & 0o777), "0o600")
        self.assertEqual(env.read_text(), f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=4242\n")
        self.assertNotIn("OTHER_SECRET", env.read_text())

    def test_env_temp_copy_is_removed_on_success_and_on_failure(self):
        tmpdir = self.root.parent / "tmpdir"
        tmpdir.mkdir()
        self.assertEqual(self.sh("install.sh", TMPDIR=str(tmpdir)).returncode, 0)
        self.assertEqual(list(tmpdir.iterdir()), [])
        # Failure path: the destination dir is a plain file, so `install -D` fails after mktemp.
        self.setUp()
        (self.root / "etc").mkdir(exist_ok=True)
        (self.root / "etc/pantheon").write_text("not a dir")
        tmpdir = self.root.parent / "tmpdir2"
        tmpdir.mkdir()
        r = self.sh("install.sh", TMPDIR=str(tmpdir))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(list(tmpdir.iterdir()), [])

    def test_token_never_printed_or_in_any_argv(self):
        r = self.sh("install.sh")
        self.assertNotIn("SECRETTOKEN", r.stdout + r.stderr)
        self.assertFalse(any("SECRETTOKEN" in x for x in self.calls()))
        d = self.sh("install.sh", "--dry-run")
        self.assertNotIn("SECRETTOKEN", d.stdout + d.stderr)

    def test_chat_id_flag_overrides_and_can_supply_a_missing_one(self):
        self.alert_src.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\n")
        self.assertEqual(self.sh("install.sh").returncode, 1)  # no chat id anywhere
        self.assertEqual(self.tree(), self.base)
        r = self.sh("install.sh", "--chat-id", "777")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("TELEGRAM_CHAT_ID=777", self.p(D_ALERTENV).read_text())

    def test_source_without_token_or_unreadable_is_refused_before_any_change(self):
        self.alert_src.write_text("TELEGRAM_CHAT_ID=1\n")
        self.assertEqual(self.sh("install.sh").returncode, 1)
        self.assertEqual(self.tree(), self.base)
        self.assertEqual(self.sh("install.sh", "--alert-env", "/nonexistent/x.env").returncode, 1)
        self.assertEqual(set(self.calls()), {"modprobe -n softdog"})  # only the read-only preflight ran

    def test_no_source_installs_but_alert_is_unconfigured_and_check_fails(self):
        r = self.sh("install.sh", HR_ALERT_ENV_SRC="")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("UNCONFIGURED", r.stderr)
        self.assertFalse(self.p(D_ALERTENV).exists())
        c = self.sh("install.sh", "--check", HR_ALERT_ENV_SRC="")
        self.assertEqual(c.returncode, 1)
        self.assertIn("FAIL alert config", c.stdout)

    def test_check_reports_hold_marker_as_fail_and_the_boot_count(self):
        self.assertEqual(self.sh("install.sh").returncode, 0)
        st = self.root / "var/lib/pantheon/host-resilience"
        (st / "LOOP_HOLD").write_text("ts=1\n")
        c = self.sh("install.sh", "--check", uid="1000")
        self.assertEqual(c.returncode, 1)
        self.assertIn("FAIL reboot loop hold", c.stdout)
        self.assertIn("info boots in the last hour:", c.stdout)

    def test_check_never_prints_credentials(self):
        self.assertEqual(self.sh("install.sh").returncode, 0)
        c = self.sh("install.sh", "--check")
        self.assertNotIn("SECRETTOKEN", c.stdout + c.stderr)

    def test_uninstall_removes_units_env_and_boot_data_exactly(self):
        before = [t for t in self.tree() if (self.root / t).is_file()]
        self.assertEqual(self.sh("install.sh").returncode, 0)
        st = self.root / "var/lib/pantheon/host-resilience"
        (st / "boots.log").write_text("1 a\n")
        (st / "state.json").write_text("{}")
        (st / "LOOP_HOLD").write_text("x")
        r = self.sh("uninstall.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        for rel in (D_BIN, D_UNIT1, D_UNIT2, D_ALERTENV):
            self.assertFalse(self.p(rel).exists(), rel)
        self.assertIn("systemctl disable pantheon-reboot-backoff.service pantheon-boot-alert.service", self.calls())
        self.assertEqual([t for t in self.tree() if (self.root / t).is_file()], before)
        self.assertFalse(st.exists())

    def test_uninstall_restores_a_preexisting_alert_env(self):
        self.p(D_ALERTENV).parent.mkdir(parents=True)
        self.p(D_ALERTENV).write_text("OLD=1\n")
        self.assertEqual(self.sh("install.sh").returncode, 0)
        self.assertEqual(self.sh("uninstall.sh").returncode, 0)
        self.assertEqual(self.p(D_ALERTENV).read_text(), "OLD=1\n")


class CheckTests(Case):
    def test_check_fails_before_install_and_is_read_only(self):
        r = self.sh("install.sh", "--check", uid="1000")
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL sysctl kernel.panic=0 (want 10)", r.stdout)
        self.assertIn("FAIL softdog not loaded", r.stdout)
        self.assertIn("FAIL /dev/watchdog missing", r.stdout)
        self.assertEqual(self.tree(), self.base)
        self.assertFalse(any(c.startswith(("sysctl -w", "modprobe", "rmmod")) or "daemon-reexec" in c
                             for c in self.calls()))

    def test_check_does_not_need_the_sysctl_binary(self):
        self.assertEqual(self.sh("install.sh").returncode, 0)
        nosysctl = self.root.parent / "bin2"
        nosysctl.mkdir()
        (nosysctl / "systemctl").write_text(FAKE_SYSTEMCTL)
        (nosysctl / "systemctl").chmod(0o755)
        path = f"{nosysctl}:/usr/bin:/bin"
        import shutil
        self.assertIsNone(shutil.which("sysctl", path=path), "test premise: sysctl must be off PATH")
        before = len(self.calls())
        r = self.sh("install.sh", "--check", uid="1000", path=path)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(any(c.startswith("sysctl") for c in self.calls()[before:]))

    def test_check_passes_after_install(self):
        self.assertEqual(self.sh("install.sh").returncode, 0)
        r = self.sh("install.sh", "--check", uid="1000")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(r.stdout.count("ok   "), 10)
        self.assertIn("ok   softdog loads at boot via pantheon-softdog.service", r.stdout)
        self.assertIn("info boots in the last hour: 1", r.stdout)
        self.assertNotIn("FAIL", r.stdout)

    def test_check_catches_modules_load_only_setup(self):
        # The T-5237 bug: softdog loaded now + modules-load.d file, but nothing explicit at boot
        # (modules-load.d honours the kmod blacklist). Everything else is fine.
        self.assertEqual(self.sh("install.sh").returncode, 0)
        self.p(D_UNIT3).unlink()
        self.p(W_UNIT3).unlink()
        self.p(D_MODLOAD).parent.mkdir(parents=True)
        self.p(D_MODLOAD).write_text("softdog\n")
        r = self.sh("install.sh", "--check", uid="1000")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAIL softdog boot unit pantheon-softdog.service not installed+enabled", r.stdout)
        self.assertIn("ok   softdog loaded", r.stdout)

    def test_check_fails_when_unit_installed_but_not_enabled(self):
        self.assertEqual(self.sh("install.sh").returncode, 0)
        self.p(W_UNIT3).unlink()
        r = self.sh("install.sh", "--check", uid="1000")
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL softdog boot unit", r.stdout)

    def test_check_flags_a_single_drifted_sysctl(self):
        self.assertEqual(self.sh("install.sh").returncode, 0)
        (self.root / "proc/sys/kernel/panic_on_oops").write_text("0\n")
        r = self.sh("install.sh", "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL sysctl kernel.panic_on_oops=0 (want 1)", r.stdout)


class UninstallTests(Case):
    def test_restore_is_exact(self):
        self.p(D_SYSCTL).parent.mkdir(parents=True)
        self.p(D_SYSCTL).write_text("# mine\nkernel.panic = 3\n")
        (self.root / "proc/sys/kernel/panic").write_text("3\n")
        before = self.tree()
        self.assertEqual(self.sh("install.sh").returncode, 0)
        r = self.sh("uninstall.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.p(D_SYSCTL).read_text(), "# mine\nkernel.panic = 3\n")
        self.assertEqual(self.live("kernel.panic"), "3")
        self.assertEqual(self.live("kernel.panic_on_oops"), "0")
        self.assertFalse(self.p(D_WD).exists())
        self.assertFalse(self.p(D_MODLOAD).exists())
        self.assertNoBootUnit()
        self.assertIn("systemctl disable pantheon-softdog.service", self.calls())
        self.assertFalse(self.backup().exists())
        files = [t for t in self.tree() if (self.root / t).is_file()]
        self.assertEqual(files, [t for t in before if (self.root / t).is_file()])
        self.assertNotIn("sys/module/softdog", self.tree())

    def test_unloads_softdog_only_after_reexec_and_only_if_we_loaded_it(self):
        self.assertEqual(self.sh("install.sh").returncode, 0)
        (self.fake / "calls").unlink()
        self.assertEqual(self.sh("uninstall.sh").returncode, 0)
        c = self.calls()
        self.assertLess(self.idx("daemon-reexec", c), self.idx("rmmod softdog", c))

    def test_preloaded_softdog_stays_loaded(self):
        (self.root / "sys/module/softdog").mkdir(parents=True)
        self.assertEqual(self.sh("install.sh").returncode, 0)
        r = self.sh("uninstall.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(any(c == "rmmod softdog" for c in self.calls()))
        self.assertTrue((self.root / "sys/module/softdog").exists())

    def test_refuses_without_backup(self):
        r = self.sh("uninstall.sh")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.calls(), [])

    def test_refuses_non_root_and_dry_run_is_pure(self):
        self.assertEqual(self.sh("install.sh").returncode, 0)
        snap, ncalls = self.tree(), len(self.calls())
        self.assertEqual(self.sh("uninstall.sh", uid="1000").returncode, 1)
        r = self.sh("uninstall.sh", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.tree(), snap)
        self.assertEqual(len(self.calls()), ncalls)
        for needle in ("daemon-reexec", "rmmod softdog", "sysctl -w kernel.panic=0", "rm -rf"):
            self.assertIn(needle, r.stdout)

    def test_rmmod_failure_keeps_backup_and_exits_3(self):
        self.assertEqual(self.sh("install.sh").returncode, 0)
        r = self.sh("uninstall.sh", FAKE_RMMOD_FAILS="1")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertTrue((self.backup() / "state").exists())
        self.assertEqual(self.live("kernel.panic"), "0")  # the rest still restored

    def test_restore_copy_failure_is_loud_and_nonzero(self):
        self.p(D_WD).parent.mkdir(parents=True)
        self.p(D_WD).write_text("[Manager]\nRuntimeWatchdogSec=0\n")
        self.assertEqual(self.sh("install.sh").returncode, 0)
        r = self.sh("uninstall.sh", FAKE_CP_FAILS="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ERROR could not restore", r.stderr)
        self.assertTrue((self.backup() / "state").exists())

    def test_usage_error(self):
        self.assertEqual(self.sh("uninstall.sh", "--bogus").returncode, 2)


if __name__ == "__main__":
    unittest.main()
