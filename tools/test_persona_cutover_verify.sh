#!/usr/bin/env bash
# test_persona_cutover_verify.sh — T-2633. Two tiers of coverage:
#
#   (a) Re-exercises the SHARED, already-persona-general
#       tools/t2590_cutover_verify_lib.sh helper cutover_telegram_poller_present
#       with BOTH recovery's and specialist's real home/pane strings -- proving the
#       generic substitution works for either persona, not just re-proving the
#       lib works in the abstract (that's test_t2590_recovery_cutover_verify.sh's
#       job, unforked, still authoritative for the lib itself).
#
#   (b) NEW: subprocess-level smoke tests of tools/persona_cutover.sh's OWN
#       arg-parsing / uid-guard / manifest-precondition wiring -- coverage the
#       recovery precedent never had for t2590_recovery_cutover.sh itself (that test
#       file only ever covered the shared lib, despite its name). Run
#       PARAMETRIZED across --persona recovery and --persona specialist to prove
#       genuine persona-generality, not an specialist-shaped rerun.
#
#       SCOPE BOUNDARY (approved, T-2633 team-lead sign-off): stops at the
#       install-done precondition's OWN failure (a deterministic,
#       host-state-coupled "harness not found on PATH" from this seat, since
#       PCX_APPLY_PATH is a hardcoded per-account absolute path this seat has
#       no access to) -- does NOT chase past it into dry-run-output/apply,
#       which shells out to t2611_recovery_persona_apply.py's real,
#       LocalHostBackend-driven systemctl calls. Mirrors
#       t2590_cutover_verify_lib.sh's own "test what's isolatable" discipline.
#
#       Reaching even THIS far required the --unit-file escape hatch on
#       persona_cutover_resolve.py (and its passthrough on persona_cutover.sh/
#       persona_rollback.sh): the real invocation shape reads the target's
#       cross-account bespoke unit file unconditionally, which is
#       unreadable from this seat (mode 700) -- discovered mid-implementation,
#       see T-2633 DECISIONS.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CUTOVER="$SCRIPT_DIR/persona_cutover.sh"
ROLLBACK="$SCRIPT_DIR/persona_rollback.sh"
INSTANCE_YAML="$REPO_ROOT/instance.yaml"

# shellcheck source=t2590_cutover_verify_lib.sh
source "$SCRIPT_DIR/t2590_cutover_verify_lib.sh"

pass=0; fail=0; skipped=0
check() { # <desc> <actual> <expected>
  if [[ "$2" == "$3" ]]; then pass=$((pass+1)); echo "PASS: $1";
  else fail=$((fail+1)); echo "FAIL: $1 (got '$2', want '$3')"; fi
}
check_contains() { # <desc> <haystack> <needle>
  if [[ "$2" == *"$3"* ]]; then pass=$((pass+1)); echo "PASS: $1";
  else fail=$((fail+1)); echo "FAIL: $1 (did not find '$3' in: $2)"; fi
}
skip() { skipped=$((skipped+1)); echo "SKIP: $1"; } # <desc>

# T-2731 (ORG-066 Rung-1 isolation, T-2443 shape): the case-6 cutover / missing-backup
# rollback assertions require the persona's LIVE user bus (/run/user/<uid>) — the script
# aborts at the XDG_RUNTIME_DIR precondition (persona_cutover.sh:132 / persona_rollback.sh:104,
# a literal `[[ -d "$XRD" ]]`) before it can reach them. On the VPS / a lingering persona that
# dir exists, so the assertions run as written. On the ORG-066 CT202 CI mirror the fleet
# accounts are nologin with NO linger (C1), so /run/user/<uid> never exists and the script
# correctly refuses; there we assert THAT deterministic early-abort and skip the user-bus-only
# checkpoint. We do NOT provide the bus (linger would push the script past the abort into live
# `systemctl --user` start/stop of the fleet bot units — the exact T-2443 page hazard).
user_bus_present() { [[ -d "/run/user/$1" ]]; } # <uid>

# =============================================================================
# Tier (a): shared-lib comms-poller check, parametrized recovery + specialist
# =============================================================================

