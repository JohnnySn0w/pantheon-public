#!/usr/bin/env python3
"""health_watcher — the SUBSCRIBING leg of the fleet health collector (T-5409, ORG-080 SS5.5).

WHY. tools/health_collector.py polls every emitter once a minute. ORG-080 SS5.5 also wants the
collector to react to `PropertiesChanged` from PersonaController1 and Health1. This long-running
process (Type=simple, see tools/systemd/pantheon-health-watcher.service) holds one signal
"tap" per bus and, on a signal, calls health_collector.run() for a FRESH FULL collect.

CONTRACTS
  * A signal is ONLY a trigger. The host Health1 emitter may send a diff
    (host_control.health.serialized_changed_properties), so no payload is ever read: the tap
    prints a bare token per signal and nothing else. Truth always comes from a full re-Get.
  * Stateless (ORG-068 E10.C3): the watcher keeps no health state, reads nothing back. Its only
    memory is a dirty flag and a timestamp for the debounce. Restart-fixable (E10.C2): the
    startup path is subscribe -> confirm match active -> initial full collect, so a change that
    happened while the watcher was down (RFC-004 SS6.12) or mid-Get (SS6.10) is recovered.
  * Subscribe BEFORE the initial Get; any signal landing during a collect sets `dirty` and
    causes exactly one follow-up collect (leading + trailing debounce, MIN_INTERVAL floor).
  * Fail closed: `collected_at` is stamped only by a completed collect. A dead or wedged
    watcher cannot make the aggregate look fresher; the timer oneshot is the unchanged
    backstop. A tap that exits (or is refused by sudo) is counted and logged as a class only,
    retried with capped backoff, and every tap (re)start triggers a full collect (a signal may
    have been lost in the gap). An account whose tap is refused is simply left to the tick.
  * The tap: `python3 -I -c <TAP_SRC>` (plain AddMatch, no BecomeMonitor privilege). For a
    session bus it is run as `sudo -n -u <acct> env DBUS_SESSION_BUS_ADDRESS=... python3 ...`
    (SudoReader._argv, argv arrays only). It exits when its stdin closes, so a killed watcher
    leaves no orphan taps.
"""
from __future__ import annotations

import argparse
import os
import pwd
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import health_collector as hc  # noqa: E402
from tools.health_export import SudoReader, _IDENT_RE, fleet_personas  # noqa: E402

MIN_INTERVAL_S = 2.0          # debounce floor between collect STARTS
READY_TIMEOUT_S = 10.0        # bound on waiting for taps to confirm their match
IDLE_TICK_S = 1.0             # loop wake-up for respawns / stop checks
BACKOFF_START_S = 5.0
BACKOFF_MAX_S = 300.0
MAX_TOKEN_LINE = 64           # tap stdout is only ever `R\n` / `S\n`; anything longer is noise

_PC_NAMESPACE = "/org/pantheon"
# T-5427: the parent-gone watch reads stdin to EOF and DISCARDS as it reads. A single
# read() buffers everything until EOF; a probe that fed a tap </dev/zero grew it to 5.4 GB
# (10-23, root OOM kill and seat hold). Kept standalone so a test can run it alone.
_STDIN_WATCH_SRC = r'''
def watch_stdin():
    while sys.stdin.buffer.read1(65536):   # discard; b"" is EOF == parent gone
        pass
    os._exit(0)
'''
_TAP_SRC = r'''
import os, sys, threading
import dbus, dbus.mainloop.glib
from gi.repository import GLib
dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
kind, iface, ns = sys.argv[1], sys.argv[2], sys.argv[3]
bus = dbus.SystemBus() if kind == "system" else dbus.SessionBus()
def on_msg(conn, msg):
    # Header-only check: the message BODY is never touched.
    if msg.get_member() == "PropertiesChanged":
        sys.stdout.write("S\n"); sys.stdout.flush()
    return None
bus.add_message_filter(on_msg)
# add_match_string is a synchronous AddMatch call: once it returns the daemon has installed
# the match, so announcing readiness below is truthful (subscribe BEFORE the initial Get).
bus.add_match_string("type='signal',interface='org.freedesktop.DBus.Properties',"
                     "member='PropertiesChanged',path_namespace='%s',arg0='%s'" % (ns, iface))
''' + _STDIN_WATCH_SRC + r'''
threading.Thread(target=watch_stdin, daemon=True).start()
sys.stdout.write("R\n"); sys.stdout.flush()
GLib.MainLoop().run()
'''


@dataclass(frozen=True)
class TapSpec:
    """One bus to subscribe on. `label` is a bounded class string, safe to log."""
    label: str
    kind: str                       # "system" | "session"
    interface: str
    account: str | None = None
    address: str | None = None      # explicit bus address (scratch tests); None = real default


