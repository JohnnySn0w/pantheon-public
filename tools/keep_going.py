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
the base cadence. Real *queued* work is already covered by activity_monitor P1.a (idle +
actionable queue → poke at 15-30 min), so backing this catch-all explore-nudge off hard
does not re-introduce the parked-with-greenlit-work failure it was built to prevent.
Disable with KEEP_GOING_ADAPTIVE=0 (reverts to legacy nudge-every-idle-cycle).

The idle/busy sentinels mirror inbox_consumer._WEDGE_* (T-0716) — kept as a small local
copy so this script stays self-contained and import-side-effect-free (parallel + separate,
per the directive). If those sentinels drift, update both.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AGENT_PANE = {
    "orchestrator": {
        "pane_target": "claude-telegram-bot:0", "unix_user": "claude",
        "pantheon_config_path": "/home/claude/pantheon-config",
        "ledger_agent_id": "orchestrator",
        "projects_dir": "/home/claude/.claude/projects/-home-claude",
    },
    "specialist": {
        "pane_target": "claude-research-bot:0", "unix_user": "claude-research",
        "pantheon_config_path": "/home/claude-research/pantheon-config",
        "ledger_agent_id": "specialist",
        "projects_dir": "/home/claude-research/.claude/projects/-home-claude-research",
    },
    "recovery": {
        "pane_target": "claude-telegram-bot-fallback:0", "unix_user": "claude-fallback",
        "pantheon_config_path": "/home/claude-fallback/pantheon-config",
        "ledger_agent_id": "recovery",
        "projects_dir": "/home/claude-fallback/.claude/projects/-home-claude-fallback",
    },
}

# Adaptive-backoff knobs (env-overridable). Default ON per the operator's TG 3508.
ADAPTIVE = os.environ.get("KEEP_GOING_ADAPTIVE", "1") != "0"
# Max exponent: 2^cap idle cycles between nudges for a perpetually-idle agent.
# 2^5 = 32 cycles ≈ 8 h at the ~15-min timer cadence.
BACKOFF_CAP_EXP = int(os.environ.get("KEEP_GOING_BACKOFF_CAP", "5"))
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
    cross-account fallback."""
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


def decide_nudge(state: dict, cur_work_ts: int | None, now_ts: int,
                 cap: int = BACKOFF_CAP_EXP) -> tuple[bool, dict, str]:
    """Pure backoff decision: (should_nudge, new_state, reason).

    A nudge is DUE only after 2^min(consecutive_ineffective, cap) idle cycles
    since the last nudge (1, 2, 4, …, 2^cap). When due, the PRIOR nudge is judged:
    effective iff the work signal advanced since it (cur_work_ts > work_ts_at_last_nudge).
    Each ineffective nudge doubles the interval; an effective one resets it to 1."""
    consec = int(state.get("consecutive_ineffective", 0) or 0)
    cycles = int(state.get("cycles_since_nudge", 0) or 0) + 1
    last_nudge_ts = state.get("last_nudge_ts")
    threshold = 2 ** min(max(consec, 0), cap)
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
def run(agent: str, *, prompt: str = DEFAULT_PROMPT,
        do_capture=capture_pane, do_inject=inject,
        do_work_ts=latest_work_ts, do_session_id=current_session_id,
        now_ts: int | None = None,
        adaptive: bool | None = None) -> int:
    cfg = AGENT_PANE.get(agent)
    if cfg is None:
        print(f"[keep_going] unknown agent {agent!r} (known: {list(AGENT_PANE)})", file=sys.stderr)
        return 64
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

    # Parked at the idle prompt → nudge CANDIDATE. Adaptive backoff gates whether
    # we actually nudge this cycle (legacy mode nudges every idle cycle).
    use_adaptive = ADAPTIVE if adaptive is None else adaptive
    if use_adaptive:
        sp = _state_path(agent)
        state = read_state(sp)
        # T-1500 (B): a restarted session voids the escalation measured against the
        # OLD session — reset to base cadence so a fresh parked agent is nudged fast.
        state, sess_reset = maybe_reset_for_new_session(state, do_session_id(cfg))
        cur_work = do_work_ts(cfg)
        now = now_ts if now_ts is not None else int(time.time())
        should, new_state, reason = decide_nudge(state, cur_work, now)
        new_state["session_id"] = state.get("session_id")
        if sess_reset:
            reason += " [new session — backoff reset]"
        if not should:
            write_state(sp, new_state)
            print(f"[keep_going] {agent}: parked but {reason} — skip")
            return 0
        if do_inject(cfg["pane_target"], prompt):
            write_state(sp, new_state)
            print(f"[keep_going] {agent}: parked at idle prompt — nudged ({reason})")
            return 0
        # Inject failed: do NOT persist new_state so the backoff isn't consumed by a
        # failed send — retry next cycle.
        print(f"[keep_going] {agent}: nudge inject failed", file=sys.stderr)
        return 8

    # Legacy (KEEP_GOING_ADAPTIVE=0): nudge every idle cycle.
    if do_inject(cfg["pane_target"], prompt):
        print(f"[keep_going] {agent}: parked at idle prompt — nudged to keep going (legacy)")
        return 0
    print(f"[keep_going] {agent}: nudge inject failed", file=sys.stderr)
    return 8


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Nudge a parked agent to keep going (idle-gated, adaptive backoff).")
    ap.add_argument("--agent", required=True, choices=sorted(AGENT_PANE))
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    args = ap.parse_args(argv)
    return run(args.agent, prompt=args.prompt)


if __name__ == "__main__":
    raise SystemExit(main())