setup_comms_sandbox() { # <home>
  SBX="$(mktemp -d -t t2633_cutover_verify_test.XXXXXX)"
  FAKEBIN="$SBX/bin"; mkdir -p "$FAKEBIN"
  PROC_FIXTURE="$SBX/proc"; mkdir -p "$PROC_FIXTURE"
  HOME_DIR="$1"
  PLUGIN="$HOME_DIR/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6"
  echo 100 > "$SBX/pane_pid"

  cat > "$FAKEBIN/tmux" <<EOF
#!/bin/bash
case "\${1:-}" in
  display-message) cat "$SBX/pane_pid"; exit 0 ;;
  *) exit 0 ;;
esac
EOF
  chmod +x "$FAKEBIN/tmux"

  cat > "$FAKEBIN/pgrep" <<EOF
#!/bin/bash
[[ "\${1:-}" == -P && "\${2:-}" =~ ^[0-9]+\$ ]] || exit 2
f="$PROC_FIXTURE/\$2/children"
[[ -f "\$f" ]] || exit 1
tr ' ' '\n' < "\$f" | sed '/^\$/d'
[[ -s "\$f" ]]
EOF
  chmod +x "$FAKEBIN/pgrep"

  for pid in 100 200; do
    mkdir -p "$PROC_FIXTURE/$pid"
    : > "$PROC_FIXTURE/$pid/status"
    : > "$PROC_FIXTURE/$pid/children"
  done
  echo "200" > "$PROC_FIXTURE/100/children"

  # T-2624/T-2630 single-process direct-spawn shape -- the CURRENT real
  # launch shape both recovery's and specialist's control-plane pollers use.
  printf '%s\0' "$HOME_DIR/.bun/bin/bun" run --cwd "$PLUGIN" --silent server.ts > "$PROC_FIXTURE/200/cmdline"
}
teardown_comms_sandbox() { rm -rf "$SBX"; }

run_comms_case() { # <label> <home> <pane>
  local label="$1" home="$2" pane="$3"
  setup_comms_sandbox "$home"
  export PATH="$FAKEBIN:$PATH"
  local r
  if cutover_telegram_poller_present "$pane" "$home" "$PROC_FIXTURE"; then r=ok; else r=no; fi
  check "comms poller present for ${label} (single-process direct-spawn shape)" "$r" "ok"
  teardown_comms_sandbox
}

run_comms_case "recovery" "/home/claude-fallback" "claude-telegram-bot-fallback:0"
run_comms_case "specialist" "/home/claude-research" "claude-research-bot:0"

# =============================================================================
# Tier (a2): T-2651 shared-lib companion-rejoin predicate. Hermetic — fake
# systemctl/tmux on PATH report their own scripted active/session state, no
# real systemd or tmux server touched. Covers the false-negative that
# rolled back a live acp-bridge companion on fire#3 (unwrapped, zero-wait
# check in persona_cutover.sh) by proving the underlying predicate itself is
# correct in isolation; the bounded-retry wrapper around it is a 4-line
# caller-side loop, not separately unit-tested (see file header scope note).
# =============================================================================

setup_companion_sandbox() { # <unit-active:0|1> <session-present:0|1>
  local active="$1" present="$2"
  CSBX="$(mktemp -d -t t2651_companion_test.XXXXXX)"
  CFAKEBIN="$CSBX/bin"; mkdir -p "$CFAKEBIN"
  cat > "$CFAKEBIN/systemctl" <<EOF
#!/bin/bash
if [[ "\${1:-}" == --user && "\${2:-}" == is-active ]]; then
  [[ "$active" == 1 ]] && exit 0 || exit 3
fi
exit 0
EOF
  chmod +x "$CFAKEBIN/systemctl"
  cat > "$CFAKEBIN/tmux" <<EOF
#!/bin/bash
if [[ "\${1:-}" == has-session ]]; then
  [[ "$present" == 1 ]] && exit 0 || exit 1
fi
exit 0
EOF
  chmod +x "$CFAKEBIN/tmux"
}
teardown_companion_sandbox() { rm -rf "$CSBX"; }

run_companion_case() { # <label> <active:0|1> <present:0|1> <expect:ok|no>
  local label="$1" active="$2" present="$3" expect="$4"
  setup_companion_sandbox "$active" "$present"
  local r
  if PATH="$CFAKEBIN:$PATH" cutover_companion_rejoined "pantheon-acp-bridge@specialist.service" "acp-bridge-specialist"; then r=ok; else r=no; fi
  check "companion-rejoined: ${label}" "$r" "$expect"
  teardown_companion_sandbox
}

