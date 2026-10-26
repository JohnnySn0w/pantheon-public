#!/usr/bin/env bash
# install.sh — host self-reboot on panic / oops / hung PID 1 (T-5237). Root only.
# Idempotent.
#
# Two parts:
#   sysctl   panic=10, panic_on_oops=1 (applied live, key by key; softlockup_panic and
#            hung_task_panic are deliberately NOT set: false positives on a KVM guest / heavy IO)
#   watchdog softdog module (loaded at boot by pantheon-softdog.service: an explicit modprobe,
#            because modules-load.d honours Ubuntu's softdog blacklist) + systemd RuntimeWatchdogSec — a TRANSACTION: it is verified
#            (RuntimeWatchdogUSec non-zero AND /dev/watchdog exists AND PID 1 holds the device
#            open, i.e. ARMED) and rolled back whole on
#            failure, never left half-applied. It is REFUSED (sysctl part kept) if softdog's
#            nowayout is set or unreadable, since a rollback could then not disarm the timer.
#
# Third part (reboot-loop backoff + boot alert, units installed and ENABLED, not started):
#   pantheon-reboot-backoff.service  every boot: count boots in the last hour, lengthen
#            kernel.panic (10/60/300s), at 4+ boots HOLD (watchdog off for that boot, panic=0)
#   pantheon-boot-alert.service      every boot: one Telegram message to the operator via the Bot API
#   Credentials are copied (token + chat id lines ONLY) from --alert-env FILE (or env
#   HR_ALERT_ENV_SRC) into /etc/pantheon/boot-alert.env (0600); the chat id comes from that
#   file or from --chat-id ID (env HR_ALERT_CHAT_ID). Without a source the alert stays
#   unconfigured: loud warning here, FAIL in --check. The token is never printed or put in argv.
#
# Usage: install.sh [--dry-run | --check] [--alert-env FILE] [--chat-id ID]
#   --dry-run  print every action, change nothing
#   --check    read-only (any user): print live state; exit 0 only if everything matches
# Exit: 0 ok | 1 error | 2 usage | 4 watchdog part refused (sysctl applied) | 5 watchdog verify
#       failed (watchdog rolled back, sysctl applied)
#
# Test hooks (never needed in production): HR_INSTALL_ROOT prefixes every filesystem path
# (including /sys, /dev and /proc), HR_TEST_UID overrides the uid used by the root check,
# HR_WAIT_SECS bounds the device/PID-1 polls (default 10).
set -euo pipefail

DRY=0
MODE=install
ALERT_SRC="${HR_ALERT_ENV_SRC:-}"
ALERT_CHAT="${HR_ALERT_CHAT_ID:-}"
usage() { echo "usage: $0 [--dry-run | --check] [--alert-env FILE] [--chat-id ID]" >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --check) MODE=check ;;
    --alert-env) [ $# -ge 2 ] || usage; ALERT_SRC="$2"; shift ;;
    --chat-id) [ $# -ge 2 ] || usage; ALERT_CHAT="$2"; shift ;;
    -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
    *) usage ;;
  esac
  shift
done

ROOT="${HR_INSTALL_ROOT:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP="$ROOT/var/lib/pantheon/host-resilience/install-backup"
SYSCTLS=("kernel.panic=10" "kernel.panic_on_oops=1")
D_SYSCTL=/etc/sysctl.d/60-pantheon-panic-reboot.conf
# Stale T-5237 v1 mechanism (blacklisted by kmod at boot, so inert): removed on install, never restored.
D_MODLOAD=/etc/modules-load.d/pantheon-softdog.conf
D_WD=/etc/systemd/system.conf.d/10-pantheon-watchdog.conf
# "<source>|<destination>|<mode>"
F_SYSCTL="60-pantheon-panic-reboot.conf|$D_SYSCTL|0644"
F_WD="10-pantheon-watchdog.conf|$D_WD|0644"
D_BIN=/usr/local/sbin/pantheon_reboot_backoff.py
D_UNIT1=/etc/systemd/system/pantheon-reboot-backoff.service
D_UNIT2=/etc/systemd/system/pantheon-boot-alert.service
D_UNIT3=/etc/systemd/system/pantheon-softdog.service
D_UNIT3_WANT=/etc/systemd/system/sysinit.target.wants/pantheon-softdog.service
D_ALERTENV=/etc/pantheon/boot-alert.env
F_BIN="pantheon_reboot_backoff.py|$D_BIN|0755"
F_UNIT1="pantheon-reboot-backoff.service|$D_UNIT1|0644"
F_UNIT2="pantheon-boot-alert.service|$D_UNIT2|0644"
F_UNIT3="pantheon-softdog.service|$D_UNIT3|0644"
STATE_DIR="$ROOT/var/lib/pantheon/host-resilience"
DESTS=("$D_SYSCTL" "$D_WD" "$D_BIN" "$D_UNIT1" "$D_UNIT2" "$D_UNIT3" "$D_ALERTENV")

