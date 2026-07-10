#!/bin/bash
# SessionStart hook (Recovery): run the boot-health self-check and surface DRIFT into
# the fresh session's context. T-1099.
#
# Runs `verify_startup.sh --at-boot` (skips the pane check — unsettled mid-boot;
# skips de-wedge — no-self-inject, T-0559; runs only the SOFT C4/C6/C7/C8 guards).
# It injects (as additionalContext on stdout) ONLY when a SOFT warning is present
# — i.e. real drift like a reverted core.hooksPath (C7/T-1091) or a stalled timer
# (C8/T-1096). An all-green report is suppressed as noise; the point is to make
# the silent-failure classes that bit Recovery this session LOUD at the next boot.
#
# FAIL-OPEN by design: bounded by a timeout, ALWAYS exits 0. A boot-health check
# must NEVER block or slow a session start. Mirrors handoff-auto-read.sh's posture.

cat >/dev/null 2>&1 || true   # drain the hook-input JSON (unused)

VS="$HOME/pantheon-config/tools/verify_startup.sh"
out=""

if [[ -f "$VS" ]]; then
  report="$(timeout 20 bash "$VS" --at-boot 2>&1 || true)"
  if printf '%s' "$report" | grep -q 'WARN:'; then
    out="⚠️ Boot-health self-check (verify_startup --at-boot) flagged SOFT warning(s) — review + remediate:"$'\n'"${report}"$'\n'
  fi
fi

printf '%s' "$out"
exit 0