run_companion_case "active unit + live session -> rejoined" 1 1 ok
run_companion_case "active unit, session not yet up -> not rejoined (the fire#3 race)" 1 0 no
run_companion_case "inactive unit, session somehow up -> not rejoined" 0 1 no
run_companion_case "inactive unit + no session -> not rejoined" 0 0 no

# =============================================================================
# Tier (a3): T-2642 rollback_scope_clean predicate. Hermetic -- fake tmux on
# PATH plus a synthetic /proc/<pid>/cgroup fixture, no real systemd/tmux
# touched. This is persona_rollback.sh's verify signal that the restarted
# bespoke seat is NOT still coupled to the leftover control-plane scope
# (T-2642 fire#3 dual-supervisor defect).
# =============================================================================

setup_rollback_scope_sandbox() { # <server-pid-or-empty> <cgroup-line-or-empty>
  local server_pid="$1" cgroup_line="$2"
  RSBX="$(mktemp -d -t t2642_rollback_scope_test.XXXXXX)"
  RFAKEBIN="$RSBX/bin"; mkdir -p "$RFAKEBIN"
  RPROC="$RSBX/proc"; mkdir -p "$RPROC"
  cat > "$RFAKEBIN/tmux" <<EOF
#!/bin/bash
if [[ "\${1:-}" == -N && "\${2:-}" == list-sessions ]]; then
  [[ -n "$server_pid" ]] && echo "$server_pid"
  exit 0
fi
exit 0
EOF
  chmod +x "$RFAKEBIN/tmux"
  if [[ -n "$server_pid" && -n "$cgroup_line" ]]; then
    mkdir -p "$RPROC/$server_pid"
    echo "$cgroup_line" > "$RPROC/$server_pid/cgroup"
  fi
}
teardown_rollback_scope_sandbox() { rm -rf "$RSBX"; }

run_rollback_scope_case() { # <label> <server-pid> <cgroup-line> <expect:ok|no>
  local label="$1" server_pid="$2" cgroup_line="$3" expect="$4"
  setup_rollback_scope_sandbox "$server_pid" "$cgroup_line"
  local r
  if PATH="$RFAKEBIN:$PATH" rollback_scope_clean "pantheon-runtime-tmux.scope" "$RPROC"; then r=ok; else r=no; fi
  check "rollback_scope_clean: ${label}" "$r" "$expect"
  teardown_rollback_scope_sandbox
}

run_rollback_scope_case "no tmux server at all -> trivially clean" "" "" ok
run_rollback_scope_case "server present, cgroup has NO scope -> clean" "555" "0::/user.slice/user-1002.slice/user@1002.service/some-other.scope" ok
run_rollback_scope_case "server present, cgroup HAS scope -> NOT clean (T-2642 defect reproduced)" "555" "0::/user.slice/user-1002.slice/user@1002.service/app.slice/pantheon-runtime-tmux.scope" no

# =============================================================================
# Tier (a4): T-2658 runtime_scope_owned_and_session_live predicate. Hermetic --
# fake tmux on PATH plus a synthetic /proc/<pid>/cgroup fixture, no real
# systemd/tmux touched. This is handoff_and_restart.sh's readiness gate for a
# control-plane-supervised persona's restart (the positive counterpart of
# rollback_scope_clean above).
# =============================================================================

setup_runtime_scope_sandbox() { # <session-live:0|1> <server-pid-or-empty> <cgroup-line-or-empty>
  local session_live="$1" server_pid="$2" cgroup_line="$3"
  USBX="$(mktemp -d -t t2658_runtime_scope_test.XXXXXX)"
  UFAKEBIN="$USBX/bin"; mkdir -p "$UFAKEBIN"
  UPROC="$USBX/proc"; mkdir -p "$UPROC"
  cat > "$UFAKEBIN/tmux" <<EOF
#!/bin/bash
if [[ "\${1:-}" == has-session ]]; then
  [[ "$session_live" == 1 ]] && exit 0 || exit 1
fi
if [[ "\${1:-}" == -N && "\${2:-}" == list-sessions ]]; then
  [[ -n "$server_pid" ]] && echo "$server_pid"
  exit 0
fi
exit 0
EOF
  chmod +x "$UFAKEBIN/tmux"
  if [[ -n "$server_pid" && -n "$cgroup_line" ]]; then
    mkdir -p "$UPROC/$server_pid"
    echo "$cgroup_line" > "$UPROC/$server_pid/cgroup"
  fi
}
teardown_runtime_scope_sandbox() { rm -rf "$USBX"; }

