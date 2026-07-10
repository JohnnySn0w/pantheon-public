#!/usr/bin/env python3
"""Pantheon ticket lifecycle CLI.

Implements the ticket system ratified in `adr/ORG-003-ticket-system.md`
(amended by ORG-019: `blocked/` state retired; blocks expressed via
`not_before` or `depends_on` frontmatter only).

States: inbox → active → done  (3-state machine, no blocked/).

Subcommands:
    ticket.py file --requester X --target Y --title "..." [--labels A,B] [--depends-on T-NNNN,...] [--stale-after 4h] [--ask "..."] [--context "..."]
    ticket.py claim <T-NNNN>
    ticket.py block <T-NNNN> (--not-before YYYY-MM-DD | --depends-on T-NNNN) [repeatable --depends-on]
    ticket.py resolve <T-NNNN> [--with "summary"]
    ticket.py list [--state inbox|active|done] [--target X] [--label X]

`list` is read-only. Every other subcommand performs exactly one
filesystem-change-plus-commit-plus-push, gated on the same auto-commit
discipline as `tools/watch_runner.py` (HERMES guard, narrow scope, ff-only
pull, plain push, no force, no --no-verify).

`block` is a frontmatter-edit command (no state change / no dir move). It
sets `not_before` and/or `depends_on` on the ticket in place. The ticket
stays in its current state directory (inbox/ or active/).

Actor identity for transition-log entries comes from `$PANTHEON_ACTOR` env
var if set, else the OS username (`getpass.getuser()`).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import getpass
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Make `from tools._git_utils import ...` work both as a script
# (`python3 tools/ticket.py ...` per the docstring's documented usage) and
# as a package import (`python3 -m unittest tools.test_ticket`). When
# invoked as a script, sys.path prepends `tools/` rather than the repo root,
# so the `tools` package isn't reachable; this bootstrap injects the repo
# root in that case. In package mode `__package__ == "tools"`, no-op.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._git_utils import (  # noqa: E402  — after sys.path bootstrap.
    _git,
    _hermes_scan_file,
    _hermes_scan_message,
    pull_or_rebase_strict,
    push_with_retry,
)

# T-0761 c4: clone-on-demand repo provisioning (called after a successful claim).
# Guarded top-level import: module may be absent in isolated worktrees used by
# tests that spawn ticket.py as a subprocess (e.g. TestRetargetDetachedHeadPush).
# When absent, provision_repos is set to None and cmd_claim skips provisioning
# gracefully. Module-level name is required so test patches resolve cleanly.
try:
    from tools.provision_repos import provision_repos  # noqa: E402
except ImportError:
    provision_repos = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parent.parent
TICKETS_ROOT = REPO_ROOT / "tickets"

STATES = ("inbox", "active", "done")  # ORG-019: blocked/ retired

# MUST stay in sync with sibling at pantheon-pwa/src/server/ticketRoutes.ts
RATIFIABLE_REPO_ALLOWLIST = ("pantheon-config", "revenue-project", "echo-prismatic", "pantheon-pwa")

FRONT_MATTER_FIELD_ORDER = (
    "id",
    "created",
    "requester",
    "target",
    "state",
    "parent_ticket",
    "depends_on",
    "labels",
    "stale_after",
    "not_before",
    "ratifiable_files",
    "intent",
    "serves",
)
# ORG-019: blocked_by / unblock_when / unblock_check removed from schema.
# Legacy tickets may still carry these fields; serialize_front_matter emits
# them via the "unknown keys" fallback at the end so existing data round-trips
# without data loss until Phase 3 migration strips them.
REQUIRED_FRONT_MATTER = ("id", "created", "requester", "target", "state")
OPTIONAL_FRONT_MATTER = (
    "parent_ticket",
    "depends_on",
    "labels",
    "stale_after",
    "not_before",
    "ratifiable_files",
    "intent",
    "serves",
)
LIST_FIELDS = ("depends_on", "labels", "ratifiable_files")

# T-0465: `intent` frontmatter disambiguates target=operator tickets.
# action  — "the operator, do X" (operator action request; default for target=operator)
# ratify  — target=operator with ratifiable_files (per ORG-015)
# noodle  — "the operator's eye welcome; no urgent action" (design-noodle, parking-the-question)
# Not set for target != operator (the field is target=operator-specific surface).
INTENT_VOCAB = ("action", "ratify", "noodle")

TICKET_FILENAME_RE = re.compile(r"T-(\d{4})\.md$")

# ---------------------------------------------------------------------------
# Allocation lock (T-0786, amended T-0823)
# ---------------------------------------------------------------------------
# A blocking LOCK_EX flock serializes all concurrent writers on this machine
# (claude, claude-fallback, kino, claude-research) through the
# allocate→write phase of ticket creation.
# Single-machine assumption: fcntl.flock semantics are only reliable on a
# local filesystem; NFS is NOT supported.
#
# The lockfile lives in /tmp (sticky, world-writable dir) with mode 0o666.
# Under the host's fs.protected_regular=2 kernel hardening, an O_CREAT open
# of a regular file the opener does NOT own in a sticky world-writable dir is
# refused (EPERM).  To avoid this, the primary open path uses plain O_WRONLY
# (no O_CREAT) so non-owner uids can open the existing lockfile safely.
# O_CREAT is used only when the file is genuinely absent (ENOENT), wrapped in
# umask(0) so the created file lands at 0o666.  A create-race fallback handles
# the TOCTOU between the ENOENT return and the O_CREAT attempt: if another uid
# wins the race and creates the file first, the O_CREAT call triggers the same
# EPERM; we catch PermissionError (and FileExistsError) and fall back to a
# plain O_WRONLY open of the now-existing file.
#
# Env-overridable via PANTHEON_TICKET_ALLOC_LOCKFILE for test isolation.

_DEFAULT_ALLOC_LOCKFILE = "/tmp/pantheon_ticket_alloc.lock"


def _alloc_lockfile() -> str:
    """Return the flock path for allocation serialization.

    Env-overridable via PANTHEON_TICKET_ALLOC_LOCKFILE so tests can use
    isolated paths without clobbering the production lockfile.
    Default: /tmp/pantheon_ticket_alloc.lock.
    """
    return os.environ.get("PANTHEON_TICKET_ALLOC_LOCKFILE") or _DEFAULT_ALLOC_LOCKFILE


def _open_world_writable(path: str):  # type: ignore[return]
    """Open (or create) path with mode 0o666, bypassing the process umask.

    Primary path: open WITHOUT O_CREAT so that, under fs.protected_regular=2,
    a uid that does not own the file can still open it for write when it
    already exists.

    O_CREAT is used only when the file is genuinely absent (FileNotFoundError
    on the no-O_CREAT open).  The O_CREAT open is wrapped in umask(0) so the
    created file lands at mode 0o666 (world-writable) regardless of the
    process umask.

    Create-race fallback: if the file is absent when we try O_WRONLY but
    another uid creates it before our O_CREAT reaches the kernel, the O_CREAT
    call hits the same protected_regular=2 refusal (PermissionError) or a
    FileExistsError on some kernels.  Both are caught; we fall back to a plain
    O_WRONLY open of the now-existing file.  This fallback is still inside the
    umask(0) / umask-restore finally block — the umask is irrelevant for a
    no-create open but keeping it inside the finally is simpler than a
    separate try/finally.

    Returns an open file object.  Caller is responsible for closing it.
    """
    try:
        fd = os.open(path, os.O_WRONLY)          # existing → protected_regular-safe
    except FileNotFoundError:
        old_umask = os.umask(0)
        try:
            fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o666)
        except (FileExistsError, PermissionError):
            fd = os.open(path, os.O_WRONLY)      # lost create race → file exists now
        finally:
            os.umask(old_umask)
    return os.fdopen(fd, "w")


@contextlib.contextmanager
def _allocation_lock():
    """Context manager: hold a blocking LOCK_EX flock for the allocate→write phase.

    Callers must wrap both _next_ticket_id() AND new_path.write_text() inside
    this context so no other writer can scan the same max between our scan and
    our write.  The lock is released on exit (before _commit_and_push), so the
    slow network push is NOT serialized.

    Usage::

        with _allocation_lock():
            tid = _next_ticket_id()
            ...build fields / text...
            new_path.write_text(text, encoding="utf-8")
        # lock released here
        _commit_and_push(...)   # outside the lock

    LOCK_EX is blocking (not LOCK_NB): writers queue for their turn rather
    than failing.  Single-machine assumption: fcntl.flock; NFS not supported.
    """
    lockfile = _alloc_lockfile()
    lock_fd = _open_world_writable(lockfile)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class TicketError(Exception):
    """Raised for any user-surfaced lifecycle / parse failure."""


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _actor() -> str:
    """Identity to record in transition-log entries.

    Resolution order: `$PANTHEON_ACTOR` env var, then `getpass.getuser()`.
    """
    explicit = os.environ.get("PANTHEON_ACTOR", "").strip()
    if explicit:
        return explicit
    return getpass.getuser()


# OS account → persona, for the one-active WIP cap (T-0877). A clone's OS user
# identifies its persona even when PANTHEON_ACTOR is unset.
_OS_USER_PERSONA: dict[str, str] = {
    "claude": "orchestrator",
    "claude-research": "specialist",
    "claude-fallback": "recovery",
}


def _claiming_persona() -> str:
    """Resolve the claiming agent's canonical persona for the WIP cap.

    Maps the actor (`$PANTHEON_ACTOR` or OS user) → persona: OS account first
    (claude/claude-research/claude-fallback), else role-id/persona via
    `_resolve_persona()`. An unrecognized identity passes through unchanged,
    so the guard fails OPEN — it never blocks a claim it cannot attribute.
    """
    raw = _actor()
    if raw in _OS_USER_PERSONA:
        return _OS_USER_PERSONA[raw]
    return _resolve_persona(raw)


def _now_iso() -> str:
    """UTC ISO 8601 timestamp, second precision, Z-suffixed."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_not_before(raw: str) -> str:
    """Validate and normalize a not_before value to full ISO 8601 UTC form.

    Accepts:
      - Bare date ``YYYY-MM-DD`` → normalized to ``YYYY-MM-DDT00:00:00Z``
      - Full datetime ``YYYY-MM-DDTHH:MM:SSZ`` → stored as-is

    Raises TicketError on anything else.
    """
    # Bare date: YYYY-MM-DD
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            datetime.fromisoformat(raw)
        except ValueError:
            raise TicketError(f"not_before: invalid date: {raw!r}")
        return raw + "T00:00:00Z"
    # Full ISO 8601 UTC: YYYY-MM-DDTHH:MM:SSZ
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", raw):
        try:
            # Normalize the trailing 'Z' → '+00:00': datetime.fromisoformat only
            # accepts a bare 'Z' on Python 3.11+, but the documented bring-up
            # baseline is >=3.10 (T-1383 / ORG-A4). The regex above already
            # guarantees a valid shape; this keeps the validation portable.
            datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise TicketError(f"not_before: invalid datetime: {raw!r}")
        return raw
    raise TicketError(
        f"not_before: expected YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ, got: {raw!r}"
    )


def _validate_ratifiable_path_part(path: str) -> str:
    """Validate the path portion of a ratifiable_files entry.

    Rules (P3 structural-invariant guard against directory traversal):
      - Must be non-empty.
      - Must be relative (no leading ``/``).
      - Must not start with ``./``.
      - Must not contain ``..`` segment.
      - Must not contain empty segments (double slashes or trailing slash).
    """
    if not path:
        raise TicketError("ratifiable_files: path part must be non-empty")
    if path.startswith("/") or path.startswith("./"):
        raise TicketError(
            f"ratifiable_files: path must be a clean relative path (no leading "
            f"'/' or './'), got: {path!r}"
        )
    parts = path.split("/")
    if any(p == ".." for p in parts):
        raise TicketError(
            f"ratifiable_files: path must not contain '..' segments (no traversal), "
            f"got: {path!r}"
        )
    if any(p == "" for p in parts):
        raise TicketError(
            f"ratifiable_files: path must not contain empty segments / double slashes, "
            f"got: {path!r}"
        )
    return path


def _parse_ratifiable_files(entries: list[str]) -> list[str]:
    """Validate a ratifiable_files list of ``<repo>:<path>`` strings.

    Each entry must:
      - Contain exactly one ``:`` separator.
      - Have a repo prefix present in RATIFIABLE_REPO_ALLOWLIST.
      - Have a path part passing _validate_ratifiable_path_part rules.

    Raises TicketError on any rule violation.
    """
    validated: list[str] = []
    for entry in entries:
        if ":" not in entry:
            raise TicketError(
                f"ratifiable_files: each entry must be '<repo>:<path>', "
                f"missing ':' in: {entry!r}"
            )
        repo, path = entry.split(":", 1)
        if repo not in RATIFIABLE_REPO_ALLOWLIST:
            raise TicketError(
                f"ratifiable_files: repo {repo!r} not in allowlist "
                f"{RATIFIABLE_REPO_ALLOWLIST}"
            )
        _validate_ratifiable_path_part(path)
        validated.append(entry)
    return validated


