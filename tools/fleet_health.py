#!/usr/bin/env python3
"""Read-only fleet-health verification sweep (T-1049 — the *verifiable* half).

Answers one question across the WHOLE Pantheon fleet: "did everyone come back
healthy?" — the check I ran by hand after the 2026-06-10 reboot, made repeatable.
It is the post-reboot (or anytime) verification tool the "fleet health sweep"
memory described as a manual procedure; `verify_startup.sh` is per-agent only and
has a boot-dialog DE-WEDGE side-effect, so it is not a clean read-only sweep.

STRICTLY READ-ONLY: every check is `is-active` / `is-enabled` / `has-session` /
a throwaway bwrap probe. It NEVER restarts, injects, de-wedges, or mutates
anything — it reports + sets an exit code. Remediation/orchestration (reboot
ordering, who drives, auto-repair) is deliberately OUT of scope — that is the
policy half of T-1049 (the operator's steer). This tool just tells you the truth
about the fleet.

A seat can also be DELIBERATELY PAUSED (T-2393 class, T-2706 here) — taken out
of service on purpose (operator disable), not dead. `check_agent` probes
`is-enabled` on the seat's session-owning unit first; `disabled`/`masked`
short-circuits to a distinct PAUSED status instead of walking into a FAIL. See
`_is_paused` — it mirrors channel_liveness_monitor._session_unit_is_disabled's
discriminator (is-enabled, not is-active) exactly, so the two tools agree on
what "paused" means. PAUSED is bucketed OUT of the FLEET DEGRADED trigger and
OUT of the exit code (a paused seat is not an incident).

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
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# Make `from tools.X import Y` work whether invoked as `python3 tools/foo.py`
# or `python3 -m tools.foo` (same shim as restart_drain.py).
from pathlib import Path as _Path
_REPO_ROOT = _Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.agent_role_map import control_plane_consume_service as _control_plane_consume_service  # noqa: E402  (needs sys.path above)
from tools.agent_role_map import instance_binding as _instance_binding  # noqa: E402  (needs sys.path above)
from tools import seat_state  # noqa: E402  (needs sys.path above)
from tools import chronicle_export  # noqa: E402  (needs sys.path above)


def _consume_timer(persona: str) -> str:
    """Derive the pantheon-persona-consume@<persona>.timer unit name from the
    canonical .service name (agent_role_map.control_plane_consume_service is
    the single source of truth for the template-unit naming fact), matching
    the .service/.timer pair template (tools/systemd/pantheon-persona-
    consume@.{service,timer})."""
    return _control_plane_consume_service(persona).removesuffix(".service") + ".timer"


def _liveness_services(persona: str, bespoke: list[str]) -> list[str]:
    """Return the correct liveness-probe unit list for *persona* (T-2685).

    A control-plane-supervised seat (T-2590/T-2633 cutover, recovery/specialist) is
    NOT probed via its disabled bespoke unit (retired at cutover -> always
    inactive -> false FAIL on a healthy seat) and NOT via the consume
    SERVICE (Type=oneshot -- active only while firing; restart_drain.py's own
    T-2658 comment: "a Type=oneshot consume unit is never is-active after it
    exits" -- same false-FAIL shape). The consume TIMER stays is-active
    ("active", waiting) continuously while armed, which is the correct
    persistent-liveness signal and reuses check_agent's existing is-active
    probe unchanged -- no new probe type needed.

    Reads the same `supervisor` marker restart_drain.py / context_fill_monitor.py
    already branch the restart mechanism on (T-2658/T-2684), so this can't
    drift independently of a persona's actual control-plane status the way a
    second, FLEET-local flag could. Returns *bespoke* unchanged for any seat
    without supervisor == "control-plane" (orchestrator pre-T-2953 today, pending
    its own cutover; recovery flipped on its T-3026 cutover 2026-09-29, and a
    completed cutover flips the persona to the timer automatically)."""
    if _instance_binding(persona).get("supervisor") == "control-plane":
        return [_consume_timer(persona)]
    return bespoke


# Load-bearing facts per agent. `services` = units whose inactivity means the
# agent is DOWN. `session` = the tmux session the agent lives in. `bwrap` flags
# the codex sandbox userns probe.
FLEET = {
    "recovery": {
        "user": "claude-fallback",
        "session": "claude-telegram-bot-fallback",
        "services": _liveness_services("recovery", ["claude-telegram-fallback.service"]),
    },
    "orchestrator": {
        "user": "claude",
        "session": "claude-telegram-bot",
        "services": _liveness_services("orchestrator", ["claude-telegram.service"]),
    },
    "specialist": {
        "user": "claude-research",
        "session": "claude-research-bot",
        "services": _liveness_services("specialist", ["claude-telegram-research.service"]),
    },
    "codex": {
        "user": "codex",
        "session": "codex-session",
        "services": ["codex-daemon.service", "codex-telegram-poller.service"],
        "bwrap": True,
    },
    # Sub-orchestrator seats (ORG-043), brought onto the consume control plane
    # 2026-08-08 (T-2677). Mailbox-first — NO telegram channel, so no poller/
    # bridge unit belongs here. _liveness_services would normally auto-derive
    # the consume timer from instance.yaml's `supervisor: control-plane`
    # marker (as it does for recovery/specialist below), but mason/artisan's `roles:`
    # binding does not yet carry that field even though T-2677 moved them onto
    # the control plane (T-2677 only filled the separate harness_runtime
    # launch-profile stanza, not the roles: binding block agent_role_map.py
    # reads — a manifest gap, flagged to HO, out of this ticket's scope to
    # fix). So the bespoke list is supplied explicitly instead of relying on
    # auto-derivation; the timer name is verified live-armed per T-2677's
    # resolve note ("both timers armed... fires green").
    "mason": {
        "user": "suborch1",
        "session": "suborch1-main",
        "services": _liveness_services("mason", ["pantheon-persona-consume@mason.timer"]),
    },
    "artisan": {
        "user": "suborch2",
        "session": "suborch2-main",
        "services": _liveness_services("artisan", ["pantheon-persona-consume@artisan.timer"]),
    },
    # Archivist — the second head (ORG-042), resident on the `exocortex`
    # account. Bespoke-telegram-service shape (T-2690 signal), NOT the consume
    # control plane — her instance.yaml binding has no `supervisor:
    # control-plane` marker (confirmed against agent_role_map.py), so
    # _liveness_services resolves to the bespoke list unchanged -- same
    # mechanism as any pre-cutover bespoke seat (orchestrator pre-T-2953); a
    # completed cutover flips the persona to the timer automatically (recovery,
    # T-3026, 2026-09-29).
    "archivist": {
        "user": "exocortex",
        "session": "claude-telegram-bot-exocortex",
        "services": _liveness_services("archivist", ["claude-telegram-exocortex.service"]),
    },
    # Watchman (T-4884 B6) — codex-primary recovery seat on `codex-fallback`, born on
    # the control plane (instance.yaml roles.system-orchestrator-1 carries
    # `supervisor: control-plane`), so _liveness_services derives the consume timer.
    # The bespoke list is empty: there was never a bespoke unit, and no Telegram
    # poller (partial seating, design §6). A codex TUI, but NOT bwrap-flagged: it
    # runs sandbox danger-full-access (instance.yaml harness_runtime), so the
    # userns probe the peer daemon's workspace-write sandbox needs does not apply.
    "watchman": {
        "user": "codex-fallback",
        "session": "codex-fallback-main",
        "services": _liveness_services("watchman", []),
    },
}

OK, WARN, FAIL, PAUSED = "OK", "WARN", "FAIL", "PAUSED"
# ORG-106 L4b (T-5527): a seat in an expected deep-sleep (phase asleep) with nothing running.
# Bucketed like PAUSED: visible, never a FAIL, never DEGRADED. A sleeping seat that is NOT
# down is a FAIL instead (check "sleep_breach").
ASLEEP = "ASLEEP"


# sudo, under -n, never prompts, and prefixes its OWN diagnostics with "sudo:".
# When the caller lacks NOPASSWD to the target account (every non-Recovery seat — only
# Recovery holds NOPASSWD to every agent user, per the module docstring), `sudo -n` exits
# rc=1 with one of these markers on stderr and EMPTY stdout, WITHOUT running the inner
# command — subprocess.run does not raise, so this must be recognised as "never ran"
# (T-2718). Matching a specific marker (not merely rc!=0) is deliberate: the inner
# command running and returning non-zero is a genuine FAIL and must NOT be softened to
# WARN — silencing a real dead seat is the expensive direction this tool guards against.
_SUDO_DENIAL_MARKERS = (
    "a password is required",   # -n + caller lacks NOPASSWD to this account (the T-2718 repro)
    "a terminal is required",   # -n + password needed but no tty
    "no askpass",               # -n + no askpass helper
    "unknown user",             # bad target account
    "not allowed to execute",   # sudoers denies this command for the caller
)


def _sudo_denied(rc, stderr: str) -> bool:
    """True iff `sudo` ITSELF refused to run the command (auth/setup denial), so the
    inner command never executed. sudo prefixes its diagnostics with "sudo:" and does
    not prompt under -n, so a "sudo:" line carrying a known denial marker means the
    request never reached the inner command — distinct from the inner command running
    and returning non-zero (a real FAIL)."""
    if rc == 0:
        return False
    low = (stderr or "").lower()
    return "sudo:" in low and any(mark in low for mark in _SUDO_DENIAL_MARKERS)


def _run(user: str, inner: str, *, timeout: int = 15):
    """Run `inner` as `user` (no sudo if it's the current user). Returns
    (rc, stdout, ran) — ran=False if the command couldn't even execute (sudo
    denied / timeout / OS error), which callers treat as WARN not FAIL."""
    import getpass
    wrapped = user != getpass.getuser()
    if not wrapped:
        argv = ["bash", "-lc", f"XDG_RUNTIME_DIR=/run/user/$(id -u) {inner}"]
    else:
        argv = ["sudo", "-n", "-u", user, "bash", "-lc",
                f"XDG_RUNTIME_DIR=/run/user/$(id -u) {inner}"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return (None, "", False)
    # A `sudo -n` auth/setup denial exits non-zero with empty stdout but does NOT raise;
    # treat it as "never ran" (WARN), never as the inner command's verdict (FAIL) — T-2718.
    if wrapped and _sudo_denied(r.returncode, r.stderr):
        return (r.returncode, "", False)
    return (r.returncode, r.stdout.strip(), True)


def _is_paused(user: str, svc: str, run) -> bool | None:
    """Is *svc* (the seat's session-owning unit — `cfg["services"][0]`)
    DISABLED, i.e. deliberately taken out of service (T-2393 discriminator,
    mirrored from channel_liveness_monitor._session_unit_is_disabled — same
    is-enabled probe, same state table, so the two tools agree on "paused").

    Returns True (disabled/masked -> paused), False (enabled/static/... ->
    judge normally), or None when we cannot tell (unqueryable, or an
    unrecognized is-enabled state). None must NOT be read as paused: fail
    toward judging the seat normally, because silencing a real dead seat is
    the expensive direction (same posture the source probe documents)."""
    rc, out, ran = run(user, f"systemctl --user is-enabled {svc}")
    if not ran:
        return None
    state = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if not state:
        return None
    if state in ("disabled", "masked", "masked-runtime"):
        return True
    if state in ("enabled", "enabled-runtime", "static", "linked", "linked-runtime",
                 "indirect", "generated", "transient", "alias"):
        return False
    return None


# T-3210: a consume seat whose attempt stage is policy-skipped (backoff /
# RuntimeTransitionPending / ProviderQuotaExhausted / RuntimeViabilityBlocked)
# completes every fire cleanly (exit 0/5) -- it reads healthy to every check
# above. persona_bringup.py's bump_policy_skip/load_policy_skip persist a
# per-persona consecutive-streak counter (<persona>.policyskip.json) this
# reads cross-account, same shape as the other cross-account probes here.
#
# Floor, not a raw count>=1 gate: a single backoff/breaker trip is routine
# operation -- the row's purpose is the PERSISTENT case, once it has run past
# any transient blip (5 fires ~= 5-15min at the observed consume cadences).
# Below the floor the counter still counts (the journal payload each fire
# carries the running count+since regardless of whether this sweep renders
# a row for it).
POLICY_SKIP_ROW_MIN_FIRES = 5


def _read_policy_skip(user: str, persona: str, *, run=_run) -> dict | None:
    """Read *persona*'s policy-skip counter (<persona>.policyskip.json) from
    *user*'s state dir cross-account -- same PANTHEON_PERSONA_STATE_DIR
    default persona_bringup.py uses (%h/.local/state/pantheon/personas).
    Returns the parsed dict, or None if absent/unreadable/corrupt: an
    unreadable counter must not fabricate a row, same fail-safe posture as
    _is_paused above."""
    rc, out, ran = run(
        user,
        "cat ~/.local/state/pantheon/personas/" f"{persona}.policyskip.json 2>/dev/null",
    )
    if not ran or not out.strip():
        return None
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _format_skip_duration(seconds: float) -> str:
    """Render an elapsed-seconds float as a single self-describing unit --
    "27 consecutive skips since <ts>" needs a human duration, not a raw
    timestamp delta, for an operator glancing at the row."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _seat_in_maintenance(persona: str) -> bool:
    """T-5285: is *persona* in an ACTIVE maintenance window right now? Any fault
    (unreadable registry, import error) reads False, so the row falls through to
    WARN -- the safe direction for a monitor. (ORG-106 L4b: shim over the shared
    seat_state.maintenance_active; same registry, same fail-open.)"""
    return seat_state.maintenance_active(persona)


def _seat_note(e) -> dict | None:
    """The always-visible seat-state annotation row for a paused / draining seat, or None."""
    if e is None:
        return None
    until = e.expires or "no expiry"
    who = f"{e.owner or '?'}: {e.reason or '-'}"
    if e.alertable is True or e.state == seat_state.STATE_HALTED:
        # T-5868: HALTED is an incident (red, with the rollback command), never "expected".
        return {"name": "seat-state", "status": FAIL,
                "detail": f"HALTED ({who}); rollback: {e.rollback_cmd or '(see seat-state record)'}"}
    if e.state == seat_state.STATE_PAUSED_INTAKE:
        return {"name": "seat-state", "status": OK,
                "detail": f"paused (intake held; {who}; until {until})"}
    if e.state == seat_state.STATE_DEEP_SLEEP:
        return {"name": "seat-state", "status": OK,
                "detail": f"draining toward deep-sleep ({who}; until {until})"}
    return None


def _session_live(user: str, cfg: dict, run) -> bool | None:
    """Is anything of this seat running (a load-bearing service active or its tmux session)?
    True on any positive signal; None when no probe could run (unknown)."""
    ran_any = False
    for svc in cfg["services"]:
        rc, out, ran = run(user, f"systemctl --user is-active {svc}")
        ran_any = ran_any or ran
        if ran and out == "active":
            return True
    rc, out, ran = run(user, f"tmux has-session -t {cfg['session']} 2>/dev/null && echo yes || echo no")
    ran_any = ran_any or ran
    if ran and out.endswith("yes"):
        return True
    return False if ran_any else None


def _policy_skip_check(persona: str, user: str, *, run=_run, seat=None) -> dict | None:
    """Render a check for *persona* iff its consume is CURRENTLY mid a
    consecutive policy-skip streak at or past POLICY_SKIP_ROW_MIN_FIRES.
    Returns None below the floor or when there is no counter file at all --
    the row exists for the persistent case, not every transient blip.

    Status: OK when instance.yaml's `expected_policy_skip` (T-3210) matches
    the CURRENT reason -- this is the expected-not-cut-over RECORD, not an
    alarm. WARN otherwise (no manifest key, or a mismatch) -- WARN in the
    fleet summary means "look at this"."""
    state = _read_policy_skip(user, persona, run=run)
    if not state:
        return None
    reason, count, since = state.get("reason"), state.get("count"), state.get("since")
    if not (isinstance(reason, str) and reason and isinstance(count, int)):
        return None
    if count < POLICY_SKIP_ROW_MIN_FIRES:
        return None
    duration = "?"
    if isinstance(since, str):
        try:
            started = datetime.fromisoformat(since.replace("Z", "+00:00"))
            duration = _format_skip_duration(
                (datetime.now(timezone.utc) - started).total_seconds()
            )
        except ValueError:
            pass
    expected = _instance_binding(persona).get("expected_policy_skip")
    if reason == "all_runtimes_held":
        # T-5285: every configured runtime is operator-held -- a deliberate state.
        status, tail = OK, "expected (all runtimes held)"
    elif reason == "seat_maintenance" and _seat_in_maintenance(persona):
        # Only while the window is live: a stale streak after it ends is NOT expected.
        status, tail = OK, "expected (seat maintenance window)"
    elif reason == "seat_paused" and seat is not None and seat.state == seat_state.STATE_PAUSED_INTAKE:
        status, tail = OK, "expected (seat paused)"
    elif reason == "seat_sleep" and seat is not None and seat.state == seat_state.STATE_DEEP_SLEEP:
        status, tail = OK, "expected (seat in deep-sleep)"
    elif expected == reason:
        status, tail = OK, "expected (manifest expected_policy_skip)"
    elif expected:
        status, tail = WARN, f"unexpected (manifest expects {expected})"
    else:
        status, tail = WARN, "unexpected"
    detail = f"policy-skipped {reason} for {duration} ({count} fires) — {tail}"
    return {"name": "consume:policy-skip", "status": status, "detail": detail}


_HARNESS_OVERRIDES_SPLIT = "@@T5028@@"


def _harness_overrides_check(persona: str, user: str, *, run=_run) -> dict | None:
    """T-5028 (f)/(f+): one cross-account read of the seat's harness-override
    spool (unlanded.jsonl), quarantine (quarantine.jsonl) and the override rows
    of its controller-evidence log. A WARN row only when something needs a
    human: an unlanded or open quarantined ledger entry, or an override with no
    outcome after the grace (a swap/restart that died mid-way). Unreadable or
    clean renders no row, the same fail-safe posture as _read_policy_skip."""
    from tools import harness_state

    base = "~/.local/state/pantheon"
    evidence = f"{base}/personas/{persona}/runtime-controller/controller-evidence.jsonl"
    sep = _HARNESS_OVERRIDES_SPLIT
    rc, out, ran = run(
        user,
        f"tail -c 2000000 {base}/harness-overrides/unlanded.jsonl 2>/dev/null; echo {sep}; "
        f"tail -c 2000000 {base}/harness-overrides/quarantine.jsonl 2>/dev/null; echo {sep}; "
        f"tail -c 2000000 {evidence} 2>/dev/null | grep -h '\"event\": \"operator_swap_hold_overrid' "
        "| tail -n 400",
    )
    del rc
    if not ran or out.count(sep) != 2:
        return None
    unlanded_text, quarantine_text, override_text = out.split(sep)
    unlanded = sum(1 for line in unlanded_text.splitlines() if line.strip().startswith("{"))
    quarantined = len(harness_state.open_quarantine_rows(quarantine_text.splitlines()))
    orphans = harness_state.orphan_override_rows(override_text.splitlines(), now=datetime.now(timezone.utc))
    if not (unlanded or quarantined or orphans):
        return None
    parts = []
    if unlanded:
        parts.append(f"{unlanded} unlanded ledger entr{'y' if unlanded == 1 else 'ies'} (retried by the next harness run)")
    if quarantined:
        parts.append(f"{quarantined} quarantined (`harness {persona} quarantine list`)")
    if orphans:
        parts.append(
            f"{len(orphans)} hold override(s) with no outcome: "
            + ", ".join(f"{o.get('destination')} {o.get('correlation_id')}" for o in orphans[:3])
        )
    return {"name": "harness-overrides", "status": WARN, "detail": "; ".join(parts)}


def check_agent(name: str, cfg: dict, *, run=_run) -> dict:
    """Return {agent, status, checks:[{name,status,detail}]}. status is the worst
    of its checks (FAIL > WARN > OK) — UNLESS the seat is deliberately PAUSED
    (T-2706), in which case this short-circuits before running the normal
    checks at all (a paused seat's is-active/session reads are moot noise, not
    an incident) and returns PAUSED with a single explanatory check."""
    user = cfg["user"]
    checks: list[dict] = []

    # ORG-106 L4b (T-5527): the seat's declared state, read ONCE and BEFORE every early
    # return below. A broken sleep (expected_down but something is running) is evaluated
    # first so no other branch (a disabled unit reading as PAUSED, a hold) can hide it.
    seat = seat_state.safe_effective_for(name)
    if seat is not None and seat.expected_down:
        live = _session_live(user, cfg, run)
        who = f"{seat.owner or '?'}: {seat.reason or '-'}; until {seat.expires or 'no expiry'}"
        if live:
            checks.append({"name": "sleep_breach", "status": FAIL,
                           "detail": f"declared asleep ({who}) but a session or service is running"})
            # fall through: the ordinary checks below add the evidence
        else:
            checks.append({"name": "seat-state", "status": OK,
                           "detail": f"asleep (deep-sleep, expected down; {who})"})
            return {"agent": name, "status": ASLEEP, "checks": checks}
    else:
        note = _seat_note(seat)
        if note is not None:
            checks.append(note)

    session_owning_svc = cfg["services"][0] if cfg.get("services") else None
    if session_owning_svc is not None:
        paused = _is_paused(user, session_owning_svc, run)
        if paused and not any(c["name"] == "sleep_breach" for c in checks):
            checks.append({"name": f"paused:{session_owning_svc}", "status": PAUSED,
                           "detail": f"{session_owning_svc} disabled — seat "
                           "deliberately paused, not judging further"})
            return {"agent": name, "status": PAUSED, "checks": checks}

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

    policy_skip = _policy_skip_check(name, user, run=run, seat=seat)
    if policy_skip is not None:
        checks.append(policy_skip)

    overrides = _harness_overrides_check(name, user, run=run)
    if overrides is not None:
        checks.append(overrides)

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


def chronicle_coverage_sweep(agents: list[str], *, run=_run) -> list[dict]:
    """T-5016: per seat, is its last session end archived to its Chronicle?

    Runs tools/session_chronicle.py `coverage` AS the seat's account (its own
    Chronicle and runtime state live in its 700 home), the same cross-account
    `_run` posture as every other check here. Read-only. Kept as its own sweep
    (not a check_agent row) so the per-agent liveness verdicts stay exactly what
    they were. A check that cannot run is a WARN, never a FAIL."""
    records: list[dict] = []
    for agent in agents:
        user = FLEET[agent]["user"]
        rc, out, ran = run(user, "python3 ~/pantheon-config/tools/session_chronicle.py "
                                 f"coverage --persona {agent} --tmux-target {FLEET[agent]['session']}")
        if not ran:
            records.append({"agent": agent, "status": WARN, "detail": "could not query (sudo/timeout)"})
            continue
        try:
            parsed = json.loads((out or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            records.append({"agent": agent, "status": WARN, "detail": "unreadable coverage output"})
            continue
        status = parsed.get("status") if parsed.get("status") in (OK, WARN, FAIL) else WARN
        records.append({"agent": agent, "status": status,
                        "detail": f"{parsed.get('detail', '?')} (last restart {parsed.get('last_restart')}, "
                                  f"latest archive {parsed.get('latest_archive')})"})
    return records


def render_chronicle(records: list[dict]) -> str:
    glyph = {OK: "✓", WARN: "!", FAIL: "✗"}
    lines = [f"[{glyph[r['status']]}] chronicle {r['agent']:9} {r['status']}: {r['detail']}"
             for r in records if r["status"] != OK]
    fails = [r["agent"] for r in records if r["status"] == FAIL]
    lines.append(f"CHRONICLE: no archive after last restart on {', '.join(fails)}" if fails
                 else "CHRONICLE: every checked seat's last session end is archived (or unknowable)")
    return "\n".join(lines)


# T-6118 PR-4b. SOFT: a low transcript ratio is a WARN and never moves the exit code.
CHRONICLE_TRANSCRIPT_DAYS = 7
# Fleet agent -> its chronicle-export seat dir. archivist decision 1 (2026-11-04): the codex
# persona exports under the appserver seat `consultant`; every other agent exports under its own name.
EXPORT_SEAT = {"codex": "consultant"}


def transcript_coverage_sweep(agents: list[str], *, run=_run) -> list[dict]:
    """Per seat, the share of non-hollow session ends that have a transcript (7d), run AS the seat's
    account like chronicle_coverage_sweep. Below threshold or unqueryable is WARN, never FAIL."""
    records: list[dict] = []
    for agent in agents:
        user = FLEET[agent]["user"]
        rc, out, ran = run(user, "python3 ~/pantheon-config/tools/session_chronicle.py "
                                 f"transcript-coverage --persona {agent} --days {CHRONICLE_TRANSCRIPT_DAYS} --json")
        if not ran:
            records.append({"agent": agent, "status": WARN, "ratio": None,
                            "detail": "could not query (sudo/timeout)"})
            continue
        try:
            parsed = json.loads((out or "").strip().splitlines()[-1])
            if not isinstance(parsed, dict) or parsed.get("status") not in (OK, FAIL):
                raise ValueError("no verdict")
        except (ValueError, IndexError):
            records.append({"agent": agent, "status": WARN, "ratio": None,
                            "detail": "unreadable transcript-coverage output"})
            continue
        records.append({"agent": agent, "status": OK if parsed["status"] == OK else WARN,
                        "ratio": parsed.get("ratio"), "eligible": parsed.get("eligible"),
                        "with_transcript": parsed.get("with_transcript"), "hollow": parsed.get("hollow"),
                        "threshold": parsed.get("threshold"), "detail": parsed.get("detail", "?")})
    return records


_EXPORT_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_real_dir(path: str, *, dir_fd: int | None = None) -> int | None:
    """fd of a real directory, or None. O_NOFOLLOW|O_DIRECTORY: a symlink (to anything) is never entered."""
    try:
        return os.open(path, _EXPORT_DIR_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None


def export_size_sweep(agents: list[str], *, days: int = CHRONICLE_TRANSCRIPT_DAYS,
                      now: datetime | None = None) -> tuple[dict, str | None]:
    """({export_seat: {YYYY-MM-DD: bytes}}, note). Reads the export tree directly (group-readable, no
    sudo; PANTHEON_CHRONICLE_EXPORT_ROOT is honoured). Bytes are the simple sum of st_size of regular
    files (lstat) per date dir; seat and date dirs are opened O_NOFOLLOW (a symlinked dir counts 0): a file hardlinked to the Chronicle copy shares an inode, so this is
    apparent bytes per file name, not unique disk usage. `.ingested` is skipped. An unreadable or
    unprovisioned tree gives a note, never an error."""
    root = chronicle_export.export_root()
    current = now or datetime.now(timezone.utc)
    floor = (current - timedelta(days=days)).strftime("%Y-%m-%d")
    note = "export tree unreadable/unprovisioned"
    sizes: dict[str, dict[str, int]] = {}
    try:
        if not root.is_dir():
            return {}, note
        for seat in sorted({EXPORT_SEAT.get(a, a) for a in agents}):
            per_day: dict[str, int] = {}
            sizes[seat] = per_day
            seat_fd = _open_real_dir(str(root / seat))
            if seat_fd is None:
                continue
            try:
                for date in sorted(os.listdir(seat_fd), reverse=True):
                    if len(date) != 10 or date[4] != "-" or date < floor:
                        continue
                    date_fd = _open_real_dir(date, dir_fd=seat_fd)
                    if date_fd is None:
                        continue
                    try:
                        total = 0
                        for name in os.listdir(date_fd):
                            st = os.stat(name, dir_fd=date_fd, follow_symlinks=False)
                            if stat.S_ISREG(st.st_mode):
                                total += st.st_size
                        per_day[date] = total
                    except OSError:
                        continue
                    finally:
                        os.close(date_fd)
            except OSError:
                continue
            finally:
                os.close(seat_fd)
    except OSError:
        return {}, note
    return sizes, None


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "K", "M", "G"):
        if size < 1024 or unit == "G":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{n}B"


def render_chronicle_export(cov: list[dict], sizes: dict, note: str | None) -> str:
    """One line per seat: transcript ratio + export bytes per day (newest first)."""
    lines = []
    for r in cov:
        seat = EXPORT_SEAT.get(r["agent"], r["agent"])
        if r.get("ratio") is None and r.get("eligible") is None:
            ratio = f"n/a ({r['detail']})"
        else:
            ratio = (f"ratio {r['ratio']:.2f} ({r.get('with_transcript')}/{r.get('eligible')}, "
                     f"hollow {r.get('hollow')})" if r.get("ratio") is not None
                     else f"ratio n/a (0 eligible, hollow {r.get('hollow')})")
        days = " ".join(f"{d} {_human(b)}" for d, b in sorted(sizes.get(seat, {}).items(), reverse=True))
        flag = "!" if r["status"] != OK else " "
        lines.append(f"[{flag}] chronicle-export {seat} {CHRONICLE_TRANSCRIPT_DAYS}d {ratio} | "
                     f"{days or 'no export files in window'}")
    if note:
        lines.append(f"chronicle-export: {note}")
    return "\n".join(lines)


def render(results: list[dict], failed: list[dict] | None = None) -> str:
    glyph = {OK: "✓", WARN: "!", FAIL: "✗", PAUSED: "⏸", ASLEEP: "z"}
    lines = []
    for r in results:
        lines.append(f"[{glyph[r['status']]}] {r['agent']:6} {r['status']}")
        for c in r["checks"]:
            # T-3210: consume:policy-skip renders even at OK status -- it is
            # the expected-not-cut-over RECORD once a seat's skip streak
            # clears the visibility floor, not merely a pass/fail probe like
            # every other OK check here (which stays suppressed as noise).
            if c["status"] != OK or c["name"] in ("consume:policy-skip", "seat-state"):
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
    # PAUSED (T-2706) is bucketed separately from fails/warns on purpose: a
    # deliberately-paused seat must never trip DEGRADED or the warnings line —
    # it's surfaced for visibility only, appended to whichever summary line
    # actually fires below.
    paused = [r["agent"] for r in results if r["status"] == PAUSED]
    asleep = [r["agent"] for r in results if r["status"] == ASLEEP]
    paused_note = (f" ({', '.join(paused)} paused)" if paused else "") + (
        f" ({', '.join(asleep)} asleep)" if asleep else "")
    lines.append("")
    if fails or failed_units_present:
        bits = []
        if fails:
            bits.append(f"FAIL on {', '.join(fails)}")
        if failed_units_present:
            bits.append(f"failed units in {', '.join(failed_units_present)}")
        lines.append("FLEET: DEGRADED — " + "; ".join(bits) + paused_note)
    elif warns:
        # T-3210: WARN no longer always means "unqueryable" -- it can also
        # mean an unexpected/mismatched policy-skip row. The per-agent detail
        # lines above already carry the specific reason for each WARN, so
        # the summary line stops asserting one.
        lines.append(f"FLEET: healthy with warnings ({', '.join(warns)})" + paused_note)
    else:
        lines.append("FLEET: all healthy ✓" + paused_note)
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only fleet-health sweep (T-1049).")
    ap.add_argument("--agent", choices=sorted(FLEET), default=None,
                    help="check one agent (default: all)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-failed-sweep", action="store_true",
                    help="skip the blanket `systemctl --failed` sweep (T-1053 detection habit)")
    ap.add_argument("--chronicle", action="store_true",
                    help="T-5016: also check every seat's Chronicle archive after its last restart")
    args = ap.parse_args(argv)

    agents = [args.agent] if args.agent else list(FLEET)
    results = sweep(agents)
    failed = None if args.no_failed_sweep else failed_units_sweep(agents)
    chronicle_records = chronicle_coverage_sweep(agents) if args.chronicle else None
    transcript_records = export_bytes = export_note = None
    if args.chronicle:  # T-6118 PR-4b: soft extras; they never touch the exit code
        transcript_records = transcript_coverage_sweep(agents, run=_run)
        export_bytes, export_note = export_size_sweep(agents)
    if args.json:
        payload = {"agents": results, "failed_units": failed}
        if chronicle_records is not None:
            payload["chronicle"] = chronicle_records
            payload["transcript_coverage"] = transcript_records
            payload["export_bytes"] = export_bytes
            if export_note:
                payload["export_note"] = export_note
        print(json.dumps(payload, indent=2))
    else:
        print(render(results, failed))
        if chronicle_records is not None:
            print(render_chronicle(chronicle_records))
            print(render_chronicle_export(transcript_records, export_bytes, export_note))
    agent_fail = any(r["status"] == FAIL for r in results)
    units_fail = bool(failed) and any(fr["status"] == FAIL for fr in failed)
    chronicle_fail = bool(chronicle_records) and any(r["status"] == FAIL for r in chronicle_records)
    return 1 if (agent_fail or units_fail or chronicle_fail) else 0


if __name__ == "__main__":
    raise SystemExit(main())
