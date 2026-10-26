"""tools/handoff_conservation.py — detect silent detail loss across a HANDOFF rewrite (T-2638).

Problem this solves: a HANDOFF.md rewrite is an end-to-end LLM rewrite of a
cumulative artifact. That shape is exactly the ACE "context collapse" failure
mode (AppWorld: an 18,282-token artifact collapsed to 122 tokens in one
rewrite) and its T-1771 sibling here (66% of a HANDOFF truncated, a whole
durable-pointer section silently gone). Normal handoff cycles legitimately
drop *some* detail every time (resolved tickets, stale pointers) — a naive
per-entity "did anything disappear?" diff would fire on every single cycle
and be a dead alert from day one. This module targets the CLIFF / WHOLE-
SECTION-LOSS shape specifically, not routine resolution churn.

Design (decided in T-2533, implemented here):
  1. A deterministic STRUCTURAL FINGERPRINT of a HANDOFF's content — NO LLM —
     over four entity classes: ticket IDs, owner/target references, section
     headings, and fenced code/shell blocks. See compute_fingerprint().
  2. Diff OLD vs NEW fingerprints via four independent checks (any one
     firing raises an alert — see check_conservation()):
       - size-cliff:    new/old character length collapses
       - entity-cliff:  new/old total tracked-entity count collapses
       - section-loss:  most of OLD's headings vanish from NEW (the T-1771
                         signature specifically — a whole section gone even
                         when overall length/entity count looks survivable)
       - open-ticket-loss (T-5347): ticket ids in OLD, absent from NEW, whose
                         ticket file sits in tickets/inbox or tickets/active
                         (exists and is not done). Catches the item-level drop
                         the three cliff checks miss (T-2819 replay: artisan
                         10-02 and 10-13). Done and unknown ids never alert;
                         the alert lists at most OPEN_TICKET_LIST_CAP ids plus
                         a "+N more" tail. The tickets root resolves from this
                         file's location (repo-root/tickets, --tickets-root
                         overrides); a missing or unreadable root skips the
                         check with one stderr note.
  3. ALERT, never block. This is advisory only; every failure mode here
     degrades to "no alert printed", never to a blocked restart.

OLD comes from tools/chronicle.py's existing latest_handoff_text() — the
snapshot the *previous* handoff-and-restart cycle archived. NEW is the
HANDOFF.md about to be archived by *this* cycle. Wired into
handoff_and_restart.sh immediately before its chronicle.py archive call,
while latest_handoff_text() still returns the prior cycle's snapshot (the
archive call below it is what turns the current text into the new latest).
That ordering is what makes OLD/NEW two genuinely consecutive HANDOFF
rewrites, matching the failure mode this exists to catch.

Thresholds are a first-cut, not calibrated against a real handoff-diff
corpus (out of scope for T-2638; alerts are advisory so a mistuned constant
costs one spurious or missed log line, never a broken restart — cheap to be
wrong, trivial to retune, per P7). Named + grouped below for easy retuning;
each alert line prints its ratio + raw counts so real alerts self-calibrate.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Repo root on sys.path so `from tools.chronicle import ...` resolves whether
# this file is run as a script (handoff_and_restart.sh) or imported top-level
# as `handoff_conservation` (test_handoff_conservation.py). Same guard as
# tools/chronicle.py.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import chronicle  # noqa: E402

# --------------------------------------------------------------- thresholds
# All three checks are independent; ANY firing raises an alert (OR'd, not
# AND'd) — see the module docstring for why a per-entity diff is the wrong
# shape. Each check is gated on a minimum OLD size/count so it can't fire off
# a trivial or bootstrap prior snapshot.
SIZE_CLIFF_RATIO = 0.35     # new/old char length below this -> size-cliff
MIN_OLD_CHARS = 500         # skip size-cliff check below this OLD length

ENTITY_CLIFF_RATIO = 0.35   # new/old total tracked-entity count below this -> entity-cliff
MIN_OLD_ENTITIES = 5        # skip entity-cliff check below this OLD entity count

HEADING_LOSS_RATIO = 0.4    # heading-survival fraction below this -> section-loss
MIN_OLD_HEADINGS = 2        # skip section-loss check below this OLD heading count

OPEN_TICKET_LIST_CAP = 20   # open-ticket-loss: list at most this many ids, then "+N more"

# ----------------------------------------------------------- entity extractors
_TICKET_RE = re.compile(r"\bT-\d+\b")
_TICKET_ID_FULL_RE = re.compile(r"^T-\d+$")
# Ticket frontmatter convention already live in this repo (target:/requester:,
# see CLAUDE.md's persona identity section) — also matches owner:/assignee:
# for the same shape used elsewhere.
_OWNER_REF_RE = re.compile(r"(?im)^[ \t]*(?:target|owner|requester|assignee)[ \t]*:[ \t]*(\S+)")
_HEADING_RE = re.compile(r"(?m)^(#{1,6})[ \t]+(.*\S)[ \t]*$")
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
# A trailing parenthetical on a heading (T-2666): real HANDOFF headings carry
# VOLATILE trailing "(...)" groups — running counts, dates, status —
# ("RESOLVED THIS SESSION (10 tickets...)", "GOTCHAS (durable + ...)") that
# change every refresh without the section itself being lost.
_HEADING_TRAILING_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _normalize_code_block(body: str) -> str:
    """Whitespace-normalize a fenced block: strip each line and drop blank
    lines entirely. Trivial reformatting of a preserved command (re-indent,
    an added/removed blank line for readability) must not read as content
    loss — the crux's "stay quiet on normal churn" requirement applies here
    too, not just at the whole-document level."""
    return "\n".join(ln.strip() for ln in body.splitlines() if ln.strip())


def _heading_stem(htext: str) -> str:
    """Normalize a heading to a stable STEM for survival matching (T-2666).

    Strips one trailing parenthetical group so a section whose volatile
    "(...)" suffix changed across a refresh still matches as surviving —
    without this, a legitimate HANDOFF refresh (which updates heading counts/
    dates every time) reads as ~0% section survival and section-loss false-
    fires, the exact dead-alert the check exists to avoid. A genuinely deleted
    section still drops survival below the floor. Whitespace-collapsed; case
    preserved (distinct sections must not merge). Falls back to the full
    collapsed heading if stripping would empty it."""
    collapsed = " ".join(htext.split())
    stem = _HEADING_TRAILING_PAREN_RE.sub("", collapsed).strip()
    return stem or collapsed


def compute_fingerprint(text: str) -> dict[str, set[str]]:
    """Deterministic structural fingerprint over the four load-bearing entity
    classes. Each value is a de-duplicated set of normalized strings —
    position doesn't matter, presence does. Headings are reduced to their
    stable stem (see _heading_stem, T-2666) so volatile trailing parentheticals
    don't read as section loss."""
    owner_refs: set[str] = set()
    for raw in _OWNER_REF_RE.findall(text):
        val = raw.strip().rstrip(".,;:)").lower()
        if val:
            owner_refs.add(val)

    headings: set[str] = set()
    for _hashes, htext in _HEADING_RE.findall(text):
        stem = _heading_stem(htext)
        if stem:
            headings.add(stem)

    code_blocks: set[str] = set()
    for body in _FENCE_RE.findall(text):
        norm = _normalize_code_block(body)
        if norm:
            code_blocks.add(norm)

    return {
        "ticket_ids": set(_TICKET_RE.findall(text)),
        "owner_refs": owner_refs,
        "headings": headings,
        "code_blocks": code_blocks,
    }


