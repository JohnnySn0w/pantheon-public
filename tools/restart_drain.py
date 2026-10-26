#!/usr/bin/env python3
"""Pantheon restart drain — process tombstone files and fire graceful restarts.

Designed to run as a systemd-user timer on each Pantheon agent's own account
(Recovery, Orchestrator, Specialist), with a ~30s cadence. Each drain instance handles only
its own account's tombstone files.

Mechanism (per ORG-020 Phase 1):
  1. Check feature flag (PANTHEON_RESTART_DRAIN_ENABLED=1). Bail clean if unset.
  2. Scan PANTHEON_STATE_DIR for restart_requested_<agent>.json tombstone files.
  3. Per tombstone:
     a. Parse JSON; skip + remove if malformed.
     b. Skip if agent name not in DRAIN_AGENT_CONFIG (unknown agent → leave file).
     c. For reason=context_fill_threshold: skip + remove if tombstone is >5 min old
        (monitor wrote a stale tombstone; the restart window has passed).
     d. For reason=api_health_force (T-0510): FORCE restart via --skip-ack (the
        agent is 400ing and can't write a handoff). Not freshness-gated.
     e. For reason=context_fill_threshold / operator / other: GRACEFUL restart —
        NO --skip-ack, so handoff_and_restart.sh steps 2-4 prompt the (healthy)
        agent to refresh HANDOFF.md and wait for the mtime-ack before the kill
        (T-0763, revising T-0534's blanket --skip-ack: the agent writes the
        handoff autonomously via send-keys, so "no interactive operator" never
        meant "cannot write a handoff").
     f. Fire handoff_and_restart.sh for that agent via subprocess.
     f. Remove tombstone on success; leave on failure (drain retries next ~30s).
  4. Return 0 if all tombstones processed cleanly; 1 if any fire failed.

Architecture note (per ORG-020 Q2 correction):
  The ADR incorrectly described the drain running on Orchestrator on behalf of Recovery.
  The sudoers analysis showed only Recovery→Orchestrator/Specialist paths exist, not the reverse.
  Corrected architecture: each agent runs its OWN drain timer. Recovery's drain reads
  Recovery's own tombstones (no sudo needed — same account). This also sidesteps the
  circular-trigger race: the drain unit is separate from the restarted unit, so
  the drain fires and completes before the restart kills the restarted process.

Feature flag:
  The feature flag is an in-script env-var check rather than a systemd
  ConditionEnvironment= directive — the in-script check emits a log line
  visible in journalctl, making the disabled state auditable. A ConditionEnvironment=
  false would silently skip the service with no log output. Justifies-via: D1
  (operator-visible behavior is worth the ~2 LoC overhead).

Environment overrides:
  PANTHEON_RESTART_DRAIN_ENABLED — must be "1" to enable drain; any other value
                                   (including absent) is treated as disabled.
  PANTHEON_STATE_DIR             — override state dir (default ~/.local/state/pantheon).

Per-account install: one .service + .timer pair; installed by each agent account.
See tools/systemd/pantheon-restart-drain.{service,timer} for unit templates.

Exit codes:
  0 — clean bail (disabled, no tombstones, all fires succeeded).
  1 — one or more fires failed (tombstones left for retry on next drain cycle).
"""

from __future__ import annotations

import json
import os
import pwd
import re
import subprocess
import sys
import time
from pathlib import Path

# Make `from tools.X import Y` work whether invoked as `python3 tools/foo.py`
# or `python3 -m tools.foo`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.agent_role_map import account_persona_map as _account_persona_map  # noqa: E402  (needs sys.path above)
from tools.agent_role_map import instance_binding as _instance_binding  # noqa: E402  (needs sys.path above)
from tools.agent_role_map import control_plane_consume_service as _control_plane_consume_service  # noqa: E402  (needs sys.path above)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"

