"""Never-touch-the-real-host guards for the host-substrate tests (T-2918).

Both run on every runner, CI included (they never skip). They are tripwires,
not the barrier: the REAL barrier is OS permissions (the tests run as an
unprivileged account; real root is never mapped into the sandbox's user
namespace). The guards make a regression loud and stop it before the syscall.

* ``real_host_state()`` -- a read-only fingerprint of the specific real-host
  paths these tests could touch (the fixture's own files, units, fragments,
  homes and mailbox trees, the fake-binary bind targets) plus a residue scan of
  /etc/passwd and /etc/group for the fixture's names and ids. It deliberately
  does NOT hash whole /etc, /home or the account databases, so a concurrent
  legitimate host change (Recovery provisioning a seat, apt) cannot false-fail it
  (review N5). conftest compares it before and after the test session.
* an audit hook (``sys.addaudithook``, process-wide, installed once, active
  only while a host_substrate test runs) that BLOCKS, before the syscall, any
  write-mode open or filesystem mutation resolving under a real host root, and
  any process launch (subprocess, os.system, os.posix_spawn, os.exec*,
  os.spawn*) whose command is a substrate mutator, looking THROUGH wrappers
  (env, nohup, timeout, nice, setsid, stdbuf, taskset, unshare, flock, chrt,
  time, ``podman unshare``, sg/sh/bash -c strings). Each wrapper is parsed with
  its own option arity; an option the table does not know REFUSES (fail closed,
  so a mis-skipped option value can never hide the real command). ``python -c``,
  ``xargs``, ``env -S`` and ``flock -c`` cannot be inspected, so they are refused
  outright; a script path run by an interpreter is not inspected (documented,
  not relied on).
"""

from __future__ import annotations

import hashlib
import os
import shlex
import sys
from contextlib import contextmanager
from typing import Iterable, Iterator

#: Real-host roots no host_substrate test may write under.
PROTECTED_ROOTS = ("/etc", "/srv", "/var", "/usr", "/boot", "/root", "/opt", "/lib", "/bin", "/sbin", "/home")
#: ...except the system temp tree (``/tmp`` is never protected at all) and, at
#: runtime, the pytest session's OWN basetemp (``allow_temp_root``; CI passes
#: ``--basetemp=../pytest-basetemp``, under the runner's work dir in /opt, and
#: xdist gives each worker its own ``popen-gwN`` under it). Python bytecode
#: caches (any ``__pycache__`` path) are exempt so an import never trips it.
EXEMPT_ROOTS = ("/var/tmp",)

