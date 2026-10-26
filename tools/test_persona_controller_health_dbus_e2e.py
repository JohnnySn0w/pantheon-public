"""End-to-end scratch-bus test for the T-5137 PersonaController1 health leg (T-5410).

What runs: a PRIVATE `dbus-daemon` (tools/_host_control_testbus.spawn_daemon: explicit
socket address, PDEATHSIG + sweep teardown) hosts the PRODUCTION bootstrap
`runtime_controller._start_persona_controller_dbus_service` in a driver subprocess
(tools/persona_controller_health_dbus_e2e_driver.py). Only the two live-state reads
(`dbus_health_properties`, `health_leg.snapshot`) are replaced by file-backed stubs. Reads go
over dbus-python (driver `getall` mode) AND `busctl --user --json=short` (the collector's real
command line). Every child gets DBUS_SESSION_BUS_ADDRESS pointing at the scratch socket and
no inherited bus env; the driver refuses a non-scratch address. No real bus, unit or
controller is ever touched. A same-process live bus hangs under pytest (see
test_persona_controller_health_dbus.py header), hence the subprocesses; all waits are bounded.

Fixtures (tools/fixtures/health_collector/persona_getall_flag_{off,on}.json) are the
verbatim `busctl --json=short ... GetAll` stdout of this emitter with pinned stub values.
RE-CAPTURE after any emitter change:

    python3 -m tools.test_persona_controller_health_dbus_e2e --capture

(a byte-stability test fails if the committed files differ from a fresh capture).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools import persona_controller_health_dbus as pchd

REPO = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO / "tools" / "fixtures" / "health_collector"
FIXTURE_OFF = FIXTURE_DIR / "persona_getall_flag_off.json"
FIXTURE_ON = FIXTURE_DIR / "persona_getall_flag_on.json"
PERSONA = "e2e"
BUS_NAME = pchd.bus_name_for(PERSONA)
OBJ_PATH = pchd.object_path_for(PERSONA)
IFACE = pchd.PERSONA_CONTROLLER_INTERFACE
WAIT_S = 30.0

#: Pinned stub values: re-capture is byte-stable. Mirror the shapes persona_controller_health_leg
#: .project() emits (Subcomponents ids -> ready/..., Revisions {"invariant_list": ref}).
STUB_BASE = {
    "ActiveRuntime": "claude/anthropic-code",
    "ViabilityStatus": "viable",
    "LastSelectionTs": "2026-10-23T14:00:00Z",
    "LastFailureClass": "",
    "NextCooldownExpiryTs": "",
    "Holds": ["codex/x"],
    "ModalPresent": True,
    "ModalKind": "session-limit",
    "ModalResetAt": "2026-10-23T18:00:00Z",
    "SeatState": "paused-intake",
    "SeatStateSources": ["seat-state"],
    "SeatStateOwner": "operator",
    "SeatStateReason": "e2e pinned stub",
    "SeatStateExpires": "2026-10-24T00:00:00Z",
    "SeatStateFault": "",
}
STUB_LEG = {
    "ContractRevision": "pantheon-health/v1",
    "Status": "ready",
    "Subcomponents": {"viability": "ready", "modal": "ready"},
    "Reasons": {},
    "Revisions": {"invariant_list": "inv-persona@sha256:abc123"},
    "InvariantListRef": "inv-persona@sha256:abc123",
    "SecurityDrift": False,
}
BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


def _have_dbus_stack() -> bool:
    try:
        import dbus  # noqa: F401
        import gi  # noqa: F401
    except ImportError:
        return False
    return all(shutil.which(t) for t in ("dbus-daemon", "busctl"))


def declared_signatures(health_leg: bool) -> dict[str, str]:
    sigs = dict(pchd.PERSONA_CONTROLLER_PROPERTY_SIGNATURES)
    if health_leg:
        sigs.update(pchd.HEALTH_LEG_SIGNATURES)
    return sigs


def fixture_signatures(text: str) -> dict[str, str]:
    """{property: signature} from a busctl --json=short GetAll document."""
    doc = json.loads(text)
    assert doc["type"] == "a{sv}", doc["type"]
    return {k: v["type"] for k, v in doc["data"][0].items()}


def drift(text: str, health_leg: bool) -> list[str]:
    """Human-readable differences between a fixture and the emitter's declarations."""
    got, want = fixture_signatures(text), declared_signatures(health_leg)
    out = [f"missing {k}" for k in want if k not in got]
    out += [f"unexpected {k}" for k in got if k not in want]
    out += [f"{k}: fixture {got[k]} != emitter {want[k]}" for k in want if k in got and got[k] != want[k]]
    if list(got) != list(want):
        out.append("property order differs")
    return out


