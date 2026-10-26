#!/usr/bin/env python3
"""Tests for tools/oom/install.sh and uninstall.sh (T-5227): ordering, refusal paths, idempotence.

The scripts run for real under bash, but against a temp OOM_INSTALL_ROOT with a FAKE systemctl
(and fake interpreter for the killer's --dry-run) first on PATH. Nothing touches the real system.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DROPINS = [
    "/etc/systemd/oomd.conf.d/50-pantheon.conf",
    "/etc/systemd/system/user.slice.d/50-pantheon-oomd.conf",
    "/etc/systemd/system/user-.slice.d/50-pantheon-oomd.conf",
]

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_DIR/calls"
rd() { cat "$FAKE_DIR/$1" 2>/dev/null || echo "$2"; }
case "$1" in
  is-enabled) rd eo_enabled not-found ;;
  is-active)
    quiet=0; [ "$2" = "--quiet" ] && { quiet=1; shift; }
    if [ "$2" = earlyoom ]; then v="$(rd eo_active inactive)"; else v="$(rd killer_active inactive)"; fi
    [ "$quiet" -eq 1 ] || echo "$v"
    [ "$v" = active ] && exit 0 || exit 1 ;;
  enable)
    case "$*" in
      *pantheon-oom-killer*) [ -n "${FAKE_KILLER_FAILS:-}" ] || echo active > "$FAKE_DIR/killer_active" ;;
      *earlyoom*) echo enabled > "$FAKE_DIR/eo_enabled" ;;
    esac ;;
  disable)
    case "$*" in
      *pantheon-oom-killer*) echo inactive > "$FAKE_DIR/killer_active" ;;
      *earlyoom*) echo disabled > "$FAKE_DIR/eo_enabled"; case "$*" in *--now*) echo inactive > "$FAKE_DIR/eo_active" ;; esac ;;
    esac ;;
  mask) echo masked > "$FAKE_DIR/eo_enabled" ;;
  start) [ -n "${FAKE_EO_START_FAILS:-}" ] || echo active > "$FAKE_DIR/eo_active" ;;
  *) : ;;
esac
exit 0
"""

FAKE_PYTHON = r"""#!/usr/bin/env bash
echo "PY $*" >> "$FAKE_DIR/calls"
if [ "$1" = "-c" ]; then printf '1001 claude-fallback\n1011 codex-fallback\n'; exit 0; fi
case "$*" in
  *sysorch_restart.py\ install*)   exit "${FAKE_SYSORCH_INSTALL_RC:-0}" ;;
  *sysorch_restart.py\ check*)     exit "${FAKE_SYSORCH_CHECK_RC:-0}" ;;
  *sysorch_restart.py\ uninstall*) exit "${FAKE_SYSORCH_UNINSTALL_RC:-0}" ;;
esac
exit "${FAKE_DRYRUN_RC:-0}"
"""


class ScriptCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        td = Path(self._td.name)
        self.root, self.fake, self.bin = td / "root", td / "fake", td / "bin"
        for d in (self.root, self.fake, self.bin):
            d.mkdir()
        (self.bin / "systemctl").write_text(FAKE_SYSTEMCTL)
        (self.bin / "fakepython").write_text(FAKE_PYTHON)
        for f in ("systemctl", "fakepython"):
            (self.bin / f).chmod(0o755)
        for d in DROPINS:
            p = self.root / d.lstrip("/")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"original {d}\n")
        self.set_eo("enabled", "active")

    def set_eo(self, enabled, active):
        (self.fake / "eo_enabled").write_text(enabled + "\n")
        (self.fake / "eo_active").write_text(active + "\n")

    def sh(self, script, *args, uid="0", **extra):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_DIR=str(self.fake),
                   OOM_INSTALL_ROOT=str(self.root), OOM_TEST_UID=uid,
                   OOM_PYTHON=str(self.bin / "fakepython"), OOM_VERIFY_SLEEP="0", **extra)
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
        self.fail(f"no systemctl call containing {needle!r}: {calls}")

    def dropin(self, d):
        return self.root / d.lstrip("/")


