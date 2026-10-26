"""Tests for codex_poller_hold.sh and its use in the hand-move SOP (T-4330).

During a hand-move, a codex fallback's receiver handoff starts
codex-telegram-poller.service on the seat's bot token. On a seat with no
poller.offset, that first start confirms every queued update and injects none.
The helper holds the unit down with a runtime drop-in; the SOP's seat script
holds it from phase 1 until exit, and its rollback releases it inline.

Hermetic: a temp XDG_RUNTIME_DIR and a fake systemctl that logs its argv.
The real-systemd behaviour (a held start exits 1 and the unit goes failed;
`mask --runtime` would be a silent no-op) is recorded in the T-4330 commit.
"""
import json
import os
import pathlib
import re
import signal
import subprocess
import tempfile
import time
import unittest

TOOLS = pathlib.Path(__file__).resolve().parent
HELPER = TOOLS / "codex_poller_hold.sh"
SOP = TOOLS.parent / "sop" / "standard" / "hand-moving-an-established-seat-onto-the-control-plane.md"
UNIT = "codex-telegram-poller.service"
HOLD_NAME = "t4330-handmove-hold.conf"


def _bash_block(marker: str) -> str:
    """The ```bash block in the SOP whose text contains `marker`."""
    blocks = re.findall(r"```bash\n(.*?)```", SOP.read_text(encoding="utf-8"), flags=re.S)
    matches = [b for b in blocks if marker in b]
    if len(matches) != 1:
        raise AssertionError(f"expected one SOP bash block containing {marker!r}, found {len(matches)}")
    return matches[0]


class _Sandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.runtime = root / "run"
        self.runtime.mkdir()
        self.log = root / "systemctl.log"
        self.fake = root / "systemctl"
        self.fake.write_text(f'#!/bin/sh\necho "$*" >> {self.log}\nexit 0\n', encoding="utf-8")
        self.fake.chmod(0o755)
        self.env = {
            "PATH": f"{root}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "XDG_RUNTIME_DIR": str(self.runtime),
            "SYSTEMCTL": str(self.fake),
        }
        self.hold = self.runtime / "systemd" / "user" / f"{UNIT}.d" / HOLD_NAME

    def tearDown(self):
        self._tmp.cleanup()

    def helper(self, *args, **extra_env):
        return subprocess.run(
            ["bash", str(HELPER), *args],
            env={**self.env, **extra_env},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def calls(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


class HelperTest(_Sandbox):
    def test_hold_writes_a_dropin_whose_start_always_fails(self):
        r = self.helper("hold", "20260929T195852Z")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.hold.read_text(encoding="utf-8")
        self.assertIn("[Service]", text)
        self.assertIn("Restart=no", text)
        self.assertRegex(text, r"(?m)^ExecStartPre=.*exit 1'$")
        self.assertIn("20260929T195852Z", text)
        # systemd expands % specifiers and $VARS in Exec lines.
        self.assertNotRegex(text, r"[%$]")
        self.assertEqual(self.calls(), ["--user daemon-reload"])

    def test_status_is_zero_only_while_held(self):
        self.assertEqual(self.helper("status").returncode, 1)
        self.helper("hold", "S")
        self.assertEqual(self.helper("status").returncode, 0)
        self.helper("release")
        self.assertEqual(self.helper("status").returncode, 1)

    def test_status_names_the_stamp_that_took_the_hold(self):
        self.helper("hold", "20260929T195852Z")
        r = self.helper("status")
        self.assertIn("20260929T195852Z", r.stdout)
        self.assertEqual(r.stdout.count("\n"), 1)

    def test_a_failed_daemon_reload_makes_hold_fail_and_leaves_no_file(self):
        # T-4335 (rev-t4330's surviving mutant): an unloaded hold holds nothing.
        self.fake.write_text(
            f'#!/bin/sh\necho "$*" >> {self.log}\n[ "$2" = daemon-reload ] && exit 1\nexit 0\n',
            encoding="utf-8")
        r = self.helper("hold", "S")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.hold.exists())

    def test_release_removes_the_hold_reloads_and_clears_failed(self):
        self.helper("hold", "S")
        r = self.helper("release")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.hold.exists())
        self.assertFalse(self.hold.parent.exists())
        self.assertEqual(
            self.calls(),
            ["--user daemon-reload", "--user daemon-reload", f"--user reset-failed {UNIT}"],
        )

    def test_release_with_nothing_held_succeeds(self):
        r = self.helper("release")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.hold.exists())

    def test_release_keeps_other_dropins(self):
        other = self.hold.parent / "other.conf"
        self.helper("hold", "S")
        other.write_text("[Service]\n", encoding="utf-8")
        self.helper("release")
        self.assertFalse(self.hold.exists())
        self.assertTrue(other.exists())

    def test_hold_needs_a_stamp(self):
        r = self.helper("hold")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.hold.exists())
        self.assertEqual(self.calls(), [])

    def test_unit_override(self):
        self.helper("hold", "S", CODEX_POLLER_UNIT="probe.service")
        self.assertTrue((self.runtime / "systemd" / "user" / "probe.service.d" / HOLD_NAME).exists())
        self.assertFalse(self.hold.exists())

    def test_unknown_verb_is_a_usage_error(self):
        self.assertEqual(self.helper("mask").returncode, 2)


