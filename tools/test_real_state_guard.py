"""T-4934: the suite-wide real-state guard (tools/_test_real_state_guard.py) fires on
real-repo git mutation and real-hub mailbox writes, and lets temp fixtures through.

SAFETY: no test here targets the live hub or a real repo, even when the guard under test
is broken. "Real" repos are stand-ins built under tmp_path; the temp-roots set is passed
(or installed) as a narrow subdir, so the tests are location-independent (they pass from
/tmp worktrees too). A mutating git call that slipped through would hit a remote-less tmp
repo and fail harmlessly.
"""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from tools import _test_real_state_guard as guard

_ID = ["-c", "user.name=t", "-c", "user.email=t@t"]


def _git(*args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


@pytest.fixture
def world(tmp_path):
    """real = stand-in 'real repo' OUTSIDE the allowed roots; allowed/ = the stand-in
    temp root. wt = a worktree of real placed INSIDE allowed/ (the /tmp worktree shape);
    clone = a genuine independent repo inside allowed/."""
    real = tmp_path / "real"
    real.mkdir()
    _git("init", "-q", cwd=real)
    _git(*_ID, "commit", "--allow-empty", "-q", "-m", "x", cwd=real)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    wt = allowed / "wt"
    _git("worktree", "add", "-q", "--detach", str(wt), cwd=real)
    clone = allowed / "clone"
    clone.mkdir()
    _git("init", "-q", cwd=clone)
    return type("W", (), dict(real=real, allowed=str(allowed), wt=wt, clone=clone,
                              roots=[str(allowed)]))


# ---- pure predicate -------------------------------------------------------
MUTATING = [
    ["git", "pull"], ["git", "push", "origin", "main"], ["git", "rebase", "x"],
    ["git", "merge", "x"], ["git", "commit", "-m", "x"], ["git", "reset", "--hard"],
    ["git", "reset", "--soft", "HEAD~1"], ["git", "reset"], ["git", "fetch", "origin", "a:b"],
    ["/usr/bin/git", "-c", "a=b", "pull"], ["git", "cherry-pick", "x"],
    ["git", "worktree", "add", "x"], ["git", "worktree", "remove", "x"],
    ["git", "stash"], ["git", "stash", "pop"], ["git", "branch", "-D", "x"],
    ["git", "branch", "-m", "a", "b"], ["git", "branch", "newbranch"],
    ["git", "tag", "v1"], ["git", "tag", "-d", "v1"], ["git", "update-ref", "a", "b"],
    ["git", "clean", "-fd"], ["git", "gc"], ["git", "checkout", "x"], ["git", "switch", "x"],
    ["git", "restore", "f"], ["git", "add", "f"], ["git", "rm", "f"],
]


@pytest.mark.parametrize("argv", MUTATING)
def test_predicate_refuses_mutations_in_worktree_of_outside_repo(world, argv):
    with pytest.raises(guard.RealStateAccessError, match="T-4934"):
        guard.check_git_call(argv, cwd=str(world.wt), allowed_roots=world.roots)


@pytest.mark.parametrize("argv", [
    ["git", "status"], ["git", "log", "-1"], ["git", "fetch", "origin", "main"],
    ["git", "diff"], ["echo", "pull"], ["git", "worktree", "list"], ["git", "stash", "list"],
    ["git", "branch"], ["git", "branch", "--show-current"], ["git", "branch", "--list"],
    ["git", "tag"], ["git", "tag", "-l"], ["git", "rev-parse", "HEAD"],
])
def test_predicate_allows_readonly_and_nongit(world, argv):
    guard.check_git_call(argv, cwd=str(world.wt), allowed_roots=world.roots)


@pytest.mark.parametrize("argv", MUTATING)
def test_predicate_allows_mutations_in_genuine_temp_repo(world, argv):
    guard.check_git_call(argv, cwd=str(world.clone), allowed_roots=world.roots)


def test_dash_C_is_the_target(world):
    guard.check_git_call(["git", "-C", str(world.clone), "pull"], cwd=str(world.wt),
                         allowed_roots=world.roots)
    with pytest.raises(guard.RealStateAccessError):
        guard.check_git_call(["git", "-C", str(world.wt), "pull"], cwd=str(world.clone),
                             allowed_roots=world.roots)
    with pytest.raises(guard.RealStateAccessError):  # relative -C joined onto cwd
        guard.check_git_call(["git", "-C", "wt", "pull"], cwd=world.allowed,
                             allowed_roots=world.roots)


def test_git_dir_and_work_tree_are_the_target(world):
    gd = str(world.real / ".git")
    for argv in (["git", f"--git-dir={gd}", "--work-tree", str(world.clone), "pull"],
                 ["git", "--git-dir", gd, "pull"]):
        with pytest.raises(guard.RealStateAccessError):
            guard.check_git_call(argv, cwd=str(world.clone), allowed_roots=world.roots)


def test_probe_failure_fails_closed(world):
    bad = Path(world.allowed) / "broken"
    bad.mkdir()
    (bad / ".git").write_text("gitdir: /nonexistent-t4934\n")  # unusable gitfile
    with pytest.raises(guard.RealStateAccessError):
        guard.check_git_call(["git", "pull"], cwd=str(bad), allowed_roots=world.roots)
    with pytest.raises(guard.RealStateAccessError):  # target dir does not exist
        guard.check_git_call(["git", "pull"], cwd=world.allowed + "/missing",
                             allowed_roots=world.roots)


def test_plain_non_repo_dir_under_temp_is_allowed(world):
    plain = Path(world.allowed) / "plain"
    plain.mkdir()
    guard.check_git_call(["git", "pull"], cwd=str(plain), allowed_roots=world.roots)


def test_non_repo_at_filesystem_boundary_is_allowed(world, monkeypatch):
    """CI runner /tmp is a tmpfs mount: git stops discovery at the boundary and says
    '(or any parent up to mount point /tmp)'. That is still 'not a repo', not a probe
    failure (PR #921 CI run 38352793259)."""
    plain = Path(world.allowed) / "plain"
    plain.mkdir()

    class BoundaryPopen:
        returncode = 128

        def __init__(self, *a, **k):
            pass

        def communicate(self, timeout=None):
            return "", ("fatal: not a git repository (or any parent up to mount point /tmp)\n"
                        "Stopping at filesystem boundary (GIT_DISCOVERY_ACROSS_FILESYSTEM "
                        "not set).\n")

    monkeypatch.setattr(guard, "_REAL_POPEN", BoundaryPopen)
    guard.check_git_call(["git", "pull"], cwd=str(plain), allowed_roots=world.roots)


# ---- installed guard ------------------------------------------------------
def test_installed_guard_refuses_run_call_check_output(world, monkeypatch):
    guard.install(monkeypatch, world.roots, git=True, mailbox=False)
    with pytest.raises(guard.RealStateAccessError, match="allows_real_git"):
        subprocess.run(["git", "pull"], cwd=world.wt, capture_output=True)
    with pytest.raises(guard.RealStateAccessError):  # also proves the probe does not recurse
        subprocess.run(["git", "-C", str(world.wt), "push", "origin", "HEAD"])
    with pytest.raises(guard.RealStateAccessError):
        subprocess.check_output(["git", "pull"], cwd=world.wt)
    with pytest.raises(guard.RealStateAccessError):
        subprocess.call(["git", "rebase", "x"], cwd=world.wt)


def test_shell_true_string_compound_and_bash_c(world, monkeypatch):
    guard.install(monkeypatch, world.roots, git=True, mailbox=False)
    for cmd in ("git pull", "echo hi && git pull", "echo hi; git push origin HEAD",
                "bash -c 'git pull'"):
        with pytest.raises(guard.RealStateAccessError):
            subprocess.run(cmd, shell=True, cwd=world.wt, capture_output=True)
    with pytest.raises(guard.RealStateAccessError):
        subprocess.run(["bash", "-c", "git pull"], cwd=world.wt, capture_output=True)
    subprocess.run("git pull", shell=True, cwd=world.clone, capture_output=True)  # allowed


def test_installed_guard_allows_genuine_temp_repo(world, monkeypatch):
    guard.install(monkeypatch, world.roots, git=True, mailbox=False)
    r = subprocess.run(["git", "-C", str(world.clone), *_ID, "commit", "--allow-empty", "-q",
                        "-m", "x"], capture_output=True)
    assert r.returncode == 0


def test_popen_is_still_a_class():
    assert subprocess.Popen is not guard._REAL_POPEN  # conftest installed the guard
    assert isinstance(subprocess.Popen, type)
    assert issubclass(subprocess.Popen, guard._REAL_POPEN)


def test_default_guard_allows_tmp_path_repo(tmp_path):
    _git("init", "-q", cwd=tmp_path)
    r = subprocess.run(["git", "-C", str(tmp_path), *_ID, "commit", "--allow-empty", "-q",
                        "-m", "x"], capture_output=True)
    assert r.returncode == 0


@pytest.mark.allows_real_git
def test_marker_opts_out_of_git_guard():
    assert subprocess.Popen is guard._REAL_POPEN


# ---- mailbox --------------------------------------------------------------
# SAFETY (T-4934 incident): no test here may write to, or even attempt a delivery into,
# the real hub. Refusal tests use a FAKE hub path that does not exist and is not under a
# temp root; if the guard failed, the op would die with ENOENT/MailboxNotFound instead
# of delivering. The real hub is only ever touched by the pure (no-I/O) predicate and a
# read-only listing.
FAKE_HUB = "/nonexistent-t4934-fake-hub"


def _real_hub():
    from tools.agent_role_map import mailbox_hub
    return mailbox_hub()


def _count_new(inbox_dir):
    try:
        return len(os.listdir(inbox_dir))
    except OSError:
        return None


def test_predicate_refuses_paths_under_the_real_hub_without_io():
    hub = _real_hub()
    for p in (hub + "/orchestrator/new/x", hub + "/orchestrator", hub):
        with pytest.raises(guard.RealStateAccessError, match="allows_real_mailbox"):
            guard.check_hub_write(p, [hub], ["/tmp"], "write-open")
    guard.check_hub_write(hub + "-sibling/x", [hub], ["/tmp"], "write-open")  # not under it


def test_predicate_allows_hub_that_is_itself_temp(tmp_path):
    guard.check_hub_write(str(tmp_path / "hub" / "x"), [str(tmp_path / "hub")],
                          [str(tmp_path)], "mkdir")


def test_real_hub_untouched_by_this_module():
    # Read-only canary around the pure predicate.
    new = _real_hub() + "/orchestrator/new"
    before = _count_new(new)
    test_predicate_refuses_paths_under_the_real_hub_without_io()
    assert _count_new(new) == before


def test_fake_hub_write_primitives_refused(monkeypatch):
    monkeypatch.setenv("PANTHEON_MAILBOX_HUB", FAKE_HUB)
    for fn in (lambda: os.open(FAKE_HUB + "/orchestrator/tmp/x", os.O_WRONLY | os.O_CREAT),
               lambda: os.rename(FAKE_HUB + "/orchestrator/new/a", FAKE_HUB + "/orchestrator/cur/a"),
               lambda: os.mkdir(FAKE_HUB + "/zzz"),
               lambda: os.unlink(FAKE_HUB + "/orchestrator/new/a")):
        with pytest.raises(guard.RealStateAccessError, match="allows_real_mailbox"):
            fn()


def test_mailbox_send_into_fake_hub_refused(monkeypatch):
    from tools import mailbox
    monkeypatch.setenv("PANTHEON_MAILBOX_HUB", FAKE_HUB)
    with pytest.raises(guard.RealStateAccessError):
        mailbox.send(Path(FAKE_HUB) / "orchestrator", "guard-test", "test-harness",
                     dedupe_pending=False, provision_if_missing=True)


def test_mailbox_provision_in_fake_hub_refused(monkeypatch):
    from tools import mailbox
    monkeypatch.setenv("PANTHEON_MAILBOX_HUB", FAKE_HUB)
    with pytest.raises(guard.RealStateAccessError):
        mailbox.provision(Path(FAKE_HUB) / "orchestrator")


def test_read_only_open_in_fake_hub_not_refused(monkeypatch):
    monkeypatch.setenv("PANTHEON_MAILBOX_HUB", FAKE_HUB)
    with pytest.raises(OSError) as ei:  # reaches the OS, not the guard
        os.open(FAKE_HUB + "/no-such", os.O_RDONLY)
    assert not isinstance(ei.value, guard.RealStateAccessError)


def test_send_to_tmp_inbox_allowed(tmp_path):
    from tools import mailbox
    inbox = mailbox.provision(tmp_path / "inbox")
    assert mailbox.send(inbox, "ok", "test-harness", dedupe_pending=False)


def test_tmp_hub_override_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("PANTHEON_MAILBOX_HUB", str(tmp_path / "hub"))
    (tmp_path / "hub").mkdir()
    os.mkdir(tmp_path / "hub" / "x")


@pytest.mark.allows_real_mailbox
def test_mailbox_marker_opts_out(monkeypatch):
    monkeypatch.setenv("PANTHEON_MAILBOX_HUB", FAKE_HUB)
    with pytest.raises(OSError) as ei:  # ENOENT from the OS, not RealStateAccessError
        os.unlink(FAKE_HUB + "/zz-no-such-dir/x")
    assert not isinstance(ei.value, guard.RealStateAccessError)


# ---- builtin open() / Path.write_text into the hub (reviewer item 3) -------
@pytest.fixture
def fake_hub(monkeypatch):
    monkeypatch.setenv("PANTHEON_MAILBOX_HUB", FAKE_HUB)
    return Path(FAKE_HUB)


def test_open_write_modes_refused_read_allowed(fake_hub):
    for mode in ("w", "a", "x", "r+", "wb", "ab"):
        with pytest.raises(guard.RealStateAccessError, match="allows_real_mailbox"):
            open(FAKE_HUB + "/f", mode)
    with pytest.raises(FileNotFoundError):  # read passes through to the OS
        open(FAKE_HUB + "/f")
    with pytest.raises(FileNotFoundError):
        open(FAKE_HUB + "/f", "rb")


def test_path_write_text_and_io_open_refused(fake_hub):
    import io
    with pytest.raises(guard.RealStateAccessError):
        (fake_hub / "x").write_text("a")
    with pytest.raises(guard.RealStateAccessError):
        (fake_hub / "x").write_bytes(b"a")
    with pytest.raises(guard.RealStateAccessError):
        io.open(FAKE_HUB + "/x", "w")
    with pytest.raises(guard.RealStateAccessError):
        (fake_hub / "x").open("a")


def test_symlink_from_tmp_into_hub_is_resolved(fake_hub, tmp_path):
    link = tmp_path / "lnk"
    link.symlink_to(FAKE_HUB)  # dangling by design
    with pytest.raises(guard.RealStateAccessError):
        (link / "x").write_text("a")
    with pytest.raises(guard.RealStateAccessError):
        os.mkdir(str(link / "d"))


def test_open_write_to_tmp_allowed(tmp_path):
    (tmp_path / "ok").write_text("a")
    with open(tmp_path / "ok", "a") as fh:
        fh.write("b")


@pytest.fixture
def refusals(monkeypatch):
    """Record every refusal raised by check_hub_write: mailbox swallows OSError /
    Exception around several of its sidecar writes, so a raised-and-swallowed refusal
    is only observable here."""
    seen = []
    real = guard.check_hub_write

    def rec(path, hubs, roots, what):
        try:
            return real(path, hubs, roots, what)
        except guard.RealStateAccessError:
            seen.append((what, str(path)))
            raise
    monkeypatch.setattr(guard, "check_hub_write", rec)
    return seen


@pytest.mark.parametrize("name", [
    "_check_urgent_rate_limit", "_log_urgent_downgrade", "_write_urgent_marker",
    "_record_sent", "_record_backoff", "_record_dlq_failure", "_record_ack"])
def test_each_mailbox_sidecar_write_path_is_refused(name, fake_hub, refusals, monkeypatch):
    """mailbox.py :198 :215 :231 :720 :798 :855 :1183 -- each driven against a fake hub
    configured as 'the hub'. _ensure_dir is neutralised so the FIRST write op reached is
    the open()/write_text itself, proving the open hook (not the mkdir hook)."""
    from tools import mailbox
    monkeypatch.setattr(mailbox, "_ensure_dir", lambda p, *a, **k: p)
    monkeypatch.setenv("PANTHEON_SENT_LEDGER", FAKE_HUB + "/sent.jsonl")  # for _record_sent
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: None)  # _record_sent mkdirs the parent
    inbox = fake_hub / "orchestrator"
    calls = {
        "_check_urgent_rate_limit": lambda: mailbox._check_urgent_rate_limit(inbox, "s"),
        "_log_urgent_downgrade": lambda: mailbox._log_urgent_downgrade(inbox, "s", "subj"),
        "_write_urgent_marker": lambda: mailbox._write_urgent_marker(inbox, "s", "subj", "n"),
        "_record_sent": lambda: mailbox._record_sent(
            inbox, {"ts_ns": 1, "sender": "s", "digest": "d"}, "n"),
        "_record_backoff": lambda: mailbox._record_backoff(inbox, "n"),
        "_record_dlq_failure": lambda: mailbox._record_dlq_failure(inbox, "n"),
        "_record_ack": lambda: mailbox._record_ack(inbox, "n", "lbl"),
    }
    try:
        calls[name]()
    except guard.RealStateAccessError:
        pass  # propagating is fine too (not every path swallows)
    assert any(w == "write-open" for w, _ in refusals), (name, refusals)


