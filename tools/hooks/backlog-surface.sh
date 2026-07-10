#!/bin/bash
# SessionStart hook: surface open BACKLOG.md items at session start.
# Part of pantheon-config T-0023.
#
# Logic:
#   - Read /home/claude/pantheon-config/BACKLOG.md
#   - Find headings matching ## B\d+ that do NOT contain "✓ CLOSED" or "CLOSED"
#   - Emit one-line summary: "Open BACKLOG items: B2, B6"
#   - If all closed or file missing, exit 0 with empty stdout
#
# Failure mode: errors stay silent; never block session start.

set -e

BACKLOG=/home/claude/pantheon-config/BACKLOG.md

# Drain stdin (Claude Code passes hook input JSON; we don't use it).
cat >/dev/null 2>&1 || true

if [[ ! -f "$BACKLOG" ]]; then
  exit 0
fi

# Extract B-IDs from open items (headings with ## B\d+ that lack CLOSED marker)
open_items=$(grep -E '^## B[0-9]+' "$BACKLOG" 2>/dev/null | grep -v 'CLOSED' | sed -E 's/^## (B[0-9]+).*/\1/' | sort -V || true)

if [[ -z "$open_items" ]]; then
  exit 0
fi

# Convert newline-separated list to comma-separated
open_list=$(echo "$open_items" | tr '\n' ',' | sed 's/,$//')

echo "Open BACKLOG items: $open_list"
exit 0
