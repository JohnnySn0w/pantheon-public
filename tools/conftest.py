"""Suite-wide safety net: no test may fire a real Telegram notification.

Several tools modules read the Telegram bot token from a FIXED file
(`~/.claude/channels/telegram/.env`) via their own module-level `TELEGRAM_ENV`
constant, with no env override. If a test calls a send/notify path without
mocking the transmit primitive — or its patch *misses* because of the
run-as-script (`import inbox_consumer`) vs run-as-package
(`import tools.inbox_consumer`) dual-module-identity problem — the real token
gets read and the operator gets pinged. (That is exactly what spammed the operator during
the T-0856 suite runs.)

This autouse fixture redirects every sender module's `TELEGRAM_ENV` to a
nonexistent path, so `read_telegram_token()` returns None ("NOT sent") and the
real token is never read. Tests that legitimately exercise the send path patch
`TELEGRAM_ENV` themselves *inside* the test body (after this fixture runs), and
that inner patch wins — so they continue to work. (T-0856.)
"""

import atexit
import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

import pytest

# T-6118/T-6144: snapshot the running account's REAL Chronicle, persona, and sender-ledger
# state BEFORE anything below redirects the environment; pytest_sessionfinish (bottom of this
# file) re-checks it. Skipped in an xdist worker (the controller owns the check; workers share
# the same real roots).
_REAL_STATE_BASELINE = None
if not os.environ.get("PYTEST_XDIST_WORKER") and os.environ.get("PANTHEON_REAL_STATE_TRIPWIRE") != "off":
    try:
        from tools import _real_state_tripwire as _tripwire

        _REAL_STATE_BASELINE = _tripwire.take_snapshot()
    except Exception as _exc:  # noqa: BLE001 -- a broken guard must not take the suite down
        warnings.warn(f"T-6118 real-state tripwire disabled: {_exc!r}")


# T-3753: pytest-xdist (via execnet) cannot serialize pathlib.PosixPath,
# datetime.datetime, or Enum subclasses by default — execnet's _Serializer
# dispatch table only carries str, int, bytes, tuple, list, dict, frozenset,
# and a few others, and there is no MRO fallback. When a subTest emits such
# an object into pytest's marshaled failure/status stream, execnet raises
# DumpError("can't serialize <type>") and the test appears to fail regardless
# of whether the assertion itself passed.
#
# Register save_ methods for the specific types the tools/ suite uses. Keeping
# the list specific (not a catch-all fallback) avoids masking legitimate
# marshaling errors that would otherwise surface real regressions. Add types
# here when a new one bites.
#
# Executed at conftest import time so pytest-xdist sees the patch before any
# test runs. Class-level patch matches execnet's dispatch shape (getattr(class,
# methodname) at execnet/gateway_base.py:1665-1670).
try:
    import datetime as _dt
    import enum as _enum
    import pathlib as _pathlib
    from execnet.gateway_base import _Serializer as _ExecnetSerializer

    def _save_str_repr(self, obj):
        _ExecnetSerializer.save_str(self, str(obj))

    def _save_enum_by_value(self, obj):
        _ExecnetSerializer.save_str(self, f"<{type(obj).__name__}.{obj.name}>")

    # pathlib types
    for _cls in (_pathlib.PosixPath, _pathlib.WindowsPath,
                 _pathlib.PurePosixPath, _pathlib.PureWindowsPath):
        _name = "save_" + _cls.__name__
        if not hasattr(_ExecnetSerializer, _name):
            setattr(_ExecnetSerializer, _name, _save_str_repr)

    # datetime types
    for _cls in (_dt.datetime, _dt.date, _dt.time, _dt.timedelta):
        _name = "save_" + _cls.__name__
        if not hasattr(_ExecnetSerializer, _name):
            setattr(_ExecnetSerializer, _name, _save_str_repr)

    # enum types — hook _save itself for enum.Enum subclasses (they have
    # dynamic __name__ per subclass, so we cannot enumerate save_ methods).
    _original_save = _ExecnetSerializer._save

    def _save_with_enum_hook(self, obj):
        if isinstance(obj, _enum.Enum):
            _save_enum_by_value(self, obj)
            return
        _original_save(self, obj)

    _ExecnetSerializer._save = _save_with_enum_hook
