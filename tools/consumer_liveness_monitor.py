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

UNIT LIST (T-4220). The same signal watches any plain oneshot service + timer registered in
`WATCHED_UNITS` (the ORG-013 cascade sweep on the `claude` seat is the first). A unit carries its
own max-silence threshold, and its account is its OWNER persona's, read through the same
`sudo -u <account> systemctl --user` path as the inbox-consumers — no new cross-account surface.
Units share the verdicts, the per-verdict cooldown, the UNKNOWN streak-gate and the mute files
(`consumer-liveness.<unit-key>.disabled`); NO_TIMER / DISABLED stay report-only, so a silently
deleted or disabled unit timer is NOT paged (usually a deliberate ORG-068 lifecycle act, but this
monitor cannot tell that from an accident, so the report hedges it and names the consequence).
`--unit KEY` checks one unit; an explicit `--agent` checks no unit.

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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class WatchedUnit:
    """A plain (non-templated) oneshot service + its timer, watched for going quiet (T-4220).

    key               state / mute-file / result name. Must not equal a persona name: it shares
                      `consumer-liveness.json` and `consumer-liveness.<key>.disabled` with them.
    service, timer    the unit file names, as `systemctl --user` on the owner's account sees them.
    owner             the persona whose seat runs the unit. Its account comes from `_substrate()`
                      (one seat map), and a page routes as this persona via route_failure_alert.
    max_silence_secs  STALLED once `now - ExecMainExitTimestamp` exceeds this (strictly).
    label             the words an alert uses for the unit.
    consequence       what a dead unit costs, as a clause in the NO_TIMER / DISABLED report's
                      "If this wasn't a deliberate <act> (ORG-068), <consequence>; <fix>" sentence
                      (empty: the clause is omitted).
    """
    key: str
    service: str
    timer: str
    owner: str
    max_silence_secs: int
    label: str
    consequence: str = ""


