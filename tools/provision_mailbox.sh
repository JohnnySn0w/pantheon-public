#!/usr/bin/env bash
# provision_mailbox.sh — stand up one agent's ORG-026 mailbox (T-0772 deployment).
#
# Idempotent. DRY-RUN BY DEFAULT — prints every mutating command and changes nothing.
# Pass --apply to execute. The live cross-account install (running --apply for an
# agent other than yourself, plus enable-linger) is the supervised step gated on
# the operator; the dry-run is safe to inspect for any agent.
#
# What a full provision does for <agent> (Path A — see the §"Path A" + §"Wake" notes
# below for WHY this differs from the original ~/inbox + .path design):
#   1. create the agent's hub subdir /home/the-ancestor/mailbox/<agent>/{tmp,new,cur}
#      owned group <group> (default veneration), mode 2770 (setgid + group-writable)
#      so any group peer can deliver + the consumer can ack. (NOT ~/inbox — a 750 home
#      is untraversable cross-account.)
#   2. install the wake units (pantheon-mailbox-wake@.service + @.timer) into the
#      agent's ~/.config/systemd/user/ , daemon-reload, enable+start the @<agent>.timer
#      (clock-driven wake; the .path inotify watch is unusable under the group-gated
#      hub — the user-systemd manager lacks the group, see §Wake note).
#   3. enable-linger for the account so the --user .timer fires without an active
#      session (required for a standing daemonless wake).
#
# Self vs cross-account: ops for your OWN account run directly; ops for another agent
# are wrapped `sudo -n -u <unix_user> ...` with that user's XDG_RUNTIME_DIR.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$REPO_ROOT/tools/systemd"
# T-1124: default to veneration — every real mailbox hub under /home/the-ancestor
# is group `veneration` (2770), NOT pantheon-shared. The old default failed a
# re-chgrp on every existing hub (cosmetic "Operation not permitted" on re-apply)
# and would mis-group a freshly-created hub. Override with --group if ever needed.
GROUP="veneration"
APPLY=0
AGENT=""

usage() { sed -n '2,30p' "$0"; }
log() { echo "[provision-mailbox] $*"; }

# --- map agent -> unix account + home (mirrors tools/mailbox_wake.py AGENT_PANE). ---
agent_user() { case "$1" in orchestrator) echo claude ;; specialist) echo claude-research ;; recovery) echo claude-fallback ;; codex) echo codex ;; *) return 1 ;; esac; }
agent_home() { case "$1" in orchestrator) echo /home/claude ;; specialist) echo /home/claude-research ;; recovery) echo /home/claude-fallback ;; codex) echo /home/codex ;; *) return 1 ;; esac; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent) AGENT="$2"; shift 2 ;;
    --group) GROUP="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --dry-run) APPLY=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[provision-mailbox] ERROR: unknown arg: $1" >&2; exit 64 ;;
  esac
done

[[ -n "$AGENT" ]] || { echo "[provision-mailbox] ERROR: --agent is required" >&2; exit 64; }
UNIX_USER="$(agent_user "$AGENT")" || { echo "[provision-mailbox] ERROR: unknown agent '$AGENT'" >&2; exit 64; }
HOME_DIR="$(agent_home "$AGENT")"
UNIT_DST="$HOME_DIR/.config/systemd/user"
SELF="$(id -un)"

# Path A (2026-06-09): the inbox is the agent's subdir in the neutral
# group-accessible hub under /home/the-ancestor (ORG-026 "Conditionally a shared
# HOME"), NOT $HOME/inbox — a private mode-750 home can't be traversed
# cross-account to deliver. The hub subdir is created + owned by the-ancestor (the
# hub host, which is in the veneration group); every agent ∈ veneration delivers
# into peers' 2770 subdirs. (Replaces the per-home ~/inbox the first cross-account
# run proved untraversable.)
ANCESTOR_HUB="/home/the-ancestor/mailbox"
INBOX="$ANCESTOR_HUB/$AGENT"