_STUBS = {
    # Every command the seat script runs before the phase-2 gate, faked. Each one logs
    # its argv to $T4335_LOG, so a test can assert on the order of real side effects.
    "systemctl": r'''
echo "systemctl $*" >> "$T4335_LOG"
case "$*" in
  "--user is-active --quiet claude-telegram-fallback.service") exit 0 ;;
  "--user is-active"*) exit 3 ;;
  "--user is-enabled"*) echo disabled; exit 1 ;;
esac
exit 0''',
    "tmux": r'''
echo "tmux $*" >> "$T4335_LOG"
case "$*" in
  *pane_pid*) echo stub-pane ;;
  *pane_current_path*) echo "$HOME" ;;
  "ls -F"*) echo claude-telegram-bot-fallback ;;
esac
exit 0''',
    "git": r'''
case "$*" in
  *"branch --show-current"*) echo main ;;
  *"merge-base --is-ancestor a85db452e"*) [ -e "$T4335_STUBS/no-t4362" ] && exit 1 ;;
esac
exit 0''',
    "python3": r'''
case "$*" in
  *backoff_remaining*) echo 0 ;;
  *"notify_policy.py in-maintenance"*)
    echo "python3 $*" >> "$T4335_LOG"
    [ -e "$T4335_STUBS/not-in-maintenance" ] && { echo not-in-maintenance; exit 1; }
    echo in-maintenance ;;
  *) echo fake argv ;;
esac''',
    "sleep": r'''
echo "sleep $*" >> "$T4335_LOG"
[ -e "$T4335_STUBS/block-sleep" ] && exec /bin/sleep 30
exit 0''',
    "rollback.sh": r'''
echo "rollback PERSONA=$PERSONA STAMP=$STAMP" >> "$T4335_LOG"''',
    # Stands in for the repo helper, so nothing is written under the real /run/user.
    "codex_poller_hold.sh": r'''
echo "helper $*" >> "$T4335_LOG"
case "$1" in
  status) [ -e "$T4335_STUBS/stale-hold" ] || exit 1; echo "# T-4330 hand-move hold (OLDSTAMP)" ;;
  hold) [ -e "$T4335_STUBS/hold-fails" ] && exit 1 ;;
esac
exit 0''',
}
_DARK = "systemctl --user disable --now claude-telegram-fallback.service"


def _code_lines(script: str) -> list:
    """The script's lines minus comments, so a commented-out line never counts."""
    return [line for line in script.splitlines() if line.strip() and not line.lstrip().startswith("#")]


