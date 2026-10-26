#!/usr/bin/env bash
# Smoke tests for tools/verify_startup.sh (T-0728).
#
# Strategy mirrors test_handoff_and_restart.sh: per-case sandbox with a fake
# `tmux` (capture-pane reads $SBX/pane_text; switches to $SBX/pane_text_after
# once the boot-dialog dismiss `-l 1` is sent; send-keys recorded), fake `curl`.
# All cases run --agent recovery as the current user → self-mode (the SessionStart
# case), so no sudo path is exercised here (cross-account is covered by review).
#
# Self-reports "N passed, M failed"; exits non-zero on any failure.

set -uo pipefail
SUT="$(cd "$(dirname "$0")" && pwd)/verify_startup.sh"
PASS=0; FAIL=0; FAILED_TESTS=()

setup_sandbox() {
  SBX=$(mktemp -d -t verify_startup_test.XXXXXX)
  FAKEBIN="$SBX/bin"; mkdir -p "$FAKEBIN"
  PROC_FIXTURE="$SBX/proc"; mkdir -p "$PROC_FIXTURE"
  : > "$SBX/pane_text"
  : > "$SBX/sendkeys_args"
  echo 100 > "$SBX/pane_pid"

  cat > "$FAKEBIN/tmux" <<EOF
#!/bin/bash
case "\${1:-}" in
  send-keys)
    echo "\$*" >> "$SBX/sendkeys_args"
    # T-2210: model the dialog clearing for ANY known option. It used to key on
    # "-l 1" alone, so the renderer dialog (dismissed with 2) never appeared to
    # clear and the SUT was blamed for a limitation of this fake.
    if [[ "\$*" == *"-l 1" || "\$*" == *"-l 2" ]]; then echo 1 > "$SBX/boot_dismissed"; fi
    exit 0 ;;
  capture-pane)
    if [[ -f "$SBX/capture_fails" ]]; then exit 1; fi
    if [[ -f "$SBX/pane_text_after" && -f "$SBX/boot_dismissed" ]]; then
      cat "$SBX/pane_text_after"
    else
      cat "$SBX/pane_text"
    fi
    exit 0 ;;
  display-message)
    [[ -f "$SBX/pane_pid" ]] || exit 1
    cat "$SBX/pane_pid"
    exit 0 ;;
  *) exit 0 ;;
esac
EOF
  chmod +x "$FAKEBIN/tmux"

  cat > "$FAKEBIN/curl" <<EOF
#!/bin/bash
[[ -f "$SBX/curl_response" ]] && cat "$SBX/curl_response"
exit 0
EOF
  chmod +x "$FAKEBIN/curl"

  # Fake git: services only `config --get core.hooksPath` for githooks_check
  # (C7). Returns $SBX/hookspath_value if set, else .githooks (the healthy
  # default so unrelated cases see an active note). Everything else exits 0.
  cat > "$FAKEBIN/git" <<EOF
#!/bin/bash
if [[ "\$*" == *"config --get core.hooksPath"* ]]; then
  if [[ -f "$SBX/hookspath_value" ]]; then cat "$SBX/hookspath_value"; else echo ".githooks"; fi
  exit 0
fi
exit 0
EOF
  chmod +x "$FAKEBIN/git"

  # Fake systemctl: services timers_check (C8). list-units emits $SBX/timers_list
  # (empty by default → no timers → unrelated cases see the healthy note); show
  # emits the requested property from \$SBX/timerstate_<unit>. Everything else
  # (e.g. service_check, skipped in self-mode) exits 0.
  cat > "$FAKEBIN/systemctl" <<EOF
#!/bin/bash
if [[ "\$*" == *"list-units"* ]]; then
  [[ -f "$SBX/timers_list" ]] && cat "$SBX/timers_list"
  exit 0
fi
if [[ "\$*" == *"show"* ]]; then
  unit=""; prop=""; prev=""
  for a in "\$@"; do
    [[ "\$a" == *.timer ]] && unit="\$a"
    [[ "\$prev" == "-p" ]] && prop="\$a"
    prev="\$a"
  done
  sf="$SBX/timerstate_\$unit"
  if [[ -n "\$prop" && -f "\$sf" ]] && grep -q "^\$prop=" "\$sf"; then
    grep "^\$prop=" "\$sf"
  else
    echo "\$prop="
  fi
  exit 0