except ImportError:
    pass


# T-4703: a session-wide PANTHEON_STATE_DIR, set at conftest IMPORT. pytest imports
# conftest before collecting test modules, so modules that read the variable at their
# own import (ci_runner_disk_monitor.STATE_PATH, activity_monitor_drift_check.STATE_DIR)
# resolve here instead of to the seat's real ~/.local/state/pantheon. The per-test
# _isolate_state_dir fixture (T-4697) still overrides it for call-time readers.
_SESSION_STATE_DIR = tempfile.TemporaryDirectory(prefix="pt-state-")
atexit.register(_SESSION_STATE_DIR.cleanup)
os.environ["PANTHEON_STATE_DIR"] = _SESSION_STATE_DIR.name

# T-6118: the two narrower overrides OUTRANK PANTHEON_STATE_DIR in every resolver
# (runtime_invocation_receipt._state_root, session_chronicle.persona_state_root,
# chronicle._chronicle_root, harness_state), and a live seat's own environment carries them
# (artisan: PANTHEON_PERSONA_STATE_DIR=/home/suborch2/.local/state/pantheon/personas), so setting
# only PANTHEON_STATE_DIR left every state_root=None path pointed at the REAL personas dir.
# Drop them: the resolvers then derive personas/ and chronicle/ from the redirected state dir,
# exactly as in CI, where neither is set. (Setting them to tmp paths instead would make a test
# that sets its own PANTHEON_STATE_DIR trip harness_state's "the two disagree" refusal.)
_SEAT_STATE_OVERRIDES = ("PANTHEON_PERSONA_STATE_DIR", "PANTHEON_CHRONICLE_ROOT")
for _name in _SEAT_STATE_OVERRIDES:
    os.environ.pop(_name, None)

# T-6118 PR-2: the memex export tree (/var/lib/pantheon/chronicle-export) is a REAL host path that
# session-end hardlinks into. Point every resolver at a deliberately ABSENT session-tmp path at import
# (export-unprovisioned: non-fatal, writes nothing), and _isolate_state_dir re-pins a per-test one.
# The tripwire (tools/_real_state_tripwire.py) fails the run if the real tree changes anyway.
os.environ["PANTHEON_CHRONICLE_EXPORT_ROOT"] = str(Path(_SESSION_STATE_DIR.name) / "chronicle-export-absent")

# Canonical (package) identities of the modules that read the real bot token.
# Repo-root is the canonical pytest invocation, where only these package
# identities exist (no bare `inbox_consumer` second identity).
_SENDER_MODULES = (
    "tools.context_fill_monitor",
    "tools.inbox_consumer",
)


