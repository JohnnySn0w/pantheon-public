"""Shared hermetic fixtures for the ORG-076 BT1 tests (T-4707). Test-support only.

* ``RepoFixture`` -- throwaway bare origin + working clone seeded with a minimal
  ``tickets/`` tree (shape copied from tools/test_delivery_rebase_controller.py's
  ``_RepoFixture``). Lock files are isolated so tests never contend with live fleet
  locks (``PANTHEON_TICKET_ALLOC_LOCKFILE``, ``PANTHEON_GIT_MUTATION_LOCKFILE``).
* ``ticket_cli_filer`` -- a ``filer`` for ``episode_store.Ports`` that runs the REAL
  ``tools/ticket.py file --no-push`` in a child process with ``REPO_ROOT`` pinned to the
  temp clone (ticket.py derives REPO_ROOT from ``__file__``, so the child patches it the
  same way tools/test_ticket.py does). ``EPISODE_TEST_TICKET_KILL`` makes the CHILD die
  at a named point inside ticket.py's commit pipeline:
    before_add   file written, nothing staged           (untracked file left behind)
    after_add    ``git add`` done, ``git commit`` not run (STAGED file left behind)
    stale_lock   staged + a stale ``.git/index.lock``      (killed ``git commit`` residue)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent

_CHILD = r"""
import os, sys, subprocess
from pathlib import Path
import tools.ticket as t
import tools._git_utils as g
repo = Path(sys.argv[1])
t.REPO_ROOT = repo
t.TICKETS_ROOT = repo / "tickets"
g.REPO_ROOT = repo
# T-4728: _git's ``cwd=REPO_ROOT`` default is bound at def time, so patching REPO_ROOT does
# not move it: bare ``_git("fetch", ...)`` calls (ID allocation) hit the REAL checkout and its
# real origin, racing other children on refs/remotes/origin/main ("cannot lock ref"). Rebind
# the default to the temp clone in every module that holds a reference.
_real_git = g._git
def _git(*a, cwd=repo):
    return _real_git(*a, cwd=cwd)
g._git = _git
t._git = _git
if os.environ.get("EPISODE_TEST_PROBE_GIT_CWD"):
    print(t._git("rev-parse", "--show-toplevel").stdout.strip())
    sys.exit(0)
mark = os.environ.get("EPISODE_TEST_CORPUS_MARK", "")
if mark:
    import tools._episode_corpus as ec
    _orig_lookup = ec.lookup_episode
    def _marked(*a, **kw):
        Path(mark).write_text("called")
        return _orig_lookup(*a, **kw)
    ec.lookup_episode = _marked
kill = os.environ.get("EPISODE_TEST_TICKET_KILL", "")
if kill:
    orig = t._stage_and_commit_scoped
    def patched(affected_paths, commit_message, **kw):
        if kill == "before_add":
            os._exit(137)
        rels = [str(p.relative_to(repo)) for p in affected_paths if p.exists()]
        subprocess.run(["git", "add", "--", *rels], cwd=repo, check=True)
        if kill == "stale_lock":
            (repo / ".git" / "index.lock").write_text("")
        os._exit(137)
    t._stage_and_commit_scoped = patched