fi
exit 0
EOF
  chmod +x "$FAKEBIN/systemctl"

  # T-2345 post-close follow-up: hermetic live-process-tree fixture. The probe
  # anchors at tmux pane_pid=100, walks PPIDs 100 -> 200 -> 300, and accepts the
  # two roles only when their absolute Bun exe plus argv/cwd shape match.
  # An unrelated, otherwise-valid pair at 900 -> 901 is deliberately outside
  # the pane tree: a global account-wide search would false-green on it.
  local home=/home/claude-fallback
  local plugin="$home/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6"
  for pid in 100 200 300 900 901; do mkdir -p "$PROC_FIXTURE/$pid"; : > "$PROC_FIXTURE/$pid/status"; : > "$PROC_FIXTURE/$pid/children"; done
  echo 200 > "$PROC_FIXTURE/100/children"
  echo 300 > "$PROC_FIXTURE/200/children"
  echo 901 > "$PROC_FIXTURE/900/children"
  for pid in 200 300 900 901; do
    ln -s "$home/.bun/bin/bun" "$PROC_FIXTURE/$pid/exe"
    ln -s "$plugin" "$PROC_FIXTURE/$pid/cwd"
  done
  printf '%s\0' "$home/.bun/bin/bun" run --cwd "$plugin" --shell=bun --silent start > "$PROC_FIXTURE/200/cmdline"
  printf '%s\0' "$home/.bun/bin/bun" server.ts > "$PROC_FIXTURE/300/cmdline"
  printf '%s\0' "$home/.bun/bin/bun" run --cwd "$plugin" --shell=bun --silent start > "$PROC_FIXTURE/900/cmdline"
  printf '%s\0' "$home/.bun/bin/bun" server.ts > "$PROC_FIXTURE/901/cmdline"

  cat > "$FAKEBIN/pgrep" <<EOF
#!/bin/bash
# Emulate only the sound parent-PID query used by the SUT. The children file is a
# synthetic PPID index; no command-line matching exists in this fixture.
[[ "\${1:-}" == -P && "\${2:-}" =~ ^[0-9]+\$ ]] || exit 2
f="$PROC_FIXTURE/\$2/children"
[[ -f "\$f" ]] || exit 1
tr ' ' '\n' < "\$f" | sed '/^\$/d'
[[ -s "\$f" ]]
EOF
  chmod +x "$FAKEBIN/pgrep"

  export PATH="$FAKEBIN:$PATH"
  export PANTHEON_VERIFY_PROC_ROOT="$PROC_FIXTURE"
  # T-4392 2a: hermetic break-glass base (absent by default -> C10 silent).
  export PANTHEON_BREAKGLASS_LIST_BASE="$SBX/breakglass"
}
teardown_sandbox() { rm -rf "$SBX"; unset PANTHEON_AGENT_ROLE_MAP PANTHEON_BREAKGLASS_LIST_BASE; }
# T-4159: verify_startup resolves restart-bound seats through agent_role_map.py.
# Every case gets a FAKE resolver so the suite never reads the live instance.yaml
# (which flips recovery/orchestrator to control-plane at their T-3650 commit B). Default:
# unresolvable (exit 2) -> the static AGENT_SERVICE map, i.e. pre-T-4159 shape.
fake_role_map() {  # $1 = none | control-plane
  local f="$SBX/fake_role_map.py"
  case "$1" in
    none) printf '%s\n' 'import sys; sys.exit(2)' > "$f" ;;
    control-plane) printf '%s\n' 'print("SUPERVISOR=control-plane\nSERVICE=pantheon-persona-consume@recovery.service\nMODE=user\nPANE=claude-telegram-bot-fallback:0\nSESSION_UNIT=pantheon-runtime-controller@recovery.service")' > "$f" ;;
  esac
  export PANTHEON_AGENT_ROLE_MAP="$f"
}

