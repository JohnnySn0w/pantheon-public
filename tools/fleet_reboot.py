#!/usr/bin/env python3
"""tools/fleet_reboot.py — Recovery-driven coordinated fleet reboot orchestration (T-1049).

The operator's protocol (Telegram 3055): "Recovery drives; agents are told to finish up +
record info + write a chronicle entry + pause until reboot," then the machine
reboots. The checks bracket it and are ADVISORY, not hard gates (Telegram 3063):
reboot_safety.py before (is everyone's work committed/pushed?) and fleet_health.py
after (did everyone come back?).

This maps onto the EXISTING per-agent mechanism rather than reinventing it:
  * Per interactive control-plane agent (orchestrator/specialist/archivist/mason/artisan/
    watchman/recovery): `handoff_and_restart.sh --no-restart`
    (T-1049 pause mode) — prompt the agent to finish + write its HANDOFF, wait for
    the mtime ack, archive it to the Chronicle, then STOP (no restart). The
    machine reboot is what cycles it; the agent's SessionStart hook resumes it
    from its handoff on the next boot.
  * codex: a different harness with no claude HANDOFF/chronicle flow. Its quiesce
    is "repo-clean + boot recovery": reboot_safety covers its repos, and
    codex-daemon.service comes back on boot (userns persistence fixed in T-1051).
    We optionally stop its services for a clean shutdown.

DRIVER SPECIAL-CASE: Recovery runs this script, so Recovery cannot prompt-itself-to-stop
mid-orchestration (the prompt would land in the very pane executing this). Recovery is
therefore quiesced LAST and DIFFERENTLY: the orchestrator archives Recovery's existing
HANDOFF.md to the Chronicle directly, then triggers the reboot (which terminates
Recovery). Refresh Recovery's HANDOFF.md before invoking with --execute.

SAFETY: dry-run by DEFAULT. Nothing is quiesced and nothing reboots unless
--execute is passed. The reboot is the final step and is itself the highest blast
radius in the fleet, so the first live fire is SUPERVISED. All side effects
(per-agent quiesce, repo/health sweeps, the reboot call) are injected so the
orchestration logic is fully unit-tested.

    python3 tools/fleet_reboot.py                 # DRY RUN — print the plan + advisory pre-check
    python3 tools/fleet_reboot.py --execute       # quiesce the fleet, then reboot (SUPERVISED)
    python3 tools/fleet_reboot.py --execute --skip-reboot   # quiesce only; do not reboot
    python3 tools/fleet_reboot.py --strict        # make the pre-check a HARD gate (default: advisory)

Exit: 0 = plan/quiesce completed (and reboot issued under --execute); 1 = a
quiesce step failed (reboot withheld); 2 = strict pre-check gate failed; 3 = the root
route (claude seat's pantheon-sudo) failed its preflight, nothing quiesced; 64 = usage.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

# Lazy imports of the sibling sweeps so unit tests can inject substitutes and so
# an import hiccup in one never bricks the orchestrator's own --help.
try:
    import reboot_safety
except Exception:  # noqa: BLE001
    reboot_safety = None
try:
    import fleet_health
except Exception:  # noqa: BLE001
    fleet_health = None
try:
    import agent_role_map
except Exception:  # noqa: BLE001
    agent_role_map = None

# This file lives in <repo>/tools/ (session_chronicle.py is its sibling).
import os
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
# T-6288: a quiesced seat runs ITS OWN clone's script, expanded by the seat's login
# shell. The driver's clone (/home/claude-fallback, mode 700) is unreadable to them.
_SEAT_HANDOFF_SCRIPT = "$HOME/pantheon-config/tools/handoff_and_restart.sh"
# T-6288: the driver (Recovery) has no root. Root goes through the claude seat's
# pantheon-sudo (T-5331), whose stdin must not be a pipe or file (T-5713).
_ROOT_ROUTE_USER = "claude"


def _root_route_argv(command: str) -> list[str]:
    return ["sudo", "-n", "-u", _ROOT_ROUTE_USER, "bash", "-lc",
            f"~/pantheon-config/tools/pantheon-sudo {command} </dev/null"]

# --------------------------------------------------------------------------- #
# Fleet registry — quiesce order + per-agent strategy.                        #
# --------------------------------------------------------------------------- #
#
# Order matters: the DRIVER (recovery) is quiesced LAST (it runs this script). The
# non-driver claude agents go first via the handoff-pause mechanism; codex (a
# foreign harness) is repo-clean + boot-recovery.
#
#   strategy "handoff_pause": cross-account `handoff_and_restart.sh --no-restart`.
#   strategy "repo_clean":    no handoff flow; rely on reboot_safety + boot restart
#                             (optionally stop services for a clean shutdown).
#   strategy "driver":        recovery — chronicle its existing HANDOFF directly here,
#                             then this process triggers the reboot.

DRIVER = "recovery"

FLEET = {
    "orchestrator": {
        "strategy": "handoff_pause",
        "user": "claude",
        "service": "claude-telegram.service",
        "mode": "user",
        "pane": "claude-telegram-bot:0",
        "handoff_file": "/home/claude/HANDOFF.md",
    },
    "specialist": {
        "strategy": "handoff_pause",
        "user": "claude-research",
        "service": "claude-telegram-research.service",
        "mode": "user",
        "pane": "claude-research-bot:0",
        "handoff_file": "/home/claude-research/HANDOFF.md",
    },
    "codex": {
        "strategy": "repo_clean",
        "user": "codex",
        "services": ["codex-daemon.service", "codex-telegram-poller.service"],
        "mode": "user",
    },
    "archivist": {
        "strategy": "handoff_pause",
        "user": "exocortex",
        "service": "pantheon-persona-consume@archivist.service",
        "mode": "user",
        "pane": "claude-telegram-bot-exocortex:0",
        "handoff_file": "/home/exocortex/HANDOFF.md",
    },
    "mason": {
        "strategy": "handoff_pause",
        "user": "suborch1",
        "service": "pantheon-persona-consume@mason.service",
        "mode": "user",
        "pane": "suborch1-main:0",
        "handoff_file": "/home/suborch1/HANDOFF.md",
    },
    "artisan": {
        "strategy": "handoff_pause",
        "user": "suborch2",
        "service": "pantheon-persona-consume@artisan.service",
        "mode": "user",
        "pane": "suborch2-main:0",
        "handoff_file": "/home/suborch2/HANDOFF.md",
    },
    "watchman": {
        "strategy": "handoff_pause",
        "user": "codex-fallback",
        "service": "pantheon-persona-consume@watchman.service",
        "mode": "user",
        "pane": "codex-fallback-main:0",
        "handoff_file": "/home/codex-fallback/HANDOFF.md",
    },
    "recovery": {
        "strategy": "driver",
        "user": "claude-fallback",
        "handoff_file": "/home/claude-fallback/HANDOFF.md",
    },
}

# Non-driver-first, driver last.
QUIESCE_ORDER = [
    "orchestrator", "specialist", "codex", "archivist", "mason", "artisan", "watchman",
    "recovery",
]

OK, WARN, FAIL = "OK", "WARN", "FAIL"


# --------------------------------------------------------------------------- #
# Injected side effects (real implementations; mocked in tests).              #
# --------------------------------------------------------------------------- #

def _run(argv: list[str], *, timeout: int = 900) -> tuple[int | None, str]:
    """Run a command; return (returncode, combined_output). None rc = it raised."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def _restart_identity(agent: str, cfg: dict) -> tuple[str, str]:
    """(service, supervisor) for the handoff-pause call (T-4159).

    Resolved from instance.yaml via agent_role_map.restart_target, so a seat that
    moved to the control plane (specialist; recovery/orchestrator after T-3650) is named by its
    persona-consume unit with --supervisor control-plane, not by the retired
    bespoke unit left in FLEET below. --no-restart stops before the restart step,
    so this only fixes what the pause records and validates; the static entry is
    the fallback when resolution fails."""
    try:
        target = agent_role_map.restart_target(agent)
        return target["service"], target["supervisor"]
    except Exception:  # noqa: BLE001 - agent_role_map absent or persona unbound
        return cfg["service"], "bespoke"