#: Binaries that mutate host substrate; a test may only reach them through the sandbox.
MUTATORS = frozenset({
    "useradd", "usermod", "userdel", "groupadd", "groupdel", "gpasswd", "setfacl", "chown",
    "chmod", "install", "mv", "unlink", "visudo", "loginctl", "systemctl", "sudo", "chpasswd",
    "passwd", "newusers", "rm",
})
#: Wrappers whose wrapped command is inspected (options skipped).
#: Wrapper option arity: name -> (flags, options taking a value, leading
#: positionals before the command, VAR=value assignments allowed). Anything
#: else refuses (``_unwrap``).
_WRAPPER_SPEC = {
    "env": ({"-i", "--ignore-environment", "-0", "--null", "-v", "--debug"},
            {"-u", "--unset", "-C", "--chdir"}, 0, True),
    "nohup": (set(), set(), 0, False),
    "timeout": ({"--preserve-status", "--foreground", "-v", "--verbose"},
                {"-s", "--signal", "-k", "--kill-after"}, 1, False),
    "nice": (set(), {"-n", "--adjustment"}, 0, False),
    "ionice": ({"-t", "--ignore"}, {"-c", "--class", "-n", "--classdata"}, 0, False),
    "setsid": ({"-c", "--ctty", "-f", "--fork", "-w", "--wait"}, set(), 0, False),
    "stdbuf": (set(), {"-i", "-o", "-e", "--input", "--output", "--error"}, 0, False),
    "taskset": ({"-a", "--all-tasks", "-c", "--cpu-list"}, set(), 1, False),
    "unshare": ({"-m", "--mount", "-u", "--uts", "-i", "--ipc", "-n", "--net", "-p", "--pid", "-U", "--user",
                 "-C", "--cgroup", "-T", "--time", "-f", "--fork", "-r", "--map-root-user", "-c",
                 "--map-current-user", "--mount-proc", "--kill-child", "--keep-caps"},
                {"--propagation", "-S", "--setuid", "-G", "--setgid", "-w", "--wd", "-R", "--root",
                 "--map-user", "--map-group"}, 0, False),
    "chrt": ({"-b", "--batch", "-d", "--deadline", "-f", "--fifo", "-i", "--idle", "-o", "--other", "-r", "--rr",
              "-R", "--reset-on-fork", "-a", "--all-tasks", "-v", "--verbose"},
             {"-T", "--sched-runtime", "-P", "--sched-period", "-D", "--sched-deadline"}, 1, False),
    "flock": ({"-s", "--shared", "-x", "-e", "--exclusive", "-u", "--unlock", "-n", "--nb", "--nonblock",
               "-o", "--close", "-F", "--no-fork", "--verbose"},
              {"-w", "--wait", "--timeout", "-E", "--conflict-exit-code"}, 1, False),
    "time": ({"-p", "--portability", "-v", "--verbose", "-a", "--append", "-q", "--quiet"},
             {"-o", "--output", "-f", "--format"}, 0, False),
}
#: Options that make a wrapper run a command STRING (refused, not parsed).
_COMMAND_STRING = {"env": ("-S", "--split-string"), "flock": ("-c", "--command")}
_WRAPPERS = frozenset(_WRAPPER_SPEC) | {"xargs", "podman"}
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "sg", "su"})
_PYTHONS = ("python", "python3", "pypy")

#: The specific real paths these tests could touch (review N5).
TOUCHABLE_PATHS = (
    "/etc/sudoers.d/pantheon-alice-probe", "/etc/sudoers.d/pantheon-alice-probe.pantheon-tmp",
    "/etc/sudoers.d/pantheon-broker-was-here", "/etc/systemd/system/t2918.service",
    "/etc/systemd/system/multi-user.target.wants/t2918.service",
    "/etc/dbus-1/system.d/org.pantheon.T2918Fixture.conf", "/etc/.pantheon-sandbox-marker",
    "/etc/pantheon-t2918-guard-probe", "/etc/pantheon-t2918-a", "/etc/pantheon-t2918-b",
    "/home/alice", "/home/pantheon-t2918-nobody", "/srv/pantheon", "/srv/journals",
    "/srv/pantheon-t2918-guard-probe", "/var/lib/systemd/linger/alice", "/var/lib/systemd/linger/pantheon-t2918",
    "/var/lib/systemd/linger/root", "/var/lib/systemd/pantheon-fake-systemd.log",
    "/usr/bin/loginctl", "/usr/bin/systemctl", "/mnt/agent", "/mnt/staging", "/mnt/repo", "/mnt/etc",
)
#: Fixture names / ids that must never appear in the real account databases.
FIXTURE_NAMES = frozenset({"alice", "pmail", "pantheon-hostbroker"})
FIXTURE_IDS = frozenset({"3001", "3101", "3201", "3202"})

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_PATH_EVENTS = {
    "os.mkdir": (0,), "os.rmdir": (0,), "os.remove": (0,), "os.rename": (0, 1), "os.chmod": (0,),
    "os.chown": (0,), "os.symlink": (1,), "os.link": (1,), "os.truncate": (0,), "os.utime": (0,),
    "shutil.rmtree": (0,), "shutil.copyfile": (1,), "shutil.copytree": (1,), "shutil.move": (0, 1),
    "os.chflags": (0,), "os.mkfifo": (0,), "os.mknod": (0,),
}


class GuardViolation(PermissionError):
    """A test tried to write the real host (blocked before the syscall)."""


def fingerprint(paths: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.encode() + b"\0")
        try:
            st = os.lstat(path)
        except OSError as exc:
            digest.update(f"absent:{exc.errno}".encode())
            continue
        digest.update(f"{st.st_mode}:{st.st_uid}:{st.st_gid}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}:{st.st_ino}".encode())
        try:
            if os.path.isdir(path):
                digest.update("\0".join(sorted(os.listdir(path))).encode())
            else:
                with open(path, "rb") as handle:
                    digest.update(hashlib.sha256(handle.read()).digest())
        except OSError:
            digest.update(b"unreadable")
    return digest.hexdigest()


