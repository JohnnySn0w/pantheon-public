#!/usr/bin/env python3
"""Reject a commit message citing a ticket that does not exist yet.

Invoked from `.githooks/commit-msg`, which is the single commit-msg hook git
runs and which already carries the HERMES byte-sequence gate. This lives here
as an importable module rather than as a second hook script because git runs
exactly ONE commit-msg hook: dropping a second file into .githooks/ would not
chain, it would be ignored, and the check would look installed while doing
nothing.

T-2404, promoting T-2300 from weekly DETECTION to commit-time PREVENTION. The
weekly lint's first real scheduled fire found two new temporal forward
references, and a commit message is immutable — every finding it reports is by
construction unfixable, which is why that lint needs a growing baseline at all
(see cited_ticket_lint_baseline.txt, and T-2401 for the same shape one file
over). The only place this defect can actually be FIXED is before the commit
object exists. pve2 being offline also means CI is not a dependable delivery
surface for the check, so it belongs in the local hook.

WHAT IT REJECTS: a citation of a real-looking id (T- plus 4+ digits) for which
no ticket file exists in the tree this commit WOULD create.

WHAT IT MUST NOT REJECT, and each of these is a real case rather than a
hypothetical:
  - A ticket filed IN THE SAME COMMIT. `ticket.py file` writes the ticket and
    commits it with a message naming its own id, so a hook that consulted only
    HEAD would reject the very workflow that allocates ids correctly. This reads
    the INDEX (git ls-files --cached), which is the would-be tree.
  - A ticket in ANY state directory. Tickets move inbox -> active -> done, and
    the id is the same throughout; scanning only one directory would reject
    every citation of finished work.
  - The T-0000 / T-9999 placeholders, per the policy the weekly lint already
    holds: these are id-shaped scratch strings, not citations.
  - Ids embedded in a longer alphanumeric run (XT-2300, T-23001abc), which are
    not citations at all.

FAIL-OPEN on its own errors. A hook that cannot run must not block a commit:
the failure mode of this check being absent is one bad citation that the weekly
lint still catches, while the failure mode of it wedging is an agent that cannot
commit anything at all.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

# Same pattern and placeholder policy as tools/cited_ticket_lint.py. Deliberately
# duplicated rather than imported: this runs as a git hook from an arbitrary cwd
# under an arbitrary interpreter, and a hook that breaks when a sibling module
# moves is a hook that silently stops gating.
_TICKET = re.compile(r"(?<![0-9A-Za-z])(T-\d{4,})(?![0-9A-Za-z])")
PLACEHOLDER_IDS = frozenset({"T-0000", "T-9999"})
_TICKET_PATH = re.compile(r"^tickets/[^/]+/(T-\d{4,})\.md$")


def cited_ids(message: str) -> list[str]:
    """Ordered, de-duplicated ids cited in the message, minus comment lines.

    git's commit template comments (`# ...`) routinely quote branch names and
    file paths; a citation inside one was never written by the author.
    """
    body = "\n".join(l for l in message.splitlines() if not l.lstrip().startswith("#"))
    seen, out = set(), []
    for m in _TICKET.finditer(body):
        tid = m.group(1)
        if tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


def tickets_in_tree(cwd=None) -> set[str]:
    """Every ticket id in the WOULD-BE tree: the index, not HEAD.

    `git ls-files --cached` lists what the commit will contain, so a ticket
    staged in this same commit counts as existing — which is exactly how
    `ticket.py file` works, and the case a HEAD-only check would break.
    """
    r = subprocess.run(["git", "ls-files", "--cached", "--", "tickets/"],
                       capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        return set()
    out = set()
    for line in r.stdout.splitlines():
        m = _TICKET_PATH.match(line.strip())
        if m:
            out.add(m.group(1))
    return out


def find_forward_refs(message: str, known: set[str]) -> list[str]:
    """Cited ids with no ticket in the tree, placeholders excluded.

    ⚠️ An EMPTY `known` set means "this tree carries no ticket registry", not
    "no ticket exists". Those are different claims and only the second licenses
    rejecting anything — "cannot tell" must never stand in for "no". Without
    this guard the gate is fail-CLOSED exactly where it is least wanted: this
    hook is meant to propagate fleet-wide, and in any repo without a tickets/
    directory every commit message citing a ticket id would be rejected, with
    guidance telling the author to file a ticket into a registry that is not
    there. The degraded state must be silent, not obstructive.
    """
    if not known:
        return []
    return [t for t in cited_ids(message)
            if t not in known and t not in PLACEHOLDER_IDS]


def _guidance(missing: list[str]) -> str:
    ids = ", ".join(missing)
    return (
        f"\n⛔ forward reference in commit message: {ids}\n"
        f"   No ticket file for these exists in the tree this commit would create.\n"
        f"\n"
        f"   A commit message is IMMUTABLE. Once this lands, the citation can never\n"
        f"   be corrected — it can only be added to a baseline as a permanent known\n"
        f"   defect (tools/cited_ticket_lint_baseline.txt). That is why this is a\n"
        f"   gate and not a warning.\n"
        f"\n"
        f"   FIX: file the ticket FIRST, then cite the id the allocator RETURNED.\n"
        f"     python3 tools/ticket.py file --title '...' --requester <you> \\\n"
        f"       --target <them> --ask '...'\n"
        f"   Filing in THIS commit is fine — a ticket staged alongside the message\n"
        f"   counts as existing, so `git add` it and re-commit.\n"
        f"\n"
        f"   Do not invent the next id by counting: concurrent agents allocate from\n"
        f"   the same range, and the id you guessed may belong to someone else's\n"
        f"   ticket by the time you push.\n"
        f"\n"
        f"   Placeholders T-0000 / T-9999 are exempt if you need an id-shaped string.\n"
    )


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return 0  # not invoked as a commit-msg hook; nothing to check
    try:
        message = pathlib.Path(argv[0]).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0  # fail-open: cannot read the message, do not block the commit
    try:
        missing = find_forward_refs(message, tickets_in_tree())
    except Exception:  # noqa: BLE001 — a broken gate must never wedge committing
        return 0
    if not missing:
        return 0
    sys.stderr.write(_guidance(missing))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
