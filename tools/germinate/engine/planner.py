"""Planner invocation: the data-only request/response protocol (ORG-080 §2, C5).

The wire shape is the planner_sandbox launcher's plan-leg protocol
(``launcher.py`` ``_run_plan_leg``): a JSON request object in, a JSON list of
operation objects out.

``InProcessJsonPlannerRunner`` is the CI tier ONLY. It is NOT the C5 sandbox
(that is the launcher's confined transient service, whose wiring into the
engine is deferred to T-3296 / T-5106). It gives two in-process defenses:

1. data-only return: both directions are forced through JSON;
2. a scoped audit-hook guard (``PlannerGuard``) while the planner module is
   imported and ``plan_<leg>`` runs. It refuses, by raising inside the audited
   call and by failing the plan afterwards: opening a file for writing,
   filesystem mutation (mkdir/rename/remove/unlink/chmod/link/symlink/
   truncate and the shutil equivalents), process creation or replacement
   (subprocess, os.system, os.exec*, os.posix_spawn, os.spawn*, os.fork),
   signals (os.kill), network or IPC sockets (socket.connect/bind/sendto),
   starting threads (which would run outside the per-thread guard), and
   sqlite3 connections.

What the guard does NOT cover: reading files (a planner can read anything the
engine's uid can), CPU and memory, ctypes or native extensions that make
syscalls without an audit event, writes through file descriptors opened
before the guard, ``os.mkfifo`` (no audit event), a direct
``_posixsubprocess.fork_exec`` call (it emits no event of its own on 3.12;
only the ``subprocess.Popen`` wrapper does), and a planner switching the
guard off from Python by assigning ``PlannerGuard`` internals (it is plain
module state in the same interpreter). A planner
is reviewed repo code; the guard catches accidents and obvious escapes, the
sandbox (C5) is what confines.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import _thread
import threading
from pathlib import Path
from typing import Any, Mapping, Protocol

REQUEST_SCHEMA = "pantheon-plan-request/v0"

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_FS_MUTATION_EVENTS = frozenset(
    {
        "os.mkdir",
        "os.rename",
        "os.replace",
        "os.remove",
        "os.rmdir",
        "os.unlink",
        "os.chmod",
        "os.chown",
        "os.link",
        "os.symlink",
        "os.truncate",
        "os.utime",
        "shutil.copyfile",
        "shutil.copymode",
        "shutil.copystat",
        "shutil.copytree",
        "shutil.move",
        "shutil.rmtree",
        "shutil.chown",
    }
)
_PROCESS_EVENTS = frozenset(
    {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty", "os.kill", "os.killpg"}
)
_SOCKET_EVENTS = frozenset({"socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg"})
#: Threads would escape the per-thread guard; sqlite3 opens databases for
#: writing without an "open" event (verified on CPython 3.12.3: these are the
#: events emitted; threading.Thread.start emits only _thread.start_new_thread).
_ESCAPE_EVENTS = frozenset({"_thread.start_new_thread", "sqlite3.connect", "sqlite3.connect/handle"})


class PlanError(RuntimeError):
    """A planner could not produce typed operation data."""


class PlannerConfinementError(PermissionError):
    """Raised inside a refused audited call while the planner guard is active."""


class PlannerGuard:
    """Process-wide audit hook (installed once; audit hooks cannot be removed),
    gated by a scoped flag. Only the thread that activated it is policed."""

    _installed = False
    _lock = threading.Lock()
    _active_thread: int | None = None
    _violations: list[str] = []
    #: Whether this interpreter emits "_thread.start_new_thread" (3.12+; absent on
    #: 3.11). None until probed at install. When False, __enter__ wraps the
    #: low-level start functions for the active thread instead (T-5330).
    _thread_event_present: bool | None = None
    _saved_starts: tuple[Any, Any] | None = None

    @classmethod
    def _install(cls) -> None:
        with cls._lock:
            if not cls._installed:
                sys.addaudithook(cls._hook)
                cls._installed = True
                cls._thread_event_present = cls._detect_thread_event()

    @staticmethod
    def _detect_thread_event() -> bool:
        """Feature probe: does starting a thread raise the audit event? Fails
        closed: any probe failure reports the event as absent, so the wrapper is used."""
        seen: list[str] = []
        live = [True]

        def probe(event: str, args: tuple) -> None:
            if live[0] and event == "_thread.start_new_thread":
                seen.append(event)

        try:
            sys.addaudithook(probe)
            t = threading.Thread(target=lambda: None)
            t.start()
            t.join()
            return bool(seen)
        except BaseException:
            return False
        finally:
            live[0] = False

    @classmethod
    def _refusing_start(cls, which: int) -> Any:
        """Replacement for a low-level thread-start function: refuses from the
        active thread, delegates otherwise. Looks the original up at call time
        (no closure or __wrapped__ pointing at it)."""

        def start_new_thread(*args: Any, **kwargs: Any) -> Any:
            if cls._active_thread is not None and threading.get_ident() == cls._active_thread:
                cls._violations.append("_thread.start_new_thread")
                raise PlannerConfinementError("planner guard refused _thread.start_new_thread")
            saved = cls._saved_starts
            assert saved is not None
            return saved[which](*args, **kwargs)

        return start_new_thread

    @classmethod
    def _patch_thread_starts(cls) -> None:
        # Originals are captured once and kept for the life of the process, so a
        # wrapper that outlives the guard window (e.g. `from _thread import
        # start_new_thread` done meanwhile) still delegates correctly.
        if cls._saved_starts is None:
            cls._saved_starts = (threading._start_new_thread, _thread.start_new_thread)  # type: ignore[attr-defined]
        try:
            threading._start_new_thread = cls._refusing_start(0)  # type: ignore[attr-defined]
            _thread.start_new_thread = cls._refusing_start(1)
        except BaseException:
            cls._restore_thread_starts()
            raise

    @classmethod
    def _restore_thread_starts(cls) -> None:
        saved = cls._saved_starts
        if saved is not None:  # reassign the module attributes only; keep the saved originals
            threading._start_new_thread, _thread.start_new_thread = saved  # type: ignore[attr-defined]

    @classmethod
    def _hook(cls, event: str, args: tuple) -> None:
        if cls._active_thread is None or threading.get_ident() != cls._active_thread:
            return
        reason = cls._classify(event, args)
        if reason is not None:
            cls._violations.append(reason)
            raise PlannerConfinementError(f"planner guard refused {reason}")

    @staticmethod
    def _classify(event: str, args: tuple) -> str | None:
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            writing = (isinstance(flags, int) and flags & _WRITE_FLAGS) or (
                isinstance(mode, str) and any(c in mode for c in "wax+")
            )
            return f"open-for-write {path!r}" if writing else None
        if event in _FS_MUTATION_EVENTS:
            return f"{event} {args[0]!r}" if args else event
        if event in _PROCESS_EVENTS:
            return event
        if event in _SOCKET_EVENTS or event in _ESCAPE_EVENTS:
            return event
        return None

    def __enter__(self) -> "PlannerGuard":
        self._install()
        cls = type(self)
        with cls._lock:  # check, patch and activate atomically: no chained wrappers
            if cls._active_thread is not None:
                raise PlanError("planner guard is already active (planners do not nest)")
            if not cls._thread_event_present:
                cls._patch_thread_starts()  # rolls itself back on failure
            cls._violations = []
            cls._active_thread = threading.get_ident()
        self._saved_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True  # importing must not write __pycache__
        return self

    def __exit__(self, *exc: object) -> None:
        cls = type(self)
        with cls._lock:
            try:
                cls._restore_thread_starts()
            finally:
                cls._active_thread = None
        sys.dont_write_bytecode = self._saved_bytecode
        self.violations = list(cls._violations)
        cls._violations = []


class PlannerRunner(Protocol):
    def run(self, planner_path: Path, leg: str, request: Mapping[str, Any]) -> list[Any]: ...


def encode_request(request: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(request, sort_keys=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PlanError(f"plan request is not plain JSON data: {exc}") from None


def decode_response(data: bytes) -> list[Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"planner response is not JSON: {exc}") from None
    if not isinstance(value, list):
        raise PlanError("planner response must be a JSON list of operations")
    return value


class InProcessJsonPlannerRunner:
    """CI-tier runner: data-only by JSON round-trip plus the audit-hook guard. Not a sandbox."""

    def run(self, planner_path: Path, leg: str, request: Mapping[str, Any]) -> list[Any]:
        encoded = encode_request(request)
        planner_path = Path(planner_path)
        tag = hashlib.sha256(str(planner_path.resolve()).encode("utf-8")).hexdigest()[:16]
        spec = importlib.util.spec_from_file_location(f"_germinate_planner_{tag}", planner_path)
        if spec is None or spec.loader is None:
            raise PlanError(f"cannot load planner module {planner_path}")
        module = importlib.util.module_from_spec(spec)
        guard = PlannerGuard()
        failure: str | None = None
        result: Any = None
        with guard:
            try:
                spec.loader.exec_module(module)
                fn = getattr(module, f"plan_{leg}", None)
                if not callable(fn):
                    failure = f"planner module {planner_path.name} has no plan_{leg}"
                else:
                    result = fn(json.loads(encoded))
            except BaseException as exc:  # noqa: BLE001 -- any planner failure is a typed plan refusal
                failure = f"plan_{leg} in {planner_path.name} raised {type(exc).__name__}: {exc}"
        if guard.violations:
            raise PlanError(
                f"plan_{leg} in {planner_path.name} attempted actuation (ORG-080 C5, CI guard): "
                + "; ".join(guard.violations[:5])
            )
        if failure is not None:
            raise PlanError(failure)
        try:
            wire = json.dumps(result, sort_keys=True, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise PlanError(f"plan_{leg} returned non-data (ORG-080 C5 data-only return): {exc}") from None
        return decode_response(wire)