def fixture_residue(files: Iterable[str] = ("/etc/passwd", "/etc/group")) -> list[str]:
    """Lines of the real account databases naming a fixture account/group/id."""

    out = []
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    fields = line.rstrip("\n").split(":")
                    if fields[0] in FIXTURE_NAMES or (len(fields) > 2 and fields[2] in FIXTURE_IDS):
                        out.append(f"{path}:{fields[0]}")
        except OSError:
            out.append(f"{path}:unreadable")
    return out


def real_host_state() -> tuple[str, tuple[str, ...]]:
    return fingerprint(TOUCHABLE_PATHS), tuple(fixture_residue())


class _State:
    active = False
    in_hook = False
    violations: list[str] = []
    temp_roots: tuple[str, ...] = ()


def allow_temp_root(path: object) -> None:
    """Admit the pytest session's actual basetemp (both its spelling and its
    realpath). Only that directory: NOT its parent, so a sibling of the
    basetemp (and, under xdist, the shared ``pytest-basetemp`` root itself)
    stays protected when it lies under a protected root."""

    raw = os.path.normpath(os.path.abspath(os.fspath(path)))
    _State.temp_roots = tuple(dict.fromkeys(_State.temp_roots + (raw, os.path.realpath(raw))))


def _within(full: str, root: str) -> bool:
    return full == root or full.startswith(root.rstrip("/") + "/")


def _protected(path: object) -> bool:
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, str) or not path:
        return False
    full = os.path.normpath(os.path.abspath(path))
    if any(_within(full, r) for r in EXEMPT_ROOTS + _State.temp_roots):
        return False
    if "__pycache__" in full.split("/"):  # bytecode caches written by imports
        return False
    return any(_within(full, r) for r in PROTECTED_ROOTS)


def _block(what: str) -> None:
    _State.violations.append(what)
    raise GuardViolation(f"T-2918 real-host guard blocked: {what}")


def _name(word: object) -> str:
    if isinstance(word, bytes):
        word = os.fsdecode(word)
    return os.path.basename(word) if isinstance(word, str) else ""


def offending(argv: list[object], depth: int = 0) -> str | None:
    """Why this command line must not run on the real host, or None.

    Looks through wrappers and ``-c`` strings; refuses ``python -c`` outright.
    """

    words = [os.fsdecode(w) if isinstance(w, bytes) else w for w in argv if isinstance(w, (str, bytes))]
    if not words or depth > 6:
        return None
    name = _name(words[0])
    if name in MUTATORS:
        return f"mutator {words[0]!r}"
    if name.startswith(_PYTHONS) and "-c" in words[1:]:
        return f"uninspectable {name} -c"
    if name in _SHELLS and "-c" in words[1:]:
        index = words.index("-c")
        if index + 1 < len(words):
            try:
                lexer = shlex.shlex(words[index + 1], posix=True, punctuation_chars=True)
                lexer.whitespace_split = True
                inner = list(lexer)
            except ValueError:
                return f"unparseable {name} -c string"
            for segment in _segments(inner):
                why = offending(segment, depth + 1)
                if why:
                    return why
        return None
    if name in _WRAPPERS:
        inner, why = _unwrap(name, words[1:])
        if why:
            return why
        return offending(inner, depth + 1)
    return None


