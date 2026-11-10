#!/usr/bin/env bash
# vps_state_backup.sh — off-box backup of the NON-git-backed critical VPS state.
#
# T-2178. DigitalOcean snapshots cover full-disk restore; GitHub remotes + Recovery's
# Forgejo mirror the git repos. This covers the gap between them: the state that
# lives ONLY on this disk and is IRREPLACEABLE if the droplet is lost between
# snapshots — the accumulated agent memory, handoffs, the kino ticket/ledger DB,
# host config, and the disk-only systemd unit definitions.
#
# Backs up (irreplaceable, low-sensitivity):
#   - kino DB (/var/lib/kino/kino.db + WAL/SHM, copied atomically-enough for restore)
#   - /etc/kino (host agent config)
#   - per-account: top-level *.md (HANDOFF/CLAUDE/RECOVERY/...), memory pins
#     (~/.claude/projects/*/memory/), systemd --user unit definitions
#   - system-level agent systemd unit files (installed cp's, disk-only)
#
# Deliberately EXCLUDES:
#   - git repos (already on GitHub + Recovery's Forgejo)
#   - ~/.claude/projects transcripts (large, disposable history, not state)
#   - echo_vault (git-backed on GitHub)
#   - credentials/settings (settings*.json, .env, keys) — re-mintable, and
#     keeping them out of an unencrypted private repo keeps the backup low-value
#     to an attacker. See T-2178 for the optional encrypt-with-off-box-key path.
#
# Restore = clone the backup repo; the tree IS the state, browsable + versioned.
# Runs as root (reads every account + /etc). Idempotent: no changes -> no commit.
set -euo pipefail

REPO_DIR="${VPS_BACKUP_REPO_DIR:-/var/lib/vps-backup/pantheon-vps-backup}"
ACCOUNTS="claude claude-fallback claude-research codex the-ancestor exocortex suborch1 suborch2"

# --- T-4546 perms preamble: begin ---
# SECURITY (T-4546). This checkout holds every seat's memory pins, HANDOFF.md files
# and kino.db, copied by root out of 0750 homes. Created under root's default umask
# 022 it was world-readable and flattened that cordon. Two layers, both on every run:
#   1. umask 077: everything this script creates (mkdir'd dirs, the sqlite .backup
#      copy, MANIFEST.txt, new .git objects) is owner-only from birth. `cp -a` still
#      preserves SOURCE modes inside the tree; that is fine, the barrier is layer 2.
#   2. harden_backup_perms (called once the checkout is known to exist): chmod 0700
#      the checkout AND its parent, so a re-provisioned host or a loosened dir heals on
#      the next run. Top level only, deliberately: a 0700 root is the barrier, and a
#      recursive chmod would only mask drift. Root-only; nothing local needs to read it
#      (backup_staleness_check.py reads the GitHub copy). Modes are not part of git
#      content beyond the exec bit, so what is committed + pushed is unchanged.
# The test (tools/test_vps_state_backup.py) executes this fenced block, so keep it
# self-contained: umask + function definition only, no calls, no other dependencies
# than $REPO_DIR.
umask 077
harden_backup_perms() {
  chmod 0700 "$(dirname "$REPO_DIR")" "$REPO_DIR"
}
# --- T-4546 perms preamble: end ---

if [ ! -d "$REPO_DIR/.git" ]; then
  echo "backup repo checkout missing at $REPO_DIR/.git — provision it first" >&2
  exit 1
fi
harden_backup_perms

# Wipe content (keep .git) so a removed source path drops from the backup too.
find "$REPO_DIR" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +

# 1. kino DB + host config -------------------------------------------------
mkdir -p "$REPO_DIR/kino" "$REPO_DIR/etc/kino"
# sqlite3 .backup gives a single CONSISTENT file even mid-write (folds the WAL
# in) — avoids committing a churning multi-MB -wal every run. Fall back to a
# raw cp of .db + WAL/SHM if sqlite3 is somehow absent.
if [ -f /var/lib/kino/kino.db ]; then
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 /var/lib/kino/kino.db ".backup '$REPO_DIR/kino/kino.db'"
  else
    for f in /var/lib/kino/kino.db /var/lib/kino/kino.db-wal /var/lib/kino/kino.db-shm; do
      [ -f "$f" ] && cp -a "$f" "$REPO_DIR/kino/"
    done
  fi
fi
[ -d /etc/kino ] && cp -a /etc/kino/. "$REPO_DIR/etc/kino/" 2>/dev/null || true

