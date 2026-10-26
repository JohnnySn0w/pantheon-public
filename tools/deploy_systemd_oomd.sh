#!/usr/bin/env bash
# tools/deploy_systemd_oomd.sh — deploy systemd-oomd as the fleet's proactive
# PSI-based shedder (T-2241, Option A; operator-approved TG 6630, 2026-09-01).
#
# MUST run as ROOT (apt install + system-unit config + enable). Recovery cannot —
# recovery's root sudo is kino/daemon-reload/journalctl-k/actions.runner only. This
# script is the turnkey artifact for a root-holder to run under supervised
# first-fire (Recovery observes; see notes/2026-09-01-systemd-oomd-fleet-shed-design.md).
#
# Idempotent. Modes:
#   (default)    install + configure + enable
#   --dry-run    print what would happen, change nothing
#   --rollback   disable oomd + remove the pantheon drop-ins (leaves the package)
#   --verify     just report current oomd state (oomctl + is-active)
#
# Reversible: --rollback (or `systemctl disable --now systemd-oomd`) restores the
# exact pre-oomd behavior; the -850 shield is untouched throughout.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_OOMD_CONF="$REPO_DIR/tools/systemd/oomd.conf.d/50-pantheon.conf"
SRC_SLICE_CONF="$REPO_DIR/tools/systemd/system/user.slice.d/50-pantheon-oomd.conf"
DST_OOMD_CONF="/etc/systemd/oomd.conf.d/50-pantheon.conf"
DST_SLICE_CONF="/etc/systemd/system/user.slice.d/50-pantheon-oomd.conf"
# T-5157: per-account pressure kill + earlyoom swap backstop (see the drop-ins).
SRC_USERSLICE_CONF="$REPO_DIR/tools/systemd/system/user-.slice.d/50-pantheon-oomd.conf"
DST_USERSLICE_CONF="/etc/systemd/system/user-.slice.d/50-pantheon-oomd.conf"
SRC_EARLYOOM_CONF="$REPO_DIR/tools/systemd/default/earlyoom"
DST_EARLYOOM_CONF="/etc/default/earlyoom"

# Running per-account slices (user-<UID>.slice), for LIVE set-property.
user_slices() { systemctl list-units --type=slice --state=active --no-legend --plain 'user-*.slice' 2>/dev/null | awk '{print $1}'; }

MODE="install"
case "${1:-}" in
  --dry-run) MODE="dry-run" ;;
  --rollback) MODE="rollback" ;;
  --verify) MODE="verify" ;;
  "") MODE="install" ;;
  *) echo "unknown arg: $1" >&2; exit 2 ;;
esac

need_root() { [ "$(id -u)" = 0 ] || { echo "ERROR: must run as root ($MODE)"; exit 1; }; }

verify_state() {
  echo "== systemd-oomd state =="
  systemctl is-active systemd-oomd 2>&1 || true
  systemctl is-enabled systemd-oomd 2>&1 || true
  echo "== user.slice effective ManagedOOM props (MUST be 'auto' since T-5157: a slice-wide kill cascades across accounts on systemd 255) =="
  systemctl show user.slice -p ManagedOOMSwap -p ManagedOOMMemoryPressure 2>&1 || true
  echo "== per-account user-<UID>.slice pressure kill: oomd's OWN view (systemctl show can say kill while oomd monitors nothing) =="
  local monitored missing=0
  monitored=$(oomctl 2>/dev/null | awk '/Path:/{print $2}') || true
  [ -n "$monitored" ] || echo "WARN: oomctl returned nothing (first-fire D-Bus timeout? retry after 'systemctl restart systemd-oomd')"
  for sl in $(user_slices); do
    if printf '%s\n' "$monitored" | grep -qx "/user.slice/$sl"; then echo "monitored: $sl"; else echo "NOT MONITORED: $sl"; missing=1; fi
  done
  echo "== earlyoom =="
  systemctl is-active --quiet earlyoom && echo "earlyoom active" || { echo "earlyoom NOT active"; missing=1; }
  [ "$missing" = 0 ] || { echo "VERIFY FAIL: a user-<UID>.slice is not oomd-monitored, or earlyoom is down"; return 1; }
  echo "== oomctl (managed cgroups + current pressure/swap) =="
  oomctl 2>&1 | sed -n '1,60p' || echo "(oomctl unavailable — package not installed?)"
}

if [ "$MODE" = "verify" ]; then verify_state; exit 0; fi

if [ "$MODE" = "rollback" ]; then
  need_root
  echo "== ROLLBACK =="
  systemctl disable --now systemd-oomd 2>&1 || true
  # clear the LIVE --runtime slice override (step 4) — daemon-reload alone won't revert it
  systemctl set-property --runtime user.slice ManagedOOMSwap=auto ManagedOOMMemoryPressure=auto 2>&1 || true
  for sl in $(user_slices); do systemctl set-property --runtime "$sl" ManagedOOMMemoryPressure=auto 2>&1 || true; done
  systemctl disable --now earlyoom 2>&1 || true  # our /etc/default/earlyoom is left in place (inert while earlyoom is disabled)
  rm -fv "$DST_OOMD_CONF" "$DST_SLICE_CONF" "$DST_USERSLICE_CONF"
  systemctl daemon-reload
  echo "rollback done — oomd disabled, live slice override reset to auto, pantheon drop-ins removed, -850 shield untouched."
  exit 0
