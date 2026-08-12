#!/usr/bin/env bash
# claude-bot-supervise.sh — systemd-supervised wrapper for the ORCHESTRATOR's
# (Orchestrator / HO) Telegram bot. The sg-wrapped variant of the supervise pattern.
#
# Same core as recovery-bot-supervise.sh / specialist-bot-supervise.sh (T-0509/T-0755):
# the old Type=oneshot + RemainAfterExit unit returned 0 immediately so systemd
# reported "active (exited)" forever even after the tmux session + inner `claude`
# died (nothing respawned it — the 2026-05-21 Recovery outage mode). This wrapper
# keeps the SAME detached tmux session but stays alive as systemd's MainPID for
# the session's lifetime; on session death it exits and Restart=always respawns
# it. CLAUDE_ARGS MUST carry --channels for a bidirectional bot.
#
# WHAT MAKES THIS THE ORCHESTRATOR VARIANT — the `sg pantheon-shared -c` wrap
# (T-0783): the orchestrator's pantheon-config clone is the CANONICAL one that
# multiple accounts (kino, etc.) write to via the `pantheon-shared` group, so the
# tmux server + inner claude TUI must inherit that group to write
# pantheon-config/.git/objects without permission failures. user-mode systemd
# CANNOT grant supplementary groups (T-0659/T-0661), so the group is inherited
# in user-space via `sg pantheon-shared -c '...'`.
#   • The RECOVERY (recovery-bot-supervise.sh) and SPECIALIST (specialist-bot-supervise.sh)
#     variants OMIT the sg-wrap on purpose: they operate on their OWN per-account
#     clones, which they own outright, so no shared-group inheritance is needed.
#   • So sg-wrap is an ORCHESTRATOR-ONLY add-on, NOT the universal spine — pick
#     the variant that matches the role's clone ownership.
#
# NOTE: catches only the process-exit / inert-daemon mode. The "alive but every
# request 400s" mode needs a health probe (context_fill_monitor api-health / drain).
set -euo pipefail

SESSION="claude-telegram-bot"
CLAUDE_BIN="/home/claude/.npm-global/bin/claude"
# ⚠️ THESE TWO FLAGS WERE LIVE-ONLY UNTIL 2026-08-03 and the repo copy had NEITHER, so a
# redeploy from this file would have silently dropped both — the model pin falling back to
# settings.json, which still says `claude-fable-5[1m]`, a model that is gone fleet-wide.
# Backported here so repo == live. See the drift class in T-2444.
#
# --permission-mode default pins the pre-v2.1.200 launch default (CC v2.1.200 flipped
# unset to Manual; unattended relaunches stall at the first tool prompt). Fleet ruling
# 2026-07-06 (orchestrator, T-1541/T-1544).
#
# --model: THE LAUNCH FLAG IS THE AUTHORITATIVE PIN and outranks settings.json — see
# bugs/supervisor-launch-flag-outranks-settings-json-on-model-pin.md. Moved
# claude-opus-5 -> claude-opus-4-7 on 2026-08-03 at the operator's request (TG 4958), after
# verifying the id resolves with a real one-shot round-trip on a plugin-free seat.
# Then 4-7 -> claude-opus-4-8 on 2026-08-03 at the operator's request (fleet uniform on
# opus 4.8); live supervise pin was flipped same day, this repo copy backported to match.
# NOTE: the cap-autoswitch mechanism this note used to guard against
# (model_stepper.py) was removed (T-2737, 2026-08-10 — stepping policy retired
# fleet-wide in favor of the opus-4-8 standard). context_fill_monitor's
# model_pin is still `claude-fable-5` regardless — see the warning above
# _MONITOR_TUNING in context_fill_monitor.py (T-2134/T-2136 history) before
# "tidying" that pin.
CLAUDE_ARGS="--channels plugin:telegram@claude-plugins-official --model claude-opus-4-8 --permission-mode default"

# (Re)create the session only if absent — idempotent across restarts. Wrapped in
# `sg pantheon-shared -c` so the tmux server + inner claude inherit the shared
# group (orchestrator-only; see header).
if ! sg pantheon-shared -c "tmux has-session -t $SESSION" 2>/dev/null; then
  sg pantheon-shared -c "tmux new-session -d -s $SESSION '$CLAUDE_BIN $CLAUDE_ARGS'"
fi

# Block as systemd's MainPID until the session goes away (5s poll).
while sg pantheon-shared -c "tmux has-session -t $SESSION" 2>/dev/null; do
  sleep 5
done

# Session ended → exit so systemd's Restart=always brings us back.
exit 0
