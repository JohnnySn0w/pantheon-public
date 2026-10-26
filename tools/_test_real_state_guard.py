"""T-4934: pytest-only guard that refuses real-state mutation from the tools/ suite.

Incidents (2026-10-12/13): a T-4881 fixture sent advisories into the LIVE mailbox hub,
and a T-4885-era test ran a real `git pull` in its cwd checkout. Protection had been
per-test discipline; this is the structural backstop (P1).

Installed per test by the autouse `_real_state_guard` fixture in tools/conftest.py. It
patches objects (subprocess.Popen, builtins/io.open, a few os primitives), not an env
var, because tests `patch.dict(os.environ, clear=True)` (see mailbox._ledger_target) and
would erase a flag-based guard. A test that mocks `subprocess`/`os` replaces what we
installed, so only REAL execution is ever intercepted.

Git rule (REBOUND 1): a mutating git command is refused when the target repo's git
COMMON dir (`git rev-parse --git-common-dir`, realpath) lies outside the temp roots. Keyed
on what git knows, not on where the checkout lives: agent worktrees live under /tmp but
share the real repo's common dir, so they are refused; a genuine temp clone/init is
allowed. Probe failure fails closed.

T-4949 command shapes: leading NAME=val words and the prefix utilities env, command,
time, nice, nohup, exec and timeout (with their own options; an unrecognised option fails
closed) are stripped to find the real program, in argv lists and shell strings alike.
Assignments, `env -i/-u/-C` and the Popen `env=` kwarg (or inherited os.environ) form the
child env the probe runs with, so GIT_DIR / GIT_COMMON_DIR are honored. `sh -c` is found
inside short-flag clusters (`bash -lc`, `sh -ec`). A work tree (--work-tree /
GIT_WORK_TREE) must itself be under the temp roots and not inside a non-temp repo.

Opt-outs: @pytest.mark.allows_real_git, @pytest.mark.allows_real_mailbox.

Known gaps: git run by a child process a test spawns (the
PANTHEON_TEST_NO_REAL_REPO_SYNC env covers the sync helpers there); `-c alias.x=...`
aliases that expand to a mutating verb, and `-c core.worktree=` / core.bare; shell
strings we cannot parse (unbalanced quotes); newline-separated commands and the `if` /
`{ ... }` keyword prefixes are not segmented; cwd tracking in shell strings is plain
`cd X` only (`pushd`/`popd` and subshell cwd are not tracked, and neither is
`export VAR=`); prefix utilities other than the set above (sudo, xargs, stdbuf, ...) and
`env -S`; Popen(executable=...); GIT_INDEX_FILE / GIT_OBJECT_DIRECTORY pointing into a
real repo are not checked; with a git dir set (flag or GIT_DIR) and no work tree, git
uses the cwd as the work tree, and that implicit work tree is not checked; a bytes-keyed `env=` fails closed; os.fdopen/io.FileIO opened
directly into the hub.
"""

from __future__ import annotations

import builtins
import io
import os
import re
import shlex
import subprocess
import tempfile

_REAL_POPEN = subprocess.Popen
_REAL_OPEN = builtins.open  # io.open is the same object
_REAL_OS = {n: getattr(os, n) for n in
            ("open", "rename", "replace", "link", "unlink", "remove", "mkdir", "rmdir")}

_ALWAYS_MUTATING = {
    "pull", "push", "rebase", "merge", "commit", "cherry-pick", "am", "revert",
    "update-ref", "clean", "gc", "checkout", "switch", "restore", "add", "rm", "reset",
}
# git global options that take a separate value argument (the ones we do not interpret)
_GLOBAL_WITH_VALUE = {"-c", "--namespace", "--exec-path"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
# T-4949: env vars that change which repo / work tree git resolves.
_GIT_LOCATE_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_CEILING_DIRECTORIES",
                    "GIT_DISCOVERY_ACROSS_FILESYSTEM")
