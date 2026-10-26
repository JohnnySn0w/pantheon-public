#!/usr/bin/env python3
"""Echo-corpus obligation lint — companion to recovery's pantheon-ticket-lint
(T-3495 / T-3477 / ORG-065 Call 2).

T-3477 added echo-prismatic's ADR corpus (`/home/claude/echo-prismatic/adr`,
the 200+ agent-authored range) to the typed-graph obligation scan
(`tools.ticket_lint.detect_typed_graph_issues` / `detect_unverified_elements`)
so a Pantheon ticket's implements/verifies/ratifies/informs/supersedes edge
into an echo ADR obligation gets validated. But `pantheon-ticket-lint`'s sole
hourly runner is the `claude-fallback` (recovery) account, which cannot read
into claude's 700-mode home — so that validation was effectively vacuous on
the only account that ever ran it (T-3494 fixed the resulting EACCES crash
with a warn-and-skip guard; the coverage gap itself is T-3495).

This module wraps (never forks) `tools.ticket_lint`'s existing helpers with
an echo-scoped filter and runs as a companion unit on the `claude` account,
which DOES have native read access to `/home/claude/echo-prismatic`. It
unions this repo's own `architecture/adr/` with echo's `adr/` (the
obligation check needs both: pantheon tickets for the edges, echo ADRs for
the `elements:` targets those edges may point at), then narrows the
resulting findings down to the echo-scoped subset via
`tools.ticket_lint.is_echo_scoped_id`.

Dry-run + alert only — this unit DETECTS drift and mailboxes recovery on
findings-present; it never writes to tickets/ or adr/. Recovery remains the sole
writer for any fix, avoiding a write race with recovery's own `--commit-prod`
cron on the same `tickets/` tree. Alert cooldown/resurfacing mirrors this
codebase's existing per-key-cooldown convention (`tools.unit_failure_alert`,
`tools.credential_expiry_monitor`): a CHANGED finding-set alerts immediately;
an UNCHANGED set is re-alerted only after `cooldown_seconds` has elapsed,
so an unresolved finding is never permanently lost (no pure delta-only
silence) but also never floods recovery's mailbox every single hourly fire.

Install (unit is HO-installed post-merge; see tools/systemd/README.md for
the fuller convention this mirrors):

    cp tools/systemd/pantheon-echo-lint.service ~/.config/systemd/user/
    cp tools/systemd/pantheon-echo-lint.timer   ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now pantheon-echo-lint.timer

Manual fire (read-only preview, no mailbox send):

    python3 tools/echo_corpus_lint.py --no-mailbox
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
import time
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.ticket_lint import (  # noqa: E402
    REPO_ROOT,
    _ECHO_PRISMATIC_ADR_DIR,
    TypedGraphFinding,
    detect_typed_graph_issues,
    detect_unverified_elements,
    is_echo_scoped_id,
)
from tools.agent_role_map import mailbox_hub  # noqa: E402  (T-4096: hub from instance.yaml)

RECOVERY_INBOX = os.environ.get("PANTHEON_RECOVERY_INBOX") or f"{mailbox_hub()}/recovery"

STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
STATE_PATH = STATE_DIR / "echo_corpus_lint_state.json"

# T-3495 alert-shape decision: 24h, not the 6h credential/unit-failure
# cooldown — this class of finding is explicitly "not urgent" (T-3495
# ticket framing: defense against FUTURE echo-corpus drift, not accumulated
# backlog), so re-nagging recovery hourly for an already-seen, unresolved
# finding is not warranted; daily resurfacing is.
ALERT_COOLDOWN_SECONDS = int(
    os.environ.get("PANTHEON_ECHO_LINT_COOLDOWN_SECONDS", str(24 * 3600))
)

# ORG-065 §3.4: `implements:` without `verifies:` is the expected transitional
# shape, not a defect (T-3572 disposition; a verifies edge is earned when a
# ticket actually verifies). Such findings are counted in the alert body but
# never trigger or sign an alert on their own, so a set made only of them no
# longer re-mails recovery every cooldown.
TRANSITIONAL_KINDS = frozenset({"implemented_unverified_element"})


def scan_echo_scoped_findings(repo_root: Path, echo_adr_dir: Path) -> list[TypedGraphFinding]:
    """Run the typed-graph obligation scan unioned over `repo_root`'s own
    `architecture/adr/` + `echo_adr_dir`, then filter to the echo-scoped
    subset only (`is_echo_scoped_id` against each finding's `detail`).

    Deliberately excludes `detect_duplicate_receipt_ids` (unrelated to ADR
    dirs / cross-repo obligations — a receipts: id uniqueness check with no
    echo-scope shape) and a standalone `scan_adr_elements` call (its
    malformed_id/duplicate_id findings are already folded into
    `detect_typed_graph_issues`'s output)."""
    tickets_root = repo_root / "tickets"
    adr_dirs = [repo_root / "architecture" / "adr", echo_adr_dir]
    findings = (
        detect_typed_graph_issues(tickets_root, adr_dirs)
        + detect_unverified_elements(tickets_root, adr_dirs)
    )
    return [f for f in findings if is_echo_scoped_id(f.detail)]


def _finding_signature(findings: list[TypedGraphFinding]) -> str:
    """Stable, order-independent content-hash of the current finding set —
    used to detect a CHANGED set (always re-alert) vs. an UNCHANGED one
    (cooldown-gated; see `_should_alert`)."""
    payload = "\n".join(sorted(f"{f.kind}:{f.detail}" for f in findings))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_state(state_path: Path) -> dict:
    try:
        return json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state))


