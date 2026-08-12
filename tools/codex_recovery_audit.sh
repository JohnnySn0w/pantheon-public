#!/usr/bin/env bash
# tools/codex_recovery_audit.sh — residual-sudo audit for the codex-recovery
# broker (ORG-048 §5 install-gate item 4, T-2805 increment 1).
#
# WHAT: a runnable pass/fail checklist confirming codex's ONLY privileged
# surface is the single pinned codex-recovery sudoers line — no broader
# grant, no group-sudo membership, no polkit equivalent, no writable install
# surface. Built now (increment 1, no live install yet) so it is reviewed
# ahead of increment 2's actual sudoers/group changes; it is the acceptance
# gate those changes must pass, not a periodic monitor (though nothing stops
# it being cron'd later).
#
# Acceptance (per the build spec): codex has EXACTLY one passwordless sudo
# command — the pinned broker path — no password-gated broad sudo, no
# dangerous group membership, no polkit equivalent, no group/world-writable
# install surface, no setuid/setcap binary in the scanned install dirs.
#
# TEST SEAM: every data source below is overridable via an env var so
# tools/test_codex_recovery.py can feed synthetic fixtures (including a
# synthetic over-grant) without needing real root/sudo in CI:
#   CODEX_RECOVERY_AUDIT_ID_OUTPUT_FILE        — file to `cat` instead of `id codex`
#   CODEX_RECOVERY_AUDIT_SUDO_L_OUTPUT_FILE     — file to `cat` instead of `sudo -n -l -U codex`
#   CODEX_RECOVERY_AUDIT_SUDOERS_DIR            — default /etc/sudoers.d
#   CODEX_RECOVERY_AUDIT_SUDOERS_MAIN_FILE      — default /etc/sudoers
#   CODEX_RECOVERY_AUDIT_POLKIT_DIR             — default /etc/polkit-1/rules.d (skipped if absent)
#   CODEX_RECOVERY_AUDIT_SCAN_DIRS              — space-separated writability/setuid scan dirs,
#                                                  default "/usr/local/sbin /usr/local/bin"
#   CODEX_RECOVERY_AUDIT_EXPECTED_BROKER_PATH   — default /usr/local/sbin/codex-recovery
#   CODEX_RECOVERY_AUDIT_EXPECTED_OWNER         — default root (tests override to their own user)
#
# Usage (production, increment 2+): sudo tools/codex_recovery_audit.sh
# Exit 0 = pass. Exit 1 = at least one FAIL, printed above the summary.
set -euo pipefail

EXPECTED_OWNER="${CODEX_RECOVERY_AUDIT_EXPECTED_OWNER:-root}"
EXPECTED_BROKER_PATH="${CODEX_RECOVERY_AUDIT_EXPECTED_BROKER_PATH:-/usr/local/sbin/codex-recovery}"
SUDOERS_DIR="${CODEX_RECOVERY_AUDIT_SUDOERS_DIR:-/etc/sudoers.d}"
SUDOERS_MAIN_FILE="${CODEX_RECOVERY_AUDIT_SUDOERS_MAIN_FILE:-/etc/sudoers}"
POLKIT_DIR="${CODEX_RECOVERY_AUDIT_POLKIT_DIR:-/etc/polkit-1/rules.d}"
SCAN_DIRS="${CODEX_RECOVERY_AUDIT_SCAN_DIRS:-/usr/local/sbin /usr/local/bin}"

DANGEROUS_GROUPS=(sudo docker lxd adm systemd-journal root shadow)

fail_count=0

pass() { printf '[ok]   %s\n' "$*"; }
fail() { printf '[FAIL] %s\n' "$*"; fail_count=$((fail_count + 1)); }
note() { printf '[note] %s\n' "$*"; }

# ---------------------------------------------------------------------------
# 1. id codex — no dangerous group membership.
# ---------------------------------------------------------------------------
if [ -n "${CODEX_RECOVERY_AUDIT_ID_OUTPUT_FILE:-}" ]; then
  id_output=$(cat "$CODEX_RECOVERY_AUDIT_ID_OUTPUT_FILE")
else
  id_output=$(id codex 2>&1 || true)
fi

groups_field=$(printf '%s\n' "$id_output" | grep -o 'groups=[^ ]*' || true)
bad_groups=()
for grp in "${DANGEROUS_GROUPS[@]}"; do
  if printf '%s' "$groups_field" | grep -qE "\((${grp})\)"; then
    bad_groups+=("$grp")
  fi
done
if [ "${#bad_groups[@]}" -eq 0 ]; then
  pass "codex is not a member of any dangerous group (${DANGEROUS_GROUPS[*]})"
else
  fail "codex is a member of dangerous group(s): ${bad_groups[*]} — id output: $id_output"
fi

# ---------------------------------------------------------------------------
# 2. sudo -l -U codex — exactly one NOPASSWD command, pinned to the broker.
# ---------------------------------------------------------------------------
if [ -n "${CODEX_RECOVERY_AUDIT_SUDO_L_OUTPUT_FILE:-}" ]; then
  sudo_l_output=$(cat "$CODEX_RECOVERY_AUDIT_SUDO_L_OUTPUT_FILE")
else
  sudo_l_output=$(sudo -n -l -U codex 2>&1 || true)
fi