# Tombstone freshness gate for context_fill_threshold triggers.
# Operator-triggered tombstones are not subject to this gate.
CONTEXT_FILL_TOMBSTONE_MAX_AGE_SECONDS = 5 * 60  # 5 minutes

# T-0510: tombstone reason written by context_fill_monitor's API-health check
# when a session is 400ing on every request ("alive but erroring"). Drained via
# a FORCE restart — handoff_and_restart.sh --skip-ack — because the agent can't
# write a handoff. NOT subject to the freshness gate (a poisoned session stays
# poisoned, so a late tombstone is still valid).
API_HEALTH_FORCE_REASON = "api_health_force"

# T-0857: wedge/staleness tombstone — context_fill_monitor's wedge detector found
# a session frozen + modal-wedged. FORCE restart (--skip-ack): a wedged session
# cannot answer the handoff prompt, so a graceful beat would just stall at the
# ack-timeout then fail. Freshness-gated like context_fill_threshold (a stale
# wedge tombstone shouldn't fire long after the window).
WEDGE_STALENESS_REASON = "wedge_staleness"

# T-0824: a tombstone whose agent is NOT in DRAIN_AGENT_CONFIG is QUARANTINED here
# (moved out of the scan path) + alerted ONCE, instead of being left to silently
# re-accumulate every ~30s drain cycle. Quarantine preserves the file for
# forensics rather than deleting evidence of a real misconfig.
QUARANTINE_DIRNAME = "quarantine"

# Timeout for the systemd-run launch subprocess call.
# The old RESTART_INVOCATION_TIMEOUT_SECONDS (12 min) was a completion wait;
# with detached launch (T-0535) systemd-run returns as soon as the unit is
# *launched*, not when the restart *completes*. A launch that takes >15s
# has stalled and should be treated as failure.
DETACHED_LAUNCH_TIMEOUT_SECONDS = 15

# ---------------------------------------------------------------------------
# Per-agent drain configuration
# ---------------------------------------------------------------------------
#
# Maps the agent name (as it appears in tombstone files) to the
# handoff_and_restart.sh invocation parameters. Each agent account runs
# its own drain timer and processes only the tombstones listed here.
#
# For a given account, typically only one agent's config applies
# (e.g., Recovery's drain only drains Recovery's tombstone). However, the drain
# can be configured to handle multiple agents from one account if needed
# in future (e.g., a root-tier drain for all three). For now each account's
# drain handles the single-agent case that matches the account.
#
# restart_bin: path to handoff_and_restart.sh ON THE RUNNING ACCOUNT.
# Since drain runs as the same account as the agent, no sudo is needed for Recovery.
# For Orchestrator/Specialist tombstones that Recovery's context_fill_monitor writes (via sudo),
# Orchestrator's/Specialist's own drain timers handle those locally — no cross-account drain.

# DRAIN_AGENT_CONFIG — built from instance.yaml (T-2076), not a hardcoded dict.
# Fully account/persona-derived (no monitor-side tuning): a self-hoster configures
# accounts + services ONCE in instance.yaml. Derivation:
#   restart_service / restart_mode / restart_target_pane  <- instance.yaml (restart_service/restart_mode/restart_pane)
#   handoff_file     = /home/<account>/HANDOFF.md
#   restart_bin      = /home/<account>/pantheon-config/tools/handoff_and_restart.sh
#   restart_lockfile = /tmp/handoff_and_restart_<persona>.lock
# Provably == the former literals for the reference instance
# (test_restart_drain.TestBuildDrainConfig). Only the fire-path agents that run a
# drain timer are built; each account runs the drain for its own agent.
#
# T-2370: the persona set is DERIVED, not enumerated. This used to open with a
# hand-carried `_DRAIN_PERSONAS = ("recovery", "orchestrator", "specialist")` — the pre-ORG-042
# three — which CONTRADICTED the manifest it claims to be built from. The inner
# `"restart_service" not in b` gate below is already the correct predicate and was
# already doing the real work; the outer tuple only ever subtracted from it. The
# measured casualty: instance.yaml grants restart_service to FOUR roles (adding
# second-head/archivist), so a restart tombstone written for archivist hit the
# `agent not in DRAIN_AGENT_CONFIG` branch and was quarantined as an unknown
# agent — the drain silently did not cover a seat the manifest says is
# restartable. Deriving deletes the list rather than extending it, so the next
# instance.yaml role to gain restart_service is covered without a code edit.
#
# Scope is unchanged for the reference instance's existing three and stays
# same-account by construction: a drain only ever sees tombstones in its OWN
# account's PANTHEON_STATE_DIR (~/.local/state/pantheon, home-rooted), so
# widening this map cannot let one account fire another's restart — it only lets
# archivist's own drain act on archivist's own tombstone.


