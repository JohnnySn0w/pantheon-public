#!/usr/bin/env python3
"""Pantheon watch-loop digest.

Reads today's reports tree at `knowledge/watch/reports/<YYYY-MM-DD>/`, summarizes one
line per report, flags above-the-bar items via simple keyword detection, and
sends a single Telegram message to the operator.

Designed to run as the second stage of the systemd-user-timer chain (driver
fires the researcher dispatches → driver writes reports + index → digest
runs ~end-of-day → digest sends Telegram). Can also run on demand:

    python3 tools/watch_digest.py                 # today's digest
    python3 tools/watch_digest.py --date 2026-04-29
    python3 tools/watch_digest.py --no-send       # print to stdout, don't send
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone, date
from pathlib import Path

# Standard library + urllib only — match B2 cost monitor's no-extra-deps posture.
import urllib.parse
import urllib.request

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._git_utils import commit_scoped, pull_repo_or_warn  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS_ROOT = REPO_ROOT / "knowledge" / "watch" / "reports"

# Bot token comes from the same .env the Telegram MCP plugin reads.
# Pantheon convention; matches B2 cost monitor.
TELEGRAM_ENV = Path(os.environ.get("PANTHEON_TELEGRAM_ENV", str(Path.home() / ".claude" / "channels" / "telegram" / ".env")))
TELEGRAM_CHAT_ID = os.environ.get("PANTHEON_TELEGRAM_CHAT_ID", "456474197")  # the operator

# Surfacing is producer-declared (P1: structural signal at the source), with the
# keyword list below as a fallback/security-fast-path only. The researcher emits
# `surface: above-bar|routine` + `headline:` frontmatter (see watch_runner.build_brief);
# `is_above_bar` honours that signal first and only falls back to keyword-matching
# when a report carries no frontmatter signal (legacy reports / emit-side not yet run).
SURFACE_ABOVE = {"above-bar", "above_bar", "above the bar", "above"}
SURFACE_BELOW = {"routine", "below-bar", "below_bar", "below the bar", "below"}

# Above-the-bar markers — keyword fast-path. Catches security/billing incidents even
# if a report forgot to self-declare; NOT the primary surfacing path anymore.
ABOVE_BAR_KEYWORDS = [
    "CVE-",
    "RCE",
    "supply-chain",
    "supply chain",
    "rugpull",
    "deprecated",
    "breaking change",
    "exploit",
    "vulnerability",
    "policy change",
    "billing",
    "classifier",
    "ToS update",
]


def read_telegram_token(path: Path) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() in ("TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "TOKEN"):
            return value.strip().strip('"').strip("'")
    return None


def parse_frontmatter(report_text: str) -> dict[str, str]:
    """Parse a leading `---`-delimited YAML-ish frontmatter block into a flat dict.

    Only the simple `key: value` lines we care about (surface, headline) — not a
    full YAML parser (no extra deps). Returns {} if the report has no frontmatter.
    """
    lines = report_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fm: dict[str, str] = {}
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        if ":" in ln:
            key, _, value = ln.partition(":")
            fm[key.strip().lower()] = value.strip().strip('"').strip("'")
    return fm


def matched_keyword(report_text: str) -> str | None:
    """Return the first `ABOVE_BAR_KEYWORDS` entry found in report_text, or None."""
    lower = report_text.lower()
    for kw in ABOVE_BAR_KEYWORDS:
        if kw.lower() in lower:
            return kw
    return None


def is_above_bar(report_text: str) -> bool:
    """Producer-declared surfacing first (P1), keyword fast-path as fallback.

    A report that self-declares `surface: above-bar|routine` in frontmatter is
    authoritative — it had the full research context. Only when no signal is
    present (legacy report, or emit-side not yet run) do we fall back to the
    keyword scan, which still catches an unflagged security/billing incident.
    """
    surface = parse_frontmatter(report_text).get("surface", "").lower()
    if surface in SURFACE_ABOVE:
        return True
    if surface in SURFACE_BELOW:
        return False
    return matched_keyword(report_text) is not None


def label_disagreement(report_text: str) -> str | None:
    """Flag a declared-routine report that trips an above-bar keyword (T-2042).

    Producer declaration stays authoritative for the *effective* bar (P1) —
    this does NOT change `is_above_bar`'s verdict or which bucket the report
    lands in. It's a distinct, additive signal: a report the author explicitly
    labeled `surface: routine` that nonetheless contains a security/billing
    keyword the org cares most about. Returns the tripped keyword, or None.

    Only fires for an EXPLICIT below-bar declaration. A no-declaration report
    with a keyword hit already surfaces above-bar via `is_above_bar`'s own
    fallback — flagging it here too would be redundant noise, not a gap.
    """
    surface = parse_frontmatter(report_text).get("surface", "").lower()
    if surface not in SURFACE_BELOW:
        return None
    return matched_keyword(report_text)


def report_summary(report_text: str, max_len: int = 140) -> str:
    """Prefer the producer's own `headline:` (its one-line 'why this matters');
    fall back to TL;DR / first-line extraction for reports without it."""
    headline = parse_frontmatter(report_text).get("headline", "")
    if headline:
        return headline[:max_len]
    return first_line_summary(report_text, max_len)


def first_line_summary(report_text: str, max_len: int = 140) -> str:
    """Pull a one-line synopsis: prefer a TL;DR or first non-header line."""
    lines = [ln.strip() for ln in report_text.splitlines() if ln.strip()]
    # Look for explicit TL;DR / Summary headers and grab the line after.
    for i, ln in enumerate(lines):
        low = ln.lower()
        if low.startswith(("# tl;dr", "## tl;dr", "## summary", "# summary")):
            for ln2 in lines[i + 1:]:
                if ln2.startswith("#"):
                    break
                if not ln2.startswith(("|", "-", "*", ">", "```")):
                    return ln2[:max_len]
    # Fallback: first non-header non-list line.
    for ln in lines:
        if ln.startswith(("#", "-", "*", "|", ">", "```")):
            continue
        return ln[:max_len]
    return "(no summary extractable)"


def collect_today(date_dir: Path) -> list[tuple[str, bool, str, str | None]]:
    """Returns list of (slug, above_bar, one_line_summary, disagreement_keyword) per report.

    `disagreement_keyword` (T-2042) is the tripped `ABOVE_BAR_KEYWORDS` entry when
    the report explicitly declared `surface: routine` but its body contains one
    anyway — see `label_disagreement`. None when there's no disagreement.
    """
    if not date_dir.exists():
        return []
    rows = []
    for path in sorted(date_dir.glob("*.md")):
        if path.name == "README.md":
            continue
        slug = path.stem
        text = path.read_text(encoding="utf-8")
        rows.append((slug, is_above_bar(text), report_summary(text), label_disagreement(text)))
    return rows


# ---------------------------------------------------------------------------
# Operator-facing lean slot (T-2199).
# ---------------------------------------------------------------------------
#
# THE GAP THIS CLOSES. sop/standard/throughline-sweep-result-filing.md:45 makes it
# a MUST that a goal-level `target=operator` ticket is ALSO lean-surfaced to the operator
# ("Telegram/digest"), because nothing auto-delivers to him — inbox_consumer is
# agent-only, activity_digest posts only RESOLVED, ticket_state_sweep surfaces only
# operator-MOOT. For specialist that MUST had NO MECHANISM: this digest was a pure
# function of knowledge/watch/reports/<date>/*.md, with no flag, file or env var by
# which any other text could reach the send. Specialist asserted the surfacing twice
# (T-2186, T-2189) and delivered it zero times — not by cutting a corner, but by
# believing a channel existed because policy named it.
#
# WHY THIS IS DERIVED FROM TICKET STATE AND NOT A LEAN QUEUE. A queue file (or a
# --message flag) is a SECOND record that can disagree with the first, and the bug
# being fixed is precisely "the record asserts something that did not happen".
# Adding a writable side-channel makes that class of failure MORE available, not
# less. `target: operator` + parked-for-N-days is already computable from the
# tickets themselves, so there is nothing to keep in sync and nothing to forget to
# write. It also fixes the gap for EVERY agent rather than for specialist specifically —
# recovery and mnemosyne have the same hole, and neither had noticed.
#
# DELIVER BEFORE RECORDING (T-2152, restated by specialist as a constraint on this fix).
# The stamp is written ONLY after send_telegram returns True. A lean-surface whose
# failure mode is a record saying it was surfaced would be the original bug wearing
# the fix's clothes.

LEAN_MIN_AGE_DAYS = 2
LEAN_RESURFACE_DAYS = 7
LEAN_MAX_ROWS = 5
# T-2209 (specialist). The lean slot had NO URGENCY AXIS, and two individually-correct
# properties composed into a silent floor: LEAN_MIN_AGE_DAYS means a ticket whose
# decision window is shorter than two days can never be surfaced AT ALL, and
# oldest-first ordering then puts a newly-parked closing-window item structurally
# last. Measured: T-2107's window closes the moment the operator starts grading, and
# best case the slot would have reached it in 4-5 days.
#
# The field is `decide_by`, the symmetric inverse of the `not_before` gate tickets
# already carry (ORG-019): not_before says "not yet", decide_by says "not later
# than". DERIVED FROM TICKET STATE ON PURPOSE — no writable side-channel, no
# --message flag. There is no second record, so there is nothing to sync and
# nothing that can disagree with the ticket (specialist's constraint, and T-2199's
# thesis: the digest must never claim something it does not derive).
LEAN_DECIDE_BY_FIELD = "decide_by"
LEAN_STAMP_VERB = "lean-surfaced"
TICKETS_ROOT = REPO_ROOT / "tickets"
# inbox/ and active/ only. done/ is closed and blocked-state was retired (ORG-019).
LEAN_TICKET_DIRS = ("inbox", "active")


def _parse_ticket_ts(raw: str) -> datetime | None:
    """Parse a ticket frontmatter/transition timestamp. None if unparseable."""
    try:
        return datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def last_lean_surface(body: str) -> datetime | None:
    """Most recent `lean-surfaced` RECEIPT timestamp in the body, or None.

    Reads the ticket's OWN transition log rather than a side ledger, so the
    receipt lives with its subject (P4) and a ticket that is hand-edited,
    retargeted or resolved carries its surfacing history with it.

    T-2202: MATCHES ON SEGMENT POSITION, NOT SUBSTRING. The first cut accepted
    the verb anywhere in a `- ` line, so PROSE THAT MERELY DISCUSSES
    lean-surfacing read as having DONE it. Measured on T-1195, whose resolve note
    says "the operator lean-surfaced the SOP-vs-directive call (TG-534)" about a
    different act on a different ticket — it parsed as a receipt and would have
    suppressed a real lean for 7 days. That is T-2199's own thesis reproduced one
    layer in: a record asserting something that did not happen. Latent only
    because LEAN_TICKET_DIRS excludes done/; fixed while free.

    BOTH RECEIPT SHAPES ARE VALID and a naive "verb immediately after [actor]"
    anchor would reject the one that matters most:

        - <ts> [specialist] lean-surfaced; ...          automated (stamp_lean_surfaced)
        - <ts> [claude] note; lean-surfaced; ...   MANUAL relay via ticket.py annotate,
                                                   where the CLI owns the verb slot

    T-1953 carries the second form — I hand-relayed it before this mechanism
    existed. Anchoring on the verb slot alone would have un-suppressed it and
    re-sent the operator something he had already received, which is the exact
    double-surfacing this suppressor exists to prevent. So the rule is:
    `lean-surfaced` must be a whole `;`-delimited SEGMENT of the text after
    `[actor] ` — which accepts both forms and rejects mid-sentence prose.
    """
    latest = None
    for line in body.splitlines():
        if not line.startswith("- ") or LEAN_STAMP_VERB not in line:
            continue
        actor_split = line.split("] ", 1)
        if len(actor_split) != 2:
            continue
        if LEAN_STAMP_VERB not in [seg.strip() for seg in actor_split[1].split(";")]:
            continue
        ts = _parse_ticket_ts(line[2:].split(" ", 1)[0])
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


def _parse_decide_by(fm: dict) -> datetime | None:
    """The `decide_by` deadline as an aware UTC datetime, or None.

    Accepts a bare date (`2026-07-30`) as well as a full timestamp, because the
    field is hand-written and a date is the natural thing to type. A bare date
    parses NAIVE, and comparing naive to aware raises TypeError — so normalise
    here rather than at each comparison site. An unparseable value returns None
    and the ticket simply keeps ordinary age-ordered treatment: a typo in a
    deadline must not make a ticket invisible, which would be a worse failure
    than the one this field exists to fix.
    """
    ts = _parse_ticket_ts(str(fm.get(LEAN_DECIDE_BY_FIELD, "") or ""))
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def collect_operator_leans(
    tickets_root: Path | None = None,
    *,
    now: datetime | None = None,
) -> list[tuple[str, str, int, Path, datetime | None]]:
    """Tickets parked at `target: operator` that are due a lean-surface.

    Returns (tid, title, days_parked, path, decide_by) with any `decide_by`
    ticket FIRST (soonest deadline first, so an already-overdue decision leads),
    then the rest oldest-first — a ticket that has rotted longest is the one most
    worth his attention, and truncation must never drop it (T-2166: truncation
    only ever eats the tail).

    A ticket qualifies when it is at `target: operator`, has been parked at least
    LEAN_MIN_AGE_DAYS, and has not been lean-surfaced within LEAN_RESURFACE_DAYS.
    The re-surface window is what keeps this a nudge rather than a daily nag —
    without it a parked ticket would appear in every digest until acted on, and
    a digest section that is always identical stops being read.

    `decide_by` EXEMPTS A TICKET FROM LEAN_MIN_AGE_DAYS (T-2209). The floor is
    anti-nag design for tickets whose only claim is age; a ticket carrying a
    stated deadline is making a different claim, and the floor was silently
    denying it a hearing. It does NOT exempt from LEAN_RESURFACE_DAYS, and that
    is deliberate rather than an oversight: the re-surface window is what keeps
    this a nudge, and a deadline is not a licence to repeat daily. If a real
    case shows a window closing inside the 7 days with no second surface, that
    is the evidence to revisit it — the ask here was one bit of urgency.

    INTERACTION WITH TRUNCATION, stated because it is a real tension with
    T-2166: enough simultaneous `decide_by` tickets could push the most-rotted
    item past LEAN_MAX_ROWS. Nothing is silently dropped — the tail count line
    still renders — and a fleet holding 5+ live deadlines at once has a
    different problem than row ordering. Revisit if that is ever observed.
    """
    # Resolved at CALL time, not bound as a default at def time. A module-level
    # default (`tickets_root: Path = TICKETS_ROOT`) is captured at import and no
    # later rebinding of TICKETS_ROOT can reach it — the same import-time-binding
    # trap that bit DELIVERY_MODE and that the T-2156 plan calls out.
    #
    # Caught by test_successful_send_writes_the_stamp, and worth recording HOW:
    # that test patched TICKETS_ROOT, the patch could not reach the captured
    # default, so the run scanned the REAL tickets tree and wrote five real
    # lean-surfaced stamps into the worktree before failing on the fixture it was
    # actually asserting about. The test did not merely detect the bug — it
    # DEMONSTRATED it, by doing the damage. Keep that test: if this default is
    # ever reintroduced, it fails again the same way.
    tickets_root = tickets_root or TICKETS_ROOT
    now = now or datetime.now(timezone.utc)
    out: list[tuple[str, str, int, Path, datetime | None]] = []
    for sub in LEAN_TICKET_DIRS:
        d = tickets_root / sub
        if not d.exists():
            continue
        for path in sorted(d.glob("T-*.md")):
            try:
                body = path.read_text(encoding="utf-8")
            except OSError:
                continue
            fm = parse_frontmatter(body)
            if fm.get("target", "").strip().lower() != "operator":
                continue
            created = _parse_ticket_ts(fm.get("created", ""))
            if created is None:
                continue
            days = (now - created).days
            decide_by = _parse_decide_by(fm)
            if days < LEAN_MIN_AGE_DAYS and decide_by is None:
                continue
            last = last_lean_surface(body)
            if last is not None and (now - last).days < LEAN_RESURFACE_DAYS:
                continue
            title = ""
            for line in body.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            out.append((fm.get("id", path.stem), title, days, path, decide_by))
    # Two-group sort. The leading 0/1 separates deadline-carrying from
    # age-ordered, so the heterogeneous second members (datetime vs int) are
    # only ever compared WITHIN a group — tuple comparison stops at the first
    # differing element, and mixing them would raise TypeError.
    out.sort(key=lambda r: (0, r[4]) if r[4] is not None else (1, -r[2]))
    return out


def stamp_lean_surfaced(leans: list[tuple[str, str, int, Path, datetime | None]], actor: str = "specialist") -> list[str]:
    """Write the delivery receipt into each surfaced ticket. Returns repo-rel paths.

    CALLED ONLY AFTER A SUCCESSFUL SEND. Delegates the line format to
    ticket.append_transition rather than reproducing it — a copied format drifts
    from the real one while every local check keeps agreeing with itself
    (T-2171: a parity test pinned against an inline copy of a rule instead of
    the rule let a whole gate arm go missing with the guard still green).
    """
    from tools.ticket import append_transition  # local: avoids import cost on every run

    written = []
    for tid, _title, days, path, _decide_by in leans:
        try:
            body = path.read_text(encoding="utf-8")
            body = append_transition(
                body, actor, LEAN_STAMP_VERB,
                f"included in the watch digest after {days}d parked at target:operator (T-2199 lean slot)",
            )
            path.write_text(body, encoding="utf-8")
            try:
                written.append(str(path.relative_to(REPO_ROOT)))
            except ValueError:
                # Outside the repo (a test fixture). Stamped, not committable.
                pass
        except Exception as exc:  # noqa: BLE001
            # A failed stamp must never fail the digest: the lean WAS delivered,
            # and the only cost of a missing receipt is that it surfaces again
            # sooner. The reverse — a receipt without a delivery — is the bug.
            print(f"warning: could not stamp {tid}: {exc}", file=sys.stderr)
    return written


def collect_day_index_drift() -> tuple[int, int, str] | str | None:
    """Day-index drift for the drift row. (items, dirs, oldest_day) | error | None.

    T-2363. `knowledge_sweep._check_day_index_drift` runs this same lint and
    deliberately swallows the result so the sweep can never fail on drift. That
    swallow is correct and unchanged. What was missing is a place for the signal
    to LAND: the docstring named the runner log + "next digest summarizer", and
    that summarizer never existed (T-2358). Measured cost: 21 days of undetected
    drift across two episodes, the first escalating 1 -> 4 items while nobody
    looked. This function is the mitigation actually being wired, in the same
    change that names it.

    Computed HERE rather than handed over from the sweep. A state file would add
    an ordering dependency and a staleness mode — a stale "0" would read exactly
    like a clean day. The lint is a pure read over report dirs, so recomputing at
    digest time is cheap and always reflects disk truth (P1: structural over
    coordinated).

    Returns None when clean, so a clean day stays silent. Returns an error STRING
    when the lint itself fails, deliberately: if a failed check were also silent,
    "clean" and "never ran" would be spelled identically in the digest, which is
    the exact ambiguity that hid the 21 days. Silence must mean one thing only.
    """
    try:
        from tools.lint_report_day_index import lint  # local import: never a hard dep
        findings = lint(REPORTS_ROOT)
    except Exception as exc:
        return f"day-index lint failed to run: {exc}"
    if not findings:
        return None
    items = sum(len(dead) + len(unindexed) for dead, unindexed in findings.values())
    return items, len(findings), min(findings)


def build_digest(
    date_iso: str,
    rows: list[tuple[str, bool, str, str | None]],
    leans: list[tuple[str, str, int, Path, datetime | None]] | None = None,
    drift: tuple[int, int, str] | str | None = None,
) -> str:
    """Render the watch digest as Markdown for Telegram.

    Header convention (per T-0018): `<emoji> [<Agent> · <digest-type> · <date>]`.
    Same skeleton in `tools/ticket_grooming.py:build_digest` so a Telegram reader
    can pattern-match the sender within the first 1-2 words. This file is
    Specialist-emitted (specialist) and uses 📡 + Markdown bold around the label.
    """
    leans = list(leans or ())

    def _lean_block() -> list[str]:
        if not leans:
            return []
        shown = leans[:LEAN_MAX_ROWS]
        out = ["", "🔸 *Parked on you:*"]
        for tid, title, days, _path, decide_by in shown:
            # Render the DEADLINE where the age would go, rather than adding a
            # marker beside it. Reordering alone would have moved the row and
            # told him nothing about why — the whole point of the field is that
            # the reason is legible from the line (T-2209).
            lead = f"⏳ decide by {decide_by:%Y-%m-%d}" if decide_by else f"{days}d"
            out.append(f"• `{tid}` — {lead} — {title}")
        if len(leans) > LEAN_MAX_ROWS:
            out.append(f"• …and {len(leans) - LEAN_MAX_ROWS} more — `ticket.py list --target operator`")
        return out

    def _drift_block() -> list[str]:
        if drift is None:
            return []
        if isinstance(drift, str):
            # The check itself broke. Say so rather than staying quiet — an
            # absent row must mean "ran and clean", never "did not run".
            return ["", f"⚠️ *Day-index check did not run:* {drift}"]
        items, dirs, oldest = drift
        return ["", f"⚠️ *Day-index drift:* {items} item(s) across {dirs} dir(s), "
                    f"oldest `{oldest}` — `python3 tools/lint_report_day_index.py`"]

    # A quiet watch day is EXACTLY when a parked ticket most needs carrying: the
    # no-reports branch used to return before anything else could be appended, so
    # the days with least noise were the days a lean could not ride out at all.
    #
    # T-2363: the drift row joins the block for the same reason, and the reason is
    # sharper for drift than for leans. Drift comes from MANUAL report deliveries
    # that bypass the runner — so the days with no runner reports are precisely the
    # days most likely to be carrying it. Appending the drift row only to the
    # has-reports branch would have blinded it on its own highest-yield days.
    if not rows:
        head = f"📭 [Specialist · watch · {date_iso}]\nNo reports today."
        tail = "\n".join(_lean_block() + _drift_block())
        return head + tail if tail else head
    above = [r for r in rows if r[1]]
    below = [r for r in rows if not r[1]]
    disagreements = [r for r in rows if r[3]]
    parts = [f"📡 *[Specialist · watch · {date_iso}]*", ""]
    parts.append(f"{len(rows)} topic(s) fired today.")
    if disagreements:
        parts.append("")
        parts.append("⚠️ *Label disagreements (declared routine, above-bar keyword tripped):*")
        for slug, _, summary, kw in disagreements:
            parts.append(f"• `{slug}` — keyword `{kw}` — {summary}")
    if above:
        parts.append("")
        parts.append("⚠️ *Above the bar:*")
        for slug, _, summary, _ in above:
            parts.append(f"• `{slug}` — {summary}")
    if below:
        parts.append("")
        parts.append("Below the bar:")
        for slug, _, summary, _ in below:
            parts.append(f"• `{slug}` — {summary}")
    parts.extend(_lean_block())
    parts.extend(_drift_block())
    parts.append("")
    parts.append(f"Full reports: `pantheon-config/knowledge/watch/reports/{date_iso}/`")
    return "\n".join(parts)


# Telegram hard-caps a sendMessage at 4096 chars; an over-long body 400s on BOTH Markdown
# AND plain text (the limit is LENGTH, not formatting, so the plain-text fallback can't save
# it). A big watch day overflows — observed 2026-08-24 (T-3135 day, 20 topics fired): the
# digest sank with "HTTP Error 400: Bad Request" on both attempts and the unit exited 1. So
# split the digest on line boundaries into <=limit chunks and send each. The limit sits below
# 4096 to leave headroom for the "(part i/n)" marker + UTF-8 multibyte slack.
TELEGRAM_MAX_CHARS = 4096
_CHUNK_LIMIT = 3800


def _chunk_lines(text: str, limit: int = _CHUNK_LIMIT) -> list[str]:
    """Split `text` into chunks of at most `limit` chars, breaking on line boundaries so a
    line's Markdown stays balanced within one chunk. A single line longer than `limit` is
    hard-split. Always returns at least one chunk."""
    lines: list[str] = []
    for line in text.split("\n"):
        while len(line) > limit:
            lines.append(line[:limit])
            line = line[limit:]
        lines.append(line)
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in lines:
        add = len(line) + (1 if cur else 0)  # +1 for the newline that rejoins them
        if cur and cur_len + add > limit:
            chunks.append("\n".join(cur))
            cur, cur_len = [line], len(line)
        else:
            cur.append(line)
            cur_len += add
    if cur:
        chunks.append("\n".join(cur))
    return chunks or [""]


def _post_telegram(url: str, chat_id: str, text: str, parse_mode: str | None) -> bool:
    """POST one sendMessage. parse_mode=None sends plain text (no markdown parsing)."""
    fields = {"chat_id": chat_id, "text": text}
    if parse_mode:
        fields["parse_mode"] = parse_mode
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status == 200


def _send_one_with_fallback(url: str, chat_id: str, text: str) -> bool:
    """Send ONE message, preferring Markdown but FALLING BACK to plain text.

    The digest embeds researcher-authored headlines — arbitrary prose that can
    contain unbalanced Markdown metacharacters (a lone `_`, `*`, or backtick).
    Legacy-Markdown parsing then 400s and the whole message is dropped (observed
    2026-06-08: a `watch_runner` underscore in one of 8 above-bar headlines sank
    the most important digest of the week). Escaping arbitrary prose for legacy
    Markdown is underspecified/fragile, so on ANY Markdown-send failure we retry
    once as plain text — losing bold formatting but GUARANTEEING delivery, which
    for an unattended alert channel is the right trade.
    """
    try:
        return _post_telegram(url, chat_id, text, "Markdown")
    except Exception as exc:  # noqa: BLE001 — markdown parse 400 / transient / etc.
        print(f"telegram markdown send failed ({exc}); retrying as plain text", file=sys.stderr)
    try:
        ok = _post_telegram(url, chat_id, text, None)
        if ok:
            print("telegram plain-text fallback succeeded", file=sys.stderr)
        return ok
    except Exception as exc:  # noqa: BLE001
        print(f"telegram plain-text send also failed: {exc}", file=sys.stderr)
        return False


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    """Send the digest, CHUNKED to Telegram's 4096-char limit, each chunk carrying the
    Markdown→plain-text fallback. Returns True only if EVERY chunk delivered — an
    incomplete send is a failure, so main() will not stamp leans on partial delivery.
    A single-chunk digest sends exactly as before (no marker), so the common case is
    unchanged.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = _chunk_lines(text)
    n = len(chunks)
    all_ok = True
    for i, chunk in enumerate(chunks, 1):
        # Continuation marker only when actually split, so the reader knows more parts
        # follow (sized into _CHUNK_LIMIT's headroom).
        body = chunk if n == 1 else f"{chunk}\n\n_(part {i}/{n})_"
        if not _send_one_with_fallback(url, chat_id, body):
            all_ok = False
    return all_ok


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pantheon watch-loop digest")
    p.add_argument("--date", help="date in YYYY-MM-DD; default = today UTC")
    p.add_argument("--no-send", action="store_true", help="print to stdout; don't send Telegram")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    pull_repo_or_warn("watch_digest")
    if args.date:
        d = date.fromisoformat(args.date)
    else:
        d = datetime.now(timezone.utc).date()
    date_iso = d.isoformat()
    rows = collect_today(REPORTS_ROOT / date_iso)
    leans = collect_operator_leans(TICKETS_ROOT)
    drift = collect_day_index_drift()
    digest = build_digest(date_iso, rows, leans, drift)
    print(digest)
    # --no-send and the missing-token path BOTH return before any stamp is
    # written. A dry run that recorded a delivery would be T-2182's dry-run-primes-
    # the-cache fault in a surface where the consequence is a false receipt.
    if args.no_send:
        return 0
    token = read_telegram_token(TELEGRAM_ENV)
    if not token:
        print(f"warning: no Telegram token at {TELEGRAM_ENV}; skipping send", file=sys.stderr)
        return 0
    ok = send_telegram(token, TELEGRAM_CHAT_ID, digest)
    if not ok:
        # No stamp on a failed send. The leans stay unsurfaced and ride the next
        # fire — which is the correct failure direction, and the whole point.
        return 1
    if leans:
        changed = stamp_lean_surfaced(leans[:LEAN_MAX_ROWS])
        if changed:
            committed, detail = commit_scoped(
                changed,
                "tickets: stamp lean-surfaced — carried in the watch digest (T-2199)\n\n"
                "Written after the Telegram send returned success, never before.\n",
                push=True,
                sync=False,
            )
            if not committed:
                print(f"warning: lean stamps written but not committed: {detail}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