def build_specs(personas, *, system_address: str | None = None,
                session_addresses: dict[str, str] | None = None) -> list[TapSpec]:
    specs = [TapSpec("system", "system", hc.HEALTH1_INTERFACE, address=system_address)]
    seen: set[str] = set()
    for _persona, account, _pane in personas:
        if not isinstance(account, str) or not _IDENT_RE.match(account) or account in seen:
            continue
        seen.add(account)          # one tap per account: one session bus carries all its personas
        specs.append(TapSpec(f"session:{account}", "session", hc.PC_INTERFACE, account=account,
                             address=(session_addresses or {}).get(account)))
    return specs


def tap_argv(spec: TapSpec, reader: SudoReader) -> list[str]:
    """The exact argv for a tap (pure; unit-tested). No shell, no payload channel."""
    py = ["python3", "-I", "-c", _TAP_SRC,
          spec.kind, spec.interface, _PC_NAMESPACE]
    env: dict[str, str] = {}
    if spec.kind == "system":
        if spec.address:
            env["DBUS_SYSTEM_BUS_ADDRESS"] = spec.address
        return reader._argv(None, py, env or None)
    if spec.address:
        env["DBUS_SESSION_BUS_ADDRESS"] = spec.address
    else:
        try:
            uid = pwd.getpwnam(spec.account or "").pw_uid
        except KeyError:
            uid = None
        if uid is None:
            return []
        env = {"DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
               "XDG_RUNTIME_DIR": f"/run/user/{uid}"}
    return reader._argv(spec.account, py, env)


class ProcessTap:
    """A real tap subprocess. A reader thread turns its stdout into queue events:
    (label, "ready") | (label, "signal") | (label, "exit")."""

    def __init__(self, spec: TapSpec, events: "queue.Queue", reader: SudoReader,
                 argv: list[str] | None = None):
        self.spec, self.events = spec, events
        argv = tap_argv(spec, reader) if argv is None else argv     # argv override: tests only
        self.proc = None
        if argv:
            try:
                self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, start_new_session=True)
            except (OSError, ValueError):
                self.proc = None
        self._pump_thread = threading.Thread(target=self._pump, daemon=True)
        self._pump_thread.start()

    def _pump(self):
        try:
            if self.proc is not None and self.proc.stdout is not None:
                self._read_tokens(self.proc.stdout)
        except (OSError, ValueError):
            pass                            # shutdown may close the pipe to unblock this reader
        finally:
            self.events.put((self.spec.label, "exit"))

    def _read_tokens(self, stream):
        """Bounded token reader: every read is at most MAX_TOKEN_LINE bytes, so a misbehaving
        tap cannot grow our memory. A line longer than the cap is discarded up to its newline
        and never acted on; the next valid token after it still counts."""
        skipping = False
        while True:
            chunk = stream.readline(MAX_TOKEN_LINE)
            if not chunk:
                return
            whole = chunk.endswith(b"\n")
            if skipping or (not whole and len(chunk) >= MAX_TOKEN_LINE):
                skipping = not whole           # over-long line: drop it, resume after its newline
                continue
            tok = chunk.strip()
            if tok == b"R":
                self.events.put((self.spec.label, "ready"))
            elif tok == b"S":
                self.events.put((self.spec.label, "signal"))

    def stop(self):
        p = self.proc
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()            # EOF is the tap's own exit cue
        except OSError:
            pass
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
                p.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self._pump_thread.join(timeout=3)   # a reaped child closes the writer, so EOF is prompt
        for stream in (p.stdin, p.stdout):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass


