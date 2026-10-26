#!/usr/bin/env python3
"""Tests for tools/echo_evidence_build.py (T-4122).

The contract under test: an Echo evidence build (review smoke, merge gate) gets
a CARGO_TARGET_DIR keyed on the worktree's realpath, so two worktrees can never
reuse each other's workspace-crate artifacts (bugs/shared-cargo-target-cross-
worktree-artifact-bleed.md). No test here runs cargo; children are trivial
python3 one-liners. Every test overrides the target root via
ECHO_EVIDENCE_TARGET_ROOT so nothing touches /var/lib/pantheon.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "tools" / "echo_evidence_build.py"
sys.path.insert(0, str(REPO_ROOT))

from tools import echo_evidence_build as eeb  # noqa: E402

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
       "-c", "core.hooksPath=/dev/null", "-c", "init.defaultBranch=main"]


def _git(*args: str, cwd: Path) -> None:
    subprocess.run([*GIT, *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "echo-main"
    r.mkdir()
    _git("init", cwd=r)
    _git("commit", "--allow-empty", "-m", "init", cwd=r)
    # cargo builds the CWD's sources, so the tool requires cwd inside --worktree
    monkeypatch.chdir(r)
    return r


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    rt = tmp_path / "evidence-targets"
    monkeypatch.setenv("ECHO_EVIDENCE_TARGET_ROOT", str(rt))
    monkeypatch.delenv("ECHO_EVIDENCE_FORCE", raising=False)
    monkeypatch.delenv("ECHO_EVIDENCE_DISK_FLOOR_GB", raising=False)
    monkeypatch.delenv("ECHO_EVIDENCE_SCCACHE", raising=False)  # the default: off
    monkeypatch.setattr(eeb, "free_gb", lambda _p: 500.0)
    return rt


class Captured:
    def __init__(self) -> None:
        self.cmd: list[str] | None = None
        self.env: dict[str, str] | None = None


@pytest.fixture
def cap(monkeypatch: pytest.MonkeyPatch) -> Captured:
    c = Captured()

    def fake_exec(cmd, env):
        c.cmd, c.env = list(cmd), dict(env)
        return 0

    monkeypatch.setattr(eeb, "_exec", fake_exec)
    return c


def _stub_sccache(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "stubbin"
    d.mkdir(exist_ok=True)
    s = d / "sccache"
    s.write_text("#!/bin/sh\n" + body + "\n")
    s.chmod(0o755)
    return d


# ---------------------------------------------------------------- keying

def test_same_worktree_same_dir(repo, root):
    a = eeb.target_dir_for(eeb.resolve_worktree(str(repo)), root)
    sub = repo / "crates"
    sub.mkdir()
    b = eeb.target_dir_for(eeb.resolve_worktree(str(sub)), root)
    assert a == b
    assert a.parent == root
    assert a.name.startswith("echo-main-")
    assert len(a.name.rsplit("-", 1)[1]) == 12


def test_two_worktrees_different_dirs_even_same_branch(repo, root, tmp_path):
    wt2 = tmp_path / "wt-same-branch"
    _git("worktree", "add", "--force", str(wt2), "main", cwd=repo)
    wt3 = tmp_path / "wt-other"
    _git("worktree", "add", "-b", "other", str(wt3), cwd=repo)
    dirs = {eeb.target_dir_for(eeb.resolve_worktree(str(p)), root)
            for p in (repo, wt2, wt3)}
    assert len(dirs) == 3


def test_symlinked_path_keys_on_realpath(repo, root, tmp_path):
    link = tmp_path / "link-to-main"
    link.symlink_to(repo)
    assert (eeb.target_dir_for(eeb.resolve_worktree(str(link)), root)
            == eeb.target_dir_for(eeb.resolve_worktree(str(repo)), root))


def test_not_a_worktree_refused(tmp_path, root, cap, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    rc = eeb.main(["--worktree", str(plain), "--", "true"])
    assert rc != 0
    assert cap.cmd is None
    assert "not a git worktree" in capsys.readouterr().err


# ---------------------------------------------------------------- env

def test_env_and_dir_created_group_writable(repo, root, cap, monkeypatch):
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    old = os.umask(0o022)  # non-vacuous: a plain mkdir under 022 would NOT be g+w
    try:
        rc = eeb.main(["--worktree", str(repo), "--", "cargo", "check"])
    finally:
        os.umask(old)
    assert rc == 0
    tdir = eeb.target_dir_for(eeb.resolve_worktree(str(repo)), root)
    assert cap.cmd == ["cargo", "check"]
    assert cap.env["CARGO_TARGET_DIR"] == str(tdir)
    assert cap.env["CARGO_INCREMENTAL"] == "0"
    assert tdir.is_dir()
    assert tdir.stat().st_mode & 0o020, "target dir must be group-writable"
    meta = json.loads((tdir / eeb.META_NAME).read_text())
    assert meta["worktree"] == str(repo.resolve())
    assert abs(meta["last_used_epoch"] - time.time()) < 60


def test_inherited_cargo_target_dir_overridden_loudly(repo, root, cap, capsys, monkeypatch):
    monkeypatch.setenv("CARGO_TARGET_DIR", "/var/lib/pantheon/echo-target")
    rc = eeb.main(["--worktree", str(repo), "--", "cargo", "check"])
    assert rc == 0
    assert cap.env["CARGO_TARGET_DIR"] != "/var/lib/pantheon/echo-target"
    assert cap.env["CARGO_TARGET_DIR"].startswith(str(root))
    err = capsys.readouterr().err
    assert "OVERRID" in err and "/var/lib/pantheon/echo-target" in err


def test_target_dir_flag_in_command_refused(repo, root, cap):
    rc = eeb.main(["--worktree", str(repo), "--", "cargo", "check", "--target-dir", "/x"])
    assert rc != 0
    assert cap.cmd is None


def test_worktree_defaults_to_cwd_toplevel(repo, root, cap, monkeypatch):
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert eeb.main(["--", "true"]) == 0
    assert cap.env["CARGO_TARGET_DIR"] == str(
        eeb.target_dir_for(repo.resolve(), root))


# ---------------------------------------------------------------- disk floor

def test_disk_floor_refuses_and_names_gc(repo, root, cap, capsys, monkeypatch):
    monkeypatch.setattr(eeb, "free_gb", lambda _p: 3.0)
    rc = eeb.main(["--worktree", str(repo), "--", "true"])
    assert rc != 0
    assert cap.cmd is None
    err = capsys.readouterr().err
    assert "--gc" in err
    assert not root.exists() or not any(root.iterdir())


def test_disk_floor_env_tunable(repo, root, cap, monkeypatch):
    monkeypatch.setattr(eeb, "free_gb", lambda _p: 3.0)
    monkeypatch.setenv("ECHO_EVIDENCE_DISK_FLOOR_GB", "2")
    assert eeb.main(["--worktree", str(repo), "--", "true"]) == 0
    assert cap.cmd == ["true"]


def test_disk_floor_force_override(repo, root, cap, monkeypatch):
    monkeypatch.setattr(eeb, "free_gb", lambda _p: 3.0)
    monkeypatch.setenv("ECHO_EVIDENCE_FORCE", "1")
    assert eeb.main(["--worktree", str(repo), "--", "true"]) == 0
    assert cap.cmd == ["true"]


# ---------------------------------------------------------------- sccache

def test_sccache_probe_fails_wrapper_unset(repo, root, cap, capsys, monkeypatch, tmp_path):
    stub = _stub_sccache(tmp_path, "exit 2")
    monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")
    monkeypatch.setenv("ECHO_EVIDENCE_SCCACHE", "1")
    monkeypatch.setenv("RUSTC_WRAPPER", "sccache")  # inherited, but non-functional
    assert eeb.main(["--worktree", str(repo), "--", "true"]) == 0
    assert "RUSTC_WRAPPER" not in cap.env
    assert "sccache" in capsys.readouterr().err


def test_sccache_absent_wrapper_unset(repo, root, cap, monkeypatch, tmp_path):
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", f"{empty}:/usr/bin:/bin")
    monkeypatch.setenv("ECHO_EVIDENCE_SCCACHE", "1")
    monkeypatch.setattr(eeb.shutil, "which",
                        lambda name, path=None: None if name == "sccache" else "/usr/bin/" + name)
    assert eeb.main(["--worktree", str(repo), "--", "true"]) == 0
    assert "RUSTC_WRAPPER" not in cap.env


def test_sccache_probe_hang_times_out(repo, root, cap, monkeypatch, tmp_path):
    stub = _stub_sccache(tmp_path, "sleep 10")
    monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")
    monkeypatch.setenv("ECHO_EVIDENCE_SCCACHE", "1")
    monkeypatch.setattr(eeb, "SCCACHE_PROBE_TIMEOUT_S", 0.5)
    # T-4793: prove the probe's own timeout cut the hang off, instead of a
    # wall-clock bound on all of main() (that hit 5.8s under CI load). The stub
    # sleeps 10s, so only TimeoutExpired explains a probe that didn't wait it out.
    real_run = eeb.subprocess.run
    probes: list[dict] = []

    def spy_run(cmd, *a, **kw):
        rec = {"cmd": list(cmd), "timeout": kw.get("timeout"), "timed_out": False}
        probes.append(rec)
        try:
            return real_run(cmd, *a, **kw)
        except subprocess.TimeoutExpired:
            rec["timed_out"] = True
            raise

    monkeypatch.setattr(eeb.subprocess, "run", spy_run)
    assert eeb.main(["--worktree", str(repo), "--", "true"]) == 0
    sc = [p for p in probes if p["cmd"] and p["cmd"][0].endswith("sccache")]
    assert len(sc) == 1
    assert sc[0]["timeout"] == 0.5
    assert sc[0]["timed_out"]
    assert "RUSTC_WRAPPER" not in cap.env


def test_sccache_probe_succeeds_wrapper_set(repo, root, cap, monkeypatch, tmp_path):
    stub = _stub_sccache(tmp_path, "exit 0")
    monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")
    monkeypatch.setenv("ECHO_EVIDENCE_SCCACHE", "1")
    assert eeb.main(["--worktree", str(repo), "--", "true"]) == 0
    assert cap.env["RUSTC_WRAPPER"] == "sccache"
    # isolation is still in force underneath the speed layer
    assert cap.env["CARGO_TARGET_DIR"].startswith(str(root))


@pytest.mark.parametrize("val", [None, "0", "", "true", "yes", "2"])
def test_sccache_off_unless_exactly_1(repo, root, cap, capsys, monkeypatch, tmp_path, val):
    # OPT-IN (T-4122 measurement): unset or anything but "1" -> no probe, wrapper cleared,
    # even with a working sccache on PATH and an inherited RUSTC_WRAPPER.
    marker = tmp_path / "probed"
    stub = _stub_sccache(tmp_path, f"touch {marker}; exit 0")
    monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")
    if val is None:
        monkeypatch.delenv("ECHO_EVIDENCE_SCCACHE", raising=False)
    else:
        monkeypatch.setenv("ECHO_EVIDENCE_SCCACHE", val)
    monkeypatch.setenv("RUSTC_WRAPPER", "sccache")
    assert eeb.main(["--worktree", str(repo), "--", "true"]) == 0
    assert "RUSTC_WRAPPER" not in cap.env
    assert not marker.exists(), "sccache is opt-in: it must not even be probed"
    assert "sccache" in capsys.readouterr().err  # says so in one line


# ---------------------------------------------------------------- gc / list

def _mk_target(root: Path, name: str, worktree: str, last_used: float) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "debug").mkdir()
    (d / "debug" / "artifact.rlib").write_bytes(b"x" * 4096)
    (d / eeb.META_NAME).write_text(json.dumps(eeb.make_meta(worktree, last_used)))
    return d


def test_gc_removes_orphan_and_stale_keeps_live_and_outside(repo, root, tmp_path, capsys):
    now = time.time()
    live = _mk_target(root, "live-aaaaaaaaaaaa", str(repo), now)
    orphan = _mk_target(root, "gone-bbbbbbbbbbbb", str(tmp_path / "deleted-wt"), now)
    stale = _mk_target(root, "old-cccccccccccc", str(repo), now - 30 * 86400)
    nometa = root / "nometa-dddddddddddd"
    nometa.mkdir()
    # a sibling OUTSIDE the root, shaped exactly like an orphan: must survive
    outside_parent = tmp_path / "echo-target"
    outside = _mk_target(outside_parent, "gone-eeeeeeeeeeee", str(tmp_path / "nope"), 0)
    # a symlink inside the root pointing at the outside dir: must not be followed
    (root / "link-ffffffffffff").symlink_to(outside)

    rc = eeb.main(["--gc", "--older-than-days", "14"])
    assert rc == 0
    assert live.is_dir()
    assert not orphan.exists()
    assert not stale.exists()
    assert nometa.is_dir(), "dirs without our meta are never deleted"
    assert outside.is_dir() and (outside / eeb.META_NAME).is_file()
    assert (outside / "debug" / "artifact.rlib").is_file()
    assert outside_parent.is_dir()


def test_gc_never_touches_shared_target(repo, root, tmp_path, monkeypatch):
    shared = _mk_target(root, "echo-target-000000000000", str(tmp_path / "gone"), 0)
    monkeypatch.setattr(eeb, "SHARED_TARGET", shared)
    assert eeb.main(["--gc", "--older-than-days", "0"]) == 0
    assert shared.is_dir()


def test_gc_refuses_root_that_is_shared_target(tmp_path, monkeypatch):
    shared = tmp_path / "echo-target"
    victim = _mk_target(shared, "x-111111111111", str(tmp_path / "gone"), 0)
    monkeypatch.setattr(eeb, "SHARED_TARGET", shared)
    monkeypatch.setenv("ECHO_EVIDENCE_TARGET_ROOT", str(shared))
    assert eeb.main(["--gc"]) != 0
    assert victim.is_dir()
    # nor a root nested inside the shared target
    monkeypatch.setenv("ECHO_EVIDENCE_TARGET_ROOT", str(shared / "x-111111111111"))
    assert eeb.main(["--gc"]) != 0
    assert victim.is_dir()


def test_list_shows_dirs(repo, root, capsys):
    _mk_target(root, "live-aaaaaaaaaaaa", str(repo), time.time())
    assert eeb.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "live-aaaaaaaaaaaa" in out
    assert str(repo) in out


def test_list_empty_root_ok(root, capsys):
    assert eeb.main(["--list"]) == 0


# ---------------------------------------------------------------- dry-run

def test_dry_run_runs_nothing(repo, root, cap, capsys, tmp_path, monkeypatch):
    marker = tmp_path / "probed"
    stub = _stub_sccache(tmp_path, f"touch {marker}; exit 0")
    monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")
    monkeypatch.setenv("ECHO_EVIDENCE_SCCACHE", "1")
    rc = eeb.main(["--worktree", str(repo), "--dry-run", "--", "cargo", "test"])
    assert rc == 0
    assert cap.cmd is None
    assert not marker.exists()
    assert not root.exists()
    out = capsys.readouterr().out
    assert "CARGO_TARGET_DIR=" in out and "cargo test" in out


# ---------------------------------------------------------------- real exec

def _run_tool(repo: Path, root: Path, *child: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, ECHO_EVIDENCE_TARGET_ROOT=str(root),
               ECHO_EVIDENCE_SCCACHE="0", ECHO_EVIDENCE_FORCE="1")
    return subprocess.run([sys.executable, str(TOOL), "--worktree", str(repo), "--", *child],
                          env=env, capture_output=True, text=True, timeout=60, cwd=repo)


def test_child_exit_code_propagates(repo, root):
    p = _run_tool(repo, root, "python3", "-c", "import sys; sys.exit(3)")
    assert p.returncode == 3, p.stderr


def test_child_gets_umask_002_and_env(repo, root):
    code = ("import os, json; m = os.umask(0); "
            "print(json.dumps({'umask': m, 'ctd': os.environ.get('CARGO_TARGET_DIR'), "
            "'inc': os.environ.get('CARGO_INCREMENTAL')}))")
    p = _run_tool(repo, root, "python3", "-c", code)
    assert p.returncode == 0, p.stderr
    got = json.loads(p.stdout.strip().splitlines()[-1])
    assert got["umask"] == 0o002
    assert got["inc"] == "0"
    assert got["ctd"].startswith(str(root))


def test_missing_command_is_usage_error(repo, root, cap):
    assert eeb.main(["--worktree", str(repo)]) != 0
    assert cap.cmd is None


# ---------------------------------------------------------------- opportunistic orphan GC

def test_normal_run_reaps_orphan_keeps_live_and_current(repo, root, cap, capsys, tmp_path):
    now = time.time()
    orphan = _mk_target(root, "gone-bbbbbbbbbbbb", str(tmp_path / "deleted-wt"), now)
    live = _mk_target(root, "live-aaaaaaaaaaaa", str(repo), now - 365 * 86400)  # old but live
    # the CURRENT worktree's dir, with meta that (wrongly) records a missing path:
    cur = eeb.target_dir_for(repo.resolve(), root)
    cur.mkdir(parents=True)
    (cur / eeb.META_NAME).write_text(json.dumps(eeb.make_meta(str(tmp_path / "moved"), now)))
    assert eeb.main(["--worktree", str(repo), "--", "true"]) == 0
    assert cap.cmd == ["true"]
    assert not orphan.exists()
    assert live.is_dir(), "age-based removal is exclusive to explicit --gc"
    assert cur.is_dir()
    err = capsys.readouterr().err
    assert "gone-bbbbbbbbbbbb" in err
    assert sum("reclaimed" in ln for ln in err.splitlines()) == 1


def test_normal_run_prints_nothing_when_no_orphans(repo, root, cap, capsys):
    _mk_target(root, "live-aaaaaaaaaaaa", str(repo), time.time())
    assert eeb.main(["--worktree", str(repo), "--", "true"]) == 0
    assert "reclaimed" not in capsys.readouterr().err


def test_orphan_gc_never_touches_outside_or_shared(repo, root, cap, tmp_path, monkeypatch):
    shared = _mk_target(root, "echo-target-000000000000", str(tmp_path / "gone1"), 0)
    monkeypatch.setattr(eeb, "SHARED_TARGET", shared)
    outside = _mk_target(tmp_path / "sib", "gone-eeeeeeeeeeee", str(tmp_path / "gone2"), 0)
    (root / "link-ffffffffffff").symlink_to(outside)
    assert eeb.main(["--worktree", str(repo), "--", "true"]) == 0
    assert shared.is_dir() and outside.is_dir()


def test_orphan_gc_error_does_not_change_exit_code(repo, root, capsys, tmp_path, monkeypatch):
    _mk_target(root, "gone-bbbbbbbbbbbb", str(tmp_path / "deleted-wt"), time.time())

    def boom(*a, **k):
        raise PermissionError("EACCES simulated")

    monkeypatch.setattr(eeb.shutil, "rmtree", boom)
    monkeypatch.setattr(eeb, "_exec", lambda cmd, env: 3)
    assert eeb.main(["--worktree", str(repo), "--", "x"]) == 3
    assert "EACCES simulated" in capsys.readouterr().err


def test_orphan_gc_unexpected_error_does_not_block(repo, root, cap, capsys, monkeypatch):
    def boom(_root):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(eeb, "_candidates", boom)
    assert eeb.main(["--worktree", str(repo), "--", "true"]) == 0
    assert cap.cmd == ["true"]
    assert "scan exploded" in capsys.readouterr().err


def test_orphan_gc_concurrent_removal_is_already_gone(repo, root, cap, capsys, tmp_path, monkeypatch):
    orphan = _mk_target(root, "gone-bbbbbbbbbbbb", str(tmp_path / "deleted-wt"), time.time())
    real_rmtree = shutil.rmtree

    def racing(path, *a, **k):
        real_rmtree(path)  # the other seat wins the race
        raise FileNotFoundError(2, "No such file or directory", str(path))

    monkeypatch.setattr(eeb.shutil, "rmtree", racing)
    assert eeb.main(["--worktree", str(repo), "--", "true"]) == 0
    assert not orphan.exists()
    err = capsys.readouterr().err
    assert "FAILED" not in err and "error" not in err.lower()


def test_dry_run_does_no_orphan_gc(repo, root, cap, tmp_path):
    orphan = _mk_target(root, "gone-bbbbbbbbbbbb", str(tmp_path / "deleted-wt"), time.time())
    assert eeb.main(["--worktree", str(repo), "--dry-run", "--", "true"]) == 0
    assert orphan.is_dir()


# ---------------------------------------------------------------- rebound #1: cross-seat safety

def _gc_both_paths(repo: Path) -> None:
    """Run the opportunistic reap (a normal run) and then explicit --gc."""
    assert eeb.main(["--worktree", str(repo), "--", "true"]) == 0
    eeb.main(["--gc", "--older-than-days", "14"])


def test_unreadable_worktree_path_is_not_orphan(repo, root, cap, tmp_path, monkeypatch):
    # another seat's LIVE worktree under a 0750 home: stat -> EACCES, NOT "gone"
    peer_wt = "/home/peer-seat/echo-live-wt"
    peer = _mk_target(root, "peer-aaaaaaaaaaaa", peer_wt, time.time())
    real_stat = os.stat

    def fake_stat(path, *a, **k):
        if os.fspath(path) == peer_wt:
            raise PermissionError(13, "Permission denied", peer_wt)
        return real_stat(path, *a, **k)

    monkeypatch.setattr(eeb.os, "stat", fake_stat)
    monkeypatch.setattr(eeb.os.path, "exists", lambda p: False)  # the old, wrong predicate
    _gc_both_paths(repo)
    assert peer.is_dir()


def test_opportunistic_reap_skips_other_uid_meta(repo, root, cap, tmp_path, monkeypatch):
    orphan = _mk_target(root, "gone-bbbbbbbbbbbb", str(tmp_path / "deleted-wt"), time.time())
    monkeypatch.setattr(eeb, "_my_uid", lambda: os.getuid() + 4242)
    assert eeb.main(["--worktree", str(repo), "--", "true"]) == 0
    assert orphan.is_dir(), "per-run reap must leave other seats' dirs to explicit --gc"
    assert eeb.main(["--gc"]) == 0
    assert not orphan.exists(), "explicit --gc still reaps a true orphan of any seat"


def test_inflight_cargo_lock_keeps_dir_both_paths(repo, root, cap, tmp_path):
    import fcntl
    orphan = _mk_target(root, "gone-bbbbbbbbbbbb", str(tmp_path / "deleted-wt"), 0)
    lock = orphan / "debug" / ".cargo-lock"
    lock.write_text("")
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _gc_both_paths(repo)
        assert orphan.is_dir()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    _gc_both_paths(repo)
    assert not orphan.exists(), "an unlocked .cargo-lock does not pin the dir"


def test_inflight_cargo_lock_depth2_keeps_dir(repo, root, cap, tmp_path):
    # cross-target layout: <tdir>/<triple>/debug/.cargo-lock
    import fcntl
    orphan = _mk_target(root, "gone-bbbbbbbbbbbb", str(tmp_path / "deleted-wt"), 0)
    lock = orphan / "x86_64-unknown-linux-gnu" / "debug" / ".cargo-lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _gc_both_paths(repo)
        assert orphan.is_dir()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@pytest.mark.parametrize("with_worktree", [False, True])
def test_deleted_cwd_is_usage_error_not_traceback(repo, root, cap, capsys, tmp_path,
                                                  monkeypatch, with_worktree):
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    monkeypatch.chdir(doomed)
    doomed.rmdir()
    argv = (["--worktree", str(repo)] if with_worktree else []) + ["--", "true"]
    assert eeb.main(argv) == eeb.EX_USAGE
    assert cap.cmd is None
    err = capsys.readouterr().err
    assert "cwd" in err and "Traceback" not in err
    assert len(err.strip().splitlines()) == 1


def test_unopenable_cargo_lock_counts_as_held(repo, root, cap, tmp_path, monkeypatch):
    orphan = _mk_target(root, "gone-bbbbbbbbbbbb", str(tmp_path / "deleted-wt"), 0)
    (orphan / "debug" / ".cargo-lock").write_text("")
    real_open = os.open

    def fake_open(path, flags, *a, **k):
        if os.fspath(path).endswith(".cargo-lock"):
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(eeb.os, "open", fake_open)
    _gc_both_paths(repo)
    assert orphan.is_dir()


@pytest.mark.parametrize("bad", [None, "", "relative/wt", 7])
def test_bad_worktree_meta_value_never_reaped(repo, root, cap, bad):
    d = root / "bad-aaaaaaaaaaaa"
    d.mkdir(parents=True)
    m = eeb.make_meta("/x", 0)
    m["worktree"] = bad
    (d / eeb.META_NAME).write_text(json.dumps(m))
    assert eeb.read_meta(d) is None
    _gc_both_paths(repo)
    assert d.is_dir()


@pytest.mark.parametrize("path", ["opportunistic", "explicit"])
def test_bad_meta_does_not_abort_reap(repo, root, cap, capsys, tmp_path, path):
    a = root / "a-nullwt-aaaaaaaaaaaa"
    a.mkdir(parents=True)
    ma = eeb.make_meta("/x", 0)
    ma["worktree"] = None
    (a / eeb.META_NAME).write_text(json.dumps(ma))
    b = _mk_target(root, "b-strepoch-bbbbbbbbbbbb", str(repo), 0)
    mb = json.loads((b / eeb.META_NAME).read_text())
    mb["last_used_epoch"] = "yesterday"
    (b / eeb.META_NAME).write_text(json.dumps(mb))
    c = _mk_target(root, "c-orphan-cccccccccccc", str(tmp_path / "deleted-wt"), time.time())
    if path == "opportunistic":
        assert eeb.main(["--worktree", str(repo), "--", "true"]) == 0
    else:
        assert eeb.main(["--gc", "--older-than-days", "0"]) == 0
    assert a.is_dir() and b.is_dir()
    assert not c.exists(), "a valid orphan after bad metas must still be reaped"


def test_per_dir_exception_does_not_abort_explicit_gc(repo, root, tmp_path, monkeypatch):
    a = _mk_target(root, "a-aaaaaaaaaaaa", str(tmp_path / "gone-a"), 0)
    c = _mk_target(root, "c-cccccccccccc", str(tmp_path / "gone-c"), 0)
    real = eeb.read_meta

    def flaky(d):
        if Path(d).name.startswith("a-"):
            raise RuntimeError("weird meta")
        return real(d)

    monkeypatch.setattr(eeb, "read_meta", flaky)
    eeb.main(["--gc"])
    assert a.is_dir() and not c.exists()


def test_rmtree_handler_swallows_file_vanishing_mid_walk(tmp_path, monkeypatch):
    d = tmp_path / "t"
    (d / "sub").mkdir(parents=True)
    for n in ("f1", "f2", "f3"):
        (d / "sub" / n).write_text("x")
    real_unlink = os.unlink
    fired = []

    def racing_unlink(path, *a, **k):
        if os.fspath(path) == "f2" and not fired:
            fired.append(1)
            real_unlink(path, *a, **k)  # the other seat removes it first...
        return real_unlink(path, *a, **k)  # ...so ours hits ENOENT

    monkeypatch.setattr(os, "unlink", racing_unlink)
    eeb._rmtree_racy(d)
    assert fired and not d.exists()


def test_rmtree_handler_still_raises_other_errors(tmp_path, monkeypatch):
    d = tmp_path / "t"
    d.mkdir()
    (d / "f").write_text("x")

    def eperm(path, *a, **k):
        raise PermissionError(1, "Operation not permitted", os.fspath(path))

    monkeypatch.setattr(os, "unlink", eperm)
    with pytest.raises(PermissionError):
        eeb._rmtree_racy(d)


# ---------------------------------------------------------------- rebound #1: cwd / manifest / target-dir

def test_cwd_outside_worktree_refused(repo, root, cap, capsys, tmp_path, monkeypatch):
    other = tmp_path / "wt-other"
    _git("worktree", "add", "-b", "other", str(other), cwd=repo)
    monkeypatch.chdir(other)
    assert eeb.main(["--worktree", str(repo), "--", "cargo", "test"]) == eeb.EX_USAGE
    assert cap.cmd is None
    assert "cwd" in capsys.readouterr().err


def test_cwd_in_subdir_of_worktree_ok(repo, root, cap, monkeypatch):
    sub = repo / "crates"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert eeb.main(["--worktree", str(repo), "--", "true"]) == 0


@pytest.mark.parametrize("form", ["split", "eq"])
def test_manifest_path_outside_worktree_refused(repo, root, cap, tmp_path, form):
    outside = tmp_path / "elsewhere" / "Cargo.toml"
    args = (["--manifest-path", str(outside)] if form == "split"
            else [f"--manifest-path={outside}"])
    assert eeb.main(["--worktree", str(repo), "--", "cargo", "test", *args]) == eeb.EX_USAGE
    assert cap.cmd is None


@pytest.mark.parametrize("form", ["split", "eq"])
def test_manifest_path_inside_worktree_ok(repo, root, cap, form):
    inside = "crates/foo/Cargo.toml"
    args = ["--manifest-path", inside] if form == "split" else [f"--manifest-path={inside}"]
    assert eeb.main(["--worktree", str(repo), "--", "cargo", "test", *args]) == 0


def test_target_dir_embedded_in_token_refused(repo, root, cap):
    rc = eeb.main(["--worktree", str(repo), "--", "bash", "-c",
                   "cargo test --target-dir /var/lib/pantheon/echo-target"])
    assert rc == eeb.EX_USAGE
    assert cap.cmd is None


def test_exec_eacces_maps_to_126(repo, root, tmp_path):
    noexec = tmp_path / "noexec.sh"
    noexec.write_text("#!/bin/sh\nexit 0\n")
    noexec.chmod(0o644)
    p = _run_tool(repo, root, str(noexec))
    assert p.returncode == 126, p.stderr


# ---------------------------------------------------------------- T-4795: no-meta backstop

_OLD = 7 * 3600  # older than NOMETA_GRACE_S (6h)


@pytest.fixture
def wt_parents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Stand-ins for /tmp and ~/echo-prismatic/.claude/worktrees."""
    tmpwt = tmp_path / "tmpwt"
    homewt = tmp_path / "home" / "echo-prismatic" / ".claude" / "worktrees"
    tmpwt.mkdir()
    homewt.mkdir(parents=True)
    monkeypatch.setattr(eeb, "_nometa_worktree_parents", lambda: [tmpwt, homewt])
    return tmpwt, homewt


