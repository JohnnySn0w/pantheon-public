#!/usr/bin/env python3
"""tools/activity_monitor_drift_check.py — activity_monitor meta-drift alarm
(T-3072, the operator TG 8225).

`tools/activity_monitor.py` now appends one JSONL line per landed poke or
escalation to a Recovery-local fire-log (`_record_fire`, same module) —
`~/.local/state/pantheon/activity_monitor/fires.jsonl`. That answers "did a
predicate fire recently?" but nothing previously asked the complementary
question: **has a predicate gone silently stale** — the T-3070 class problem
(a language-regex detector can quietly stop matching after a model-language
drift and nothing notices, because a detector that never fires looks
identical to a detector with nothing left to detect).

This tool reads that fire-log, computes the gap since each (agent,
predicate) pair's last fire (or since this tool's own bootstrap, for a pair
that has never fired), and alerts when the gap exceeds a per-predicate
threshold. Predicates without a configured threshold are UNARMED — read and
reported, never alerted — per the ticket's "others (p1c/p1d/tui_wedge/
escalations) unarmed until baseline observed" ruling.

Non-goals (ticket-scoped): NOT self-heal (the alarm is signal only,
remediation is human/HO discipline); NOT retroactive (a never-fired pair's
gap floors at the pair's first-observation ts — the global bootstrap for a
pair present at install, its own first-seen ts for one added later, T-3570 —
not at some inferred pre-history); NOT complex per-persona thresholds (flat
per-predicate env vars only).

T-3570 (two false-alarm fixes for the T-3555 artisan/mason sub-orch coverage):
  RC1 — PER-PAIR first-seen floor (above): a pair added after install no
  longer inherits the whole since-global-bootstrap gap on day one.
  RC2 — a "can this predicate CURRENTLY fire for this agent?" precondition
  (`_predicate_can_currently_fire`): a NEVER-fired predicate that provably
  cannot fire right now (p1b for an agent whose actionable queue is
  confirmed-empty — a sub-orch idling between HO dispatches) is healthy-quiet,
  not silently stale, so it is suppressed. This is a per-PREDICATE precondition
  (it automates the manual "verify it's matching, not just quiet" step), NOT a
  per-persona threshold — a busy orchestrator keeps queue>=1 and still alarms on a
  genuine p1b stale, and a probe that cannot determine the queue never
  suppresses (unknown must not stand in for a confirmed 'no').

Usage:
    python3 tools/activity_monitor_drift_check.py --check     # timer path
    python3 tools/activity_monitor_drift_check.py --dry-run   # classify + print, no alert

Environment overrides:
    PANTHEON_STATE_DIR                                     — state dir root (default ~/.local/state/pantheon).
    ACTIVITY_MONITOR_DRIFT_THRESHOLD_P1A_DAYS              — default 3.
    ACTIVITY_MONITOR_DRIFT_THRESHOLD_P1B_DAYS              — default 7 (ticket-specified).
    ACTIVITY_MONITOR_DRIFT_THRESHOLD_P1C_DAYS              — unset by default (unarmed).
    ACTIVITY_MONITOR_DRIFT_THRESHOLD_P1D_DAYS              — unset by default (unarmed).
    ACTIVITY_MONITOR_DRIFT_THRESHOLD_CAP_MODAL_DAYS        — unset by default (unarmed).
    ACTIVITY_MONITOR_DRIFT_THRESHOLD_CAP_MODAL_UNSAFE_DAYS — unset by default (unarmed).
    ACTIVITY_MONITOR_DRIFT_THRESHOLD_SPEND_CAP_DAYS        — unset by default (unarmed).
    ACTIVITY_MONITOR_DRIFT_THRESHOLD_CYBER_REFUSAL_DAYS    — unset by default (unarmed).
    ACTIVITY_MONITOR_DRIFT_THRESHOLD_STUCK_POKE_DAYS       — unset by default (unarmed).
    ACTIVITY_MONITOR_DRIFT_THRESHOLD_TUI_WEDGE_DAYS        — unset by default (unarmed).
    ACTIVITY_MONITOR_DRIFT_ALERT_COOLDOWN_SECONDS          — default 24h; per-(agent,predicate) re-alert cooldown.
    PANTHEON_ORCHESTRATOR_INBOX                                   — default <live hub>/orchestrator (/home/post-office/mailbox/orchestrator).

Default-safe (T-0261): refuses a flagless run (exit 64).
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

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.activity_monitor import (  # noqa: E402
    AGENT_CONFIG,               # agents to sweep
    _scan_actionable_tickets,   # T-3570 RC2: can-it-currently-fire probe (int | None)
)
from tools.agent_role_map import mailbox_hub  # noqa: E402  (T-4096: hub from instance.yaml)
from tools import seat_state  # noqa: E402  (ORG-106 L4c)

STATE_DIR = Path(os.environ.get("PANTHEON_STATE_DIR") or (Path.home() / ".local" / "state" / "pantheon"))
FIRES_PATH = STATE_DIR / "activity_monitor" / "fires.jsonl"
BOOTSTRAP_PATH = STATE_DIR / "activity_monitor" / "drift_check_bootstrap.json"
# T-3570 RC1: per-(agent,predicate) first-observation floor. A pair ADDED to
# AGENT_CONFIG after the drift-check's original install (artisan/mason, T-3555)
# is first seen here and graced from THAT ts — not the stale global bootstrap
# floor, which day-one false-alarmed a newly-added never-fired pair.
FIRST_SEEN_PATH = STATE_DIR / "activity_monitor" / "drift_check_first_seen.json"
ALERT_STATE_PATH = STATE_DIR / "activity_monitor" / "drift_alert_state.json"

ORCHESTRATOR_INBOX = os.environ.get("PANTHEON_ORCHESTRATOR_INBOX") or f"{mailbox_hub()}/orchestrator"

# Every fire-log key `_record_fire` can produce today (tools/activity_monitor.py):
# the poke predicates that route through `_dispatch_poke` + the direct P1.d
# branch, and the escalation vocabs `_escalate_stuck` records under (keyed on
# `vocab`, not the triggering predicate — see that function's docstring).
KNOWN_PREDICATES = (
    "p1a", "p1b", "p1c", "p1d",
    "cap-modal", "cap-modal-unsafe", "spend-cap", "cyber-refusal",
    "stuck-poke", "tui-wedge",
    "interactive-await",  # T-5793: P1.b blocked on AskUserQuestion/EnterPlanMode -> Recovery
)

# Ticket-ratified defaults; every other KNOWN_PREDICATE stays unarmed (None)
# until a baseline observation period justifies a threshold (HO ruling,
# T-3072 ticket annotation, "others unarmed until baseline observed").
#
# p1a UNARMED (T-3299): this drift-check catches a LANGUAGE-regex detector that
# silently stops matching (the T-3070 class). p1a's idle signal is MTIME-based
# (latest_jsonl_mtime / latest_commit_ts), so it cannot language-drift; and it
# fires only on idle>1hr-with-actionable-work, which a continuously-busy fleet
# never trips — so "p1a never fired in Nd" is a HEALTH signal, not staleness, and
# arming it false-alarms indefinitely on a busy fleet. Left read+reported (still
# in KNOWN_PREDICATES) but never alerted, like p1c/p1d/tui_wedge. A positive
# matcher-health probe (assert p1a's signal source still reads the agent's
# jsonl-mtime + queue) would let it be re-armed meaningfully — the "(b)" option
# on T-3299, deferred as a broader drift-check enhancement.
DEFAULT_THRESHOLD_DAYS: dict[str, int] = {
    "p1b": 7,
}

ALERT_COOLDOWN_SECONDS = int(
    os.environ.get("ACTIVITY_MONITOR_DRIFT_ALERT_COOLDOWN_SECONDS", str(24 * 60 * 60))
)


def _log(msg: str) -> None:
    print(f"[activity_monitor_drift_check ts={int(time.time())}] {msg}", flush=True)


def _threshold_days_for(predicate: str) -> int | None:
    """Per-predicate threshold in days, or None (unarmed). Env override wins;
    unset falls back to DEFAULT_THRESHOLD_DAYS (which has no entry — i.e.
    None — for anything not explicitly ticket-ratified)."""
    env_key = "ACTIVITY_MONITOR_DRIFT_THRESHOLD_" + predicate.upper().replace("-", "_") + "_DAYS"
    override = os.environ.get(env_key)
    if override is not None:
        try:
            return int(override)
        except ValueError:
            return None
    return DEFAULT_THRESHOLD_DAYS.get(predicate)


def read_fires(path: Path) -> list[dict]:
    """Read every well-formed fire record from the JSONL. Malformed lines are
    skipped, never raised — a single corrupt line must not blind this tool to
    every genuine record around it. Missing file -> []."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def last_fire_by_key(fires: list[dict]) -> dict[tuple[str, str], int]:
    """Group fire records by (agent, predicate) -> max ts. Records missing
    agent/predicate/ts, or carrying a non-numeric ts, are skipped."""
    out: dict[tuple[str, str], int] = {}
    for rec in fires:
        agent = rec.get("agent")
        predicate = rec.get("predicate")
        ts = rec.get("ts")
        if not isinstance(agent, str) or not isinstance(predicate, str):
            continue
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        key = (agent, predicate)
        ts_int = int(ts)
        if key not in out or ts_int > out[key]:
            out[key] = ts_int
    return out


