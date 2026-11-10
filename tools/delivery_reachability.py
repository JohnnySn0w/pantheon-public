"""tools/delivery_reachability.py — can each seat ACTUALLY receive a ticket right now? (T-2232)

THE GAP, stated precisely, because it is one word wide: T-2144's guard asserts every persona is
CLASSIFIED — present in DELIVERY_MODE (how it receives) or DELIVERY_EXEMPT (why it does not). That
is a check on the DECLARATION. artisan was declared `send-keys` and had no inbox-consumer unit on its
account at all, so a ticket filed at artisan went nowhere while the guard stayed green. A declaration
is a promise; delivery is a capability; the guard checked the promise.

AND THE OTHER HALF, which is why "the consumer ran fine" was never evidence either: mason's consumer
passed for weeks on `skip:empty_inbox` — it exited before the inject step because there was nothing
to deliver. A run that never exercises the path cannot testify about the path. So THIS MODULE NEVER
READS CONSUMER OUTCOMES. Not once. It asks only about the substrate, because that is the thing whose
absence a green run cannot rule out. (test_never_consults_consumer_outcomes pins that.)

WHAT IT CAN AND CANNOT HONESTLY CLAIM — the distinction the whole design turns on:

    it verifies PRECONDITIONS.   It does not deliver anything and therefore does not
                                 prove delivery. `READY` means "nothing known blocks it".

Proving delivery means injecting into a live agent's pane, which costs that agent a turn and is
exactly the "self-targeted alert" class we do not do. Preconditions are what is honestly checkable,
so READY is deliberately worded as an absence of known blockers rather than a positive guarantee —
overclaiming here would rebuild the very substitution this module exists to break.

EIGHT STATES (HELD added by T-5254; ASLEEP and SLEEP_BREACH by T-6302, see below), and the split between two of them IS the finding:

    READY     unit installed AND enabled, and the target (tmux session / inbox) is present.
    DISABLED  the unit exists and is switched off. SOMEBODY RAN `disable` — a deliberate act, so
              this is an authorised no. Reported, never alerted.
    MISSING   declared deliverable, but no delivery unit exists on that account AT ALL. Nobody
              decided this; it is the gap nothing was watching. ALERT.
    BROKEN    unit enabled but its target is gone (no tmux session / no inbox) — a poke would fail.
              ALERT.
    UNKNOWN   could not determine. ALERT, never silently green.
    HELD      the seat is in an ACTIVE seat-maintenance window (notify_policy, T-2984) — held on
              purpose, so its missing unit/pane is expected. Reported, never alerted, and not
              probed at all. Fail-open: any registry fault reads "not held" and probes normally.

⭐ DISABLED vs MISSING is the load-bearing line, and it is what the TG-6694 pause manifest could not
express. A pause manifest can only list what was STOPPED, so a seat that never had a unit is absent
from it and survives a complete-looking restore. "Switched off" and "never existed" read identically
from every angle except this one: `systemctl --user list-unit-files` distinguishes them, and the
whole point of separating them here is that one is a decision and the other is an omission.

⚠️ TEMPLATED UNITS ARE PER-ACCOUNT. Querying `pantheon-inbox-consumer@<agent>.timer` from ONE
account reports NO UNIT for every agent including healthy ones — an instance is not listed until it
is instantiated on ITS OWN manager. Every probe here runs `sudo -u <account> ... systemctl --user`
against that account's own manager. This trap already produced one clean and entirely wrong table.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

AGENT = "recovery"
CONSUMER_TIMER = "pantheon-inbox-consumer@{agent}.timer"
CONSUMER_TEMPLATE = "pantheon-inbox-consumer@.timer"
PROBE_TIMEOUT = 30

READY, DISABLED, MISSING, BROKEN, UNKNOWN = "READY", "DISABLED", "MISSING", "BROKEN", "UNKNOWN"
HELD = "HELD"  # T-5254: non-alerting; deliberately held seat-maintenance window
# T-6302: a seat the seat-state record marks expected_down (ORG-106 deep-sleep, phase asleep).
# ASLEEP is non-alerting and never READY. SLEEP_BREACH is declared-asleep-but-pane-present: kept
# visible and non-READY, but not in ALERTING, since recovery_watcher / fleet_health own that page.
ASLEEP = "ASLEEP"
SLEEP_BREACH = "SLEEP_BREACH"
ALERTING = (MISSING, BROKEN, UNKNOWN)


def _me() -> str:
    """This process's own account name. sudo-to-self is a grant nobody made, so the probe
    must recognise its own seat and skip the wrapper — see _sudo_user."""
    try:
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:
        return os.environ.get("USER", "")


def _seat_in_maintenance(agent: str) -> bool:
    """T-5254: True iff `agent` is in an ACTIVE seat-maintenance window. Fail-open (False)
    on any fault so a registry problem can never mask a real delivery gap. ORG-106 L4c: a
    shim over the shared seat_state.maintenance_active."""
    return _seat_state_mod().maintenance_active(agent)


def _seat_state_mod():
    """ORG-106 L4c: the seat_state module, importable from a package or a bare run."""
    try:
        from tools import seat_state
    except ImportError:  # bare run with cwd=tools/
        import seat_state  # type: ignore
    return seat_state


def _log(msg: str) -> None:
    print(f"[delivery_reachability] {msg}", file=sys.stderr)


def _substrate() -> dict:
    """agent -> {account, pane}, derived from mailbox_wake.AGENT_PANE.

    One owner for the substrate map (mail_staleness_monitor already reuses it for the same
    reason). Import is lazy and tolerant: a missing map yields {} and every agent reports
    UNKNOWN, which alerts — the honest outcome when we cannot even enumerate the seats."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import mailbox_wake as mw
    except Exception:
        return {}
    out = {}
    for agent, cfg in getattr(mw, "AGENT_PANE", {}).items():
        home = cfg.get("home") or ""
        out[agent] = {"account": Path(home).name or None,
                      "pane": cfg.get("pane_target")}
    return out