def _should_alert(state: dict, signature: str, now: float, cooldown_seconds: int) -> bool:
    """New/changed finding-set -> alert immediately. Unchanged set -> only
    once `cooldown_seconds` has elapsed since the last alert."""
    if state.get("last_signature") != signature:
        return True
    last_alert_ts = state.get("last_alert_ts", 0)
    return (now - last_alert_ts) >= cooldown_seconds


def _compose_body(findings: list[TypedGraphFinding], now: float,
                  n_transitional: int = 0) -> str:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    lines = [
        f"Echo-corpus obligation lint (T-3495 companion to T-3477/ORG-065) — "
        f"{len(findings)} finding(s) as of {ts}:",
        "",
    ]
    for f in findings:
        lines.append(f"  - {f.kind}: {f.detail}")
    if n_transitional:
        lines.append(
            f"  (plus {n_transitional} expected-transitional "
            f"implemented_unverified_element finding(s), ORG-065 §3.4; not listed)"
        )
    lines.append("")
    lines.append(
        "Action (recovery): echo-prismatic (/home/claude/echo-prismatic/adr) "
        "obligation-graph findings, detected here on the claude account "
        "(recovery has no read access to that dir — T-3477/T-3494/T-3495). Recovery "
        "remains the writer for any fix (edit the ticket's "
        "implements/verifies/ratifies edge, or the ADR's elements: block). "
        "Detection-only; nothing was mutated by this alert."
    )
    return "\n".join(lines)


def _alert_recovery(body: str, n: int, sender: str, recovery_inbox: str) -> None:
    from tools import mailbox
    mailbox.send(
        recovery_inbox, body, sender,
        subject=f"echo-corpus-lint: {n} finding(s)",
        severity="normal",
    )


def run(
    *,
    repo_root: Path,
    echo_adr_dir: Path,
    state_path: Path,
    cooldown_seconds: int,
    sender: str,
    recovery_inbox: str,
    send_mailbox: bool = True,
    now: float | None = None,
) -> int:
    now = time.time() if now is None else now
    findings = scan_echo_scoped_findings(repo_root, echo_adr_dir)

    if not findings:
        print("[echo_corpus_lint] clean — no echo-scoped findings")
        return 0

    if not send_mailbox:
        print(f"[echo_corpus_lint] {len(findings)} finding(s) (mailbox skipped: --no-mailbox)")
        for f in findings:
            print(f"  - {f.kind}: {f.detail}")
        return 0

    actionable = [f for f in findings if f.kind not in TRANSITIONAL_KINDS]
    n_transitional = len(findings) - len(actionable)
    if not actionable:
        print(
            f"[echo_corpus_lint] {n_transitional} expected-transitional finding(s) "
            f"(ORG-065 §3.4), nothing actionable — no alert"
        )
        return 0

    signature = _finding_signature(actionable)
    state = _load_state(state_path)
    if not _should_alert(state, signature, now, cooldown_seconds):
        print(
            f"[echo_corpus_lint] {len(actionable)} actionable finding(s), unchanged "
            f"+ within cooldown — throttled"
        )
        return 0

    body = _compose_body(actionable, now, n_transitional)
    _alert_recovery(body, len(actionable), sender, recovery_inbox)
    _save_state(state_path, {"last_alert_ts": now, "last_signature": signature})
    print(f"[echo_corpus_lint] alerted recovery: {len(actionable)} actionable finding(s)")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="echo_corpus_lint.py",
        description=(
            "Companion echo-prismatic ADR obligation scan (T-3495) — "
            "read-only detect + mailbox-alert recovery on findings."
        ),
    )
    p.add_argument("--repo-root", type=Path, default=REPO_ROOT,
                    help="pantheon-config repo root (default: this repo)")
    p.add_argument("--echo-adr-dir", type=Path, default=_ECHO_PRISMATIC_ADR_DIR,
                    help="echo-prismatic ADR dir (default: production path)")
    p.add_argument("--state-path", type=Path, default=STATE_PATH,
                    help="cooldown-state file (default: pantheon state dir)")
    p.add_argument("--cooldown-seconds", type=int, default=ALERT_COOLDOWN_SECONDS,
                    help="minimum seconds between re-alerts of an unchanged finding set")
    p.add_argument("--sender", default=None,
                    help="mailbox sender identity (default: this account's username)")
    p.add_argument("--recovery-inbox", default=RECOVERY_INBOX,
                    help="recovery's mailbox path (default: production path)")
    p.add_argument("--no-mailbox", action="store_true",
                    help="print findings; skip the mailbox alert (manual/preview runs)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return run(
        repo_root=args.repo_root.resolve(),
        echo_adr_dir=args.echo_adr_dir,
        state_path=args.state_path,
        cooldown_seconds=args.cooldown_seconds,
        sender=args.sender or getpass.getuser(),
        recovery_inbox=args.recovery_inbox,
        send_mailbox=not args.no_mailbox,
    )


if __name__ == "__main__":
    sys.exit(main())
