#!/usr/bin/env bash
# tools/codex_recovery_audit.sh — residual-sudo audit for the codex-recovery
# broker (ORG-048 §5 install-gate item 4, T-2805 increment 1).
#
# WHAT: a runnable pass/fail checklist confirming the recovery seat's
# (codex-fallback, T-4884) ONLY privileged surface is the single pinned
# codex-recovery sudoers line, that the legacy `codex` account holds no grant
# at all, and that Recovery's (claude-fallback) only broker lines are the exact,
# generated `lease <op> <target>` / `linger-enable <target>` argv lines — no broader grant, no
# group-sudo membership, no polkit equivalent, no writable install surface,
# no wildcard in any codex-recovery sudoers line, and the broker's
# interpreter line is `#!/usr/bin/python3 -I` (T-4884 B4). Built now (increment 1, no live install yet) so it is reviewed
# ahead of increment 2's actual sudoers/group changes; it is the acceptance
# gate those changes must pass, not a periodic monitor (though nothing stops
# it being cron'd later).
#
# Acceptance (per the build spec, re-homed by T-4884): the recovery account
# has EXACTLY one passwordless sudo command — the pinned broker path — no
# password-gated broad sudo, no dangerous group membership; legacy `codex` has
# no sudo grant and no dangerous group; no polkit equivalent, no
# group/world-writable install surface, no setuid/setcap binary in the
# scanned install dirs.
#
# TEST SEAM: every data source below is overridable via an env var so
# tools/test_codex_recovery.py can feed synthetic fixtures (including a
# synthetic over-grant) without needing real root/sudo in CI:
#   CODEX_RECOVERY_AUDIT_ACCOUNT                — recovery invoker, default codex-fallback
#   CODEX_RECOVERY_AUDIT_LEASE_ACCOUNT          — lease-only invoker, default claude-fallback
#   CODEX_RECOVERY_AUDIT_ID_OUTPUT_FILE        — file to `cat` instead of `id <recovery account>`
#   CODEX_RECOVERY_AUDIT_SUDO_L_OUTPUT_FILE     — file to `cat` instead of `sudo -n -l -U <recovery account>`
#   CODEX_RECOVERY_AUDIT_LEGACY_ID_OUTPUT_FILE     — file to `cat` instead of `id codex`
#   CODEX_RECOVERY_AUDIT_LEGACY_SUDO_L_OUTPUT_FILE — file to `cat` instead of `sudo -n -l -U codex`
#   CODEX_RECOVERY_AUDIT_SUDOERS_DIR            — default /etc/sudoers.d
#   CODEX_RECOVERY_AUDIT_SUDOERS_MAIN_FILE      — default /etc/sudoers
#   CODEX_RECOVERY_AUDIT_POLKIT_DIR             — default /etc/polkit-1/rules.d (skipped if absent)
#   CODEX_RECOVERY_AUDIT_SCAN_DIRS              — space-separated writability/setuid scan dirs,
#                                                  default "/usr/local/sbin /usr/local/bin"
#   CODEX_RECOVERY_AUDIT_EXPECTED_BROKER_PATH   — default /usr/local/sbin/codex-recovery (the path the
#                                                  sudoers lines must name)
#   CODEX_RECOVERY_AUDIT_BROKER_FILE            — the installed file whose owner/mode/shebang is checked,
#                                                  default = EXPECTED_BROKER_PATH (T-5142: lets tests stop
#                                                  reading a host's real root-owned broker)
#   CODEX_RECOVERY_AUDIT_EXPECTED_OWNER         — default root (tests override to their own user)
#   CODEX_RECOVERY_AUDIT_CODEX_NUMERIC_IDS      — space-separated uids/gids of the codex accounts,
#                                                  default from `id -u/-g` (T-5036)
#
# Usage (production, increment 2+): sudo tools/codex_recovery_audit.sh
# Exit 0 = pass. Exit 1 = at least one FAIL, printed above the summary.
set -euo pipefail

