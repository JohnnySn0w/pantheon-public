#!/usr/bin/env python3
"""tools/spine_groom_cross_repo.py — extend spine-index grooming to Pantheon-adjacent repos (T-0666).

`spine_index_groom.py` grooms pantheon-config's own doc hierarchy. T-0666 (the operator
msg 2058 "yeah" + msg 3853 GO, option-a: owner-run) extends the same AUTOGEN-sentinel
grooming to the claude-owned adjacent repos, run by THIS user (orchestrator) — clean perms,
no /home/claude ACL puncture, no shared-location migration.

Reuses spine_index_groom's handlers + sentinel machinery wholesale; this module only
adds (1) the per-repo doc-dir → handler map, (2) a one-time sentinel-region bootstrap
(the adjacent repos' READMEs have no AUTOGEN region yet, so the generic handler would
skip them), and (3) a per-repo groom loop that commits/pushes to each repo's own remote.

  python3 tools/spine_groom_cross_repo.py            # dry-run (default): preview per repo
  python3 tools/spine_groom_cross_repo.py --apply    # bootstrap + groom + commit + push
  python3 tools/spine_groom_cross_repo.py --no-push   # apply locally, skip push

Manual / owner-timer driven — NOT the pantheon-config cron. A disabled orchestrator user
timer is wired separately; enable after a verified first run.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import _git_utils  # noqa: E402
from tools.spine_index_groom import (  # noqa: E402
    SENTINEL_START,
    SENTINEL_END,
    adr_handler,
    notes_handler,
    write_sentinel_region,
    read_sentinel_region,
)

# Base dir holding the sibling repos (claude home). Override in tests.
DEFAULT_BASE = Path("/home/claude")

# Per-repo doc-dir → handler map. adr/ uses the dated-ADR table handler; every
# other doc tree uses the flat-.md notes handler (same choice pantheon-config makes
# for lessons/rulebook/architecture-shaped dirs).
# (Sibling repos below still keep `adr/` at repo root; pantheon-config's ADR dir
# moved under architecture/ on 2026-09-06, T-3472, ORG-094, but that's a
# pantheon-config-local move — the sibling repos are unaffected.)
#
# echo-prismatic is INTENTIONALLY EXCLUDED from this direct-push config: its `main`
# is a protected branch (required `ci-gate` status check + a full-Rust pre-push smoke
# gate), so a docs-only spine-groom can't direct-push — it would need a PR that runs
# the entire workspace build for a README change. On a paused repo that's
# disproportionate; tracked as a PR-flow follow-up (groom it when a PR is open anyway,
# or via admin-bypass when it's worth it). pantheon-pwa + revenue-project have unprotected
# main → direct-push works. See run_cross_repo's revert-on-push-fail safety net.
CROSS_REPO_DOCS: dict[str, dict[str, object]] = {
    "pantheon-pwa": {"adr": adr_handler},
    "revenue-project": {
        "adr": adr_handler,
        "notes": notes_handler,
        "research": notes_handler,
        "topics": notes_handler,
        "implementations": notes_handler,
    },
}

# Repos whose main is protected (PR + CI required) — excluded from direct-push
# grooming above; documented here so the exclusion is explicit, not forgotten.
PR_FLOW_REPOS: dict[str, str] = {
    "echo-prismatic": "protected main (ci-gate + full-Rust pre-push gate); groom via PR-flow",
}

_BOOTSTRAP_BLOCK = (
    "\n## Index (auto-generated — `tools/spine_groom_cross_repo.py`)\n\n"
    f"{SENTINEL_START}\n{SENTINEL_END}\n"
)


def needs_bootstrap(readme_text: str) -> bool:
    """True if the README has no AUTOGEN sentinel region yet."""
    return read_sentinel_region(readme_text) is None


def bootstrap_text(readme_text: str) -> str:
    """Append a sentinel region to README text (idempotent — no-op if present)."""
    if not needs_bootstrap(readme_text):
        return readme_text
    sep = "" if readme_text.endswith("\n") or readme_text == "" else "\n"
    return readme_text + sep + _BOOTSTRAP_BLOCK


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def _mirror_leg(repo_root: Path) -> str | None:
    """ORG-101 section 2 dual-write leg (T-4705), called ONLY after origin accepted the push.

    The origin push stays a bare `git push` (push.default pushes the current branch
    to its same-name upstream), so the mirror gets the same ref: HEAD:<branch>. A
    detached HEAD has no such branch, so the leg is skipped. Inert (None) until
    repos.yaml marks a writable mirror for this repo's origin. Never raises: origin
    already landed, so nothing here may change the push result.
    """
    try:
        rp = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
        branch = rp.stdout.strip()
        if rp.returncode != 0 or not branch or branch == "HEAD":
            return "mirror leg skipped (no current branch)"
        return _git_utils.mirror_push_after_origin(repo_root, f"HEAD:{branch}")
    except Exception as exc:  # noqa: BLE001 -- best-effort leg; never undo origin
        return f"mirror push failed ({exc!r}); reconciler will heal"


def groom_one_dir(repo_root: Path, subdir: str, handler, *, apply: bool, push: bool) -> str:
    """Bootstrap (if needed) + groom one doc dir. Returns a status line."""
    d = repo_root / subdir
    if not d.is_dir():
        return f"{repo_root.name}/{subdir}: MISSING dir — skipped"
    readme = d / "README.md"
    rows = handler(d)

    if not apply:
        boot = "create README+region" if not readme.exists() else (
            "add region" if needs_bootstrap(readme.read_text(encoding="utf-8")) else "region present")
        return f"{repo_root.name}/{subdir}: would {boot}, would write {len(rows)} row(s)"

    # Apply: ensure README + sentinel region exist, then write the rows.
    text = readme.read_text(encoding="utf-8") if readme.exists() else f"# {subdir}\n"
    new_text = bootstrap_text(text)
    if new_text != text:
        readme.write_text(new_text, encoding="utf-8")
    changed = write_sentinel_region(readme, rows)
    if not changed and new_text == text:
        return f"{repo_root.name}/{subdir}: noop"
    return f"{repo_root.name}/{subdir}: wrote {len(rows)} row(s)"


def run_cross_repo(base: Path, *, apply: bool, push: bool,
                   docs: dict[str, dict[str, object]] | None = None) -> list[str]:
    """Groom each configured repo's doc dirs. Commits+pushes per repo on apply."""
    docs = docs if docs is not None else CROSS_REPO_DOCS
    results: list[str] = []
    for repo_name, dirmap in docs.items():
        repo_root = base / repo_name
        if not (repo_root / ".git").exists():
            results.append(f"{repo_name}: NOT a git repo at {repo_root} — skipped")
            continue
        touched: list[str] = []
        for subdir, handler in dirmap.items():
            line = groom_one_dir(repo_root, subdir, handler, apply=apply, push=push)
            results.append(line)
            if apply and "wrote" in line:
                touched.append(str((repo_root / subdir / "README.md").relative_to(repo_root)))
        if apply and touched:
            pre_sha = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
            _git(["add", *touched], repo_root)
            msg = f"spine(index): cross-repo groom — {len(touched)} doc dir(s) (T-0666)"
            cp = _git(["commit", "-m", msg], repo_root)
            if cp.returncode != 0:
                results.append(f"{repo_name}: commit FAILED: {cp.stderr.strip()}")
                continue
            results.append(f"{repo_name}: commit ok")
            if push:
                pp = _git(["push"], repo_root)
                if pp.returncode == 0:
                    results.append(f"{repo_name}: push ok")
                    note = _mirror_leg(repo_root)
                    if note is not None:
                        results.append(f"{repo_name}: {note}")
                else:
                    # Never leave a divergent local main: revert to the exact
                    # pre-commit SHA (a protected branch or transient push failure
                    # must not strand an unpushable commit on the working tree).
                    _git(["reset", "--hard", pre_sha], repo_root)
                    results.append(
                        f"{repo_name}: push FAILED — reverted local commit "
                        f"(protected branch? needs PR-flow): {pp.stderr.strip().splitlines()[-1] if pp.stderr.strip() else ''}")
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write + commit (default: dry-run)")
    ap.add_argument("--no-push", action="store_true", help="apply locally, skip push")
    ap.add_argument("--base", default=str(DEFAULT_BASE), help="base dir holding the repos")
    args = ap.parse_args(argv)

    results = run_cross_repo(Path(args.base), apply=args.apply, push=not args.no_push)
    for line in results:
        print(line)
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"\nspine_groom_cross_repo: {len(results)} action(s) [{mode}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