def _mk_nometa(root: Path, worktree: Path, age_s: float) -> Path:
    """A raw-CARGO_TARGET_DIR build dir: target_dir_for's name, no meta file."""
    d = eeb.target_dir_for(worktree, root)
    (d / "debug").mkdir(parents=True)
    (d / "debug" / "artifact.rlib").write_bytes(b"x" * 4096)
    t = time.time() - age_s
    for dp, dns, fns in os.walk(d):
        for n in dns + fns:
            os.utime(os.path.join(dp, n), (t, t))
    os.utime(d, (t, t))
    assert not (d / eeb.META_NAME).exists()
    return d


@pytest.mark.parametrize("which", ["tmp", "home"])
@pytest.mark.parametrize("path", ["explicit", "opportunistic"])
def test_nometa_orphan_gone_worktree_old_is_reaped(repo, root, cap, capsys, wt_parents,
                                                   which, path):
    tmpwt, homewt = wt_parents
    wt = tmpwt / "echo-T-4294" if which == "tmp" else homewt / "T-4294"
    d = _mk_nometa(root, wt, _OLD)  # worktree never created == removed
    assert d.name.startswith(wt.name + "-")
    if path == "explicit":
        assert eeb.main(["--gc", "--older-than-days", "0"]) == 0
    else:
        assert eeb.main(["--worktree", str(repo), "--", "true"]) == 0
    assert not d.exists()
    err = capsys.readouterr().err
    assert f"{d.name} (no meta; worktree gone)" in err
    if path == "explicit":
        assert f"gc: reap {d.name} (no meta; worktree gone)" in err