EXPECTED_OWNER="${CODEX_RECOVERY_AUDIT_EXPECTED_OWNER:-root}"
EXPECTED_BROKER_PATH="${CODEX_RECOVERY_AUDIT_EXPECTED_BROKER_PATH:-/usr/local/sbin/codex-recovery}"
BROKER_FILE="${CODEX_RECOVERY_AUDIT_BROKER_FILE:-$EXPECTED_BROKER_PATH}"
SUDOERS_DIR="${CODEX_RECOVERY_AUDIT_SUDOERS_DIR:-/etc/sudoers.d}"
SUDOERS_MAIN_FILE="${CODEX_RECOVERY_AUDIT_SUDOERS_MAIN_FILE:-/etc/sudoers}"
POLKIT_DIR="${CODEX_RECOVERY_AUDIT_POLKIT_DIR:-/etc/polkit-1/rules.d}"
RECOVERY_ACCOUNT="${CODEX_RECOVERY_AUDIT_ACCOUNT:-codex-fallback}"
LEASE_ACCOUNT="${CODEX_RECOVERY_AUDIT_LEASE_ACCOUNT:-claude-fallback}"
LEGACY_ACCOUNT="codex"
EXPECTED_SHEBANG='#!/usr/bin/python3 -I'
SCAN_DIRS="${CODEX_RECOVERY_AUDIT_SCAN_DIRS:-/usr/local/sbin /usr/local/bin}"

DANGEROUS_GROUPS=(sudo docker lxd adm systemd-journal root shadow)
# T-4884 B7: the recovery account reads the sudo journal for the detective lease
# audit (design §4.4), so systemd-journal is ratified for it ONLY. It's still
# dangerous for legacy codex.
RECOVERY_ALLOWED_GROUPS=(systemd-journal)

fail_count=0

pass() { printf '[ok]   %s\n' "$*"; }
fail() { printf '[FAIL] %s\n' "$*"; fail_count=$((fail_count + 1)); }
note() { printf '[note] %s\n' "$*"; }

# ---------------------------------------------------------------------------
# 1. id <account> — no dangerous group membership (recovery account + legacy codex).
# ---------------------------------------------------------------------------
check_groups() {
  local account="$1" seam_file="$2" id_output groups_field
  if [ -n "$seam_file" ]; then
    id_output=$(cat "$seam_file")
  else
    id_output=$(id "$account" 2>&1 || true)
  fi
  groups_field=$(printf '%s\n' "$id_output" | grep -o 'groups=[^ ]*' || true)
  local bad_groups=()
  for grp in "${DANGEROUS_GROUPS[@]}"; do
    if [ "$account" = "$RECOVERY_ACCOUNT" ] && [[ " ${RECOVERY_ALLOWED_GROUPS[*]} " == *" $grp "* ]]; then
      continue
    fi
    if printf '%s' "$groups_field" | grep -qE "\((${grp})\)"; then
      bad_groups+=("$grp")
    fi
  done
  if [ "${#bad_groups[@]}" -eq 0 ]; then
    pass "$account is not a member of any dangerous group (${DANGEROUS_GROUPS[*]})"
  else
    fail "$account is a member of dangerous group(s): ${bad_groups[*]} — id output: $id_output"
  fi
}

check_groups "$RECOVERY_ACCOUNT" "${CODEX_RECOVERY_AUDIT_ID_OUTPUT_FILE:-}"
check_groups "$LEGACY_ACCOUNT" "${CODEX_RECOVERY_AUDIT_LEGACY_ID_OUTPUT_FILE:-}"

# ---------------------------------------------------------------------------
# 2. sudo -l -U <recovery account> — exactly one NOPASSWD command, pinned to the broker.
# ---------------------------------------------------------------------------
if [ -n "${CODEX_RECOVERY_AUDIT_SUDO_L_OUTPUT_FILE:-}" ]; then
  sudo_l_output=$(cat "$CODEX_RECOVERY_AUDIT_SUDO_L_OUTPUT_FILE")
else
  sudo_l_output=$(sudo -n -l -U "$RECOVERY_ACCOUNT" 2>&1 || true)
fi

# The recovery account's NOPASSWD lines must be exactly these:
#  - `(root) NOPASSWD: <broker>`: required, exactly once (ORG-048 §5, re-homed);
#  - `(<lease account>) NOPASSWD: ALL`: at most once. This is the MUTUAL direct sudo
#    the operator ratified (TG 11705, T-4884 A1). Its run-as must be exactly the lease
#    account: never root, never ALL, never a list.
# Any other NOPASSWD line FAILS, including ALL as root or as anyone else.
pinned_lines=0; mutual_lines=0; other_lines=""
while IFS= read -r ln; do
  ln=$(printf '%s' "$ln" | xargs)
  case "$ln" in *NOPASSWD*) ;; *) continue ;; esac
  if [ "$ln" = "(root) NOPASSWD: ${EXPECTED_BROKER_PATH}" ]; then
    pinned_lines=$((pinned_lines + 1))
  elif [ "$ln" = "(${LEASE_ACCOUNT}) NOPASSWD: ALL" ]; then
    mutual_lines=$((mutual_lines + 1))
  else
    other_lines="${other_lines}${ln}; "
  fi