@pytest.fixture(scope="session")
def _private_tmux_tmpdir():
    """One private TMUX_TMPDIR per test process (per xdist worker). It is SHORT on
    purpose: tmux puts its socket at $TMUX_TMPDIR/tmux-<uid>/default, unix socket paths
    are limited to ~108 bytes, and pytest's basetemp in CI is deep
    (/opt/actions-runner-*/_work/.../pytest-basetemp/popen-gwN/...). Same /tmp-mkdtemp
    choice as test_mailbox_wake's T-4320 test."""
    d = tempfile.mkdtemp(prefix="pt-tmux-", dir="/tmp")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolate_tmux_server(monkeypatch, _private_tmux_tmpdir):
    """T-4433: no test may reach the tmux server of the seat that runs the suite.

    Seats run pytest from their own accounts, often inside their live pane. There, $TMUX
    names the agent's real server, and without it tmux still defaults to the account's
    real socket. A test that forgets a stub then types into (send-keys) or reads
    (capture-pane) the running agent's pane: test_mailbox_wake T-4320 used to leave
    wake-prompt residue there, and a keep_going test nearly did on 2026-09-30 (T-4432).
    With TMUX unset and TMUX_TMPDIR private, every tmux call from a test, or from a
    shell suite it spawns (they inherit os.environ), resolves to a private server.

    Not covered: `sudo -u <other> tmux ...` resets the environment and still reaches
    that account's server. That path is read-mostly (capture-pane), and a follow-up if
    ever wanted."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", _private_tmux_tmpdir)
    yield


@pytest.fixture(autouse=True)
def _isolate_mute_registry(monkeypatch, tmp_path):
    """T-4668: notify_policy now falls back to the LIVE health-plane mute registry when
    PANTHEON_MUTE_REGISTRY is unset, so a test routing an alert for a muted seat (artisan,
    mason) would be silenced by real fleet state. Point it at a file that never exists;
    a test that wants a registry sets its own."""
    monkeypatch.setenv("PANTHEON_MUTE_REGISTRY", str(tmp_path / "no-notify_mute.json"))
    yield


@pytest.fixture(autouse=True)
def _isolate_maintenance_registry(monkeypatch, tmp_path):
    """T-5282: persona_bringup consume now skips a seat in seat-maintenance, read from the
    SHARED registry (/var/lib/pantheon/seat-maintenance/) when no env override is set, so a
    test consuming as a seat that is armed in the live fleet (mason) would skip on real
    state. Point it at a file that never exists; a test that wants an entry sets its own."""
    monkeypatch.setenv("PANTHEON_MAINTENANCE_REGISTRY", str(tmp_path / "no-seat_maintenance.json"))
    yield


@pytest.fixture(autouse=True)
def _isolate_seat_state_reads(request, monkeypatch):
    """ORG-106 L4b (T-5527): the monitors read each seat's state through
    seat_state.safe_effective_for, which for another account shells out through the
    privilege helper. No test may reach that. Default it to None ("unknown", read as
    active / not held); a monitor test that wants a state patches the same name inside
    the test body (that runs after this fixture, same contract as the fixtures above).
    test_seat_state.py tests the real helper, so it is exempt."""
    if request.module.__name__.rsplit(".", 1)[-1] == "test_seat_state":
        yield
        return
    from tools import seat_state

    monkeypatch.setattr(seat_state, "safe_effective_for", lambda persona, **kw: None)
    yield


@pytest.fixture(autouse=True)
def _isolate_instance_manifest(monkeypatch):
    """T-5457: a seat session launched before the runtime-controller unset carries
    PANTHEON_INSTANCE_MANIFEST=<live clone>/instance.yaml, so a worktree run would read
    the LIVE manifest. Drop it; readers then default to the checkout under test. A test
    that wants a manifest path sets its own."""
    monkeypatch.delenv("PANTHEON_INSTANCE_MANIFEST", raising=False)
    yield


@pytest.fixture(autouse=True)
def _neutralize_telegram_token(monkeypatch, tmp_path):
    """Point every sender module's TELEGRAM_ENV at a nonexistent file."""
    nonexistent = tmp_path / "no-telegram-token.env"  # never created
    for mod in _SENDER_MODULES:
        # raising=False: a module may not be importable in a given subset run;
        # skip it rather than error the whole suite.
        try:
            monkeypatch.setattr(f"{mod}.TELEGRAM_ENV", nonexistent, raising=False)
        except (ImportError, AttributeError):
            pass
    # ticket_lint honors this env override for its own token path.
    monkeypatch.setenv("PANTHEON_TELEGRAM_ENV", str(nonexistent))
    yield