def _build_drain_config() -> dict:
    cfg: dict = {}
    for persona in sorted(set(_account_persona_map().values())):
        b = _instance_binding(persona)
        account = b.get("account")
        if not account or "restart_service" not in b:
            continue
        entry = {
            "restart_service": b["restart_service"],
            "restart_mode": b.get("restart_mode", "user"),
            "handoff_file": f"/home/{account}/HANDOFF.md",
            "restart_bin": f"/home/{account}/pantheon-config/tools/handoff_and_restart.sh",
            "restart_lockfile": f"/tmp/handoff_and_restart_{persona}.lock",
        }
        # restart_target_pane gated on presence (T-2076 review #4): a fork role that
        # omits restart_pane leaves the key ABSENT → --validate (which requires it)
        # fails loudly, rather than passing `--target-pane None` to handoff_and_restart.sh.
        if "restart_pane" in b:
            entry["restart_target_pane"] = b["restart_pane"]
        # T-2658: a control-plane-supervised persona (T-2633 cutover, e.g. specialist)
        # is NOT restarted via its (disabled) bespoke restart_service set above
        # -- OVERRIDE it here to the derived pantheon-persona-consume@<persona>
        # unit, and carry "supervisor" so handoff_and_restart.sh knows to
        # kill-session first and use the scope/session-liveness readiness gate
        # instead of `systemctl is-active` (a Type=oneshot consume unit is
        # never is-active after it exits). The bespoke value stays intact in
        # instance.yaml itself (and in agent_role_map.instance_binding()'s
        # return) for an operator reading the manifest directly -- only this
        # BUILT entry, the one the drain actually fires against, changes.
        if b.get("supervisor") == "control-plane":
            entry["restart_service"] = _control_plane_consume_service(persona)
            entry["supervisor"] = "control-plane"
        cfg[persona] = entry
    return cfg


DRAIN_AGENT_CONFIG: dict[str, dict] = _build_drain_config()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    """Emit a structured log line to stdout (captured by systemd journal)."""
    ts = int(time.time())
    print(f"[restart_drain ts={ts}] {msg}", flush=True)


def _notify_restart_applied(agent: str, reason: str, unit: str) -> None:
    """Surface a SUCCESS-path notification when a drain-applied restart launches.

    Per T-0709 (sweep follow-up to T-0708): the drain is where a queued restart
    is ACTUALLY applied — and for api_health_force tombstones it is the ONLY
    notification point (context_fill_monitor's T-0708 write-time notify covers
    reason=context_fill_threshold only). Pre-T-0709 this success path merely
    _log + unlinked the tombstone, so the real restart fired silently.

    Routed as an audit-style info Telegram direct to the operator (NOT via
    _send_alert_via_recovery): every drain restart is a same-account self-restart,
    so the restarted agent cannot reliably surface its own restart — Recovery's pane
    is about to die, and orchestrator/specialist drains cannot reach Recovery's pane
    cross-account (the recovery-routing fallback would just emit misleading
    'recovery-pane unreachable' noise). Matches the existing auto-resolve-success
    precedent (inbox_consumer pane_wedge_auto_resolved → _send_telegram_info).
    Best-effort: a send failure must never abort the drain — the restart has
    already launched.
    """
    msg = (
        f"♻️ [restart-drain] {agent} restart APPLIED (reason={reason}); "
        f"detached unit {unit} launched. Self-restart — no action needed."
    )
    try:
        try:
            from tools.inbox_consumer import _send_telegram_info  # run-as-package / tests
        except ImportError:
            from inbox_consumer import _send_telegram_info  # run-as-script (prod)
        _send_telegram_info(msg)
    except Exception as exc:
        _log(f"  restart-applied notify failed ({exc!r}); restart already launched")


