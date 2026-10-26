#!/usr/bin/env python3
"""Shared FAIL-CLOSED runner for security-gating PreToolUse hooks (T-4392 phase 2a). Stdlib only.

Why: a Claude Code PreToolUse hook that exits non-zero-non-2, or is killed by the harness
timeout, is a NON-BLOCKING error, so the tool call PROCEEDS (hooks reference: only exit 2
blocks). A guard whose job is to deny must therefore deny on its own whenever it cannot reach a
confident ALLOW (orchestrator's ruling on the T-4392 phase-1 inventory, 2026-09-30T07:04Z):

  * exception    -- anything the hook raises (any BaseException, SystemExit included);
  * ambiguity    -- an unparseable or non-object payload, a non-dict tool_input or non-str
                    command on a Bash call, a non-dict decision, or `raise Ambiguous(...)`;
  * deadline     -- DEADLINE_S of wall clock counted from PROCESS START (the harness counts
                    its timeout from spawn), as T-4389 part A does for worker-guard;
  * cap          -- a Bash command longer than the hook's max_command_chars, checked before
                    the hook sees it.

Each of those exits 2 with ONE stderr line naming the hook and the cause. A normal verdict is
unchanged: an allow is silent exit 0, a rule deny prints the hook's own JSON and exits 0.

Usage, inside the hook (NO launcher script; the import itself must be guarded, since an
ImportError would otherwise exit 1, which fails open):

    def main():
        try:
            sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
            import _failclosed
        except BaseException as exc:
            sys.stderr.write("BLOCKED (<hook>, fail-closed, T-4392): ... " + repr(exc) + "\\n")
            sys.exit(2)
        _failclosed.run("<hook>", decide_fn)

The insert is unconditional and first, so a decoy _failclosed on PYTHONPATH cannot win.

BUDGET. DEADLINE_S is a module constant. Nothing reads it from the environment or argv (a
worker controls its own env); run(deadline_s=...) is for tests and can only SHORTEN it. The
invariant DEADLINE_S + DEADLINE_MARGIN_S <= MIN_WIRED_TIMEOUT_S is pinned by a test: the
wired Claude Code timeouts are 5000 SECONDS today on orchestrator/recovery/specialist and go to 30 once each
hook's self-deadline lands. 15s, not 5s: measured start->decision of the real wrapped
mcp-list-guard on a loaded box was p99 2.31s at 8-parallel on 2 cores and p99 3.70s (74% of a
5s budget) at 16-parallel on 1 core (load avg ~32), too close for a main-thread hook (T-4392 2a). The timer is SIGALRM (it is armed before stdin is read, so a
stdin hang is covered too; CPython's regex engine polls for signals, so a backtracking match
is interrupted), or, where SIGALRM is unavailable or we are off the main thread, a daemon
thread with a checked join, after which the process leaves via os._exit.

BREAK-GLASS (Recovery's design, T-4392 note 2026-09-30T10:44Z; worker-untriggerable, auto-
expiring). Read ONLY on a fail-closed path, never on the normal path. A flag lives at
    /var/lib/pantheon-breakglass/<account>/flag
and is honoured only if ALL hold (each level is opened with openat + O_NOFOLLOW|O_DIRECTORY and
fstat'ed; there is no full-path resolve):
  * the base is root-owned and not group/world-writable;
  * the account dir is not group/world-writable, and its owner is root or in the account's
    allowlist and is not the hook process's uid (the "permitted creator");
  * the flag is a regular file, not group/world-writable, nlink == 1, its owner is in the
    account's allowlist and is not the hook process's uid (a worker shares the seat uid and
    cannot create a file owned by another account);
  * its content parses STRICTLY (see FLAG FORMAT; anything else is ignored);
  * now < expiry <= now + 24h, AND expiry <= st_ctime + 24h (+60s slack), so a flag cannot be
    written early with a far expiry and lie dormant (ctime is set only by the kernel);
  * it names this hook or `*`. worker-guard is EXCLUDED, even under `*`.
Allowlists (names resolved with pwd at call time; uid `claude` is never allowed anywhere):
  claude/claude-research/suborch1/suborch2/exocortex -> {root, claude-fallback};
  claude-fallback (recovery's seat) -> {root}; any other account -> {root}.
Effect while honoured: that hook's FAIL-CLOSED outcomes revert to the old fail-open exit 0,
and every converted call prints a loud line on stderr AND as stdout {"systemMessage": ...}
(shown to the user; no permissionDecision, so the normal permission flow is unchanged). The
hook's normal rule logic still runs and still denies.

FLAG FORMAT (ASCII, at most MAX_FLAG_BYTES, every line newline-terminated):
    v1 <expiry-epoch-seconds> <hooks>\\n
    [reason: <printable ASCII, 1..200 chars>\\n]
<hooks> is `*` or a comma list of hook names ([a-z0-9_-]+, no `.py`), e.g.
    v1 1790000000 mcp-list-guard
Create it atomically as the permitted account (write a temp file in the same dir, then rename).

SURFACING: `python3 _failclosed.py --list [--account A | --all] [--base DIR]` prints every flag
file (LIVE or REJECTED with the reason); verify-hook-integrity.sh and verify_startup.sh call
it (soft warnings), and tools/breakglass_watch.py mails Recovery at creation and 1h before expiry.

NAMED RESIDUALS (accepted, T-4392 phase 2a rulings):
  1. Break-glass cannot rescue an IMPORT failure of this module: the break-glass code lives
     here. The hook's own guard denies with a recovery hint (run the hook from the
     pantheon-config clone, or unwire it via the Edit tool).
  2. A SyntaxError in a hook file itself, or a failure of its own module-level stdlib imports,
     still exits 1 (fail-open): nothing of the hook runs, and there is no launcher by ruling.
  3. A hook DEPLOYED AS A COPY (not the clone path or a symlink) has no _failclosed.py next to
     it and denies every call; rollout must run hooks from the clone or a symlink.
  4. Only the base and the account dir are ownership-checked; ancestors (/, /var, /var/lib)
     are walked with O_NOFOLLOW but not ownership-checked.
  5. The thread fallback (no SIGALRM) cannot interrupt a C-level regex holding the GIL; Linux
     always takes the SIGALRM path.
  6. A hook's own module-level imports and regex compiles run BEFORE main() calls run(), so the
     timer does not cover them (their time still counts against the budget, which is measured
     from process start, but a hang there is bounded only by the harness timeout).
  7. Under the ruled allowlist, a subagent on the RECOVERY seat (uid claude-fallback) can create
     break-glass flags for the orchestrator/specialist/mason/artisan/archivist seats. Accepted by design: a
     compromised recovery account already has sudo -u to those seats; every live flag is mailed to
     Recovery by tools/breakglass_watch.py and surfaced at each seat's SessionStart/boot.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time

# ---- budget ----------------------------------------------------------------------------------
DEADLINE_S = 15.0
# The smallest hook timeout a wrapped hook will be wired under (Claude Code SECONDS; the 5000s
# on orchestrator/recovery/specialist go to 30 after this lands), and the margin the deadline must leave.
MIN_WIRED_TIMEOUT_S = 30.0
DEADLINE_MARGIN_S = 5.0
_DEADLINE_FLOOR_S = 0.05
_BREAKGLASS_CHECK_S = 1.0
DEFAULT_MAX_COMMAND_CHARS = 65_536

# ---- break-glass -----------------------------------------------------------------------------
BREAKGLASS_BASE = "/var/lib/pantheon-breakglass"
FLAG_NAME = "flag"
MAX_FLAG_BYTES = 512
MAX_HORIZON_S = 24 * 3600
CTIME_SLACK_S = 60
EXCLUDED_HOOKS = frozenset({"worker-guard"})
_NEVER_OWNER = "claude"
_SEAT_ALLOWLIST = ("root", "claude-fallback")
ACCOUNT_ALLOWLISTS = {
    "claude": _SEAT_ALLOWLIST,           # orchestrator
    "claude-research": _SEAT_ALLOWLIST,  # specialist
    "suborch1": _SEAT_ALLOWLIST,         # mason
    "suborch2": _SEAT_ALLOWLIST,         # artisan
    "exocortex": _SEAT_ALLOWLIST,        # archivist
    "claude-fallback": ("root",),        # recovery: root only (recovery can sudo to every non-root account)
}
DEFAULT_ALLOWLIST = ("root",)

_REMEDY = {
    "cap": "Split the command, or write long content to a file with the Write tool and run that.",
    "deadline": ("Retry once the load eases (a transient stall is not a lockout); if it repeats "
                 "on the same command, split or simplify it. Break-glass is only for a "
                 "persistent hook failure."),
    "exception": "A hook bug or a malformed payload; report it to the hook's owner.",
    "ambiguity": "The hook could not read this call unambiguously; report it to the hook's owner.",
}


class Ambiguous(Exception):
    """Raised by a hook's decide_fn when it cannot reach a confident verdict."""


