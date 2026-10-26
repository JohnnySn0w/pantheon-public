#!/usr/bin/env python3
"""failed_unit_reconcile — find unit failures whose ALERT never arrived (T-2362).

WHY (T-2358). `pantheon-unit-failure-alert@` is the last-resort visibility channel
for every unattended seat, and on 2026-07-28 it lost 9 alerts. The mechanism was not
a defect in the alert path: EVERY unit on that seat was hanging, alerts included,
with genuine ~60s timeouts. The channel shares a failure domain with the thing it
reports on — same host, filesystem, interpreter, import path, hub — so it is down in
exactly the correlated case it exists for, by construction rather than by accident.

That made in-band mitigation pointless (an alert-on-the-alert runs on the same
stalled seat; a disk breadcrumb writes to the same stalled disk), and T-2358 decided
to accept the in-band loss and move visibility to an INDEPENDENT surface. This is
that surface.

⚠️ THE GAP IT CLOSES, precisely: Recovery cannot currently distinguish "peer had no unit
failures" from "peer had failures whose alerts died on the way out". Both present as
SILENCE and they mean opposite things. Verified when T-2358 was decided: repo-wide,
nothing ran `systemctl --failed` or `is-failed` against another account, so the
in-band alert was the ONLY visibility a peer seat had.

WHAT MAKES POST-HOC RECONCILIATION POSSIBLE AT ALL: systemd LATCHES the failed
state. It outlives the outage that suppressed the alert, so a sweep run afterwards
can still see what was lost — which is why this works despite running long after the
event, and why it must never `reset-failed` (that is what destroys the evidence).

    for each account:  systemctl --user list-units --failed   -> {units} | UNKNOWN
    against:           recovery's mailbox, subjects "unit-failure: <unit>"
    report:            failed units with NO alert ever received  <- the asymmetry

⚠️ TRI-STATE PER ACCOUNT IS LOAD-BEARING. "Could not reach the seat" is NOT "no
failed units". Collapsing them would reproduce T-2309's defect here, and in this
tool the blind value would produce a confident all-clear — the worst possible
direction for something whose whole job is detecting silence. Unreachable accounts
are reported as UNKNOWN and never counted as clean.

Report-only by design. No reset-failed, no remediation: clearing the latched state
is a judgement, and it is the act that destroys the only remaining evidence.

Usage:
    python3 tools/failed_unit_reconcile.py            # sweep all accounts
    python3 tools/failed_unit_reconcile.py --account claude-research
    python3 tools/failed_unit_reconcile.py --json
"""
from __future__ import annotations

import argparse
import getpass
import json
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.agent_role_map import account_persona_map, mailbox_hub  # noqa: E402

RECOVERY_INBOX = pathlib.Path(mailbox_hub()) / "recovery"  # T-4096: hub from instance.yaml
PROBE_TIMEOUT_SECONDS = 20

# Sentinel distinct from both "no failures" and an empty set.
UNKNOWN = "UNKNOWN"

_SUBJECT_RE = re.compile(r"^unit-failure:\s*(?P<unit>\S+)")


def failed_units(account: str, *, timeout: int = PROBE_TIMEOUT_SECONDS):
    """{unit names} for `account`, or UNKNOWN if the seat could not be asked.

    An empty SET means "asked, nothing failed". UNKNOWN means "did not get an
    answer" — sudo denied, no user manager, timeout, tooling missing. The two must
    never merge: this tool exists to detect silence, so a blind read that returned
    an empty set would manufacture exactly the all-clear it is meant to catch.
    """
    inner = "XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user list-units --failed --plain --no-legend"
    argv = (["bash", "-lc", inner] if account == getpass.getuser()
            else ["sudo", "-n", "-u", account, "bash", "-lc", inner])
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return UNKNOWN
    if r.returncode != 0:
        return UNKNOWN
    out = set()
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # `--plain --no-legend` gives: UNIT LOAD ACTIVE SUB DESCRIPTION
        unit = line.split()[0]
        if unit.endswith(".service") or unit.endswith(".timer"):
            out.add(unit)
    return out


def alerted_units(inbox: pathlib.Path = RECOVERY_INBOX) -> dict[str, set[str]]:
    """{sender account -> {units we HAVE received an alert for}}.

    Reads both cur/ (consumed) and new/ (not yet drained) — an alert sitting
    undrained still ARRIVED, and counting only cur/ would report a false asymmetry
    for anything delivered since the last consume.
    """
    seen: dict[str, set[str]] = {}
    for sub in ("cur", "new"):
        d = inbox / sub
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if not f.is_file():
                continue
            try:
                msg = json.loads(f.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            m = _SUBJECT_RE.match(str(msg.get("subject") or ""))
            if not m:
                continue
            seen.setdefault(str(msg.get("sender") or "?"), set()).add(m.group("unit"))
    return seen


def reconcile(accounts=None, *, inbox: pathlib.Path = RECOVERY_INBOX) -> dict:
    """Per account: failed units, and which of them we never got an alert for."""
    mapping = account_persona_map()
    accounts = list(accounts or mapping.keys())
    alerts = alerted_units(inbox)
    result = {"unreachable": [], "clean": [], "accounted": {}, "SILENT": {}}
    for acct in sorted(accounts):
        persona = mapping.get(acct, acct)
        failed = failed_units(acct)
        if failed is UNKNOWN:
            result["unreachable"].append(acct)
            continue
        if not failed:
            result["clean"].append(acct)
            continue
        # Alerts are sent under the PERSONA name, not the unix account.
        got = alerts.get(persona, set()) | alerts.get(acct, set())
        silent = sorted(u for u in failed if u not in got)
        if silent:
            result["SILENT"][acct] = silent
        accounted = sorted(u for u in failed if u in got)
        if accounted:
            result["accounted"][acct] = accounted
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--account", action="append", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    r = reconcile(args.account)
    if args.json:
        print(json.dumps(r, indent=2, sort_keys=True, default=sorted))
        return 1 if r["SILENT"] else 0

    print(f"[failed_unit_reconcile] clean={len(r['clean'])} "
          f"unreachable={len(r['unreachable'])} "
          f"accounted={sum(len(v) for v in r['accounted'].values())} "
          f"SILENT={sum(len(v) for v in r['SILENT'].values())}")
    if r["unreachable"]:
        print("  ⚠️ UNKNOWN — could not ask these seats. NOT the same as 'no "
              "failures'; they are simply unmeasured:")
        for a in r["unreachable"]:
            print(f"    {a}")
    if r["accounted"]:
        print("  ok — failed, and an alert for it DID reach recovery (already visible):")
        for a, units in sorted(r["accounted"].items()):
            for u in units:
                print(f"    {a}  {u}")
    if not r["SILENT"]:
        print("  no silent failures — every latched failure has a matching alert")
        return 0
    print("  ⛔ SILENT FAILURES — latched failed, and NO alert ever reached recovery. "
          "This is the case the in-band channel cannot cover (T-2358):")
    for a, units in sorted(r["SILENT"].items()):
        for u in units:
            print(f"    {a}  {u}")
    print("  Triage per unit; do NOT reset-failed before reading the journal — "
          "clearing the latch destroys the only remaining evidence.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