@pytest.fixture(autouse=True)
def _isolate_git_mutation_lockfile(monkeypatch, tmp_path):
    """Point the git-mutation lock at a per-test tmp file so no test takes the
    REAL repo lockfile — an isolation leak in the main checkout, and an outright
    FAILURE in a linked worktree.

    Any test that calls a code path taking `_git_utils.git_mutation_lock`
    (inbox_consumer._stamp_auto_delivered, ticket.py commits, …) without setting
    its own lockfile resolves `_git_mutation_lockfile()` → `_git_common_dir()`.
    When subprocess is mocked (so `git rev-parse --git-common-dir` can't run) it
    falls back to `REPO_ROOT/.git`. In a linked worktree that path is a *file*,
    and `_open_lockfile_world_writable`'s `path.parent.mkdir(...)` — parent being
    `.git` itself when the filename has no dir component under it — raises
    `FileExistsError`, so every such test fails serially and under `-n auto`
    (T-4040: 9 test_inbox_consumer failures in every worktree smoke).

    Redirecting the env override to a per-test `tmp_path` file makes the whole
    suite worktree-safe and, as a P1 backstop, stops any future test from
    silently reintroducing the leak. Tests that exercise real lock semantics set
    `PANTHEON_GIT_MUTATION_LOCKFILE` themselves in `setUp` / the test body; that
    runs AFTER this autouse fixture, so their value wins (same override-inside-
    the-test contract as `_neutralize_telegram_token` above).
    """
    monkeypatch.setenv(
        "PANTHEON_GIT_MUTATION_LOCKFILE", str(tmp_path / "git-mutation.lock")
    )
    yield


@pytest.fixture(autouse=True)
def _isolate_state_dir(monkeypatch, tmp_path):
    """T-4697: point PANTHEON_STATE_DIR at a per-test dir that starts empty, so no test
    reads the seat's REAL ~/.local/state/pantheon. Since 2026-10-07 orchestrator's seat holds a
    real manifest pin, which arms `_git_utils._resolve_write_mirror` and adds a
    `git remote get-url` call to every mocked push path. TestFullLifecycleGitCommands
    failed on that seat only, and passed in CI, which has no seat state. Tests that
    want state set PANTHEON_STATE_DIR themselves inside the test; that runs after
    this fixture, so their value wins (same contract as the fixtures above).
    """
    monkeypatch.setenv("PANTHEON_STATE_DIR", str(tmp_path / "pantheon-state"))
    # T-6118: see _SEAT_STATE_OVERRIDES above; a test that wants one sets it inside its body.
    for name in _SEAT_STATE_OVERRIDES:
        monkeypatch.delenv(name, raising=False)
    # T-6118 PR-2: a per-test export root that does not exist (tests that export create their own).
    monkeypatch.setenv("PANTHEON_CHRONICLE_EXPORT_ROOT", str(tmp_path / "chronicle-export-unprovisioned"))
    # T-6118: the import-time home constants (runtime_controller.DEFAULT_STATE_ROOT, chronicle.CHRONICLE_ROOT, ...)
    # are deliberately NOT repointed: the resolvers' fallbacks must keep agreeing
    # (test_t5028_mr2 drift guard), and a test that clears os.environ is caught by the session tripwire.
    yield


def _stub_session_chronicle_runner(argv, *, timeout):
    """T-5016: the suite-default runner for runtime_controller's session-end hooks."""
    del argv, timeout
    return {"status": "stubbed"}