# ---------------------------------------------------------- open-ticket lookup
def default_tickets_root() -> Path:
    """repo-root/tickets, resolved from this file's own location."""
    return Path(__file__).resolve().parent.parent / "tickets"


def _ticket_ids_in(d: Path) -> set[str] | None:
    """Ticket ids (T-NNNN.md stems) in one tickets/ subdir; None if unreadable."""
    try:
        with os.scandir(d) as it:
            return {e.name[:-3] for e in it
                    if e.name.endswith(".md") and _TICKET_ID_FULL_RE.match(e.name[:-3])}
    except OSError:
        return None


def open_ticket_ids(tickets_root: Path | str) -> set[str] | None:
    """Ids whose ticket file is in tickets/inbox or tickets/active and NOT in
    tickets/done. None when neither inbox nor active is readable (the caller
    skips the check). Never raises."""
    try:
        root = Path(tickets_root)
        inbox = _ticket_ids_in(root / "inbox")
        active = _ticket_ids_in(root / "active")
        if inbox is None and active is None:
            return None
        done = _ticket_ids_in(root / "done") or set()
        return ((inbox or set()) | (active or set())) - done
    except Exception:  # advisory contract: degrade to "skip", never raise
        return None


def _ticket_sort_key(tid: str) -> tuple[int, str]:
    try:
        return (int(tid[2:]), tid)
    except ValueError:
        return (0, tid)


def _open_ticket_loss(old_ids: set[str], new_ids: set[str],
                      tickets_root: Path | str) -> str | None:
    open_ids = open_ticket_ids(tickets_root)
    if open_ids is None:
        return None
    lost = sorted((old_ids - new_ids) & open_ids, key=_ticket_sort_key)
    if not lost:
        return None
    shown = ", ".join(lost[:OPEN_TICKET_LIST_CAP])
    more = len(lost) - OPEN_TICKET_LIST_CAP
    tail = f" +{more} more" if more > 0 else ""
    return (f"open-ticket-loss: {len(lost)} still-open ticket id(s) in prior "
            f"handoff absent from new (inbox/active): {shown}{tail}")