def _load_bootstrap_ts(now: float) -> int:
    """Return this tool's bootstrap timestamp — the floor used as "last fire"
    for a (agent, predicate) pair with zero fire history, so a fresh install
    doesn't read as an infinite gap on day one (T-3072 first-install grace).
    Written once, on first run; stable thereafter (best-effort: if the write
    fails, the next run just re-bootstraps rather than raising)."""
    try:
        data = json.loads(BOOTSTRAP_PATH.read_text(encoding="utf-8"))
        ts = data.get("bootstrap_ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            return int(ts)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    ts = int(now)
    try:
        BOOTSTRAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        BOOTSTRAP_PATH.write_text(json.dumps({"bootstrap_ts": ts}), encoding="utf-8")
    except OSError as exc:
        _log(f"could not persist bootstrap ts (non-fatal, will re-bootstrap next run): {exc}")
    return ts


def _load_first_seen() -> dict[str, int]:
    """Per-(agent,predicate) first-observation floors, keyed "agent:predicate".
    Missing/corrupt file -> {} (the seed step below re-populates)."""
    try:
        data = json.loads(FIRST_SEEN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, int] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = int(v)
    return out


def _save_first_seen(store: dict[str, int]) -> None:
    try:
        FIRST_SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIRST_SEEN_PATH.write_text(json.dumps(store), encoding="utf-8")
    except OSError as exc:
        _log(f"could not persist first-seen store (non-fatal): {exc}")


def _seed_first_seen(store: dict[str, int], now: float, agents: list[str]) -> bool:
    """Record `now` as the first-observation floor for every currently-known
    ARMED (agent, predicate) pair not already in `store`. Mutates `store`;
    returns True if anything was added (so the caller persists).

    A pair is seeded the first run at which it is BOTH known (in AGENT_CONFIG)
    AND armed — so a pair added later (artisan/mason, T-3555) gets a fresh grace
    window from its own first observation, not the stale global bootstrap.
    One-time upgrade note: on the first run of this code, every current pair is
    seeded to `now`; this only affects NEVER-fired pairs' floors (a pair that
    has fired uses its real last-fire ts), and the only such armed pair today
    (mason p1b) is benign — already RC2-suppressed while its queue is empty."""
    changed = False
    for agent in agents:
        for predicate in KNOWN_PREDICATES:
            if _threshold_days_for(predicate) is None:
                continue
            key = f"{agent}:{predicate}"
            if key not in store:
                store[key] = int(now)
                changed = True
    return changed


def _predicate_can_currently_fire(agent: str, predicate: str) -> bool | None:
    """Point-in-time precondition probe (T-3570 RC2): CAN `predicate` fire for
    `agent` right now, independent of detector health? Automates the manual
    "verify it's matching, not just quiet" step for the never-fired case.

    Returns True (can fire now), False (provably cannot fire now), or None
    (cannot determine — treat as UNKNOWN and never suppress: a failed probe
    must not stand in for a confirmed 'no', which would mask a genuine stale).

    Only p1b has a modeled precondition today: it gates on
    count_actionable_tickets >= 1 (activity_monitor.check_p1b), so an agent
    whose actionable queue is CONFIRMED-empty structurally cannot fire p1b —
    its silence is a health signal, not staleness (the same reasoning that
    unarms p1a globally). A sub-orch (artisan/mason) idles at 0 active tickets
    between HO dispatches, so this is where its never-fired p1b reads as
    benign-quiet — with NO per-persona hardcode (a busy orchestrator keeps queue>=1
    and still alarms on a genuine p1b stale). Every other predicate returns
    None (no modeled precondition -> never suppress)."""
    if predicate != "p1b":
        return None
    cfg = AGENT_CONFIG.get(agent)
    if not cfg:
        return None
    n = _scan_actionable_tickets(
        cfg["pantheon_config_path"],
        cfg["ticket_targets"],
        cfg["sudo_user"],
        queue_states=cfg.get("queue_states", ("inbox", "active")),
    )
    if n is None:
        return None  # probe failed -> unknown; do NOT suppress
    return n >= 1


# T-5439: a persona in seat-maintenance, or with EVERY configured runtime held
# in its holds.json, has no live pane by design, so p1b (and any pane-reading
# predicate) cannot match. Its silence is an authorised no, not a stale
# detector; mason (held since 10-19, T-5222) paged p1b daily until this. Same
# shape as the parity_remediate #1064 and T-5282 consume guards. Both probes
# fail toward ALERTING: a fault or an unreadable holds file never quiets a seat.
REPO_ROOT = Path(__file__).resolve().parent.parent
HOLDS_READ_TIMEOUT_SECONDS = 10


def _seat_in_maintenance(agent: str) -> bool:
    """notify_policy.in_maintenance, fail-open (any raise reads False). ORG-106 L4c: a
    shim over the shared seat_state.maintenance_active, keeping this site's log line."""
    return seat_state.maintenance_active(
        agent, on_fault=lambda exc: _log(f"seat-maintenance check failed open for {agent}: {exc!r}"))


def _configured_runtimes(agent: str) -> tuple[str, ...]:
    """The persona's primary + fallback runtime refs from instance.yaml, or ()
    if they cannot be resolved (which never counts as all-held)."""
    try:
        from tools.persona_bringup import configured_runtime_targets
        return tuple(configured_runtime_targets(REPO_ROOT / "instance.yaml", agent))
    except Exception as exc:  # noqa: BLE001 -- unknown runtimes: keep alerting
        _log(f"could not resolve configured runtimes for {agent}: {exc!r}")
        return ()


def _read_persona_holds(agent: str) -> dict | None:
    """The `holds` mapping from the persona's controller holds.json, read as
    its own account (Recovery cannot traverse the 750 home). {} when the file is
    absent (no holds); None when unknown (sudo/timeout fault, malformed file,
    unknown agent). The controller fails a malformed file CLOSED for launches;
    here it must not quiet a possibly-dead seat, so it reads as unknown."""
    cfg = AGENT_CONFIG.get(agent)
    if not cfg:
        return None
    home = Path(cfg["pantheon_config_path"]).parent
    path = home / ".local" / "state" / "pantheon" / "personas" / agent / "overrides" / "holds.json"
    from tools.seat_exec import seat_argv
    # rc 3 marks "absent" so it is distinguishable from a sudo/cat failure.
    inner = f'if [ -e "{path}" ]; then cat "{path}"; else exit 3; fi'
    try:
        r = subprocess.run(
            seat_argv(cfg["sudo_user"], ["bash", "-c", inner]),
            capture_output=True, text=True, timeout=HOLDS_READ_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"holds read failed for {agent}: {exc!r}")
        return None
    if r.returncode == 3:
        return {}
    if r.returncode != 0:
        return None
    try:
        holds = json.loads(r.stdout).get("holds")
    except (json.JSONDecodeError, AttributeError):
        return None
    return dict(holds) if isinstance(holds, dict) else None


def _persona_quiesced(agent: str) -> str | None:
    """Why `agent` is deliberately quiet ("seat-maintenance" / "all runtimes
    held"), or None if it is not (or that cannot be confirmed)."""
    if _seat_in_maintenance(agent):
        return "seat-maintenance"
    # ORG-106 L4c: a declared deep-sleep or pause is authorised quiet too. L2 stops the
    # activity monitor's pokes for such a seat, so "no fires" is the expected state. Keyed
    # on the seat-state record only (an OOM hold alone is not a declaration; in the live
    # shape it re-arms maintenance, which the line above already covers).
    seat = seat_state.safe_effective_for(agent)
    if seat is not None and seat.state == seat_state.STATE_DEEP_SLEEP:
        return "seat-sleep"
    if seat is not None and seat.state == seat_state.STATE_PAUSED_INTAKE:
        return "seat-paused"
    runtimes = _configured_runtimes(agent)
    if runtimes:
        holds = _read_persona_holds(agent)
        if holds is not None and all(r in holds for r in runtimes):
            return "all runtimes held"
    return None


def compute_drift(
    fires: list[dict],
    bootstrap_ts: int,
    now: int,
    agents: list[str] | None = None,
    first_seen: dict[str, int] | None = None,
) -> list[dict]:
    """For every (agent, predicate) in `agents` (default: AGENT_CONFIG's
    keys) x KNOWN_PREDICATES with an ARMED threshold, compute the gap since
    last fire and return the ones whose gap has crossed the threshold. Unarmed
    predicates (no configured threshold) are never included, regardless of gap.

    For a NEVER-fired pair the gap floors at the pair's first-observation ts:
    `first_seen["agent:predicate"]` when a `first_seen` store is supplied
    (T-3570 RC1 — a pair added after install is graced from its own first
    observation), else `bootstrap_ts` (legacy single-floor behavior, kept so
    existing callers/tests are unchanged).

    Returns a list of dicts: {agent, predicate, gap_days, threshold_days,
    last_fire_ts} — last_fire_ts is None for a pair that has never fired
    (the gap was computed against the first-observation floor instead).
    """
    agent_list = sorted(AGENT_CONFIG.keys()) if agents is None else agents
    last_fire = last_fire_by_key(fires)
    drifted: list[dict] = []
    for agent in agent_list:
        for predicate in KNOWN_PREDICATES:
            threshold_days = _threshold_days_for(predicate)
            if threshold_days is None:
                continue
            last_ts = last_fire.get((agent, predicate))
            if last_ts is not None:
                effective_last_ts = last_ts
            elif first_seen is not None:
                effective_last_ts = first_seen.get(f"{agent}:{predicate}", bootstrap_ts)
            else:
                effective_last_ts = bootstrap_ts
            gap_days = (now - effective_last_ts) / 86400.0
            if gap_days >= threshold_days:
                drifted.append({
                    "agent": agent,
                    "predicate": predicate,
                    "gap_days": gap_days,
                    "threshold_days": threshold_days,
                    "last_fire_ts": last_ts,
                })
    return drifted


def _load_alert_state() -> dict:
    try:
        return json.loads(ALERT_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_alert_state(state: dict) -> None:
    try:
        ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        ALERT_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError as exc:
        _log(f"could not persist alert state (non-fatal): {exc}")


def _should_alert(agent: str, predicate: str, now: float, state: dict) -> bool:
    """24h (default) per-(agent,predicate) dedup cooldown (T-3072) — prevents
    daily paging while a single drift persists unresolved."""
    key = f"{agent}:{predicate}"
    last = state.get(key) or {}
    last_ts = last.get("ts")
    if not isinstance(last_ts, (int, float)):
        return True
    return (now - last_ts) >= ALERT_COOLDOWN_SECONDS


def _record_alert(state: dict, agent: str, predicate: str, now: float) -> None:
    state[f"{agent}:{predicate}"] = {"ts": now}


def _route_failure_alert(agent: str, msg: str, vocab: str) -> None:
    """Import-wrapped so tests patch it (mirrors activity_monitor.py's own
    `_route_failure_alert` seam)."""
    try:
        from tools.inbox_consumer import route_failure_alert
    except ImportError:
        from inbox_consumer import route_failure_alert
    route_failure_alert(agent, msg, vocab)


def _alert(
    agent: str, predicate: str, gap_days: float, threshold_days: int, last_fire_ts: int | None
) -> None:
    """Dispatch the drift alert via BOTH channels the ticket names:

    - `route_failure_alert(agent=<drifted persona>, ...)` — NOT agent="recovery".
      Passing "recovery" would trip the SELF_TRIAGE_AGENT carve-out and bypass
      Recovery's pane entirely (then a direct-operator alert; since T-4399, orchestrator's
      mailbox with the operator as fallback); the
      ticket's routing ruling #1 ("Recovery-triage only, no additional direct-
      Telegram") wants the `agent != "recovery"` path (`_send_alert_via_recovery` —
      Recovery's pane + a throttled `[recovery routed]` audit-Telegram), which is
      also what every other Recovery-fixable-condition monitor in this tree does
      (`consumer_liveness_monitor`, the memory-snapshot-coverage monitor's
      documented "Recovery-triage, not the recovery-self carve-out" choice) — a
      stale detector is exactly as Recovery-fixable as those.
    - `mailbox.send` to orchestrator's durable inbox, so the drift is visible on
      Orchestrator's next mailbox-wake even without a Telegram touch.

    Each channel is independently try/except-wrapped — one channel failing
    must not suppress the other, and neither failure may crash the check.
    """
    last_fire_str = (
        datetime.fromtimestamp(last_fire_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if last_fire_ts is not None else "never (since drift-check install)"
    )
    msg = (
        f"⏰ [detector-drift] activity_monitor predicate '{predicate}' has not "
        f"fired for {agent} in {gap_days:.1f}d (threshold {threshold_days}d). "
        f"Last fire: {last_fire_str}. The detector may have gone silently stale "
        f"(T-3070 class problem) — verify it's still matching, not just quiet."
    )
    vocab = f"detector-drift-{agent}-{predicate}"

    try:
        _route_failure_alert(agent, msg, vocab)
    except Exception as exc:  # noqa: BLE001 — alert path must never crash the check
        _log(f"route_failure_alert failed (non-fatal): {exc!r}")

    try:
        from tools import mailbox
        mailbox.send(
            ORCHESTRATOR_INBOX, msg, "activity_monitor_drift_check",
            subject=f"activity_monitor drift: {predicate} not fired in {int(gap_days)}d",
        )
    except Exception as exc:  # noqa: BLE001 — alert path must never crash the check
        _log(f"mailbox.send failed (non-fatal): {exc!r}")


def check(*, dry_run: bool, now: float | None = None) -> int:
    """Read the fire-log, compute drift, alert (throttled) on any ARMED
    predicate whose gap has crossed threshold. Returns the count of drifted
    (agent, predicate) pairs (0 = none)."""
    now = time.time() if now is None else now
    bootstrap_ts = _load_bootstrap_ts(now)
    first_seen = _load_first_seen()
    agent_list = sorted(AGENT_CONFIG.keys())
    if _seed_first_seen(first_seen, now, agent_list):
        _save_first_seen(first_seen)
    fires = read_fires(FIRES_PATH)
    drifted = compute_drift(fires, bootstrap_ts, int(now), first_seen=first_seen)

    # T-3570 RC2: drop never-fired pairs that provably cannot CURRENTLY fire —
    # healthy-quiet, not silently stale. Restricted to never-fired pairs: a pair
    # that fired then went silent is the genuine T-3070 signal and always alarms
    # (subject to cooldown), regardless of the current queue snapshot. An
    # unknown probe (None) never suppresses — only a confirmed cannot-fire does.
    kept: list[dict] = []
    for d in drifted:
        if d["last_fire_ts"] is None and \
                _predicate_can_currently_fire(d["agent"], d["predicate"]) is False:
            _log(f"suppressed (healthy-quiet: {d['predicate']} cannot currently fire "
                 f"for {d['agent']}, gap={d['gap_days']:.1f}d)")
            continue
        kept.append(d)
    drifted = kept

    # T-5439: drop every pair for a persona that is deliberately quiet (seat
    # maintenance or fully held) -- fired-then-silent included, since a held
    # seat's pane is gone. Logged, never alerted.
    quiesced: dict[str, str | None] = {}
    kept = []
    for d in drifted:
        agent = d["agent"]
        if agent not in quiesced:
            quiesced[agent] = _persona_quiesced(agent)
        if quiesced[agent]:
            _log(f"suppressed (authorised quiet: {agent} {quiesced[agent]}; "
                 f"{d['predicate']} gap={d['gap_days']:.1f}d)")
            continue
        kept.append(d)
    drifted = kept

    if not drifted:
        _log(f"OK: no drift (fires={len(fires)}, bootstrap_ts={bootstrap_ts})")
        return 0

    for d in drifted:
        _log(f"DRIFT: agent={d['agent']} predicate={d['predicate']} "
             f"gap={d['gap_days']:.1f}d threshold={d['threshold_days']}d")

    if dry_run:
        _log(f"[dry-run] would evaluate {len(drifted)} drifted pair(s) for alert")
        return len(drifted)

    state = _load_alert_state()
    for d in drifted:
        if _should_alert(d["agent"], d["predicate"], now, state):
            _alert(d["agent"], d["predicate"], d["gap_days"], d["threshold_days"], d["last_fire_ts"])
            _record_alert(state, d["agent"], d["predicate"], now)
            _log(f"alerted: {d['agent']}/{d['predicate']}")
        else:
            _log(f"alert suppressed (cooldown): {d['agent']}/{d['predicate']}")
    _save_alert_state(state)
    return len(drifted)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--check", action="store_true", help="check drift + alert (timer path)")
    p.add_argument("--dry-run", action="store_true", help="classify + print, send no alert")
    args = p.parse_args(argv)

    if not (args.check or args.dry_run):
        print(
            "ERROR: refusing to run with no mode flag. Pass --check (timer path) "
            "or --dry-run to preview without alerting.",
            file=sys.stderr,
        )
        return 64

    check(dry_run=args.dry_run)
    # Exit 0 always: a monitor must not fail its own timer (mirrors
    # priority_list_health_watchdog's convention). A detected-and-handled
    # drift is a successful run, not a crash — and this keeps the systemd
    # OnFailure= guard reserved for genuine tool crashes: if this returned
    # non-zero on every persisting drift, OnFailure would page Recovery a SECOND
    # time on top of this tool's own dedup'd alert, for as long as the drift
    # lasted (a composition risk between two individually-sound choices,
    # caught at plan review — T-2896-shaped).
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
