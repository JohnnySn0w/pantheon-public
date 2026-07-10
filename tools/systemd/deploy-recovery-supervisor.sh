#!/usr/bin/env bash
# deploy-recovery-supervisor.sh — install the vendored Recovery bot supervisor wrapper
# from this repo to its live location (~/.local/bin/recovery-bot-supervise.sh).
#
# T-0526: recovery-bot-supervise.sh (the systemd ExecStart wrapper that holds MainPID
# for the bot session so Restart=always works — restart-capability #1) is the
# canonical source in tools/systemd/; this script is the deploy mechanism.
#
# Design: COPY-on-deploy, not symlink. The live wrapper is Recovery's own launch
# path; a symlink into the repo would make the launch depend on the repo being
# present at a fixed path at respawn time (fragile). A standalone copy has no
# runtime dependency on the repo. Re-run this after editing the vendored copy.
#
# SAFETY (self-modifying Recovery's own launch path): backs up the existing live
# file (timestamped), installs atomically, then `bash -n`-verifies the deployed
# file AND diffs it against the source. Any failure aborts and restores the
# backup, so a broken wrapper never reaches the live path (which would break the
# next respawn). Idempotent: a no-op when the live file already matches.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SRC_DIR/recovery-bot-supervise.sh"
DEST="$HOME/.local/bin/recovery-bot-supervise.sh"

[[ -f "$SRC" ]] || { echo "ERROR: vendored source not found: $SRC" >&2; exit 1; }
if ! bash -n "$SRC"; then echo "ERROR: vendored source fails syntax check: $SRC" >&2; exit 1; fi

mkdir -p "$(dirname "$DEST")"

# No-op fast path: already in sync.
if [[ -f "$DEST" ]] && diff -q "$SRC" "$DEST" >/dev/null 2>&1; then
  echo "[deploy-recovery-supervisor] already in sync: $DEST"
  exit 0
fi

# Back up the existing live file before overwriting.
BACKUP=""
if [[ -f "$DEST" ]]; then
  BACKUP="${DEST}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  cp -p "$DEST" "$BACKUP"
  echo "[deploy-recovery-supervisor] backed up live file → $BACKUP"
fi

# Install (mode 755). install(1) writes to a temp + renames → atomic-ish.
install -m 755 "$SRC" "$DEST"

# Verify the deployed file: syntax + byte-identical to source. On any failure,
# restore the backup so the live path is never left broken.
if ! bash -n "$DEST" || ! diff -q "$SRC" "$DEST" >/dev/null 2>&1; then
  echo "ERROR: deployed file failed verification" >&2
  if [[ -n "$BACKUP" ]]; then
    cp -p "$BACKUP" "$DEST"
    echo "[deploy-recovery-supervisor] RESTORED backup → $DEST" >&2
  fi
  exit 1
fi

echo "[deploy-recovery-supervisor] deployed + verified: $DEST (mode 755, syntax OK, matches source)"
