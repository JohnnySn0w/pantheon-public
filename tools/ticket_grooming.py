#!/usr/bin/env python3
"""Pantheon ticket-grooming staleness scanner.

Implements the cron'd grooming agent specified in
`architecture/adr/ORG-003-ticket-system.md` §"Grooming agent (Recovery)".

Per fire (single-shot, no subcommands):

1. Scan `tickets/active/` for `T-NNNN.md` files.
2. For each: locate the LAST transition-log entry whose actor is not
   `[grooming]`; record its timestamp.
3. Compute staleness against `stale_after` (per-ticket override or default
   `4h` for active).
4. If stale AND the latest log entry isn't already a `[grooming] stale` flag,
   append a `[grooming] stale (no activity since <ts>); flagging for review`
   bullet to the transition log.
5. Auto-commit modified files via the same shared `_git_utils` discipline as
   `tools/watch_runner.py`: HERMES guard → `git pull --ff-only` → narrow
   `git add -- tickets/active/` → strict-set scope assert →
   plain `git commit` → plain `git push`. No force, no `--no-verify`.
6. Send a plain-text digest to Telegram via Bot API direct-curl, regardless
   of whether anything was flagged (proof-of-life so silence ≠ crash).

CLI:
    python3 tools/ticket_grooming.py                       # normal fire
    python3 tools/ticket_grooming.py --dry-run             # scan + report only
    python3 tools/ticket_grooming.py --no-digest           # commit but skip Telegram
    python3 tools/ticket_grooming.py --threshold-active 2h

Designed for daily cron at 12:00 UTC on `claude-fallback` (Recovery).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make `from tools._git_utils import ...` work both as a script
# (`python3 tools/ticket_grooming.py` per cron) and as a package import
# (`python3 -m unittest tools.test_ticket_grooming`). When invoked as a
# script, sys.path prepends `tools/` rather than the repo root, so the
# `tools` package isn't reachable; this bootstrap injects the repo root in
# that case. In package mode `__package__ == "tools"`, no-op.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._git_utils import (  # noqa: E402  — after sys.path bootstrap.
    _git,
    _hermes_scan_message,  # re-exported for tests (TestHermesGuardBelt)
    commit_scoped,
    pull_repo_or_warn,
    push_with_retry,
    with_worktree,
)
from tools.hermes_history_audit import run_history_audit  # noqa: E402
from tools.ticket import (  # noqa: E402
    TICKET_FILENAME_RE,
    TicketError,
    _build_ticket_text,
    _format_transition_entry,
    append_transition,
    parse_ticket,
)
from tools.watch_digest import read_telegram_token  # noqa: E402
# T-0976: reuse the dependency-graph structural detector (cycles + stale-done-deps)
# from ticket_lint. ticket_lint does NOT import ticket_grooming, so no import cycle.
# Folded into the DAILY grooming digest (which Recovery already sends as proof-of-life)
# so deadlock-class defects surface once daily, WITHOUT re-enabling the hourly
# ticket_lint Telegram that the operator suppressed (msg_1934 / --no-digest).
from tools.ticket_lint import detect_dep_graph_issues, detect_unanchored_gates  # noqa: E402
# T-2806: reuse adr_reeval_walk's hardened `---`-frontmatter YAML parser for
# build_disposition/scoping_ticket rather than re-implementing it -- inherits
# its T-2591 "warn on malformed YAML, never silently drop" behavior for free.
from tools.adr_reeval_walk import _load_yaml, _split_frontmatter  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TICKETS_ROOT = REPO_ROOT / "tickets"

# Module-level alias for with_worktree — allows unit tests to patch
# ticket_grooming._with_worktree to bypass the worktree shim without
# touching _git_utils internals. Production code calls this alias.
_with_worktree = with_worktree

STATES_TO_GROOM = ("active",)

# T-0693: closed-ticket acceptance check. Tickets that transitioned to done/
# within this window are re-checked for claimed-but-absent artifacts. 24h so
# the daily cron (one fire/day) always sees yesterday's closes exactly once.
ACCEPTANCE_LOOKBACK = timedelta(hours=24)

# T-0693: label stamped onto a ticket auto-reopened by the acceptance check.
# Doubles as the idempotency marker (mirrors latest_entry_is_grooming_stale):
# a ticket already carrying it is not re-processed.
ACCEPTANCE_UNMET_LABEL = "acceptance-unmet"

# Per ORG-003 §"Lifecycle" step 6: 4h for active.
DEFAULT_STALE_AFTER: dict[str, str] = {"active": "4h"}

# Telegram destination: same env-var convention as `tools/watch_digest.py`.
# Default prefers `pantheon.env` (per-bot for unattended Pantheon fires) over
# `.env` (the canonical MCP-plugin file the operator-Recovery chat uses), so a
# manual `python3 ticket_grooming.py` lands the digest on the same bot the
# systemd-cron'd fire uses — no surprise routing on operator-Recovery's chat.
def _default_telegram_env() -> Path:
    chan = Path.home() / ".claude" / "channels" / "telegram"
    pantheon = chan / "pantheon.env"
    return pantheon if pantheon.exists() else chan / ".env"


TELEGRAM_ENV = Path(
    os.environ.get("PANTHEON_TELEGRAM_ENV", str(_default_telegram_env()))
)
TELEGRAM_CHAT_ID = os.environ.get("PANTHEON_TELEGRAM_CHAT_ID", "456474197")  # the operator

GROOMING_ACTOR = "grooming"

_THRESHOLD_RE = re.compile(r"^(\d+)([hd])$")


# ---------------------------------------------------------------------------
# Threshold parser.
# ---------------------------------------------------------------------------


def parse_threshold(raw: str) -> timedelta:
    """Parse `<N>h` or `<N>d` into a timedelta.

    Strict format. Rejects: bare numbers, weeks (`Nw`), uppercase, whitespace,
    zero values (`0d`/`0h` are typos, not "always stale"). Raises `TicketError`
    on any failure so callers can surface lifecycle errors uniformly.
    """
    if not raw:
        raise TicketError("threshold: empty value")
    # Pre-check for weeks specifically so we can give a helpful message.
    if raw.endswith("w") and raw[:-1].isdigit():
        raise TicketError(f"threshold: weeks not supported in {raw!r}; use Nd")
    m = _THRESHOLD_RE.fullmatch(raw)
    if not m:
        raise TicketError(
            f"threshold: invalid format {raw!r}; expected <N>h or <N>d (lowercase, no spaces)"
        )
    n = int(m.group(1))
    unit = m.group(2)
    if n == 0:
        raise TicketError(f"threshold: must be positive, got {raw!r}")
    if unit == "h":
        return timedelta(hours=n)
    return timedelta(days=n)


def _format_age(delta: timedelta) -> str:
    """Render a timedelta as `<N>h` or `<N>d` for the digest. Days for >=24h,
    hours otherwise. Floor (so a 8h59m gap renders as `8h`).
    """
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        total_seconds = 0
    if total_seconds >= 86400:
        return f"{total_seconds // 86400}d"
    return f"{total_seconds // 3600}h"


# ---------------------------------------------------------------------------
# Time helpers (extracted so tests can patch `_now_utc` cleanly).
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    """UTC now, microsecond=0. Module-level so tests patch one symbol."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def _now_iso(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO 8601 ts; tolerate `Z` suffix. Returns None on parse error."""
    try:
        normalized = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Transition-log walking.
# ---------------------------------------------------------------------------

_TRANSITION_HEADER = "## Transition log"

# Bullet shape: "- <ts> [<actor>] <rest>" where <rest> starts with the verb
# and may include any of: " in inbox", "; moving to active", " (no activity
# since ...); flagging for review", etc. We deliberately don't try to parse
# verb-vs-detail delimiters — the verb-vs-rest convention is "first
# whitespace-separated token of <rest>", and the "; <detail>" form is
# rendered but not enforced (see ticket.py `_format_transition_entry` which
# emits with-or-without semicolon based on whether `detail` was provided).
_LOG_BULLET_RE = re.compile(
    r"^-\s+(?P<ts>\S+)\s+\[(?P<actor>[^\]]+)\]\s+(?P<rest>.+?)\s*$"
)


def _iter_transition_log_lines(body: str) -> list[str]:
    """Return the bullet lines under the body's `## Transition log` heading.

    Lines are returned in source order. Stops at the next `## ` heading or
    EOF. Returns `[]` if the heading is absent or the section is empty.
    Non-bullet lines (hand-edit noise) are filtered out.
    """
    lines = body.splitlines()
    out: list[str] = []
    in_section = False
    for ln in lines:
        if ln.strip() == _TRANSITION_HEADER:
            in_section = True
            continue
        if not in_section:
            continue
        if ln.startswith("## "):
            break
        if ln.startswith("- "):
            out.append(ln)
    return out


def _parse_log_entry(line: str) -> tuple[datetime, str] | None:
    """Parse one bullet line into (ts, actor) or return None on shape mismatch
    or unparseable timestamp.
    """
    m = _LOG_BULLET_RE.match(line)
    if not m:
        return None
    ts = _parse_iso(m.group("ts"))
    if ts is None:
        return None
    return ts, m.group("actor")


# ---------------------------------------------------------------------------
# T-3440 / ORG-089 §Decision.4: active-spend state machine over the transition
# log. v1 spend unit = time-in-active (§Decision.4). A closed two-state machine
# (interval open / closed); every recognized verb is a deterministic transition,
# every UNRECOGNIZED token is a NO-OP (fail-safe: an unknown verb can never
# corrupt the accounting or crash the breaker).
# ---------------------------------------------------------------------------

# The two re-decision verbs that RESTART the overrun clock (§Decision.4: re-earn
# and reopen "restart the overrun clock"; adjust-appetite does NOT — DECISION-6).
_SPEND_RESET_VERBS = frozenset({"re-earn", "reopen"})


def _extract_log_verb(rest: str) -> str:
    """From a bullet's post-`[actor]` remainder, extract the leading verb token —
    up to the first `@` (sha stamp), `;` (detail separator), or whitespace. A
    trailing `:` (hand-built `reopened:`-style bullets) is stripped by the caller."""
    m = re.match(r"\s*([^@;\s]+)", rest or "")
    return m.group(1) if m else ""


def _classify_spend_verb(actor: str, verb: str) -> str:
    """Classify one transition entry for the interval machine:
    'open' | 'close' | 'reset' | 'noop'.

    Handles BOTH the present-tense tokens the falsifier uses
    (claim/block/unblock/resolve) and the past-tense forms real logs carry via
    `_LOG_VERB` (claimed/blocked/resolved/unclaimed), plus the cascade's
    `actor='auto-unblock'` resume and the manual block-gate-cleared resume notes
    ('not_before gate cleared', 'depends_on gate(s) cleared'). Anything else —
    annotate/addendum/note/labels/stale/overrun-flag/adjust-appetite/filed — is a
    NO-OP. This heterogeneity is why v1 is time-in-active and honestly imperfect
    (ADR §Deferred: truer spend units replace it later); the fail-safe direction
    is absolute, so ambiguity resolves to NO-OP, never to a spurious interval."""
    a = (actor or "").strip().lower()
    v = (verb or "").strip().lower().rstrip(":")
    if v in _SPEND_RESET_VERBS:
        return "reset"
    # OPEN / resume active work.
    if v.startswith("claim"):                       # claim / claimed
        return "open"
    if "unblock" in v or a == "auto-unblock":       # unblock / unblocked / cascade
        return "open"
    if "cleared" in v and ("gate" in v or "depends_on" in v or "not_before" in v):
        return "open"                               # a block gate was cleared → resume
    # CLOSE / pause or terminate.
    if v.startswith("block"):                       # blocked (gate set → pause)
        return "close"
    if v.startswith("unclaim"):                     # unclaimed (back to inbox)
        return "close"
    if v.startswith("resolve"):                     # resolved (terminal)
        return "close"
    if v.startswith("drop"):                        # drop / dropped (terminal)
        return "close"
    return "noop"


def _active_spend_impl(body: str, now: datetime) -> tuple[int, bool]:
    """Return (active_spend_seconds, malformed_flag). Never raises.

    malformed_flag is set (and the offending transition SKIPPED) on an open verb
    while already open, a close verb while not open, a non-monotonic timestamp,
    or an unparseable ts — the overrun check reads the flag to DIGEST-FLAG the
    ticket and DECLINE to fire the breaker on it (fail-safe)."""
    open_since: datetime | None = None
    accum = 0.0
    malformed = False
    for line in _iter_transition_log_lines(body):
        m = _LOG_BULLET_RE.match(line)
        if not m:
            continue
        ts = _parse_iso(m.group("ts"))
        if ts is None:
            malformed = True
            continue
        cls = _classify_spend_verb(m.group("actor"), _extract_log_verb(m.group("rest")))
        if cls == "noop":
            continue
        if cls == "reset":
            accum = 0.0
            open_since = ts
        elif cls == "open":
            if open_since is not None:
                malformed = True          # open while already open
            open_since = ts
        elif cls == "close":
            if open_since is None:
                malformed = True          # close while not open
            else:
                delta = (ts - open_since).total_seconds()
                if delta >= 0:
                    accum += delta
                else:
                    malformed = True      # non-monotonic timestamps
                open_since = None
    # An in-flight (still-open) interval accrues to `now` — the whole point of
    # detecting overrun on a live ticket.
    if open_since is not None:
        delta = (now - open_since).total_seconds()
        if delta > 0:
            accum += delta
    return int(accum), malformed


def active_spend_from_transition_log(ticket, now: datetime | None = None) -> int:
    """Active-spend seconds for a ticket, over its transition-log verbs
    (ORG-089 §Decision.4, T-3440 Scope C). Closed state machine: intervals open
    on claim/unblock and reset on re-earn/reopen, close on block/unclaim/resolve/
    drop; annotations are no-ops; an in-flight interval accrues to `now`.
    Malformed order is skipped, never raised (fail-safe).

    `ticket` may be a parsed Ticket (has `.body`) or a raw body string."""
    if now is None:
        now = datetime.now(timezone.utc)
    body = getattr(ticket, "body", ticket)
    seconds, _malformed = _active_spend_impl(body, now)
    return seconds


# ---------------------------------------------------------------------------
# T-3440 / ORG-089 §Decision.4 + §Surface 4a: appetite calibration store,
# calibration policy, completeness check, and overrun check.
# ---------------------------------------------------------------------------

# The ORG-089 landing date — completeness grandfather boundary (D-H / DECISION-10)
# and the file-time legacy boundary. ADRs ratified before this are exempt.
_ORG089_LANDING = datetime(2026, 9, 6, tzinfo=timezone.utc)


def _appetite_ledger_root() -> Path:
    """The warm-up calibration store root (fleet-git-synced by construction,
    §Build plan). Evaluated at call time so a patched REPO_ROOT (tests) is
    honored, mirroring _default_adr_dirs."""
    return REPO_ROOT / "ledger" / "appetite"


def _iso_week_shard_name(now: datetime) -> str:
    """`<ISO-year>-W<NN>.jsonl` for `now` (zero-padded 2-digit ISO week, matching
    the sibling ledger/<year>-W<NN>/ dir convention; T-3440 Q6-confirmed the
    appetite sub-tree uses .jsonl FILES, not dirs)."""
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}.jsonl"


def _appetite_outcome_log(
    *,
    tid: str,
    appetite,
    outcome: str,
    now: datetime,
    active_spend_s: int | None = None,
    budget_s: int | None = None,
    ledger_root: Path | None = None,
) -> Path | None:
    """Append one terminal-transition event to the weekly appetite shard
    (ledger/appetite/<year>-W<NN>.jsonl). Fires on resolve->shipped /
    drop->dropped / reopen->reopened (T-3440 Scope C). Fail-safe: NEVER raises
    (calibration data, not a gate — §Build plan).

    Returns the shard Path on a successful write, None on any failure. T-3451:
    the caller (ticket.py's `_do_transition`) folds a non-None return into the
    SAME commit as the transition it fired from, so the event ships atomically
    instead of landing on disk uncommitted."""
    try:
        root = ledger_root or _appetite_ledger_root()
        root.mkdir(parents=True, exist_ok=True)
        shard = root / _iso_week_shard_name(now)
        event = {
            "ts": _now_iso(now),
            "tid": tid,
            "appetite": appetite,
            "outcome": outcome,
            "active_spend_s": active_spend_s,
            "budget_s": budget_s,
        }
        with shard.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
        return shard
    except Exception:  # noqa: BLE001 — outcome logging must never break a caller
        return None


@dataclass(frozen=True)
class AppetitePolicy:
    mode: str
    calibration_version: int
    bands: dict


def load_appetite_policy(policy_path: Path) -> "AppetitePolicy | None":
    """Load rulebook/policies/appetite-time-budget.yaml.

    Returns None — meaning the overrun check stays SILENT — when the file is
    absent OR unparseable OR mode != 'active' OR calibration_version == 0 (the
    warm-up generate-only state, §Decision.4 / D-D). Only a calibrated,
    active-mode policy yields a non-None AppetitePolicy. Fail-safe throughout.
    T-3430 is the writer that flips mode:active + calibration_version>0."""
    try:
        text = policy_path.read_text(encoding="utf-8")
    except OSError:
        return None
    data = _load_yaml(text, source=str(policy_path))
    if not isinstance(data, dict):
        return None
    mode = str(data.get("mode") or "").strip()
    try:
        cver = int(data.get("calibration_version") or 0)
    except (TypeError, ValueError):
        cver = 0
    bands = data.get("bands") if isinstance(data.get("bands"), dict) else {}
    if mode != "active" or cver == 0:
        return None
    return AppetitePolicy(mode=mode, calibration_version=cver, bands=bands)