run_test() {
  local name="$1" expected="$2"; shift 2
  setup_sandbox
  fake_role_map none
  declare -f "preflight_${name}" >/dev/null && "preflight_${name}"
  local actual=0
  # ⚠️ T-2322: --self PINS THE MODE, and it is load-bearing rather than tidy.
  # verify_startup decides self-vs-cross-account from
  #     [[ "$AGENT_UNIX_USER" == "$CURRENT_USER" ]]
  # so a case that says only `--agent recovery` silently exercises the SELF path on
  # recovery's account and the CROSS-ACCOUNT path on every other seat — and there
  # capture_pane shells out through `sudo -n -u <agent>`, which the sandbox's
  # fake tmux on PATH cannot intercept. The capture then fails, the pane verdict
  # becomes "uncheckable", and the three cases that assert the hard-fail (exit 7)
  # get exit 0 instead. That is why this suite read 16/16 as recovery and 9/7 as
  # mason AND orchestrator, deterministically, for 39 days: not a regression in the SUT
  # and not ordinary rot, but a test that never declared the mode it meant.
  # Every case below fixtures a pane into $SBX and therefore means the self path.
  # Say so, rather than inheriting it from whoever happens to run the suite.
  "$SUT" --self "$@" >"$SBX/stdout" 2>"$SBX/stderr" || actual=$?
  local ok=1
  [[ "$actual" -eq "$expected" ]] || ok=0
  if [[ "$ok" == 1 ]] && declare -f "postcheck_${name}" >/dev/null; then
    "postcheck_${name}" || ok=0
  fi
  if [[ "$ok" == 1 ]]; then
    echo "  PASS: $name (exit=$actual)"; PASS=$((PASS+1))
  else
    echo "  FAIL: $name (expected=$expected got=$actual)"
    echo "  --- stdout ---"; cat "$SBX/stdout"; echo "  --- stderr ---"; cat "$SBX/stderr"
    FAIL=$((FAIL+1)); FAILED_TESTS+=("$name")
  fi
  teardown_sandbox
}

IDLE_PANE='╭─────────────────────────────────────╮
│ ❯ Try "what is the weather"         │
╰─────────────────────────────────────╯
  ? for shortcuts'

BOOT_DIALOG='  WARNING: Loading development channels
────────────────────────────────────────────────────────────────────────────────
  ❯ 1. I am using this for local development
    2. Exit
────────────────────────────────────────────────────────────────────────────────
  Enter to confirm · Esc to cancel'

# T-2210: the SECOND boot modal (Claude Code v2.1.220). Note the options — 1 is
# "Yes, try it", i.e. accepting a renderer change on a peer's live session.
RENDERER_DIALOG='  Try the new fullscreen renderer?
────────────────────────────────────────────────────────────────────────────────
  ❯ 1. Yes, try it
    2. Not now
────────────────────────────────────────────────────────────────────────────────
  Enter to confirm · Esc to cancel'

DESTRUCTIVE_DIALOG='  Bash command
    rm -rf /tmp/x
────────────────────────────────────────────────────────────────────────────────
  ❯ 1. Yes
    2. No
────────────────────────────────────────────────────────────────────────────────
  Do you want to proceed?
  Esc to cancel · Tab to amend'

# --- cases ---
preflight_healthy_self() { printf '%s' "$IDLE_PANE" > "$SBX/pane_text"; }
# T-2322 GUARD: assert the SUT actually ran in self mode. Without this the pin
# above can be dropped and the suite goes back to passing on recovery and only on
# recovery — the failure would once again look like a SUT regression to whoever runs
# it next, which is exactly the 39 days this cost.
postcheck_healthy_self() {
  grep -q "self=1" "$SBX/stdout" || {
    echo "  (SUT ran CROSS-ACCOUNT, not self — the T-2322 host-coupling is back;"
    echo "   run_test must pass --self, or this suite only passes as recovery)"
    return 1
  }
}

preflight_pane_not_idle() { printf '%s' "$DESTRUCTIVE_DIALOG" > "$SBX/pane_text"; }
# T-4646: Claude Code 2.1.289's auto-mode startup nudge has the frame run and a ❯
# option cursor but no confirm footer, so it read as idle (rc 0). Never dismissed.
AUTO_MODE_NUDGE='────────────────────────────────────────────────────────────────────────────────
 Make auto mode your default permission mode?

 Auto mode lets Claude handle permission prompts automatically. Claude checks each
 tool call for risky actions and prompt injection before executing, runs the ones it
 assesses as lower-risk, and blocks the rest.

 ❯ Yes, set auto mode as my default permission mode
   No, keep manual'