def _quarantine_tombstone(path: Path, agent_name: str, state_dir: Path) -> None:
    """T-0824: move an unknown-agent tombstone into the quarantine dir + alert ONCE.

    Leaving it in place caused a silent every-30s re-scan loop with no alert (the
    T-0762 bringup-eval finding). Moving it out of the glob path stops the loop;
    the alert (best-effort, fired once at quarantine time — not on a loop, since
    the file is now out of scan) surfaces what is almost always a real misconfig
    (a tombstone written for a persona the drain doesn't know). The file is
    preserved (not deleted) for forensics.
    """
    quarantine_dir = state_dir / QUARANTINE_DIRNAME
    try:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        dest = quarantine_dir / f"{path.name}.{int(time.time())}"
        path.rename(dest)
        _log(f"  unknown agent '{agent_name}'; QUARANTINED tombstone → {dest}")
    except OSError as exc:
        # If we can't move it, at least don't crash the drain; log loudly.
        _log(f"  unknown agent '{agent_name}'; quarantine move FAILED ({exc!r}); leaving in place")
    msg = (
        f"⚠️ [restart-drain] unknown-agent tombstone for '{agent_name}' "
        f"(not in DRAIN_AGENT_CONFIG) — quarantined. Likely a recovery-layer "
        f"misconfig; investigate. (T-0824)"
    )
    try:
        try:
            from tools.inbox_consumer import _send_telegram_info  # run-as-package / tests
        except ImportError:
            from inbox_consumer import _send_telegram_info  # run-as-script (prod)
        _send_telegram_info(msg)
    except Exception as exc:
        _log(f"  quarantine alert send failed ({exc!r})")


def _is_under_home(path: str, home: str) -> bool:
    """True iff `path` is the current account's own home tree — a PATH-BOUNDARY
    check, not a string prefix (T-0892).

    A bare `path.startswith(home)` is wrong: `/home/claude` is a string prefix of
    the SIBLING homes `/home/claude-fallback` and `/home/claude-research`, so on
    the claude account those siblings get misclassified as same-account, then
    hard-fail when their 0750 homes can't be traversed from here. Requiring a
    trailing separator ('/home/claude/') makes only true descendants of home
    count, so sibling-account paths are correctly deferred (cross-account) on
    every account → --validate returns 0 everywhere.
    """
    return path == home or path.startswith(home.rstrip(os.sep) + os.sep)


def account_exists(account: str) -> bool:
    """T-2071: is `account` a real Unix account on this box? (globally-visible
    passwd DB — checkable regardless of which account runs the drain). KeyError is
    the definitive-absence signal; any OTHER exception fails OPEN (returns True) so
    a passwd-DB error never false-alarms. Sibling of
    context_fill_monitor.account_exists."""
    try:
        pwd.getpwnam(account)
        return True
    except KeyError:
        return False
    except Exception as exc:  # pragma: no cover - passwd DB pathology
        _log(f"account_exists({account!r}): unexpected {exc!r}; fail-open")
        return True


def _account_from_home_path(*paths: str | None) -> str | None:
    """Extract the `<account>` from the first `/home/<account>/…` path given.
    DRAIN_AGENT_CONFIG has no explicit account field (the drain runs same-account),
    so the account is inferred from the home-rooted paths for the T-2071 existence
    check. Returns None if no path is /home/<account>/-shaped."""
    for p in paths:
        if not p:
            continue
        m = re.match(r"/home/([^/]+)/", str(p))
        if m:
            return m.group(1)
    return None


