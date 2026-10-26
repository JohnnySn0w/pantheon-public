#!/usr/bin/env python3
"""CI-runner disk monitor: CT202 (pve2 `ci-runner`) rootfs + /ci-cache free space (T-4176).

WHY. CT202 hosts the three self-hosted runners (pantheon-config, echo-prismatic,
revenue-project) on ONE rootfs. On 2026-09-28 an echo coverage build filled it (ENOSPC):
the echo job failed and a pantheon-config Runner.Worker hung at startup, holding the
only pantheon-config runner for 57 min with ~20 runs queued behind it (T-4170/T-4174).
Nothing watched that disk; tools/disk_usage_monitor.py covers swarm's own / only.

WHAT. One `df -P / /ci-cache` inside CT202 per fire (hourly Recovery timer), run over
`ssh pve2` via `nsenter -a` into the container's init pid under a remote timeout
(T-4484; `pct exec` hangs under load).
Classify each mount like disk_usage_monitor (WARN >= 85%, URGENT >= 92%) and, on a
breach, mail RECOVERY (pantheon-internal: never the operator) with a best-effort top-consumers
breakdown. Throttled per class; an OK reading clears the throttle so the next breach
alerts at once.

FAIL LOUD. A probe that cannot produce a reading (ssh/nsenter failure, unparseable output,
a mount missing from the output) is its own alert class and never reads as OK. If even
that mail cannot be sent, exit 2 so the unit's OnFailure route pages Recovery.

Default-safe (T-0261): refuses a flagless run. --dry-run classifies and prints only.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import tailnet_self  # noqa: E402  (T-5385)

SSH_HOST = os.environ.get("PANTHEON_CI_RUNNER_SSH_HOST", "pve2")
CT_ID = os.environ.get("PANTHEON_CI_RUNNER_CT", "202")
MOUNTS = ("/", "/ci-cache")
WARN_PCT = int(os.environ.get("PANTHEON_CI_RUNNER_DISK_WARN_PCT", "85"))
URGENT_PCT = int(os.environ.get("PANTHEON_CI_RUNNER_DISK_URGENT_PCT", "92"))
COOLDOWN_SECONDS = int(os.environ.get("PANTHEON_CI_RUNNER_DISK_COOLDOWN_SECONDS", str(6 * 3600)))
PROBE_TIMEOUT = 60
CONSUMERS_TIMEOUT = 150  # CT202's disk is seek-bound HDD; du is slow, and best-effort
STATE_PATH = Path(os.environ.get("PANTHEON_STATE_DIR", str(Path.home() / ".local" / "state" / "pantheon"))) \
    / "ci_runner_disk_state.json"
CONSUMER_PATHS = (
    "/opt/actions-runner-pc/_work", "/opt/actions-runner-pc/_diag",
    "/opt/actions-runner-pc-light/_work", "/opt/actions-runner-pc-light/_diag",  # T-4355
    "/opt/actions-runner-echo-prismatic/_work", "/opt/actions-runner-echo-prismatic/_diag",
    "/opt/actions-runner-echo-light/_work", "/opt/actions-runner-echo-light/_diag",  # T-4355
    "/opt/actions-runner-revenue-project/_work", "/home/runner/.cargo", "/home/runner/.cache",
    "/ci-cache",
)


class ProbeError(RuntimeError):
    """The probe could not produce a trustworthy reading."""


def _log(msg: str) -> None:
    print(f"[ci_runner_disk_monitor] {msg}", flush=True)


# T-4484: margin between the REMOTE bound on the command and the local ssh timeout, so
# the remote `timeout` fires first and the local one only catches a hung ssh itself.
REMOTE_TIMEOUT_MARGIN = 15


def _remote_command(cmd: tuple[str, ...], *, timeout: int) -> str:
    """The shell line pve2 runs: enter CT202's namespaces via its init pid and run `cmd`
    under a REMOTE timeout. (T-4484)

    Not `pct exec`: it hangs when CT202 is loaded, and the hourly probe then read as
    PROBE FAILED on a healthy disk. `nsenter -a` against the container's init pid (from
    `lxc-info -p -H`) answers under the same load. The bound lives INSIDE the remote
    command because a local timeout kills only the ssh client, which would leave the
    probe running on pve2's seek-bound HDD. An empty pid fails the line before
    nsenter runs.
    """
    remote = max(timeout - REMOTE_TIMEOUT_MARGIN, 5)
    return (f"P=$(timeout 10 lxc-info -n {shlex.quote(CT_ID)} -p -H) && [ -n \"$P\" ] && "
            f"exec timeout {remote} nsenter -t \"$P\" -a -- {shlex.join(cmd)}")


def _ct_exec(*cmd: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", SSH_HOST,
         _remote_command(cmd, timeout=timeout)],
        capture_output=True, text=True, timeout=timeout,
    )


def parse_df(output: str) -> dict[str, int]:
    """`df -P` -> {mountpoint: used_pct}. Raises ProbeError on any missing mount."""
    usage: dict[str, int] = {}
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6 and parts[4].endswith("%") and parts[4][:-1].isdigit():
            usage[parts[5]] = int(parts[4][:-1])
    missing = [m for m in MOUNTS if m not in usage]
    if missing:
        raise ProbeError(f"df output lacks {missing}: {output.strip()[:200]!r}")
    return usage


def probe(run=_ct_exec) -> dict[str, int]:
    try:
        r = run("df", "-P", *MOUNTS, timeout=PROBE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ProbeError(f"probe did not complete: {exc!r}") from exc
    if r.returncode != 0:
        raise ProbeError(f"probe exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]!r}")
    return parse_df(r.stdout)


def classify(pct: int) -> str:
    return "URGENT" if pct >= URGENT_PCT else "WARN" if pct >= WARN_PCT else "OK"


def top_consumers(run=_ct_exec) -> str:
    """Best-effort sizes of the usual suspects; never raises."""
    try:
        r = run("du", "-xsh", *CONSUMER_PATHS, timeout=CONSUMERS_TIMEOUT)
        lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
        return "\n".join(lines) or f"(no du output; rc={r.returncode})"
    except Exception as exc:  # noqa: BLE001 -- the breakdown must never break the alert
        return f"(top consumers unavailable: {exc!r})"


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state))
    except OSError as exc:
        _log(f"state write failed (non-fatal): {exc!r}")


def _in_cooldown(state: dict, cls: str, now: float) -> bool:
    last = state.get(cls)  # absent = never alerted (not "alerted at epoch 0")
    return last is not None and now - last < COOLDOWN_SECONDS


def _mail_recovery(subject: str, body: str, severity: str) -> None:
    from tools import mailbox
    from tools.agent_role_map import mailbox_hub
    mailbox.send(f"{mailbox_hub()}/recovery", body, "recovery", subject=subject, severity=severity)


def check(*, dry_run: bool, now: float | None = None, run=_ct_exec, send=_mail_recovery) -> int:
    """One fire. Returns 0, or 2 when an alert that had to go out could not be sent."""
    now = time.time() if now is None else now
    state = _load_state()
    try:
        usage = probe(run)
    except ProbeError as exc:
        _log(f"PROBE FAILED: {exc}")
        off = tailnet_self.off_tailnet()
        if off:  # T-5385: this host is off the tailnet; nothing is known about CT202's disk
            if not dry_run:
                tailnet_self.report_off_tailnet("ci-runner-disk", off, detail=str(exc))
            return 0
        if dry_run:
            return 0
        if _in_cooldown(state, "PROBE_FAILED", now):
            _log("probe-failure alert suppressed (cooldown)")
            return 0
        try:
            send("ci-runner-disk: PROBE FAILED (CT202 disk is UNWATCHED)",
                 f"The CT202 free-space probe could not read the disk: {exc}\n"
                 f"This is NOT an OK reading. Check `ssh {SSH_HOST} "
                 f"'{_remote_command(('df', '-P', *MOUNTS), timeout=PROBE_TIMEOUT)}'` "
                 "(pve2 reach, CT202 running). T-4176/T-4484.", "normal")
        except Exception as mail_exc:  # noqa: BLE001
            _log(f"cannot mail recovery about the probe failure: {mail_exc!r}")
            return 2
        state["PROBE_FAILED"] = now
        _save_state(state)
        return 0

    state.pop("PROBE_FAILED", None)
    tailnet_self.note_reachable()
    worst = max(usage.values())
    cls = classify(worst)
    _log(" ".join(f"{m}={p}%" for m, p in usage.items()) + f" -> {cls}")
    if cls == "OK":
        for k in ("WARN", "URGENT"):
            state.pop(k, None)
        _save_state(state)
        return 0
    if dry_run:
        return 0
    if _in_cooldown(state, cls, now):
        _log(f"{cls} alert suppressed (cooldown)")
        _save_state(state)
        return 0
    body = (
        f"CT202 (pve2 ci-runner) disk {cls}: "
        + ", ".join(f"{m} {p}%" for m, p in usage.items())
        + f" (WARN {WARN_PCT}% / URGENT {URGENT_PCT}%).\n"
        "Three runners share this rootfs; a full disk hangs pantheon-config CI (T-4170/T-4174).\n"
        f"Top consumers:\n{top_consumers(run)}\n"
        "Action (recovery): reclaim runner junk (_diag >3d, _work/_update) or the owning build's "
        "target, coordinate with the repo owner; see bugs/heavy-build-on-live-ci-runner-*.md."
    )
    try:
        send(f"ci-runner-disk-{cls.lower()}: CT202 at {worst}%", body,
             "urgent" if cls == "URGENT" else "normal")
    except Exception as exc:  # noqa: BLE001
        _log(f"cannot mail recovery: {exc!r}")
        return 2
    state[cls] = now
    _save_state(state)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--check", action="store_true", help="probe + mail recovery on breach (timer path)")
    p.add_argument("--dry-run", action="store_true", help="probe + classify + print, send nothing")
    args = p.parse_args(argv)
    if not (args.check or args.dry_run):
        print("ERROR: refusing to run with no mode flag. Pass --check (timer path) "
              "or --dry-run to preview.", file=sys.stderr)
        return 64
    return check(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