preflight_auto_mode_nudge() { printf '%s' "$AUTO_MODE_NUDGE" > "$SBX/pane_text"; }
postcheck_auto_mode_nudge() {
  if [[ -s "$SBX/sendkeys_args" ]]; then echo "  (violation: sent keys to the auto-mode nudge)"; return 1; fi
  return 0
}
# T-4020: a manual-mode idle pane whose rotating "? for shortcuts" hint has cycled
# to another tip ("← for agents"). Realistic full render (frame + ❯ + mode footer)
# → idle (rc 0). Also exercises the mode-footer liveness sentinel end-to-end.
MANUAL_MODE_IDLE_PANE='╭─────────────────────────────────────╮
│ ❯                                   │
╰─────────────────────────────────────╯
  ⏸ manual mode on · ← for agents'
preflight_manual_mode_idle() { printf '%s' "$MANUAL_MODE_IDLE_PANE" > "$SBX/pane_text"; }
# ISOLATION case: a pane where the idle input box (frame + ❯) is NOT in the
# captured region so pane_is_idle CANNOT fire — only the mode footer distinguishes
# idle from wedged. Proves the T-4020 LIVENESS_SENTINELS mode-footer alternate is
# load-bearing: WITHOUT it both idle checks fail → rc 7; WITH it → rc 0.
MANUAL_MODE_FOOTER_ONLY_PANE='● Same steady state.
  Holding for the operator.

  ⏸ manual mode on · ← for agents'
preflight_manual_mode_footer_only() { printf '%s' "$MANUAL_MODE_FOOTER_ONLY_PANE" > "$SBX/pane_text"; }
# T-5465: observed Specialist control-plane idle composer after its Codex runtime
# returned to the prompt. The Claude-only classifier called this wedged even
# though the controller, poller, and pane process were all healthy.
CODEX_IDLE_PANE=$'• Ran git status --short
  └ (clean)