run_runtime_scope_case() { # <label> <session-live> <server-pid> <cgroup-line> <expect:ok|no>
  local label="$1" session_live="$2" server_pid="$3" cgroup_line="$4" expect="$5"
  setup_runtime_scope_sandbox "$session_live" "$server_pid" "$cgroup_line"
  local r
  if PATH="$UFAKEBIN:$PATH" runtime_scope_owned_and_session_live \
      "claude-research-bot" "pantheon-runtime-tmux.scope" "$UPROC"; then r=ok; else r=no; fi
  check "runtime_scope_owned_and_session_live: ${label}" "$r" "$expect"
  teardown_runtime_scope_sandbox
}

run_runtime_scope_case "session dead -> not live (kill-session pending consume restart)" 0 555 "0::/user.slice/user-1002.slice/user@1002.service/app.slice/pantheon-runtime-tmux.scope" no
run_runtime_scope_case "session live, no tmux server evidence -> not live" 1 "" "" no
run_runtime_scope_case "session live, server present, cgroup has NO scope -> not scope-owned" 1 555 "0::/user.slice/user-1002.slice/user@1002.service/some-other.scope" no
run_runtime_scope_case "session live + scope-owned -> live (the happy-path restart witness)" 1 555 "0::/user.slice/user-1002.slice/user@1002.service/app.slice/pantheon-runtime-tmux.scope" ok

# =============================================================================
# Tier (b): persona_cutover.sh's own arg-parsing / uid-guard / manifest-
# precondition wiring, parametrized across both live personas.
# =============================================================================

FAKEBIN2="$(mktemp -d -t t2633_fakebin.XXXXXX)"
cat > "$FAKEBIN2/id" <<'EOF'
#!/bin/bash
if [[ "${1:-}" == "-u" ]]; then echo "${FAKE_UID:?FAKE_UID not set}"; exit 0; fi
exec /usr/bin/id "$@"
EOF
chmod +x "$FAKEBIN2/id"

write_stale_manifest() { # <out-path> <role> <persona> <account>
  python3 - "$1" "$2" "$3" "$4" <<'PYEOF'
import sys
import yaml

out, role, persona, account = sys.argv[1:5]
manifest = {
    "manifest_version": 2,
    "harness_runtime": {
        "schema": "pantheon-harness-runtime/v1",
        "controller": {
            "tmux": {"session": "pantheon-{persona}", "window": "harness", "pane": "0"},
            "quota": {"weekly_remaining_percent": 5},
        },
        "harnesses": {
            "claude": {
                "default_profile": "interactive-subscription",
                "profiles": {
                    "interactive-subscription": {
                        "executable": "{account_home}/.npm-global/bin/claude",
                        "mode": "interactive",
                        "meter": "subscription",
                        "quota_source": "statusline",
                    }
                },
            }
        },
        # Deliberately STALE/pre-edit shape: no model, no channels, no tmux
        # override, no post_start -- the exact "loads cleanly but isn't
        # synced" gap T-2635/closure#2 exists to catch.
        "roles": {
            role: {
                "claude": {
                    "profile": "interactive-subscription",
                    "model": None,
                    "native": {"permission_mode": "default"},
                }
            }
        },
    },
    "roles": {role: {"persona": persona, "account": account, "harness": "claude-code"}},
}
with open(out, "w", encoding="utf-8") as f:
    yaml.safe_dump(manifest, f)
PYEOF
}

STALE_DIR="$(mktemp -d -t t2633_stale_manifest.XXXXXX)"
write_stale_manifest "$STALE_DIR/recovery-stale.yaml" recovery recovery claude-fallback
write_stale_manifest "$STALE_DIR/specialist-stale.yaml" specialist specialist claude-research