class ScratchEmitter:
    """Private bus + production-bootstrap driver. Context manager; always tears down."""

    def __init__(self, *, flag_on: bool) -> None:
        self.flag_on = flag_on

    def child_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("DBUS_", "PANTHEON_PERSONA_HEALTH_LEG"))}
        env["DBUS_SESSION_BUS_ADDRESS"] = self.bus.bus_address
        env["XDG_RUNTIME_DIR"] = self.work  # no real /run/user/<uid>/bus reachable by fallback
        env["PYTHONPATH"] = str(REPO)
        if self.flag_on:
            env["PANTHEON_PERSONA_HEALTH_LEG"] = "on"
        return env

    def __enter__(self) -> "ScratchEmitter":
        from tools import _host_control_testbus as testbus

        self.bus = self.driver = self.log = None
        self.work = tempfile.mkdtemp(prefix="t5410-", dir="/tmp")
        try:
            self.bus = testbus.spawn_daemon("t5410-", BUS_CONFIG)
            assert self.bus.bus_address.startswith("unix:path=/tmp/"), self.bus.bus_address
            self.write_state(STUB_BASE, STUB_LEG)
            self.log = open(os.path.join(self.work, "driver.log"), "w", encoding="utf-8")
            self.driver = subprocess.Popen(
                [sys.executable, "-m", "tools.persona_controller_health_dbus_e2e_driver",
                 "serve", self.work],
                cwd=REPO, env=self.child_env(), stdout=self.log, stderr=self.log,
                preexec_fn=testbus.pdeathsig_preexec_fn())
            self.bus.register_driver(self.driver.pid)
            self._wait(lambda: os.path.exists(os.path.join(self.work, "ready")), "driver ready")
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        from tools import _host_control_testbus as testbus

        if self.driver is not None and self.driver.poll() is None:
            self.driver.terminate()
            try:
                self.driver.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.driver.kill()
                self.driver.wait(timeout=10)
        if self.log is not None:
            self.log.close()
        if self.bus is not None:
            testbus.stop_daemon(self.bus)
        shutil.rmtree(self.work, ignore_errors=True)

    def _wait(self, pred, what: str) -> None:
        deadline = time.monotonic() + WAIT_S
        while not pred():
            if self.driver is not None and self.driver.poll() is not None:
                raise AssertionError(f"driver exited while waiting for {what}: {self.driver_log()}")
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out waiting for {what}: {self.driver_log()}")
            time.sleep(0.05)

    def driver_log(self) -> str:
        try:
            return Path(self.work, "driver.log").read_text(encoding="utf-8")[-2000:]
        except OSError:
            return ""

    def write_state(self, base: dict, leg: dict) -> None:
        tmp = Path(self.work, "state.json.tmp")
        tmp.write_text(json.dumps({"base": base, "leg": leg}), encoding="utf-8")
        tmp.rename(Path(self.work, "state.json"))

    def busctl_getall(self) -> str:
        """The collector's real command line, against the scratch bus."""
        proc = subprocess.run(
            ["busctl", "--user", "--json=short", "call", BUS_NAME, OBJ_PATH,
             "org.freedesktop.DBus.Properties", "GetAll", "s", IFACE],
            env=self.child_env(), capture_output=True, text=True, timeout=WAIT_S)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    def driver_mode(self, mode: str, outfile: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-m", "tools.persona_controller_health_dbus_e2e_driver", mode, outfile],
            cwd=REPO, env=self.child_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def introspection(self) -> dict:
        out = os.path.join(self.work, "introspect.json")
        proc = self.driver_mode("introspect", out)
        text, _ = proc.communicate(timeout=WAIT_S)
        assert proc.returncode == 0, text
        return json.loads(Path(out).read_text(encoding="utf-8"))

    def dbus_python_getall(self) -> dict:
        out = os.path.join(self.work, "getall.json")
        proc = self.driver_mode("getall", out)
        text, _ = proc.communicate(timeout=WAIT_S)
        assert proc.returncode == 0, text
        return json.loads(Path(out).read_text(encoding="utf-8"))


@unittest.skipUnless(_have_dbus_stack(), "needs dbus-daemon, busctl, dbus-python, PyGObject")
class ScratchBusE2ETests(unittest.TestCase):
    def test_flag_on_getall_22_properties_with_signatures(self) -> None:
        want = declared_signatures(True)
        self.assertEqual(len(want), 22)
        with ScratchEmitter(flag_on=True) as em:
            via_py = em.dbus_python_getall()
            via_busctl = fixture_signatures(em.busctl_getall())
        self.assertEqual({k: v[0] for k, v in via_py.items()}, want)
        self.assertEqual(via_busctl, want)
        self.assertEqual(via_py["Holds"][1], ["codex/x"])
        self.assertEqual(via_py["Subcomponents"][1], STUB_LEG["Subcomponents"])
        self.assertEqual(via_py["SecurityDrift"], ["b", False])
        self.assertEqual(via_py["InvariantListRef"][1], STUB_LEG["InvariantListRef"])

    def test_flag_off_getall_exactly_fifteen_base_properties(self) -> None:
        want = declared_signatures(False)
        self.assertEqual(len(want), 15)
        with ScratchEmitter(flag_on=False) as em:
            via_py = em.dbus_python_getall()
            via_busctl = fixture_signatures(em.busctl_getall())
        self.assertEqual({k: v[0] for k, v in via_py.items()}, want)
        self.assertEqual(via_busctl, want)

    def test_flag_wiring_reaches_introspect_and_get(self) -> None:
        """GetAll marshals whatever the provider returns, so the flag's wiring
        (`health_leg=True`) is observable on Introspect and Get, which this pins."""
        with ScratchEmitter(flag_on=True) as em:
            on = em.introspection()
        with ScratchEmitter(flag_on=False) as em:
            off = em.introspection()
        self.assertEqual(on["xml_props"], declared_signatures(True))
        self.assertEqual(on["get_leg"], "ok")
        self.assertEqual(off["xml_props"], declared_signatures(False))
        self.assertEqual(off["get_leg"], "org.freedesktop.DBus.Error.InvalidArgs")

    def test_properties_changed_carries_full_snapshot_after_transition(self) -> None:
        with ScratchEmitter(flag_on=True) as em:
            sig_file = os.path.join(em.work, "sig.json")
            listener = em.driver_mode("listen", sig_file)
            try:
                em._wait(lambda: os.path.exists(os.path.join(em.work, "sig.ready")), "listener ready")
                # Service is up and parked and nothing has been emitted yet: now transition.
                self.assertFalse(os.path.exists(sig_file))
                em.write_state({**STUB_BASE, "ViabilityStatus": "all-held", "ActiveRuntime": ""},
                               {**STUB_LEG, "Status": "degraded", "Reasons": {"viability": "all-held"},
                                "SecurityDrift": True})
                Path(em.work, "emit").write_text("1", encoding="utf-8")
                em._wait(lambda: os.path.exists(sig_file), "PropertiesChanged signal")
            finally:
                listener.kill()
                listener.communicate(timeout=10)
            sig = json.loads(Path(sig_file).read_text(encoding="utf-8"))
        self.assertEqual(sig["iface"], IFACE)
        self.assertEqual(sig["invalidated"], [])
        self.assertEqual({k: v[0] for k, v in sig["changed"].items()}, declared_signatures(True))
        self.assertEqual(sig["changed"]["ViabilityStatus"][1], "all-held")   # the transition
        self.assertEqual(sig["changed"]["Status"][1], "degraded")
        self.assertEqual(sig["changed"]["Reasons"][1], {"viability": "all-held"})
        self.assertEqual(sig["changed"]["SecurityDrift"][1], True)
        self.assertEqual(sig["changed"]["Holds"][1], ["codex/x"])            # full snapshot, not a diff

    def test_committed_fixtures_equal_fresh_capture(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            capture(Path(td))
            for name in (FIXTURE_OFF.name, FIXTURE_ON.name):
                self.assertEqual((FIXTURE_DIR / name).read_text(encoding="utf-8"),
                                 Path(td, name).read_text(encoding="utf-8"),
                                 f"{name} stale: re-run --capture")


class FixtureDriftGuardTests(unittest.TestCase):
    """No bus needed: emitter drift (names, signatures, order) fails here."""

    def test_flag_off_fixture_matches_emitter(self) -> None:
        self.assertEqual(drift(FIXTURE_OFF.read_text(encoding="utf-8"), False), [])

    def test_flag_on_fixture_matches_emitter(self) -> None:
        self.assertEqual(drift(FIXTURE_ON.read_text(encoding="utf-8"), True), [])

    def test_guard_goes_red_on_mutated_fixture(self) -> None:
        doc = json.loads(FIXTURE_ON.read_text(encoding="utf-8"))
        doc["data"][0]["SecurityDrift"]["type"] = "s"
        del doc["data"][0]["Reasons"]
        doc["data"][0]["Extra"] = {"type": "s", "data": ""}
        problems = drift(json.dumps(doc), True)
        self.assertIn("missing Reasons", problems)
        self.assertIn("unexpected Extra", problems)
        self.assertIn("SecurityDrift: fixture s != emitter b", problems)

    def test_flag_off_fixture_is_not_valid_for_flag_on(self) -> None:
        self.assertTrue(drift(FIXTURE_OFF.read_text(encoding="utf-8"), True))

    def test_fixtures_are_parsed_by_the_collector(self) -> None:
        from tools import health_collector as hc

        off = hc.parse_getall(FIXTURE_OFF.read_text(encoding="utf-8"))
        on = hc.parse_getall(FIXTURE_ON.read_text(encoding="utf-8"))
        self.assertEqual(len(off), 15)
        self.assertEqual(len(on), 22)
        self.assertEqual(on["Subcomponents"][0], "a{ss}")
        self.assertEqual(on["Holds"], ("as", ["codex/x"]))


def capture(out_dir: Path) -> None:
    """Write both fixtures (verbatim busctl stdout) under out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for flag_on, target in ((False, FIXTURE_OFF.name), (True, FIXTURE_ON.name)):
        with ScratchEmitter(flag_on=flag_on) as em:
            Path(out_dir, target).write_text(em.busctl_getall(), encoding="utf-8")


if __name__ == "__main__" and "--capture" in sys.argv:  # noqa: E305
    capture(FIXTURE_DIR)
    print(f"captured -> {FIXTURE_DIR}")
    sys.exit(0)
