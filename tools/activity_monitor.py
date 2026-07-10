#!/usr/bin/env python3
"""activity_monitor — fire structural poke to an agent's pane when one of:

  P1.a — idle >1hr with ≥1 actionable HO-targeted queue item.
  P1.b — the operator absent >15min + agent in self-imposed await state + queue.
  P1.d — agent's pane shows a rate-limit API-error as latest visible state +
         jsonl confirms the rate-limit assistant turn is ≥5min old + no
         further activity since (turn was silently abandoned by the runtime).

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
  ACTIVITY_MONITOR_P1D_AGE_SECONDS      — override P1.d rate-limit-age threshold.
  ACTIVITY_MONITOR_BRIDGE_HEALTH_URL    — override bridge /health URL.

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
import re
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"

# P1.a — idle threshold. Per ORG-011 ratified spec.
P1A_IDLE_THRESHOLD_SECONDS = 3600  # 1 hour

# P1.b — operator-absent threshold. Per ORG-011 ratified spec.
P1B_OPERATOR_ABSENT_THRESHOLD_SECONDS = 15 * 60

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

# Bridge /health endpoint (used for P1.b operator-inbound state).
DEFAULT_BRIDGE_HEALTH_URL = "http://127.0.0.1:8765/health"

# Await-state regex (P1.b heuristic; Recovery-discretion may override at runtime
# but the script-level signal is regex-only). Patterns chosen per ORG-011 §P1.b.
AWAIT_STATE_REGEX = re.compile(
    r"(waiting on|pausing on|ready when you|no further chain-dispatch until|deferring to you|awaiting your)",
    re.IGNORECASE,
)

# P1.d — rate-limit error pattern. Matches both Claude Code's synthetic
# "API Error: Server is temporarily limiting requests..." and any variant
# that surfaces "rate limit" / "rate limited" in the assistant text.
RATE_LIMIT_REGEX = re.compile(
    r"(API Error.*[Rr]ate [Ll]imit|temporarily limiting requests|[Rr]ate [Ll]imited)",
)

# P1.b extension (T-0297) — tool_use names that block Claude Code's loop on
# user input. When the latest assistant turn is one of these, the agent is
# in await-state regardless of whether its text content matches the await
# regex (these tool_use turns typically have NO text content alongside).
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
        "jsonl_dir": Path("/home/claude/.claude/projects/-home-claude"),
        "ledger_agent_id": "orchestrator",
        "ticket_targets": frozenset({"orchestrator", "orchestrator"}),
        "pantheon_config_path": Path("/home/claude/pantheon-config"),
        "bridge_inbound_key": "orchestrator",
        # P1.c (ORG-037 Layer 3): the session's team root (T-1290). active-team
        # is signalled by `<teams_dir>/*/inboxes` existing. Only agents with a
        # teams_dir configured are eligible for the Layer-3 backstop.
        "teams_dir": Path("/home/claude/.claude/teams"),
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
) -> None:
    """Persist the last-poke state. The bounded-poke fields (T-1363) are optional
    and backward-compatible: legacy callers omit them and the file is unchanged
    in shape except for an added consecutive_ineffective=0.

    - pre_poke_jsonl_mtime: the main jsonl mtime captured RIGHT BEFORE this poke,
      so the NEXT cycle can tell whether this poke advanced the session (worked).
    - consecutive_ineffective: running count of pokes that did NOT advance.
    - escalated: this fire suppressed the poke + escalated (stuck, not idle).
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
    advances its main jsonl mtime. So if the prior poke (SAME predicate) recorded
    a pre-poke mtime and the jsonl has NOT advanced past it, that poke was
    ineffective — the session is stuck, not idle.

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

    The `ledger/*/*.jsonl` glob structurally excludes the flat
    `2026.jsonl.archive-pre-d10-shard` (it lives at ledger/ root, not in a
    subfolder), matching the helper's archive exclusion. Pre-migration the
    glob matches nothing and only the legacy file is read — no behavior change.

    Runs remotely under `sudo bash -c`; built as a single shell string. No
    untrusted interpolation — repo_path is a config-derived Path.
    """
    ledger_dir = f"{repo_path}/ledger"
    legacy = f"{ledger_dir}/2026.jsonl"
    # `ls -d ... 2>/dev/null` tolerates the no-match case; `cat` of a
    # non-existent legacy path is suppressed. Files are emitted in sorted
    # (chronological) segment order, legacy last, then reversed by tac.
    return (
        f"{{ for f in $(ls -d {ledger_dir}/*/*.jsonl 2>/dev/null | sort); "
        f"do cat \"$f\"; done; "
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
        if obj.get("agent") != ledger_agent_id:
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
        if obj.get("agent") != ledger_agent_id:
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


def count_actionable_tickets(repo_path: Path, targets: frozenset[str], sudo_user: str) -> int:
    """Count tickets in inbox/ + active/ where target is in `targets`, no
    unmet depends_on, not_before (if set) is in the past, the labels field
    does not contain interactive-required, AND blocked_by is not "event"
    (T-0644: event-driven holds wait on a structural witness — pane state,
    cap-hit cycle, calendar tick — not on HO-availability, so they must
    not count toward the autonomous-actionable queue).

    Reads via sudo-as-<sudo_user> shell pipeline since the agent's
    pantheon-config tree (e.g., /home/claude/pantheon-config) is owned by
    the agent's account and Recovery (the caller) can't traverse the 750-mode
    home directory above it. Single sudo call walks both state dirs +
    extracts the relevant frontmatter fields via awk; Python parses the
    structured output.

    Best-effort: any sudo / shell / parse failure returns 0 (no actionable
    work detected). Fail-open keeps the predicate from firing on flaky
    cross-account state reads.
    """
    inbox_dir = repo_path / "tickets" / "inbox"
    active_dir = repo_path / "tickets" / "active"
    # awk emits one line per ticket: "<path>|<target>|<depends_on>|<not_before>|<labels>|<blocked_by>"
    # — stops scanning each file at the closing '---' of the frontmatter.
    awk_prog = (
        r"""BEGIN { in_fm=0; target=""; deps=""; nb=""; labels=""; blocked=""; path="" }
        FNR==1 { if (path!="") print path "|" target "|" deps "|" nb "|" labels "|" blocked;
                 path=FILENAME; in_fm=0; target=""; deps=""; nb=""; labels=""; blocked="" }
        /^---$/ { if (in_fm==0) { in_fm=1 } else { in_fm=2 } }
        in_fm==1 && /^target:/ { sub(/^target:[[:space:]]*/, ""); target=$0 }
        in_fm==1 && /^depends_on:/ { sub(/^depends_on:[[:space:]]*/, ""); deps=$0 }
        in_fm==1 && /^not_before:/ { sub(/^not_before:[[:space:]]*/, ""); nb=$0 }
        in_fm==1 && /^labels:/ { sub(/^labels:[[:space:]]*/, ""); labels=$0 }
        in_fm==1 && /^blocked_by:/ { sub(/^blocked_by:[[:space:]]*/, ""); blocked=$0 }
        END { if (path!="") print path "|" target "|" deps "|" nb "|" labels "|" blocked }"""
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
        return 0
    if result.returncode != 0:
        return 0
    count = 0
    targets_lower = {t.lower() for t in targets}
    now = datetime.now(timezone.utc)
    for line in result.stdout.splitlines():
        # Tolerate both old 5-field and new 6-field shapes so test fixtures
        # without the blocked_by column still parse cleanly.
        parts = line.split("|", 5)
        if len(parts) < 5:
            continue
        _path = parts[0]
        target = parts[1]
        deps = parts[2]
        nb = parts[3]
        labels = parts[4]
        blocked_by = parts[5] if len(parts) == 6 else ""
        if target.strip().lower() not in targets_lower:
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
        count += 1
    return count


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


def latest_assistant_message_body(jsonl_path: Path, sudo_user: str) -> str | None:
    """Read the most-recent assistant-message text content from the jsonl.

    Iterates tac'd lines, returns the first line where type=='assistant' and
    message.content has a 'text' block. Concatenates text blocks if multiple.
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
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        if parts:
            return "\n".join(parts)
    return None


