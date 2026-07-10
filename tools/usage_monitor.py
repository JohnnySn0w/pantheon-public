#!/usr/bin/env python3
"""
Pantheon — usage monitor.

Walks Claude Code transcript JSONLs under ~/.claude/projects/, sums per-turn
token usage into rolling windows (1h / 5h / 24h / 7d), appends a snapshot to
state, and (optionally) Telegram-alerts when the 5h window crosses a
threshold.

Bootstrap-mode design:
  - Run on cron every 30 min with no --alert-tokens; it just collects data.
  - After ~7 days of state accumulation, run with --propose-threshold to
    print percentile-based suggestions.
  - Then re-deploy the cron with --alert-tokens N + --send-telegram set.

No credentials touched. Reads transcripts the user already owns; sends
alerts via the bot token at ~/.claude/channels/telegram/.env.
"""

from __future__ import annotations
import argparse
import json
import os
import sys
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"
STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
STATE_FILE = STATE_DIR / "usage.jsonl"
ALERT_COOLDOWN_FILE = STATE_DIR / "alert_cooldown"
TELEGRAM_ENV = Path.home() / ".claude" / "channels" / "telegram" / ".env"
OPERATOR_CHAT_ID = "456474197"  # from access.json allowFrom

WINDOWS_HOURS = (1, 5, 24, 168)  # 1h, 5h, 24h, 7d
ALERT_COOLDOWN_HOURS = 4


@dataclass
class Snapshot:
    ts: str  # ISO UTC of when the snapshot was taken
    tokens_by_window: dict[str, int]  # "1h" -> int
    sessions_by_window: dict[str, int]  # active sessions touched within window
    transcripts_scanned: int


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2)
    except (ValueError, TypeError):
        return None


def turn_tokens(usage: dict) -> int:
    """All token classes summed. Cache reads count too — they hit Max plan limits."""
    if not isinstance(usage, dict):
        return 0
    return (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("output_tokens") or 0)
    )


def scan_transcripts(now: datetime) -> tuple[dict[int, int], dict[int, set[str]], int]:
    """Walk transcripts; return tokens-per-window-hours, sessions-per-window, scanned count."""
    cutoffs = {h: now - timedelta(hours=h) for h in WINDOWS_HOURS}
    tokens = {h: 0 for h in WINDOWS_HOURS}
    sessions = {h: set() for h in WINDOWS_HOURS}
    earliest_cutoff = min(cutoffs.values())
    scanned = 0

    if not TRANSCRIPT_ROOT.exists():
        return tokens, {h: set() for h in WINDOWS_HOURS}, 0

    for jsonl in TRANSCRIPT_ROOT.rglob("*.jsonl"):
        # Cheap mtime prefilter — if file hasn't been touched within longest window, skip it.
        try:
            mtime = datetime.fromtimestamp(jsonl.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < earliest_cutoff:
            continue
        scanned += 1
        session_id = jsonl.stem
        try:
            with jsonl.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") != "assistant":
                        continue
                    msg = rec.get("message") or {}
                    usage = msg.get("usage")
                    if not usage:
                        continue
                    # Prefer the message timestamp; fall back to record-level ts
                    ts_raw = rec.get("timestamp") or msg.get("created_at") or rec.get("ts")
                    ts = parse_ts(ts_raw) if isinstance(ts_raw, str) else None
                    if ts is None:
                        # Fall back to file mtime — coarse, but better than dropping the row
                        ts = mtime
                    toks = turn_tokens(usage)
                    if toks <= 0:
                        continue
                    for h, cutoff in cutoffs.items():
                        if ts >= cutoff:
                            tokens[h] += toks
                            sessions[h].add(session_id)
        except OSError:
            continue
    return tokens, sessions, scanned


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def write_snapshot(snap: Snapshot) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(snap), separators=(",", ":")) + "\n")


def read_telegram_token() -> str | None:
    if not TELEGRAM_ENV.exists():
        return None
    for line in TELEGRAM_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return None


