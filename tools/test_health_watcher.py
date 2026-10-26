"""Tests for tools/health_watcher.py + the T-5409 additions to tools/health_collector.py.

RFC-004 SS6 tests 10 (initial Get + subscription reconcile, incl. a change mid-Get) and 12
(a signal missed while the watcher was down is recovered). Test 11 (restart + re-Get while
emitters keep publishing) and tests 13-14 are follow-ups.

Two layers:
  * Unit: fake taps + fake collect function (no bus, no subprocess, no sudo).
  * Scratch bus: REAL PersonaController1Object / Health1Object served on PRIVATE dbus-daemons
    (tools/_host_control_testbus: explicit addresses, PDEATHSIG), the REAL tap subprocess, the
    real busctl reader. Never a live bus, never sudo, never systemctl. Every wait is bounded and
    teardown kills taps and daemons even on failure. Skipped (typed) if dbus-python / gi /
    dbus-daemon are missing.

The assertions that matter are on the OUTPUT FILE having converged, not on a collect having been
called (an arbitrary call count proves nothing about the aggregate being true).
"""
from __future__ import annotations

import fcntl
import io
import json
import os
import pwd
import queue
import shutil
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tools import health_collector as hc
from tools import health_watcher as hw
from tools.health_export import SudoReader

REPO = Path(__file__).resolve().parent.parent
ME = pwd.getpwuid(os.getuid()).pw_name
PERSONAS = [("scratchp", ME, None)]

try:
    import dbus
    import dbus.bus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib
    import tools._host_control_testbus as testbus
    HAVE_DBUS = bool(testbus.DBUS_DAEMON)
except ImportError:  # pragma: no cover
    HAVE_DBUS = False


# =================================================================== unit layer
class FakeTap:
    def __init__(self, spec, events, *, behaviour="ready"):
        self.spec, self.events, self.stopped = spec, events, False
        if behaviour == "ready":
            events.put((spec.label, "ready"))
        elif behaviour == "refuse":
            events.put((spec.label, "exit"))

    def stop(self):
        self.stopped = True


