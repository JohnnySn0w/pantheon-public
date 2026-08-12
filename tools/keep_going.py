"""tools/keep_going.py — periodic anti-idle nudge for a standing agent (T-0772 sibling).

The operator's directive (2026-06-06): a tmux injection that tells a parked agent to keep
going, running PARALLEL to the mailbox inbox-push wake but as its OWN script — never
merged. This is the anti-park heartbeat: the failure it kills is an agent sitting idle
for hours with greenlit work queued (the 3h Recovery park that prompted it).

Fired by `pantheon-keep-going@<agent>.timer` every ~15 min as a `--user` oneshot.

Idle-GATED so it never interrupts an agent mid-work: it captures the agent's own pane
and nudges ONLY when the bottom status region shows a parked idle prompt — NOT when
"esc to interrupt" is present (actively generating) and NOT when a modal dialog footer
owns the bottom region. Inconclusive captures fail safe = skip (a missed nudge is
harmless; a nudge fired into active work is the disruption we avoid). The idle-gate is
also the natural throttle: once a nudge wakes the agent it goes busy, so the next tick
skips — no extra debounce needed.

Known limitation: an agent idle because it is legitimately WAITING on a human answer
looks the same as a parked agent, so it will be nudged. That nudge is low-harm (the
agent can just restate it is blocked on the operator), but noted.

ADAPTIVE BACKOFF (T-1364, the operator TG 3508 — the nudge "is wasting a lot of usage…
especially for specialist, it's almost entirely a waste"). The original design nudged every
~15 min whenever the pane looked idle, with NO notion of whether the nudge produced any
work — the exact unbounded-repeating-remediation anti-pattern fixed for the activity_monitor
pokes (see memory feedback_bounded_repeating_remediation). A perpetually-idle agent
therefore burned a full turn every 15 min forever — real money on usage-credit billing.

The fix: a nudge is judged effective iff the agent's WORK SIGNAL advanced after it (a
ledger entry tagged with the agent's id — the same agent-specific signal activity_monitor
uses, NOT just the reflexive "holding" reply). Each ineffective nudge DOUBLES the wait
(1→2→4→…→2^cap idle cycles, ≈15 min → 8 h); the first nudge that produces work RESETS to
the base cadence.
Disable with KEEP_GOING_ADAPTIVE=0 (reverts to legacy nudge-every-idle-cycle).

QUEUE-AWARE FLOOR (T-1926). This docstring used to justify the hard backoff by asserting
"real *queued* work is already covered by activity_monitor P1.a" — that premise was FALSE
for the ORG-043 seats and is removed rather than reworded: activity_monitor.AGENT_CONFIG
enumerates ONLY orchestrator, so no P1.a net exists for metis/atlas (nor specialist/recovery, who are not
exposed because their ledger cadence keeps this module's own signal live). A seat parked
mid-arc produced no ledger entries, so every nudge scored ineffective, the wait doubled to
the 2^cap ceiling (~8 h), and the net that supposedly justified that ceiling did not cover
it — reintroducing the parked-with-queued-work failure this module exists to prevent.

Rather than build a second mechanism around the false premise, the premise is removed at
source: an agent whose ticket queue is NON-EMPTY does not back off past a floor. Backing
off an idle agent with nothing to do stays correct and still saves pool; backing off an
agent that demonstrably has work is the bug. Opt-in per agent via the `queue_aware_backoff`
key in AGENT_PANE (seats-only on first fire — supervised-first-fire; widening to the other
three is a separate call, not a side effect of T-1926).

The idle/busy sentinels mirror inbox_consumer._WEDGE_* (T-0716) — kept as a small local
copy so this script stays self-contained and import-side-effect-free (parallel + separate,
per the directive). If those sentinels drift, update both.

DELIVERY FLAG (T-1827 R1, per knowledge/episodic/2026-07-20-producer-repoint-mailbox-design/
notes.md §5 R1): the nudge's DECISION logic (idle-gate + adaptive backoff) is unchanged for
every agent. Only the final delivery step is per-agent switchable — DELIVERY_MODE routes
specialist's nudge through mailbox.send() (the ORG-031 ACP intake pilot) while every other agent
(and any agent absent from the map, fail-open) keeps the byte-identical send_keys_submit.sh
pane-inject. Flipping a DELIVERY_MODE entry back to "send-keys" fully restores prior
behavior; both delivery paths stay in-tree per the rollback contract.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import getpass
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:  # run-as-script (python3 tools/keep_going.py): tools/ on sys.path[0]
    import agent_role_map
except ImportError:  # run-as-package (import tools.keep_going)
    from tools import agent_role_map

# Dual-identity-safe import (mirrors acp_bridge.py / ancestor_consume.py / mailbox.py's
# own _lease import): `python -m unittest discover tools` (package identity
# tools.mailbox) and a bare run from inside tools/ both resolve to ONE module object.
try:
    from tools import mailbox
except ImportError:  # bare run with cwd=tools/
    import mailbox  # type: ignore

# T-2024: reuse T-1980's cap detectors (detect_capability_cap +
# detect_consent_fallback) so the injector can SKIP nudging a usage/capability-
# capped seat. Same dual-identity-safe import shape. context_fill_monitor has no
# import-time side effects (all guarded by __main__), so importing it here is free.
try:
    from tools import context_fill_monitor as _cfm
except ImportError:  # bare run with cwd=tools/
    import context_fill_monitor as _cfm  # type: ignore

AGENT_PANE = {
    "orchestrator": {
        "pane_target": "claude-telegram-bot:0", "unix_user": "claude",
        "pantheon_config_path": "/home/claude/pantheon-config",
        "ledger_agent_id": "orchestrator",
        # T-2687: derive the live-session projects dir from the shared
        # agent_role_map helper (single source of truth for the control-plane
        # `-pantheon-config` slug), not a hand-copied per-seat literal. Fixes the
        # recovery/specialist control-plane seats, whose plain `-home-<account>` slug read
        # a stale/other project dir than their live session writes.
        "projects_dir": agent_role_map.live_session_projects_dir("claude"),
    },
    "specialist": {
        "pane_target": "claude-research-bot:0", "unix_user": "claude-research",
        "pantheon_config_path": "/home/claude-research/pantheon-config",
        "ledger_agent_id": "specialist",
        "projects_dir": agent_role_map.live_session_projects_dir("claude-research"),
    },
    "recovery": {
        "pane_target": "claude-telegram-bot-fallback:0", "unix_user": "claude-fallback",
        "pantheon_config_path": "/home/claude-fallback/pantheon-config",
        "ledger_agent_id": "recovery",
        "projects_dir": agent_role_map.live_session_projects_dir("claude-fallback"),
    },
    # Metis + Atlas (ORG-043 sub-orchestrator seats), added 2026-07-21: their
    # bringup armed pantheon-keep-going@{metis,atlas}.timer (T-1896) but this map
    # never gained the seats, so every fire died at argparse choices= — heartbeat
    # dead since bringup, silently (no OnFailure wiring on the seat units either).
    # Pane targets mirror mailbox_wake.AGENT_PANE; plain claude TUIs, no bridge.
    # `queue_aware_backoff` + `ticket_targets` (T-1926): seats-only on first fire. These
    # two keys are what opt an agent into the queue-aware floor; their ABSENCE on
    # orchestrator/specialist/recovery above is deliberate, so those three keep bit-identical nudge
    # behaviour. `ticket_targets` mirrors activity_monitor.AGENT_CONFIG's key of the same
    # name — a ticket may address either the persona or the audit role-id, so both forms
    # are listed (identical for the seats, which is why each set has one member).
    "metis": {
        "pane_target": "suborch1-main:0", "unix_user": "suborch1",
        "pantheon_config_path": "/home/suborch1/pantheon-config",
        "ledger_agent_id": "metis",
        "projects_dir": agent_role_map.live_session_projects_dir("suborch1"),
        "queue_aware_backoff": True,
        "ticket_targets": frozenset({"metis"}),
    },
    "atlas": {
        "pane_target": "suborch2-main:0", "unix_user": "suborch2",
        "pantheon_config_path": "/home/suborch2/pantheon-config",
        "ledger_agent_id": "atlas",
        "projects_dir": agent_role_map.live_session_projects_dir("suborch2"),
        "queue_aware_backoff": True,
        "ticket_targets": frozenset({"atlas"}),
    },
}

# Registry personas deliberately NOT nudged by this module, with reasons (T-2370).
#
# MAPPED-OR-EXEMPT. Keep in lockstep with AGENT_PANE: the coverage test fails if a
# registry persona is in neither, or in both. Same shape as
# `mailbox_mcp._NO_MCP_RECIPIENT` (T-2285), `ancestor_consume._NO_REPLY_INBOX`
# (T-2252) and `inbox_consumer.DELIVERY_EXEMPT` (T-2144). The defect class this
# closes is SILENCE, not absence: before this, AGENT_PANE simply lacked mnemosyne
# and nothing in the code could tell a deliberate exclusion from the stale omission
# that had already cost metis and atlas a dead heartbeat from bringup (see the
# comment above their entries). An exclusion is fine; an unexplained one is not.
_NO_KEEP_GOING: dict[str, str] = {
    "mnemosyne": (
        "EXEMPT, not stale — checked at T-2370 rather than assumed. Three "
        "independent reasons, any one sufficient: (1) NO pantheon-keep-going@"
        "mnemosyne.timer is armed anywhere on the box — the only armed instance is "
        "@orchestrator — so an AGENT_PANE entry would assert a coverage that nothing "
        "delivers, which is worse than the honest absence. (2) ORG-042 puts the "
        "second head in a SEPARATE instance under the `exocortex` user, and their "
        "wake/inbox-consumer timers install in THEIR user manager during their own "
        "harness bringup — hub-side maps only ready their config (see "
        "mailbox_wake.AGENT_PANE's mnemosyne note). (3) They are spawn-gated, so "
        "their pane is legitimately ABSENT much of the time; mailbox_wake documents "
        "that 'mnemosyne's absent pane' returns UNKNOWN and must not trigger a wake. "
        "REVISIT THIS if a keep-going timer is ever armed for them — at that point "
        "the metis/atlas failure (timer armed, map missing the seat, every fire "
        "dying at argparse choices=) is reproduced exactly, and the fix is to move "
        "her from here into AGENT_PANE."
    ),
    "codex": (
        "EXEMPT — different harness, not a different opinion. codex is an OpenAI "
        "codex-CLI agent, not Claude Code (T-1041/T-1106). Its TUI needs the "
        "`harness: codex` submit profile because the claude-code '[Pasted text' "
        "sentinel is always-absent there and the default profile FALSE-PASSES — it "
        "reports a nudge submitted without confirming it. This module's AGENT_PANE "
        "carries no harness key and its delivery path assumes the default profile, "
        "so a codex entry here would silently report success while nudging nothing. "
        "Supporting it means teaching this module the harness profile first."
    ),
    "the-ancestor": (
        "EXEMPT — not a ticket-working seat. It is the mailbox hub (ORG-026): a "
        "channel-less standing session that consumes its own mailbox in-session. It "
        "holds no ticket queue and files no ledger entries, so both this module's "
        "idle-gate signal and its queue-aware floor are undefined for it. Nothing "
        "to keep going."
    ),
}

# Per-agent delivery mode (T-1827 R1). "send-keys" (default, byte-identical legacy
# pane-inject) or "mailbox" (routes the nudge through mailbox.send() instead — the
# ORG-031 ACP intake pilot). ONLY specialist is flipped; every other agent, and any agent
# absent from this map, fails open to "send-keys" via .get(agent, "send-keys") at the
# call site — never KeyError. Rollback = flip the entry back.
DELIVERY_MODE = {
    "orchestrator": "send-keys",
    "specialist": "mailbox",
    "recovery": "send-keys",
}

# Mirrors mailbox_wake.MAILBOX_HUB (ORG-026 Path A hub convention) — kept as a small
# local copy for the same self-contained-import reason as the idle/busy sentinels
# above; if the hub path drifts, update both.
MAILBOX_HUB = Path("/home/the-ancestor/mailbox")

# Adaptive-backoff knobs (env-overridable). Default ON per the operator's TG 3508.
ADAPTIVE = os.environ.get("KEEP_GOING_ADAPTIVE", "1") != "0"
# Max exponent: 2^cap idle cycles between nudges for a perpetually-idle agent.
# 2^5 = 32 cycles ≈ 8 h at the ~15-min timer cadence.
BACKOFF_CAP_EXP = int(os.environ.get("KEEP_GOING_BACKOFF_CAP", "5"))

# T-1926 queue-aware floor: the largest backoff exponent permitted while the agent's
# ticket queue is non-empty. 2**1 = 2 idle cycles; at the 15-min timer cadence that is a
# 30-min nudge spacing (vs the 2**5 = ~8 h ceiling that applies when the queue is empty).
#
# ANCHOR — read from tools/activity_monitor.py directly, never from prose describing it
# (the docstring above used to quote "15-30 min" for P1.a, which is three different
# quantities blurred together; that is exactly the secondhand-constant trap this comment
# exists to close). The two constants, BY NAME:
#
#   ACTIVITY_MONITOR_COOLDOWN_SECONDS = 1800  # 30 min — P1.a's REPEAT-poke cadence  <- THE ANCHOR
#   P1A_IDLE_THRESHOLD_SECONDS        = 3600  # 1 h    — P1.a's FIRST-fire delay (NOT the anchor)
#
# RESOLVED (T-1926 rebound r1): the floor is 2**1 / 30 min, anchored to the COOLDOWN.
# A backoff floor bounds REPEAT SPACING, and ORG-011 already ratified 30 min as the
# re-poke cadence for this exact predicate (idle + actionable queue). The seat floor is
# that same predicate evaluated at the keep_going tier, so it inherits the same ratified
# number rather than inventing one. The 3600 s figure governs FIRST fire, which this
# module handles differently (it nudges from cycle 1 and only stretches on repeated
# ineffectiveness) — anchoring to it would have made a queued seat WAIT LONGER than the
# net it stands in for, in a failure mode (actionable work present during backoff, e.g.
# T-1904 sitting undelivered) where faster pickup is the whole point.
QUEUED_BACKOFF_FLOOR_EXP = int(os.environ.get("KEEP_GOING_QUEUED_FLOOR_EXP", "1"))
_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"

_SEND_KEYS_HELPER = Path(__file__).resolve().parent / "send_keys_submit.sh"

# Mirrors inbox_consumer._WEDGE_LIVENESS_SENTINELS / _WEDGE_DIALOG_FOOTERS (T-0716).
_BUSY = "esc to interrupt"                                   # actively generating
# Parked-at-prompt footer hints. Claude Code shows DIFFERENT footers depending on
# whether a task list exists and is shown/hidden:
#   - no task list           → "? for shortcuts"
#   - task list shown         → "ctrl+t to hide tasks"
#   - task list HIDDEN        → "ctrl+t to show tasks"   ← was MISSING (T-1364 follow-up)
# The missing "show tasks" variant made an idle orchestrator (which keeps a hidden task
# list) read as inconclusive every cycle, so keep_going never nudged it — the gap
# that left orchestrator parked after the 2026-06-28 weekly-cap reset until the operator
# manually re-prompted. Busy is checked FIRST, so a "ctrl+t to … tasks" footer that
# coexists with "esc to interrupt" still correctly reads as busy.
_IDLE_HINTS = ("? for shortcuts", "ctrl+t to hide tasks", "ctrl+t to show tasks")
_DIALOG_FOOTERS = (
    "Esc to cancel · Tab to amend",
    "Enter to confirm · Esc to cancel",
    "Enter to confirm · Esc to exit",
)
_BOTTOM_REGION_LINES = 6  # Claude Code's working-status block height (inbox_consumer)

DEFAULT_PROMPT = (
    "Please keep going. Continue the next step of your in-flight task or pick up your "
    "next queued ticket. If you're blocked, update the ticket with the blocker, send it "
    "to the right person (retarget/assign), and move on to your next actionable item — "
    "don't sit idle. If you're waiting on a response, make sure it actually went through "
    "Telegram (via the reply tool, not just transcript). If you have nothing explicit to "
    "do, you may play around or skim through repos to tidy them up."
)


# ----------------------------------------------------------------------- idle gate
def is_idle_at_prompt(pane: str) -> bool | None:
    """Tri-state on the pane's live bottom region:
      True  — parked idle prompt (safe to nudge),
      False — busy ("esc to interrupt") or a modal footer owns the bottom (skip),
      None  — inconclusive / empty capture (fail safe → skip).
    """
    non_blank = [ln for ln in pane.splitlines() if ln.strip()]
    if not non_blank:
        return None
    bottom = "\n".join(non_blank[-_BOTTOM_REGION_LINES:])
    if any(f in bottom for f in _DIALOG_FOOTERS):
        return False  # a modal owns the pane — keep-going is the wrong tool
    if _BUSY in bottom:
        return False  # actively generating — never interrupt
    if any(h in bottom for h in _IDLE_HINTS):
        return True   # parked at the idle prompt
    return None        # can't tell — fail safe


# ------------------------------------------------------------------ pane I/O (stubbable)
def capture_pane(pane_target: str, unix_user: str) -> str | None:
    """Capture the agent's pane. Same-account (the @.service runs as the agent) → no
    sudo; cross-account wraps sudo -n -u with that user's XDG_RUNTIME_DIR."""
    inner = f"XDG_RUNTIME_DIR=/run/user/$(id -u) tmux capture-pane -t {pane_target} -p"
    if unix_user == getpass.getuser():
        argv = ["bash", "-lc", inner]
    else:
        argv = ["sudo", "-n", "-u", unix_user, "bash", "-lc", inner]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[keep_going] capture error: {exc!r}", file=sys.stderr)
        return None
    return r.stdout if r.returncode == 0 else None