# ------------------------------------------------------- conservation predicate
def check_conservation(old_text: str, new_text: str,
                       tickets_root: Path | str | None = None) -> list[str]:
    """Compare a prior HANDOFF snapshot (old_text) against the current one
    (new_text). Returns a list of human-readable alert reasons; an empty list
    means quiet — no suspected loss. Each reason embeds its ratio + raw
    counts so a real alert is self-explaining without reading this file.

    tickets_root enables the open-ticket-loss check (T-5347); None skips it,
    keeping the three cliff checks a pure function of the two texts."""
    alerts: list[str] = []

    old_len, new_len = len(old_text), len(new_text)
    if old_len >= MIN_OLD_CHARS:
        ratio = new_len / old_len
        if ratio < SIZE_CLIFF_RATIO:
            alerts.append(
                f"size-cliff: new handoff is {ratio:.0%} of prior length "
                f"({new_len}/{old_len} chars, floor {SIZE_CLIFF_RATIO:.0%})"
            )

    old_fp = compute_fingerprint(old_text)
    new_fp = compute_fingerprint(new_text)

    old_total = sum(len(v) for v in old_fp.values())
    new_total = sum(len(v) for v in new_fp.values())
    if old_total >= MIN_OLD_ENTITIES:
        ratio = new_total / old_total
        if ratio < ENTITY_CLIFF_RATIO:
            alerts.append(
                f"entity-cliff: new handoff retains {ratio:.0%} of prior tracked "
                f"entities ({new_total}/{old_total} across ticket ids/owner refs/"
                f"headings/code blocks, floor {ENTITY_CLIFF_RATIO:.0%})"
            )

    old_headings = old_fp["headings"]
    if len(old_headings) >= MIN_OLD_HEADINGS:
        survived = old_headings & new_fp["headings"]
        ratio = len(survived) / len(old_headings)
        if ratio < HEADING_LOSS_RATIO:
            lost = sorted(old_headings - survived)
            alerts.append(
                f"section-loss: only {ratio:.0%} of prior section headings survived "
                f"({len(survived)}/{len(old_headings)}, floor {HEADING_LOSS_RATIO:.0%}); "
                f"lost: {lost}"
            )

    if tickets_root is not None:
        try:
            reason = _open_ticket_loss(old_fp["ticket_ids"], new_fp["ticket_ids"],
                                       tickets_root)
        except Exception:  # advisory contract: never raise
            reason = None
        if reason:
            alerts.append(reason)

    return alerts


# --------------------------------------------------------------------------- CLI
def _log(msg: str) -> None:
    print(f"[handoff-conservation] {msg}", file=sys.stderr)


def _cli_check(args: argparse.Namespace) -> int:
    """check a live HANDOFF against its latest Chronicle snapshot; print any
    alerts (non-fatal — always safe to ignore the return code, the caller
    must never block a restart on this)."""
    try:
        new_text = Path(args.handoff_file).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        _log(f"cannot read handoff {args.handoff_file} (non-fatal): {exc}")
        return 0

    agent = args.agent or chronicle.agent_from_handoff_path(args.handoff_file)
    if not agent:
        _log(f"cannot resolve agent from {args.handoff_file} and no --agent given; skipping")
        return 0

    root = Path(args.root) if args.root else None
    old_text = chronicle.latest_handoff_text(agent, root=root)
    if old_text is None:
        _log(f"no prior chronicle snapshot for {agent}; nothing to compare, skipping")
        return 0

    tickets_root = Path(args.tickets_root) if args.tickets_root else default_tickets_root()
    if open_ticket_ids(tickets_root) is None:
        _log(f"tickets root {tickets_root} missing or unreadable; "
             f"skipping open-ticket-loss check (non-fatal)")
        tickets_root = None

    alerts = check_conservation(old_text, new_text, tickets_root=tickets_root)
    if not alerts:
        return 0
    for reason in alerts:
        _log(f"ALERT: {reason}")
    return 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="HANDOFF conservation check (T-2638): alert (non-fatal) on "
                     "suspected silent detail loss across a HANDOFF rewrite."
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser(
        "check",
        help="compare the current HANDOFF against its latest Chronicle snapshot; "
             "alert (non-fatal) on suspected detail loss",
    )
    c.add_argument("--handoff-file", required=True)
    c.add_argument("--agent", default=None, help="override (else derived from handoff path)")
    c.add_argument("--root", default=None, help="chronicle root (default: <state-dir>/chronicle)")
    c.add_argument("--tickets-root", default=None,
                   help="tickets dir for the open-ticket-loss check "
                        "(default: <repo-root>/tickets, from this file's location)")
    c.set_defaults(func=_cli_check)
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
