#!/usr/bin/env bash
# uninstall.sh — remove the Pantheon early-OOM killer and restore the state recorded by
# install.sh (T-5227 / ORG-104). Root only.
#
# ORDER IS THE SAFETY PROPERTY: restore the old layers FIRST and verify earlyoom is active,
# and only then stop and remove the killer, so the box is never left without OOM handling.
#
# Usage: uninstall.sh [--dry-run]     (--dry-run prints every action and changes nothing)
# Test hooks: OOM_INSTALL_ROOT, OOM_TEST_UID (see install.sh).
set -euo pipefail

DRY=0
case "${1:-}" in
  --dry-run) DRY=1 ;;
  "") ;;
  -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

ROOT="${OOM_INSTALL_ROOT:-}"
BACKUP="$ROOT/var/lib/pantheon/oom/install-backup"
UNIT=pantheon-oom-killer.service
# Keep these two lists in step with NEUTRALIZE / INSTALL in install.sh.
RESTORE=(
  /etc/systemd/oomd.conf.d/50-pantheon.conf
  /etc/systemd/system/user.slice.d/50-pantheon-oomd.conf
  /etc/systemd/system/user-.slice.d/50-pantheon-oomd.conf
)
REMOVE=(
  /usr/local/sbin/pantheon_oom_killer.py
  /etc/systemd/system/pantheon-oom-killer.service
)

if [ "${OOM_TEST_UID:-$(id -u)}" -ne 0 ]; then
  if [ "$DRY" -eq 0 ]; then
    echo "uninstall.sh: must run as root (use --dry-run to preview)" >&2
    exit 1
  fi
  echo "# dry-run as non-root: nothing below is executed"
fi

run() { echo "+ $*"; if [ "$DRY" -eq 0 ]; then "$@"; fi; }
bname() { echo "$1" | tr '/' '%'; }

if [ ! -f "$BACKUP/state" ]; then
  if [ "$DRY" -eq 1 ]; then
    echo "# NOTE: no install backup at $BACKUP/state; a real run would refuse here"
  else
    echo "uninstall.sh: no install backup at $BACKUP/state; refusing to guess the prior state" >&2
    exit 1
  fi
fi
EO_ENABLED="$(sed -n 's/^earlyoom_enabled=//p' "$BACKUP/state" 2>/dev/null || true)"
EO_ACTIVE="$(sed -n 's/^earlyoom_active=//p' "$BACKUP/state" 2>/dev/null || true)"

# --- 0. remove ORG-104 rule-3 restart drop-ins (sysorch_restart.py), then reload ----------------
# Best-effort: a failure here is reported but must not block restoring earlyoom below.
PY="${OOM_PYTHON:-python3}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$DRY" -eq 0 ]; then
  echo "+ $PY $HERE/sysorch_restart.py uninstall --apply"
  "$PY" "$HERE/sysorch_restart.py" uninstall --apply || echo "uninstall.sh: WARNING sysorch_restart uninstall failed; continuing" >&2
  run systemctl daemon-reload
  while read -r uid account; do
    [ -n "${account:-}" ] || continue
    run systemctl --user --machine="${account}@.host" daemon-reload \
      || echo "uninstall.sh: WARNING user-manager reload failed for $account (uid $uid)" >&2
  done < <("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import sysorch_restart as s; [print(x.uid, x.account) for x in s.system_orchestrators(s.load_instance())]' "$HERE" 2>/dev/null || true)
else
  echo "+ $PY $HERE/sysorch_restart.py uninstall"
  echo "+ systemctl daemon-reload (and a daemon-reload in each system orchestrator's user manager)"
fi

# --- 1. restore the old layers FIRST -----------------------------------------------------------
for f in "${RESTORE[@]}"; do
  b="$BACKUP/$(bname "$f")"
  if [ -f "$b" ]; then
    run mkdir -p "$(dirname "$ROOT$f")"
    run cp -p "$b" "$ROOT$f"
  else
    echo "# no backup for $f (was absent before install)"
  fi
done
run systemctl daemon-reload
run systemctl try-restart systemd-oomd.service || echo "# systemd-oomd not restarted"

# Restore the unit's recorded enable-state exactly (enabled/disabled/masked/static/...).
case "$EO_ENABLED" in
  enabled|enabled-runtime) run systemctl enable earlyoom ;;
  masked|masked-runtime)   run systemctl mask earlyoom ;;
  disabled)                run systemctl disable earlyoom || true ;;
  *)                       echo "# earlyoom enable-state '${EO_ENABLED:-?}' needs no restore (static/absent/unknown)" ;;
esac
if [ "$EO_ACTIVE" = "active" ]; then
  run systemctl start earlyoom
  echo "+ verify: systemctl is-active --quiet earlyoom"
  if [ "$DRY" -eq 0 ] && ! systemctl is-active --quiet earlyoom; then
    echo "uninstall.sh: earlyoom is not active after restore; ABORTING with the killer left running" >&2
    exit 1
  fi
fi
echo "# earlyoom restored to enabled=${EO_ENABLED:-?} active=${EO_ACTIVE:-?}"

# --- 2. only now stop and remove the killer --------------------------------------------------
run systemctl disable --now "$UNIT" || echo "# killer unit not present"
for f in "${REMOVE[@]}"; do run rm -f "$ROOT$f"; done
run systemctl daemon-reload

# Backup is removed only after everything above succeeded (set -e).
run rm -rf "$BACKUP"
echo "# done"