def test_nometa_worktree_present_is_kept(repo, root, cap, wt_parents):
    wt = wt_parents[0] / "echo-T-4772"
    wt.mkdir()
    d = _mk_nometa(root, wt, 30 * 86400)
    _gc_both_paths(repo)
    eeb.main(["--gc", "--older-than-days", "0"])
    assert d.is_dir()


def test_nometa_worktree_unstatable_is_kept(repo, root, cap, wt_parents, monkeypatch):
    wt = wt_parents[0] / "echo-T-4772"
    d = _mk_nometa(root, wt, 30 * 86400)
    real_stat = os.stat

    def fake_stat(p, *a, **k):
        if os.fspath(p) == str(wt):
            raise PermissionError(13, "Permission denied", str(wt))
        return real_stat(p, *a, **k)

    monkeypatch.setattr(eeb.os, "stat", fake_stat)
    _gc_both_paths(repo)
    assert d.is_dir()


@pytest.mark.parametrize("name", [
    "nometa-dddddddddddd",            # not a ticket worktree basename
    "echo-main-aaaaaaaaaaaa",         # not a ticket worktree basename
    "echo-T-4294-aaaaaaaaaaaa",       # right shape, digest maps to no candidate path
    "echo-T-4294-ABCDEFabcdef",       # digest not lowercase hex
    "echo-T-4294",                    # no digest
])
def test_nometa_name_not_matching_is_kept(repo, root, cap, capsys, wt_parents, name):
    d = root / name
    (d / "debug").mkdir(parents=True)
    t = time.time() - 30 * 86400
    os.utime(d / "debug", (t, t))
    os.utime(d, (t, t))
    _gc_both_paths(repo)
    assert d.is_dir()
    assert "never deleted" in capsys.readouterr().err