def _delivery_modes() -> dict:
    """agent -> declared delivery mode. Lazy import: inbox_consumer carries live
    Telegram/tmux side-effect surface we must not pull in at module scope on the
    real-token host (mailbox_wake.py makes the same choice for the same reason)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from inbox_consumer import DELIVERY_MODE
        return dict(DELIVERY_MODE)
    except Exception:
        return {}


class Ops:
    """The side-effect seam. Every probe is READ-ONLY — nothing here injects, sends or enables."""

    def _sudo_user(self, account: str, cmd: str) -> tuple[int, str]:
        """Run `cmd` on `account`'s own user manager. Returns (rc, stdout).

        ⚠️ NOT WRAPPED IN sudo WHEN account IS US. `sudo -n -u claude-fallback` FROM
        claude-fallback is DENIED — recovery's NOPASSWD grant covers the OTHER pantheon accounts,
        and sudo-to-self is a separate grant nobody made. The denial writes to stderr and
        leaves stdout EMPTY, which is byte-identical to a successful query that found nothing.
        That conflation made this probe report MISSING for recovery, a seat with a demonstrably
        live `pantheon-inbox-consumer@recovery.timer` — a fabricated fault, caught only by running
        it against the real fleet. Callers must therefore distinguish rc != 0 from empty output.
        """
        full = f"XDG_RUNTIME_DIR=/run/user/$(id -u) {cmd}"
        argv = (["bash", "-c", full] if account == _me()
                else ["sudo", "-n", "-u", account, "bash", "-c", full])
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError):
            return 255, ""
        return r.returncode, r.stdout

    def unit_state(self, account: str, agent: str) -> str | None:
        """'enabled'/'disabled'/'missing' on THAT account's manager, or None if unaskable.

        Checks the TEMPLATE's presence separately from the INSTANCE's enablement, because
        that is precisely the MISSING-vs-DISABLED split: an instance of a template nobody
        installed is not 'disabled', it does not exist."""
        rc, out = self._sudo_user(
            account, f"systemctl --user list-unit-files '{CONSUMER_TEMPLATE}' --no-legend")
        # ANY non-zero rc means we could not ASK — a denied sudo, a dead manager, a timeout.
        # Only rc == 0 with empty output is evidence of absence. Reading a failed command's
        # empty stdout as "the unit is not there" is how this reported MISSING for a seat whose
        # timer was live; "could not ask" and "asked, found nothing" must never merge.
        if rc != 0:
            return None
        if not out.strip():
            return "missing"
        rc2, out2 = self._sudo_user(
            account, f"systemctl --user is-enabled {CONSUMER_TIMER.format(agent=agent)} 2>&1")
        text = (out2 or "").strip().splitlines()
        state = text[-1].strip() if text else ""
        if state in ("enabled", "enabled-runtime", "static", "alias", "indirect"):
            return "enabled"
        if state in ("disabled", "masked", "linked"):
            return "disabled"
        # `is-enabled` on an uninstantiated instance of an INSTALLED template answers with the
        # template's preset, which is not an installation. Treat anything unrecognised as
        # unaskable rather than guessing — UNKNOWN alerts, a wrong guess does not.
        return None

    def tmux_session_exists(self, account: str, pane: str) -> bool | None:
        session = (pane or "").split(":", 1)[0]
        if not session:
            return None
        rc, _ = self._sudo_user(account, f"tmux has-session -t {session}; echo RC=$?")
        if rc == 255:
            return None
        # `tmux has-session` answers 1 for "no such session", and the wrapper answers non-zero
        # for "could not ask" — same number, opposite meanings. The inner RC marker separates
        # them: no marker means the shell never ran, which is UNKNOWN, not absent.
        _, out = self._sudo_user(account, f"tmux has-session -t {session} >/dev/null 2>&1; echo RC=$?")
        for line in (out or "").splitlines():
            if line.startswith("RC="):
                return line.strip() == "RC=0"
        return None

    def inbox_exists(self, agent: str) -> bool | None:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import mailbox as mb
            import mailbox_wake as mw
            return bool(mb.exists(Path(mw.MAILBOX_HUB) / agent))
        except Exception:
            return None

    def alert(self, msg: str, vocab: str) -> None:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from inbox_consumer import route_failure_alert
            route_failure_alert(AGENT, msg, vocab=vocab)
        except Exception as exc:
            _log(f"alert delivery FAILED ({exc!r}); verdicts still returned")


def probe_agent(agent: str, mode: str, sub: dict, ops: Ops) -> dict:
    """One agent's reachability. Returns {agent, mode, state, detail}."""
    account, pane = sub.get("account"), sub.get("pane")
    if not account:
        return {"agent": agent, "mode": mode, "state": UNKNOWN,
                "detail": "no account known for this agent (absent from the substrate map)"}

    if mode == "mailbox":
        ok = ops.inbox_exists(agent)
        if ok is None:
            return {"agent": agent, "mode": mode, "state": UNKNOWN,
                    "detail": "could not stat the hub inbox"}
        return ({"agent": agent, "mode": mode, "state": READY,
                 "detail": "hub inbox present; nothing known blocks a poke"} if ok else
                {"agent": agent, "mode": mode, "state": BROKEN,
                 "detail": "declared mailbox delivery but the hub inbox does not exist"})

    unit = ops.unit_state(account, agent)
    if unit is None:
        return {"agent": agent, "mode": mode, "state": UNKNOWN,
                "detail": f"could not query {account}'s own user manager"}
    if unit == "missing":
        return {"agent": agent, "mode": mode, "state": MISSING,
                "detail": (f"declared '{mode}' but NO inbox-consumer unit exists on {account}. "
                           f"Nobody disabled this — it was never installed, so a pause manifest "
                           f"cannot record it and a restore cannot bring it back. A ticket filed "
                           f"at {agent} goes nowhere.")}
    if unit == "disabled":
        return {"agent": agent, "mode": mode, "state": DISABLED,
                "detail": (f"unit present on {account}, timer switched OFF — a deliberate act, "
                           f"so this is an authorised no, not a gap. Needs enabling to receive.")}

    session = ops.tmux_session_exists(account, pane)
    if session is None:
        return {"agent": agent, "mode": mode, "state": UNKNOWN,
                "detail": f"timer enabled but {account}'s tmux could not be queried"}
    if not session:
        return {"agent": agent, "mode": mode, "state": BROKEN,
                "detail": (f"timer ENABLED but tmux session '{(pane or '').split(':')[0]}' does "
                           f"not exist on {account} — every poke would fail into nothing")}
    return {"agent": agent, "mode": mode, "state": READY,
            "detail": "unit enabled and the target session is present; nothing known blocks a poke"}