_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# T-4949 command-prefix utilities: name -> (options taking a value, options without one,
# positionals consumed before the program). Any other option fails closed.
_PREFIXES = {
    "env": ({"-u", "--unset", "-C", "--chdir"},
            {"-i", "--ignore-environment", "-0", "--null", "-v", "--debug"}, 0),
    "command": (set(), {"-p", "-v", "-V"}, 0),
    "time": ({"-f", "--format", "-o", "--output"},
             {"-p", "-a", "--append", "-v", "--verbose", "-q", "--quiet", "--portability"}, 0),
    "nice": ({"-n", "--adjustment"}, set(), 0),
    "nohup": (set(), set(), 0),
    "exec": ({"-a"}, {"-c", "-l", "-cl", "-lc"}, 0),
    "timeout": ({"-s", "--signal", "-k", "--kill-after"},
                {"--preserve-status", "--foreground", "-v", "--verbose"}, 1),
}
_BRANCH_TAG_LISTING = {
    "-l", "--list", "--show-current", "-a", "--all", "-r", "--remotes", "-v", "-vv",
    "--verbose", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
    "--format", "--sort", "-n",
}
_BRANCH_TAG_WRITING = {
    "-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-f", "--force",
    "-u", "--set-upstream-to", "--unset-upstream", "--edit-description", "-s", "--sign",
    "-F", "--file",
}
_PROBE_TIMEOUT_S = 15
# Both discovery-exhausted forms: "(or any of the parent directories)" and, when the
# walk stops at a filesystem boundary (tmpfs /tmp on CI), "(or any parent up to mount
# point ...)". A broken gitfile reads "not a git repository: <path>" and stays closed.
_NOT_A_REPO = "not a git repository (or any "

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


class RealStateAccessError(RuntimeError):
    """A test tried to mutate real (non-temp) git state or the live mailbox hub."""


def _under(path: str, roots) -> bool:
    for r in roots:
        r = str(r)
        if path == r or path.startswith(r.rstrip("/") + "/"):
            return True
    return False


def allowed_roots(extra=()) -> list[str]:
    roots = {tempfile.gettempdir(), os.path.realpath(tempfile.gettempdir())}
    roots.update(str(e) for e in extra)
    roots.update(os.path.realpath(str(e)) for e in extra)
    return sorted(roots)


# ------------------------------------------------------------------------------ git
def _parse_git(argv):
    """Return (subcommand, sub_args, [-C paths], git_dir_or_None, work_tree_or_None) or
    None if argv is not a git call."""
    if not argv or os.path.basename(str(argv[0])) != "git":
        return None
    i, cpaths, gitdir, worktree = 1, [], None, None
    while i < len(argv):
        a = str(argv[i])
        if a == "-C" and i + 1 < len(argv):
            cpaths.append(str(argv[i + 1]))
            i += 2
        elif a == "--git-dir" and i + 1 < len(argv):
            gitdir = str(argv[i + 1])
            i += 2
        elif a.startswith("--git-dir="):
            gitdir = a.split("=", 1)[1]
            i += 1
        elif a == "--work-tree" and i + 1 < len(argv):
            worktree = str(argv[i + 1])
            i += 2
        elif a.startswith("--work-tree="):
            worktree = a.split("=", 1)[1]
            i += 1
        elif a in _GLOBAL_WITH_VALUE:
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return a, [str(x) for x in argv[i + 1:]], cpaths, gitdir, worktree
    return None


def _flag_key(f: str) -> str:
    return f.split("=", 1)[0]


def _branch_tag_mutating(args) -> bool:
    """branch / tag: mutating iff a writing flag is present, or a positional name is
    given without a listing flag."""
    flags = [_flag_key(a) for a in args if a.startswith("-")]
    if any(f in _BRANCH_TAG_WRITING for f in flags):
        return True
    if any(f in _BRANCH_TAG_LISTING for f in flags):
        return False
    return any(not a.startswith("-") for a in args)