# ---- T-4949: prefix commands, VAR=val, -c clusters, work tree, env= --------
# SAFETY: same stand-ins as above (world.real is a tmp_path repo playing "real"). No
# test names the real repo, a remote or the hub. Most go through the pure classifiers.
def _check_all(args, *, shell, cwd, roots, env=None):
    for argv, c, e in guard.commands_in(args, shell, cwd, env):
        guard.check_git_call(argv, c, roots, env=e)


PREFIXED = [
    "FOO=1 git pull", "env git pull", "env -i A=b git pull", "env -u HOME git pull",
    "env -- git pull", "command git pull", "command -p git pull", "time git pull",
    "time -p git pull", "nice git pull", "nice -n 5 git pull", "nice -5 git pull",
    "nohup git pull", "exec git pull", "exec -a x git pull", "timeout 5 git pull",
    "timeout -s KILL -k 2 5 git pull", "timeout --signal=TERM --preserve-status 5s git pull",
    "A=1 B=2 env C=3 nice -n 1 timeout 5 git pull",
]


@pytest.mark.parametrize("cmd", PREFIXED)
def test_commands_in_unwraps_prefixes(cmd, tmp_path):
    shell_argvs = [a for a, _c, _e in guard.commands_in(cmd, True, str(tmp_path))]
    list_argvs = [a for a, _c, _e in guard.commands_in(cmd.split(), False, str(tmp_path))]
    for got in (shell_argvs, list_argvs):
        assert ["git", "pull"] in got, got