# ---------------------------------------------------------------------------
# Front-matter parser / serializer.
# ---------------------------------------------------------------------------


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Split a ticket file's text into (front_matter_fields, body).

    Front matter is delimited by leading `---\\n` and a closing `---\\n`. Each
    line within is `key: value`. Values are scalars, `null`, or single-line
    flow-style lists `[a, b, c]`. No multi-line strings, no nested mappings.
    """
    if not text.startswith("---\n"):
        raise TicketError("front matter: missing opening `---` delimiter")
    rest = text[4:]
    end = rest.find("\n---\n")
    if end == -1:
        # Allow trailing `---` with no newline — rare hand-edit case.
        if rest.endswith("\n---"):
            fm_block = rest[: -len("\n---")]
            body = ""
        else:
            raise TicketError("front matter: missing closing `---` delimiter")
    else:
        fm_block = rest[:end]
        body = rest[end + len("\n---\n") :]

    fields: dict = {}
    for raw_line in fm_block.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        m = re.match(r"^([a-z_][a-z0-9_]*):\s*(.*)$", line)
        if not m:
            raise TicketError(f"front matter: cannot parse line: {raw_line!r}")
        key = m.group(1)
        raw_val = m.group(2).strip()
        fields[key] = _parse_value(key, raw_val)

    for req in REQUIRED_FRONT_MATTER:
        if req not in fields:
            raise TicketError(f"front matter: missing required field `{req}`")

    # Body must lead with a blank line; strip exactly one if present so callers
    # work against the body content directly.
    if body.startswith("\n"):
        body = body[1:]
    return fields, body


def _parse_value(key: str, raw: str):
    if raw == "" or raw.lower() == "null":
        return None
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if inner == "":
            return []
        items = _tokenize_flow_list(inner)
        # Reject empty tokens (e.g. `[a, ,b]`) — clearer to refuse than coerce.
        if any(item == "" for item in items):
            raise TicketError(f"front matter: empty list element in `{key}`")
        return items
    return raw


def _tokenize_flow_list(inner: str) -> list[str]:
    """Split a YAML flow-list interior on commas, respecting double-quoted spans.

    Items may be bare or double-quoted. Inside a double-quoted span, commas
    are not treated as separators. Backslash-escaped characters (``\\"`` and
    ``\\\\``) are handled so that ``\\"`` does not close the quoted span.
    After splitting, quoted tokens have their surrounding quotes stripped and
    ``\\"`` / ``\\\\`` escape sequences unescaped. Bare tokens are stripped of
    surrounding whitespace only.
    """
    tokens: list[str] = []
    current: list[str] = []
    in_quoted = False
    i = 0
    while i < len(inner):
        ch = inner[i]
        if in_quoted:
            if ch == "\\":
                # Consume the backslash and the next character as a unit.
                current.append(ch)
                i += 1
                if i < len(inner):
                    current.append(inner[i])
                    i += 1
                continue
            elif ch == '"':
                # Closing double-quote — end the quoted span.
                current.append(ch)
                in_quoted = False
                i += 1
                continue
            else:
                current.append(ch)
                i += 1
                continue
        else:
            if ch == '"':
                # Opening double-quote — begin a quoted span.
                current.append(ch)
                in_quoted = True
                i += 1
                continue
            elif ch == ",":
                tokens.append("".join(current))
                current = []
                i += 1
                continue
            else:
                current.append(ch)
                i += 1
                continue
    tokens.append("".join(current))

    result: list[str] = []
    for token in tokens:
        token = token.strip()
        if token.startswith('"') and token.endswith('"') and len(token) >= 2:
            # Strip surrounding quotes and unescape.
            unquoted = token[1:-1]
            unquoted = unquoted.replace('\\"', '"').replace("\\\\", "\\")
            result.append(unquoted)
        else:
            result.append(token)
    return result


def serialize_front_matter(fields: dict) -> str:
    """Emit front matter in `FRONT_MATTER_FIELD_ORDER`.

    Required fields always emit; optional fields emit only when present in
    `fields`. None for optional present fields renders as `null`. Lists render
    as flow-style `[a, b, c]`.
    """
    for req in REQUIRED_FRONT_MATTER:
        if req not in fields:
            raise TicketError(f"front matter: missing required field `{req}` for serialization")

    lines: list[str] = ["---"]
    for key in FRONT_MATTER_FIELD_ORDER:
        if key not in fields:
            continue
        lines.append(f"{key}: {_render_value(key, fields[key])}")
    # Any unknown keys (forward-compat) — emit at end in insertion order.
    for key in fields:
        if key in FRONT_MATTER_FIELD_ORDER:
            continue
        lines.append(f"{key}: {_render_value(key, fields[key])}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _render_value(key: str, val) -> str:
    if val is None:
        return "null"
    if isinstance(val, list):
        return "[" + ", ".join(_quote_flow_item(str(item)) for item in val) + "]"
    return str(val)


# YAML flow-style special characters that require an item to be double-quoted.
# Comma is the primary trigger (it is the item separator). The bracket and
# brace chars are YAML flow-collection delimiters. Double-quote itself must
# be quoted so the escape sequence renders correctly. Bare colons (without a
# trailing space) are valid in YAML plain scalars and are NOT included here —
# `repo:path` values in ratifiable_files must NOT be force-quoted on colons
# alone, as that would churn every existing ticket.
_FLOW_SPECIAL = frozenset(',[]{}\"')


def _quote_flow_item(s: str) -> str:
    """Return *s* as a YAML flow-style scalar token.

    If *s* contains any YAML-flow-special character or has leading/trailing
    whitespace, it is wrapped in double quotes with internal backslashes and
    double-quotes escaped. Otherwise it is returned bare (byte-identical to
    the pre-fix output for items that never needed quoting).
    """
    needs_quoting = s != s.strip() or any(ch in _FLOW_SPECIAL for ch in s)
    if not needs_quoting:
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# ---------------------------------------------------------------------------
# Ticket type + locator.
# ---------------------------------------------------------------------------


@dataclass
class Ticket:
    path: Path
    fields: dict
    body: str
    title: str = ""
    state_dir: str = field(init=False)

    def __post_init__(self) -> None:
        self.state_dir = self.path.parent.name


def parse_ticket(path: Path) -> Ticket:
    text = path.read_text(encoding="utf-8")
    fields, body = parse_front_matter(text)
    title = _extract_title(body)
    return Ticket(path=path, fields=fields, body=body, title=title)


def _extract_title(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _next_ticket_id() -> str:
    """Return the next available ticket ID as T-NNNN.

    MUST be called from within an ``_allocation_lock()`` context so the
    scan and the subsequent file write are atomic across concurrent writers.
    This function does NOT acquire the lock itself — see _allocation_lock().

    Side effect: runs `git fetch origin main` to refresh the remote-tracking
    ref before scanning. Raises TicketError if fetch fails — proceeding with
    a stale local view recreates the duplicate-ID race (T-0068). The fetch
    here is redundant with _commit_and_push's pre-push pull, but the
    discipline window is at allocation time, not push time.

    Scans both the local working tree (tickets/) AND origin/main:tickets/ via
    `git ls-tree -r --name-only`, takes max(local_max, origin_max), returns
    that + 1.
    """
    # Step 1: fetch to refresh remote-tracking ref. Fail-loud on error.
    res = _git("fetch", "origin", "main")
    if res.returncode != 0:
        raise TicketError(
            f"git fetch failed (cannot safely allocate ticket ID): "
            f"{res.stderr.strip() or res.stdout.strip()}"
        )

    # Step 2: scan local working tree.
    local_max = 0
    for state in STATES:
        d = TICKETS_ROOT / state
        if not d.exists():
            continue
        for entry in d.iterdir():
            if not entry.is_file():
                continue
            m = TICKET_FILENAME_RE.fullmatch(entry.name)
            if not m:
                continue
            n = int(m.group(1))
            if n > local_max:
                local_max = n

    # Step 3: scan origin/main:tickets/ via ls-tree. Non-zero return (e.g.
    # fresh clone where origin/main has no commits yet) is silently treated
    # as origin_max=0; local_max wins, and T-0001 is correct for that case.
    origin_max = 0
    res = _git("ls-tree", "-r", "--name-only", "origin/main", "tickets/")
    if res.returncode == 0:
        for line in res.stdout.splitlines():
            # Each line is a repo-relative path: "tickets/<state>/T-NNNN.md"
            fname = line.split("/")[-1]
            m = TICKET_FILENAME_RE.fullmatch(fname)
            if not m:
                continue
            n = int(m.group(1))
            if n > origin_max:
                origin_max = n

    return f"T-{max(local_max, origin_max) + 1:04d}"


def _find_ticket(tid: str) -> Path:
    if not re.fullmatch(r"T-\d{4}", tid):
        raise TicketError(f"invalid ticket id: {tid!r} (expected T-NNNN)")
    fname = f"{tid}.md"
    for state in STATES:
        candidate = TICKETS_ROOT / state / fname
        if candidate.exists():
            return candidate
    raise TicketError(f"ticket not found: {tid}")


# ---------------------------------------------------------------------------
# Transition-log helpers.
# ---------------------------------------------------------------------------


_TRANSITION_HEADER = "## Transition log"

# Map lifecycle verb (driving CLI subcommand + commit subject) to the
# past-tense form recorded in the in-file transition log. Per ADR-003
# §"Transition-log format": verbs are past-tense in the log.
_LOG_VERB = {
    "claim": "claimed",
    "unclaim": "unclaimed",
    "block": "blocked",  # past-tense; still used for frontmatter-gate log entry
    "resolve": "resolved",
    "retarget": "retargeted",
    # T-0653: noodle/action lifecycle close verbs for target=operator queue.
    "close-noodle": "noodle-closed",
    "close-action": "action-closed",
}


def append_transition(body: str, actor: str, verb: str, detail: str | None) -> str:
    """Append one bullet line to the body's `## Transition log` section.

    Refuses if the section header is absent (clearer than auto-creating;
    grooming agent's job to repair malformed files).
    """
    if _TRANSITION_HEADER not in body:
        raise TicketError(
            f"body missing required section header `{_TRANSITION_HEADER}`"
        )
    entry = _format_transition_entry(_now_iso(), actor, verb, detail)
    # Ensure body ends with a newline before appending.
    if not body.endswith("\n"):
        body = body + "\n"
    return body + entry + "\n"


def _format_transition_entry(ts: str, actor: str, verb: str, detail: str | None) -> str:
    if detail:
        return f"- {ts} [{actor}] {verb}; {detail}"
    return f"- {ts} [{actor}] {verb}"


# ---------------------------------------------------------------------------
# Ticket file builders.
# ---------------------------------------------------------------------------


def _build_ticket_text(fields: dict, body: str) -> str:
    return serialize_front_matter(fields) + "\n" + body


def _build_initial_body(title: str, ask: str, context: str, actor: str) -> str:
    ask_text = ask.strip() + "\n" if ask else ""
    ctx_text = context.strip() + "\n" if context else ""
    # Initial filing entry is rendered as "filed in inbox" (no semicolon),
    # per the ADR's example. Subsequent transitions use past-tense verbs
    # like "claimed; <detail>".
    initial_log = f"- {_now_iso()} [{actor}] filed in inbox"
    parts = [
        f"# {title}",
        "",
        "## Ask",
        "",
        ask_text,
        "## Context",
        "",
        ctx_text,
        "## Transition log",
        "",
        initial_log,
        "",
    ]
    # Join, then collapse only the explicit empty placeholders we left for
    # missing ask/context. Trailing single newline after the last log entry.
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Commit-message templates.
# ---------------------------------------------------------------------------


def _build_commit_message(verb: str, tid: str, title: str | None, detail: str | None) -> str:
    if verb == "file":
        subject = f"tickets: file {tid} — {title}"
    elif verb == "claim":
        subject = f"tickets: claim {tid}"
    elif verb == "unclaim":
        subject = f"tickets: unclaim {tid}"
    elif verb == "block":
        subject = f"tickets: block {tid}"
        if detail:
            short = detail if len(detail) <= 60 else detail[:57] + "..."
            subject = f"tickets: block {tid} — {short}"
    elif verb == "resolve":
        subject = f"tickets: resolve {tid}"
        if detail:
            short = detail if len(detail) <= 60 else detail[:57] + "..."
            subject = f"tickets: resolve {tid} — {short}"
    elif verb == "retarget":
        subject = f"tickets: retarget {tid}"
        if detail:
            short = detail if len(detail) <= 60 else detail[:57] + "..."
            subject = f"tickets: retarget {tid} — {short}"
    elif verb == "close-noodle":
        subject = f"tickets: close-noodle {tid}"
        if detail:
            short = detail if len(detail) <= 60 else detail[:57] + "..."
            subject = f"tickets: close-noodle {tid} — {short}"
    elif verb == "close-action":
        subject = f"tickets: close-action {tid}"
        if detail:
            short = detail if len(detail) <= 60 else detail[:57] + "..."
            subject = f"tickets: close-action {tid} — {short}"
    else:
        raise TicketError(f"unknown verb for commit message: {verb!r}")

    body_parts: list[str] = []
    # Include the full detail in the body when it was truncated in the subject,
    # so the audit chain retains the unabbreviated reason / summary.
    if verb in ("block", "resolve", "retarget", "close-noodle", "close-action") \
            and detail and len(detail) > 60:
        body_parts.append(detail)

    if not body_parts:
        return subject + "\n"
    return subject + "\n\n" + "\n".join(body_parts) + "\n"


# ---------------------------------------------------------------------------
# README table rebuild.
# ---------------------------------------------------------------------------

# Number of most-recent done tickets (by ID descending) to keep as full rows.
# Older done tickets are collapsed to per-created-month digest lines.
# Tunable: raising K keeps more detail; lowering K compresses the index further.
# Current value chosen to cover ~5 weeks of typical throughput while keeping
# the boot-loaded index well under 100 rows. justifies-via D10 (bound the index).
README_DONE_ROW_FLOOR = 40


def _partition_done_rows(
    done_tickets: list["Ticket"],
) -> tuple[list[str], list[str]]:
    """Split done tickets into full table rows and monthly digest lines.

    Args:
        done_tickets: Done-state tickets already sorted by ID descending
                      (highest ID first).  The caller owns this sort.

    Returns:
        A 2-tuple ``(full_rows, digest_lines)`` where:

        - ``full_rows`` is a list of formatted ``| col | … |`` row strings,
          one per ticket, for the top ``README_DONE_ROW_FLOOR`` tickets by ID.
        - ``digest_lines`` is a list of formatted digest-row strings, one per
          YYYY-MM bucket, for any tickets beyond the floor, sorted by month
          descending (newest month first).

        If ``len(done_tickets) <= README_DONE_ROW_FLOOR`` then ``digest_lines``
        is empty.  If ``done_tickets`` is empty both lists are empty.

        The digest line for a boundary month (where some tickets of that month
        appear in ``full_rows`` and others are collapsed) reflects ONLY the
        collapsed subset — its count and T-lo/T-hi span the collapsed tickets
        alone, not the whole month.
    """
    if not done_tickets:
        return [], []

    # Split into the kept full-row set and the to-be-collapsed set.
    # done_tickets is sorted descending by ID; the top K stay as full rows.
    kept = done_tickets[:README_DONE_ROW_FLOOR]
    collapsed = done_tickets[README_DONE_ROW_FLOOR:]

    # Build full row strings for kept tickets.
    full_rows: list[str] = []
    for t in kept:
        tid = t.fields.get("id", "?")
        state = t.fields.get("state", "?")
        title = t.title or "(no title)"
        requester = t.fields.get("requester", "?")
        target = t.fields.get("target", "?")
        created_iso = t.fields.get("created", "")
        filed = created_iso[:10] if len(created_iso) >= 10 else "?"
        full_rows.append(
            f"| {tid} | {state} | {title} | {requester} → {target} | {filed} |"
        )

    if not collapsed:
        return full_rows, []

    # Group collapsed tickets by YYYY-MM bucket (from 'created' frontmatter).
    # Tickets within each bucket are still in ID-descending order (inherited
    # from the input sort), so min/max ID are first and last in each group.
    buckets: dict[str, list[str]] = {}
    for t in collapsed:
        created_iso = t.fields.get("created", "")
        month = created_iso[:7] if len(created_iso) >= 7 else "???-??"
        tid = t.fields.get("id", "?")
        buckets.setdefault(month, []).append(tid)

    # Build digest lines, one per month, sorted month descending.
    digest_lines: list[str] = []
    for month in sorted(buckets.keys(), reverse=True):
        tids = buckets[month]  # already descending (highest first)
        count = len(tids)
        hi = tids[0]   # highest ID in the collapsed subset for this month
        lo = tids[-1]  # lowest  ID in the collapsed subset for this month
        digest_lines.append(
            f"| {month} | done (digest) | {count} done"
            f" · {lo}–{hi} · grep tickets/done/ | — | — |"
        )

    return full_rows, digest_lines


def _rebuild_readme_table(tickets_root: Path, readme_path: Path) -> bool:
    """Regenerate the 'Current state' table in tickets/README.md from filesystem.

    Reads all ticket files from tickets/{inbox,active,done}/*.md,
    parses frontmatter, and replaces the table in README.md with a freshly
    generated one sorted by ticket ID descending.

    Returns True if README content changed (and was written), False if the
    rebuilt content matches what was already on disk. The caller uses this
    signal to decide whether to stage README in the auto-commit pipeline —
    post-ORG-019 block-subcommand edits frontmatter in place without moving
    the ticket between dirs, which means the table is often unchanged.

    Idempotent: running twice on the same state produces the same output.
    """
    # Collect all tickets
    tickets: list[Ticket] = []
    for state in STATES:
        state_dir = tickets_root / state
        if not state_dir.exists():
            continue
        for entry in sorted(state_dir.iterdir()):
            if not entry.is_file():
                continue
            if not TICKET_FILENAME_RE.fullmatch(entry.name):
                continue
            try:
                tickets.append(parse_ticket(entry))
            except Exception:
                # Skip malformed tickets rather than blocking the whole rebuild
                continue

    # Sort by ID descending (T-0017 before T-0001)
    tickets.sort(key=lambda t: t.fields.get("id", ""), reverse=True)

    # Separate by state so decay applies only to done tickets.
    live_tickets = [t for t in tickets if t.fields.get("state") != "done"]
    done_tickets = [t for t in tickets if t.fields.get("state") == "done"]

    # Build full rows for live (inbox + active) tickets — never decayed.
    live_rows: list[str] = []
    for t in live_tickets:
        tid = t.fields.get("id", "?")
        state = t.fields.get("state", "?")
        title = t.title or "(no title)"
        requester = t.fields.get("requester", "?")
        target = t.fields.get("target", "?")
        created_iso = t.fields.get("created", "")
        filed = created_iso[:10] if len(created_iso) >= 10 else "?"
        live_rows.append(
            f"| {tid} | {state} | {title} | {requester} → {target} | {filed} |"
        )

    # Apply done-row decay: top README_DONE_ROW_FLOOR by ID stay as full rows;
    # older done tickets collapse to per-month digest lines.
    done_full_rows, digest_lines = _partition_done_rows(done_tickets)

    # Assemble table: header, live rows, done full rows, digest lines.
    table_lines = [
        "| ID | State | Title | Requester → Target | Filed |",
        "|---|---|---|---|---|",
        *live_rows,
        *done_full_rows,
        *digest_lines,
    ]

    # Read existing README
    readme_text = readme_path.read_text(encoding="utf-8")

    # Find the "## Current state" section
    marker = "## Current state"
    if marker not in readme_text:
        # If the marker doesn't exist, we can't rebuild — refuse rather than
        # corrupting the file.
        raise TicketError(f"README.md missing required section header `{marker}`")

    # Split at the marker
    before_marker = readme_text[: readme_text.index(marker)]
    after_marker = readme_text[readme_text.index(marker) + len(marker) :]

    # Find the end of the table: first line after marker that doesn't start with |
    # and isn't blank (preserves the blank line after the table)
    after_lines = after_marker.splitlines(keepends=True)
    table_end_idx = 0
    for i, line in enumerate(after_lines):
        stripped = line.lstrip()
        # Skip blank lines and table lines
        if stripped == "" or stripped.startswith("|"):
            table_end_idx = i + 1
        else:
            break

    # Reconstruct README
    new_content = (
        before_marker
        + marker
        + "\n\n"
        + "\n".join(table_lines)
        + "\n"
        + "".join(after_lines[table_end_idx:])
    )

    changed = new_content != readme_text
    if changed:
        try:
            readme_path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            # T-1003: cross-owner README (kino PWA uid creates 0644 file; claude-user
            # ticket.py then hits PermissionError on write). Also covers ENOSPC/EROFS.
            # The README rebuild is best-effort — a perms hiccup must never abort a
            # ticket op. Degrade gracefully: warn to stderr and return False so the
            # caller skips staging README (no phantom git-add of an unwritten file).
            # stderr not stdout: ticket.py stdout may be parsed by callers.
            # justifies-via P1 (structural: ticket op completes; README self-heals on
            # next successful write by any actor who owns the file).
            print(
                f"ticket.py: WARNING: README rebuild skipped for {readme_path}: "
                f"permission/OS error ({exc}); ticket op itself still completed.",
                file=sys.stderr,
            )
            return False
    # T-0695: keep tickets/README.md group-writable (0664). The PWA runs this
    # via ticket.py as uid=kino; a kino-created/recreated README (git pipeline
    # or umask-022 creation) can land 0644 (group read-only), which then blocks
    # subsequent claude-user ticket.py writes (PermissionError on this rebuild).
    # Best-effort: only the file's owner can chmod, so a cross-owner call raises
    # EPERM — harmless, since in that case the file is already group-writable.
    # justifies-via P1 (structural fix at the write path, caller-agnostic).
    try:
        os.chmod(readme_path, 0o664)
    except OSError:
        pass
    return changed


# ---------------------------------------------------------------------------
# Auto-commit-and-push pipeline.
# ---------------------------------------------------------------------------


def _board_conflict_resolver(unmerged_paths: list[str], cwd: Path) -> bool:
    """push_with_retry rebase-conflict resolver (T-0846).

    Resolve a rebase conflict that is SOLELY the generated `tickets/README`
    board by regenerating it from the (rebased) ticket files. ANY other
    conflicted path -> False, so a real ticket-file conflict still surfaces /
    aborts. The board is a pure function of the filesystem, so concurrent ticket
    ops only ever "conflict" on it cosmetically; regenerate-from-state is the
    correct merge. `cwd` is the repo the rebase runs in (== REPO_ROOT for
    ticket.py); we operate via the REPO_ROOT-defaulted `_git`.
    """
    if set(unmerged_paths) != {"tickets/README.md"}:
        return False
    readme_path = REPO_ROOT / "tickets" / "README.md"
    # Clear the conflict markers by checking out one whole side, then regenerate
    # the table from the (already-merged) ticket files on disk. WHICH side is
    # arbitrary — the table is fully rebuilt either way; we only need a
    # marker-free base. (`--theirs` during a REBASE is the local-commit side,
    # inverted vs merge, but it doesn't matter here.) Assumes the README's
    # non-table prose (static header/footer) isn't being concurrently edited in
    # a way that races a ticket op — that prose changes only via deliberate doc
    # commits, which don't collide with the board.
    co = _git("checkout", "--theirs", "--", "tickets/README.md", cwd=cwd)
    if co.returncode != 0:
        return False
    try:
        _rebuild_readme_table(TICKETS_ROOT, readme_path)
    except Exception:
        return False
    return _git("add", "--", "tickets/README.md", cwd=cwd).returncode == 0


def _pull_if_needed(no_push: bool) -> None:
    """Pre-mutation pull. Called by callers BEFORE filesystem changes (T-0244).

    Failure modes for git pull (network down, ff-not-possible, no remote in
    hermetic e2e) used to leave the working tree mid-transition with files
    moved/edited but no commit. Hoisting the pull to a pre-mutation check
    means pull-failure raises BEFORE any filesystem change happens.

    Skipped when no_push=True: --no-push semantically means "local-only
    operation; don't network." The next pushing op (or a manual push) carries
    the local-ahead state to origin. (The T-0211 60s sync timer that formerly
    auto-reconciled was retired by T-0365 — 2026-06-09; callers that need
    propagation either push themselves, e.g. ticket_lint's unified
    push_with_retry, or run a normal pushing lifecycle op next.) This is what
    makes --no-push usable in hermetic / offline contexts (T-0243: Playwright
    e2e fixtures with no git remote).
    """
    if no_push:
        return
    # ff-only pull, but self-heal a diverged clone by rebasing local (unpushed)
    # commits onto origin/main rather than hard-failing (T-0846). A prior ticket
    # op can leave a stranded local commit (push failed after commit); without
    # this, the NEXT op's ff-only pull raises and the clone stays wedged. Rebase
    # is the reconcile strategy (the operator's call 2026-06-05). The board resolver
    # handles a purely-cosmetic tickets/README conflict; real ticket conflicts
    # still abort + fail loud inside pull_or_rebase_strict.
    ok, msg = pull_or_rebase_strict(conflict_resolver=_board_conflict_resolver)
    if not ok:
        raise TicketError(msg)


def _commit_and_push(
    affected_paths: list[Path],
    commit_message: str,
    no_push: bool = False,
    rebase_on_nonff: bool = True,
) -> None:
    """Run the auto-commit pipeline for one ticket transition.

    T-1088 CONVERGENCE EXCEPTION (do NOT migrate to `_git_utils.commit_scoped`):
    the T-1088 P1 convergence folded the hand-rolled scope-narrowed-commit
    pattern across ~9 tools onto `commit_scoped`, but this caller is a
    documented exception (the second, alongside `watch_runner.git_commit_and_push`).
    Forcing it onto the atomic primitive would require THREE caller-specific
    extensions plus a guard-semantics weakening, on the highest-blast-radius
    caller (every ticket op + the restart/handoff path commits through here):
      1. Add-list != scope. A transition does `git mv old new`; `git add -- old`
         then fails fatal-128 ("pathspec did not match any files") because the
         worktree file is gone (T-0004). So we stage only EXISTING paths but
         must keep `old` (pre-staged as a deletion by git mv) in the scope set.
         commit_scoped conflates its `paths` arg as both add-list and scope.
      2. FILE-path re-allocation. cmd_file passes rebase_on_nonff=False so a
         racing same-ID push surfaces as a non-ff FAILURE (push_attempts=1, no
         rebase) and the cmd_file loop re-allocates a fresh ID — rebasing an
         add/add collision would be wrong. commit_scoped's push is fixed at
         push_with_retry's default 3-attempt rebase-on-nonff.
      3. Push-side board conflict_resolver. commit_scoped threads its
         conflict_resolver only into the sync (pull) step, not push_with_retry.
      Plus: commit_scoped's leak-only guard runs WITHOUT `--no-renames`, so its
      correctness for a rename would hinge on git's similarity heuristic;
      this pipeline's strict-equality + `--no-renames` guard is a deliberate
      stronger invariant for the most critical commits in the system.
    Convergence value (dedup of the COMMON pattern) is captured by the 9
    conforming callers; this outlier stays on its proven inline impl.

    Order: HERMES guard on each affected file -> HERMES belt on commit
    message -> ff-only pull -> rebuild README table -> stage exact paths ->
    diff-cached scope-set assert -> commit -> push (skipped when no_push).
    Each step fails fast with TicketError.

    rebase_on_nonff: when True (resolve/block/retarget — EDIT ops on an existing
    ticket), a non-ff push is reconciled by fetch+rebase onto fresh origin/main
    and retried — this is the T-0846 fix that stops stranding ratification
    resolves. When False (the FILE path), a non-ff push is NOT rebased: for a
    brand-new ticket a non-ff means a racing agent grabbed the same ID, and the
    correct response is to RE-ALLOCATE a fresh ID (cmd_file's retry loop), not to
    rebase an add/add-conflicting commit. Rebasing the FILE path silently bypasses
    re-allocation in the no-conflict case and add/add-conflicts in the collision
    case; a single plain push that surfaces the non-ff lets the loop re-allocate.
    """
    # 1. HERMES guard on every affected file that exists on disk now.
    for p in affected_paths:
        ok, detail = _hermes_scan_file(p)
        if not ok:
            raise TicketError(f"HERMES guard refused: {detail}")

    # 2. HERMES belt on commit message.
    if not _hermes_scan_message(commit_message):
        raise TicketError("HERMES guard refused: commit message construction")

    # 3. (Pull-first removed in T-0244.) Pull is now done by callers BEFORE any
    #    filesystem changes (_do_transition, cmd_file, cmd_retarget), so a pull
    #    failure can't leave the working tree in an inconsistent state where the
    #    file has been moved/edited but no commit happened. See _pull_if_needed.

    # 3a. Rebuild README table from current filesystem state. Only add README
    #     to affected_paths if the rebuild actually changed content — post-
    #     ORG-019 block-subcommand edits frontmatter in place without moving
    #     the ticket between dirs, so the table is often byte-identical and
    #     staging README would fail the step-5 scope guard with a no-op delta.
    readme_path = REPO_ROOT / "tickets" / "README.md"
    readme_changed = _rebuild_readme_table(TICKETS_ROOT, readme_path)
    if readme_changed:
        # HERMES guard on the freshly written README
        ok, detail = _hermes_scan_file(readme_path)
        if not ok:
            raise TicketError(f"HERMES guard refused (README): {detail}")
        affected_paths.append(readme_path)

    # 4. Stage paths that exist in the working tree. We pass repo-relative
    #    form to keep the step-5 diff-cached output directly comparable.
    #    Paths previously moved via `git mv` (e.g. the old half of a
    #    transition rename) are already staged as deletions and must NOT
    #    be re-added — `git add -- <missing>` fails with "pathspec did not
    #    match any files" and aborts the pipeline mid-flight (see T-0004).
    #    The strict-set assertion in step 5 still validates the full
    #    expected set against the index, so this narrowing does not
    #    weaken the scope guard against accidental cross-ticket bundling.
    rel_paths: list[str] = []
    for p in affected_paths:
        try:
            rel_paths.append(str(p.relative_to(REPO_ROOT)))
        except ValueError:
            raise TicketError(f"path outside repo: {p}")
    to_add = [rp for rp in rel_paths if (REPO_ROOT / rp).exists()]
    if to_add:
        res = _git("add", "--", *to_add)
        if res.returncode != 0:
            raise TicketError(
                f"git add failed: {res.stderr.strip() or res.stdout.strip()}"
            )

    # 5. Read back staged paths; assert the staged set is exactly our scope.
    #    `--no-renames` disables git's rename-detection coalescing — without
    #    it, a `git mv old new` shows up as a single rename-entry that
    #    `--name-only` reports as just the destination, which would make
    #    the expected-set [old, new] comparison fail (T-0004 fix companion).
    res = _git("diff", "--cached", "--name-only", "--no-renames")
    if res.returncode != 0:
        raise TicketError(
            f"git diff --cached failed: {res.stderr.strip() or res.stdout.strip()}"
        )
    staged = sorted(p for p in res.stdout.split("\n") if p.strip())
    expected = sorted(rel_paths)
    if staged != expected:
        _git("reset", "HEAD")
        raise TicketError(
            f"scope guard refused: staged set {staged!r} != expected {expected!r}"
        )

    # 6. Commit (no signing flags, no --no-verify).
    res = _git("commit", "-m", commit_message)
    if res.returncode != 0:
        raise TicketError(
            f"git commit failed: {res.stderr.strip() or res.stdout.strip()}"
        )

    # 7. Push with rebase-on-non-ff retry (skipped when --no-push; the next
    #    pushing op carries the local-ahead state — the 60s sync timer that
    #    formerly did this was retired T-0365). T-0846: a PLAIN push strands the commit
    #    when origin advanced between the pre-pull and the push (the
    #    PWA-ratification-stranding class — an interactive resolve commits, the
    #    push is rejected non-ff, and the commit sits local-only). push_with_retry
    #    fetches + rebases onto fresh origin/main and retries; the board resolver
    #    keeps the generated tickets/README from blocking that rebase.
    if no_push:
        return
    # rebase_on_nonff=False (FILE path) -> attempts=1: a single push, no rebase.
    # A non-ff then surfaces as a "git push failed" so cmd_file's loop re-allocates
    # a fresh ticket ID rather than rebasing a same-ID add/add collision.
    push_attempts = 3 if rebase_on_nonff else 1
    ok, detail = push_with_retry(
        REPO_ROOT,
        refspec="HEAD:main",
        attempts=push_attempts,
        conflict_resolver=_board_conflict_resolver if rebase_on_nonff else None,
    )
    if not ok:
        raise TicketError(f"git push failed: {detail}")


# ---------------------------------------------------------------------------
# Subcommand handlers.
# ---------------------------------------------------------------------------


def _parse_csv_list(raw: str | None) -> list[str]:
    if raw is None or raw.strip() == "":
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


_MAX_FILE_RETRIES = 3


def cmd_file(args: argparse.Namespace) -> int:
    title = args.title.strip()
    if not title:
        raise TicketError("--title must not be empty")
    requester = args.requester.strip()
    target = args.target.strip()

    # T-0306: validate target against agent_role_map + operator. Same check as
    # cmd_retarget — prevents target=echo (or any unknown) from slipping
    # through and producing tickets with no consumer (precedent: T-0109 +
    # T-0110 sat 6 days because target=echo has no session).
    valid = _valid_target_values()
    if target not in valid:
        raise TicketError(
            f"unknown target {target!r}; expected one of {sorted(valid)}"
        )

    # T-0492: normalize role-ID inputs to persona names on write.
    # Persona names are canonical for ticket target/requester (msg_1661).
    # "orchestrator" → "orchestrator", "specialist" → "specialist"; others pass through.
    target = _resolve_persona(target)
    requester = _resolve_persona(requester)

    # Fields that don't change across retry attempts.
    created = _now_iso()
    labels = _parse_csv_list(args.labels)
    depends_on = _parse_csv_list(args.depends_on)
    raw_parent = getattr(args, "parent_ticket", None)
    parent_ticket: str | None = None
    if isinstance(raw_parent, str) and raw_parent.strip():
        parent_ticket = raw_parent.strip()
        if not re.fullmatch(r"T-\d{4}", parent_ticket):
            raise TicketError(
                f"--parent-ticket value {parent_ticket!r} does not match the T-NNNN format"
            )
    actor = _actor()

    # --- T-0852: ratify file-time impl-followup gate (resolved once, pre-loop) ---
    # A target=operator + intent=ratify file must not dead-end: it carries either a
    # spawned impl child (gated on this ratify, cascade-unblocked on the operator's
    # ratify) or an explicit --terminal assertion. Keyed on the EFFECTIVE intent so
    # the auto-default-to-ratify path (--ratifiable-file, no --intent) is also gated.
    spawn_impl = getattr(args, "spawn_impl", False)
    terminal = getattr(args, "terminal", False)
    impl_title = getattr(args, "impl_title", None)
    impl_ask = getattr(args, "impl_ask", None)
    impl_target = getattr(args, "impl_target", None)

    _explicit_intent = (
        args.intent.strip()
        if isinstance(getattr(args, "intent", None), str) and args.intent.strip()
        else None
    )
    _has_ratifiable = bool(getattr(args, "ratifiable_file", None))
    _effective_intent = (
        (_explicit_intent or ("ratify" if _has_ratifiable else "action"))
        if target == "operator"
        else _explicit_intent
    )
    gate_applies = target == "operator" and _effective_intent == "ratify"

    ask_text = args.ask or ""
    if gate_applies:
        if spawn_impl and terminal:
            raise TicketError("--spawn-impl and --terminal are mutually exclusive (T-0852)")
        if not (spawn_impl or terminal):
            raise TicketError(
                "ratify ticket (target=operator, intent=ratify) must not dead-end: "
                "pass --spawn-impl (+ --impl-title) to atomically file an "
                "implementation ticket gated on this ratify, or --terminal to assert "
                "no implementation follow-up is needed (T-0852)"
            )
        if spawn_impl:
            if not (isinstance(impl_title, str) and impl_title.strip()):
                raise TicketError("--spawn-impl requires --impl-title (T-0852)")
            impl_title = impl_title.strip()
            impl_target = (
                impl_target.strip()
                if isinstance(impl_target, str) and impl_target.strip()
                else requester
            )
            impl_target = _resolve_persona(impl_target)
            if impl_target not in _valid_target_values():
                raise TicketError(
                    f"--impl-target {impl_target!r} not a recognized target; "
                    f"expected one of {sorted(_valid_target_values())}"
                )
        if terminal:
            if "ratify-terminal" not in labels:
                labels.append("ratify-terminal")
            ask_text = (
                f"{ask_text}\n\n[T-0852] TERMINAL ratify: filer asserts this "
                f"ratification needs no implementation follow-up."
            ).strip()
    elif spawn_impl or terminal:
        raise TicketError(
            "--spawn-impl/--terminal are ratify-only (target=operator + intent=ratify); "
            f"refusing for target={target!r} intent={_effective_intent!r} (T-0852)"
        )

    body = _build_initial_body(title, ask_text, args.context or "", actor)

    last_push_err: str | None = None
    new_path: Path | None = None
    child_path: Path | None = None

    for attempt in range(_MAX_FILE_RETRIES):
        # The _allocation_lock() context spans allocate→write so no other
        # writer can scan the same max between our _next_ticket_id() call
        # and the moment we land the file on disk (T-0786).  The lock is
        # released before _commit_and_push so the slow network push is NOT
        # serialized.  _next_ticket_id fetches origin on every attempt,
        # ensuring a fresh remote view even after a prior push failure
        # bumped origin's max.
        with _allocation_lock():
            tid = _next_ticket_id()
            fields: dict = {
                "id": tid,
                "created": created,
                "requester": requester,
                "target": target,
                "state": "inbox",
                "parent_ticket": parent_ticket,
                "depends_on": depends_on,
            }
            if labels:
                fields["labels"] = labels
            if args.stale_after:
                fields["stale_after"] = args.stale_after.strip()
            if args.not_before:
                fields["not_before"] = _parse_not_before(args.not_before.strip())
            raw_ratifiable = getattr(args, "ratifiable_file", None) or []
            if raw_ratifiable:
                fields["ratifiable_files"] = _parse_ratifiable_files(
                    [e.strip() for e in raw_ratifiable]
                )

            # ORG-022: ratifiable label ↔ ratifiable_files pairing enforcement.
            # Asymmetric: refuse label-without-files (malformed stub); auto-add
            # label-when-files-present (files are the concrete signal; preserves
            # writer ergonomics). Pre-ratified by the operator msg_1705.
            has_ratifiable_label = "ratification" in (labels or [])
            has_ratifiable_files = bool(fields.get("ratifiable_files"))
            if has_ratifiable_label and not has_ratifiable_files:
                raise TicketError(
                    "ratification label requires --ratifiable-file <repo>:<path> "
                    "(one or more); per ORG-022 file-tier pairing enforcement"
                )
            if has_ratifiable_files and not has_ratifiable_label:
                # Auto-add the canonical 'ratification' label (matches PWA
                # frontend filter at web/src/components/TicketList.svelte +
                # TicketDetail.svelte). Pre-ORG-022 convention was inconsistent
                # ('ratifiable' vs 'ratification'); ORG-022 codifies 'ratification'.
                if labels:
                    labels = labels + ["ratification"]
                else:
                    labels = ["ratification"]
                fields["labels"] = labels

            # T-0465: intent disambiguation for target=operator queue.
            raw_intent = getattr(args, "intent", None)
            if isinstance(raw_intent, str) and raw_intent.strip():
                intent = raw_intent.strip()
                if intent not in INTENT_VOCAB:
                    raise TicketError(
                        f"--intent {intent!r} not in vocab {INTENT_VOCAB}; "
                        f"choose one of: {', '.join(INTENT_VOCAB)}"
                    )
                if target != "operator":
                    raise TicketError(
                        f"--intent is target=operator-specific; refusing for target={target!r}"
                    )
                # T-1321: explicit intent=ratify requires at least one
                # ratifiable_files entry — the PWA ratifies a committed diff,
                # not prose.  _has_ratifiable is computed above (line ~1295)
                # from the --ratifiable-file args; re-check fields in case a
                # prior code path already populated ratifiable_files.
                if intent == "ratify" and not (
                    _has_ratifiable or fields.get("ratifiable_files")
                ):
                    raise TicketError(
                        "intent=ratify requires at least one ratifiable file; "
                        "pass --ratifiable-file <repo>:<path> (per ORG-015)"
                    )
                fields["intent"] = intent
            elif target == "operator":
                # Default for target=operator without explicit --intent: 'ratify' if
                # ratifiable_files present (matches ORG-015 ratification path),
                # else 'action' (operator-action default per T-0465 body).
                fields["intent"] = "ratify" if fields.get("ratifiable_files") else "action"

            # T-1103 / ORG-034: serves — arc-intent field (optional; free-text).
            raw_serves = getattr(args, "serves", None)
            if isinstance(raw_serves, str) and raw_serves.strip():
                fields["serves"] = raw_serves.strip()

            # Pull-first (T-0244): must succeed BEFORE writing the new file so
            # a pull failure doesn't leave an untracked ticket file orphaned.
            # Skipped when no_push=True.
            no_push = getattr(args, "no_push", False)
            _pull_if_needed(no_push)

            text = _build_ticket_text(fields, body)
            new_path = TICKETS_ROOT / "inbox" / f"{tid}.md"
            new_path.parent.mkdir(parents=True, exist_ok=True)
            new_path.write_text(text, encoding="utf-8")

            # T-0852: spawn the impl child INSIDE the lock, after the parent is on
            # disk, so the child's _next_ticket_id() sees it and allocates tid+1.
            # _locked=True avoids re-acquiring the (blocking) alloc lock we hold.
            # Re-run per attempt: on a push race the parent tid is re-allocated, so
            # the child's depends_on=[tid] must re-bind to the fresh tid.
            child_path = None
            if gate_applies and spawn_impl:
                child_ask = impl_ask or (
                    f"Implement the change(s) ratified in {tid}. See the ratified "
                    f"artifact(s) on the parent ticket. Spawned at file-time per "
                    f"T-0852 so the ratification does not dead-end; this ticket is "
                    f"gated on {tid} and auto-unblocks when the operator ratifies it."
                )
                child_path = _spawn_child_ticket(
                    parent_tid=tid,
                    target=impl_target,
                    intent=None,
                    labels=["ratify-impl"],
                    title=impl_title,
                    ask=child_ask,
                    depends_on=[tid],
                    _locked=True,
                )
        # lock released here — _commit_and_push (slow network) runs outside

        commit_message = _build_commit_message("file", tid, title, None)

        # Subject-length warning: title is requester's editorial call; surface
        # but do not refuse if the subject grows past 100 chars.
        subject = commit_message.splitlines()[0]
        if len(subject) > 100:
            print(
                f"warning: commit subject {len(subject)} chars (>100): {subject!r}",
                file=sys.stderr,
            )

        no_push = getattr(args, "no_push", False)
        try:
            # FILE path: rebase_on_nonff=False so a racing same-ID push surfaces as
            # a non-ff failure and this loop re-allocates a fresh ID (T-0786 race
            # design), rather than push_with_retry rebasing an add/add collision.
            # T-0852: the impl child (if spawned) lands in the SAME atomic commit.
            affected = [new_path] if child_path is None else [new_path, child_path]
            _commit_and_push(
                affected, commit_message, no_push=no_push, rebase_on_nonff=False
            )
            if child_path is not None:
                print(
                    f"filed {tid} → tickets/inbox/{tid}.md "
                    f"(+ impl {child_path.stem} → tickets/inbox/{child_path.name}, "
                    f"gated on {tid})"
                )
            else:
                print(f"filed {tid} → tickets/inbox/{tid}.md")
            return 0
        except TicketError as exc:
            err = str(exc)
            if "git push failed" not in err:
                # Non-push errors (HERMES guard, scope guard, pull failure,
                # etc.) are not recoverable by retry — surface immediately.
                raise
            last_push_err = err
            # _commit_and_push committed before the push. Undo that commit
            # (soft reset: moves HEAD back, un-stages changes, leaves working
            # tree intact) so the next attempt starts from a clean state.
            # Delete the ticket file(s) first so the working tree is clean post-
            # reset (not yet on origin, so no remote cleanup needed). T-0852: the
            # spawned impl child is re-created on the next attempt against the
            # re-allocated tid, so it must be removed here too.
            if new_path.exists():
                new_path.unlink()
            if child_path is not None and child_path.exists():
                child_path.unlink()
            _git("reset", "HEAD~1")

    raise TicketError(
        f"git push failed after {_MAX_FILE_RETRIES} attempts: {last_push_err}"
    )


def _do_transition(
    tid: str,
    expected_state: str,
    new_state: str,
    verb: str,
    detail: str | None,
    no_push: bool = False,
    cascade_callback=None,
) -> int:
    """Shared implementation for claim / block / unblock / resolve.

    cascade_callback (optional): callable(parent_tid: str) -> list[Path].
    Called AFTER the parent's git mv + frontmatter edit but BEFORE the
    commit, so any side-effect file moves it performs land in the same
    atomic commit as the parent transition. Used by cmd_resolve for
    auto-unblock cascade per T-0328.
    """
    old_path = _find_ticket(tid)
    if old_path.parent.name != expected_state:
        raise TicketError(
            f"state-mismatch: {tid} is in `{old_path.parent.name}`, "
            f"{verb} requires `{expected_state}`"
        )

    new_dir = TICKETS_ROOT / new_state
    new_dir.mkdir(parents=True, exist_ok=True)
    new_path = new_dir / old_path.name

    # 0. Pull-first (T-0244). Must succeed BEFORE any filesystem mutation so a
    #    pull failure leaves the working tree clean. Skipped when no_push=True.
    _pull_if_needed(no_push)

    # 1. git mv (renames index entry + working-tree file in one step).
    rel_old = str(old_path.relative_to(REPO_ROOT))
    rel_new = str(new_path.relative_to(REPO_ROOT))
    res = _git("mv", rel_old, rel_new)
    if res.returncode != 0:
        raise TicketError(
            f"git mv failed: {res.stderr.strip() or res.stdout.strip()}"
        )

    # 2. Edit front matter + append transition log.
    text = new_path.read_text(encoding="utf-8")
    fields, body = parse_front_matter(text)
    fields["state"] = new_state
    actor = _actor()
    log_verb = _LOG_VERB.get(verb, verb)
    body = append_transition(body, actor, log_verb, detail)
    new_text = _build_ticket_text(fields, body)
    new_path.write_text(new_text, encoding="utf-8")

    # 2b. Cascade callback (T-0328): runs after parent mv+edit, returns
    # additional paths to fold into the commit. Callback is responsible
    # for its own git mv + frontmatter edits. Failure raises TicketError
    # which propagates and aborts before commit.
    cascade_paths: list[Path] = []
    if cascade_callback is not None:
        cascade_paths = cascade_callback(tid)

    # 2c. Ratify-lift (T-0491): when resolving a ratify-intent ticket that has
    # ratifiable_files, rewrite each same-repo doc's first **Status:** line to
    # "Ratified <YYYY-MM-DD> by the operator." in the same atomic commit. Cross-repo
    # entries and missing Status lines emit warnings but never block the resolve.
    # This is a best-effort convenience lift — HO can hand-tune edge cases.
    # Gate: verb==resolve AND intent==ratify AND ratifiable_files non-empty
    #       AND resolution-with-summary does NOT start with "Rejected:".
    # The "Rejected:" carve-out (T-0641): pantheon-pwa's /reject route also
    # shells `ticket.py resolve --with "Rejected: ..."` for the artifact-
    # rejection verb (T-0342). Without this guard the lift fires on rejected
    # ratify tickets too, rewriting the artifact's Status to "Ratified by
    # the operator" even though the operator rejected. Caught in T-0637 smoke (2026-05-27).
    # lift_paths is kept SEPARATE from cascade_paths so the cascade commit-message
    # tail and stdout print are gated on real cascade dependants only (T-0491 fix).
    lift_paths: list[Path] = []
    if verb == "resolve":
        ratify_intent = fields.get("intent") == "ratify"
        ratifiable_files_list = fields.get("ratifiable_files") or []
        summary_str = (detail or "").lstrip()
        is_reject = summary_str.startswith("Rejected:")
        if ratify_intent and ratifiable_files_list and not is_reject:
            lift_warnings: list[str] = []
            lift_paths = _apply_ratify_lift(
                ratifiable_files_list, REPO_ROOT, lift_warnings
            )
            for w in lift_warnings:
                print(f"warning: ratify-lift: {w}", file=sys.stderr)

    # 3. Build commit message + auto-commit pipeline. Both old and new
    #    paths must be staged so the rename is captured atomically.
    title = _extract_title(body)
    commit_message = _build_commit_message(verb, tid, title, detail)
    if cascade_paths:
        # Only real cascade-unblock results go here — NOT lift_paths.
        cascade_count = len(cascade_paths)
        commit_message = (
            commit_message + f"\n\nAuto-unblock cascade per ORG-013 (orig. T-0328): "
            f"{cascade_count} dependant"
            f"{'s' if cascade_count != 1 else ''} had depends_on updated in place."
        )
    subject = commit_message.splitlines()[0]
    if len(subject) > 100:
        print(
            f"warning: commit subject {len(subject)} chars (>100): {subject!r}",
            file=sys.stderr,
        )

    _commit_and_push(
        [old_path, new_path] + cascade_paths + lift_paths,
        commit_message,
        no_push=no_push,
    )
    print(f"{verb} {tid} → tickets/{new_state}/{tid}.md")
    if cascade_paths:
        # Only print cascade line for real cascade-unblock results.
        cascade_count = len(cascade_paths)
        print(f"  auto-unblock cascade: {cascade_count} dependant"
              f"{'s' if cascade_count != 1 else ''} had depends_on updated in place")
    return 0


def _origin_state_of_ticket(tid: str) -> str | None:
    """Best-effort check of ticket's state on origin/main.

    Returns the state directory name ('inbox' | 'active' | 'done') if the
    ticket file exists at origin/main:tickets/<state>/<tid>.md for any state;
    None if not found (either truly not on origin or fetch failure).

    Used by cmd_claim's pre-claim guard (T-0463) to refuse a local claim if
    origin shows the ticket has already moved past inbox (typical sign:
    another clone claimed it concurrently and pushed before this clone did).
    """
    # Best-effort fetch; fail-open. Network outage / offline VPS shouldn't
    # block local claim attempts. Caller falls back to the stale local ref.
    _git("fetch", "origin", "main")
    for state in STATES:
        res = _git(
            "show", f"origin/main:tickets/{state}/{tid}.md",
        )
        if res.returncode == 0:
            return state
    return None


def cmd_claim(args: argparse.Namespace) -> int:
    # T-0463: pre-claim origin-side state check. Refuses if origin shows the
    # ticket already past inbox (concurrent-claim divergence pre-empt).
    # Caveat: TOCTOU window between this check and the local claim landing on
    # origin — bounds the worst case but doesn't eliminate it. Pair with
    # PWA-tier optimistic-lock UI (T-0367 follow-on) for fuller coverage.
    origin_state = _origin_state_of_ticket(args.ticket)
    if origin_state is not None and origin_state != "inbox":
        raise TicketError(
            f"concurrent-claim conflict: {args.ticket} is in `{origin_state}` "
            f"on origin/main (likely claimed by another clone). Pull origin/main "
            f"and re-evaluate before re-attempting claim."
        )

    # One-active WIP cap (T-0877; the operator directive 2026-06-06 msg 2662): an
    # agent holds at most one active ticket at a time. Refuse a manual claim
    # if the claiming persona already holds active work — finish (resolve),
    # retarget, or unclaim it first. This extends the T-0717 auto-dispatch
    # mutex to MANUAL claims, so the 1-active discipline can't be bypassed by
    # claiming directly. operator is exempt (claims his own decision tickets).
    # Cap is overridable via PANTHEON_MAX_ACTIVE_TICKETS (default 1) for test
    # isolation / deliberate exceptions; unset in production = enforce 1.
    persona = _claiming_persona()
    if persona != "operator":
        max_active = int(os.environ.get("PANTHEON_MAX_ACTIVE_TICKETS", "1"))
        held = [
            t.fields.get("id", "?")
            for t in _iter_all_tickets()
            if t.fields.get("state") == "active"
            and _resolve_persona(str(t.fields.get("target", ""))) == persona
        ]
        if len(held) >= max_active:
            raise TicketError(
                f"WIP cap ({max_active} active per agent): {persona} already "
                f"holds active {', '.join(held)}. Finish (resolve), retarget, or "
                f"unclaim it before claiming {args.ticket}. "
                f"See sop/standard/ticket-flow.md."
            )

    rc = _do_transition(
        tid=args.ticket,
        expected_state="inbox",
        new_state="active",
        verb="claim",
        detail="moving to active",
        no_push=getattr(args, "no_push", False),
    )

    # T-0761 c4: clone-on-demand provisioning. Runs ONLY on a successful claim
    # (rc == 0) AND when provision_repos is available (None when module absent
    # in isolated subprocess worktrees). Best-effort: any failure (network,
    # missing deploy key, bad repo) warns to stderr and continues — claim
    # return code is NEVER altered.
    # Mirrors the README-rebuild OSError graceful-degradation pattern above.
    # Grant validation is intentionally omitted here: ticket_lint's gate already
    # validated grants before the ticket could carry repos: entries. No re-impl
    # of persona→role mapping needed (no live instance.yaml reader exists yet).
    if rc == 0 and provision_repos is not None:
        try:
            ticket_path = _find_ticket(args.ticket)
            text = ticket_path.read_text(encoding="utf-8")
            fields, _ = parse_front_matter(text)
            warnings = provision_repos(fields, repo_root=REPO_ROOT)
            for w in warnings:
                print(
                    f"ticket.py: provision_repos: {w}",
                    file=sys.stderr,
                )
        except Exception as exc:  # noqa: BLE001 — best-effort; never blocks claim
            print(
                f"ticket.py: WARNING: repo provisioning skipped ({exc}); "
                f"claim already completed.",
                file=sys.stderr,
            )

    return rc


def cmd_unclaim(args: argparse.Namespace) -> int:
    """Move a ticket from active/ back to inbox/. Inverse of cmd_claim.

    T-0663: needed when a claim was premature (deps not clear, ticket parked,
    bandwidth not available). No origin-state pre-check — the contested
    concurrent-race surface is inbox→active (claim); the inverse demotion
    has no concurrent race surface.
    """
    return _do_transition(
        tid=args.ticket,
        expected_state="active",
        new_state="inbox",
        verb="unclaim",
        detail="moving to inbox",
        no_push=getattr(args, "no_push", False),
    )


def cmd_block(args: argparse.Namespace) -> int:
    """Set not_before and/or depends_on on a ticket (frontmatter edit; no state change).

    Per ORG-019: blocking is expressed via two structural frontmatter fields only:
      --not-before <ISO ts>   calendar gate (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)
      --depends-on T-NNNN     ticket-dep gate (repeatable for multiple deps)

    T-0663 extension:
      --clear-not-before      remove the not_before calendar gate (mutually exclusive
                              with --not-before and --depends-on). No-op + no commit
                              if not_before is already absent.

    The ticket stays in its current state directory (inbox/ or active/).
    At least one of --not-before, --depends-on, or --clear-not-before must be supplied.
    Refuses on done/ tickets.
    """
    not_before_raw: str | None = getattr(args, "not_before", None)
    depends_on_list: list[str] = getattr(args, "depends_on", None) or []
    clear_not_before: bool = getattr(args, "clear_not_before", False)
    no_push: bool = getattr(args, "no_push", False)

    # --clear-not-before is mutually exclusive with --not-before and --depends-on.
    if clear_not_before and not_before_raw:
        raise TicketError(
            "block: --clear-not-before is mutually exclusive with --not-before"
        )
    if clear_not_before and depends_on_list:
        raise TicketError(
            "block: --clear-not-before is mutually exclusive with --depends-on"
        )

    # Must supply at least one gate (original check extended to include clear_not_before).
    if not not_before_raw and not depends_on_list and not clear_not_before:
        raise TicketError(
            "block: must specify at least one of --not-before or --depends-on"
        )

    # --clear-not-before branch: lift calendar gate, stay in current lane.
    if clear_not_before:
        path = _find_ticket(args.ticket)
        if path.parent.name == "done":
            raise TicketError(
                f"state-mismatch: {args.ticket} is in `done`; cannot clear block gate on closed ticket"
            )
        text = path.read_text(encoding="utf-8")
        fields, body = parse_front_matter(text)
        if "not_before" not in fields:
            # Already absent — idempotent no-op; skip commit to preserve audit signal.
            print(
                f"block {args.ticket}: not_before already absent; no change"
            )
            return 0
        del fields["not_before"]
        actor = _actor()
        body = append_transition(body, actor, "not_before gate cleared", None)
        new_text = _build_ticket_text(fields, body)
        _pull_if_needed(no_push)
        path.write_text(new_text, encoding="utf-8")
        commit_message = f"tickets: {args.ticket} not_before cleared\n"
        _commit_and_push([path], commit_message, no_push=no_push)
        state_dir = path.parent.name
        print(
            f"block {args.ticket} --clear-not-before → tickets/{state_dir}/{args.ticket}.md "
            f"(not_before removed; file stays in place)"
        )
        return 0

    # Validate and normalize not_before if provided.
    not_before: str | None = None
    if not_before_raw:
        not_before = _parse_not_before(not_before_raw.strip())

    # Find the ticket (any non-done state).
    path = _find_ticket(args.ticket)
    if path.parent.name == "done":
        raise TicketError(
            f"state-mismatch: {args.ticket} is in `done`; cannot set block gate on closed ticket"
        )

    # Pull-first (T-0244).
    _pull_if_needed(no_push)

    # Edit frontmatter in-place.
    text = path.read_text(encoding="utf-8")
    fields, body = parse_front_matter(text)

    # Merge depends_on: union of existing + new deps (preserves any already set).
    existing_deps: list[str] = fields.get("depends_on") or []
    if depends_on_list:
        merged_deps = list(existing_deps)
        for dep in depends_on_list:
            if not re.fullmatch(r"T-\d{4}", dep):
                raise TicketError(f"--depends-on value {dep!r} does not match T-NNNN format")
            if dep not in merged_deps:
                merged_deps.append(dep)
        fields["depends_on"] = merged_deps

    if not_before:
        fields["not_before"] = not_before

    # Build log detail line.
    detail_parts: list[str] = []
    if not_before:
        detail_parts.append(f"not_before: {not_before}")
    if depends_on_list:
        detail_parts.append(f"depends_on: [{', '.join(depends_on_list)}]")
    detail = "; ".join(detail_parts)

    actor = _actor()
    body = append_transition(body, actor, "blocked", detail)
    new_text = _build_ticket_text(fields, body)
    path.write_text(new_text, encoding="utf-8")

    commit_message = _build_commit_message("block", args.ticket, _extract_title(body), detail)
    _commit_and_push([path], commit_message, no_push=no_push)
    state_dir = path.parent.name
    print(f"block {args.ticket} → tickets/{state_dir}/{args.ticket}.md (gate set; file stays in place)")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    detail = getattr(args, "with_summary", None)
    return _do_transition(
        tid=args.ticket,
        expected_state="active",
        new_state="done",
        verb="resolve",
        detail=detail,
        no_push=getattr(args, "no_push", False),
        cascade_callback=_cascade_unblock_dependants,
    )


# ---------------------------------------------------------------------------
# T-0653: noodle/action close lifecycle for target=operator queue.
#
# the operator's target=operator tickets carry intent: action|ratify|noodle. ratify
# closes via cmd_resolve (the existing ratify-lift path). action + noodle
# need their own close verbs:
#   - close-noodle: the operator acknowledges + closes; optional --spawn-to creates
#     a follow-up action ticket for an agent.
#   - close-action: the operator did the action; close + AUTO-spawn a review
#     ticket back to the original requester ("did this unblock you?").
#
# Both verbs cascade-unblock dependants (ORG-013) AND, when spawning, fold
# the child file into the same atomic commit so we never have a half-closed
# parent + missing child window.
# ---------------------------------------------------------------------------


def _spawn_child_ticket(
    *,
    parent_tid: str,
    target: str,
    intent: str | None,
    labels: list[str],
    title: str,
    ask: str,
    depends_on: list[str] | None = None,
    _locked: bool = False,
) -> Path:
    """Allocate next T-NNNN + write a child ticket file in tickets/inbox/.

    Used by close-noodle (--spawn-to), close-action (auto-review), and the
    T-0852 ratify file-time impl-followup spawn. The returned Path is folded
    into the parent's atomic commit (by the close path's cascade callback, or
    by cmd_file's affected-paths list) — both writes land together so a crash
    between them is impossible.

    The child's `requester` is the actor (or `recovery`). The parent_ticket field
    preserves lineage. `intent` is set only when target=operator (per T-0465).

    depends_on: child's depends_on list. For the ratify impl-spawn (T-0852)
        this is [parent_tid] so the impl ticket is gated on the ratify and
        ORG-013 cascade-unblocks it when the operator resolves the ratify. Default
        [] (close-noodle/close-action children have no gate).

    _locked: when True, the CALLER already holds _allocation_lock() — skip
        re-acquiring it. cmd_file spawns the impl child INSIDE its own
        allocation lock (so the child's _next_ticket_id sees the just-written
        parent on disk and gets parent_tid+1); re-acquiring the blocking
        LOCK_EX from the same process on a second fd would self-deadlock.
    """
    actor = _actor()
    body = _build_initial_body(title, ask, "", actor)
    # _allocation_lock() spans allocate→write so no concurrent writer can
    # claim the same T-NNNN between our scan and our file creation (T-0786).
    # _locked=True: caller holds it already (cmd_file's ratify-impl spawn).
    lock_cm = contextlib.nullcontext() if _locked else _allocation_lock()
    with lock_cm:
        tid = _next_ticket_id()
        fields: dict = {
            "id": tid,
            "created": _now_iso(),
            "requester": actor if actor in _valid_target_values() else "recovery",
            "target": target,
            "state": "inbox",
            "parent_ticket": parent_tid,
            "depends_on": list(depends_on) if depends_on else [],
        }
        if labels:
            fields["labels"] = labels
        if intent is not None and target == "operator":
            fields["intent"] = intent
        new_path = TICKETS_ROOT / "inbox" / f"{tid}.md"
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.write_text(_build_ticket_text(fields, body), encoding="utf-8")
    return new_path


def _close_lifecycle_cascade(spawn_fn):
    """Compose the standard ORG-013 cascade-unblock with an optional
    child-ticket spawn. Both side effects land in the parent's atomic
    commit via _do_transition's cascade_callback hook.
    """
    def cb(parent_tid):
        paths = _cascade_unblock_dependants(parent_tid)
        if spawn_fn is not None:
            child = spawn_fn(parent_tid)
            if child is not None:
                paths.append(child)
        return paths
    return cb


def _validate_operator_close(tid: str, expected_intent: str) -> tuple[Path, dict, str]:
    """Find the ticket, validate target=operator + intent matches the verb,
    return (path, fields, body_text). Refuses with TicketError otherwise.
    """
    path = _find_ticket(tid)
    text = path.read_text(encoding="utf-8")
    fields, body = parse_front_matter(text)
    if fields.get("target") != "operator":
        raise TicketError(
            f"close-{expected_intent}: {tid} has target={fields.get('target')!r}; "
            f"only target=operator tickets close via this verb"
        )
    if fields.get("intent") != expected_intent:
        raise TicketError(
            f"close-{expected_intent}: {tid} has intent={fields.get('intent')!r}; "
            f"only intent={expected_intent} closes via this verb "
            f"(use 'resolve' for ratify, 'close-noodle'/'close-action' for the others)"
        )
    return path, fields, body


def cmd_close_noodle(args: argparse.Namespace) -> int:
    detail = getattr(args, "with_summary", None)
    path, fields, body = _validate_operator_close(args.ticket, "noodle")

    spawn_to = getattr(args, "spawn_to", None)
    as_action = getattr(args, "as_action", False)

    spawn_fn = None
    if spawn_to:
        if not as_action:
            raise TicketError(
                "close-noodle --spawn-to requires --as-action "
                "(spawn-as-noodle not supported; file manually if you want that shape)"
            )
        valid = _valid_target_values()
        if spawn_to not in valid:
            raise TicketError(
                f"close-noodle --spawn-to {spawn_to!r} not a recognized target; "
                f"expected one of {sorted(valid)}"
            )
        spawn_to_canon = _resolve_persona(spawn_to)
        parent_title = _extract_title(body) or args.ticket
        child_title = f"[spawned from {args.ticket}] {parent_title}"
        child_ask = (
            f"Spawned from noodle {args.ticket} via the operator's close-noodle "
            f"--spawn-to {spawn_to_canon} --as-action.\n\n"
            f"Read the parent ticket ({args.ticket}, now in done/) for the full "
            f"discussion + the operator's close comment; execute the action it points to.\n\n"
            f"the operator's close comment: {detail or '(no comment)'}"
        )

        def make_child(parent_tid: str) -> Path:
            return _spawn_child_ticket(
                parent_tid=parent_tid,
                target=spawn_to_canon,
                intent=None,  # only set for target=operator
                labels=["spawned-from-noodle"],
                title=child_title,
                ask=child_ask,
            )

        spawn_fn = make_child

    return _do_transition(
        tid=args.ticket,
        expected_state=path.parent.name,
        new_state="done",
        verb="close-noodle",
        detail=detail,
        no_push=getattr(args, "no_push", False),
        cascade_callback=_close_lifecycle_cascade(spawn_fn),
    )


def cmd_close_action(args: argparse.Namespace) -> int:
    detail = getattr(args, "with_summary", None)
    path, fields, body = _validate_operator_close(args.ticket, "action")

    original_requester = fields.get("requester")
    if not isinstance(original_requester, str) or not original_requester.strip():
        raise TicketError(
            f"close-action: {args.ticket} has no requester field; "
            f"cannot route the auto-spawned review ticket"
        )
    parent_title = _extract_title(body) or args.ticket

    def make_review(parent_tid: str) -> Path:
        review_title = f"Review: did closing {parent_tid} unblock the downstream work?"
        review_ask = (
            f"the operator closed {parent_tid} ('{parent_title}') as completed.\n\n"
            f"the operator's close comment: {detail or '(no comment)'}\n\n"
            f"Verify the closure actually unblocked the downstream work this "
            f"action was meant to enable. If yes, resolve this review with "
            f"'verified'. If the action did NOT in fact unblock, file a "
            f"corrective ticket pointing at whoever owns the next step, with "
            f"this review as parent_ticket so the chain stays traceable."
        )
        return _spawn_child_ticket(
            parent_tid=parent_tid,
            target=original_requester,
            # intent only matters when target=operator (rare for review back
            # to original requester, but support it for self-routed cases).
            intent="action" if original_requester == "operator" else None,
            labels=["review", "follow-up", "auto-spawned-from-action-close"],
            title=review_title,
            ask=review_ask,
        )

    return _do_transition(
        tid=args.ticket,
        expected_state=path.parent.name,
        new_state="done",
        verb="close-action",
        detail=detail,
        no_push=getattr(args, "no_push", False),
        cascade_callback=_close_lifecycle_cascade(make_review),
    )


# ---------------------------------------------------------------------------
# Auto-unblock cascade (ORG-013, orig. T-0328): when a parent ticket resolves,
# scan for inbox/+active/ tickets that depend on it; if all their other deps
# are also done, remove parent_tid from the dependant's depends_on frontmatter.
# File stays in place — no git mv. Runs as cascade_callback inside
# _do_transition so the parent resolve + cascade edits land in one atomic commit.
#
# Post-ORG-019 semantics: no blocked/ directory. The cascade operates on
# inbox/ and active/ only. The file is not moved; only frontmatter is edited.
# ---------------------------------------------------------------------------


def _find_dependants(parent_tid: str) -> list[Path]:
    """Return ticket paths in inbox/+active/ whose depends_on includes parent_tid."""
    dependants: list[Path] = []
    for state in ("inbox", "active"):
        d = TICKETS_ROOT / state
        if not d.exists():
            continue
        for entry in d.iterdir():
            if not entry.is_file() or not TICKET_FILENAME_RE.fullmatch(entry.name):
                continue
            try:
                t = parse_ticket(entry)
            except TicketError:
                continue  # malformed; skip silently — grooming surfaces these separately
            deps = t.fields.get("depends_on") or []
            if parent_tid in deps:
                dependants.append(entry)
    return dependants


def _all_other_deps_done(deps: list[str], just_resolved_tid: str) -> bool:
    """True iff every dep besides just_resolved_tid is currently in done/."""
    for dep in deps:
        if dep == just_resolved_tid:
            continue
        if not re.fullmatch(r"T-\d{4}", dep):
            # Malformed dep id — fail closed (don't auto-unblock on uncertainty)
            return False
        fname = f"{dep}.md"
        if not (TICKETS_ROOT / "done" / fname).exists():
            return False
    return True


def _cascade_unblock_dependants(parent_tid: str) -> list[Path]:
    """Remove parent_tid from depends_on on inbox/+active/ tickets whose deps are now satisfied.

    Post-ORG-019 semantics: no file moves. The cascade edits depends_on frontmatter
    in-place and appends a transition log entry. Returns list of modified paths for
    the parent's atomic commit. Per ORG-013 (amended) + the operator msg_1293 ratification
    of T-0328: always-on (no opt-out), only fire on ALL-deps-done, fail-closed on
    malformed dep IDs.

    T-0971 (arm-A): also strips auto_delivered / auto_delivered_at stamps unconditionally
    when the dep is removed. A stale stamp on a now-unblocked ticket would be counted
    as an in-flight dispatch by check_dispatch_locked() (T-0864 arm-B only excludes
    dep-blocked tickets; it does not cover the moment they get unblocked). A fresh stamp
    will be applied on the ticket's eventual real delivery.
    """
    affected: list[Path] = []
    for dep_path in _find_dependants(parent_tid):
        try:
            t = parse_ticket(dep_path)
        except TicketError:
            continue
        deps = t.fields.get("depends_on") or []
        if not _all_other_deps_done(deps, parent_tid):
            continue  # multi-dep partial-resolve; wait for the rest

        # Remove parent_tid from depends_on in frontmatter.
        fields = t.fields
        remaining_deps = [d for d in deps if d != parent_tid]
        fields["depends_on"] = remaining_deps

        # T-0971 arm-A: strip any auto_delivered / auto_delivered_at stamp unconditionally.
        # A stale stamp on an unblocked ticket would hold the dispatch mutex and suppress
        # all inbox delivery for the agent (T-0768 incident pattern re-armed by unblock).
        had_ad = fields.pop("auto_delivered", None)
        had_ad_at = fields.pop("auto_delivered_at", None)
        had_stamp = bool(had_ad or had_ad_at)

        detail = f"dep {parent_tid} resolved; removed from depends_on"
        if had_stamp:
            detail += "; auto_delivered stamp cleared"
        body = append_transition(
            t.body, "auto-unblock", "dep resolved",
            detail
        )
        new_text = _build_ticket_text(fields, body)
        dep_path.write_text(new_text, encoding="utf-8")

        affected.append(dep_path)
    return affected


# ---------------------------------------------------------------------------
# Ratify-lift helpers (T-0491)
# ---------------------------------------------------------------------------

# Repo name that ticket.py operates in (pantheon-config). Same-repo entries
# in ratifiable_files use this name; cross-repo entries are skipped (v1).
_PANTHEON_CONFIG_REPO_NAME = "pantheon-config"

# Tolerant regex for "awaiting … ratification" clauses to strip after the
# Ratified prefix is prepended.  Matches common phrasings (with or without
# "the operator", with or without trailing period, with or without bold markers).
# Non-fatal if nothing matches — the prepended Ratified is authoritative.
_AWAITING_RATIFICATION_RE = re.compile(
    r"\*{0,2}"            # optional bold-open **
    r"Awaiting"
    r"(?:\s+the operator)?"    # optional "the operator"
    r"\s+ratification"
    r"\.?"                # optional trailing period
    r"\*{0,2}"            # optional bold-close **
    r"\s*",               # trailing whitespace
    re.IGNORECASE,
)

# Regex that matches the FIRST **Status:** line (captures the rest-of-line).
_STATUS_LINE_RE = re.compile(r"^(\*\*Status:\*\*)[ \t]*(.*)", re.MULTILINE)


def _lift_ratifiable_doc_status(
    doc_path: Path,
    repo_root: Path,
    warnings: list[str],
) -> str | None:
    """Rewrite the first **Status:** line of *doc_path* to the Ratified form.

    Returns the new file content as a string if a change was made, or None if
    the doc is already Ratified (idempotent) or has no Status line.

    Appends human-readable warning strings to *warnings* for:
      - no **Status:** line found
    Does NOT raise on any of these — callers must not block on lift failure.

    The Ratified prefix format is: **Ratified <YYYY-MM-DD> by the operator.**
    Any trailing/embedded "Awaiting … ratification" clause (tolerant regex)
    is stripped; the rest of the line (Drafted/Authored/Amended history) is
    preserved as lineage.
    """
    try:
        content = doc_path.read_text(encoding="utf-8")
    except OSError as e:
        warnings.append(f"could not read {doc_path}: {e}")
        return None

    m = _STATUS_LINE_RE.search(content)
    if m is None:
        warnings.append(
            f"no **Status:** line found in {doc_path.relative_to(repo_root)!s}; "
            "Status lift skipped — resolve proceeds"
        )
        return None

    rest = m.group(2)

    # Idempotent: if the rest already starts with "**Ratified" or "Ratified", no-op.
    stripped_rest = rest.lstrip()
    if stripped_rest.lower().startswith("**ratified") or stripped_rest.lower().startswith("ratified"):
        return None

    # Strip awaiting-clause (best-effort; non-fatal if nothing matches).
    rest_cleaned = _AWAITING_RATIFICATION_RE.sub("", rest).rstrip()

    # Build the Ratified prefix.  Date is UTC per brief.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ratified_prefix = f"**Ratified {today} by the operator.**"

    # Compose the new Status line content.
    if rest_cleaned:
        new_rest = f"{ratified_prefix} {rest_cleaned}"
    else:
        new_rest = ratified_prefix

    new_status_line = f"**Status:** {new_rest}"
    new_content = content[: m.start()] + new_status_line + content[m.end():]

    doc_path.write_text(new_content, encoding="utf-8")
    return new_content


def _apply_ratify_lift(
    ratifiable_files: list[str],
    repo_root: Path,
    warnings: list[str],
) -> list[Path]:
    """Process each ratifiable_files entry; return list of modified same-repo paths.

    Same-repo entries (repo == pantheon-config): attempt the Status lift and
    return the path if it was modified (so it gets staged in the resolve commit).
    Cross-repo entries: skip + warn (v1 scope; D3 same-repo-only rationale).
    Missing docs: warn + skip.
    """
    lifted: list[Path] = []
    for entry in ratifiable_files:
        # Entry format validated at file-time: "<repo>:<rel-path>".
        if ":" not in entry:
            warnings.append(f"malformed ratifiable_files entry {entry!r}; skipping")
            continue

        repo_name, rel_path = entry.split(":", 1)

        if repo_name != _PANTHEON_CONFIG_REPO_NAME:
            # Cross-repo: v1 skip + warn.  The cross-repo lift is follow-on work.
            warnings.append(
                f"cross-repo entry {entry!r} (repo={repo_name!r}) — "
                "Status lift skipped for cross-repo docs in v1; "
                "resolve proceeds. HO can lift manually."
            )
            continue

        doc_path = repo_root / rel_path
        if not doc_path.exists():
            warnings.append(
                f"ratifiable doc {rel_path!r} not found in repo root; "
                "Status lift skipped — resolve proceeds"
            )
            continue

        result = _lift_ratifiable_doc_status(doc_path, repo_root, warnings)
        if result is not None:
            # File was modified — stage it.
            lifted.append(doc_path)

    return lifted


def _valid_target_values() -> set[str]:
    """Union of persona names + role IDs from the agent role map."""
    from tools.agent_role_map import AGENT_ROLE_MAP
    return set(AGENT_ROLE_MAP.keys()) | set(AGENT_ROLE_MAP.values()) | {"operator"}


def _resolve_persona(value: str) -> str:
    """Normalize a ticket target/requester value to its canonical persona name.

    Wraps agent_role_map.resolve_persona(). Converts role IDs to persona names
    on write so ticket front-matter is always persona-canonical (T-0492/msg_1661).

    Examples: "orchestrator" -> "orchestrator", "specialist" -> "specialist",
    "orchestrator" -> "orchestrator" (passthrough), "operator" -> "operator" (passthrough).

    Only use for ticket `target:` and `requester:` fields. Ledger `agent:`
    fields remain role-ID-canonical — use resolve_role() there.
    """
    from tools.agent_role_map import resolve_persona
    return resolve_persona(value)


def cmd_retarget(args: argparse.Namespace) -> int:
    """Swap a ticket's `target` field + append transition log + commit.

    Default: cross-actor retargets ALSO reset state to inbox so the new
    target sees the ticket as fresh work (per the operator directive
    2026-05-14 msg_1494: "if tickets get re-routed to another member,
    we should reset status to inbox too"). The inbox/active
    state was meaningful to the prior target; the new target shouldn't
    inherit it.

    Pass --keep-state to preserve the existing state (rare: mid-work
    handoff where the new target picks up exactly where the prior left off).

    Refuses on done (closed; re-open is a separate concern).
    """
    new_target = args.to
    note = getattr(args, "note", None)
    keep_state = getattr(args, "keep_state", False)

    valid = _valid_target_values()
    if new_target not in valid:
        raise TicketError(
            f"unknown target {new_target!r}; expected one of "
            f"{sorted(valid)}"
        )

    # T-0492: normalize role-ID → persona BEFORE no-op check so that
    # `retarget --to orchestrator` on a `target: orchestrator` ticket correctly
    # no-ops rather than re-writing the same effective target with a role-ID form.
    new_target = _resolve_persona(new_target)

    path = _find_ticket(args.ticket)
    if path.parent.name == "done":
        raise TicketError(
            f"state-mismatch: {args.ticket} is in `done`, "
            f"retarget refuses on done (closed)"
        )

    text = path.read_text(encoding="utf-8")
    fields, body = parse_front_matter(text)
    old_target = fields.get("target", "?")
    old_state = path.parent.name

    if old_target == new_target:
        print(
            f"retarget {args.ticket} → {new_target}: no-op "
            f"(target already {new_target!r})",
            file=sys.stderr,
        )
        return 0

    # Pull-first (T-0244): must succeed BEFORE overwriting the ticket file so
    # a pull failure doesn't leave an edited-but-uncommitted file on disk.
    # Skipped when no_push=True.
    _pull_if_needed(args.no_push)

    # Determine target state. Default: cross-actor retarget resets to inbox
    # unless --keep-state is passed or the ticket is already in inbox.
    new_state = old_state if (keep_state or old_state == "inbox") else "inbox"

    fields["target"] = new_target

    # T-0465: intent disambiguation tracks target=operator status.
    raw_intent = getattr(args, "intent", None)
    if isinstance(raw_intent, str) and raw_intent.strip():
        intent = raw_intent.strip()
        if intent not in INTENT_VOCAB:
            raise TicketError(
                f"--intent {intent!r} not in vocab {INTENT_VOCAB}; "
                f"choose one of: {', '.join(INTENT_VOCAB)}"
            )
        if new_target != "operator":
            raise TicketError(
                f"--intent is target=operator-specific; refusing for new target={new_target!r}"
            )
        # T-1321: explicit intent=ratify requires ratifiable_files to already
        # be present in the ticket's frontmatter.  cmd_retarget has no
        # --ratifiable-file argument, so the effective check is the existing
        # fields dict (already populated from parse_front_matter above).
        if intent == "ratify" and not fields.get("ratifiable_files"):
            raise TicketError(
                "intent=ratify requires ratifiable_files to already be set in "
                "the ticket's frontmatter; add them before retargeting to "
                "intent=ratify (per ORG-015)"
            )
        fields["intent"] = intent
    elif new_target == "operator" and "intent" not in fields:
        # Newly retargeted to operator without explicit --intent: auto-default
        # (ratify if ratifiable_files present, else action) per cmd_file pattern.
        fields["intent"] = "ratify" if fields.get("ratifiable_files") else "action"
    elif new_target != "operator" and "intent" in fields:
        # Retargeted away from operator: intent no longer applicable; drop it
        # (target=operator-specific surface per T-0465).
        fields.pop("intent")

    # T-1103 / ORG-034: serves — arc-intent field. When --serves is given,
    # overwrite (or set) the field. When absent, leave existing serves: intact.
    raw_serves = getattr(args, "serves", None)
    if isinstance(raw_serves, str) and raw_serves.strip():
        fields["serves"] = raw_serves.strip()

    actor = _actor()
    log_verb = _LOG_VERB["retarget"]
    detail = f"{old_target} → {new_target}"
    if note:
        detail = f"{detail}; {note}"
    if new_state != old_state:
        detail = f"{detail}; state {old_state} → {new_state} (cross-actor reset)"
    body = append_transition(body, actor, log_verb, detail)

    # If state changes, also git-mv the file to the new state directory.
    paths_to_stage: list[Path] = [path]
    if new_state != old_state:
        fields["state"] = new_state
        new_dir = TICKETS_ROOT / new_state
        new_dir.mkdir(parents=True, exist_ok=True)
        new_path = new_dir / path.name
        old_path_abs = path
        rel_old = str(path.relative_to(REPO_ROOT))
        rel_new = str(new_path.relative_to(REPO_ROOT))
        res = _git("mv", rel_old, rel_new)
        if res.returncode != 0:
            raise TicketError(
                f"git mv failed: {res.stderr.strip() or res.stdout.strip()}"
            )
        path = new_path
        paths_to_stage = [old_path_abs, new_path]

    new_text = _build_ticket_text(fields, body)
    path.write_text(new_text, encoding="utf-8")

    title = _extract_title(body)
    commit_message = _build_commit_message(
        "retarget", args.ticket, title, detail
    )
    subject = commit_message.splitlines()[0]
    if len(subject) > 100:
        print(
            f"warning: commit subject {len(subject)} chars (>100): {subject!r}",
            file=sys.stderr,
        )

    _commit_and_push(paths_to_stage, commit_message, no_push=args.no_push)
    state_suffix = f" (state {old_state} → {new_state})" if new_state != old_state else ""
    print(f"retarget {args.ticket} → {new_target}{state_suffix}")
    return 0


# ---------------------------------------------------------------------------
# `list` (read-only).
# ---------------------------------------------------------------------------


def _iter_all_tickets() -> list[Ticket]:
    out: list[Ticket] = []
    for state in STATES:
        d = TICKETS_ROOT / state
        if not d.exists():
            continue
        names = sorted(
            entry.name
            for entry in d.iterdir()
            if entry.is_file() and TICKET_FILENAME_RE.fullmatch(entry.name)
        )
        for name in names:
            out.append(parse_ticket(d / name))
    return out


def _format_age(created_iso: str, now: datetime | None = None) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        # `Z` suffix is not parsed by fromisoformat in older Python; normalize.
        normalized = created_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "0s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def cmd_list(args: argparse.Namespace) -> int:
    tickets = _iter_all_tickets()
    if args.state:
        tickets = [t for t in tickets if t.fields.get("state") == args.state]
    if args.target:
        tickets = [t for t in tickets if t.fields.get("target") == args.target]
    if args.label:
        tickets = [
            t for t in tickets if args.label in (t.fields.get("labels") or [])
        ]

    if not tickets:
        print("(no tickets)")
        return 0

    rows = []
    for t in tickets:
        rows.append(
            (
                t.fields.get("id", "?"),
                t.fields.get("state", "?"),
                t.fields.get("requester", "?"),
                t.fields.get("target", "?"),
                _format_age(t.fields.get("created", "")),
                t.title,
            )
        )

    # Column widths
    w_id = max(len(r[0]) for r in rows)
    w_state = max(len(r[1]) for r in rows)
    w_req = max(len(r[2]) for r in rows)
    w_tgt = max(len(r[3]) for r in rows)
    w_age = max(len(r[4]) for r in rows)

    for r in rows:
        print(
            f"{r[0]:<{w_id}}  {r[1]:<{w_state}}  "
            f"{r[2]:<{w_req}} → {r[3]:<{w_tgt}}  "
            f"{r[4]:>{w_age}}  {r[5]}"
        )
    return 0


def _not_before_passed(raw: str, now: datetime) -> bool:
    """True if a `not_before` calendar gate has passed (or is unparseable).

    An absent gate is handled by the caller. An unparseable value is treated as
    passed (do not silently hide a ticket on a malformed gate — surface it so the
    bad value gets noticed). Per ORG-019 not_before semantics.
    """
    try:
        nb = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return True
    if nb.tzinfo is None:
        nb = nb.replace(tzinfo=timezone.utc)
    return nb <= now


def cmd_ready(args: argparse.Namespace) -> int:
    """List currently-unblocked tickets (the human-facing `bd ready` analog).

    A ticket is "ready" when it is in `inbox/` or `active/`, has no unresolved
    `depends_on` (dep gate clear), and its `not_before` calendar gate has passed
    or is absent (ORG-019 block semantics). Optional `--target` filter. Read-only.
    """
    now = datetime.now(timezone.utc)
    tickets = _iter_all_tickets()
    ready = []
    for t in tickets:
        if t.fields.get("state") not in ("inbox", "active"):
            continue
        if args.target and t.fields.get("target") != args.target:
            continue
        if t.fields.get("depends_on"):  # non-empty dep gate → blocked
            continue
        nb = t.fields.get("not_before")
        if nb and not _not_before_passed(nb, now):  # future calendar gate → blocked
            continue
        ready.append(t)

    if not ready:
        print("(no ready tickets)")
        return 0

    rows = [
        (
            t.fields.get("id", "?"),
            t.fields.get("state", "?"),
            t.fields.get("requester", "?"),
            t.fields.get("target", "?"),
            _format_age(t.fields.get("created", "")),
            t.title,
        )
        for t in ready
    ]
    w_id = max(len(r[0]) for r in rows)
    w_state = max(len(r[1]) for r in rows)
    w_req = max(len(r[2]) for r in rows)
    w_tgt = max(len(r[3]) for r in rows)
    w_age = max(len(r[4]) for r in rows)
    for r in rows:
        print(
            f"{r[0]:<{w_id}}  {r[1]:<{w_state}}  "
            f"{r[2]:<{w_req}} → {r[3]:<{w_tgt}}  "
            f"{r[4]:>{w_age}}  {r[5]}"
        )
    return 0


def _ticket_brief(t: "Ticket") -> dict:
    """Compact JSON-serializable view of a ticket for `prime` output."""
    return {
        "id": t.fields.get("id"),
        "title": t.title,
        "state": t.fields.get("state"),
        "requester": t.fields.get("requester"),
        "target": t.fields.get("target"),
        "labels": t.fields.get("labels") or [],
        "depends_on": t.fields.get("depends_on") or [],
        "not_before": t.fields.get("not_before"),
        "created": t.fields.get("created"),
    }


def cmd_prime(args: argparse.Namespace) -> int:
    """Emit project ticket-state as JSON for a fresh-agent bootstrap (bd-prime analog).

    Categories (each a list of compact ticket briefs):
      - ready:         inbox/active, depends_on empty, not_before passed
      - blocked:       inbox/active, blocked by a dep gate or a future not_before
      - active:        state == active
      - recently_done: most-recent N done tickets (by id desc; --done-limit, default 15)

    Optional `--target` filters ready/blocked/active (not recently_done). Read-only.
    """
    now = datetime.now(timezone.utc)
    tickets = _iter_all_tickets()

    def _match_target(t):
        return not args.target or t.fields.get("target") == args.target

    ready, blocked, active = [], [], []
    done = []
    for t in tickets:
        state = t.fields.get("state")
        if state == "done":
            done.append(t)
            continue
        if state not in ("inbox", "active"):
            continue
        if state == "active" and _match_target(t):
            active.append(t)
        if not _match_target(t):
            continue
        nb = t.fields.get("not_before")
        is_dep_gated = bool(t.fields.get("depends_on"))
        is_cal_gated = bool(nb) and not _not_before_passed(nb, now)
        if is_dep_gated or is_cal_gated:
            blocked.append(t)
        else:
            ready.append(t)

    done_sorted = sorted(done, key=lambda t: t.fields.get("id") or "", reverse=True)
    recently_done = done_sorted[: max(0, args.done_limit)]

    payload = {
        "ready": [_ticket_brief(t) for t in ready],
        "blocked": [_ticket_brief(t) for t in blocked],
        "active": [_ticket_brief(t) for t in active],
        "recently_done": [_ticket_brief(t) for t in recently_done],
        "counts": {
            "ready": len(ready),
            "blocked": len(blocked),
            "active": len(active),
            "done_total": len(done),
        },
    }
    print(json.dumps(payload, indent=2))
    return 0


# ---------------------------------------------------------------------------
# argparse wiring.
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    actor_help = (
        "Actor identity for the transition-log entry comes from "
        "$PANTHEON_ACTOR if set, else the OS username (getpass.getuser())."
    )
    p = argparse.ArgumentParser(
        prog="ticket.py",
        description=(
            "Pantheon ticket lifecycle CLI. " + actor_help
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("file", help="file a new ticket in inbox/")
    f.add_argument(
        "--requester",
        required=True,
        help="who is filing the ticket. Canonical: persona name (orchestrator/specialist/recovery/operator). "
             "Role IDs (orchestrator/specialist) accepted as legacy aliases — "
             "normalized to persona name on write (T-0492).",
    )
    f.add_argument(
        "--target",
        required=True,
        help="who should action the ticket. Canonical: persona name (orchestrator/specialist/recovery/operator). "
             "Role IDs (orchestrator/specialist) accepted as legacy aliases — "
             "normalized to persona name on write (T-0492).",
    )
    f.add_argument("--title", required=True)
    f.add_argument("--labels", default=None, help="comma-separated label list")
    f.add_argument("--depends-on", default=None, help="comma-separated ticket ids")
    f.add_argument(
        "--parent-ticket",
        default=None,
        help="parent ticket id (T-NNNN format); links this ticket as a child for lineage tracking",
    )
    f.add_argument("--stale-after", default=None, help="e.g. 4h, 2d")
    f.add_argument(
        "--not-before",
        default=None,
        help="earliest dispatch date/time: YYYY-MM-DD (interpreted as 00:00:00Z) "
             "or YYYY-MM-DDTHH:MM:SSZ",
    )
    f.add_argument(
        "--ratifiable-file",
        dest="ratifiable_file",
        action="append",
        default=None,
        metavar="REPO:PATH",
        help="'<repo>:<path>' entry for a file being proposed for ratification "
             "(e.g. pantheon-config:sop/standard/foo.md). Repeatable for multi-file "
             "ratification. repo must be in the allowlist; path must be a clean "
             "relative path with no leading '/' or './', no '..' segments, no "
             "empty path components.",
    )
    f.add_argument(
        "--intent",
        default=None,
        choices=INTENT_VOCAB,
        help="T-0465: disambiguate target=operator queue. action (operator action "
             "request; default), ratify (with --ratifiable-file; ORG-015 path), or "
             "noodle (design-noodle, no urgent action). target=operator-only field; "
             "ignored / refused for other targets. Auto-default when target=operator: "
             "'ratify' if --ratifiable-file given else 'action'.",
    )
    f.add_argument("--ask", default=None, help="optional inline body for ## Ask")
    f.add_argument("--context", default=None, help="optional inline body for ## Context")
    f.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                   help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    # T-0852: ratify file-time impl-followup gate. A target=operator + intent=ratify
    # file must NOT dead-end — it must carry a downstream implementation ticket, so
    # the operator's later ratify cascade-unblocks real work. Exactly one of --spawn-impl
    # or --terminal is REQUIRED on a ratify file; refused on non-ratify files.
    f.add_argument(
        "--spawn-impl", dest="spawn_impl", action="store_true", default=False,
        help="T-0852 (ratify-only): atomically spawn an implementation child ticket "
             "(depends_on this ratify, so it cascade-unblocks when the operator ratifies). "
             "Requires --impl-title. Mutually exclusive with --terminal.",
    )
    f.add_argument("--impl-title", dest="impl_title", default=None,
                   help="T-0852: title for the --spawn-impl child ticket")
    f.add_argument("--impl-ask", dest="impl_ask", default=None,
                   help="T-0852: optional ## Ask body for the --spawn-impl child "
                        "(defaults to a stub pointing at the ratified artifact)")
    f.add_argument("--impl-target", dest="impl_target", default=None,
                   help="T-0852: target for the --spawn-impl child (default: this "
                        "ratify's requester — the work routes back to who asked)")
    f.add_argument(
        "--terminal", dest="terminal", action="store_true", default=False,
        help="T-0852 (ratify-only): explicitly assert this ratification needs NO "
             "implementation follow-up (rare). Tags the ticket 'ratify-terminal' "
             "for audit. Mutually exclusive with --spawn-impl.",
    )
    f.add_argument(
        "--serves",
        default=None,
        help="T-1103 / ORG-034: arc-intent declaration — a short phrase naming "
             "what this ticket serves (e.g. a goals.md key or a free-text arc "
             "descriptor). 'goal-neutral-maintenance' is a valid value for "
             "housekeeping tickets. Optional; the ticket-lint will WARN (never "
             "fail) on new inbox tickets that omit this field.",
    )
    f.set_defaults(func=cmd_file)

    c = sub.add_parser("claim", help="move ticket inbox/ → active/")
    c.add_argument("ticket")
    c.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                   help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    c.set_defaults(func=cmd_claim)

    uc = sub.add_parser("unclaim", help="move ticket active/ → inbox/ (inverse of claim)")
    uc.add_argument("ticket")
    uc.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                    help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    uc.set_defaults(func=cmd_unclaim)

    b = sub.add_parser(
        "block",
        help="set not_before / depends_on gate on a ticket (frontmatter edit; no dir move)"
    )
    b.add_argument("ticket")
    b.add_argument(
        "--not-before",
        dest="not_before",
        default=None,
        metavar="DATE_OR_TS",
        help=(
            "Calendar gate: earliest pickup date. "
            "Formats: YYYY-MM-DD (interpreted as 00:00:00Z) or YYYY-MM-DDTHH:MM:SSZ. "
            "inbox_consumer suppresses until the timestamp passes."
        ),
    )
    b.add_argument(
        "--depends-on",
        dest="depends_on",
        action="append",
        default=None,
        metavar="T-NNNN",
        help=(
            "Ticket-dep gate. Repeatable for multiple deps. "
            "Must match T-NNNN format. Forward-references (dep not yet filed) are allowed. "
            "Cascade-unblock (ORG-013) removes the dep from frontmatter when parent resolves."
        ),
    )
    b.add_argument(
        "--clear-not-before",
        dest="clear_not_before",
        action="store_true",
        default=False,
        help=(
            "T-0663: remove the not_before calendar gate from the ticket. "
            "Mutually exclusive with --not-before and --depends-on. "
            "No-op (no commit) if not_before is already absent. "
            "Ticket stays in its current lane."
        ),
    )
    b.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                   help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    b.set_defaults(func=cmd_block)

    r = sub.add_parser("resolve", help="move ticket active/ → done/")
    r.add_argument("ticket")
    r.add_argument("--with", dest="with_summary", default=None)
    r.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                   help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    r.set_defaults(func=cmd_resolve)

    rt = sub.add_parser(
        "retarget",
        help="swap a ticket's target field (no state change; refuses on done)",
    )
    rt.add_argument("ticket")
    rt.add_argument(
        "--to",
        required=True,
        help="new target. Canonical: persona name (orchestrator/specialist/recovery/operator). "
             "Role IDs (orchestrator/specialist) accepted as legacy aliases — "
             "normalized to persona name on write (T-0492).",
    )
    rt.add_argument(
        "--note",
        default=None,
        help="optional one-line context appended to transition log",
    )
    rt.add_argument(
        "--keep-state",
        dest="keep_state",
        action="store_true",
        default=False,
        help="preserve existing state on cross-actor retarget "
             "(default: reset to inbox so new target sees fresh work)",
    )
    rt.add_argument(
        "--intent",
        default=None,
        choices=INTENT_VOCAB,
        help="T-0465: set/amend intent field on retarget. Required only when "
             "explicit intent change is desired; otherwise: retarget to operator "
             "auto-defaults intent if absent; retarget away from operator drops "
             "the field (target=operator-specific surface).",
    )
    rt.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                   help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    rt.add_argument(
        "--serves",
        default=None,
        help="T-1103 / ORG-034: set or overwrite the serves: arc-intent field. "
             "When omitted, any existing serves: value is preserved. "
             "Free-text; 'goal-neutral-maintenance' is a valid value.",
    )
    rt.set_defaults(func=cmd_retarget)

    cn = sub.add_parser(
        "close-noodle",
        help="close a target=operator intent=noodle ticket (optionally spawn child action)",
    )
    cn.add_argument("ticket")
    cn.add_argument("--with", dest="with_summary", default=None)
    cn.add_argument(
        "--spawn-to",
        dest="spawn_to",
        default=None,
        help="spawn a child ticket to this target as part of the close. "
             "Persona name (orchestrator/specialist/recovery/operator) or legacy role ID.",
    )
    cn.add_argument(
        "--as-action",
        dest="as_action",
        action="store_true",
        default=False,
        help="required when --spawn-to is set; the spawned child is filed "
             "with intent=action (if target=operator) and parent_ticket linkage.",
    )
    cn.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                    help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    cn.set_defaults(func=cmd_close_noodle)

    ca = sub.add_parser(
        "close-action",
        help="close a target=operator intent=action ticket; auto-spawns review to original requester",
    )
    ca.add_argument("ticket")
    ca.add_argument("--with", dest="with_summary", default=None)
    ca.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                    help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    ca.set_defaults(func=cmd_close_action)

    ls = sub.add_parser("list", help="list tickets (read-only)")
    ls.add_argument("--state", choices=STATES, default=None)
    ls.add_argument("--target", default=None)
    ls.add_argument("--label", default=None)
    ls.set_defaults(func=cmd_list)

    rdy = sub.add_parser(
        "ready",
        help="list currently-unblocked tickets (inbox/active, no depends_on, "
             "not_before passed) — read-only",
    )
    rdy.add_argument("--target", default=None, help="filter by target persona")
    rdy.set_defaults(func=cmd_ready)

    pr = sub.add_parser(
        "prime",
        help="emit project ticket-state as JSON for a fresh-agent bootstrap "
             "(ready/blocked/active/recently_done) — read-only",
    )
    pr.add_argument("--target", default=None, help="filter ready/blocked/active by target persona")
    pr.add_argument("--done-limit", type=int, default=15, help="max recently-done tickets (default 15)")
    pr.set_defaults(func=cmd_prime)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except TicketError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