def detect_await_state(body: str) -> bool:
    return bool(AWAIT_STATE_REGEX.search(body))


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


def capture_pane(pane: str, sudo_user: str) -> str | None:
    """Capture the visible content of the agent's tmux pane.

    Returns None on any subprocess failure (sudo denied, tmux not running,
    etc.) — fail-open so P1.d doesn't fire on flaky reads.
    """
    cmd = _sudo(sudo_user) + [
        "bash", "-c",
        f"XDG_RUNTIME_DIR=/run/user/$(id -u) tmux capture-pane -p -t {pane}",
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
    jsonl_ts = latest_jsonl_mtime(cfg["jsonl_dir"], cfg["sudo_user"]) or 0
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
    queue_count = count_actionable_tickets(cfg["pantheon_config_path"], cfg["ticket_targets"], cfg["sudo_user"])
    return queue_count >= 1


def check_p1b(
    cfg: dict,
    bridge_url: str,
    operator_absent_threshold_seconds: int,
) -> bool:
    """P1.b: the operator absent + HO in await-state + queue.

    Await-state has two sources (T-0297 extension):
      1. Text-content regex (AWAIT_STATE_REGEX) on the latest assistant turn.
      2. Latest assistant turn is a user-blocking tool_use (AskUserQuestion etc.).
    """
    health = fetch_bridge_health(bridge_url)
    if health is None:
        return False  # fail-open
    absent_seconds = operator_last_inbound_age_seconds(health, cfg["bridge_inbound_key"])
    if absent_seconds is None or absent_seconds < operator_absent_threshold_seconds:
        return False
    jsonl = find_latest_jsonl(cfg["jsonl_dir"], cfg["sudo_user"])
    if jsonl is None:
        return False
    # Signal 1: text-regex await
    body = latest_assistant_message_body(jsonl, cfg["sudo_user"])
    text_await = body is not None and detect_await_state(body)
    # Signal 2: interactive tool_use await (T-0297)
    interactive_tool = latest_assistant_interactive_tool_use(jsonl, cfg["sudo_user"])
    tool_await = interactive_tool is not None
    if not (text_await or tool_await):
        return False
    queue_count = count_actionable_tickets(cfg["pantheon_config_path"], cfg["ticket_targets"], cfg["sudo_user"])
    return queue_count >= 1


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
    jsonl_ts = latest_jsonl_mtime(cfg["jsonl_dir"], cfg["sudo_user"]) or 0
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
    " sop/standard/ho-absent-operator-keep-working.md: record progress on the"
    " awaited item, shelve it (ticket.py block <id> --reason ...), and swap to"
    " the next actionable. Pull queue now."
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


def send_poke(cfg: dict, prompt: str) -> bool:
    """Send the poke prompt to the agent's tmux pane.

    T-0538: delegates to the target user's tools/send_keys_submit.sh via
    sudo -nu <sudo_user>. The helper handles text send, paste-mode settle,
    Enter send, and submit verification with retry. XDG_RUNTIME_DIR is set
    in the bash -c string so the helper's tmux calls reach the target user's
    session bus (same pattern as capture_pane()). Returns True iff helper
    exits 0; False on any failure (send-keys error, timeout, OSError).
    """
    pane = cfg["tmux_target_pane"]
    sudo_user = cfg["sudo_user"]
    helper = Path(cfg["pantheon_config_path"]) / "tools" / "send_keys_submit.sh"
    cmd = _sudo(sudo_user) + [
        "bash", "-c",
        f'XDG_RUNTIME_DIR=/run/user/$(id -u) INJECT_ENTER_DELAY={INJECT_ENTER_DELAY}'
        f' bash {helper} {pane} {json.dumps(prompt)}',
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=_TMUX_TIMEOUT_SECONDS
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


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


def _escalate_stuck(cfg: dict, agent: str, predicate: str, msg: str, vocab: str,
                    state_path: Path, cur_mtime: int, consecutive: int) -> None:
    """Suppress-and-escalate: record the escalated state and, ONLY on the
    transition into escalated (not on every subsequent cycle), route the alert to
    Recovery. Alert-routing failure never raises (must not wedge the monitor)."""
    prior = read_poke_state(state_path) or {}
    already = bool(prior.get("escalated")) and prior.get("predicate") == predicate
    if not already:
        try:
            _route_failure_alert(agent, msg, vocab)
            _log(agent, f"{predicate}: ESCALATED to Recovery (route_failure_alert) — {msg[:80]}")
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
# Main
# ---------------------------------------------------------------------------


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
    poke send failed).

    Ship-dark: bounded_enabled=False reproduces the legacy send_poke +
    write_poke_state behavior exactly — so with the flag off, nothing changes.
    """
    if bounded_enabled:
        # Pre-poke state-gate (T-1363 inc.3): a CYBER-BLOCKED session keeps emitting
        # refusals, so its jsonl ADVANCES after a poke — which would fool the
        # effectiveness signal (jsonl-advanced == "poke worked") into poking forever.
        # Detect it up front and SUPPRESS: it needs a RESTART, not a poke. (Restart-
        # routing via route_failure_alert is the next increment; for now suppress +
        # an escalated marker, which is the same shape as the effectiveness-suppress.)
        if _session_cyber_blocked(cfg):
            cur_mtime = latest_jsonl_mtime(cfg["jsonl_dir"], cfg["sudo_user"]) or 0
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
        cur_mtime = latest_jsonl_mtime(cfg["jsonl_dir"], cfg["sudo_user"]) or 0
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
        if send_poke(cfg, prompt):
            _log(agent, f"{predicate} poke sent (bounded; consecutive_ineffective={consecutive})")
            write_poke_state(
                state_path, agent, predicate,
                pre_poke_jsonl_mtime=cur_mtime, consecutive_ineffective=consecutive,
            )
            return 0
        _log(agent, f"{predicate} poke send failed")
        return 1
    # Legacy / ship-dark path — byte-for-byte the prior behavior.
    if send_poke(cfg, prompt):
        _log(agent, f"{predicate} poke sent")
        write_poke_state(state_path, agent, predicate)
        return 0
    _log(agent, f"{predicate} poke send failed")
    return 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ORG-011 Phase B activity monitor (per-agent).")
    parser.add_argument(
        "--agent",
        choices=sorted(AGENT_CONFIG.keys()),
        default="orchestrator",
        help="Which agent's activity to monitor (default: orchestrator).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    agent = args.agent
    cfg = AGENT_CONFIG[agent]

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

    state_path = _state_path_for_agent(agent)
    cooldown_map = {
        "p1a": cooldown_seconds,
        "p1b": cooldown_seconds,
        "p1d": p1d_cooldown_seconds,
        "p1c": p1c_cooldown_seconds,
    }

    if in_cooldown(state_path, cooldown_map):
        _log(agent, "in cooldown; skipping")
        return 0

    # Check P1.a first (it's a stronger signal: completely-idle).
    if check_p1a(cfg, idle_threshold):
        _log(agent, "P1.a fired: idle > threshold + queue ≥ 1")
        return _dispatch_poke(cfg, agent, "p1a", P1A_POKE_PROMPT, state_path,
                              bounded_enabled=bounded_poke_enabled,
                              max_ineffective=max_ineffective_pokes)

    # P1.b: the operator away + agent self-waiting.
    if check_p1b(cfg, bridge_url, operator_absent_threshold):
        _log(agent, "P1.b fired: the operator absent + await-state + queue ≥ 1")
        return _dispatch_poke(cfg, agent, "p1b", P1B_POKE_PROMPT, state_path,
                              bounded_enabled=bounded_poke_enabled,
                              max_ineffective=max_ineffective_pokes)

    # P1.d: rate-limit aborted the turn + has persisted past threshold.
    if check_p1d(cfg, p1d_age_threshold):
        _log(agent, "P1.d fired: rate-limit visible + stale > threshold")
        if send_poke(cfg, P1D_RETRY_PROMPT):
            _log(agent, "P1.d retry-inject sent")
            write_poke_state(state_path, agent, "p1d")
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

    _log(agent, "no predicate fired; bail")
    return 0


if __name__ == "__main__":
    sys.exit(main())