@pytest.mark.parametrize("cmd", PREFIXED)
def test_predicate_refuses_prefixed_git_in_worktree(world, cmd):
    for args, shell in ((cmd, True), (cmd.split(), False)):
        with pytest.raises(guard.RealStateAccessError):
            _check_all(args, shell=shell, cwd=str(world.wt), roots=world.roots)
        _check_all(args, shell=shell, cwd=str(world.clone), roots=world.roots)


def test_unknown_prefix_option_fails_closed(world):
    for cmd in ("env --frobnicate git pull", "nice --weird=3 git pull",
                "timeout --bogus 5 git pull"):
        with pytest.raises(guard.RealStateAccessError):
            _check_all(cmd, shell=True, cwd=str(world.wt), roots=world.roots)


def test_env_chdir_feeds_cwd(world):
    with pytest.raises(guard.RealStateAccessError):
        _check_all(f"env -C {world.wt} git pull", shell=True, cwd=str(world.clone),
                   roots=world.roots)
    with pytest.raises(guard.RealStateAccessError):
        _check_all(["env", f"--chdir={world.wt}", "git", "pull"], shell=False,
                   cwd=str(world.clone), roots=world.roots)
    _check_all(f"env -C {world.clone} git pull", shell=True, cwd=str(world.wt),
               roots=world.roots)


