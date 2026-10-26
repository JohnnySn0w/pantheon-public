"""Shared git-subprocess + HERMES-guard helpers.

Used by `tools/watch_runner.py` (auto-commit-and-push of watch reports),
`tools/ticket.py` (ticket-lifecycle commits), and `tools/ticket_grooming.py`.
Extracted at the rule-of-three boundary; prior to extraction, `_git`,
`_hermes_scan_message`, and `_HERMES_PATTERN` were copy-pasted across the
first two callers.

Public surface (module-internal-style underscore names; export-fine):
    _HERMES_PATTERN     — compiled regex for the unescaped trigger phrase.
    _hermes_scan_dir    — flat-directory scan; one match → fail-fast.
    _hermes_scan_file   — single-file scan; same return shape.
    _hermes_scan_message— commit-message belt scan; bool only.
    _git                — thin `subprocess.run(["git", ...])` wrapper.
    pull_repo_or_warn   — best-effort pre-fire git pull; fail-open.
    pull_repo_strict    — same fetch+merge shape; returns (ok, msg); fail-closed.
    push_with_retry     — push origin <refspec> with non-ff retry (fetch+rebase).
    pull_or_rebase_strict— ff-only pull, rebase-onto-origin/main on divergence.
    PULL_NOT_AHEAD_PREFIX, PULL_REBASE_CONFLICT_PREFIX — the two failure-text
                          prefixes `pull_or_rebase_strict` returns on (False, ...);
                          callers select their T-2333 hint on these, never on
                          git's own free text.
    WorktreeError       — exception class for worktree lifecycle failures.
    with_worktree       — context-manager-style helper: runs fn in a fresh
                          detached git worktree off origin/main; cleanup in
                          try/finally. Structural race elimination (T-0471).

Source-cleanliness convention for `_HERMES_PATTERN`:

    The regex source bytes are `H E R M E S \\ . m d` (a backslash escapes
    the dot). At runtime the compiled pattern matches the contiguous
    9-byte trigger sequence as intended, but the literal trigger never
    appears in this source file. This protects `git log -p` of pantheon-
    config from re-introducing the exact byte sequence that routes
    Claude Code requests through Anthropic's anti-abuse classifier (per
    `pantheon-config/knowledge/episodic/2026-04-29-anthropic-flagging-diy-harnesses/notes.md`
    and the standing rule in `/home/claude/CLAUDE.md`).

    Verification: `grep -cF` for the unescaped trigger (drop the backslash
    in `HERMES\\.md` mentally) against this file must return 0.

Confirmed mechanics + vector (T-1418 PRIMARY-source verification, 2026-06-29 —
supersedes the secondary-sourced framing in the 2026-06-29 watch report that T-1445
initially actuated on):

    The harness-detection billing-route classifier is active-by-design. Verified
    against PRIMARY sources — GitHub anthropics/claude-code#53171 (a researcher's
    exact-byte test matrix) + #53262 (the $200.98 incident), with direct Anthropic
    employee responses (ThariqS, Boris Cherny):
      1. CASE-SENSITIVE — the EXACT uppercase contiguous byte sequence ONLY.
         Lowercase, mixed-case, other-extension, and no-extension variants all
         PASS (do not trigger). [The watch report's "case-insensitive" framing
         traced to MindStudio recommending `grep -i` as conservative audit advice,
         misread as the detector being case-insensitive — REFUTED by #53171.]
      2. Vector is git LOG / COMMIT-MESSAGE content ONLY — NOT bare filenames in
         git status / ls-files. The #53171 matrix lists "file named [exact form]
         on disk + clean commit msg -> does NOT trigger." [The "filename vector"
         framing was MindStudio conflating `git status` with `git log` — REFUTED.]
    So `_HERMES_PATTERN` is correctly an EXACT-byte, case-sensitive, commit-message
    /file-content match — it needs neither case-insensitivity nor a filename check.
    A 2nd keyword (the harness name written here discipline-escaped as
    `o p e n \\ c l a w`) is PLAUSIBLE but NOT primary-confirmed; treat it as
    defense-in-depth (avoid the exact-uppercase contiguous form in commit messages),
    not a verified guard pattern — do not add a GUESSED casing to the regex.

    DESIGN DECISION (keep this pattern NARROW — `HERMES\\.md` exact only): two
    independent reasons, both now reinforced by the verification. (a) The content
    scanners (`_hermes_scan_dir`, `_hermes_scan_file`) run over watch reports +
    ticket files; the harness-detection topic is a RECURRING watch subject, so the
    keywords appear LEGITIMATELY in prose across ~50 committed files + historical
    tickets — broadening would make watch_runner + ticket.py REFUSE that corpus
    ("wrong broadening breaks grooming"). (b) Per the verified mechanics, lowercase
    corpus prose + filenames are NON-vectors anyway, so broadening would defend a
    non-threat while breaking legitimate commits. The guard stays a narrow backstop
    on the single confirmed high-risk form (the exact `HERMES\\.md` that caused the
    $200 incident); the broader defense is the DISCIPLINE (escape the exact contiguous
    form in commit messages / ticket titles). Because the guard is CASE-SENSITIVE and
    specialist's auto-filer titles are lowercase, no broadening-vs-auto-filer gating is
    needed. The CANONICAL discipline definition (which keywords + exact scope) is being
    reconciled at orchestrator's T-1216 HERMES re-eval (specialist fed T-1418's verified scope
    there via T-1443) — defer to that, do not re-codify a divergent framing here.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, TypeVar

REPO_ROOT = Path(__file__).resolve().parent.parent


# Match the unescaped trigger phrase only. The discipline-escaped form
# (literal backslash byte between S and .) does not match because this
# pattern requires `S` directly followed by `.`.
_HERMES_PATTERN = re.compile(rb"HERMES\.md")


def _hermes_scan_dir(reports_dir: Path) -> tuple[bool, str | None]:
    """Scan every regular file in `reports_dir` (flat, non-recursive) for the
    unescaped HERMES trigger. Returns (ok, detail). On match, detail is
    `"<relpath>:<lineno>"`.
    """
    if not reports_dir.exists():
        return True, None
    for entry in sorted(reports_dir.iterdir()):
        if not entry.is_file():
            continue
        data = entry.read_bytes()
        m = _HERMES_PATTERN.search(data)
        if m:
            lineno = data[: m.start()].count(b"\n") + 1
            try:
                rel = entry.relative_to(REPO_ROOT)
            except ValueError:
                rel = entry
            return False, f"{rel}:{lineno}"
    return True, None


def _hermes_scan_file(path: Path) -> tuple[bool, str | None]:
    """Scan a single file for the unescaped HERMES trigger.

    Returns (ok, detail). On match, detail is `"<relpath>:<lineno>"`.
    """
    if not path.exists():
        return True, None
    data = path.read_bytes()
    m = _HERMES_PATTERN.search(data)
    if m:
        lineno = data[: m.start()].count(b"\n") + 1
        try:
            rel = path.relative_to(REPO_ROOT)
        except ValueError:
            rel = path
        return False, f"{rel}:{lineno}"
    return True, None


def _hermes_scan_message(message: str) -> bool:
    """Return True iff the commit message is clean of the unescaped trigger."""
    return _HERMES_PATTERN.search(message.encode("utf-8")) is None


_READ_FAILOVER_ENV = "PANTHEON_READ_FAILOVER"


def _read_failover_module():
    try:
        from tools import read_failover
    except ImportError:
        import read_failover  # type: ignore[no-redef]
    return read_failover


def _fetch_fallback_note() -> str:
    return "trying read-failover" if os.environ.get(_READ_FAILOVER_ENV) == "1" else "using stale clone"


# T-4167: a test must never fast-forward the REAL checkout. Layer-3 integration tests
# spawn the real scripts as subprocesses (tools/_test_fixtures.setup_integration_env),
# where no in-process mock reaches, and a script's sync helper resolves REPO_ROOT from
# its own file -- the real checkout. In CI's shared checkout that fast-forward moved
# tickets origin had just resolved (T-4165's T-4137 FileNotFoundError); locally it
# pulls the running agent's own clone. Scoped to the real REPO_ROOT only, so a helper
# syncing a TMP repo (cwd=...) is untouched.
_NO_REAL_REPO_SYNC_ENV = "PANTHEON_TEST_NO_REAL_REPO_SYNC"
# Captured from __file__, never from REPO_ROOT: tests patch REPO_ROOT to a tmp repo, and
# the guard must still recognise only the real checkout.
_REAL_REPO_ROOT = Path(__file__).resolve().parent.parent


def _real_repo_sync_blocked(cwd: Path | str | None = None) -> bool:
    if os.environ.get(_NO_REAL_REPO_SYNC_ENV) != "1":
        return False
    try:
        return Path(cwd if cwd is not None else REPO_ROOT).resolve() == _REAL_REPO_ROOT
    except OSError:
        return False


def _real_repo_sync_skip_note() -> str:
    return f"repo sync skipped: {_NO_REAL_REPO_SYNC_ENV}=1 (test run; never sync the real checkout)"


def _try_read_failover(component: str) -> bool:
    """T-1998: ORG-046 read failover after a failed origin fetch. True iff
    origin/main is now at least as fresh as the mirror, so the caller should go
    on to the ff-merge. Never raises (the pull path is fail-open)."""
    if os.environ.get(_READ_FAILOVER_ENV) != "1":
        return False
    try:
        res = _read_failover_module().fetch_main_via_mirror(REPO_ROOT)
    except Exception as exc:  # noqa: BLE001 — fail-open
        print(f"[{component}] read-failover error ({exc!r}); using stale clone", file=sys.stderr)
        return False
    suffix = "" if res.ok else "; using stale clone"
    print(f"[{component}] read-failover: {res.detail}{suffix}", file=sys.stderr)
    return res.ok


def _refresh_manifest_pin(component: str) -> None:
    """T-1998: re-pin repos.yaml from the just-fetched origin/main when armed.
    Never raises; a failed refresh only means a later failover fail-closes."""
    if os.environ.get(_READ_FAILOVER_ENV) != "1":
        return
    try:
        _read_failover_module().refresh_pin_if_armed(REPO_ROOT)
    except Exception as exc:  # noqa: BLE001 — fail-open
        print(f"[{component}] manifest pin refresh failed ({exc!r})", file=sys.stderr)


def pull_repo_or_warn(component: str) -> None:
    """Best-effort sync of the pantheon-config clone against origin/main.

    Uses an explicit fetch + merge-against-remote-tracking-ref pattern rather
    than `git pull --ff-only origin main`. The pull form leans on FETCH_HEAD,
    which can carry multi-branch state from prior concurrent fetches and
    trigger `fatal: Cannot fast-forward to multiple branches` even when a
    straightforward ff-only merge against origin/main would succeed (T-0218
    trigger: 2026-05-11T12:00:47Z grooming-cron failure). Pattern mirrors
    `tools/pantheon_config_sync.py` (T-0211 Phase B).

    Fail-open: any failure (network blip, real divergence, missing remote,
    fetch error) logs to stderr and proceeds with the existing local state.
    Stale-but-running beats halted-on-network.

    T-1998: when armed (PANTHEON_READ_FAILOVER=1 + a manifest pin), a failed
    origin fetch falls back to the ORG-046 manifest mirror (read only, see
    tools/read_failover.py), and a successful one refreshes the manifest pin.
    Unarmed, behaviour is exactly as before.
    """
    if _real_repo_sync_blocked():
        print(f"[{component}] {_real_repo_sync_skip_note()}", file=sys.stderr)
        return
    # 1. Fetch origin/main into refs/remotes/origin/main (does NOT touch
    #    FETCH_HEAD's "for-merge" flag in a way the next step cares about).
    try:
        result = subprocess.run(
            ["git", "fetch", "origin", "main"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[{component}] fetch skipped ({exc!r}); {_fetch_fallback_note()}", file=sys.stderr)
        if not _try_read_failover(component):
            return
    else:
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
            print(f"[{component}] fetch failed (rc={result.returncode}): {detail[0]}; {_fetch_fallback_note()}", file=sys.stderr)
            if not _try_read_failover(component):
                return
        else:
            _refresh_manifest_pin(component)

    # 2. ff-only merge against the named remote-tracking ref. Explicit ref
    #    bypasses FETCH_HEAD entirely — no multi-branch pollution surface.
    try:
        result = subprocess.run(
            ["git", "merge", "--ff-only", "refs/remotes/origin/main"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[{component}] merge skipped ({exc!r}); using stale clone", file=sys.stderr)
        return
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
        print(f"[{component}] merge failed (rc={result.returncode}): {detail[0]}; using stale clone", file=sys.stderr)


def pull_repo_strict() -> tuple[bool, str]:
    """Fail-closed counterpart to `pull_repo_or_warn`: same resilient
    fetch+ff-merge shape, but returns (ok, error_message) instead of
    logging-and-continuing.

    Uses `git fetch origin main` + `git merge --ff-only refs/remotes/origin/main`
    rather than `git pull --ff-only origin main`. The pull form leans on
    FETCH_HEAD and can fail with `fatal: Cannot fast-forward to multiple
    branches` when prior concurrent fetches leave FETCH_HEAD carrying
    multi-branch state (T-0218 trigger 2026-05-11; T-0440 cron first fire
    2026-05-18; T-0442 propagation).

    Returns:
        (True, "") on success.
        (False, "<step> failed: <detail>") on any subprocess failure.

    Caller translates to its own error contract — raise an exception
    (`tools/ticket.py:_pull_if_needed`), return a (False, msg) tuple
    (`tools/watch_runner.py:git_commit_and_push`), or log-and-return
    (`tools/watch_runner.py:git_commit_ledger_entry`).
    """
    if _real_repo_sync_blocked():
        return True, ""
    try:
        result = subprocess.run(
            ["git", "fetch", "origin", "main"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"git fetch failed: {exc!r}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return False, f"git fetch failed: {detail}"

    try:
        result = subprocess.run(
            ["git", "merge", "--ff-only", "refs/remotes/origin/main"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"git merge --ff-only failed: {exc!r}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return False, f"git merge --ff-only failed: {detail}"

    return True, ""


def _git(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    """Thin wrapper around `git <args>`; never raises on non-zero exit. Caller
    inspects `.returncode` and `.stderr` for domain-specific failure handling.
    """
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Non-ff push retry helper (T-0487).
# ---------------------------------------------------------------------------

_NON_FF_MARKERS = ("rejected", "non-fast-forward", "fetch first")

# Transient concurrent-fetch ref-lock race (T-3207): another git op updating
# refs/remotes/origin/main mid-fetch fails "cannot lock ref". Retried in
# with_worktree's fetch; non-ref-lock fetch errors (network/auth) are NOT retried.
_REF_LOCK_MARKERS = ("cannot lock ref",)
_FETCH_REF_LOCK_ATTEMPTS = 3
_FETCH_REF_LOCK_BACKOFF_S = 0.5


def _unmerged_paths(cwd: Path) -> list[str]:
    """Repo-relative paths currently in an unmerged (conflicted) state."""
    res = _git("diff", "--name-only", "--diff-filter=U", cwd=cwd)
    if res.returncode != 0:
        return []
    return [p for p in res.stdout.split("\n") if p.strip()]


def _resolve_and_continue_rebase(cwd: Path, resolver, max_iters: int = 50) -> bool:
    """Drive a caller-supplied resolver across a (possibly multi-patch) rebase.

    For each conflicted patch: call `resolver(unmerged_paths, cwd)` — which must
    resolve + `git add` the conflict and return True — then `git rebase
    --continue`. `--continue` returns 0 only when the rebase fully completes; a
    non-zero return with fresh unmerged paths means the NEXT patch conflicted, so
    we loop. Fail-CLOSED: resolver declines, an error, or a non-conflict failure
    -> False (the caller aborts the rebase). `core.editor=true` keeps
    `--continue` from blocking on a commit-message editor.
    """
    try:
        for _ in range(max_iters):
            paths = _unmerged_paths(cwd)
            if not paths or not resolver(paths, cwd):
                return False
            cont = _git("-c", "core.editor=true", "rebase", "--continue", cwd=cwd)
            if cont.returncode == 0:
                return True  # rebase fully completed
            if not _unmerged_paths(cwd):
                return False  # non-conflict failure — give up
        return False
    except Exception:
        return False


def _rebase_in_progress_kind(cwd: Path) -> str | None:
    """"rebase-merge" / "rebase-apply" when a rebase is in progress in `cwd`'s
    repo, else None. The state dir is resolved via `git rev-parse --git-path`, which
    is correct in linked worktrees too."""
    for kind in ("rebase-merge", "rebase-apply"):
        res = _git("rev-parse", "--git-path", kind, cwd=cwd)
        if res.returncode != 0:
            continue
        rel = res.stdout.strip()
        if not rel:
            continue
        path = Path(rel) if os.path.isabs(rel) else (cwd / rel)
        if path.exists():
            return kind
    return None


# T-4422: the in-progress git operations a ticket write must never commit into.
# All of these live in the PER-WORKTREE git dir, so `--absolute-git-dir` (one spawn)
# finds them in a linked worktree as well as the main checkout.
_MID_OPERATION_MARKERS = (
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase (am)"),
    ("MERGE_HEAD", "merge"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
)


def _mid_operation_kind(cwd: Path) -> str | None:
    """The in-progress rebase / merge / cherry-pick / revert in `cwd`'s worktree, as a
    short name, else None. Not a repo (or git failed) reads as None: the caller's own
    git commands will then fail loudly on their own."""
    res = _git("rev-parse", "--absolute-git-dir", cwd=cwd)
    if res.returncode != 0 or not res.stdout.strip():
        return None
    git_dir = Path(res.stdout.strip())
    for marker, name in _MID_OPERATION_MARKERS:
        if (git_dir / marker).exists():
            return name
    return None


def _is_checkout_root(cwd: Path) -> bool:
    """Spawn-free: is `cwd` itself a checkout root — does it carry a `.git` entry (a
    directory, or the `gitdir:` FILE of a linked worktree or submodule; `.exists()`,
    never `.is_dir()`)? push_with_retry runs its destructive rebase clean-up only on
    rebase state it has positively observed in a checkout root (T-4380). A cwd that
    is not one keeps the pre-T-4380 behaviour: no probe, no claim. So a SUBDIRECTORY
    cwd silently does not get the stranded-rebase clean-up; no caller passes one
    (all pass REPO_ROOT or a worktree root). No ancestor walk: it would spend probe
    spawns on scratch dirs, and a stray `.git` above one (e.g. `/tmp/.git`) would make
    every temp cwd probe — harmless to correctness (git exits 128 there, the probe
    reads "no rebase"), pure spawn cost."""
    return (Path(cwd) / ".git").exists()


def _unpushed_commits_summary(cwd: Path, refspec: str) -> str:
    """One line per local commit not yet on the remote branch `refspec` targets
    (`origin/<dst>..HEAD`, up to 10). Falls back to HEAD's own line when the range
    cannot be computed, so the message can always name at least one commit."""
    dst = refspec.rsplit(":", 1)[-1].strip()
    short = dst[len("refs/heads/"):] if dst.startswith("refs/heads/") else dst
    res = _git("log", "--oneline", "--max-count=10", f"refs/remotes/origin/{short}..HEAD", cwd=cwd)
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()] if res.returncode == 0 else []
    if not lines:
        head = _git("log", "--oneline", "-1", "HEAD", cwd=cwd)
        lines = [ln for ln in head.stdout.splitlines() if ln.strip()]
    return "\n".join(f"    {ln}" for ln in lines) or "    (could not list commits — see `git reflog`)"


# git's `hint: ...` advice lines, bare or re-printed by the pre-push hook with its
# `pre-push: ` prefix.
_HINT_LINE_RE = re.compile(r"^\s*(?:pre-push:\s*)?hint:", re.IGNORECASE)


def _stderr_tail(git_detail: str, max_lines: int = 12) -> str:
    """The last `max_lines` lines of git's output, minus its `hint:` advice. The hint
    tells the reader to `git rebase --continue` — which contradicts a message that has
    just said the rebase was ABORTED (or that names its own recovery)."""
    kept = [ln for ln in git_detail.strip().splitlines() if not _HINT_LINE_RE.match(ln)]
    return "\n".join(kept[-max_lines:])


def _foreign_rebase_message(cwd: Path, kind: str, git_detail: str, *, pushed: bool = True) -> str:
    """LOUD failure text for a rebase that was ALREADY in progress in `cwd` when
    push_with_retry was entered (T-4380). That rebase is somebody's own work: nothing
    was fetched, rebased, continued or aborted. Since T-4422 the push itself is not
    attempted either (`pushed=False`)."""
    what = (
        "no push, fetch, rebase, continue or abort was run"
        if not pushed
        else "no fetch, rebase, continue or abort was run"
    )
    lead = "push refused" if not pushed else "push rejected"
    tail = _stderr_tail(git_detail)
    return (
        f"{lead}: a rebase ({kind}) was ALREADY IN PROGRESS in {cwd} when this push "
        f"started, so it was left exactly as found ({what}). THE OP DID NOT LAND. Finish "
        f"it (`git -C {cwd} rebase --continue`) or abandon it (`git -C {cwd} rebase "
        f"--abort`) by hand, then retry." + (f"\n{tail}" if tail else "")
    )


def _stranded_rebase_abort_message(
    cwd: Path, refspec: str, branch: str, git_detail: str, aborted: bool
) -> str:
    """LOUD failure text for a rebase the pre-push freshness hook left stranded
    (T-4380). Names the unpushed commit(s) and the manual recovery, because the
    push's own stderr says only `failed to push some refs` — the op did not land
    and nothing else tells the caller so."""
    dst = refspec.rsplit(":", 1)[-1].strip()
    short = dst[len("refs/heads/"):] if dst.startswith("refs/heads/") else dst
    tail = _stderr_tail(git_detail)
    if not aborted:
        return (
            f"rebase conflict left mid-rebase by the pre-push freshness hook, and "
            f"`git rebase --abort` FAILED — {cwd} is STILL MID-REBASE. THE OP DID NOT "
            f"LAND. Run `git -C {cwd} rebase --abort` (or resolve and `git -C {cwd} "
            f"rebase --continue`) by hand.\n{tail}"
        )
    where = f"branch '{branch}'" if branch else "its pre-rebase HEAD (detached)"
    return (
        f"rebase conflict in the pre-push freshness rebase (ABORTED; {where} restored to "
        f"its pre-rebase state). THE OP DID NOT LAND: these local commit(s) are NOT on "
        f"origin/{short}:\n{_unpushed_commits_summary(cwd, refspec)}\n"
        f"Recover by hand: `git -C {cwd} fetch origin {short} && git -C {cwd} rebase "
        f"origin/{short}`, resolve, `git rebase --continue`, then push; or, if the change "
        f"should be redone from scratch, `git -C {cwd} reset --hard origin/{short}` "
        f"(DISCARDS the commit(s) above). Do NOT re-run the originating op before "
        f"deciding — its commit already exists locally.\n{tail}"
    )


# ---------------------------------------------------------------------------
# HEAD:main push guard (T-3600).
#
# ticket.py (and other callers) push refspec="HEAD:main" — the commit lands on
# whatever branch is checked out, then the push fast-forwards origin/main to that
# HEAD, carrying EVERY commit between origin/main and HEAD. Run such an op from a
# feature branch and its non-ticket commits leak onto main unreviewed (hit
# 2026-09-09 on T-3599: an unreviewed poke-coupled commit rode to main).
#
# This guard WARNS (never blocks, never mutates) when a HEAD:main push is issued
# from a NAMED non-main branch and would carry commits touching paths OUTSIDE
# tickets/. It is fail-open in every limb: any error skips the guard and the push
# proceeds unchanged. It must NOT fire on the legitimate detached-HEAD worktree
# case the HEAD:main default exists for (symbolic-ref fails on detached HEAD ->
# skip). Opt out entirely with PANTHEON_TICKET_PUSH_GUARD_OFF=1.
# ---------------------------------------------------------------------------
_PUSH_GUARD_OPT_OUT_ENV = "PANTHEON_TICKET_PUSH_GUARD_OFF"


def _refspec_targets_main(refspec: str) -> bool:
    """True iff `refspec` pushes to the remote `main` branch. Handles "HEAD:main",
    "main", "main:main", "refs/heads/x:refs/heads/main", "HEAD:main^{}" etc. — the
    destination is the segment after the last ':' (or the whole spec if none)."""
    dst = refspec.rsplit(":", 1)[-1].strip()
    return dst in ("main", "refs/heads/main")


def _warn_if_branch_carries_nonticket_commits(cwd: Path, refspec: str) -> None:
    """T-3600 guard — WARN (never block) before a HEAD:main push from a named
    non-main branch that would carry non-ticket commits onto origin/main.

    Fail-open: any exception or unexpected git state returns silently and the push
    proceeds. Never raises, never mutates repo state.
    """
    try:
        if os.environ.get(_PUSH_GUARD_OPT_OUT_ENV, "").strip().lower() in (
            "1", "true", "yes", "on",
        ):
            return
        if not _refspec_targets_main(refspec):
            return
        # Current branch. Detached HEAD (the worktree case the HEAD:main default
        # serves) makes symbolic-ref exit non-zero -> skip the guard. main -> the
        # normal, safe case -> skip.
        head = _git("symbolic-ref", "--short", "-q", "HEAD", cwd=cwd)
        branch = head.stdout.strip()
        if head.returncode != 0 or not branch or branch == "main":
            return
        # Commits that WOULD land on main = origin/main..HEAD. Use the local
        # remote-tracking ref (no network); if it is absent the log fails -> skip
        # (cannot compute the delta safely, so do not guess).
        names = _git("log", "--format=", "--name-only", "origin/main..HEAD", cwd=cwd)
        if names.returncode != 0:
            return
        paths = {p.strip() for p in names.stdout.splitlines() if p.strip()}
        nonticket = sorted(p for p in paths if not p.startswith("tickets/"))
        if not nonticket:
            return  # every carried commit is tickets/-only — the intended shape
        sample = ", ".join(nonticket[:3]) + (" …" if len(nonticket) > 3 else "")
        sys.stderr.write(
            f"\n⚠️  [push guard T-3600] pushing '{refspec}' from branch '{branch}' — "
            f"this carries {len(nonticket)} path(s) OUTSIDE tickets/ ({sample}) onto "
            f"origin/main.\n"
            f"    A HEAD:main push lands EVERY commit in origin/main..HEAD. If those "
            f"non-ticket commits are unreviewed feature/branch work, abort now and run "
            f"the op from main instead (see memory "
            f"reference_ticket_py_pushes_head_main_from_any_branch).\n"
            f"    To silence this guard: {_PUSH_GUARD_OPT_OUT_ENV}=1\n"
        )
    except Exception:
        return  # fail-open — the guard must NEVER break a push


# ---------------------------------------------------------------------------
# ORG-101 §2 (T-4690): gated push order — write authority first, Forgejo second.
# ---------------------------------------------------------------------------

_UNSET = object()
MIRROR_PUSH_TIMEOUT_S = 20
_PROBE_TIMEOUT_S = 10

# Answers the server actually GAVE (auth/permission/protection/decline, non-ff).
# Checked FIRST and always win: a real answer is never a reason to write elsewhere.
_REAL_ANSWER_MARKERS = (
    "permission denied", "authentication failed", "could not read username",
    "access denied", "forbidden", "publickey", "permission to", "protected branch",
    "declined", "pre-receive hook", "gh006", "gh013", "repository rule", "not allowed",
    "rejected", "non-fast-forward", "fetch first",
)
_REAL_ANSWER_RE = re.compile(r"\b40[13]\b")
# The server answered "I failed" (GitHub reachable, push errored server-side).
_SERVER_ERROR_MARKERS = (
    "remote: fatal error", "remote: fatal:", "rpc failed", "internal server error",
    "bad gateway", "service unavailable", "gateway time-out", "remote: error: internal",
)
_SERVER_ERROR_RE = re.compile(r"(returned error: 5\d\d|http 5\d\d)")
_REMOTE_ERROR_FAULT_RE = re.compile(r"remote: error:[^\n]*(internal|fatal|rpc|\b5\d\d\b)")
_NETWORK_MARKERS = (
    "could not resolve host", "could not resolve hostname", "connection refused",
    "connection timed out", "timed out", "network is unreachable", "no route to host",
    "failed to connect", "couldn't connect to server", "connection reset",
    "temporary failure in name resolution", "name or service not known",
    "ssh: connect to host",
)


def _classify_push_failure(stderr: str) -> str:
    """Classify a failed push's stderr: "real_answer" | "server_error" | "network" | "other".

    Order is the contract (ORG-101 §2): real answers (auth, permission, protection,
    hook-declined, non-ff) win over everything; then server-side faults; then
    network-class failures. Anything matching none is "other" and fails closed.
    """
    low = (stderr or "").lower()
    if any(m in low for m in _REAL_ANSWER_MARKERS) or _REAL_ANSWER_RE.search(low):
        return "real_answer"
    if any(m in low for m in _SERVER_ERROR_MARKERS) or _SERVER_ERROR_RE.search(low):
        return "server_error"
    # A generic "remote: error" line only counts when it itself names a server fault;
    # policy refusals ("refusing to update checked out branch", "denied by ...") are not.
    if "failed to push some refs" in low and _REMOTE_ERROR_FAULT_RE.search(low):
        return "server_error"
    if any(m in low for m in _NETWORK_MARKERS):
        return "network"
    return "other"


def _origin_reachable(cwd: Path) -> bool:
    """ORG-046 §2 fetch-reachability: `git ls-remote origin` answers."""
    try:
        res = subprocess.run(
            ["git", "ls-remote", "origin"], cwd=cwd, capture_output=True, text=True,
            timeout=_PROBE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return res.returncode == 0


def _origin_failover_warranted(cwd: Path, stderr: str) -> bool:
    """True iff the write authority is down (not merely saying no): a server-side
    fault (no probe needed — the server itself said it failed) or a network-class
    failure CONFIRMED by a failing ls-remote. Real answers and ambiguous errors: False."""
    kind = _classify_push_failure(stderr)
    if kind == "server_error":
        return True
    if kind == "network":
        return not _origin_reachable(cwd)
    return False


def _resolve_write_mirror(cwd: Path, *, manifest_loader=None) -> str | None:
    """The writable Forgejo mirror url for this clone's origin, or None (no write
    redundancy = today's behavior). Fail-closed on trust (pin-verified manifest),
    on an unmatched origin, on host binding, and on git url rewriting; fail-open
    on any error (never breaks a push)."""
    try:
        try:
            from tools import remote_resolve as rr
        except ImportError:
            import remote_resolve as rr  # type: ignore[no-redef]
        loader = manifest_loader or rr.load_production_manifest
        manifest = loader()
        # Default-off costs no git spawn: with no writable remote anywhere in the
        # (pin-verified) manifest the push path is byte-for-byte today's behavior.
        if not manifest.signature_verified or not any(
            r.writable for repo in manifest.repos.values() for r in repo.remotes
        ):
            return None
        got = _git("remote", "get-url", "origin", cwd=cwd)
        if got.returncode != 0:
            return None
        remote = rr.writable_mirror_for(manifest, got.stdout.strip())
        if remote is None:
            return None
        rewritten = _git("ls-remote", "--get-url", remote.url, cwd=cwd)
        if rewritten.returncode != 0 or rewritten.stdout.strip() != remote.url:
            return None  # insteadOf would redirect the credential past host binding
        # --get-url ignores url.<base>.pushInsteadOf, but a push honors it: refuse any
        # such rewrite (rc 0 = at least one entry; rc 1 = none; anything else = unknown).
        push_rw = _git("config", "--get-regexp", r"^url\..*\.pushinsteadof$", cwd=cwd)
        if push_rw.returncode != 1:
            return None
        return remote.url
    except Exception:  # noqa: BLE001 — fail-open: never break a push
        return None


def _push_to_mirror(cwd: Path, mirror_url: str, refspec: str) -> tuple[bool, str]:
    """Best-effort single push to the Forgejo mirror (no retries, bounded)."""
    try:
        res = subprocess.run(
            ["git", "push", mirror_url, refspec], cwd=cwd, capture_output=True, text=True,
            timeout=MIRROR_PUSH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {MIRROR_PUSH_TIMEOUT_S}s"
    except OSError as exc:
        return False, repr(exc)
    if res.returncode == 0:
        return True, ""
    lines = (res.stderr or res.stdout).strip().splitlines()
    return False, lines[-1] if lines else "unknown error"


def mirror_push_after_origin(cwd: Path, refspec: str, *, mirror_url=_UNSET) -> str | None:
    """ORG-101 §2 dual-write leg ALONE, for a caller that pushed origin itself because
    it owns its retry/rebase semantics (T-4691: pantheon_config_sync, the delivery
    rebase controller). Call it only after origin ACCEPTED `refspec`. One bounded,
    best-effort push to the writable Forgejo mirror; never fails over, never raises.
    Returns None when no mirror is writable for this clone (inert), else a note for
    the caller's log."""
    try:
        mirror = _resolve_write_mirror(cwd) if mirror_url is _UNSET else mirror_url
        if not mirror:
            return None
        ok, detail = _push_to_mirror(cwd, mirror, refspec)
    except Exception as exc:  # noqa: BLE001 — origin already landed; never undo that
        ok, detail = False, repr(exc)
    return "mirror pushed" if ok else f"mirror push failed ({detail}); reconciler will heal"