\033[36m›\033[0m Summarize recent commits
  \033[2mgpt-5.6-sol high · ~/pantheon-config · Main [default]\033[0m'
preflight_codex_control_plane_idle() {
  printf '%s' "$CODEX_IDLE_PANE" > "$SBX/pane_text"
  fake_role_map control-plane
}

# Modal counterexample: an old composer may remain in scrollback while the live
# bottom region is an approval dialog. The modal footer must veto the tempting
# prompt/footer pair rather than weakening fail-closed handling.
CODEX_MODAL_PANE='› Summarize recent commits
  gpt-5.6-sol high · ~/pantheon-config · Main [default]

  Would you like to run the following command?
  › 1. Yes, proceed
    2. No, cancel
  Press enter to confirm or esc to cancel'
preflight_codex_modal_stays_wedged() {
  printf '%s' "$CODEX_MODAL_PANE" > "$SBX/pane_text"
  fake_role_map control-plane
}
postcheck_codex_modal_stays_wedged() {
  if [[ -s "$SBX/sendkeys_args" ]]; then echo "  (violation: sent keys to a Codex modal)"; return 1; fi
  return 0
}
postcheck_pane_not_idle() {  # fail-closed: a destructive dialog must NOT be dismissed
  if grep -q -- "-l 1" "$SBX/sendkeys_args"; then echo "  (violation: pressed 1 on destructive dialog)"; return 1; fi
  return 0
}

preflight_capture_fails() { : > "$SBX/capture_fails"; }  # capture failure → SOFT → exit 0

preflight_boot_dialog_dismissed() {
  printf '%s' "$BOOT_DIALOG" > "$SBX/pane_text"
  printf '%s' "$IDLE_PANE"   > "$SBX/pane_text_after"
}
postcheck_boot_dialog_dismissed() {
  grep -q "dismissing inline" "$SBX/stdout" || { echo "  (no dismiss logged)"; return 1; }
  grep -q -- "-l 1" "$SBX/sendkeys_args" || { echo "  (no '1' sent)"; return 1; }
  return 0
}

# T-2345 post-close follow-up: the poller check must not inherit a valid pair
# elsewhere on the uid.
# Break the IN-PANE runner while leaving the valid 900 -> 901 pair intact. A
# global pgrep would report PRESENT; the pane-rooted tree must report ABSENT and
# self-mode must refuse to cycle itself.
preflight_poller_absent_ignores_unrelated_pair() {
  printf '%s' "$BOOT_DIALOG" > "$SBX/pane_text"
  printf '%s' "$IDLE_PANE"   > "$SBX/pane_text_after"
  printf '%s\0' /home/claude-fallback/.bun/bin/bun not-the-runner > "$PROC_FIXTURE/200/cmdline"
}
postcheck_poller_absent_ignores_unrelated_pair() {
  grep -q "poller child is ABSENT" "$SBX/stdout" || { echo "  (missing ABSENT verdict)"; return 1; }
  grep -q "escalation UNAVAILABLE in self-mode" "$SBX/stdout" || { echo "  (self-cycle refusal missing)"; return 1; }
}

# If tmux cannot supply a live pane pid, that is UNKNOWN, never ABSENT and never
# a claimed healthy agent. The check remains soft/uncheckable per T-2307.
preflight_poller_tree_unknown() {
  printf '%s' "$BOOT_DIALOG" > "$SBX/pane_text"
  printf '%s' "$IDLE_PANE"   > "$SBX/pane_text_after"
  rm -f "$SBX/pane_pid"
}
postcheck_poller_tree_unknown() {
  grep -q "poller child could not be determined" "$SBX/stdout" || { echo "  (missing UNKNOWN verdict)"; return 1; }
  grep -q "pane=uncheckable" "$SBX/stdout" || { echo "  (UNKNOWN was not uncheckable)"; return 1; }
}

preflight_denylist_blocks() {  # boot sentinel in scrollback + destructive focused
  printf '%s\n%s' "  (scrollback) I am using this for local development" "$DESTRUCTIVE_DIALOG" > "$SBX/pane_text"
}
postcheck_denylist_blocks() {
  if grep -q -- "-l 1" "$SBX/sendkeys_args"; then echo "  (violation: dismissed despite denylist)"; return 1; fi
  return 0
}

preflight_no_dewedge() { printf '%s' "$BOOT_DIALOG" > "$SBX/pane_text"; printf '%s' "$IDLE_PANE" > "$SBX/pane_text_after"; }
postcheck_no_dewedge() {
  if grep -q -- "-l 1" "$SBX/sendkeys_args"; then echo "  (violation: dismissed with --no-dewedge)"; return 1; fi
  return 0
}

preflight_liveness_downgrade() {  # dialog footer in scrollback (ABOVE bottom-6) but bottom shows live input
  printf '%s\n' \
    "  Enter to confirm · Esc to cancel  (captured peer dialog, scrolled up)" \
    "  ● work output line 1" "  ● work output line 2" "  ● work output line 3" \
    "$IDLE_PANE" > "$SBX/pane_text"
}

# C7 (T-1091): hooksPath parity-guard. Pane idle in both → exit stays 0 (SOFT);
# only the note/warn text differs by the faked core.hooksPath value.
preflight_githooks_active()   { printf '%s' "$IDLE_PANE" > "$SBX/pane_text"; echo ".githooks"  > "$SBX/hookspath_value"; }
postcheck_githooks_active()   { grep -q "git hooks active"     "$SBX/stdout" || { echo "  (no active note)";    return 1; }; }
preflight_githooks_reverted() { printf '%s' "$IDLE_PANE" > "$SBX/pane_text"; echo ".git/hooks" > "$SBX/hookspath_value"; }
postcheck_githooks_reverted() { grep -q "git hooks NOT active" "$SBX/stdout" || { echo "  (no revert warning)"; return 1; }; }

# C4 token-grep regression (tidy-skim 2026-06-11): the plugin .env key is
# TELEGRAM_BOT_TOKEN; the old '^BOT_TOKEN=' anchor never matched it, so the
# getMe probe was silently dead fleet-wide. With the real Recovery .env present
# (self-mode reads it) + a faked ok:true getMe, there must be NO "token not
# readable" warning — i.e. the token IS now found. Skips cleanly if the .env
# is absent in this environment (the warning is then legitimate).
preflight_telegram_token_found() { printf '%s' "$IDLE_PANE" > "$SBX/pane_text"; printf '{"ok": true}' > "$SBX/curl_response"; }
postcheck_telegram_token_found() {
  [[ -r "/home/claude-fallback/.claude/channels/telegram/.env" ]] || { echo "  (skip: no .env here)"; return 0; }
  if grep -q "token not readable" "$SBX/stdout"; then echo "  (token still not found — grep pattern regressed)"; return 1; fi
  return 0
}

# C8 (T-1097): timer-stall watchdog. Pane idle → exit stays 0 (SOFT) in BOTH;
# only the note/warn differs by the faked timer state. A healthy monotonic timer
# (finite NextElapseUSecMonotonic) must NOT flag; a stalled one (monotonic=
# infinity + realtime empty + never triggered) MUST flag. Guards against the
# false-positive traps (calendar monotonic=0, the --value=infinity quirk).
_TIMER=pantheon-inbox-consumer@recovery.timer
preflight_timer_healthy() {
  printf '%s' "$IDLE_PANE" > "$SBX/pane_text"
  echo "$_TIMER" > "$SBX/timers_list"
  printf 'NextElapseUSecMonotonic=1d 7h 13min\nNextElapseUSecRealtime=\nLastTriggerUSec=Thu 2026-06-11 05:56:52 UTC\n' > "$SBX/timerstate_$_TIMER"
}
postcheck_timer_healthy() { grep -q "no stalled timers" "$SBX/stdout" || { echo "  (healthy timer not recognized)"; return 1; }; }
preflight_timer_stalled() {
  printf '%s' "$IDLE_PANE" > "$SBX/pane_text"
  echo "$_TIMER" > "$SBX/timers_list"
  printf 'NextElapseUSecMonotonic=infinity\nNextElapseUSecRealtime=\nLastTriggerUSec=\n' > "$SBX/timerstate_$_TIMER"
}
postcheck_timer_stalled() { grep -q "stalled timer(s)" "$SBX/stdout" || { echo "  (stalled timer not flagged)"; return 1; }; }

# --at-boot (T-1099): SessionStart-hook mode MUST exit 0 even when the pane would
# otherwise wedge (rc=7) — the pane is unsettled mid-boot, so C1-C3 is skipped.
# Feed a DESTRUCTIVE_DIALOG (normally → wedged/rc=7) and assert --at-boot still
# exits 0 + logs the pane-skip (proves it can never brick a boot).
preflight_at_boot_never_wedges() { printf '%s' "$DESTRUCTIVE_DIALOG" > "$SBX/pane_text"; }
postcheck_at_boot_never_wedges() {
  grep -q "pane check skipped" "$SBX/stdout" || { echo "  (pane not skipped at boot)"; return 1; }
  if grep -q -- "-l 1" "$SBX/sendkeys_args"; then echo "  (violation: sent keys at boot — no-self-inject breached)"; return 1; fi
  if grep -q "BREAK-GLASS" "$SBX/stdout"; then echo "  (C10 spoke with no flag present)"; return 1; fi
  return 0
}

# C10 (T-4392 2a): a break-glass flag for the agent's account is a SOFT warning at boot. The
# sandbox base is owned by the test user, so the REAL ownership check rejects it; the line
# still surfaces (hook, owner, reason). LIVE formatting: tools/test_failclosed.py.
preflight_at_boot_breakglass_flag() {
  printf '%s' "$IDLE_PANE" > "$SBX/pane_text"
  mkdir -m 755 -p "$SBX/breakglass/claude-fallback"; chmod 755 "$SBX/breakglass"
  printf 'v1 %s mcp-list-guard\n' "$(( $(date +%s) + 3600 ))" > "$SBX/breakglass/claude-fallback/flag"
  chmod 644 "$SBX/breakglass/claude-fallback/flag"
}
postcheck_at_boot_breakglass_flag() {
  local want="WARN: BREAK-GLASS flag for claude-fallback: REJECTED claude-fallback: base is not root-owned"
  [[ "$(id -u)" -eq 0 ]] && want="WARN: BREAK-GLASS flag for claude-fallback: REJECTED claude-fallback: "  # root owns the fixture
  grep -qF "$want" "$SBX/stdout" \
    || { echo "  (break-glass flag not surfaced as a SOFT warning)"; return 1; }
}

echo "=== verify_startup.sh smoke ==="
run_test healthy_self           0 --agent recovery
run_test pane_not_idle          7 --agent recovery
run_test auto_mode_nudge        7 --agent recovery   # T-4646
run_test manual_mode_idle       0 --agent recovery
run_test manual_mode_footer_only 0 --agent recovery
run_test codex_control_plane_idle 0 --agent recovery
run_test codex_modal_stays_wedged 7 --agent recovery
run_test capture_fails          0 --agent recovery
run_test boot_dialog_dismissed  0 --agent recovery
run_test poller_absent_ignores_unrelated_pair 7 --agent recovery
run_test poller_tree_unknown     0 --agent recovery
run_test denylist_blocks        7 --agent recovery
run_test no_dewedge             7 --agent recovery --no-dewedge
run_test liveness_downgrade     0 --agent recovery
run_test githooks_active        0 --agent recovery
run_test githooks_reverted      0 --agent recovery
run_test telegram_token_found   0 --agent recovery
run_test timer_healthy          0 --agent recovery
run_test timer_stalled          0 --agent recovery
run_test at_boot_never_wedges   0 --agent recovery --at-boot
run_test at_boot_breakglass_flag 0 --agent recovery --at-boot
run_test unknown_agent         64 --agent bogus


# --- T-2210 ---------------------------------------------------------------
preflight_renderer_dialog_dismissed() { printf '%s' "$RENDERER_DIALOG" > "$SBX/pane_text"; printf '%s' "$IDLE_PANE" > "$SBX/pane_text_after"; }
postcheck_renderer_dialog_dismissed() {
  # ⚠️ THE KEY MATTERS, not just that something was sent. Option 1 here is
  # "Yes, try it" — enabling a renderer change on a peer's session that nobody
  # asked for, on a fleet whose recovery tooling reads the pane.
  grep -q -- "-l 2" "$SBX/sendkeys_args" || { echo "  (expected option 2 = 'Not now')"; return 1; }
  if grep -q -- "-l 1" "$SBX/sendkeys_args"; then echo "  (sent option 1 = 'Yes, try it' — accepted a UI change on a peer)"; return 1; fi
  return 0
}
preflight_dev_dialog_still_uses_option_1() { printf '%s' "$BOOT_DIALOG" > "$SBX/pane_text"; printf '%s' "$IDLE_PANE" > "$SBX/pane_text_after"; }
postcheck_dev_dialog_still_uses_option_1() {
  grep -q -- "-l 1" "$SBX/sendkeys_args" || { echo "  (dev-channels dialog must still take option 1)"; return 1; }
}
preflight_foreign_harness_refuses() { printf '%s' "$RENDERER_DIALOG" > "$SBX/pane_text"; }
postcheck_foreign_harness_refuses() {
  # codex runs a different TUI; every sentinel in the SUT is Claude Code's.
  if grep -qE -- "-l [0-9]" "$SBX/sendkeys_args"; then echo "  (pressed a key into a foreign harness)"; return 1; fi
  grep -q "harness" "$SBX/stdout" || { echo "  (no harness-refusal note)"; return 1; }
}
preflight_no_service_skips() { printf '%s' "$IDLE_PANE" > "$SBX/pane_text"; }
postcheck_no_service_skips() {
  grep -q "no session-owning service" "$SBX/stdout" || { echo "  (expected an explicit skip note)"; return 1; }
  if grep -q "service  not active" "$SBX/stdout"; then echo "  (warned about an EMPTY unit name)"; return 1; fi
  return 0
}

run_test renderer_dialog_dismissed     0 --agent recovery
run_test dev_dialog_still_uses_option_1 0 --agent recovery
run_test foreign_harness_refuses       7 --agent codex
run_test no_service_skips              0 --agent mason
# T-4159: a control-plane seat's session unit is its runtime controller.
preflight_control_plane_session_unit() { printf '%s' "$IDLE_PANE" > "$SBX/pane_text"; fake_role_map control-plane; }
postcheck_control_plane_session_unit() {
  grep -q "session supervisor for recovery: pantheon-runtime-controller@recovery.service" "$SBX/stdout" \
    || { echo "  (expected the controller as recovery's session unit)"; return 1; }
  if grep -q "claude-telegram-fallback.service" "$SBX/stdout"; then echo "  (named the retired bespoke unit)"; return 1; fi
  return 0
}
run_test control_plane_session_unit    0 --agent recovery

# T-5299: a control-plane seat WITHOUT a Telegram plugin (artisan) gets a session unit from
# restart-target, which used to be the only "has a poller" signal. The poller probe must
# stay NOT_APPLICABLE: an ABSENT verdict there leads to poller_escalate killing a healthy
# session. Boot dialog -> dismissed -> idle is the path that reaches the probe.
preflight_control_plane_no_telegram_is_poller_na() {
  printf '%s' "$BOOT_DIALOG" > "$SBX/pane_text"
  printf '%s' "$IDLE_PANE"   > "$SBX/pane_text_after"
  fake_role_map control-plane
}
postcheck_control_plane_no_telegram_is_poller_na() {
  grep -q "no telegram poller expected for artisan" "$SBX/stdout" || { echo "  (missing NOT_APPLICABLE note)"; return 1; }
  if grep -q "poller child is ABSENT" "$SBX/stdout"; then echo "  (artisan judged comms-dark)"; return 1; fi
  return 0
}
run_test control_plane_no_telegram_is_poller_na 0 --agent artisan

echo "=== T-2210: every seat is addressable ==="
for a in recovery orchestrator specialist archivist mason artisan codex; do
  if bash "$SUT" --agent "$a" --self --no-dewedge >/dev/null 2>&1 || [ $? -ne 64 ]; then
    echo "  PASS: $a is a known agent"; PASS=$((PASS+1))
  else
    echo "  FAIL: $a not in the agent table"; FAIL=$((FAIL+1)); FAILED_TESTS+=("agent_$a")
  fi
done

# --- constants must stay byte-equal with handoff_and_restart.sh (drift guard) ---
echo "=== constant-drift guard ==="
HR="$(cd "$(dirname "$0")" && pwd)/handoff_and_restart.sh"
drift=0
for v in BOOT_DIALOG_SENTINEL BOOT_RENDERER_SENTINEL BOOT_DISMISS_DENYLIST VERIFY_PATTERN IDLE_SENTINEL DIALOG_EXCLUDE; do
  # Compare the single-quoted VALUE only (handoff_and_restart.sh has trailing
  # inline comments; verify_startup.sh doesn't — the values must match).
  a="$(sed -n "s/^$v=\('[^']*'\).*/\1/p" "$SUT" | head -1)"
  b="$(sed -n "s/^$v=\('[^']*'\).*/\1/p" "$HR" | head -1)"
  if [[ "$a" != "$b" ]]; then
    echo "  FAIL: $v drifted between verify_startup.sh and handoff_and_restart.sh"
    echo "    verify_startup: $a"; echo "    handoff_restart: $b"
    drift=1
  fi
done
if [[ "$drift" == 0 ]]; then echo "  PASS: shared constants byte-equal"; PASS=$((PASS+1)); else FAIL=$((FAIL+1)); FAILED_TESTS+=("constant_drift"); fi

# --- T-0816: the retired bridge-client path must not regress. All three agents
# self-poll their own bot (T-0767); telegram_check probes own-bot getMe only —
# no AGENT_BRIDGE_CLIENT map, no bridge /health probe. ---
echo "=== bridge-client retirement guard (T-0816) ==="
bridge_resid=0
if grep -qE 'AGENT_BRIDGE_CLIENT|HEALTH_URL|8765' "$SUT"; then
  echo "  FAIL: residual bridge-client probe (map / health-URL / :8765) in verify_startup.sh"
  bridge_resid=1
fi
if ! grep -q 'getMe' "$SUT"; then
  echo "  FAIL: telegram_check own-bot getMe plugin probe missing"
  bridge_resid=1
fi
if [[ "$bridge_resid" == 0 ]]; then echo "  PASS: bridge path retired; single plugin getMe probe for all agents"; PASS=$((PASS+1)); else FAIL=$((FAIL+1)); FAILED_TESTS+=("bridge_retirement"); fi

echo
echo "Results: $PASS passed, $FAIL failed"
(( FAIL == 0 )) || { printf 'Failed: %s\n' "${FAILED_TESTS[*]}"; exit 1; }