def test_var_assignment_git_dir_feeds_resolution(world):
    real_gd = str(world.real / ".git")
    for cmd in (f"GIT_DIR={real_gd} git pull", f"env GIT_DIR={real_gd} git pull",
                f"GIT_COMMON_DIR={real_gd} git pull"):
        with pytest.raises(guard.RealStateAccessError):
            _check_all(cmd, shell=True, cwd=str(world.clone), roots=world.roots)
    _check_all(f"GIT_DIR={world.clone}/.git git pull", shell=True, cwd=str(world.wt),
               roots=world.roots)


def test_env_i_and_u_clear_inherited_git_dir(world):
    env = {**os.environ, "GIT_DIR": str(world.real / ".git")}
    with pytest.raises(guard.RealStateAccessError):
        _check_all("git pull", shell=True, cwd=str(world.clone), roots=world.roots, env=env)
    _check_all("env -i PATH=/usr/bin:/bin git pull", shell=True, cwd=str(world.clone),
               roots=world.roots, env=env)
    _check_all("env -u GIT_DIR git pull", shell=True, cwd=str(world.clone),
               roots=world.roots, env=env)


@pytest.mark.parametrize("argv", [
    ["bash", "-lc", "git pull"], ["sh", "-ec", "git pull"], ["bash", "-l", "-c", "git pull"],
    ["bash", "-o", "pipefail", "-c", "git pull"], ["bash", "-cl", "git pull"],
    ["env", "bash", "-lc", "git pull"], ["bash", "--norc", "-c", "git pull"],
])
def test_shell_flag_clusters(argv, world):
    assert ["git", "pull"] in [a for a, _c, _e in guard.commands_in(argv, False, None)]
    with pytest.raises(guard.RealStateAccessError):
        _check_all(argv, shell=False, cwd=str(world.wt), roots=world.roots)


