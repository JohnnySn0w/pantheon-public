#!/usr/bin/env python3
"""SessionStart hook: prune stale ~/.claude/teams/session-<id>/ dirs left by
dead sessions (T-3074).

THE FAILURE THIS EXISTS FOR: the harness does not reliably remove a session's
~/.claude/teams/session-<id>/ dir when the session ends (verified 2026-08-25:
a 2026-07-28 dir with zero teammates survived a month; an 8-teammate dir from
a live arc vanished for an unrecorded reason the same week). A stale dir's
inboxes/<agent>.json can still be addressed by name, and a dormant subagent
sharing a name with a FRESH dispatch (e.g. two arcs both spawning a
"bt2-implementer") can wake on a stray SendMessage and emit misdirected
chatter into the new arc — see T-3020 BT2 phantom bail-message incident.

CHOSEN SHAPE (T-3074 2026-08-25 disposition, (a)-variant): a conservative,
fail-open SessionStart hook — sibling of ensure-hooks-active.sh, wired the
same way (symlinked into ~/.claude/hooks/, invoked directly — no `python3`
prefix needed, matches the ho_stop_failure.py precedent already in this
settings.json for a direct-invoked python hook). No shutdown verb exists to
add (CC 2.1.243 has no graceful teardown for named subagents); (b) a Recovery-side
cadence sweep and (d) an upstream ask were rejected as heavier than a per-seat
hook for this problem shape.

PRUNE RULE (conservative — any doubt keeps the dir):
  prune session-<id>/ iff
    <id> != the current session's short id, AND
    no file OR directory anywhere under session-<id>/ has an mtime within
      --max-age-hours (a directory's own mtime moves when a file inside it is
      created/deleted, so a quiet-but-recently-touched inboxes/ dir counts as
      activity even with no individual file mtime bump), AND
    the matching session transcript
      (<projects-root>/*/<id-prefix>*.jsonl) is ABSENT, or its mtime is also
      older than --max-age-hours.

Never deletes the CURRENT session's dir. If the current session's id cannot
be resolved from any source, the hook no-ops the entire run rather than
guessing — "any doubt, keep" extended one level up.

FAIL-OPEN BY DESIGN: a SessionStart hook must never block or slow startup.
The whole run is wrapped in a catch-all; any unexpected exception is logged
to stderr and the process still exits 0. One stderr line per pruned (or
would-prune, in --dry-run) dir; silent otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

# Verified 2026-08-25 against code.claude.com/docs/en/hooks (live fetch, D11):
# SessionStart hooks receive `session_id` (full session UUID) as a *common
# input field* on stdin JSON -- that's the primary path below. No session-id
# env var is documented anywhere in the current hooks reference, but a live
# `env` inspection of this seat's Bash-tool child process (2026-08-25,
# review round 1) found $CLAUDE_CODE_SESSION_ID set to the full session UUID
# -- hooks are children of the same process tree, so they inherit it too.
# This is the real fallback (not $CLAUDE_SESSION_ID, which is unset and not
# a thing); stdin JSON still wins when both are present.
_ENV_SESSION_ID = "CLAUDE_CODE_SESSION_ID"

_TEAMS_DIR_PREFIX = "session-"


def _short_id(session_id: str) -> str:
    """First 8 chars of a session id/UUID — matches the `session-<id>` dir
    suffix (a UUID's first hyphen-delimited segment is exactly 8 hex chars,
    so this slice is safe whether the input is already short or a full UUID)."""
    return session_id[:8]


def _read_stdin_session_id() -> str | None:
    try:
        raw = sys.stdin.read()
    except Exception:
        return None
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    sid = data.get("session_id")
    if isinstance(sid, str) and sid:
        return sid
    return None


def _derive_from_newest_transcript(projects_root: Path) -> str | None:
    """Last-resort fallback: the session id of the most-recently-modified
    transcript anywhere under projects_root. Only used when no explicit
    session id is available from the CLI flag, stdin JSON, or env var."""
    newest_path = None
    newest_mtime = -1.0
    try:
        for jsonl in projects_root.glob("*/*.jsonl"):
            try:
                m = jsonl.stat().st_mtime
            except OSError:
                continue
            if m > newest_mtime:
                newest_mtime = m
                newest_path = jsonl
    except OSError:
        return None
    if newest_path is None:
        return None
    return newest_path.stem  # filename minus .jsonl == full session uuid


def resolve_current_session(
    current_session_arg: str | None, projects_root: Path
) -> tuple[str | None, str]:
    """Returns (short_id_or_None, source_description)."""
    if current_session_arg:
        return _short_id(current_session_arg), "cli-flag"
    sid = _read_stdin_session_id()
    if sid:
        return _short_id(sid), "stdin-json"
    env_sid = os.environ.get(_ENV_SESSION_ID)
    if env_sid:
        return _short_id(env_sid), "env-var"
    derived = _derive_from_newest_transcript(projects_root)
    if derived:
        return _short_id(derived), "derived-newest-transcript"
    return None, "unresolved"


def _dir_max_mtime(d: Path) -> float:
    """Newest mtime of any FILE or DIRECTORY under d (including d itself),
    recursive. A directory's own mtime moves when an entry inside it is
    created, renamed, or deleted, so this counts as activity even when no
    individual file's content changed."""
    newest = None
    try:
        newest = d.stat().st_mtime
    except OSError:
        pass
    for root, dirs, files in os.walk(d):
        for name in dirs + files:
            p = Path(root) / name
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if newest is None or m > newest:
                newest = m
    return newest if newest is not None else 0.0