def inject(pane_target: str, prompt: str) -> bool:
    """Inject + submit-verify via send_keys_submit.sh (same path as the mailbox waker)."""
    try:
        r = subprocess.run(
            [str(_SEND_KEYS_HELPER), pane_target, prompt],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[keep_going] inject error: {exc!r}", file=sys.stderr)
        return False
    if r.returncode != 0:
        print(f"[keep_going] inject failed rc={r.returncode}: {r.stderr.strip()}", file=sys.stderr)
    return r.returncode == 0


def mailbox_inbox_for(agent: str) -> Path:
    """The mailbox hub inbox path for `agent` (ORG-026 Path A convention). Pure, no I/O."""
    return MAILBOX_HUB / agent


def send_mailbox(agent: str, prompt: str, *, inbox: Path | None = None) -> bool:
    """Deliver the nudge via mailbox.send() instead of a pane inject (T-1827 R1).

    payload is `prompt` VERBATIM — it already satisfies the design note's
    state-pointer invariant, so it is never reworded for this delivery path.
    `inbox` overrides the default hub path (tests only; production always uses
    mailbox_inbox_for(agent))."""
    box = inbox if inbox is not None else mailbox_inbox_for(agent)
    try:
        # sender="keep_going", not the account persona (T-1935 forensics,
        # Specialist): each timer fire is a fresh one-shot pid stamped with a
        # hardcoded sender, and stamping it "orchestrator" made the nudge stream
        # read as orchestrator's own session sends during the double-delivery
        # investigation. Provenance should name the PRODUCER. Note: with
        # mailbox.send()'s pending-window dedupe (T-1935), a nudge whose
        # identical predecessor still sits unconsumed in new/ collapses onto
        # it — the desired semantic (N pending identical nudges wake exactly
        # as well as one).
        mailbox.send(str(box), prompt, sender="keep_going", subject="keep-going nudge")
        return True
    except (OSError, ValueError, TypeError) as exc:
        print(f"[keep_going] mailbox send error: {exc!r}", file=sys.stderr)
        return False


# ----------------------------------------------------------- work signal (effectiveness)
def _ledger_tac_cmd(repo_path: str, n: int = 500) -> str:
    """Shell expression that tac|heads the newest N ledger lines across the union
    (per-week subfolder shards + legacy flat 2026.jsonl). Mirrors
    activity_monitor._ledger_union_tac_cmd — kept local so keep_going stays
    self-contained. repo_path is config-derived (no untrusted interpolation)."""
    ledger_dir = f"{repo_path}/ledger"
    legacy = f"{ledger_dir}/2026.jsonl"
    return (
        f"{{ for f in $(ls -d {ledger_dir}/*/*.jsonl 2>/dev/null | sort); "
        f"do cat \"$f\"; done; "
        f"[ -f {legacy} ] && cat {legacy}; }} 2>/dev/null | tac | head -n {n}"
    )


def latest_work_ts(cfg: dict) -> int | None:
    """Latest epoch ts of a ledger entry tagged with this agent's ledger_agent_id,
    or None. THE effectiveness signal: did the agent produce real work (a ledger
    entry), not merely a reflexive "holding" reply. Agent-specific (distinct id per
    agent), so another agent's work never falsely resets the backoff. Runs as the
    agent (same-account → no sudo) reading its own clone; mirrors capture_pane's
    cross-account fallback.

    T-1950 (Atlas D-22, HO-ratified): this is a NO-AGENT-TAGGED-LEDGER-ENTRY
    signal, NOT an idleness/activity metric — do not read it as one, and do not
    widen it to count commits/pane activity (deliberate P6 strictness: a nudge
    system that counts any activity as success congratulates itself for
    reflexive work). The two DIVERGE exactly when an agent does real work that
    doesn't land an agent-tagged entry: HO conventionally LIFTS a seat's arc
    into the ledger at action-close (agent:orchestrator, not agent:<seat>),
    and sub-arc actions (a hook install, oracle prep) aren't per-commit
    ledgered. A seat in that state scores "ineffective" while genuinely
    productive — accepted; the queued-work FLOOR (T-1926) is the safety net for
    parked-with-work, not this signal. Seats should self-author ledger entries
    for arcs they OWN (§5 clarification) to keep their own signal accurate."""
    repo = cfg.get("pantheon_config_path")
    agent_id = cfg.get("ledger_agent_id")
    if not repo or not agent_id:
        return None
    inner = _ledger_tac_cmd(str(repo), 500)
    user = cfg["unix_user"]
    if user == getpass.getuser():
        argv = ["bash", "-c", inner]
    else:
        argv = ["sudo", "-n", "-u", user, "bash", "-c", inner]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[keep_going] ledger read error: {exc!r}", file=sys.stderr)
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    latest: int | None = None
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict) or obj.get("agent") != agent_id:
            continue
        ts_str = obj.get("ts")
        if not isinstance(ts_str, str):
            continue
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        ep = int(dt.timestamp())
        if latest is None or ep > latest:
            latest = ep
    return latest