# 2. per-account agent state ----------------------------------------------
for u in $ACCOUNTS; do
  H="/home/$u"
  [ -d "$H" ] || continue
  D="$REPO_DIR/hosts/$u"
  mkdir -p "$D"
  # top-level markdown (handoffs, CLAUDE.md, RECOVERY.md, ...)
  find "$H" -maxdepth 1 -type f -name '*.md' -exec cp -a {} "$D/" \; 2>/dev/null || true
  # memory pins (durable cross-session facts), per project slug
  for md in "$H"/.claude/projects/*/memory; do
    [ -d "$md" ] || continue
    slug="$(basename "$(dirname "$md")")"
    mkdir -p "$D/memory/$slug"
    cp -a "$md/." "$D/memory/$slug/" 2>/dev/null || true
  done
  # systemd --user unit definitions
  if [ -d "$H/.config/systemd/user" ]; then
    mkdir -p "$D/systemd-user"
    find "$H/.config/systemd/user" -maxdepth 1 -type f \
      \( -name '*.service' -o -name '*.timer' -o -name '*.target' \) \
      -exec cp -a {} "$D/systemd-user/" \; 2>/dev/null || true
  fi
  # drop an empty account dir if nothing was captured
  find "$D" -mindepth 1 -maxdepth 1 | read -r _ || rmdir "$D" 2>/dev/null || true
done

# 3. system-level agent systemd units (disk-only cp's) ---------------------
mkdir -p "$REPO_DIR/etc-systemd"
find /etc/systemd/system -maxdepth 1 -type f \
  \( -name 'claude*' -o -name 'kino*' -o -name 'pantheon*' \
     -o -name 'actions.runner*' -o -name '*mailbox*' -o -name 'ho-*' \) \
  -exec cp -a {} "$REPO_DIR/etc-systemd/" \; 2>/dev/null || true

# 4. manifest --------------------------------------------------------------
{
  echo "# VPS non-git state backup (T-2178)"
  echo "generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "host: $(hostname)"
  echo "covers: kino DB, /etc/kino, per-account memory pins + top-level markdown + systemd --user units, system agent units"
  echo "excludes: git repos (GitHub+Forgejo), .claude/projects transcripts, echo_vault (git-backed), credentials/settings (re-mintable)"
} > "$REPO_DIR/MANIFEST.txt"

# 4b. confidentiality scrub -----------------------------------------------
# A map of WHERE secrets live is the one class too sensitive for an
# unencrypted off-box repo, even a private one (T-2178). Actual token VALUES
# are not stored anywhere on the box's memory pins (verified), but credential
# INDEXES are — drop them. Everything else is no more sensitive than
# pantheon-config already is on GitHub-private.
find "$REPO_DIR/hosts" -type f \( -iname '*credential*index*' -o -iname '*secret*index*' \) -delete 2>/dev/null || true

# 5. commit + push ---------------------------------------------------------
# Strip any nested .git dirs captured from source (some memory dirs are their
# own git repos) — an embedded repo becomes a gitlink and its content is lost.
# mindepth 2 guarantees the backup repo's own top-level .git is never matched.
# T-4871: match .git of ANY type. A linked worktree's .git is a FILE pointing
# back into the LIVE seat repo; left in place, root's `git add -A` below
# refreshes that live worktree's index as root:root 0600 and breaks the seat.
# --- T-4871 nested-git strip: begin ---
find "$REPO_DIR" -mindepth 2 -name .git -prune -exec rm -rf {} + 2>/dev/null || true
# --- T-4871 nested-git strip: end ---

cd "$REPO_DIR"
git add -A
if git diff --cached --quiet; then
  echo "vps_state_backup: no changes since last run — nothing to commit"
  exit 0
fi
git -c user.name='vps-backup' -c user.email='vps-backup@pantheon.local' \
  commit -q -m "vps-state backup $(date -u +%Y-%m-%dT%H:%M:%SZ)"
# --- T-5993 mirror leg: begin ---
# ORG-101 dual-write leg (T-4705 / T-5993). Root must never import code from a
# claude-writable checkout, so _git_utils is imported only from a lib dir that the
# running uid owns and nobody else can write (the installed_copy_drift rule):
# every entry under it, the dir included, must be owned by this uid, have no group
# or world write bit, and not be a symlink. python3 -I ignores PYTHONPATH and the
# user site. Every outcome prints one line and returns 0: the mirror is
# best-effort, origin already landed, and the backup never fails on this leg.
mirror_leg() {
  local repo="$1" lib="${VPS_BACKUP_MIRROR_TOOLS_DIR:-/usr/local/lib/pantheon-vps-backup}" bad note
  if [ ! -d "$lib/tools" ] || [ ! -f "$lib/tools/_git_utils.py" ]; then
    echo "vps_state_backup: mirror leg: skipped (no lib at $lib)"
    return 0
  fi
  bad="$(find "$lib" \( -type l -o ! -uid "$(id -u)" -o -perm /022 \) -print -quit 2>/dev/null)" || bad="find failed"
  if [ -n "$bad" ]; then
    echo "vps_state_backup: mirror leg: skipped (unsafe: $bad)"
    return 0
  fi
  note="$(cd / && python3 -I -c '
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import _git_utils
print(_git_utils.mirror_push_after_origin(Path(sys.argv[2]), "main:main") or "inert")
' "$lib/tools" "$repo" 2>/dev/null | tail -n 1)" || note=""
  echo "vps_state_backup: mirror leg: ${note:-error (the mirror helper failed)}"
  return 0
}
if [ "${VPS_BACKUP_NO_PUSH:-0}" != "1" ]; then
  git push -q origin main
  mirror_leg "$REPO_DIR" || true
fi
# --- T-5993 mirror leg: end ---
echo "vps_state_backup: committed + ${VPS_BACKUP_NO_PUSH:+(push skipped) }pushed"