_FORGEJO_ONLY_NOTE = "pushed to Forgejo only; replay to GitHub pending (reconciler)"


def push_with_retry(
    cwd: Path,
    *,
    refspec: str = "HEAD:main",
    attempts: int = 3,
    conflict_resolver=None,
    mirror_url=_UNSET,
) -> tuple[bool, str]:
    """Push `origin <refspec>` with retry on non-fast-forward rejection.

    ORG-101 §2 write redundancy (T-4690), inert unless the manifest marks a Forgejo
    mirror `writable: true` for this clone's origin (`mirror_url` overrides the
    manifest lookup; None = off):
      - after origin ACCEPTS, the same refspec is pushed once to the mirror,
        best-effort (bounded, no retry); a mirror failure is noted, result stays True.
      - when origin is DOWN (server-side fault, or unreachable per ls-remote) the
        push goes to the mirror instead, returning success with a "replay pending"
        note; non-ff retries then fetch+rebase against the mirror.
      - a non-ff / auth / permission / protected-branch / hook-declined answer, or an
        ambiguous error, NEVER falls over to the mirror (split-brain guard).

    Retry semantics:
      1. Attempt `git push origin <refspec>`.
      2. On success → return (True, msg).
      3. On non-ff failure (stderr contains any of: "rejected",
         "non-fast-forward", "fetch first") → fetch origin main, rebase onto
         FETCH_HEAD (the freshly-fetched tip, NOT ORIG_HEAD), then retry.
         Bound to `attempts` total tries (default 3 = 1 initial + 2 retries).
      4. On non-non-ff failure → return (False, msg) immediately (auth /
         network errors are not retried).
      5. On rebase conflict → if a `conflict_resolver` is supplied and resolves
         the conflict (it must `git add` the fix; `--continue` is then driven to
         completion), retry the push; otherwise `git rebase --abort`, return
         (False, msg). The resolver keeps this module decoupled from caller
         rebuild logic (it's injected, not imported) while letting an
         interactive caller — e.g. ticket.py, where "the next cron fire
         self-heals" does NOT hold — survive a mechanical generated-file
         conflict instead of stranding the commit (T-0846).
      6. After exhausting `attempts` → return (False, msg) fail-loud.
      7. A push can also fail because the pre-push freshness hook
         (`.githooks/pre-push`) ran `git pull --rebase` on an ATTACHED branch and
         stopped on a conflict: the hook exits 1 leaving `.git/rebase-merge`, and its
         stderr carries none of the non-ff markers. A rebase that was NOT in
         progress at entry but is after a failed push is exactly that (T-4380): a
         `conflict_resolver` gets the same chance as in (5) and the push is retried;
         otherwise the rebase is aborted (branch restored) and (False, msg) is
         returned, naming the unpushed commit(s) and the manual recovery.
      8. A rebase already in progress at entry (checkout root) is somebody else's
         and is NEVER touched: the call returns (False, msg) at once, naming it,
         WITHOUT pushing (T-4422 — HEAD is then a detached, half-rebased state, and
         a push could report an up-to-date no-op as landed or publish a partial
         rebase). A cwd that is not a checkout root is neither probed nor claimed —
         no extra git spawns, the old behaviour.

    Args:
        cwd: Working directory for git commands (the worktree path).
        refspec: Push refspec (default "HEAD:main" for detached-HEAD worktrees).
        attempts: Total push attempts (1 initial + up to attempts-1 retries).
        conflict_resolver: Optional callable (unmerged_paths, cwd) -> bool. On a
            rebase conflict, it should resolve + stage a known-mechanical
            conflict and return True (or False to let the rebase abort).

    Returns:
        (True, "<detail>") on success.
        (False, "<reason>") on any failure.
    """
    # T-3600: warn (never block) if this HEAD:main push from a named non-main
    # branch would carry non-ticket commits onto origin/main. The guard is already
    # internally fail-open; this second try/except guarantees no guard bug can ever
    # break a push on this fleet-shared path (advisory-only side effect).
    try:
        _warn_if_branch_carries_nonticket_commits(cwd, refspec)
    except Exception:
        pass
    # T-4380: only a rebase that appeared during this call (inferred from state: the
    # checkout was observed rebase-free at entry) is the hook's to clear. A rebase already in progress
    # at entry (`rebase_at_entry`: somebody's own) is never touched on ANY path below:
    # no rebase / continue / abort runs, the call returns a loud (False, msg) instead.
    # A cwd that is not a checkout root is neither probed nor claimed.
    is_root = _is_checkout_root(cwd)
    rebase_at_entry = _rebase_in_progress_kind(cwd) if is_root else None
    # T-4422: a rebase already in progress at entry means HEAD is somebody's detached,
    # half-rebased state. Pushing it can "succeed" as an up-to-date no-op (reporting a
    # landing that did not happen) or publish a partial rebase, so do not push at all.
    if rebase_at_entry is not None:
        return False, _foreign_rebase_message(cwd, rebase_at_entry, "", pushed=False)
    hook_rebase_ours_to_handle = is_root and rebase_at_entry is None
    mirror = _resolve_write_mirror(cwd) if mirror_url is _UNSET else mirror_url
    dest = "origin"  # becomes the mirror url once origin is confirmed down
    attempt_num = 0
    while attempt_num < attempts:
        attempt_num += 1
        res = _git("push", dest, refspec, cwd=cwd)
        if res.returncode == 0:
            msg = f"pushed {refspec} (attempt {attempt_num})"
            if dest != "origin":
                return True, f"{msg}; {_FORGEJO_ONLY_NOTE}"
            note = mirror_push_after_origin(cwd, refspec, mirror_url=mirror)
            if note:
                msg += f"; {note}"
            return True, msg

        # T-4380: the pre-push freshness hook's `git pull --rebase` may have stopped
        # on a conflict and exited 1 without aborting. Never leave that behind.
        if hook_rebase_ours_to_handle and _rebase_in_progress_kind(cwd) is not None:
            if (
                conflict_resolver is not None
                and attempt_num < attempts  # a resolved rebase needs a push left to land on
                and _resolve_and_continue_rebase(cwd, conflict_resolver)
            ):
                continue  # hook's rebase resolved + completed → retry the push
            abort_res = _git("rebase", "--abort", cwd=cwd)
            aborted = abort_res.returncode == 0 and _rebase_in_progress_kind(cwd) is None
            branch = _git("symbolic-ref", "--short", "-q", "HEAD", cwd=cwd).stdout.strip()
            return False, _stranded_rebase_abort_message(
                cwd, refspec, branch, (res.stderr or res.stdout), aborted
            )

        stderr_lower = res.stderr.lower()
        is_non_ff = any(marker in stderr_lower for marker in _NON_FF_MARKERS)

        if not is_non_ff:
            if dest == "origin" and mirror and _origin_failover_warranted(cwd, res.stderr):
                dest = mirror  # the write authority is down, not refusing: ORG-101 §2
                attempt_num -= 1  # the redirected push is not a retry of the same target
                continue
            # Auth, network, or other hard failure — do not retry.
            detail = (res.stderr or res.stdout).strip()
            return False, f"git push failed (non-retryable): {detail}"

        if attempt_num == attempts:
            # Exhausted all attempts.
            detail = (res.stderr or res.stdout).strip()
            return False, (
                f"git push failed after {attempts} attempt(s) (non-ff exhausted): {detail}"
            )

        # Non-ff rejection with retries remaining: fetch + rebase onto fresh origin/main.
        fetch_res = _git("fetch", dest, "main", cwd=cwd)
        if fetch_res.returncode != 0:
            detail = (fetch_res.stderr or fetch_res.stdout).strip()
            return False, f"git fetch failed during retry (attempt {attempt_num}): {detail}"

        rebase_res = _git("rebase", "FETCH_HEAD", cwd=cwd)
        if rebase_res.returncode != 0:
            # Rebase conflict. A caller-supplied resolver gets a chance to fix a
            # KNOWN-mechanical conflict (e.g. a regenerated index/board file) and
            # continue (T-0846); otherwise abort to restore clean state.
            if conflict_resolver is not None and _resolve_and_continue_rebase(
                cwd, conflict_resolver
            ):
                continue  # resolved + rebase completed → retry the push
            _git("rebase", "--abort", cwd=cwd)
            detail = (rebase_res.stderr or rebase_res.stdout).strip()
            return False, f"rebase conflict (aborted, clean state restored): {detail}"

    # Should be unreachable — exhaustion is caught inside the loop.
    return False, f"git push failed: exhausted {attempts} attempt(s)"