def probe_asleep(agent: str, mode: str, sub: dict, ops: Ops, seat) -> dict:
    """T-6302: verdict for an expected_down seat. Probes only the tmux session (read-only), so a
    sleep-breach (declared asleep, pane present) is reported rather than hidden by ASLEEP."""
    who = f"{seat.owner or '?'}: {seat.reason or '-'}; until {seat.expires or 'no expiry'}"
    if mode == "mailbox":  # no pane to breach; hub-inbox presence says nothing about sleep
        return {"agent": agent, "mode": mode, "state": ASLEEP,
                "detail": f"seat-state deep-sleep, expected down ({who}); not READY, not a fault"}
    account, pane = sub.get("account"), sub.get("pane")
    session = ops.tmux_session_exists(account, pane) if account else None
    if session is None:
        return {"agent": agent, "mode": mode, "state": UNKNOWN,
                "detail": f"declared asleep ({who}) but its tmux could not be queried"}
    if session:
        return {"agent": agent, "mode": mode, "state": SLEEP_BREACH,
                "detail": (f"declared asleep ({who}) but tmux session "
                           f"'{(pane or '').split(':')[0]}' is present. recovery_watcher "
                           f"SLEEP_BREACH owns the page; reported here, never READY")}
    return {"agent": agent, "mode": mode, "state": ASLEEP,
            "detail": f"seat-state deep-sleep, expected down ({who}); pane absent on purpose"}


