#!/usr/bin/env python3
"""cited_ticket_lint — find T-NNNN ids cited in commit messages that did not exist yet.

WHY (T-2300, sibling of T-2272's cited_hash_lint). A commit message that cites a
ticket is a pointer the reader is meant to follow. When it points at an id that had
not landed when the commit was written, the pointer is unverifiable and race-exposed.
It may later land on unrelated work; even when the intended ticket lands later and
captures the id, the citation still did not resolve in the tree where it was made.

⚠️ THE OBVIOUS CHECK IS THE VACUOUS ONE, AND THAT IS THE WHOLE POINT OF THIS TOOL.
"Does T-NNNN exist?" passes on exactly the defect it is meant to catch. Ticket ids
are allocated densely and monotonically, so a forward reference to the NEXT id is
allocated within hours — by the time any checker looks, it resolves to a real file
and reads as fine. The citation is never broken; it is silently WRONG, which is
strictly worse, because nothing downstream ever re-opens it.

Live instance that motivated the shape (2026-07-29): a resolve note said "FOLLOW-UP
FILED: T-2356" written moments before filing. The allocator returned T-2357. T-2356
had gone to specialist 11:10:13Z for an unrelated watch-report taxonomy. An existence
check passes. Only "did it exist AT THIS COMMIT" fails.

So the question this asks is ancestry, not existence:

    OK          — the ticket file was present in the tree at the citing commit,
                  or was added BY it (filing a ticket cites its own new id).
    FORWARD     — not present then, but exists NOW. The citation had no target in
                  that tree; audit whether the id later captured unrelated work or
                  the intended ticket merely landed afterwards. Fails either way.
    UNALLOCATED — no such ticket anywhere, then or now. A typo or an invented id.
                  Loud rather than silent, but still a broken pointer. Fails.

⚠️ WHY ANCESTRY AND NOT TIMESTAMPS. This repo's pre-push hook rebases, which
rewrites commit dates' relationship to content. Author/commit dates can therefore
say a ticket predates a commit that in fact does not contain it. Position in
`git rev-list` is the only ordering that survives a rebase, so that is what is used.

⚠️ SCOPE IS COMMIT MESSAGES ONLY, deliberately. The same defect occurs in ticket
PROSE, but a ticket file has no single commit to resolve "at that moment" against —
answering it needs line-level blame, and a citation to a later follow-up is often
legitimate there. Guessing would make this noisy, and a noisy gate gets ignored,
which is the failure mode T-2272 documents at length. Filed separately instead.

Usage:
    python3 tools/cited_ticket_lint.py                 # sweep recent origin/main
    python3 tools/cited_ticket_lint.py --limit 500     # deeper history
    python3 tools/cited_ticket_lint.py --range A..B    # an explicit range
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

# Ticket ids are `T-` plus 4+ digits. Distinctive enough that prose false-positives
# are rare, but still guarded against being embedded in a longer alphanumeric run
# so things like `XT-2300` or `T-23001abc` do not register.
_TICKET = re.compile(r"(?<![0-9A-Za-z])(T-\d{4,})(?![0-9A-Za-z])")

TICKET_DIRS = ("active", "inbox", "done")

# Conventional scratch ids. These are not citations — they are placeholders people
# type when they need an id-shaped string for a throwaway (the real instance that
# put this here: "verified end-to-end on a throwaway T-9999 stage"). Reported as
# INFO, never a failure.
#
# Suppressing them is safe in the direction that matters: the exemption is only
# ever reached when the id resolves to NO ticket. If a real T-9999 is ever
# allocated it lands in `birth` and is classified on ancestry like anything else,
# so this can never hide a genuine forward reference — only an "id that does not
# exist", which for these strings is the intended meaning.
PLACEHOLDER_IDS = frozenset({"T-0000", "T-9999"})

# ⚠️ COMMIT MESSAGES ARE IMMUTABLE, SO HISTORICAL FINDINGS CAN NEVER BE FIXED.
# Without a baseline this sweep would fail every single week on the same
# pre-existing citations, forever, and a weekly red that nobody can action is the
# alarm fatigue that gets a gate muted — the failure this whole ticket family
# exists to avoid. The baseline records findings that were already in history when
# the lint was introduced; they are reported as INFO so the record stays visible,
# and only NEW forward references fail.
#
# It is deliberately keyed on (sha, id): a rewritten or new commit does not inherit
# an exemption, so this can suppress only the exact citations that were audited.
BASELINE_PATH = REPO / "tools" / "cited_ticket_lint_baseline.txt"


def load_baseline(path=None) -> set[tuple[str, str]]:
    """{(short_sha, ticket_id)} already accepted as unfixable history."""
    path = pathlib.Path(path or BASELINE_PATH)
    out: set[tuple[str, str]] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            out.add((parts[0], parts[1]))
    return out

STATUS_OK, STATUS_FORWARD, STATUS_UNALLOCATED = "OK", "FORWARD", "UNALLOCATED"
STATUS_PLACEHOLDER = "PLACEHOLDER"
STATUS_BASELINED = "BASELINED"


def _git(*args, cwd=REPO) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True)


def cited_ids(text: str) -> list[str]:
    """Distinct ticket ids appearing in the text, in order of first appearance."""
    out, seen = [], set()
    for m in _TICKET.finditer(text):
        tok = m.group(1)
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def commit_order(ref: str = "origin/main", cwd=REPO) -> dict[str, int]:
    """sha -> position in `git rev-list <ref>`, newest first (0 == tip).

    One call, then every ancestry question is an O(1) integer comparison. A SMALLER
    index means NEWER. See the module docstring for why this is used instead of
    commit timestamps.
    """
    r = _git("rev-list", "--topo-order", ref, cwd=cwd)
    if r.returncode != 0:
        return {}
    return {sha: i for i, sha in enumerate(r.stdout.split())}


def ticket_birth(ref: str = "origin/main", cwd=REPO) -> dict[str, str]:
    """ticket id -> sha of the commit that first ADDED its file.

    A ticket file is added once, then re-added on every state move (inbox -> active
    -> done shows up as an addition at the new path). We want the EARLIEST such
    commit: that is when the id became real. `git log` walks newest-first, so the
    last addition seen for an id is the earliest one.
    """
    r = _git("log", "--diff-filter=A", "--name-only", "--format=%H", ref,
             "--", "tickets/", cwd=cwd)
    if r.returncode != 0:
        return {}
    birth: dict[str, str] = {}
    sha = ""
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.fullmatch(r"[0-9a-f]{40}", line):
            sha = line
            continue
        m = re.search(r"tickets/[^/]+/(T-\d{4,})\.md$", line)
        if m:
            birth[m.group(1)] = sha   # overwritten as we walk back -> earliest wins
    return birth


def classify(cited: str, citing_sha: str, order: dict[str, int],
             birth: dict[str, str]) -> str:
    """OK / FORWARD / UNALLOCATED for one (commit, cited-id) pair."""
    born = birth.get(cited)
    if born is None:
        return STATUS_PLACEHOLDER if cited in PLACEHOLDER_IDS else STATUS_UNALLOCATED
    pos_born, pos_citing = order.get(born), order.get(citing_sha)
    if pos_born is None or pos_citing is None:
        return STATUS_OK          # off-ref commit: cannot order it, do not cry wolf
    # Smaller index == newer. The ticket must NOT be newer than the commit citing it.
    # Equal is fine and common: `tickets: file T-2357` adds the file it names.
    return STATUS_FORWARD if pos_born < pos_citing else STATUS_OK


def sweep(*, ref: str = "origin/main", limit: int | None = 200,
          rev_range: str | None = None, cwd=REPO, baseline=None) -> dict:
    order = commit_order(ref, cwd=cwd)
    birth = ticket_birth(ref, cwd=cwd)
    baseline = load_baseline() if baseline is None else baseline

    log_args = ["log", "--format=%H%x00%B%x1e"]
    if rev_range:
        log_args.append(rev_range)
    else:
        log_args.append(ref)
        if limit:
            log_args.append(f"-{limit}")
    r = _git(*log_args, cwd=cwd)

    findings = {STATUS_FORWARD: [], STATUS_UNALLOCATED: [], STATUS_PLACEHOLDER: [],
                STATUS_BASELINED: []}
    scanned = 0
    for record in r.stdout.split("\x1e"):
        record = record.strip("\n")
        if not record or "\x00" not in record:
            continue
        sha, message = record.split("\x00", 1)
        sha = sha.strip()
        scanned += 1
        for cid in cited_ids(message):
            st = classify(cid, sha, order, birth)
            if st != STATUS_OK:
                subject = message.strip().splitlines()[0][:72] if message.strip() else ""
                if (sha[:9], cid) in baseline and st != STATUS_PLACEHOLDER:
                    st = STATUS_BASELINED
                findings[st].append((sha[:9], cid, subject))
    findings["commits_scanned"] = scanned
    findings["tickets_known"] = len(birth)
    return findings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ref", default="origin/main")
    ap.add_argument("--limit", type=int, default=200,
                    help="how many commits back to scan (default 200)")
    ap.add_argument("--range", dest="rev_range", default=None,
                    help="explicit rev range, e.g. A..B (overrides --limit)")
    ap.add_argument("--no-fetch", action="store_true")
    args = ap.parse_args(argv)

    if not args.no_fetch:
        # Ancestry must be measured against the REMOTE's view; a stale local ref
        # can call a perfectly good citation a forward reference.
        _git("fetch", "-q", "origin")

    f = sweep(ref=args.ref, limit=args.limit, rev_range=args.rev_range)
    fwd, unalloc = f[STATUS_FORWARD], f[STATUS_UNALLOCATED]
    placeholders, baselined = f[STATUS_PLACEHOLDER], f[STATUS_BASELINED]

    print(f"[cited_ticket_lint] {f['commits_scanned']} commit(s), "
          f"{f['tickets_known']} ticket ids known; "
          f"{len(fwd)} FORWARD, {len(unalloc)} UNALLOCATED, "
          f"{len(baselined)} baselined, {len(placeholders)} placeholder (info)")
    if baselined:
        print("  (baselined — real defects in IMMUTABLE commit messages, kept "
              "visible but not failing; see cited_ticket_lint_baseline.txt)")
        for sha, cid, subject in baselined:
            print(f"    {sha}  {cid}  {subject}")

    if unalloc:
        print("  ⚠️ UNALLOCATED — no such ticket, then or now (typo / invented id):")
        for sha, cid, subject in unalloc:
            print(f"    {sha}  {cid}  {subject}")
    if fwd:
        print("  ⛔ FORWARD REFERENCE — the id was absent from this ref at the "
              "citing commit but exists now; audit whether it captured unrelated "
              "work or the intended ticket landed later:")
        for sha, cid, subject in fwd:
            print(f"    {sha}  {cid}  {subject}")
        print("  Prevent by filing first and citing the id the allocator RETURNED. "
              "For an already-published hit: verify ancestry and intended referent, "
              "repair mutable records if wrong, then baseline only that exact "
              "(sha, id) pair with its audit rationale.")
    if not fwd and not unalloc:
        print("  passing — no unbaselined forward or unallocated citations")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
