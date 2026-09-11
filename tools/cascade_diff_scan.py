"""GHA-side diff-walk for the ORG-013 cascade-on-PR-merge trigger (T-3289).

Closes the T-3288 gap: `ticket.py resolve` fires the ORG-013 cascade-unblock
pass via `_do_transition`'s cascade_callback hook, but a ticket resolved via
GitHub PR-squash-merge (or an admin-merge) never invokes `ticket.py resolve`
at all, so the cascade never fires and dependants keep stale `depends_on:`
refs indefinitely. `.github/workflows/cascade-on-merge.yml` pipes a push's
rename-only `git log` output through this module to find every ticket that
moved into `done/` in the push, and invokes `python3 tools/ticket.py cascade
--tid <T-NNNN>` (the T-3289 subcommand) for each.

Public surface:
    extract_resolved_tids — pure function: rename-only name-status text ->
                             ordered, deduped list of resolved T-NNNN tids.
    run_cascade_for_tids  — invoke `ticket.py cascade --tid <tid>` for each
                             tid, independently (one failure never blocks
                             another); returns [(tid, returncode), ...].
    main                  — CLI entry point; reads name-status text from
                             stdin; exits 0 if no tids found or all cascade
                             invocations succeeded, 1 if any failed (after
                             running every one).

Mirrors `tools/adr_scoping_lint.py` / `tools/hermes_diff_scan.py`'s shape: a
pure function over already-computed git-output text, no git subprocess of
its own for the IDENTIFY step, fully unit-testable without a real repo. The
INVOKE step (run_cascade_for_tids) does shell out, and is exercised via a
mocked subprocess.run in tests — the real end-to-end path is covered by
`tools/test_ticket_cascade.py`'s direct cmd_cascade() tests instead, since a
real `ticket.py cascade` invocation is structurally refused by
`_assert_cli_checkout_allowed` from any `agent/*`-branch worktree.

Expected input shape — the output of:
    git log --diff-filter=R --name-status --format="" <before>..<head>
`--format=""` suppresses commit-message bodies so only raw name-status lines
remain (avoids a commit message that happens to contain an R-prefixed line
being misparsed as a real rename record). Empirically verified against this
repo's own history (git 2.43.0): a `ticket.py resolve`-driven `git mv`
already produces a same-line rename entry (e.g. `R094\ttickets/active/
T-3262.md\ttickets/done/T-3262.md`) WITHOUT needing an explicit `-M` flag —
rename detection is on by default for this repo/git version, matching the
`--no-renames`-disables-coalescing comment already in `tools/ticket.py`'s
`_commit_and_push`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent

# Matches one `git log --diff-filter=R --name-status --format=""` rename
# record, e.g.:  R094\ttickets/inbox/T-1234.md\ttickets/done/T-1234.md
_RENAME_LINE_RE = re.compile(r"^R\d+\t(\S+)\t(\S+)$")
# Old-side path must be a ticket currently in inbox/ or active/.
_RESOLVABLE_PATH_RE = re.compile(r"^tickets/(?:inbox|active)/(T-\d+)\.md$")
# New-side path must land in done/.
_DONE_PATH_RE = re.compile(r"^tickets/done/(T-\d+)\.md$")


def extract_resolved_tids(name_status_text: str) -> list[str]:
    """Parse rename-only name-status text; return the ordered, deduped list
    of tids for every `tickets/{inbox,active}/T-NNNN.md` -> `tickets/done/
    T-NNNN.md` rename found.

    Anything else is skipped: a non-rename line, a rename that doesn't land
    in done/ (e.g. inbox/ -> active/, a claim), a rename of a non-ticket
    file, or a rename whose old-side and new-side tids disagree (should
    never happen via `git mv`, but fail-closed rather than trust one side
    blindly).
    """
    seen: dict[str, None] = {}
    for raw_line in name_status_text.splitlines():
        line = raw_line.rstrip("\n")
        if not line:
            continue
        m = _RENAME_LINE_RE.match(line)
        if not m:
            continue
        old_path, new_path = m.group(1), m.group(2)
        old_m = _RESOLVABLE_PATH_RE.match(old_path)
        new_m = _DONE_PATH_RE.match(new_path)
        if not old_m or not new_m:
            continue
        if old_m.group(1) != new_m.group(1):
            continue  # disagreeing tids across the rename — fail closed
        seen.setdefault(old_m.group(1), None)
    return list(seen.keys())


def run_cascade_for_tids(
    tids: list[str], *, repo_root: Path = _REPO_ROOT
) -> list[tuple[str, int]]:
    """Invoke `python3 tools/ticket.py cascade --tid <tid>` for each tid, in
    order. Independent per-tid: a non-zero exit on one tid does not stop the
    rest from running (T-3289 scope: "a fail on one does not block
    another"). Returns [(tid, returncode), ...] in input order.
    """
    results: list[tuple[str, int]] = []
    for tid in tids:
        proc = subprocess.run(
            [sys.executable, str(repo_root / "tools" / "ticket.py"), "cascade", "--tid", tid],
            cwd=str(repo_root),
        )
        results.append((tid, proc.returncode))
    return results


def main() -> int:
    """CLI entry point.

    Usage:
        git log --diff-filter=R --name-status --format="" <before>..HEAD \
            | python3 tools/cascade_diff_scan.py

    Exits 0 if no resolved tids are found in the input, or every cascade
    invocation for a found tid succeeded. Exits 1 if any cascade invocation
    failed — but only AFTER running every tid found (P4: a genuine cascade
    bug must surface in CI, not be silently swallowed by an earlier sibling
    tid's success).
    """
    name_status_text = sys.stdin.read()
    tids = extract_resolved_tids(name_status_text)
    if not tids:
        print(
            "cascade-on-merge: no ticket resolves (inbox/active -> done "
            "renames) in this push; nothing to do"
        )
        return 0

    print(f"cascade-on-merge: found {len(tids)} resolved tid(s): {', '.join(tids)}")
    results = run_cascade_for_tids(tids)

    failed = [tid for tid, rc in results if rc != 0]
    for tid, rc in results:
        status = "ok" if rc == 0 else f"FAILED (exit {rc})"
        print(f"  cascade --tid {tid}: {status}")

    if failed:
        print(
            f"cascade-on-merge: {len(failed)} of {len(tids)} cascade "
            f"invocation(s) failed: {', '.join(failed)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
