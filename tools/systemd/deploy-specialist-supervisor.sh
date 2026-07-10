#!/usr/bin/env bash
# deploy-specialist-supervisor.sh — install the vendored Specialist bot supervisor wrapper
# from this repo to its live location (~/.local/bin/specialist-bot-supervise.sh on
# the claude-research account).
#
# Mirror of deploy-recovery-supervisor.sh (T-0526) for Specialist (T-0517 parity / T-0755).
# COPY-on-deploy, not symlink: the live wrapper is Specialist's own launch path and
# must not depend on the repo being present at respawn time. Backs up the live
# file, installs atomically, bash -n + diff verifies, restores on failure,
# no-ops when in sync. Run as the claude-research user.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SRC_DIR/specialist-bot-supervise.sh"
DEST="$HOME/.local/bin/specialist-bot-supervise.sh"

[[ -f "$SRC" ]] || { echo "ERROR: vendored source not found: $SRC" >&2; exit 1; }
if ! bash -n "$SRC"; then echo "ERROR: vendored source fails syntax check: $SRC" >&2; exit 1; fi

mkdir -p "$(dirname "$DEST")"

if [[ -f "$DEST" ]] && diff -q "$SRC" "$DEST" >/dev/null 2>&1; then
  echo "[deploy-specialist-supervisor] already in sync: $DEST"
  exit 0
fi

BACKUP=""
if [[ -f "$DEST" ]]; then
  BACKUP="${DEST}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  cp -p "$DEST" "$BACKUP"
  echo "[deploy-specialist-supervisor] backed up live file → $BACKUP"
fi

install -m 755 "$SRC" "$DEST"

if ! bash -n "$DEST" || ! diff -q "$SRC" "$DEST" >/dev/null 2>&1; then
  echo "ERROR: deployed file failed verification" >&2
  if [[ -n "$BACKUP" ]]; then
    cp -p "$BACKUP" "$DEST"
    echo "[deploy-specialist-supervisor] RESTORED backup → $DEST" >&2
  fi
  exit 1
fi

echo "[deploy-specialist-supervisor] deployed + verified: $DEST (mode 755, syntax OK, matches source)"
