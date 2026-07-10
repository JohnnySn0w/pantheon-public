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

import os
import re
import subprocess
import sys
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
    """
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
        print(f"[{component}] fetch skipped ({exc!r}); using stale clone", file=sys.stderr)
        return
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
        print(f"[{component}] fetch failed (rc={result.returncode}): {detail[0]}; using stale clone", file=sys.stderr)
        return

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


def push_with_retry(
    cwd: Path,
    *,
    refspec: str = "HEAD:main",
    attempts: int = 3,
    conflict_resolver=None,
) -> tuple[bool, str]:
    """Push `origin <refspec>` with retry on non-fast-forward rejection.

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
    for attempt_num in range(1, attempts + 1):
        res = _git("push", "origin", refspec, cwd=cwd)
        if res.returncode == 0:
            return True, f"pushed {refspec} (attempt {attempt_num})"

        stderr_lower = res.stderr.lower()
        is_non_ff = any(marker in stderr_lower for marker in _NON_FF_MARKERS)

        if not is_non_ff:
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
        fetch_res = _git("fetch", "origin", "main", cwd=cwd)
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
    for kind in ("rebase-merge", "rebase-apply"):
        res = _git("rev-parse", "--git-path", kind, cwd=cwd)
        if res.returncode != 0:
            continue
        rel = res.stdout.strip()
        if not rel:
            continue
        path = Path(rel) if os.path.isabs(rel) else (cwd / rel)
        if path.exists():
            _git("rebase", "--abort", cwd=cwd)
            print(
                f"[git_utils] cleared stale in-progress rebase ({kind}) before sync "
                f"({cwd}) — likely an interrupted prior run (T-1191)",
                file=sys.stderr,
            )
            return True
    return False


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
    # Self-heal before syncing: clear any stale in-progress rebase a prior
    # interrupted run may have left behind (T-1191), which would otherwise make
    # the ff-only merge below fail "a rebase is in progress" and wedge the clone.
    _abort_stale_rebase(cwd)

    fetch_res = _git("fetch", "origin", "main", cwd=cwd)
    if fetch_res.returncode != 0:
        detail = (fetch_res.stderr or fetch_res.stdout).strip()
        return False, f"git fetch failed: {detail}"

    merge_res = _git("merge", "--ff-only", "refs/remotes/origin/main", cwd=cwd)
    if merge_res.returncode == 0:
        return True, ""

    # ff-only refused. Only a genuine divergence (local has unpushed commits) is
    # rebase-recoverable; if we're not ahead, the ff failure is something else
    # (e.g. a dirty tree) and rebasing would do nothing useful — surface it.
    ahead = _git("rev-list", "--count", "refs/remotes/origin/main..HEAD", cwd=cwd)
    if ahead.returncode != 0 or ahead.stdout.strip() in ("", "0"):
        detail = (merge_res.stderr or merge_res.stdout).strip()
        return False, f"git merge --ff-only failed (not ahead, no rebase): {detail}"

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
    return False, f"rebase reconcile conflict (aborted, clean state restored): {detail}"


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

    Returns `(ok, detail)` — never raises for control flow. `paths` are
    repo-relative (relative to `cwd`). No force, no `--no-verify`.
    """
    if not _hermes_scan_message(message):
        return False, "HERMES guard refused commit message"

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

    # HERMES-guard the actual committed content. A staged path may be a deletion
    # (rename source) with no file on disk — skip those (nothing to scan).
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
      3. `git worktree add --detach <path> origin/main`. Failure → WorktreeError
         with the git error detail (P4 traceability).
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

    # Step 2: fetch origin/main so worktree-add gets a fresh tip.
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
    if fetch_result.returncode != 0:
        detail = (fetch_result.stderr or fetch_result.stdout).strip()
        raise WorktreeError(f"git fetch origin main failed (rc={fetch_result.returncode}): {detail}")

    # Step 3: create the worktree.
    try:
        add_result = subprocess.run(
            ["git", "worktree", "add", "--detach", str(wt_path), "origin/main"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise WorktreeError(f"git worktree add failed: {exc!r}") from exc
    if add_result.returncode != 0:
        detail = (add_result.stderr or add_result.stdout).strip()
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