def quiesce_handoff_pause(agent: str, cfg: dict, *, run=_run, dry_run: bool) -> dict:
    """Cross-account `handoff_and_restart.sh --no-restart` for a claude agent."""
    service, supervisor = _restart_identity(agent, cfg)
    inner = (
        f"XDG_RUNTIME_DIR=/run/user/$(id -u) {_SEAT_HANDOFF_SCRIPT} "
        f"--service {service} --mode {cfg['mode']} "
        f"--target-pane {cfg['pane']} --handoff-file {cfg['handoff_file']} "
        f"--supervisor {supervisor} --no-restart"
    )
    argv = ["sudo", "-n", "-u", cfg["user"], "bash", "-lc", inner]
    if dry_run:
        return {"agent": agent, "status": OK, "action": "DRY-RUN would run: " + " ".join(argv)}
    rc, out = run(argv)
    status = OK if rc == 0 else FAIL
    return {"agent": agent, "status": status, "rc": rc, "detail": out.strip()[-500:]}


def quiesce_repo_clean(agent: str, cfg: dict, *, run=_run, dry_run: bool,
                       stop_services: bool) -> dict:
    """codex quiesce: it has no claude handoff flow. reboot_safety already covers
    its repos; codex-daemon comes back on boot. Optionally stop its services for a
    clean shutdown. Never fails the orchestration on codex (advisory) — a codex
    quiesce hiccup must not block a fleet reboot the other agents are ready for."""
    if not stop_services:
        return {"agent": agent, "status": OK,
                "action": "no-op (repo-clean covered by pre-check; daemon restarts on boot)"}
    actions = []
    for svc in cfg.get("services", []):
        inner = f"XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user stop {svc}"
        argv = ["sudo", "-n", "-u", cfg["user"], "bash", "-lc", inner]
        if dry_run:
            actions.append("DRY-RUN would run: " + " ".join(argv))
            continue
        rc, out = run(argv)
        actions.append(f"stop {svc}: rc={rc}")
    return {"agent": agent, "status": WARN if not dry_run else OK, "action": "; ".join(actions)}


