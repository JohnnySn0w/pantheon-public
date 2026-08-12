#!/usr/bin/env bash
# acp-bridge-supervise.sh — systemd-supervised wrapper for the mailbox->ACP inbound
# bridge (T-1633 Unit 2, ORG-031). Modeled on ancestor-bot-supervise.sh: hosts the
# bridge as a detached tmux session so the operator can still `tmux capture-pane` /
# `send-keys` for debugging (operator surface preserved, per the T-1630 spike's
# rung-5 discussion), and blocks as systemd's MainPID for as long as that session
# lives; on session death it exits and Restart=always respawns it.
#
# NOT installed/enabled by any provision script (T-1633 Unit 2 is BUILD-ONLY). The
# pilot flip — installing + enabling pantheon-acp-bridge@<agent>.service — is a
# later, supervised HO+the operator cross-account step.
#
# SAFETY INVARIANT — clean room, NEVER a shadow API key:
#   This script must NEVER set or pass through any ANTHROPIC_*/CLAUDE_CODE_* env
#   var. tools/acp_bridge.py's assert_clean_room() is the real gate (hard-exits on
#   any such var, even empty-string); the strip below is defense-in-depth so this
#   script itself can never be the leak (a leak silently flips subscription billing
#   to metered API billing — a money event).
set -euo pipefail

for name in $(compgen -v); do
  case "$name" in
    ANTHROPIC*|CLAUDE_CODE*) unset "$name" ;;
  esac
done

# systemd user units run with a minimal PATH (/usr/local/bin:/usr/bin) that
# does NOT include the per-account npm prefix where the pinned wrapper lives
# (~/.npm-global/bin/claude-code-acp). Without this, the engine's Popen fails
# FileNotFoundError and the unit crash-loops — found live on the first specialist
# flip attempt (T-1689, 2026-07-12). Prepend rather than replace so an
# operator-supplied PATH (or the trap test's stubbed tmux dir) still resolves.
export PATH="$HOME/.npm-global/bin:$PATH"

: "${ACP_BRIDGE_AGENT:?ACP_BRIDGE_AGENT is required (the persona being piloted, e.g. orchestrator)}"

SESSION="${ACP_BRIDGE_SESSION:-acp-bridge-$ACP_BRIDGE_AGENT}"
# The mangled-cwd invariant (tools/context_fill_monitor.py's per-agent jsonl_dir
# mapping): each agent's REAL cwd for its live session is its own $HOME, so the
# bridge's --cwd must match that, not the pantheon-config checkout.
BRIDGE_CWD="${ACP_BRIDGE_CWD:-$HOME}"
# Path-A hub inbox root (T-1015, 2026-06-09): /home/the-ancestor/mailbox/<agent>,
# mode 2770 group veneration. tools/mailbox.py's `inbox` param is the ROOT dir
# (it owns {tmp,new,cur} under it), not new/ itself.
BRIDGE_INBOX="${ACP_BRIDGE_INBOX:-/home/the-ancestor/mailbox/$ACP_BRIDGE_AGENT}"
# Working-turn agents need more than the 300s default (mnemosyne redelivery loop,
# T-1830 residue): override per-agent via unit drop-in Environment=.
BRIDGE_PROMPT_TIMEOUT="${ACP_BRIDGE_PROMPT_TIMEOUT:-300}"
# Mailbox consumer-lease TTL (T-1935/T-1990). A deep multi-message drain is
# bounded by prompt_timeout x batch_depth; that product must stay under this or
# a second bridge instance could reap the lease mid-drain. Raise in lockstep
# with ACP_BRIDGE_PROMPT_TIMEOUT via unit drop-in Environment= if it ever grows.
# T-2287: 7200 -> 300. This is the HOLDER side, and the holder's recorded TTL is
# what _holder_is_stale actually judges against, so this number decides how long a
# WEDGED bridge is protected from reaping. It had to outlast a whole agent turn
# while the consumer lease spanned the handler; it now only has to outlast the
# claim step. Keep >= nothing and <= the acquirer constant in mailbox_mcp.py —
# test_lease.TestAcquirerTtlMustNotUndercutHolder parses BOTH and enforces it.
BRIDGE_CONSUME_TTL="${ACP_BRIDGE_CONSUME_TTL:-300}"
BRIDGE_WRAPPER_CMD="${ACP_BRIDGE_WRAPPER_CMD:-claude-code-acp}"
BRIDGE_POLL_INTERVAL="${ACP_BRIDGE_POLL_INTERVAL:-5}"
# T-2038 usage-cap circuit breaker: seconds to defer drains after a session/prompt
# usage-cap error, so a capped seat is not re-hammered every poll. Also bounds
# recovery latency after the cap resets. Override per-agent via unit drop-in.
BRIDGE_CAP_COOLDOWN="${ACP_BRIDGE_CAP_COOLDOWN:-300}"
# T-1935 half (a): the agent's live main-session pane (e.g. claude-research-bot:0).
# When set, the bridge defers drains while that pane is affirmatively busy — the
# wrapper must not run possibly-repo-mutating turns interleaved with the live
# session on the same checkout. Empty = gate off (probe also fails open on a
# missing/renamed pane, so a stale value degrades to pre-T-1935 behavior, never
# a stalled pipeline). Set per-agent via unit drop-in Environment=.
BRIDGE_MAIN_PANE="${ACP_BRIDGE_MAIN_PANE:-}"
# T-2259: when set (any non-empty value) AND --main-pane is configured, an
# affirmatively idle main pane takes the drain via a mailbox_wake kick instead
# of paying one ACP turn per message. Opt-in per agent via unit drop-in.
BRIDGE_PREFER_MAIN_SEAT="${ACP_BRIDGE_PREFER_MAIN_SEAT:-}"
# The hub group. `veneration` was RETIRED (renamed to pantheon-mailbox, gid 1006)
# and no longer exists on the host — `sg veneration -c ...` exits 1 before the
# bridge ever starts, and this default is unconditionally what the ExecStart wraps
# (no unit sets ACP_BRIDGE_GROUP). Every bridge currently running predates the
# rename, so nothing was observed breaking; the bill lands on the next RESTART,
# which for the paused seats is the un-pause. Verified 2026-08-03:
#   sg veneration      -> "group 'veneration' does not exist", rc=1
#   sg pantheon-mailbox -> rc=0
BRIDGE_GROUP="${ACP_BRIDGE_GROUP:-pantheon-mailbox}"
BRIDGE_LOG="${ACP_BRIDGE_LOG:-$HOME/.local/state/pantheon/acp-bridge-$ACP_BRIDGE_AGENT.log}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRIDGE_PY="$REPO_ROOT/tools/acp_bridge.py"