def _wait(pred, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class WatcherHarness:
    """Runs a Watcher on a thread with fake taps; `truth` is the fake emitter state and
    `out` the fake aggregate that collect() writes (a snapshot of truth at collect time)."""

    def __init__(self, labels=("system", "session:a"), min_interval=0.1, collect_hook=None,
                 refuse=(), rc=0, **kw):
        self.truth = {"v": 0}
        self.out = {"v": None, "mode": None}
        self.calls = []
        self.taps = {}
        self.refuse = set(refuse)
        self.hook = collect_hook
        self.rc = rc
        self.logs = []
        specs = [hw.TapSpec(l, "system" if l == "system" else "session", "i") for l in labels]

        def collect_fn(mode):
            seen = self.truth["v"]                 # the "Get"
            self.calls.append(mode)
            if self.hook:
                self.hook(self)
            if callable(self.rc):
                r = self.rc(len(self.calls))
                if r:
                    return r
            self.out.update(v=seen, mode=mode)
            return 0

        def factory(spec, events):
            beh = "refuse" if spec.label in self.refuse else "ready"
            t = FakeTap(spec, events, behaviour=beh)
            self.taps.setdefault(spec.label, []).append(t)
            return t

        self.w = hw.Watcher(specs, collect_fn=collect_fn, tap_factory=factory,
                            min_interval=min_interval, idle_tick=0.05, backoff_start=0.1,
                            backoff_max=0.4, log=self.logs.append, **kw)
        self.thread = threading.Thread(target=self.w.run, daemon=True)

    def signal(self, label="system"):
        self.w.events.put((label, "signal"))

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.w.stop_event.set()
        self.thread.join(timeout=10)
        assert not self.thread.is_alive()


class WatcherLogicTests(unittest.TestCase):
    def test_startup_does_initial_full_collect(self):
        with WatcherHarness() as h:
            self.assertTrue(_wait(lambda: h.out["mode"] == "startup"))

    def test_g1_signal_after_park_converges_output(self):
        with WatcherHarness() as h:
            self.assertTrue(_wait(lambda: h.out["v"] == 0))
            time.sleep(0.4)                       # parked: idle, no pending work
            n = len(h.calls)
            h.truth["v"] = 7
            h.signal("session:a")
            self.assertTrue(_wait(lambda: h.out["v"] == 7), "output did not converge after signal")
            self.assertEqual(h.out["mode"], "signal")
            self.assertGreater(len(h.calls), n)

    def test_signal_during_collect_causes_followup_and_converges(self):
        # RFC-004 SS6.10: a state change mid-Get is not missed.
        def hook(h):
            if len(h.calls) == 1:                 # inside the initial collect, after its Get
                h.truth["v"] = 5
                h.signal("system")
                time.sleep(0.2)
        with WatcherHarness(collect_hook=hook) as h:
            self.assertTrue(_wait(lambda: h.out["v"] == 5), "mid-Get change was missed")
            self.assertGreaterEqual(len(h.calls), 2)

    def test_signal_storm_is_debounced_and_converges(self):
        with WatcherHarness(min_interval=0.3) as h:
            self.assertTrue(_wait(lambda: h.out["v"] == 0))
            base = len(h.calls)
            t0 = time.monotonic()
            for i in range(1, 61):
                h.truth["v"] = i
                h.signal()
            self.assertTrue(_wait(lambda: h.out["v"] == 60))
            elapsed = time.monotonic() - t0
            made = len(h.calls) - base
            self.assertLessEqual(made, int(elapsed / 0.3) + 2)
            self.assertLess(made, 10)

    def test_refused_tap_is_counted_logged_and_leaves_account_to_tick(self):
        with WatcherHarness(refuse={"session:a"}) as h:
            self.assertTrue(_wait(lambda: h.out["mode"] == "startup"))   # still collects
            self.assertTrue(_wait(lambda: h.w.refused.get("session:a", 0) >= 1))
            self.assertNotIn("system", h.w.refused)
            self.assertTrue(any("session:a" in m for m in h.logs))
            self.assertTrue(all("sudo" not in m and "password" not in m for m in h.logs))

    def test_refused_tap_retries_with_capped_backoff_then_triggers_collect(self):
        state = {"n": 0}
        with WatcherHarness(refuse=set()) as h:
            self.assertTrue(_wait(lambda: h.out["v"] == 0))
            base = len(h.calls)
            h.truth["v"] = 3
            h.w.events.put(("session:a", "exit"))            # tap dies mid-run
            self.assertTrue(_wait(lambda: h.out["v"] == 3), "gap did not trigger a re-Get")
            self.assertTrue(_wait(lambda: len(h.taps["session:a"]) >= 2), "tap not respawned")
            self.assertGreater(len(h.calls), base)
            state["n"] = 1

    def test_busy_lock_keeps_dirty_and_retries(self):
        with WatcherHarness(rc=lambda n: hc.RC_BUSY if n == 1 else 0) as h:
            self.assertTrue(_wait(lambda: h.out["mode"] is not None))
            self.assertGreaterEqual(len(h.calls), 2)

    def test_startup_collect_retried_after_busy_keeps_startup_label(self):
        with WatcherHarness(rc=lambda n: hc.RC_BUSY if n == 1 else 0) as h:
            self.assertTrue(_wait(lambda: h.out["mode"] is not None))
        self.assertEqual(h.calls[:2], ["startup", "startup"])
        self.assertEqual(h.out["mode"], "startup")

    def test_stop_stops_every_tap(self):
        h = WatcherHarness()
        with h:
            self.assertTrue(_wait(lambda: h.out["mode"] == "startup"))
        self.assertTrue(all(t.stopped for ts in h.taps.values() for t in ts))


class _RecordingStream(io.BytesIO):
    """Records the size argument of every readline() so the bound is asserted, not assumed."""

    def __init__(self, data):
        super().__init__(data)
        self.limits = []

    def readline(self, size=-1):
        self.limits.append(size)
        return super().readline(size)


class TokenReaderTests(unittest.TestCase):
    def _pump(self, data):
        ev = queue.Queue()
        tap = hw.ProcessTap(hw.TapSpec("t", "session", "i", account="no-such-acct-xyz"), queue.Queue(),
                            SudoReader())
        tap.spec, tap.events = hw.TapSpec("t", "session", "i"), ev
        stream = _RecordingStream(data)
        tap._read_tokens(stream)
        out = []
        while not ev.empty():
            out.append(ev.get_nowait())
        return out, stream

    def test_every_read_is_bounded(self):
        _, stream = self._pump(b"R\n" + b"x" * 100_000 + b"\nS\n")
        self.assertTrue(stream.limits and all(isinstance(n, int) and 0 < n <= hw.MAX_TOKEN_LINE
                                              for n in stream.limits), stream.limits[:3])

    def test_overlong_line_discarded_and_next_valid_token_counts(self):
        out, _ = self._pump(b"R\n" + b"S" * 5000 + b"\nS\n")
        self.assertEqual(out, [("t", "ready"), ("t", "signal")])       # the long line is not a signal

    def test_overlong_line_ending_exactly_at_cap_boundary(self):
        out, _ = self._pump(b"x" * (hw.MAX_TOKEN_LINE - 1) + b"\nS\n" + b"y" * hw.MAX_TOKEN_LINE + b"\nS\n")
        self.assertEqual(out, [("t", "signal"), ("t", "signal")])

    def test_overlong_without_newline_then_eof_is_inert(self):
        out, _ = self._pump(b"S" * 10_000)
        self.assertEqual(out, [])

    def test_watcher_not_stalled_by_overlong_line_from_real_subprocess(self):
        prog = ("import sys,time\nsys.stdout.write('R\\n'); sys.stdout.flush()\n"
                "sys.stdout.write('z'*300000+'\\n'); sys.stdout.write('S\\n'); sys.stdout.flush()\n"
                "sys.stdin.read()\n")
        h = WatcherHarness(labels=("system",))
        h.w.tap_factory = lambda spec, ev: hw.ProcessTap(spec, ev, SudoReader(),
                                                         argv=["python3", "-I", "-c", prog])
        h.truth["v"] = 0
        with h:
            self.assertTrue(_wait(lambda: h.out["mode"] == "startup", 30))
            self.assertTrue(_wait(lambda: "signal" in h.calls, 30), h.calls)    # valid token acted on


class TapArgvTests(unittest.TestCase):
    def _sudo(self):
        with mock.patch.object(hc.pwd, "getpwuid") as g:
            g.return_value.pw_name = "claude-fallback"
            return SudoReader()

    def test_system_tap_runs_directly_without_sudo_or_shell(self):
        argv = hw.tap_argv(hw.TapSpec("system", "system", hc.HEALTH1_INTERFACE), self._sudo())
        self.assertEqual(argv, ["python3", "-I", "-c", hw._TAP_SRC, "system",
                                "org.pantheon.Health1", "/org/pantheon"])

    def test_session_tap_is_sudo_n_u_with_bus_env(self):
        spec = hw.TapSpec("session:claude", "session", hc.PC_INTERFACE, account="claude")
        with mock.patch.object(hw.pwd, "getpwnam") as g:
            g.return_value.pw_uid = 1234
            argv = hw.tap_argv(spec, self._sudo())
        self.assertEqual(argv[:9], ["sudo", "-n", "-u", "claude", "env",
                                    "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1234/bus",
                                    "XDG_RUNTIME_DIR=/run/user/1234", "python3", "-I"])
        self.assertEqual(argv[-3:], ["session", "org.pantheon.PersonaController1", "/org/pantheon"])

    def test_unknown_account_has_no_argv(self):
        spec = hw.TapSpec("session:x", "session", hc.PC_INTERFACE, account="no-such-acct-xyz")
        self.assertEqual(hw.tap_argv(spec, self._sudo()), [])

    def test_build_specs_one_tap_per_account_and_bad_idents_skipped(self):
        specs = hw.build_specs([("a", "acc", None), ("b", "acc", None), ("c", "../x", None),
                                ("d", "other", None)])
        self.assertEqual([s.label for s in specs], ["system", "session:acc", "session:other"])

    def test_tap_source_never_prints_payload(self):
        # Tap output vocabulary is exactly R and S; nothing from a signal body is written.
        writes = [l for l in hw._TAP_SRC.splitlines() if "stdout.write" in l]
        self.assertTrue(writes and all(('"S\\n"' in w) or ('"R\\n"' in w) for w in writes), writes)
        self.assertNotIn("print(", hw._TAP_SRC)

    def test_unusable_tap_exits_as_refused_event(self):
        ev = queue.Queue()
        spec = hw.TapSpec("session:x", "session", hc.PC_INTERFACE, account="no-such-acct-xyz")
        tap = hw.ProcessTap(spec, ev, SudoReader())
        self.assertEqual(ev.get(timeout=5), ("session:x", "exit"))
        tap.stop()


class ModeAndLockTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.dir = Path(self._t.name)
        self.out = self.dir / "fleet-health.json"
        self.reader = mock.Mock()
        self.reader.getall_system.return_value = ("unreachable", None)
        self.reader.getall_session.return_value = ("unreachable", None)

    def tearDown(self):
        self._t.cleanup()

    def test_mode_enum_roundtrip_and_sanitized(self):
        for mode in ("poll", "signal", "startup"):
            doc = hc.collect(PERSONAS, reader=self.reader, now=1.0, mode=mode)
            self.assertEqual(doc["collector"]["mode"], mode)
        self.assertEqual(hc.sanitize_doc({"collector": {"mode": "evil\x00"}, "entries": []})
                         ["collector"]["mode"], "poll")
        self.assertEqual(hc.collect(PERSONAS, reader=self.reader, now=1.0)["collector"]["mode"], "poll")
        self.assertEqual(hc.sanitize_doc({"entries": []})["schema_version"], 1)

    def test_run_writes_requested_mode(self):
        self.assertEqual(hc.run(PERSONAS, reader=self.reader, out=self.out, mode="signal"), 0)
        self.assertEqual(json.loads(self.out.read_text())["collector"]["mode"], "signal")

    def test_collected_at_is_stamped_by_collect_not_by_trigger(self):
        hc.run(PERSONAS, reader=self.reader, out=self.out, now=1_800_000_000.0, mode="signal")
        self.assertEqual(json.loads(self.out.read_text())["collected_at"], 1_800_000_000)

    def test_lock_held_returns_busy_and_writes_nothing_then_releases(self):
        fd = os.open(self.dir, os.O_RDONLY | os.O_DIRECTORY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            rc = hc.run(PERSONAS, reader=self.reader, out=self.out, lock_wait_s=0.0)
            self.assertEqual(rc, hc.RC_BUSY)
            self.assertFalse(self.out.exists())
            self.reader.getall_system.assert_not_called()      # collected inside the lock only
        finally:
            os.close(fd)
        self.assertEqual(hc.run(PERSONAS, reader=self.reader, out=self.out, lock_wait_s=0.0), 0)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["fleet-health.json"])

    def test_oneshot_main_maps_busy_to_zero(self):
        with mock.patch.object(hc, "run", return_value=hc.RC_BUSY), \
                mock.patch.object(hc, "fleet_personas", return_value=PERSONAS):
            self.assertEqual(hc.main(["--out", str(self.out)]), 0)


class StdinWatchTests(unittest.TestCase):
    """T-5427: the tap's parent-gone watch reads stdin to EOF. It must discard as it
    reads: on 10-23 a probe that fed the tap </dev/zero grew it to 5.4 GB and the
    root OOM killer took it (and put the seat on hold)."""

    def _run(self, stdin, seconds):
        import resource
        def limit():
            resource.setrlimit(resource.RLIMIT_AS, (256 << 20, 256 << 20))
        src = ("import os, sys, threading, time\n" + hw._STDIN_WATCH_SRC
               + "t = threading.Thread(target=watch_stdin, daemon=True); t.start()\n"
               # a watch thread that died (e.g. MemoryError) must fail the test, not hide
               + "while t.is_alive(): time.sleep(0.2)\n"
               + "os._exit(3)\n")
        p = subprocess.Popen([sys.executable, "-I", "-c", src], stdin=stdin,
                             stderr=subprocess.PIPE, preexec_fn=limit)
        try:
            return p.wait(timeout=seconds), p
        except subprocess.TimeoutExpired:
            return None, p
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stderr.close()

    def test_endless_stdin_is_discarded_in_bounded_memory(self):
        with open(os.devnull.replace("null", "zero"), "rb") as zero:
            rc, _ = self._run(zero, 3)
        self.assertIsNone(rc, "stdin watch died on an endless stream (unbounded read?)")

    def test_eof_exits_zero(self):
        rc, _ = self._run(subprocess.DEVNULL, 10)
        self.assertEqual(rc, 0)

    def test_tap_source_embeds_the_bounded_watch(self):
        self.assertIn(hw._STDIN_WATCH_SRC, hw._TAP_SRC)
        self.assertNotIn("stdin.buffer.read()", hw._TAP_SRC)


class UnitFileAndHygieneTests(unittest.TestCase):
    SVC = REPO / "tools/systemd/pantheon-health-watcher.service"

    def test_watcher_unit(self):
        t = self.SVC.read_text()
        self.assertIn("Type=simple", t)
        self.assertIn("tools/health_watcher.py", t)
        self.assertIn("Restart=on-failure", t)
        self.assertIn("OnFailure=pantheon-unit-failure-alert@%n.service", t)
        # T-5409 install (2026-10-25): long-running, so it must be enabled to
        # return with the user manager after a reboot.
        self.assertIn("[Install]", t)
        self.assertIn("WantedBy=default.target", t)
        self.assertIn("MemoryMax=", t)  # T-5427: defense in depth

    def test_collector_units_unchanged_in_shape(self):
        t = (REPO / "tools/systemd/pantheon-health-collector.service").read_text()
        self.assertIn("Type=oneshot", t)

    def test_no_trigger_byte_sequence(self):
        trigger = "HERME" + "S.md"
        for p in (REPO / "tools/health_watcher.py", REPO / "tools/health_collector.py",
                  Path(__file__), self.SVC):
            self.assertNotIn(trigger, p.read_text(), p.name)


# ============================================================== scratch-bus layer
_BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*"/>
    <allow own="*"/>
    <allow receive_sender="*"/>
  </policy>
</busconfig>
"""


@unittest.skipUnless(HAVE_DBUS, "dbus-python / gi / dbus-daemon unavailable (typed skip)")
class ScratchBusTests(unittest.TestCase):
    """Real emitters on two private buses ("system" and "session" roles), real taps, real busctl."""

    @classmethod
    def setUpClass(cls):
        from tools.host_control import health as H
        from tools.host_control import health_dbus as HD
        from tools.host_control import wire as broker_wire
        from tools import persona_controller_health_dbus as PD
        cls.H, cls.HD, cls.PD, cls.broker_wire = H, HD, PD, broker_wire
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        dbus.mainloop.glib.threads_init()
        cls.sys_bus = cls.ses_bus = None
        cls.loop = None
        try:
            cls.sys_bus = testbus.spawn_daemon("t5409s-", _BUS_CONFIG)
            cls.ses_bus = testbus.spawn_daemon("t5409p-", _BUS_CONFIG)
            cls.loop = GLib.MainLoop()
            cls.loop_thread = threading.Thread(target=cls.loop.run, daemon=True)
            cls.loop_thread.start()
            cls._export_emitters()
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        if cls.loop is not None:
            cls.loop.quit()
        for h in (cls.ses_bus, cls.sys_bus):
            if h is not None:
                testbus.stop_daemon(h)

    # ---- emitters (all bus work is marshalled onto the GLib thread)
    @classmethod
    def on_glib(cls, fn, timeout=10.0):
        done, box = threading.Event(), {}

        def _run():
            try:
                box["r"] = fn()
            except BaseException as exc:  # noqa: BLE001
                box["e"] = exc
            done.set()
            return False
        GLib.idle_add(_run)
        assert done.wait(timeout), "glib marshal timed out"
        if "e" in box:
            raise box["e"]
        return box.get("r")

    @classmethod
    def _export_emitters(cls):
        H, HD, PD = cls.H, cls.HD, cls.PD
        cls.hstate = {n: "ready" for n in H.BROKER_ROSTER}
        cls.pstate = {"status": "ready"}

        def probe(name):
            def _p():
                st = cls.hstate[name]
                return H.HealthProbeResult(status=st, reason="" if st == "ready" else "x", revision="r1")
            return _p

        def build():
            sconn = dbus.bus.BusConnection(cls.sys_bus.bus_address)
            cls._keep = [sconn]
            provider = H.HealthProvider(service="broker", probes={n: probe(n) for n in H.BROKER_ROSTER})
            cls.host_obj = HD.Health1Object(
                sconn, object_path=HD.broker_health_path(), provider=provider,
                monitor_uids=frozenset({os.getuid()}))
            cls.host_name = dbus.service.BusName(cls.broker_wire.BUS_NAME, sconn)
            cls.host_provider = provider
            pconn = dbus.bus.BusConnection(cls.ses_bus.bus_address)
            cls._keep.append(pconn)

            def payload():
                return {
                    "ActiveRuntime": "claude/x", "ViabilityStatus": "viable", "LastSelectionTs": "",
                    "LastFailureClass": "", "NextCooldownExpiryTs": "", "Holds": [],
                    "ModalPresent": False, "ModalKind": "", "ModalResetAt": "",
                    "ContractRevision": "pantheon-health/v1", "Status": cls.pstate["status"],
                    "Subcomponents": {"viability": "ready"},
                    "Reasons": {} if cls.pstate["status"] == "ready" else {"viability": "x"},
                    "Revisions": {"viability": "r1"}, "InvariantListRef": "inv@sha256:abc",
                    "SecurityDrift": False}
            handle = dbus.service.BusName(PD.bus_name_for("scratchp"), pconn)
            cls.pc_obj = PD.PersonaController1Object(
                pconn, object_path=PD.object_path_for("scratchp"), provider=payload,
                bus_name_handle=handle, health_leg=True)
        cls.on_glib(build)

    @classmethod
    def set_state(cls, *, host=None, persona=None, emit=True):
        def _go():
            if host is not None:
                for n in cls.hstate:
                    cls.hstate[n] = host
            if persona is not None:
                cls.pstate["status"] = persona
            if emit:
                if host is not None:
                    cls.host_obj.emit_properties_changed(cls.host_provider.snapshot())
                if persona is not None:
                    cls.pc_obj.emit_properties_changed()
        cls.on_glib(_go)

    # ---- plumbing
    def setUp(self):
        self.set_state(host="ready", persona="ready", emit=False)
        self._d = tempfile.mkdtemp(prefix="t5409out-")
        self.addCleanup(shutil.rmtree, self._d, True)
        self.out = Path(self._d) / "fleet-health.json"
        self.reader = hc.CollectorReader(system_address=self.sys_bus.bus_address,
                                         session_addresses={ME: self.ses_bus.bus_address})
        self.watchers = []
        self.addCleanup(self._stop_all)

    def _stop_all(self):
        for w, t in self.watchers:
            w.stop_event.set()
            t.join(timeout=20)
            w._shutdown()                                 # idempotent; kills taps even on failure

    def specs(self):
        return hw.build_specs(PERSONAS, system_address=self.sys_bus.bus_address,
                              session_addresses={ME: self.ses_bus.bus_address})

    def start_watcher(self, reader=None):
        rd = reader or self.reader
        sudo = SudoReader()
        w = hw.Watcher(
            self.specs(),
            collect_fn=lambda mode: hc.run(PERSONAS, reader=rd, out=self.out, mode=mode, lock_wait_s=0.0),
            tap_factory=lambda spec, ev: hw.ProcessTap(spec, ev, sudo),
            min_interval=0.2, ready_timeout=30.0, idle_tick=0.1, backoff_start=0.2, backoff_max=0.8,
            log=lambda m: None)
        t = threading.Thread(target=w.run, daemon=True)
        t.start()
        self.watchers.append((w, t))
        return w

    def doc(self):
        try:
            return json.loads(self.out.read_text())
        except (OSError, ValueError):
            return None

    def status(self, cid):
        d = self.doc()
        if not d:
            return None
        return {e["component_id"]: e["current_status"] for e in d["entries"]}.get(cid)

    def wait_status(self, cid, want, timeout=60.0):
        ok = _wait(lambda: self.status(cid) == want, timeout)
        self.assertTrue(ok, f"{cid} never converged to {want}; last={self.status(cid)}")

    # ---- RFC-004 SS6 test 10
    def test10_initial_get_then_subscription_reconciles(self):
        self.set_state(host="degraded", persona="degraded", emit=False)
        w = self.start_watcher()
        self.wait_status("persona-controller.scratchp", "degraded")     # initial Get
        self.wait_status("host-broker", "degraded")
        self.assertTrue(_wait(lambda: len(w.ready) == 2, 30))
        time.sleep(1.0)                                                 # parked: idle
        self.set_state(persona="ready")                                 # signal -> converge (G1)
        self.wait_status("persona-controller.scratchp", "ready")
        self.assertEqual(self.doc()["collector"]["mode"], "signal")
        self.set_state(host="ready")                                    # system-bus leg
        self.wait_status("host-broker", "ready")

    def test10_change_mid_get_is_not_missed(self):
        outer = self

        class FlipMidGet(hc.CollectorReader):
            flipped = False

            def getall_session(self, account, persona, interface):
                res = super().getall_session(account, persona, interface)   # stale read
                if not FlipMidGet.flipped:
                    FlipMidGet.flipped = True
                    outer.set_state(persona="degraded")     # change + signal while collect is running
                    time.sleep(1.0)                         # let the signal reach the watcher queue
                return res

        rd = FlipMidGet(system_address=self.sys_bus.bus_address,
                        session_addresses={ME: self.ses_bus.bus_address})
        self.start_watcher(reader=rd)
        self.wait_status("persona-controller.scratchp", "degraded")
        self.assertTrue(FlipMidGet.flipped)

    # ---- RFC-004 SS6 test 12
    def test12_signal_missed_while_watcher_down_is_recovered_on_up(self):
        self.set_state(persona="degraded", host="degraded")              # nobody is listening
        self.assertIsNone(self.doc())
        self.start_watcher()
        self.wait_status("persona-controller.scratchp", "degraded")
        self.wait_status("host-broker", "degraded")

    def test12_tap_killed_mid_run_gap_is_recovered_on_respawn(self):
        w = self.start_watcher()
        self.wait_status("persona-controller.scratchp", "ready")
        self.assertTrue(_wait(lambda: len(w.ready) == 2, 30))
        victim = w.taps["session:" + ME]
        victim.proc.kill()                                               # tap dies; signal is lost
        self.set_state(persona="degraded")
        self.wait_status("persona-controller.scratchp", "degraded")
        self.assertGreaterEqual(w.refused.get("session:" + ME, 0), 1)

    # ---- RFC-004 SS6 test 11
    def test11_restart_regets_while_emitters_keep_publishing(self):
        w1 = self.start_watcher()
        self.wait_status("persona-controller.scratchp", "ready")
        self.assertTrue(_wait(lambda: len(w1.ready) == 2, 30))
        # ---- watcher DOWN
        w1.stop_event.set()
        self.watchers[0][1].join(timeout=30)
        w1._shutdown()
        self.set_state(persona="degraded")                      # emitter publishes with nobody listening
        # poison the old output: a restarted instance must not read anything back from it
        self.out.write_text(json.dumps({"collected_at": 1, "entries": [
            {"component_id": "ghost", "current_status": "ready"},
            {"component_id": "persona-controller.scratchp", "current_status": "ready"}]}))
        # ---- NEW instance; before ANY further signal its startup Get must match current truth
        w2 = self.start_watcher()
        self.wait_status("persona-controller.scratchp", "degraded", timeout=60)
        d = self.doc()
        self.assertNotIn("ghost", [e["component_id"] for e in d["entries"]])
        self.assertGreater(d["collected_at"], 1_000_000_000)
        # ---- emitters keep publishing right after the new watcher is up
        self.assertTrue(_wait(lambda: len(w2.ready) == 2, 30))
        self.set_state(host="degraded")
        self.set_state(persona="ready")
        self.wait_status("host-broker", "degraded")
        self.wait_status("persona-controller.scratchp", "ready")

    def test_taps_do_not_outlive_watcher(self):
        w = self.start_watcher()
        self.assertTrue(_wait(lambda: len(w.ready) == 2, 30))
        procs = [t.proc for t in w.taps.values()]
        w.stop_event.set()
        for p in procs:
            self.assertIsNotNone(p)
            self.assertTrue(_wait(lambda: p.poll() is not None, 15), "tap leaked")


if __name__ == "__main__":
    unittest.main()
