"""Shared private-dbus-daemon spawn/teardown/sweep plumbing for the
host_control test harness (T-2932).

Why this exists: ``PrivateBusCase`` (tools/test_host_control_dbus.py),
``ApprovalPrivateBusCase`` (tools/test_host_control_approval_dbus.py) and
``HealthBusHarness`` (tools/test_host_control_health_dbus.py) each spawn a
real ``dbus-daemon --nofork`` subprocess. Teardown is correct on the happy
path, but a killed test process (session restart, background-Bash timeout,
manual kill) SIGHUPs the process group; ``dbus-daemon`` treats SIGHUP as a
partial config reload rather than exit (man dbus-daemon), so the daemon
survives its parent, gets reparented to ``systemd --user``, and its bus_dir
leaks on disk. Census 2026-08-25: 169 orphaned daemons / 734 MB RSS over 5
days on this VPS.

Two layers close this:

1. PDEATHSIG (``pdeathsig_preexec_fn`` / ``spawn_daemon``) -- the kernel
   kills the daemon (and, where applied, the driver) the instant the
   spawning process's main thread dies, for ANY reason. This closes the
   killed-session case structurally, with no sweep and no timer.
2. A pidfile registry + self-healing ``sweep()`` (belt-and-braces) --
   catches the residual: PDEATHSIG not delivered (non-Linux, or a future
   call site that violates the per-thread caveat below), pre-fix orphans,
   other seats. ``sweep()`` runs at the START of every ``spawn_daemon()``
   call, so a killed run cleans up on the NEXT run -- no timer, no systemd
   unit, no explicit invocation required by callers.

Module naming: this lives in ``tools/`` (leading underscore, matching the
``tools/_test_fixtures.py`` precedent for shared non-discovered test
support) rather than inside ``tools/host_control/`` -- that package is the
shipped ORG-052 Action Broker service; test-only scaffolding does not
belong inside a production package boundary.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

DBUS_DAEMON = shutil.which("dbus-daemon")

# /usr/include/linux/prctl.h -- verified at source on this host (T-2932 plan).
_PR_SET_PDEATHSIG = 1

# Root of the no-XDG fallback pidfile dir (``<root>/pantheon-testbus-<uid>``).
# A module constant rather than an inline literal so the helper's own tests
# can point it at a per-test root and stay hermetic under parallel runs.
_FALLBACK_BASE_ROOT = Path("/tmp")


def pdeathsig_preexec_fn(sig: int = signal.SIGTERM) -> Callable[[], None]:
    """Return a ``preexec_fn`` for ``subprocess.Popen`` that asks the
    kernel to deliver ``sig`` to the child when the CALLING THREAD of this
    process dies, for any reason -- crash, SIGKILL, or a SIGHUP that kills
    a tmux pane/session group. Verified empirically (T-2932 plan): a
    throwaway parent using this preexec_fn, SIGKILLed, has its child reaped
    by the kernel in well under 2 seconds with no cooperation from the
    child or any other process.

    CAVEAT (prctl(2)): PR_SET_PDEATHSIG is a property of the CALLING
    THREAD, not the process -- the signal fires when THAT thread exits,
    not merely when some other thread in the process does. Every call site
    in this suite invokes ``spawn_daemon`` from setUpClass/__enter__, i.e.
    the interpreter's main thread, which lives for the process's lifetime
    -- satisfying the constraint. ``spawn_daemon`` asserts this at call
    time so a future thread-based call site fails loud instead of silently
    losing PDEATHSIG coverage (sweep() remains the backstop either way).

    Linux-only. On any other platform this returns a no-op; sweep() is the
    only backstop there.
    """

    if sys.platform != "linux":
        return lambda: None

    def _preexec() -> None:
        try:
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.prctl(_PR_SET_PDEATHSIG, sig)
        except OSError:
            pass  # best-effort; sweep() is the backstop

    return _preexec


def _pidfile_base_dir() -> Path:
    """Prefer ``$XDG_RUNTIME_DIR/pantheon-testbus`` (tmpfs, uid-owned by
    the login session) when the env var is set and the base dir is
    genuinely owned by us; otherwise fall back to
    ``/tmp/pantheon-testbus-<uid>``."""

    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        base = Path(xdg)
        try:
            owned = base.is_dir() and base.stat().st_uid == os.getuid()
        except OSError:
            owned = False
        if owned:
            return base / "pantheon-testbus"
    return _FALLBACK_BASE_ROOT / f"pantheon-testbus-{os.getuid()}"


class PidfileDirRefused(OSError):
    """Raised when the testbus pidfile dir fails the ownership check
    (symlink / not-a-directory / wrong uid). Subclasses OSError
    deliberately: spawn_daemon()'s callers (setUpClass/__enter__) should
    see this propagate loud and uncaught -- a squatted or hostile dir at
    spawn time is a real problem worth failing on. sweep()'s existing
    ``except OSError`` catches this same type, so the identical failure
    at sweep time is instead skipped-with-one-stderr-line -- sweep must
    never raise into a caller that isn't asking for a new daemon, only
    trying to clean up leftovers from old ones."""


def _verify_owned_dir(path: Path) -> None:
    """Raise PidfileDirRefused unless ``path`` is a plain directory (not
    a symlink) owned by our own uid. ``/tmp`` is sticky + world-writable,
    so a pre-existing entry planted by a different uid must never be
    trusted -- refuse loud rather than use it."""

    st = path.lstat()
    if stat.S_ISLNK(st.st_mode):
        raise PidfileDirRefused(f"testbus pidfile dir {path} is a symlink -- refusing to use it")
    if not stat.S_ISDIR(st.st_mode):
        raise PidfileDirRefused(f"testbus pidfile dir {path} is not a directory -- refusing to use it")
    if st.st_uid != os.getuid():
        raise PidfileDirRefused(
            f"testbus pidfile dir {path} is owned by uid {st.st_uid}, not "
            f"{os.getuid()} -- refusing to use a dir we don't own "
            "(shared /tmp is sticky + world-writable)"
        )


def _pidfile_dir_for_spawn() -> Path:
    base = _pidfile_base_dir()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    _verify_owned_dir(base)
    return base


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def _start_ticks(pid: int) -> int:
    """Field 22 (starttime, clock ticks since boot) of ``/proc/<pid>/stat``.

    ``comm`` (field 2) is parenthesized and may itself contain ')', so we
    split on the LAST ')' (per proc(5)) and count fields from there: state
    is field 3 (index 0 in the post-split list), so starttime (field 22)
    is index 19. Verified against ``/usr/include/linux/prctl.h``-adjacent
    proc(5) field numbering plus an empirical read of ``/proc/self/stat``
    on this host (T-2932 plan)."""

    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    after_comm = raw.rsplit(")", 1)[1].split()
    return int(after_comm[19])


def _short_tmpdir(prefix: str) -> str:
    """A directory whose socket path stays under the AF_UNIX path limit."""

    candidate = tempfile.mkdtemp(prefix=prefix)
    if len(candidate) + len("/bus.sock") < 100:
        return candidate
    os.rmdir(candidate)
    return tempfile.mkdtemp(prefix=prefix, dir="/tmp")


@dataclass
class DaemonHandle:
    process: subprocess.Popen
    log: TextIO
    bus_dir: str
    bus_address: str
    socket_path: str
    pidfile: Path

    def register_driver(self, pid: int | None) -> None:
        """Append a driver pid to this daemon's pidfile (best-effort
        defense-in-depth -- the census found no orphaned drivers, since
        they reliably die on SIGHUP; the daemon is the actual leak
        vector). Safe to call more than once per pidfile (e.g. a
        restarted driver); stale entries from prior restarts are harmless
        -- sweep()'s cmdline leaf-check skips any pid that no longer looks
        like a driver."""

        if pid is None:
            return
        try:
            data = json.loads(self.pidfile.read_text(encoding="utf-8"))
        except OSError:
            return
        driver_pids = data.setdefault("driver_pids", [])
        if pid not in driver_pids:
            driver_pids.append(pid)
        _atomic_write_json(self.pidfile, data)


def spawn_daemon(
    prefix: str,
    bus_config_template: str,
    *,
    timeout: float = 20.0,
) -> DaemonHandle:
    """Spawn a private dbus-daemon with PDEATHSIG-on-parent-death and a
    pidfile registered for the sweep backstop. Runs ``sweep()`` first
    (self-healing on the next run). Caller still owns waiting for the
    service subprocess to own its bus name and any higher-level protocol;
    this only owns the daemon + the socket-listening wait (identical
    20s-deadline / AssertionError text to the three pre-refactor copies)."""

    assert threading.current_thread() is threading.main_thread(), (
        "spawn_daemon must be called from the process's main thread -- "
        "PR_SET_PDEATHSIG is a per-thread property of the CALLING thread, "
        "so calling from a helper thread that exits early would silently "
        "defeat it for this daemon"
    )

    sweep()
    # Validate the pidfile dir BEFORE anything else is created -- a
    # refused dir (symlink / wrong uid / not-a-dir) must raise with
    # nothing yet to clean up, rather than after a real daemon is
    # already running untracked (T-2932 round 2b).
    pidfile_dir = _pidfile_dir_for_spawn()

    bus_dir = None
    log = None
    process = None
    try:
        bus_dir = _short_tmpdir(prefix)
        socket_path = os.path.join(bus_dir, "bus.sock")
        bus_address = f"unix:path={socket_path}"
        bus_config = os.path.join(bus_dir, "bus.conf")
        with open(bus_config, "w", encoding="utf-8") as handle:
            handle.write(bus_config_template.format(socket=socket_path))
        log = open(  # noqa: SIM115 - closed by stop_daemon, or by the except clause below on failure
            os.path.join(bus_dir, "daemon.log"), "w", encoding="utf-8"
        )
        process = subprocess.Popen(
            [DBUS_DAEMON, f"--config-file={bus_config}", "--nofork"],
            stdout=log,
            stderr=log,
            preexec_fn=pdeathsig_preexec_fn(),
        )
        deadline = time.monotonic() + timeout
        while not os.path.exists(socket_path):
            if time.monotonic() > deadline:
                raise AssertionError("private dbus-daemon never listened")
            if process.poll() is not None:
                raise AssertionError("private dbus-daemon exited early")
            time.sleep(0.05)

        pidfile = pidfile_dir / f"{process.pid}.json"
        _atomic_write_json(
            pidfile,
            {
                "daemon_pid": process.pid,
                "driver_pids": [],
                "bus_dir": bus_dir,
                "parent_pid": os.getpid(),
                "parent_start_ticks": _start_ticks(os.getpid()),
            },
        )
    except BaseException:
        # Belt-and-braces: whatever failed, leave nothing running and
        # nothing on disk -- this is the same teardown stop_daemon()
        # does on the happy path, inlined here because there is no
        # DaemonHandle yet to hand back to a caller for cleanup.
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        if log is not None:
            log.close()
        if bus_dir:
            shutil.rmtree(bus_dir, ignore_errors=True)
        raise

    return DaemonHandle(
        process=process,
        log=log,
        bus_dir=bus_dir,
        bus_address=bus_address,
        socket_path=socket_path,
        pidfile=pidfile,
    )


def stop_daemon(handle: DaemonHandle) -> None:
    """terminate/wait(10)/kill teardown (identical dance to the three
    pre-refactor copies), close the log, rmtree bus_dir, remove the
    pidfile. Deliberately does NOT call sweep() -- sweep runs at spawn
    time only, per design."""

    process = handle.process
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    if handle.log is not None:
        handle.log.close()
    if handle.bus_dir:
        shutil.rmtree(handle.bus_dir, ignore_errors=True)
    try:
        handle.pidfile.unlink()
    except OSError:
        pass


def _log_sweep_error(context: object, exc: BaseException) -> None:
    print(f"testbus sweep: {context}: {type(exc).__name__}: {exc}", file=sys.stderr)


def _parent_still_owns(parent_pid: int, recorded_start_ticks: int) -> bool:
    try:
        return _start_ticks(parent_pid) == recorded_start_ticks
    except (OSError, ValueError, IndexError):
        return False


def _maybe_kill(pid: object, cmdline_needle: bytes) -> None:
    if not pid:
        return
    try:
        cmdline = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except (OSError, ValueError):
        return  # already gone, or not a usable pid -- nothing to do
    if cmdline_needle not in cmdline:
        return  # pid reused by an unrelated process -- never touch it
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def _sweep_one(pidfile: Path) -> None:
    data = json.loads(pidfile.read_text(encoding="utf-8"))
    parent_pid = data.get("parent_pid")
    recorded_start = data.get("parent_start_ticks")
    if parent_pid is None or recorded_start is None:
        return
    if _parent_still_owns(parent_pid, recorded_start):
        return  # live, matching parent -- leave it alone

    _maybe_kill(data.get("daemon_pid"), b"dbus-daemon")
    for driver_pid in data.get("driver_pids") or []:
        _maybe_kill(driver_pid, b"tools.test_host_control")

    bus_dir = data.get("bus_dir")
    if bus_dir:
        shutil.rmtree(bus_dir, ignore_errors=True)
    pidfile.unlink()


def sweep() -> None:
    """Self-healing sweep: reap any registered daemon (+ drivers) whose
    recorded parent is gone or pid-reused. Tolerant of races -- any
    exception encountered per-pidfile is logged to stderr (one line) and
    swallowed; sweep() must never raise into the calling test. Tolerates
    the pidfile dir being entirely absent (nothing to do)."""

    base = _pidfile_base_dir()
    if not base.exists():
        return
    try:
        _verify_owned_dir(base)
    except OSError as exc:
        _log_sweep_error(base, exc)
        return
    try:
        candidates = sorted(base.glob("*.json"))
    except OSError as exc:
        _log_sweep_error(base, exc)
        return
    for pidfile in candidates:
        try:
            _sweep_one(pidfile)
        except Exception as exc:  # noqa: BLE001 -- must never raise into the test
            _log_sweep_error(pidfile, exc)