def test_nometa_ambiguous_mapping_is_kept(repo, root, cap, wt_parents, monkeypatch):
    tmpwt, _ = wt_parents
    d = _mk_nometa(root, tmpwt / "echo-T-4294", _OLD)
    # the same parent listed twice -> two candidates reproduce the digest
    monkeypatch.setattr(eeb, "_nometa_worktree_parents", lambda: [tmpwt, tmpwt])
    assert eeb._nometa_worktree(d.name) is None
    _gc_both_paths(repo)
    assert d.is_dir()


def test_nometa_inflight_build_is_kept(repo, root, cap, wt_parents):
    import fcntl
    d = _mk_nometa(root, wt_parents[0] / "echo-T-4294", _OLD)
    lock = d / "debug" / ".cargo-lock"
    lock.write_text("")
    t = time.time() - _OLD
    os.utime(lock, (t, t))
    os.utime(d / "debug", (t, t))
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _gc_both_paths(repo)
        eeb.main(["--gc", "--older-than-days", "0"])
        assert d.is_dir()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert eeb.main(["--gc", "--older-than-days", "0"]) == 0
    assert not d.exists(), "an unlocked .cargo-lock does not pin the dir"


@pytest.mark.parametrize("fresh", ["dir", "deep-file"])
def test_nometa_too_new_is_kept(repo, root, cap, wt_parents, fresh):
    d = _mk_nometa(root, wt_parents[0] / "echo-T-4294", _OLD)
    now = time.time() - 3600  # 1h ago: inside the 6h grace
    target = d if fresh == "dir" else d / "debug" / "artifact.rlib"
    os.utime(target, (now, now))
    _gc_both_paths(repo)
    eeb.main(["--gc", "--older-than-days", "0"])
    assert d.is_dir()