class Watcher:
    """Event loop. `collect_fn(mode) -> rc` does one full collect+write (hc.run);
    `tap_factory(spec, events) -> handle with .stop()` starts a tap."""

    def __init__(self, specs, *, collect_fn, tap_factory, min_interval: float = MIN_INTERVAL_S,
                 ready_timeout: float = READY_TIMEOUT_S, idle_tick: float = IDLE_TICK_S,
                 backoff_start: float = BACKOFF_START_S, backoff_max: float = BACKOFF_MAX_S,
                 log=None):
        self.specs = {s.label: s for s in specs}
        self.collect_fn, self.tap_factory = collect_fn, tap_factory
        self.min_interval, self.ready_timeout, self.idle_tick = min_interval, ready_timeout, idle_tick
        self.backoff_start, self.backoff_max = backoff_start, backoff_max
        self.log = log or (lambda m: print(f"health_watcher: {m}", file=sys.stderr, flush=True))
        self.events: queue.Queue = queue.Queue()
        self.taps: dict[str, object] = {}
        self.ready: set[str] = set()
        self.refused: dict[str, int] = {}
        self._next_try: dict[str, float] = {}
        self._backoff: dict[str, float] = {}
        self.collects = 0
        self.stop_event = threading.Event()

    # ---- taps
    def _start_tap(self, label: str):
        self.ready.discard(label)
        self.taps[label] = self.tap_factory(self.specs[label], self.events)

    def _on_exit(self, label: str, now: float):
        self.ready.discard(label)
        h = self.taps.pop(label, None)
        if h is not None:
            h.stop()
        self.refused[label] = self.refused.get(label, 0) + 1
        self.log(f"tap {label} exited (count={self.refused[label]}); left to the tick, retrying")
        b = min(self._backoff.get(label, self.backoff_start / 2) * 2, self.backoff_max)
        self._backoff[label] = b
        self._next_try[label] = now + b

    def _apply(self, ev, now: float) -> str | None:
        label, kind = ev
        if kind == "ready":
            self.ready.add(label)
            self._backoff.pop(label, None)
            return "gap"            # (re)subscribed: a signal may have been lost before this
        if kind == "signal":
            return "signal"
        if kind == "exit" and label in self.taps:
            self._on_exit(label, now)
            return "gap"
        return None

    def _drain(self, now: float) -> bool:
        hit = False
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return hit
            hit = (self._apply(ev, now) is not None) or hit

    # ---- main loop
    def run(self) -> int:
        for label in self.specs:
            self._start_tap(label)
        deadline = time.monotonic() + self.ready_timeout
        while len(self.ready) + len(self.refused) < len(self.specs) and time.monotonic() < deadline:
            try:
                self._apply(self.events.get(timeout=0.1), time.monotonic())
            except queue.Empty:
                pass
            if self.stop_event.is_set():
                return self._shutdown()
        # `dirty` is only a "collect is wanted" bit. The signals themselves live in self.events
        # (a queue), never in the flag: a signal that arrives mid-collect stays queued and is
        # applied after the collect returns, and a collect refused by a busy lock re-arms
        # `dirty` without consuming anything. So clearing the flag at the "wrong" moment alone
        # (e.g. after the collect) is equivalent: the queued signal re-sets it on the next pass.
        # The real bug shape is DISCARDING queued events after a collect (what the T-5409
        # falsification mutant did); _drain() runs only BEFORE a collect, to fold signals that
        # are already queued into that same collect.
        dirty, mode, last_start = True, "startup", float("-inf")
        while not self.stop_event.is_set():
            now = time.monotonic()
            for label, t in list(self._next_try.items()):      # capped-backoff respawns
                if now >= t and label not in self.taps:
                    del self._next_try[label]
                    self._start_tap(label)
            if dirty and now - last_start >= self.min_interval:
                self._drain(now)               # fold queued signals into THIS collect
                dirty = False
                last_start = now
                rc = self.collect_fn(mode)
                self.collects += 1
                if rc == hc.RC_BUSY:
                    dirty = True               # a peer held the lock; try again after the floor
                if rc != hc.RC_BUSY:
                    mode = "signal"            # a startup collect retried after RC_BUSY stays "startup"
                continue
            wait = self.idle_tick
            if dirty:
                wait = min(wait, max(0.0, last_start + self.min_interval - now))
            try:
                ev = self.events.get(timeout=wait)
            except queue.Empty:
                continue
            r = self._apply(ev, time.monotonic())
            if r is not None:
                dirty = True
                if mode != "startup":
                    mode = "signal"
        return self._shutdown()

    def _shutdown(self) -> int:
        for h in list(self.taps.values()):
            h.stop()
        self.taps.clear()
        return 0



def _collect_rc_note(rc: int) -> str | None:
    """The watcher's stderr note for a collector run's exit code (T-6041).

    health_collector.run exits 2 for three causes (an un-subscribable emitter
    declaration, an unwritable out dir, a failed write) and prints the real cause
    on stderr itself, so this note names the exit code and defers to that line
    rather than guessing one cause. Any other rc needs no note.
    """
    if rc == 2:
        return "health_watcher: health_collector exited 2 (cause on its stderr line above; tick will report)"
    return None

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--out", type=Path, default=hc.DEFAULT_OUT)
    ap.add_argument("--min-interval", type=float, default=MIN_INTERVAL_S)
    args = ap.parse_args(argv)
    personas = fleet_personas()
    reader = hc.CollectorReader()
    sudo = SudoReader()

    def collect_fn(mode: str) -> int:
        rc = hc.run(personas, reader=reader, out=args.out, mode=mode, lock_wait_s=0.0)
        note = _collect_rc_note(rc)
        if note:
            print(note, file=sys.stderr, flush=True)
        return rc

    w = Watcher(build_specs(personas), collect_fn=collect_fn,
                tap_factory=lambda spec, ev: ProcessTap(spec, ev, sudo), min_interval=args.min_interval)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: w.stop_event.set())
    return w.run()


if __name__ == "__main__":
    sys.exit(main())