def _transcript_recent(projects_root: Path, short_id: str, cutoff: float) -> bool:
    """True iff a transcript file whose name is prefixed by short_id exists
    anywhere under projects_root/*/ with mtime >= cutoff."""
    try:
        matches = list(projects_root.glob(f"*/{short_id}*.jsonl"))
    except OSError:
        return False
    for m in matches:
        try:
            if m.stat().st_mtime >= cutoff:
                return True
        except OSError:
            continue
    return False


def find_stale_dirs(
    teams_root: Path,
    projects_root: Path,
    current_short_id: str | None,
    max_age_hours: float,
):
    """Yields (dir_path, dir_id) for each candidate that should be pruned."""
    if current_short_id is None:
        return  # can't tell what's current -- do nothing (doubt -> keep everything)
    cutoff = time.time() - max_age_hours * 3600
    try:
        candidates = sorted(teams_root.glob(f"{_TEAMS_DIR_PREFIX}*"))
    except OSError:
        return
    for d in candidates:
        try:
            if d.is_symlink():
                # A symlinked session-* entry is never a real team dir this
                # hook owns -- following it into rmtree could delete whatever
                # it points at (possibly outside teams_root entirely). Skip
                # explicitly rather than relying on is_dir()'s follow-through.
                print(f"prune-stale-teams: skipping symlink {d}", file=sys.stderr)
                continue
            if not d.is_dir():
                continue
        except OSError:
            continue
        dir_id = d.name[len(_TEAMS_DIR_PREFIX):]
        if dir_id == current_short_id:
            continue
        try:
            dmax = _dir_max_mtime(d)
        except OSError:
            continue
        if dmax >= cutoff:
            continue  # recent activity in the dir itself -> keep
        if _transcript_recent(projects_root, dir_id, cutoff):
            continue  # transcript still fresh -> keep
        yield d, dir_id


def run(
    teams_root: Path,
    projects_root: Path,
    current_session_arg: str | None,
    max_age_hours: float,
    dry_run: bool,
) -> int:
    current_short_id, source = resolve_current_session(current_session_arg, projects_root)
    if current_short_id is None:
        print(
            "prune-stale-teams: could not resolve current session id from any "
            "source -- skipping run (doubt -> keep everything)",
            file=sys.stderr,
        )
        return 0

    if not teams_root.is_dir():
        return 0  # nothing to do

    for d, dir_id in find_stale_dirs(teams_root, projects_root, current_short_id, max_age_hours):
        action = "would prune" if dry_run else "pruned"
        print(
            f"prune-stale-teams: {action} {d} "
            f"(dir_id=session-{dir_id}, current=session-{current_short_id} via {source}, "
            f"max_age_hours={max_age_hours})",
            file=sys.stderr,
        )
        if not dry_run:
            try:
                # find_stale_dirs() already filters out symlinks explicitly
                # (see above); shutil.rmtree() also refuses a symlink target
                # on its own (raises OSError rather than recursing through
                # it), so this stays belt-and-suspenders either way -- caught
                # below like any other per-dir removal failure.
                shutil.rmtree(d)
            except OSError as e:
                print(f"prune-stale-teams: failed to remove {d}: {e}", file=sys.stderr)
                continue
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prune stale ~/.claude/teams/session-<id>/ dirs left by dead sessions (T-3074)."
    )
    parser.add_argument("--teams-root", default=str(Path.home() / ".claude" / "teams"))
    parser.add_argument("--projects-root", default=str(Path.home() / ".claude" / "projects"))
    parser.add_argument("--current-session", default=None)
    parser.add_argument("--max-age-hours", type=float, default=6.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    return run(
        teams_root=Path(args.teams_root),
        projects_root=Path(args.projects_root),
        current_session_arg=args.current_session,
        max_age_hours=args.max_age_hours,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # fail-open: SessionStart must never block startup
        print(f"prune-stale-teams: unexpected error, failing open: {e}", file=sys.stderr)
        sys.exit(0)