def _is_mutating(sub: str, args: list[str]) -> bool:
    if sub in _ALWAYS_MUTATING:
        return True
    if sub == "fetch":  # only refspec writes; plain fetch moves FETCH_HEAD only
        return any(":" in a for a in args if not a.startswith("-"))
    if sub == "stash":
        return not (args and args[0] in ("list", "show"))
    if sub == "worktree":
        return not (args and args[0] == "list")
    if sub in ("branch", "tag"):
        return _branch_tag_mutating(args)
    return False


def _probe_common_dir(target: str, gitdir, cache, env=None):
    """realpath of `git rev-parse --git-common-dir` for `target`, run with the ORIGINAL
    Popen (no recursion) and the child's effective env (T-4949: GIT_DIR/GIT_COMMON_DIR
    from env= or a VAR=val prefix are honored). Returns (path_or_None, not_a_repo)."""
    env = dict(os.environ if env is None else env)
    env.setdefault("PATH", os.environ.get("PATH", os.defpath))
    key = (target, gitdir, tuple(env.get(k) for k in _GIT_LOCATE_VARS))
    if cache is not None and key in cache:
        return cache[key]
    cmd = ["git"] + (["--git-dir", gitdir] if gitdir else []) + ["rev-parse", "--git-common-dir"]
    result = (None, False)
    try:
        proc = _REAL_POPEN(cmd, cwd=target, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, env=env)
        out, err = proc.communicate(timeout=_PROBE_TIMEOUT_S)
        if proc.returncode == 0 and out.strip():
            c = out.strip().splitlines()[0]
            result = (os.path.realpath(c if os.path.isabs(c) else os.path.join(target, c)),
                      False)
        else:
            result = (None, _NOT_A_REPO in (err or ""))
    except Exception:  # missing cwd, timeout, git absent...: fail closed
        result = (None, False)
    if cache is not None:
        cache[key] = result
    return result


def _refuse(msg: str):
    raise RealStateAccessError(
        f"T-4934 real-state guard: refused {msg}, outside the temp roots, during a test. "
        f"Use a tmp_path repo or stub the call; only if the test genuinely needs real git, "
        f"mark it @pytest.mark.allows_real_git."
    )


def _repo_ok(target, gitdir, env, allowed_roots, cache, *, gitdir_set) -> tuple:
    """(ok, common): the repo at `target` is a genuine temp repo, or (with no git dir in
    play) not a repo at all under the roots."""
    common, not_repo = _probe_common_dir(target, gitdir, cache, env)
    if common is None and not_repo and not gitdir_set and _under(target, allowed_roots):
        return True, common
    return (common is not None and _under(common, allowed_roots)), common


def check_git_call(argv, cwd, allowed_roots, cache=None, env=None) -> None:
    """Refuse a mutating git command whose repo's git COMMON dir is outside the temp
    roots, or whose work tree (--work-tree / GIT_WORK_TREE, T-4949) is outside them or
    inside a non-temp repo. `env` is the child's effective environment (None: inherit
    os.environ). Probe failure fails closed, except 'not a git repository' for a target
    under the roots with no git dir in play (nothing there to mutate)."""
    parsed = _parse_git(argv)
    if parsed is None:
        return
    sub, args, cpaths, gitdir, worktree = parsed
    if not _is_mutating(sub, args):
        return
    env = dict(os.environ if env is None else env)
    target = os.fspath(cwd) if cwd is not None else os.getcwd()
    for c in cpaths:
        target = c if os.path.isabs(c) else os.path.join(target, c)
    target = os.path.realpath(target)
    if gitdir is not None and not os.path.isabs(gitdir):
        gitdir = os.path.join(target, gitdir)
    gitdir_set = gitdir is not None or bool(env.get("GIT_DIR"))
    ok, common = _repo_ok(target, gitdir, env, allowed_roots, cache, gitdir_set=gitdir_set)
    if not ok:
        where = (f"repo common dir {common!r}" if common
                 else f"target {target!r} (git probe failed; failing closed)")
        _refuse(f"`git {sub}` against {where}")
    if worktree is None:
        worktree = env.get("GIT_WORK_TREE") or None
    if worktree is None:
        return
    wt = os.path.realpath(worktree if os.path.isabs(worktree)
                          else os.path.join(target, worktree))
    clean = {k: v for k, v in env.items() if k not in _GIT_LOCATE_VARS}
    if not (_under(wt, allowed_roots)
            and _repo_ok(wt, None, clean, allowed_roots, cache, gitdir_set=False)[0]):
        _refuse(f"`git {sub}` with work tree {wt!r} (outside the roots, in a non-temp "
                f"repo, or the probe failed; T-4949)")


