#!/usr/bin/env python3
"""tools/policy_outflow_sync.py — ORG-012 OUTFLOW: keep briefs pointed at changed policy.

T-0834 (the operator 2026-06-05). The ORG-012 knowledge loop has two halves:
  - INFLOW  (T-0781, tools/knowledge_sweep.py): detect corpus changes, file a sweep.
  - OUTFLOW (this): ensure newly-ratified P&Ds / ADRs / RFCs / SOPs actually get
    APPLIED — i.e. that agents' briefs/memories reflect them — otherwise ratified
    knowledge sits unread.

DESIGN (the deliberate choices, per the operator's requirements "keep briefing MINIMAL,
pointers are GOOD — link to the authoritative artifact, do not inline it"):

  - It does NOT auto-edit persona-brief PROSE. Auto-rewriting hand-curated D2 docs
    is both un-minimal and risky. Instead the application path is two static,
    cheap pieces: (1) each brief carries a one-time POINTER BLOCK to the
    authoritative indexes (rulebook/, architecture/adr/README.md, sop/*/README.md) — those
    indexes are themselves auto-maintained (spine_index_groom), so they always
    reflect the current set; (2) THIS job maintains the *recency signal* the
    static pointers lack — a generated digest of what was ratified/changed
    RECENTLY, so an agent notices new knowledge instead of merely having a link
    to everything.

  - OUTPUT: a GENERATED, pointer-only digest at notes/recent-policy-changes.md —
    for each policy artifact (rulebook/ + architecture/adr/ + sop/) touched in the last
    --days window: a link, its title, the change date, and the commit subject.
    No artifact bodies are inlined (the operator's "do not inline"). Briefs point at
    this one file for "what's new."

  - DEFAULT-SAFE (T-0261, sop/standard/auto-commit-cron-default-safe-contract.md):
    regenerates every run so the window ages correctly, but COMMITS only when the
    digest content actually changed; quiet no-op otherwise. No state file needed —
    the digest file is its own state.

  - The digest lives OUTSIDE the watched dirs (notes/, not rulebook/architecture/adr/sop) so
    regenerating it never self-triggers the change it reports.

  - CADENCE: daily timer now (the operator's "daily for now"). The trigger-on-folder-
    change upgrade (ORG-013-shaped git/inotify hook on rulebook/architecture/adr/sop) is a
    follow-up whose hook plumbing may sub-delegate to Recovery (the operator's note).

  python3 tools/policy_outflow_sync.py            # regenerate; commit+push if changed
  python3 tools/policy_outflow_sync.py --dry-run  # print the digest, write/commit nothing
  python3 tools/policy_outflow_sync.py --no-push   # commit locally, skip push
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

# Bootstrap so `from tools._git_utils import ...` works whether run as a script
# (`python3 tools/policy_outflow_sync.py`) or imported as a package module.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.ledger_shard import TS_FORMAT, iter_ledger_entry_files  # noqa: E402

# Authoritative policy dirs whose changes must reach briefs (the operator: P&Ds/ADRs/SOPs).
# architecture/rfc/ added (design-spec tier, established by ORG-092) so ratifiable RFCs
# surface in the recency digest like ADRs — the outflow gap the rfc tier left open.
WATCHED = ("rulebook", "architecture/adr", "architecture/rfc", "sop")
# Generated digest — OUTSIDE WATCHED so regenerating it cannot self-trigger.
DIGEST_REL = "notes/recent-policy-changes.md"
DEFAULT_WINDOW_DAYS = 30
# Filenames that are indexes/readmes, not ratifiable artifacts — still reported
# (a README change can be meaningful) but never treated specially.

# T-1580 pt.2: repo-tracked standing-context (always-loaded) doc glob set, checked
# for size drift each run. Glob (not a hardcoded list) so new rulebook/*.md files
# are auto-covered. Per-agent home-dir files (e.g. ~/CLAUDE.md, ~/HANDOFF.md) are
# OUT of scope — cross-user perms, per-agent self-check territory instead.
STANDING_CONTEXT_GLOBS = ("rulebook/*.md", "sop/*/README.md")
# claude-code#54393 post-mortem's measured drift/bloat line for CLAUDE.md-class
# standing-context docs, in KiB (verified against T-1580's own numbers: directives.md
# at 39631B / 40960B = 96.8%; sop/standard/README.md at 34990B / 40960B = 85.4%).
SIZE_DRIFT_LINE_BYTES = 40 * 1024
# Warn threshold — cheaper/earlier than the drift line itself.
SIZE_WARN_BYTES = 35 * 1024
# Stable, greppable prefix so journal searches / future alerting can key on it.
SIZE_WARN_TOKEN = "SIZE-WARN"

# T-1589 (ORG-040 tier 2): authority-claim <-> ledger corroboration warn-check.
# Scan globs for ratification-marker prose. architecture/adr/ and architecture/rfc/
# are flat; rulebook/**/*.md picks up both rulebook/*.md and rulebook/policies/*.md
# (the ratifiable-class tiers named in ORG-015) with one pattern. architecture/rfc/
# is a ratifiable substrate (RFCs are Ratify-required, ORG-092), so its
# "Ratified ... by the operator" markers get the same anchor<->ledger corroboration as
# ADRs. sop/ is deliberately NOT scanned — it is not a ratifiable-claim substrate
# per ORG-015 and carries no such markers. (The globs match each dir's README/
# TEMPLATE too, but those carry no dated ratifi+operator+anchor lines, so they never
# produce a marker — same as adr/README.md already does today.) (ADR/RFC path per
# ORG-094 architecture umbrella; adr/ moved to architecture/adr/ 2026-09-06, T-3472.)
AUTHORITY_MARKER_GLOBS = (
    "architecture/adr/*.md", "architecture/rfc/*.md", "rulebook/**/*.md")
# Markers dated before this predate the msg-id/TG-id anchor convention (evidence:
# the only anchor-less "Ratified ... by the operator" markers in the corpus are the
# 2026-04-28 founding-week ones, architecture/adr/ORG-002 + rulebook/principles.md,
# both saying only "(Telegram)" with no id; the first msg-id citation appears
# 2026-05-02, architecture/adr/ORG-004). Anchor-less lines are already skipped by
# the anchor-presence gate
# in find_authority_markers, so this floor is a named, auditable belt-and-suspenders
# for the historical case rather than the sole line of defense.
AUTHORITY_MARKER_DATE_FLOOR = date(2026, 5, 1)
# ±window (days) for artifact/ticket-ref corroboration (see _marker_corroborated).
CORROBORATION_WINDOW_DAYS = 2
AUTHORITY_WARN_TOKEN = "AUTHORITY-WARN"

_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
# msg-id family: msg_NNN / msg NNN / TG NNN / Telegram NNN. Requires a digit
# immediately (after an underscore/space separator) after the keyword, so
# approximate refs like "msg_~1497-window" (ORG-013) do not match — by
# construction, not a special case.
_MSG_ANCHOR_RE = re.compile(r"(?:msg|tg|telegram)[_\s]+(\d{3,7})", re.IGNORECASE)
# Ticket-ref anchor: T-NNNN. Left word-boundary (T-1601 [1]) so this doesn't
# spuriously match inside words shaped like REPORT-1234 / SUPPORT-9999 (the T
# there is preceded by a word char, so \b excludes it; a genuine "T-1234" is
# always preceded by whitespace/punctuation/string-start, so \b still matches).
_TICKET_ANCHOR_RE = re.compile(r"\bT-(\d{3,5})")

_RATIFIABLE_RE = re.compile(r"ratifiab\w*")
_STRONG_RATIFIED_RE = re.compile(r"\bratified\b[^.;]{0,80}?\bby operator\b")
_PENDING_RATIFY_RE = re.compile(r"\b(?:pending|until|awaiting|not yet|no)\b(?:\W+\w+){0,3}?\W+ratifi")

_GEN_HEADER = (
    "<!-- GENERATED by tools/policy_outflow_sync.py (T-0834) — do not hand-edit; "
    "edits are overwritten on the next run. -->"
)


def repo_root(start: Path | None = None) -> Path:
    cur = (start or Path(__file__)).resolve()
    for cand in (cur, *cur.parents):
        if (cand / ".git").exists():
            return cand
    return Path(__file__).resolve().parent.parent


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def collect_raw_log(root: Path, since_days: int) -> str:
    """Raw `git log` over the watched dirs in the window. Separated for testability."""
    return _git(
        root, "log", f"--since={since_days} days ago",
        "--name-status", "--date=short", "--pretty=format:C\t%h\t%ad\t%s",
        "--", *WATCHED,
    )


def parse_log(raw: str) -> dict[str, dict]:
    """Parse `git log --name-status` into {path: most-recent change} (pure).

    Commit lines are `C\\t<hash>\\t<date>\\t<subject>`; file lines are
    `<status>\\t<path>` (or `R<score>\\t<old>\\t<new>` for renames). The first time
    we see a path walking newest→oldest is its most-recent change.
    """
    out: dict[str, dict] = {}
    # Paths gone as of HEAD: deleted, or the OLD side of a rename. Walking
    # newest→oldest, once a path is tombstoned an OLDER commit that touched it
    # must NOT resurrect it via setdefault (T-1310: an added-then-renamed or
    # added-then-deleted artifact otherwise kept its dead path in the digest).
    gone: set[str] = set()
    cur = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        if line.startswith("C\t"):
            _, h, d, *subj = line.split("\t")
            cur = {"hash": h, "date": d, "subject": "\t".join(subj)}
            continue
        if cur is None:
            continue
        parts = line.split("\t")
        status = parts[0]
        # Rename/copy lines carry old + new; the new path is last.
        path = parts[-1]
        # Old side of a rename is gone as of this (newer) commit — unless a newer
        # commit already re-established it as live (then it's in `out`).
        if status.startswith("R") and len(parts) >= 3:
            old = parts[1]
            if old.endswith(".md") and old not in out:
                gone.add(old)
        if not path.endswith(".md"):
            continue
        # README/index + TEMPLATE files are the auto-maintained indexes
        # (spine_index_groom) / authoring scaffolds, not ratifiable artifacts —
        # briefs point at the indexes directly; excluding both keeps the digest to
        # real knowledge changes (and drops spine-groom + template-edit noise).
        # (architecture/rfc/ carries a TEMPLATE.md; adr/ does not — but excluding by
        # name is dir-agnostic and correct for any tier.)
        if Path(path).name in ("README.md", "TEMPLATE.md"):
            continue
        if status.startswith("D"):  # deletion — artifact gone, don't point at it
            if path not in out:
                gone.add(path)
            out.pop(path, None)
            continue
        if path in gone:  # tombstoned by a newer delete/rename — don't resurrect
            continue
        out.setdefault(path, {**cur, "status": status[:1]})
    return out


def _title_of(root: Path, rel_path: str) -> str:
    """First markdown heading of a file, else its stem. Best-effort (file may be gone).

    Skips YAML frontmatter (delimited by opening/closing `---`) before scanning for
    a markdown heading — otherwise YAML comment lines like `# elements: — ORG-065 ...`
    inside the frontmatter get returned as if they were the H1.
    """
    p = root / rel_path
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        i = 0
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i < len(lines) and lines[i].strip() == "---":
            i += 1
            while i < len(lines) and lines[i].strip() != "---":
                i += 1
            if i < len(lines):
                i += 1  # skip closing ---
        for line in lines[i:]:
            s = line.strip()
            if s.startswith("#"):
                return s.lstrip("#").strip()
    except OSError:
        pass
    return Path(rel_path).stem


def standing_context_files(root: Path) -> list[Path]:
    """Resolve the standing-context file set: rulebook/*.md, sop/*/README.md, and
    CLAUDE.md at the repo root if present (T-1580 pt.2). Pure filesystem glob —
    no git involved, so this reflects the current tree regardless of window."""
    files: list[Path] = []
    for pattern in STANDING_CONTEXT_GLOBS:
        files.extend(sorted(root.glob(pattern)))
    claude_md = root / "CLAUDE.md"
    if claude_md.exists():
        files.append(claude_md)
    return files


def size_warnings(root: Path) -> list[str]:
    """Greppable WARN lines for standing-context files at/over SIZE_WARN_BYTES.

    Pure (given `root`); warn-only — never raises, never affects caller's return
    code. Threshold is inclusive (>=) so a file landing exactly on the line warns.
    """
    warnings = []
    for f in standing_context_files(root):
        size = f.stat().st_size
        if size >= SIZE_WARN_BYTES:
            pct = size / SIZE_DRIFT_LINE_BYTES * 100
            warnings.append(
                f"policy_outflow_sync: {SIZE_WARN_TOKEN} {f.relative_to(root)} "
                f"{size}B ({pct:.1f}% of the {SIZE_DRIFT_LINE_BYTES}B/40KB drift line, "
                f"warn threshold {SIZE_WARN_BYTES}B/35KB)"
            )
    return warnings


def extract_anchor_ids(text: str) -> tuple[set[str], set[str]]:
    """Extract (msg_ids, ticket_refs) out-of-band anchors from one line of text.

    Shared by both marker-side (adr/rulebook prose) and ledger-side (raw JSONL
    line) extraction so the same anchor vocabulary is recognized on both sides
    of the corroboration match. Pure, never raises.
    """
    msg_ids = {m.group(1) for m in _MSG_ANCHOR_RE.finditer(text)}
    ticket_refs = {f"T-{m.group(1)}" for m in _TICKET_ANCHOR_RE.finditer(text)}
    return msg_ids, ticket_refs


@dataclass(frozen=True)
class AuthorityMarker:
    rel_path: str
    line_no: int
    marker_date: date
    msg_ids: frozenset[str]
    ticket_refs: frozenset[str]
    line_text: str


def find_authority_markers(root: Path) -> list[AuthorityMarker]:
    """Scan AUTHORITY_MARKER_GLOBS for ratification-claim lines (T-1589).

    A line becomes a scannable marker only if it (a) mentions both "ratifi..."
    and "operator" (case-insensitive), (b) carries a YYYY-MM-DD date, and (c)
    carries at least one extractable anchor (msg-id or ticket-ref). (c) is a
    deliberate precision gate: per ORG-040, an authority claim without an
    out-of-band anchor "is not a ratification marker, it is a draft note" —
    that is tier 1's concern, not this tier-2 corroboration check's (and
    without it, prose that merely *restates* an already-anchored ratification
    elsewhere in the same doc — e.g. adr/ORG-038's body paragraph restating
    "Ratified 2026-07-06 by the operator." without repeating the T-1582 anchor
    carried in its own frontmatter line — would false-positive as unanchored).
    The date floor (AUTHORITY_MARKER_DATE_FLOOR) then skips markers predating
    the anchor convention. Pure, never raises (unreadable files are skipped).
    """
    markers: list[AuthorityMarker] = []
    files: list[Path] = []
    for pattern in AUTHORITY_MARKER_GLOBS:
        files.extend(root.glob(pattern))
    for f in sorted(set(files)):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = f.relative_to(root).as_posix()
        for i, line in enumerate(text.splitlines(), start=1):
            low = line.lower()
            # T-3309: "ratifiable" (e.g. ratifiable_files) is not a claim. A line with a
            # non-claim phrase ("pending / until / awaiting / not yet / no ... ratif...")
            # is skipped unless it ALSO makes the strong claim "Ratified ... by the operator".
            claim = _RATIFIABLE_RE.sub("", low)
            if "ratifi" not in claim or "operator" not in low:
                continue
            if _PENDING_RATIFY_RE.search(claim) and not _STRONG_RATIFIED_RE.search(claim):
                continue
            date_m = _DATE_RE.search(line)
            if not date_m:
                continue
            try:
                marker_date = datetime.strptime(date_m.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            msg_ids, ticket_refs = extract_anchor_ids(line)
            if not msg_ids and not ticket_refs:
                continue
            if marker_date < AUTHORITY_MARKER_DATE_FLOOR:
                continue
            markers.append(AuthorityMarker(
                rel, i, marker_date, frozenset(msg_ids), frozenset(ticket_refs),
                line.strip(),
            ))
    markers.sort(key=lambda m: (m.rel_path, m.line_no))
    return markers


def _load_ledger_anchor_index(root: Path) -> list[tuple[date, set[str], set[str]]]:
    """Read every ledger shard, return (entry_date, msg_ids, ticket_refs) per entry.

    Routes through ledger_shard.iter_ledger_entry_files (T-0148 backward-compat
    primitive, same one tools/cycle_stats.py and tools/retro_metrics.py already
    use for read-only ledger analysis) so this is correct pre/post/during the
    weekly-shard migration. Anchor extraction runs on the RAW json line text
    (not a re-serialization) so it uniformly covers ids appearing in rationale
    prose or inside links[].ref alike — no separate structured-vs-prose path
    needed. Malformed lines/entries are skipped, never raise (warn-only tool).
    """
    out: list[tuple[date, set[str], set[str]]] = []
    for shard in iter_ledger_entry_files(root / "ledger"):
        try:
            text = shard.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = entry.get("ts")
            if not isinstance(ts, str):
                continue
            try:
                entry_date = datetime.strptime(ts, TS_FORMAT).date()
            except ValueError:
                continue
            # T-3309: a retroactive capture keeps its honest write-time ts but declares
            # the date of the event it records; corroboration is measured from that.
            event_date = entry.get("event_date")
            if entry.get("retroactive_capture") is True and isinstance(event_date, str):
                try:
                    entry_date = date.fromisoformat(event_date)
                except ValueError:
                    pass
            msg_ids, ticket_refs = extract_anchor_ids(line)
            out.append((entry_date, msg_ids, ticket_refs))
    return out


def _marker_corroborated(
    marker: AuthorityMarker, entries: list[tuple[date, set[str], set[str]]],
) -> bool:
    """True if some ledger entry corroborates `marker`.

    Two paths, asymmetric on purpose (mirrors the ticket's own "same msg id, OR
    same artifact ref within a window" phrasing): a shared msg-id needs no
    window (msg ids are exact out-of-band identifiers — an unrelated reuse is
    implausible), a shared ticket-ref needs the ±CORROBORATION_WINDOW_DAYS
    window (ticket refs get reused across a ticket's whole lifecycle, so only
    a nearby mention is meaningful corroboration of THIS ratification).
    """
    for entry_date, entry_msg_ids, entry_ticket_refs in entries:
        if marker.msg_ids & entry_msg_ids:
            return True
        if (marker.ticket_refs & entry_ticket_refs
                and abs((entry_date - marker.marker_date).days) <= CORROBORATION_WINDOW_DAYS):
            return True
    return False


def authority_corroboration_warnings(root: Path) -> list[str]:
    """Greppable WARN lines for ratification markers lacking ledger corroboration.

    ORG-040 tier 2 (T-1589): the accidental-authority-confusion catch. Warn-only
    by construction (mirrors size_warnings) — never raises, never affects the
    caller's return code, never touches git. Deterministic: markers are sorted
    by (file, line) and the ledger index is read fresh each call.
    """
    markers = find_authority_markers(root)
    if not markers:
        return []
    entries = _load_ledger_anchor_index(root)
    warnings = []
    for m in markers:
        if _marker_corroborated(m, entries):
            continue
        anchors = ", ".join(sorted(m.msg_ids) + sorted(m.ticket_refs))
        warnings.append(
            f"policy_outflow_sync: {AUTHORITY_WARN_TOKEN} {m.rel_path}:{m.line_no} "
            f"ratified-marker dated {m.marker_date.isoformat()} (anchors: {anchors}) "
            f"has no corroborating ledger entry within ±{CORROBORATION_WINDOW_DAYS}d "
            "(ORG-040 tier 2, T-1589) — possible accidental authority-confusion, "
            "human review"
        )
    return warnings


def build_digest(root: Path, changes: dict[str, dict], since_days: int) -> str:
    """Render the pointer-only digest grouped by watched dir (pure given `changes`)."""
    lines = [
        "# Recent policy changes",
        "",
        _GEN_HEADER,
        "",
        f"Ratified/changed P&Ds, ADRs, and SOPs in the last {since_days} days — the "
        "ORG-012 outflow recency signal (T-0834). Pointers only; read the linked "
        "artifact for content. Briefs link here for *what's new*; the authoritative "
        "full set is each dir's README/index.",
        "",
    ]
    if not changes:
        lines.append(f"_No policy artifacts changed in the last {since_days} days._")
        return "\n".join(lines) + "\n"

    by_dir: dict[str, list[tuple[str, dict]]] = {d: [] for d in WATCHED}
    for path, meta in changes.items():
        # Multi-segment WATCHED entries (e.g. "architecture/adr" per ORG-094)
        # can't be matched via first-segment split; use prefix match with a
        # trailing "/" so "architecture/adr" matches "architecture/adr/*.md"
        # but not a hypothetical sibling "architecture/adrs/*".
        for d in WATCHED:
            if path.startswith(d + "/"):
                by_dir[d].append((path, meta))
                break

    for d in WATCHED:
        entries = by_dir[d]
        if not entries:
            continue
        # Newest first.
        entries.sort(key=lambda e: e[1]["date"], reverse=True)
        lines.append(f"## {d}/")
        lines.append("")
        for path, meta in entries:
            title = _title_of(root, path)
            tag = "new" if meta.get("status") == "A" else "changed"
            lines.append(
                f"- [{title}]({_rel_link(path)}) — {meta['date']} ({tag}; "
                f"`{meta['hash']}` {meta['subject']})"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _rel_link(path_from_root: str) -> str:
    """Link target relative to the digest's location (notes/)."""
    return "../" + path_from_root


def generate(root: Path, since_days: int) -> str:
    return build_digest(root, parse_log(collect_raw_log(root, since_days)), since_days)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS)
    ap.add_argument("--dry-run", action="store_true", help="print digest, write nothing")
    ap.add_argument("--no-push", action="store_true", help="commit locally, skip push")
    ap.add_argument("--root", default=None, help="repo root override (tests)")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve() if args.root else repo_root()

    # WARN-only (T-1580 pt.2): unconditional, including --dry-run — independent of
    # the digest-changed/no-op branches below, never affects rc, never touches git.
    for warning in size_warnings(root):
        print(warning, file=sys.stderr)

    # WARN-only (T-1589, ORG-040 tier 2): same unconditional shape as above.
    for warning in authority_corroboration_warnings(root):
        print(warning, file=sys.stderr)

    digest = generate(root, args.days)

    if args.dry_run:
        sys.stdout.write(digest)
        return 0

    target = root / DIGEST_REL
    current = target.read_text(encoding="utf-8") if target.exists() else None
    if current == digest:
        print("policy_outflow_sync: no change; digest current", file=sys.stderr)
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(digest, encoding="utf-8")
    # T-2336: pathspec-scoped commit + git_mutation_lock — the bare commit here
    # could swallow a concurrent writer's staged files under this message on the
    # shared checkout. Lock is repo-anchored (not root-derived), so a non-prod
    # `root` over-locks harmlessly.
    try:
        from tools._git_utils import GitMutationLockTimeout, git_mutation_lock
    except ImportError:  # bare run with cwd=tools/
        from _git_utils import GitMutationLockTimeout, git_mutation_lock  # type: ignore
    try:
        with git_mutation_lock(label="policy-outflow digest refresh"):
            _git(root, "add", DIGEST_REL)
            _git(root, "commit", "-m",
                 "chore(policy-outflow): refresh recent-policy-changes digest (T-0834)",
                 "--", DIGEST_REL)
    except subprocess.CalledProcessError as exc:
        print(f"policy_outflow_sync: git step failed: {exc.stderr or exc}", file=sys.stderr)
        return 1
    except GitMutationLockTimeout as exc:
        print(f"policy_outflow_sync: git mutation lock timeout: {exc}", file=sys.stderr)
        return 1
    if not args.no_push:
        # T-1207: bounded retry on non-fast-forward. This shared repo has a
        # high-frequency concurrent writer (Specialist ticket.py + watch auto-commits)
        # that can advance origin between our pre-push rebase and the push, losing
        # the race — a single push then exits 1 and the systemd unit goes "failed"
        # despite the commit being fine. push_with_retry (T-0487) does fetch+rebase
        # +retry up to 3 attempts; only fail the unit if every attempt loses.
        from tools._git_utils import push_with_retry
        ok, detail = push_with_retry(root)
        if not ok:
            print(f"policy_outflow_sync: push failed after retries: {detail}", file=sys.stderr)
            return 1
        print(f"policy_outflow_sync: {detail}", file=sys.stderr)
    print("policy_outflow_sync: digest refreshed + committed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