done <<< "$sudo_l_output"

if [ -n "$other_lines" ]; then
  fail "$RECOVERY_ACCOUNT has an unexpected passwordless grant: $other_lines (sudo -l output: $sudo_l_output)"
elif [ "$pinned_lines" -eq 1 ] && [ "$mutual_lines" -le 1 ]; then
  pass "$RECOVERY_ACCOUNT passwordless sudo = the pinned ${EXPECTED_BROKER_PATH} line (+ ${mutual_lines} ratified mutual ${LEASE_ACCOUNT} grant)"
else
  fail "$RECOVERY_ACCOUNT's passwordless sudo is not exactly one pinned ${EXPECTED_BROKER_PATH} line (+ at most the mutual ${LEASE_ACCOUNT} grant) — sudo -l output: $sudo_l_output"
fi

# 2b. legacy codex — no sudo grant of any kind (T-4884 §10: codex gets no
# recovery grant and is removed from group sudo; B12).
if [ -n "${CODEX_RECOVERY_AUDIT_LEGACY_SUDO_L_OUTPUT_FILE:-}" ]; then
  legacy_sudo_l=$(cat "$CODEX_RECOVERY_AUDIT_LEGACY_SUDO_L_OUTPUT_FILE")
else
  legacy_sudo_l=$(sudo -n -l -U "$LEGACY_ACCOUNT" 2>&1 || true)
fi
legacy_grants=$(printf '%s\n' "$legacy_sudo_l" | grep -E '^[[:space:]]*\(' || true)
if [ -z "$legacy_grants" ]; then
  pass "$LEGACY_ACCOUNT holds no sudo grant"
else
  fail "$LEGACY_ACCOUNT still holds sudo grant(s): $legacy_grants"
fi

# ---------------------------------------------------------------------------
# 3. sudoers.d + main sudoers — no OTHER codex reference anywhere.
# ---------------------------------------------------------------------------
broker_re="${EXPECTED_BROKER_PATH//\//\\/}"
name_re='[A-Za-z0-9]([A-Za-z0-9_.-]{0,62}[A-Za-z0-9])?'
# The ONLY codex-mentioning sudoers lines allowed (the generator's output,
# tools/codex_recovery_sudoers.py): the recovery account's single broker
# line, the lease account's exact `lease <op> <target>` lines, and the
# command-scoped env_reset Defaults. Anything else — including ANY line for
# the legacy `codex` account — fails.
allowed_line_res=(
  "^${RECOVERY_ACCOUNT}[[:space:]]+ALL=\\(root\\)[[:space:]]+NOPASSWD:[[:space:]]+${broker_re}\$"
  "^${LEASE_ACCOUNT}[[:space:]]+ALL=\\(root\\)[[:space:]]+NOPASSWD:[[:space:]]+${broker_re}[[:space:]]+lease[[:space:]]+(acquire|release|status)[[:space:]]+${name_re}\$"
  # T-5059: the lease account's exact `linger-enable <target>` argv lines.
  "^${LEASE_ACCOUNT}[[:space:]]+ALL=\\(root\\)[[:space:]]+NOPASSWD:[[:space:]]+${broker_re}[[:space:]]+linger-enable[[:space:]]+${name_re}\$"
  "^Defaults!${broker_re}[[:space:]]+env_reset\$"
  # T-4884 B7: the two exact mutual direct-sudo lines (the operator TG 11705).
  "^${LEASE_ACCOUNT}[[:space:]]+ALL=\\(${RECOVERY_ACCOUNT}\\)[[:space:]]+NOPASSWD:[[:space:]]+ALL\$"
  "^${RECOVERY_ACCOUNT}[[:space:]]+ALL=\\(${LEASE_ACCOUNT}\\)[[:space:]]+NOPASSWD:[[:space:]]+ALL\$"
)
# Recovery's pre-existing asymmetric grant (ORG-063 r4) may name codex accounts as
# RUN-AS targets. That's Recovery's privilege, not codex's, so it's allowed, but only
# when the run-as list is plain account names and contains neither root nor ALL.
recovery_runas_re="^${LEASE_ACCOUNT}[[:space:]]+ALL=\\([a-z0-9_-]+([[:space:]]*,[[:space:]]*[a-z0-9_-]+)*\\)[[:space:]]+NOPASSWD:[[:space:]]+ALL\$"