fi

# ---- preconditions (checked in every non-rollback mode) ----
echo "== preconditions =="
fs=$(stat -fc %T /sys/fs/cgroup 2>/dev/null || echo "?")
[ "$fs" = "cgroup2fs" ] || { echo "ERROR: cgroup v2 required (got: $fs)"; exit 1; }
[ -r /proc/pressure/memory ] || { echo "ERROR: PSI (/proc/pressure) required"; exit 1; }
[ -f "$SRC_OOMD_CONF" ] && [ -f "$SRC_SLICE_CONF" ] && [ -f "$SRC_USERSLICE_CONF" ] && [ -f "$SRC_EARLYOOM_CONF" ] || { echo "ERROR: repo config files missing"; exit 1; }
echo "cgroup2fs=ok PSI=ok config-sources=ok systemd=$(systemctl --version | head -1 | awk '{print $2}')"

RUN() { if [ "$MODE" = "dry-run" ]; then echo "DRY: $*"; else echo "+ $*"; "$@"; fi; }
PLACE() { # PLACE <src> <dst>
  if [ "$MODE" = "dry-run" ]; then echo "DRY: install -D -m0644 $1 $2"; else
    install -D -m0644 "$1" "$2"; echo "+ placed $2"; fi
}

[ "$MODE" = "dry-run" ] || need_root

echo "== 1. install package =="
if dpkg -s systemd-oomd >/dev/null 2>&1; then
  echo "systemd-oomd already installed"
else
  RUN apt-get update
  RUN apt-get install -y systemd-oomd
fi
if dpkg -s earlyoom >/dev/null 2>&1; then
  echo "earlyoom already installed"
else
  # The package starts earlyoom with Debian defaults; step 5b restarts it on ours.
  RUN apt-get install -y earlyoom
fi

echo "== 2. place pantheon config drop-ins =="
PLACE "$SRC_OOMD_CONF" "$DST_OOMD_CONF"
PLACE "$SRC_SLICE_CONF" "$DST_SLICE_CONF"
PLACE "$SRC_USERSLICE_CONF" "$DST_USERSLICE_CONF"

echo "== 3. reload =="
RUN systemctl daemon-reload

# The /etc drop-ins give the slices their ManagedOOM* state at BOOT/load (reliable). But
# user.slice is already running and long-lived — daemon-reload does not reliably
# re-apply ManagedOOM* to the live slice (verified 2026-09-01: user.slice was
# ManagedOOM{Swap,MemoryPressure}=auto with empty DropInPaths). So apply it LIVE
# now via set-property --runtime (transient; the /etc drop-in owns reboot
# persistence, so no competing persistent drop-in is created). Without this, oomd
# would silently not manage user.slice until the next reboot — a no-op deploy.
# T-5157: user.slice is deliberately auto (see the slice drop-in for the systemd
# 255 cascade bug); per-account user@.service pressure kill does the shedding.
echo "== 4. apply user.slice state LIVE (drop-in = reboot persistence; set-property = running slice) =="
RUN systemctl set-property --runtime user.slice ManagedOOMSwap=auto ManagedOOMMemoryPressure=auto
# Same live-apply gap for the already-running per-account slices.
for sl in $(user_slices); do RUN systemctl set-property --runtime "$sl" ManagedOOMMemoryPressure=kill; done

echo "== 5. enable oomd =="
RUN systemctl enable --now systemd-oomd

echo "== 5b. earlyoom swap/low-mem backstop (our /etc/default/earlyoom) =="
PLACE "$SRC_EARLYOOM_CONF" "$DST_EARLYOOM_CONF"
RUN systemctl enable earlyoom
RUN systemctl restart earlyoom

# First-fire gotcha (observed live 2026-09-01, orchestrator T-3398 deploy): enable --now
# starts oomd, but the very first `oomctl` can hit a 25s org.freedesktop.oom1
# D-Bus activation timeout — oomd is up but hasn't claimed the bus name yet, so
# D-Bus tries to re-activate it. One restart makes it claim the name cleanly.
echo "== 6. settle oomd (claim the org.freedesktop.oom1 D-Bus name) =="
RUN systemctl restart systemd-oomd || true

echo "== 7. verify =="
if [ "$MODE" != "dry-run" ]; then verify_state; fi
echo "DONE ($MODE). Supervised first-fire: watch \`oomctl\` + \`journalctl -u systemd-oomd -f\` under normal load; confirm no false-kill of a healthy session. Rollback: $0 --rollback"