# --------------------------------------------------------- session identity (T-1500 B)
def current_session_id(cfg: dict) -> str | None:
    """Basename of the newest session jsonl in the agent's projects dir, or None.

    THE restart signal: a new Claude Code session writes a new <uuid>.jsonl, so a
    changed basename means the agent restarted since the last state write. Probe
    failure (sudo denied, empty dir, timeout) returns None — fail-safe, treated as
    'unknown', never as 'new session'. Mirrors context_fill_monitor.find_latest_jsonl
    (same sudo'd-find shape; 700-mode projects dirs need the agent's own account)."""
    pd = cfg.get("projects_dir")
    if not pd:
        return None
    inner = (
        f"find {pd} -maxdepth 1 -name '*.jsonl' -printf '%T@ %f\\n' 2>/dev/null"
        " | sort -rn | head -1"
    )
    user = cfg["unix_user"]
    if user == getpass.getuser():
        argv = ["bash", "-c", inner]
    else:
        argv = ["sudo", "-n", "-u", user, "bash", "-c", inner]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[keep_going] session probe error: {exc!r}", file=sys.stderr)
        return None
    if r.returncode != 0:
        return None
    parts = r.stdout.strip().split()
    return parts[-1] if len(parts) == 2 else None


def maybe_reset_for_new_session(state: dict, cur_session_id: str | None) -> tuple[dict, bool]:
    """T-1500 (B): a NEW session resets the adaptive backoff. Pure.

    Rationale (2026-07-02 gap): the ineffectiveness backoff persisted across a
    restart, so a fresh session with queued handoff work sat parked 6.5 h while
    every cycle logged 'backed off'. A restart is exactly when a nudge is likely
    effective again, so the escalation must not outlive the session it measured.

    Returns (state', reset_happened). Fail-safe: unknown current session (probe
    failure) or a state written before this feature (no session_id recorded) →
    no reset, just record/keep going with the existing backoff."""
    if not cur_session_id:
        return (state, False)
    prev = state.get("session_id")
    if prev is None or prev == cur_session_id:
        new = dict(state)
        new["session_id"] = cur_session_id
        return (new, False)
    return ({"session_id": cur_session_id}, True)