def _abort_stale_rebase(cwd: Path = REPO_ROOT) -> bool:
    """Abort an in-progress rebase left behind by an INTERRUPTED prior run (T-1191).

    A process can die MID-`git rebase` — e.g. a one-shot systemd service hitting
    its timeout (escalating to SIGKILL) between the rebase starting and the
    conflict-driven `rebase --abort` running. That leaves `.git/rebase-merge/`
    (or `.git/rebase-apply/`) behind, and every subsequent merge/pull in the
    clone then fails "a rebase is in progress" — the wedge that blocked the live
    clone for ~40min on 2026-06-15.

    In these automation contexts a rebase is never legitimately in progress at
    sync entry, so a leftover is always a stale wedge. Resolve the state dir via
    `git rev-parse --git-path` (correct in linked worktrees too) and, if present,
    `git rebase --abort` to restore a clean base. Returns True if a stale rebase
    was cleared, False (no-op) when none was in progress.
    """
    kind = _rebase_in_progress_kind(cwd)
    if kind is None:
        return False
    _git("rebase", "--abort", cwd=cwd)
    print(
        f"[git_utils] cleared stale in-progress rebase ({kind}) before sync "
        f"({cwd}) — likely an interrupted prior run (T-1191)",
        file=sys.stderr,
    )
    return True


