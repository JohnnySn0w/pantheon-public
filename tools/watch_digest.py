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

from tools._git_utils import pull_repo_or_warn  # noqa: E402

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
    lower = report_text.lower()
    return any(kw.lower() in lower for kw in ABOVE_BAR_KEYWORDS)


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


def collect_today(date_dir: Path) -> list[tuple[str, bool, str]]:
    """Returns list of (slug, above_bar, one_line_summary) for each report file."""
    if not date_dir.exists():
        return []
    rows = []
    for path in sorted(date_dir.glob("*.md")):
        if path.name == "README.md":
            continue
        slug = path.stem
        text = path.read_text(encoding="utf-8")
        rows.append((slug, is_above_bar(text), report_summary(text)))
    return rows


def build_digest(date_iso: str, rows: list[tuple[str, bool, str]]) -> str:
    """Render the watch digest as Markdown for Telegram.

    Header convention (per T-0018): `<emoji> [<Agent> · <digest-type> · <date>]`.
    Same skeleton in `tools/ticket_grooming.py:build_digest` so a Telegram reader
    can pattern-match the sender within the first 1-2 words. This file is
    Specialist-emitted (specialist) and uses 📡 + Markdown bold around the label.
    """
    if not rows:
        return f"📭 [Specialist · watch · {date_iso}]\nNo reports today."
    above = [r for r in rows if r[1]]
    below = [r for r in rows if not r[1]]
    parts = [f"📡 *[Specialist · watch · {date_iso}]*", ""]
    parts.append(f"{len(rows)} topic(s) fired today.")
    if above:
        parts.append("")
        parts.append("⚠️ *Above the bar:*")
        for slug, _, summary in above:
            parts.append(f"• `{slug}` — {summary}")
    if below:
        parts.append("")
        parts.append("Below the bar:")
        for slug, _, summary in below:
            parts.append(f"• `{slug}` — {summary}")
    parts.append("")
    parts.append(f"Full reports: `pantheon-config/knowledge/watch/reports/{date_iso}/`")
    return "\n".join(parts)


def _post_telegram(url: str, chat_id: str, text: str, parse_mode: str | None) -> bool:
    """POST one sendMessage. parse_mode=None sends plain text (no markdown parsing)."""
    fields = {"chat_id": chat_id, "text": text}
    if parse_mode:
        fields["parse_mode"] = parse_mode
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status == 200


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    """Send the digest, preferring Markdown but FALLING BACK to plain text.

    The digest embeds researcher-authored headlines — arbitrary prose that can
    contain unbalanced Markdown metacharacters (a lone `_`, `*`, or backtick).
    Legacy-Markdown parsing then 400s and the whole digest is dropped (observed
    2026-06-08: a `watch_runner` underscore in one of 8 above-bar headlines sank
    the most important digest of the week). Escaping arbitrary prose for legacy
    Markdown is underspecified/fragile, so on ANY Markdown-send failure we retry
    once as plain text — losing bold formatting but GUARANTEEING delivery, which
    for an unattended alert channel is the right trade.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
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
    digest = build_digest(date_iso, rows)
    print(digest)
    if args.no_send:
        return 0
    token = read_telegram_token(TELEGRAM_ENV)
    if not token:
        print(f"warning: no Telegram token at {TELEGRAM_ENV}; skipping send", file=sys.stderr)
        return 0
    ok = send_telegram(token, TELEGRAM_CHAT_ID, digest)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
