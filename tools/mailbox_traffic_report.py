#!/usr/bin/env python3
"""mailbox_traffic_report — who sends mail to whom, and how often.

Answers the operator's ask (TG 7708/7710, 2026-09-28): "track who is sending mail where,
and how often" — "put the mailbox metrics into the kino stats page".

SOURCE OF TRUTH: the hub's RECIPIENT side. Every delivered message is a maildir file
`<hub>/<recipient>/{new,cur}/<ts_ns:020d>.<pid>.<seq>.<sender>[:2,<flags>]`, and cur/ is
the never-pruned audit trail (mailbox.py header), so the filename listing alone gives
sender, recipient and time for the whole hub history with no payload reads. The
per-sender `sent.jsonl` ledgers are NOT used: they start 2026-07-29 (T-2275), skip any
sender whose ledger guard can't resolve the hub, and historically carried fixture rows.

ACCESS: the hub sits under its owner's home, which other accounts can't traverse, so
the listing runs AS the hub owner via seat_exec.seat_argv (direct when we ARE the owner,
else `sudo -n -u <owner>`). A failed listing raises ListingFailed — never an empty
report, and never a committed all-zero snapshot (empty output with rc != 0 is "could not
look", not "no mail"; bugs/sudo-u-to-runners-own-account-...md rule 2).

WHY A COMMITTED SNAPSHOT (same design as welltrod_stats.py): kino runs as an account
that cannot read the hub, so a Recovery timer collects and commits
knowledge/stats/mailbox/latest.json and kino serves it read-only via `--format json`.

Modes (sop/standard/auto-commit-cron-default-safe-contract.md; no flag = refuse, exit 64):
    --format json                  serve: print the committed latest.json (kino's entry)
    --report [--raw-senders]       live human-readable report (read-only)
    --collect --dry-run            compute the snapshot, print JSON, write nothing
    --collect --commit-prod        timer: write snapshot + scope-guarded commit/push
    --collect --commit-prod --repo-root X [--no-push]   hermetic smoke
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import agent_role_map as arm  # noqa: E402
from tools.seat_exec import seat_argv  # noqa: E402

SCHEMA = "pantheon-mailbox-traffic/1"
SNAPSHOT_DIR = "knowledge/stats/mailbox"
HISTORY_KEEP_DAYS = 180
DEFAULT_DAYS = 30
EX_USAGE = 64
# <ts_ns zero-padded>.<pid>.<seq>.<sender>, optionally a maildir info suffix (":2,S").
_NAME_RE = re.compile(r"^(?P<ts>\d{19,20})\.\d+\.\d+\.(?P<sender>[^/:]+)(?::2,[A-Za-z]*)?$")
# Per-ticket worker identities ("implementer-T3328") collapse to their role by default.
_WORKER_RE = re.compile(r"^(?P<role>[a-z][\w-]*?)-T\d+$")
WINDOWS: tuple[tuple[str, timedelta | None], ...] = (
    ("24h", timedelta(hours=24)), ("7d", timedelta(days=7)),
    ("30d", timedelta(days=30)), ("all", None),
)


class ListingFailed(RuntimeError):
    """The hub could not be listed — unknown, never to be reported as zero traffic."""


@dataclass(frozen=True)
class Delivery:
    ts: datetime
    sender: str
    recipient: str
    unread: bool


def hub_owner(hub: str) -> str:
    """The account whose home holds the hub (`/home/<owner>/…`), else the live hub
    owner (post-office; mailbox_onboard.DEFAULT_HUB_OWNER, T-2693)."""
    parts = Path(hub).parts
    return parts[2] if len(parts) > 2 and parts[1] == "home" else "post-office"


def list_hub(hub: str, owner: str, *, run: Callable = subprocess.run) -> list[str]:
    """`<recipient>/<new|cur>/<name>` for every delivered message, listed AS `owner`.

    Dot-entries under the hub (per-inbox state such as .urgent_rate, .inflight,
    .acks.jsonl) are pruned, never entered: the listing needs only new/ and cur/,
    and a private state dir the owner can't read failed the whole report
    (T-4370: mason/.urgent_rate, created by mailbox.py's URGENT rate limiter
    without a post-office ACL, gave find EACCES and exit 1)."""
    # T-4386 (rev-t4356-t4370 N2): -mindepth 1 keeps the prune off the hub root itself,
    # so a dot-named hub is listed, not pruned to a silent []. The prune still runs at
    # depth 2, before any dot-dir is entered. The hub goes to find as given (no rstrip),
    # and -H follows it if it's a symlink, with or without a trailing slash. Only
    # depth-3 entries (<recipient>/<new|cur>/<name>) are kept, as before.
    argv = seat_argv(owner, ["find", "-H", hub, "-mindepth", "1", "-maxdepth", "3",
                             "(", "-name", ".*", "-prune", ")", "-o",
                             "(", "-type", "f", "(", "-path", "*/new/*", "-o", "-path", "*/cur/*", ")",
                             "-printf", "%d %P\\n", ")"])
    try:
        r = run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ListingFailed(f"listing {hub} as {owner} failed: {exc}") from exc
    if r.returncode != 0:
        raise ListingFailed(f"listing {hub} as {owner} exited {r.returncode}: "
                            f"{(r.stderr or '').strip()[:300]}")
    return [ln.split(" ", 1)[1] for ln in r.stdout.splitlines()
            if ln.startswith("3 ") and ln[2:].strip()]


def normalize_sender(sender: str, account_personas: dict[str, str]) -> str:
    """Account names -> persona (claude-research -> specialist); per-ticket workers -> role-*."""
    if sender in account_personas:
        return account_personas[sender]
    w = _WORKER_RE.match(sender)
    return f"{w.group('role')}-*" if w else sender


def parse(paths: Iterable[str], *, account_personas: dict[str, str] | None = None
          ) -> tuple[list[Delivery], int]:
    """Listing lines -> Deliveries. `account_personas=None` keeps raw sender names.
    Returns (deliveries, unparseable_count) — bad names are counted, never silently lost."""
    out: list[Delivery] = []
    bad = 0
    for p in paths:
        parts = p.split("/")
        m = _NAME_RE.match(parts[-1]) if len(parts) == 3 and parts[1] in ("new", "cur") else None
        if not m:
            bad += 1
            continue
        sender = m.group("sender")
        if account_personas is not None:
            sender = normalize_sender(sender, account_personas)
        ts = datetime.fromtimestamp(int(m.group("ts")) / 1e9, tz=timezone.utc)
        out.append(Delivery(ts, sender, parts[0], parts[1] == "new"))
    return out, bad


def summarize(deliveries: list[Delivery], *, now: datetime, days: int = DEFAULT_DAYS) -> dict:
    windows = {}
    for label, span in WINDOWS:
        sel = [d for d in deliveries if span is None or d.ts >= now - span]
        windows[label] = {
            "total": len(sel),
            "pairs": [{"sender": s, "recipient": r, "count": c}
                      for (s, r), c in Counter((d.sender, d.recipient) for d in sel).most_common()],
            "by_sender": dict(Counter(d.sender for d in sel).most_common()),
            "by_recipient": dict(Counter(d.recipient for d in sel).most_common()),
        }
    start = (now - timedelta(days=days - 1)).date()
    per_day = Counter(d.ts.date() for d in deliveries if d.ts.date() >= start)
    return {
        "schema": SCHEMA,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "first_message": min(d.ts for d in deliveries).strftime("%Y-%m-%d") if deliveries else None,
        "windows": windows,
        "daily": [{"date": (start + timedelta(days=i)).isoformat(),
                   "count": per_day.get(start + timedelta(days=i), 0)} for i in range(days)],
        "unread_by_recipient": dict(Counter(d.recipient for d in deliveries if d.unread)),
    }


def collect(*, hub: str | None = None, run: Callable = subprocess.run,
            now: datetime | None = None, raw_senders: bool = False,
            days: int = DEFAULT_DAYS) -> dict:
    """Listing -> snapshot dict. Raises ListingFailed (never returns an empty snapshot
    for a hub it could not read)."""
    hub = hub or arm.mailbox_hub()
    paths = list_hub(hub, hub_owner(hub), run=run)
    deliveries, bad = parse(paths, account_personas=None if raw_senders else arm.account_persona_map())
    snap = summarize(deliveries, now=now or datetime.now(timezone.utc), days=days)
    snap["unparseable_names"] = bad
    return snap


def render_text(s: dict, *, top: int = 10) -> str:
    w = s["windows"]
    lines = [f"Mailbox traffic (since {s['first_message']}; generated {s['generated_at']})",
             "Totals: " + "  ".join(f"{k} {w[k]['total']}" for k, _ in WINDOWS)]
    for label in ("7d", "all"):
        lines.append(f"\nTop sender→recipient ({label}):")
        lines += [f"  {p['sender']} → {p['recipient']}: {p['count']}" for p in w[label]["pairs"][:top]]
    lines.append("\nBy sender (7d): " + ", ".join(f"{k} {v}" for k, v in w["7d"]["by_sender"].items()))
    lines.append("By recipient (7d): " + ", ".join(f"{k} {v}" for k, v in w["7d"]["by_recipient"].items()))
    lines.append("\nPer day: " + " ".join(f"{d['date'][5:]}:{d['count']}" for d in s["daily"][-14:]))
    if s["unread_by_recipient"]:
        lines.append("Unread now: " + ", ".join(f"{k} {v}" for k, v in s["unread_by_recipient"].items()))
    return "\n".join(lines)


def write_snapshot(snapshot: dict, repo_root: str) -> list[str]:
    """latest.json + a dated history file (pruned past HISTORY_KEEP_DAYS). Returns the
    repo-relative scope for the commit."""
    base = Path(repo_root) / SNAPSHOT_DIR
    hist = base / "history"
    hist.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot, indent=1) + "\n"
    (base / "latest.json").write_text(payload, encoding="utf-8")
    day = snapshot["generated_at"][:10]
    (hist / f"{day}.json").write_text(payload, encoding="utf-8")
    cutoff = (_dt.datetime.strptime(day, "%Y-%m-%d") - _dt.timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
    for old in hist.glob("*.json"):
        if old.stem < cutoff:
            old.unlink()
    return [SNAPSHOT_DIR + "/"]


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Who sends mail to whom, and how often.")
    ap.add_argument("--format", choices=["json"], help="serve the committed latest.json (read-only)")
    ap.add_argument("--report", action="store_true", help="live human-readable report (read-only)")
    ap.add_argument("--collect", action="store_true", help="collect a snapshot (needs a mode flag)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--commit-prod", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--repo-root", default=None, help="repo root override (tests/smoke)")
    ap.add_argument("--hub", default=None, help="hub path (default: instance.yaml mailbox hub)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help="per-day series length")
    ap.add_argument("--top", type=int, default=10, help="--report: pairs shown")
    ap.add_argument("--raw-senders", action="store_true",
                    help="keep account / per-ticket worker names instead of personas / role-*")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    repo_root = str(Path(args.repo_root).resolve() if args.repo_root
                    else Path(__file__).resolve().parent.parent)

    if args.format == "json" and not (args.collect or args.report):
        # Serve: a missing snapshot is a renderable empty state (exit 0), as welltrod.
        latest = Path(repo_root) / SNAPSHOT_DIR / "latest.json"
        if not latest.exists():
            print(json.dumps({"error": "no_snapshot", "expected": str(latest)}))
            return 0
        sys.stdout.write(latest.read_text(encoding="utf-8"))
        return 0

    modes = [args.report, args.collect and args.dry_run, args.collect and args.commit_prod]
    if sum(bool(m) for m in modes) != 1 or (args.collect and args.dry_run and args.commit_prod):
        print("mailbox_traffic_report: refusing — pick ONE mode: --format json (serve), "
              "--report, --collect --dry-run, or --collect --commit-prod  "
              f"[exit {EX_USAGE}]", file=sys.stderr)
        return EX_USAGE

    try:
        snapshot = collect(hub=args.hub, raw_senders=args.raw_senders, days=args.days)
    except ListingFailed as exc:
        print(f"mailbox_traffic_report: {exc} — nothing written", file=sys.stderr)
        return 2
    if snapshot["unparseable_names"]:
        print(f"mailbox_traffic_report: {snapshot['unparseable_names']} unparseable "
              "file name(s) skipped", file=sys.stderr)

    if args.report:
        print(render_text(snapshot, top=args.top))
        return 0
    if args.dry_run:
        print(json.dumps(snapshot, indent=1))
        return 0

    from tools import _git_utils

    msg = "chore(mailbox-traffic): refresh mailbox traffic snapshot"
    if args.repo_root:
        ok, detail = _git_utils.commit_scoped(write_snapshot(snapshot, repo_root), msg,
                                              cwd=Path(repo_root), push=not args.no_push,
                                              allow_prefix=True)
    else:
        # Never dirty the live shared checkout: land from a fresh worktree (T-0471).
        def _land(wt: Path):
            return _git_utils.commit_scoped(write_snapshot(snapshot, str(wt)), msg, cwd=wt,
                                            push=not args.no_push, sync=False, allow_prefix=True)
        try:
            ok, detail = _git_utils.with_worktree(_land)
        except _git_utils.WorktreeError as exc:
            print(f"mailbox_traffic_report: worktree failed: {exc}", file=sys.stderr)
            return 1
    if not ok:
        print(f"mailbox_traffic_report: commit failed: {detail}", file=sys.stderr)
        return 1
    print(f"mailbox_traffic_report: snapshot committed ({detail})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