# T-5033: EXACT-line allowlist (string equality after whitespace normalisation,
# no regex) for the ratified host lines that mention codex. Each carries its cite.
ratified_exact_lines=(
  # Recovery's legacy consultant rescue grant (56-recovery-rescues-codex-fallback/fallback files; ORG-063 r4).
  "${LEASE_ACCOUNT} ALL=(codex) NOPASSWD: ALL"
  # MUTUAL Recovery<->Watchman grant, ORG-048 Amendment A1 (the operator TG 11705, adopted TG 11731;
  # notes/2026-10-12-t4884-codex-recovery-seat-design.md sec 5/7).
  "${LEASE_ACCOUNT} ALL=(${RECOVERY_ACCOUNT}) NOPASSWD: ALL"
  "${RECOVERY_ACCOUNT} ALL=(${LEASE_ACCOUNT}) NOPASSWD: ALL"
  # s3star audit runner's constrained tmux wrapper (T-1884; tools/sudoers.d/pantheon-s3star-tmux).
  "claude ALL=(codex) NOPASSWD: /usr/local/bin/pantheon-s3star-tmux"
)

# Collapse whitespace runs, strip ends, and fold `ALL = (x)` / `NOPASSWD :` spacing.
normalize_sudoers_line() {
  printf '%s' "$1" | sed -E 's/[[:space:]]+/ /g; s/^ //; s/ $//; s/ ?= ?\(/=(/; s/NOPASSWD ?:/NOPASSWD:/; s/NOPASSWD: ?/NOPASSWD: /'
}

line_is_allowed() {
  local normalized="$1" re runas exact
  normalized=$(normalize_sudoers_line "$normalized")
  for exact in "${ratified_exact_lines[@]}"; do
    [ "$normalized" = "$exact" ] && return 0
  done
  for re in "${allowed_line_res[@]}"; do
    if printf '%s' "$normalized" | grep -qE "$re"; then
      return 0
    fi
  done
  if printf '%s' "$normalized" | grep -qE "$recovery_runas_re"; then
    runas=$(printf '%s' "$normalized" | sed -E 's/^[^(]*\(([^)]*)\).*/\1/')
    if ! printf '%s' "$runas" | grep -qwE 'root|ALL'; then
      return 0
    fi
  fi
  return 1
}

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
  local bad=0 bad_lines="" trimmed
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    # T-5033 REBOUND 1: NO xargs here (it strips quotes/backslashes, letting a
    # trailing-backslash continuation normalise to an allowed string). Trim
    # ourselves, and reject any codex line carrying a quote or backslash.
    trimmed=$(printf '%s' "$line" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')
    if printf '%s' "$line" | grep -qE '["'"'"'\\]'; then
      bad=1
      bad_lines="${bad_lines}${line} [quote/backslash]; "
    elif ! line_is_allowed "$trimmed"; then
      bad=1
      bad_lines="${bad_lines}${line}; "
    fi
  done <<< "$matches"
  # Any sudoers line continuation touching a codex line (either side) FAILS.
  local cont_hits
  cont_hits=$(find "$path" -type f 2>/dev/null | sort | while IFS= read -r f; do
    awk -v F="$f" '
      /^[[:space:]]*#/ { prev_cont = 0; next }
      {
        cont = ($0 ~ /\\[[:space:]]*$/)
        codex = (tolower($0) ~ /codex/)
        if (codex && (cont || prev_cont)) print F ": " $0
        prev_cont = cont
      }' "$f" 2>/dev/null
  done | tr '\n' '|' || true)
  if [ -n "$cont_hits" ]; then
    bad=1
    bad_lines="${bad_lines}line-continuation touching a codex line: ${cont_hits}; "
  fi
  if [ "$bad" -eq 0 ]; then
    pass "$label codex reference(s) match only the pinned broker line and the generated exact lease lines"
  else
    fail "$label has an unexpected codex reference — not allowed: $bad_lines"
  fi
}

check_sudoers_source "sudoers.d" "$SUDOERS_DIR"
check_sudoers_source "main sudoers file" "$SUDOERS_MAIN_FILE"

# 3b. No wildcard in ANY codex-recovery sudoers line (T-4884 round-2 #3).
# Independent of 3: even an otherwise-allowed shape must not carry a `*`.
check_no_wildcard() {
  local label="$1" path="$2"
  [ -e "$path" ] || { note "$label absent ($path) — skipped wildcard check"; return; }
  local hits
  hits=$(grep -RhE . "$path" 2>/dev/null \
    | grep -vE '^[[:space:]]*#' \
    | grep -F 'codex-recovery' \
    | grep -F '*' || true)
  if [ -z "$hits" ]; then
    pass "$label has no wildcard in any codex-recovery line"
  else
    fail "$label has a wildcard in a codex-recovery line: $hits"
  fi
}