def test_shell_without_c_is_not_a_command_string():
    got = [a for a, _c, _e in guard.commands_in(["bash", "-l", "git pull"], False, None)]
    assert got == [["bash", "-l", "git pull"]]
    got = [a for a, _c, _e in guard.commands_in(["bash", "-o", "c", "x"], False, None)]
    assert got == [["bash", "-o", "c", "x"]]


def test_installed_guard_refuses_bash_lc_and_timeout(world, monkeypatch):
    guard.install(monkeypatch, world.roots, git=True, mailbox=False)
    with pytest.raises(guard.RealStateAccessError):
        subprocess.run(["bash", "-lc", "git pull"], cwd=world.wt, capture_output=True)
    with pytest.raises(guard.RealStateAccessError):
        subprocess.run("timeout 5 git pull", shell=True, cwd=world.wt, capture_output=True)


def test_work_tree_outside_roots_or_in_real_repo_refused(world):
    gd = f"{world.clone}/.git"
    plain = Path(world.allowed) / "plain"
    plain.mkdir()
    for argv in (["git", f"--git-dir={gd}", f"--work-tree={world.real}", "checkout", "."],
                 ["git", "--git-dir", gd, "--work-tree", str(world.wt), "checkout", "."],
                 ["git", f"--git-dir={gd}", "--work-tree", str(world.real), "reset", "--hard"]):
        with pytest.raises(guard.RealStateAccessError, match="work tree"):
            guard.check_git_call(argv, cwd=str(world.clone), allowed_roots=world.roots)
    guard.check_git_call(["git", f"--git-dir={gd}", f"--work-tree={plain}", "checkout", "."],
                         cwd=str(world.clone), allowed_roots=world.roots)
    guard.check_git_call(["git", f"--git-dir={gd}", f"--work-tree={world.real}", "status"],
                         cwd=str(world.clone), allowed_roots=world.roots)  # read-only