def probe_all(ops: Ops | None = None, substrate: dict | None = None,
              modes: dict | None = None) -> list[dict]:
    ops = ops or Ops()
    sub = substrate if substrate is not None else _substrate()
    modes = modes if modes is not None else _delivery_modes()
    if not modes:
        return [{"agent": "*", "mode": "?", "state": UNKNOWN,
                 "detail": "DELIVERY_MODE could not be read — the seat inventory is unknown, "
                           "which is not the same as empty"}]
    out = []
    for a, m in sorted(modes.items()):
        seat = None if _seat_in_maintenance(a) else _seat_state_mod().safe_effective_for(a)
        if _seat_in_maintenance(a):
            out.append({"agent": a, "mode": m, "state": HELD,
                        "detail": "seat-maintenance ACTIVE — held on purpose; not probed (T-5254)"})
        elif seat is not None and seat.expected_down:
            out.append(probe_asleep(a, m, sub.get(a, {}), ops, seat))
        elif seat is not None and seat.remediation_vetoed:
            # ORG-106 L4c: a seat in deep-sleep is down on purpose; its delivery is not a fault.
            out.append({"agent": a, "mode": m, "state": HELD,
                        "detail": f"seat-state {seat.state} — held on purpose; not probed (ORG-106)"})
        else:
            out.append(probe_agent(a, m, sub.get(a, {}), ops))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="report; never alert")
    args = ap.parse_args(argv)

    ops = Ops()
    results = probe_all(ops)
    faults = [r for r in results if r["state"] in ALERTING]

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            _log(f"{r['state']:<9} {r['agent']:<14} {r['detail']}")

    if faults and not args.dry_run:
        lines = "\n".join(f"  {r['state']} {r['agent']}: {r['detail']}" for r in faults)
        ops.alert(f"[delivery-reachability] {len(faults)} seat(s) cannot receive tickets:\n{lines}",
                  vocab="delivery-reachability")
    # Exit 0 regardless: the ALERT is the output. A non-zero exit fires OnFailure and would
    # report the monitor instead of the fault.
    return 0


if __name__ == "__main__":
    sys.exit(main())