@dataclass
class AppetiteCompletenessFinding:
    adr_id: str
    reason: str  # 'appetite-field' | 'appetite-section' | 'riskiest-section'


# Boilerplate/placeholder markers that mean a section body is not substantive.
_BOILERPLATE_MARKERS = ("TODO", "TBD", "FIXME", "<...>", "XXX", "PLACEHOLDER")
# The two required RECORD-mode section headers (case-insensitive prefix match).
_APPETITE_HEADER = "appetite"
_RISKIEST_HEADER = "riskiest assumption"


def _adr_ratify_date(status_text: str | None, fm: dict) -> datetime | None:
    """Best-effort ratify date for the grandfather test: a `Ratified YYYY-MM-DD`
    (or `Ratified YYYY-MM-DDTHH:MM...`) token in the status line, else the
    frontmatter `date:` field. None when neither parses."""
    for source in (status_text or "", str(fm.get("date") or "") if isinstance(fm, dict) else ""):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", source)
        if m:
            dt = _parse_iso(m.group(1))
            if dt is not None:
                return dt
    return None


def _adr_section_substantive(body: str, header_prefix: str) -> bool:
    """True iff the ADR body has a `## <header_prefix>...` section whose content
    is present and not boilerplate. False-negative-biased (a thin-but-real
    section is accepted) — the check is digest-only and must not false-flag a
    populated ADR."""
    lines = body.splitlines()
    collecting = False
    content: list[str] = []
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip().lower()
            if collecting:
                break  # next heading ends the section
            if heading.startswith(header_prefix):
                collecting = True
            continue
        if collecting:
            content.append(ln)
    if not collecting:
        return False  # section header absent
    text = "\n".join(content).strip()
    if len(text) < 12:
        return False  # empty / trivially short
    upper = text.upper()
    if any(marker in upper for marker in _BOILERPLATE_MARKERS):
        return False
    return True


def _appetite_completeness_check(
    adr_dirs: list[Path], tickets_root: Path, now: datetime
) -> list[AppetiteCompletenessFinding]:
    """ALWAYS-ON, DIGEST-ONLY (§Surface 4a-completeness). Flags each ratified
    NON-VAULT ADR ratified on/after the ORG-089 landing (grandfathered before)
    whose `appetite:` frontmatter is absent/non-int OR §Appetite body is
    missing/boilerplate OR §Riskiest assumption body is missing/boilerplate.
    Never mutates, never blocks, never raises on a single malformed ADR."""
    from tools.ticket_purpose import normalize_appetite, AppetiteError, is_vault_adr

    findings: list[AppetiteCompletenessFinding] = []
    for adr_dir in adr_dirs:
        try:
            if not adr_dir.exists():
                continue
        except OSError:
            continue
        for path in _iter_adr_files(adr_dir):
            m = _ADR_ID_RE.match(path.name)
            if not m:
                continue
            adr_id = f"{m.group(1)}-{m.group(2)}"
            if is_vault_adr(adr_id):
                continue  # operator-authored vault ADR — exempt
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            status_text = _adr_status_snippet(text)
            if status_text is None or not _is_ratified_status(status_text):
                continue
            fm = _adr_frontmatter(text, source=path.name)
            ratify_dt = _adr_ratify_date(status_text, fm)
            if ratify_dt is not None and ratify_dt < _ORG089_LANDING:
                continue  # legacy grandfathering by ratify-date (D-H)
            body = text.split("\n---", 1)[-1] if text.startswith("---") else text
            # appetite frontmatter present + int?
            try:
                appetite_ok = normalize_appetite(
                    fm.get("appetite") if isinstance(fm, dict) else None
                ) is not None
            except AppetiteError:
                appetite_ok = False
            if not appetite_ok:
                findings.append(AppetiteCompletenessFinding(adr_id, "appetite-field"))
            if not _adr_section_substantive(body, _APPETITE_HEADER):
                findings.append(AppetiteCompletenessFinding(adr_id, "appetite-section"))
            if not _adr_section_substantive(body, _RISKIEST_HEADER):
                findings.append(AppetiteCompletenessFinding(adr_id, "riskiest-section"))
    return findings


@dataclass
class AppetiteOverrunFinding:
    tid: str
    appetite: int
    spend_s: int
    budget_s: int
    claimant: str
    malformed: bool = False


def _claimant_of(ticket) -> str:
    """The claimant for the overrun advisory: the actor of the most recent
    `claim` transition, else the ticket `target` (DECISION-7)."""
    for line in reversed(_iter_transition_log_lines(ticket.body)):
        m = _LOG_BULLET_RE.match(line)
        if not m:
            continue
        verb = _extract_log_verb(m.group("rest")).lower().rstrip(":")
        if verb.startswith("claim"):
            return m.group("actor")
    return str(ticket.fields.get("target") or "")


def _appetite_band_budget(policy: "AppetitePolicy", appetite: int) -> int | None:
    """Look up the calibrated time-budget (seconds) for an appetite score, or
    None if the band is unmapped. Accepts either a direct per-score map or a
    band-keyed map; unmapped → None (fail-safe: no budget, no fire)."""
    bands = policy.bands or {}
    for key in (appetite, str(appetite)):
        if key in bands:
            try:
                return int(bands[key])
            except (TypeError, ValueError):
                return None
    return None


def _appetite_overrun_check(
    policy_path: Path,
    tickets_root: Path,
    now: datetime,
    *,
    apply: bool = True,
    send_fn=None,
) -> list[AppetiteOverrunFinding]:
    """§Decision.4 circuit breaker. CALIBRATION-GATED: returns [] (SILENT) when
    load_appetite_policy is None (file absent / generate-only / calibration_version
    0). In active mode: for each active/ ticket, compares active-spend to the
    calibrated band budget; on overrun (and not already flagged) writes
    `substate: appetite-overrun` in-place (when apply) + appends an overrun-flag
    transition + sends a best-effort mailbox advisory to claimant + orchestrator.

    Fail-safe: a malformed transition log / missing appetite / unmapped band /
    parse error DIGEST-FLAGS the ticket and DECLINES to fire — never blocks
    legitimate work, never raises."""
    policy = load_appetite_policy(policy_path)
    if policy is None:
        return []  # silent in warm-up / generate-only
    from tools.ticket_purpose import normalize_appetite, AppetiteError

    findings: list[AppetiteOverrunFinding] = []
    active_dir = tickets_root / "active"
    if not active_dir.exists():
        return findings
    for path in sorted(active_dir.iterdir()):
        if not path.is_file() or not TICKET_FILENAME_RE.fullmatch(path.name):
            continue
        try:
            ticket = parse_ticket(path)
        except TicketError:
            continue
        if ticket.fields.get("substate") == "appetite-overrun":
            continue  # already flagged — idempotent
        try:
            appetite = normalize_appetite(ticket.fields.get("appetite"))
        except AppetiteError:
            appetite = None
        if appetite is None:
            continue  # missing/corrupt appetite → fail-safe skip (never fire)
        budget_s = _appetite_band_budget(policy, appetite)
        if budget_s is None:
            continue  # unmapped band → no fire
        spend_s, malformed = _active_spend_impl(ticket.body, now)
        if malformed:
            # Data-corruption fail-safe: surface for the digest, never fire.
            findings.append(AppetiteOverrunFinding(
                tid=str(ticket.fields.get("id") or path.stem),
                appetite=appetite, spend_s=spend_s, budget_s=budget_s,
                claimant=_claimant_of(ticket), malformed=True,
            ))
            continue
        if spend_s <= budget_s:
            continue
        tid = str(ticket.fields.get("id") or path.stem)
        claimant = _claimant_of(ticket)
        finding = AppetiteOverrunFinding(
            tid=tid, appetite=appetite, spend_s=spend_s, budget_s=budget_s,
            claimant=claimant,
        )
        if apply:
            _apply_overrun_flag(path, ticket, now)
            _send_overrun_advisory(finding, send_fn=send_fn)
        findings.append(finding)
    return findings


def _apply_overrun_flag(path: Path, ticket, now: datetime) -> None:
    """Write `substate: appetite-overrun` in-place + append an overrun-flag
    transition. Best-effort; a write failure is swallowed (the breaker must not
    crash grooming). No git commit here — the grooming commit pass stages it, in
    keeping with the other in-place grooming mutations."""
    try:
        from tools.ticket import _build_ticket_text
        fields = dict(ticket.fields)
        fields["substate"] = "appetite-overrun"
        body = ticket.body
        if not body.endswith("\n"):
            body += "\n"
        entry = (f"- {_now_iso(now)} [{GROOMING_ACTOR}] overrun-flag: appetite "
                 f"budget exceeded; substate -> appetite-overrun (ORG-089 §Decision.4)")
        new_body = body + entry + "\n"
        path.write_text(_build_ticket_text(fields, new_body), encoding="utf-8")
    except Exception:  # noqa: BLE001 — surfacing is the goal, not crashing grooming
        pass


def _send_overrun_advisory(finding: "AppetiteOverrunFinding", *, send_fn=None) -> None:
    """Best-effort mailbox advisory to the claimant + orchestrator (soft-signal per
    D-M). Never raises. `send_fn` is injectable for tests; production resolves
    the mailbox hub + tools.mailbox.send."""
    try:
        from tools.keep_going import mailbox_inbox_for
        from tools import mailbox as _mailbox
        send = send_fn or _mailbox.send
        recipients = {finding.claimant, "orchestrator"} - {""}
        payload = (
            f"ORG-089 appetite circuit breaker: {finding.tid} has overrun its "
            f"appetite budget (appetite {finding.appetite}; active-spend "
            f"{finding.spend_s}s > budget {finding.budget_s}s). It is now "
            f"substate: appetite-overrun — claim/delivery/resolve are refused "
            f"until you re-earn / adjust-appetite / drop it (soft-signal advisory)."
        )
        for r in recipients:
            try:
                send(str(mailbox_inbox_for(r)), payload, sender="grooming",
                     subject=f"appetite-overrun {finding.tid}")
            except Exception:  # noqa: BLE001 — one bad recipient must not stop others
                pass
    except Exception:  # noqa: BLE001 — advisory is best-effort
        pass


def find_last_non_grooming_entry(body: str) -> tuple[datetime | None, str | None]:
    """Walk the body's transition log bottom-up; return `(ts, actor)` of the
    most recent entry whose actor is NOT `grooming`. Returns `(None, None)`
    if no such entry exists (no log section, empty section, only `[grooming]`
    entries, or all bullets unparseable).
    """
    bullets = _iter_transition_log_lines(body)
    for line in reversed(bullets):
        parsed = _parse_log_entry(line)
        if parsed is None:
            continue
        ts, actor = parsed
        if actor != GROOMING_ACTOR:
            return ts, actor
    return None, None


def latest_entry_is_grooming_stale(body: str) -> bool:
    """Return True iff the very last parseable bullet in the transition log
    has actor `grooming` AND the rest-of-line starts with the verb `stale`.
    Drives the append-idempotency check.
    """
    bullets = _iter_transition_log_lines(body)
    for line in reversed(bullets):
        m = _LOG_BULLET_RE.match(line)
        if not m:
            continue
        if m.group("actor") != GROOMING_ACTOR:
            return False
        # First whitespace-separated token of `rest` is the verb. Strip a
        # trailing `;` so "stale;" classifies as "stale".
        rest = m.group("rest").strip()
        first_token = rest.split(None, 1)[0] if rest else ""
        first_token = first_token.rstrip(";")
        return first_token == "stale"
    return False


# ---------------------------------------------------------------------------
# T-1586: stale-draft-without-operator-child check.
#
# WHY: a ticket held by an agent (orchestrator/specialist/recovery) can accumulate a dated
# "## <title> (<actor>, <date> — DRAFT/pre-decision/awaiting ...)" section —
# real decision work percolated but not yet surfaced as a ticket the operator can
# actually rule on — and then just sit (T-1371/ORG-038 sat 7 days, T-1478's
# fleet catalog 5 days; both found only by a manual sweep). This is warn-only
# (never blocks grooming, never auto-files) and read-only (no ticket mutation,
# no commit) — v1 surfaces the gap in the digest; a human/agent still files
# the actual ratify ticket.
#
# Scope is inbox/ + active/ (both), which is DELIBERATELY separate from
# STATES_TO_GROOM (= ("active",) only, driving the unrelated
# time-since-last-activity check above) — this check's candidates are
# routinely still in inbox/.
# ---------------------------------------------------------------------------

STALE_DRAFT_SCAN_STATES = ("inbox", "active")

# The label empirically present on all three real motivating tickets
# (T-1371, T-1478, T-1479). A ticket whose `target` is already "operator" is
# EXCLUDED by design — it already IS the operator-facing surface (no
# structural gap to close), so it's not a candidate regardless of label.
# Concretely regression-tested against a target=operator ticket that DOES
# carry an old dated header (T-1213-shaped) to confirm it's not flagged.
STALE_DRAFT_LABEL = "interactive-required"

# ~3 days per the T-1586 ask; both real cases (5d/7d) clear this comfortably.
STALE_DRAFT_THRESHOLD = timedelta(days=3)

# A qualifying header is a `## `-prefixed line carrying BOTH a date token and
# one of these marker words. Word-boundary + case-insensitive. Derived
# verbatim from the three real header lines:
#   T-1371: "## Percolation findings (orchestrator, 2026-06-29 — partial, pre-decision)"
#   T-1478: "## Re-eval result (orchestrator, 2026-07-01 — DRAFT proposal, awaiting
#            the operator's ratification; no threshold value changed)"
#   T-1479: "## Draft citation map (orchestrator, 2026-07-01 — DRAFT, awaiting
#            the operator's bless; NO rulebook edit landed)"
# `\bdraft\b` (not a bare substring) deliberately does NOT match "drafted"
# (past tense) — avoids false-positiving purely-descriptive headers like
# T-1213's "## Options analysis (drafted 2026-06-16, ...)".
_STALE_DRAFT_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_STALE_DRAFT_MARKER_RE = re.compile(r"\b(?:draft|pre-decision|awaiting)\b", re.IGNORECASE)


def _newest_draft_header_date(body: str) -> datetime | None:
    """Return the newest date found in a qualifying `## ` draft-marker header,
    or None if the body has no such header.

    A ticket can accumulate more than one draft-marker header over time (an
    old percolation note plus a fresher follow-up); the NEWEST date is the
    staleness anchor, so a recent follow-up resets the clock even if an older
    draft header is still present in the body.
    """
    newest: datetime | None = None
    for line in body.splitlines():
        if not line.lstrip().startswith("## "):
            continue
        if not _STALE_DRAFT_MARKER_RE.search(line):
            continue
        m = _STALE_DRAFT_DATE_RE.search(line)
        if not m:
            continue
        try:
            dt = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if newest is None or dt > newest:
            newest = dt
    return newest


@dataclass
class StaleDraftTicket:
    tid: str
    state: str
    target: str
    title: str
    header_date: datetime
    age: timedelta


def _has_open_operator_link(
    tid: str,
    fields: dict,
    operator_open: dict[str, str],
    by_id: dict[str, dict],
) -> bool:
    """True iff `tid` already has an operator-facing structural link.

    Two OR'd signals, each grounded in one of the two real fixes:
      (i) `tid`'s own `depends_on` names an id that resolves to a currently
          OPEN (inbox/active) ticket with `target: operator` — T-1478's real
          fix shape (`depends_on: [T-1584]`).
      (ii) some OTHER currently-open `target: operator` ticket's raw text
          (frontmatter + body) mentions `tid` as a whole token — T-1371's
          real fix shape (T-1582's Ask body names "T-1371" in prose, with no
          depends_on/parent_ticket back-reference on T-1371 itself).
    False-negative-biased: missing a link just re-surfaces a warn-only digest
    line, never a wrong mutation.
    """
    deps = fields.get("depends_on")
    if isinstance(deps, list):
        for dep_id in deps:
            dep_fields = by_id.get(str(dep_id))
            if dep_fields is not None and str(dep_fields.get("target", "")) == "operator":
                return True

    mention_re = re.compile(rf"\b{re.escape(tid)}\b")
    for other_tid, raw_text in operator_open.items():
        if other_tid == tid:
            continue
        if mention_re.search(raw_text):
            return True
    return False


def find_stale_draft_tickets(
    tickets_root: Path,
    now: datetime,
    threshold: timedelta = STALE_DRAFT_THRESHOLD,
) -> list["StaleDraftTicket"]:
    """Scan inbox/ + active/ for interactive-required tickets carrying a
    stale dated DRAFT/proposal/pre-decision header with no open operator-
    facing link. Read-only — no mutation, no commit. Sorted by tid for
    stable digest ordering.
    """
    entries: list[tuple[str, str, dict, str, str, str]] = []
    for state in STALE_DRAFT_SCAN_STATES:
        d = tickets_root / state
        if not d.exists():
            continue
        for path in sorted(d.iterdir()):
            if not (path.is_file() and TICKET_FILENAME_RE.fullmatch(path.name)):
                continue
            try:
                ticket = parse_ticket(path)
            except TicketError:
                continue
            tid = str(ticket.fields.get("id", path.stem))
            raw_text = path.read_text(encoding="utf-8")
            entries.append((tid, state, ticket.fields, ticket.body, ticket.title, raw_text))

    by_id = {tid: fields for tid, _state, fields, _body, _title, _raw in entries}
    operator_open = {
        tid: raw_text
        for tid, _state, fields, _body, _title, raw_text in entries
        if str(fields.get("target", "")) == "operator"
    }

    out: list[StaleDraftTicket] = []
    for tid, state, fields, body, title, _raw in entries:
        labels = fields.get("labels")
        if not (isinstance(labels, list) and STALE_DRAFT_LABEL in labels):
            continue  # condition (a)

        header_date = _newest_draft_header_date(body)
        if header_date is None:
            continue  # condition (b): no qualifying header at all
        age = now - header_date
        if age <= threshold:
            continue  # condition (b): not stale yet

        if _has_open_operator_link(tid, fields, operator_open, by_id):
            continue  # condition (c): already linked

        out.append(
            StaleDraftTicket(
                tid=tid,
                state=state,
                target=str(fields.get("target", "?")),
                title=title or "(no title)",
                header_date=header_date,
                age=age,
            )
        )
    out.sort(key=lambda f: f.tid)
    return out


