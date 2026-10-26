#!/usr/bin/env bash
# install.sh — install the Pantheon early-OOM killer (T-5227 / ORG-104). Root only. Idempotent.
#
# ORDER IS THE SAFETY PROPERTY: the box must never be left without OOM handling.
#   1. record prior state (earlyoom unit states, the oomd drop-ins) — written once
#   2. install the killer + unit, start it, VERIFY it (active + a root --dry-run exits 0)
#      -> on failure abort here with earlyoom and the oomd drop-ins UNTOUCHED
#   3. only then disable earlyoom, move the pantheon systemd-oomd drop-ins aside, and
#      try-restart systemd-oomd (the oomd SERVICE stays running, with no pantheon policy)
#
# Usage: install.sh [--dry-run]     (--dry-run prints every action and changes nothing)
#
# Test hooks (never needed in production): OOM_INSTALL_ROOT prefixes every filesystem path,
# OOM_TEST_UID overrides the uid used by the root check, OOM_PYTHON picks the interpreter,
# OOM_VERIFY_SLEEP sets the settle delay.
set -euo pipefail

DRY=0
case "${1:-}" in
  --dry-run) DRY=1 ;;
  "") ;;
  -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

ROOT="${OOM_INSTALL_ROOT:-}"
PY="${OOM_PYTHON:-python3}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP="$ROOT/var/lib/pantheon/oom/install-backup"
BIN="$ROOT/usr/local/sbin/pantheon_oom_killer.py"
UNIT=pantheon-oom-killer.service

# Existing drop-ins this installer neutralizes (backed up, then removed). Extend here.
NEUTRALIZE=(
  /etc/systemd/oomd.conf.d/50-pantheon.conf
  /etc/systemd/system/user.slice.d/50-pantheon-oomd.conf
  /etc/systemd/system/user-.slice.d/50-pantheon-oomd.conf
)
# Files this installer adds: "<source relative to tools/oom>|<destination>|<mode>".
# Recovery's rule-3 drop-ins are added here (and to REMOVE in uninstall.sh).
INSTALL=(
  "pantheon_oom_killer.py|/usr/local/sbin/pantheon_oom_killer.py|0755"
  "pantheon-oom-killer.service|/etc/systemd/system/pantheon-oom-killer.service|0644"
)

if [ "${OOM_TEST_UID:-$(id -u)}" -ne 0 ]; then
  if [ "$DRY" -eq 0 ]; then
    echo "install.sh: must run as root (use --dry-run to preview)" >&2
    exit 1
  fi
  echo "# dry-run as non-root: nothing below is executed"
fi

run() { echo "+ $*"; if [ "$DRY" -eq 0 ]; then "$@"; fi; }
bname() { echo "$1" | tr '/' '%'; }

# --- 1. record prior state (once) --------------------------------------------------------
if [ -f "$BACKUP/state" ]; then
  echo "# prior-state backup already present ($BACKUP/state); keeping it"
else
  run mkdir -p "$BACKUP"
  EO_ENABLED=unknown
  EO_ACTIVE=unknown
  if [ "$DRY" -eq 0 ]; then
    EO_ENABLED="$(systemctl is-enabled earlyoom 2>/dev/null || true)"
    EO_ACTIVE="$(systemctl is-active earlyoom 2>/dev/null || true)"
  fi
  echo "+ record prior state: earlyoom enabled=${EO_ENABLED:-unknown} active=${EO_ACTIVE:-unknown}"
  if [ "$DRY" -eq 0 ]; then
    printf 'earlyoom_enabled=%s\nearlyoom_active=%s\n' "${EO_ENABLED:-unknown}" "${EO_ACTIVE:-unknown}" > "$BACKUP/state"
  fi
fi
for f in "${NEUTRALIZE[@]}"; do
  if [ -e "$ROOT$f" ] && [ ! -e "$BACKUP/$(bname "$f")" ]; then
    run cp -p "$ROOT$f" "$BACKUP/$(bname "$f")"
  fi
done

# --- 2. install, start, verify -------------------------------------------------------------
for spec in "${INSTALL[@]}"; do
  IFS='|' read -r src dst mode <<<"$spec"
  run install -D -m "$mode" "$HERE/$src" "$ROOT$dst"
done
run systemctl daemon-reload
run systemctl enable "$UNIT"
# T-5231: restart, not `enable --now`, so a re-run (the update leg) replaces an
# already-running killer's old code; on a first install it simply starts it.
run systemctl restart "$UNIT"

echo "+ verify: sleep ${OOM_VERIFY_SLEEP:-3}; systemctl is-active --quiet $UNIT; $PY $BIN --dry-run"
if [ "$DRY" -eq 0 ]; then
  sleep "${OOM_VERIFY_SLEEP:-3}"
  if ! systemctl is-active --quiet "$UNIT"; then
    echo "install.sh: $UNIT is not active after start; ABORTING with earlyoom and the oomd drop-ins untouched" >&2
    exit 1
  fi
  if ! "$PY" "$BIN" --dry-run >/dev/null; then
    echo "install.sh: killer --dry-run failed; ABORTING with earlyoom and the oomd drop-ins untouched" >&2
    exit 1
  fi
fi

# --- 3. only now retire the old layers -------------------------------------------------------
run systemctl disable --now earlyoom || echo "# earlyoom not installed or already disabled"
for f in "${NEUTRALIZE[@]}"; do
  if [ -e "$ROOT$f" ]; then
    run rm -f "$ROOT$f"
  else
    echo "# $f absent; nothing to move aside"
  fi
done
run systemctl daemon-reload
run systemctl try-restart systemd-oomd.service || echo "# systemd-oomd not restarted"

# --- 4. ORG-104 rule 3: system-orchestrator restart (sysorch_restart.py) ---------------------
# A failed install/check is reported loudly but NEVER rolls back the killer.
WARN=0
sysorch() {
  echo "+ $PY $HERE/sysorch_restart.py $*"
  if [ "$DRY" -eq 0 ]; then "$PY" "$HERE/sysorch_restart.py" "$@"; fi
}
if [ "$DRY" -eq 1 ]; then
  sysorch install
else
  sysorch install --apply || { echo "install.sh: WARNING sysorch_restart install failed (killer left in place)" >&2; WARN=1; }
fi
sysorch check || { echo "install.sh: WARNING sysorch_restart check FAILED (killer left in place)" >&2; WARN=1; }

run systemctl daemon-reload
if [ "$DRY" -eq 1 ]; then
  echo "+ (a daemon-reload in each system orchestrator's user manager, derived from sysorch_restart)"
else
  while read -r uid account; do
    [ -n "${account:-}" ] || continue
    run systemctl --user --machine="${account}@.host" daemon-reload \
      || { echo "install.sh: WARNING user-manager reload failed for $account (uid $uid)" >&2; WARN=1; }
  done < <("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import sysorch_restart as s; [print(x.uid, x.account) for x in s.system_orchestrators(s.load_instance())]' "$HERE" 2>/dev/null || true)
fi
if [ "$WARN" -ne 0 ]; then echo "# done WITH WARNINGS (see above)"; exit 3; fi
echo "# done"