@pytest.mark.parametrize("gc_args", [[], ["--older-than-days", "14"], ["--older-than-days", "30"]])
def test_nometa_gc_age_cutoff_does_not_extend_grace(repo, root, cap, capsys, wt_parents, gc_args):
    """r1: plain --gc (what the low-disk refusal tells people to run) must reclaim
    a no-meta worktree-gone orphan after the fixed 6h window, not after 14d."""
    fresh = _mk_nometa(root, wt_parents[0] / "echo-T-4772", 3600)
    d = _mk_nometa(root, wt_parents[0] / "echo-T-4294", _OLD)
    assert eeb.main(["--gc", *gc_args]) == 0
    assert not d.exists()
    assert fresh.is_dir()
    assert "written within the last 6h" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["no", "invalid"])
def test_nometa_skip_log_distinguishes_invalid(repo, root, cap, capsys, kind):
    d = root / "main-runs"
    d.mkdir(parents=True)
    if kind == "invalid":
        (d / eeb.META_NAME).write_text("{not json")
    eeb.main(["--gc", "--dry-run"])
    assert d.is_dir()
    assert f"gc: skip main-runs ({kind} {eeb.META_NAME}; never deleted)" in capsys.readouterr().err


def test_nometa_other_owner_is_kept_both_paths(repo, root, cap, wt_parents, monkeypatch):
    d = _mk_nometa(root, wt_parents[0] / "echo-T-4294", _OLD)
    monkeypatch.setattr(eeb, "_my_uid", lambda: os.getuid() + 4242)
    _gc_both_paths(repo)
    eeb.main(["--gc", "--older-than-days", "0"])
    assert d.is_dir(), "a no-meta dir is only ever reaped by its owning seat"


