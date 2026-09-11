#!/bin/bash
# T-3277 Option A: reconcile persona-consume timer drift across 4 seats.
# Read-only default (--dry-run). Pass --commit-prod to actually mutate.
# Reversible: original files backed up per-seat before edit.
#
# Fixes: installed pantheon-persona-consume@.timer has OnBootSec=2min
# (from pre-2026-08-15 installs, before commit 4b824356f flipped it).
# Repo canonical is OnActiveSec=2min. Semantic drift only; live behavior
# essentially identical (OnUnitInactiveSec=2min handles ongoing cadence).
#
# Affected seats: claude-research (specialist), claude-fallback (recovery),
# suborch1 (metis), suborch2 (atlas). Orchestrator's seat (claude) is already
# correct — not touched.
#
# Full audit evidence + rationale: T-3277 annotations.
# operator-authorized only. Do NOT run without ratify.

set -uo pipefail

DRY_RUN=1
[[ "${1:-}" == "--commit-prod" ]] && DRY_RUN=0

SUDO_PW=$(tr -d '\n' < /home/claude/root.env)
STAMP=$(TZ=UTC date +%Y%m%dT%H%M%SZ)
DRIFTED_ACCTS=(claude-research claude-fallback suborch1 suborch2)
TIMER_PATH='~/.config/systemd/user/pantheon-persona-consume@.timer'
BACKUP_SUFFIX=".pre-t3277-reconcile-${STAMP}"

echo "T-3277 Option A reconcile — $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "  mode: $([ $DRY_RUN -eq 1 ] && echo 'DRY-RUN (no writes)' || echo 'COMMIT (live mutation)')"
echo "  stamp: $STAMP"
echo "  backup suffix: $BACKUP_SUFFIX"
echo ""

sudo_run() {
    local acct="$1"
    shift
    echo "$SUDO_PW" | sudo -S -k -u "$acct" "$@" 2>/dev/null
}

for acct in "${DRIFTED_ACCTS[@]}"; do
    echo "=== $acct ==="

    # Pre-flight: verify current state is OnBootSec (target of the fix)
    current=$(sudo_run "$acct" grep -E '^On(Boot|Active)Sec' /home/"$acct"/.config/systemd/user/pantheon-persona-consume@.timer)
    echo "  pre: $current"

    if ! echo "$current" | grep -q 'OnBootSec=2min'; then
        echo "  SKIP: not in drifted state (OnBootSec expected)"
        continue
    fi

    if [ $DRY_RUN -eq 1 ]; then
        echo "  DRY-RUN would: cp timer${BACKUP_SUFFIX}; sed OnBootSec → OnActiveSec; systemctl --user daemon-reload"
        continue
    fi

    # 1. Backup
    sudo_run "$acct" cp /home/"$acct"/.config/systemd/user/pantheon-persona-consume@.timer \
        /home/"$acct"/.config/systemd/user/pantheon-persona-consume@.timer"$BACKUP_SUFFIX"
    echo "  backup: pantheon-persona-consume@.timer${BACKUP_SUFFIX}"

    # 2. Swap OnBootSec → OnActiveSec (matches repo canonical)
    sudo_run "$acct" sed -i 's/^OnBootSec=2min$/OnActiveSec=2min/' \
        /home/"$acct"/.config/systemd/user/pantheon-persona-consume@.timer

    # 3. Verify swap succeeded
    post=$(sudo_run "$acct" grep -E '^On(Boot|Active)Sec' /home/"$acct"/.config/systemd/user/pantheon-persona-consume@.timer)
    echo "  post: $post"

    if ! echo "$post" | grep -q 'OnActiveSec=2min'; then
        echo "  ERROR: swap did not take; restoring backup"
        sudo_run "$acct" mv /home/"$acct"/.config/systemd/user/pantheon-persona-consume@.timer"$BACKUP_SUFFIX" \
            /home/"$acct"/.config/systemd/user/pantheon-persona-consume@.timer
        continue
    fi

    # 4. Daemon-reload (systemctl --user needs a login shell/session; use sudo -i or systemd-run)
    # NOTE: This requires user-session systemd manager to be reachable.
    # On accounts where no user-session is running, this will fail — but the file swap
    # is already in place and will take effect at next login/reboot.
    sudo_run "$acct" bash -lc 'systemctl --user daemon-reload' || \
        echo "  WARN: daemon-reload failed (no user session?) — file swap persists, effect at next reload"

    echo "  DONE: $acct reconciled"
done

echo ""
echo "Complete."
[ $DRY_RUN -eq 1 ] && echo "This was a DRY-RUN. Pass --commit-prod to mutate."

# Post-run verification (informational):
echo ""
echo "--- Verification (all seats) ---"
for acct in claude "${DRIFTED_ACCTS[@]}"; do
    v=$(sudo_run "$acct" grep -E '^On(Boot|Active)Sec' /home/"$acct"/.config/systemd/user/pantheon-persona-consume@.timer 2>/dev/null | tr '\n' ' ')
    echo "  $acct: $v"
done

# Rollback: if a specific seat needs revert, run:
#   echo "$SUDO_PW" | sudo -S -k -u <acct> mv <timer>.pre-t3277-reconcile-<STAMP> <timer>
#   echo "$SUDO_PW" | sudo -S -k -u <acct> bash -lc 'systemctl --user daemon-reload'