run_cutover_case() { # <persona> <account> <uid> <supervise-basename>
  local persona="$1" account="$2" uid="$3" supervise_basename="$4"
  local unit_file
  unit_file="$(mktemp -t t2633_unit.XXXXXX)"
  printf '[Service]\nExecStart=/home/%s/.local/bin/%s\n' "$account" "$supervise_basename" > "$unit_file"

  # 1. no --persona at all -> dies (via die(), rc 1) before touching python.
  local out rc
  out="$(bash "$CUTOVER" --stamp t1 2>&1)"; rc=$?
  check "cutover(${persona}): no --persona dies with rc 1" "$rc" "1"
  check_contains "cutover(${persona}): no --persona message" "$out" "no --persona"

  # 2. no --stamp -> dies before touching python.
  out="$(bash "$CUTOVER" --persona "$persona" 2>&1)"; rc=$?
  check "cutover(${persona}): no --stamp dies with rc 1" "$rc" "1"
  check_contains "cutover(${persona}): no --stamp message" "$out" "no --stamp"

  # 3. unknown persona -> resolver's ValueError surfaces as a clean abort,
  #    no traceback, real (no stub needed).
  out="$(bash "$CUTOVER" --persona nonexistent-persona-xyz --stamp t1 --manifest "$INSTANCE_YAML" 2>&1)"; rc=$?
  check "cutover(${persona}): unknown persona dies with rc 1" "$rc" "1"
  check_contains "cutover(${persona}): unknown persona message" "$out" "persona resolution / manifest precondition failed"

  # 4. STALE target manifest -> manifest precondition FAILS (closure #2: a
  #    clean load_binding() load is NOT the same as "synced").
  local stale_manifest="$STALE_DIR/${persona}-stale.yaml"
  out="$(bash "$CUTOVER" --persona "$persona" --stamp t1 --manifest "$stale_manifest" --unit-file "$unit_file" 2>&1)"; rc=$?
  check "cutover(${persona}): stale target manifest dies with rc 1" "$rc" "1"
  check_contains "cutover(${persona}): stale manifest message" "$out" "not synced for persona"

  # 5. SYNCED manifest (this repo's own, real) + real (non-stubbed) uid ->
  #    manifest precondition PASSES, then the REAL uid-guard rejects (we are
  #    genuinely not $uid on this box).
  out="$(bash "$CUTOVER" --persona "$persona" --stamp t1 --manifest "$INSTANCE_YAML" --unit-file "$unit_file" 2>&1)"; rc=$?
  check "cutover(${persona}): synced manifest, real (wrong) uid dies with rc 1" "$rc" "1"
  check_contains "cutover(${persona}): manifest precondition OK printed" "$out" "manifest precondition OK"
  check_contains "cutover(${persona}): uid-guard rejects real caller" "$out" "must run as ${account} (uid ${uid})"

  # 6. SYNCED manifest + STUBBED uid (id -u -> the target's own uid) -> the
  #    uid-guard now PASSES, XRD /run/user/<uid> exists on this box for real
  #    (verified live), reaches the install-done precondition -- which fails
  #    deterministically from this seat (PCX_APPLY_PATH is a hardcoded
  #    per-account absolute path we have no access to). This is the approved
  #    scope boundary: we do not chase further.
  out="$(FAKE_UID="$uid" PATH="$FAKEBIN2:$PATH" bash "$CUTOVER" --persona "$persona" --stamp t1 --manifest "$INSTANCE_YAML" --unit-file "$unit_file" 2>&1)"; rc=$?
  check "cutover(${persona}): stubbed uid passes guard, dies rc 1" "$rc" "1"
  if user_bus_present "$uid"; then
    # Live user bus (VPS / lingering persona): reaches the install-done precondition.
    check_contains "cutover(${persona}): install-done precondition reached" "$out" "install-done precondition failed"
  else
    # No /run/user/<uid> (ORG-066 CT202 Rung-1): the script correctly aborts at the user-bus
    # precondition. Assert that, and skip the install-done checkpoint (needs a bus we must not
    # provide — T-2731).
    check_contains "cutover(${persona}): aborts at absent user bus /run/user/${uid}" "$out" "XDG_RUNTIME_DIR /run/user/${uid} missing"
    skip "cutover(${persona}): install-done precondition (no user bus /run/user/${uid} — needs linger, T-2731)"
  fi

  rm -f "$unit_file"
}

