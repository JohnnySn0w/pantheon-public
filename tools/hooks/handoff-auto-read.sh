#!/bin/bash
# SessionStart hook (Pantheon personas: orchestrator / specialist / recovery).
#
# Injects (as additionalContext on stdout) two orientation pointers:
#   1. ORG MAP — $HOME/pantheon-config/README.md, ALWAYS if present. Its Read-order
#      section indexes principles, directives, ADRs, tickets, ledger — the org context
#      a fresh session needs (T-0512: instance HANDOFF alone was too little context).
#   2. INSTANCE HANDOFF — $HOME/HANDOFF.md, ONLY if fresh (mtime within $THRESHOLD_HOURS).
#      The freshness guard prevents replay loops on a stale handoff.
#
# $HOME-relative so one canonical script works for all three personas (T-0512 generalize).
# Part of pantheon-config T-0008 deliverable 1 + T-0512.
#
# Failure mode: errors stay silent on stderr; never block session start.

set -e

HANDOFF="$HOME/HANDOFF.md"
ORG_README="$HOME/pantheon-config/README.md"
THRESHOLD_HOURS=1

# Drain stdin (Claude Code passes hook input JSON; we don't use it).
cat >/dev/null 2>&1 || true

out=""

# 1. Org map — always point at it if present (small + cheap; orientation for any session).
if [[ -f "$ORG_README" ]]; then
  out+="Read ${ORG_README} for org orientation — its Read-order section indexes principles, directives, the ADR index, tickets, and the ledger."$'\n'
fi

# 2. Instance handoff — inject only if fresh (mtime within threshold).
if [[ -f "$HANDOFF" ]]; then
  mtime=$(stat -c %Y "$HANDOFF" 2>/dev/null || echo 0)
  now=$(date +%s)
  if (( mtime > 0 && now - mtime <= THRESHOLD_HOURS * 3600 )); then
    out+="Read ${HANDOFF} and follow it. (Auto-injected by handoff-auto-read.sh — file's mtime is within ${THRESHOLD_HOURS}h, indicating a fresh handoff from the previous session.)"$'\n'
  fi
fi

printf '%s' "$out"
exit 0