# ------------------------------------------------------------------------- backoff state
def _state_path(agent: str) -> Path:
    d = os.environ.get("PANTHEON_STATE_DIR")
    base = Path(d) if d else _DEFAULT_STATE_DIR
    return base / f"keep_going_{agent}.json"


def _lock_path(agent: str) -> Path:
    sp = _state_path(agent)
    return sp.with_name(sp.name + ".lock")


@contextlib.contextmanager
def _agent_lock(agent: str):
    """T-2583: serializes the read-decide-deliver-write critical section per
    agent, so two concurrent invocations on one seat cannot both observe the
    same pre-nudge state and both decide to deliver (a double-nudge).

    A DEDICATED lockfile (`<state_path>.lock`), not an flock on the state file
    itself: write_state() open/truncate/writes the state path, and coupling the
    lock's fd to a file that gets rewritten under the same path invites fd/inode
    confusion for no benefit — the lock's content is irrelevant, only holding
    it matters.

    FAIL-OPEN by design: any problem acquiring the lock (missing fcntl support,
    permissions, OSError of any kind) degrades to the pre-T-2583 unlocked
    behavior rather than raising or blocking — an occasional double-nudge is far
    better than a stuck or blocked continuation kicker. LOCK_EX is blocking
    (not LOCK_NB): the critical section is short (one state read, one pure
    decision, one pane-inject/mailbox-send, one state write), so a brief wait is
    cheap and simpler than a non-blocking retry loop.

    Release is guaranteed on every exit path (normal return, skip-return, or an
    exception) via the surrounding try/finally."""
    lp = _lock_path(agent)
    fh = None
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lp, "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        # fh may already be open (mkdir/open succeeded but flock raised) —
        # close it before nulling so this narrow error path doesn't leak an fd.
        if fh is not None:
            fh.close()
        print(f"[keep_going] {agent}: lock unavailable ({exc!r}) — proceeding unlocked (fail open)",
              file=sys.stderr)
        fh = None
    try:
        yield
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()