@pytest.fixture(autouse=True)
def _isolate_session_chronicle(monkeypatch, tmp_path):
    """T-5016: no test archives, stages or re-arms a REAL seat's session.

    runtime_controller now runs tools/session_chronicle.py before every pane
    (re)start and after every supervised session end. That duty renames the
    seat's pane-timed log, closes its tmux tap and may page Recovery, so the suite
    defaults it off on three independent layers:
      * PANTHEON_SESSION_CHRONICLE_HOOKS=off: both controller hooks return at once;
      * the controller's runner seam is replaced by a stub (survives a test that
        clears os.environ, which would erase the env layer);
      * PANE_LOG_DIR is a per-test dir and PANTHEON_SESSION_CHRONICLE_ALERT=off,
        for shell suites and subprocesses that run the duty CLI for real.
    Composes with _isolate_state_dir (PANTHEON_STATE_DIR) and _real_state_guard,
    and does not touch either. Tests that exercise the hooks set the env / seam
    themselves inside the test body, which runs after this fixture."""
    import sys

    monkeypatch.setenv("PANTHEON_SESSION_CHRONICLE_HOOKS", "off")
    monkeypatch.setenv("PANTHEON_SESSION_CHRONICLE_ALERT", "off")
    monkeypatch.setenv("PANE_LOG_DIR", str(tmp_path / "pane-logs"))
    for name in ("tools.runtime_controller", "runtime_controller"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "_SESSION_CHRONICLE_RUNNER"):
            monkeypatch.setattr(module, "_SESSION_CHRONICLE_RUNNER", _stub_session_chronicle_runner)
    yield


@pytest.fixture(autouse=True)
def _disarm_read_failover(monkeypatch):
    """Keep every test off the ORG-046 read-failover path (T-1998) unless it
    opts in.

    `_git_utils.pull_repo_or_warn` refreshes the real manifest pin and may fetch
    from the mirror when PANTHEON_READ_FAILOVER=1 and a pin exists. The many
    tests that patch only `tools._git_utils.subprocess` would then reach the
    live pin store and real git through read_failover's own runner (codex
    T-1998 review). The env switch is checked before anything else, so clearing
    it makes the path fully inert even when an armed seat's env leaks into the
    test process. tools/test_read_failover.py sets it (with a tmp
    PANTHEON_STATE_DIR) inside the test, so its value wins (same contract as the
    fixtures above).
    """
    monkeypatch.delenv("PANTHEON_READ_FAILOVER", raising=False)
    yield


class TicketTreeDriftWarning(UserWarning):
    """A test changed the REAL ticket tree (T-4167)."""


_REAL_TICKET_TREE = Path(__file__).resolve().parent.parent / "tickets"
_ticket_tree_watch = None


@pytest.fixture(autouse=True)
def _real_ticket_tree_tripwire(request):
    """CI-only: flag any test that adds, removes or moves an entry in the REAL
    tickets/ tree, or fetches/pulls/moves HEAD in the real checkout (T-4167).

    CI's pytest-xdist -n auto shares ONE checkout, so a test that moves a real
    ticket (a fake `git mv` stub fed an absolute path, or real git run against the
    import-time REPO_ROOT) races every live-tree reader -- the T-4165 flake moved
    tickets/active/T-4137.md mid-run, most likely via a real `git pull` in the
    checkout, so the checkout's fetch/ref markers are watched too. CI-only because
    a local run legitimately races the owning agent's own ticket commits (and its
    automation's pulls). A concurrent worker can trip
    another worker's test, so the mover is the test that is flagged on every run.
    A HARD drift (tree entries, HEAD, ORIG_HEAD or the branch ref moved) FAILS the
    test; a fetch-only drift (FETCH_HEAD alone, nothing a reader sees) only warns.
    Fail became the default once CI run 36481372538 showed 0 HARD drift on py3.11 and
    py3.12 after the Layer-3 real-repo-sync fix (a6649a37c).
    PANTHEON_TICKET_TRIPWIRE=warn downgrades HARD to a warning (escape hatch).
    """
    global _ticket_tree_watch
    if os.environ.get("GITHUB_ACTIONS") != "true" or not _REAL_TICKET_TREE.is_dir():
        yield
        return
    if _ticket_tree_watch is None:
        from tools._test_fixtures import TicketTreeWatch
        _ticket_tree_watch = TicketTreeWatch(
            _REAL_TICKET_TREE, git_dir=_REAL_TICKET_TREE.parent / ".git"
        )
    token = _ticket_tree_watch.begin()
    yield
    drift = _ticket_tree_watch.drift(token)
    if drift is None:
        return
    removed, added, git_changed = drift
    # A fetch alone (FETCH_HEAD only) moves nothing a reader sees; a changed tree, HEAD,
    # ORIG_HEAD or branch ref is a real move of the checkout.
    kind = "fetch-only" if (not removed and not added and git_changed == ["FETCH_HEAD"]) else "HARD"
    msg = (
        f"T-4167 ticket-tree drift ({kind}) during {request.node.nodeid} "
        f"[{os.environ.get('PYTEST_XDIST_WORKER', 'main')}]: "
        f"removed={removed[:5]} added={added[:5]} git={git_changed}"
    )
    if kind == "HARD" and os.environ.get("PANTHEON_TICKET_TRIPWIRE") != "warn":
        pytest.fail(msg)
    warnings.warn(TicketTreeDriftWarning(msg))


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "allows_real_git: T-4934 opt-out; test genuinely needs real mutating git")
    config.addinivalue_line(
        "markers", "allows_real_mailbox: T-4934 opt-out; test genuinely needs the real mailbox hub")
    config.addinivalue_line(
        "markers", "nested_namespace: T-5915; test builds its own user/pid namespaces and fails "
                   "inside an outer one. tools/soak_leg.py runs these un-namespaced in leg B and "
                   "everything else namespaced in leg A; plain CI runs them all")