class _FailClosed(Exception):
    def __init__(self, cause: str, detail: str) -> None:
        super().__init__(f"{cause}: {detail}")
        self.cause = cause
        self.detail = detail


class _DeadlineExpired(BaseException):
    """Raised by the SIGALRM handler; a BaseException so no `except Exception` swallows it."""


def _on_alarm(signum, frame):
    raise _DeadlineExpired


# ---- clock -----------------------------------------------------------------------------------
def elapsed_since_process_start(started_monotonic: float | None = None) -> float:
    """Seconds since THIS process started (T-4389 A's mechanism): /proc/self/stat starttime
    against CLOCK_BOOTTIME (tick truncation can only over-state it: the fail-closed direction),
    else the caller's monotonic stamp, else 0.0."""
    try:
        with open("/proc/self/stat", "rb") as f:
            stat = f.read()
        fields = stat[stat.rindex(b")") + 2:].split()  # comm may hold spaces/parens
        started = int(fields[19]) / os.sysconf("SC_CLK_TCK")  # field 22
        return max(0.0, time.clock_gettime(time.CLOCK_BOOTTIME) - started)
    except Exception:
        pass
    if started_monotonic is not None:
        return max(0.0, time.monotonic() - started_monotonic)
    return 0.0


def _clamp_deadline(deadline_s) -> float:
    # Only a value in (0, DEADLINE_S] is honoured: 0 would disarm setitimer and NaN compares false.
    if deadline_s is not None and 0 < deadline_s <= DEADLINE_S:
        return float(deadline_s)
    return DEADLINE_S