get_usec() { systemctl show -p RuntimeWatchdogUSec 2>/dev/null | sed -n 's/^RuntimeWatchdogUSec=//p' || true; }
usec_nonzero() { case "$1" in ""|0|0us|0ms|0s|0min) return 1 ;; *) return 0 ;; esac; }
softdog_loaded() { [ -d "$ROOT/sys/module/softdog" ]; }
# Read a sysctl straight from /proc/sys (no sysctl binary: it lives in /usr/sbin, absent from a
# non-root PATH). "kernel.panic" -> $ROOT/proc/sys/kernel/panic.
sysctl_get() { local f; f="$ROOT/proc/sys/${1//.//}"; cat "$f" 2>/dev/null || echo unreadable; }
# Does PID 1 hold /dev/watchdog (or watchdog0) open? Needs root to read /proc/1/fd.
pid1_holds_watchdog() {
  local l t
  for l in "$ROOT"/proc/1/fd/*; do
    [ -L "$l" ] || continue
    t="$(readlink "$l" 2>/dev/null || true)"
    case "$t" in */dev/watchdog|*/dev/watchdog0) return 0 ;; esac
  done
  return 1
}
# wait_for <seconds> <command...>: poll once a second, bounded; returns the last status.
wait_for() {
  local n="$1" i=0
  shift
  while ! "$@"; do
    [ "$i" -ge "$n" ] && return 1
    i=$((i + 1))
    sleep 1
  done
}
WAIT="${HR_WAIT_SECS:-10}"
# shellcheck disable=SC2317  # invoked indirectly via wait_for
dev_watchdog_exists() { [ -e "$ROOT/dev/watchdog" ]; }

# --- --check: read-only ----------------------------------------------------------------------
if [ "$MODE" = check ]; then
  bad=0
  for kv in "${SYSCTLS[@]}"; do
    name="${kv%%=*}"; want="${kv#*=}"
    got="$(sysctl_get "$name")"
    if [ "$got" = "$want" ]; then echo "ok   sysctl $name=$got"
    else echo "FAIL sysctl $name=$got (want $want)"; bad=1; fi
  done
  if softdog_loaded; then echo "ok   softdog loaded"; else echo "FAIL softdog not loaded"; bad=1; fi
  # The BOOT path, not just the current state: modules-load.d is bypassed by the kmod blacklist,
  # so only an installed + enabled pantheon-softdog.service re-arms the watchdog after a reboot.
  if [ -f "$ROOT$D_UNIT3" ] && [ -L "$ROOT$D_UNIT3_WANT" ]; then
    echo "ok   softdog loads at boot via pantheon-softdog.service (installed + enabled; explicit modprobe bypasses the kmod blacklist)"
  else
    echo "FAIL softdog boot unit pantheon-softdog.service not installed+enabled (modules-load.d is bypassed by the kmod blacklist; the watchdog will not arm after reboot)"; bad=1
  fi
  u="$(get_usec)"
  if usec_nonzero "$u"; then echo "ok   RuntimeWatchdogUSec=$u"
  else echo "FAIL RuntimeWatchdogUSec=${u:-unreadable} (want non-zero)"; bad=1; fi
  if [ -e "$ROOT/dev/watchdog" ]; then echo "ok   /dev/watchdog exists"
  else echo "FAIL /dev/watchdog missing"; bad=1; fi
  if pid1_holds_watchdog; then echo "ok   PID 1 holds the watchdog device (armed)"
  else echo "FAIL PID 1 does not hold the watchdog device (or /proc/1/fd unreadable: run as root)"; bad=1; fi
  n="$(python3 "$HERE/pantheon_reboot_backoff.py" count 2>/dev/null || echo unknown)"
  echo "info boots in the last hour: $n"
  if [ -e "$STATE_DIR/LOOP_HOLD" ]; then echo "FAIL reboot loop hold (marker $STATE_DIR/LOOP_HOLD present)"; bad=1
  else echo "ok   no reboot loop hold"; fi
  if [ -f "$ROOT$D_BIN" ] && [ -f "$ROOT$D_UNIT1" ] && [ -f "$ROOT$D_UNIT2" ]; then echo "ok   backoff + alert units installed"
  else echo "FAIL backoff/alert units not installed"; bad=1; fi
  if [ -e "$ROOT$D_ALERTENV" ]; then echo "ok   alert config present (values not shown)"
  else echo "FAIL alert config $D_ALERTENV missing (Telegram boot alert unconfigured)"; bad=1; fi
  exit "$bad"