@pytest.fixture(autouse=True)
def _real_state_guard(request, monkeypatch, tmp_path_factory):
    """T-4934: refuse mutating git against non-temp repos and writes into the LIVE
    mailbox hub for the duration of each test. See tools/_test_real_state_guard.py."""
    from tools import _test_real_state_guard as g
    roots = g.allowed_roots(extra=[tmp_path_factory.getbasetemp()])
    g.install(
        monkeypatch, roots,
        git=request.node.get_closest_marker("allows_real_git") is None,
        mailbox=request.node.get_closest_marker("allows_real_mailbox") is None,
    )
    yield


def pytest_sessionfinish(session, exitstatus):
    """T-6118/T-6144: fail if the run changed REAL Chronicle/persona/sender state.

    The roots are the running account's (passwd home). The seat's own persona subtree is
    exempt, except Chronicle session records with a /tmp handoff (the fixture-leak signature);
    see tools/_real_state_tripwire.py. PANTHEON_REAL_STATE_TRIPWIRE=warn reports without
    failing, =off disables (checked again here, at call time). Controller process only.

    Contract (pinned by tools/test_t6118_sessionfinish_contract.py): the only status change is
    exactly 0 -> TESTS_FAILED; any other status is left alone. A broken guard fails open: it
    prints and never raises."""
    try:
        if os.environ.get("PANTHEON_REAL_STATE_TRIPWIRE") == "off":
            return
        if _REAL_STATE_BASELINE is None or hasattr(session.config, "workerinput"):
            return
        from tools import _real_state_tripwire as tripwire

        changes = tripwire.diff(_REAL_STATE_BASELINE, tripwire.take_snapshot())
        if not changes:
            return
        shown = "\n".join(f"  {line}" for line in changes[:40])
        more = f"\n  ... and {len(changes) - 40} more" if len(changes) > 40 else ""
        print(
            f"\nT-6118 REAL STATE TOUCHED: this test run changed {len(changes)} path(s) under the "
            f"account's real Chronicle/personas/sender state:\n{shown}{more}\n"
            "A test must isolate these (tools/conftest.py, tools/_seat_state_isolation.py).",
            file=sys.stderr,
        )
        if os.environ.get("PANTHEON_REAL_STATE_TRIPWIRE") != "warn" and session.exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
    except Exception as exc:  # noqa: BLE001 -- a broken guard fails open, never errors the session
        try:
            print(f"T-6118 real-state tripwire errored (ignored): {exc!r}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