def test_nometa_symlink_and_shared_never_followed(repo, root, cap, wt_parents, tmp_path,
                                                  monkeypatch):
    outside = _mk_nometa(tmp_path / "sib", wt_parents[0] / "echo-T-1111", _OLD)
    link = root / eeb.target_dir_for(wt_parents[0] / "echo-T-2222", root).name
    root.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)
    shared = _mk_nometa(root, wt_parents[0] / "echo-T-3333", _OLD)
    monkeypatch.setattr(eeb, "SHARED_TARGET", shared)
    _gc_both_paths(repo)
    eeb.main(["--gc", "--older-than-days", "0"])
    assert outside.is_dir() and shared.is_dir() and link.is_symlink()


@pytest.mark.parametrize("how", ["cwd", "argv"])
def test_nometa_live_process_is_kept(repo, root, cap, capsys, wt_parents, how):
    d = _mk_nometa(root, wt_parents[0] / "echo-T-4294", _OLD)
    kw = {"cwd": d / "debug"} if how == "cwd" else {}
    argv = ["sleep", "30"] if how == "cwd" else ["sh", "-c", "sleep 30", f"--out-dir={d}/debug/deps"]
    proc = subprocess.Popen(argv, **kw)
    try:
        for _ in range(100):  # wait until exec'd so /proc shows the final cwd/argv
            if eeb._referenced([str(d)], eeb._proc_refs()):
                break
            time.sleep(0.02)
        _gc_both_paths(repo)
        assert eeb.main(["--gc", "--older-than-days", "0"]) == 0
        assert d.is_dir()
        assert "referenced by a live process" in capsys.readouterr().err
    finally:
        proc.kill()
        proc.wait()
    assert eeb.main(["--gc", "--older-than-days", "0"]) == 0
    assert not d.exists(), "reaped once the process is gone"