nopasswd_lines=$(printf '%s\n' "$sudo_l_output" | grep -c 'NOPASSWD' || true)
pinned_lines=$(printf '%s\n' "$sudo_l_output" | grep -F "NOPASSWD: ${EXPECTED_BROKER_PATH}" | grep -vc 'ALL$' || true)
has_all_grant=$(printf '%s\n' "$sudo_l_output" | grep -E 'NOPASSWD:\s*ALL\b' || true)

if [ -n "$has_all_grant" ]; then
  fail "codex has a broad NOPASSWD:ALL grant — sudo -l output: $sudo_l_output"
elif [ "$nopasswd_lines" -eq 1 ] && [ "$pinned_lines" -eq 1 ]; then
  pass "codex has exactly one passwordless sudo command, pinned to ${EXPECTED_BROKER_PATH}"
else
  fail "codex's passwordless sudo grant is not exactly one pinned ${EXPECTED_BROKER_PATH} line — sudo -l output: $sudo_l_output"
fi

# ---------------------------------------------------------------------------
# 3. sudoers.d + main sudoers — no OTHER codex reference anywhere.
# ---------------------------------------------------------------------------
allowed_line_re="^codex[[:space:]]+ALL=\\(root\\)[[:space:]]+NOPASSWD:[[:space:]]+${EXPECTED_BROKER_PATH//\//\\/}\$"

check_sudoers_source() {
  local label="$1" path="$2"
  [ -e "$path" ] || { note "$label absent ($path) — skipped"; return; }
  # Two-step, not a single '[^#].*codex' pattern: a POSIX-ERE match consumes
  # the character '[^#]' matches, so a line that STARTS with "codex" (the
  # exact shape every real entry has) would need "codex" to appear a SECOND
  # time to match — it never does, so a single-pattern match silently missed
  # every real entry, good or rogue. Filter comments/blanks first, then grep
  # for "codex" as an independent pass.
  local matches
  matches=$(grep -RhE . "$path" 2>/dev/null \
    | grep -vE '^[[:space:]]*#' \
    | grep -vE '^[[:space:]]*$' \
    | grep -i 'codex' || true)
  if [ -z "$matches" ]; then
    pass "$label has no codex references"
    return
  fi
  local bad=0
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    if ! printf '%s' "$(printf '%s' "$line" | xargs)" | grep -qE "$allowed_line_re"; then
      bad=1
    fi
  done <<< "$matches"
  if [ "$bad" -eq 0 ]; then
    pass "$label codex reference(s) match only the pinned broker line"
  else
    fail "$label has an unexpected codex reference — matches: $matches"
  fi
}

check_sudoers_source "sudoers.d" "$SUDOERS_DIR"
check_sudoers_source "main sudoers file" "$SUDOERS_MAIN_FILE"

# ---------------------------------------------------------------------------
# 4. polkit — no rule references codex.
# ---------------------------------------------------------------------------
if [ -d "$POLKIT_DIR" ]; then
  polkit_hits=$(grep -RilE 'codex' "$POLKIT_DIR" 2>/dev/null || true)
  if [ -z "$polkit_hits" ]; then
    pass "no polkit rule references codex"
  else
    fail "polkit rule(s) reference codex: $polkit_hits"
  fi
else
  note "polkit rules dir absent ($POLKIT_DIR) — skipped"
fi

# ---------------------------------------------------------------------------
# 5. Writability of the install surface (broker path + scan dirs).
# ---------------------------------------------------------------------------
check_not_writable() {
  local path="$1"
  [ -e "$path" ] || { note "$path absent — skipped writability check"; return; }
  local owner mode
  owner=$(stat -c '%U' "$path")
  mode=$(stat -c '%a' "$path")
  local last_digit="${mode: -1}"
  local group_digit="${mode: -2:1}"
  if [ "$owner" != "$EXPECTED_OWNER" ]; then
    fail "$path is not owned by $EXPECTED_OWNER (owner: $owner)"
  elif (( (10#$group_digit & 2) != 0 || (10#$last_digit & 2) != 0 )); then
    fail "$path is group- or other-writable (mode $mode)"
  else
    pass "$path is $EXPECTED_OWNER-owned and not group/other-writable (mode $mode)"
  fi
}

check_not_writable "$EXPECTED_BROKER_PATH"
for dir in $SCAN_DIRS; do
  check_not_writable "$dir"
done

# ---------------------------------------------------------------------------
# 6. setuid/setcap binaries in the scanned install dirs.
# ---------------------------------------------------------------------------
for dir in $SCAN_DIRS; do
  [ -d "$dir" ] || { note "$dir absent — skipped setuid/setcap scan"; continue; }
  setuid_hits=$(find "$dir" -maxdepth 2 -type f \( -perm -4000 -o -perm -2000 \) 2>/dev/null || true)
  if [ -n "$setuid_hits" ]; then
    fail "setuid/setgid binaries found under $dir: $setuid_hits"
  else
    pass "no setuid/setgid binaries under $dir"
  fi
  if command -v getcap >/dev/null 2>&1; then
    setcap_hits=$(getcap -r "$dir" 2>/dev/null || true)
    if [ -n "$setcap_hits" ]; then
      fail "capability-bearing binaries found under $dir: $setcap_hits"
    else
      pass "no capability-bearing binaries under $dir"
    fi
  else
    note "getcap not available — skipped capability scan for $dir"
  fi
done

# ---------------------------------------------------------------------------
echo "---"
if [ "$fail_count" -eq 0 ]; then
  echo "codex-recovery residual-privilege audit: PASS"
  exit 0
else
  echo "codex-recovery residual-privilege audit: FAIL ($fail_count check(s) failed)"
  exit 1
fi