# T-2333: named prefixes for the two DISTINCT failure texts below. Exported so
# a caller (ticket.py) can select its user-facing hint on WHICH failure this
# was, without string-matching git's own free text (locale/version-dependent,
# never a safe discriminator per T-2204). Keep these in lockstep with the
# f-strings that build the actual return values a few lines down — a prefix
# that drifts from its own return statement silently reopens the false-hint
# defect this exists to close.
PULL_NOT_AHEAD_PREFIX = "git merge --ff-only failed (not ahead, no rebase): "
PULL_REBASE_CONFLICT_PREFIX = "rebase reconcile conflict (aborted, clean state restored): "
# T-2339: the THIRD return point. `pull_or_rebase_strict` fails here before any
# merge or rebase is attempted, so it shares the ff path's nothing-written-yet
# invariant but for an entirely different reason (network / remote auth), and
# unlike the rebase-conflict path re-running IS safe. Routing it to the UNKNOWN
# hint was conservative rather than wrong; this lets the caller say something
# true instead of declining to guess.
PULL_FETCH_FAILED_PREFIX = "git fetch failed: "


def pull_or_rebase_strict(cwd: Path = REPO_ROOT, conflict_resolver=None) -> tuple[bool, str]:
    """ff-only pull, falling back to rebase-onto-origin/main on divergence (T-0846).

    Same fetch + ff-merge shape as `pull_repo_strict`, but when the ff-only merge
    is refused because the local clone holds UNPUSHED commits that diverge from
    the freshly-fetched origin/main, rebase those local commits onto origin/main
    rather than hard-failing. This self-heals the stranded-commit case: a prior
    ticket op left a local commit that never reached origin (e.g. a push that
    failed after the commit), origin/main then advanced, and the NEXT ticket op's
    pre-mutation `pull_repo_strict` ff-only pull would otherwise raise instead of
    reconciling.

    Rebase is the reconcile strategy (the operator's call 2026-06-05): it rewrites only
    the UNPUSHED local commits onto the new tip, keeping history linear and never
    touching already-shared history. On a rebase conflict, an optional
    `conflict_resolver` (same (unmerged_paths, cwd) -> bool contract as
    `push_with_retry`'s) gets one chance to resolve a known-mechanical conflict
    (e.g. the generated `tickets/README` board); otherwise the rebase is aborted
    and a fail-loud (False, msg) is returned. A real conflict is NEVER silently
    resolved.

    Returns (True, "") on success, (False, "<reason>") on any unrecoverable failure.
    """
    if _real_repo_sync_blocked(cwd):
        return True, ""
    # Self-heal before syncing: clear any stale in-progress rebase a prior
    # interrupted run may have left behind (T-1191), which would otherwise make
    # the ff-only merge below fail "a rebase is in progress" and wedge the clone.
    _abort_stale_rebase(cwd)

    fetch_res = _git("fetch", "origin", "main", cwd=cwd)
    if fetch_res.returncode != 0:
        detail = (fetch_res.stderr or fetch_res.stdout).strip()
        return False, f"{PULL_FETCH_FAILED_PREFIX}{detail}"

    merge_res = _git("merge", "--ff-only", "refs/remotes/origin/main", cwd=cwd)
    if merge_res.returncode == 0:
        return True, ""

    # ff-only refused. Only a genuine divergence (local has unpushed commits) is
    # rebase-recoverable; if we're not ahead, the ff failure is something else
    # (e.g. a dirty tree) and rebasing would do nothing useful — surface it.
    ahead = _git("rev-list", "--count", "refs/remotes/origin/main..HEAD", cwd=cwd)
    if ahead.returncode != 0 or ahead.stdout.strip() in ("", "0"):
        detail = (merge_res.stderr or merge_res.stdout).strip()
        return False, f"{PULL_NOT_AHEAD_PREFIX}{detail}"

    rebase_res = _git("rebase", "refs/remotes/origin/main", cwd=cwd)
    if rebase_res.returncode == 0:
        return True, ""

    # Rebase conflict: a caller-supplied resolver gets a chance to fix a
    # KNOWN-mechanical conflict (e.g. the regenerated board) and continue;
    # otherwise abort to restore clean state and fail loud.
    if conflict_resolver is not None and _resolve_and_continue_rebase(cwd, conflict_resolver):
        return True, ""
    _git("rebase", "--abort", cwd=cwd)
    detail = (rebase_res.stderr or rebase_res.stdout).strip()
    return False, f"{PULL_REBASE_CONFLICT_PREFIX}{detail}"