class InstallTests(ScriptCase):
    def test_killer_is_started_and_verified_before_earlyoom_is_touched(self):
        r = self.sh("install.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        c = self.calls()
        start = self.idx("restart pantheon-oom-killer", c)
        verify = self.idx("is-active --quiet pantheon-oom-killer", c)
        retire = self.idx("disable --now earlyoom", c)
        self.assertLess(start, verify)
        self.assertLess(verify, retire)
        self.assertLess(retire, self.idx("try-restart systemd-oomd", c))
        for d in DROPINS:
            self.assertFalse(self.dropin(d).exists(), d)
        backup = self.root / "var/lib/pantheon/oom/install-backup"
        self.assertIn("earlyoom_enabled=enabled", (backup / "state").read_text())
        for d in DROPINS:
            self.assertTrue((backup / d.replace("/", "%")).exists(), d)
        self.assertTrue((self.root / "usr/local/sbin/pantheon_oom_killer.py").exists())
        self.assertTrue((self.root / "etc/systemd/system/pantheon-oom-killer.service").exists())

    def test_aborts_leaving_earlyoom_and_dropins_when_killer_not_active(self):
        r = self.sh("install.sh", FAKE_KILLER_FAILS="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("disable --now earlyoom", "\n".join(self.calls()))
        for d in DROPINS:
            self.assertTrue(self.dropin(d).exists(), d)
        self.assertEqual((self.fake / "eo_active").read_text().strip(), "active")

    def test_aborts_when_killer_dry_run_fails(self):
        r = self.sh("install.sh", FAKE_DRYRUN_RC="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("disable --now earlyoom", "\n".join(self.calls()))
        self.assertTrue(self.dropin(DROPINS[0]).exists())

    def test_refuses_without_root_and_touches_nothing(self):
        r = self.sh("install.sh", uid="1000")
        self.assertEqual(r.returncode, 1)
        self.assertIn("must run as root", r.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.root / "usr").exists())

    def test_dry_run_prints_actions_and_changes_nothing(self):
        r = self.sh("install.sh", "--dry-run", uid="1000")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("+ systemctl enable pantheon-oom-killer.service", r.stdout)
        self.assertIn("+ systemctl restart pantheon-oom-killer.service", r.stdout)
        self.assertIn("+ systemctl disable --now earlyoom", r.stdout)
        self.assertEqual(self.calls(), [])
        self.assertTrue(self.dropin(DROPINS[0]).exists())
        self.assertFalse((self.root / "usr").exists())

    def test_idempotent_rerun_keeps_original_backup(self):
        self.assertEqual(self.sh("install.sh").returncode, 0)
        # the fake (like the real system) now reports earlyoom disabled; backup must not follow
        r = self.sh("install.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        state = (self.root / "var/lib/pantheon/oom/install-backup/state").read_text()
        self.assertIn("earlyoom_enabled=enabled", state)
        self.assertIn("earlyoom_active=active", state)
        backup = self.root / "var/lib/pantheon/oom/install-backup"
        self.assertEqual((backup / DROPINS[0].replace("/", "%")).read_text(), f"original {DROPINS[0]}\n")

    def test_rerun_restarts_an_already_running_killer(self):
        """T-5231: the update leg must replace a running killer's code;
        `enable --now` is a no-op on an active unit."""
        self.sh("install.sh")
        self.sh("install.sh")
        c = self.calls()
        restarts = [x for x in c if "restart pantheon-oom-killer" in x and "try-restart" not in x]
        self.assertEqual(len(restarts), 2, c)
        self.assertFalse([x for x in c if "enable --now pantheon-oom-killer" in x], c)
        self.assertLess(self.idx("enable pantheon-oom-killer", c), self.idx("restart pantheon-oom-killer", c))

    def test_unknown_argument_rejected(self):
        self.assertEqual(self.sh("install.sh", "--bogus").returncode, 2)


class SysorchWiringTests(ScriptCase):
    def test_install_runs_sysorch_after_retiring_old_layers_then_check_then_reloads(self):
        r = self.sh("install.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        c = self.calls()
        retire = self.idx("disable --now earlyoom", c)
        inst = self.idx("sysorch_restart.py install --apply", c)
        chk = self.idx("sysorch_restart.py check", c)
        self.assertLess(retire, inst)
        self.assertLess(inst, chk)
        later = c[chk:]
        self.assertTrue(any(x.strip() == "daemon-reload" for x in later))
        self.assertTrue(any("--machine=claude-fallback@.host daemon-reload" in x for x in later))
        self.assertTrue(any("--machine=codex-fallback@.host daemon-reload" in x for x in later))

    def test_failed_check_is_loud_but_does_not_roll_back_the_killer(self):
        r = self.sh("install.sh", FAKE_SYSORCH_CHECK_RC="1")
        self.assertEqual(r.returncode, 3)
        self.assertIn("check FAILED", r.stderr)
        self.assertNotIn("disable --now pantheon-oom-killer", "\n".join(self.calls()))
        self.assertEqual((self.fake / "killer_active").read_text().strip(), "active")
        self.assertTrue(any("--machine=" in x for x in self.calls()))     # reloads still ran

    def test_failed_sysorch_install_continues_and_reports(self):
        r = self.sh("install.sh", FAKE_SYSORCH_INSTALL_RC="1")
        self.assertEqual(r.returncode, 3)
        self.assertIn("sysorch_restart install failed", r.stderr)
        self.assertEqual((self.fake / "killer_active").read_text().strip(), "active")

    def test_sysorch_not_run_when_killer_verification_aborts(self):
        self.assertNotEqual(self.sh("install.sh", FAKE_KILLER_FAILS="1").returncode, 0)
        self.assertNotIn("sysorch_restart.py install", "\n".join(self.calls()))

    def test_dry_run_prints_plain_install_and_check_without_executing(self):
        r = self.sh("install.sh", "--dry-run", uid="1000")
        self.assertIn("sysorch_restart.py install", r.stdout)
        self.assertNotIn("--apply", r.stdout)
        self.assertIn("sysorch_restart.py check", r.stdout)
        self.assertEqual(self.calls(), [])

    def test_uninstall_removes_sysorch_before_any_earlyoom_restore(self):
        self.assertEqual(self.sh("install.sh").returncode, 0)
        before = len(self.calls())
        r = self.sh("uninstall.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        c = self.calls()[before:]
        un = self.idx("sysorch_restart.py uninstall --apply", c)
        self.assertLess(un, self.idx("enable earlyoom", c))
        self.assertLess(un, self.idx("start earlyoom", c))
        self.assertTrue(any("--machine=claude-fallback@.host daemon-reload" in x for x in c))

    def test_uninstall_sysorch_failure_does_not_block_earlyoom_restore(self):
        self.assertEqual(self.sh("install.sh").returncode, 0)
        r = self.sh("uninstall.sh", FAKE_SYSORCH_UNINSTALL_RC="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING sysorch_restart uninstall failed", r.stderr)
        self.assertEqual((self.fake / "eo_active").read_text().strip(), "active")


class UninstallTests(ScriptCase):
    def test_restores_and_verifies_earlyoom_before_stopping_the_killer(self):
        self.assertEqual(self.sh("install.sh").returncode, 0)
        before = len(self.calls())
        r = self.sh("uninstall.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        c = self.calls()[before:]
        enable = self.idx("enable earlyoom", c)
        start = self.idx("start earlyoom", c)
        verify = self.idx("is-active --quiet earlyoom", c)
        stop = self.idx("disable --now pantheon-oom-killer", c)
        self.assertLess(enable, verify)
        self.assertLess(start, verify)
        self.assertLess(verify, stop)
        for d in DROPINS:
            self.assertEqual(self.dropin(d).read_text(), f"original {d}\n")
        self.assertFalse((self.root / "usr/local/sbin/pantheon_oom_killer.py").exists())
        self.assertFalse((self.root / "etc/systemd/system/pantheon-oom-killer.service").exists())
        self.assertFalse((self.root / "var/lib/pantheon/oom/install-backup").exists())
        self.assertIn("try-restart systemd-oomd", "\n".join(c))

    def test_aborts_with_killer_left_running_when_earlyoom_will_not_start(self):
        self.assertEqual(self.sh("install.sh").returncode, 0)
        before = len(self.calls())
        r = self.sh("uninstall.sh", FAKE_EO_START_FAILS="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("disable --now pantheon-oom-killer", "\n".join(self.calls()[before:]))
        self.assertTrue((self.root / "usr/local/sbin/pantheon_oom_killer.py").exists())
        self.assertTrue((self.root / "var/lib/pantheon/oom/install-backup/state").exists())

    def test_refuses_without_backup(self):
        r = self.sh("uninstall.sh")
        self.assertEqual(r.returncode, 1)
        self.assertIn("refusing to guess", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_refuses_without_root(self):
        r = self.sh("uninstall.sh", uid="1000")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.calls(), [])

    def test_masked_prior_state_is_restored_as_masked(self):
        self.set_eo("masked", "inactive")
        self.assertEqual(self.sh("install.sh").returncode, 0)
        before = len(self.calls())
        self.assertEqual(self.sh("uninstall.sh").returncode, 0)
        c = self.calls()[before:]
        self.idx("mask earlyoom", c)
        self.assertNotIn("start earlyoom", "\n".join(c))
        self.assertEqual((self.fake / "eo_enabled").read_text().strip(), "masked")

    def test_static_prior_state_needs_no_enable_or_mask(self):
        self.set_eo("static", "active")
        self.assertEqual(self.sh("install.sh").returncode, 0)
        before = len(self.calls())
        self.assertEqual(self.sh("uninstall.sh").returncode, 0)
        joined = "\n".join(self.calls()[before:])
        self.assertNotIn("enable earlyoom", joined)
        self.assertNotIn("mask earlyoom", joined)
        self.assertIn("start earlyoom", joined)


class UnitFileTests(unittest.TestCase):
    def test_unit_never_hits_a_start_limit_and_is_unkillable(self):
        text = (HERE / "pantheon-oom-killer.service").read_text()
        unit = text.split("[Service]")[0]
        self.assertIn("StartLimitIntervalSec=0", unit)
        self.assertIn("Restart=always", text)
        self.assertIn("OOMScoreAdjust=-1000", text)

    def test_scripts_pass_bash_syntax_check(self):
        for f in ("install.sh", "uninstall.sh"):
            r = subprocess.run(["bash", "-n", str(HERE / f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
