#!/usr/bin/env python3
"""Artifact-GC — sweep stale ticket-scoped evidence dirs (T-1360).

`screenshots/T-NNNN/` dirs hold UI evidence captured during ticket work (the
playwright-screenshot pattern). Once the ticket is *done* and past a grace
period, that evidence has served its resolve-time review purpose — git history
is the durable archive — and the working tree just accumulates kruft (the operator
TG 3467). This janitor removes them on a cadence: the recurring fix for the
buildup.

Discipline mirrors `ticket_grooming.py`:
  - Scoped commit via `_git_utils.commit_scoped` (HERMES byte-guard + a scope
    guard that REFUSES any staged path outside the artifact roots). No force,
    no `--no-verify`.
  - A plain-text digest to the OWNER's mailbox (tools/owner_digest.py, default
    recovery) ONLY when something was actually removed under --apply (a weekly
    no-op stays silent; a report-only run delivers nothing). Never Telegram:
    nothing here needs the operator (T-6370, the T-6369 pattern). A failed delivery
    exits 1 so OnFailure pages the owner.
  - Report-only by default; `--apply` is required to remove + commit.

Conservative by construction — a dir is removed ONLY when its ticket is found
in `tickets/done/` AND its newest transition-log entry is older than the grace
period. Unknown ticket, non-done ticket, or undatable ticket → KEPT.

New ticket-scoped artifact roots can be added to ARTIFACT_ROOTS.

Second sweep domain (T-1506): `/tmp/echo-T-NNNN/target/` cargo build caches.
Parallel Echo /tmp worktrees (`tools/stage-echo-worktree.sh`) each carry a full
`target/` that a done ticket never needs again — git history isn't even the
archive here, a `target/` is pure regenerable build cache. Four of these
ballooned to 61G and pushed the root FS to 91% (2026-07-04 incident,
Recovery-remediated by hand; this automates that remediation). Gate mirrors the
screenshots domain (done + grace) plus one extra check unique to a live build
cache: no cargo/rustc process may have its cwd inside the workspace. Nothing
in this domain is git-tracked, so there is no commit step — removal is
best-effort per directory. New tmp-target roots are not anticipated (the
domain is scoped to the one known worktree-staging convention); if that
changes, generalize ECHO_WORKSPACE_RE the way ARTIFACT_ROOTS generalizes the
screenshots domain.

Usage:
    python3 tools/artifact_gc.py                 # live: sweep + commit + digest
    python3 tools/artifact_gc.py --dry-run       # report only
    python3 tools/artifact_gc.py --no-digest     # sweep + commit, skip the owner digest
    python3 tools/artifact_gc.py --grace 14d     # override screenshots grace
    python3 tools/artifact_gc.py --tmp-grace 24h # override tmp-target grace
    python3 tools/artifact_gc.py --only tmp-targets  # sweep one domain only
    python3 tools/artifact_gc.py --repo-root /path/to/clone   # tests
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make `from tools.X import ...` work both as a script and as a module.
_THIS = Path(__file__).resolve()
_REPO_ROOT_DEFAULT = _THIS.parent.parent
if str(_REPO_ROOT_DEFAULT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT_DEFAULT))

from tools._git_utils import _git, commit_scoped, pull_or_rebase_strict  # noqa: E402
from tools._lease import UNKNOWN  # noqa: E402  — tri-state sentinel (T-2307/T-2309)
from tools.ticket import STATES  # noqa: E402
from tools.owner_digest import send_owner_digest  # noqa: E402

# ---------------------------------------------------------------------------
# Config.
# ---------------------------------------------------------------------------

# Repo-relative roots that hold ticket-scoped `T-NNNN/` artifact dirs.
ARTIFACT_ROOTS = ("screenshots",)

# A ticket-scoped artifact dir is named exactly `T-<digits>`.
TICKET_DIR_RE = re.compile(r"^T-\d+$")

DEFAULT_GRACE = timedelta(days=30)
_THRESHOLD_RE = re.compile(r"^(\d+)([hd])$")

# --- tmp-target domain (T-1506) ---------------------------------------------

# Echo /tmp worktree naming convention (tools/stage-echo-worktree.sh):
# `/tmp/echo-T-NNNN/`. Anything under `echo-*` that doesn't match this exact
# shape (e.g. `echo-filibuster`, a non-ticket workspace) is out of scope —
# reported as unmanaged, never touched.
ECHO_WORKSPACE_RE = re.compile(r"^echo-(T-\d+)$")

# Shorter than DEFAULT_GRACE: a `target/` cache carries zero review value once
# its ticket is done (unlike screenshot evidence, which the 30d grace protects
# for post-resolve review) — the only reason to wait at all is to not yank the
# cache out from under a same-day follow-up rebuild in the same workspace.
TMP_GRACE_DEFAULT = timedelta(hours=48)

# Process names that indicate an in-flight build. cwd-based, not lock-file
# based: a cargo/rustc process's cwd inside the workspace is a live-build
# signal that must block removal even once the ticket is done + past grace.
_BUILD_PROC_NAMES = {"cargo", "rustc"}

PROC_ROOT_DEFAULT = Path("/proc")

# Transition-log bullet: "- <ts> [<actor>] <rest>". We only need <ts>.
_LOG_BULLET_RE = re.compile(r"^-\s+(?P<ts>\S+)\s+\[(?P<actor>[^\]]+)\]\s+")

GC_ACTOR = "artifact-gc"


# ---------------------------------------------------------------------------
# Parsing helpers.
# ---------------------------------------------------------------------------


def parse_grace(raw: str) -> timedelta:
    """Parse a grace string like '30d' or '12h' into a timedelta."""
    m = _THRESHOLD_RE.match(raw.strip())
    if not m:
        raise ValueError(f"invalid grace '{raw}' (expected e.g. '30d' or '12h')")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(days=n) if unit == "d" else timedelta(hours=n)


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO 8601 ts; tolerate a `Z` suffix. None on parse error."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def newest_log_ts(ticket_path: Path) -> datetime | None:
    """Newest parseable transition-log timestamp in the ticket, or None.

    For a done ticket the newest log entry is its resolve (or a later edit) —
    the right basis for "how long has this been closed?". Falls back to the
    frontmatter `created` if no log entry parses.
    """
    try:
        text = ticket_path.read_text(encoding="utf-8")
    except OSError:
        return None
    stamps: list[datetime] = []
    for line in text.splitlines():
        m = _LOG_BULLET_RE.match(line.strip())
        if not m:
            continue
        dt = _parse_iso(m.group("ts"))
        if dt:
            stamps.append(dt)
    if stamps:
        return max(stamps)
    # Fallback: the frontmatter `created:` line, scanned directly (no full
    # ticket validation — a ticket missing an unrelated required field must
    # still be datable).
    m = re.search(r"(?m)^created:\s*(\S+)", text)
    return _parse_iso(m.group(1)) if m else None


def locate_ticket(ticket_id: str, repo_root: Path) -> tuple[str | None, Path | None]:
    """Find a ticket by ID across state dirs. Returns (state, path) or (None, None)."""
    for state in STATES:
        p = repo_root / "tickets" / state / f"{ticket_id}.md"
        if p.exists():
            return state, p
    return None, None


# ---------------------------------------------------------------------------
# Discovery + classification.
# ---------------------------------------------------------------------------


def find_artifact_dirs(repo_root: Path) -> list[Path]:
    """All `T-NNNN/` dirs under the configured artifact roots, sorted."""
    out: list[Path] = []
    for root_name in ARTIFACT_ROOTS:
        root = repo_root / root_name
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if d.is_dir() and TICKET_DIR_RE.match(d.name):
                out.append(d)
    return out


@dataclass
class Decision:
    path: Path           # the artifact dir
    ticket_id: str
    action: str          # "remove" | "keep"
    reason: str


def _ticket_stale_reason(
    ticket_id: str, repo_root: Path, grace: timedelta, now: datetime
) -> tuple[bool, str]:
    """Shared done+grace gate for both sweep domains (T-1506 extraction).

    Returns (True, "<reason>") when the ticket is done and past grace — the
    caller may proceed to remove. Returns (False, "<reason>") for every keep
    case: ticket not found, not done, or done-but-undatable/within-grace.
    """
    state, ticket_path = locate_ticket(ticket_id, repo_root)
    if state is None:
        return False, "ticket not found (orphan) — kept for safety"
    if state != "done":
        return False, f"ticket still {state}"
    resolved = newest_log_ts(ticket_path)
    if resolved is None:
        return False, "done but resolve-time undatable — kept"
    age = now - resolved
    if age > grace:
        return True, f"done {_fmt_age(age)} ago (> grace)"
    return False, f"done only {_fmt_age(age)} ago (within grace)"


def classify(art_dir: Path, repo_root: Path, grace: timedelta, now: datetime) -> Decision:
    """Decide remove-vs-keep for one artifact dir. Conservative: only 'remove'
    a dir whose ticket is DONE and closed longer ago than `grace`."""
    ticket_id = art_dir.name
    is_stale, reason = _ticket_stale_reason(ticket_id, repo_root, grace, now)
    return Decision(art_dir, ticket_id, "remove" if is_stale else "keep", reason)


def _fmt_age(delta: timedelta) -> str:
    days = delta.days
    if days >= 1:
        return f"{days}d"
    hours = delta.seconds // 3600
    return f"{hours}h"


# ---------------------------------------------------------------------------
# Tmp-target domain (T-1506): /tmp/echo-T-NNNN/target/ cargo build caches.
# ---------------------------------------------------------------------------


def _dir_size_bytes(path: Path) -> int:
    """Best-effort recursive byte total for a directory tree (digest sizing).

    Entries that vanish or error mid-walk (a build racing the scan, unreadable
    perms) are simply skipped — this is a reporting figure, not a correctness
    input, so an undercount is fine and a crash is not.
    """
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


@dataclass
class TmpCandidate:
    workspace: Path      # /tmp/echo-T-NNNN
    target: Path         # /tmp/echo-T-NNNN/target
    ticket_id: str


def find_tmp_target_dirs(tmp_root: Path) -> tuple[list[TmpCandidate], list[Path]]:
    """Ticket-shaped `echo-T-NNNN/target/` candidates under `tmp_root`, plus
    the `echo-*` workspaces that do NOT match the ticket shape (unmanaged —
    reported, never touched). A ticket-shaped workspace with no `target/` yet
    (nothing to clean) is silently skipped: it's neither a candidate nor
    unmanaged.
    """
    candidates: list[TmpCandidate] = []
    unmanaged: list[Path] = []
    if not tmp_root.is_dir():
        return candidates, unmanaged
    for d in sorted(tmp_root.glob("echo-*")):
        if not d.is_dir():
            continue
        m = ECHO_WORKSPACE_RE.match(d.name)
        if not m:
            unmanaged.append(d)
            continue
        target = d / "target"
        if target.is_dir():
            candidates.append(TmpCandidate(workspace=d, target=target, ticket_id=m.group(1)))
    return candidates, unmanaged


def find_live_build_pids(workspace: Path, proc_root: Path = PROC_ROOT_DEFAULT) -> list[int]:
    """Pids that may be building into ``workspace/target``.

    A positive match is any process whose argv names the workspace/target, or
    any cargo/rustc process owned by the target's owner.  The owner-wide check
    is deliberately conservative: Cargo may run from a parent workspace and
    pass an absolute ``--target-dir``, while rustc children commonly have a cwd
    outside the target they are writing.  T-6205 proved that cwd/fd checks and
    an advisory lock can both be negative during a live build.

    Scanned via `proc_root` (default `/proc`; tests inject a fixture dir with
    real symlinks — no filesystem mocking needed for correctness).

    Any per-entry read failure (process exited mid-scan, permission denied,
    malformed /proc entry) is skipped rather than raised — a transient miss
    just means one fewer positive match, not a crash; on this single-owner
    VPS a genuinely running build's own /proc entries are always readable by
    the user that started it.

    ⚠️ RETURNS `UNKNOWN` WHEN THE SCAN COULD NOT HAPPEN AT ALL (T-2309). That is a
    different answer from `[]`, and the difference is load-bearing: `[]` means "I
    looked and nothing is building", while an unscannable proc_root means "I could
    not look". The caller deletes a directory tree on the negative, so collapsing
    the two makes total blindness authorise an rmtree of a `target/` that a live
    cargo build may be writing into — and it does so SILENTLY, because a missing
    proc_root makes `Path.is_dir()` return False rather than raise (an EACCES one
    does raise, so only the absent case was ever silent).

    That is the same shape as T-2305, where an unreadable /proc/locks stood in for
    "unlocked" and forked a lease. Here it would have falsified this module's own
    stated guarantee — "a `target/` is only ever removed when nothing could still
    be writing into it" — which is precisely the sentence that makes the deletion
    look safe to a reader.

    UNKNOWN raises on truthiness by construction, so an `if live_pids:` caller
    fails loudly instead of silently taking the delete branch. That is deliberate:
    the defect being fixed WAS a truthiness test on a blind value.
    """
    pids: list[int] = []
    if not proc_root.is_dir():
        return UNKNOWN
    workspace = workspace.resolve()
    target = workspace / "target"
    try:
        target_uid = target.stat().st_uid
    except OSError:
        return UNKNOWN
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        try:
            raw_cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            raw_cmdline = b""
        argv = [os.fsdecode(part) for part in raw_cmdline.split(b"\0") if part]
        if any(str(workspace) in arg for arg in argv):
            pids.append(int(entry.name))
            continue
        if comm in _BUILD_PROC_NAMES:
            try:
                proc_uid = entry.stat().st_uid
            except OSError:
                return UNKNOWN
            if proc_uid == target_uid:
                pids.append(int(entry.name))
                continue
        try:
            cwd = (entry / "cwd").resolve()
        except OSError:
            continue
        if comm in _BUILD_PROC_NAMES and (cwd == workspace or workspace in cwd.parents):
            pids.append(int(entry.name))
    return pids


@dataclass
class TmpDecision:
    workspace: Path
    target: Path
    ticket_id: str
    action: str          # "remove" | "keep"
    reason: str
    size_bytes: int | None = None  # populated by sweep_tmp_targets, not classify_tmp


def classify_tmp(
    candidate: TmpCandidate,
    repo_root: Path,
    grace: timedelta,
    now: datetime,
    proc_root: Path = PROC_ROOT_DEFAULT,
) -> TmpDecision:
    """Decide remove-vs-keep for one tmp-target candidate. Same done+grace gate
    as the screenshots domain, plus a live-build-process check as the final
    gate before removal — a `target/` is only ever removed when nothing could
    still be writing into it.
    """
    is_stale, reason = _ticket_stale_reason(candidate.ticket_id, repo_root, grace, now)
    if not is_stale:
        return TmpDecision(candidate.workspace, candidate.target, candidate.ticket_id, "keep", reason)
    live_pids = find_live_build_pids(candidate.workspace, proc_root)
    if live_pids is UNKNOWN:
        # T-2309: could not scan procfs at all. Fail CLOSED — the asymmetry is
        # not close. Keeping a stale target/ costs disk on a dir the module
        # itself calls regenerable, and the next sweep retries; removing one
        # out from under a live build corrupts work in progress. Checked before
        # the truthiness test below because UNKNOWN refuses to be coerced.
        return TmpDecision(
            candidate.workspace, candidate.target, candidate.ticket_id, "keep",
            f"build-liveness UNKNOWN (procfs at {proc_root} not scannable) — "
            f"refusing to remove on an unverified negative",
        )
    if live_pids:
        return TmpDecision(
            candidate.workspace, candidate.target, candidate.ticket_id, "keep",
            f"live build process detected (pid {live_pids})",
        )
    return TmpDecision(candidate.workspace, candidate.target, candidate.ticket_id, "remove", reason)


@dataclass
class TmpSweepResult:
    decisions: list[TmpDecision]
    removed: list[TmpDecision]
    unmanaged: list[Path]


def sweep_tmp_targets(
    tmp_root: Path,
    repo_root: Path,
    grace: timedelta,
    now: datetime,
    *,
    proc_root: Path = PROC_ROOT_DEFAULT,
    dry_run: bool,
) -> TmpSweepResult:
    """Classify every tmp-target candidate; remove the stale ones (unless
    dry_run). No git commit — nothing here is repo-tracked. Removal is
    best-effort per directory: a `target/` is always regenerable, so a single
    dir's OSError is logged and skipped rather than aborting the whole sweep
    (unlike the screenshots domain, where a failed commit must roll back to
    protect an atomic git history — there's no analogous atomicity need here).
    """
    candidates, unmanaged = find_tmp_target_dirs(tmp_root)
    decisions = [classify_tmp(c, repo_root, grace, now, proc_root) for c in candidates]
    to_remove = [d for d in decisions if d.action == "remove"]
    for d in to_remove:
        d.size_bytes = _dir_size_bytes(d.target)

    if dry_run:
        return TmpSweepResult(decisions, to_remove, unmanaged)

    removed: list[TmpDecision] = []
    for d in to_remove:
        # T-6206: classification and deletion are separated by the size walk.
        # Recheck at the destructive boundary so a build that starts after the
        # first scan cannot inherit the earlier "remove" decision.
        live_pids = find_live_build_pids(d.workspace, proc_root)
        if live_pids is UNKNOWN:
            d.action = "keep"
            d.reason = "build-liveness UNKNOWN at delete boundary — refusing removal"
            continue
        if live_pids:
            d.action = "keep"
            d.reason = f"live build process detected at delete boundary (pid {live_pids})"
            continue
        try:
            shutil.rmtree(d.target)
        except OSError as exc:
            print(f"warning: failed to remove {d.target}: {exc}", file=sys.stderr)
            continue
        removed.append(d)
    return TmpSweepResult(decisions, removed, unmanaged)


# ---------------------------------------------------------------------------
# Digest (plain text, to the owner's mailbox; T-6370).
# ---------------------------------------------------------------------------


def build_digest(
    decisions: list[Decision],
    removed: list[Decision],
    tmp_decisions: list[TmpDecision],
    tmp_removed: list[TmpDecision],
    dry_run: bool,
) -> str:
    """Merged digest across both sweep domains (T-1506). Callers pass empty
    lists for a domain that didn't run (e.g. `--only screenshots`)."""
    head = "🧹 [artifact-gc]" + (" (dry-run)" if dry_run else "")
    verb = "would remove" if dry_run else "removed"
    would_be = "would be " if dry_run else ""
    lines = [head]

    total = len(decisions)
    kept = total - len(removed)
    lines.append(f"screenshots: scanned {total}; {len(removed)} {would_be}removed, {kept} kept.")
    if removed:
        lines.append(f"{verb}:")
        for d in removed:
            lines.append(f"  • {ARTIFACT_ROOTS[0]}/{d.ticket_id}/ — {d.reason}")

    tmp_total = len(tmp_decisions)
    tmp_kept = tmp_total - len(tmp_removed)
    freed_gb = sum(d.size_bytes or 0 for d in tmp_removed) / (1024 ** 3)
    lines.append("")
    lines.append(
        f"tmp-targets: scanned {tmp_total}; {len(tmp_removed)} {would_be}removed "
        f"({freed_gb:.1f}GB {would_be}freed), {tmp_kept} kept."
    )
    if tmp_removed:
        lines.append(f"{verb}:")
        for d in tmp_removed:
            gb = (d.size_bytes or 0) / (1024 ** 3)
            lines.append(f"  • {d.workspace.name}/target — {gb:.1f}GB — {d.reason}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sweep.
# ---------------------------------------------------------------------------


@dataclass
class SweepResult:
    decisions: list[Decision]
    removed: list[Decision]
    committed: bool
    commit_detail: str


def sweep(
    repo_root: Path,
    grace: timedelta,
    now: datetime,
    *,
    dry_run: bool,
    do_commit: bool = True,
) -> SweepResult:
    """Classify every artifact dir; remove the stale ones (unless dry_run) and
    commit the removals scoped to the artifact roots."""
    decisions = [classify(d, repo_root, grace, now) for d in find_artifact_dirs(repo_root)]
    removed = [d for d in decisions if d.action == "remove"]

    if dry_run or not removed:
        return SweepResult(decisions, removed if dry_run else [], False, "dry-run" if dry_run else "no stale dirs")

    # COMMIT-SAFETY: a destructive unattended action must verify it can land
    # BEFORE destroying. rmtree-then-commit is not atomic — if the commit's sync
    # failed AFTER the rmtree (divergence / network / wrong default branch), the
    # deletion would be orphaned uncommitted in the working tree. So pre-sync
    # first; if the repo isn't in a committable state, abort WITHOUT touching any
    # files, then commit with sync=False (we already synced). And if that final
    # local commit still fails after the rmtree, roll the working tree back from
    # HEAD (T-1384) so a failed commit never leaves an irreversible deletion.
    if do_commit:
        ok, detail = pull_or_rebase_strict(repo_root)
        if not ok:
            return SweepResult(decisions, [], False, f"pre-sync failed; aborted before any removal: {detail}")

    # Remove the stale dirs from the working tree; commit_scoped then stages the
    # deletions via `git add -- <root>` and refuses anything out of scope.
    for d in removed:
        shutil.rmtree(d.path)

    if not do_commit:
        # Test/inspection path: removals on disk, but no git interaction.
        return SweepResult(decisions, removed, False, "commit skipped (do_commit=False)")

    ticket_list = ", ".join(d.ticket_id for d in removed)
    message = (
        f"chore(artifact-gc): remove {len(removed)} stale ticket-evidence dir(s)\n\n"
        f"Swept {ARTIFACT_ROOTS[0]}/ for T-NNNN dirs whose ticket is done past the "
        f"grace period; git history preserves them. Removed: {ticket_list}.\n\n"
        f"Automated by tools/artifact_gc.py (T-1360)."
    )
    ok, detail = commit_scoped(
        list(ARTIFACT_ROOTS), message, cwd=repo_root, allow_prefix=True, sync=False
    )
    if not ok:
        # ROLLBACK (T-1384): the rmtree already ran, but the commit failed. A
        # destructive janitor must never leave an irreversible deletion on a
        # failed commit. We only synced + deleted the working tree, so the
        # removed dirs are still tracked at HEAD — restore them and report the
        # rollback rather than orphaning the deletion.
        rb = _git("checkout", "HEAD", "--", *ARTIFACT_ROOTS, cwd=repo_root)
        if rb.returncode == 0:
            return SweepResult(
                decisions, [], False,
                f"commit failed AFTER removal; rolled back working tree from HEAD: {detail}",
            )
        return SweepResult(
            decisions, removed, False,
            f"commit failed AFTER removal AND rollback FAILED "
            f"({(rb.stderr or rb.stdout).strip()}); recover manually via git: {detail}",
        )
    return SweepResult(decisions, removed, ok, detail)


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sweep stale ticket-scoped evidence dirs (T-1360) and tmp-target build caches (T-1506).")
    # Default is SAFE (report-only) — a destructive janitor must not delete on a
    # bare/accidental invocation. The timer passes --apply explicitly.
    p.add_argument("--apply", action="store_true", help="actually remove (+ commit for screenshots) (default: report-only)")
    p.add_argument("--no-digest", action="store_true", help="skip the owner-mailbox digest")
    p.add_argument("--grace", default="30d", help="screenshots grace period, e.g. 30d or 12h (default 30d)")
    p.add_argument("--tmp-grace", default="48h", help="tmp-target grace period, e.g. 48h or 3d (default 48h)")
    p.add_argument("--repo-root", default=str(_REPO_ROOT_DEFAULT), help="repo clone to operate on (ticket lookups for both domains)")
    p.add_argument("--tmp-root", default="/tmp", help="root to scan for echo-T-NNNN/target/ workspaces (default /tmp)")
    p.add_argument(
        "--only", choices=["screenshots", "tmp-targets"], default=None,
        help="sweep only one domain (default: both — this is what keeps both domains on the same cadence)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        grace = parse_grace(args.grace)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        tmp_grace = parse_grace(args.tmp_grace)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    repo_root = Path(args.repo_root).resolve()
    tmp_root = Path(args.tmp_root)
    now = datetime.now(timezone.utc)
    dry_run = not args.apply

    run_screenshots = args.only in (None, "screenshots")
    run_tmp = args.only in (None, "tmp-targets")

    result = sweep(repo_root, grace, now, dry_run=dry_run) if run_screenshots else None
    tmp_result = (
        sweep_tmp_targets(tmp_root, repo_root, tmp_grace, now, dry_run=dry_run) if run_tmp else None
    )

    exit_code = 0

    if result is not None:
        for d in result.decisions:
            print(f"[{d.action}] screenshots/{d.ticket_id}: {d.reason}")
        # Loud, non-zero if there was stale work to do but it didn't land —
        # covers BOTH the pre-sync abort (nothing removed) and a post-removal
        # commit failure. tmp-targets has no analogous commit step (see
        # sweep_tmp_targets docstring) so it never affects the exit code —
        # a per-dir removal failure there is a logged warning, not a fail-loud.
        classified_removes = [d for d in result.decisions if d.action == "remove"]
        if not dry_run and classified_removes and not result.committed:
            print(f"error: {result.commit_detail}", file=sys.stderr)
            exit_code = 1

    if tmp_result is not None:
        for d in tmp_result.decisions:
            print(f"[{d.action}] tmp-targets/{d.workspace.name}: {d.reason}")
        for u in tmp_result.unmanaged:
            print(f"[unmanaged] {u} (non-ticket-shaped echo-* workspace; not swept)")

    # Digest only when something was removed in EITHER domain, and only under
    # --apply: a weekly janitor that finds nothing is a silent no-op, and a
    # report-only run delivers nothing durable (its decisions are on stdout).
    # The digest goes to the owner's mailbox, never the operator's chat (T-6370).
    shot_removed = result.removed if result is not None else []
    tmp_removed = tmp_result.removed if tmp_result is not None else []
    if not args.no_digest and not dry_run and (shot_removed or tmp_removed):
        shot_decisions = result.decisions if result is not None else []
        tmp_decisions = tmp_result.decisions if tmp_result is not None else []
        digest = build_digest(shot_decisions, shot_removed, tmp_decisions, tmp_removed, dry_run)
        try:
            send_owner_digest(digest, sender=GC_ACTOR, subject="artifact-gc digest")
        except Exception as exc:  # noqa: BLE001 — any delivery failure is loud
            print(f"error: owner digest not delivered ({type(exc).__name__}: {exc})",
                  file=sys.stderr)
            print(digest)
            exit_code = exit_code or 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