def _validate_config() -> int:
    """T-0824: pre-arm validation. Check every DRAIN_AGENT_CONFIG persona key
    against the canonical persona set (agent_role_map.py, the runtime mirror of
    sop/standard/agent-role-map.md) and that each restart_bin path resolves.
    Returns 0 if all checks pass, 1 otherwise. Runnable at bringup before arming.
    """
    try:
        try:
            from agent_role_map import AGENT_ROLE_MAP  # run-as-script
        except ImportError:
            from tools.agent_role_map import AGENT_ROLE_MAP  # run-as-package
        valid_personas = set(AGENT_ROLE_MAP.keys())
    except Exception as exc:
        _log(f"validate: could not load canonical persona set ({exc!r})")
        return 1

    # Path checks are account-aware: each account runs its OWN drain and can only
    # stat paths under its own home. A path under another account's home is
    # unstattable from here (permission, not absence) — that account's own
    # --validate run checks it. So hard-fail only on THIS account's paths; soft-note
    # cross-account ones. Persona-key + structural-field checks are account-agnostic.
    home = str(Path.home())
    problems: list[str] = []
    skipped: list[str] = []
    for agent, cfg in DRAIN_AGENT_CONFIG.items():
        if agent not in valid_personas:
            problems.append(f"agent '{agent}' not a canonical persona {sorted(valid_personas)}")
        for field in ("restart_service", "restart_target_pane", "restart_bin"):
            if not cfg.get(field):
                problems.append(f"agent '{agent}': no {field} configured")
        # T-2071 seeder footgun: the agent's account (inferred from its /home/<acct>/
        # paths, since drain has no explicit sudo_user) must exist on this box. A
        # cross-account path is soft-skipped below, so leftover reference-instance
        # defaults on a fork's box would otherwise pass --validate silently. Account
        # existence is globally visible, so hard-check it for every agent.
        acct = _account_from_home_path(cfg.get("restart_bin"), cfg.get("handoff_file"))
        if acct and not account_exists(acct):
            problems.append(
                f"agent '{agent}': account '{acct}' (from its home paths) does not "
                f"exist on this box — its drain timer cannot run and no restart will "
                f"apply (likely leftover reference-instance defaults; set "
                f"DRAIN_AGENT_CONFIG to your real accounts)"
            )
        restart_bin = cfg.get("restart_bin")
        if restart_bin:
            if _is_under_home(str(restart_bin), home):
                if not os.path.exists(restart_bin):
                    problems.append(f"agent '{agent}': restart_bin does not resolve: {restart_bin}")
            else:
                skipped.append(f"agent '{agent}': restart_bin cross-account (not under {home}); "
                               f"that account's own --validate checks it")

    for s in skipped:
        _log(f"validate skip: {s}")
    if problems:
        for p in problems:
            _log(f"validate FAIL: {p}")
        return 1
    _log(f"validate OK: {len(DRAIN_AGENT_CONFIG)} agent config(s) structurally valid "
         f"({', '.join(sorted(DRAIN_AGENT_CONFIG))}); {len(skipped)} cross-account path(s) deferred")
    return 0


def _parse_args(argv: list[str] | None):
    import argparse
    p = argparse.ArgumentParser(description="Pantheon restart drain")
    p.add_argument(
        "--validate", action="store_true",
        help="T-0824: validate DRAIN_AGENT_CONFIG (persona keys + resolvable paths) "
             "and exit (0=ok, 1=problems). Runnable at bringup before arming.",
    )
    return p.parse_args(argv)


def _state_dir() -> Path:
    """Return the state dir, honouring PANTHEON_STATE_DIR env override."""
    env = os.environ.get("PANTHEON_STATE_DIR")
    return Path(env) if env else _DEFAULT_STATE_DIR