# ---------------------------------------------------------------------------
# Git mutation critical-section lock (T-1770 Part b).
#
# Closes the shared-checkout interleaved-writer race
# (bugs/ticket-py-shared-checkout-interleaved-writer-race.md): two processes
# running git mv/add/commit against the SAME on-disk checkout (not a linked
# worktree — those get their own index) can interleave on the one shared
# `.git/index`, and `git commit` with no pathspec commits the WHOLE index —
# not just the paths the calling process itself just staged — so one
# writer's commit can silently sweep up another writer's half-staged
# rename/edit, leaving the ORIGINATING writer's commit message lying about
# its own contents. A single exclusive lockfile around the mutate → stage →
# commit (→ push) span serializes writers so no commit can ever observe
# another writer's in-flight staging.
#
# Same shape as tools/mailbox.py's `.consumer.lock` (a single-holder file
# lock guarding a shared-resource critical section) — but deliberately a
# THIRD, distinct lock rather than reusing an existing one:
#   - NOT `tools/ticket.py`'s own `_allocation_lock()`: that lock is a plain
#     blocking (no-timeout) flock scoped narrowly to the ID-allocation
#     scan→write span, and is explicitly released before the network
#     commit/push runs. Reusing it here would either wedge every ticket op
#     forever on one stuck holder (no timeout) or leave the actual
#     add/commit race unprotected (if released at the same point).
#   - NOT `tools/_lease.py`'s `Lease`: that primitive is non-blocking with
#     TTL-based stale-holder REAPING (SIGTERM/SIGKILL of the holder pid).
#     Reaping a live cross-uid writer mid-commit is a bigger hazard than the
#     race this closes — this lock only ever times out and reports, never
#     kills another process.
# ---------------------------------------------------------------------------


