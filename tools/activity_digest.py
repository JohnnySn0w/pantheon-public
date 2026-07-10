#!/usr/bin/env python3
"""Activity digest — 5hr cycle system-pulse signal to the operator's Telegram.

Each fire (scheduled by systemd timer at 00/05/10/15/20 UTC hours):
  1. Compute the 5hr window [boundary, now).
  2. Gather tickets resolved in that window from tickets/done/.
  3. Classify each ticket by BACKLOG arc (via b<N> label) and project
     (via resolve-text scan for repo names).
  4. Load BACKLOG arc shortforms from BACKLOG.md headings.
  5. Render a short Telegram-Markdown digest.
  6. POST to the operator's Telegram chat.
  7. On non-empty window: append JSONL entry to ledger/2026.jsonl.
  8. Log one line to ~/.local/log/pantheon/activity_digest.log.

On quiet window (zero resolved tickets): post a one-line "no notable
activity" message. Ledger is not written; only the log is touched.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: make `from tools.ticket import ...` work as script or package.
# ---------------------------------------------------------------------------

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.ticket import TICKETS_ROOT, parse_ticket, Ticket  # noqa: E402
from tools.agent_role_map import resolve_role  # noqa: E402
from tools.inbox_consumer import OPERATOR_CHAT_ID, TELEGRAM_ENV  # noqa: E402
from tools._git_utils import pull_repo_or_warn  # noqa: E402
from tools._audit import append_ledger_entries, build_ledger_entry  # noqa: E402

# ---------------------------------------------------------------------------
# Paths and constants.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_DIR = REPO_ROOT / "ledger"
# T-0148 Phase 2: the ledger write target is resolved at WRITE time via
# ledger_shard.current_write_target(LEDGER_DIR, now) — NOT frozen at import.
# A frozen module constant would pin every digest to ledger/2026.jsonl even
# after Phase 3 archives it. LEDGER_PATH stays as an explicit test/override
# hook: when non-None it forces that exact file (tests patch it); when None
# (production) write_ledger_entry resolves the current target dynamically.
LEDGER_PATH: Path | None = None
LOG_PATH = Path.home() / ".local" / "log" / "pantheon" / "activity_digest.log"
BACKLOG_PATH = REPO_ROOT / "BACKLOG.md"

# Fixed UTC hour marks for 5hr cycle boundaries.
FIXED_OFFSETS = (0, 5, 10, 15, 20)

# Regex to find resolved transition-log lines.
_RESOLVE_RE = re.compile(
    r"- (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \[.*?\] resolved"
)

# Characters that break Telegram Markdown when unescaped.
_MD_STRIP_RE = re.compile(r"[*_`\[\]]")


# ---------------------------------------------------------------------------
# Utility helpers.
# ---------------------------------------------------------------------------


def _strip_md(text: str) -> str:
    """Remove Telegram-Markdown special characters from a plain-text string."""
    return _MD_STRIP_RE.sub("", text)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dt_to_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Phase 1 — Window computation.
# ---------------------------------------------------------------------------


def compute_window() -> tuple[datetime, datetime]:
    """Return (window_start, window_end) for the current 5hr cycle.

    window_end  = datetime.now(UTC)
    window_start = most recent fixed UTC hour boundary (0/5/10/15/20) prior to now.
    """
    now = datetime.now(timezone.utc)
    boundary_hour = now.hour - (now.hour % 5)
    window_start = now.replace(hour=boundary_hour, minute=0, second=0, microsecond=0)
    return window_start, now


# ---------------------------------------------------------------------------
# Phase 2 — BACKLOG arc map.
# ---------------------------------------------------------------------------


def load_backlog_arcs() -> dict[str, str]:
    """Parse BACKLOG.md headings into {arc_id: short_title} map.

    Heading form:
      ## B<N> — <full title>
      ## B<N> — <full title>  ✓ CLOSED <date>

    Short title = first 6 words of <full title>.
    Returns empty dict if BACKLOG.md is unreadable.
    """
    pattern = re.compile(r"^## (B\d+) — (.+?)(?:\s+✓ CLOSED.*)?$")
    arcs: dict[str, str] = {}
    try:
        text = BACKLOG_PATH.read_text(encoding="utf-8")
    except OSError:
        return arcs
    for line in text.splitlines():
        m = pattern.match(line)
        if m:
            arc_id = m.group(1)
            title = m.group(2).strip()
            words = title.split()
            short = " ".join(words[:6])
            arcs[arc_id] = short
    return arcs


# ---------------------------------------------------------------------------
# Phase 3 — Data gathering.
# ---------------------------------------------------------------------------


def gather_resolved_tickets(
    window_start: datetime, window_end: datetime
) -> list[Ticket]:
    """Return tickets from tickets/done/ resolved within [window_start, window_end).

    Uses the last 'resolved' transition-log entry timestamp as the signal.
    Malformed ticket files are skipped silently.
    """
    done_dir = TICKETS_ROOT / "done"
    if not done_dir.exists():
        return []

    results: list[Ticket] = []
    for path in done_dir.iterdir():
        if not path.is_file() or not path.name.endswith(".md"):
            continue
        if path.name == "README.md":
            continue
        try:
            ticket = parse_ticket(path)
        except Exception:
            continue

        # Find all resolved timestamps in the transition log.
        matches = _RESOLVE_RE.findall(ticket.body)
        if not matches:
            continue

        # Use the last resolved timestamp.
        ts_str = matches[-1]
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            continue

        if window_start <= ts < window_end:
            results.append(ticket)

    return results


# ---------------------------------------------------------------------------
# Phase 4 — Classification.
# ---------------------------------------------------------------------------


def classify_arc(ticket: Ticket) -> str | None:
    """Return the BACKLOG arc ID (e.g. 'B10') from the ticket's labels, or None.

    Matches labels of the form b<N> or B<N> (case-insensitive, exact token).
    First match wins.
    """
    labels = ticket.fields.get("labels") or []
    arc_pattern = re.compile(r"^[Bb](\d+)$")
    for label in labels:
        m = arc_pattern.match(label)
        if m:
            return f"B{m.group(1)}"
    return None


def classify_project(ticket: Ticket) -> list[str]:
    """Return list of project names this ticket's work landed in.

    Heuristic: scan the resolve transition-log line for repo name mentions.
    - 'pantheon-bridge' in resolve text → include 'pantheon-bridge'
    - Default (always included unless only pantheon-bridge is found) → 'pantheon-config'

    A ticket can appear under both if both repos are mentioned.
    """
    # Extract the last resolved line from the transition log.
    matches = _RESOLVE_RE.findall(ticket.body)
    resolve_text = ""
    if matches:
        # Find the actual resolve line body (text after the timestamp + actor).
        last_ts = matches[-1]
        for line in ticket.body.splitlines():
            if last_ts in line and "resolved" in line:
                resolve_text = line
                break

    projects: list[str] = []
    has_bridge = "pantheon-bridge" in resolve_text
    has_config = "pantheon-config" in resolve_text or not has_bridge

    if has_config:
        projects.append("pantheon-config")
    if has_bridge:
        projects.append("pantheon-bridge")

    return projects


# ---------------------------------------------------------------------------
# Phase 5 — Markdown rendering.
# ---------------------------------------------------------------------------


def render_digest(
    tickets: list[Ticket],
    arc_map: dict[str, str],
    window_start: datetime,
    window_end: datetime,
) -> str:
    """Render the Telegram-Markdown digest string.

    Quiet window (zero tickets): one-line *No notable activity* message.
    Non-empty: header + project counts + arcs section + footer.
    """
    start_iso = _dt_to_iso(window_start)
    end_iso = _dt_to_iso(window_end)

    if not tickets:
        return f"*No notable activity in {start_iso} – {end_iso}*"

    # --- Classify each ticket ---
    project_buckets: dict[str, list[Ticket]] = {}
    arc_buckets: dict[str, list[Ticket]] = {}

    for t in tickets:
        for proj in classify_project(t):
            project_buckets.setdefault(proj, []).append(t)
        arc_id = classify_arc(t)
        if arc_id:
            arc_buckets.setdefault(arc_id, []).append(t)

    lines: list[str] = []

    # Header.
    lines.append(f"*Activity digest: {start_iso} – {end_iso}*")
    lines.append("")

    # Project counts.
    lines.append("*Projects:*")
    for proj in sorted(project_buckets):
        count = len(project_buckets[proj])
        noun = "ticket" if count == 1 else "tickets"
        lines.append(f"• {proj}: {count} {noun} resolved")
    lines.append("")

    # Arcs touched.
    if arc_buckets:
        lines.append("*Arcs touched:*")
        for arc_id in sorted(arc_buckets, key=lambda x: int(x[1:])):
            short = arc_map.get(arc_id, arc_id)
            lines.append(f"*{arc_id}*({_strip_md(short)})")
            for t in arc_buckets[arc_id]:
                safe_title = _strip_md(t.title)
                lines.append(f"  · {t.fields.get('id', '?')} — {safe_title}")
        lines.append("")

    # Uncategorized tickets (no arc label).
    uncat = [t for t in tickets if classify_arc(t) is None]
    if uncat:
        lines.append("*Other:*")
        for t in uncat:
            safe_title = _strip_md(t.title)
            lines.append(f"  · {t.fields.get('id', '?')} — {safe_title}")
        lines.append("")

    # Footer.
    arc_count = len(arc_buckets)
    ticket_count = len(tickets)
    arc_noun = "arc" if arc_count == 1 else "arcs"
    ticket_noun = "ticket" if ticket_count == 1 else "tickets"
    lines.append(f"_{ticket_count} {ticket_noun} · {arc_count} {arc_noun}_")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase 6 — Telegram delivery.
# ---------------------------------------------------------------------------


def _read_telegram_token() -> str | None:
    if not TELEGRAM_ENV.exists():
        return None
    for line in TELEGRAM_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return None


def send_telegram(text: str) -> None:
    """POST text to the operator's Telegram chat via direct Bot API."""
    token = _read_telegram_token()
    if not token:
        print("[activity_digest] no telegram token; message NOT sent", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({
        "chat_id": OPERATOR_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if not (200 <= resp.status < 300):
                print(
                    f"[activity_digest] telegram POST returned {resp.status}",
                    file=sys.stderr,
                )
    except (urllib.error.URLError, OSError) as exc:
        print(f"[activity_digest] telegram POST failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Phase 7 — Ledger entry (non-empty fires only).
# ---------------------------------------------------------------------------


def write_ledger_entry(
    *,
    window_start: datetime,
    ticket_count: int,
    arc_count: int,
    agent: str,
) -> None:
    """Append a JSONL ledger entry for this digest fire.

    Schema mirrors ORG-001 (id, ts, agent, action, rationale, justifies_via,
    links, cost_usd, parent_id, supersedes).
    """
    now_str = _now_iso()
    action = f"activity_digest_{_dt_to_iso(window_start)}"
    entry = build_ledger_entry(
        entry_id=f"{now_str}-digest",
        ts=now_str,
        agent=agent,
        action=action,
        rationale=(
            f"5hr activity digest fired: {ticket_count} ticket(s) resolved, "
            f"{arc_count} arc(s) touched in window {_dt_to_iso(window_start)}."
        ),
        justifies_via=["B10", "D1", "P1"],
    )
    # Resolve the write target at write time (T-0148 Phase 2): an explicit
    # LEDGER_PATH override (tests) wins (passed as `shard`); otherwise
    # current_write_target picks the legacy ledger/2026.jsonl while it exists,
    # else the current ISO-week shard. Behavior-preserving pre-migration
    # (2026.jsonl still present). T-1088: build + append via the shared
    # _audit helper (canonical 10-field schema + sort_keys serialization).
    append_ledger_entries([entry], ledger_dir=LEDGER_DIR, shard=LEDGER_PATH)


# ---------------------------------------------------------------------------
# Phase 8 — Logging.
# ---------------------------------------------------------------------------


def write_log(
    *,
    agent: str,
    window_start: datetime,
    ticket_count: int,
    arc_count: int,
) -> None:
    """Append one line to the activity digest log.

    Non-quiet format: <ISO> <agent> <window-start> <N>tickets <M>arcs
    Quiet format:     <ISO> <agent> <window-start> quiet
    """
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    now_str = _now_iso()
    ws_str = _dt_to_iso(window_start)
    if ticket_count == 0:
        line = f"{now_str} {agent} {ws_str} quiet\n"
    else:
        line = f"{now_str} {agent} {ws_str} {ticket_count}tickets {arc_count}arcs\n"
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line)


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Post 5hr activity digest to the operator's Telegram."
    )
    parser.add_argument(
        "--agent",
        required=True,
        metavar="NAME",
        help="Persona name of the running agent (e.g. orchestrator, recovery).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print rendered markdown to stdout; skip Telegram POST and ledger write.",
    )
    args = parser.parse_args(argv)

    pull_repo_or_warn("activity_digest")
    persona = args.agent
    role_id = resolve_role(persona)

    window_start, window_end = compute_window()
    arc_map = load_backlog_arcs()
    tickets = gather_resolved_tickets(window_start, window_end)
    digest = render_digest(tickets, arc_map, window_start, window_end)

    ticket_count = len(tickets)
    arc_ids_touched = {classify_arc(t) for t in tickets if classify_arc(t) is not None}
    arc_count = len(arc_ids_touched)

    if args.dry_run:
        print(digest)
        return 0

    send_telegram(digest)

    if ticket_count > 0:
        write_ledger_entry(
            window_start=window_start,
            ticket_count=ticket_count,
            arc_count=arc_count,
            agent=role_id,
        )

    write_log(
        agent=persona,
        window_start=window_start,
        ticket_count=ticket_count,
        arc_count=arc_count,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
