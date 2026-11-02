#!/usr/bin/env python3
"""activity_monitor — fire structural poke to an agent's pane when one of:

  P1.a — idle >1hr with ≥1 actionable HO-targeted queue item.
  P1.b — the operator absent >15min + agent in self-imposed await state + queue.
  P1.d — agent's pane shows a rate-limit API-error as latest visible state +
         jsonl confirms the rate-limit assistant turn is ≥5min old + no
         further activity since (turn was silently abandoned by the runtime).

T-4510 fleet usage gate: at >=95% (configurable) of the fleet's SHARED weekly Max, the
periodic anti-idle/queue content pokes (P1.a, P1.b) suppress themselves and resume at the
reset (tools/fleet_usage_gate.py; fail-open; logged once per window). The gate sits at the
send point inside _dispatch_poke, AFTER every recovery branch. P1.d, P1.c, the cap-modal /
cap / cyber / effectiveness handling and the TUI-wedge escalation are recovery and never
consult it.

Per ORG-011 Phase B / T-0223 (P1.a + P1.b) and T-0254 (P1.d). Mirrors
`tools/context_fill_monitor.py` shape (per-agent AGENT_CONFIG + systemd
template @<agent>.{service,timer}). Designed to run as a systemd-user timer
on the `claude-fallback` (Recovery) account; reads state across accounts via
the same NOPASSWD-sudo posture context_fill_monitor uses.

Cadence: 15 min (timer). Per-predicate cooldown — P1.a/P1.b: 30 min; P1.d:
15 min (rate-limit transients should clear faster, and if a retry-inject
didn't help we want to detect and re-act on the next cycle). All
env-overridable.

Bail-and-poll: each cycle exits quickly. No long sleep inside the process.

Environment overrides:
  PANTHEON_STATE_DIR                    — override state dir.
  ACTIVITY_MONITOR_COOLDOWN_SECONDS     — override P1.a/P1.b cooldown.
  ACTIVITY_MONITOR_P1D_COOLDOWN_SECONDS — override P1.d cooldown.
  ACTIVITY_MONITOR_P1A_IDLE_SECONDS     — override P1.a idle threshold.
  ACTIVITY_MONITOR_P1B_OPERATOR_SECONDS  — override P1.b operator-absent threshold.
  ACTIVITY_MONITOR_P1B_ENABLED          — P1.b kill-switch (default 1; 0 skips P1.b only).
  ACTIVITY_MONITOR_P1D_AGE_SECONDS      — override P1.d rate-limit-age threshold.
  ACTIVITY_MONITOR_BRIDGE_HEALTH_URL    — override bridge /health URL.
  ACTIVITY_MONITOR_P1B_BACKOFF_ENABLED  — T-3991 handled-wait backoff (default on; kill-switch).
  ACTIVITY_MONITOR_P1B_BACKOFF_COOLDOWN_SECONDS   — stretched p1b cooldown when backoff engages.
  ACTIVITY_MONITOR_P1B_BACKOFF_AFTER_FIRES        — consecutive p1b fires before backoff engages.
  ACTIVITY_MONITOR_P1B_BACKOFF_STREAK_GAP_SECONDS — max gap counting fires as one wait-streak.

Exit codes:
  0 — clean bail (in cooldown, no agent state available, predicates didn't fire)
      OR a poke was sent successfully.
  1 — poke was attempted but tmux send-keys failed.
  2 — argv error.
"""

from __future__ import annotations

import argparse
import json
import os
import hashlib
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Make `from tools.X import Y` work whether invoked as `python3 tools/foo.py`
# or `python3 -m tools.foo`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import agent_role_map  # noqa: E402  (after the sys.path shim above)
from tools import telegram_inbound  # noqa: E402  (T-4212: shared inbound parser)
from tools import fleet_usage_gate  # noqa: E402  (T-4510: fleet usage gate, stdlib-only)
from tools import harness_idle  # noqa: E402  (T-5793: harness resolver + reader seam)
from tools import health_export  # noqa: E402  (T-5793: SudoReader, argv-only cross-seat reads)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"

# P1.a — idle threshold. Per ORG-011 ratified spec.
P1A_IDLE_THRESHOLD_SECONDS = 3600  # 1 hour

# P1.b — operator-absent threshold. Per ORG-011 ratified spec.
P1B_OPERATOR_ABSENT_THRESHOLD_SECONDS = 15 * 60

# T-3709 — P1.b write-activity suppression window. If the agent has committed
# to its own pantheon-config within this window it is demonstrably producing
# git-visible output (ledger/ticket/notes/bugs/tools/adr/... — ANY path), so it
# is a busy-productive session, not an idle-waiting one. The operator-inbound
# gate alone cannot tell these apart (the operator being asleep says nothing about
# whether the HO is working). 15min matches the implementer-inbox-threshold
# (impl15, dispatch-and-wait-liveness) and the monitor's own 15min cadence.
# Override via ACTIVITY_MONITOR_P1B_WRITE_ACTIVITY_SECONDS.
ACTIVITY_MONITOR_P1B_WRITE_ACTIVITY_SECONDS = 15 * 60  # 15 min

# Per-poke cooldown to prevent spam if a poke doesn't yield activity quickly.
ACTIVITY_MONITOR_COOLDOWN_SECONDS = 30 * 60  # 30 min — P1.a/P1.b

# T-0644: when HO logged an explicit queue terminal-state audit recently,
# stretch P1.a's idle threshold to this longer window so we don't re-pay
# terminal-state ack tokens every cycle. The default reflects "wait 4hr
# before another idle-poke even if the queue still shows >=1 unblocked
# item, because HO already wrote down that the queue is in terminal state."
ACTIVITY_MONITOR_P1A_TERMINAL_STATE_IDLE_SECONDS = 4 * 60 * 60  # 4hr

# P1.d cooldown — shorter than P1.a/P1.b. Rate-limit retries should be re-eval'd
# on the next cycle if the first inject didn't restore the turn.
P1D_COOLDOWN_SECONDS = 15 * 60  # 15 min

# P1.d — rate-limit assistant turn must be ≥ this old before retry-inject fires.
P1D_RATE_LIMIT_AGE_SECONDS = 5 * 60  # 5 min

# P1.c-narrow (ORG-037 Layer 3) — active-team + dispatcher-turn-firing-stopped.
# The narrow "forgot-to-arm-Layer-1" residual that msg_1326's revert left
# uncovered (proven by the 2026-06-25 orchestrator-stall incident). Ship DARK:
# only evaluated when PANTHEON_LAYER3_ENABLED is truthy, supervised first-fire
# per agent (orchestrator first). See notes/2026-06-26-layer3-recovery-backstop-design.md.
P1C_STALE_THRESHOLD_SECONDS = 30 * 60  # 30 min — Layer-1 cadence ceiling (1200s) + 10min margin; aligns ORG-037 orchestrator role-threshold
P1C_COOLDOWN_SECONDS = 30 * 60  # 30 min — matches P1.a/P1.b

# P1.b handled-wait backoff (T-3991, interim before the T-3076 P1.b reinvent).
# Field data (T-3958, two session-instances) showed P1.b re-firing every ~30min
# through a genuinely-drained operator-quiet hold — 10+ fires in one ~5h stretch —
# even after the operator had ALREADY done the SOP-prescribed action (converted the
# operator-blocked asks into target=operator tickets). Fires 1-2 carry the signal
# (they legitimately surfaced the wait); fires 3+ are pure churn. The existing
# T-3404 terminal-state-ledger-ack suppression does not recognize a filed
# target=operator ticket as "handled", so it never engaged in those instances.
# Interim heuristic: once N consecutive p1b fires have landed in one wait-streak
# AND a target=operator ticket exists that was created since the streak began (the
# artifact SOP step 3 prescribes = evidence the wait is already handled), stretch
# the p1b cooldown so the churn fires back off. Doubly-gated + benign ceiling: the
# worst case is a still-poked-but-slower cadence (p1a idle-net + the stretched p1b
# both remain), never a silenced safety net — so this ships default-ON with an
# env kill-switch rather than dark. It only ever fires the monitor LESS.
P1B_BACKOFF_COOLDOWN_SECONDS = 60 * 60  # 60 min — the T-3991 proposed downshift
P1B_BACKOFF_AFTER_FIRES = 2            # backoff engages once >= this many p1b fires are already on the streak (so fire 3+ is rate-limited)
P1B_BACKOFF_STREAK_GAP_SECONDS = 90 * 60  # fires within this gap count as one wait-streak; > the 60min backoff cadence so the streak survives its own downshift

# Bounded-poke (T-1363) — stop re-poking a session that isn't responding.
# A poke that LANDS makes the session take a turn → its main jsonl mtime
# advances. A poke that does NOT advance the jsonl was ineffective → the session
# is stuck (capped / cyber-blocked / wedged), not idle, so re-poking just burns
# tokens. After MAX_INEFFECTIVE_POKES consecutive ineffective pokes, SUPPRESS +
# escalate instead of re-poking. The poke-once-vs-skip design fork (T-1363) is
# exactly this parameter: N=1 = poke-once-then-escalate; N=0 = skip pokes
# entirely (straight to escalate). Ship-dark: bounded behavior is active only
# when ACTIVITY_MONITOR_BOUNDED_POKE_ENABLED is truthy — otherwise the legacy
# unbounded (cooldown-only) behavior is unchanged.
MAX_INEFFECTIVE_POKES_DEFAULT = 1

# T-5793: P1.b's await signal comes from the harness's own session log
# (harness_idle.read_idle over the SudoReader): IDLE with quiet >= this many seconds
# means the turn ended and the seat is waiting on itself. Also the minimum age of an
# interactive-await (AskUserQuestion/EnterPlanMode) before it is escalated.
P1B_LOG_QUIET_SECONDS = 15 * 60
# Rollback lever: "log" (default) or "pane". "pane" restores the pre-T-5793 P1.b path
# verbatim: the Claude-transcript structural signals (T-3071 text-final turn, T-0297
# interactive tool_use, which POKES), no interactive-await escalation, and no positive
# pane gate before a text poke. Read per run, so a unit drop-in flips it.
IDLE_SOURCE_ENV = "ACTIVITY_MONITOR_IDLE_SOURCE"
INTERACTIVE_AWAIT_PREDICATE = "p1b-interactive"
INTERACTIVE_AWAIT_VOCAB = "interactive-await"


def idle_source() -> str:
    return "pane" if os.environ.get(IDLE_SOURCE_ENV, "log").strip().lower() == "pane" else "log"

# Bridge /health endpoint (used for P1.b operator-inbound state).
DEFAULT_BRIDGE_HEALTH_URL = "http://127.0.0.1:8765/health"

# P1.d — rate-limit error pattern. Matches both Claude Code's synthetic
# "API Error: Server is temporarily limiting requests..." and any variant
# that surfaces "rate limit" / "rate limited" in the assistant text.
RATE_LIMIT_REGEX = re.compile(
    r"(API Error.*[Rr]ate [Ll]imit|temporarily limiting requests|[Rr]ate [Ll]imited)",
)

# P1.b extension (T-0297) — tool_use names that block Claude Code's loop on
# user input. When the latest assistant turn is one of these, the agent is
# in await-state regardless of the structural Signal-1 check (T-3071) —
# these tool_use turns typically have NO text content alongside, so their
# final block IS the tool_use itself.
USER_BLOCKING_TOOL_NAMES = frozenset({
    "AskUserQuestion",   # standard interactive menu — exact match Orchestrator used in msg_1181 incident
    "EnterPlanMode",     # plan-mode entry; blocks for user ratification on exit
})

# How many lines of the bottom of pane to inspect for the rate-limit pattern.
P1D_PANE_TAIL_LINES = 60

# Subprocess timeouts.
_GIT_TIMEOUT_SECONDS = 30
_JSONL_PROBE_TIMEOUT_SECONDS = 30
_TMUX_TIMEOUT_SECONDS = 10

# T-0538: paste-mode settle delay — matches inbox_consumer's INJECT_ENTER_DELAY.
# Env-overridable so tests can fast-path without real tmux.
INJECT_ENTER_DELAY = float(os.environ.get("INJECT_ENTER_DELAY", "0.7"))

# Cross-account sudo prefix factory.
def _sudo(user: str) -> list[str]:
    return ["sudo", "-nu", user]


# ---------------------------------------------------------------------------
# Per-agent config
# ---------------------------------------------------------------------------