fi

if [ "${HR_TEST_UID:-$(id -u)}" -ne 0 ]; then
  if [ "$DRY" -eq 0 ]; then
    echo "install.sh: must run as root (use --dry-run to preview, --check to inspect)" >&2
    exit 1
  fi
  echo "# dry-run as non-root: nothing below is executed"
fi

run() { echo "+ $*"; if [ "$DRY" -eq 0 ]; then "$@"; fi; }
bname() { echo "$1" | tr '/' '%'; }
state_get() { sed -n "s/^$1=//p" "$BACKUP/state" 2>/dev/null || true; }
inst() {
  local src dst mode
  IFS='|' read -r src dst mode <<<"$1"
  run install -D -m "$mode" "$HERE/$src" "$ROOT$dst"
}
restore_or_remove() {
  local b
  b="$BACKUP/$(bname "$1")"
  if [ -f "$b" ]; then
    if ! { mkdir -p "$(dirname "$ROOT$1")" && run cp -p "$b" "$ROOT$1"; }; then
      echo "install.sh: ERROR could not restore $1 from $b" >&2
      return 1
    fi
  else
    run rm -f "$ROOT$1"
  fi
}

# --- 0. preflight: refuse before touching anything -------------------------------------------
if [ "$DRY" -eq 0 ]; then
  if ! modprobe -n softdog >/dev/null 2>&1; then
    echo "install.sh: softdog module is not loadable (modprobe -n softdog failed); refusing" >&2
    exit 1
  fi
else
  echo "+ preflight: modprobe -n softdog"
fi
# Alert credentials: validate BEFORE touching anything. Values are never echoed.
ALERT_OK=0
if [ -z "$ALERT_SRC" ]; then
  echo "# WARNING: no --alert-env source given; the Telegram boot alert will be UNCONFIGURED" >&2
elif [ ! -r "$ALERT_SRC" ] || ! grep -q '^TELEGRAM_BOT_TOKEN=.' "$ALERT_SRC"; then
  echo "install.sh: alert env source '$ALERT_SRC' is unreadable or has no TELEGRAM_BOT_TOKEN=; refusing" >&2
  exit 1
else
  if [ -z "$ALERT_CHAT" ]; then ALERT_CHAT="$(grep -m1 '^TELEGRAM_CHAT_ID=' "$ALERT_SRC" | cut -d= -f2- || true)"; fi
  if [ -z "$ALERT_CHAT" ]; then
    echo "install.sh: no chat id: pass --chat-id or put TELEGRAM_CHAT_ID= in '$ALERT_SRC'; refusing" >&2
    exit 1
  fi
  ALERT_OK=1
