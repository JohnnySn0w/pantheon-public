#!/usr/bin/env bash
# T-2629: durable re-assert of the getUpdates 409 resilience patch onto the
# vendored telegram plugin's server.ts.
#
# WHY THIS EXISTS. The telegram plugin ships a getUpdates retry loop that GIVES
# UP permanently after 8 consecutive 409 Conflicts (`if (is409 && attempt >= 8)
# return`). The process then stays alive (MCP stdin holds it) but DEAF to inbound
# — recoverable only by a full restart. Slot-release/contention timing is
# unbounded (a transient diagnostic getUpdates probe, a peer session, Telegram
# slot lag), so a finite give-up budget is the wrong shape: the T-2590/T-2624
# `--scope` cutover hit exactly this (orchestrator's diagnostic probe stole the slot;
# the incumbent poller 409'd — it recovered within budget that round, but a
# longer contention would have gone dark). FIX: never give up on 409; retry
# forever with capped exponential backoff + jitter (jitter desynchronizes two
# contending pollers so they don't tight-ping-pong the slot — the desync is what
# the give-up bought, preserved here without the deafness).
#
# WHY A RE-ASSERT (not just an in-place edit). The plugin files live under
# ~/.claude/plugins/ — OUTSIDE the pantheon-config repo. A plugin-cache rebuild /
# version bump restores the pristine upstream server.ts, silently reverting the
# fix. This mirrors the T-2625 `.mcp.json` durable-pin guard in
# recovery-bot-supervise.sh: self-heal the live copies on every boot.
#
# SAFETY. Marker-guarded + EXACT-LITERAL substitution (no fuzzy regex): a copy is
# rewritten ONLY if it still contains the exact old give-up block AND lacks the
# T-2629 marker. If upstream ever changes the surrounding code the literal match
# fails -> no-op (logged), never a corrupt half-edit. Fail-open: any error exits
# 0 so this can never brick a boot.
set -uo pipefail

MARKER='T-2629: never give up permanently on 409'

read -r -d '' OLD_BLOCK <<'EOF'
      const is409 = err instanceof GrammyError && err.error_code === 409
      if (is409 && attempt >= 8) {
        process.stderr.write(
          `telegram channel: 409 Conflict persists after ${attempt} attempts — ` +
          `another poller is holding the bot token (stray 'bun server.ts' process or a second session). Exiting.\n`,
        )
        return
      }
      const delay = Math.min(1000 * attempt, 15000)
      const detail = is409
        ? `409 Conflict${attempt === 1 ? ' — another instance is polling (zombie session, or a second Claude Code running?)' : ''}`
        : `polling error: ${err}`
      process.stderr.write(`telegram channel: ${detail}, retrying in ${delay / 1000}s\n`)
      await new Promise(r => setTimeout(r, delay))
EOF

read -r -d '' NEW_BLOCK <<'EOF'
      const is409 = err instanceof GrammyError && err.error_code === 409
      // T-2629: never give up permanently on 409. Slot-release/contention timing
      // is unbounded (a transient diagnostic probe, a peer session's getUpdates,
      // Telegram-side slot lag), and a poller that exits this loop stays
      // alive-but-deaf until a full restart — the worst failure mode. Instead
      // retry forever with capped exponential backoff + jitter, so the poller
      // reclaims the slot the instant the conflict clears, and two contending
      // pollers desynchronize (jitter) rather than tight-ping-ponging the slot.
      // The desync is what the old `attempt >= 8 → exit` give-up bought us; the
      // long backoff cap preserves the anti-hammer property without going deaf.
      const cap = 60000
      const base = Math.min(1000 * 2 ** Math.min(attempt - 1, 6), cap) // 1,2,4,…,cap
      const delay = Math.round(base / 2 + Math.random() * (base / 2))  // 50–100% jitter
      if (is409 && attempt === 8) {
        process.stderr.write(
          `telegram channel: 409 Conflict persists after ${attempt} attempts — ` +
          `another poller may be holding the bot token (stray 'bun server.ts' process or a second session). ` +
          `Continuing to retry — will reclaim the slot when it clears.\n`,
        )
      }
      const detail = is409
        ? `409 Conflict${attempt === 1 ? ' — another instance is polling (zombie session, or a second Claude Code running?)' : ` (attempt ${attempt})`}`
        : `polling error: ${err}`
      process.stderr.write(`telegram channel: ${detail}, retrying in ${(delay / 1000).toFixed(1)}s\n`)
      await new Promise(r => setTimeout(r, delay))
EOF

# Both live copies: the version-cache (what actually runs) and the marketplace
# source (what a cache rebuild copies FROM — patch it too so a rebuild carries
# the fix forward instead of reverting it). Overridable via T2629_TARGETS
# (newline-separated absolute paths) for the regression test; production leaves
# it unset and uses the live plugin globs.
targets=()
if [ -n "${T2629_TARGETS:-}" ]; then
  while IFS= read -r f; do [ -n "$f" ] && targets+=("$f"); done <<< "$T2629_TARGETS"
else
  while IFS= read -r f; do targets+=("$f"); done < <(
    ls /home/claude-fallback/.claude/plugins/cache/claude-plugins-official/telegram/*/server.ts 2>/dev/null
    ls /home/claude-fallback/.claude/plugins/marketplaces/claude-plugins-official/external_plugins/telegram/server.ts 2>/dev/null
  )
fi

for f in "${targets[@]}"; do
  [ -f "$f" ] || continue
  OLD="$OLD_BLOCK" NEW="$NEW_BLOCK" MARKER="$MARKER" python3 - "$f" <<'PY' || true
import os, sys
f = sys.argv[1]
old, new, marker = os.environ["OLD"], os.environ["NEW"], os.environ["MARKER"]
try:
    t = open(f, encoding="utf-8").read()
except Exception as e:
    print(f"[t2629-patch] skip {f}: {e}"); sys.exit(0)
if marker in t:
    sys.exit(0)  # already patched — idempotent no-op
if old not in t:
    print(f"[t2629-patch] no-op {f}: old give-up block not found (upstream changed?) — left as-is")
    sys.exit(0)
t2 = t.replace(old, new, 1)
tmp = f + ".t2629.tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(t2)
os.replace(tmp, f)
print(f"[t2629-patch] re-asserted 409 resilience patch onto {f}")
PY
done
exit 0