def send_telegram(text: str) -> bool:
    """POST to the Telegram Bot API. Returns True on 2xx."""
    token = read_telegram_token()
    if not token:
        print("usage_monitor: no telegram token; alert NOT sent", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": OPERATOR_CHAT_ID, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError) as e:
        print(f"usage_monitor: telegram send failed: {e}", file=sys.stderr)
        return False


def alert_in_cooldown(now: datetime) -> bool:
    if not ALERT_COOLDOWN_FILE.exists():
        return False
    try:
        last = parse_ts(ALERT_COOLDOWN_FILE.read_text().strip())
    except OSError:
        return False
    if last is None:
        return False
    return (now - last) < timedelta(hours=ALERT_COOLDOWN_HOURS)


def stamp_cooldown(now: datetime) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ALERT_COOLDOWN_FILE.write_text(now.isoformat())


def propose_threshold() -> int:
    """Print a token-threshold proposal from accumulated state."""
    if not STATE_FILE.exists():
        print("No state yet. Run with no flags on a cron for a week, then come back.")
        return 1
    five_h_values = []
    for line in STATE_FILE.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            v = row.get("tokens_by_window", {}).get("5h")
            if isinstance(v, int) and v > 0:
                five_h_values.append(v)
        except json.JSONDecodeError:
            continue
    if len(five_h_values) < 14:  # arbitrary "enough" floor
        print(f"Only {len(five_h_values)} non-zero 5h snapshots. Want >=14. Keep collecting.")
        return 1
    five_h_values.sort()
    n = len(five_h_values)
    p50 = five_h_values[n // 2]
    p90 = five_h_values[int(n * 0.9)]
    p95 = five_h_values[int(n * 0.95)]
    p99 = five_h_values[min(n - 1, int(n * 0.99))]
    print(f"5h-window token consumption ({n} snapshots):")
    print(f"  p50: {fmt_tokens(p50)}")
    print(f"  p90: {fmt_tokens(p90)}")
    print(f"  p95: {fmt_tokens(p95)}")
    print(f"  p99: {fmt_tokens(p99)}")
    print(f"")
    print(f"Suggested --alert-tokens: {p95} (p95 — alerts on the top 5% of 5h windows)")
    return 0


def build_alert_text(tokens: dict[int, int], threshold: int) -> str:
    five_h = tokens[5]
    pct_over = ((five_h - threshold) / threshold) * 100 if threshold else 0
    return (
        f"Pantheon usage alert — 5h window crossed threshold.\n"
        f"5h: {fmt_tokens(five_h)} tokens ({pct_over:+.0f}% vs threshold {fmt_tokens(threshold)})\n"
        f"24h: {fmt_tokens(tokens[24])} | 7d: {fmt_tokens(tokens[168])} | 1h: {fmt_tokens(tokens[1])}\n"
        f"Cooldown: {ALERT_COOLDOWN_HOURS}h before another alert."
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Pantheon usage monitor.")
    p.add_argument(
        "--alert-tokens",
        type=int,
        default=None,
        help="Alert if rolling 5h window exceeds this many tokens. Omit to just collect data.",
    )
    p.add_argument(
        "--send-telegram",
        action="store_true",
        help="Actually send the alert via Telegram (otherwise just prints to stderr).",
    )
    p.add_argument(
        "--print-summary",
        action="store_true",
        help="Print current rolling-window summary to stdout.",
    )
    p.add_argument(
        "--propose-threshold",
        action="store_true",
        help="Print percentile-based threshold proposals from accumulated state, then exit.",
    )
    args = p.parse_args()

    if args.propose_threshold:
        return propose_threshold()

    now = utcnow()
    tokens, sessions, scanned = scan_transcripts(now)

    snap = Snapshot(
        ts=now.isoformat(),
        tokens_by_window={f"{h}h": tokens[h] for h in WINDOWS_HOURS},
        sessions_by_window={f"{h}h": len(sessions[h]) for h in WINDOWS_HOURS},
        transcripts_scanned=scanned,
    )
    write_snapshot(snap)

    if args.print_summary:
        print(
            f"5h: {fmt_tokens(tokens[5])} tokens ({len(sessions[5])} sessions) | "
            f"24h: {fmt_tokens(tokens[24])} ({len(sessions[24])}) | "
            f"7d: {fmt_tokens(tokens[168])} ({len(sessions[168])}) | "
            f"1h: {fmt_tokens(tokens[1])} ({len(sessions[1])})"
        )

    if args.alert_tokens is not None and tokens[5] > args.alert_tokens:
        if alert_in_cooldown(now):
            print(
                f"usage_monitor: 5h={tokens[5]} > {args.alert_tokens} but in cooldown",
                file=sys.stderr,
            )
            return 0
        text = build_alert_text(tokens, args.alert_tokens)
        if args.send_telegram:
            if send_telegram(text):
                stamp_cooldown(now)
                print("usage_monitor: alert sent", file=sys.stderr)
            else:
                print("usage_monitor: alert SEND FAILED", file=sys.stderr)
                return 1
        else:
            print(f"--- ALERT (dry-run, no --send-telegram) ---\n{text}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