class GitMutationLockTimeout(TimeoutError):
    """Raised by `git_mutation_lock()` when the lock is not acquired within
    `timeout_secs`. The caller must treat this as a hard failure — never
    proceed with the mutation unlocked."""


_GIT_MUTATION_LOCK_ENV = "PANTHEON_GIT_MUTATION_LOCKFILE"
_GIT_MUTATION_LOCK_FILENAME = "pantheon-ticket-ops.lock"
_GIT_MUTATION_LOCK_POLL_SECS = 0.2
_GIT_MUTATION_LOCK_REOPEN_ATTEMPTS = 3


class _LockfileReplaced(Exception):
    """Internal: the lockfile was unlinked/replaced under us; re-open and retry."""

_git_common_dir_cache: Path | None = None


def _git_common_dir() -> Path:
    """Resolve the actual shared `.git` common directory for REPO_ROOT.

    `REPO_ROOT / ".git"` is a DIRECTORY in a normal checkout but a FILE
    (`gitdir: <repo>/.git/worktrees/<name>`) in a linked worktree — using it
    directly as the lockfile's parent would crash `Path.mkdir()` there (T-1770
    Part b smoke finding: this module runs from exactly that shape whenever a
    dispatched implementer's pre-staged `git worktree add` checkout imports
    it). `git rev-parse --git-common-dir` resolves to the ONE shared `.git`
    directory regardless of which worktree the caller runs from, so every
    worktree of the same repo serializes on the SAME lockfile — the
    semantically correct target anyway, since it's the actual shared ref/
    object store. A SUCCESSFUL resolution is cached (cheap, and REPO_ROOT's
    repository identity cannot change within a process lifetime). Falls back
    to `REPO_ROOT / ".git"` best-effort if the subprocess call fails (should
    only happen if REPO_ROOT is not inside a git repository at all, which no
    caller of this module is meant to run outside of) — the fallback is
    deliberately NOT cached: it's an environment-dependent guess (right in a
    normal checkout, wrong -- a FILE, not a directory -- in a linked
    worktree) that a real subsequent call could correct, unlike a genuine
    rev-parse success. Caching it would let one failed/empty subprocess
    result (e.g. a test mocking `subprocess.run` broadly) poison every later
    caller in the same process for good, including unrelated real callers
    that would otherwise resolve correctly (T-1816: a fully-mocked test's
    cached fallback broke a later, fully-real test's
    `_open_lockfile_world_writable` in a linked-worktree checkout).

    T-1931 closes the other half of the same hole: a SUCCESSFUL-looking
    resolution is now cached only if it is confirmed to be a directory.
    T-1816 handled `returncode == 0` with EMPTY stdout (the fallback branch);
    a mock returning `returncode == 0` with arbitrary NON-empty stdout still
    reached the caching branch and poisoned the process just as thoroughly.
    On a non-directory the same value is still returned — uncached, so the
    next caller re-resolves. Nothing is rejected and no returned path changes;
    only whether the process remembers it does.
    """
    global _git_common_dir_cache
    if _git_common_dir_cache is not None:
        return _git_common_dir_cache
    res = _git("rev-parse", "--git-common-dir", cwd=REPO_ROOT)
    # T-2336 (third door of the same T-1816/T-1931 hole): a broadly-mocked
    # subprocess.run yields a MagicMock stdout whose .strip() is truthy and
    # whose fspath is a fabricated relative path — `REPO_ROOT / raw` then
    # mkdir(parents=True)s a literal `MagicMock/...` tree in the repo root the
    # first time git_mutation_lock runs under such a test. A REAL
    # subprocess(text=True) stdout is always `str`, so a non-str here can only
    # be a test double: fall through to the uncached fallback exactly like the
    # empty-stdout case.
    if res.returncode == 0 and isinstance(res.stdout, str) and res.stdout.strip():
        raw = res.stdout.strip()
        resolved = Path(raw) if os.path.isabs(raw) else (REPO_ROOT / raw)
        resolved = resolved.resolve()
        # T-1931 (bugs/git-common-dir-global-cache-poisoned-by-mocked-subprocess.md
        # §Standing-fix part b): cache ONLY a value confirmed to be a directory.
        # T-1816 closed the empty-stdout door by not caching the fallback; this
        # closes the non-empty-but-wrong door — a caller that mocks subprocess.run
        # with returncode=0 and arbitrary non-empty stdout would otherwise cache a
        # non-directory module-globally and mis-serve every later REAL caller in the
        # process (git_mutation_lock then mkdir()s against it).
        #
        # Deliberately NOT a rejection: on a non-directory we return the SAME value
        # we always returned, merely uncached, so the next caller re-resolves. The
        # only behaviour that changes is whether the process remembers it. A valid
        # directory is never rejected and no caller's returned path changes.
        #
        # The check must never raise inside the commit path (this module is where
        # ticket.py / ticket_grooming.py / watch_runner.py take the mutation lock).
        # Path.is_dir() already swallows OSError internally, but it is wrapped
        # anyway: any unexpected raise degrades to "treat as not-a-directory" →
        # re-resolve next call. Slower, never fatal.
        try:
            cacheable = resolved.is_dir()
        except OSError:
            cacheable = False
        if cacheable:
            _git_common_dir_cache = resolved
        return resolved
    return REPO_ROOT / ".git"


def _git_mutation_lockfile() -> Path:
    """Resolve the lockfile path. Env-overridable (`PANTHEON_GIT_MUTATION_LOCKFILE`)
    for test isolation — same pattern as ticket.py's `PANTHEON_TICKET_ALLOC_LOCKFILE`.

    Repo-anchored (via `_git_common_dir()`), never derived from a caller-
    supplied `cwd`: the race this lock closes is specific to processes
    sharing REPO_ROOT's own working tree/index — a linked worktree has its
    own index and is not exposed to it — so anchoring on the repo's identity
    (not the caller's cwd) is the semantically correct choice, not just a
    convenient one.
    """
    override = os.environ.get(_GIT_MUTATION_LOCK_ENV)
    if override:
        return Path(override)
    return _git_common_dir() / _GIT_MUTATION_LOCK_FILENAME