fi

# --- 1. record prior state (write-once; the state file is written LAST as the commit marker) ---
if [ -f "$BACKUP/state" ]; then
  echo "# prior-state backup already present ($BACKUP/state); keeping it"
else
  run mkdir -p "$BACKUP"
  echo "+ record prior state: sysctl values, softdog preloaded?, pre-existing destination files"
  if [ "$DRY" -eq 0 ]; then
    for f in "${DESTS[@]}"; do
      if [ -e "$ROOT$f" ]; then cp -p "$ROOT$f" "$BACKUP/$(bname "$f")"; fi
    done
    {
      for kv in "${SYSCTLS[@]}"; do
        name="${kv%%=*}"
        v="$(sysctl_get "$name")"
        echo "sysctl.$name=${v/unreadable/unknown}"
      done
      # softdog_preloaded is recorded ONCE, on the first run, and never rewritten: 0 means
      # "install.sh loaded it" (so rollback/uninstall may rmmod it) even on later re-runs when
      # it is of course loaded; 1 means it was already loaded before us and is never unloaded.
      if softdog_loaded; then echo softdog_preloaded=1; else echo softdog_preloaded=0; fi
      echo "runtime_watchdog_usec_before=$(get_usec)"
    } > "$BACKUP/state.tmp"
    mv "$BACKUP/state.tmp" "$BACKUP/state"
  fi
fi

# --- 2. sysctl part: file, then the exact keys live --------------------------------------------
if ! inst "$F_SYSCTL"; then
  echo "install.sh: ERROR could not install $D_SYSCTL" >&2
  exit 1
fi
for kv in "${SYSCTLS[@]}"; do
  if ! run sysctl -w "$kv"; then
    echo "install.sh: ERROR sysctl -w $kv failed" >&2
    exit 1
  fi
done

# --- 2b. reboot-loop backoff + boot alert: units installed and enabled (NOT started: starting
# them now would log a phantom boot and page the operator) ---------------------------------------------
if ! { inst "$F_BIN" && inst "$F_UNIT1" && inst "$F_UNIT2"; }; then
  echo "install.sh: ERROR could not install the backoff/alert files" >&2
  exit 1
fi
if [ "$ALERT_OK" -eq 1 ]; then
  echo "+ build $D_ALERTENV from $ALERT_SRC (token + chat id lines only; values not shown)"
  if [ "$DRY" -eq 0 ]; then
    tmp=""
    trap 'rm -f "${tmp:-}"' EXIT
    trap 'exit 130' HUP INT TERM
    tmp="$(umask 077 && mktemp)"
    { grep -m1 '^TELEGRAM_BOT_TOKEN=' "$ALERT_SRC"; printf 'TELEGRAM_CHAT_ID=%s\n' "$ALERT_CHAT"; } > "$tmp"
    if ! install -D -m 0600 "$tmp" "$ROOT$D_ALERTENV"; then
      rm -f "$tmp"
      echo "install.sh: ERROR could not install $D_ALERTENV" >&2
      exit 1
    fi
    rm -f "$tmp"
    trap - EXIT HUP INT TERM
  fi
fi
if ! { run systemctl daemon-reload && run systemctl enable pantheon-reboot-backoff.service pantheon-boot-alert.service; }; then
  echo "install.sh: ERROR could not enable the backoff/alert units" >&2
  exit 1
fi

# --- 3. watchdog part: a verified transaction ----------------------------------------------------
wd_rollback() {
  echo "install.sh: rolling the watchdog part back" >&2
  restore_or_remove "$D_WD" || true
  systemctl disable pantheon-softdog.service || echo "install.sh: WARNING could not disable pantheon-softdog.service during rollback" >&2
  restore_or_remove "$D_UNIT3" || true
  systemctl daemon-reload || echo "install.sh: WARNING daemon-reload failed during rollback" >&2
  systemctl daemon-reexec || echo "install.sh: WARNING daemon-reexec failed during rollback" >&2
  if [ "$(state_get softdog_preloaded)" = 0 ] && softdog_loaded; then
    rmmod softdog || echo "install.sh: WARNING rmmod softdog failed during rollback" >&2
  fi
}