def test_nometa_live_process_environ_is_kept(repo, root, cap, wt_parents, tmp_path, monkeypatch):
    d = _mk_nometa(root, wt_parents[0] / "echo-T-4294", _OLD)
    fake = tmp_path / "proc"
    (fake / "4242").mkdir(parents=True)
    (fake / "4242" / "environ").write_bytes(b"HOME=/x\0CARGO_TARGET_DIR=" + str(d).encode() + b"\0")
    (fake / "self").mkdir()  # non-numeric entries are ignored
    monkeypatch.setattr(eeb, "PROC", fake)
    eeb.main(["--gc", "--older-than-days", "0"])
    assert d.is_dir()
    (fake / "4242" / "environ").write_bytes(b"CARGO_TARGET_DIR=" + str(d).encode() + b"x\0")
    assert eeb.main(["--gc", "--older-than-days", "0"]) == 0
    assert not d.exists(), "a longer sibling name is not a reference"


def test_referenced_path_boundary():
    p = "/r/echo-T-1-aaaaaaaaaaaa"
    assert eeb._referenced([p], [p])
    assert eeb._referenced([p], [p + "/debug/deps"])
    assert eeb._referenced([p], ["--out-dir=" + p + "/debug"])
    assert eeb._referenced([p], ["-Ldependency=" + p + ":/other"])
    assert not eeb._referenced([p], [p + "b"])
    assert not eeb._referenced([p], [p + "-x", "/r/echo-T-1"])


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads 0o000 dirs")
def test_nometa_unreadable_subtree_is_kept(repo, root, cap, capsys, wt_parents):
    d = _mk_nometa(root, wt_parents[0] / "echo-T-4294", _OLD)
    sub = d / "debug"
    t = time.time() - _OLD
    sub.chmod(0o000)
    os.utime(sub, (t, t))
    try:
        eeb.main(["--gc", "--older-than-days", "0"])
        assert d.is_dir()
        assert "tree not fully readable" in capsys.readouterr().err
    finally:
        sub.chmod(0o755)


def test_gc_dry_run_reports_and_deletes_nothing(repo, root, cap, capsys, wt_parents):
    nm = _mk_nometa(root, wt_parents[0] / "echo-T-4294", _OLD)
    orphan = _mk_target(root, "gone-111111111111", str(wt_parents[0] / "gone"), 0)
    assert eeb.main(["--gc", "--older-than-days", "0", "--dry-run"]) == 0
    assert nm.is_dir() and orphan.is_dir()
    err = capsys.readouterr().err
    assert f"gc: would reap {nm.name} (no meta; worktree gone)" in err
    assert f"gc: would remove {nm.name}" in err and f"gc: would remove {orphan.name}" in err
    assert "2 dir(s)" in err and "nothing deleted" in err
    assert eeb.main(["--gc", "--older-than-days", "0"]) == 0
    assert not nm.exists() and not orphan.exists()