AGENT_CONFIG: dict[str, dict] = {
    "orchestrator": {
        "tmux_target_pane": "claude-telegram-bot:0",
        "sudo_user": "claude",
        # T-4159: derived, not hardcoded. -home-claude while orchestrator is bespoke
        # (home cwd); -home-claude-pantheon-config once its T-3650 hand-move marks
        # it control-plane. A frozen -home-claude would read a dead dir after the
        # move and false-flag a live orchestrator as idle (P1.a).
        "jsonl_dir": Path(agent_role_map.live_session_projects_dir("claude")),
        "ledger_agent_id": "orchestrator",
        # T-5649: + orchestrator's single-member head role. Since ORG-088 (10-19) tickets
        # target role pools, and the head role has exactly one seat, so its open
        # tickets are orchestrator's queue even before delivered_to is set. The shared
        # role:engineering_orchestrator pool is deliberately NOT listed for any
        # seat: hundreds of open pool tickets would make P1.a/P1.b always see work.
        "ticket_targets": frozenset({"orchestrator", "orchestrator",
                                     "role:head_engineering_orchestrator"}),
        "pantheon_config_path": Path("/home/claude/pantheon-config"),
        "bridge_inbound_key": "orchestrator",
        # P1.c (ORG-037 Layer 3): the session's team root (T-1290). active-team
        # is signalled by `<teams_dir>/*/inboxes` existing. Only agents with a
        # teams_dir configured are eligible for the Layer-3 backstop.
        "teams_dir": Path("/home/claude/.claude/teams"),
    },
    # T-3555 (the operator directive TG 9289, 2026-09-07): extend activity-monitor
    # coverage to the two ORG-043 sub-orchestrator seats so Recovery auto-catches their
    # wedges (recurring Claude Code permission-modal stalls) without HO involvement.
    # NARROW scope by design — the await/cap-modal/rate-limit predicates (P1.a/P1.b/
    # P1.d), NOT the P1.c Layer-3 worker-silence witness apparatus (msg_1326 veto is
    # reversed only for these two, not fleet-wide): no teams_dir, so P1.c bails
    # safely (cfg.get + no active-team inboxes). Substrate = scope-managed
    # (pantheon-runtime-tmux.scope), NOT service-managed — activity_monitor POKES,
    # never restarts, so the scope-vs-service split is moot here; the pane target is
    # the live pre-existing session per instance.yaml. jsonl_dir is the ACTIVE
    # -pantheon-config project (consume-runtime launches cwd=<account>/pantheon-config;
    # the plain -home-<account> dir is month-stale — verified 2026-09-07).
    "artisan": {
        "tmux_target_pane": "suborch2-main:0",
        "sudo_user": "suborch2",
        "jsonl_dir": Path("/home/suborch2/.claude/projects/-home-suborch2-pantheon-config"),
        "ledger_agent_id": "artisan",
        "ticket_targets": frozenset({"artisan"}),
        "pantheon_config_path": Path("/home/suborch2/pantheon-config"),
        "bridge_inbound_key": "artisan",
        # T-3557(e): sub-orch seats count ACTIVE tickets only. An unclaimed
        # inbox ticket is undispatched/pending for a scope-managed seat, not
        # work it could be stuck on — counting it false-fired p1a/p1b on
        # legitimately-idle seats. orchestrator (no key → inbox+active) is unchanged.
        "queue_states": ("active",),
    },
    "mason": {
        "tmux_target_pane": "suborch1-main:0",
        "sudo_user": "suborch1",
        "jsonl_dir": Path("/home/suborch1/.claude/projects/-home-suborch1-pantheon-config"),
        "ledger_agent_id": "mason",
        "ticket_targets": frozenset({"mason"}),
        "pantheon_config_path": Path("/home/suborch1/pantheon-config"),
        "bridge_inbound_key": "mason",
        # T-3557(e): sub-orch seats count ACTIVE tickets only (see artisan note).
        "queue_states": ("active",),
    },
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log(agent: str, msg: str) -> None:
    ts = int(time.time())
    print(f"[activity_monitor agent={agent} ts={ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# State (cooldown)
# ---------------------------------------------------------------------------


def _state_path_for_agent(agent: str) -> Path:
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"activity_monitor_{agent}_last_poke.json"


def in_cooldown(state_path: Path, cooldown_seconds_by_predicate: dict[str, int] | int) -> bool:
    """Check cooldown. Accepts either:
      - int (legacy single cooldown), OR
      - dict mapping predicate-id → cooldown (per-predicate cooldown via the
        predicate-id recorded in state at last fire).

    Per-predicate cooldown allows P1.d's faster re-eval (15min) without spamming
    P1.a/P1.b (30min).
    """
    if not state_path.exists():
        return False
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        last_ts = int(data.get("ts", 0))
        last_pred = data.get("predicate", "p1a")
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return False
    if isinstance(cooldown_seconds_by_predicate, dict):
        cooldown = cooldown_seconds_by_predicate.get(
            last_pred, max(cooldown_seconds_by_predicate.values(), default=0)
        )
    else:
        cooldown = cooldown_seconds_by_predicate
    return (int(time.time()) - last_ts) < cooldown


def write_poke_state(
    state_path: Path,
    agent: str,
    predicate: str,
    *,
    pre_poke_jsonl_mtime: int | None = None,
    consecutive_ineffective: int = 0,
    escalated: bool = False,
    modal_dismissals: int = 0,
) -> None:
    """Persist the last-poke state. The bounded-poke fields (T-1363) are optional
    and backward-compatible: legacy callers omit them and the file is unchanged
    in shape except for an added consecutive_ineffective=0.

    - pre_poke_jsonl_mtime: the main jsonl mtime captured RIGHT BEFORE this poke,
      so the NEXT cycle can tell whether this poke advanced the session (worked).
    - consecutive_ineffective: running count of pokes that did NOT advance.
    - escalated: this fire suppressed the poke + escalated (stuck, not idle).
    - modal_dismissals: Enter-confirms sent at the cap modal (T-1369), which is
      what bounds that remediation across cycles. Written only when non-zero so
      every existing state file and its assertions keep their exact shape.
    """
    data = {
        "ts": int(time.time()),
        "agent": agent,
        "predicate": predicate,
        "consecutive_ineffective": int(consecutive_ineffective),
    }
    if pre_poke_jsonl_mtime is not None:
        data["pre_poke_jsonl_mtime"] = int(pre_poke_jsonl_mtime)
    if escalated:
        data["escalated"] = True
    if modal_dismissals:
        data["modal_dismissals"] = int(modal_dismissals)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def read_poke_state(state_path: Path) -> dict | None:
    """Read the last-poke state file, or None if absent/unparseable."""
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def poke_effectiveness_decision(
    prev_state: dict | None,
    cur_jsonl_mtime: int,
    predicate: str,
    max_ineffective: int,
) -> tuple[bool, int, str]:
    """Decide whether to SUPPRESS this poke because prior pokes weren't working.

    Effectiveness signal: a poke that lands makes the session take a turn, which
    advances its newest timestamped jsonl entry (`latest_jsonl_activity`, T-5367; the
    state key keeps its historical name pre_poke_jsonl_mtime). So if the prior poke (SAME predicate) recorded
    a pre-poke mtime and the jsonl has NOT advanced past it, that poke was
    ineffective — the session is stuck, not idle.

    NAME COLLISION NOTE (T-1950): this `consecutive_ineffective` is a
    DIFFERENT metric from tools/keep_going.py's same-named counter. This one
    keys on jsonl-mtime advance (did the session take a turn at all); keep
    going's keys on agent-tagged LEDGER entries (did the nudge produce owned,
    recorded work) and is explicitly NOT an activity metric. Don't aggregate
    or compare the two by name.

    Returns (suppress, consecutive_ineffective, reason), where
    consecutive_ineffective is the UPDATED count to persist with this fire.
    suppress=True → do NOT poke; escalate instead.

    The poke-once-vs-skip fork (T-1363) is the max_ineffective parameter:
      N=1 → poke once, suppress on the next ineffective cycle (poke-once-then-escalate).
      N=0 → suppress immediately, never poke (skip pokes, straight to escalate).
    """
    consecutive = 0
    if (
        prev_state
        and prev_state.get("predicate") == predicate
        and prev_state.get("pre_poke_jsonl_mtime") is not None
    ):
        prev_mtime = int(prev_state.get("pre_poke_jsonl_mtime") or 0)
        if cur_jsonl_mtime > prev_mtime:
            consecutive = 0  # prior poke advanced the session → it worked → reset
        else:
            consecutive = int(prev_state.get("consecutive_ineffective", 0)) + 1
    if consecutive >= max_ineffective:
        return (
            True,
            consecutive,
            f"{consecutive} consecutive ineffective poke(s) >= {max_ineffective}; "
            "session stuck not idle → suppress + escalate",
        )
    return (False, consecutive, f"poke (consecutive_ineffective={consecutive})")


# ---------------------------------------------------------------------------
# Fire-tracking (T-3072 activity_monitor meta-drift alarm)
# ---------------------------------------------------------------------------


def _fires_log_path() -> Path:
    """Recovery-local JSONL fire-log path — mirrors `_state_path_for_agent`'s
    PANTHEON_STATE_DIR override convention (same env var, same call-time
    read) so tests can redirect it the same way they already redirect the
    cooldown state file."""
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / "activity_monitor" / "fires.jsonl"


def _record_fire(agent: str, predicate: str, *, deferred: bool = False) -> None:
    """Append one fire-event record to the Recovery-local drift-tracking JSONL
    (T-3072 meta-drift alarm — `tools/activity_monitor_drift_check.py` reads
    this to alert when a predicate has gone silently stale, the T-3070 class
    problem). Best-effort: broad catch, log-only on failure — a broken
    fire-log must NEVER wedge the primary poke path (D12), the same posture
    as `_escalate_stuck`'s alert-routing catch and the P1.d
    session_reset_log append below.

    deferred=True (T-5648): the predicate MATCHED but the poke was deferred (busy
    pane, T-3599). The drift check counts it, because the matcher is alive; the
    T-3991 backoff reader skips it, because no poke landed.
    """
    try:
        path = _fires_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        record = {
            "ts": now,
            "ts_iso": datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "agent": agent,
            "predicate": predicate,
        }
        if deferred:
            record["deferred"] = True
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:  # noqa: BLE001 — fire-tracking must never wedge the monitor
        _log(agent, f"_record_fire({predicate!r}) failed (non-fatal): {exc!r}")


# ---------------------------------------------------------------------------
# P1.b handled-wait backoff (T-3991)
# ---------------------------------------------------------------------------


def _read_recent_fires(agent: str, limit: int = 50) -> list[dict]:
    """Read the tail of the Recovery-local fires.jsonl, filtered to `agent`.

    Best-effort mirror of `_record_fire`'s posture (D12): a broken/absent fire-log
    returns [] and never wedges the monitor — no backoff is strictly safer than a
    crash (it just keeps the legacy 30min cadence). The fire-log is Recovery-local
    (PANTHEON_STATE_DIR), so this is a direct read, no sudo. `limit` bounds how far
    back a streak can be reconstructed; the multiplier over-reads to survive
    multi-agent interleaving before the per-agent filter.
    """
    try:
        path = _fires_log_path()
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        out: list[dict] = []
        for ln in lines[-(limit * 6):]:
            try:
                rec = json.loads(ln)
            except (json.JSONDecodeError, ValueError):
                continue
            if (isinstance(rec, dict) and rec.get("agent") == agent and isinstance(rec.get("ts"), int)
                    and not rec.get("deferred")):  # T-5648: delivered pokes only
                out.append(rec)
        return out[-limit:]
    except Exception as exc:  # noqa: BLE001 — fire-log read must never wedge the monitor
        _log(agent, f"_read_recent_fires failed (non-fatal): {exc!r}")
        return []


def count_consecutive_p1b_fires(
    fires: list[dict], now: int, streak_gap_seconds: int
) -> tuple[int, int | None]:
    """Pure: count the trailing run of contiguous p1b fires for ONE agent.

    Walks the records newest-first. A run continues while each fire is within
    `streak_gap_seconds` of the previous one (and the newest within the gap of
    `now`); it breaks at the first non-p1b fire, the first over-gap jump, or the
    start of the list. Returns (count, streak_start_ts) where streak_start_ts is
    the oldest ts still inside the run, or (0, None) when the trailing state is
    not an in-window p1b run.

    The gap-break models "a new wait shape" (a long quiet, or a different
    predicate firing) resetting the streak — the T-3991 reset condition — without
    needing to inspect operator-inbound state directly (a returned the operator makes
    p1b stop firing, so its fires simply stop landing and the run goes stale).
    """
    recs = sorted(
        (r for r in fires if isinstance(r.get("ts"), int)), key=lambda r: int(r["ts"])
    )
    count = 0
    streak_start: int | None = None
    prev_ts = now
    for rec in reversed(recs):
        ts = int(rec["ts"])
        if prev_ts - ts > streak_gap_seconds:
            break
        if rec.get("predicate") != "p1b":
            break
        count += 1
        streak_start = ts
        prev_ts = ts
    return count, streak_start


def _parse_ticket_created_epoch(value: str) -> int | None:
    """Parse a ticket front-matter `created:` value to epoch seconds, or None."""
    value = value.strip().strip("'\"")
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


def has_recent_operator_ticket(
    pantheon_config_path: Path, sudo_user: str, since_ts: int
) -> bool:
    """True iff a target=operator ticket was CREATED at/after `since_ts`.

    This is the "operator already handled the wait" signal for the P1.b backoff:
    SOP step 3 says an operator-blocked ask becomes a target=operator ticket, and the
    monitored agent files that ticket into its OWN clone (this reads
    `pantheon_config_path` via `sudo_user` = that agent's account), so a
    just-filed ticket is visible with no fetch. Scans inbox + active only (a
    resolved ticket is no longer an open wait). Best-effort: any error returns
    False, i.e. NO backoff — the safe default keeps the legacy poke cadence rather
    than silence a poke on a bad read.
    """
    p = str(pantheon_config_path)
    script = (
        f'for f in "{p}"/tickets/inbox/*.md "{p}"/tickets/active/*.md; do '
        '[ -f "$f" ] || continue; '
        'if grep -qE "^target:[[:space:]]*operator[[:space:]]*$" "$f"; then '
        'grep -m1 -E "^created:" "$f"; fi; '
        'done'
    )
    cmd = _sudo(sudo_user) + ["bash", "-c", script]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=_GIT_TIMEOUT_SECONDS
        )
    except (subprocess.SubprocessError, OSError):
        return False
    for line in result.stdout.splitlines():
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue
        ts = _parse_ticket_created_epoch(parts[1])
        if ts is not None and ts >= since_ts:
            return True
    return False


def p1b_effective_cooldown(
    agent: str,
    cfg: dict,
    base_cooldown: int,
    *,
    enabled: bool,
    backoff_cooldown: int,
    after_fires: int,
    streak_gap_seconds: int,
    now: int | None = None,
) -> int:
    """Return the cooldown to apply to P1.b this cycle: `backoff_cooldown` when the
    handled-wait backoff engages (T-3991), else `base_cooldown`.

    Backoff engages iff: enabled, AND >= `after_fires` consecutive p1b fires are on
    the current wait-streak, AND a target=operator ticket was created since the
    streak began. The ticket probe is evaluated LAST (and only when the streak gate
    already passed) so the common no-streak path pays no sudo cost.
    """
    if not enabled:
        return base_cooldown
    now = int(time.time()) if now is None else now
    streak, streak_start = count_consecutive_p1b_fires(
        _read_recent_fires(agent), now, streak_gap_seconds
    )
    if streak < after_fires or streak_start is None:
        return base_cooldown
    if not has_recent_operator_ticket(
        cfg["pantheon_config_path"], cfg["sudo_user"], streak_start
    ):
        return base_cooldown
    _log(
        agent,
        f"P1.b handled-wait backoff ENGAGED (T-3991): {streak} consecutive p1b "
        f"fires + a target=operator ticket filed since the streak began → p1b "
        f"cooldown {base_cooldown // 60}min → {backoff_cooldown // 60}min",
    )
    return backoff_cooldown


# ---------------------------------------------------------------------------
# Activity-signal sources (P1.a)
# ---------------------------------------------------------------------------


def latest_commit_ts(repo_path: Path, sudo_user: str) -> int | None:
    """Return latest commit timestamp on the repo (any author). 0 / None on failure."""
    cmd = _sudo(sudo_user) + ["git", "-C", str(repo_path), "log", "-1", "--format=%ct"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=_GIT_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def latest_jsonl_mtime(jsonl_dir: Path, sudo_user: str) -> int | None:
    """Return mtime of the most-recently-modified session jsonl in jsonl_dir.

    This is the canonical "agent is producing tokens" signal — mirrors the
    same source context_fill_monitor uses for fill-state probes. Catches
    orchestration overhead (mid-b9-cycle reasoning) that doesn't surface as
    commits or ledger entries but is real Orchestrator activity.

    Returns None on any failure (no jsonl present, sudo denied, etc.).
    """
    cmd = _sudo(sudo_user) + [
        "bash", "-c",
        f"find {jsonl_dir} -maxdepth 1 -name '*.jsonl' -printf '%T@\\n' 2>/dev/null | sort -rn | head -1",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=_GIT_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        # find -printf '%T@' returns float seconds; truncate to int epoch.
        return int(float(raw))
    except ValueError:
        return None


# T-5367: the poke-landed signal reads the newest TIMESTAMPED entry, not the file mtime.
# Claude Code rewrites untimestamped metadata lines in place while a seat is idle (T-5050:
# 2026-10-22 specialist/archivist mtimes 11-26 min old, newest entries ~5h old), so on mtime a
# poke that never landed could read as "the session took a turn" and reset the bound.
ACTIVITY_TAIL_BYTES = 256 * 1024
ACTIVITY_CANDIDATES = 3  # newest few files by mtime; an idle rewrite can reorder them


def _jsonl_entry_epoch(jsonl_dir: Path, sudo_user: str) -> float | None:
    """Newest entry `timestamp` in the tails of the newest few session jsonls, read as
    sudo_user. Parsing is health_export's (T-5050). None on any failure."""
    try:  # lazy, mirroring latest_productive_jsonl_mtime's import shape
        from tools.health_export import _newest_entry_epoch  # noqa: PLC0415
    except ImportError:  # pragma: no cover - import shape varies by runner
        from health_export import _newest_entry_epoch  # type: ignore[no-redef]  # noqa: PLC0415
    script = (
        f"find {shlex.quote(str(jsonl_dir))} -maxdepth 1 -name '*.jsonl' -printf '%T@ %p\\n' "
        f"2>/dev/null | sort -rn | head -{ACTIVITY_CANDIDATES} | cut -d' ' -f2- | "
        f"while IFS= read -r f; do printf '\\0'; tail -c {ACTIVITY_TAIL_BYTES} \"$f\"; done"
    )
    try:
        result = subprocess.run(_sudo(sudo_user) + ["bash", "-c", script], capture_output=True,
                                check=False, timeout=_GIT_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        return None
    best = None
    for chunk in result.stdout.split(b"\0")[1:]:
        try:
            ts = _newest_entry_epoch(chunk.decode("utf-8", "replace"), claude=True)
        except Exception:  # noqa: BLE001 - a bad tail is "no evidence", never a crash
            ts = None
        if ts is not None and (best is None or ts > best):
            best = ts
    return best


def latest_jsonl_activity(jsonl_dir: Path, sudo_user: str) -> int | None:
    """The poke-effectiveness signal (T-5367): did the session take a turn at all?

    The newest timestamped entry, capped by the newest file mtime (an entry cannot
    postdate its write). With no readable timestamped entry it falls back to the mtime,
    the pre-T-5367 reading. Error turns still count (they are timestamped records), which
    is what effectiveness wants; see `latest_productive_jsonl_mtime` for why liveness
    differs. None only when there is no mtime either.
    """
    mtime = latest_jsonl_mtime(jsonl_dir, sudo_user)
    if mtime is None:
        return None
    entry = _jsonl_entry_epoch(jsonl_dir, sudo_user)
    return min(int(entry), mtime) if entry is not None else mtime


# T-2387: how far back to look for a NON-ERROR assistant turn. 200 mirrors
# context_fill_monitor.API_HEALTH_TAIL_LINES (same file, same sudo'd tail read).
# Sizing matters in one direction only: if a genuinely-working seat's newest
# non-error assistant record falls OUTSIDE this window, this reads as "no
# productive turn" and the caller treats the seat as idle. The cost of that is a
# spurious POKE — activity_monitor nudges, it does not restart — which is why a
# generous window plus a low-harm consequence is an acceptable trade here and
# would NOT be on context_fill_monitor's force-restart path.
PRODUCTIVE_TAIL_LINES = 200

# T-2387 ship-dark. Default OFF: the liveness sites keep their existing
# turn-counting behaviour until this is armed per-agent via a systemd drop-in,
# per the T-1126 supervised-first-fire convention. Arming changes which seats
# look idle, and therefore which get poked.
PRODUCTIVE_LIVENESS_ENABLED = os.environ.get(
    "PANTHEON_PRODUCTIVE_LIVENESS_ENABLED", "0") == "1"

# T-5400 ship-dark, same T-1126 convention. Armed, the UNARMED-productive liveness
# path reads the newest timestamped entry (`latest_jsonl_activity`, T-5367) instead of
# the raw mtime, which an idle metadata rewrite advances (T-5050), making an idle or
# wedged seat look recently active. Arming makes such seats read idle sooner: more pokes.
ENTRY_LIVENESS_ENABLED = os.environ.get(
    "PANTHEON_ENTRY_LIVENESS_ENABLED", "0") == "1"


def jsonl_turn_signal(jsonl_dir: Path, sudo_user: str) -> int | None:
    """Did the seat take a turn, any outcome: entry-based when T-5400 is armed, else
    the raw mtime. Shared by the liveness default and tui_wedge_sampler so the wedge
    detector and the sampler that measured its threshold read the same clock."""
    if ENTRY_LIVENESS_ENABLED:
        return latest_jsonl_activity(jsonl_dir, sudo_user)
    return latest_jsonl_mtime(jsonl_dir, sudo_user)


def latest_productive_jsonl_mtime(
    jsonl_dir: Path, sudo_user: str, agent: str = "?"
) -> int | None:
    """Epoch of the newest assistant turn that was NOT an API error (T-2387).

    WHY THIS EXISTS SEPARATELY FROM `latest_jsonl_mtime`, and why that function
    was deliberately NOT changed in place: the two answer DIFFERENT questions
    with OPPOSITE requirements for an error turn.

      "did the session take a turn at all?"  — poke-effectiveness. An error turn
          COUNTS: the poke landed, the session woke, it produced output. That is
          `latest_jsonl_activity` (T-5367: the newest timestamped entry, since
          the raw mtime also moves on an idle metadata rewrite).
      "is this seat doing work?"             — liveness. An error turn must NOT
          count. This function.

    Conflating them is the T-2387 defect: artisan hit the monthly spend cap and
    produced 23 turns / 23 errors on 07-30 and 22 / 22 on 07-31 — 59 identical
    failures across two days — and every check asking "did it produce a turn"
    answered YES for the entire outage. mtime advances on an error just as
    happily as on real work.

    KEYED ON THE OUTCOME BOOLEAN, NOT ON A STATUS OR A STRING. The runtime sets
    `isApiErrorMessage` on the record itself, so this covers the spend cap (429),
    the api-health case (400), model-outage (404) and every error class nobody
    has met yet, with no enumeration to maintain. The tree already carries four
    detectors keyed to specific error identities — including a literal substring
    match on "monthly spend limit" — and each new class has arrived as a new
    narrow detector. That is the pattern this breaks.

    THIS IS ONE ARM OF TWO. It sees turns that RAN and errored. It cannot see a
    seat producing no record at all — a crashloop, a wedged tool, a dead harness.
    That case is covered by the caller's existing staleness threshold: with no
    productive turn found this returns None, the caller's activity clock falls
    back to its other signals, and an idle seat trips the normal idle gate.

    Returns None when no non-error assistant turn is found in the window, or on
    any read failure (fail-open, mirroring `read_recent_records`).
    """
    try:  # lazy import: context_fill_monitor is heavy and only needed when armed
        from tools.context_fill_monitor import (  # noqa: PLC0415
            _parse_iso8601_to_epoch, find_latest_jsonl, read_recent_records,
        )
    except ImportError:  # pragma: no cover - import shape varies by runner
        from context_fill_monitor import (  # type: ignore[no-redef]  # noqa: PLC0415
            _parse_iso8601_to_epoch, find_latest_jsonl, read_recent_records,
        )

    jsonl = find_latest_jsonl(Path(jsonl_dir), sudo_user, agent)
    if jsonl is None:
        return None
    for rec in read_recent_records(jsonl, sudo_user, agent, limit=PRODUCTIVE_TAIL_LINES):
        if rec.get("type") != "assistant" or rec.get("isApiErrorMessage"):
            continue
        ts = _parse_iso8601_to_epoch(rec.get("timestamp"))
        if ts is not None:
            return int(ts)
    return None


def liveness_jsonl_mtime(jsonl_dir: Path, sudo_user: str, agent: str = "?") -> int | None:
    """The jsonl activity signal for LIVENESS callers, ship-dark switched (T-2387).

    Armed  → newest non-error assistant turn (a seat erroring in a loop goes quiet).
    Default→ `jsonl_turn_signal`: the raw mtime (pre-T-2387 behaviour), or the newest
             timestamped entry when T-5400's ENTRY_LIVENESS_ENABLED is armed.

    Poke-effectiveness callers must NOT use this — they want the turn-counting
    signal regardless of outcome. See `latest_productive_jsonl_mtime`.
    """
    if PRODUCTIVE_LIVENESS_ENABLED:
        return latest_productive_jsonl_mtime(jsonl_dir, sudo_user, agent)
    return jsonl_turn_signal(jsonl_dir, sudo_user)


def active_team_inboxes(teams_dir: Path, sudo_user: str) -> bool:
    """True iff an active-team inbox dir exists under teams_dir (T-1290 signal).

    Looks for `teams_dir/*/inboxes` — the per-team inbox subdir present whenever
    a session has spawned a team. NOT `teams_dir` non-empty (the implicit
    session team makes that always-true). Fail-CLOSED (False) on any probe
    failure so a sudo/FS error can never manufacture a fire.
    """
    cmd = _sudo(sudo_user) + [
        "bash", "-c",
        f"find {teams_dir} -maxdepth 2 -type d -name inboxes 2>/dev/null | head -1",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=_GIT_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return bool(result.stdout.strip())


def newest_team_artifact_mtime(teams_dir: Path, sudo_user: str) -> int | None:
    """Newest mtime (int epoch) of any file under teams_dir, or None.

    Captures teammate-side artifacts — inbox message files + subagent
    transcripts (subagents/agent-*.jsonl). Used by check_p1c's §7 sharpening:
    a teammate artifact NEWER than the dispatcher's main-jsonl mtime is positive
    evidence of an unreported sub-result (a plausibly-dropped completion wake),
    vs. a long-dead stale teams dir. Returns None on any failure.
    """
    cmd = _sudo(sudo_user) + [
        "bash", "-c",
        f"find {teams_dir} -type f -printf '%T@\\n' 2>/dev/null | sort -rn | head -1",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=_GIT_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _ledger_union_tac_cmd(repo_path: Path, n: int = 500) -> str:
    """Shell expression that tac|heads the newest N lines across the ledger union.

    T-0148: post-migration the newest entries live in the latest per-week
    subfolder shard `ledger/<seg>/<seg>.jsonl`, not the flat `2026.jsonl`.
    Reading only `{year}.jsonl` would go blind after the migration and break
    ORG-011 P1.a idle probes. This mirrors ledger_shard.iter_ledger_entry_files
    in shell: concatenate every subfolder shard (segment-name sorted, so
    chronological) followed by the legacy flat file if it still exists, then
    `tac | head` to surface the newest entries.

    Only `<seg>/<seg>.jsonl` is a shard, the helper's own rule (T-4517). A
    `ledger/*/*.jsonl` glob also took `ledger/appetite/<seg>.jsonl`, which sorts
    after every `2026-W*` dir, so `tac | head` returned only appetite lines and
    every ledger-gated check went blind. The flat
    `2026.jsonl.archive-pre-d10-shard` lives at ledger/ root, so it is never a
    shard. Pre-migration no subfolder matches and only the legacy file is read.

    Runs remotely under `sudo bash -c`; built as a single shell string. No
    untrusted interpolation — repo_path is a config-derived Path.
    """
    ledger_dir = f"{repo_path}/ledger"
    legacy = f"{ledger_dir}/2026.jsonl"
    # `ls -d ... 2>/dev/null` tolerates the no-match case; `cat` of a
    # non-existent legacy path is suppressed. Files are emitted in sorted
    # (chronological) segment order, legacy last, then reversed by tac.
    return (
        f"{{ for d in $(ls -d {ledger_dir}/*/ 2>/dev/null | sort); "
        f"do s=\"${{d%/}}\"; f=\"$s/${{s##*/}}.jsonl\"; [ -f \"$f\" ] && cat \"$f\"; done; "
        f"[ -f {legacy} ] && cat {legacy}; }} 2>/dev/null | tac | head -n {n}"
    )


def latest_terminal_state_ledger_ts_for_agent(
    repo_path: Path, sudo_user: str, ledger_agent_id: str
) -> int | None:
    """Return latest ledger entry ts where agent == ledger_agent_id AND the
    action contains 'terminal_state' or 'queue_audit', or None.

    Per T-0644: when HO logs an explicit queue terminal-state audit (e.g.
    `ho_queue_terminal_state_audit_post_t0500_...`), that's a structural
    acknowledgement that the queue has nothing autonomously-actionable.
    P1.a uses this to stretch its idle threshold so repeat fires within
    the next ~4hr don't re-pay the same terminal-state ack tokens.
    """
    cmd = _sudo(sudo_user) + [
        "bash",
        "-c",
        _ledger_union_tac_cmd(repo_path, 500),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_JSONL_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    # T-0148: scan ALL matching lines and take max(ts), NOT first-after-tac.
    # The union (_ledger_union_tac_cmd) emits legacy-last, so tac yields legacy
    # lines FIRST — in a transition window (legacy + shards coexist) a
    # first-match could return a STALE ts and mis-fire ORG-011 P1.a idle
    # detection. max(ts) is robust to source-concatenation ordering.
    latest: int | None = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        # T-4737: legacy id (history) or ORG-100 role id + seat slot (new).
        if not agent_role_map.ledger_entry_by(obj, ledger_agent_id):
            continue
        action = obj.get("action") or ""
        if not isinstance(action, str):
            continue
        if "terminal_state" not in action and "queue_audit" not in action:
            continue
        ts_str = obj.get("ts")
        if not isinstance(ts_str, str):
            continue
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        ts = int(dt.timestamp())
        if latest is None or ts > latest:
            latest = ts
    return latest


def latest_ledger_entry_ts_for_agent(repo_path: Path, sudo_user: str, ledger_agent_id: str) -> int | None:
    """Return latest ledger entry ts where agent == ledger_agent_id, or None.

    Reads the newest entries across the ledger union (T-0148): pre-migration
    the legacy flat 2026.jsonl, post-migration the per-week subfolder shards.
    ORG-011's P1.a is concerned with activity in the last 1hr, so 500 newest
    lines is ample headroom regardless of layout.
    """
    # Read via sudo + tac to get most-recent matching entry without loading the
    # whole file. tac+grep+head pattern mirrors context_fill_monitor.
    cmd = _sudo(sudo_user) + [
        "bash",
        "-c",
        _ledger_union_tac_cmd(repo_path, 500),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_JSONL_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    # T-0148: max(ts) over all matching lines, not first-after-tac (ordering-
    # robust across the legacy∪shard union; see the terminal_state probe above).
    latest: int | None = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        # T-4737: legacy id (history) or ORG-100 role id + seat slot (new).
        if not agent_role_map.ledger_entry_by(obj, ledger_agent_id):
            continue
        ts_str = obj.get("ts")
        if not isinstance(ts_str, str):
            continue
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        ts = int(dt.timestamp())
        if latest is None or ts > latest:
            latest = ts
    return latest


# ---------------------------------------------------------------------------
# Queue source (P1.a + P1.b)
# ---------------------------------------------------------------------------


def _scan_actionable_tickets(
    repo_path: Path,
    targets: frozenset[str],
    sudo_user: str,
    queue_states: tuple[str, ...] = ("inbox", "active"),
) -> int | None:
    """Count tickets in the `queue_states` dirs where target OR delivered_to (T-5515) is
    in `targets`, no
    unmet depends_on, not_before (if set) is in the past, the labels field
    does not contain interactive-required, AND blocked_by is not "event"
    (T-0644: event-driven holds wait on a structural witness — pane state,
    cap-hit cycle, calendar tick — not on HO-availability, so they must
    not count toward the autonomous-actionable queue).

    Returns the count, or **None** when the cross-account probe could not be
    completed (sudo timeout / OSError / non-zero rc). The None vs 0 distinction
    matters for callers that must not let a *failed* probe stand in for a
    *confirmed-empty* queue (T-3570: the drift-check suppresses a never-fired
    predicate only on a CONFIRMED-empty queue, never on an unknown one). The
    always-on poke path does not care — it fails open via
    `count_actionable_tickets`, the thin wrapper below.

    T-3557(e): `queue_states` selects which lifecycle dirs count as the
    actionable queue. The default ("inbox", "active") is the always-on
    HEAD-ORCHESTRATOR reading — orchestrator is meant to continuously DRAIN its
    inbox, so an inbox ticket sitting while it idles IS a "why aren't you
    working" signal. Scope-managed SUB-ORCHESTRATOR seats (artisan/mason) are
    dispatched work by the HO and are legitimately idle between dispatches;
    their inbox is a pending/undispatched bucket (persona-consume is a wake
    controller, not a ticket-mover — inbox tickets wait for dispatch, they
    do not auto-become active work). So sub-orchs pass ("active",): only a
    CLAIMED, in-progress ticket counts as work the seat could be stuck on.
    This kills the idle-with-unclaimed-inbox false-fire (mason sat idle with
    T-3457 unclaimed in inbox → p1a/p1b false-fired) WITHOUT hiding a genuine
    wedge: a seat wedged mid-work holds an ACTIVE ticket → still fires; a
    wedge-before-claim (wake Enter eaten by a modal) is caught by
    mail_staleness + the modal detector, not by this queue predicate.

    Reads via sudo-as-<sudo_user> shell pipeline since the agent's
    pantheon-config tree (e.g., /home/claude/pantheon-config) is owned by
    the agent's account and Recovery (the caller) can't traverse the 750-mode
    home directory above it. Single sudo call walks both state dirs +
    extracts the relevant frontmatter fields via awk; Python parses the
    structured output.
    """
    inbox_dir = repo_path / "tickets" / "inbox"
    active_dir = repo_path / "tickets" / "active"
    # awk emits one line per ticket: "<path>|<target>|<depends_on>|<not_before>|<labels>|<blocked_by>|<delivered_to>"
    # — stops scanning each file at the closing '---' of the frontmatter.
    awk_prog = (
        r"""BEGIN { in_fm=0; target=""; deps=""; nb=""; labels=""; blocked=""; dlv=""; sg=""; path="" }
        FNR==1 { if (path!="") print path "|" target "|" deps "|" nb "|" labels "|" blocked "|" dlv "|" sg;
                 path=FILENAME; in_fm=0; target=""; deps=""; nb=""; labels=""; blocked=""; dlv=""; sg="" }
        /^---$/ { if (in_fm==0) { in_fm=1 } else { in_fm=2 } }
        in_fm==1 && /^target:/ { sub(/^target:[[:space:]]*/, ""); target=$0 }
        in_fm==1 && /^depends_on:/ { sub(/^depends_on:[[:space:]]*/, ""); deps=$0 }
        in_fm==1 && /^not_before:/ { sub(/^not_before:[[:space:]]*/, ""); nb=$0 }
        in_fm==1 && /^labels:/ { sub(/^labels:[[:space:]]*/, ""); labels=$0 }
        in_fm==1 && /^blocked_by:/ { sub(/^blocked_by:[[:space:]]*/, ""); blocked=$0 }
        in_fm==1 && /^delivered_to:/ { sub(/^delivered_to:[[:space:]]*/, ""); dlv=$0 }
        in_fm==1 && /^security_gate:/ { sub(/^security_gate:[[:space:]]*/, ""); sg=$0 }
        END { if (path!="") print path "|" target "|" deps "|" nb "|" labels "|" blocked "|" dlv "|" sg }"""
    )
    cmd = _sudo(sudo_user) + [
        "bash", "-c",
        f"find {inbox_dir} {active_dir} -maxdepth 1 -name '*.md' -type f 2>/dev/null | "
        f"xargs -r awk '{awk_prog}'",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=_JSONL_PROBE_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    count = 0
    targets_lower = {t.lower() for t in targets}
    now = datetime.now(timezone.utc)
    for line in result.stdout.splitlines():
        # Tolerate the older 5- and 6-field shapes so test fixtures without the
        # blocked_by / delivered_to columns still parse cleanly.
        parts = line.split("|", 7)
        if len(parts) < 5:
            continue
        _path = parts[0]
        # T-3557(e): honor the per-agent queue-source. A ticket's lifecycle
        # state is its containing dir (inbox/ vs active/); skip any state not
        # in queue_states so sub-orch seats count active-only (see docstring).
        if Path(_path).parent.name not in queue_states:
            continue
        target = parts[1]
        deps = parts[2]
        nb = parts[3]
        labels = parts[4]
        blocked_by = parts[5] if len(parts) >= 6 else ""
        delivered_to = parts[6] if len(parts) >= 7 else ""
        security_gate = parts[7] if len(parts) == 8 else ""  # T-5678
        # T-5515: since the ORG-088 role pooling (10-19) a ticket targets role:<R> and
        # names its holder in delivered_to. Match either, or the count is 0 for every
        # seat and p1b (stuck with work) can never fire.
        if (target.strip().lower() not in targets_lower
                and delivered_to.strip().lower() not in targets_lower):
            continue
        if deps.strip() and deps.strip() != "[]":
            continue
        nb_dt = _parse_not_before(nb.strip())
        if nb_dt is not None and nb_dt > now:
            continue
        if "interactive-required" in labels:
            continue
        if blocked_by.strip().lower() == "event":
            continue
        if security_gate.strip().lower() == "pending":  # T-5678: awaiting HO security-accept
            continue
        count += 1
    return count


def count_actionable_tickets(
    repo_path: Path,
    targets: frozenset[str],
    sudo_user: str,
    queue_states: tuple[str, ...] = ("inbox", "active"),
) -> int:
    """Fail-open wrapper over `_scan_actionable_tickets` (unchanged contract):
    any sudo / shell / parse failure returns 0 (no actionable work detected),
    keeping the poke predicates from firing on flaky cross-account state reads.
    Callers that need to tell a failed probe from a confirmed-empty queue call
    `_scan_actionable_tickets` directly (it returns None on failure)."""
    n = _scan_actionable_tickets(repo_path, targets, sudo_user, queue_states)
    return n if n is not None else 0


def _parse_not_before(value: str) -> datetime | None:
    """Parse a not_before frontmatter value to a tz-aware UTC datetime.

    Returns None for absent (empty / 'null' / '~') or unparseable values —
    matching the historical behavior where not_before was unread (counted).
    Accepts 'YYYY-MM-DD' (treated as 00:00:00Z) and full ISO 'YYYY-MM-DDTHH:MM:SSZ'.
    """
    if not value or value.lower() in ("null", "~"):
        return None
    iso = value.replace("Z", "+00:00")
    if len(iso) == 10 and iso.count("-") == 2:
        iso = iso + "T00:00:00+00:00"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Bridge + JSONL probes (P1.b)
# ---------------------------------------------------------------------------


def fetch_bridge_health(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def operator_last_inbound_age_seconds(health: dict, bridge_inbound_key: str) -> int | None:
    """Return seconds since the operator's last inbound to the agent's bridge channel.

    None when the bridge field is missing or unparseable. Fail-open (treats
    missing as 'cannot determine' rather than 'absent'); P1.b suppresses.
    """
    last_inbound = (health.get("last_inbound_at") or {}).get(bridge_inbound_key)
    if not isinstance(last_inbound, str):
        return None
    try:
        dt = datetime.fromisoformat(last_inbound)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(time.time() - dt.timestamp())


def find_latest_jsonl(jsonl_dir: Path, sudo_user: str) -> Path | None:
    """Mirror of context_fill_monitor.find_latest_jsonl — locate newest jsonl."""
    cmd = _sudo(sudo_user) + [
        "bash",
        "-c",
        f"find {jsonl_dir} -maxdepth 1 -name '*.jsonl' -printf '%T@ %p\\n' 2>/dev/null",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_JSONL_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    lines = [line for line in result.stdout.strip().split("\n") if line]
    if not lines:
        return None
    lines.sort(reverse=True)
    try:
        return Path(lines[0].split(" ", 1)[1])
    except IndexError:
        return None


def latest_assistant_ends_without_tool_use(jsonl_path: Path, sudo_user: str) -> bool:
    """Structural Signal-1 (T-3071): True iff the latest assistant turn's
    final content-block has type=='text' — the model stopped speaking and is
    awaiting external input, by construction. A turn ending in tool_use is
    mid-action (expects a tool_result to continue the loop), so that's False.

    Language-agnostic, brevity-agnostic, prompt-drift-agnostic: replaces the
    T-3070 AWAIT_STATE_REGEX language-regex approach, which treated
    stochastic model text as a load-bearing signal (the operator TG 8225).

    Fail-CLOSED: returns False (no await) on subprocess/probe error, no
    assistant turn found in the scanned window, empty/non-list content, or a
    final block whose type isn't 'text' (tool_use, thinking-only, or any
    other non-text terminal block). Never manufactures an await-fire from
    missing or ambiguous data.
    """
    cmd = _sudo(sudo_user) + [
        "bash",
        "-c",
        f"tac {jsonl_path} | head -n 200",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_JSONL_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list) or not content:
            return False
        last_block = content[-1]
        return isinstance(last_block, dict) and last_block.get("type") == "text"
    return False


def latest_telegram_inbound_ts(jsonl_path: Path, sudo_user: str) -> int | None:
    """Read the most-recent operator-inbound Telegram timestamp from HO's jsonl.

    Replaces the T-0223 `fetch_bridge_health` Gate-1 source, retired when
    pantheon-bridge was decommissioned (T-0767, 2026-06-04). Reads the last 500
    lines of the jsonl (cross-account, via sudo) and parses them with the shared
    tools/telegram_inbound parser. It returns the max inbound ts (unix seconds),
    or None on no inbound / probe error.

    T-4212: T-3075 counted only `queue-operation` enqueue entries. A message to an
    IDLE HO is written as a `user` turn, and one delivered mid-turn as a
    `queued_command` attachment, so chatting with an idle HO read as the operator ABSENT.
    The shared parser accepts all three shapes, and only strings that START with
    the channel tag (quoted prose never counts).
    """
    cmd = _sudo(sudo_user) + [
        "bash",
        "-c",
        f"tac {jsonl_path} | head -n 500",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_JSONL_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return telegram_inbound.latest_inbound_ts_from_lines(result.stdout.splitlines())


# ---------------------------------------------------------------------------
# Cyber-safeguard refusal detector (T-1363 increment 3, pre-poke state-gate)
# ---------------------------------------------------------------------------
# Anthropic's real-time cyber classifier can block a sanctioned security-research
# turn (e.g. Specialist, 2026-06-22, T-1125's sibling — see memory
# reference_cyber_safeguard_block_on_security_research). The session keeps
# emitting refusals (18 in the captured sample) and is WEDGED — a poke just burns
# another refusal. The recovery is a RESTART (clears the flagged context), NOT a
# poke, so the pre-poke gate must route this state away from the poker.
#
# Signature (STRUCTURAL, unambiguous — confirmed against the real 2026-06-22
# Specialist refusal jsonl): the latest assistant message has
#   message.stop_reason == "refusal"  AND  message.stop_details.category == "cyber"
# (message.stop_details.type == "refusal", isApiErrorMessage true). Keying on the
# structured stop_details — NOT the explanation prose — keeps it robust to wording
# changes in the refusal text.
CYBER_REFUSAL_CATEGORY = "cyber"


def is_cyber_refusal_message(obj: dict) -> bool:
    """Pure: True iff a parsed jsonl line is an assistant cyber-safeguard refusal."""
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return False
    msg = obj.get("message")
    if not isinstance(msg, dict) or msg.get("stop_reason") != "refusal":
        return False
    sd = msg.get("stop_details")
    return isinstance(sd, dict) and sd.get("category") == CYBER_REFUSAL_CATEGORY


# --- cap-state classification (T-1369, pre-poke gate branches 2+3) -----------
# Both strings MEASURED from live outages, not guessed — the ticket's own
# instruction. Separator is U+00B7 MIDDLE DOT in both; a regex written from a
# paraphrase ("limit - resets") would silently never match.
#   session: "You've hit your session limit · resets 9:40am (UTC)"
#            (orchestrator, 38x, 2026-08-02 06:28-09:34Z)
#   spend:   "You've hit your monthly spend limit · raise it at claude.ai/..."
#            (artisan, 59x, 2026-07-30/31, a 48h outage — T-2387)
#
# ⚠️ THE TWO BEHAVE OPPOSITELY, and the discriminator is IN the strings
# ("resets" vs "raise it"). The session limit self-clears at the stated time —
# verified end-to-end 2026-08-02: cap named 09:40, first successful turn minutes
# after, zero intervention. The spend limit holds until a HUMAN raises it —
# T-2290's falsification: artisan erred every turn for two days. A generic
# "limit" substring collapses two branches that need opposite routing.
_CAP_SESSION_RE = re.compile(r"hit your session limit.*resets\s+(\S+(?:\s*\([A-Z]+\))?)")
_CAP_SPEND_RE = re.compile(r"hit your (?:monthly|weekly)?\s*spend limit")


def classify_cap_message(obj: dict) -> tuple[str, str | None] | None:
    """Pure: classify a parsed jsonl line as a cap-error assistant turn.

    Returns ("session_limit", reset_hint) — self-clears at reset_hint;
            ("spend_limit", None)         — holds until a human acts;
            None                          — not a cap error.

    Keys on the newest assistant turn's TEXT, requiring isApiErrorMessage —
    an agent DISCUSSING cap strings (this fleet writes tickets full of them)
    must not read as capped. That guard is load-bearing: recovery's own transcript
    quotes both wire strings verbatim many times over.
    """
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None
    if not obj.get("isApiErrorMessage"):
        return None
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        texts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
    else:
        return None
    blob = " ".join(texts)
    m = _CAP_SESSION_RE.search(blob)
    if m:
        return ("session_limit", m.group(1))
    if _CAP_SPEND_RE.search(blob):
        return ("spend_limit", None)
    return None


# ---------------------------------------------------------------------------
# T-1369 branch 4: the PANE-ONLY cap modal.
#
# Specimen captured live off orchestrator 2026-08-02 (specimens/orchestrator_cap_modal_pane.txt)
# after it sat wedged ~50 minutes PAST its own printed reset and the operator had to
# ask "can u check on orchestrator". This state is invisible to detect_cap_state: the
# session-limit error IS in the jsonl, but the thing keeping the seat down is the
# `/rate-limit-options` menu holding the TUI. The jsonl branch correctly declines
# to poke and correctly declines to escalate (a session limit self-clears) — and
# then the cap clears and the seat STILL does not move, because the modal is up.
#
#     What do you want to do?
#     ❯ 1. Stop and wait for limit to reset
#       2. Upgrade your plan
#     Enter to confirm · Esc to cancel
#
# ⚠️ SAFETY, and it is the whole design: option 2 SPENDS MONEY, which is
# the operator's lane under the standing financial rule. Enter confirms WHATEVER the
# cursor sits on. So it is not enough to recognise the menu — we must read where
# the ❯ actually IS and refuse to touch the keyboard unless it is on option 1.
# "Recognise the menu, press Enter" is the version of this that buys a plan.
_MODAL_QUESTION = "What do you want to do?"
_MODAL_OPT1 = "Stop and wait for limit to reset"
_MODAL_OPT2 = "Upgrade your plan"
_MODAL_CURSOR = "❯"
# capture_pane() uses `tmux capture-pane -p` (no -e), so production text has no
# escapes — but the specimen was captured WITH -e, and a detector that only works
# on whichever form the fixture happens to be in is a detector tested against
# itself. Strip unconditionally so both forms take the same path.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

MODAL_SAFE = "cap_modal_option1"
MODAL_UNSAFE = "cap_modal_unsafe_selection"


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def classify_cap_modal(pane_text: str | None) -> str | None:
    """Pure: classify a pane capture as the /rate-limit-options menu.

    MODAL_SAFE   — the menu is up AND the cursor is on option 1 (stop and wait).
                   Enter here is the same keystroke a human would send, and it
                   spends nothing.
    MODAL_UNSAFE — the menu is up but the cursor is NOT provably on option 1
                   (on option 2, or no cursor line found at all). NEVER send
                   keys in this state; hand it to a human.
    None         — not this menu. Anything unrecognised is None, so a pane we
                   cannot parse is never treated as dismissable.

    Requires BOTH option labels, not just the question: "What do you want to
    do?" is a generic TUI prompt and matching it alone would make every future
    menu look like this one.
    """
    if not pane_text:
        return None
    text = _strip_ansi(pane_text)
    if _MODAL_QUESTION not in text:
        return None
    if _MODAL_OPT1 not in text or _MODAL_OPT2 not in text:
        return None
    # Locate the cursor by looking at the OPTION lines specifically, not at the
    # first line carrying the glyph. In the real specimen the '❯' also prefixes
    # the shell prompt line ('❯ /rate-limit-options', the command that opened the
    # menu), which sits ABOVE the options — a first-match scan reads that line,
    # finds no option-1 text, and calls a perfectly safe modal unsafe. Caught by
    # testing against the captured pane instead of a hand-typed approximation.
    opt1_selected = opt2_selected = False
    for line in text.splitlines():
        if _MODAL_OPT1 in line and _MODAL_CURSOR in line:
            opt1_selected = True
        elif _MODAL_OPT2 in line and _MODAL_CURSOR in line:
            opt2_selected = True
    if opt1_selected and not opt2_selected:
        return MODAL_SAFE
    return MODAL_UNSAFE


# ---------------------------------------------------------------------------
# T-3558(b2): the Claude Code TOOL-PERMISSION modal — a DISTINCT wedge class
# from the cap modal above.
#
# Specimen: tools/testdata/artisan_permission_modal_pane.txt — a REAL capture (modal
# region) off artisan/suborch2 2026-09-07, the Read(//tmp/**) push-gate variant that
# wedged artisan (it read /tmp push-logs via the Read TOOL in manual mode; the prompt
# sat unanswered -> mail staled -> caught by mail_staleness, cleared by Recovery).
#
# DIAGNOSTIC / DETECTION ONLY. A permission prompt is a SAFETY BOUNDARY and must
# NEVER be auto-answered (T-3557c trust surface; operator-flagged). This classifier
# exists so a STUCK escalation can NAME the wedge ("wedged on a permission modal for
# Read(//tmp/**)") -> Recovery's playbook is to suggest the behavioural fix (Bash cat/tail
# for /tmp logs, pre-permitted) rather than grant a permission. There is deliberately
# no _session-gate / send-keys counterpart (unlike send_modal_confirm for the cap
# modal): nothing in this module presses a key on a permission prompt.
#
# Require BOTH the "Do you want to proceed?" question AND the "Tab to amend" footer
# (mirrors classify_cap_modal's require-multiple-tokens discipline). "Tab to amend"
# is what cleanly separates this from the cap modal, whose footer is
# "Enter to confirm · Esc to cancel" (no amend) — so the two classifiers never
# collide on each other's real specimens (asserted in tests).
_PERMISSION_MODAL_QUESTION = "Do you want to proceed?"
_PERMISSION_MODAL_FOOTER = "Tab to amend"
# The requested permission is echoed in the "don't ask again for <Tool>(<pattern>)"
# option line — the most useful diagnostic token (names the exact tool+scope).
_PERMISSION_DONT_ASK_RE = re.compile(r"don't ask again for\s+(\S.*\S|\S)\s*$")

PERMISSION_MODAL = "permission_modal"


def classify_permission_modal(pane_text: str | None) -> str | None:
    """Pure: detect a Claude Code tool-permission prompt in a pane capture.

    Returns the requested-permission descriptor (e.g. "Read(//tmp/**)") when the
    modal is up and its "don't ask again for <X>" line is parseable; the generic
    PERMISSION_MODAL label when the modal is up but that line is absent/unparseable;
    None for anything unrecognised (so an unparseable pane is never mistaken for a
    permission wedge). DIAGNOSTIC ONLY — never used to answer the prompt.

    Requires BOTH the question and the "Tab to amend" footer so it does not
    false-match the cap modal or a bare "proceed?" prompt.
    """
    if not pane_text:
        return None
    text = _strip_ansi(pane_text)
    if _PERMISSION_MODAL_QUESTION not in text or _PERMISSION_MODAL_FOOTER not in text:
        return None
    for line in text.splitlines():
        m = _PERMISSION_DONT_ASK_RE.search(line)
        if m:
            return m.group(1).strip()
    return PERMISSION_MODAL


# ---------------------------------------------------------------------------
# T-3558(b2), the preview-chip slice: a FOCUSED artifact-preview chip takes the
# composer's Enter, so a wake or poke never submits and the seat sits wedged.
#
# Specimens: tools/testdata/artisan_preview_chip_{focused,unfocused}_pane.txt, REAL
# artisan bytes from 2026-09-07 (its pipe-pane log, the T-3497 slice-1 wedge that was
# cleared before anyone could capture-pane it), replayed through a VT emulator at the
# pane's own 209x50 geometry. Not hand-typed.
#
# Healthy chip:  "  ⧉  t2738-preview · ctrl+] to open"              (harmless)
# Focused chip:  "❯ ⧉  t2738-preview · Enter to open · x to dismiss" (the wedge)
#
# Match the focused chip's whole LINE, not its tokens: prose quoting "Enter to open ·
# x to dismiss" (this ticket's own body, shown in a pane) must not read as a wedge.
# DETECTION ONLY. Clearing it (x, then Enter) is deterministic, but arming a
# modal-specific auto-clear on a seat is the operator's call (TG 6791 authorised generic
# pokes at orchestrator parity, nothing more), so nothing here sends a key.
_PREVIEW_CHIP_FOCUSED_RE = re.compile(
    r"^\s*❯\s*⧉\s+(\S+)\s+·\s+Enter to open\s+·\s+x to dismiss\s*$")

PREVIEW_CHIP = "preview_chip"


def classify_preview_chip(pane_text: str | None) -> str | None:
    """Pure: "preview_chip:<name>" when a focused preview chip is holding the
    composer's Enter; None otherwise, including the harmless unfocused chip and
    anything unrecognised. DIAGNOSTIC ONLY."""
    if not pane_text:
        return None
    for line in _strip_ansi(pane_text).splitlines():
        m = _PREVIEW_CHIP_FOCUSED_RE.match(line)
        if m:
            return f"{PREVIEW_CHIP}:{m.group(1)}"
    return None


def classify_wedge(pane_text: str | None) -> str | None:
    """The named wedge class for a STUCK escalation, or None: a permission modal
    (its requested permission) first, then a focused preview chip. The cap modal
    keeps its own gate (_session_cap_modal); it is not a STUCK class."""
    return classify_permission_modal(pane_text) or classify_preview_chip(pane_text)


def _session_cap_modal(cfg: dict) -> str | None:
    """Pre-poke gate seam for the pane modal, parallel to _session_cap_state.

    No configured pane → None, not a KeyError. Two reasons, and the second is
    the one that matters: an agent can legitimately have no pane (specialist-a2a by
    construction), and this call sits on the dispatch path that EVERY poke goes
    through, so a cfg without the key must degrade rather than shell out or
    raise. Subscript here would also mean any test exercising _dispatch_poke
    without stubbing this seam runs a live `tmux capture-pane` against a
    sibling's real session — the existing cap-gate tests were saved from exactly
    that only by naming their key differently.
    """
    pane = cfg.get("tmux_target_pane")
    if not pane:
        return None
    return classify_cap_modal(capture_pane(pane, cfg.get("sudo_user")))


def send_modal_confirm(cfg: dict) -> bool:
    """Send a BARE Enter to confirm the highlighted menu option.

    Deliberately NOT send_poke(): that sends prompt TEXT and then Enter, and
    typing text into a live menu is how a dismissal becomes a selection. Callers
    must have classified MODAL_SAFE first — this function does not re-check.
    """
    return send_bare_enter(cfg)


def send_bare_enter(cfg: dict) -> bool:
    """One bare Enter to the agent's pane, no text. The caller owns the decision
    (send_modal_confirm: MODAL_SAFE; _flush_stuck_poke: submit_verify.should_repress)."""
    pane = cfg["tmux_target_pane"]
    cmd = _sudo(cfg["sudo_user"]) + [
        "bash", "-c",
        f"XDG_RUNTIME_DIR=/run/user/$(id -u) tmux send-keys -t {pane} Enter",
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=_TMUX_TIMEOUT_SECONDS
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


# Bounded remediation (feedback_bounded_repeating_remediation): a dismissal that
# does not take must not become an infinite Enter-presser. Three attempts across
# cycles, tracked in the poke state, then escalate and hold.
MAX_MODAL_DISMISSALS = int(os.environ.get("ACTIVITY_MONITOR_MAX_MODAL_DISMISSALS", "3"))


def detect_cap_state(jsonl_path: Path, sudo_user: str) -> tuple[str, str | None] | None:
    """Cap-state of the MOST-RECENT assistant turn, or None.

    Same shape and fail-safe direction as detect_cyber_refusal: a later
    non-cap assistant message means the agent recovered → None; any read or
    parse failure → None (never mis-route a healthy session on a probe glitch).
    """
    cmd = _sudo(sudo_user) + ["bash", "-c", f"tac {jsonl_path} | head -n 200"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False,
            timeout=_JSONL_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        # First assistant line in tac order == most-recent assistant turn.
        return classify_cap_message(obj)
    return None


def _session_cap_state(cfg: dict) -> tuple[str, str | None] | None:
    """Pre-poke gate seam for cap-state, parallel to _session_cyber_blocked."""
    jsonl = find_latest_jsonl(cfg["jsonl_dir"], cfg["sudo_user"])
    if jsonl is None:
        return None
    return detect_cap_state(jsonl, cfg["sudo_user"])


def _session_cyber_blocked(cfg: dict) -> bool:
    """Pre-poke gate helper (T-1363 inc.3): True iff the agent's session is
    currently cyber-blocked — its most-recent assistant turn is a cyber-safeguard
    refusal. Resolves the newest jsonl then classifies it. Single seam so the
    dispatch path patches one thing in tests. Fail-safe False on any miss."""
    jsonl = find_latest_jsonl(cfg["jsonl_dir"], cfg["sudo_user"])
    if jsonl is None:
        return False
    return detect_cyber_refusal(jsonl, cfg["sudo_user"])


def detect_cyber_refusal(jsonl_path: Path, sudo_user: str) -> bool:
    """True iff the MOST-RECENT assistant message in the agent's session jsonl is a
    cyber-safeguard refusal — i.e. the session is cyber-blocked and a poke is futile.

    Reads the newest assistant turn only (tac | head): a later non-refusal assistant
    message means the agent recovered, so this is False. Any read/parse error fails
    SAFE → False (don't mis-route a healthy session to restart on a probe glitch)."""
    cmd = _sudo(sudo_user) + ["bash", "-c", f"tac {jsonl_path} | head -n 200"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False,
            timeout=_JSONL_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        # First assistant line in tac order == most-recent assistant turn.
        return is_cyber_refusal_message(obj)
    return False


def latest_assistant_interactive_tool_use(jsonl_path: Path, sudo_user: str) -> str | None:
    """Return the tool name of the latest assistant turn IFF that turn is a
    tool_use with name in USER_BLOCKING_TOOL_NAMES (AskUserQuestion etc.).

    Returns None when:
      - The latest assistant turn is text-only (no tool_use blocks).
      - The latest tool_use is not in the user-blocking allowlist.
      - jsonl read fails.

    Per T-0297 — extends the await-state signal beyond text-regex matching
    to catch interactive-menu tool_use turns (which have empty text content
    and would otherwise slip past P1.b's await-state detection).
    """
    cmd = _sudo(sudo_user) + [
        "bash",
        "-c",
        f"tac {jsonl_path} | head -n 200",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_JSONL_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        # The first (most-recent) assistant turn wins. If it has any tool_use
        # block whose name is user-blocking, return that name.
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                if isinstance(name, str) and name in USER_BLOCKING_TOOL_NAMES:
                    return name
        # Found an assistant turn but none of its tool_use blocks match → return None.
        return None
    return None


# ---------------------------------------------------------------------------
# Pane capture + rate-limit signal sources (P1.d)
# ---------------------------------------------------------------------------


def capture_pane(pane: str, sudo_user: str, *, escapes: bool = False) -> str | None:
    """Capture the visible content of the agent's tmux pane.

    Returns None on any subprocess failure (sudo denied, tmux not running,
    etc.) — fail-open so P1.d doesn't fire on flaky reads. escapes=True adds -e
    (keep SGR attributes) for the idle probe, which needs DIM (T-4284).
    """
    flags = "-p -e" if escapes else "-p"
    cmd = _sudo(sudo_user) + [
        "bash", "-c",
        f"XDG_RUNTIME_DIR=/run/user/$(id -u) tmux capture-pane {flags} -t {pane}",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=_TMUX_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def detect_rate_limit_in_pane(pane_text: str, tail_lines: int = P1D_PANE_TAIL_LINES) -> bool:
    """True iff the rate-limit pattern appears in the bottom `tail_lines` of pane.

    Bottom-scoped match avoids false-firing on a rate-limit error from earlier
    in the agent's session that's since been scrolled past (i.e., the agent
    recovered and has been working since).
    """
    lines = pane_text.splitlines()
    tail = "\n".join(lines[-tail_lines:]) if len(lines) > tail_lines else pane_text
    return bool(RATE_LIMIT_REGEX.search(tail))


def latest_rate_limit_assistant_ts(jsonl_path: Path, sudo_user: str) -> int | None:
    """Return epoch ts of the most-recent assistant turn that's a rate-limit
    API-error (or None if no such turn in the recent slice).

    Identifies the error by structural flags first (isApiErrorMessage + error
    == 'rate_limit'), then falls back to text-content regex if the structured
    flags aren't set on this Claude Code version.
    """
    cmd = _sudo(sudo_user) + [
        "bash",
        "-c",
        f"tac {jsonl_path} | head -n 500",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_JSONL_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "assistant":
            continue
        structured_match = (
            obj.get("isApiErrorMessage") is True
            and obj.get("error") == "rate_limit"
        )
        text_match = False
        if not structured_match:
            msg = obj.get("message")
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            t = block.get("text")
                            if isinstance(t, str) and RATE_LIMIT_REGEX.search(t):
                                text_match = True
                                break
        if not (structured_match or text_match):
            continue
        ts_str = obj.get("timestamp")
        if not isinstance(ts_str, str):
            continue
        try:
            # Claude Code emits ISO with ms + Z; fromisoformat accepts after py3.11.
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return None


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def check_p1a(cfg: dict, idle_threshold_seconds: int) -> bool:
    """P1.a: idle >threshold + queue ≥1 actionable HO-target. Returns True if both.

    Activity signals (max across all) — must be Orchestrator-specific (T-0283):
      - latest ledger entry where agent=<ledger_agent_id> (already agent-filtered)
      - mtime of latest session jsonl (Orchestrator's own session writes)

    Dropped `latest_commit_ts` per T-0283: it was repo-wide (any author),
    so Recovery-side commits (ticket filings, editorial fixes) made Orchestrator
    look active even when Orchestrator was idle.
    The remaining two signals are both Orchestrator-tagged.

    T-0644: when HO's latest ledger activity was an explicit queue
    terminal-state audit (`*terminal_state*` or `*queue_audit*` action),
    use the longer ACTIVITY_MONITOR_P1A_TERMINAL_STATE_IDLE_SECONDS
    threshold instead of the normal one. HO already wrote down "queue is
    in terminal state, nothing actionable"; repeat fires within the next
    ~4hr would just re-bill HO for the same acknowledgement.
    """
    ledger_ts = latest_ledger_entry_ts_for_agent(
        cfg["pantheon_config_path"], cfg["sudo_user"], cfg["ledger_agent_id"]
    ) or 0
    # T-2387: LIVENESS site — an error turn must not count as activity.
    jsonl_ts = liveness_jsonl_mtime(
        cfg["jsonl_dir"], cfg["sudo_user"], cfg.get("agent", "?")) or 0
    latest_activity = max(ledger_ts, jsonl_ts)
    if latest_activity == 0:
        # Can't determine activity → fail-open (don't fire).
        return False
    # T-0644 stretched threshold: if the most recent agent-tagged ledger
    # entry is a terminal-state ack and it's also the most recent activity
    # (i.e. ack hasn't been superseded by a fresher jsonl write), use the
    # stretched threshold.
    threshold = idle_threshold_seconds
    terminal_ts = latest_terminal_state_ledger_ts_for_agent(
        cfg["pantheon_config_path"], cfg["sudo_user"], cfg["ledger_agent_id"]
    )
    if (
        terminal_ts is not None
        and terminal_ts >= ledger_ts
        and terminal_ts >= jsonl_ts
    ):
        threshold = int(os.environ.get(
            "ACTIVITY_MONITOR_P1A_TERMINAL_STATE_IDLE_SECONDS",
            ACTIVITY_MONITOR_P1A_TERMINAL_STATE_IDLE_SECONDS,
        ))
    idle_seconds = int(time.time()) - latest_activity
    if idle_seconds < threshold:
        return False
    queue_count = count_actionable_tickets(
        cfg["pantheon_config_path"], cfg["ticket_targets"], cfg["sudo_user"],
        queue_states=cfg.get("queue_states", ("inbox", "active")))
    return queue_count >= 1


def agent_committed_recently(
    repo_path: Path, sudo_user: str, author: str, within_seconds: int
) -> bool:
    """T-3709: True iff `author` has a git commit in `repo_path` (that agent's
    own pantheon-config clone) within the last `within_seconds`, by commit date.

    Signals the agent is actively producing git-visible OUTPUT — a busy session,
    not an idle-waiting one. Author-filtered to the agent's persona name so a
    commit merely PULLED from another agent (peer clones carry others' commits)
    does not count as this agent's work; only its own commits do. Deliberately
    NOT scoped to a path subset — a commit to tools/ / adr/ / sop/ is just as
    much active work as one to ledger/ or tickets/, and over-narrowing would
    re-introduce the false-positive.

    Fail-CLOSED toward 'not recently' (False) on any error/timeout: a git
    failure must PRESERVE the P1.b fire safety-net (poke fires), never silence
    it. A genuinely-wedged agent cannot commit, so this cannot mask a real wedge.
    """
    cmd = _sudo(sudo_user) + [
        "git", "-C", str(repo_path), "log", "-1",
        f"--author={author}", f"--since={within_seconds} seconds ago",
        "--format=%H",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_JSONL_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


def p1b_idle_reading(cfg: dict, agent: str | None = None) -> "harness_idle.IdleReading":
    """T-5793: the seat's idle reading from its live harness's session log, read as
    the seat's account through the SudoReader. Never raises."""
    user = str(cfg.get("sudo_user") or "")
    name = agent or str(cfg.get("bridge_inbound_key") or "")
    return harness_idle.read_idle(
        name, account=user, home=f"/home/{user}", cwd=None,
        default_harness=str(cfg.get("active_runtime_harness") or "claude-code"),
        reader=_seat_reader(), quiet_s=P1B_LOG_QUIET_SECONDS,
        claude_projects_dir=str(cfg["jsonl_dir"]))


def check_p1b(
    cfg: dict,
    bridge_url: str,
    operator_absent_threshold_seconds: int,
    *,
    agent: str | None = None,
) -> bool:
    """P1.b: the operator absent + HO in await-state + queue.

    T-5793 (ACTIVITY_MONITOR_IDLE_SOURCE=log, the default): the await-state is
    harness_idle's IDLE verdict with quiet >= P1B_LOG_QUIET_SECONDS, read from the
    live harness's own session log (codex rollout or Claude transcript) through the
    SudoReader. It replaces Signal 1 below, and also works on codex. Signal 2 (a
    user-blocking tool_use) no longer pokes here: it is escalated to Recovery by
    check_p1b_interactive + main(). Every other gate is unchanged.

    ACTIVITY_MONITOR_IDLE_SOURCE=pane keeps the pre-T-5793 await-state, which has
    two sources:
      1. Structural (T-3071): latest assistant turn's final content-block is
         text, not tool_use — see latest_assistant_ends_without_tool_use.
      2. Latest assistant turn is a user-blocking tool_use (AskUserQuestion
         etc., T-0297).

    operator-absent detection (Gate-1) reads HO's own jsonl for the most-recent
    `plugin:telegram:telegram` inbound marker (T-3075 rewire, 2026-08-22 —
    replaces the T-0223 bridge-health path retired 10wk earlier by the T-0767
    pantheon-bridge decom, which had silently fail-open'd the whole gate).
    The `bridge_url` param is accepted for signature-compat with the fire
    site + legacy env-override but no longer consulted.
    """
    del bridge_url  # signature-preserved; no longer consulted (T-3075)
    jsonl = find_latest_jsonl(cfg["jsonl_dir"], cfg["sudo_user"])
    if jsonl is None:
        return False
    latest_inbound_ts = latest_telegram_inbound_ts(jsonl, cfg["sudo_user"])
    if latest_inbound_ts is None:
        # Fail-CLOSED (T-3075): no verifiable operator-inbound = TREAT as absent.
        # Bias toward safety-net-fires-when-uncertain rather than the prior
        # silent bail. Signals 1-2 + queue-count still gate the actual poke —
        # this only says "don't bail on Gate-1 alone."
        absent_seconds = operator_absent_threshold_seconds  # exactly at threshold
    else:
        absent_seconds = int(time.time()) - latest_inbound_ts
    if absent_seconds < operator_absent_threshold_seconds:
        return False
    if idle_source() == "pane":
        # Signal 1: structural await (T-3071)
        structural_await = latest_assistant_ends_without_tool_use(jsonl, cfg["sudo_user"])
        # Signal 2: interactive tool_use await (T-0297)
        interactive_tool = latest_assistant_interactive_tool_use(jsonl, cfg["sudo_user"])
        tool_await = interactive_tool is not None
        if not (structural_await or tool_await):
            return False
    else:
        # T-5793: the turn ended and nothing happened for P1B_LOG_QUIET_SECONDS. IDLE
        # already implies quiet >= quiet_s; WORKING / BG_WAIT / UNKNOWN do not fire.
        reading = p1b_idle_reading(cfg, agent)
        if reading.verdict is not harness_idle.Verdict.IDLE \
                or (reading.quiet_s or 0) < P1B_LOG_QUIET_SECONDS:
            return False
    queue_count = count_actionable_tickets(
        cfg["pantheon_config_path"], cfg["ticket_targets"], cfg["sudo_user"],
        queue_states=cfg.get("queue_states", ("inbox", "active")))
    if queue_count < 1:
        return False
    # T-3404: honor the T-0644 terminal-state ack for P1.b too (was P1.a-only).
    # If the HO's latest LEDGER activity is a terminal-state/queue_audit ack and
    # it's within the suppression window, suppress — the HO has structurally
    # acknowledged the queue is terminal (everything gated / on the operator's desk).
    # LEDGER-gated, deliberately NOT jsonl-gated: the intervening "hold stable"
    # text turns that a P1.b poke provokes don't write the ledger, so they can't
    # lift the suppression — that is exactly what stops the ~15min
    # poke→ack→poke churn (T-3404). It is superseded by: (a) the operator's return
    # (the absent-gate above already suppresses then), (b) the 4hr window expiry
    # (a fresh "still holding?" re-check), or (c) real new ledger work by the HO
    # (a non-terminal ledger entry → terminal_ts < ledger_ts). New inbound work
    # still reaches the HO via the inbox-consumer regardless of this poke.
    terminal_ts = latest_terminal_state_ledger_ts_for_agent(
        cfg["pantheon_config_path"], cfg["sudo_user"], cfg["ledger_agent_id"]
    )
    if terminal_ts is not None:
        ledger_ts = latest_ledger_entry_ts_for_agent(
            cfg["pantheon_config_path"], cfg["sudo_user"], cfg["ledger_agent_id"]
        ) or 0
        window = int(os.environ.get(
            "ACTIVITY_MONITOR_P1A_TERMINAL_STATE_IDLE_SECONDS",
            ACTIVITY_MONITOR_P1A_TERMINAL_STATE_IDLE_SECONDS,
        ))
        if terminal_ts >= ledger_ts and (int(time.time()) - terminal_ts) < window:
            return False
    # T-3709: AND-NOT recent-write-activity. The gates above establish
    # "operator-absent + await-shaped-turn + non-empty queue" — but a session
    # doing continuous real work looks identical to an idle-waiting one through
    # the operator-inbound gate (the operator asleep says nothing about the agent's
    # productivity). If the agent has committed git-visible output within the
    # write-activity window, it is working, not idle-waiting → suppress. Gates on
    # OUTPUT (commits), NOT transcript (thinking/tool_use fire constantly and are
    # too noisy). Bounded: a busy-then-genuinely-idle agent is poked at most
    # ~N min after its last commit (next cycle once the window lapses).
    write_window = int(os.environ.get(
        "ACTIVITY_MONITOR_P1B_WRITE_ACTIVITY_SECONDS",
        ACTIVITY_MONITOR_P1B_WRITE_ACTIVITY_SECONDS,
    ))
    if agent_committed_recently(
        cfg["pantheon_config_path"],
        cfg["sudo_user"],
        cfg["bridge_inbound_key"],
        write_window,
    ):
        return False
    return True


def interactive_await_tool(records: list, now: float,
                           min_age_seconds: int = P1B_LOG_QUIET_SECONDS) -> str | None:
    """Pure: the user-blocking tool (USER_BLOCKING_TOOL_NAMES) the session is blocked
    on, or None. Blocked = the NEWEST user/assistant record is an assistant record
    carrying that tool_use (Claude Code writes one record per content block), so no
    tool_result has come back, and it is at least `min_age_seconds` old."""
    for rec in reversed(records):
        if not isinstance(rec, dict) or rec.get("type") not in ("user", "assistant"):
            continue
        if rec.get("type") != "assistant":
            return None
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        content = msg.get("content")
        if not isinstance(content, list):
            return None
        for block in content:
            if (isinstance(block, dict) and block.get("type") == "tool_use"
                    and block.get("name") in USER_BLOCKING_TOOL_NAMES):
                ts = harness_idle._parse_ts(rec.get("timestamp"))
                if ts is None or now - ts < min_age_seconds:
                    return None
                return str(block["name"])
        return None
    return None


def check_p1b_interactive(cfg: dict, operator_absent_threshold_seconds: int, *,
                          agent: str | None = None, now: float | None = None) -> str | None:
    """T-5793: P1.b's former Signal 2 (T-0297) as an escalation trigger. Returns the
    blocking tool name when the operator has been absent >= the P1.b threshold (Gate-1,
    unchanged) AND the seat's live Claude transcript ends in an unanswered
    AskUserQuestion/EnterPlanMode at least P1B_LOG_QUIET_SECONDS old; else None.

    Only Claude has these tools, so a codex runtime returns None. Deliberately NOT
    gated on the queue, the terminal-state ack or recent commits: those gates bound
    POKE churn on an idle seat, while a seat blocked on a question is worth one
    transition-bounded alert whatever its queue. Never raises (None)."""
    try:
        name = agent or str(cfg.get("bridge_inbound_key") or "")
        if _active_runtime_harness(name, cfg) != "claude-code":
            return None
        jsonl = find_latest_jsonl(cfg["jsonl_dir"], cfg["sudo_user"])
        if jsonl is None:
            return None
        latest_inbound_ts = latest_telegram_inbound_ts(jsonl, cfg["sudo_user"])
        t = time.time() if now is None else now
        absent = (operator_absent_threshold_seconds if latest_inbound_ts is None
                  else int(t) - latest_inbound_ts)
        if absent < operator_absent_threshold_seconds:
            return None
        user = str(cfg["sudo_user"])
        reader = _seat_reader()
        path = harness_idle.locate_log("claude-code", user, f"/home/{user}", None,
                                       reader=reader, claude_projects_dir=str(cfg["jsonl_dir"]))
        if path is None:
            return None
        text = reader.read_text_tail(user, str(path), harness_idle.TAIL_BYTES)
        if not text:
            return None
        records = harness_idle.parse_jsonl(
            text, truncated=len(text.encode("utf-8", errors="replace")) >= harness_idle.TAIL_BYTES)
        return interactive_await_tool(records, t)
    except Exception:  # noqa: BLE001 — a probe fault must never wedge the monitor
        return None


def _rearm_interactive_await(state_path: Path) -> None:
    """Re-arm the interactive-await alert once the seat is no longer blocked: drop the
    escalated flag from a p1b-interactive state record, keeping its ts so this never
    starts a fresh cooldown. Best-effort."""
    prior = read_poke_state(state_path)
    if not prior or prior.get("predicate") != INTERACTIVE_AWAIT_PREDICATE \
            or not prior.get("escalated"):
        return
    prior.pop("escalated", None)
    try:
        state_path.write_text(json.dumps(prior) + "\n", encoding="utf-8")
    except OSError:
        pass


def check_p1d(cfg: dict, age_threshold_seconds: int) -> bool:
    """P1.d: pane shows rate-limit + jsonl confirms latest rate-limit assistant
    turn is older than threshold.

    Capture-pane is the primary signal — the pane is what the user sees and
    reflects what the agent's runtime considers "current". The jsonl scan
    provides the timestamp + structural confirmation. Both gates must pass.

    Per the operator msg_1125 — capture-pane is sufficient for the visible-state
    signal; jsonl is used here only to time the staleness.
    """
    pane_text = capture_pane(cfg["tmux_target_pane"], cfg["sudo_user"])
    if pane_text is None:
        return False  # fail-open
    if not detect_rate_limit_in_pane(pane_text):
        return False
    jsonl = find_latest_jsonl(cfg["jsonl_dir"], cfg["sudo_user"])
    if jsonl is None:
        return False
    rate_limit_ts = latest_rate_limit_assistant_ts(jsonl, cfg["sudo_user"])
    if rate_limit_ts is None:
        return False
    age = int(time.time()) - rate_limit_ts
    return age >= age_threshold_seconds


def check_p1c(
    cfg: dict,
    stale_threshold_seconds: int,
    require_fresh_teammate: bool = True,
) -> bool:
    """P1.c-narrow (ORG-037 Layer 3): active-team + dispatcher-turn-firing-stopped.

    The narrow "forgot-to-arm-Layer-1" residual msg_1326's revert left
    uncovered (proven by the 2026-06-25 orchestrator-stall incident). Fires iff
    ALL hold:

      1. active-team — cfg["teams_dir"]/*/inboxes exists (T-1290 signal).
      2. dispatcher dormant — now − max(main-jsonl mtime, latest ledger ts)
         > stale_threshold. This is the no-pending-wakeup proxy UNIFIED with
         no-activity: a working Layer-1 fires turns that advance the main jsonl,
         so a frozen main jsonl means no turn fired (Layer-1 absent or broken) —
         strictly better than literal wakeup-introspection (unobservable
         cross-account; ScheduleWakeup is session-scoped/in-memory).
      3. (§7 sharpening, default on) a teammate artifact mtime is NEWER than the
         dispatcher's main-jsonl mtime — positive evidence of an unreported
         sub-result, eliminating the legit-idle-with-stale-teams-dir false fire
         (the dispatcher's own dispatch writes are ≤ its last turn, so they
         don't trip it; only a sub-result landing AFTER its last turn does).

    Recovery is a POKE, not a restart (healthy-but-dormant session, not a
    wedge). Strictly narrower than the reverted P1.c: never inspects individual
    workers' silence; watches only the dispatcher's own turn-firing. Fail-CLOSED
    on every probe failure (never manufacture a fire).
    See notes/2026-06-26-layer3-recovery-backstop-design.md.
    """
    teams_dir = cfg.get("teams_dir")
    if teams_dir is None:
        return False  # agent not configured for Layer 3 → never fire
    # Conjunct 1: active team.
    if not active_team_inboxes(teams_dir, cfg["sudo_user"]):
        return False
    # Conjunct 2: dispatcher dormant (no-pending-wakeup proxy + no-activity).
    # T-2387: LIVENESS site — a dispatcher erroring every wake is dormant, not busy.
    jsonl_ts = liveness_jsonl_mtime(
        cfg["jsonl_dir"], cfg["sudo_user"], cfg.get("agent", "?")) or 0
    ledger_ts = latest_ledger_entry_ts_for_agent(
        cfg["pantheon_config_path"], cfg["sudo_user"], cfg["ledger_agent_id"]
    ) or 0
    latest_activity = max(jsonl_ts, ledger_ts)
    if latest_activity == 0:
        return False  # can't determine activity → fail-open (don't fire)
    if (int(time.time()) - latest_activity) < stale_threshold_seconds:
        return False
    # Conjunct 3 (§7 sharpening): a teammate artifact advanced AFTER the
    # dispatcher's last main-session turn → a plausibly-dropped sub-result.
    if require_fresh_teammate:
        newest_team = newest_team_artifact_mtime(teams_dir, cfg["sudo_user"])
        if newest_team is None or newest_team <= jsonl_ts:
            return False
    return True


# ---------------------------------------------------------------------------
# Poke
# ---------------------------------------------------------------------------


P1A_POKE_PROMPT = (
    "[activity-monitor P1.a] You appear idle (no commit/Telegram/ledger output"
    " in >1hr) but your queue has >=1 actionable item. Check tickets/inbox/ +"
    " tickets/active/ for target=orchestrator and pull the next"
    " completable item."
)

P1B_POKE_PROMPT = (
    "[activity-monitor P1.b] You appear to be in self-imposed wait-state while"
    " the operator is away (>15min since his last inbound). Per"
    " sop/standard/ho-absent-operator-keep-working.md: (1) if you have an active"
    " team (`ls ~/.claude/teams/*/inboxes/`), check each teammate's inbox mtime"
    " against role-thresholds (orch 30min / impl 15min / rev 5min) and status-"
    " check any that exceeded — grandparent-routing / wake-hook drops are the"
    " top cause of this fire firing while you feel legitimately-awaiting; (2)"
    " record progress on the awaited item, shelve it (ticket.py block <id>"
    " --not-before/--depends-on — ORG-019; the old --reason flag was retired"
    " with blocked/), and swap to the next actionable; (3) any operator-blocked"
    " asks become target=operator tickets, never idle-wait-on-chat. Pull queue"
    " now."
)

P1D_RETRY_PROMPT = (
    "[activity-monitor P1.d] Your previous turn appears to have been aborted by"
    " an API rate-limit error (server-side throttle, not the 5h cap). Retry the"
    " response you were composing. If the rate-limit persists, surface a tight"
    " status to the operator; otherwise resume normally."
)

P1C_POKE_PROMPT = (
    "[liveness backstop] Your session has an active team but has fired no turn"
    " for >30min. Ground-truth each in-flight dispatch now — check the"
    " dispatchee's inbox mtime + repo/PR/ticket state — then either re-poke via"
    " the reliable direction (spawner->subagent SendMessage /"
    " task-notification-to-spawner) or proceed if the await already resolved."
    " Re-arm your Layer-1 self-wakeup before going idle again. If the team is"
    " actually done, clean up ~/.claude/teams/*."
)


def send_poke(cfg: dict, prompt: str, *, harness: str | None = None) -> bool:
    """Send the poke prompt to the agent's tmux pane.

    T-0538: delegates to the target user's tools/send_keys_submit.sh via
    sudo -nu <sudo_user>. The helper handles text send, paste-mode settle,
    Enter send, and submit verification with retry. XDG_RUNTIME_DIR is set
    in the bash -c string so the helper's tmux calls reach the target user's
    session bus (same pattern as capture_pane()). Returns True iff helper
    exits 0; False on any failure (send-keys error, timeout, OSError).

    T-5793: the helper gets `--harness <profile>` for the seat's LIVE runtime
    (callers pass _active_runtime_harness(agent, cfg)). Without it a Claude-configured seat
    failed over to codex got the claude-code verify profile, whose sentinel is
    always absent on codex's TUI, so the submit false-passed (the T-1106 class).
    """
    pane = cfg["tmux_target_pane"]
    sudo_user = cfg["sudo_user"]
    helper = Path(cfg["pantheon_config_path"]) / "tools" / "send_keys_submit.sh"
    # Callers pass the resolved profile; the fallback is the cfg-declared harness, else
    # claude-code (the helper's own default, i.e. the pre-T-5793 behaviour).
    profile = harness or str(cfg.get("active_runtime_harness") or "claude-code")
    # T-4141: shlex.quote, NOT json.dumps. json.dumps yields a DOUBLE-quoted shell
    # string, so backticks and $(...) in the prompt were executed as the target user
    # (P1B_POKE_PROMPT's backticked ls arrived in the pane as '()').
    cmd = _sudo(sudo_user) + [
        "bash", "-c",
        f'XDG_RUNTIME_DIR=/run/user/$(id -u) INJECT_ENTER_DELAY={INJECT_ENTER_DELAY}'
        f' bash {shlex.quote(str(helper))} --harness {shlex.quote(profile)}'
        f' {shlex.quote(pane)} {shlex.quote(prompt)}',
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=_TMUX_TIMEOUT_SECONDS
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _pane_idle_probe(cfg: dict) -> bool | None:
    """THREE-state probe of the agent's pane for the poke busy-guard (T-3599):
    True = idle at a prompt, False = BUSY (generating, OR a background agent is
    still running), None = UNKNOWN. T-4284: input DOES submit once the main turn
    has ended with a background agent running, and mail delivery now wakes that
    state. But a P1.b poke is a NUDGE, and an agent waiting on its own delegated
    work is not in a self-imposed wait, so this probe passes
    background_agents_busy=True and keeps T-4087's skip.

    Classification is DELEGATED to inbox_consumer.pane_idle_state — the same
    single-owner the mailbox_wake busy-defer consults (T-2681/T-3554) — so the two
    poke/wake consumers cannot drift apart on the sentinel set (the exact drift
    T-2171 had to fix in keep_going). Part A (merged @ad76818457) taught that
    classifier to read the manage-view footer as BUSY (T-4087 and T-4284 later
    narrowed that to background AGENTS, and only for nudge callers); this probe is
    how activity_monitor finally CONSULTS it.

    Fail-open to None on any capture/import/classify failure: a flaky probe must
    never, by itself, skip a needed poke or swallow a real send-failure — each
    caller picks the safe direction for None (guard-1 pokes on None, guard-2 keeps
    exit-1 on None). Same contract inbox_consumer.pane_idle_state documents.
    """
    pane = cfg.get("tmux_target_pane")
    if not pane:
        return None
    pane_text = capture_pane(pane, cfg["sudo_user"], escapes=True)
    if pane_text is None:
        return None
    try:
        try:
            from tools.inbox_consumer import pane_idle_state  # run-as-package / tests
        except ImportError:
            from inbox_consumer import pane_idle_state  # run-as-script (prod)
        return pane_idle_state(pane_text, background_agents_busy=True)
    except Exception:  # noqa: BLE001 — best-effort signal; UNKNOWN on any error
        return None


def _inject_safe(cfg: dict, agent: str, harness: str) -> bool:
    """T-5793: positive evidence that a TEXT poke may be pasted right now:
    harness_idle.pane_inject_safe on a fresh `-e` capture for the live harness
    (claude: pane_idle_state(..., background_agents_busy=True) is True; codex: ready
    composer, no codex modal, no busy footer). An uncapturable pane is not safe.
    Applied only immediately before send_poke, never at the top of _dispatch_poke,
    so the recovery branches (cap-modal Enter-confirm, escalations) stay reachable."""
    pane = cfg.get("tmux_target_pane")
    if not pane:
        return False
    return harness_idle.pane_inject_safe(capture_pane(pane, cfg["sudo_user"], escapes=True), harness)


def _gate_text_poke(cfg: dict, agent: str, predicate: str) -> str | None:
    """The harness for a text poke, or None when the poke must DEFER (log mode only:
    the pane is not positively inject-safe). pane mode keeps the pre-T-5793 send."""
    harness = _active_runtime_harness(agent, cfg)
    if idle_source() == "pane" or _inject_safe(cfg, agent, harness):
        return harness
    _log(agent, f"{predicate}: pane not positively inject-safe for {harness} "
                f"(modal / dialog / busy / unknown) — deferring text poke (T-5793)")
    _record_fire(agent, predicate, deferred=True)
    return None


def _soft_defer_or_fail(cfg: dict, agent: str, predicate: str) -> int:
    """T-3599 send-fail soft-defer: called only after send_poke() has FAILED.
    Re-probe the pane and return 0 (soft-defer) IFF it is now DEFINITIVELY busy
    (False) — a generating pane queues-not-submits, and a pane whose main turn
    ended with a background agent running is deliberately not nudged (T-4284). So
    the send lost nothing (the mail it re-injects stays queued), the state
    self-clears, and a return-1 there just mislabels a benign busy pane as a
    unit-failure (OnFailure -> recovery).

    A genuinely IDLE pane that rejected the submit (True) is the real T-2245
    send-liveness anomaly, and an UNKNOWN re-probe (None) is NOT proof-of-busy:
    both keep exit-1 so a real wedge still alarms. ("Cannot tell" must not stand in
    for "not a problem" — feedback_unknown_must_not_stand_in_for_no.)"""
    if _pane_idle_probe(cfg) is False:
        _log(agent, f"{predicate}: send failed but pane BUSY "
                    f"(generating/manage-view) — soft-defer, not exit-1 (T-3599)")
        return 0
    return 1


# ---------------------------------------------------------------------------
# Escalation (T-1363 inc.3): when a poke is SUPPRESSED because the session is
# stuck (cyber-blocked, or N ineffective pokes with a frozen jsonl), don't just
# log — route the stuck-remedy to Recovery. Bounded: alert ONCE per escalation episode
# (on the False->True escalated transition), NOT every cycle (else the alert
# becomes the same unbounded-repeat the poke bounding was meant to kill).
# ---------------------------------------------------------------------------


def _route_failure_alert(agent: str, msg: str, vocab: str) -> None:
    """Route a stuck-session failure to Recovery (others->Recovery per
    feedback_failure_alert_routing_recovery_vs_self). Import-wrapped so tests patch it."""
    try:
        from tools.inbox_consumer import route_failure_alert  # run-as-package / tests
    except ImportError:
        from inbox_consumer import route_failure_alert  # run-as-script (prod)
    route_failure_alert(agent, msg, vocab)


# ---------------------------------------------------------------------------
# T-3558(b1): wedge-specimen capture.
#
# With auto-poke armed (T-3557c), _escalate_stuck fires only when a poke was
# INEFFECTIVE — i.e. exactly at a live wedge. That is the one moment we can grab
# a REAL pane capture of the wedge class (preview-chip / permission-modal / ...),
# which the T-3558(b2) classifiers need as a specimen (the classify_cap_modal
# design forbids hand-typed fixtures). SHIP-DARK, flag-gated (default off) so it
# is a deliberate, reversible arming and so existing escalation tests do not shell
# out to live tmux. STATE-DIR, never the repo tree (no git noise); a captured
# specimen is PROMOTED to tools/testdata/ by hand when (b2) is built. Fail-open in
# every limb — a capture failure must never perturb the escalation.
# ---------------------------------------------------------------------------
_WEDGE_SPECIMEN_KEEP = int(os.environ.get("ACTIVITY_MONITOR_WEDGE_SPECIMEN_KEEP", "20"))


def _wedge_specimen_dir() -> Path:
    """State-dir specimens path — mirrors `_fires_log_path`'s PANTHEON_STATE_DIR
    convention so tests redirect it the same way."""
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / "activity_monitor" / "specimens"


def _capture_wedge_specimen(cfg: dict, agent: str, vocab: str) -> tuple[Path | None, str | None]:
    """Best-effort: save the current pane capture as a wedge specimen. Returns
    (path, wedge_class) — the saved file's path and the detected wedge class
    (classify_wedge: permission modal or focused preview chip), either of which
    may be None. Fail-open — any
    capture/write/prune error is swallowed (logged) so it can never perturb the
    escalation it rides on. Only runs when ACTIVITY_MONITOR_WEDGE_SPECIMEN_CAPTURE
    is truthy (checked by the caller). The wedge_class lets the caller name the
    class in the Recovery-facing alert (T-3558b2) off this SAME capture — no second
    capture_pane on the escalation path."""
    try:
        pane = cfg.get("tmux_target_pane")
        if not pane:
            return (None, None)
        text = capture_pane(pane, cfg.get("sudo_user"))
        if not text:
            return (None, None)  # nothing to preserve (capture failed / empty)
        # T-3558(b2): name the wedge class in the specimen if we recognise it, so
        # the saved file + log line carry the diagnostic (which permission was
        # blocking, or which preview chip holds the Enter). DETECTION ONLY:
        # nothing here answers a prompt or clears a chip.
        wedge_class = classify_wedge(text)
        d = _wedge_specimen_dir()
        d.mkdir(parents=True, exist_ok=True)
        # Microsecond stamp: readable AND collision-free even for two wedges in
        # the same second (a 1s stamp would overwrite the earlier one).
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        path = d / f"{agent}_{vocab}_{stamp}.txt"
        header = f"# detected-class: {wedge_class}\n" if wedge_class else ""
        path.write_text(header + text)
        # Bounded: keep only the newest _WEDGE_SPECIMEN_KEEP files (wedges are
        # rare, but a diagnostic sidecar must not grow without limit).
        specimens = sorted(d.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in specimens[_WEDGE_SPECIMEN_KEEP:]:
            try:
                stale.unlink()
            except OSError:
                pass
        _log(agent, f"wedge-specimen saved (class={wedge_class or 'unclassified'}): {path}")
        return (path, wedge_class)
    except Exception as exc:  # noqa: BLE001 — specimen capture must never wedge escalation
        _log(agent, f"wedge-specimen capture failed (ignored): {exc!r}")
        return (None, None)


def _escalate_stuck(cfg: dict, agent: str, predicate: str, msg: str, vocab: str,
                    state_path: Path, cur_mtime: int, consecutive: int) -> None:
    """Suppress-and-escalate: record the escalated state and, ONLY on the
    transition into escalated (not on every subsequent cycle), route the alert to
    Recovery. Alert-routing failure never raises (must not wedge the monitor)."""
    prior = read_poke_state(state_path) or {}
    already = bool(prior.get("escalated")) and prior.get("predicate") == predicate
    # T-3072: fire-tracking is keyed on VOCAB, not the triggering `predicate`
    # param — cap-modal / spend-cap / cyber-refusal / stuck-poke / tui-wedge
    # each track drift independently rather than being smeared under
    # whichever predicate happened to trigger this dispatch. Recorded on
    # EVERY call (not gated by `already`) — the underlying condition is still
    # observably firing each cycle even while the alert itself stays quiet.
    _record_fire(agent, vocab)
    if not already:
        # T-3558(b1): grab a real pane specimen of the wedge at onset (flag-gated,
        # fail-open). BEFORE alert-routing so a specimen is captured even if
        # routing later raises; its own try/except means it can never itself wedge.
        # T-3558(b2): reuse that same capture's classification to NAME the wedge
        # class in the Recovery-facing alert (e.g. "permission modal for Read(//tmp/**)")
        # so the escalation is actionable on sight — no second capture_pane, and
        # gated by the same flag so unarmed seats (orchestrator) never shell out here.
        detected_class = None
        if os.environ.get(
            "ACTIVITY_MONITOR_WEDGE_SPECIMEN_CAPTURE", ""
        ).strip().lower() in ("1", "true", "yes", "on"):
            _, detected_class = _capture_wedge_specimen(cfg, agent, vocab)
        if detected_class and detected_class.startswith(f"{PREVIEW_CHIP}:"):
            hint = "a focused preview chip holds the Enter; clear it by hand (x, then Enter)"
        else:
            hint = "do not auto-answer a permission prompt"
        alert_msg = msg if not detected_class else (
            f"{msg}\n[activity_monitor T-3558: detected wedge class → {detected_class} "
            f"— DIAGNOSTIC ONLY, verify the pane; {hint}]"
        )
        try:
            _route_failure_alert(agent, alert_msg, vocab)
            _log(agent, f"{predicate}: ESCALATED to Recovery (route_failure_alert) — {alert_msg[:80]}")
        except Exception as exc:  # noqa: BLE001 — escalation must never wedge the monitor
            _log(agent, f"{predicate}: escalation alert routing failed {exc!r}")
    else:
        _log(agent, f"{predicate}: already escalated — not re-alerting (bounded)")
    write_poke_state(
        state_path, agent, predicate,
        pre_poke_jsonl_mtime=cur_mtime,
        consecutive_ineffective=consecutive, escalated=True,
    )


# ---------------------------------------------------------------------------
# T-2632 PART 2: park-attribution (asserted-only core)
#
# Distinguish a seat that is QUIET-BY-DESIGN (parked) from one that is WEDGED,
# so quiet-by-design does not read as a wedge. This is the reusable CORE — a pure,
# fail-safe query. WIRING it into the wedge disposition (activity_monitor's
# suppress/_escalate_stuck decision + the context-fill staleness wedge) is the
# gated fresh-context step (T-2632): a caller uses (attributable → suppress the
# escalate-alert; unattributed → escalate-to-Recovery as today), and MUST NOT let
# attribution gate an auto-restart.
#
# Why asserted-only: the T-2632 investigation found the annotation's INFERRED
# from-a-live-handle attribution is not safely implementable with today's
# observables — session-level handles (Task/ScheduleWakeup/Monitor) are
# harness-internal (not disk-queryable cross-account), and the only queryable
# "timer" surface is the fleet systemd timers, which are ALWAYS present with a
# pending next-fire, so keying off them would attribute EVERY quiet and mask
# EVERY wedge. The inferred half is blocked on a harness-exposed session-handle
# registry. This core therefore consults only DECLARED signals.
# ---------------------------------------------------------------------------


def _hold_signal(agent: str, now: float | None) -> tuple[bool, str] | None:
    """Asserted-hold signal: an active, TTL-bounded context_fill_hold means the
    seat's quiet is a declared hold. Import-wrapped (run-as-package / run-as-script)
    and fail-safe — any error returns None ('no signal', so the wedge proceeds)."""
    try:
        try:
            from tools.context_fill_hold import hold_status  # package / tests
        except ImportError:
            from context_fill_hold import hold_status  # script (prod)
        st = hold_status(agent, now=now)
        if st.get("active"):
            return (True, f"context_fill_hold active (reason={st.get('reason') or 'unspecified'})")
    except Exception:
        return None
    return None


def quiet_is_attributable(
    agent: str,
    *,
    now: float | None = None,
    signals=None,
) -> tuple[bool, str]:
    """Is this seat's QUIET attributable to a DECLARED intentional-silence signal?
    Returns (attributable, reason).

    Asserted sources (declared intent, NOT inferred):
      1. an active context_fill_hold (TTL-bounded operator/agent hold);
      2. [wiring pass] the seat's session-owning systemd unit DISABLED — the
         TG-6694 deliberate pause — supplied via `signals` (mirrors
         channel_liveness_monitor._is_deliberately_paused, which resolves the
         unit name + sudo_user this pure core deliberately does not carry).

    `signals` is an iterable of callables `(agent, now) -> (bool, reason) | None`,
    evaluated after the built-in hold check; the first truthy result wins. It is
    the extension point the wiring pass uses to add the unit-disabled probe (and,
    eventually, a harness session-handle signal) without changing this contract.

    FAIL-SAFE — the load-bearing invariant (T-2632 design #2 / T-2615 'a hold must
    never mask a real wedge'): any error or ambiguity yields (False, ...). 'Not
    attributable' means the wedge signal PROCEEDS to alert, so this can never mask
    a wedge. Callers use the result ONLY to downgrade a wedge's DISPOSITION
    (escalate-alert vs suppress-alert), NEVER to gate an auto-restart.
    """
    checks = [_hold_signal]
    if signals:
        checks.extend(signals)
    for check in checks:
        try:
            result = check(agent, now)
        except Exception:
            result = None  # fail-safe: a broken signal never attributes
        if result and result[0]:
            return (True, result[1])
    return (False, "unattributed — no declared hold/pause explains the quiet")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


# T-4141: the pokes this monitor injects. If one never submitted it sits in the
# composer; flush it with a bare Enter (bounded) instead of stacking another on top.
STUCK_POKE_PROMPTS = (P1A_POKE_PROMPT, P1B_POKE_PROMPT, P1C_POKE_PROMPT)
MAX_STUCK_POKE_REPRESSES = 3


def _flush_stuck_poke(cfg: dict, agent: str, state_path: Path) -> bool:
    """True iff an earlier poke was found stuck in the composer and ONE Enter was
    sent (the caller then skips this cycle's poke). The decision is
    submit_verify.should_repress: our poke text is still in the composer, and the
    pane is not generating, not running a background agent, and not on a modal.
    Bounded at MAX_STUCK_POKE_REPRESSES per stuck episode; the counter resets once
    no stuck poke is seen. Fail-safe: any capture/import error means no keystroke."""
    counter = Path(str(state_path) + ".stuck_repress")
    try:
        try:
            from tools import submit_verify as sv  # run-as-package / tests
        except ImportError:
            import submit_verify as sv  # run-as-script (prod)
        pane_text = capture_pane(cfg["tmux_target_pane"], cfg["sudo_user"])
        profile = sv.get_profile("claude-code")
        stuck = bool(pane_text) and any(
            sv.should_repress(profile, pane_text, p) for p in STUCK_POKE_PROMPTS)
    except Exception:
        return False
    if not stuck:
        counter.unlink(missing_ok=True)
        return False
    try:
        done = int(counter.read_text().strip() or 0)
    except (OSError, ValueError):
        done = 0
    if done >= MAX_STUCK_POKE_REPRESSES:
        _log(agent, f"stuck poke still in the composer after {done} Enters — holding (T-4141)")
        return False
    sent = send_bare_enter(cfg)
    try:
        counter.write_text(f"{done + 1}\n")
    except OSError:
        pass
    _log(agent, f"stuck poke in the composer of a not-busy pane — sent Enter "
                f"{done + 1}/{MAX_STUCK_POKE_REPRESSES} (ok={sent}) instead of poking again (T-4141)")
    return sent


# T-4510: the periodic anti-idle/queue pokes the fleet usage gate may suppress at >=95% of
# the shared weekly Max. Deliberately NOT here: P1.d (abandoned-turn retry), P1.c (liveness
# backstop, ships dark), the cap-modal/cap/cyber/effectiveness branches and the TUI-wedge
# escalation — those are recovery (sop/standard/cross-agent-dispatch-discipline.md).
USAGE_GATED_PREDICATES = frozenset({"p1a", "p1b"})

# `_dispatch_poke` returns this INSTEAD of an exit code when the usage gate suppressed the
# content poke. main() must then FALL THROUGH to the later predicates (P1.d, P1.c): a gated
# P1.a/P1.b that ended main() would shadow the P1.d retry-inject, which is recovery.
USAGE_SUPPRESSED = -1


def _seat_reader():
    """The cross-seat reader (T-5793). activity_monitor runs as Recovery, and the persona
    state files are 0600 under the seat's own account, so every read goes through
    health_export.SudoReader (argv-only `sudo -n -u <seat>`). Tests patch this."""
    return health_export.SudoReader()


def _active_runtime_harness(agent: str, cfg: dict) -> str:
    """Resolve the provider that owns the live pane (usage-gate accounting and the
    send_poke submit profile).

    The shared fleet usage record is Claude-only. Controller-managed seats retain
    their persona/tmux identity while running Codex, so gating by persona would park
    the fallback precisely when it should carry work.

    T-5793: resolved by harness_idle.resolve_harness over the SudoReader: the
    controller's runtime.json, then the active-runtime.json cache, then the role's
    historical Claude default. The old raw Path.read_text ran as Recovery against the
    seat's 0600 file, always failed, and always fell back to claude-code. Any
    failure still fails to claude-code. `active_runtime_path` (tests) names the
    cache file; runtime.json is looked up beside it under runtime-controller/.
    """
    declared = cfg.get("active_runtime_harness")
    if declared in {"claude-code", "codex"}:
        return str(declared)
    user = str(cfg.get("sudo_user") or "")
    override = cfg.get("active_runtime_path")
    try:
        profile, _started, _source = harness_idle.resolve_harness(
            agent, home=Path(f"/home/{user}"), default="claude-code", account=user,
            reader=_seat_reader(),
            persona_dir=Path(override).parent if override else None)
    except Exception:  # noqa: BLE001 — resolver never raises; belt and braces
        return "claude-code"
    return profile


def _poke_suppressed_by_usage(
    agent: str, predicate: str, now: float | None = None, *, cfg: dict | None = None,
) -> bool:
    """T-4510: True iff this content poke must be skipped because the fleet's shared weekly
    Max usage is at/above the gate's threshold (default 95%). Called immediately before
    `send_poke(cfg, prompt)` — AFTER every recovery branch in `_dispatch_poke` — and only for
    USAGE_GATED_PREDICATES. Logs once per window per (agent, predicate), not once per tick.
    Fails OPEN: any gate error means the poke proceeds (today's behaviour)."""
    if predicate not in USAGE_GATED_PREDICATES:
        return False
    if cfg is not None and _active_runtime_harness(agent, cfg) == "codex":
        return False
    try:
        decision = fleet_usage_gate.evaluate(now)
        if not decision.suppress:
            return False
        if fleet_usage_gate.should_log_once(
                f"activity_monitor:{agent}:{predicate}", decision.window_key):
            _log(agent, f"{predicate}: poke SUPPRESSED by the fleet usage gate — {decision.reason}; "
                        f"resumes automatically below the threshold or at the reset (T-4510)")
        return True
    except Exception:  # noqa: BLE001 — a gate fault must never block or break a poke path
        return False


def _dispatch_poke(
    cfg: dict,
    agent: str,
    predicate: str,
    prompt: str,
    state_path: Path,
    *,
    bounded_enabled: bool,
    max_ineffective: int,
) -> int:
    """Send a poke for `predicate` — or, in bounded mode (T-1363), SUPPRESS it
    and escalate when prior pokes weren't advancing the session (stuck, not
    idle). Returns the main() exit code (0 = poked or cleanly suppressed; 1 =
    poke send failed) — or USAGE_SUPPRESSED (T-4510) when the fleet usage gate
    skipped the content poke, which is NOT an exit code: main() falls through to
    P1.d/P1.c instead of ending the cycle.

    Ship-dark: bounded_enabled=False reproduces the legacy send_poke +
    write_poke_state behavior exactly — so with the flag off, nothing changes
    EXCEPT the T-3599 busy-guard below, which only ever DEFERS (never injects), so
    it is safe-direction in both paths (mirrors the T-3579 part-A safe inversion).
    """
    # T-3599 decide-time busy-guard (part B of the T-3579 fix). A GENERATING pane
    # QUEUES an injected submit rather than running it — so a poke there is doomed
    # and its send-failure would fire OnFailure -> a false unit-failure alert to
    # recovery (confirmed live pantheon-activity-monitor@artisan 2026-09-08T19:15Z). A pane
    # whose turn ended with a background agent running accepts input (T-4284), but
    # it is waiting on delegated work, so the probe reads it busy and it is not poked.
    # Mirror the mailbox_wake busy-defer (T-2681/T-3554): DEFER instead of poking.
    # Only a DEFINITIVELY-busy pane (False) skips; None (UNKNOWN) and True (idle)
    # fall through and poke exactly as before — a flaky/foreign pane must not skip a
    # needed poke. SAFE-DIRECTION: this branch only ever returns-0-without-poking;
    # it never injects, so the T-2729 shell-inject hazard is structurally
    # impossible and the sole failure mode is over-defer (poke retained; the
    # effectiveness/TUI-wedge/mail_staleness backstops still catch a real wedge).
    # T-4141: BEFORE the busy-guard. A stuck poke reads BUSY there (text in the
    # composer under the manage footer), so it would defer forever and never clear.
    if _flush_stuck_poke(cfg, agent, state_path):
        return 0
    if _pane_idle_probe(cfg) is False:
        _log(agent, f"{predicate}: pane BUSY (generating/manage-view) — "
                    f"deferring poke, not exit-1 (T-3599)")
        _record_fire(agent, predicate, deferred=True)  # T-5648: matched, deferred
        return 0
    if bounded_enabled:
        # Pre-poke state-gate (T-1363 inc.3): a CYBER-BLOCKED session keeps emitting
        # refusals, so its jsonl ADVANCES after a poke — which would fool the
        # effectiveness signal (jsonl-advanced == "poke worked") into poking forever.
        # Detect it up front and SUPPRESS: it needs a RESTART, not a poke. (Restart-
        # routing via route_failure_alert is the next increment; for now suppress +
        # an escalated marker, which is the same shape as the effectiveness-suppress.)
        # Cap gate (T-1369 branches 2+3), BEFORE the cyber gate: cheaper probe of
        # the same surface, and both cap states share the property that a poke is
        # futile — MEASURED 2026-08-02: the waker kicked a session-capped orchestrator 8x
        # over 3.5h, every kick delivered, every turn an error, pane back to idle.
        # Bounded, correct, useless. Also: a capped session's jsonl ADVANCES on
        # each failed kick (error turns are still turns), which fools the
        # effectiveness signal exactly the way the cyber comment below describes.
        # Modal gate (T-1369 branch 4), BEFORE the jsonl cap gate: when the
        # `/rate-limit-options` menu is up it is the modal, not the cap, that
        # holds the seat down — orchestrator sat on one ~50 min past its own reset with
        # the jsonl branch below behaving exactly as designed. The jsonl branch
        # cannot see this state, and its correct "self-clears, no escalation"
        # ruling is precisely what makes the modal invisible. Also a poke here is
        # worse than futile: send_poke types TEXT into a live menu.
        modal = _session_cap_modal(cfg)
        if modal == MODAL_SAFE:
            prior = read_poke_state(state_path) or {}
            attempts = int(prior.get("modal_dismissals", 0) or 0)
            if attempts < MAX_MODAL_DISMISSALS:
                sent = send_modal_confirm(cfg)
                _log(agent, f"{predicate}: cap MODAL up, cursor on option 1 "
                            f"(stop+wait) — sent Enter (attempt {attempts + 1}"
                            f"/{MAX_MODAL_DISMISSALS}, ok={sent}) (T-1369)")
                write_poke_state(
                    state_path, agent, predicate,
                    pre_poke_jsonl_mtime=latest_jsonl_activity(
                        cfg["jsonl_dir"], cfg["sudo_user"]) or 0,
                    consecutive_ineffective=int(
                        prior.get("consecutive_ineffective", 0) or 0),
                    escalated=bool(prior.get("escalated")),
                    modal_dismissals=attempts + 1,
                )
                if sent:
                    _record_fire(agent, predicate)
                return 0 if sent else 1
            # Bound reached: the menu is still up after N confirms, so pressing
            # Enter is not the remedy and repeating it is just noise on someone
            # else's keyboard.
            _log(agent, f"{predicate}: cap MODAL still up after "
                        f"{MAX_MODAL_DISMISSALS} confirms — holding (T-1369)")
            _escalate_stuck(
                cfg, agent, predicate,
                msg=(f"🪟 [{predicate}] {agent} is held by the /rate-limit-options "
                     f"MODAL and {MAX_MODAL_DISMISSALS} Enter-confirms did not "
                     f"clear it. Poking suppressed (a poke types text into a live "
                     f"menu). Needs a human at the pane."),
                vocab="cap-modal", state_path=state_path,
                cur_mtime=latest_jsonl_activity(cfg["jsonl_dir"], cfg["sudo_user"]) or 0,
                consecutive=int(prior.get("consecutive_ineffective", 0) or 0),
            )
            return 0
        if modal == MODAL_UNSAFE:
            # The menu is up but the cursor is not provably on option 1. Option 2
            # SPENDS MONEY — the operator's lane — so this branch never touches the
            # keyboard, no matter how wedged the seat looks.
            prior = read_poke_state(state_path) or {}
            _log(agent, f"{predicate}: cap MODAL up with cursor NOT on option 1 — "
                        f"NOT sending keys (option 2 spends); escalating (T-1369)")
            _escalate_stuck(
                cfg, agent, predicate,
                msg=(f"🛑 [{predicate}] {agent} is on the /rate-limit-options modal "
                     f"and the selection is NOT 'stop and wait'. Refusing to send "
                     f"Enter: the other option is 'Upgrade your plan', which spends "
                     f"— that is the operator's lane. Needs a human at the pane."),
                vocab="cap-modal-unsafe", state_path=state_path,
                cur_mtime=latest_jsonl_activity(cfg["jsonl_dir"], cfg["sudo_user"]) or 0,
                consecutive=int(prior.get("consecutive_ineffective", 0) or 0),
            )
            return 0
        cap = _session_cap_state(cfg)
        if cap is not None:
            kind, reset_hint = cap
            if kind == "session_limit":
                # SELF-CLEARS at the time printed in the message — verified live
                # (cap said 09:40, first good turn minutes after, no intervention).
                # So: no poke (futile), and NO escalation either — paging a human
                # for a state with a printed expiry is the alarm that teaches
                # people to skip alarms. The next cycle re-probes; once the cap
                # clears this branch stops taken and normal poking resumes. That
                # IS the retry route — the retry is the timer's own cadence.
                _log(agent, f"{predicate}: session CAPPED (session limit, resets "
                            f"{reset_hint or 'per message'}) — NOT poking; "
                            f"self-clears, no escalation (T-1369)")
                return 0
            # spend_limit: self-clears at the PRINTED reset (T-2290: the "monthly"
            # text is really the current usage window). The wait is usually hours but
            # CAN be long — artisan sat ~48h erroring every turn (T-2387: a far window /
            # exhausted budget), which is why we hold + escalate ONCE (transition-gated
            # inside _escalate_stuck) and never poke (futile until the window rolls) or
            # restart (re-hits it). It is NOT permanent and needs no raise; a raise
            # (the operator's lane) only shortens a long wait. The escalation records the
            # seat-state, it does not ask for a raise.
            cur_mtime = latest_jsonl_activity(cfg["jsonl_dir"], cfg["sudo_user"]) or 0
            prior = read_poke_state(state_path) or {}
            _log(agent, f"{predicate}: session CAPPED (spend limit) — NOT poking; "
                        f"holds until raised, escalating once (T-1369)")
            _escalate_stuck(
                cfg, agent, predicate,
                msg=(f"⛔ [{predicate}] {agent} is SPEND-CAPPED — every turn errors. "
                     f"It self-clears at the printed reset (T-2290: the current usage "
                     f"window), but the wait can be long — artisan sat ~48h (T-2387). "
                     f"Poking/restarting suppressed (futile until the window rolls). A "
                     f"raise (the operator's lane) only shortens the wait; this alert records "
                     f"the seat-state, it is not a raise request."),
                vocab="spend-cap", state_path=state_path, cur_mtime=cur_mtime,
                consecutive=int(prior.get("consecutive_ineffective", 0) or 0),
            )
            return 0
        if _session_cyber_blocked(cfg):
            cur_mtime = latest_jsonl_activity(cfg["jsonl_dir"], cfg["sudo_user"]) or 0
            prior = read_poke_state(state_path) or {}
            _log(agent, f"{predicate}: session CYBER-BLOCKED (refusal) — NOT poking "
                        f"(jsonl-advance would fool effectiveness); needs RESTART")
            _escalate_stuck(
                cfg, agent, predicate,
                msg=(f"🛑 [{predicate}] {agent} is CYBER-BLOCKED (repeated stop_reason:refusal "
                     f"category:cyber) — a poke just produces another refusal. RESTART to clear "
                     f"the flagged context (reference_cyber_safeguard_block_on_security_research). "
                     f"Do NOT replicate the research."),
                vocab="cyber-refusal", state_path=state_path, cur_mtime=cur_mtime,
                consecutive=int(prior.get("consecutive_ineffective", 0) or 0),
            )
            return 0
        cur_mtime = latest_jsonl_activity(cfg["jsonl_dir"], cfg["sudo_user"]) or 0
        suppress, consecutive, reason = poke_effectiveness_decision(
            read_poke_state(state_path), cur_mtime, predicate, max_ineffective
        )
        if suppress:
            # Stuck (N ineffective pokes, jsonl frozen) — route the remedy to Recovery
            # ONCE (transition-gated inside _escalate_stuck), not every cycle.
            _log(agent, f"{predicate}: {reason}; NOT poking (stuck, not idle) — escalating")
            _escalate_stuck(
                cfg, agent, predicate,
                msg=(f"⛔ [{predicate}] {agent} appears STUCK: {consecutive} consecutive "
                     f"ineffective poke(s) — its main jsonl didn't advance after the poke, so "
                     f"it's not idle-recoverable (capped / wedged / blocked). Poking suppressed; "
                     f"needs investigation/restart, not another poke."),
                vocab="stuck-poke", state_path=state_path, cur_mtime=cur_mtime,
                consecutive=consecutive,
            )
            return 0
        if _poke_suppressed_by_usage(agent, predicate, cfg=cfg):  # T-4510: after recovery
            return USAGE_SUPPRESSED
        harness = _gate_text_poke(cfg, agent, predicate)  # T-5793: positive pane gate
        if harness is None:
            return 0
        if send_poke(cfg, prompt, harness=harness):
            _log(agent, f"{predicate} poke sent (bounded; consecutive_ineffective={consecutive})")
            write_poke_state(
                state_path, agent, predicate,
                pre_poke_jsonl_mtime=cur_mtime, consecutive_ineffective=consecutive,
            )
            _record_fire(agent, predicate)
            return 0
        _log(agent, f"{predicate} poke send failed")
        return _soft_defer_or_fail(cfg, agent, predicate)  # T-3599 send-fail busy-guard
    # Legacy / ship-dark path — byte-for-byte the prior poke/state behavior
    # (the _record_fire call is a new, best-effort, non-wedging side-write —
    # T-3072 — it does not change what this branch pokes or persists).
    if _poke_suppressed_by_usage(agent, predicate, cfg=cfg):  # T-4510: provider-aware gate
        return USAGE_SUPPRESSED
    harness = _gate_text_poke(cfg, agent, predicate)  # T-5793: positive pane gate
    if harness is None:
        return 0
    if send_poke(cfg, prompt, harness=harness):
        _log(agent, f"{predicate} poke sent")
        write_poke_state(state_path, agent, predicate)
        _record_fire(agent, predicate)
        return 0
    _log(agent, f"{predicate} poke send failed")
    return _soft_defer_or_fail(cfg, agent, predicate)  # T-3599 send-fail busy-guard


# ---------------------------------------------------------------------------
# TUI-alive-but-not-emitting wedge (T-1369 final branch)
# ---------------------------------------------------------------------------
#
# The wedge: the pane keeps changing (spinner animating — TUI render loop alive)
# while the session jsonl stays frozen (no productive turn emitted). A perfectly
# HEALTHY agent in a long silent turn produces the SAME observation — pane moves,
# jsonl still — so the ONLY thing separating "wedged, restart it" from "working,
# leave it alone" is DURATION. The threshold below was MEASURED, not guessed:
# tui_wedge_sampler ran 60s cadence over 2026-08-03..05 across known-healthy
# operation and recorded the run-length distribution of this exact ambiguous
# state — orchestrator n=110 runs, max 779s (~13min), 71% <=120s, thin tail (3/110
# >600s); recovery n=20, max 541s. The healthy ceiling is ~13min.
#
# The threshold is set at 1800s — ~2.3x orchestrator's measured max — with deliberate
# margin on the CHEAP-failure side. The sampler measures a run FROM THE LAST
# UNAMBIGUOUS SAMPLE (T-1369 hygiene: fails toward SHORTER runs), so the true
# healthy ceiling sits ABOVE the observed one; and the action here is a page to
# Recovery RECOMMENDING a restart (never an auto-restart — same disposition as the
# cyber branch). A threshold that fails LOW pages Recovery for a healthy long turn
# (alarm fatigue); one that fails high delays a real wedge. 1800s buys margin
# against the false page while still catching a wedge ~30min in, versus the
# status quo of "sits until a human notices" (measured at hours, T-1369 history).
TUI_WEDGE_JSONL_FROZEN_SECONDS = int(os.environ.get(
    "ACTIVITY_MONITOR_TUI_WEDGE_FROZEN_SECONDS", str(30 * 60)))
# Two pane captures this far apart decide "animating". A Claude Code spinner
# repaints several times a second, so a ~2s gap reliably shows motion on a live
# render loop and shows none on a still idle prompt.
TUI_WEDGE_MOTION_PROBE_GAP_SECONDS = float(os.environ.get(
    "ACTIVITY_MONITOR_TUI_WEDGE_PROBE_GAP", "2"))


def _pane_fp(text: str | None) -> str | None:
    """Truncated sha256 of pane text, or None for a None capture. A fingerprint,
    not the text: the motion test only needs equality, and this branch has no
    business retaining a copy of a sibling's working screen."""
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def pane_is_animating(cfg: dict, gap_seconds: float) -> bool | None:
    """True iff the pane text CHANGES across two captures `gap_seconds` apart.

    A changing pane means the TUI render loop is alive (spinner animating); it
    says NOTHING about whether the backend is emitting — that is the whole point,
    it is the ambiguous half of the wedge signal. A still pane (unchanged) is an
    idle prompt or a frozen static screen. Returns None when it cannot tell (no
    configured pane, or either capture failed) — the caller treats None as "do
    not fire", never as motion."""
    pane = cfg.get("tmux_target_pane")
    if not pane:
        return None
    first = capture_pane(pane, cfg.get("sudo_user"))
    if first is None:
        return None
    time.sleep(gap_seconds)
    second = capture_pane(pane, cfg.get("sudo_user"))
    if second is None:
        return None
    return _pane_fp(first) != _pane_fp(second)


# Twin of context_fill_monitor._pane_has_background_agent (T-3726), plus the T-5605
# agent-list rows (context_fill reads those via its token-growth check): the inline
# background-agent glyph and the manage-view footer. "← for agents" alone is NOT
# a marker; it also renders on the idle view.
_BG_AGENT_MARKERS = ("⧉", "↓ to manage")

# T-5605: newer builds list background agents in a footer, one row per agent, with
# neither marker above: "◯ implementer  <task>  1h 41m 18s · ↓ 361.2k tokens". A
# FINISHED agent keeps its row with a frozen timer (orchestrator 2026-10-26 17:10Z vs
# 17:37Z, identical), so a row only proves a running agent if it CHANGES between
# two captures; one capture would let a stale list mask a real wedge forever.
_BG_AGENT_ROW_RE = re.compile(
    r"^\s*[◯○◐◑◒◓] \S+\s.*\b\d+[hms](?: \d+[ms])*(?: \d+s)? · [↓↑] [\d.]+k? tokens",
    re.MULTILINE)


def _pane_has_background_agent(pane_text: str | None) -> bool:
    return bool(pane_text) and any(m in pane_text for m in _BG_AGENT_MARKERS)


def _background_agent_running(cfg: dict, probe_gap: float) -> bool:
    """A '⧉' / manage-footer marker, or an agent-list row that moves across
    probe_gap. A failed capture is not evidence (False)."""
    pane = cfg.get("tmux_target_pane") or ""
    first = capture_pane(pane, cfg.get("sudo_user"))
    if _pane_has_background_agent(first):
        return True
    rows = _BG_AGENT_ROW_RE.findall(first or "")
    if not rows:
        return False
    time.sleep(probe_gap)
    second = capture_pane(pane, cfg.get("sudo_user"))
    return second is not None and _BG_AGENT_ROW_RE.findall(second) != rows


def check_tui_wedge(
    cfg: dict, frozen_threshold: int, probe_gap: float
) -> tuple[bool, str]:
    """The TUI-alive-but-not-emitting wedge detector (T-1369 final branch).

    Fires (True) iff ALL of:
      1. the session jsonl has emitted no PRODUCTIVE turn for > frozen_threshold
         (liveness mtime, so an error-turn does not reset it — same signal p1a
         uses); AND
      2. the freeze is UNEXPLAINED — no cap modal, no cap state, no cyber block.
         Each of those is a KNOWN freeze-cause with its own branch that owns the
         remedy; a capped/blocked seat's liveness is frozen too, so without this
         guard the wedge branch would double-fire on a state already handled; AND
      3. the pane is ANIMATING (changes across a short gap) — i.e. not sitting
         still at an idle prompt (which is p1a's poke path, not a wedge).

    Read-only: it stats mtimes and captures panes, nothing else. It never sends
    keys and never restarts — the ACTION (a restart RECOMMENDATION to Recovery) is
    the caller's. Returns (fired, human-readable reason).

    Ordering matters for cost and safety: the cheap duration gate (1) short-
    circuits first, so the cap/cyber probes and the ~2s pane motion probe only
    run once a seat is already long-frozen. The explained-cause guards (2) run
    BEFORE the motion probe so a capped seat never even gets probed for motion.
    """
    jsonl_ts = liveness_jsonl_mtime(
        cfg["jsonl_dir"], cfg["sudo_user"], cfg.get("agent", "?")) or 0
    if jsonl_ts == 0:
        # Cannot measure the freeze → fail-open (do not fire), same direction
        # as check_p1a's activity==0 guard.
        return (False, "no liveness jsonl mtime — cannot measure freeze (fail-open)")
    frozen_for = int(time.time()) - jsonl_ts
    if frozen_for < frozen_threshold:
        return (False, f"jsonl frozen {frozen_for}s < threshold {frozen_threshold}s")
    # Long freeze. Is a KNOWN state holding it? Those branches own the remedy.
    if _session_cap_modal(cfg) is not None:
        return (False, f"frozen {frozen_for}s but cap MODAL up — its branch owns it")
    if _session_cap_state(cfg) is not None:
        return (False, f"frozen {frozen_for}s but CAPPED — the cap branch owns it")
    if _session_cyber_blocked(cfg):
        return (False, f"frozen {frozen_for}s but CYBER-BLOCKED — its branch owns it")
    # T-4712: a session waiting on a background agent has a frozen main jsonl and
    # a pane that "animates" (the agent's elapsed timer ticks ~1Hz). That is the
    # agent being BUSY, not wedged. Same markers as context_fill_monitor's T-3726
    # guard; a stuck background agent stays covered by that monitor's
    # token-growth check. A failed capture is not evidence: fall through.
    if _background_agent_running(cfg, probe_gap):
        return (False, f"frozen {frozen_for}s but a background agent holds the "
                       f"turn (⧉ / '↓ to manage') — busy, not wedged")
    # Unexplained long freeze. Distinguish the moving-pane wedge from a plain
    # still idle prompt (p1a territory).
    animating = pane_is_animating(cfg, probe_gap)
    if animating is None:
        return (False, f"frozen {frozen_for}s, no known cause, but pane capture "
                       f"failed — cannot confirm animation (fail-open)")
    if not animating:
        return (False, f"frozen {frozen_for}s, no known cause, but pane is STILL "
                       f"(idle-at-prompt, not the moving-pane wedge)")
    return (True, f"jsonl frozen {frozen_for}s (>{frozen_threshold}s) with pane "
                  f"ANIMATING and no cap/modal/cyber cause — TUI-alive-not-emitting")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ORG-011 Phase B activity monitor (per-agent).")
    parser.add_argument(
        "--agent",
        choices=sorted(AGENT_CONFIG.keys()),
        default="orchestrator",
        help="Which agent's activity to monitor (default: orchestrator).",
    )
    return parser.parse_args(argv)


def _seat_hold(agent: str):
    """ORG-106 L2 (T-5525, supersedes T-5244's maintenance-only check): the seat's
    EffectiveSeatState when anything holds it (seat-state record, maintenance, OOM hold),
    else None. Runs under Recovery's manager for other seats, so this goes through
    seat_state.effective_for (cross-account record read). Fail-open (None) on any fault:
    a reader fault must never mask a real wedge."""
    try:
        from tools import seat_state
        e = seat_state.effective_for(agent)
    except Exception as exc:  # noqa: BLE001
        _log(agent, f"seat-state reader fault (fail-open): {type(exc).__name__}: {exc}")
        return None
    if e.fault:
        _log(agent, f"seat-state reader fault (fail-open): {e.fault}")
    blocked = e.intake_blocked or e.launch_blocked or e.remediation_vetoed
    return e if blocked else None


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    agent = args.agent
    cfg = AGENT_CONFIG[agent]

    # T-5244: a seat in an ACTIVE seat-maintenance window is held on purpose (e.g. its
    # controller disabled, the pane gone). Every action below (pokes, modal Enter,
    # wedge alerts) is wrong there, and a poke into a missing pane fails -> exit 1 ->
    # OnFailure -> a false unit-failure alert (mason, 2026-10-19 19:00Z). Fail-open:
    # a registry fault reads "not in maintenance", and the window auto-expires.
    # ORG-106 L2 (T-5525): maintenance, the OOM hold and deep-sleep (remediation_vetoed /
    # launch_blocked) keep that skip-everything behaviour. A paused-intake record only
    # stops NEW work: a paused seat is alive and must stay watched, so liveness, wedge and
    # modal predicates keep running and just the queue pokes (P1.a / P1.b) are skipped.
    hold = _seat_hold(agent)
    if hold is not None and (hold.remediation_vetoed or hold.launch_blocked):
        if "maintenance" in hold.sources:
            _log(agent, "seat-maintenance ACTIVE: skipping all predicates (T-5244)")
        else:
            _log(agent, f"seat held ({', '.join(hold.sources)}): skipping all predicates "
                        f"(ORG-106 L2)")
        return 0
    intake_held = hold is not None and hold.intake_blocked
    if intake_held:
        _log(agent, f"seat intake paused ({', '.join(hold.sources)}): queue pokes "
                    f"P1.a/P1.b skipped, liveness predicates still run (ORG-106 L2)")

    cooldown_seconds = int(os.environ.get(
        "ACTIVITY_MONITOR_COOLDOWN_SECONDS", ACTIVITY_MONITOR_COOLDOWN_SECONDS
    ))
    p1d_cooldown_seconds = int(os.environ.get(
        "ACTIVITY_MONITOR_P1D_COOLDOWN_SECONDS", P1D_COOLDOWN_SECONDS
    ))
    idle_threshold = int(os.environ.get(
        "ACTIVITY_MONITOR_P1A_IDLE_SECONDS", P1A_IDLE_THRESHOLD_SECONDS
    ))
    operator_absent_threshold = int(os.environ.get(
        "ACTIVITY_MONITOR_P1B_OPERATOR_SECONDS", P1B_OPERATOR_ABSENT_THRESHOLD_SECONDS
    ))
    # T-4683: an explicit P1.b off-switch. Raising the threshold does NOT pause
    # P1.b: check_p1b's fail-closed branch sets absent == threshold whenever the
    # session jsonl has no telegram inbound yet (every fresh session after a
    # restart), so any threshold passes. Recovery predicates are unaffected.
    p1b_enabled = os.environ.get("ACTIVITY_MONITOR_P1B_ENABLED", "1") != "0"
    p1d_age_threshold = int(os.environ.get(
        "ACTIVITY_MONITOR_P1D_AGE_SECONDS", P1D_RATE_LIMIT_AGE_SECONDS
    ))
    bridge_url = os.environ.get(
        "ACTIVITY_MONITOR_BRIDGE_HEALTH_URL", DEFAULT_BRIDGE_HEALTH_URL
    )
    # P1.c (ORG-037 Layer 3) — ship dark: only evaluated when explicitly
    # enabled (supervised first-fire per agent). Default-OFF makes the
    # committed code inert everywhere until a per-agent flag flip.
    p1c_enabled = os.environ.get("PANTHEON_LAYER3_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on"
    )
    p1c_stale_threshold = int(os.environ.get(
        "ACTIVITY_MONITOR_P1C_STALE_SECONDS", P1C_STALE_THRESHOLD_SECONDS
    ))
    p1c_cooldown_seconds = int(os.environ.get(
        "ACTIVITY_MONITOR_P1C_COOLDOWN_SECONDS", P1C_COOLDOWN_SECONDS
    ))
    p1c_require_fresh_teammate = os.environ.get(
        "ACTIVITY_MONITOR_P1C_REQUIRE_FRESH_TEAMMATE", "1"
    ).strip().lower() in ("1", "true", "yes", "on")
    # Bounded-poke (T-1363) — ship dark: default-off → legacy unbounded behavior.
    # When enabled, p1a/p1b/p1c route through _dispatch_poke, which suppresses +
    # escalates once consecutive ineffective pokes reach max_ineffective_pokes.
    bounded_poke_enabled = os.environ.get(
        "ACTIVITY_MONITOR_BOUNDED_POKE_ENABLED", ""
    ).strip().lower() in ("1", "true", "yes", "on")
    max_ineffective_pokes = int(os.environ.get(
        "ACTIVITY_MONITOR_MAX_INEFFECTIVE_POKES", MAX_INEFFECTIVE_POKES_DEFAULT
    ))
    # TUI-alive-but-not-emitting wedge (T-1369 final) — ship dark. A restart-
    # RECOMMENDING branch (never auto-restart) gets the same default-off,
    # supervised-first-fire treatment as every other consequential branch here.
    tui_wedge_enabled = os.environ.get(
        "ACTIVITY_MONITOR_TUI_WEDGE_ENABLED", ""
    ).strip().lower() in ("1", "true", "yes", "on")

    # P1.b handled-wait backoff (T-3991) — default-ON with an env kill-switch; see
    # the constant block. Stretches the p1b cooldown once a wait has repeated-fired
    # AND been ticketed to the operator. Failure-direction is benign (still-poked, just
    # slower), so unlike the escalate/restart branches it does not ship dark.
    p1b_backoff_enabled = os.environ.get(
        "ACTIVITY_MONITOR_P1B_BACKOFF_ENABLED", "1"
    ).strip().lower() in ("1", "true", "yes", "on")
    p1b_backoff_cooldown = int(os.environ.get(
        "ACTIVITY_MONITOR_P1B_BACKOFF_COOLDOWN_SECONDS", P1B_BACKOFF_COOLDOWN_SECONDS
    ))
    p1b_backoff_after_fires = int(os.environ.get(
        "ACTIVITY_MONITOR_P1B_BACKOFF_AFTER_FIRES", P1B_BACKOFF_AFTER_FIRES
    ))
    p1b_backoff_streak_gap = int(os.environ.get(
        "ACTIVITY_MONITOR_P1B_BACKOFF_STREAK_GAP_SECONDS", P1B_BACKOFF_STREAK_GAP_SECONDS
    ))
    p1b_cooldown = p1b_effective_cooldown(
        agent, cfg, cooldown_seconds,
        enabled=p1b_backoff_enabled,
        backoff_cooldown=p1b_backoff_cooldown,
        after_fires=p1b_backoff_after_fires,
        streak_gap_seconds=p1b_backoff_streak_gap,
    )

    state_path = _state_path_for_agent(agent)
    cooldown_map = {
        "p1a": cooldown_seconds,
        "p1b": p1b_cooldown,
        "p1d": p1d_cooldown_seconds,
        "p1c": p1c_cooldown_seconds,
        INTERACTIVE_AWAIT_PREDICATE: cooldown_seconds,
    }

    if in_cooldown(state_path, cooldown_map):
        _log(agent, "in cooldown; skipping")
        return 0

    # TUI-wedge (T-1369 final) — evaluated BEFORE the poke predicates, and
    # independent of a ticket queue: a wedge is a wedge whether or not there is
    # queued work, and p1a's 1-hour idle threshold would otherwise let a seat
    # that has been frozen-but-animating for 30+ minutes sit unremarked. Ship
    # dark. The action is an escalation to Recovery RECOMMENDING a restart, bounded
    # to one page per episode by _escalate_stuck's transition gate — never an
    # auto-restart, so a false fire costs a page, not a killed session.
    if tui_wedge_enabled:
        wedged, reason = check_tui_wedge(
            cfg, TUI_WEDGE_JSONL_FROZEN_SECONDS, TUI_WEDGE_MOTION_PROBE_GAP_SECONDS)
        if wedged:
            cur_mtime = latest_jsonl_activity(cfg["jsonl_dir"], cfg["sudo_user"]) or 0
            prior = read_poke_state(state_path) or {}
            # T-2632 PART 2 (park-attribution wiring): before paging an ambiguous-
            # QUIET wedge, check whether the freeze is attributable to a DECLARED
            # signal — an active context_fill_hold. A deliberately-held session can
            # legitimately sit in a long turn (pane-animating + jsonl-frozen — exactly
            # this branch's signature), so a hold EXPLAINS the freeze; suppress the
            # page. This downgrades only the ALERT — this branch never auto-restarts,
            # so attribution can never mask a wedge into a restart (design #2 / the
            # T-2615 "a hold must never mask a real wedge" invariant holds trivially
            # here: there is no restart to mask). Unattributed quiet escalates exactly
            # as before. Hold is the sufficient asserted signal for THIS path: a
            # UNIT-disabled deliberate pause stops the session, so its pane cannot
            # animate → check_tui_wedge condition #3 fails → this branch never fires
            # for a paused seat regardless. Fail-safe: quiet_is_attributable returns
            # (False, …) on any error, so a broken hold-lookup pages (never silently
            # suppresses).
            attributable, why = quiet_is_attributable(agent)
            if attributable:
                _log(agent, f"TUI-WEDGE fired: {reason} — quiet is ATTRIBUTABLE "
                            f"({why}); SUPPRESSING the page (declared hold, not a wedge) (T-2632)")
                return 0
            _log(agent, f"TUI-WEDGE fired: {reason} — escalating restart "
                        f"recommendation to Recovery (T-1369)")
            _escalate_stuck(
                cfg, agent, "tui_wedge",
                msg=(f"🧊 [tui_wedge] {agent} looks WEDGED: its session jsonl has "
                     f"emitted no productive turn for "
                     f">{TUI_WEDGE_JSONL_FROZEN_SECONDS // 60}min while its pane is "
                     f"still ANIMATING (TUI render loop alive, backend not emitting), "
                     f"and no cap/modal/cyber state explains the freeze. The measured "
                     f"healthy ceiling for this pane-moving-jsonl-frozen state is "
                     f"~13min (T-1369 sampler, n=110), so this is well past it. "
                     f"Recommend a RESTART to clear the wedge — but VERIFY at the pane "
                     f"first: a genuinely long single turn is the one benign cause, "
                     f"and this branch recommends, it does not auto-restart."),
                vocab="tui-wedge", state_path=state_path, cur_mtime=cur_mtime,
                consecutive=int(prior.get("consecutive_ineffective", 0) or 0),
            )
            return 0
        _log(agent, f"TUI-wedge: not fired ({reason})")

    # T-4510: P1.a/P1.b are the gated predicates. A usage-SUPPRESSED one must NOT end the
    # cycle: check_p1a counts error turns as non-activity (T-2387), so a seat stuck on an
    # API-error abort for >1h with queue>=1 satisfies p1a AND p1d at once, and returning here
    # would shadow the P1.d retry-inject (recovery). On suppression fall through to P1.d and
    # P1.c; every non-suppressed dispatch keeps today's return-early behaviour exactly.
    usage_suppressed = False

    # T-5793: P1.b interactive-await (the former T-0297 Signal 2). A seat blocked on an
    # unanswered AskUserQuestion/EnterPlanMode while the operator is away is escalated to Recovery
    # ONCE (transition-bounded _escalate_stuck), never poked: a poke typed into a live
    # menu is blind input. Before P1.a, which would otherwise match the same blocked seat
    # (idle + queue) and end the cycle on a deferred poke. Not skipped for paused intake
    # (a surfacing, not new work) or usage suppression (it costs no turn). log mode only.
    if p1b_enabled and idle_source() == "log":
        tool = check_p1b_interactive(cfg, operator_absent_threshold, agent=agent)
        if tool is not None:
            _log(agent, f"P1.b interactive-await: blocked on {tool} while the operator is away")
            _escalate_stuck(
                cfg, agent, INTERACTIVE_AWAIT_PREDICATE,
                msg=(f"❓ [p1b] {agent} is blocked on an unanswered {tool} prompt "
                     f"(>= {P1B_LOG_QUIET_SECONDS // 60}min) while the operator has been away "
                     f"for >= {operator_absent_threshold // 60}min. Not poking: a poke would "
                     f"type into the live menu. It needs the operator's answer, or a human at "
                     f"the pane to answer or dismiss it."),
                vocab=INTERACTIVE_AWAIT_VOCAB, state_path=state_path, cur_mtime=0,
                consecutive=0,
            )
            return 0
        _rearm_interactive_await(state_path)

    # Check P1.a first (it's a stronger signal: completely-idle).
    if not intake_held and check_p1a(cfg, idle_threshold):
        _log(agent, "P1.a fired: idle > threshold + queue ≥ 1")
        rc = _dispatch_poke(cfg, agent, "p1a", P1A_POKE_PROMPT, state_path,
                            bounded_enabled=bounded_poke_enabled,
                            max_ineffective=max_ineffective_pokes)
        if rc != USAGE_SUPPRESSED:
            return rc
        usage_suppressed = True  # P1.b is behind the same gate: skip it, keep P1.a's precedence

    # P1.b: the operator away + agent self-waiting.
    if not p1b_enabled:
        _log(agent, "P1.b skipped: ACTIVITY_MONITOR_P1B_ENABLED=0")
    elif intake_held:
        pass  # logged above; P1.b is a queue poke (ORG-106 L2)
    elif not usage_suppressed and check_p1b(cfg, bridge_url, operator_absent_threshold,
                                            agent=agent):
        _log(agent, "P1.b fired: the operator absent + await-state + queue ≥ 1")
        rc = _dispatch_poke(cfg, agent, "p1b", P1B_POKE_PROMPT, state_path,
                            bounded_enabled=bounded_poke_enabled,
                            max_ineffective=max_ineffective_pokes)
        if rc != USAGE_SUPPRESSED:
            return rc
        usage_suppressed = True

    # P1.d: rate-limit aborted the turn + has persisted past threshold.
    if check_p1d(cfg, p1d_age_threshold):
        _log(agent, "P1.d fired: rate-limit visible + stale > threshold")
        p1d_harness = _gate_text_poke(cfg, agent, "p1d")  # T-5793: positive pane gate
        if p1d_harness is None:
            return 0
        if send_poke(cfg, P1D_RETRY_PROMPT, harness=p1d_harness):
            _log(agent, "P1.d retry-inject sent")
            write_poke_state(state_path, agent, "p1d")
            _record_fire(agent, "p1d")  # T-3072 — doesn't route through _dispatch_poke
            # T-0256: append session-reset stats entry. Best-effort; failure
            # must not block the fire (which has already succeeded).
            try:
                from tools.session_reset_log import append_event as _sr_append
                _sr_append(
                    "rate_limit_retry_inject",
                    trigger="p1d_pane_capture",
                    agent=agent,
                    predicate="p1d",
                )
            except Exception as exc:  # noqa: BLE001 — best-effort side-effect
                _log(agent, f"session_reset_log append failed (non-fatal): {exc}")
            return 0
        _log(agent, "P1.d retry-inject send failed")
        return 1

    # P1.c-narrow (ORG-037 Layer 3): active-team + dispatcher-turn-firing-stopped.
    # Ship dark — only evaluated when explicitly enabled (supervised first-fire).
    if p1c_enabled and check_p1c(cfg, p1c_stale_threshold, p1c_require_fresh_teammate):
        _log(agent, "P1.c fired: active-team + dispatcher dormant > threshold")
        return _dispatch_poke(cfg, agent, "p1c", P1C_POKE_PROMPT, state_path,
                              bounded_enabled=bounded_poke_enabled,
                              max_ineffective=max_ineffective_pokes)

    if usage_suppressed:
        _log(agent, "only usage-suppressed P1.a/P1.b fired (T-4510); P1.d/P1.c did not; bail")
        return 0
    _log(agent, "no predicate fired; bail")
    return 0


if __name__ == "__main__":
    sys.exit(main())