def quiesce_driver(agent: str, cfg: dict, *, chronicle_archive, dry_run: bool) -> dict:
    """Recovery (driver): archive its existing HANDOFF.md to the Chronicle directly
    (it cannot prompt-itself-to-stop mid-run). The reboot itself terminates Recovery;
    its SessionStart hook resumes from the handoff on the next boot."""
    if dry_run:
        return {"agent": agent, "status": OK,
                "action": f"DRY-RUN would chronicle-archive {cfg['handoff_file']} then reboot"}
    ok, detail = chronicle_archive(cfg["handoff_file"])
    return {"agent": agent, "status": OK if ok else WARN,
            "action": f"chronicle archive {cfg['handoff_file']}: {detail}"}


def chronicle_archive_handoff(handoff_file: str, *, run=_run) -> tuple[bool, str]:
    """Best-effort Chronicle archive of a handoff (Recovery's own clone). Non-fatal.

    T-5016: through the session-end duty, not chronicle.py directly. The session
    is not ending yet (the reboot ends it), so the record is written now and its
    pane log is attached by the controller's first start after the reboot, under
    the same session claim: one record, not a fleet_reboot entry plus a second
    one at the next start."""
    script = os.path.join(_TOOLS_DIR, "session_chronicle.py")
    argv = [
        "python3", script, "session-end",
        "--handoff-file", handoff_file,
        "--cause", "fleet-reboot",
        "--defer-pane-log", "--certain",
        "--entrypoint", "fleet_reboot",
    ]
    rc, out = run(argv)
    ok = rc == 0 and '"status": "ok"' in (out or "")
    return ok, out.strip()[-300:]


