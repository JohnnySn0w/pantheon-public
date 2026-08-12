"""tools/fleet_host_health.py — off-site availability poller for the home cluster (T-2454).

THE GAP THIS EXISTS FOR: the fleet's monitoring (netdata) lives INSIDE the thing it watches —
CT201 is an LXC on pve2. When pve2 is the fault (the 2026-08-02 saturated-uplink incident,
T-2375), the monitor goes down WITH its target and nothing off-site says so. The operator, 2026-08-02:
"without proper monitoring I have no idea." A monitor co-located with its target is blind exactly
when it matters.

THE FIX (T-2454 Option B, operator-approved): recovery runs on the VPS — OFF both boxon and pve2. This
poller reaches the home hosts from that off-site vantage and alerts when one is unreachable or
unhealthy. It takes NO production reconfig of netdata, NO shared streaming secret, and NO service
restarts on the home hosts; it only READS from outside. Reversible by construction — deleting the
timer removes it with zero trace on boxon/pve2.

⚠️ REACHABILITY IS THE PRIMARY SIGNAL, AND A TIMEOUT IS A RESULT, NOT AN ERROR. The incident this
watches for is precisely "recovery, off-site, can no longer reach the home cluster." So when the ICMP
probe TIMES OUT that is not a measurement failure to swallow — it is the alarm. host_reachable()
returns False (=> UNREACHABLE, alert) on a timeout, and only None when the probe could not be RUN
at all (ping binary missing). Distinguishing those two is the whole discipline of the file.

⚠️ UNKNOWN ALERTS — it never falls through to OK. Sibling of ci_audit_liveness.py (T-2440): a
monitor whose broken state is indistinguishable from health is the exact failure it was built to
catch. "reachable but its netdata could not be read" is NOT green — it is UNKNOWN, and UNKNOWN
alerts. A collector that observes but cannot be shown to ALERT just moves the blind spot
(confirm-the-instrument); the known-positive test + the end-to-end synthetic proof exist for that.

FOUR VERDICTS PER HOST:
    OK           reachable, and (if it exposes netdata) no CRITICAL alarms raised.
    UNREACHABLE  the off-site ICMP probe got no reply. ALERT — chase the HOST/uplink. During a
                 home-net saturation this is the signal, and the poll timing out IS it.
    CRITICAL     reachable, but its netdata reports one or more CRITICAL alarms. ALERT.
    UNKNOWN      reachable but unmeasurable (ping un-runnable, or netdata unreadable/unparseable
                 while the host still pings — i.e. the collector itself is down). ALERT.

AUTHORISED NO (per-host + fleet-wide). A planned pve2 outage or maintenance window is a legitimate
quiet. The mute is a file with an EXPIRY so it cannot become permanent by neglect:
`<state>/fleet-host-health.disabled` mutes ALL hosts; `<state>/fleet-host-health.<name>.disabled`
mutes one. Both are JSON {reason, until}; past `until`, or unparseable, the mute does not apply and
the monitor speaks again — it fails TOWARD alerting.

NOT COVERED, stated rather than discovered: rich per-host metrics (CPU/mem/disk trends). This is an
availability + CRITICAL-alarm poller, not a metrics store. The richer netdata-streaming option
(T-2454 Option A) is a deliberate follow-up; it shares pve2's blast radius, which is why the
off-site reachability poller comes first.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AGENT = "recovery"                       # recovery-owned verdicts route direct to the operator (T-0722)

# Each home target: a name, the ICMP address (tailnet name or IP that resolves FROM recovery), and an
# optional netdata base URL. pve2 is reachability-only — its netdata IS ct201, watched separately,
# so if pve2 drops we see BOTH pve2 and ct201-netdata go UNREACHABLE (corroboration, not double
# counting). Verified reachable from recovery 2026-08-03: boxon 100.65.78.101, pve2 100.91.113.24,
# ct201 100.119.110.9.
HOSTS: list[dict] = [
    {"name": "boxon",         "ping": "boxon",         "netdata": "http://boxon:19999"},
    {"name": "pve2",          "ping": "pve2",          "netdata": None},
    {"name": "ct201-netdata", "ping": "100.119.110.9", "netdata": "http://100.119.110.9:19999"},
]

PING_COUNT = int(os.environ.get("PANTHEON_FLEET_HEALTH_PING_COUNT", "3"))     # >=1 reply == up
PING_TIMEOUT = int(os.environ.get("PANTHEON_FLEET_HEALTH_PING_TIMEOUT", "3")) # per-probe, seconds
NETDATA_TIMEOUT = int(os.environ.get("PANTHEON_FLEET_HEALTH_NETDATA_TIMEOUT", "10"))
DEFAULT_COOLDOWN_SECS = int(os.environ.get("PANTHEON_FLEET_HEALTH_COOLDOWN_SECS", str(6 * 3600)))
# T-2717: UNKNOWN (host reachable but netdata unreadable/unmeasurable) must PERSIST
# this many consecutive checks before it alerts. At the 5-min timer cadence, 3 =
# ~15 min of sustained unmeasurability. A netdata that is restarting or briefly
# loaded reads UNKNOWN for a poll or two; alerting on the first poll pings the
# operator for a transient (the "finnicky" complaint). A genuinely-stuck netdata
# stays UNKNOWN across the streak and still surfaces. UNREACHABLE/CRITICAL are NOT
# streak-gated — a host that is actually down or in a real alarm speaks immediately.
UNKNOWN_ALERT_STREAK = int(os.environ.get("PANTHEON_FLEET_HEALTH_UNKNOWN_STREAK", "3"))

_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
_STATE_NAME = "fleet-host-health.json"
_DISABLE_NAME = "fleet-host-health.disabled"           # fleet-wide mute
_LABEL = "fleet-host-health"

OK, UNREACHABLE, CRITICAL, UNKNOWN, DISABLED = (
    "OK", "UNREACHABLE", "CRITICAL", "UNKNOWN", "DISABLED")
ALERTING = (UNREACHABLE, CRITICAL, UNKNOWN)


def _log(msg: str) -> None:
    print(f"[fleet_host_health] {msg}", file=sys.stderr)


class Ops:
    """Every side effect behind one seam, so classify_host() is pure decision (and testable)."""

    def icmp_reachable(self, host: str) -> bool | None:
        """True/False, or None if we could NOT run the probe (never 'down' for a missing binary).

        `-c PING_COUNT`: any single reply means up, so ordinary packet loss does not fabricate an
        UNREACHABLE. A total timeout (no reply) IS the host being unreachable, for our purpose."""
        try:
            r = subprocess.run(
                ["ping", "-c", str(PING_COUNT), "-W", str(PING_TIMEOUT), host],
                capture_output=True, text=True, timeout=PING_COUNT * PING_TIMEOUT + 5)
        except subprocess.TimeoutExpired:
            return False               # the whole probe hung with no reply -> unreachable
        except OSError:
            return None                # ping binary absent/unrunnable -> we could not measure
        return r.returncode == 0

    def netdata_critical_alarms(self, base_url: str) -> list[str] | None:
        """CRITICAL alarm names currently raised, [] if none, or None if unmeasurable (-> UNKNOWN).

        None is NOT the same as [] — the whole UNKNOWN-alerts discipline turns on it. curl failing,
        a non-2xx, or unparseable JSON all mean 'we could not tell', which alerts; an empty list is
        a real 'nothing critical', which is green."""
        url = f"{base_url}/api/v1/alarms?active=true"
        try:
            r = subprocess.run(["curl", "-s", "--max-time", str(NETDATA_TIMEOUT), url],
                               capture_output=True, text=True, timeout=NETDATA_TIMEOUT + 5)
        except (subprocess.TimeoutExpired, OSError):
            return None
        if r.returncode != 0:
            return None
        try:
            data = json.loads(r.stdout)
        except ValueError:
            return None
        alarms = data.get("alarms")
        if not isinstance(alarms, dict):
            return None
        return [name for name, a in alarms.items()
                if isinstance(a, dict) and a.get("status") == "CRITICAL"]

    def alert(self, msg: str, vocab: str, agent: str = AGENT) -> None:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from inbox_consumer import route_failure_alert       # late: heavy side-effect surface
            route_failure_alert(agent, msg, vocab=vocab)
        except Exception as exc:                        # an alert path must never crash the probe
            _log(f"alert delivery FAILED ({exc!r}); verdict still recorded in state")

    def now(self) -> int:
        return int(time.time())


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
        return None                                    # unparseable expiry -> not a valid mute
    if expiry <= now:
        return None                                    # lapsed -> the monitor speaks again
    reason = str(data.get("reason") or "no reason recorded")
    return f"{reason} (until {until})"


def classify_host(cfg: dict, *, reachable: bool | None,
                  critical_alarms: list[str] | None) -> dict:
    """Pure decision for one host. Returns {verdict, detail}. No I/O.

    `critical_alarms` is only meaningful when reachable is True and the host exposes netdata; the
    caller passes None otherwise and this never inspects it in those branches."""
    name = cfg["name"]
    if reachable is None:
        return {"verdict": UNKNOWN,
                "detail": (f"{name}: could not RUN the reachability probe (ping missing/unrunnable "
                           f"on recovery). Reachability is currently unmeasurable — NOT a green "
                           f"result.")}
    if reachable is False:
        return {"verdict": UNREACHABLE,
                "detail": (f"{name} does not answer ICMP from recovery (off-site, over tailnet). The "
                           f"host or the home uplink is the thing to chase — netdata lives on the "
                           f"home cluster, so if this is pve2 the on-box monitor is down too. Last "
                           f"time this happened the cause was a saturated home uplink (T-2375).")}
    if cfg.get("netdata") is None:
        return {"verdict": OK,
                "detail": f"{name} reachable from recovery (ICMP). Reachability-only target."}
    if critical_alarms is None:
        return {"verdict": UNKNOWN,
                "detail": (f"{name} answers ICMP but its netdata ({cfg['netdata']}) could not be "
                           f"read or parsed. If this is ct201-netdata, the fleet's own collector "
                           f"is down while the box still pings — health is unmeasurable, which is "
                           f"NOT green.")}
    if critical_alarms:
        shown = ", ".join(critical_alarms[:8]) + (" …" if len(critical_alarms) > 8 else "")
        return {"verdict": CRITICAL,
                "detail": (f"{name} reachable but netdata reports {len(critical_alarms)} CRITICAL "
                           f"alarm(s): {shown}")}
    return {"verdict": OK,
            "detail": f"{name} reachable, netdata reports no CRITICAL alarms."}


def check_host_once(cfg: dict, *, ops: Ops, state_dir: Path, now: int,
                    cooldown_secs: int, dry_run: bool) -> dict:
    """Probe one host and record/alert. Returns {name, verdict, detail, alerted}."""
    name = cfg["name"]
    muted = (disabled_reason(state_dir, now, _DISABLE_NAME)
             or disabled_reason(state_dir, now, f"fleet-host-health.{name}.disabled"))
    if muted:
        return _finish(cfg, {"verdict": DISABLED, "detail": f"authorised quiet: {muted}"},
                       ops=ops, state_dir=state_dir, now=now,
                       cooldown_secs=cooldown_secs, dry_run=dry_run)

    reachable = ops.icmp_reachable(cfg["ping"])
    critical = None
    if reachable is True and cfg.get("netdata"):
        critical = ops.netdata_critical_alarms(cfg["netdata"])
    decided = classify_host(cfg, reachable=reachable, critical_alarms=critical)
    return _finish(cfg, decided, ops=ops, state_dir=state_dir, now=now,
                   cooldown_secs=cooldown_secs, dry_run=dry_run)


def _finish(cfg: dict, decided: dict, *, ops: Ops, state_dir: Path, now: int,
            cooldown_secs: int, dry_run: bool) -> dict:
    """Record this host's verdict; alert if alerting and the per-host cooldown has lapsed.

    A CHANGED verdict always speaks (UNREACHABLE -> CRITICAL is new information); only a repeat of
    the SAME verdict is throttled by the cooldown."""
    name = cfg["name"]
    verdict = decided["verdict"]
    result = {"name": name, "verdict": verdict, "detail": decided["detail"], "alerted": False}

    state_path = state_dir / _STATE_NAME
    full = _read_json(state_path)
    hstate = full.get(name) if isinstance(full.get(name), dict) else {}

    # T-2717: track the consecutive-UNKNOWN streak. UNKNOWN only becomes
    # alert-eligible once it has PERSISTED for UNKNOWN_ALERT_STREAK checks — a
    # transient netdata blip (restart / brief load) reads UNKNOWN for a poll or two
    # and must NOT ping. Any non-UNKNOWN verdict resets the streak.
    prev_streak = hstate.get("unknown_streak")
    prev_streak = prev_streak if isinstance(prev_streak, int) and prev_streak >= 0 else 0
    unknown_streak = prev_streak + 1 if verdict == UNKNOWN else 0
    hstate["unknown_streak"] = unknown_streak

    alert_eligible = verdict in ALERTING
    if verdict == UNKNOWN and unknown_streak < UNKNOWN_ALERT_STREAK:
        # Not yet sustained — stay quiet, but keep counting toward the streak.
        alert_eligible = False
        result["detail"] = (f"{result['detail']} [suppressed: UNKNOWN streak "
                            f"{unknown_streak}/{UNKNOWN_ALERT_STREAK}]")

    if alert_eligible and not dry_run:
        last_alert = hstate.get("last_alert_epoch")
        last_verdict = hstate.get("last_alert_verdict")
        due = (not isinstance(last_alert, int)
               or last_verdict != verdict
               or now - last_alert >= cooldown_secs)
        if due:
            ops.alert(f"[{_LABEL}] {name} {verdict}: {result['detail']}",
                      vocab=f"{_LABEL} ({verdict})", agent=AGENT)
            result["alerted"] = True
            hstate["last_alert_epoch"] = now
            hstate["last_alert_verdict"] = verdict

    hstate["last_verdict"] = verdict
    hstate["last_check_epoch"] = now
    full[name] = hstate
    _write_json(state_path, full)
    return result


def check_all(*, ops: Ops | None = None, state_dir: Path | None = None,
              hosts: list[dict] | None = None,
              cooldown_secs: int = DEFAULT_COOLDOWN_SECS, dry_run: bool = False) -> list[dict]:
    ops = ops or Ops()
    state_dir = Path(state_dir) if state_dir else _DEFAULT_STATE_DIR
    now = ops.now()
    return [check_host_once(cfg, ops=ops, state_dir=state_dir, now=now,
                            cooldown_secs=cooldown_secs, dry_run=dry_run)
            for cfg in (hosts if hosts is not None else HOSTS)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=None,
                    help="check only this host name (default: all in HOSTS)")
    ap.add_argument("--cooldown-secs", type=int, default=DEFAULT_COOLDOWN_SECS)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="decide and print; never alert, never touch alert state")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    hosts = HOSTS
    if args.host:
        hosts = [h for h in HOSTS if h["name"] == args.host]
        if not hosts:
            _log(f"no such host '{args.host}'; known: {[h['name'] for h in HOSTS]}")
            return 2

    results = check_all(state_dir=Path(args.state_dir) if args.state_dir else None,
                        hosts=hosts, cooldown_secs=args.cooldown_secs, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(results))
    else:
        for r in results:
            _log(f"{r['verdict']}: {r['detail']}" + (" [ALERTED]" if r["alerted"] else ""))
    # Exit 0 even on an alerting verdict: the ALERT is the output, and a non-zero exit would make
    # systemd's OnFailure fire a SECOND alert about the monitor that just worked correctly.
    return 0


if __name__ == "__main__":
    sys.exit(main())