def _call_with_deadline(fn, budget: float):
    """Return ("done", value) or ("timeout", abandoned_thread). fn's exceptions propagate."""
    if hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer") \
            and threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGALRM, _on_alarm)
        try:
            signal.setitimer(signal.ITIMER_REAL, budget)
            try:
                return "done", fn()
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
        except _DeadlineExpired:
            return "timeout", False
        finally:
            if previous is not None:
                signal.signal(signal.SIGALRM, previous)
    return _call_on_thread(fn, budget)


def _call_on_thread(fn, budget: float):
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed back below
            box["error"] = exc

    worker = threading.Thread(target=target, name="failclosed-decide", daemon=True)
    worker.start()
    worker.join(budget)
    if worker.is_alive():
        return "timeout", True
    if "error" in box:
        raise box["error"]
    return "done", box.get("value")


# ---- the runner ------------------------------------------------------------------------------
def _evaluate(decide_fn, stdin, max_command_chars: int):
    raw = (stdin if stdin is not None else sys.stdin).read()
    if not raw.strip():
        data = {}
    else:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise _FailClosed("ambiguity", f"unparseable hook payload ({type(exc).__name__})")
    if not isinstance(data, dict):
        raise _FailClosed("ambiguity", "hook payload is not a JSON object")
    if data.get("tool_name") == "Bash":
        tool_input = data.get("tool_input")
        if tool_input is not None and not isinstance(tool_input, dict):
            raise _FailClosed("ambiguity", "Bash tool_input is not an object")
        command = (tool_input or {}).get("command")
        if command is not None and not isinstance(command, str):
            raise _FailClosed("ambiguity", "Bash command is not a string")
        if command is not None and len(command) > max_command_chars:
            raise _FailClosed(
                "cap", f"the Bash command is {len(command)} characters, over this hook's "
                f"{max_command_chars}-character cap")
    payload = decide_fn(data)
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise _FailClosed("ambiguity", f"the hook's decision is a {type(payload).__name__}, not a dict")
    return json.dumps(payload)