class SeatScriptBehaviourTest(unittest.TestCase):
    """T-4335: run the SOP's own seat script, as the driver saves it, against stubs that
    record every call. A fake HOME and PATH stubs keep it off the live seat."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = pathlib.Path(self._tmp.name)
        self.home, self.stubs, self.log = root / "home", root / "stubs", root / "calls.log"
        self.stubs.mkdir()
        for name, body in _STUBS.items():
            path = self.stubs / name
            path.write_text("#!/bin/sh" + body + "\n", encoding="utf-8")
            path.chmod(0o755)
        tools = self.home / "pantheon-config" / "tools"
        tools.mkdir(parents=True)
        os.replace(self.stubs / "codex_poller_hold.sh", tools / "codex_poller_hold.sh")
        # T-4377: phase 0 checks the clone carries the T-4371 render gate by feature.
        (tools / "send_keys_submit.sh").write_text("# stub\n--render-wait\n", encoding="utf-8")
        # T-2953: phase 0 checks the clone carries the one-shot maintenance bypass by feature.
        (tools / "persona_bringup.py").write_text("# stub\ndef consume_maintenance_bypass(): pass\n", encoding="utf-8")
        memdir = self.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(self.home)) / "memory"
        memdir.mkdir(parents=True)
        (self.home / ".claude" / "settings.json").write_text(
            '{"autoMemoryDirectory": "%s"}' % memdir, encoding="utf-8")
        (self.home / ".claude.json").write_text("{}", encoding="utf-8")
        for rel in (".local/bin/recovery-bot-supervise.sh", ".config/systemd/user/claude-telegram-fallback.service"):
            (self.home / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.home / rel).write_text("stub\n", encoding="utf-8")
        self.script = root / "handmove-seat.sh"
        self.script.write_text(_bash_block("# handmove-seat.sh"), encoding="utf-8")
        self.env = {
            "PATH": f"{self.stubs}:/usr/bin:/bin", "HOME": str(self.home),
            "PERSONA": "recovery", "STAMP": "20260930T000000Z", "ROLLBACK": str(self.stubs / "rollback.sh"),
            "T4335_LOG": str(self.log), "T4335_STUBS": str(self.stubs),
        }

    def run_script(self, text=None):
        if text is not None:
            self.script.write_text(text, encoding="utf-8")
        return subprocess.run(["bash", str(self.script)], env=self.env, capture_output=True,
                              text=True, timeout=60)

    def calls(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def assert_order(self, *entries):
        calls = self.calls()
        idx = []
        for entry in entries:
            self.assertIn(entry, calls, f"missing {entry!r} in {calls}")
            idx.append(calls.index(entry))
        self.assertEqual(idx, sorted(idx), f"out of order in {calls}")

    def test_a_failed_gate_holds_before_the_dark_window_and_releases_after_the_rollback(self):
        # Phase 2's "tmux server still up" gate fails (the stub server never goes away).
        r = self.run_script()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("GATE FAILED", r.stdout)
        self.assert_order("helper status", "helper hold 20260930T000000Z", _DARK,
                          "rollback PERSONA=recovery STAMP=20260930T000000Z", "helper release")

    def test_a_normal_exit_releases_the_hold(self):
        # Run through phase 1, then exit 0: the same trap covers DONE.
        text = self.script.read_text(encoding="utf-8")
        cut = text.index("# ---- phase 2")
        r = self.run_script(text[:cut] + "exit 0\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assert_order("helper hold 20260930T000000Z", "helper release")
        self.assertNotIn(_DARK, self.calls())

    def test_sigterm_mid_move_releases_the_hold(self):
        # Stopping the driver's transient unit TERMs the script's whole cgroup.
        (self.stubs / "block-sleep").touch()
        proc = subprocess.Popen(["bash", str(self.script)], env=self.env, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 30
            while not any(c.startswith("sleep") for c in self.calls()):
                self.assertLess(time.monotonic(), deadline, f"never reached phase 2's sleep: {self.calls()}")
                time.sleep(0.05)
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        self.assert_order("helper hold 20260930T000000Z", _DARK, "helper release")
        self.assertFalse(any(c.startswith("rollback") for c in self.calls()))

    def test_a_failed_hold_is_fatal_before_the_seat_goes_dark(self):
        (self.stubs / "hold-fails").touch()
        r = self.run_script()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("could not hold codex-telegram-poller", r.stdout)
        self.assertNotIn(_DARK, self.calls())
        # The trap is set BEFORE the hold, so even a half-made hold is released.
        self.assert_order("helper hold 20260930T000000Z", "rollback PERSONA=recovery STAMP=20260930T000000Z",
                          "helper release")

    def test_a_stale_hold_refuses_in_preflight_and_changes_nothing(self):
        # rev-t4330: after a SIGKILL the hold stays, and a held unit reads "failed", not
        # "active". Phase 0 is read-only, so refuse and name it; don't release it here.
        (self.stubs / "stale-hold").touch()
        r = self.run_script()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("PREFLIGHT REFUSED", r.stdout)
        self.assertIn("OLDSTAMP", r.stdout)
        helper_calls = [c for c in self.calls() if c.startswith("helper")]
        self.assertEqual(helper_calls, ["helper status"])
        self.assertNotIn(_DARK, self.calls())

    def test_preflight_refuses_without_the_helper(self):
        (self.home / "pantheon-config" / "tools" / "codex_poller_hold.sh").unlink()
        r = self.run_script()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("lacks tools/codex_poller_hold.sh", r.stdout)
        self.assertNotIn(_DARK, self.calls())

    def test_preflight_refuses_unless_the_seat_is_in_maintenance(self):
        # T-4377: the T-4362 consumer guard only holds the clone still while the seat is
        # armed, so an un-armed seat must refuse before anything changes.
        (self.stubs / "not-in-maintenance").touch()
        r = self.run_script()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("PREFLIGHT REFUSED", r.stdout)
        self.assertIn("recovery is not in maintenance", r.stdout)
        self.assertNotIn(_DARK, self.calls())
        self.assertNotIn("helper hold 20260930T000000Z", self.calls())

    def test_preflight_checks_maintenance_and_passes_when_armed(self):
        r = self.run_script()
        self.assertIn("python3 " + str(self.home / "pantheon-config" / "tools" / "notify_policy.py")
                      + " in-maintenance recovery", self.calls())
        self.assertNotIn("is not in maintenance", r.stdout)

    def test_preflight_refuses_without_the_t4371_render_gate(self):
        (self.home / "pantheon-config" / "tools" / "send_keys_submit.sh").write_text(
            "# stub without the gate\n", encoding="utf-8")
        r = self.run_script()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("lacks T-4371", r.stdout)
        self.assertNotIn(_DARK, self.calls())

    def test_preflight_refuses_without_the_t2953_bypass(self):
        (self.home / "pantheon-config" / "tools" / "persona_bringup.py").write_text(
            "# stub without the bypass\n", encoding="utf-8")
        r = self.run_script()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("lacks the T-2953 maintenance bypass", r.stdout)
        self.assertNotIn(_DARK, self.calls())

    def test_preflight_refuses_without_the_t4362_guard(self):
        (self.stubs / "no-t4362").touch()
        r = self.run_script()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("lacks T-4362", r.stdout)
        self.assertNotIn(_DARK, self.calls())

    def test_exactly_one_exit_trap_and_it_releases(self):
        # A second `trap ... EXIT` later in the script would replace the release.
        traps = [l for l in _code_lines(_bash_block("# handmove-seat.sh"))
                 if re.match(r"\s*trap\b.*\bEXIT\s*$", l)]
        self.assertEqual(len(traps), 1, traps)
        self.assertIn('codex_poller_hold.sh" release', traps[0])


class MaintenanceCliContractTest(unittest.TestCase):
    """T-4377 (rev-t4377 r1): phase 0 trusts `notify_policy.py in-maintenance` to exit 0 ONLY
    when the seat is armed. The seat-script harness stubs python3, so pin the REAL CLI here:
    any other exit on an armed seat would refuse a good move, and a 0 on an un-armed one would
    let the move fire with the T-4362 guard asleep."""

    CLI = TOOLS / "notify_policy.py"

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.reg = pathlib.Path(d.name) / "seat_maintenance.json"
        self.env = dict(os.environ, PANTHEON_MAINTENANCE_REGISTRY=str(self.reg))
        self.env.pop("PANTHEON_STATE_DIR", None)

    def rc(self, *args):
        return subprocess.run(["python3", str(self.CLI), *args], env=self.env,
                              capture_output=True, text=True, timeout=60).returncode

    def arm(self, persona="recovery"):
        self.assertEqual(self.rc("enter-maintenance", persona, "--minutes", "5", "--reason", "T-4377 test"), 0)

    def test_no_registry_is_not_armed(self):
        self.assertEqual(self.rc("in-maintenance", "recovery"), 1)

    def test_armed_seat_exits_zero(self):
        self.arm()
        self.assertEqual(self.rc("in-maintenance", "recovery"), 0)

    def test_another_seat_armed_is_not_armed(self):
        self.arm("orchestrator")
        self.assertEqual(self.rc("in-maintenance", "recovery"), 1)

    def test_expired_entry_is_not_armed(self):
        self.arm()
        data = json.loads(self.reg.read_text(encoding="utf-8"))
        data["maintenance"]["recovery"]["expires"] = "2020-01-01T00:05:00Z"
        self.reg.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.rc("in-maintenance", "recovery"), 1)

    def test_corrupt_registry_is_not_armed(self):
        self.reg.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.rc("in-maintenance", "recovery"), 1)

    @unittest.skipIf(os.geteuid() == 0, "root can read a mode-000 file")
    def test_unreadable_registry_is_not_armed(self):
        self.arm()
        self.reg.chmod(0)
        self.addCleanup(self.reg.chmod, 0o600)
        self.assertEqual(self.rc("in-maintenance", "recovery"), 1)


class SopContractTest(_Sandbox):
    def test_rollback_releases_what_the_helper_holds(self):
        # Run the rollback's own release lines against a hold the helper made:
        # the two must agree on the drop-in path.
        rollback = _bash_block("# handmove-rollback.sh")
        start = rollback.find("# T-4330: release")
        end = rollback.find('must "codex poller hold still present"')
        self.assertNotEqual(start, -1, "the rollback never releases the codex poller hold")
        self.assertNotEqual(end, -1)
        launchers_down = rollback.find('disable --now "pantheon-runtime-controller@$PERSONA.service"')
        self.assertLess(launchers_down, start, "release before both launchers are down")
        snippet = rollback[start:rollback.find("\n", end) + 1]
        self.helper("hold", "S")
        self.assertTrue(self.hold.exists())
        r = subprocess.run(
            ["bash", "-c", 'set -uo pipefail\nbad=0; must() { echo "MUST: $*"; bad=1; }\n'
             'systemctl() { "$SYSTEMCTL" "$@"; }\n' + snippet + 'exit "$bad"\n'],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.hold.exists())
        self.assertIn(f"--user reset-failed {UNIT}", self.calls())


if __name__ == "__main__":
    unittest.main()