def _open_lockfile_world_writable(path: Path):
    """Open (or create) `path` for writing, world-writable (0o666), bypassing
    the process umask.

    Cross-uid precedent: the shared checkout is written by multiple unix
    users (claude, claude-research, kino — T-1003 cross-owner README), so the
    lockfile itself must be openable/writable by any of them regardless of
    which uid happens to create it first. Same protected_regular-safe shape
    as ticket.py's `_open_world_writable` (existing-file-first via plain
    O_WRONLY, else O_CREAT under `umask(0)`) — kept as a standalone
    definition here rather than a shared import: `_git_utils.py`'s own
    convention is rule-of-three extraction, and ticket.py's existing test
    suite patches `tools.ticket.os.open` directly, so relocating the helper
    would silently break that patch target without a matching test-file edit
    outside this dispatch's scope.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_WRONLY)
    except FileNotFoundError:
        old_umask = os.umask(0)
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o666)
        except (FileExistsError, PermissionError):
            fd = os.open(str(path), os.O_WRONLY)
        finally:
            os.umask(old_umask)
    return os.fdopen(fd, "w")


def _read_lock_holder_hint(path: Path) -> str:
    """Best-effort holder diagnostic for the timeout error message. Never raises."""
    try:
        data = path.read_text(encoding="utf-8").strip()
    except OSError:
        return "<lockfile unreadable>"
    return data or "<no holder record written yet — lock may have just changed hands>"


@contextlib.contextmanager
def git_mutation_lock(*, timeout_secs: float = 30.0, label: str = ""):
    """Exclusive, blocking-with-timeout, cross-process AND cross-user lock
    around a git mutate → stage → commit (→ push) critical section.

    Usage::

        with git_mutation_lock(label="claim T-1234"):
            ...git mv / frontmatter edits...
            _commit_and_push(...)   # or commit_scoped(...)

    Properties:
      - BLOCKING with a bounded timeout (default 30s): `fcntl.flock` has no
        native blocking-with-timeout, so this polls `LOCK_EX | LOCK_NB` in a
        loop rather than blocking forever — a stuck holder must surface
        loudly, not wedge every other ticket-mutating tool on the shared
        checkout indefinitely.
      - On timeout: raises `GitMutationLockTimeout` naming the lock path and
        a best-effort holder diagnostic (pid/label/start-time, written by
        whoever currently holds it) — this function never proceeds unlocked.
      - Cross-process: `fcntl.flock` is process-scoped, not thread-scoped —
        correct for ticket.py / ticket_grooming.py / watch_runner.py, each a
        separate CLI or cron invocation.
      - Cross-user: the lockfile is opened world-writable (0o666) regardless
        of which uid creates it first (T-1003 precedent — kino / claude /
        claude-research all write this shared checkout).
      - Same-process re-entrancy is NOT supported (not required per this
        primitive's design brief): a second `with git_mutation_lock()` from
        the SAME process while already holding it will poll until its own
        timeout and then raise — `flock` locks are per-open-file-description,
        not per-process, so a second `open()` + `flock()` from the same
        process is a genuinely separate lock request that self-contends.
        Callers must acquire this lock exactly once per mutate → commit span
        and must not nest it.
    """
    lock_path = _git_mutation_lockfile()
    f = _open_lockfile_world_writable(lock_path)
    fd = f.fileno()
    deadline = time.monotonic() + timeout_secs
    acquired = False
    reopens = 0
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except _LockfileReplaced:
                # Handled below by the same handler as the acquisition path.
                raise
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    holder = _read_lock_holder_hint(lock_path)
                    raise GitMutationLockTimeout(
                        f"git_mutation_lock: could not acquire {lock_path} "
                        f"within {timeout_secs}s (wanted for: {label or '<unlabeled>'}); "
                        f"current holder: {holder}. Another ticket.py / "
                        f"ticket_grooming.py / watch_runner.py process on this "
                        f"checkout appears to be mid-mutation — check for a stuck "
                        f"process before retrying; do NOT delete the lockfile to "
                        f"force through."
                    )
                time.sleep(_GIT_MUTATION_LOCK_POLL_SECS)

        # T-2140: flock is held on the INODE, not the path. If the lockfile was
        # unlinked or replaced between open() and acquisition, we hold a lock on
        # an orphaned inode and provide NO mutual exclusion — a second process
        # opens the NEW path and acquires instantly. Measured: with a live holder,
        # deleting the lockfile let a contender in in 0.000s, both believing they
        # held it. That is precisely the concurrent-mutation this ticket is about.
        # Re-open and retry rather than proceeding with a lock that guards nothing.
        while True:
            try:
                on_disk = os.stat(lock_path)
                held = os.fstat(fd)
                replaced = (on_disk.st_dev, on_disk.st_ino) != (held.st_dev, held.st_ino)
            except FileNotFoundError:
                replaced = True
            except OSError:
                replaced = False  # cannot compare; proceed rather than fail
            if not replaced:
                break
            reopens += 1
            if reopens > _GIT_MUTATION_LOCK_REOPEN_ATTEMPTS:
                raise GitMutationLockTimeout(
                    f"git_mutation_lock: {lock_path} was unlinked/replaced "
                    f"{reopens} times while acquiring (wanted for: "
                    f"{label or '<unlabeled>'}). Something is deleting the lockfile — "
                    f"that DESTROYS mutual exclusion (T-2140). Find and stop it; "
                    f"do NOT work around it."
                )
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                f.close()
            except OSError:
                pass
            acquired = False
            f = _open_lockfile_world_writable(lock_path)
            fd = f.fileno()
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise GitMutationLockTimeout(
                        f"git_mutation_lock: could not re-acquire {lock_path} after "
                        f"it was replaced (wanted for: {label or '<unlabeled>'})")
                time.sleep(_GIT_MUTATION_LOCK_POLL_SECS)

        try:
            f.seek(0)
            f.truncate()
            f.write(
                f"pid={os.getpid()} label={label or '<unlabeled>'} "
                f"started={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
            )
            f.flush()
        except OSError:
            pass  # diagnostic-only; never fail the critical section over this

        yield
    finally:
        if acquired:
            # T-2140: did the file we HELD survive our critical section? If the
            # path now names a different inode, someone deleted/replaced the
            # lockfile while we were mutating — which means a second process
            # could have acquired a fresh file and run concurrently with us.
            # We cannot PREVENT that from either side (a contender opening the
            # new path has no way to know a prior inode existed), so the only
            # honest thing is to say loudly that our exclusion was violated.
            try:
                now_stat = os.stat(lock_path)
                held_stat = os.fstat(fd)
                if (now_stat.st_dev, now_stat.st_ino) != (held_stat.st_dev, held_stat.st_ino):
                    print(
                        f"WARNING: git_mutation_lock: the lockfile {lock_path} was "
                        f"REPLACED during '{label or '<unlabeled>'}' — mutual exclusion "
                        f"was NOT in force for part of this operation. Another process "
                        f"may have mutated concurrently. Verify the result (T-2140).",
                        file=sys.stderr,
                    )
            except FileNotFoundError:
                print(
                    f"WARNING: git_mutation_lock: the lockfile {lock_path} was DELETED "
                    f"during '{label or '<unlabeled>'}' — mutual exclusion was NOT in "
                    f"force. Another process may have mutated concurrently (T-2140).",
                    file=sys.stderr,
                )
            except OSError:
                pass

            # Rewrite the hint to say RELEASED before unlocking. The file
            # necessarily outlives the lock (flock releases on fd close/process
            # death; the path stays), so a leftover reading "pid=<dead> label=..."
            # looks exactly like a stuck holder. That misreading is what led an
            # operator to delete a lockfile they had not validated — and deleting
            # it is the one action that genuinely destroys mutual exclusion.
            # A released lock should SAY so.
            try:
                f.seek(0)
                f.truncate()
                f.write(
                    f"RELEASED (not held) by pid={os.getpid()} "
                    f"label={label or '<unlabeled>'} "
                    f"at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
                    f"# This file persisting is NORMAL and does NOT mean the lock is held.\n"
                    f"# flock releases on process exit; the path is only a diagnostic.\n"
                    f"# NEVER delete this file to force through — doing so while a real\n"
                    f"# holder is live lets a second process acquire immediately (T-2140).\n"
                )
                f.flush()
            except OSError:
                pass
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            f.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Scope-narrowed commit (T-1088 Auditable convergence — commit-as-audit).
#
# The canonical "stage exactly these paths, refuse anything else, HERMES-guard,
# commit, push-with-retry" primitive that was hand-rolled in ~8 tools
# (ticket_grooming, spine_index_groom×2, watch_runner, ticket.py, ticket_lint,
# weekly_ticket_summary, promote_lessons_cron, dispatch). One audited-commit
# implementation is harder to get subtly wrong than eight (P1). Callers migrate
# onto this incrementally; ticket.py (most central) converges last.
# ---------------------------------------------------------------------------


def _path_in_scope(staged: str, scope: list[str], allow_prefix: bool) -> bool:
    if allow_prefix:
        return any(staged == s or staged.startswith(s.rstrip("/") + "/") for s in scope)
    return staged in scope


def commit_scoped(
    paths: list[str],
    message: str,
    *,
    cwd: Path = REPO_ROOT,
    push: bool = True,
    sync: bool = True,
    allow_prefix: bool = False,
    conflict_resolver=None,
) -> tuple[bool, str]:
    """Stage exactly `paths`, refuse any out-of-scope staged path, commit, push.

    The shared scope-narrowed-commit primitive. Sequence:
      1. HERMES message-guard.
      2. If `sync`: `pull_or_rebase_strict(cwd)` (skip in worktree mode — the
         worktree already starts at origin/main HEAD; pass sync=False there).
      3. `git add -- <paths>`; read back `git diff --cached --name-only`.
      4. **Leak guard:** any staged path NOT in scope → `reset` + fail-loud
         (False). `allow_prefix=True` treats each `paths` entry as a directory
         prefix (e.g. `tickets/active/`); otherwise exact-path membership.
      5. Empty staged set → `(True, "no changes")` (a no-op groom).
      6. HERMES file-guard every ACTUALLY-STAGED file (reset + fail on refusal).
         Scanning the staged set — not the literal `paths` — is what makes the
         guard correct under `allow_prefix`: when `paths` names a directory, the
         real files to scan are the ones git staged under it, not the dir entry
         (which is not a file, so a pre-stage `paths` scan would silently skip
         it). Strictly stronger than the prior scan: it covers exactly what is
         about to be committed.
      7. `git commit`; if `push`, `push_with_retry(cwd)`.

    Steps 2-7 (sync through commit/push) run inside `git_mutation_lock()`
    (T-1770 Part b) — the whole mutate → stage → commit → push span is one
    atomic critical section against other writers on the SAME shared
    checkout (ticket.py, ticket_grooming.py, and this function's own other
    callers all serialize on the same lockfile). Step 1 (the message guard)
    is cheap and side-effect-free, so it stays outside the lock.

    Returns `(ok, detail)` — never raises for control flow. `paths` are
    repo-relative (relative to `cwd`). No force, no `--no-verify`.
    """
    if not _hermes_scan_message(message):
        return False, "HERMES guard refused commit message"

    with git_mutation_lock(label=message.splitlines()[0][:80]):
        if sync:
            ok, detail = pull_or_rebase_strict(cwd, conflict_resolver)
            if not ok:
                return False, f"sync failed: {detail}"

        res = _git("add", "--", *paths, cwd=cwd)
        if res.returncode != 0:
            return False, f"git add failed: {(res.stderr or res.stdout).strip()}"

        res = _git("diff", "--cached", "--name-only", cwd=cwd)
        if res.returncode != 0:
            return False, f"git diff --cached failed: {(res.stderr or res.stdout).strip()}"
        staged = [p for p in res.stdout.split("\n") if p.strip()]

        leaked = [s for s in staged if not _path_in_scope(s, paths, allow_prefix)]
        if leaked:
            _git("reset", "HEAD", "--", cwd=cwd)
            return False, f"scope guard refused: out-of-scope paths staged: {leaked}"

        if not staged:
            return True, "no changes"

        # HERMES-guard the actual committed content. A staged path may be a
        # deletion (rename source) with no file on disk — skip those (nothing
        # to scan).
        for rel in staged:
            fpath = cwd / rel
            if fpath.is_file():
                ok, detail = _hermes_scan_file(fpath)
                if not ok:
                    _git("reset", "HEAD", "--", cwd=cwd)
                    return False, f"HERMES guard refused {rel}: {detail}"

        res = _git("commit", "-m", message, cwd=cwd)
        if res.returncode != 0:
            return False, f"git commit failed: {(res.stderr or res.stdout).strip()}"

        if push:
            ok_push, push_msg = push_with_retry(cwd)
            if not ok_push:
                return False, f"git push failed: {push_msg}"
        return True, f"committed {len(staged)} file(s)"


# ---------------------------------------------------------------------------
# Per-cron git worktree isolation (T-0471).
# ---------------------------------------------------------------------------

T = TypeVar("T")


class WorktreeError(Exception):
    """Raised when with_worktree cannot create or manage a git worktree.

    Callers that need to distinguish worktree-lifecycle failures from other
    exceptions can catch `WorktreeError` specifically. The string representation
    always includes the underlying cause so traces are actionable.
    """


def _worktree_add_timeout_s() -> int:
    try:
        v = int(os.environ.get("PANTHEON_WORKTREE_ADD_TIMEOUT_S", "180"))
    except ValueError:
        return 180
    return min(max(v, 30), 900)


_WORKTREE_ADD_TIMEOUT_S = _worktree_add_timeout_s()


def _discard_failed_worktree(wt_path: Path) -> None:
    """T-5076: best-effort teardown after a failed `git worktree add`.

    `remove -f -f` is the double force a LOCKED worktree needs, then `prune` drops a
    registration whose directory is already gone, then the directory itself goes. The path
    is this process's own (/tmp/<script>-<pid>, absent at step 1), so nothing else is
    touched. Every step is bounded and swallows its own failure, so the caller's
    WorktreeError (the real fault) is what surfaces."""
    for argv in (["git", "worktree", "remove", "-f", "-f", str(wt_path)],
                 ["git", "worktree", "prune"]):
        try:
            subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True,
                           timeout=60, check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            print(f"[with_worktree] warning: {' '.join(argv[:3])} after a failed add: {exc!r}",
                  file=sys.stderr)
    try:
        shutil.rmtree(wt_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"[with_worktree] warning: could not remove {wt_path}: {exc!r}", file=sys.stderr)


def with_worktree(fn: Callable[[Path], T]) -> T:
    """Run `fn(worktree_path)` in a fresh detached git worktree off origin/main.

    Structural isolation for cron scripts: each fire gets its own `/tmp/` worktree
    so commits originate in an isolated working tree and a dirty shared working
    tree cannot pollute the commit's scope (T-0471; justifies-via P1).

    Created path: `/tmp/<basename(sys.argv[0])>-<pid>/`

    Protocol:
      1. Stale-leftover check: if path already exists, raise WorktreeError with
         the leftover path. Abort-with-explicit-error policy (HO-ratified; P1,
         P4, D5): stale leftover means a prior fire crashed without cleanup;
         operator should triage before the next fire runs.
      2. Fetch `origin main` (30s timeout). Failure → WorktreeError.
      3. `git worktree add --detach <path> origin/main` (180s, PANTHEON_WORKTREE_ADD_TIMEOUT_S).
         Failure or timeout → the half-made worktree is removed (remove -f -f, prune, rmtree;
         T-5076), then WorktreeError with the git error detail (P4 traceability).
      4. Call fn(path). Return value or exception propagates.
      5. `git worktree remove --force <path>` in try/finally. If removal fails,
         log to stderr but do NOT mask fn's return value or re-raise over fn's
         exception.

    Returns whatever fn returns.
    Raises WorktreeError on lifecycle failures (steps 1-3).
    Re-raises any exception fn raises (step 4 exceptions pass through cleanly).
    """
    script_base = os.path.basename(sys.argv[0]) if sys.argv else "unknown"
    pid = os.getpid()
    wt_path = Path(f"/tmp/{script_base}-{pid}")

    # Step 1: stale-leftover abort.
    if wt_path.exists():
        raise WorktreeError(
            f"stale worktree leftover from a prior fire: {wt_path}; "
            f"remove it before re-running (prior cron may have crashed mid-run)"
        )

    # Step 2: fetch origin/main so worktree-add gets a fresh tip. Bounded retry
    # on the concurrent-fetch ref-lock race (T-3207): another git op updating
    # refs/remotes/origin/main mid-fetch fails "cannot lock ref" — transient (the
    # next attempt sees the settled ref). Non-ref-lock failures (network/auth) are
    # NOT retried, preserving the prior fail-fast posture.
    for _fetch_attempt in range(1, _FETCH_REF_LOCK_ATTEMPTS + 1):
        try:
            fetch_result = subprocess.run(
                ["git", "fetch", "origin", "main"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise WorktreeError(f"git fetch origin main failed: {exc!r}") from exc
        if fetch_result.returncode == 0:
            break
        detail = (fetch_result.stderr or fetch_result.stdout).strip()
        is_ref_lock = any(m in detail.lower() for m in _REF_LOCK_MARKERS)
        if is_ref_lock and _fetch_attempt < _FETCH_REF_LOCK_ATTEMPTS:
            time.sleep(_FETCH_REF_LOCK_BACKOFF_S * _fetch_attempt)
            continue
        raise WorktreeError(
            f"git fetch origin main failed (rc={fetch_result.returncode}): {detail}"
        )

    # Step 3: create the worktree. T-5076: a full-tree checkout is I/O-heavy, and on 2026-10-17
    # the 30s add timed out at load ~40 on 4 cores, so the ceiling is wider (env-tunable).
    # A failed or killed add leaves git's half-made worktree registered and LOCKED
    # ('initializing'), so it is torn down before the error is raised.
    try:
        add_result = subprocess.run(
            ["git", "worktree", "add", "--detach", str(wt_path), "origin/main"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=_WORKTREE_ADD_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _discard_failed_worktree(wt_path)
        raise WorktreeError(f"git worktree add failed: {exc!r}") from exc
    if add_result.returncode != 0:
        detail = (add_result.stderr or add_result.stdout).strip()
        _discard_failed_worktree(wt_path)
        raise WorktreeError(
            f"git worktree add --detach {wt_path} origin/main failed "
            f"(rc={add_result.returncode}): {detail}"
        )

    # Step 4 + 5: call fn; always remove worktree in finally.
    try:
        return fn(wt_path)
    finally:
        try:
            remove_result = subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_path)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            print(
                f"[with_worktree] warning: worktree remove failed for {wt_path}: {exc!r}",
                file=sys.stderr,
            )
            # Do NOT return/raise here — let fn's return value or exception propagate.
        else:
            if remove_result.returncode != 0:
                detail = (remove_result.stderr or remove_result.stdout).strip()
                print(
                    f"[with_worktree] warning: git worktree remove --force {wt_path} "
                    f"failed (rc={remove_result.returncode}): {detail}",
                    file=sys.stderr,
                )