# UNIT LIST (T-4220). Each entry is a oneshot whose timer can fire for a long time and then stop,
# which OnFailure (no run started, so none failed) and verify_startup C8 (boot-time, never-armed
# only) cannot see. Keep max_silence_secs in lockstep with the timer's cadence: the
# test_consumer_liveness_units registry guard parses the real .timer and fails if they disagree.
#
# cascade-sweep: fires at *:04,19,34,49 (15 min). Normal staleness peaks near 16-17 min (cadence +
# RandomizedDelaySec=60 + a ~1s run). 3600s = 4 cadences: it still reads LIVE across TWO missed
# fires (a user-manager reload, a seat session reset) and pages on the third, ~60-65 min after
# the last completed run given the 5-min poll. A dead sweep only degrades to resolve-time cascade
# (stale depends_on never blocks dispatch), so the extra latency is cheap and a false page is not.
#
# RESIDUAL (accepted, T-4220): a unit whose timer is DISABLED or whose files are REMOVED reads
# DISABLED / NO_TIMER, which is report-only here, so a silently deleted timer is not paged.
# Those are usually deliberate lifecycle acts (ORG-068 disable/remove) and a page-until-muted
# would be noise, but nothing here can tell a deliberate one from an accident: the report says
# only that intent is possible, and names the consequence and the fix. Who owns paging a deleted
# timer is an open ownership gap (a follow-up ticket), deliberately not built in this module.
WATCHED_UNITS: tuple[WatchedUnit, ...] = (
    WatchedUnit(
        key="cascade-sweep",
        service="pantheon-cascade-sweep.service",
        timer="pantheon-cascade-sweep.timer",
        owner="orchestrator",
        max_silence_secs=3600,
        label="ORG-013 cascade sweep",
        consequence="the sweep has degraded to resolve-time-only cascade",
    ),
)


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

    def _enablement(self, account: str, listed: str, timer: str) -> str | None:
        """'enabled' / 'disabled' / 'missing' on THAT account's manager, or None if unaskable.

        Checks that `listed` (the template for an instance, the unit file itself for a plain
        unit) is INSTALLED separately from `timer`'s enablement — that split IS the
        NO_TIMER-vs-DISABLED line: a unit nobody installed is not 'disabled', it does not
        exist."""
        rc, out = self._sudo_user(
            account, f"systemctl --user list-unit-files '{listed}' --no-legend")
        if rc != 0:
            return None  # could not ASK (denied sudo / dead manager / timeout) — never "absent"
        if not out.strip():
            return "missing"
        rc2, out2 = self._sudo_user(account, f"systemctl --user is-enabled {timer} 2>&1")
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

    def _last_exit(self, account: str, service: str) -> dict | None:
        """{'exit_epoch': int|None, 'result': str, 'active_state': str} for `service`, or None
        if the manager could not be queried at all.

        exit_epoch is when the oneshot last EXITED — i.e. a run went to completion. None here
        means it has no completed run yet (never fired / just enabled)."""
        rc, out = self._sudo_user(
            account,
            f"systemctl --user show --timestamp=unix {service} "
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

    def timer_enablement(self, account: str, agent: str) -> str | None:
        """The inbox-consumer instance for `agent`: see `_enablement` (template vs instance)."""
        return self._enablement(account, CONSUMER_TEMPLATE, CONSUMER_TIMER.format(agent=agent))

    def service_last_exit(self, account: str, agent: str) -> dict | None:
        """The inbox-consumer service for `agent`: see `_last_exit`."""
        return self._last_exit(account, CONSUMER_SERVICE.format(agent=agent))

    def unit_enablement(self, account: str, timer: str) -> str | None:
        """A plain (non-templated) timer, installed AND enabled on `account`'s manager."""
        return self._enablement(account, timer, timer)

    def unit_last_exit(self, account: str, service: str) -> dict | None:
        """A plain oneshot service's last completed run on `account`'s manager."""
        return self._last_exit(account, service)

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


def _if_unintended(unit: WatchedUnit, act: str, fix: str) -> str:
    """The hedge for a unit whose timer is gone or off. NO_TIMER / DISABLED are report-only (an
    ORG-068 lifecycle act can be deliberate) but nothing here can tell that from an accident, so
    the report states the fact and only the POSSIBILITY of intent, then the consequence and fix."""
    consequence = f"{unit.consequence}; " if unit.consequence else ""
    return (f"If this wasn't a deliberate {act} (ORG-068), {consequence}"
            f"{fix} per tools/systemd/README.md.")


def classify_agent(agent: str, *, account: str | None, enablement: str | None,
                   service: dict | None, now: int,
                   cadence: int = CADENCE_SECS, stall_multiple: int = STALL_MULTIPLE,
                   unit: WatchedUnit | None = None) -> dict:
    """Pure decision for one agent. Returns {agent, verdict, detail}. No I/O.

    Reads `now` and `service['exit_epoch']` fresh each call and derives the verdict from their
    DIFFERENCE — there is no stored liveness answer to go stale (the freshness point).

    `unit` (T-4220): when set, `agent` is the unit's key and the subject is that plain oneshot
    unit instead of an inbox-consumer instance — same verdict logic (one T-3099 mid-run rule,
    one strict `>` boundary), but the threshold is `unit.max_silence_secs` and the wording
    names the unit. With `unit=None` every string below is byte-identical to before T-4220
    (pinned by test_consumer_liveness_units.AgentPathPinned)."""
    if unit is None:
        noun, who, run_word = "inbox-consumer", "consumer", "cycle"
        threshold = cadence * stall_multiple
    else:
        noun = who = unit.label
        run_word = "run"
        threshold = unit.max_silence_secs
    if not account:
        return {"agent": agent, "verdict": UNKNOWN,
                "detail": f"{agent}: no account known (absent from the substrate map) — unmeasurable."}
    if enablement is None:
        return {"agent": agent, "verdict": UNKNOWN,
                "detail": f"{agent}: could not query {account}'s user manager (denied sudo / dead "
                          f"manager / timeout). Liveness is unmeasurable — NOT a green result."}
    if enablement == "missing":
        if unit is None:
            detail = (f"{agent}: no inbox-consumer unit on {account}. delivery_reachability "
                      f"(MISSING) owns this alarm — not re-paged here.")
        else:
            detail = (f"{agent}: {unit.label} timer ({unit.timer}) is not installed on {account}. "
                      + _if_unintended(unit, "removal", "reinstall")
                      + " Reported, not paged.")
        return {"agent": agent, "verdict": NO_TIMER, "detail": detail}
    if enablement == "disabled":
        if unit is None:
            detail = (f"{agent}: {noun} timer on {account} is switched OFF — an "
                      f"authorised no, not a stall.")
        else:
            detail = (f"{agent}: {unit.label} timer ({unit.timer}) on {account} is switched OFF "
                      f"(disabled or masked). " + _if_unintended(unit, "disable", "re-enable")
                      + " Reported, not paged.")
        return {"agent": agent, "verdict": DISABLED, "detail": detail}

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
                    "detail": f"{agent}: {who} is mid-{run_word} (ActiveState={active_state}); "
                              f"exit-timestamp transiently cleared during activation — alive, "
                              f"not never-fired (T-3099)."}
        return {"agent": agent, "verdict": UNKNOWN,
                "detail": f"{agent}: timer enabled but the {who} has no completed-run "
                          f"timestamp yet (never fired / just enabled). verify_startup C8 owns "
                          f"the never-fired case — held UNKNOWN here, not silently green."}

    staleness = now - exit_epoch
    if staleness > threshold:
        mins, thr_m = staleness // 60, threshold // 60
        extra = ""
        result, active = service.get("result", ""), service.get("active_state", "")
        if result and result != "success":
            extra = f" Last run Result={result}"
        if active:
            extra += f" (ActiveState={active})"
        if unit is None:
            detail = (f"{agent}: inbox-consumer on {account} last COMPLETED a cycle {mins} min "
                      f"ago (cadence {cadence // 60} min, threshold {thr_m} min). The timer is "
                      f"enabled but has stopped delivering cycles — a stall the empty-queue "
                      f"detectors cannot see, and OnFailure does not catch because no run "
                      f"started to fail.{extra}")
        else:
            detail = (f"{agent}: {noun} on {account} last COMPLETED a run {mins} min ago "
                      f"(threshold {thr_m} min). The timer is enabled but has stopped firing — "
                      f"OnFailure does not catch it because no run started to fail.{extra}")
        return {"agent": agent, "verdict": STALLED, "detail": detail}
    return {"agent": agent, "verdict": LIVE,
            "detail": f"{agent}: {noun} completed a {run_word} {staleness // 60} min ago "
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


def check_unit_once(unit: WatchedUnit, *, ops: Ops, substrate: dict, state_dir: Path, now: int,
                    cooldown_secs: int, dry_run: bool) -> dict:
    """Probe one registered unit and record/alert. Returns {agent (= unit.key), verdict,
    detail, alerted}. The account is the OWNER persona's (one seat map, `_substrate`), and an
    alert routes as that persona — `route_failure_alert` sends non-recovery personas via Recovery."""
    account = substrate.get(unit.owner)
    muted = (disabled_reason(state_dir, now, _DISABLE_NAME)
             or disabled_reason(state_dir, now, f"consumer-liveness.{unit.key}.disabled"))
    if muted:
        decided = {"agent": unit.key, "verdict": DISABLED,
                   "detail": f"{unit.key}: authorised quiet: {muted}"}
    else:
        enablement = ops.unit_enablement(account, unit.timer) if account else None
        service = ops.unit_last_exit(account, unit.service) if enablement == "enabled" else None
        decided = classify_agent(unit.key, account=account, enablement=enablement,
                                 service=service, now=now, unit=unit)
    decided["route_as"] = unit.owner
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
            # `route_as` (units only): the persona the page is about. `agent` is then the unit
            # key, which is a state/mute namespace, not a persona route_failure_alert knows.
            ops.alert(f"[{_LABEL}] {result['detail']}",
                      vocab=f"{_LABEL} ({verdict})", agent=decided.get("route_as", agent))
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
              cooldown_secs: int = DEFAULT_COOLDOWN_SECS, dry_run: bool = False,
              units: list[WatchedUnit] | None = None) -> list[dict]:
    """Seats first, then units. `agents=None` means the whole fleet: every seat plus every
    `WATCHED_UNITS` entry. An explicit `agents=[...]` checks exactly those agents and no units;
    an explicit `units=[...]` overrides the registry (`agents=[]` + `units=[u]` is a units-only
    run, which is what `--unit` does)."""
    ops = ops or Ops()
    state_dir = Path(state_dir) if state_dir else _DEFAULT_STATE_DIR
    sub = substrate if substrate is not None else _substrate()
    seats = agents if agents is not None else _seat_inventory()
    explicit_units = units is not None
    if units is None:
        units = list(WATCHED_UNITS) if agents is None else []
    now = ops.now()
    results: list[dict] = []
    if not seats and not (explicit_units and units):
        results.append({"agent": "*", "verdict": UNKNOWN, "alerted": False,
                        "detail": "seat inventory could not be read (DELIVERY_MODE unreadable) — the "
                                  "roster is unknown, which is not the same as empty."})
    results += [check_agent_once(a, ops=ops, substrate=sub, state_dir=state_dir, now=now,
                                 cooldown_secs=cooldown_secs, dry_run=dry_run)
                for a in seats]
    # An unreadable seat roster must not blind the unit checks: they do not depend on it.
    results += [check_unit_once(u, ops=ops, substrate=sub, state_dir=state_dir, now=now,
                                cooldown_secs=cooldown_secs, dry_run=dry_run)
                for u in units]
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--agent", default=None,
                       help="check only this agent (default: all seats plus every registered unit)")
    scope.add_argument("--unit", default=None, choices=[u.key for u in WATCHED_UNITS],
                       help="check only this registered unit, e.g. `--unit cascade-sweep --dry-run`")
    ap.add_argument("--cooldown-secs", type=int, default=DEFAULT_COOLDOWN_SECS)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--dry-run", action="store_true", help="decide and print; never alert")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    agents = None
    units = None
    if args.agent:
        agents = [args.agent]
    elif args.unit:
        agents = []
        units = [u for u in WATCHED_UNITS if u.key == args.unit]

    results = check_all(state_dir=Path(args.state_dir) if args.state_dir else None,
                        agents=agents, units=units, cooldown_secs=args.cooldown_secs,
                        dry_run=args.dry_run)
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
