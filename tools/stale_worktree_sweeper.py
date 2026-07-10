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
        if res.returncode == 0 and res.stdout.strip():
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


def classify(user: str, repo: str, wt: dict) -> tuple[bool, str]:
    """Return (removable, reason). Conservative — removable only if every guard passes."""
    path = wt["path"]
    if wt.get("main"):
        return False, "main checkout"
    if not path.startswith(THROWAWAY_PREFIXES):
        return False, f"not a throwaway path ({path})"
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--clean", action="store_true", help="actually remove (default: dry-run report)")
    args = p.parse_args(argv)
    sweep(clean=args.clean)
    return 0  # Exit 0 always: a backstop sweeper must not fail its own timer.


if __name__ == "__main__":
    raise SystemExit(main())