def evaluate(hook_name: str, decide_fn, *, max_command_chars: int = DEFAULT_MAX_COMMAND_CHARS,
             deadline_s: float | None = None, stdin=None, started_monotonic: float | None = None,
             _bg_policy: dict | None = None):
    """Run one hook call; return (exit_code, stdout_text, stderr_text, abandoned_thread).

    run() is the entry point; this is split out so tests can drive it in-process."""
    limit = _clamp_deadline(deadline_s)
    at_start = elapsed_since_process_start(started_monotonic)
    remaining = limit - at_start
    if not remaining > 0:
        return _fail_closed(hook_name, "deadline",
                            f"the {limit:g}s budget was already spent at start "
                            f"({at_start:.2f}s since process start)", _bg_policy) + (False,)
    budget = min(limit, max(_DEADLINE_FLOOR_S, remaining))
    try:
        status, value = _call_with_deadline(
            lambda: _evaluate(decide_fn, stdin, max_command_chars), budget)
    except _FailClosed as fc:
        return _fail_closed(hook_name, fc.cause, fc.detail, _bg_policy) + (False,)
    except Ambiguous as exc:
        return _fail_closed(hook_name, "ambiguity", str(exc) or "the hook reported ambiguity",
                            _bg_policy) + (False,)
    except BaseException as exc:  # noqa: BLE001 - every other failure is a fail-closed deny
        return _fail_closed(hook_name, "exception",
                            f"{type(exc).__name__}: {exc}"[:300] + _where(exc),
                            _bg_policy) + (False,)
    if status == "timeout":
        return _fail_closed(hook_name, "deadline",
                            f"not decided within the {limit:g}s budget from process start "
                            f"({at_start:.2f}s had elapsed before the hook body started, "
                            f"{elapsed_since_process_start(started_monotonic):.2f}s at the deny)",
                            _bg_policy) + (bool(value),)
    return 0, value or "", "", False


def _where(exc: BaseException) -> str:
    """' (at file:line in func)' for the innermost frame, so a CI deny is self-diagnosing."""
    try:
        import traceback
        frames = traceback.extract_tb(exc.__traceback__)
        if frames:
            f = frames[-1]
            return f" (at {os.path.basename(f.filename)}:{f.lineno} in {f.name})"
    except Exception:
        pass
    return ""


def run(hook_name: str, decide_fn, *, max_command_chars: int = DEFAULT_MAX_COMMAND_CHARS,
        deadline_s: float | None = None, stdin=None, started_monotonic: float | None = None,
        _bg_policy: dict | None = None):
    """Decide one PreToolUse call fail-closed and exit (see the module docstring)."""
    code, out, err, abandoned = evaluate(
        hook_name, decide_fn, max_command_chars=max_command_chars, deadline_s=deadline_s,
        stdin=stdin, started_monotonic=started_monotonic, _bg_policy=_bg_policy)
    if out:
        sys.stdout.write(out + "\n")
    if err:
        sys.stderr.write(err)
    if abandoned:  # a daemon thread may be stuck in C; do not wait on finalization
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    sys.exit(code)


def _fail_closed(hook_name: str, cause: str, detail: str, policy: dict | None):
    """(exit_code, stdout, stderr) for a fail-closed outcome, after the break-glass check."""
    flag = None
    if hook_name not in EXCLUDED_HOOKS:
        try:
            flag = _breakglass_bounded(hook_name, policy)
        except BaseException:  # noqa: BLE001 - any doubt means no break-glass
            flag = None
    if flag is not None:
        loud = (f"BREAK-GLASS ACTIVE for {hook_name} on {flag.account}: would have DENIED "
                f"({cause}: {detail}); allowing (fail-open) -- flag owner={flag.owner_name} "
                f"expires={_iso(flag.expiry)} (T-4392)")
        return 0, json.dumps({"systemMessage": loud}), loud + "\n"
    msg = (f"BLOCKED ({hook_name}, fail-closed, T-4392): {cause}: {detail}. "
           f"{_REMEDY.get(cause, '')}").rstrip()
    return 2, "", msg + "\n"


def _breakglass_bounded(hook_name: str, policy: dict | None):
    """The break-glass check under its own short alarm; a timeout means not honoured."""
    status, value = _call_with_deadline(lambda: breakglass_for(hook_name, policy), _BREAKGLASS_CHECK_S)
    if status != "done":
        return None
    return value


# ---- break-glass -----------------------------------------------------------------------------
class FlagStatus:
    """One flag file as found: live (honourable) or rejected, with the reason."""

    __slots__ = ("account", "live", "reason", "owner_uid", "owner_name", "expiry", "hooks",
                 "note", "ino", "ctime_ns")

    def __init__(self, account, live=False, reason="", owner_uid=None, owner_name=None,
                 expiry=None, hooks=(), note=None, ino=None, ctime_ns=None):
        self.account = account
        self.live = live
        self.reason = reason
        self.owner_uid = owner_uid
        self.owner_name = owner_name
        self.expiry = expiry
        self.hooks = tuple(hooks)
        self.note = note
        self.ino = ino
        self.ctime_ns = ctime_ns

    def names(self, hook_name: str) -> bool:
        return hook_name not in EXCLUDED_HOOKS and ("*" in self.hooks or hook_name in self.hooks)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


