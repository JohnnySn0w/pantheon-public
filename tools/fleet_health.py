#!/usr/bin/env python3
"""Read-only fleet-health verification sweep (T-1049 — the *verifiable* half).

Answers one question across the WHOLE Pantheon fleet: "did everyone come back
healthy?" — the check I ran by hand after the 2026-06-10 reboot, made repeatable.
It is the post-reboot (or anytime) verification tool the "fleet health sweep"
memory described as a manual procedure; `verify_startup.sh` is per-agent only and
has a boot-dialog DE-WEDGE side-effect, so it is not a clean read-only sweep.

STRICTLY READ-ONLY: every check is `is-active` / `has-session` / a throwaway
bwrap probe. It NEVER restarts, injects, de-wedges, or mutates anything — it
reports + sets an exit code. Remediation/orchestration (reboot ordering, who
drives, auto-repair) is deliberately OUT of scope — that is the policy half of
T-1049 (the operator's steer). This tool just tells you the truth about the fleet.

Per agent it checks: each load-bearing service is `active`, the tmux session
exists, and (codex only) the bwrap sandbox can create a userns — the exact gap
that bit silently after the reboot (apparmor_restrict_unprivileged_userns
reverting to 1; see the codex-bwrap-userns memory).

PLUS a blanket `systemctl --failed` sweep (system scope + every agent's user
scope) — because enumerated per-agent service checks CANNOT catch a failed unit
nobody thought to enumerate. That is exactly the gap that left mosquitto.service
crash-looped (and a watchdog-crash-looping echo daemon downstream) undetected for
~5h after the 2026-06-09 reboot (T-1053). The bug's prescribed detection habit,
automated into the post-reboot verification (`--no-failed-sweep` to skip).

Cross-account via `sudo -n -u <user>` (Recovery holds NOPASSWD sudo to every agent
user). A check that cannot run (sudo denied, timeout) is a WARN, never a crash.

    python3 tools/fleet_health.py            # sweep all agents
    python3 tools/fleet_health.py --agent codex
    python3 tools/fleet_health.py --json
Exit: 0 = all healthy; 1 = at least one FAIL; 64 = usage error.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

# Load-bearing facts per agent. `services` = units whose inactivity means the
# agent is DOWN. `session` = the tmux session the agent lives in. `bwrap` flags
# the codex sandbox userns probe.
FLEET = {
    "recovery": {
        "user": "claude-fallback",
        "session": "claude-telegram-bot-fallback",
        "services": ["claude-telegram-fallback.service"],
    },
    "orchestrator": {
        "user": "claude",
        "session": "claude-telegram-bot",
        "services": ["claude-telegram.service"],
    },
    "specialist": {
        "user": "claude-research",
        "session": "claude-research-bot",
        "services": ["claude-telegram-research.service"],
    },
    "codex": {
        "user": "codex",
        "session": "codex-session",
        "services": ["codex-daemon.service", "codex-telegram-poller.service"],
        "bwrap": True,
    },
}

OK, WARN, FAIL = "OK", "WARN", "FAIL"


def _run(user: str, inner: str, *, timeout: int = 15):
    """Run `inner` as `user` (no sudo if it's the current user). Returns
    (rc, stdout, ran) — ran=False if the command couldn't even execute (sudo
    denied / timeout / OS error), which callers treat as WARN not FAIL."""
    import getpass
    if user == getpass.getuser():
        argv = ["bash", "-lc", f"XDG_RUNTIME_DIR=/run/user/$(id -u) {inner}"]
    else:
        argv = ["sudo", "-n", "-u", user, "bash", "-lc",
                f"XDG_RUNTIME_DIR=/run/user/$(id -u) {inner}"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return (None, "", False)
    return (r.returncode, r.stdout.strip(), True)


def check_agent(name: str, cfg: dict, *, run=_run) -> dict:
    """Return {agent, status, checks:[{name,status,detail}]}. status is the worst
    of its checks (FAIL > WARN > OK)."""
    user = cfg["user"]
    checks: list[dict] = []

    for svc in cfg["services"]:
        rc, out, ran = run(user, f"systemctl --user is-active {svc}")
        if not ran:
            checks.append({"name": f"svc:{svc}", "status": WARN,
                           "detail": "could not query (sudo/timeout)"})
        elif out == "active":
            checks.append({"name": f"svc:{svc}", "status": OK, "detail": "active"})
        else:
            checks.append({"name": f"svc:{svc}", "status": FAIL,
                           "detail": f"not active (got '{out or 'unknown'}')"})

    rc, out, ran = run(user, f"tmux has-session -t {cfg['session']} 2>/dev/null "
                             f"&& echo yes || echo no")
    if not ran:
        checks.append({"name": "session", "status": WARN,
                       "detail": "could not query"})
    elif out.endswith("yes"):
        checks.append({"name": "session", "status": OK,
                       "detail": f"{cfg['session']} present"})
    else:
        checks.append({"name": "session", "status": FAIL,
                       "detail": f"{cfg['session']} MISSING"})

    if cfg.get("bwrap"):
        # the throwaway userns probe — the exact silent gap after the reboot.
        rc, out, ran = run(user,
                           "bwrap --ro-bind / / --unshare-user --uid 0 true "
                           "&& echo ok || echo broken")
        if not ran:
            checks.append({"name": "bwrap-userns", "status": WARN,
                           "detail": "could not run probe"})
        elif out.endswith("ok") and rc == 0:
            checks.append({"name": "bwrap-userns", "status": OK,
                           "detail": "userns OK (shell sandbox works)"})
        else:
            checks.append({"name": "bwrap-userns", "status": FAIL,
                           "detail": "uid map denied — check "
                           "kernel.apparmor_restrict_unprivileged_userns (=0?)"})

    order = {OK: 0, WARN: 1, FAIL: 2}
    worst = max((c["status"] for c in checks), key=lambda s: order[s], default=OK)
    return {"agent": name, "status": worst, "checks": checks}


def sweep(agents: list[str], *, run=_run) -> list[dict]:
    return [check_agent(a, FLEET[a], run=run) for a in agents]


def _parse_failed_units(out: str) -> list[str]:
    """Extract unit names from `systemctl --failed --no-legend --plain` output
    (first whitespace token per non-empty line). Empty output -> no failed units."""
    units = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        tok = line.split()[0]
        if tok.endswith((".service", ".socket", ".timer", ".mount", ".target",
                         ".path", ".scope", ".slice", ".automount")):
            units.append(tok)
    return units


def failed_units_sweep(agents: list[str], *, run=_run) -> list[dict]:
    """Blanket `systemctl --failed` sweep — SYSTEM scope once + USER scope per
    distinct agent account. Catches crash-looped / failed units that the
    enumerated per-agent service checks would NOT (you can't poll a unit you
    didn't think to enumerate). This is the exact gap that left a failed
    mosquitto.service — and a watchdog-crash-looping echo daemon downstream —
    undetected for ~5h after the 2026-06-09 reboot (T-1053, bug
    service-binds-tailscale-ip-fails-on-boot-before-tailscaled). The bug's
    prescribed detection habit, automated into the post-reboot verification.

    Returns one record per scope: {scope, status, units, detail}. A scope with
    any failed unit is FAIL; unqueryable (sudo denied/timeout) is WARN; clean is OK.
    """
    import getpass
    records: list[dict] = []
    cmd = "systemctl --failed --no-legend --plain --no-pager 2>/dev/null"

    # SYSTEM scope (one check; run as the current user — system unit states are
    # world-readable). The failed unit need not belong to any enumerated agent.
    rc, out, ran = run(getpass.getuser(), cmd)
    if not ran:
        records.append({"scope": "system", "status": WARN, "units": [],
                        "detail": "could not query system --failed"})
    else:
        units = _parse_failed_units(out)
        records.append({"scope": "system", "status": FAIL if units else OK,
                        "units": units,
                        "detail": (", ".join(units) if units else "no failed units")})

    # USER scope per DISTINCT agent account.
    seen_users: set[str] = set()
    for a in agents:
        user = FLEET[a]["user"]
        if user in seen_users:
            continue
        seen_users.add(user)
        rc, out, ran = run(user, "systemctl --user " + cmd)
        if not ran:
            records.append({"scope": f"user:{user}", "status": WARN, "units": [],
                            "detail": "could not query user --failed (sudo/timeout)"})
        else:
            units = _parse_failed_units(out)
            records.append({"scope": f"user:{user}",
                            "status": FAIL if units else OK, "units": units,
                            "detail": (", ".join(units) if units else "no failed units")})
    return records


def render(results: list[dict], failed: list[dict] | None = None) -> str:
    glyph = {OK: "✓", WARN: "!", FAIL: "✗"}
    lines = []
    for r in results:
        lines.append(f"[{glyph[r['status']]}] {r['agent']:6} {r['status']}")
        for c in r["checks"]:
            if c["status"] != OK:
                lines.append(f"      {glyph[c['status']]} {c['name']}: {c['detail']}")

    failed_units_present = []
    if failed is not None:
        for fr in failed:
            if fr["status"] != OK:
                lines.append(f"[{glyph[fr['status']]}] failed-units {fr['scope']}: {fr['detail']}")
            if fr["status"] == FAIL:
                failed_units_present.append(fr["scope"])

    fails = [r["agent"] for r in results if r["status"] == FAIL]
    warns = [r["agent"] for r in results if r["status"] == WARN]
    lines.append("")
    if fails or failed_units_present:
        bits = []
        if fails:
            bits.append(f"FAIL on {', '.join(fails)}")
        if failed_units_present:
            bits.append(f"failed units in {', '.join(failed_units_present)}")
        lines.append("FLEET: DEGRADED — " + "; ".join(bits))
    elif warns:
        lines.append(f"FLEET: healthy with warnings ({', '.join(warns)} unqueryable)")
    else:
        lines.append("FLEET: all healthy ✓")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only fleet-health sweep (T-1049).")
    ap.add_argument("--agent", choices=sorted(FLEET), default=None,
                    help="check one agent (default: all)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-failed-sweep", action="store_true",
                    help="skip the blanket `systemctl --failed` sweep (T-1053 detection habit)")
    args = ap.parse_args(argv)

    agents = [args.agent] if args.agent else list(FLEET)
    results = sweep(agents)
    failed = None if args.no_failed_sweep else failed_units_sweep(agents)
    if args.json:
        print(json.dumps({"agents": results, "failed_units": failed}, indent=2))
    else:
        print(render(results, failed))
    agent_fail = any(r["status"] == FAIL for r in results)
    units_fail = bool(failed) and any(fr["status"] == FAIL for fr in failed)
    return 1 if (agent_fail or units_fail) else 0


if __name__ == "__main__":
    raise SystemExit(main())