check_no_wildcard "sudoers.d" "$SUDOERS_DIR"
check_no_wildcard "main sudoers file" "$SUDOERS_MAIN_FILE"

# 3c. Indirection the codex grep above cannot see (T-5036). The grep reads lines containing the
# string "codex", and drops every line starting with '#' as a comment. sudoers disagrees on both:
#   - a user, group or runas can be named by NUMBER: `#1003`, `%#1004`, `(#1011)`. A line that
#     starts `#1003 ALL=...` is a user spec, not a comment. A numeric id for either codex account,
#     anywhere on a non-comment line, FAILS: none of the ratified lines use one.
#   - `@include`/`@includedir` and the legacy `#include`/`#includedir` pull in files this audit
#     never scans. The only one allowed is the main file's `@includedir`/`#includedir` of the
#     scanned sudoers.d dir; any other include FAILS.
# Alias definitions that NAME codex are already caught by 3 (the name is on the line). A
# non-codex alias (e.g. pantheon-pwa's Cmnd_Alias KINO_SVC) is fine and stays allowed.
# Authoritative effective grants still come from `sudo -l -U` in 2, which resolves aliases and
# includes; this is the source-level backstop for what that output summarises.
codex_numeric_ids() {
  if [ -n "${CODEX_RECOVERY_AUDIT_CODEX_NUMERIC_IDS:-}" ]; then
    printf '%s\n' $CODEX_RECOVERY_AUDIT_CODEX_NUMERIC_IDS
    return
  fi
  local a
  for a in "$RECOVERY_ACCOUNT" "$LEGACY_ACCOUNT"; do
    id -u "$a" 2>/dev/null || true
    id -g "$a" 2>/dev/null || true
  done
}

check_sudoers_indirection() {
  local label="$1" path="$2" allowed_includedir="$3"
  [ -e "$path" ] || { note "$label absent ($path) — skipped indirection check"; return; }
  local ids
  ids=$(codex_numeric_ids | grep -E '^[0-9]+$' | sort -u | paste -sd '|' -)
  local hits=""
  hits=$(find "$path" -type f 2>/dev/null | sort | while IFS= read -r f; do
    awk -v F="$f" -v IDS="$ids" -v OKDIR="$allowed_includedir" '
      {
        line = $0
        sub(/^[[:space:]]+/, "", line)
        if (line ~ /^[@#]include(dir)?([[:space:]]|$)/) {
          n = split(line, w, /[[:space:]]+/)
          dir = w[2]; sub(/\/+$/, "", dir); ok = OKDIR; sub(/\/+$/, "", ok)
          if (!(OKDIR != "" && w[1] ~ /includedir$/ && n == 2 && dir == ok))
            print F ": include outside the scanned set: " $0
          next
        }
        if (line ~ /^#/ && line !~ /^#[0-9]/) next
        if (IDS != "" && line ~ ("(^|[^0-9A-Za-z_])%?#(" IDS ")([^0-9]|$)"))
          print F ": numeric id of a codex account: " $0
      }' "$f" 2>/dev/null
  done | tr '\n' '|' || true)
  if [ -z "$hits" ]; then
    pass "$label has no include outside the scanned set and no numeric codex id"
  else
    fail "$label has sudoers indirection the codex grep cannot see: $hits"
  fi
}

check_sudoers_indirection "sudoers.d" "$SUDOERS_DIR" ""
check_sudoers_indirection "main sudoers file" "$SUDOERS_MAIN_FILE" "$SUDOERS_DIR"

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

check_not_writable "$BROKER_FILE"
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
# 7. Broker interpreter line — absolute, isolated-mode python (T-4884 §5).
#    Defence in depth, not the boundary: it stops PYTHONPATH / user-site
#    injection; the boundary is the path chain + sudoers argv + validation.
# ---------------------------------------------------------------------------
if [ -e "$BROKER_FILE" ]; then
  first_line=$(head -n 1 "$BROKER_FILE" 2>/dev/null || true)
  if [ "$first_line" = "$EXPECTED_SHEBANG" ]; then
    pass "$BROKER_FILE interpreter line is '$EXPECTED_SHEBANG'"
  else
    fail "$BROKER_FILE interpreter line is '$first_line', expected '$EXPECTED_SHEBANG'"
  fi
else
  note "$BROKER_FILE absent — skipped interpreter-line check"
fi

# ---------------------------------------------------------------------------
echo "---"
if [ "$fail_count" -eq 0 ]; then
  echo "codex-recovery residual-privilege audit: PASS"
  exit 0
else
  echo "codex-recovery residual-privilege audit: FAIL ($fail_count check(s) failed)"
  exit 1
fi
