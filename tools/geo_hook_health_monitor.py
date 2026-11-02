#!/usr/bin/env python3
"""Recovery-side liveness monitor for the geo-hook service on pve2 (T-3647).

pve2 has a prior "silently off the tailnet for 2 days" history (T-2392); if the
geo-hook LXC (or pve2) goes dark, the operator's geofence triggers die silently. This
polls the service's GET /health from swarm — an INDEPENDENT failure domain from
pve2 (swarm reaches the LXC over the tailnet, not via pve2) — and mailboxes recovery
after N consecutive failures, with hysteresis so a single blip never alarms and a
sustained outage never re-spams. Recovery sends a clear-note.

Run by a recovery systemd --user timer (geo-hook-health.timer) every 5 min. Alerts go
to RECOVERY's mailbox (recovery-ops concern; Recovery assesses + runs the pve2 recovery
runbook or escalates), NOT auto-relayed to the operator.

Tests: python3 -m unittest tools.test_geo_hook_health_monitor
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # T-4096: the hub path comes from instance.yaml, never a literal.
    from tools.agent_role_map import mailbox_hub
    from tools import tailnet_self
except ImportError:  # script run: tools/ is on sys.path, the repo root is not
    from agent_role_map import mailbox_hub  # type: ignore
    import tailnet_self  # type: ignore

DEFAULT_URL = "http://100.84.210.244:8787/health"  # geo-hook tailnet IP (T-3647)
DEFAULT_THRESHOLD = 3  # consecutive failures before alerting (~15 min at 5-min cadence)
DEFAULT_STATE = Path.home() / ".local" / "state" / "pantheon" / "geo-hook-health.json"
MAILBOX_HUB = mailbox_hub()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def poll(url: str, timeout: float = 8.0) -> tuple[bool, str]:
    """Return (ok, detail). ok iff HTTP 200 and body status == 'ok'."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — fixed tailnet URL
            if resp.status != 200:
                return False, f"http {resp.status}"
            body = json.loads(resp.read().decode(errors="replace"))
            if body.get("status") == "ok":
                return True, "ok"
            return False, f"status={body.get('status')!r}"
    except Exception as exc:  # noqa: BLE001 — any failure is a liveness failure
        return False, f"{type(exc).__name__}: {exc}"[:200]


def _load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"consecutive_failures": 0, "alerting": False, "last_ok": None}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def _send(subject: str, payload: str, sender_dir: Path | None = None) -> None:
    """Mailbox recovery. Import lazily so tests can stub with a fake mailbox module."""
    import mailbox  # tools/ is on sys.path when run as a module or script

    mailbox.send(f"{MAILBOX_HUB}/recovery", sender="geo-hook-monitor", subject=subject, payload=payload)


def evaluate(ok: bool, detail: str, state: dict[str, Any], *, url: str, threshold: int,
             send=_send) -> dict[str, Any]:
    """Advance the hysteresis state machine; fire an alert/recovery mailbox on a
    threshold crossing only. Pure but for the injected `send`. Returns new state."""
    new = dict(state)
    if ok:
        new["last_ok"] = _now()
        if state.get("alerting"):
            send(
                "geo-hook RECOVERED",
                f"geo-hook /health is OK again at {_now()} ({url}); "
                f"was alerting after {state.get('consecutive_failures')} consecutive failures.",
            )
        new["consecutive_failures"] = 0
        new["alerting"] = False
        return new
    new["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
    if new["consecutive_failures"] >= threshold and not state.get("alerting"):
        send(
            f"geo-hook UNREACHABLE ({new['consecutive_failures']}x)",
            f"geo-hook /health has failed {new['consecutive_failures']} consecutive polls "
            f"(threshold {threshold}) as of {_now()}.\nURL: {url}\nLast failure: {detail}\n"
            f"pve2/LXC may be off the tailnet (cf. T-2392). Recovery: check pve2 reachability "
            f"(ssh pve2), the geo-hook.service in CT 204, and tailscale; runbook "
            f"sop/remediation/pve2-hung-box-power-recovery.md if pve2 is hung. Not relayed "
            f"to the operator — escalate only if the outage is sustained + his geofence matters now.",
        )
        new["alerting"] = True
    return new


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Recovery-side geo-hook liveness monitor (T-3647).")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    p.add_argument("--state", default=str(DEFAULT_STATE))
    args = p.parse_args(argv)

    state_path = Path(args.state)
    ok, detail = poll(args.url)
    if ok:
        tailnet_self.note_reachable()
    else:
        off = tailnet_self.off_tailnet()
        if off:  # T-5385: this host is off the tailnet; the poll says nothing about geo-hook
            tailnet_self.report_off_tailnet("geo-hook", off, detail=detail)
            print(f"[geo-hook-health] skipped: this host is off the tailnet ({off}); state unchanged")
            return 0
    new = evaluate(ok, detail, _load_state(state_path), url=args.url, threshold=args.threshold)
    _save_state(state_path, new)
    print(f"[geo-hook-health] ok={ok} detail={detail!r} "
          f"consecutive_failures={new['consecutive_failures']} alerting={new['alerting']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
