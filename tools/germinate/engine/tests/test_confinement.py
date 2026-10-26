"""T-5074 review MAJOR 1: planner code is guarded (not sandboxed) and contract
paths are contained in the checkout. Scenarios from the reviewer's probe.py."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.germinate.engine.contract import ContractError, contained_module_path
from tools.germinate.engine.executor import EngineRefusal
from tools.germinate.engine.planner import InProcessJsonPlannerRunner, PlanError, PlannerGuard
from tools.germinate.engine.tests import helpers as H
from tools.germinate.registry.tests import fixtures as F
from tools.germinate.registry.validate import check_contracts, errors

RUNNER = InProcessJsonPlannerRunner()


def _planner(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "p.py"
    path.write_text(body, encoding="utf-8")
    return path


def test_reviewer_escape_repro_is_refused(tmp_path):
    escaped = tmp_path / "ESCAPED-BY-PLANNER"
    planner = (
        "import pathlib\n"
        f"def plan_install(request):\n    pathlib.Path({str(escaped)!r}).write_text('planner ran unconfined')\n    return []\n"
        "def plan_remove(request):\n    return []\n"
    )
    _repo, eng = H.simple_setup(tmp_path, planner_src=planner)
    with pytest.raises(EngineRefusal, match="plan-refused: .*attempted actuation"):
        eng.plan("install", "system", "comp-a")
    assert not escaped.exists()


@pytest.mark.parametrize(
    "body",
    [
        "import os\n    os.mkdir({out!r})",
        "open({out!r}, 'a').close()",
        "import os\n    os.close(os.open({out!r}, os.O_WRONLY | os.O_CREAT, 0o600))",
        "import os\n    os.system('true')",
        "import subprocess\n    subprocess.run(['true'])",
        "import os\n    os.execv('/bin/true', ['true'])",
        "import os\n    os.kill(os.getpid(), 0)",
        # reviewer bypass.py: a thread would run outside the per-thread guard
        "import threading, pathlib\n    t = threading.Thread(target=lambda: pathlib.Path({out!r}).write_text('x'))\n    t.start()\n    t.join()",
        # reviewer bypass.py: sqlite3 creates its database without an "open" event
        "import sqlite3\n    sqlite3.connect({out!r}).execute('create table t(x)')",
    ],
)
def test_guard_refuses_each_actuation_class(tmp_path, body):
    out = str(tmp_path / "target")
    code = "def plan_install(request):\n    " + body.format(out=out) + "\n    return []\n"
    with pytest.raises(PlanError, match="attempted actuation"):
        RUNNER.run(_planner(tmp_path, code), "install", {})
    assert not os.path.exists(out)


def test_a_planner_that_swallows_the_refusal_still_fails(tmp_path):
    out = tmp_path / "target"
    code = f"def plan_install(request):\n    try:\n        open({str(out)!r}, 'w')\n    except Exception:\n        pass\n    return []\n"
    with pytest.raises(PlanError, match="open-for-write"):
        RUNNER.run(_planner(tmp_path, code), "install", {})
    assert not out.exists()


def test_module_level_code_is_guarded_too(tmp_path):
    out = tmp_path / "target"
    code = f"open({str(out)!r}, 'w').close()\ndef plan_install(request):\n    return []\n"
    with pytest.raises(PlanError, match="attempted actuation"):
        RUNNER.run(_planner(tmp_path, code), "install", {})
    assert not out.exists()


def test_benign_planner_that_reads_and_imports_passes(tmp_path):
    data = tmp_path / "data.txt"
    data.write_text("read me")
    code = (
        "import json, hashlib, pathlib\n"
        f"def plan_install(request):\n    text = pathlib.Path({str(data)!r}).read_text()\n"
        "    return [{'digest': hashlib.sha256(text.encode()).hexdigest()}]\n"
    )
    assert RUNNER.run(_planner(tmp_path, code), "install", {})[0]["digest"]
    assert not (tmp_path / "__pycache__").exists()  # importing under the guard writes no bytecode


def test_guard_is_scoped_to_the_planner_call(tmp_path):
    with pytest.raises(PlanError):
        RUNNER.run(_planner(tmp_path, f"def plan_install(r):\n    open({str(tmp_path / 'x')!r}, 'w')\n"), "install", {})
    (tmp_path / "after").write_text("the engine can write again")  # hook installed, but inactive
    assert PlannerGuard._active_thread is None


# --- path containment ---------------------------------------------------------


@pytest.mark.parametrize("module", ["/etc/x.yaml", "../outside/x.yaml", "tools/../../x.yaml", ""])
def test_contained_module_path_refuses(tmp_path, module):
    with pytest.raises(ContractError):
        contained_module_path(tmp_path, module)


def test_symlink_out_of_the_checkout_refuses(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "tools" / "components").symlink_to(outside)
    with pytest.raises(ContractError, match="outside the checkout"):
        contained_module_path(repo, "tools/components/x.yaml")
    assert contained_module_path(repo, "tools/x.yaml") == repo.resolve() / "tools" / "x.yaml"


def _repo_with_module(tmp_path, module: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _p, digest = H.write_contract(repo, "comp-a")
    H.make_checkout(repo, [H.row("comp-a", digest, contract={"repo": "pantheon-config", "module": module, "digest": digest})])
    return repo


@pytest.mark.parametrize("module", ["/tmp/elsewhere/comp-a.yaml", "../repo/tools/germinate/components/comp-a.yaml"])
def test_engine_refuses_uncontained_contract_paths(tmp_path, module):
    repo = _repo_with_module(tmp_path, module)
    with pytest.raises(EngineRefusal, match="absolute|'\\.\\.'"):
        H.engine(tmp_path, repo).plan("install", "system", "comp-a")


@pytest.mark.parametrize("module", ["/tmp/elsewhere/comp-a.yaml", "../repo/tools/germinate/components/comp-a.yaml"])
def test_validator_refuses_uncontained_contract_paths(tmp_path, module):
    doc = F.doc(H.row("comp-a", "sha256:" + "a" * 64, contract={"repo": "pantheon-config", "module": module, "digest": "sha256:" + "a" * 64}))
    assert "relative path without '..'" in "\n".join(str(f) for f in errors(check_contracts(doc, tmp_path)))


def test_uncovered_shapes_are_documented_not_claimed():
    """fork_exec (no own audit event on 3.12), os.mkfifo (none) and flipping
    PlannerGuard internals are named as uncovered rather than tested as refused."""

    from tools.germinate.engine import backend, planner

    for doc in (planner.__doc__, backend.__doc__):
        assert "mkfifo" in doc and "fork_exec" in doc and "PlannerGuard" in doc


# --- sockets: environment-proof against long tmp paths (CI #1030) -------------
#
# CPython validates an AF_UNIX address (sun_path, 108 bytes) BEFORE it raises
# the socket.connect / socket.bind audit event, so a long tmp_path makes the OS
# error win and the guard never sees the call. These cases use a short
# directory directly under /tmp so the audit event fires; the other
# confinement cases take ordinary file paths (PATH_MAX-bound, not sun_path),
# so tmp_path length does not affect them.

SUN_PATH_MAX = 107  # usable bytes of sockaddr_un.sun_path on Linux


@pytest.fixture
def short_socket_path():
    import tempfile

    directory = tempfile.mkdtemp(prefix="g", dir="/tmp")
    path = os.path.join(directory, "s")
    try:
        if len(path.encode()) > SUN_PATH_MAX:
            pytest.skip(f"cannot make an AF_UNIX path within {SUN_PATH_MAX} bytes here: {path!r}")
        yield path
    finally:
        if os.path.exists(path):
            os.unlink(path)
        os.rmdir(directory)


@pytest.mark.parametrize("call", ["connect", "bind"])
def test_guard_refuses_unix_socket_connect_and_bind(tmp_path, short_socket_path, call):
    code = (
        "import socket\n"
        "def plan_install(request):\n"
        f"    socket.socket(socket.AF_UNIX).{call}({short_socket_path!r})\n"
        "    return []\n"
    )
    with pytest.raises(PlanError, match=f"attempted actuation.*socket.{call}"):
        RUNNER.run(_planner(tmp_path, code), "install", {})
    assert not os.path.exists(short_socket_path)  # bind would have created it


# --- T-5330: thread starts on interpreters with no thread-start audit event ----

import _thread
import threading


@pytest.fixture
def no_thread_event(monkeypatch):
    """Force the fallback path (as on CPython 3.11) regardless of interpreter."""
    # T-5426: install first. The first __enter__ in a process runs _install(), which
    # re-probes and would overwrite the forced value (order-dependent under xdist).
    PlannerGuard._install()
    monkeypatch.setattr(PlannerGuard, "_thread_event_present", False)


def test_thread_start_wrappers_restored_exactly_after_exit(no_thread_event):
    before = (threading._start_new_thread, _thread.start_new_thread)
    with PlannerGuard():
        assert threading._start_new_thread is not before[0]
    assert (threading._start_new_thread, _thread.start_new_thread) == before
    with pytest.raises(RuntimeError):
        with PlannerGuard():
            raise RuntimeError("boom")
    assert (threading._start_new_thread, _thread.start_new_thread) == before
    assert PlannerGuard._active_thread is None


def test_fallback_wrapper_exposes_no_original(no_thread_event):
    with PlannerGuard():
        for fn in (threading._start_new_thread, _thread.start_new_thread):
            assert not hasattr(fn, "__wrapped__")


def test_fallback_refuses_direct_low_level_start(no_thread_event):
    with pytest.raises(Exception):  # PlannerConfinementError (PermissionError)
        with PlannerGuard() as g:
            try:
                _thread.start_new_thread(lambda: None, ())
            finally:
                assert PlannerGuard._violations == ["_thread.start_new_thread"]
    assert g.violations == ["_thread.start_new_thread"]


def test_fallback_does_not_affect_other_threads(no_thread_event):
    started, done = threading.Event(), threading.Event()
    results: list[str] = []

    def other():
        started.set()
        try:
            t = threading.Thread(target=done.set)
            t.start()
            t.join()
            results.append("ok")
        except BaseException as exc:  # pragma: no cover - failure path
            results.append(repr(exc))

    # hold the guard active on a worker while a third thread starts threads
    gate = threading.Event()

    def guarded():
        with PlannerGuard():
            gate.set()
            done.wait(5)

    g = threading.Thread(target=guarded)
    g.start()
    assert gate.wait(5)
    o = threading.Thread(target=other)
    o.start()
    o.join(5)
    done.set()
    g.join(5)
    assert results == ["ok"]


def test_probe_failure_fails_closed(monkeypatch):
    def boom(self):
        raise RuntimeError("cannot start threads")

    monkeypatch.setattr(threading.Thread, "start", boom)
    assert PlannerGuard._detect_thread_event() is False


def test_probe_reports_interpreter_reality():
    import sys

    seen: list[str] = []
    live = [True]
    sys.addaudithook(lambda e, a: seen.append(e) if live[0] and e == "_thread.start_new_thread" else None)
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    live[0] = False
    assert PlannerGuard._detect_thread_event() is bool(seen)


def test_no_patch_when_event_present(monkeypatch):
    PlannerGuard._install()  # T-5426: see no_thread_event
    monkeypatch.setattr(PlannerGuard, "_thread_event_present", True)
    before = (threading._start_new_thread, _thread.start_new_thread)
    with PlannerGuard():
        assert (threading._start_new_thread, _thread.start_new_thread) == before


def test_stale_wrapper_after_exit_still_starts_threads(no_thread_event):
    with PlannerGuard():
        low_level = _thread.start_new_thread  # e.g. `from _thread import start_new_thread`
        via_threading = threading._start_new_thread
    ran = threading.Event()
    low_level(ran.set, ())
    assert ran.wait(5)
    ran.clear()
    via_threading(ran.set, ())
    assert ran.wait(5)


def test_partial_patch_failure_rolls_back_and_leaks_nothing(no_thread_event, monkeypatch):
    import sys

    before = (threading._start_new_thread, _thread.start_new_thread)
    bytecode = sys.dont_write_bytecode
    calls = {"n": 0}
    real = PlannerGuard._refusing_start.__func__

    def flaky(cls, which):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("second patch fails")
        return real(cls, which)

    monkeypatch.setattr(PlannerGuard, "_refusing_start", classmethod(flaky))
    with pytest.raises(RuntimeError, match="second patch"):
        PlannerGuard().__enter__()
    assert (threading._start_new_thread, _thread.start_new_thread) == before
    assert sys.dont_write_bytecode == bytecode
    assert PlannerGuard._active_thread is None


def test_concurrent_entries_cannot_both_pass(no_thread_event):
    results: list[str] = []
    barrier = threading.Barrier(2)
    release = threading.Event()

    def enter():
        barrier.wait(5)
        try:
            with PlannerGuard():
                results.append("in")
                release.wait(5)
        except PlanError:
            results.append("refused")
            release.set()

    ts = [threading.Thread(target=enter) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert sorted(results) == ["in", "refused"]