def _consume_prefix(name, rest, cwd, env):
    """Consume one command-prefix utility's options (and, for env, its NAME=val words;
    for timeout, its DURATION). Short options may be clustered (`env -iv`, `nice -n5`):
    each character is applied in order, and a value-taking one takes the rest of the
    token or the next argument. Returns (remaining_argv, cwd), or None on an option we
    do not recognise (the caller then fails closed). Mutates `env`."""
    with_val, flags, npos = _PREFIXES[name]

    def apply(key, val):
        nonlocal cwd
        if name != "env":
            return
        if key in ("-i", "--ignore-environment"):
            env.clear()
        elif key in ("-u", "--unset"):
            env.pop(val, None)
        elif key in ("-C", "--chdir"):
            base = os.fspath(cwd) if cwd is not None else os.getcwd()
            cwd = val if os.path.isabs(val) else os.path.join(base, val)

    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--":
            i += 1
            break
        if name == "env" and a == "-":  # same as -i
            env.clear()
            i += 1
            continue
        if not a.startswith("-") or a == "-":
            break
        if name == "nice" and a[1:].isdigit():  # nice -5
            i += 1
            continue
        if a.startswith("--"):
            key, val = (a.split("=", 1) + [None])[:2]
            if val is not None:
                if key not in with_val:
                    return None
                i += 1
            elif key in with_val:
                if i + 1 >= len(rest):
                    return None
                val = rest[i + 1]
                i += 2
            elif key in flags:
                i += 1
            else:
                return None
            apply(key, val)
            continue
        if a in flags:  # whole-token short flags (exec -cl)
            apply(a, None)
            i += 1
            continue
        i += 1
        for k, ch in enumerate(a[1:]):
            opt = "-" + ch
            if opt in with_val:
                val = a[2 + k:]
                if not val:
                    if i >= len(rest):
                        return None
                    val = rest[i]
                    i += 1
                apply(opt, val)
                break
            if opt not in flags:
                return None
            apply(opt, None)
    if name == "env":
        while i < len(rest) and _ASSIGN_RE.match(rest[i]):
            k, v = rest[i].split("=", 1)
            env[k] = v
            i += 1
    if i + npos > len(rest):
        return None
    return rest[i + npos:], cwd


def _unwrap(argv, cwd, env):
    """T-4949: strip leading NAME=val assignments (applied to the child env) and the
    command-prefix utilities in _PREFIXES, returning (program_argv, cwd, [env, ...]).
    An unrecognised prefix option fails closed: jump to the first later git/shell word;
    for `env` (whose options may have cleared the environment) the command is checked
    under both the inherited and a cleared env (T-4949 REBOUND 1)."""
    while argv:
        if _ASSIGN_RE.match(argv[0]):
            k, v = argv[0].split("=", 1)
            env[k] = v
            argv = argv[1:]
            continue
        name = os.path.basename(argv[0])
        if name not in _PREFIXES:
            break
        before = dict(env)
        got = _consume_prefix(name, argv[1:], cwd, env)
        if got is not None:
            argv, cwd = got
            continue
        env.clear()
        env.update(before)  # undo any partial effects; consider both outcomes below
        envs = [env, {}] if name == "env" else [env]
        for j in range(1, len(argv)):
            t = argv[j]
            if _ASSIGN_RE.match(t):
                k, v = t.split("=", 1)
                for e in envs:
                    e[k] = v
            elif os.path.basename(t) == "git" or os.path.basename(t) in _SHELLS:
                return argv[j:], cwd, envs
        return [], cwd, envs
    return argv, cwd, [env]