def _iso(epoch) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))
    except Exception:
        return str(epoch)


def _uid_of(name: str):
    import pwd
    try:
        return pwd.getpwnam(name).pw_uid
    except (KeyError, TypeError):
        return None


def _name_of(uid) -> str:
    import pwd
    try:
        return pwd.getpwuid(uid).pw_name
    except (KeyError, TypeError, OverflowError):
        return str(uid)


def current_account() -> str:
    return _name_of(os.geteuid())


def allowlist_uids(account: str, hook_uids) -> frozenset:
    """The uids allowed to own a flag for `account`, resolved now; never claude, never the hook."""
    uids = {_uid_of(n) for n in ACCOUNT_ALLOWLISTS.get(account, DEFAULT_ALLOWLIST)}
    uids.discard(None)
    uids.discard(_uid_of(_NEVER_OWNER))
    return frozenset(uids - set(hook_uids))


def _default_hook_uids(account: str) -> frozenset:
    uids = set()
    acct_uid = _uid_of(account)
    if acct_uid is not None:
        uids.add(acct_uid)
    if account == current_account():
        uids.update((os.getuid(), os.geteuid()))
    return frozenset(uids)


def _safe_component(name: str) -> bool:
    return bool(name) and name not in (".", "..") and "/" not in name and "\0" not in name


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_dir_nofollow(path: str) -> int:
    """openat walk from `/`: every component with O_NOFOLLOW|O_DIRECTORY (a symlink -> ELOOP)."""
    if not path.startswith("/"):
        raise OSError(f"not absolute: {path!r}")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    try:
        for part in [p for p in path.split("/") if p]:
            if not _safe_component(part):
                raise OSError(f"unsafe path component {part!r}")
            nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _gw_writable(st) -> bool:
    return bool(st.st_mode & 0o022)


def parse_flag(content: bytes):
    """(expiry, hooks, reason_or_None) for a strictly well-formed flag, else None."""
    import re
    if len(content) > MAX_FLAG_BYTES:
        return None
    m = re.fullmatch(
        rb"v1 ([1-9][0-9]{0,11}) (\*|[a-z0-9_-]+(?:,[a-z0-9_-]+)*)\n"
        rb"(?:reason: ([\x20-\x7e]{1,200})\n)?", content)
    if m is None:
        return None
    hooks = tuple(m.group(2).decode("ascii").split(","))
    reason = m.group(3).decode("ascii") if m.group(3) else None
    return int(m.group(1)), hooks, reason