def test_git_work_tree_env_and_prefix_refused(world):
    gd = f"{world.clone}/.git"
    env = {**os.environ, "GIT_DIR": gd, "GIT_WORK_TREE": str(world.real)}
    with pytest.raises(guard.RealStateAccessError, match="work tree"):
        guard.check_git_call(["git", "checkout", "."], cwd=str(world.clone),
                             allowed_roots=world.roots, env=env)
    with pytest.raises(guard.RealStateAccessError, match="work tree"):
        _check_all(f"GIT_DIR={gd} GIT_WORK_TREE={world.real} git checkout .", shell=True,
                   cwd=str(world.clone), roots=world.roots)


def test_env_kwarg_git_dir_seen(world, monkeypatch):
    real_gd = str(world.real / ".git")
    with pytest.raises(guard.RealStateAccessError):
        guard.check_git_call(["git", "pull"], cwd=str(world.clone), allowed_roots=world.roots,
                             env={**os.environ, "GIT_DIR": real_gd})
    child = {k: v for k, v in os.environ.items() if k != "GIT_DIR"}
    # inherited (env=None) GIT_DIR is honored...
    monkeypatch.setenv("GIT_DIR", real_gd)
    with pytest.raises(guard.RealStateAccessError):
        guard.check_git_call(["git", "pull"], cwd=str(world.clone), allowed_roots=world.roots)
    # ...but an explicit child env without it wins
    guard.check_git_call(["git", "pull"], cwd=str(world.clone), allowed_roots=world.roots,
                         env=child)