_SH_LONG_WITH_VALUE = ("--rcfile", "--init-file")


def _shell_command_strings(argv):
    """Candidate `-c` command strings for a shell argv, by bash's rule: options are
    scanned left to right; inside a short cluster each `o`/`O` consumes the next
    argument in order (`-eo pipefail`, `-co pipefail CMD`); if `c` was seen, the first
    non-option word is the command string. If the scan meets an option word it cannot
    classify, fail closed: every non-option word is a candidate (T-4949 REBOUND 1)."""
    i, n, has_c, ambiguous = 1, len(argv), False, False
    while i < n:
        a = argv[i]
        if a in _SH_LONG_WITH_VALUE:
            i += 2
            continue
        if a in ("--", "-"):
            i += 1
            break
        if a.startswith("--"):
            i += 1
            continue
        if len(a) > 1 and a[0] in "-+":
            if not a[1:].isalpha():
                ambiguous = True
                i += 1
                continue
            take = 0
            for ch in a[1:]:
                if ch in "oO":
                    take += 1
                elif ch == "c" and a[0] == "-":
                    has_c = True
            i += 1 + take
            continue
        break
    if not ambiguous:
        rest = argv[i + 1:]
        ambiguous = any(len(t) > 1 and t[0] == "-" and not t.startswith("--")
                        and "c" in t[1:] for t in rest) and not has_c
    if ambiguous:
        return [t for t in argv[1:] if not (len(t) > 1 and t[0] in "-+")]
    return [argv[i]] if has_c and i < n else []