def inspect_flag(account: str, *, base: str = BREAKGLASS_BASE, root_uid: int = 0,
                 hook_uids=None, allowed_owner_uids=None, now: float | None = None):
    """FlagStatus for `account`'s flag file, or None when there is no flag file (or no dir)."""
    import stat as st_mod
    if not _safe_component(account):
        return FlagStatus(account, reason="unsafe account name")
    hook_uids = frozenset(_default_hook_uids(account) if hook_uids is None else hook_uids)
    allowed = frozenset(allowlist_uids(account, hook_uids) if allowed_owner_uids is None
                        else allowed_owner_uids) - hook_uids
    now = time.time() if now is None else now
    try:
        base_fd = _open_dir_nofollow(base)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return FlagStatus(account, reason=f"base not walkable without symlinks ({exc.strerror or exc})")
    # A failed base/dir check is only REPORTED when a flag file actually exists, so a listing
    # stays silent where there is nothing to honour; it still decides first (in walk order).
    pending = None
    try:
        bst = os.fstat(base_fd)
        if bst.st_uid != root_uid or _gw_writable(bst):
            pending = "base is not root-owned or is group/world-writable"
        try:
            acct_fd = os.open(account, _DIR_FLAGS, dir_fd=base_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            return FlagStatus(account, reason=pending or f"account dir unusable ({exc.strerror or exc})")
        try:
            ast = os.fstat(acct_fd)
            if pending is None and _gw_writable(ast):
                pending = "account dir is group/world-writable"
            # `allowed` already excludes the hook's uids; a root-owned dir is always a permitted creator.
            if pending is None and not (ast.st_uid == root_uid or ast.st_uid in allowed):
                pending = f"account dir owner {_name_of(ast.st_uid)} is not a permitted creator"
            try:
                ffd = os.open(FLAG_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                              | getattr(os, "O_CLOEXEC", 0), dir_fd=acct_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                return FlagStatus(account, reason=pending
                                  or f"flag not openable as a plain file ({exc.strerror or exc})")
            try:
                fst = os.fstat(ffd)
                owner = _name_of(fst.st_uid)
                common = dict(owner_uid=fst.st_uid, owner_name=owner, ino=fst.st_ino,
                              ctime_ns=fst.st_ctime_ns)
                if pending is not None:
                    return FlagStatus(account, reason=pending, **common)
                if not st_mod.S_ISREG(fst.st_mode):
                    return FlagStatus(account, reason="flag is not a regular file", **common)
                if _gw_writable(fst):
                    return FlagStatus(account, reason="flag is group/world-writable", **common)
                if fst.st_nlink != 1:
                    return FlagStatus(account, reason=f"flag has nlink {fst.st_nlink}", **common)
                if fst.st_uid in hook_uids:
                    return FlagStatus(account, reason="flag is owned by the hook's own uid", **common)
                if fst.st_uid not in allowed:
                    return FlagStatus(account, reason=f"flag owner {owner} is not in the allowlist", **common)
                chunks, total = [], 0
                while total <= MAX_FLAG_BYTES:
                    chunk = os.read(ffd, MAX_FLAG_BYTES + 1 - total)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                parsed = parse_flag(b"".join(chunks))
                if parsed is None:
                    return FlagStatus(account, reason="flag content is malformed", **common)
                expiry, hooks, note = parsed
                more = dict(common, expiry=expiry, hooks=hooks, note=note)
                if not expiry > now:
                    return FlagStatus(account, reason="flag has expired", **more)
                if expiry > now + MAX_HORIZON_S:
                    return FlagStatus(account, reason="flag expiry is more than 24h out", **more)
                if expiry > fst.st_ctime + MAX_HORIZON_S + CTIME_SLACK_S:
                    return FlagStatus(account, reason="flag expiry is more than 24h after its ctime", **more)
                return FlagStatus(account, live=True, reason="live", **more)
            finally:
                os.close(ffd)
        finally:
            os.close(acct_fd)
    finally:
        os.close(base_fd)


def breakglass_for(hook_name: str, policy: dict | None = None):
    """The LIVE FlagStatus naming `hook_name` for this process's account, else None.
    `policy` overrides inspect_flag's keywords (and `account`) for tests only."""
    if hook_name in EXCLUDED_HOOKS:
        return None
    policy = dict(policy or {})
    account = policy.pop("account", None) or current_account()
    status = inspect_flag(account, **policy)
    if status is not None and status.live and status.names(hook_name):
        return status
    return None


def list_flags(accounts=None, *, base: str = BREAKGLASS_BASE, now: float | None = None):
    """FlagStatus for every account dir with a flag file (all dirs under the base by default)."""
    if accounts is None:
        try:
            fd = _open_dir_nofollow(base)
        except OSError:
            return []
        try:
            accounts = sorted(n for n in os.listdir(fd) if _safe_component(n))
        finally:
            os.close(fd)
    out = []
    for account in accounts:
        try:
            status = inspect_flag(account, base=base, now=now)
        except Exception as exc:  # a listing must never crash its caller
            status = FlagStatus(account, reason=f"inspect failed ({type(exc).__name__})")
        if status is not None:
            out.append(status)
    return out


def format_status(s: FlagStatus, now: float | None = None) -> str:
    now = time.time() if now is None else now
    if s.live:
        left = int(s.expiry - now)
        note = f" reason={s.note!r}" if s.note else ""
        return (f"LIVE {s.account}: hooks={','.join(s.hooks)} owner={s.owner_name} "
                f"expires={_iso(s.expiry)} (in {left // 3600}h{(left % 3600) // 60:02d}m){note}")
    owner = f" (owner={s.owner_name})" if s.owner_name else ""
    return f"REJECTED {s.account}: {s.reason}{owner}"


def _main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="List break-glass flags (read-only; T-4392).")
    p.add_argument("--list", action="store_true", required=True)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--account")
    g.add_argument("--all", action="store_true")
    p.add_argument("--base", default=BREAKGLASS_BASE, help="tests only")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    try:
        accounts = None if args.all else [args.account or current_account()]
        statuses = list_flags(accounts, base=args.base)
    except Exception as exc:  # surfacing is soft: never fail the caller
        print(f"break-glass listing failed: {exc!r}", file=sys.stderr)
        return 0
    for s in statuses:
        print(json.dumps(s.as_dict()) if args.json else format_status(s))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