def read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def write_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError as exc:
        print(f"[keep_going] state write failed: {exc!r}", file=sys.stderr)


def has_queued_work(cfg: dict) -> bool:
    """True if the agent has >=1 ticket in inbox/ or active/ addressed to it (T-1926).

    The "is there queued work" half of the queue-aware floor. Deliberately narrow:

      - Filesystem-only, no YAML parse and no imports beyond stdlib. This module is
        kept import-side-effect-free on purpose (see the sentinels note above), and a
        oneshot that runs every 15 min on five accounts should not pay a yaml import.
      - Reads `tickets/inbox` + `tickets/active`. This is not a new predicate: it is
        activity_monitor's ratified P1.a predicate with the persona substituted. Its
        P1A_POKE_PROMPT says verbatim "Check tickets/inbox/ + tickets/active/ for
        target=orchestrator and pull the next completable item" — same two dirs,
        same target-field match. That provenance is the point: the seat floor mirrors
        the ORG-011-ratified net rather than inventing a parallel notion of "has work".
        `active` is included precisely because the T-1926 failure is a seat PARKED
        mid-arc — a claimed-but-unfinished ticket is the signal, not just unstarted ones.
      - Mail is NOT counted: unread mailbox messages already have their own 5-min waker
        (pantheon-mailbox-wake@<agent>.timer). Counting it here would claim coverage this
        change did not add, and would double-drive a path that already works.

    Fail-SAFE (returns False) on any unreadable tree: an agent with no detectable queue
    keeps the ordinary backoff, which is the pre-T-1926 behaviour. A detector fault must
    never invent a queue and pin an idle agent at the floor forever, burning pool.
    """
    targets = cfg.get("ticket_targets") or frozenset()
    if not targets:
        return False
    root = Path(cfg["pantheon_config_path"]) / "tickets"
    try:
        for state_dir in ("inbox", "active"):
            d = root / state_dir
            if not d.is_dir():
                continue
            for path in d.glob("T-*.md"):
                try:
                    if _ticket_is_actionable(path, targets, root):
                        return True
                except OSError:
                    continue
    except OSError:
        return False
    return False


