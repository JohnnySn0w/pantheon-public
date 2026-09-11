"""tools/consumer_liveness_monitor.py — a live inbox-consumer that SILENTLY stops
firing, while its queue is EMPTY, is invisible to every queue-keyed detector (T-2605).

THE GAP, stated as one sentence because it is one gap wide: a consumer that stops
completing cycles WITH AN EMPTY QUEUE produces no unread mail, so nothing that keys on
queue depth ever speaks. T-2237 killed the severity half (senders classify, the monitor
keys on urgent-undelivered); this is its mirror — the death that leaves NOTHING queued
behind it, so there is no message to carry a severity and no unread count to trip.

WHY THE THREE EXISTING SURFACES DO NOT COVER IT (verified 2026-08-05, re-confirmed here):
  - mail_staleness_monitor keys on unread>0. Empty queue => no signal, by design.
  - delivery_reachability (T-2232) checks PRECONDITIONS (unit installed+enabled, target
    present) and DELIBERATELY never reads consumer outcomes. An enabled timer that stopped
    FIRING still reads READY there — "can it receive" is not "is it still running".
  - channel_liveness_monitor watches the Telegram inbound POLLERS (bot.pid), not a
    mailbox/inbox consumer timer.

AND WHY THE TWO systemd-native guards do not cover it either — this is the load-bearing
scoping, because a detector that duplicates an existing alarm is noise, and one that
claims a slice already owned is a false safety net:
  - `OnFailure=pantheon-unit-failure-alert@` fires when a RUN fails. `TimeoutStartSec=5min`
    on the service means a HUNG cycle is killed at 5 min => Result=timeout => OnFailure
    pages Recovery fast. So the hung-run path is already watched. A timer that simply STOPS
    FIRING never starts a run, so nothing fails, so OnFailure never fires. That is the hole.
  - verify_startup C8 catches a timer with NextElapse=infinity / never-fired — but only AT
    BOOT, and only the never-armed shape. A timer that fired for weeks and then went quiet
    (a wedged user-manager, the T-1096 "OnBootSec-only, override-added-late" stall class)
    is not a boot event and is not never-fired; nothing continuous watches it.

So the uncovered slice is exactly: an ENABLED inbox-consumer timer whose service has
stopped completing cycles, on a QUIET queue. This module watches that, and only that.

THE SIGNAL — and why it is honest on an empty queue. inbox_consumer runs to completion
every cycle even when the inbox is empty (it logs skip:empty_inbox and exits 0), so the
oneshot service's `ExecMainExitTimestamp` ADVANCES on every 10-min fire regardless of
queue depth. A cycle that never runs (timer stopped firing) or hangs (killed at 5 min,
but the PREVIOUS good exit is what we measure) leaves that timestamp frozen. So
`now - ExecMainExitTimestamp` is a queue-independent "how long since this consumer last
completed a cycle". That is the mechanism the ticket asked for: last-consume recency, NOT
age-of-mail. It is read from a surface OTHER than the queue.

⭐ FRESHNESS, the ticket's sharpest correctness point: "a liveness answer true at T says
nothing at T+cadence." This module stores NO cached liveness verdict. Every check reads the
CURRENT ExecMainExitTimestamp and computes `now - exit` against `now` at check time — the
verdict is recomputed from scratch each run, so a reading that was fresh at T becomes STALLED
at T+threshold with no code change and no stored "alive=true" to rot. classify_agent() is a
pure function of (enablement, exit_epoch, now); test_freshness_same_exit_flips_when_now_advances
pins exactly this by feeding one exit_epoch at two different `now`s.

FIVE VERDICTS:
    LIVE      timer enabled and it completed a cycle within the stall threshold. Green.
    STALLED   timer ENABLED but no completed cycle within the threshold — the consumer has
              silently stopped, and the empty-queue detectors cannot see it. ALERT.
    DISABLED  the timer is switched off. Somebody ran `disable` — an authorised no. Report.
    NO_TIMER  no inbox-consumer unit on that account AT ALL. delivery_reachability (MISSING)
              owns this alarm; re-paging it here would be a duplicate. Report, never alert.
    UNKNOWN   could not query the manager, or the service has no completed-run timestamp yet
              (never fired / just enabled — verify_startup C8 owns never-fired). ALERT, but
              STREAK-GATED: a single sudo/timeout glitch is not an incident. Never silent-green.

⚠️ TEMPLATED UNITS ARE PER-ACCOUNT (the delivery_reachability trap). Querying
`pantheon-inbox-consumer@<agent>.timer` from ONE account reports nothing for every agent —
an instance is not listed until instantiated on ITS OWN manager. Every probe here runs
`sudo -u <account> ... systemctl --user` against that account's own manager, and "could not
ask" (rc!=0) is kept strictly distinct from "asked, found nothing" (rc==0, empty) — merging
them is precisely how delivery_reachability once fabricated a MISSING for a live timer.

AUTHORISED NO. A planned pause (the TG-6694 pause-manifest class) is a legitimate quiet:
`<state>/consumer-liveness.disabled` mutes all agents, `consumer-liveness.<agent>.disabled`
mutes one. Both are JSON {reason, until}; past `until`, or unparseable, the mute lapses and
the monitor speaks again — it fails TOWARD alerting.
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AGENT = "recovery"  # recovery-owned verdicts route per route_failure_alert (via-Recovery for others)

CONSUMER_TIMER = "pantheon-inbox-consumer@{agent}.timer"
CONSUMER_SERVICE = "pantheon-inbox-consumer@{agent}.service"
CONSUMER_TEMPLATE = "pantheon-inbox-consumer@.timer"
PROBE_TIMEOUT = 30

# The inbox-consumer timer fires every OnUnitActiveSec=10min (tools/systemd/
# pantheon-inbox-consumer@.timer). Kept as a constant rather than parsed live: the cadence is
# a stable, load-bearing value, and live-parsing TimersMonotonic is fragile. IF the unit's
# cadence changes, change this in lockstep (the same discipline mail_staleness_monitor keeps
# for its SAMPLE_INTERVAL_SECONDS).
CADENCE_SECS = int(os.environ.get("PANTHEON_CONSUMER_LIVENESS_CADENCE_SECS", "600"))
# STALLED once no completed cycle for this many cadences. 3 => ~30 min: survives one skipped
# fire and a just-restarted manager's first-fire lag, while a genuine wedge still surfaces
# within ~30-35 min (vs the current: never). The threshold IS the hysteresis for STALLED, so
# STALLED is NOT additionally streak-gated — it is measured against a real timestamp, not a
# flapping poll, and erring toward alerting is the safe direction for a liveness guard.
STALL_MULTIPLE = int(os.environ.get("PANTHEON_CONSUMER_LIVENESS_STALL_MULTIPLE", "3"))
# UNKNOWN (could-not-ask / no-timestamp-yet) MUST persist this many consecutive checks before
# it alerts — a transient sudo denial or a manager mid-reload reads UNKNOWN for a poll or two
# and must not page. Mirrors fleet_host_health's T-2717 UNKNOWN streak-gate exactly.
UNKNOWN_ALERT_STREAK = int(os.environ.get("PANTHEON_CONSUMER_LIVENESS_UNKNOWN_STREAK", "3"))
DEFAULT_COOLDOWN_SECS = int(os.environ.get("PANTHEON_CONSUMER_LIVENESS_COOLDOWN_SECS", str(6 * 3600)))

_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
_STATE_NAME = "consumer-liveness.json"
_DISABLE_NAME = "consumer-liveness.disabled"  # fleet-wide mute
_LABEL = "consumer-liveness"

LIVE, STALLED, DISABLED, NO_TIMER, UNKNOWN = "LIVE", "STALLED", "DISABLED", "NO_TIMER", "UNKNOWN"
ALERTING = (STALLED, UNKNOWN)


def _log(msg: str) -> None:
    print(f"[consumer_liveness_monitor] {msg}", file=sys.stderr)


def _me() -> str:
    """This process's own account. sudo-to-self is a grant nobody made, so the probe must
    recognise its own seat and skip the wrapper (see Ops._sudo_user)."""
    try:
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:
        return os.environ.get("USER", "")


def _substrate() -> dict:
    """agent -> account, from mailbox_wake.AGENT_PANE (one owner for the substrate map, as
    delivery_reachability and mail_staleness_monitor both reuse it). A missing map yields {}
    and every agent reports UNKNOWN, which alerts — the honest outcome when the seat roster
    cannot even be enumerated."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import mailbox_wake as mw
    except Exception:
        return {}
    out = {}
    for agent, cfg in getattr(mw, "AGENT_PANE", {}).items():
        home = cfg.get("home") or ""
        out[agent] = Path(home).name or None
    return out