def test_git_dir_env_disables_not_a_repo_exemption(world):
    plain = Path(world.allowed) / "plain2"
    plain.mkdir()
    with pytest.raises(guard.RealStateAccessError):
        guard.check_git_call(["git", "pull"], cwd=str(plain), allowed_roots=world.roots,
                             env={**os.environ, "GIT_DIR": str(plain / "nope")})


def test_installed_guard_sees_popen_env_kwarg(world, monkeypatch):
    guard.install(monkeypatch, world.roots, git=True, mailbox=False)
    with pytest.raises(guard.RealStateAccessError):
        subprocess.run(["git", "pull"], cwd=world.clone, capture_output=True,
                       env={**os.environ, "GIT_DIR": str(world.real / ".git")})


def test_pushd_is_untracked_documented_gap(tmp_path):
    got = [(a, c) for a, c, _e in guard.commands_in("pushd /elsewhere && git pull", True,
                                                     str(tmp_path))]
    assert (["git", "pull"], str(tmp_path)) in got


# ---- T-4949 REBOUND 1: o/O inside short clusters; clustered env options ----
O_CLUSTERS = [
    ["bash", "-eo", "pipefail", "-c", "git pull"],
    ["bash", "-co", "pipefail", "git pull"],
    ["bash", "-cO", "extglob", "git pull"],
    ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", "git pull"],
]


@pytest.mark.parametrize("argv", O_CLUSTERS)
def test_shell_o_value_inside_cluster(argv, world):
    as_string = " ".join(shlex.quote(a) for a in argv)
    for args, shell in ((argv, False), (as_string, True)):
        got = [a for a, _c, _e in guard.commands_in(args, shell, None)]
        assert ["git", "pull"] in got, got
        with pytest.raises(guard.RealStateAccessError):
            _check_all(args, shell=shell, cwd=str(world.wt), roots=world.roots)
        _check_all(args, shell=shell, cwd=str(world.clone), roots=world.roots)


def test_gha_default_shell_script_is_not_a_command_string():
    argv = ["bash", "--noprofile", "--norc", "-eo", "pipefail", "/x/script.sh"]
    assert [a for a, _c, _e in guard.commands_in(argv, False, None)] == [argv]


def test_ambiguous_shell_options_fail_closed(world):
    # `-e9` is not a shell option cluster we understand: every non-option word is a
    # candidate command string.
    argv = ["bash", "-e9", "x", "-c", "git pull"]
    with pytest.raises(guard.RealStateAccessError):
        _check_all(argv, shell=False, cwd=str(world.wt), roots=world.roots)


def test_env_clustered_options_apply_their_effects(world):
    inherited = {**os.environ, "GIT_DIR": f"{world.clone}/.git"}
    for cmd in ("env -i git pull", "env -iv git pull", "env -0i git pull"):
        with pytest.raises(guard.RealStateAccessError):
            _check_all(cmd, shell=True, cwd=str(world.wt), roots=world.roots, env=inherited)
    _check_all("env -v git pull", shell=True, cwd=str(world.wt), roots=world.roots,
               env=inherited)  # GIT_DIR kept: the temp clone is the repo
    for cmd in (f"env -vC {world.wt} git pull", f"env -vC{world.wt} git pull"):
        with pytest.raises(guard.RealStateAccessError):  # clustered -C feeds the cwd
            _check_all(cmd, shell=True, cwd=str(world.clone), roots=world.roots)


def test_env_unknown_option_probes_inherited_and_cleared(world):
    inherited = {**os.environ, "GIT_DIR": f"{world.clone}/.git"}
    with pytest.raises(guard.RealStateAccessError):  # cleared env -> world.wt -> real
        _check_all("env -iZ git pull", shell=True, cwd=str(world.wt), roots=world.roots,
                   env=inherited)
    with pytest.raises(guard.RealStateAccessError):  # inherited env -> real
        _check_all("env --frob git pull", shell=True, cwd=str(world.clone),
                   roots=world.roots, env={**os.environ, "GIT_DIR": str(world.real / ".git")})


@pytest.mark.parametrize("argv", MUTATING)
def test_existing_shapes_unchanged_through_commands_in(world, argv):
    with pytest.raises(guard.RealStateAccessError):
        _check_all(argv, shell=False, cwd=str(world.wt), roots=world.roots)
    _check_all(argv, shell=False, cwd=str(world.clone), roots=world.roots)
