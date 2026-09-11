#!/usr/bin/env python3
"""Stale git-worktree sweeper (T-1324 part b / 2026-06-25 VPS disk-full).

The recurring disk consumer: agents spawn throwaway build worktrees (e.g.
/tmp/echo-T-1312) during arcs, each carrying a 10-20G Rust `target/`. If the arc
closes without removing them, they accumulate until the root FS fills — which on
2026-06-25 hit 100% and silently failed cargo/git writes across every account. The
disk monitor (T-1324 part a) alerts after the fact; this sweeps the cause.

It is CONSERVATIVE by construction — it removes a worktree only when ALL hold:
  1. it is NOT the repo's main/canonical checkout;
  2. its path is a throwaway location (default: under /tmp);
  3. its tracked working tree is CLEAN (no uncommitted changes — `git status
     --porcelain` empty over tracked files; build artifacts under a gitignored
     `target/` don't count);
  4. its HEAD is an ANCESTOR of origin/main (i.e. fully merged) OR its branch is
     gone — so removing it can never lose unmerged work.

T-2616 (2026-08-06): a worktree younger than PANTHEON_WORKTREE_MIN_AGE_MIN minutes
(default 60) is EXEMPT even when 3+4 both hold — a freshly pre-staged agent
worktree (HO dispatch discipline: `git worktree add` on `agent/<task>` before an
implementer writes any code) is empty, clean, and sits on origin/main HEAD,
otherwise indistinguishable from an abandoned merged worktree. See
`_worktree_age_minutes` for the age signal and its fail-safe-toward-sweeping
direction when unmeasurable.

Cross-account: each repo is operated as its owning unix user via `sudo -n`. Dry-run
by DEFAULT (lists what would go + reclaimable size); pass --clean to actually remove
via `git worktree remove --force` (force only discards the gitignored build cache,
never tracked/unmerged work — guard 3+4 already proved those safe). Exit 0 always.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# (owner_unix_user, main_checkout_path) repos that spawn throwaway worktrees.
REPOS = [
    ("claude", "/home/claude/echo-prismatic"),
    ("claude", "/home/claude/pantheon-config"),
    ("claude-research", "/home/claude-research/pantheon-config"),
    ("claude-fallback", "/home/claude-fallback/pantheon-config"),
]

# A worktree is "throwaway" (eligible) only if its path starts with one of these.
THROWAWAY_PREFIXES = tuple(
    os.environ.get("PANTHEON_WORKTREE_THROWAWAY_PREFIXES", "/tmp/").split(",")
)

# Don't alert/sweep worktrees smaller than this (GiB) in the size summary — tiny
# ones aren't worth the churn (still removed if eligible; this is just reporting).
MIN_REPORT_GIB = float(os.environ.get("PANTHEON_WORKTREE_MIN_REPORT_GIB", "1"))

# T-2616: a worktree younger than this (minutes) is EXEMPT from removal even when
# guards 3+4 both pass — see `_worktree_age_minutes` / `classify`.
MIN_AGE_MIN = float(os.environ.get("PANTHEON_WORKTREE_MIN_AGE_MIN", "60"))


def _run(user: str, args: list[str], *, cwd: str | None = None, timeout: int = 60):
    """Run a command as `user` (self → direct, sibling → sudo -n). Returns CompletedProcess."""
    me = getpass.getuser()
    cmd = args if user == me else ["sudo", "-n", "-u", user, *args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)


def _git(user: str, repo: str, *args: str, timeout: int = 60):
    return _run(user, ["git", "-C", repo, *args], timeout=timeout)


def _du_gib(user: str, path: str) -> float:
    try:
        res = _run(user, ["du", "-sb", path], timeout=120)
        # du prints a valid grand total even when it exits NONZERO from an
        # unreadable subdir; use the total rather than discarding it for rc!=0
        # (same du semantics fixed in disk_usage_monitor per T-3441). This is
        # report-only (size never gates removal), so the effect is just a more
        # honest reclaim-size summary when a worktree has an unreadable subdir.
        if res.stdout.strip():
            return int(res.stdout.split()[0]) / (1024 ** 3)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return 0.0


def list_worktrees(user: str, repo: str) -> list[dict]:
    """Parse `git worktree list --porcelain` into dicts: {path, head, branch, bare, main}."""
    res = _git(user, repo, "worktree", "list", "--porcelain")
    if res.returncode != 0:
        return []
    trees, cur = [], {}
    for line in res.stdout.splitlines():
        if not line.strip():
            if cur:
                trees.append(cur)
                cur = {}
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur = {"path": val, "head": None, "branch": None, "detached": False}
        elif key == "HEAD":
            cur["head"] = val
        elif key == "branch":
            cur["branch"] = val
        elif key == "detached":
            cur["detached"] = True
    if cur:
        trees.append(cur)
    # First entry is the main checkout.
    for i, t in enumerate(trees):
        t["main"] = (i == 0) or (os.path.realpath(t["path"]) == os.path.realpath(repo))
    return trees


def _worktree_age_minutes(user: str, path: str) -> float | None:
    """Age of the worktree at `path`, in minutes, or None if unmeasurable.

    Uses the birth time of `<path>/.git` — the gitlink file `git worktree add`
    writes once and never rewrites afterward. That rules out two signals the
    T-2616 incident showed are unstable: the `.git/worktrees/<name>/` admin-dir
    mtime (bumped by a sweep run itself) and its `index` file (rewritten by
    every `git add`/`commit`). Birth time isn't exposed by Python's os.stat()
    on Linux, so this shells out via the existing cross-account `_run()` path,
    same as the git status/merge-base calls below.

    Returns None — "unknown", never "young" — when birth time can't be read
    (stat failure) or the filesystem/kernel doesn't expose it (`%W` == 0).
    Callers must treat None as falling through to the pre-T-2616 behavior.
    """
    res = _run(user, ["stat", "-c", "%W", f"{path}/.git"])
    if res.returncode != 0:
        return None
    try:
        birth = int(res.stdout.strip())
    except ValueError:
        return None
    if birth <= 0:
        return None
    return (time.time() - birth) / 60.0


def classify(user: str, repo: str, wt: dict) -> tuple[bool, str]:
    """Return (removable, reason). Conservative — removable only if every guard passes."""
    path = wt["path"]
    if wt.get("main"):
        return False, "main checkout"
    if not path.startswith(THROWAWAY_PREFIXES):
        return False, f"not a throwaway path ({path})"
    # T-2616 age guard: a freshly pre-staged agent worktree is empty+clean with
    # HEAD == origin/main, satisfying guards 3+4 below — indistinguishable from
    # an abandoned merged worktree without age as a discriminator. Exempt only
    # when age is KNOWN and below threshold; unmeasurable age falls through to
    # guards 3+4 unchanged (never grants exemption on missing data), but warns
    # loudly — a silently-unmeasurable signal would make this guard a no-op.
    age_min = _worktree_age_minutes(user, path)
    if age_min is None:
        print(
            f"[WARN] {path}: worktree age unmeasurable (stat -c %W of .git "
            f"failed or unsupported) — NOT exempting from sweep guards; if "
            f"this fires broadly the T-2616 age guard is a silent no-op.",
            file=sys.stderr, flush=True,
        )
    elif age_min < MIN_AGE_MIN:
        return False, f"too young ({age_min:.1f}m < {MIN_AGE_MIN:.0f}m threshold)"
    # Guard 3: tracked working tree must be clean (run git IN the worktree path).
    st = _run(user, ["git", "-C", path, "status", "--porcelain"])
    if st.returncode != 0:
        return False, f"status failed (rc={st.returncode})"
    if st.stdout.strip():
        return False, "dirty (uncommitted tracked changes)"
    # Guard 4: HEAD merged into origin/main (ancestor) OR branch gone.
    head = wt.get("head")
    if not head:
        return False, "no HEAD"
    anc = _run(user, ["git", "-C", path, "merge-base", "--is-ancestor", head, "origin/main"])
    if anc.returncode == 0:
        return True, "merged (HEAD is ancestor of origin/main)"
    # Not an ancestor → has commits not in main. Only safe if the branch is GONE
    # (deleted) AND those commits are still unreachable-from-main → we do NOT remove
    # (could lose work). Conservative: keep it, surface for human review.
    return False, "UNMERGED (HEAD not in origin/main) — keep for review"


def heal_stale_gc_log(user: str, repo: str, *, clean: bool) -> str | None:
    """Clear a stale .git/gc.log that blocks all future auto-gc (T-1691).

    A worktree dying mid-gc leaves a corrupt HEAD; git's failed gc writes
    .git/gc.log ('bad object … / failed to run repack') which then BLOCKS every
    subsequent auto-gc until removed — silent repo-maintenance degradation. This
    heals it, but ONLY when the underlying cause is proven resolved:
      1. `git worktree prune` has run (the sweep does this before calling us);
      2. `git fsck --connectivity-only` PASSES — so we never clear a log that
         reflects REAL ongoing corruption (that must stay loud for a human).
    Returns a status string when it acts/would-act, else None. Best-effort:
    any probe error → None (leave the log; a backstop must not mask problems)."""
    gc_log = f"{repo}/.git/gc.log"
    if _run(user, ["test", "-f", gc_log]).returncode != 0:
        return None  # no stale log — nothing to heal
    # Gate: fsck must pass (connectivity-only is fast; --no-progress keeps output clean).
    fsck = _git(user, repo, "fsck", "--connectivity-only", "--no-progress", timeout=120)
    if fsck.returncode != 0:
        return (f"[gc.log KEPT] {repo}: fsck FAILED (rc={fsck.returncode}) — "
                f"real corruption, leaving gc.log for a human")
    if not clean:
        return f"[gc.log would-clear] {repo}: stale (fsck clean) — pass --clean to remove"
    rm = _run(user, ["rm", "-f", gc_log])
    if rm.returncode != 0:
        return f"[gc.log FAILED] {repo}: rm rc={rm.returncode} {rm.stderr.strip()[:80]}"
    return f"[gc.log cleared] {repo}: stale log removed (fsck clean); auto-gc can resume"


def sweep(*, clean: bool) -> int:
    """Scan all repos; report (and with --clean, remove) eligible stale worktrees."""
    eligible, reclaim_gib = [], 0.0
    for user, repo in REPOS:
        # list_worktrees runs `git -C repo` as the owner; a missing repo → rc!=0 → [].
        for wt in list_worktrees(user, repo):
            removable, reason = classify(user, repo, wt)
            tag = "REMOVABLE" if removable else "keep"
            if removable:
                sz = _du_gib(user, wt["path"])
                reclaim_gib += sz
                eligible.append((user, repo, wt["path"], sz))
                print(f"[{tag}] {wt['path']} ({sz:.1f}G) — {reason}", flush=True)
            else:
                # Only print kept non-main worktrees (skip noise from the main checkout).
                if not wt.get("main"):
                    print(f"[{tag}] {wt['path']} — {reason}", flush=True)
    if not eligible:
        print("[stale_worktree_sweeper] no stale worktrees found.", flush=True)
        return 0
    print(f"[stale_worktree_sweeper] {len(eligible)} stale worktree(s), ~{reclaim_gib:.1f}G reclaimable.", flush=True)
    if not clean:
        print("[stale_worktree_sweeper] dry-run (default) — pass --clean to remove.", flush=True)
        return len(eligible)
    removed = 0
    for user, repo, path, sz in eligible:
        res = _git(user, repo, "worktree", "remove", "--force", path)
        if res.returncode == 0:
            removed += 1
            print(f"[removed] {path} (~{sz:.1f}G)", flush=True)
        else:
            print(f"[FAILED] {path}: rc={res.returncode} {res.stderr.strip()[:80]}", flush=True)
    print(f"[stale_worktree_sweeper] removed {removed}/{len(eligible)}; pruning refs.", flush=True)
    for user, repo in REPOS:
        _git(user, repo, "worktree", "prune")
    return len(eligible)


def sweep_gc_logs(*, clean: bool) -> None:
    """Heal stale gc.logs across all repos (T-1691). Runs every sweep, after the
    worktree prune, independent of whether any worktree was eligible — a stale
    gc.log can persist long after its worktree is gone. Best-effort per repo."""
    for user, repo in REPOS:
        _git(user, repo, "worktree", "prune")  # ensure cause is pruned before healing
        try:
            msg = heal_stale_gc_log(user, repo, clean=clean)
        except (OSError, subprocess.SubprocessError) as exc:
            msg = f"[gc.log probe error] {repo}: {exc!r} (non-fatal)"
        if msg:
            print(msg, flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--clean", action="store_true", help="actually remove (default: dry-run report)")
    args = p.parse_args(argv)
    sweep(clean=args.clean)
    sweep_gc_logs(clean=args.clean)  # T-1691: heal gc-blocking stale logs too
    return 0  # Exit 0 always: a backstop sweeper must not fail its own timer.


if __name__ == "__main__":
    raise SystemExit(main())