def _seat_inventory() -> list[str]:
    """The agents whose inbox-consumers exist to be watched — DELIVERY_MODE's keys, the same
    total seat map delivery_reachability iterates. Lazy import: inbox_consumer carries live
    Telegram/tmux side-effect surface we must not pull in at module scope on the real host."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from inbox_consumer import DELIVERY_MODE
        return sorted(DELIVERY_MODE.keys())
    except Exception:
        return []


def _parse_systemd_ts(val: str) -> int | None:
    """A systemd timestamp property value -> epoch seconds, or None for empty/never/unparseable.

    We query with --timestamp=unix so timestamp-typed properties render as '@<epoch>'; the
    human fallback ('Sun 2026-08-09 17:02:09 UTC') is parsed as UTC because these hosts report
    UTC (the value ends in ' UTC'). 'n/a'/empty/0 => the run never happened => None."""
    val = (val or "").strip()
    if not val or val.lower() in ("n/a", "0"):
        return None
    if val.startswith("@"):
        try:
            return int(float(val[1:]))
        except ValueError:
            return None
    for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %Z"):
        try:
            return calendar.timegm(time.strptime(val, fmt))
        except ValueError:
            continue
    return None


class Ops:
    """Every side effect behind one seam, so classify_agent() is pure decision (and testable).
    Every probe is READ-ONLY — nothing here enables, disables, injects or restarts."""

    def now(self) -> int:
        return int(time.time())

    def _sudo_user(self, account: str, cmd: str) -> tuple[int, str]:
        """Run `cmd` on `account`'s own user manager. Returns (rc, stdout).

        NOT wrapped in sudo when account IS us: `sudo -n -u <self>` from our own account is
        DENIED (recovery's NOPASSWD covers the OTHER pantheon accounts; sudo-to-self is a separate
        grant nobody made), and that denial leaves stdout EMPTY — byte-identical to a clean
        query that found nothing. Callers MUST distinguish rc!=0 from empty output; conflating
        them is how a probe fabricates a fault for its own live seat."""
        full = f"XDG_RUNTIME_DIR=/run/user/$(id -u) {cmd}"
        argv = (["bash", "-c", full] if account == _me()
                else ["sudo", "-n", "-u", account, "bash", "-c", full])
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError):
            return 255, ""
        return r.returncode, r.stdout

    def timer_enablement(self, account: str, agent: str) -> str | None:
        """'enabled' / 'disabled' / 'missing' on THAT account's manager, or None if unaskable.

        Checks the TEMPLATE's presence separately from the INSTANCE's enablement — that split
        IS the NO_TIMER-vs-DISABLED line: an instance of a template nobody installed is not
        'disabled', it does not exist."""
        rc, out = self._sudo_user(
            account, f"systemctl --user list-unit-files '{CONSUMER_TEMPLATE}' --no-legend")
        if rc != 0:
            return None  # could not ASK (denied sudo / dead manager / timeout) — never "absent"
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
        # is-enabled on an uninstantiated instance of an INSTALLED template answers with the
        # template's preset, which is not an installation. Anything unrecognised is unaskable,
        # not a guess — UNKNOWN alerts (streak-gated), a wrong guess does not.
        return None

    def service_last_exit(self, account: str, agent: str) -> dict | None:
        """{'exit_epoch': int|None, 'result': str, 'active_state': str} for the consumer
        service, or None if the manager could not be queried at all.

        exit_epoch is when the oneshot last EXITED — i.e. a cycle ran to completion. None here
        means it has no completed run yet (never fired / just enabled)."""
        rc, out = self._sudo_user(
            account,
            f"systemctl --user show --timestamp=unix {CONSUMER_SERVICE.format(agent=agent)} "
            f"-p ExecMainExitTimestamp -p Result -p ActiveState")
        if rc != 0:
            return None
        fields: dict[str, str] = {}
        for line in (out or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                fields[k.strip()] = v.strip()
        # An empty property block (no ExecMainExitTimestamp key at all) means the unit is
        # unknown to this manager — treat as unaskable rather than "never ran", so we do not
        # silently green a seat we could not actually read.
        if "ExecMainExitTimestamp" not in fields:
            return None
        return {
            "exit_epoch": _parse_systemd_ts(fields.get("ExecMainExitTimestamp", "")),
            "result": fields.get("Result", ""),
            "active_state": fields.get("ActiveState", ""),
        }

    def alert(self, msg: str, vocab: str, agent: str) -> None:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from inbox_consumer import route_failure_alert  # late: heavy side-effect surface
            route_failure_alert(agent, msg, vocab=vocab)
        except Exception as exc:  # an alert path must never crash the probe
            _log(f"alert delivery FAILED ({exc!r}); verdict still recorded in state")


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except OSError as exc:
        _log(f"state write failed: {exc!r}")


def disabled_reason(state_dir: Path, now: int, name: str) -> str | None:
    """The AUTHORISED no, or None. Expired/malformed/absent all mean NOT disabled.
    Fails toward alerting: a mute that outlives its reason is how a monitor dies quietly."""
    data = _read_json(state_dir / name)
    if not data:
        return None
    until = data.get("until")
    try:
        expiry = int(datetime.fromisoformat(str(until).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None
    if expiry <= now:
        return None
    reason = str(data.get("reason") or "no reason recorded")
    return f"{reason} (until {until})"


def classify_agent(agent: str, *, account: str | None, enablement: str | None,
                   service: dict | None, now: int,
                   cadence: int = CADENCE_SECS, stall_multiple: int = STALL_MULTIPLE) -> dict:
    """Pure decision for one agent. Returns {agent, verdict, detail}. No I/O.

    Reads `now` and `service['exit_epoch']` fresh each call and derives the verdict from their
    DIFFERENCE — there is no stored liveness answer to go stale (the freshness point)."""
    if not account:
        return {"agent": agent, "verdict": UNKNOWN,
                "detail": f"{agent}: no account known (absent from the substrate map) — unmeasurable."}
    if enablement is None:
        return {"agent": agent, "verdict": UNKNOWN,
                "detail": f"{agent}: could not query {account}'s user manager (denied sudo / dead "
                          f"manager / timeout). Liveness is unmeasurable — NOT a green result."}
    if enablement == "missing":
        return {"agent": agent, "verdict": NO_TIMER,
                "detail": f"{agent}: no inbox-consumer unit on {account}. delivery_reachability "
                          f"(MISSING) owns this alarm — not re-paged here."}
    if enablement == "disabled":
        return {"agent": agent, "verdict": DISABLED,
                "detail": f"{agent}: inbox-consumer timer on {account} is switched OFF — an "
                          f"authorised no, not a stall."}

    # enabled from here.
    if service is None:
        return {"agent": agent, "verdict": UNKNOWN,
                "detail": f"{agent}: timer enabled but the service state on {account} could not "
                          f"be read — unmeasurable, NOT green."}
    exit_epoch = service.get("exit_epoch")
    if not isinstance(exit_epoch, int):
        # T-3099: exit_epoch is ALSO None during the oneshot's ~3s activation window —
        # systemd clears the prior ExecMainExitTimestamp when a new run activates. If the
        # unit is CURRENTLY running (a cycle in progress), this is a mid-fire sample, NOT a
        # never-fired consumer. Return LIVE: it is demonstrably alive right now. (A genuinely
        # hung activation times out to 'failed' and then falls through to the UNKNOWN branch
        # below, so this cannot mask a real stall.) Without this, the consumer-liveness timer
        # phase-aligning with the inbox-consumer timer samples it mid-run for 3 polls and
        # false-pages "never fired" — a windowed instrument sampling at the wrong moment.
        active_state = (service.get("active_state") or "").strip()
        if active_state in ("activating", "active", "reloading"):
            return {"agent": agent, "verdict": LIVE,
                    "detail": f"{agent}: consumer is mid-cycle (ActiveState={active_state}); "
                              f"exit-timestamp transiently cleared during activation — alive, "
                              f"not never-fired (T-3099)."}
        return {"agent": agent, "verdict": UNKNOWN,
                "detail": f"{agent}: timer enabled but the consumer has no completed-run "
                          f"timestamp yet (never fired / just enabled). verify_startup C8 owns "
                          f"the never-fired case — held UNKNOWN here, not silently green."}

    threshold = cadence * stall_multiple
    staleness = now - exit_epoch
    if staleness > threshold:
        mins, cad_m, thr_m = staleness // 60, cadence // 60, threshold // 60
        extra = ""
        result, active = service.get("result", ""), service.get("active_state", "")
        if result and result != "success":
            extra = f" Last run Result={result}"
        if active:
            extra += f" (ActiveState={active})"
        return {"agent": agent, "verdict": STALLED,
                "detail": (f"{agent}: inbox-consumer on {account} last COMPLETED a cycle {mins} min "
                           f"ago (cadence {cad_m} min, threshold {thr_m} min). The timer is enabled "
                           f"but has stopped delivering cycles — a stall the empty-queue detectors "
                           f"cannot see, and OnFailure does not catch because no run started to "
                           f"fail.{extra}")}
    return {"agent": agent, "verdict": LIVE,
            "detail": f"{agent}: inbox-consumer completed a cycle {staleness // 60} min ago "
                      f"(<= {threshold // 60} min threshold)."}


def check_agent_once(agent: str, *, ops: Ops, substrate: dict, state_dir: Path, now: int,
                     cooldown_secs: int, dry_run: bool) -> dict:
    """Probe one agent and record/alert. Returns {agent, verdict, detail, alerted}."""
    account = substrate.get(agent)
    muted = (disabled_reason(state_dir, now, _DISABLE_NAME)
             or disabled_reason(state_dir, now, f"consumer-liveness.{agent}.disabled"))
    if muted:
        return _finish({"agent": agent, "verdict": DISABLED,
                        "detail": f"{agent}: authorised quiet: {muted}"},
                       ops=ops, state_dir=state_dir, now=now,
                       cooldown_secs=cooldown_secs, dry_run=dry_run)

    enablement = ops.timer_enablement(account, agent) if account else None
    service = None
    if enablement == "enabled":
        service = ops.service_last_exit(account, agent)
    decided = classify_agent(agent, account=account, enablement=enablement,
                             service=service, now=now)
    return _finish(decided, ops=ops, state_dir=state_dir, now=now,
                   cooldown_secs=cooldown_secs, dry_run=dry_run)


def _finish(decided: dict, *, ops: Ops, state_dir: Path, now: int,
            cooldown_secs: int, dry_run: bool) -> dict:
    """Record this agent's verdict; alert if alert-eligible and the cooldown has lapsed.
    A CHANGED verdict always speaks; only a repeat of the SAME verdict is throttled."""
    agent = decided["agent"]
    verdict = decided["verdict"]
    result = {"agent": agent, "verdict": verdict, "detail": decided["detail"], "alerted": False}

    state_path = state_dir / _STATE_NAME
    full = _read_json(state_path)
    astate = full.get(agent) if isinstance(full.get(agent), dict) else {}

    # UNKNOWN must PERSIST UNKNOWN_ALERT_STREAK checks before it pages — a transient query
    # glitch reads UNKNOWN for a poll or two. Any non-UNKNOWN verdict resets the streak.
    prev = astate.get("unknown_streak")
    prev = prev if isinstance(prev, int) and prev >= 0 else 0
    unknown_streak = prev + 1 if verdict == UNKNOWN else 0
    astate["unknown_streak"] = unknown_streak

    alert_eligible = verdict in ALERTING
    if verdict == UNKNOWN and unknown_streak < UNKNOWN_ALERT_STREAK:
        alert_eligible = False
        result["detail"] = (f"{result['detail']} [suppressed: UNKNOWN streak "
                            f"{unknown_streak}/{UNKNOWN_ALERT_STREAK}]")

    if alert_eligible and not dry_run:
        last_alert = astate.get("last_alert_epoch")
        last_verdict = astate.get("last_alert_verdict")
        due = (not isinstance(last_alert, int)
               or last_verdict != verdict
               or now - last_alert >= cooldown_secs)
        if due:
            ops.alert(f"[{_LABEL}] {result['detail']}",
                      vocab=f"{_LABEL} ({verdict})", agent=agent)
            result["alerted"] = True
            astate["last_alert_epoch"] = now
            astate["last_alert_verdict"] = verdict

    astate["last_verdict"] = verdict
    astate["last_check_epoch"] = now
    full[agent] = astate
    _write_json(state_path, full)
    return result


def check_all(*, ops: Ops | None = None, state_dir: Path | None = None,
              agents: list[str] | None = None, substrate: dict | None = None,
              cooldown_secs: int = DEFAULT_COOLDOWN_SECS, dry_run: bool = False) -> list[dict]:
    ops = ops or Ops()
    state_dir = Path(state_dir) if state_dir else _DEFAULT_STATE_DIR
    sub = substrate if substrate is not None else _substrate()
    seats = agents if agents is not None else _seat_inventory()
    now = ops.now()
    if not seats:
        return [{"agent": "*", "verdict": UNKNOWN, "alerted": False,
                 "detail": "seat inventory could not be read (DELIVERY_MODE unreadable) — the "
                           "roster is unknown, which is not the same as empty."}]
    return [check_agent_once(a, ops=ops, substrate=sub, state_dir=state_dir, now=now,
                             cooldown_secs=cooldown_secs, dry_run=dry_run)
            for a in seats]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--agent", default=None, help="check only this agent (default: all seats)")
    ap.add_argument("--cooldown-secs", type=int, default=DEFAULT_COOLDOWN_SECS)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--dry-run", action="store_true", help="decide and print; never alert")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    agents = None
    if args.agent:
        agents = [args.agent]

    results = check_all(state_dir=Path(args.state_dir) if args.state_dir else None,
                        agents=agents, cooldown_secs=args.cooldown_secs, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(results))
    else:
        for r in results:
            _log(f"{r['verdict']}: {r['detail']}" + (" [ALERTED]" if r.get("alerted") else ""))
    # Exit 0 even when alerting: the ALERT is the output. A non-zero exit would fire OnFailure
    # and page a SECOND time about the monitor that just worked correctly.
    return 0


if __name__ == "__main__":
    sys.exit(main())