def _shell_segments(cmd: str, cwd, env):
    """Yield (argv, cwd, env) for each simple command in a shell string. Tracks plain
    `cd X` only; recurses into `sh -c '<string>'`. Unparseable strings yield nothing
    (known gap)."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return
    segs, seg = [], []
    for t in toks:
        if t and all(ch in ";&|()<>" for ch in t):
            if t in (";", "&&", "||", "|", "&"):
                segs.append(seg)
                seg = []
            continue
        seg.append(t)
    segs.append(seg)
    for s in segs:
        if not s:
            continue
        yield from _argv_commands(s, cwd, env)
        if s[0] == "cd" and len(s) > 1:
            base = os.fspath(cwd) if cwd is not None else os.getcwd()
            cwd = s[1] if os.path.isabs(s[1]) else os.path.join(base, s[1])


def _argv_commands(argv, cwd, env):
    """Yield (argv, cwd, env) to check for one argv: unwraps VAR=val / prefix
    utilities, then expands `sh -c '<cmd>'` (incl. -lc / -ec / -eo X -c clusters)."""
    argv, cwd, envs = _unwrap([str(x) for x in argv], cwd, dict(env))
    for e in envs:
        if argv and os.path.basename(argv[0]) in _SHELLS:
            for s in _shell_command_strings(argv):
                yield from _shell_segments(s, cwd, e)
        yield argv, cwd, e


def commands_in(args, shell: bool, cwd, env=None):
    """Yield (argv, cwd, env) for every command a Popen(args, shell=, cwd=, env=) would
    run that we can see. env=None means the child inherits os.environ."""
    env = dict(os.environ if env is None else env)
    if shell:
        cmd = args if isinstance(args, str) else (args[0] if args else "")
        yield from _shell_segments(os.fspath(cmd), cwd, env)
    elif isinstance(args, (list, tuple)):
        yield from _argv_commands(args, cwd, env)


def make_guarded_popen(roots, cache=None):
    cache = {} if cache is None else cache

    class GuardedPopen(_REAL_POPEN):
        def __init__(self, args, *a, **kw):
            shell = kw.get("shell", a[7] if len(a) > 7 else False)
            cwd = kw.get("cwd", a[8] if len(a) > 8 else None)
            env = kw.get("env", a[9] if len(a) > 9 else None)
            for argv, c, e in commands_in(args, shell, cwd, env):
                check_git_call(argv, c, roots, cache, env=e)
            super().__init__(args, *a, **kw)
    return GuardedPopen


# -------------------------------------------------------------------------- mailbox
def real_hubs() -> list[str]:
    hubs = []
    env = os.environ.get("PANTHEON_MAILBOX_HUB")
    if env:
        hubs.append(env)
    try:
        from tools.agent_role_map import mailbox_hub
        hubs.append(mailbox_hub())
    except Exception:
        pass
    return [os.path.realpath(h) for h in hubs if h]


def check_hub_write(path, hubs, roots, what):
    """Pure predicate (no I/O beyond path resolution): refuse a write-ish op on `path`
    inside any hub in `hubs` that is not itself under a temp root. `hubs` may be a list
    or a zero-arg callable (resolved per call, so a test's env patch is honored). Both the
    path and the hub are realpath'd, so a symlink from tmp into the hub cannot bypass."""
    if callable(hubs):
        hubs = hubs()
    if isinstance(path, int) or path is None:
        return
    try:
        p = os.fsdecode(os.fspath(path))
    except TypeError:
        return
    p = os.path.realpath(p)
    for h in hubs:
        h = os.path.realpath(h)
        if _under(p, [h]) and not _under(h, roots):
            raise RealStateAccessError(
                f"T-4934 real-state guard: refused {what} on {p!r}, inside the LIVE mailbox "
                f"hub {h!r}, during a test. Point the test at a tmp_path inbox (or patch "
                f"PANTHEON_MAILBOX_HUB); only if it genuinely needs the real hub, mark it "
                f"@pytest.mark.allows_real_mailbox."
            )


def install(monkeypatch, roots, *, git: bool, mailbox: bool) -> None:
    if git:
        monkeypatch.setattr(subprocess, "Popen", make_guarded_popen(roots))
    if not mailbox:
        return
    hubs = real_hubs  # callable: re-resolved on every call
    real = _REAL_OS

    def g_open(path, flags, *a, **kw):
        if flags & _WRITE_FLAGS:
            check_hub_write(path, hubs, roots, "write-open")
        return real["open"](path, flags, *a, **kw)

    def g_builtin_open(file, mode="r", *a, **kw):
        if isinstance(mode, str) and any(c in mode for c in "wax+"):
            check_hub_write(file, hubs, roots, "write-open")
        return _REAL_OPEN(file, mode, *a, **kw)

    def two(name):
        def f(src, dst, *a, **kw):
            check_hub_write(src, hubs, roots, name)
            check_hub_write(dst, hubs, roots, name)
            return real[name](src, dst, *a, **kw)
        return f

    def one(name):
        def f(path, *a, **kw):
            check_hub_write(path, hubs, roots, name)
            return real[name](path, *a, **kw)
        return f

    monkeypatch.setattr(builtins, "open", g_builtin_open)
    monkeypatch.setattr(io, "open", g_builtin_open)  # pathlib (Path.write_text/open) uses io.open
    monkeypatch.setattr(os, "open", g_open)
    for n in ("rename", "replace", "link"):
        monkeypatch.setattr(os, n, two(n))
    for n in ("unlink", "remove", "mkdir", "rmdir"):
        monkeypatch.setattr(os, n, one(n))