mkdir -p "$(dirname "$BRIDGE_LOG")"

# The orphaned-cgroup hazard (T-1689): on the pilot account (claude-research)
# a tmux SERVER ALREADY EXISTS (specialist's TUI), so this session lands in that
# pre-existing server, OUTSIDE the systemd unit's cgroup. Without this trap,
# `systemctl --user stop/disable --now` would kill only this script's
# MainPID and ORPHAN the live bridge+wrapper -> rollback would create the
# exact two-consumer split-Maildir the design prevents. Tear the session
# down on SIGTERM (systemd stop) or any exit path.
# `|| true` is LOAD-BEARING, not defensive habit (T-2264 follow-up, 2026-07-29).
# Under `set -e` (line 19) a failing command in an EXIT trap OVERRIDES the
# script's own `exit 0`. This trap runs `kill-session` on every exit path — and
# on the NORMAL path the session is already gone, because the session going away
# is precisely what ends the poll loop below. So kill-session necessarily fails
# there, and the script that just completed successfully reported status=1.
#
# Consequence, measured: EVERY clean bridge shutdown was recorded as a unit
# FAILURE — 7 for mnemosyne and 8 for specialist in 24h. Harmless until T-2264 wired
# OnFailure= onto this unit, at which point each one pages Recovery. A recovery
# alert that fires on every routine restart is one nobody reads, which would
# have quietly undone the reason T-2264 exists (a real 90-minute bridge outage
# that nobody saw).
#
# Failing to find the session means there is nothing to tear down — that is the
# trap's goal met, not missed. Real start failures still surface: `tmux
# new-session` below runs under the same `set -e` and aborts non-zero.
trap 'tmux kill-session -t "$SESSION" 2>/dev/null || true' TERM EXIT

# (Re)create the session only if it isn't already up — idempotent across restarts.
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  # The Path-A hub is mode-2770 group veneration; a user-mode systemd service
  # carries only the account's primary group (user systemd cannot grant
  # supplementary groups — SupplementaryGroups= fails 216 on --user units), so the
  # inbox-reading bridge process needs the `sg veneration -c` wrap exactly as
  # pantheon-mailbox-wake@.service does (T-1015). Build the inner command via an
  # array + `printf %q` so agent/cwd/inbox values are always safely quoted
  # regardless of content, then wrap it for sg, then wrap THAT for tmux.
  inner_argv=(python3 "$BRIDGE_PY" --agent "$ACP_BRIDGE_AGENT" --cwd "$BRIDGE_CWD"
              --inbox "$BRIDGE_INBOX" --wrapper-cmd "$BRIDGE_WRAPPER_CMD"
              --poll-interval "$BRIDGE_POLL_INTERVAL" --prompt-timeout "$BRIDGE_PROMPT_TIMEOUT"
              --consume-ttl "$BRIDGE_CONSUME_TTL" --cap-cooldown "$BRIDGE_CAP_COOLDOWN")
  if [ -n "$BRIDGE_MAIN_PANE" ]; then
    inner_argv+=(--main-pane "$BRIDGE_MAIN_PANE")
  fi
  if [ -n "$BRIDGE_PREFER_MAIN_SEAT" ]; then
    inner_argv+=(--prefer-main-seat)
  fi
  inner_cmd=$(printf '%q ' "${inner_argv[@]}")
  sg_cmd="/usr/bin/sg $BRIDGE_GROUP -c $(printf '%q' "$inner_cmd")"
  tmux_cmd="$sg_cmd 2>&1 | tee -a $(printf '%q' "$BRIDGE_LOG")"
  tmux new-session -d -s "$SESSION" "$tmux_cmd"
fi

# Block as systemd's MainPID until the session goes away (poll, not busy-wait).
while tmux has-session -t "$SESSION" 2>/dev/null; do
  sleep 5
done

exit 0