def _unwrap(name: str, args: list[str]) -> tuple[list[str], str | None]:
    """The command a wrapper runs, parsed with the wrapper's OWN option arity
    (``_WRAPPER_SPEC``). An option the table does not know refuses (fail
    closed): a mis-skipped option value would hide the real command (review
    r2b: ``timeout -s KILL 5 useradd``, ``env -u X useradd``, ``flock LOCK useradd``)."""

    if name == "xargs":
        return [], "uninspectable xargs (its command arguments come from stdin)"
    if name == "podman":
        if args[:1] != ["unshare"]:
            return [], f"uninspectable podman subcommand {args[:1]!r}"
        return args[1:], None
    flags, valued, leading, assignments = _WRAPPER_SPEC[name]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            i += 1
            break
        if assignments and "=" in arg and not arg.startswith("-"):
            i += 1
            continue
        if arg.startswith("--"):
            opt, has_value = arg.split("=", 1)[0], "=" in arg
            if opt in _COMMAND_STRING.get(name, ()):
                return [], f"{name} {opt} runs a command string; refused"
            if opt in valued:
                i += 1 if has_value else 2
                if i > len(args):
                    return [], f"{name} {opt} is missing its value"
                continue
            if opt in flags and not has_value:
                i += 1
                continue
            return [], f"unknown {name} option {arg!r}; refused (fail closed)"
        if arg.startswith("-") and len(arg) > 1:
            if name == "nice" and arg[1:].isdigit():
                i += 1
                continue
            consumed = 1
            for j, ch in enumerate(arg[1:]):
                opt = "-" + ch
                if opt in _COMMAND_STRING.get(name, ()):
                    return [], f"{name} {opt} runs a command string; refused"
                if opt in valued:
                    if not arg[2 + j:]:
                        consumed = 2
                        if i + 1 >= len(args):
                            return [], f"{name} {opt} is missing its value"
                    break
                if opt not in flags:
                    return [], f"unknown {name} option {arg!r}; refused (fail closed)"
            i += consumed
            continue
        break
    rest = args[i:]
    return (rest[leading:] if len(rest) > leading else []), None


def _segments(tokens: list[str]) -> list[list[str]]:
    out, current = [], []
    for token in tokens:
        if token and set(token) <= set("();<>|&"):  # any shell operator token
            out.append(current)
            current = []
        else:
            current.append(token)
    out.append(current)
    return [s for s in out if s]


def _check_launch(argv: object) -> None:
    if isinstance(argv, (str, bytes)):
        argv = [argv]
    if isinstance(argv, (list, tuple)):
        why = offending(list(argv))
        if why:
            _block(f"process launch {list(argv)[:3]!r}: {why}")


def _hook(event: str, args: tuple) -> None:
    if not _State.active or _State.in_hook:
        return
    _State.in_hook = True
    try:
        if event == "open":
            path, mode, flags = (list(args) + [None, None, None])[:3]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & _WRITE_FLAGS)
            if writing and _protected(path):
                _block(f"open({path!r}, {mode!r})")
        elif event in _PATH_EVENTS:
            for index in _PATH_EVENTS[event]:
                if index < len(args) and _protected(args[index]):
                    _block(f"{event}({args[index]!r})")
        elif event == "subprocess.Popen":  # (executable, args, cwd, env)
            argv = args[1] if len(args) > 1 else None
            _check_launch(argv if argv else args[0])
            if args[0] and argv and _name(args[0]) != _name(argv[0] if isinstance(argv, (list, tuple)) else argv):
                _check_launch([args[0]])
        elif event == "os.system":  # (command,)
            command = args[0] if args else ""
            command = os.fsdecode(command) if isinstance(command, bytes) else command
            _check_launch(["/bin/sh", "-c", command])
        elif event in ("os.posix_spawn", "os.exec"):  # (path, argv, env)
            _check_launch([args[0]] + list(args[1] or [])[1:] if len(args) > 1 else [args[0]])
        elif event == "os.spawn":  # (mode, path, args, env)
            _check_launch([args[1]] + list(args[2] or [])[1:] if len(args) > 2 else [args[1]])
    finally:
        _State.in_hook = False


_installed = False


def install() -> None:
    global _installed
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True


@contextmanager
def active() -> Iterator[list[str]]:
    """Arm the hook for one test; yields the violation list it records."""

    install()
    _State.violations = []
    _State.active = True
    try:
        yield _State.violations
    finally:
        _State.active = False


@contextmanager
def expect_violation() -> Iterator[None]:
    """For the guard's own negative tests: the block must fire, and the
    recorded violation is consumed so the per-test assertion stays clean."""

    before = len(_State.violations)
    try:
        yield
    except GuardViolation:
        del _State.violations[before:]
        return
    raise AssertionError("the real-host guard did NOT fire")