# ---------------------------------------------------------------------------
# Flagged ticket record + scan.
# ---------------------------------------------------------------------------


@dataclass
class FlaggedTicket:
    path: Path
    tid: str
    state: str
    requester: str
    target: str
    title: str
    last_ts: datetime
    stale_age: timedelta


def _iter_grooming_targets() -> list[Path]:
    """Yield every `T-NNNN.md` under `tickets/active/`,
    sorted (id-stable). Skips `.gitkeep` and any non-matching filename.
    """
    out: list[Path] = []
    for state in STATES_TO_GROOM:
        d = TICKETS_ROOT / state
        if not d.exists():
            continue
        for entry in sorted(d.iterdir()):
            if not entry.is_file():
                continue
            if not TICKET_FILENAME_RE.fullmatch(entry.name):
                continue
            out.append(entry)
    return out


def append_grooming_stale(ticket_path: Path, last_ts: datetime, now: datetime) -> None:
    """Append a `[grooming] stale (no activity since <ts>); flagging for review`
    bullet to the ticket's transition log and rewrite the file in place.
    """
    ticket = parse_ticket(ticket_path)
    body = ticket.body
    detail = f"no activity since {_now_iso(last_ts)}; flagging for review"
    entry = _format_transition_entry(_now_iso(now), GROOMING_ACTOR, "stale", detail)
    if not body.endswith("\n"):
        body = body + "\n"
    new_body = body + entry + "\n"
    new_text = _build_ticket_text(ticket.fields, new_body)
    ticket_path.write_text(new_text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Auto-commit pipeline.
# ---------------------------------------------------------------------------


def _build_commit_message(today_iso: str, flagged: list[FlaggedTicket]) -> str:
    """Templated commit message. Subject + per-state bullets + trailer.

    Subject form: `tickets(grooming): flag <N> stale ticket(s) — <YYYY-MM-DD>`.
    """
    n = len(flagged)
    subject = f"tickets(grooming): flag {n} stale ticket(s) — {today_iso}"

    by_state: dict[str, list[FlaggedTicket]] = {s: [] for s in STATES_TO_GROOM}
    for f in flagged:
        if f.state in by_state:
            by_state[f.state].append(f)

    body_lines: list[str] = []
    for state in STATES_TO_GROOM:
        items = by_state[state]
        if not items:
            continue
        rendered = ", ".join(
            f"{f.tid} (stale {_format_age(f.stale_age)})" for f in items
        )
        body_lines.append(f"{state.capitalize()}: {rendered}")

    body_lines.append("")
    body_lines.append("Auto-committed by ticket_grooming.py (Recovery unattended).")
    body_lines.append("Scope: tickets/active/ only.")

    return subject + "\n\n" + "\n".join(body_lines) + "\n"


def git_commit_and_push(
    modified_paths: list[Path],
    commit_message: str,
    today_iso: str,
    *,
    cwd: "Path | None" = None,
) -> tuple[bool, str]:
    """Commit modified ticket files and push to origin/main.

    Order: per-file HERMES guard -> commit-message HERMES belt -> ff-only
    pull (skipped in worktree mode — worktree starts at origin/main HEAD) ->
    narrow `git add -- tickets/active/` -> strict-set scope assert ->
    empty-set short-circuit -> plain commit -> plain push.
    No force, no `--no-verify`.

    `cwd` is the working tree for git operations. None = REPO_ROOT (legacy
    path); pass worktree path for T-0471 isolated-worktree commits.

    `today_iso` is unused in the pipeline body but kept in the signature for
    parity with `watch_runner.git_commit_and_push` and to make the date
    available if future audit-trail enrichment wants it.
    """
    _cwd = cwd if cwd is not None else REPO_ROOT
    del today_iso  # currently unused; reserved for symmetry / future use
    del modified_paths  # HERMES file-guard now scans the actually-staged files in commit_scoped

    # Delegates to the shared commit_scoped primitive (T-1088 convergence).
    # allow_prefix: stage the tickets/active/ DIR and assert every staged path is
    # under it (the prior hardcoded scope guard). sync only in legacy/shared-tree
    # mode — a worktree already starts at origin/main HEAD. pull_or_rebase_strict
    # is a superset of the prior fetch+ff-merge (adds rebase-onto-origin fallback).
    return commit_scoped(
        ["tickets/active/"], commit_message,
        cwd=_cwd, push=True, sync=(cwd is None), allow_prefix=True,
    )


# ---------------------------------------------------------------------------
# T-0693: closed-ticket acceptance check.
#
# WHY: a ticket can be moved to done/ on a dispatch greenlight while its
# implementation never actually lands (the T-0148 mis-close that prompted this:
# greenlit, marked done, but the claimed migration script never existed in git
# and no shard was ever committed). This is a mechanical, heuristic-only
# back-stop: each fire, for tickets that closed in the last 24h, extract the
# concrete artifacts the ticket *claims* (file/script paths, git commit SHAs)
# and verify they exist on disk / in git. A ticket FAILS only if a claimed,
# mechanically-checkable artifact is MISSING. Prose-only acceptance (no
# checkable artifacts) is skipped silently — never flagged. On FAIL the ticket
# is auto-refiled done -> inbox with an `acceptance-unmet` label + a transition
# marker; the picking agent verifies before working (which absorbs heuristic
# false-positives: a legitimately-done ticket gets re-closed by the agent).
#
# DESIGN BIAS: conservative. A missed mis-close costs one undetected stale
# ticket; a false auto-reopen costs an agent's verification cycle + churn on a
# correctly-done ticket. So when in doubt, DON'T flag.
# ---------------------------------------------------------------------------

# Verbs (first whitespace token of a transition-log bullet's `rest`) that mark
# a ticket as having moved to done. `resolved` is the canonical close verb
# (ticket.py `_LOG_VERB["resolve"] == "resolved"`); `done` is tolerated for
# hand-edited logs that write the literal state.
_DONE_VERBS = ("resolved", "done")

# Path extraction: a repo-relative-looking path with a known source/doc/config
# extension. Anchored loosely; we post-filter illustrative/glob forms below.
# Examples matched: tools/foo.py, path/to/x.sh, architecture/adr/ORG-001-foo.md, a/b/c.yaml.
_KNOWN_PATH_EXTS = r"py|sh|md|yaml|yml|json|jsonl|toml|txt|tsv|cfg|ini|service|sql|js|ts|html|css"
# T-2361: the trailing (?![\w-]) boundary is the structural half of the fix and
# matters more than which extensions are listed. Without it, a listed extension
# matched a PREFIX of the real one — `ts` ⊂ `tsv`, `json` ⊂ `jsonl`, `sql` ⊂
# `sqlite` — so `data/x.tsv` was mined as `data/x.ts`, a path nobody claimed;
# _artifact_exists failed on it and the groomer false-reopened a correctly-done
# ticket with `acceptance-unmet`. With the boundary, a prefix match backtracks
# into the alternation (finding `tsv`/`jsonl`, now listed) or fails entirely —
# and an UNLISTED extension (.sqlite) simply stops matching, which lands on this
# module's stated side of doubt ("when in doubt, DON'T flag") instead of
# flagging with certainty on a phantom path. `-` is in the lookahead alongside
# \w so `tools/foo.py-backup` reads as not-a-claim rather than as `foo.py`.
_PATH_RE = re.compile(
    r"(?<![\w/.-])"                       # not preceded by a path-char (avoid mid-token)
    r"([A-Za-z0-9_][\w./-]*"              # first segment
    r"/[\w./-]+"                          # at least one slash + more
    rf"\.(?:{_KNOWN_PATH_EXTS}))"
    r"(?![\w-])"                          # T-2361: ext must END here, not prefix a longer one
)
# T-0973: a slash-joined filename LIST in prose (e.g. `repos.yaml/instance.yaml`,
# meaning two separate files) is matched by _PATH_RE as ONE bogus path whose first
# segment is itself a complete file. Split such a token at each `.<knownext>/`
# boundary so each real filename is verified separately — otherwise _artifact_exists
# fails on the non-existent joined path and the groomer FALSE-REOPENS a done ticket
# (it did exactly this to T-0882). A `/` NOT preceded by a known extension — i.e. a
# normal directory separator like `tools/foo.py` or `a/b/c.yaml` — is left intact,
# so legitimate multi-segment paths are unaffected. (Forward match, not a lookbehind:
# the extensions are variable-width and Python re forbids variable-width lookbehind.)
_EXT_SLASH_RE = re.compile(rf"\.(?:{_KNOWN_PATH_EXTS})/")


def _split_joined_filename_list(token: str) -> list[str]:
    """Split a `a.yaml/b.yaml` filename-list token into ['a.yaml', 'b.yaml'].
    A normal path (`tools/foo.py`, `a/b/c.yaml`) has no `.<ext>/` boundary, so it
    returns unchanged as a single-element list."""
    parts: list[str] = []
    start = 0
    for mm in _EXT_SLASH_RE.finditer(token):
        cut = mm.end() - 1            # the `/` itself
        parts.append(token[start:cut])
        start = mm.end()              # resume after the `/`
    parts.append(token[start:])
    return parts

# Commit SHA extraction: 7-40 hex chars, presented near a commit/sha cue. We
# require a contextual cue word so we don't treat arbitrary hex (e.g. a colour
# code, a hash digest in prose) as a commit reference. The SHA itself is the
# captured group.
_SHA_RE = re.compile(
    # Word-boundary on the bare-word cues (commit/sha/at) so prose tokens that
    # merely END in the cue ("combat 0000000", "format 7f3a9b1") don't extract
    # a spurious SHA → false reopen (T-0693 review finding). `@` is a non-word
    # char so it needs no \b.
    r"(?:\b(?:commit|sha|at)|landed (?:at|in)|@)\s+`?([0-9a-f]{7,40})`?\b",
    re.IGNORECASE,
)

# Landing-cue words: a path is treated as a CLAIMED-LANDED artifact only if one
# of these appears on the same line. This is the central false-positive guard
# (T-0693): the Ask/Context sections describe work to DO (and reference paths
# that may be cross-repo or not-yet-created); only claim text says what landed.
# Restricting extraction to landing-cue lines focuses on the closer's claim.
# Matched with word boundaries (NOT substring): the bare "at" cue as a substring
# matched any word ending in -at ("format tools/x.py", "that …") → false reopen
# (T-0693 review finding). \b-anchoring "at" requires it to be a standalone word.
_LANDING_CUE_RE = re.compile(
    r"\b(?:created|landed|added|shipped|wrote|writes to|commit(?:ted)?"
    r"|implemented in|at)\b",
    re.IGNORECASE,
)

# NEGATED landing cue (T-1709 false-reopen): a resolution that transparently
# says an artifact did NOT land ("hadn't landed on disk", "was never created",
# "did not ship") must NOT be mined as a landing CLAIM. The clause-negation
# guard misses this because the negation follows the path AND the past-negative
# contractions ("hadn't") aren't in the general negation set. This matches a
# negative auxiliary within a few words before a landing verb. Applied
# CLAUSE-scoped (T-1741), not line-scoped: only the path whose own clause
# contains the match is dropped, so a negated-landing phrase in one clause
# does not suppress an unrelated, legitimately-landed path co-claimed in an
# earlier clause on the same line ("landed tools/foo.py; no longer landed
# tools/old.py" keeps foo.py). Safe under the false-negatives-over-false-
# positives bias — a missed mis-close is cheaper than a false auto-reopen.
_NEGATED_LANDING_RE = re.compile(
    r"\b(?:not|never|no\s+longer|"
    r"(?:had|has|have|was|were|is|are|did|does|do|would|could|will|won)"
    r"(?:n['’]?t|\s+not))"
    r"\s+(?:\w+\s+){0,2}?"
    r"(?:created|landed|added|shipped|wrote|committed|implemented|ship|land|exist)",
    re.IGNORECASE,
)

# Negation markers used by `_path_is_negated_in_clause`. Compiled once at
# module load; matched as whole words (word-boundary anchored) so embedded
# substrings ("another", "notable") do not fire.
#
# `n't` is intentionally ABSENT: in real contractions ("isn't", "won't",
# "didn't") the `n` is preceded by a word character, so `\bn't\b` never
# matches. The covered phrasing `does\s+not` and `doesn['']?t` already
# handle all realistic closer forms.
#
# Unicode right-single-quote (') is tolerated alongside ASCII (') in
# "doesn't" because autocorrect on mobile occasionally substitutes it.
_NEGATION_MARKERS_RE = re.compile(
    r"\b(?:no|not|never|does\s+not|doesn['’]?t|no\s+longer)\b",
    re.IGNORECASE,
)

# Clause delimiters for `_path_is_negated_in_clause`. A clause boundary resets
# the look-back so a negation in a prior clause does NOT suppress a path in the
# current clause. Order matters only for readability; the regex finds the
# rightmost match in the prefix segment.
_CLAUSE_DELIMITERS_RE = re.compile(
    r",|;|:|(?<= )— |(?<= )—(?= )"  # comma, semicolon, colon, spaced em-dash
)

# Words/markers that make a path on a line illustrative/suggested rather than a
# hard landed-claim. A landing-cue line that ALSO carries one of these is
# dropped (suggestion wins — conservative).
_ILLUSTRATIVE_CUES = (
    "e.g.",
    "e.g ",
    "recommend",
    "suggest",
    "example",
    "for instance",
    "such as",
    "out of scope",
    "would live",
    "should live",
    "could live",
    "might live",
    "proposed",
    "tbd",
    " or ",   # "documented in X or Y" — a disjunctive suggestion, not a claim
    # T-0977: a path described as the UNAFFECTED / non-changed case is, by
    # definition, not a newly-landed deliverable. "normal dir-sep paths
    # (tools/foo.py) untouched" false-reopened T-0973. These words rarely
    # co-occur with a genuine "I created <this path>" assertion, so the
    # over-suppression risk is low (broader words like "normal"/"like" were
    # deliberately NOT added — too collision-prone; the parenthetical-path
    # predicate below carries the primary load).
    "untouched",
    "unchanged",
    "left intact",
    "left alone",
)

# Branch/PR markers: an artifact (SHA or path) on a line that also mentions a
# PR or feature branch is work-in-flight on a branch / in another repo, NOT
# necessarily present in the repo Recovery verifies against. Skip flagging artifacts
# on such lines (a PR-branch commit absent from main is merge-pending, not a
# mis-close — and `git cat-file -e` / a path check can't tell "on an unfetched
# branch / in another repo" from "never existed"). Applies to BOTH SHAs and
# paths (T-0683 dry-run: a `tools/install-hooks.sh` claimed in an echo-prismatic
# `PR #74` line collided with pantheon-config's own `tools/` dir).
_BRANCH_PR_MARKERS = (
    "pr #",
    "pr#",
    "branch",
    "agent/",
    "worktree",
)

# Other-repo markers (T-0697 class-3): a claim line that explicitly names a
# SIBLING repo describes an artifact in THAT repo, which this clone cannot
# verify. The top-level-dir guard below already skips paths whose top dir is
# absent here (e.g. echo-prismatic's `src-frontend/`), and _BRANCH_PR_MARKERS
# catches PR-tagged cross-repo lines — but a cross-repo path in a dir name
# SHARED by both repos (e.g. `tools/`) with no PR tag would still collide with
# this repo's own dir (T-0683 shape). Naming the sibling repo on the line is the
# reliable "resolve against the correct repo" signal. Extend this tuple when a
# new sibling repo joins the org. Path-only (SHAs are already never-flag per the
# class-1 fix in check_acceptance).
_OTHER_REPO_MARKERS = (
    "echo-prismatic",
    "revenue-project",
    # T-1489: siblings whose deliverables get referenced in pantheon-config
    # tickets. `claude-config` bit us concretely (T-1443 false-reopen): BOTH
    # repos have a top-level `pinned-rules/` dir, so the top-level-dir guard
    # below is fooled by the name collision (the dir exists here, but the
    # specific file lives in claude-config) — naming the sibling repo on the
    # line is the reliable signal. Added the other current siblings for parity.
    "claude-config",
    "pantheon-public",
    "pantheon-pwa",
)

# Ticket-file paths (`tickets/<state>/T-NNNN.md`) are MOBILE: a ticket file
# moves across state dirs (inbox -> active -> done) over its lifecycle, so a
# path claimed at close time routinely no longer exists at that exact location
# later (T-0667 dry-run: claimed `tickets/inbox/T-0678.md`, since moved out of
# inbox). These are managed by the ticket system, not deliverable artifacts in
# the acceptance sense — never treat them as claimed-created files.
_TICKET_PATH_RE = re.compile(r"^tickets/[a-z]+/T-\d{4,}\.md$")  # T-3215


def find_done_transition(body: str) -> datetime | None:
    """Return the timestamp of the most recent transition-log entry whose verb
    marks the ticket as moved-to-done (`resolved` or `done`), or None if no
    such entry exists / the log is malformed.

    The verb is the first whitespace-separated token of the bullet's `rest`
    group (the same convention `latest_entry_is_grooming_stale` uses). A
    trailing `;` is stripped so `resolved;` classifies as `resolved`. Walks
    bottom-up so the *most recent* close wins (a reopen-then-reclose history
    resolves to the latest close).
    """
    bullets = _iter_transition_log_lines(body)
    for line in reversed(bullets):
        m = _LOG_BULLET_RE.match(line)
        if not m:
            continue
        ts = _parse_iso(m.group("ts"))
        if ts is None:
            continue
        rest = m.group("rest").strip()
        first_token = rest.split(None, 1)[0] if rest else ""
        first_token = first_token.rstrip(";").lower()
        if first_token in _DONE_VERBS:
            return ts
    return None


def _paren_spans(line: str) -> list[tuple[int, int]]:
    """Return the `(open_idx, close_idx)` spans of every TOP-LEVEL parenthesised
    region in `line` (nested parens collapse into their outermost span).
    """
    spans: list[tuple[int, int]] = []
    depth = 0
    start = -1
    for i, ch in enumerate(line):
        if ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")" and depth > 0:
            depth -= 1
            if depth == 0:
                spans.append((start, i))
    return spans


def _span_in_parens(paren_spans: list[tuple[int, int]], lo: int, hi: int) -> bool:
    """True iff the `[lo, hi)` token span is strictly enclosed by one of the
    parenthesised spans (the `(` precedes `lo` and the `)` is at/after `hi`)."""
    return any(o < lo and hi <= c for (o, c) in paren_spans)


def _line_all_paths_parenthetical(line: str) -> bool:
    """True iff the line has ≥1 path-shaped token and EVERY such token sits
    inside a parenthesised span (T-0977).

    A path buried in parentheses on an otherwise-prose line is an aside / a
    category-example ("normal dir-sep paths (tools/foo.py) untouched"), not the
    asserted deliverable — closers name a real landed artifact inline. The
    "EVERY path parenthetical" scoping (not "any") is deliberate: a line that
    co-claims a real path inline PLUS a parenthetical aside still gets mined for
    the inline path, so genuine claims are never suppressed by this predicate.

    NOTE (T-0819): this LINE-level predicate only suppresses when *every* path
    is parenthetical. The per-path parenthetical guard in `extract_artifacts`
    handles the mixed case (a long re-resolve bullet that names inline paths
    AND parenthetical asides on the same line) — see the class-3 note there.
    """
    path_spans = [m.span() for m in _PATH_RE.finditer(line)]
    if not path_spans:
        return False
    paren_spans = _paren_spans(line)
    return all(_span_in_parens(paren_spans, lo, hi) for (lo, hi) in path_spans)


def _line_is_illustrative(line: str) -> bool:
    """True if a line's prose marks its paths as illustrative/suggested
    (example / recommendation / disjunction / parenthetical aside) rather than a
    hard landed-claim.
    """
    low = line.lower()
    if any(cue in low for cue in _ILLUSTRATIVE_CUES):
        return True
    # T-0977: a line whose ONLY path-shaped tokens are parenthetical asides.
    return _line_all_paths_parenthetical(line)


def _line_has_landing_cue(line: str) -> bool:
    """True if the line asserts an artifact LANDED (created/landed/added/at/...).

    Central false-positive guard: only landing-cue lines are mined for paths,
    so Ask/Context prose that merely *references* a path (work to do, a
    cross-repo file, a maybe-future home) is never treated as a claim.
    """
    return _LANDING_CUE_RE.search(line) is not None


def _line_mentions_branch_or_pr(line: str) -> bool:
    """True if the line references a PR or feature/agent branch — a SHA there
    is work-in-flight, not necessarily merged into the verified repo.
    """
    low = line.lower()
    return any(marker in low for marker in _BRANCH_PR_MARKERS)


def _line_mentions_other_repo(line: str) -> bool:
    """True if the line names a sibling repo (T-0697 class-3) — a path there
    lives in another repo this clone cannot verify, so don't flag it.
    """
    low = line.lower()
    return any(marker in low for marker in _OTHER_REPO_MARKERS)


def _path_is_negated_in_clause(line: str, path_start: int) -> bool:
    """True if the path token at `path_start` is preceded within its clause by
    a negation marker.

    'Clause' = the segment of `line` ending at `path_start`, bounded on the
    left by the nearest preceding clause delimiter (`,`, `;`, `:`, or a spaced
    em-dash ` — `).  A negation marker counts only if it falls INSIDE that
    segment — between the delimiter and the path.  Negation appearing AFTER
    `path_start` (in a later clause) is irrelevant and does NOT suppress.

    Word-boundary anchoring in `_NEGATION_MARKERS_RE` ensures that substrings
    such as "another" or "notable" — which contain `no` / `not` — do not fire.

    Conservative-drop bias: when a negation is found, return True so the
    caller drops the path.  This mirrors `_ILLUSTRATIVE_CUES` and the module-
    level "false-negatives over false-positives" mandate.
    """
    # The prefix is the portion of the line BEFORE the path token starts.
    prefix = line[:path_start]

    # Find the rightmost clause delimiter in the prefix; the clause segment
    # starts immediately after it (or from the start of the line if none).
    clause_start = 0
    for m in _CLAUSE_DELIMITERS_RE.finditer(prefix):
        clause_start = m.end()

    clause_segment = prefix[clause_start:]
    return _NEGATION_MARKERS_RE.search(clause_segment) is not None


def _clause_bounds(line: str, path_start: int, path_end: int) -> tuple[int, int]:
    """Return the (start, end) offsets of the clause containing the path
    token, bounded by the nearest `_CLAUSE_DELIMITERS_RE` matches before
    `path_start` and at-or-after `path_end` (or the line's edges if none).

    Unlike `_path_is_negated_in_clause` (prefix-only — negation must precede
    the path), this returns the FULL clause, including text after the path.
    Needed for the negated-landing check (T-1741): a negated landing verb can
    follow the path ("<path> hadn't landed on disk yet" — T-1737), so the
    scan must cover both sides of the path within its own clause.
    """
    start = 0
    for m in _CLAUSE_DELIMITERS_RE.finditer(line[:path_start]):
        start = m.end()
    end = len(line)
    m2 = _CLAUSE_DELIMITERS_RE.search(line, path_end)
    if m2:
        end = m2.start()
    return start, end


def _claim_region(body: str) -> str:
    """Return the body text where a closer asserts what landed.

    Scope = (a) the FINAL transition-log `resolved`/`done` bullet (the current
    close claim) + (b) any `## Acceptance` section. Deliberately EXCLUDES
    `## Ask` and `## Context`, which describe work-to-do and frequently
    reference cross-repo or not-yet-created paths — the dominant false-positive
    source (T-0693 dry-run found 3/4 false flags came from Ask/Context prose).

    T-0819: scope is the FINAL resolve bullet only, NOT every resolve bullet.
    In a reopen->re-resolve history the earlier bullets are SUPERSEDED narration
    — they accumulate long re-resolve notes that DISCUSS other tickets' paths
    (e.g. "T-0752 was also reopened (tools/ledger.py) — that one is Orchestrator's"),
    which the path extractor then mined as if they were THIS ticket's own
    deliverables (class-3 false positives, incl. the self-referential reopen of
    the path-fix tickets themselves). The current close is the closer's
    authoritative own-deliverable claim; aligns with `find_done_transition`,
    which likewise takes the most-recent close. Trade-off accepted under the
    module's false-negatives-over-false-positives mandate: a deliverable named
    ONLY in a superseded earlier bullet (and never restated at re-close) goes
    unchecked — rare, since re-resolves routinely restate the artifact.

    Falls back to the whole body only if neither region is found (defensive;
    in practice every done ticket has a resolved entry).
    """
    parts: list[str] = []

    # (a) the FINAL resolved/done transition bullet (the authoritative current
    # close claim); earlier resolve bullets are superseded narration — see WHY.
    final_resolve: str | None = None
    for line in _iter_transition_log_lines(body):
        m = _LOG_BULLET_RE.match(line)
        if not m:
            continue
        rest = m.group("rest").strip()
        first_token = rest.split(None, 1)[0] if rest else ""
        if first_token.rstrip(";").lower() in _DONE_VERBS:
            final_resolve = line
    if final_resolve is not None:
        parts.append(final_resolve)

    # (b) ## Acceptance section (if present).
    in_acc = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("## acceptance"):
            in_acc = True
            continue
        if in_acc:
            if stripped.startswith("## "):
                in_acc = False
                continue
            parts.append(line)

    if not parts:
        return body
    return "\n".join(parts)


def extract_artifacts(
    body: str, repo_root: "Path | None" = None
) -> list[tuple[str, str]]:
    """Extract concrete, mechanically-checkable artifacts a ticket CLAIMS-LANDED.

    Returns a list of `(artifact, kind)` where kind is `"path"` or `"sha"`.
    De-duplicated, source-order-stable.

    Extraction heuristic (deliberately conservative — see module-level WHY).
    All extraction runs over the CLAIM REGION only (`_claim_region`: resolved
    transition bullets + any `## Acceptance` section), never Ask/Context.

    Paths — a path is extracted iff ALL hold:
      - matches a repo-relative shape: ≥1 `/` + a known source/doc/config
        extension (`.py .sh .md .yaml ...`). Bare filenames (no `/`) are NOT
        extracted (too ambiguous — could be a noun).
      - the line carries a LANDING cue (`created`/`landed`/`added`/`at`/...) —
        the line asserts the artifact landed.
      - the line is NOT illustrative (`e.g.`/`recommend`/`X or Y`/...).
      - the line does NOT mention a PR / feature-branch / worktree (the path
        is then likely in another repo / on an unmerged branch — T-0683).
      - the path has no glob/placeholder (`*`, `<`, `>`, `...`).
      - the path is NOT a mobile ticket-file path (`tickets/<state>/T-NNNN.md`,
        which moves across state dirs over its lifecycle — T-0667).
      - (cross-repo guard) if `repo_root` is given, the path's top-level
        directory must EXIST under repo_root. A claimed path like
        `src-frontend/index.html` whose `src-frontend/` dir is absent is an
        artifact in another repo (echo-prismatic etc.) that this repo cannot
        verify — skip rather than false-flag.

    SHAs — extracted iff:
      - 7-40 hex chars preceded by a commit cue (`commit`/`sha`/`at`/`@`/
        `landed at/in`); AND
      - the line does NOT mention a PR / feature-branch / worktree (a commit
        on an unmerged branch is merge-pending, not a mis-close; `git cat-file`
        cannot distinguish "unfetched branch" from "never existed").

    Bias: false-negatives over false-positives (a missed mis-close is cheaper
    than a false auto-reopen — T-0693 dispatch mandate).
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []

    for raw_line in _claim_region(body).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        illustrative = _line_is_illustrative(line)
        has_landing = _line_has_landing_cue(line)
        branch_or_pr = _line_mentions_branch_or_pr(line)
        other_repo = _line_mentions_other_repo(line)

        # Paths — landing-cue gated, non-illustrative, not on a PR/branch line,
        # not naming a sibling repo, not a mobile ticket-file path, not cross-repo,
        # not a negated-landing statement (checked per-path below, T-1741).
        if (has_landing and not illustrative and not branch_or_pr
                and not other_repo):
            paren_spans = _paren_spans(line)
            for m in _PATH_RE.finditer(line):
                # T-0819 class-3 (path-granular parenthetical guard): a path
                # buried in a parenthetical aside is meta-commentary about
                # another ticket/agent ("note T-0752 was also reopened
                # (tools/ledger.py) — that one is Orchestrator's"), NOT the closer's
                # own asserted deliverable. The LINE-level
                # `_line_all_paths_parenthetical` guard only fires when EVERY
                # path is parenthetical, so a long re-resolve bullet that mixes
                # inline claims with parenthetical asides on one line leaks the
                # asides. Drop the parenthetical paths per-path here while still
                # mining genuine inline co-claims on the same line.
                if _span_in_parens(paren_spans, m.start(1), m.end(1)):
                    continue
                # T-0973: split a slash-joined filename list (`a.yaml/b.yaml`) into
                # its real files; a single normal path returns as a 1-element list.
                for path in _split_joined_filename_list(m.group(1)):
                    if any(c in path for c in ("*", "<", ">")) or "..." in path:
                        continue
                    # Clause-level negation guard (T-0769): if the path token is
                    # preceded within its clause by a negation marker ("NO", "not",
                    # "never", "does not", "doesn't", "no longer"), drop it.
                    # Clause-level (not line-level) so a negated path on the same
                    # line as a legitimate co-claimed artifact does NOT suppress the
                    # legitimate one.  Conservative-drop bias: when in doubt, drop.
                    if _path_is_negated_in_clause(line, m.start(1)):
                        continue
                    # Negated-landing guard (T-1709 origin, T-1741 clause-
                    # scoping fix): if the path's own clause contains a
                    # negated landing verb — before OR after the path
                    # ("X hadn't landed", "no longer landed X") — drop it.
                    # Clause-scoped, not line-scoped, so a negated-landing
                    # phrase in one clause does not suppress a legitimately
                    # landed path co-claimed in another clause on the same
                    # line ("landed tools/foo.py; no longer landed
                    # tools/old.py" keeps foo.py, drops old.py).
                    clause_start, clause_end = _clause_bounds(
                        line, m.start(1), m.end(1))
                    if _NEGATED_LANDING_RE.search(
                            line[clause_start:clause_end]) is not None:
                        continue
                    # Mobile ticket-file path — never a deliverable artifact.
                    if _TICKET_PATH_RE.match(path):
                        continue
                    # Cross-repo guard: top-level dir must exist in this repo.
                    if repo_root is not None:
                        top = path.split("/", 1)[0]
                        # A leading `<repo-name>/` is a self-reference to THIS repo
                        # (T-0697 class-2), not a cross-repo path — let it through so
                        # _artifact_exists can strip the prefix and verify the
                        # repo-relative remainder. Otherwise the top-level dir must
                        # exist here, else it's another repo's path we can't verify.
                        if top != repo_root.name and not (repo_root / top).exists():
                            continue
                    key = (path, "path")
                    if key not in seen:
                        seen.add(key)
                        out.append(key)

        # SHAs — cue-gated (in _SHA_RE) + not on a PR/branch line.
        if not branch_or_pr:
            for m in _SHA_RE.finditer(line):
                sha = m.group(1).lower()
                key = (sha, "sha")
                if key not in seen:
                    seen.add(key)
                    out.append(key)

    return out


# T-0817: a pre-T-0148 flat ledger path. ORG-001 sub-year sharding (T-0148)
# replaced the monolithic `ledger/<year>.jsonl` with `ledger/<year>-W<NN>/`
# weekly-shard dirs + a `<year>.jsonl.archive-pre-d10-shard` snapshot. A
# transition/doc reference to the OLD flat path is therefore NOT a missing
# deliverable once the migrated layout exists — the acceptance check was
# false-reopening tickets (T-0751, T-0778) that merely mention the old path.
# (`_PATH_RE` has no `jsonl` extension, so the prose `ledger/<year>.jsonl`
# extracts as `ledger/<year>.json` — the `jsonl?` below matches both forms.)
_LEDGER_FLATFILE_RE = re.compile(r"^ledger/(\d{4})\.jsonl?$")


def _ledger_flatfile_migrated(artifact: str, repo_root: Path) -> bool:
    """True iff `artifact` is a flat `ledger/<year>.json[l]` path whose content
    was migrated to the weekly-shard layout that now exists under `repo_root`.

    Migrated form present == any `ledger/<year>-W<NN>/` shard dir OR the
    `<year>.jsonl.archive-pre-d10-shard` snapshot. Year-specific by design: a
    genuinely-missing ledger (no shards, no archive for that year) still flags.
    """
    m = _LEDGER_FLATFILE_RE.match(artifact)
    if not m:
        return False
    year = m.group(1)
    ledger_dir = repo_root / "ledger"
    if not ledger_dir.is_dir():
        return False
    if (ledger_dir / f"{year}.jsonl.archive-pre-d10-shard").exists():
        return True
    shard_re = re.compile(rf"^{year}-W\d{{1,2}}$")
    return any(
        entry.is_dir() and shard_re.match(entry.name)
        for entry in ledger_dir.iterdir()
    )


def _artifact_exists(artifact: str, kind: str, repo_root: Path) -> bool:
    """Verify one extracted artifact.

    path -> exists on disk under `repo_root` (resolved relative to repo root).
    sha  -> `git cat-file -e <sha>^{commit}` succeeds (the object exists AND is
            a commit). Run with cwd=repo_root so it consults that repo's
            object store.
    """
    if kind == "path":
        candidate = (repo_root / artifact).resolve()
        if candidate.exists():
            return True
        # Class-2 (T-0697): repo-name-prefixed path. A closer sometimes writes
        # the artifact as `<repo-name>/<repo-relative>` (e.g.
        # `pantheon-config/README.md`) rather than repo-relative (`README.md`).
        # If the literal path is absent but stripping a leading `<repo-name>/`
        # prefix yields an existing file, accept it. The literal check runs
        # FIRST, so if a repo genuinely has a `<repo-name>/` subdir, a real file
        # under it still verifies literally before the strip fallback runs.
        prefix = repo_root.name + "/"
        if artifact.startswith(prefix):
            stripped = artifact[len(prefix):]
            if stripped and (repo_root / stripped).resolve().exists():
                return True
        # T-0817: a pre-T-0148 flat ledger path migrated to the weekly-shard
        # layout is satisfied by the migrated form, not a missing deliverable.
        if _ledger_flatfile_migrated(artifact, repo_root):
            return True
        return False
    if kind == "sha":
        res = _git("cat-file", "-e", f"{artifact}^{{commit}}", cwd=repo_root)
        return res.returncode == 0
    return True  # unknown kind — never flag (conservative)


def _path_relocated_in_git(artifact: str, repo_root: Path) -> bool:
    """True if `artifact` is missing on disk but has commit history in this repo
    — i.e. it once existed and was deleted or renamed away (RELOCATED), not
    never-created.

    T-0818: a closer's resolve note names a path that was later moved/renamed
    (and the old path deleted) — e.g. T-0751's `reports/PR/pantheon-bringup-guide.md`
    relocated to `sop/standard/pantheon-bringup.md` (commit d2d04c61). The
    deliverable IS live under a new name; the stale note's old path is missing.
    A path with ANY history (its last touch being the delete/rename commit) is
    therefore INDETERMINATE — mirrors the absent-SHA treatment (T-0697): never
    a reopen trigger. A path git has NEVER tracked is a genuine never-landed
    deliverable and still flags.

    `git log -n1 -- <path>` over HEAD's ancestry returns the path's last commit
    even after deletion (and the old name's history up to a rename), so it
    covers both delete and rename-away without `--follow`. In a non-git
    repo_root (test tmpdirs) `git log` fails → returns False → the path still
    flags (the conservative direction).
    """
    candidates = [artifact]
    prefix = repo_root.name + "/"
    if artifact.startswith(prefix):
        candidates.append(artifact[len(prefix):])  # T-0697 class-2 repo-prefixed form
    for cand in candidates:
        if not cand:
            continue
        res = _git("log", "-n", "1", "--format=%H", "--", cand, cwd=repo_root)
        if res.returncode == 0 and res.stdout.strip():
            return True
    return False


def check_acceptance(body: str, repo_root: Path) -> tuple[str, str] | None:
    """Mechanical acceptance check on a ticket body.

    Returns the FIRST `(artifact, kind)` that is claimed-but-missing, or None
    if every extracted artifact verifies (or there were no checkable artifacts
    at all — prose-only acceptance is a None / skip, never a flag).

    `repo_root` is threaded into extraction for the cross-repo top-level-dir
    guard AND used to verify each artifact.
    """
    for artifact, kind in extract_artifacts(body, repo_root):
        if _artifact_exists(artifact, kind, repo_root):
            continue
        # Class-1 (T-0697): a recorded commit SHA absent from THIS clone's
        # object store cannot be distinguished from a cherry-picked /
        # squash-merged / rebased / cross-clone commit — all produce or
        # relocate SHAs while the content still landed (T-0642: recorded branch
        # SHA 5ba1d52, cherry-picked to main as 1f63d46). Content-equivalence
        # against an object we don't possess is infeasible, so an absent SHA is
        # INDETERMINATE — never a reopen trigger. Only a missing PATH (a
        # concrete deliverable expected on disk) flags a mis-close.
        if kind == "sha":
            continue
        # Class-3 (T-0818): a missing PATH that git has history for was
        # RELOCATED/renamed/deleted, not never-created — INDETERMINATE, like an
        # absent SHA. Only a path git has never tracked is a genuine mis-close.
        if kind == "path" and _path_relocated_in_git(artifact, repo_root):
            continue
        return artifact, kind
    return None


@dataclass
class ReopenedTicket:
    """A ticket the acceptance check flagged + refiled done -> inbox this fire."""

    tid: str
    title: str
    missing_artifact: str
    missing_kind: str
    # Paths AFTER the refile (file now lives under tickets/inbox/).
    new_path: Path


def _has_acceptance_unmet_label(fields: dict) -> bool:
    """True if the ticket's frontmatter `labels` already carries the
    acceptance-unmet marker. Drives refile idempotency (mirrors
    `latest_entry_is_grooming_stale` for the stale path): a ticket already
    flagged is not re-processed.
    """
    labels = fields.get("labels")
    if not isinstance(labels, list):
        return False
    return ACCEPTANCE_UNMET_LABEL in labels


def refile_done_to_inbox(
    ticket_path: Path,
    missing_artifact: str,
    now: datetime,
    tickets_root: Path,
) -> Path:
    """Refile a mis-closed ticket done/ -> inbox/ with the acceptance-unmet flag.

    Mutations:
      - frontmatter `labels`: append `acceptance-unmet` (create the list if
        absent or null).
      - frontmatter `state`: set to `inbox`.
      - transition log: append a `[grooming] reopened: ...` bullet naming the
        missing artifact.
      - move the file from tickets/done/T-NNNN.md to tickets/inbox/T-NNNN.md.

    Returns the new (inbox) path. Writes the new content to the destination and
    removes the source so a subsequent `git add -- tickets/done/ tickets/inbox/`
    stages both the deletion and the addition.
    """
    ticket = parse_ticket(ticket_path)
    fields = dict(ticket.fields)

    # labels: append marker, creating the list if absent/null.
    labels = fields.get("labels")
    if not isinstance(labels, list):
        labels = []
    if ACCEPTANCE_UNMET_LABEL not in labels:
        labels = labels + [ACCEPTANCE_UNMET_LABEL]
    fields["labels"] = labels

    fields["state"] = "inbox"

    # Build the bullet directly (not via _format_transition_entry, which joins
    # verb + detail with "; "): the T-0693 spec fixes the literal marker form
    # `reopened: marked done ...` (colon) so the picking agent + any grep can
    # match `[grooming] reopened:` exactly.
    entry = (
        f"- {_now_iso(now)} [{GROOMING_ACTOR}] reopened: marked done but "
        f"acceptance-check found {missing_artifact} missing — verify before "
        "working; re-close if actually met"
    )
    body = ticket.body
    if not body.endswith("\n"):
        body = body + "\n"
    new_body = body + entry + "\n"
    new_text = _build_ticket_text(fields, new_body)

    inbox_dir = tickets_root / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    new_path = inbox_dir / ticket_path.name
    new_path.write_text(new_text, encoding="utf-8")
    ticket_path.unlink()
    return new_path


def _scan_done_for_acceptance(
    now: datetime,
    repo_root: Path,
    tickets_root: Path,
    *,
    apply_refile: bool,
) -> list[ReopenedTicket]:
    """Scan tickets/done/ for recently-closed tickets that fail acceptance.

    For each `T-NNNN.md` in tickets/done/:
      1. Skip if it already carries the acceptance-unmet label (idempotency).
      2. Skip if it did not transition to done within ACCEPTANCE_LOOKBACK.
      3. Run the mechanical acceptance check; skip if it passes / has no
         checkable artifacts.
      4. On FAIL: record a ReopenedTicket. If `apply_refile`, also refile the
         file done -> inbox (mutating frontmatter + appending the marker).

    `repo_root` is used to verify artifacts (path-exists / git cat-file).
    `tickets_root` is the tickets/ dir to scan + refile within (threaded so a
    worktree / tmp tree works without rebinding module globals — mirrors
    `_scan_state_dir`'s tickets_root param, T-0478).

    In dry-run callers pass `apply_refile=False`: the returned records describe
    what WOULD be reopened, with `new_path` pointing at the prospective inbox
    location (no file is touched).
    """
    done_dir = tickets_root / "done"
    out: list[ReopenedTicket] = []
    if not done_dir.exists():
        return out

    files = [
        e
        for e in sorted(done_dir.iterdir())
        if e.is_file() and TICKET_FILENAME_RE.fullmatch(e.name)
    ]
    for path in files:
        try:
            ticket = parse_ticket(path)
        except TicketError as e:
            print(f"warning: skipping malformed done ticket {path}: {e}", file=sys.stderr)
            continue

        # 1. Idempotency: already flagged -> skip.
        if _has_acceptance_unmet_label(ticket.fields):
            continue

        # 1b. T-3440 / ORG-089: a DROPPED bet is a terminal disposition, not a
        # completed deliverable — its acceptance surface must NOT be re-opened
        # (a dropped ticket was abandoned, not shipped; reopening it as
        # acceptance-unmet would falsely resurrect an intentionally-closed bet).
        if ticket.fields.get("substate") == "dropped":
            continue

        # 2. Closed within the lookback window?
        done_ts = find_done_transition(ticket.body)
        if done_ts is None:
            continue
        if now - done_ts > ACCEPTANCE_LOOKBACK:
            continue

        # 3. Mechanical acceptance check.
        miss = check_acceptance(ticket.body, repo_root)
        if miss is None:
            continue
        missing_artifact, missing_kind = miss

        tid = ticket.fields.get("id", path.stem)
        title = ticket.title or "(no title)"

        # 4. Refile (or, in dry-run, just compute the prospective path).
        if apply_refile:
            new_path = refile_done_to_inbox(
                path, missing_artifact, now, tickets_root
            )
        else:
            new_path = tickets_root / "inbox" / path.name

        out.append(
            ReopenedTicket(
                tid=tid,
                title=title,
                missing_artifact=missing_artifact,
                missing_kind=missing_kind,
                new_path=new_path,
            )
        )
    return out


def _build_reopen_commit_message(today_iso: str, reopened: list[ReopenedTicket]) -> str:
    """Templated commit message for the acceptance-refile commit.

    Subject: `tickets(grooming): reopen <N> mis-closed ticket(s) — <YYYY-MM-DD>`.
    Separate scope (tickets/done/ + tickets/inbox/) from the stale-flag commit.
    """
    n = len(reopened)
    subject = f"tickets(grooming): reopen {n} mis-closed ticket(s) — {today_iso}"
    body_lines = [
        f"{r.tid}: {r.missing_kind} {r.missing_artifact} claimed but missing"
        for r in reopened
    ]
    body_lines.append("")
    body_lines.append(
        "Auto-reopened by ticket_grooming.py acceptance-check (Recovery unattended)."
    )
    body_lines.append("Scope: tickets/done/ + tickets/inbox/ only.")
    return subject + "\n\n" + "\n".join(body_lines) + "\n"


def git_commit_and_push_reopen(
    modified_paths: list[Path],
    commit_message: str,
    *,
    cwd: "Path | None" = None,
) -> tuple[bool, str]:
    """Commit the acceptance-refile (done -> inbox moves) and push.

    SEPARATE-SCOPE sibling of `git_commit_and_push`: the refile spans
    `tickets/done/` (deletions) + `tickets/inbox/` (additions), which the
    stale-flag commit's hardcoded `tickets/active/` scope guard would refuse.
    This function keeps the identical discipline but asserts on the
    done+inbox scope instead:

      per-file HERMES guard -> commit-message HERMES belt -> ff-only sync
      (legacy/shared-tree path only) -> narrow `git add -- tickets/done/
      tickets/inbox/` -> strict-set scope readback (every staged path must be
      under tickets/done/ or tickets/inbox/) -> empty-set short-circuit ->
      plain commit -> push_with_retry. No force, no --no-verify.

    `cwd` is the working tree (None = REPO_ROOT legacy; worktree path otherwise).
    `modified_paths` are the post-refile inbox paths (HERMES-scanned); the
    matching done/ deletions are staged by the directory `git add`.
    """
    _cwd = cwd if cwd is not None else REPO_ROOT
    del modified_paths  # HERMES file-guard now scans the actually-staged files in commit_scoped

    # Delegates to commit_scoped (T-1088 convergence). SEPARATE-SCOPE: stages the
    # done/ + inbox/ dirs (captures the refile rename as delete + add) and asserts
    # every staged path is under one of them. sync only in legacy/shared-tree mode.
    return commit_scoped(
        ["tickets/done/", "tickets/inbox/"], commit_message,
        cwd=_cwd, push=True, sync=(cwd is None), allow_prefix=True,
    )


# ---------------------------------------------------------------------------
# Digest builder + plain-text Telegram sender.
# ---------------------------------------------------------------------------


def build_active_queue_snapshot(agents: list[str]) -> str:
    """Render a per-agent active-queue snapshot section for the grooming digest.

    For each persona in `agents`, counts tickets in `tickets/active/` whose
    target matches the persona name or its role ID (via agent_role_map). Reports
    total count and the oldest ticket (by `created` field) per agent.

    Read-only. Does NOT modify any ticket files.

    Returns a plain-text multi-line string ready to embed in the digest.
    """
    from tools.agent_role_map import AGENT_ROLE_MAP

    active_dir = TICKETS_ROOT / "active"
    # Collect all active tickets once; classify per-agent below.
    all_active: list[tuple[str, str, str, str]] = []  # (created, tid, title, target)
    if active_dir.exists():
        for path in sorted(active_dir.iterdir()):
            if not (path.is_file() and TICKET_FILENAME_RE.fullmatch(path.name)):
                continue
            try:
                ticket = parse_ticket(path)
            except TicketError:
                continue
            tid = ticket.fields.get("id", path.stem)
            created = ticket.fields.get("created", "")
            title = ticket.title or "(no title)"
            target = ticket.fields.get("target", "")
            all_active.append((created, tid, title, target))

    lines = ["Active queue snapshot:"]
    for persona in agents:
        role_id = AGENT_ROLE_MAP.get(persona, persona)
        agent_tickets = [
            (created, tid, title)
            for (created, tid, title, target) in all_active
            if target == persona or target == role_id
        ]
        n = len(agent_tickets)
        if n == 0:
            lines.append(f"  {persona}: 0 tickets active")
        else:
            # Sort by created ascending (oldest first), lower tid as tiebreak.
            agent_tickets.sort(key=lambda x: (x[0], x[1]))
            oldest_created, oldest_tid, oldest_title = agent_tickets[0]
            # Render created date as YYYY-MM-DD (first 10 chars of ISO ts).
            oldest_date = oldest_created[:10] if len(oldest_created) >= 10 else oldest_created
            ticket_word = "ticket" if n == 1 else "tickets"
            lines.append(
                f"  {persona}: {n} {ticket_word} active,"
                f" oldest created {oldest_date}"
                f" ({oldest_tid}: {oldest_title})"
            )
    return "\n".join(lines)


def _render_reopened_section(reopened: list["ReopenedTicket"]) -> str:
    """Render the acceptance-check auto-reopen section for the digest.

    Proof-of-visibility alongside the stale section: lists each ticket reopened
    this fire (tid + the missing artifact that triggered it). Empty list ->
    empty string (section omitted entirely).
    """
    if not reopened:
        return ""
    lines = [f"{len(reopened)} ticket(s) auto-reopened (acceptance unmet):"]
    for r in reopened:
        lines.append(
            f"  - {r.tid} ({r.missing_kind} {r.missing_artifact} claimed but "
            f"missing) - \"{r.title}\""
        )
    return "\n".join(lines)


def _render_stale_draft_section(stale_drafts: list["StaleDraftTicket"]) -> str:
    """Render the T-1586 stale-draft-without-operator-child section.

    Warn-only, read-only proof-of-visibility alongside the dep-graph and
    reopened sections. Empty list -> empty string (section omitted).
    """
    if not stale_drafts:
        return ""
    lines = [
        f"⚠️ Stale drafts awaiting an operator-facing ticket ({len(stale_drafts)}):"
    ]
    for d in stale_drafts:
        lines.append(
            f"  - {d.tid} ({d.state}, draft dated {d.header_date.date().isoformat()}, "
            f"{_format_age(d.age)} stale) - \"{d.title}\""
        )
    return "\n".join(lines)


def _render_dep_graph_section(findings: list) -> str:
    """Render the dependency-graph defects section for the digest (T-0976).

    Surfaces the serious structural defects that ticket_lint's detector finds
    (dependency cycles = deadlocks, and stale done-deps that cascade-unblock
    missed) ONCE DAILY here, because the hourly ticket_lint runs --no-digest
    (the operator msg_1934). Cycles are listed first (the deadlock class). Empty
    list -> empty string (section omitted).
    """
    if not findings:
        return ""
    cycles = [f for f in findings if getattr(f, "kind", None) == "cycle"]
    stale = [f for f in findings if getattr(f, "kind", None) == "stale_done_dep"]
    dropped = [f for f in findings if getattr(f, "kind", None) == "dropped_dep"]
    lines: list[str] = [
        f"⚠️ Dependency-graph defects ({len(findings)}) — run "
        "`ticket_lint --dry-run` for detail:"
    ]
    for f in cycles:
        lines.append(f"  - [CYCLE/deadlock] {f.detail}")
    for f in dropped:
        lines.append(f"  - [dropped-dep] {f.detail}")
    for f in stale:
        lines.append(f"  - [stale-done-dep] {f.detail}")
    return "\n".join(lines)


def _render_unanchored_gates_section(findings: list) -> str:
    """Render the T-2833 unanchored/bad date-gate section for the daily digest.

    Surfaces not_before date gates that fail the anchor rule (unanchored,
    malformed, or dangling-ref) so pre-existing/hand-edited arbitrary gates get
    migrated to a --depends-on dependency, an event-driven watch, or a real
    operator:<ref> anchor. Empty list -> empty string (section omitted)."""
    if not findings:
        return ""
    lines: list[str] = [
        f"📌 Unanchored/bad date gates ({len(findings)}) — a not_before must "
        "anchor to the operator or be a depends_on:"
    ]
    for f in findings:
        lines.append(f"  - [{getattr(f, 'kind', '?')}] {f.detail}")
    return "\n".join(lines)


def check_manifest_freshness(repo_root: Path, now: datetime,
                             max_age_days: int = 30) -> str | None:
    """T-1624: surface root-manifest staleness in the daily digest.

    T-1621 found instance.yaml/repos.yaml had drifted badly (wrong token_refs,
    dead repos marked live, missing repos) because nothing forces them current.
    This is surfacing-only — no auto-edit: if either manifest's mtime is older
    than `max_age_days`, return a one-line note for the digest; None when both
    are fresh. Fail-open: unreadable/missing files are reported as such, never
    raise (the digest must always send).
    """
    stale_bits: list[str] = []
    for name in ("instance.yaml", "repos.yaml"):
        try:
            mtime = datetime.fromtimestamp((repo_root / name).stat().st_mtime, tz=timezone.utc)
        except OSError:
            stale_bits.append(f"{name} MISSING/unreadable")
            continue
        age_days = (now - mtime).days
        if age_days >= max_age_days:
            stale_bits.append(f"{name} unreviewed {age_days}d")
    if not stale_bits:
        return None
    return ("\U0001F4C7 manifest freshness (T-1624): " + "; ".join(stale_bits)
            + " — review against live reality (see T-1621 for the drift class).")


# ---------------------------------------------------------------------------
# T-1814: ratified-but-unbuilt ADR surfacing.
#
# WHY: a ratified ADR whose carved build ticket stalls in inbox/active can sit
# silently while module docs + green tests advertise the fix as landed (the
# motivating case: ADR-239 was operator-ratified 2026-07-05 but its build
# ticket T-1602 sat unbuilt for weeks). Read-only, surfacing-only — no ticket
# mutation, no commit; folded into the daily digest alongside the dep-graph
# and stale-draft sections above.
# ---------------------------------------------------------------------------

# Filename recognition: this repo's own ADRs use `ORG-NNN-slug.md`; sibling
# repos (echo-prismatic) use `ADR-NNN — slug.md`. Both are "ADRs" in this
# org's convention (CLAUDE.md itself cites `architecture/adr/ORG-019-...md` as an ADR), so
# both prefixes are scanned uniformly.
_ADR_ID_RE = re.compile(r"^(ADR|ORG)-(\d+)")

# Status-line anchor: `^\*\*Status:` (deliberately NOT requiring the closing
# `**` immediately after the colon) tolerates BOTH bold-wrapping conventions
# observed in the live corpus:
#   (a) `**Status:** **Ratified ...**  Proposed (...)`  (ADR-239/ADR-232 shape)
#   (b) `**Status: RATIFIED ...** (trailing prose)`      (ORG-028/030/031/032/036 shape)
# The captured remainder is scanned for Ratified/exclusion words
# case-insensitively regardless of which convention produced it — a stray
# leading `**` in form (a)'s capture is cosmetic snippet noise, not a
# correctness issue. A ratified ADR silently not surfacing because of a known
# bold-wrapping variant is precisely the failure class this check exists to
# kill (T-1814 plan amendment).
_ADR_STATUS_LINE_RE = re.compile(r"^\*\*Status:\s*(.*)$", re.MULTILINE)

_ADR_RATIFIED_RE = re.compile(r"\bRatified\b", re.IGNORECASE)

# T-2825: alongside Superseded/Retired/Rejected, also excludes on a
# NOT-YET-ratified negation phrase in the first sentence -- "unratified" and
# "awaiting ... ratif[ication/y]" (e.g. "DRAFT awaiting the operator's
# ratification"). This catches ORG-065's real shape: a genuinely-unratified
# status line whose LATER clause happens to mention the operator ratifying an
# unrelated ticket ("...at the same time he ratified T-2169"), which the
# whole-line Ratified-anywhere-in-line scan otherwise treats as ratified.
# Deliberately NOT a bare-leading-word check (e.g. excluding on a status line
# that merely STARTS with "Proposed"/"Draft") -- ORG-019's real shape is
# "Drafted 2026-05-18 ... **Ratified 2026-05-19T00:58Z by the operator...**" and
# IS genuinely ratified; a leading-word exclusion would misclassify it (and
# every Drafted-then-Ratified ADR), silently hiding a real ORG-014-class gap.
# Corpus-verified against all 70 status-line-bearing architecture/adr/ files (T-2825): the
# only flip is ORG-065 True->False; ORG-019/020/021 (all "Drafted..." shape)
# and the other unratified drafts (ORG-062/063/064/067/069/070/071/072) are
# unchanged.
_ADR_EXCLUDE_STATUS_RE = re.compile(
    r"\b(?:Superseded|Retired|Rejected|Unratified)\b"
    r"|\bawaiting\b.*?\bratif\w*\b",
    re.IGNORECASE,
)

_ADR_SNIPPET_MAX = 100


def _default_adr_dirs() -> list[Path]:
    """Default ADR directories: this repo's `architecture/adr/` + echo-prismatic's.

    Evaluated at call time (not module load) so a `--repo-root` override
    (main()'s global REPO_ROOT reassignment) is reflected.
    """
    return [REPO_ROOT / "architecture" / "adr", Path("/home/claude/echo-prismatic/adr")]


def _iter_adr_files(adr_dir: Path) -> list[Path]:
    """Return `ADR-*.md` and `ORG-*.md` files directly under `adr_dir`, sorted."""
    files = list(adr_dir.glob("ADR-*.md")) + list(adr_dir.glob("ORG-*.md"))
    return sorted(files)


def _adr_status_snippet(text: str) -> str | None:
    """Return the status-line remainder (text after `**Status:`), or None if
    the file has no such line at all."""
    m = _ADR_STATUS_LINE_RE.search(text)
    if not m:
        return None
    return m.group(1).strip()


def _is_ratified_status(status_text: str) -> bool:
    """True iff `status_text` signals Ratified and is NOT excluded by a
    Superseded/Retired/Rejected marker in its OWN status sentence. Exclusion
    is checked first (locked design point 1: those words exclude even when
    Ratified also appears).

    Exclusion is scoped to the FIRST SENTENCE of the status line (up to the
    first `.` followed by whitespace), not the whole line: real-corpus
    testing against the T-1528/ADR-232 fixture found a genuinely-ratified
    status line whose LATER `**Related:**` metadata references an unrelated
    "PR #173 (superseded lever, do-not-merge)" — a whole-line exclusion scan
    false-excludes a ratified ADR because of a co-mentioned unrelated
    artifact. This mirrors the module's existing clause-scoped negation
    pattern (`_path_is_negated_in_clause` / `_NEGATED_LANDING_RE`, T-1741)
    rather than inventing a new convention. Ratified-detection stays
    whole-line per the locked design ("Ratified-anywhere-in-line is the
    signal") — only the exclusion scan is sentence-scoped.
    """
    first_sentence = re.split(r"(?<=\.)\s+", status_text, maxsplit=1)[0]
    if _ADR_EXCLUDE_STATUS_RE.search(first_sentence):
        return False
    return _ADR_RATIFIED_RE.search(status_text) is not None


def _truncate_snippet(text: str, limit: int = _ADR_SNIPPET_MAX) -> str:
    """Cap a status-line snippet for compact digest rendering (design point 4)."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


@dataclass
class RatifiedUnbuiltADR:
    adr_id: str
    status_snippet: str
    open_tickets: list[tuple[str, str]]  # (tid, state)


# T-2806: ADR frontmatter markers. `build_disposition` in {no-build-needed,
# built} exempts an ADR from the zero-ticket backstop (HO Q1 ruling
# 2026-08-10: both mean "not an open build gap" -- the field would not need
# a third state if `built` behaved identically to `needs-build`).
_ADR_EXEMPT_DISPOSITIONS = frozenset({"no-build-needed", "built"})

# Built-in prefix -> label-template fallback, used when adr-namespaces.yaml
# is absent or fails to parse (T-2806 design: this fully covers today's two
# namespaces, so a missing/broken config is not a data-loss coverage gap the
# way a missing ADR directory is -- see _load_adr_namespaces).
_DEFAULT_ADR_NAMESPACES: dict[str, str] = {"ORG": "org-{num}", "ADR": "adr-{num}"}
_ADR_NAMESPACES_CONFIG_FILENAME = "adr-namespaces.yaml"

# ORG and ADR prefixes always cross-accept BOTH org-NNN and adr-NNN as join
# labels regardless of what adr-namespaces.yaml configures (the operator TG 7593:
# "accept both during migration" off the pre-T-2806 adr-NNN-only convention).
# Accepted collision tradeoff during the migration period.
_ADR_MIGRATION_SHIM_PREFIXES = frozenset({"ORG", "ADR"})

def _adr_namespaces_config_path() -> Path:
    """adr-namespaces.yaml lives at repo root (T-2806) -- sibling of
    repos.yaml/instance.yaml, deliberately NOT under architecture/adr/ (D2-protected).
    Evaluated at call time (not module load), mirroring _default_adr_dirs,
    so a patched REPO_ROOT in tests is honored."""
    return REPO_ROOT / _ADR_NAMESPACES_CONFIG_FILENAME


def _load_adr_namespaces() -> dict[str, str]:
    """Load the ADR-prefix -> build-label-template map.

    Absent or unparseable config falls back SILENTLY to
    `_DEFAULT_ADR_NAMESPACES` -- unlike a missing ADR directory this loses no
    data, since the fallback already covers both namespaces in production
    use today; the config's only job is extending to a future third prefix.
    Malformed YAML still gets `_load_yaml`'s existing T-2591 stderr warning.
    """
    path = _adr_namespaces_config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return dict(_DEFAULT_ADR_NAMESPACES)
    data = _load_yaml(text, source=str(path))
    namespaces = data.get("namespaces") if isinstance(data, dict) else None
    if not isinstance(namespaces, dict):
        return dict(_DEFAULT_ADR_NAMESPACES)
    out = dict(_DEFAULT_ADR_NAMESPACES)
    for prefix, entry in namespaces.items():
        if not isinstance(entry, dict):
            continue
        template = entry.get("label_template")
        if isinstance(template, str) and template.strip():
            out[str(prefix)] = template.strip()
    return out


def _acceptable_build_labels(
    prefix: str, num: str, namespace_map: dict[str, str]
) -> set[str]:
    """The set of ticket labels that satisfy the build-ticket join for one
    ADR. Primary = the configured template for `prefix`. ORG/ADR
    additionally always cross-accept both `org-NNN` and `adr-NNN` regardless
    of config (the migration shim, see `_ADR_MIGRATION_SHIM_PREFIXES`).
    Falls back to `adr-{num}` if `prefix` has no config entry and isn't in
    the shim set (unrecognized-prefix safety net).
    """
    labels: set[str] = set()
    template = namespace_map.get(prefix)
    if template:
        labels.add(template.format(num=num))
    if prefix in _ADR_MIGRATION_SHIM_PREFIXES:
        labels.add(f"org-{num}")
        labels.add(f"adr-{num}")
    if not labels:
        labels.add(f"adr-{num}")
    return labels


def _adr_frontmatter(text: str, source: str = "?") -> dict:
    """Parse an ADR's `---`-delimited YAML frontmatter (build_disposition /
    scoping_ticket live here, T-2806). See the module-level import comment:
    reuses `tools.adr_reeval_walk`'s hardened parser rather than
    re-implementing it."""
    fm_block, _body = _split_frontmatter(text)
    return _load_yaml(fm_block, source=source)


def _adr_build_disposition(fm: dict) -> str | None:
    val = fm.get("build_disposition") if isinstance(fm, dict) else None
    return val.strip() if isinstance(val, str) and val.strip() else None


def _adr_scoping_ticket(fm: dict) -> str | None:
    val = fm.get("scoping_ticket") if isinstance(fm, dict) else None
    return val.strip() if isinstance(val, str) and val.strip() else None


def _scoping_ticket_state(tickets_root: Path, tid: str) -> str | None:
    """T-2826: 'done' / 'open' / None (absent) for a `scoping_ticket` tid, by
    exact `<tid>.md` membership under tickets/{done,inbox,active}. Read-only
    file-existence check, not a full `parse_ticket` -- only the open/done/
    absent distinction matters for the exemption below, and this mirrors
    `adr_scoping_lint.check_adr_frontmatter`'s own light-touch handling of
    the same field."""
    if (tickets_root / "done" / f"{tid}.md").exists():
        return "done"
    if (tickets_root / "inbox" / f"{tid}.md").exists():
        return "open"
    if (tickets_root / "active" / f"{tid}.md").exists():
        return "open"
    return None


def _build_label_index(tickets_root: Path) -> dict[str, list[tuple[str, str]]]:
    """One pass over tickets/{inbox,active,done}, indexing every ticket that
    carries `build`, keyed by EACH of its other labels -> [(tid, state), ...]
    (sorted by tid within each label). Indexing by every label (not just an
    `adr-NNN`/`org-NNN`-shaped subset) keeps this genuinely config-driven —
    a namespace config entry can name any label shape (T-2806 design point
    2's "a new prefix is a config add, not a code change" only holds if the
    index doesn't itself hardcode the shape it's willing to see).

    T-2806: the zero-ticket-blindness fix needs `done`-state coverage (a
    ratified ADR's only build ticket may already be closed), but
    tickets/done runs to ~2.6k files -- re-scanning it per-ADR (71 ADRs
    today) would be ~169k ticket-parses/run. This single pass amortizes the
    cost across the whole run instead: O(tickets) once, O(1) lookup per ADR
    -- fewer total parses than the pre-fix inbox+active-only per-ADR scan.
    """
    index: dict[str, list[tuple[str, str]]] = {}
    for state in ("inbox", "active", "done"):
        d = tickets_root / state
        if not d.exists():
            continue
        for path in sorted(d.iterdir()):
            if not (path.is_file() and TICKET_FILENAME_RE.fullmatch(path.name)):
                continue
            try:
                ticket = parse_ticket(path)
            except TicketError:
                continue
            labels = ticket.fields.get("labels")
            if not isinstance(labels, list) or "build" not in labels:
                continue
            tid = str(ticket.fields.get("id", path.stem))
            for label in labels:
                if isinstance(label, str):
                    index.setdefault(label, []).append((tid, state))
    for entries in index.values():
        entries.sort(key=lambda t: t[0])
    return index


def _find_build_tickets_for_adr(
    label_index: dict[str, list[tuple[str, str]]],
    prefix: str,
    num: str,
    namespace_map: dict[str, str],
) -> list[tuple[str, str]]:
    """Return every (tid, state) build ticket matching this ADR, across ALL
    states (inbox/active/done) — the caller splits out 'open' (non-done) for
    display. Sorted by tid, deduped by tid (a tid appearing under >1
    acceptable label — e.g. both the migration-shim and config-driven labels
    — counts once)."""
    seen: dict[str, str] = {}
    for label in _acceptable_build_labels(prefix, num, namespace_map):
        for tid, state in label_index.get(label, []):
            seen[tid] = state
    return sorted(seen.items())


def find_ratified_unbuilt_adrs(
    adr_dirs: list[Path], tickets_root: Path
) -> tuple[list[RatifiedUnbuiltADR], list[str]]:
    """Scan `adr_dirs` for ratified, needs-build ADRs lacking a build ticket.

    Read-only, no mutation, no commit (never routed through the commit path
    the staleness/acceptance checks use). Returns `(flagged, coverage_gaps)`:
      - `flagged`: one entry per ratified, needs-build ADR that EITHER has a
        stalled OPEN build ticket OR has ZERO build tickets in any state at
        all (T-2806: the latter is the ORG-014 class this check exists to
        catch — an ADR whose only build ticket is already `done` is NOT
        flagged; `build_disposition: no-build-needed`/`built` are exempt).
        Sorted by adr_id.
      - `coverage_gaps`: str(path) for every configured ADR dir absent on
        this host — always populated when absent, never a silent skip
        (design point 3). adr-namespaces.yaml being absent/malformed is NOT
        surfaced here — see `_load_adr_namespaces`, its fallback loses no
        data.
    """
    flagged: list[RatifiedUnbuiltADR] = []
    coverage_gaps: list[str] = []
    namespace_map = _load_adr_namespaces()
    label_index = _build_label_index(tickets_root)
    for adr_dir in adr_dirs:
        # Path.exists() re-raises EACCES (pathlib swallows only ENOENT-class
        # errors), and the cross-repo default dir lives under a 700/750-mode
        # sibling home this account may not traverse (T-1867: first live fire
        # crashed here). Unreadable-on-this-host is a coverage gap exactly
        # like absent-on-this-host — surfaced, never a crash.
        try:
            adr_dir_exists = adr_dir.exists()
        except OSError as exc:
            coverage_gaps.append(f"{adr_dir} (unreadable: {exc.strerror})")
            continue
        if not adr_dir_exists:
            coverage_gaps.append(str(adr_dir))
            continue
        for path in _iter_adr_files(adr_dir):
            m = _ADR_ID_RE.match(path.name)
            if not m:
                continue
            prefix, num = m.group(1), m.group(2)
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            status_text = _adr_status_snippet(text)
            if status_text is None or not _is_ratified_status(status_text):
                continue
            fm = _adr_frontmatter(text, source=path.name)
            if _adr_build_disposition(fm) in _ADR_EXEMPT_DISPOSITIONS:
                continue
            # T-2826: an OPEN scoping_ticket (inbox/active) already satisfies
            # the same tracked edge `adr_scoping_lint.check_adr_frontmatter`
            # accepts at creation time -- exempt symmetrically. A DONE or
            # missing-on-disk scoping ticket does NOT exempt: falls through
            # to the normal join so the flag re-arms if the scoping ticket
            # closed without producing build work (self-healing, not a
            # permanent mask).
            scoping_ticket = _adr_scoping_ticket(fm)
            if scoping_ticket and _scoping_ticket_state(tickets_root, scoping_ticket) == "open":
                continue
            all_tickets = _find_build_tickets_for_adr(
                label_index, prefix, num, namespace_map
            )
            open_tickets = [t for t in all_tickets if t[1] != "done"]
            if all_tickets and not open_tickets:
                # Every matching ticket is already done -> already built.
                continue
            flagged.append(
                RatifiedUnbuiltADR(
                    adr_id=f"{prefix}-{num}",
                    status_snippet=_truncate_snippet(status_text),
                    open_tickets=open_tickets,
                )
            )
    flagged.sort(key=lambda f: f.adr_id)
    return flagged, coverage_gaps


def _coverage_gap_suffix(gap: str) -> str:
    """'unavailable' for gaps carrying an '(unreadable: ...)' reason (T-1867),
    'absent' for plain missing-dir paths."""
    return "unavailable" if "(unreadable:" in gap else "absent"


def _render_ratified_unbuilt_adr_section(
    flagged: list["RatifiedUnbuiltADR"], coverage_gaps: list[str]
) -> str:
    """Render the T-1814 ratified-but-unbuilt-ADR section for the digest.

    Empty ONLY when both `flagged` and `coverage_gaps` are empty (mirrors the
    sibling sections' empty->omit convention) — a configured-but-absent ADR
    dir always renders its coverage-gap line, so absence is never silently
    dropped (design point 3). Fires every daily run while the condition
    holds — repetition is intended (standing signal, design point 4).
    """
    if not flagged and not coverage_gaps:
        return ""
    lines = [f"\U0001F4D0 Ratified ADRs with unbuilt build ticket(s) ({len(flagged)}):"]
    for f in flagged:
        if f.open_tickets:
            tickets_str = ", ".join(f"{tid} ({state})" for tid, state in f.open_tickets)
            lines.append(f"  - {f.adr_id} ({f.status_snippet}) - open: {tickets_str}")
        else:
            # T-2806: the zero-ticket case (the ORG-014 class) -- an explicit
            # message, not a misleading blank "open: " that reads as a bug.
            lines.append(
                f"  - {f.adr_id} ({f.status_snippet}) - NO BUILD TICKET FOUND (zero-ticket gap)"
            )
    for gap in coverage_gaps:
        lines.append(f"  coverage gap: {gap} {_coverage_gap_suffix(gap)} on this host")
    return "\n".join(lines)


def find_ticket_integrity_issues(tickets_root: Path) -> list[str]:
    """T-2982: read-only integrity scan for the daily digest. Surfaces two
    addressing/notification hazards that are otherwise only found by accident:

      (a) DUPLICATE IDs — the same T-NNNN in >1 state dir (a concurrent
          cross-agent allocation collision, T-2972 class). A dup id breaks the
          system's addressing: lookups, cross-refs, the auto-consumer.
      (b) SAME-not_before CLUSTERS — >=3 inbox tickets sharing an EXACT
          not_before. They all come due the instant that timestamp passes and
          THUNDER (the 2026-08-20 burst: 12 tickets at one timestamp -> ~9 notifs
          at once). Stagger them before they fire.

    Returns human-readable finding strings; empty list when clean. Fail-open:
    unreadable dirs/files are skipped, never raises (the digest must always send)."""
    import collections
    issues: list[str] = []
    # (a) duplicate IDs across all state dirs
    locs: dict[str, list[str]] = collections.defaultdict(list)
    for state in ("inbox", "active", "done", "blocked"):
        d = tickets_root / state
        if not d.is_dir():
            continue
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for f in entries:
            m = re.match(r"(T-\d+)\.md$", f.name)
            if m:
                locs[m.group(1)].append(state)
    for tid, states in sorted(locs.items()):
        if len(states) > 1:
            issues.append(
                f"[DUP-ID] {tid} appears in {len(states)} dirs ({', '.join(states)}) "
                f"— breaks ticket addressing; re-file one copy under a fresh id"
            )
    # (b) same-not_before clusters in inbox (>=3)
    by_nb: dict[str, list[str]] = collections.defaultdict(list)
    inbox = tickets_root / "inbox"
    if inbox.is_dir():
        try:
            entries = sorted(inbox.iterdir())
        except OSError:
            entries = []
        for f in entries:
            if not f.name.endswith(".md"):
                continue
            try:
                for ln in f.read_text(encoding="utf-8").splitlines():
                    if ln.startswith("not_before:"):
                        by_nb[ln.split(":", 1)[1].strip()].append(f.stem)
                        break
            except OSError:
                continue
    for nb, tids in sorted(by_nb.items()):
        if len(tids) >= 3:
            issues.append(
                f"[GATE-CLUSTER] {len(tids)} tickets share not_before={nb} — they all "
                f"come due at once (thundering-herd); stagger: {', '.join(sorted(tids))}"
            )
    return issues


def _render_integrity_section(issues: list[str]) -> str:
    """Render the T-2982 ticket-integrity section (dup-IDs + gate clusters).
    Empty list -> empty string (section omitted, per the sibling-section convention)."""
    if not issues:
        return ""
    lines = [f"🧮 Ticket-integrity issues ({len(issues)}):"]
    for issue in issues:
        lines.append(f"  - {issue}")
    return "\n".join(lines)


@dataclass
class DroppedTicket:
    tid: str
    title: str
    dropped_reason: str
    dropped_ts: datetime


def find_dropped_this_week(
    tickets_root: Path, now: datetime, window_days: int = 7
) -> list["DroppedTicket"]:
    """T-3440 / ORG-089 §4b: the distinct 'dropped this week' digest surface — a
    calibration signal, not noise. Scans tickets/done/ for `substate: dropped`
    tickets whose drop transition is within `window_days`. Read-only, fail-safe
    (a malformed ticket is skipped, never raised)."""
    out: list[DroppedTicket] = []
    done_dir = tickets_root / "done"
    if not done_dir.exists():
        return out
    for path in sorted(done_dir.iterdir()):
        if not path.is_file() or not TICKET_FILENAME_RE.fullmatch(path.name):
            continue
        try:
            t = parse_ticket(path)
        except TicketError:
            continue
        if t.fields.get("substate") != "dropped":
            continue
        drop_ts = None
        for line in reversed(_iter_transition_log_lines(t.body)):
            m = _LOG_BULLET_RE.match(line)
            if not m:
                continue
            if _extract_log_verb(m.group("rest")).lower().rstrip(":").startswith("drop"):
                drop_ts = _parse_iso(m.group("ts"))
                break
        if drop_ts is None or (now - drop_ts) > timedelta(days=window_days):
            continue
        out.append(DroppedTicket(
            tid=str(t.fields.get("id") or path.stem),
            title=t.title or "(no title)",
            dropped_reason=str(t.fields.get("dropped_reason") or ""),
            dropped_ts=drop_ts,
        ))
    return out


def _render_appetite_completeness_section(findings: list) -> str:
    """T-3440 / ORG-089 §4a-completeness digest section. Empty -> ''."""
    if not findings:
        return ""
    by_adr: dict[str, list[str]] = {}
    for f in findings:
        by_adr.setdefault(f.adr_id, []).append(f.reason)
    lines = [f"📐 ADRs missing appetite/riskiest fields ({len(by_adr)}) — ORG-089 RECORD-mode:"]
    for adr_id in sorted(by_adr):
        lines.append(f"  - {adr_id}: {', '.join(sorted(set(by_adr[adr_id])))}")
    return "\n".join(lines)


def _render_appetite_overrun_section(findings: list) -> str:
    """T-3440 / ORG-089 §4a-overrun digest section. Empty -> '' (and the whole
    section is absent in the generate-only warm-up, when the check returns [])."""
    if not findings:
        return ""
    lines = [f"⏱️ Appetite-budget overruns ({len(findings)}) — ORG-089 circuit breaker:"]
    for f in findings:
        flag = " (DATA-FLAG: malformed log, not fired)" if getattr(f, "malformed", False) else ""
        lines.append(
            f"  - {f.tid}: spend {f.spend_s}s > budget {f.budget_s}s "
            f"(appetite {f.appetite}, claimant {f.claimant}){flag}"
        )
    return "\n".join(lines)


def _render_dropped_this_week_section(findings: list) -> str:
    """T-3440 / ORG-089 §4b 'dropped this week' digest surface. Empty -> ''."""
    if not findings:
        return ""
    lines = [f"🗑️ Dropped this week ({len(findings)}) — ORG-089 calibration signal:"]
    for f in findings:
        lines.append(f"  - {f.tid} \"{f.title}\" — {f.dropped_reason}")
    return "\n".join(lines)


def build_digest(
    now_iso: str,
    flagged: list[FlaggedTicket],
    counts: dict[str, int],
    hermes_line: str | None = None,
    active_queue_section: str | None = None,
    reopened: list["ReopenedTicket"] | None = None,
    dep_graph_findings: list | None = None,
    unanchored_gate_findings: list | None = None,
    stale_draft_findings: list["StaleDraftTicket"] | None = None,
    manifest_line: str | None = None,
    ratified_unbuilt_findings: list["RatifiedUnbuiltADR"] | None = None,
    adr_coverage_gaps: list[str] | None = None,
    integrity_findings: list[str] | None = None,
    appetite_completeness_findings: list | None = None,
    appetite_overrun_findings: list | None = None,
    dropped_this_week: list | None = None,
) -> str:
    """Render the digest as plain text (no Markdown).

    `counts` maps state -> total ticket count under that state directory
    (regardless of staleness). Always includes the active state line for
    consistent shape across digest history.

    `hermes_line` is an optional line produced by `run_history_audit`; when
    provided it is appended at the end of the digest so every grooming fire
    includes the HERMES whole-history audit result as proof-of-discipline.

    Header convention (per T-0018): `<emoji> [<Agent> · <digest-type> · <date>]`.
    Same skeleton in `tools/watch_digest.py:build_digest` so a Telegram reader
    can pattern-match the sender within the first 1-2 words. This file is
    Recovery-emitted and uses 🧹 + plain text.
    """
    header = f"🧹 [Recovery · grooming · {now_iso}]"
    hermes_suffix = f"\n{hermes_line}" if hermes_line else ""
    reopened_section = _render_reopened_section(reopened or [])
    dep_graph_section = _render_dep_graph_section(dep_graph_findings or [])
    unanchored_gates_section = _render_unanchored_gates_section(unanchored_gate_findings or [])
    stale_draft_section = _render_stale_draft_section(stale_draft_findings or [])
    ratified_unbuilt_section = _render_ratified_unbuilt_adr_section(
        ratified_unbuilt_findings or [], adr_coverage_gaps or []
    )
    integrity_section = _render_integrity_section(integrity_findings or [])
    appetite_completeness_section = _render_appetite_completeness_section(
        appetite_completeness_findings or [])
    appetite_overrun_section = _render_appetite_overrun_section(
        appetite_overrun_findings or [])
    dropped_this_week_section = _render_dropped_this_week_section(dropped_this_week or [])
    _appetite_sections = "".join(
        "\n" + s + "\n" for s in (
            appetite_overrun_section, appetite_completeness_section, dropped_this_week_section
        ) if s
    )

    if not flagged:
        body = (
            f"{header}\n\n"
            "No stale tickets found.\n"
            f"Active: {counts.get('active', 0)} ticket(s). "
            f"All within their stale_after thresholds.\n"
        )
        if dep_graph_section:
            body += "\n" + dep_graph_section + "\n"
        if unanchored_gates_section:
            body += "\n" + unanchored_gates_section + "\n"
        if stale_draft_section:
            body += "\n" + stale_draft_section + "\n"
        if reopened_section:
            body += "\n" + reopened_section + "\n"
        if ratified_unbuilt_section:
            body += "\n" + ratified_unbuilt_section + "\n"
        if integrity_section:
            body += "\n" + integrity_section + "\n"
        body += _appetite_sections
        if active_queue_section:
            body += "\n" + active_queue_section + "\n"
        if manifest_line:
            body += "\n" + manifest_line + "\n"
        return body + hermes_suffix

    bullets: list[str] = []
    for f in flagged:
        age = _format_age(f.stale_age)
        bullets.append(
            f"  - {f.tid} ({f.state} {age} since last activity at {_now_iso(f.last_ts)}) "
            f"- {f.requester} -> {f.target} - \"{f.title}\""
        )

    total = counts.get("active", 0)
    parts = [
        header,
        "",
        f"{len(flagged)} stale ticket(s):",
        *bullets,
        "",
        (
            f"Active/ count: {counts.get('active', 0)}. "
            f"Total under grooming: {total}."
        ),
        "",
    ]
    result = "\n".join(parts)
    if dep_graph_section:
        result += "\n" + dep_graph_section + "\n"
    if unanchored_gates_section:
        result += "\n" + unanchored_gates_section + "\n"
    if stale_draft_section:
        result += "\n" + stale_draft_section + "\n"
    if reopened_section:
        result += "\n" + reopened_section + "\n"
    if ratified_unbuilt_section:
        result += "\n" + ratified_unbuilt_section + "\n"
    if integrity_section:
        result += "\n" + integrity_section + "\n"
    result += _appetite_sections
    if active_queue_section:
        result += "\n" + active_queue_section + "\n"
    if manifest_line:
        result += "\n" + manifest_line + "\n"
    if hermes_line:
        result += hermes_line + "\n"
    return result


def _send_telegram_plain(token: str, chat_id: str, text: str) -> bool:
    """Send a plain-text Telegram message (no `parse_mode`).

    Local 6-line sender rather than reusing `watch_digest.send_telegram`
    because that helper hard-codes `parse_mode: "Markdown"`. Plain-text
    matters here so unescaped underscores/asterisks/backticks in ticket
    titles don't render as broken Markdown. The duplication is accepted
    because the alternative (parameterizing `watch_digest.send_telegram`)
    would require editing a file outside this dispatch's scope.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as exc:  # noqa: BLE001
        print(f"telegram send failed: {exc}", file=sys.stderr)
        return False


# Telegram Bot API sendMessage has a 4096-char limit on `text`. Digest bodies
# routinely exceed this once the active-queue snapshot + HERMES audit line all
# accumulate (May 2026: ~8KB observed). Long bodies return HTTP 400, silently
# breaking the operator's proof-of-life signal. Chunk on paragraph boundaries below
# this cap.
_TELEGRAM_MAX_TEXT = 4000  # below 4096 to leave headroom for url-encoding/safety


def _chunk_for_telegram(text: str, limit: int = _TELEGRAM_MAX_TEXT) -> list[str]:
    """Split `text` into chunks <= limit chars, splitting at \\n\\n boundaries
    where possible. Single paragraphs longer than `limit` are hard-split at
    `limit` (uncommon path; preserves data over aesthetics).
    """
    if len(text) <= limit:
        return [text]
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = (current + "\n\n" + para) if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        # candidate exceeds limit. Flush current if non-empty, then handle para.
        if current:
            chunks.append(current)
            current = ""
        if len(para) <= limit:
            current = para
        else:
            # Single paragraph too long; hard-split at limit boundaries.
            for i in range(0, len(para), limit):
                chunks.append(para[i : i + limit])
            current = ""
    if current:
        chunks.append(current)
    return chunks


def _send_digest(text: str) -> bool:
    """Resolve token + send. No-token = warning + treat as success (parity
    with `watch_digest.py`'s no-token behavior; missing token shouldn't fail
    the whole run).

    Chunks long bodies to dodge Telegram's 4096-char sendMessage limit (the
    silent root cause of multi-day digest-send failures in May 2026).
    """
    token = read_telegram_token(TELEGRAM_ENV)
    if not token:
        print(
            f"warning: no Telegram token at {TELEGRAM_ENV}; skipping send",
            file=sys.stderr,
        )
        return True
    chunks = _chunk_for_telegram(text)
    all_ok = True
    for i, chunk in enumerate(chunks):
        if len(chunks) > 1:
            chunk = f"[part {i + 1}/{len(chunks)}]\n{chunk}"
        if not _send_telegram_plain(token, TELEGRAM_CHAT_ID, chunk):
            all_ok = False
    return all_ok


# ---------------------------------------------------------------------------
# Main scan loop.
# ---------------------------------------------------------------------------


def _scan_state_dir(
    state: str,
    now: datetime,
    threshold: timedelta,
    tickets_root: "Path | None" = None,
) -> tuple[list[FlaggedTicket], list[FlaggedTicket], int]:
    """Walk one state dir; classify each ticket.

    Returns `(to_append, already_flagged, total_count)`. `total_count` is the
    number of ticket files in the dir regardless of staleness — used for the
    digest's per-state counts. `to_append` need a new `[grooming] stale` line;
    `already_flagged` are stale but already-flagged (digest still mentions
    them).

    `tickets_root` overrides the module-global TICKETS_ROOT when provided.
    Pass the worktree path's tickets/ dir from inside with_worktree callbacks
    (T-0478: do not rebind module-global TICKETS_ROOT from inside a worktree
    context; thread the path instead).
    """
    to_append: list[FlaggedTicket] = []
    already: list[FlaggedTicket] = []

    _tickets_root = tickets_root if tickets_root is not None else TICKETS_ROOT
    d = _tickets_root / state
    if not d.exists():
        return to_append, already, 0

    files = [
        e
        for e in sorted(d.iterdir())
        if e.is_file() and TICKET_FILENAME_RE.fullmatch(e.name)
    ]
    for path in files:
        try:
            ticket = parse_ticket(path)
        except TicketError as e:
            print(f"warning: skipping malformed ticket {path}: {e}", file=sys.stderr)
            continue

        # Per-ticket override on threshold.
        per_ticket_raw = ticket.fields.get("stale_after")
        try:
            per_ticket = (
                parse_threshold(per_ticket_raw) if per_ticket_raw else threshold
            )
        except TicketError as e:
            print(
                f"warning: ticket {path.name} has invalid stale_after; using default ({e})",
                file=sys.stderr,
            )
            per_ticket = threshold

        last_ts, _last_actor = find_last_non_grooming_entry(ticket.body)
        if last_ts is None:
            # No non-grooming entry — skip (malformed or only-grooming case).
            continue

        age = now - last_ts
        if age <= per_ticket:
            continue

        flagged = FlaggedTicket(
            path=path,
            tid=ticket.fields.get("id", path.stem),
            state=state,
            requester=str(ticket.fields.get("requester", "?")),
            target=str(ticket.fields.get("target", "?")),
            title=ticket.title or "(no title)",
            last_ts=last_ts,
            stale_age=age,
        )
        if latest_entry_is_grooming_stale(ticket.body):
            already.append(flagged)
        else:
            to_append.append(flagged)

    return to_append, already, len(files)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ticket_grooming.py",
        description=(
            "Pantheon ticket-grooming staleness scanner. Default-safe contract "
            "(T-0260 per sop/standard/auto-commit-cron-default-safe-contract.md): "
            "must pass --commit-prod, --dry-run, or --repo-root <path> explicitly."
        ),
    )
    p.add_argument(
        "--commit-prod", action="store_true",
        help="real run: scan, append, commit + push to production. systemd cron sets this; smoke runs MUST NOT.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="scan + report what would be flagged; no side effects (no append, no commit, no push, no digest)",
    )
    p.add_argument(
        "--repo-root", type=Path, default=None,
        help="override REPO_ROOT (and TICKETS_ROOT = REPO_ROOT/tickets). Tmp-target for integration tests; safe for smoke.",
    )
    p.add_argument(
        "--no-digest",
        action="store_true",
        help="append + commit + push but skip the Telegram send (modifier; pair with --commit-prod or --repo-root)",
    )
    p.add_argument(
        "--threshold-active",
        default=None,
        help=f"override active-state default ({DEFAULT_STALE_AFTER['active']}); format <N>h or <N>d",
    )
    p.add_argument(
        "--adr-dirs",
        default=None,
        help=(
            "comma-separated ADR directories to scan for ratified-but-unbuilt "
            "ADRs (T-1814); default: <repo>/architecture/adr, /home/claude/echo-prismatic/adr"
        ),
    )
    return p.parse_args(argv)


def _enforce_default_safe(args: argparse.Namespace) -> int | None:
    """Refuse to run if neither a prod nor a safe mode is explicitly chosen.

    Default-safe contract (T-0260 per the auto-commit-cron SOP): avoid silent
    prod-target. Caller must opt in via --commit-prod, OR opt out via --dry-run
    / --repo-root <path>. Mixing --commit-prod with a safe-mode flag is
    ambiguous → refuse rather than guess.

    Returns exit code (64) if the invocation should be rejected; None otherwise.
    """
    safe_explicit = args.dry_run or (args.repo_root is not None)
    if args.commit_prod and safe_explicit:
        print(
            "ERROR: --commit-prod conflicts with safe-mode flag (--dry-run / --repo-root override).",
            file=sys.stderr,
        )
        return 64
    if not args.commit_prod and not safe_explicit:
        print(
            "ERROR: refusing to run without explicit mode. Pass one of:\n"
            "  --commit-prod        commit + push to production (systemd cron path)\n"
            "  --dry-run            scan + report only; no side effects\n"
            "  --repo-root <path>   redirect to a tmp pantheon-config dir (smoke path)\n"
            "Per sop/standard/auto-commit-cron-default-safe-contract.md (T-0260).",
            file=sys.stderr,
        )
        return 64
    return None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Default-safe gate (T-0260): refuse silent prod-target.
    refusal = _enforce_default_safe(args)
    if refusal is not None:
        return refusal

    # --repo-root override (tmp-target for integration smoke). Reassign module
    # constants so all downstream functions (scan, retarget, commit) operate on
    # the tmp pantheon-config equivalent rather than the real /home/<user>/...
    if args.repo_root is not None:
        global REPO_ROOT, TICKETS_ROOT
        REPO_ROOT = args.repo_root.resolve()
        TICKETS_ROOT = REPO_ROOT / "tickets"

    now = _now_utc()
    today_iso = now.date().isoformat()
    now_human = now.strftime("%Y-%m-%d %H:%M UTC")

    # Resolve thresholds (CLI override or per-state default).
    try:
        thresholds = {
            "active": parse_threshold(
                args.threshold_active or DEFAULT_STALE_AFTER["active"]
            ),
        }
    except TicketError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # T-1814: resolve ADR dirs (CLI override or default), after any
    # --repo-root reassignment above so the default picks up the right REPO_ROOT.
    if args.adr_dirs:
        adr_dirs = [Path(p.strip()) for p in args.adr_dirs.split(",") if p.strip()]
    else:
        adr_dirs = _default_adr_dirs()

    # --dry-run: scan current state (shared tree) + report; no writes.
    if args.dry_run:
        to_append_dry: list[FlaggedTicket] = []
        already_dry: list[FlaggedTicket] = []
        counts_dry: dict[str, int] = {}
        for state in STATES_TO_GROOM:
            a, b, total = _scan_state_dir(state, now, thresholds[state])
            to_append_dry.extend(a)
            already_dry.extend(b)
            counts_dry[state] = total
        all_stale_dry = to_append_dry + already_dry
        # T-0693: closed-ticket acceptance check (report only; apply_refile=False).
        reopened_dry = _scan_done_for_acceptance(
            now, REPO_ROOT, TICKETS_ROOT, apply_refile=False
        )
        print(f"🧹 [Recovery · grooming · {now_human}] (dry-run)")
        if not all_stale_dry:
            print(f"  no stale tickets. active={counts_dry.get('active', 0)}.")
        else:
            print(f"  would flag {len(to_append_dry)} ticket(s) (new), "
                  f"{len(already_dry)} already-flagged):")
            for f in all_stale_dry:
                tag = "NEW" if f in to_append_dry else "already-flagged"
                print(
                    f"    [{tag}] {f.tid} ({f.state}, stale {_format_age(f.stale_age)}) "
                    f"{f.requester} -> {f.target}"
                )
        if not reopened_dry:
            print("  no mis-closed tickets (acceptance check).")
        else:
            print(f"  would reopen {len(reopened_dry)} mis-closed ticket(s):")
            for r in reopened_dry:
                print(
                    f"    [REOPEN] {r.tid} ({r.missing_kind} {r.missing_artifact} "
                    f"claimed but missing)"
                )
        dep_graph_dry = detect_dep_graph_issues(TICKETS_ROOT)
        if not dep_graph_dry:
            print("  no dependency-graph defects (cycles / stale-done-deps).")
        else:
            print(f"  dependency-graph defects ({len(dep_graph_dry)}):")
            for f in dep_graph_dry:
                print(f"    [{f.kind}] {f.detail}")
        stale_draft_dry = find_stale_draft_tickets(TICKETS_ROOT, now)
        if not stale_draft_dry:
            print("  no stale interactive-required drafts without an operator-facing ticket.")
        else:
            print(
                f"  {len(stale_draft_dry)} stale draft(s) without an operator-facing ticket:"
            )
            for d in stale_draft_dry:
                print(
                    f"    [STALE-DRAFT] {d.tid} ({d.state}, {_format_age(d.age)} since "
                    f"draft dated {d.header_date.date().isoformat()})"
                )
        ratified_unbuilt_dry, adr_gaps_dry = find_ratified_unbuilt_adrs(adr_dirs, TICKETS_ROOT)
        if not ratified_unbuilt_dry and not adr_gaps_dry:
            print("  no ratified-but-unbuilt ADRs found.")
        else:
            print(
                f"  ratified-but-unbuilt ADR(s) ({len(ratified_unbuilt_dry)}):"
            )
            for r in ratified_unbuilt_dry:
                if r.open_tickets:
                    tickets_str = ", ".join(f"{tid} ({state})" for tid, state in r.open_tickets)
                    print(f"    [ADR-UNBUILT] {r.adr_id} ({r.status_snippet}) - open: {tickets_str}")
                else:
                    print(
                        f"    [ADR-UNBUILT] {r.adr_id} ({r.status_snippet}) - "
                        "NO BUILD TICKET FOUND (zero-ticket gap)"
                    )
            for gap in adr_gaps_dry:
                print(
                    f"    [COVERAGE-GAP] {gap} "
                    f"{_coverage_gap_suffix(gap)} on this host"
                )
        integrity_dry = find_ticket_integrity_issues(TICKETS_ROOT)  # T-2982
        if not integrity_dry:
            print("  no ticket-integrity issues (dup-id / gate-cluster).")
        else:
            print(f"  ticket-integrity issue(s) ({len(integrity_dry)}):")
            for issue in integrity_dry:
                print(f"    {issue}")
        return 0

    # T-0471: per-cron git worktree isolation.
    # In production (--commit-prod, no --repo-root): wrap the scan+append+commit
    # block in with_worktree so each fire runs in an isolated /tmp/ worktree off
    # origin/main. Dirty shared working tree cannot contaminate the commit scope
    # (2026-05-19 trigger; justifies-via P1).
    # In --repo-root mode (integration tests / smoke): skip the worktree; the
    # caller already provides an isolated tmp repo.
    # pull_repo_or_warn removed for production path: with_worktree fetches
    # origin/main before worktree creation; shared-tree pull is structurally
    # superseded (P1). --repo-root path retains pull_repo_or_warn for safety.
    # Unit tests bypass _with_worktree via patch.object(..., lambda fn: fn(None)),
    # so wt_path=None → scan uses module-global TICKETS_ROOT (patched by setUp),
    # commit uses cwd=None (→ REPO_ROOT, also patchable), or patched
    # git_commit_and_push directly.
    use_worktree = args.commit_prod and args.repo_root is None

    # Mutable containers to capture results from inside the worktree lambda.
    _result: list[tuple[list, list, dict, int, list]] = []

    def _scan_and_commit(wt_path: "Path | None") -> None:
        """Inner function: scan + append + commit. Runs inside the worktree."""
        # T-0478: do NOT rebind module-global REPO_ROOT/TICKETS_ROOT here.
        # The original T-0471 retrofit did `global REPO_ROOT, TICKETS_ROOT;
        # REPO_ROOT = wt_path` which leaked past the with_worktree cleanup and
        # crashed run_history_audit (which runs post-block). Thread wt_path
        # explicitly instead — mirrors spine_index_groom's correct pattern.
        # In worktree mode (wt_path is not None): use the worktree's tickets dir.
        # In test bypass mode (wt_path=None): use the module-global TICKETS_ROOT
        # (test setUp may have patched it already).
        _wt_tickets_root: "Path | None" = (
            wt_path / "tickets" if wt_path is not None else None
        )

        to_append_inner: list[FlaggedTicket] = []
        already_flagged_inner: list[FlaggedTicket] = []
        counts_inner: dict[str, int] = {}
        for state in STATES_TO_GROOM:
            a, b, total = _scan_state_dir(
                state, now, thresholds[state], tickets_root=_wt_tickets_root
            )
            to_append_inner.extend(a)
            already_flagged_inner.extend(b)
            counts_inner[state] = total

        failures_inner = 0

        if to_append_inner:
            for f in to_append_inner:
                try:
                    append_grooming_stale(f.path, f.last_ts, now)
                except TicketError as e:
                    print(f"error: failed to append flag to {f.path.name}: {e}", file=sys.stderr)
                    failures_inner += 1

            commit_msg = _build_commit_message(today_iso, to_append_inner)
            ok, gmsg = git_commit_and_push(
                [f.path for f in to_append_inner], commit_msg, today_iso,
                cwd=wt_path,
            )
            stream = sys.stdout if ok else sys.stderr
            print(f"[git] {gmsg}", file=stream)
            if not ok:
                failures_inner += 1

        # T-0693: closed-ticket acceptance check + auto-refile (separate scope).
        # Resolve the repo root to verify artifacts against: the worktree
        # checkout in worktree mode (full tree off origin/main), else the
        # module-global REPO_ROOT (patched in unit tests / set by --repo-root).
        _accept_repo_root = wt_path if wt_path is not None else REPO_ROOT
        _accept_tickets_root = (
            _wt_tickets_root if _wt_tickets_root is not None else TICKETS_ROOT
        )
        reopened_inner = _scan_done_for_acceptance(
            now, _accept_repo_root, _accept_tickets_root, apply_refile=True
        )
        if reopened_inner:
            reopen_msg = _build_reopen_commit_message(today_iso, reopened_inner)
            ok, gmsg = git_commit_and_push_reopen(
                [r.new_path for r in reopened_inner], reopen_msg,
                cwd=wt_path,
            )
            stream = sys.stdout if ok else sys.stderr
            print(f"[git/reopen] {gmsg}", file=stream)
            if not ok:
                failures_inner += 1

        _result.append((
            to_append_inner + already_flagged_inner,
            to_append_inner,
            counts_inner,
            failures_inner,
            reopened_inner,
        ))

    if use_worktree:
        _with_worktree(_scan_and_commit)
    else:
        # --repo-root path: caller provides an isolated tmp repo already at a
        # known-clean state. No pull needed — the tmp repo's origin is a local
        # bare repo set up by the test fixture, and pulling the real origin
        # would touch the shared working tree with the wrong cwd (T-0471).
        _scan_and_commit(None)

    if not _result:
        return 1  # with_worktree raised; already propagated

    all_stale, _to_append, counts, failures, reopened = _result[0]

    # HERMES whole-history audit — read-only; runs after the worktree is gone.
    hermes_line = run_history_audit(REPO_ROOT)

    # T-0383: active-queue snapshot — per-agent count + oldest-ticket summary.
    active_queue_section = build_active_queue_snapshot(["orchestrator", "specialist", "recovery"])

    # T-0976: dependency-graph structural defects (cycles + stale-done-deps),
    # read-only over the shared tree. Folded into this daily digest so deadlock-
    # class defects surface despite the hourly ticket_lint running --no-digest.
    dep_graph_findings = detect_dep_graph_issues(TICKETS_ROOT)

    # T-2833: not_before date gates that fail the anchor rule (unanchored /
    # malformed / dangling-ref). Read-only; surfaced daily so pre-existing
    # arbitrary gates get migrated to a depends_on / event-watch / operator-anchor.
    unanchored_gate_findings = detect_unanchored_gates(TICKETS_ROOT)

    # T-1586: interactive-required tickets carrying a stale dated DRAFT/
    # proposal section with no open operator-facing link. Read-only, over the
    # shared tree (same rationale as the dep-graph scan above).
    stale_draft_findings = find_stale_draft_tickets(TICKETS_ROOT, now)

    # T-1814: ratified-but-unbuilt ADR surfacing. Read-only, over the shared
    # tree (same rationale as the dep-graph/stale-draft scans above) — NOT
    # routed through the commit path the staleness/acceptance checks use.
    ratified_unbuilt_findings, adr_coverage_gaps = find_ratified_unbuilt_adrs(
        adr_dirs, TICKETS_ROOT
    )
    integrity_findings = find_ticket_integrity_issues(TICKETS_ROOT)  # T-2982

    # T-3440 / ORG-089 §4a-completeness: always-on, digest-only. Flags ratified
    # non-vault ADRs (post-landing) missing appetite fields / boilerplate sections.
    appetite_completeness_findings = _appetite_completeness_check(adr_dirs, TICKETS_ROOT, now)
    # T-3440 / ORG-089 §4a-overrun: calibration-gated — returns [] (SILENT) while
    # the policy file is absent / generate-only / calibration_version 0. Surfaced
    # report-only here (apply=False); the in-place substate write + mailbox
    # advisory (apply=True) are implemented + tested and get routed through the
    # commit pass by T-3430, the flip-writer that turns the breaker ON (DECISION-13).
    _appetite_policy_path = REPO_ROOT / "rulebook" / "policies" / "appetite-time-budget.yaml"
    appetite_overrun_findings = _appetite_overrun_check(
        _appetite_policy_path, TICKETS_ROOT, now, apply=False
    )
    # T-3440 / ORG-089 §4b: distinct 'dropped this week' calibration surface.
    dropped_this_week = find_dropped_this_week(TICKETS_ROOT, now)

    # Digest: always send (proof-of-life), unless --no-digest.
    if not args.no_digest:
        digest = build_digest(
            now_human, all_stale, counts,
            hermes_line=hermes_line,
            active_queue_section=active_queue_section,
            reopened=reopened,
            dep_graph_findings=dep_graph_findings,
            unanchored_gate_findings=unanchored_gate_findings,
            stale_draft_findings=stale_draft_findings,
            manifest_line=check_manifest_freshness(REPO_ROOT, now),
            ratified_unbuilt_findings=ratified_unbuilt_findings,
            adr_coverage_gaps=adr_coverage_gaps,
            integrity_findings=integrity_findings,
            appetite_completeness_findings=appetite_completeness_findings,
            appetite_overrun_findings=appetite_overrun_findings,
            dropped_this_week=dropped_this_week,
        )
        if failures > 0:
            digest = digest + f"\nWARNING: ticket_grooming auto-commit reported {failures} failure(s); see stderr.\n"
        ok = _send_digest(digest)
        if not ok:
            print("warning: telegram digest send failed", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