def trigger_reboot(*, run=_run, dry_run: bool) -> dict:
    """Issue the machine reboot. Gated by the caller (only on --execute)."""
    argv = _root_route_argv("systemctl reboot")
    if dry_run:
        return {"status": OK, "action": "DRY-RUN would run: " + " ".join(argv)}
    rc, out = run(argv)
    # A successful reboot usually kills us before we read rc; treat any return as best-effort.
    return {"status": OK if rc in (0, None) else FAIL, "rc": rc, "detail": out.strip()[-300:]}


def preflight_reboot_privilege(*, run=_run, dry_run: bool) -> dict:
    """T-6288: prove the root route works BEFORE quiescing anyone, so --execute can
    never leave the fleet paused and then fail to reboot."""
    argv = _root_route_argv("true")
    if dry_run:
        return {"status": OK, "action": "DRY-RUN would run: " + " ".join(argv)}
    rc, out = run(argv, timeout=60)
    return {"status": OK if rc == 0 else FAIL, "rc": rc, "detail": out.strip()[-300:]}


# --------------------------------------------------------------------------- #
# Pre-check (advisory by default).                                            #
# --------------------------------------------------------------------------- #

def run_pre_check(*, sweep=None) -> tuple[bool, list[dict]]:
    """Run the reboot_safety repo-cleanliness sweep. Returns (clean, results).
    clean == no FAIL (DIRTY) repo. WARN (ahead/unpushed) does not flip clean."""
    if sweep is None:
        if reboot_safety is None:
            return False, [{"agent": "fleet", "status": FAIL,
                            "detail": "reboot_safety unavailable; every seat is unchecked"}]
        sweep = reboot_safety.sweep
    results = sweep()
    for agent in FLEET:
        seat_results = [r for r in results if r.get("agent") == agent]
        for evidence_key in ("repo", "continuity"):
            if not any(evidence_key in r for r in seat_results):
                results.append({
                    "agent": agent,
                    "status": FAIL,
                    "detail": f"seat omitted from reboot-safety {evidence_key} sweep; unchecked",
                })
    clean = not any(r.get("status") == FAIL for r in results)
    return clean, results


# --------------------------------------------------------------------------- #
# Orchestration.                                                              #
# --------------------------------------------------------------------------- #