def _ticket_is_actionable(path: Path, targets: frozenset, root: Path) -> bool:
    """True if this ticket is addressed to the agent AND startable now (T-1951).

    Mirrors inbox_consumer's gate semantics — THE SEMANTICS OF RECORD is
    inbox_consumer's eligibility classification (dependency_blocked +
    not_before_unmet); this is a deliberate minimal LOCAL line-parse of the two
    frontmatter gate fields, NOT an import of the consumer, because this module
    is kept import-light / yaml-free by design (see has_queued_work's docstring).
    The two implementations are pinned against each other by a cross-impl
    PARITY TEST (test_keep_going.py::GateParityWithInboxConsumer) so they cannot
    drift silently — the drift-guard is the test, not shared code (the
    import-light constraint rules out the shared-leaf shape used elsewhere).

    Gate semantics mirrored exactly, including edge behavior:
      - labels: a ticket labelled `interactive-required` is NOT actionable — the
        consumer buckets it as interactive_required and never auto-delivers it
        (inbox_consumer.list_eligible_tickets). Added T-2171: this arm was missing,
        so an agent could be pinned at the T-1926 floor by a queue the consumer
        refuses to dispatch — verbatim the harm this function exists to prevent,
        through the one bucket it did not cover. Latent when found, not active:
        every agent still had a non-interactive actionable ticket, so no floor was
        wrongly held; nearest to biting was orchestrator at 62 of 83.
      - depends_on: MET iff every dep's file exists in tickets/done/ (the
        consumer's _ticket_state_on_disk == "done" check, filesystem-only here).
      - not_before: unmet iff it parses (fromisoformat, Z→+00:00) AND is in the
        future; malformed/unparseable is FAIL-OPEN (counts as actionable), same
        as the consumer's documented malformed-treated-as-missing behavior.
    Gated tickets floor nothing: counting work the consumer itself refuses to
    dispatch pinned agents at the T-1926 floor burning pool for an empty queue.
    Fail-safe direction preserved: skipping too much → ordinary backoff
    (pre-T-1926 behavior); this only ever REMOVES reasons to floor.
    """
    from datetime import datetime, timezone  # stdlib; local to keep module import-light

    target_val: str | None = None
    not_before_val: str | None = None
    depends_raw: str | None = None
    labels_raw: str | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("target:"):
            target_val = line.split(":", 1)[1].strip()
        elif line.startswith("not_before:"):
            not_before_val = line.split(":", 1)[1].strip()
        elif line.startswith("depends_on:"):
            depends_raw = line.split(":", 1)[1].strip()
        elif line.startswith("labels:"):
            labels_raw = line.split(":", 1)[1].strip()
        elif line.startswith("# "):
            break  # past the frontmatter block

    if target_val not in targets:
        return False

    # Checked before the other gates to mirror the consumer's own ordering. Split
    # on commas rather than substring-matching, so `interactive-required-followup`
    # is not mistaken for the label.
    if labels_raw:
        labels = {p.strip().strip("'\"") for p in labels_raw.strip("[]").split(",")}
        if "interactive-required" in labels:
            return False  # interactive_required

    if depends_raw and depends_raw not in ("[]", ""):
        deps = [p.strip() for p in depends_raw.strip("[]").split(",") if p.strip()]
        for dep in deps:
            if not (root / "done" / f"{dep}.md").is_file():
                return False  # dependency_blocked

    if not_before_val:
        try:
            nb_dt = datetime.fromisoformat(not_before_val.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < nb_dt:
                return False  # not_before_unmet
        except (ValueError, TypeError):
            pass  # malformed → fail-open, mirroring the consumer

    return True


def decide_nudge(state: dict, cur_work_ts: int | None, now_ts: int,
                 cap: int = BACKOFF_CAP_EXP,
                 floor_exp: int | None = None) -> tuple[bool, dict, str]:
    """Pure backoff decision: (should_nudge, new_state, reason).

    A nudge is DUE only after 2^min(consecutive_ineffective, cap) idle cycles
    since the last nudge (1, 2, 4, …, 2^cap). When due, the PRIOR nudge is judged:
    effective iff the work signal advanced since it (cur_work_ts > work_ts_at_last_nudge).
    Each ineffective nudge doubles the interval; an effective one resets it to 1.

    `floor_exp` (T-1926) clamps the WAIT while the agent has queued work: the exponent
    is capped at floor_exp, so the spacing cannot grow past 2^floor_exp cycles. None
    (the default) reproduces pre-T-1926 behaviour exactly — callers that do not opt in
    are bit-identical.

    Note what the floor does NOT touch: `consecutive_ineffective` still increments
    normally. The floor shortens the wait, it does not pretend the nudges are working.
    So if the queue later empties, the backoff resumes from the count it had actually
    reached rather than from zero — no free reset, no way to hold an agent at the base
    cadence by parking a ticket in its queue.

    SEMANTICS (T-1950): `consecutive_ineffective` counts nudge cycles that
    produced no ledger entry tagged with THIS agent's id — it is NOT an
    idleness or activity metric, and must not be consumed as one (dashboards,
    escalations, digests included). HO-lifted seat arcs and non-ledgered
    sub-arc work legitimately score "ineffective" while productive; the
    T-1926 floor above, not this counter, is what protects that case. See
    latest_work_ts's docstring for the full disposition.
    """
    consec = int(state.get("consecutive_ineffective", 0) or 0)
    cycles = int(state.get("cycles_since_nudge", 0) or 0) + 1
    last_nudge_ts = state.get("last_nudge_ts")
    eff_exp = min(max(consec, 0), cap)
    if floor_exp is not None:
        eff_exp = min(eff_exp, max(int(floor_exp), 0))
    threshold = 2 ** eff_exp
    if cycles < threshold:
        new = dict(state)
        new["cycles_since_nudge"] = cycles
        return (False, new, f"backed off ({cycles}/{threshold} idle cycles, consec_ineffective={consec})")
    if last_nudge_ts is None:
        new_consec, eff_note = 0, "first nudge"
    else:
        prev_work = state.get("work_ts_at_last_nudge")
        effective = cur_work_ts is not None and (prev_work is None or cur_work_ts > prev_work)
        if effective:
            new_consec, eff_note = 0, "prior nudge produced work → reset to base cadence"
        else:
            new_consec = consec + 1
            eff_note = f"prior nudge produced no work → backoff x{2 ** min(new_consec, cap)}"
    new = {
        "last_nudge_ts": now_ts,
        "work_ts_at_last_nudge": cur_work_ts,
        "consecutive_ineffective": new_consec,
        "cycles_since_nudge": 0,
    }
    return (True, new, f"nudge due ({eff_note})")


# ------------------------------------------------------------------------------- run
def is_usage_capped(cfg: dict, agent: str, now_ts: int,
                    do_read_records=None, do_find_jsonl=None) -> bool:
    """True iff the agent's live session is currently usage/capability-capped
    (T-2024). Reuses T-1980's detectors: detect_capability_cap (a recent run of
    assistant `isApiErrorMessage` spend/usage-limit records) OR
    detect_consent_fallback (the Fable weekly-credits model_consent_fallback
    system record). When either fires, keep-going must NOT nudge — a "please keep
    going" prompt into a hit cap only burns the remaining budget and can shove the
    agent deeper into the cap (the operator, TG 4174). T-1980 DETECTS the cap; this
    makes the injector RESPECT it.

    Reads the agent's newest session jsonl via context_fill_monitor's sudo-aware
    helpers (same-account → no sudo, cross-account → sudo -nu). FAIL-OPEN: any
    resolution/read/parse error returns False, so a detection miss degrades to the
    pre-T-2024 behavior (one possibly-wasted nudge), never a stalled heartbeat.
    do_read_records / do_find_jsonl are injection seams for testing."""
    find_jsonl = do_find_jsonl or _cfm.find_latest_jsonl
    read_records = do_read_records or _cfm.read_recent_records
    sudo_user = cfg["unix_user"]
    try:
        jsonl = find_jsonl(Path(cfg["projects_dir"]), sudo_user, agent)
        if jsonl is None:
            return False
        records = read_records(jsonl, sudo_user, agent)
        if not records:
            return False
        if _cfm.detect_capability_cap(records, now_ts) is not None:
            return True
        if _cfm.detect_consent_fallback(records) is not None:
            return True
        return False
    except Exception as exc:  # any surprise → fail open (nudge as before)
        print(f"[keep_going] {agent}: cap-check error {exc!r} — proceeding (fail open)",
              file=sys.stderr)
        return False


def run(agent: str, *, prompt: str = DEFAULT_PROMPT,
        do_capture=capture_pane, do_inject=inject,
        do_work_ts=latest_work_ts, do_session_id=current_session_id,
        do_queued_work=has_queued_work,
        do_mailbox_send=send_mailbox, mailbox_inbox: Path | None = None,
        do_cap_check=is_usage_capped,
        now_ts: int | None = None,
        adaptive: bool | None = None) -> int:
    cfg = AGENT_PANE.get(agent)
    if cfg is None:
        print(f"[keep_going] unknown agent {agent!r} (known: {list(AGENT_PANE)})", file=sys.stderr)
        return 64

    def deliver() -> bool:
        """Delivery-mode dispatch (T-1827 R1): the decision logic above is identical
        for every agent; only this final step is per-agent switchable. Fail-open to
        send-keys for any agent missing from DELIVERY_MODE."""
        if DELIVERY_MODE.get(agent, "send-keys") == "mailbox":
            return do_mailbox_send(agent, prompt, inbox=mailbox_inbox)
        return do_inject(cfg["pane_target"], prompt)

    pane = do_capture(cfg["pane_target"], cfg["unix_user"])
    if pane is None:
        print(f"[keep_going] {agent}: pane uncapturable — skip (fail safe)", file=sys.stderr)
        return 0  # not an error: a missed nudge is harmless
    idle = is_idle_at_prompt(pane)
    if idle is None:
        print(f"[keep_going] {agent}: idle state inconclusive — skip (fail safe)")
        return 0
    if idle is False:
        print(f"[keep_going] {agent}: busy or modal — skip (no nudge into active work)")
        return 0

    # T-2024: parked-at-idle is necessary but not sufficient — a usage/capability
    # cap can leave the pane LOOKING idle while every turn would just re-hit the
    # cap. Nudging then wastes the remaining budget and can push the agent deeper
    # in. Skip on cap (subtractive; fail-open, so a detection miss just nudges as
    # before). Placed before the backoff block so a capped skip does not consume
    # the adaptive-backoff budget.
    now_for_cap = now_ts if now_ts is not None else int(time.time())
    if do_cap_check(cfg, agent, now_for_cap):
        print(f"[keep_going] {agent}: usage/capability cap detected — skip (no nudge into a hit cap)")
        return 0

    # Parked at the idle prompt → nudge CANDIDATE. Adaptive backoff gates whether
    # we actually nudge this cycle (legacy mode nudges every idle cycle).
    use_adaptive = ADAPTIVE if adaptive is None else adaptive
    if use_adaptive:
        # T-2583: the whole read-decide-deliver-write section is one critical
        # section per agent — held across deliver() and the final write_state,
        # not just the read/decide, or two concurrent invocations could both
        # decide should_nudge=True before either persists (a double-nudge).
        with _agent_lock(agent):
            sp = _state_path(agent)
            state = read_state(sp)
            # T-1500 (B): a restarted session voids the escalation measured against the
            # OLD session — reset to base cadence so a fresh parked agent is nudged fast.
            state, sess_reset = maybe_reset_for_new_session(state, do_session_id(cfg))
            cur_work = do_work_ts(cfg)
            now = now_ts if now_ts is not None else int(time.time())
            # T-1926: opt-in only. Agents without `queue_aware_backoff` pass floor_exp=None
            # and take the identical pre-T-1926 path, including skipping the queue scan.
            floor_exp = None
            if cfg.get("queue_aware_backoff") and do_queued_work(cfg):
                floor_exp = QUEUED_BACKOFF_FLOOR_EXP
            should, new_state, reason = decide_nudge(state, cur_work, now, floor_exp=floor_exp)
            if floor_exp is not None:
                reason += f" [queue non-empty — backoff floored at 2^{floor_exp} cycles]"
            new_state["session_id"] = state.get("session_id")
            if sess_reset:
                reason += " [new session — backoff reset]"
            if not should:
                write_state(sp, new_state)
                print(f"[keep_going] {agent}: parked but {reason} — skip")
                return 0
            if deliver():
                write_state(sp, new_state)
                print(f"[keep_going] {agent}: parked at idle prompt — nudged ({reason})")
                return 0
            # Delivery failed: do NOT persist new_state so the backoff isn't consumed by
            # a failed send — retry next cycle.
            print(f"[keep_going] {agent}: nudge delivery failed", file=sys.stderr)
            return 8

    # Legacy (KEEP_GOING_ADAPTIVE=0): nudge every idle cycle.
    if deliver():
        print(f"[keep_going] {agent}: parked at idle prompt — nudged to keep going (legacy)")
        return 0
    print(f"[keep_going] {agent}: nudge delivery failed", file=sys.stderr)
    return 8


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Nudge a parked agent to keep going (idle-gated, adaptive backoff).")
    ap.add_argument("--agent", required=True, choices=sorted(AGENT_PANE))
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    args = ap.parse_args(argv)
    return run(args.agent, prompt=args.prompt)


if __name__ == "__main__":
    raise SystemExit(main())