WD_RC=0
if ! run modprobe softdog; then
  echo "install.sh: modprobe softdog failed" >&2
  WD_RC=5
fi
if [ "$WD_RC" -eq 0 ]; then
  echo "+ check: softdog nowayout must be N/0"
  if [ "$DRY" -eq 0 ]; then
    nw="$(cat "$ROOT/sys/module/softdog/parameters/nowayout" 2>/dev/null || echo unreadable)"
    if [ "$nw" = unreadable ]; then
      # Some kernels (Ubuntu 6.8 generic) do not expose softdog's parameters in sysfs. Then
      # nowayout is the build default unless overridden: N only if the kernel config says
      # "# CONFIG_WATCHDOG_NOWAYOUT is not set" AND nothing sets nowayout in modprobe.d or
      # on the kernel command line. Anything else stays "unreadable" (refuse).
      kcfg="$ROOT/boot/config-${HR_KERNEL_RELEASE:-$(uname -r)}"
      if grep -qx '# CONFIG_WATCHDOG_NOWAYOUT is not set' "$kcfg" 2>/dev/null \
         && ! grep -rqs 'nowayout' "$ROOT/etc/modprobe.d" \
         && ! grep -qs 'nowayout' "$ROOT/proc/cmdline"; then
        nw=N
      fi
    fi
    case "$nw" in
      N|n|0) ;;
      *)
        echo "install.sh: softdog nowayout is '$nw' (set or unreadable); a rollback could not disarm the timer. Watchdog part REFUSED; sysctl part kept." >&2
        if [ "$(state_get softdog_preloaded)" = 0 ]; then rmmod softdog || true; fi
        WD_RC=4 ;;
    esac
  fi
fi
if [ "$WD_RC" -eq 0 ]; then
  # The device node must exist BEFORE PID 1 is told to open it.
  echo "+ wait (<= ${WAIT}s) for $ROOT/dev/watchdog"
  if [ "$DRY" -eq 0 ] && ! wait_for "$WAIT" dev_watchdog_exists; then
    echo "install.sh: /dev/watchdog did not appear within ${WAIT}s after modprobe softdog" >&2
    WD_RC=5
  fi
fi
if [ "$WD_RC" -eq 0 ]; then
  # Boot persistence: explicit-modprobe unit (the stale modules-load.d file is deleted: it is
  # inert under the kmod blacklist), enabled BEFORE the drop-in/reexec so rollback covers it.
  { inst "$F_UNIT3" && run rm -f "$ROOT$D_MODLOAD" && run systemctl daemon-reload \
      && run systemctl enable pantheon-softdog.service \
      && inst "$F_WD" && run systemctl daemon-reexec; } || WD_RC=5
fi
if [ "$WD_RC" -eq 0 ]; then
  echo "+ verify: systemctl show -p RuntimeWatchdogUSec is non-zero; $ROOT/dev/watchdog exists; PID 1 holds it open (/proc/1/fd)"
  if [ "$DRY" -eq 0 ]; then
    u="$(get_usec)"
    if [ -e "$ROOT/dev/watchdog" ]; then present=yes; else present=no; fi
    if wait_for "$WAIT" pid1_holds_watchdog; then held=yes; else held=no; fi
    if ! usec_nonzero "$u" || [ "$present" = no ] || [ "$held" = no ]; then
      echo "install.sh: watchdog verify FAILED (RuntimeWatchdogUSec='${u:-}', /dev/watchdog present: $present, held by PID 1: $held)" >&2
      WD_RC=5
    fi
  fi
fi
if [ "$WD_RC" -eq 5 ] && [ "$DRY" -eq 0 ]; then wd_rollback; fi
case "$WD_RC" in
  0) echo "# done" ;;
  4) echo "# done WITHOUT the watchdog part (refused)" ;;
  *) echo "# watchdog part FAILED and was rolled back; sysctl part applied" ;;
esac
exit "$WD_RC"