def orchestrate(*, execute: bool, strict: bool, skip_reboot: bool,
                stop_codex_services: bool,
                pre_sweep=None,
                quiesce_handoff=quiesce_handoff_pause,
                quiesce_repo=quiesce_repo_clean,
                quiesce_drv=quiesce_driver,
                chronicle_archive=chronicle_archive_handoff,
                reboot=trigger_reboot,
                reboot_preflight=preflight_reboot_privilege,
                log=print) -> dict:
    """Run the full sequence. Returns a structured report; the CLI maps it to an
    exit code. dry_run is the inverse of `execute`."""
    dry_run = not execute
    report: dict = {"dry_run": dry_run, "strict": strict, "skip_reboot": skip_reboot,
                    "pre_check": None, "quiesce": [], "reboot": None, "exit": 0}

    # --- Phase 1: advisory pre-check -------------------------------------------
    clean, pre_results = run_pre_check(sweep=pre_sweep)
    report["pre_check"] = {"clean": clean, "results": pre_results}
    log(f"[pre-check] reboot_safety: {'CLEAN' if clean else 'UNSAFE (work or continuity unchecked)'}")
    for r in pre_results:
        if r.get("status") != OK:
            log(f"  [{r.get('status')}] {r.get('agent')}: {r.get('repo', r.get('detail', ''))}")
    if not clean:
        if strict:
            log("[pre-check] STRICT gate: unsafe preflight — ABORTING (no quiesce, no reboot).")
            report["exit"] = 2
            return report
        log("[pre-check] ADVISORY: unsafe work/continuity noted; continuing the plan "
            "(the operator: advisory, not a gate). A live quiesce still fails closed if a "
            "fresh handoff cannot be recorded.")

    # --- Phase 1b: root route preflight (only when a reboot will follow) -------
    if not skip_reboot:
        pre = reboot_preflight(dry_run=dry_run)
        report["reboot_preflight"] = pre
        log(f"[preflight] root route ({_ROOT_ROUTE_USER} pantheon-sudo): [{pre.get('status')}] "
            f"{pre.get('action', pre.get('detail', ''))}")
        if pre.get("status") != OK:
            log("[preflight] root route unavailable — ABORTING before quiesce (no seat paused).")
            report["exit"] = 3
            return report

    # --- Phase 2: quiesce each agent (driver last) -----------------------------
    for agent in QUIESCE_ORDER:
        cfg = FLEET[agent]
        strat = cfg["strategy"]
        log(f"[quiesce] {agent} (strategy={strat})...")
        if strat == "handoff_pause":
            res = quiesce_handoff(agent, cfg, dry_run=dry_run)
        elif strat == "repo_clean":
            res = quiesce_repo(agent, cfg, dry_run=dry_run, stop_services=stop_codex_services)
        elif strat == "driver":
            res = quiesce_drv(agent, cfg, chronicle_archive=chronicle_archive, dry_run=dry_run)
        else:
            res = {"agent": agent, "status": FAIL, "detail": f"unknown strategy {strat!r}"}
        report["quiesce"].append(res)
        log(f"  -> [{res.get('status')}] {res.get('action', res.get('detail', ''))}")
        # A hard FAIL on a claude agent's quiesce withholds the reboot — we do not
        # reboot a fleet with an agent that never recorded its state. (codex is
        # advisory: WARN never blocks.)
        if res.get("status") == FAIL:
            log(f"[quiesce] {agent} FAILED to quiesce — withholding reboot.")
            report["exit"] = 1
            return report

    # --- Phase 3: reboot (only under --execute, unless --skip-reboot) ----------
    if skip_reboot:
        log("[reboot] --skip-reboot: fleet quiesced; NOT rebooting.")
        report["reboot"] = {"status": OK, "action": "skipped (--skip-reboot)"}
        return report
    log("[reboot] issuing machine reboot..." if execute else "[reboot] (dry-run)")
    report["reboot"] = reboot(dry_run=dry_run)
    log(f"[reboot] -> [{report['reboot'].get('status')}] {report['reboot'].get('action', report['reboot'].get('detail', ''))}")
    log("[post] after boot, run: python3 tools/fleet_health.py  (advisory: did everyone come back?)")
    return report


# --------------------------------------------------------------------------- #
# CLI.                                                                        #
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Recovery-driven coordinated fleet reboot (T-1049).")
    ap.add_argument("--execute", action="store_true",
                    help="actually quiesce + reboot. WITHOUT this it is a dry run (default).")
    ap.add_argument("--strict", action="store_true",
                    help="make the reboot_safety pre-check a HARD gate (default: advisory).")
    ap.add_argument("--skip-reboot", action="store_true",
                    help="quiesce the fleet but do NOT reboot (e.g. to test the quiesce live).")
    ap.add_argument("--stop-codex-services", action="store_true",
                    help="also `systemctl --user stop` codex's services during its quiesce.")
    args = ap.parse_args(argv)

    if not args.execute:
        print("=== DRY RUN (pass --execute to actually quiesce + reboot) ===")
    report = orchestrate(
        execute=args.execute, strict=args.strict, skip_reboot=args.skip_reboot,
        stop_codex_services=args.stop_codex_services,
    )
    return report["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
