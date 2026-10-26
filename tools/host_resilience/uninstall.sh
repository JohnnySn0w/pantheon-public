#!/usr/bin/env bash
# uninstall.sh — remove the host self-reboot config and restore exactly the state recorded by
# install.sh (T-5237). Root only.
#
# ORDER IS THE SAFETY PROPERTY: drop the watchdog config and daemon-reexec FIRST (PID 1 closes
# /dev/watchdog cleanly), and only then rmmod softdog — and only if install.sh loaded it.
#
# Usage: uninstall.sh [--dry-run]     (--dry-run prints every action and changes nothing)
# Exit: 0 ok | 1 error/refusal | 2 usage | 3 done with warnings (backup kept)
# Test hooks: HR_INSTALL_ROOT, HR_TEST_UID (see install.sh).
set -euo pipefail

DRY=0
case "${1:-}" in
  --dry-run) DRY=1 ;;
  "") ;;
  -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

ROOT="${HR_INSTALL_ROOT:-}"
BACKUP="$ROOT/var/lib/pantheon/host-resilience/install-backup"
# Keep in step with install.sh.
SYSCTLS=("kernel.panic" "kernel.panic_on_oops")
D_SYSCTL=/etc/sysctl.d/60-pantheon-panic-reboot.conf
D_MODLOAD=/etc/modules-load.d/pantheon-softdog.conf
D_WD=/etc/systemd/system.conf.d/10-pantheon-watchdog.conf
D_BIN=/usr/local/sbin/pantheon_reboot_backoff.py
D_UNIT1=/etc/systemd/system/pantheon-reboot-backoff.service
D_UNIT2=/etc/systemd/system/pantheon-boot-alert.service
D_UNIT3=/etc/systemd/system/pantheon-softdog.service
D_ALERTENV=/etc/pantheon/boot-alert.env
STATE_DIR="$ROOT/var/lib/pantheon/host-resilience"

if [ "${HR_TEST_UID:-$(id -u)}" -ne 0 ]; then
  if [ "$DRY" -eq 0 ]; then
    echo "uninstall.sh: must run as root (use --dry-run to preview)" >&2
    exit 1
  fi
  echo "# dry-run as non-root: nothing below is executed"
fi

run() { echo "+ $*"; if [ "$DRY" -eq 0 ]; then "$@"; fi; }
bname() { echo "$1" | tr '/' '%'; }
state_get() { sed -n "s/^$1=//p" "$BACKUP/state" 2>/dev/null || true; }
restore_or_remove() {
  local b
  b="$BACKUP/$(bname "$1")"
  if [ -f "$b" ]; then
    if ! { run mkdir -p "$(dirname "$ROOT$1")" && run cp -p "$b" "$ROOT$1"; }; then
      echo "uninstall.sh: ERROR could not restore $1 from $b; backup kept, stopping" >&2
      exit 1
    fi
  else
    run rm -f "$ROOT$1"
  fi
}

if [ ! -f "$BACKUP/state" ]; then
  if [ "$DRY" -eq 1 ]; then
    echo "# NOTE: no install backup at $BACKUP/state; a real run would refuse here"
  else
    echo "uninstall.sh: no install backup at $BACKUP/state; refusing to guess the prior state" >&2
    exit 1
  fi
fi
PRE="$(state_get softdog_preloaded)"
WARN=0

# --- 0. backoff + alert units: disable, remove, restore (boot data is deleted with them) -----------
run systemctl disable pantheon-reboot-backoff.service pantheon-boot-alert.service || echo "# units were not enabled"
run systemctl disable pantheon-softdog.service || echo "# softdog boot unit was not enabled"
for f in "$D_UNIT1" "$D_UNIT2" "$D_UNIT3" "$D_BIN" "$D_ALERTENV"; do restore_or_remove "$f"; done
run rm -f "$STATE_DIR/boots.log" "$STATE_DIR/state.json" "$STATE_DIR/LOOP_HOLD" "$STATE_DIR/boots.log.tmp"
run systemctl daemon-reload || { echo "uninstall.sh: WARNING daemon-reload failed" >&2; WARN=1; }

# --- 1. watchdog part: config out, reexec (PID 1 closes the device), THEN rmmod ---------------
restore_or_remove "$D_WD"
run systemctl daemon-reexec || { echo "uninstall.sh: WARNING daemon-reexec failed" >&2; WARN=1; }
if [ "$PRE" = 0 ]; then
  if [ "$DRY" -eq 1 ] || [ -d "$ROOT/sys/module/softdog" ]; then
    run rmmod softdog || { echo "uninstall.sh: WARNING rmmod softdog failed (still in use?)" >&2; WARN=1; }
  fi
else
  echo "# softdog was already loaded before install; leaving it loaded"
fi
# stale T-5237 v1 modules-load file: always deleted, never restored (inert under the kmod blacklist)
run rm -f "$ROOT$D_MODLOAD"

# --- 2. sysctl part: file, then the prior live values ------------------------------------------
restore_or_remove "$D_SYSCTL"
for name in "${SYSCTLS[@]}"; do
  prior="$(state_get "sysctl.$name")"
  case "$prior" in
    ''|unknown) echo "# no recorded prior value for $name; leaving the live value" ;;
    *) run sysctl -w "$name=$prior" || { echo "uninstall.sh: WARNING could not restore $name" >&2; WARN=1; } ;;
  esac
done

# --- 3. drop the backup only if everything above succeeded -------------------------------------
if [ "$WARN" -ne 0 ]; then
  echo "# done WITH WARNINGS (backup kept at $BACKUP)"
  exit 3
fi
run rm -rf "$BACKUP"
run rmdir --ignore-fail-on-non-empty "$STATE_DIR"
echo "# done"