# For a cross-account install, read the unit source from the TARGET agent's OWN
# clone — the install runs AS the target user, which cannot read the operator's
# ($SELF's) home (mode 750), so reading from $REPO_ROOT (the operator's clone)
# fails with `install: cannot stat '.../pantheon-mailbox-wake@.path': Permission
# denied`. Own-account installs keep the local clone. (Gap found 2026-06-09 in the
# first real cross-account provisioning run.)
if [[ "$UNIX_USER" != "$SELF" ]]; then
  UNIT_SRC="$HOME_DIR/pantheon-config/tools/systemd"
fi

# run <cmd...> — execute (or just print under dry-run) in the agent's account context.
run() {
  local pretty="$*"
  if [[ "$UNIX_USER" != "$SELF" ]]; then
    pretty="sudo -n -u $UNIX_USER XDG_RUNTIME_DIR=/run/user/\$(id -u $UNIX_USER) $*"
  fi
  if [[ "$APPLY" == "1" ]]; then
    log "RUN: $pretty"
    if [[ "$UNIX_USER" != "$SELF" ]]; then
      sudo -n -u "$UNIX_USER" env "XDG_RUNTIME_DIR=/run/user/$(id -u "$UNIX_USER")" "$@"
    else
      "$@"
    fi
  else
    log "DRY: $pretty"
  fi
}

log "agent=$AGENT user=$UNIX_USER home=$HOME_DIR group=$GROUP apply=$APPLY"
getent group "$GROUP" >/dev/null || log "WARNING: group '$GROUP' does not exist — create it before --apply"

# 1. inbox maildir — the agent's subdir in the hub. Created + owned by the hub host
#    (the-ancestor), since the hub lives in its home; setgid+2770 group veneration
#    lets every veneration member deliver. Listing $INBOX itself (not just the
#    leaves) so the parent gets 2770/veneration too, not the install -d default.
#    (Path A — see header. Requires the operator to hold sudo into $HUB_OWNER.)
HUB_OWNER="the-ancestor"
if [[ "$APPLY" == "1" ]]; then
  log "RUN: sudo -n -u $HUB_OWNER install -d -m 2770 -g $GROUP $INBOX {tmp,new,cur}"
  sudo -n -u "$HUB_OWNER" install -d -m 2770 -g "$GROUP" "$INBOX" "$INBOX/tmp" "$INBOX/new" "$INBOX/cur"
else
  log "DRY: sudo -n -u $HUB_OWNER install -d -m 2770 -g $GROUP $INBOX/{tmp,new,cur}"
fi

# 2. install + enable the wake units. The .timer (every 5 min) is THE wake
#    mechanism under Path A: it fires on a clock, then the sg-wrapped .service gets
#    the veneration group at run time and reads the hub. The .path inotify watch is
#    NOT installed — it is dead under Path A (T-1015): the watch is set up by the
#    agent's user-systemd MANAGER, which carries only the account's primary group
#    (user systemd can't grant supplementary groups), so it cannot traverse the
#    0710 veneration hub home to watch the subdir. The .service is sg-wrappable; the
#    manager is not. Confirmed: a hands-off delivery never fired the .path. So the
#    clock-driven timer is the autonomous wake; ~5 min worst-case latency is fine
#    for inter-agent mail. (.path unit file kept in tools/systemd for history.)
run install -d -m 0755 "$UNIT_DST"
for u in pantheon-mailbox-wake@.service pantheon-mailbox-wake@.timer; do
  run install -m 0644 "$UNIT_SRC/$u" "$UNIT_DST/$u"
done
run systemctl --user daemon-reload
run systemctl --user enable --now "pantheon-mailbox-wake@${AGENT}.timer"

# 3. linger so the --user watch runs without an active login session.
run loginctl enable-linger "$UNIX_USER"

log "done (apply=$APPLY). Verify: systemctl --user status pantheon-mailbox-wake@${AGENT}.timer"