run_cutover_case "recovery" "claude-fallback" "1001" "recovery-bot-supervise.sh"
run_cutover_case "specialist" "claude-research" "1002" "specialist-bot-supervise.sh"

# --- persona_rollback.sh: no-manifest-dependency + missing-backup guard ----

run_rollback_case() { # <persona> <account> <uid> <supervise-basename>
  local persona="$1" account="$2" uid="$3" supervise_basename="$4"
  local unit_file
  unit_file="$(mktemp -t t2633_unit.XXXXXX)"
  printf '[Service]\nExecStart=/home/%s/.local/bin/%s\n' "$account" "$supervise_basename" > "$unit_file"

  local out rc
  out="$(bash "$ROLLBACK" --stamp t1 2>&1)"; rc=$?
  check "rollback(${persona}): no --persona dies with rc 1" "$rc" "1"

  out="$(bash "$ROLLBACK" --persona "$persona" 2>&1)"; rc=$?
  check "rollback(${persona}): no --stamp dies with rc 1" "$rc" "1"

  # Stubbed uid passes the guard; missing backups (this sandbox never ran a
  # real cutover) must abort BEFORE touching any live file -- the
  # never-a-partial-restore precondition, generalized unchanged from
  # t2590_recovery_rollback.sh.
  out="$(FAKE_UID="$uid" PATH="$FAKEBIN2:$PATH" bash "$ROLLBACK" --persona "$persona" --stamp definitely-no-such-stamp --unit-file "$unit_file" 2>&1)"; rc=$?
  check "rollback(${persona}): missing backups dies with rc 1" "$rc" "1"
  if user_bus_present "$uid"; then
    check_contains "rollback(${persona}): missing-backup message" "$out" "missing backup"
  else
    # No /run/user/<uid> (ORG-066 CT202 Rung-1): aborts at the user-bus precondition before the
    # missing-backup guard. Assert that; skip the missing-backup checkpoint (needs a bus — T-2731).
    check_contains "rollback(${persona}): aborts at absent user bus /run/user/${uid}" "$out" "XDG_RUNTIME_DIR /run/user/${uid} missing"
    skip "rollback(${persona}): missing-backup checkpoint (no user bus /run/user/${uid} — needs linger, T-2731)"
  fi

  # --manifest is accepted but unused (no manifest dependency) -- must still
  # reach the SAME missing-backup abort, not error out on the flag itself.
  out="$(FAKE_UID="$uid" PATH="$FAKEBIN2:$PATH" bash "$ROLLBACK" --persona "$persona" --stamp definitely-no-such-stamp --manifest "$STALE_DIR/${persona}-stale.yaml" --unit-file "$unit_file" 2>&1)"; rc=$?
  check "rollback(${persona}): --manifest accepted, still reaches missing-backup" "$rc" "1"
  check_contains "rollback(${persona}): --manifest ignored-note printed" "$out" "ignoring"

  rm -f "$unit_file"
}

run_rollback_case "recovery" "claude-fallback" "1001" "recovery-bot-supervise.sh"
run_rollback_case "specialist" "claude-research" "1002" "specialist-bot-supervise.sh"

rm -rf "$FAKEBIN2" "$STALE_DIR"

# --- T-2642: rollback's DRY-RUN echo block must stay in sync with EXECUTE ---
# (persona_rollback.sh's own header comment calls this out at the DRY-RUN
# block). Cheap structural grep, not a subprocess run -- neither branch is
# reachable via subprocess from this seat (see the missing-backup scope-
# boundary note in run_rollback_case above), so this is the regression guard
# available at this seat: both new teardown steps must appear in the DRY-RUN
# text.
DRYRUN_BLOCK="$(sed -n '/DRY-RUN — would then run/,/Dry-run OK/p' "$ROLLBACK")"
check_contains "rollback DRY-RUN block mentions control-plane scope teardown" "$DRYRUN_BLOCK" "RUNTIME_SCOPE_UNIT"
check_contains "rollback DRY-RUN block mentions tmux kill-session guard" "$DRYRUN_BLOCK" "kill-session"

echo "----------------------------------------"
echo "$pass passed, $fail failed, $skipped skipped"
[[ "$fail" -eq 0 ]]
