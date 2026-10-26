#!/usr/bin/env python3
"""Is THIS host on the tailnet? One answer for every check that reaches a host over 100.x (T-5385).

WHY. On 2026-10-23 swarm's own node key expired (00:18Z, T-5381). tailscaled stayed active but
went NeedsLogin, so every check that reaches a host over the tailnet reported ITS host as down:
pve2 heat, CT202 disk (UNWATCHED), ci-audit-liveness HOST_DOWN pve2, geo-hook UNREACHABLE,
fleet-host-health boxon UNREACHABLE. Seven alerts for one fault, and two sent the reader to the
wrong one. The governing fact was swarm's own BackendState, which none of them read.

USE (on a check's per-host FAILURE path only, so a healthy run costs nothing extra):

    off = tailnet_self.off_tailnet()
    if off:
        tailnet_self.report_off_tailnet("ci-runner-disk", off, detail=str(exc))
        return 0            # not this host's fault: no per-host verdict, no page
    ... the per-host alert as before ...

and on its success path, `tailnet_self.note_reachable()` (re-arms the next outage's alert).

`off_tailnet()` returns the BackendState only when it is DEFINITELY not "Running". An unknown
state (no tailscale CLI, a timeout, unparseable output) returns None, so a check that cannot tell
keeps alerting exactly as before: suppression needs a positive reading. `report_off_tailnet()`
mails recovery ONE "swarm off tailnet" alert per outage, shared by every caller on this account
through a stamp file (flocked), re-sent at most every REALERT_SECS while the outage lasts.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

STATE_PATH = Path(os.environ.get("PANTHEON_STATE_DIR") or Path.home() / ".local/state/pantheon") / "tailnet-self.json"
REALERT_SECS = 6 * 3600
STATUS_TIMEOUT_S = 10


def backend_state(run: Callable = subprocess.run) -> Optional[str]:
    """swarm's own tailscale BackendState ("Running", "NeedsLogin", "Stopped", ...), or None."""
    try:
        r = run(["tailscale", "status", "--json", "--self"], capture_output=True, text=True,
                timeout=STATUS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    # `tailscale status` exits non-zero while logged out but still prints the JSON.
    try:
        state = json.loads(r.stdout or "").get("BackendState")
    except (ValueError, AttributeError):
        return None
    return state if isinstance(state, str) and state else None


def off_tailnet(run: Callable = subprocess.run) -> Optional[str]:
    """The BackendState when this host is definitely off the tailnet, else None."""
    state = backend_state(run)
    return state if state is not None and state != "Running" else None


def _mail_recovery(subject: str, body: str) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools import mailbox
    from tools.agent_role_map import mailbox_hub
    mailbox.send(f"{mailbox_hub()}/recovery", body, "tailnet_self", subject=subject, severity="normal")


def report_off_tailnet(monitor: str, state: str, *, detail: str = "", now: Optional[float] = None,
                       send: Callable[[str, str], None] = _mail_recovery,
                       path: Path = STATE_PATH) -> bool:
    """Record that `monitor` hit the outage; mail recovery once per outage. True if mailed."""
    now = time.time() if now is None else now
    print(f"[tailnet_self] {monitor}: this host is off the tailnet ({state}); per-host verdict "
          f"suppressed: {detail[:200]}", file=sys.stderr)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            doc = {}
        doc.setdefault("since", now)
        doc["state"] = state
        doc.setdefault("monitors", {})[monitor] = now
        last = doc.get("alerted_at")  # absent = never alerted this outage (not "at epoch 0")
        due = last is None or now - last >= REALERT_SECS
        mailed = False
        if due:
            since = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(doc["since"]))
            try:
                send(f"swarm off tailnet ({state}): tailnet-path checks suppressed",
                     f"This host's own tailscale reads BackendState={state} (first seen {since} by "
                     f"{monitor}), so every check that reaches a host over 100.x fails for this one "
                     f"reason. Per-host UNREACHABLE/HOST_DOWN/PROBE FAILED alerts are suppressed "
                     f"until it is Running again; nothing about those hosts is known.\n"
                     f"First detail: {detail[:300]}\n"
                     f"Check `tailscale status`. NeedsLogin = the node key expired: `tailscale login` "
                     f"needs the operator's browser approval, and don't restart tailscaled while a login "
                     f"is pending (T-5381). T-5385.")
                doc["alerted_at"] = now
                mailed = True
            except Exception as exc:  # noqa: BLE001 -- the suppression must not hinge on mail
                print(f"[tailnet_self] could not mail recovery: {exc!r}", file=sys.stderr)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc))
        os.replace(tmp, path)
    return mailed


def note_reachable(path: Path = STATE_PATH) -> None:
    """A tailnet-path check succeeded: the outage (if any) is over; re-arm the next alert."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"[tailnet_self] could not clear {path}: {exc!r}", file=sys.stderr)


if __name__ == "__main__":
    s = backend_state()
    print(s or "unknown")
    raise SystemExit(0 if s == "Running" else 1)