def _is_enabled() -> bool:
    """Return True iff PANTHEON_RESTART_DRAIN_ENABLED=1."""
    return os.environ.get("PANTHEON_RESTART_DRAIN_ENABLED") == "1"


def _find_tombstones(state_dir: Path) -> list[Path]:
    """Return all restart_requested_*.json files in state_dir."""
    if not state_dir.exists():
        return []
    return sorted(state_dir.glob("restart_requested_*.json"))


def _load_tombstone(path: Path) -> dict | None:
    """Load and parse tombstone JSON. Returns None on any failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _is_stale_context_fill(data: dict) -> bool:
    """True if this is a freshness-gated tombstone that is too old — the restart
    window has passed. Gates context_fill_threshold and wedge_staleness (T-0857);
    api_health_force is NOT gated (a poisoned session stays poisoned)."""
    if data.get("reason") not in ("context_fill_threshold", WEDGE_STALENESS_REASON):
        return False
    requested_at = data.get("requested_at")
    if not isinstance(requested_at, (int, float)):
        return True  # treat malformed timestamp as stale
    age = int(time.time()) - int(requested_at)
    return age > CONTEXT_FILL_TOMBSTONE_MAX_AGE_SECONDS


def _fire_restart(cfg: dict, agent: str, *, skip_ack: bool = False) -> tuple[bool, str]:
    """Launch handoff_and_restart.sh in a DETACHED systemd-run transient unit.

    Returns (success, output) where:
      success=True  — systemd-run returned rc=0 (unit launched in its own
                      cgroup). The restart proceeds independently of the drain;
                      the drain's job is done.
      success=False — either the systemd-run launch failed (rc!=0) or an
                      OSError/TimeoutExpired was raised on the launch call.
                      Tombstone retained for retry on the next drain cycle.

    Detaching into a separate cgroup (T-0535) means stopping/disabling the
    drain service mid-flight no longer SIGTERM-kills the in-progress restart.

    skip_ack=True appends --skip-ack to the inner handoff_and_restart invocation,
    bypassing the handoff-prompt + ack-wait (steps 2-4). The drain passes
    skip_ack=True ONLY for forced restarts (reason=api_health_force) where the
    agent is 400ing and cannot write a handoff (T-0763, revising T-0534). For
    graceful restarts (context_fill_threshold, operator, …) it passes
    skip_ack=False, so steps 2-4 prompt the healthy agent to refresh HANDOFF.md
    and wait for the mtime-ack before the kill — the agent writes the handoff
    autonomously via send-keys, no interactive operator required. Either way the
    wrapper still owns systemctl (steps 5-8 run), so this is not a parallel
    restart path.

    The default remains False so the function stays general; callers must be
    explicit (P1 — call-site legibility over implicit convention).
    """
    restart_bin = cfg["restart_bin"]
    lockfile = cfg["restart_lockfile"]
    mode = cfg["restart_mode"]

    # inner: the actual handoff_and_restart.sh invocation that runs inside the
    # transient unit's shell. XDG_RUNTIME_DIR shim is belt-and-suspenders:
    # --setenv on systemd-run is the correct mechanism; the inner shim is
    # preserved for consistency with self_restart.sh and works in both user
    # and system mode.
    inner = (
        f"XDG_RUNTIME_DIR=/run/user/$(id -u) {restart_bin} "
        f"--service {cfg['restart_service']} "
        f"--mode {mode} "
        f"--target-pane {cfg['restart_target_pane']} "
        f"--handoff-file {cfg['handoff_file']} "
        f"--lockfile {lockfile}"
    )
    # Per-agent steady-state sentinel override (the shared default is stale for
    # Claude Code 2.1.x — see DRAIN_AGENT_CONFIG note). Single-quoted: the
    # pattern contains no single quotes, so '?', '+', '|', '❯' stay literal.
    if cfg.get("idle_sentinel"):
        inner += f" --idle-sentinel '{cfg['idle_sentinel']}'"
    if skip_ack:
        inner += " --skip-ack"
    else:
        # T-2829: a GRACEFUL restart (context_fill_threshold, operator, …) runs the
        # handoff beat (steps 2-4), which STALLS if the session is wedged behind a
        # permission modal — the modal captures the input the handoff prompt needs,
        # so the session never hands off and the restart used to exit 4 leaving it
        # wedged (the 2026-08-11 specialist settings.json-modal incident, ~3.3d stuck).
        #   --dismiss-modal              : Escape first (fail-closed — cancels the
        #                                  modal, never approves) so a modal-wedged
        #                                  session can still write its handoff.
        #   --hard-restart-on-ack-timeout: if it STILL can't hand off within the
        #                                  timeout, fall through to the restart
        #                                  rather than abandon it wedged.
        # Force restarts (--skip-ack) skip steps 2-4 entirely and kill the process
        # (the modal dies with it), so neither flag applies there.
        inner += " --dismiss-modal --hard-restart-on-ack-timeout"
    # T-2658: control-plane seats (specialist, T-2633 cutover) need the kill-
    # session + scope/session-liveness branch inside handoff_and_restart.sh
    # instead of a plain `systemctl restart` of a bespoke unit.
    if cfg.get("supervisor") == "control-plane":
        inner += " --supervisor control-plane"

    # Derive collision-free unit name from the service stem (mirrors self_restart.sh:81).
    # e.g. claude-telegram-fallback.service → drain-restart-claude-telegram-fallback
    stem = cfg["restart_service"].removesuffix(".service")
    stem_safe = re.sub(r"[^a-zA-Z0-9]+", "-", stem).rstrip("-")
    unit_name = f"drain-restart-{stem_safe}"

    # Scope flag: --user for user-mode units; absent for system-mode.
    scope_flag = "--user" if mode == "user" else None

    # Step 1: reset-failed (best-effort — clears any lingering failed instance
    # so systemd-run can reuse the fixed unit name; a failed unit would otherwise
    # block the launch). Errors are swallowed: a missing unit always returns rc=1,
    # and an OSError/Timeout here must not abort the launch attempt.
    reset_cmd = ["systemctl"]
    if scope_flag:
        reset_cmd.append(scope_flag)
    reset_cmd += ["reset-failed", unit_name]
    try:
        subprocess.run(reset_cmd, capture_output=True, check=False, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass  # best-effort; proceed to launch regardless

    # Step 2: launch the transient unit (detached — own cgroup).
    run_cmd = ["systemd-run"]
    if scope_flag:
        run_cmd.append(scope_flag)
    run_cmd += ["--collect", f"--unit={unit_name}"]
    if mode == "user":
        run_cmd.append(f"--setenv=XDG_RUNTIME_DIR=/run/user/{os.getuid()}")
        # T-4834: --collect GCs a failed unit and the tombstone is already removed, so
        # a failed restart (e.g. the T-4828 exit-3 dialog refusal) was silent. Page
        # Recovery through the user-unit alert template; its journal read outlives GC.
        # systemd-run -p does not expand %n, so the instance is the literal unit name.
        run_cmd += ["-p", f"OnFailure=pantheon-unit-failure-alert@{unit_name}.service.service"]
    run_cmd += ["--", "bash", "-c", inner]

    try:
        result = subprocess.run(
            run_cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=DETACHED_LAUNCH_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"systemd-run launch error: {exc!r}"

    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run one drain cycle. Returns 0 on clean bail or all-success; 1 on any failure."""

    args = _parse_args(argv)
    if args.validate:
        return _validate_config()

    if not _is_enabled():
        _log("drain disabled; PANTHEON_RESTART_DRAIN_ENABLED not set to '1'; exiting")
        return 0

    state_dir = _state_dir()
    tombstones = _find_tombstones(state_dir)

    if not tombstones:
        _log(f"no tombstones found in {state_dir}; nothing to do")
        return 0

    _log(f"found {len(tombstones)} tombstone(s): {[p.name for p in tombstones]}")

    any_failure = False

    for tombstone_path in tombstones:
        agent_name = tombstone_path.stem.removeprefix("restart_requested_")
        _log(f"processing tombstone: {tombstone_path.name} (agent={agent_name})")

        # Load and validate JSON
        data = _load_tombstone(tombstone_path)
        if data is None:
            _log(f"  malformed tombstone JSON; removing {tombstone_path.name}")
            tombstone_path.unlink(missing_ok=True)
            continue

        # Check agent is known. T-0824: an unknown-agent tombstone used to be left
        # in place and silently re-scanned every ~30s forever. Quarantine it (move
        # out of the scan path so it stops re-accumulating) + alert once.
        if agent_name not in DRAIN_AGENT_CONFIG:
            _quarantine_tombstone(tombstone_path, agent_name, state_dir)
            continue

        reason = data.get("reason", "unknown")
        # is_force gates --skip-ack (T-0763, revising T-0534): ONLY a poisoned
        # api_health_force session — which literally cannot answer a prompt — gets
        # a forced restart that skips the handoff beat. Every OTHER reason
        # (context_fill_threshold, operator, …) is a GRACEFUL restart of a healthy
        # agent: handoff_and_restart.sh steps 2-4 prompt it to refresh HANDOFF.md
        # and wait for the mtime-ack first. The agent writes the handoff
        # autonomously via send-keys, so "no interactive operator" (T-0534's
        # rationale) does NOT mean "cannot write a handoff" — those were conflated,
        # and that conflation cut off Recovery's handoff on 2026-06-03.
        # Note: api_health_force is not freshness-gated; context_fill_threshold and
        # wedge_staleness (T-0857) are. wedge_staleness also forces --skip-ack: a
        # wedged session cannot answer the handoff prompt (Recovery-locked decision).
        is_force = reason in (API_HEALTH_FORCE_REASON, WEDGE_STALENESS_REASON)

        # Freshness gate for context_fill_threshold triggers
        if _is_stale_context_fill(data):
            _log(f"  context_fill_threshold tombstone is stale (>{CONTEXT_FILL_TOMBSTONE_MAX_AGE_SECONDS}s old); removing")
            tombstone_path.unlink(missing_ok=True)
            continue

        # Fire the restart — skip_ack gated on reason (T-0763): api_health_force
        # forces (poisoned session can't hand off); every graceful reason runs the
        # pre-restart handoff beat (steps 2-4) so the fresh session boots from a
        # current HANDOFF.md instead of a stale one.
        cfg = DRAIN_AGENT_CONFIG[agent_name]
        # Derive unit name here for the log (mirrors _fire_restart's derivation).
        _stem = cfg["restart_service"].removesuffix(".service")
        _unit = f"drain-restart-{re.sub(r'[^a-zA-Z0-9]+', '-', _stem).rstrip('-')}"
        _log(
            f"  launching detached restart unit for {agent_name} "
            f"(service={cfg['restart_service']}, unit={_unit}, "
            f"reason={reason}{', --skip-ack FORCE' if is_force else ', graceful handoff beat'})"
        )
        success, output = _fire_restart(cfg, agent_name, skip_ack=is_force)

        if success:
            _log(
                f"  detached restart unit '{_unit}' launched for {agent_name}; "
                f"tombstone removed (T-0535)"
            )
            _notify_restart_applied(agent_name, reason, _unit)  # T-0709
            tombstone_path.unlink(missing_ok=True)
        else:
            _log(f"  systemd-run launch FAILED for {agent_name} (unit={_unit}): {output[:300]}")
            _log(f"  tombstone retained for retry on next drain cycle")
            any_failure = True

    return 1 if any_failure else 0


if __name__ == "__main__":
    sys.exit(main())
