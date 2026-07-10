#!/usr/bin/env python3
"""Read-only PRE-reboot safety check: is the fleet's work all committed + pushed?

The "safe" half of the operator's safe+verifiable-reboots ask (Telegram 3036) — the
sibling of `fleet_health.py` (the post-reboot "did everyone come back?" sweep).
This makes repeatable the assertion every graceful-restart handoff makes by hand
("NOTHING UP IN THE AIR IS UNRECORDED. Everything committed/pushed."): it sweeps
every agent's git clone and reports anything that would be lost or invisible
across a reboot.

STRICTLY READ-ONLY: `git status --porcelain` + a local rev-list count. It never
commits, pushes, fetches, or mutates — it reports + sets an exit code. Whether a
reboot should be GATED on a clean result is policy (the orchestration half of
T-1049, the operator's steer); this tool just tells the truth.

Per repo:
  * DIRTY  (FAIL) — uncommitted/untracked changes: real work in the air a reboot
    won't lose from disk but that isn't in git history. The thing to commit first.
  * AHEAD  (WARN) — local commits not on origin/main (per the last-known remote
    ref; no fetch). Survives the reboot in local .git, but invisible to peers
    until pushed — relevant for a coordinated fleet reboot.
  * CLEAN  (OK).

Cross-account via NOPASSWD sudo. An unreadable repo is WARN, never a crash.

    python3 tools/reboot_safety.py           # sweep the whole fleet
    python3 tools/reboot_safety.py --json
Exit: 0 = every repo clean+pushed; 1 = a DIRTY repo; 64 = usage.
"""

from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys

# (agent, unix_user, repo_path). The main clones each agent commits from — NOT
# detached worktrees (those are tooling-internal). Verified present 2026-06-10.
REPOS = [
    ("recovery", "claude-fallback", "/home/claude-fallback/pantheon-config"),
    ("orchestrator", "claude", "/home/claude/pantheon-config"),
    ("specialist", "claude-research", "/home/claude-research/pantheon-config"),
    ("codex", "codex", "/home/codex/pantheon-config"),
    ("codex", "codex", "/home/codex/codex-config"),
]

OK, WARN, FAIL = "OK", "WARN", "FAIL"


def _run(user: str, argv_str: str, *, timeout: int = 20):
    """Run a shell snippet as `user` (no sudo if it's the current user). Returns
    (rc, stdout, ran); ran=False if it couldn't execute (sudo/timeout/OS)."""
    if user == getpass.getuser():
        argv = ["bash", "-lc", argv_str]
    else:
        argv = ["sudo", "-n", "-u", user, "bash", "-lc", argv_str]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return (None, "", False)
    return (r.returncode, r.stdout, True)


def check_repo(agent: str, user: str, path: str, *, run=_run) -> dict:
    """Classify one repo: CLEAN / DIRTY (uncommitted) / AHEAD (unpushed)."""
    rc, out, ran = run(user, f"git -C {path} status --porcelain")
    if not ran or rc != 0:
        return {"agent": agent, "repo": path, "status": WARN,
                "detail": "could not read repo (sudo/missing/timeout)"}
    dirty = [ln for ln in out.splitlines() if ln.strip()]
    if dirty:
        return {"agent": agent, "repo": path, "status": FAIL,
                "detail": f"{len(dirty)} uncommitted/untracked change(s)"}

    # committed-but-unpushed (local-known; NO fetch — a reboot keeps these on
    # disk, but peers can't see them until pushed).
    rc, out, ran = run(
        user,
        f"git -C {path} rev-list --count @{{upstream}}..HEAD 2>/dev/null "
        f"|| git -C {path} rev-list --count origin/main..HEAD 2>/dev/null "
        f"|| echo '?'")
    ahead = out.strip()
    if ahead == "?" or ahead == "":
        return {"agent": agent, "repo": path, "status": WARN,
                "detail": "clean working tree; unpushed count unknown (no upstream)"}
    if ahead != "0":
        return {"agent": agent, "repo": path, "status": WARN,
                "detail": f"clean, but {ahead} local commit(s) not on origin"}
    return {"agent": agent, "repo": path, "status": OK,
            "detail": "clean + pushed"}


def sweep(*, run=_run) -> list[dict]:
    return [check_repo(a, u, p, run=run) for (a, u, p) in REPOS]


def render(results: list[dict]) -> str:
    glyph = {OK: "✓", WARN: "!", FAIL: "✗"}
    lines = []
    for r in results:
        lines.append(f"[{glyph[r['status']]}] {r['agent']:6} {r['repo']}")
        if r["status"] != OK:
            lines.append(f"        {r['detail']}")
    dirty = [r["repo"] for r in results if r["status"] == FAIL]
    ahead = [r["repo"] for r in results if r["status"] == WARN and "not on origin" in r["detail"]]
    lines.append("")
    if dirty:
        lines.append(f"NOT SAFE TO REBOOT — uncommitted work in: {', '.join(dirty)}")
    elif ahead:
        lines.append(f"committed but UNPUSHED in: {', '.join(ahead)} "
                     "(survives reboot locally; push for peers to see)")
    else:
        lines.append("SAFE — every fleet repo is clean + pushed ✓")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only pre-reboot fleet repo-cleanliness check (T-1049).")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    results = sweep()
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(render(results))
    return 1 if any(r["status"] == FAIL for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