sys.exit(t.main(sys.argv[2:]))
"""


def git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


class RepoFixture:
    """bare origin + work clone. Use as a context manager."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ep-fixture-")
        self.root = Path(self._tmp.name)
        self.origin = self.root / "origin.git"
        self.repo = self.root / "work"
        self.locks = self.root / "locks"
        self.locks.mkdir()
        git("init", "--bare", str(self.origin), cwd=self.root)
        git("init", str(self.repo), cwd=self.root)
        for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
            git("config", k, v, cwd=self.repo)
        for d in ("inbox", "active", "done"):
            (self.repo / "tickets" / d).mkdir(parents=True)
            (self.repo / "tickets" / d / ".gitkeep").write_text("")
        (self.repo / "tickets" / "README.md").write_text(
            "# Tickets\n\n## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n|---|---|---|---|---|\n\n"
            "## Related\n\n- Link\n", encoding="utf-8")
        git("add", "-A", cwd=self.repo)
        git("commit", "-m", "base", cwd=self.repo)
        git("branch", "-M", "main", cwd=self.repo)
        git("remote", "add", "origin", str(self.origin), cwd=self.repo)
        git("push", "-u", "origin", "main", cwd=self.repo)
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.origin)

    def __enter__(self) -> "RepoFixture":
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        self._tmp.cleanup()

    @property
    def env(self) -> dict[str, str]:
        e = dict(os.environ)
        e.update({
            "PANTHEON_TICKET_ALLOC_LOCKFILE": str(self.locks / "alloc.lock"),
            "PANTHEON_GIT_MUTATION_LOCKFILE": str(self.locks / "gitmut.lock"),
            "PANTHEON_ACTOR": "ep-test",
            "PYTHONPATH": str(SRC_ROOT),
        })
        return e

    # -- helpers -------------------------------------------------------------
    def ticket_cli(self, argv: list[str], *, kill: str = "", env_extra: dict | None = None,
                   timeout: int = 120) -> subprocess.CompletedProcess:
        env = self.env
        if kill:
            env["EPISODE_TEST_TICKET_KILL"] = kill
        env.update(env_extra or {})
        return subprocess.run([sys.executable, "-c", _CHILD, str(self.repo), *argv],
                              cwd=self.repo, env={**env, "PYTHONPATH": str(SRC_ROOT)},
                              capture_output=True, text=True, timeout=timeout)

    def origin_log_count(self, needle: str) -> int:
        """Commits on the bare origin's main that INTRODUCED ``needle`` under tickets/."""
        r = git("log", "--format=%H", "-S", needle, "main", "--", "tickets/", cwd=self.origin)
        return len([ln for ln in r.stdout.splitlines() if ln.strip()])

    def origin_ticket_files(self) -> list[str]:
        r = git("ls-tree", "-r", "--name-only", "main", "tickets/", cwd=self.origin)
        return [p for p in r.stdout.splitlines() if p.endswith(".md") and "README" not in p]

    def peer_advance(self, name: str = "peer") -> None:
        """A peer clone pushes an unrelated commit to origin/main."""
        peer = self.root / f"peer-{name}"
        git("clone", str(self.origin), str(peer), cwd=self.root)
        for k, v in (("user.email", "p@p"), ("user.name", "p"), ("commit.gpgsign", "false")):
            git("config", k, v, cwd=peer)
        (peer / f"{name}.txt").write_text("x\n")
        git("add", "-A", cwd=peer)
        git("commit", "-m", f"peer {name}", cwd=peer)
        git("push", "origin", "HEAD:main", cwd=peer)


def ticket_cli_filer(fx: RepoFixture, *, kill: str = ""):
    """A ``filer`` callable (``argv -> CompletedProcess``-like) bound to a fixture."""

    def _filer(argv: list[str]) -> subprocess.CompletedProcess:
        return fx.ticket_cli(argv, kill=kill)

    return _filer


# --------------------------------------------------------------------------- #
# episode_store wiring helpers (imported lazily so this module stays import-light)
# --------------------------------------------------------------------------- #
def hermetic_pusher(repo: Path):
    """The production push step (push_with_retry attempts=1) with the ORG-101 mirror leg
    explicitly disabled, so the test never reads a real manifest or reaches any mirror."""
    from tools import _git_utils
    from tools.episode_store import PushResult

    ok, detail = _git_utils.push_with_retry(repo, refspec="HEAD:main", attempts=1, mirror_url=None)
    nonff = (not ok) and any(m in detail.lower() for m in _git_utils._NON_FF_MARKERS)
    return PushResult(ok=ok, nonff=nonff, detail=detail)


class FakeClock:
    """Deterministic, strictly advancing clock (1s per call) + deterministic ids."""

    def __init__(self, start: str = "2026-10-08T01:00:00+00:00", step: int = 1):
        from datetime import datetime, timedelta
        self._t = datetime.fromisoformat(start)
        self._step = timedelta(seconds=step)
        self._n = 0

    def __call__(self):
        self._t = self._t + self._step
        return self._t

    def rand(self) -> str:
        self._n += 1
        return f"{self._n:08x}"


def make_ports(fx: RepoFixture, *, clock: "FakeClock | None" = None, pages: "list | None" = None,
               kill_at: str = "", **over):
    from tools.episode_store import Ports

    clock = clock or FakeClock()
    pages = pages if pages is not None else []

    def kill_hook(point: str) -> None:
        if kill_at and point == kill_at:
            os._exit(137)

    kw = dict(repo=fx.repo, filer=lambda argv: fx.ticket_cli(argv), pusher=hermetic_pusher,
              page=lambda s, b: pages.append((s, b)), clock=clock, rand=clock.rand,
              kill_hook=kill_hook, lock_timeout=20.0, index_lock_stale_secs=0.0)
    kw.update(over)
    return Ports(**kw)


def make_spec(**over):
    from tools.episode_store import DetectorSpec

    kw = dict(detector="quota-probe", domain_key="quota-probe:specialist", subsystem="quota",
              requester="recovery", n=3, m=2, harness="claude", account="claude")
    kw.update(over)
    return DetectorSpec(**kw)
