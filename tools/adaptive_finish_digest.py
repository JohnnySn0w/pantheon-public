#!/usr/bin/env python3
"""tools/adaptive_finish_digest.py — weekly Telegram digest of adaptive-finish hits.

T-0907 (the follow-on deferred by the inline TODO in tools/adaptive_finish_detect.py;
cadence locked by HO in the T-0220 brief, decision #1). The detector logs an
audit-tier hit record per narrate-then-exit subagent finish to
``~/.local/state/pantheon/adaptive_finish_log.jsonl``. This tool reads that log,
summarizes the last N days (default 7), and sends a plain-text Telegram digest via
the shared watch_digest Bot-API helper.

It is READ-ONLY over the detector's state — the detector code is untouched.

NOISE DISCIPLINE: by default the send is SKIPPED when the window has zero hits
(the operator is noise-averse — the ticket_lint Telegram was suppressed per msg_1934).
Pass --always to send a "0 hits" digest anyway.

The pantheon-bridge path the original TODO named is dead (2026-06-13 billing change);
this mirrors the live pattern (cron → direct Bot-API digest) used by
tools/ticket_grooming.py + tools/watch_digest.py.

  python3 tools/adaptive_finish_digest.py            # send weekly digest (skip if empty)
  python3 tools/adaptive_finish_digest.py --dry-run  # print what would be sent, send nothing
  python3 tools/adaptive_finish_digest.py --days 30  # custom window
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

# Make `from tools.watch_digest import ...` work both as a script and as a module.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.watch_digest import (  # noqa: E402
    TELEGRAM_CHAT_ID,
    TELEGRAM_ENV,
    read_telegram_token,
    send_telegram,
)

_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
_LOG_FILE_NAME = "adaptive_finish_log.jsonl"
_SAMPLE_LIMIT = 5  # excerpts shown in the digest


def resolve_log_path(state_dir: str | None = None) -> Path:
    """Log path: explicit --state-dir > PANTHEON_STATE_DIR env > default.

    Mirrors tools/adaptive_finish_detect.py's resolution so the digest reads
    exactly what the detector writes.
    """
    if state_dir:
        return Path(state_dir) / _LOG_FILE_NAME
    env = os.environ.get("PANTHEON_STATE_DIR")
    base = Path(env) if env else _DEFAULT_STATE_DIR
    return base / _LOG_FILE_NAME


def load_hits(log_path: Path) -> list[dict]:
    """Parse the jsonl hit log, skipping malformed lines (read-only, fail-open).

    A missing log is an empty history, not an error — the detector only creates the
    file on the first hit.
    """
    if not log_path.exists():
        return []
    hits: list[dict] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue  # tolerate a torn final line / corruption
        if isinstance(obj, dict):
            hits.append(obj)
    return hits


def window_hits(hits: list[dict], since_ts: float) -> list[dict]:
    """Hits whose ts is >= since_ts. Records without a numeric ts are excluded."""
    out = []
    for h in hits:
        ts = h.get("ts")
        if isinstance(ts, (int, float)) and ts >= since_ts:
            out.append(h)
    return out


def summarize(hits: list[dict]) -> dict:
    """Aggregate a window of hit records into digest fields."""
    by_agent = Counter(h.get("agent_type") or "unknown" for h in hits)
    by_phrase = Counter(h.get("matched_phrase") or "?" for h in hits)
    fp_marked = sum(1 for h in hits if h.get("false_positive_marked"))
    samples = [
        {
            "agent_type": h.get("agent_type") or "unknown",
            "phrase": h.get("matched_phrase") or "?",
            "excerpt": (h.get("message_excerpt") or "").strip().replace("\n", " "),
        }
        for h in hits[-_SAMPLE_LIMIT:]
    ]
    return {
        "total": len(hits),
        "by_agent": dict(by_agent.most_common()),
        "by_phrase": dict(by_phrase.most_common()),
        "false_positive_marked": fp_marked,
        "samples": samples,
    }


def format_digest(summary: dict, days: int) -> str:
    """Plain-text (no-markdown) Telegram digest body."""
    lines = [f"🧭 adaptive-finish digest — last {days}d"]
    total = summary["total"]
    lines.append(f"hits: {total}")
    if total == 0:
        lines.append("(no narrate-then-exit finishes detected this window)")
        return "\n".join(lines)

    fp = summary["false_positive_marked"]
    if fp:
        lines.append(f"marked false-positive: {fp}")

    if summary["by_agent"]:
        agents = ", ".join(f"{k}×{v}" for k, v in summary["by_agent"].items())
        lines.append(f"by agent: {agents}")
    if summary["by_phrase"]:
        phrases = ", ".join(f"{k!r}×{v}" for k, v in summary["by_phrase"].items())
        lines.append(f"by phrase: {phrases}")

    if summary["samples"]:
        lines.append("recent:")
        for s in summary["samples"]:
            ex = s["excerpt"]
            if len(ex) > 100:
                ex = ex[:100] + "…"
            lines.append(f"  · [{s['agent_type']}] {ex}")
    return "\n".join(lines)


def build_digest(
    *,
    days: int,
    state_dir: str | None,
    now: float | None = None,
) -> tuple[int, str]:
    """Return (hit_count, formatted_digest) for the window. Pure-ish (now injectable)."""
    now = time.time() if now is None else now
    since = now - days * 86400
    hits = window_hits(load_hits(resolve_log_path(state_dir)), since)
    summary = summarize(hits)
    return summary["total"], format_digest(summary, days)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7, help="window length (default 7)")
    ap.add_argument("--state-dir", default=None, help="override the state dir (tests)")
    ap.add_argument("--chat-id", default=TELEGRAM_CHAT_ID, help="Telegram chat id")
    ap.add_argument("--dry-run", action="store_true", help="print the digest, send nothing")
    ap.add_argument("--always", action="store_true",
                    help="send even when there are zero hits (default: skip-on-empty)")
    args = ap.parse_args(argv)

    count, digest = build_digest(days=args.days, state_dir=args.state_dir)

    if count == 0 and not args.always:
        print("adaptive_finish_digest: 0 hits in window; skipping send (use --always to force)",
              file=sys.stderr)
        return 0

    if args.dry_run:
        print(digest)
        return 0

    token = read_telegram_token(TELEGRAM_ENV)
    if not token:
        print(f"warning: no Telegram token at {TELEGRAM_ENV}; skipping send", file=sys.stderr)
        return 0
    ok = send_telegram(token, args.chat_id, digest)
    if not ok:
        print("adaptive_finish_digest: Telegram send failed", file=sys.stderr)
        return 1
    print(f"adaptive_finish_digest: sent ({count} hits)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
