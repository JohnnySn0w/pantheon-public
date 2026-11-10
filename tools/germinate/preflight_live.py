"""The LIVE read-only preflight reader (T-5921; ORG-080 §1, T-3191 i/ii/viii): DARK per seat.

``LivePreflightPort`` satisfies ``engine/preflight.PreflightPort`` against the real
host, READ-ONLY: it observes and never changes anything. It lives here, outside
engine/, because the engine safety lint bans process reach there
(engine/tests/test_safety_lint.py); nothing under engine/ imports this module.

DARK BY CONSTRUCTION. ``LIVE_READER_SEATS`` maps a TARGET seat account to the literal
``False`` (no env var, no flag, no override; the host-substrate pattern,
host_substrate/ports.py:56). The constructor refuses unless the pin for the seat it is
built for is the literal ``True``. Enabling a seat (artisan Phase 0: ``suborch2``) is a
separate, reviewed one-line change. A seat not listed refuses.

What it observes:

* ``operator()`` -- in-process: ``os.getuid`` / ``pwd`` and the short hostname.
* ``sudo_listing()`` -- ``SUDO_LIST_ARGV`` (``sudo -n -l`` under a cleared, fixed
  environment, ``LC_ALL=C``): exit code, stdout and stderr, classified by
  ``engine/preflight.sudo_outcome``. ``-n`` never prompts.
* ``checkout(repo)`` -- ``git rev-parse HEAD`` and ``git status --porcelain=v1``
  (``git_argv``), both with ``--no-optional-locks`` so git writes no index lock.
  Clean means the porcelain output is empty (untracked files count as dirty).
* the component probe (``path_stat`` / ``file_sha256`` / ``read_json`` /
  ``account``) -- in-process ``lstat`` / ``O_NOFOLLOW`` reads / ``pwd``; no process.

Every command is a FIXED argv list run through an injected ``CommandRunner``
(tools/_organ_lifecycle.py); the default ``BoundedRunner`` adds a timeout and no
stdin. A runner failure, timeout or unexpected output is ``Unobservable`` (a NO-GO
in the receipt, never a pass).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from tools._organ_lifecycle import CommandResult, CommandRunner
from tools.germinate.engine import preflight as PF

#: Per TARGET seat account: the literal False until a reviewed flip (T-5921 ruling Q2).
LIVE_READER_SEATS: Final[Mapping[str, bool]] = {"suborch2": False}

TIER_LIVE_READONLY: Final = "live-readonly"

ENV: Final = "/usr/bin/env"
SUDO: Final = "/usr/bin/sudo"
GIT: Final = "/usr/bin/git"
SAFE_PATH: Final = "PATH=/usr/bin:/bin"

SUDO_LIST_ARGV: Final = (ENV, "-i", SAFE_PATH, "LC_ALL=C", SUDO, "-n", "-l")

COMMAND_TIMEOUT_SECONDS: Final = 15
MAX_SHA_BYTES: Final = 1 << 20
MAX_JSON_BYTES: Final = 1 << 16
_REV_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class LiveReaderDisabled(RuntimeError):
    """The live reader is dark for this seat (LIVE_READER_SEATS)."""


def git_argv(home: str, repo: str, *args: str) -> tuple[str, ...]:
    """The fixed git argv: cleared environment (HOME for git's config lookup only), C locale,
    ``--no-optional-locks`` (read-only: no index lock), ``-C <repo>``."""

    return (ENV, "-i", SAFE_PATH, f"HOME={home}", "LC_ALL=C", GIT, "--no-optional-locks", "-C", repo, *args)


class BoundedRunner:
    """The live default runner: argv only (no shell), no stdin, a timeout."""

    def __init__(self, timeout: int = COMMAND_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout

    def run(self, argv: Sequence[str]) -> CommandResult:
        completed = subprocess.run(
            list(argv), capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL, timeout=self.timeout
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


_KINDS = (
    (stat.S_ISREG, "file"), (stat.S_ISDIR, "directory"), (stat.S_ISLNK, "symlink"), (stat.S_ISSOCK, "socket"),
    (stat.S_ISFIFO, "fifo"), (stat.S_ISCHR, "char"), (stat.S_ISBLK, "block"),
)


def _kind(mode: int) -> str:
    for test, name in _KINDS:
        if test(mode):
            return name
    raise PF.Unobservable("unknown file type")


class LivePreflightPort:
    """The real-host, READ-ONLY preflight port for one target seat. DARK (see module doc)."""

    tier = TIER_LIVE_READONLY

    def __init__(self, seat: str, *, runner: CommandRunner | None = None) -> None:
        if LIVE_READER_SEATS.get(seat) is not True:
            raise LiveReaderDisabled(
                f"the live preflight reader is dark for seat {seat!r} (LIVE_READER_SEATS; a reviewed flip enables it)"
            )
        self.seat = seat
        self._runner: CommandRunner = runner if runner is not None else BoundedRunner()

    # --- operator, sudo, checkout ------------------------------------------------

    def operator(self) -> PF.OperatorIdentity:
        uid = os.getuid()
        try:
            user = pwd.getpwuid(uid).pw_name
        except KeyError:
            raise PF.Unobservable(f"uid {uid} has no passwd entry") from None
        return PF.OperatorIdentity(user, uid, os.uname().nodename.split(".", 1)[0])

    def _run(self, argv: Sequence[str]) -> CommandResult:
        try:
            return self._runner.run(list(argv))
        except (OSError, subprocess.SubprocessError) as exc:
            raise PF.Unobservable(f"{argv[-1] if argv else '?'}: {type(exc).__name__}: {exc}") from None

    def sudo_listing(self) -> PF.SudoListing:
        result = self._run(SUDO_LIST_ARGV)
        return PF.SudoListing(result.returncode, result.stdout, result.stderr)

    def checkout(self, repo_root: Path) -> PF.CheckoutObservation:
        repo = PF.checked_path(str(Path(repo_root)))
        home = self.operator_home()
        head = self._run(git_argv(home, repo, "rev-parse", "HEAD"))
        if head.returncode != 0:
            raise PF.Unobservable(f"git rev-parse HEAD exited {head.returncode}: {head.stderr.strip()[:200]}")
        rev = head.stdout.strip()
        if not _REV_RE.match(rev):
            raise PF.Unobservable(f"git rev-parse HEAD printed {rev[:80]!r}, not a full commit id")
        status = self._run(git_argv(home, repo, "status", "--porcelain=v1", "--untracked-files=normal"))
        if status.returncode != 0:
            raise PF.Unobservable(f"git status exited {status.returncode}: {status.stderr.strip()[:200]}")
        return PF.CheckoutObservation(rev, status.stdout == "")

    def operator_home(self) -> str:
        try:
            return pwd.getpwuid(os.getuid()).pw_dir
        except KeyError:
            raise PF.Unobservable("the operator has no passwd entry") from None

    # --- the component probe (in-process, read-only, never follows a link) --------

    def path_stat(self, path: str) -> PF.PathStat | None:
        path = PF.checked_path(path)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PF.Unobservable(f"lstat {path}: {exc.strerror}") from None
        return PF.PathStat(_kind(st.st_mode), st.st_uid, st.st_gid, f"{stat.S_IMODE(st.st_mode):04o}")

    def _read(self, path: str, limit: int) -> bytes | None:
        path = PF.checked_path(path)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as exc:
            why = "a symlink (never followed)" if exc.errno == errno.ELOOP else exc.strerror
            raise PF.Unobservable(f"open {path}: {why}") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise PF.Unobservable(f"{path} is not a regular file")
            chunks, total = [], 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise PF.Unobservable(f"{path} is larger than {limit} bytes")
                chunks.append(chunk)
        finally:
            os.close(fd)
        return b"".join(chunks)

    def file_sha256(self, path: str) -> str | None:
        data = self._read(path, MAX_SHA_BYTES)
        return None if data is None else hashlib.sha256(data).hexdigest()

    def read_json(self, path: str) -> Any:
        data = self._read(path, MAX_JSON_BYTES)
        if data is None:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PF.Unobservable(f"{path} is not JSON: {exc}") from None

    def account(self, name: str) -> PF.AccountInfo | None:
        name = PF.checked_account(name)
        try:
            entry = pwd.getpwnam(name)
        except KeyError:
            return None
        return PF.AccountInfo(entry.pw_uid, entry.pw_dir)


def live_port_factory(seat: str | None) -> LivePreflightPort:
    """What ``germinate plan --preflight --observation-tier live`` wires (tools/germinate/cli.py)."""

    if not seat:
        raise LiveReaderDisabled("the live reader needs a seat scope (--seat ACCOUNT)")
    return LivePreflightPort(seat)
