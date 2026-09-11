#!/usr/bin/env bash
# test_t2590_recovery_cutover_verify.sh — T-2620/T-2622. Exercises
# tools/t2590_cutover_verify_lib.sh's three verify helpers via the SAME
# sourcing shape t2590_recovery_cutover.sh uses, so no real tmux/systemd/claude
# process is needed. Auto-discovered + run under `pytest tools/ -q` by
# test_shell_suites.py's `tools/test_*.sh` glob. Expect: "N passed, 0 failed",
# exit 0.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=t2590_cutover_verify_lib.sh
source "$SCRIPT_DIR/t2590_cutover_verify_lib.sh"

pass=0; fail=0
check() { # <desc> <actual> <expected>
  if [[ "$2" == "$3" ]]; then pass=$((pass+1)); echo "PASS: $1";
  else fail=$((fail+1)); echo "FAIL: $1 (got '$2', want '$3')"; fi
}

# --- select_cutover_claude_pid ----------------------------------------------
# T-2620 live-fire shape: the tmux launcher (lower pid, wrong comm) and the
# real claude child (higher pid, right comm) both show up as candidates —
# `head -1` used to pick the launcher; this must pick the claude pid instead.
pid="$(printf '%s\n' "3268013 tmux" "3268020 claude" | select_cutover_claude_pid)"
check "pid-select: launcher-then-claude picks claude, not head -1" "$pid" "3268020"

# Order-independence: claude line first, launcher second.
pid="$(printf '%s\n' "3268020 claude" "3268013 tmux" | select_cutover_claude_pid)"
check "pid-select: claude-then-launcher picks claude" "$pid" "3268020"

# Only the launcher present (claude not up yet, or already exited) — no match.
pid="$(printf '%s\n' "3268013 tmux" | select_cutover_claude_pid)"
rc=$?
check "pid-select: launcher-only yields empty" "$pid" ""
check "pid-select: launcher-only returns rc 1" "$rc" "1"

# Renamed tmux server comm ("tmux: server") must not false-positive-match.
pid="$(printf '%s\n' "3268013 tmux: server" "3268020 claude" | select_cutover_claude_pid)"
check "pid-select: renamed tmux-server comm excluded" "$pid" "3268020"

# --- cutover_urgent_env_confirmed -------------------------------------------
SHOW_MULTI="PANTHEON_INSTANCE_MANIFEST=/home/claude-fallback/pantheon-config/instance.yaml PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED=1"
if cutover_urgent_env_confirmed "PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED=1" "$SHOW_MULTI"; then r=ok; else r=no; fi
check "env-confirm: token present amid other vars" "$r" "ok"

SHOW_ZERO="PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED=0"
if cutover_urgent_env_confirmed "PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED=1" "$SHOW_ZERO"; then r=ok; else r=no; fi
check "env-confirm: wrong value (=0) is not a match" "$r" "no"

# Whole-token match, not substring: a var whose NAME contains the target as a
# substring must not false-positive.
SHOW_SUBSTR="NOT_PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED=1"
if cutover_urgent_env_confirmed "PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED=1" "$SHOW_SUBSTR"; then r=ok; else r=no; fi
check "env-confirm: substring var-name is not a match" "$r" "no"

if cutover_urgent_env_confirmed "PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED=1" ""; then r=ok; else r=no; fi
check "env-confirm: empty show-string is not a match" "$r" "no"

# --- cutover_telegram_poller_present ----------------------------------------
# T-2622: hermetic live-process-tree fixture, same convention as
# test_verify_startup.sh's PANTHEON_VERIFY_PROC_ROOT sandbox (fake `pgrep -P`
# walking a synthetic PPID index, fake `tmux display-message` for pane_pid).
setup_comms_sandbox() {
  SBX="$(mktemp -d -t t2590_cutover_verify_test.XXXXXX)"
  FAKEBIN="$SBX/bin"; mkdir -p "$FAKEBIN"
  PROC_FIXTURE="$SBX/proc"; mkdir -p "$PROC_FIXTURE"
  HOME_DIR="/home/claude-fallback"
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

  # T-2651: real shape is a CHAIN, not siblings -- the plugin runner (200)
  # spawns the server.ts child (300) directly, root(100) -> runner(200) ->
  # server(300). Fixtures that need a THIRD pid (e.g. the sibling-not-child
  # negative case) add it and override 100/children themselves.
  for pid in 100 200 300; do
    mkdir -p "$PROC_FIXTURE/$pid"
    : > "$PROC_FIXTURE/$pid/status"
    : > "$PROC_FIXTURE/$pid/children"
  done
  echo "200" > "$PROC_FIXTURE/100/children"
  echo "300" > "$PROC_FIXTURE/200/children"
}
teardown_comms_sandbox() { rm -rf "$SBX"; }

fixture_bun_pair() { # <runner-present:0|1> <server-present:0|1>
  local runner="$1" server="$2"
  ln -sf "$HOME_DIR/.bun/bin/bun" "$PROC_FIXTURE/200/exe"
  ln -sf "$PLUGIN" "$PROC_FIXTURE/200/cwd"
  ln -sf "$HOME_DIR/.bun/bin/bun" "$PROC_FIXTURE/300/exe"
  ln -sf "$PLUGIN" "$PROC_FIXTURE/300/cwd"
  if [[ "$runner" == 1 ]]; then
    printf '%s\0' "$HOME_DIR/.bun/bin/bun" run --cwd "$PLUGIN" --shell=bun --silent start > "$PROC_FIXTURE/200/cmdline"
  else
    printf '%s\0' "$HOME_DIR/.bun/bin/bun" not-the-runner > "$PROC_FIXTURE/200/cmdline"
  fi
  if [[ "$server" == 1 ]]; then
    printf '%s\0' "$HOME_DIR/.bun/bin/bun" server.ts > "$PROC_FIXTURE/300/cmdline"
  else
    printf '%s\0' "$HOME_DIR/.bun/bin/bun" not-the-server > "$PROC_FIXTURE/300/cmdline"
  fi
}

# T-2624: the absolute-bun PATH fix makes the poller a SINGLE process
# `bun run --cwd <plugin> --silent server.ts` (no separate start-runner + child).
# The old two-process fixture cannot express it; this puts that one process at
# pid 200 and an unrelated bun at 300, so ONLY the direct process can satisfy
# the check — proving the single-process shape is recognized.
fixture_bun_direct() {
  ln -sf "$HOME_DIR/.bun/bin/bun" "$PROC_FIXTURE/200/exe"
  ln -sf "$PLUGIN" "$PROC_FIXTURE/200/cwd"
  printf '%s\0' "$HOME_DIR/.bun/bin/bun" run --cwd "$PLUGIN" --silent server.ts > "$PROC_FIXTURE/200/cmdline"
  ln -sf "$HOME_DIR/.bun/bin/bun" "$PROC_FIXTURE/300/exe"
  ln -sf "$PLUGIN" "$PROC_FIXTURE/300/cwd"
  printf '%s\0' "$HOME_DIR/.bun/bin/bun" not-relevant > "$PROC_FIXTURE/300/cmdline"
}

# T-2630: same direct-spawn poller but with NO readable exe/cwd symlink — readlink
# returns empty, exactly as yama ptrace_scope denies it for a same-uid non-ancestor
# in the real cutover-verify context (the bug that false-negatived a green cutover).
# Identification must come from cmdline (argv[0]/argv[3]) alone, never readlink.
fixture_bun_direct_ptrace_denied() {
  printf '%s\0' "$HOME_DIR/.bun/bin/bun" run --cwd "$PLUGIN" --silent server.ts > "$PROC_FIXTURE/200/cmdline"
  printf '%s\0' "$HOME_DIR/.bun/bin/bun" not-relevant > "$PROC_FIXTURE/300/cmdline"
}

# T-2651: the 2-process runner+server.ts shape (fixture_bun_pair's proper
# parent(200)/child(300) chain, set up by setup_comms_sandbox) but with NO
# readable exe/cwd symlink on the server.ts child — reproduces the ptrace_scope
# denial that false-negatived fire#3 (verify runs as claude-research, a
# non-ancestor of specialist's poller; readlink of a same-uid non-ancestor's
# /proc/<pid>/cwd returns empty). Identification of the server.ts child must
# come from tree position (its parent is the matched runner pid) + cmdline
# alone, never readlink.
fixture_bun_pair_ptrace_denied() {
  printf '%s\0' "$HOME_DIR/.bun/bin/bun" run --cwd "$PLUGIN" --shell=bun --silent start > "$PROC_FIXTURE/200/cmdline"
  printf '%s\0' "$HOME_DIR/.bun/bin/bun" server.ts > "$PROC_FIXTURE/300/cmdline"
}

# T-2651 negative case: a plausible server.ts process that is a SIBLING of the
# runner (child of root, pid 400) rather than the runner's own child. Proves
# the fix gates on tree POSITION, not merely on argv shape — a stray/unrelated
# bun server.ts elsewhere in the tree must not false-positive the check.
fixture_bun_server_sibling_not_child() {
  mkdir -p "$PROC_FIXTURE/400"
  : > "$PROC_FIXTURE/400/status"
  : > "$PROC_FIXTURE/400/children"
  echo "200 400" > "$PROC_FIXTURE/100/children"
  : > "$PROC_FIXTURE/200/children"   # runner has no child here -- 300 unused
  printf '%s\0' "$HOME_DIR/.bun/bin/bun" run --cwd "$PLUGIN" --shell=bun --silent start > "$PROC_FIXTURE/200/cmdline"
  ln -sf "$HOME_DIR/.bun/bin/bun" "$PROC_FIXTURE/200/exe"
  ln -sf "$PLUGIN" "$PROC_FIXTURE/200/cwd"
  printf '%s\0' "$HOME_DIR/.bun/bin/bun" server.ts > "$PROC_FIXTURE/400/cmdline"
  ln -sf "$HOME_DIR/.bun/bin/bun" "$PROC_FIXTURE/400/exe"
  ln -sf "$PLUGIN" "$PROC_FIXTURE/400/cwd"
}

fixture_bare_bun_runner_absolute_server_child() {
  printf '%s\0' bun run --cwd "$PLUGIN" --shell=bun --silent start > "$PROC_FIXTURE/200/cmdline"
  printf '%s\0' "$HOME_DIR/.bun/bin/bun" server.ts > "$PROC_FIXTURE/300/cmdline"
}

fixture_runner_with_plugin_path() { # <plugin-path>
  local plugin_path="$1"
  printf '%s\0' bun run --cwd "$plugin_path" --shell=bun --silent start > "$PROC_FIXTURE/200/cmdline"
  printf '%s\0' "$HOME_DIR/.bun/bin/bun" server.ts > "$PROC_FIXTURE/300/cmdline"
}

fixture_runner_argv0_path() { # <argv0>
  local argv0="$1"
  printf '%s\0' "$argv0" run --cwd "$PLUGIN" --shell=bun --silent start > "$PROC_FIXTURE/200/cmdline"
  printf '%s\0' "$HOME_DIR/.bun/bin/bun" server.ts > "$PROC_FIXTURE/300/cmdline"
}

fixture_bare_bun_server_child() {
  printf '%s\0' bun run --cwd "$PLUGIN" --shell=bun --silent start > "$PROC_FIXTURE/200/cmdline"
  printf '%s\0' bun server.ts > "$PROC_FIXTURE/300/cmdline"
}

fixture_duplicate_pair_groups() {
  for pid in 400 500; do
    mkdir -p "$PROC_FIXTURE/$pid"
    : > "$PROC_FIXTURE/$pid/status"
    : > "$PROC_FIXTURE/$pid/children"
  done
  echo "200 400" > "$PROC_FIXTURE/100/children"
  echo "300" > "$PROC_FIXTURE/200/children"
  echo "500" > "$PROC_FIXTURE/400/children"
  fixture_bare_bun_runner_absolute_server_child
  printf '%s\0' bun run --cwd "$PLUGIN" --shell=bun --silent start > "$PROC_FIXTURE/400/cmdline"
  printf '%s\0' "$HOME_DIR/.bun/bin/bun" server.ts > "$PROC_FIXTURE/500/cmdline"
}

fixture_duplicate_servers_under_runner() {
  mkdir -p "$PROC_FIXTURE/400"
  : > "$PROC_FIXTURE/400/status"
  : > "$PROC_FIXTURE/400/children"
  echo "300 400" > "$PROC_FIXTURE/200/children"
  fixture_bare_bun_runner_absolute_server_child
  printf '%s\0' "$HOME_DIR/.bun/bin/bun" server.ts > "$PROC_FIXTURE/400/cmdline"
}

fixture_direct_and_pair_groups() {
  for pid in 400 500; do
    mkdir -p "$PROC_FIXTURE/$pid"
    : > "$PROC_FIXTURE/$pid/status"
    : > "$PROC_FIXTURE/$pid/children"
  done
  echo "200 400" > "$PROC_FIXTURE/100/children"
  echo "500" > "$PROC_FIXTURE/400/children"
  fixture_bun_direct
  printf '%s\0' bun run --cwd "$PLUGIN" --shell=bun --silent start > "$PROC_FIXTURE/400/cmdline"
  printf '%s\0' "$HOME_DIR/.bun/bin/bun" server.ts > "$PROC_FIXTURE/500/cmdline"
}

setup_comms_sandbox
fixture_bun_pair 1 1
export PATH="$FAKEBIN:$PATH"
if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
check "comms: runner+server both present -> OK" "$r" "ok"
teardown_comms_sandbox

setup_comms_sandbox
fixture_bun_pair 0 1
export PATH="$FAKEBIN:$PATH"
if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
check "comms: runner missing -> not present" "$r" "no"
teardown_comms_sandbox

setup_comms_sandbox
fixture_bun_pair 1 0
export PATH="$FAKEBIN:$PATH"
if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
check "comms: server missing -> not present" "$r" "no"
teardown_comms_sandbox

setup_comms_sandbox
fixture_bun_pair 0 0
export PATH="$FAKEBIN:$PATH"
if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
check "comms: neither present -> not present" "$r" "no"
teardown_comms_sandbox

# Pane not resolvable at all (tmux display-message fails) — must not match.
setup_comms_sandbox
fixture_bun_pair 1 1
rm -f "$SBX/pane_pid"
export PATH="$FAKEBIN:$PATH"
if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
check "comms: pane unresolvable -> not present (fail-closed)" "$r" "no"
teardown_comms_sandbox

# T-2624: the single-process direct-spawn shape must be recognized. This is the
# exact false-negative that rolled back a WORKING cutover on 2026-08-06 (the
# absolute-bun poller `bun run --cwd <plugin> --silent server.ts` matched neither
# old branch, so the check reported comms-dark on a green tool-verified boot).
setup_comms_sandbox
fixture_bun_direct
export PATH="$FAKEBIN:$PATH"
if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
check "comms: T-2624 single-process 'bun run --cwd .. --silent server.ts' -> OK" "$r" "ok"
teardown_comms_sandbox

# T-2630: the poller must be recognized with NO readable exe/cwd (the ptrace_scope
# denial that false-negatived a genuinely-green cutover). Identification via cmdline only.
setup_comms_sandbox
fixture_bun_direct_ptrace_denied
export PATH="$FAKEBIN:$PATH"
if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
check "comms: T-2630 direct-spawn with NO readable exe/cwd (ptrace-denied) -> OK" "$r" "ok"
teardown_comms_sandbox

# T-2651: the 2-process (specialist-shaped) runner+server.ts poller must still be
# recognized when the server.ts child's exe/cwd cannot be read (ptrace_scope
# denial) -- this is the exact false-negative that rolled back a
# mechanically-successful specialist fire#3.
setup_comms_sandbox
fixture_bun_pair_ptrace_denied
export PATH="$FAKEBIN:$PATH"
if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
check "comms: T-2651 2-process runner+server.ts with NO readable exe/cwd (ptrace-denied) -> OK" "$r" "ok"
teardown_comms_sandbox

# T-2651 negative: a server.ts process that is NOT the runner's child (sibling)
# must not satisfy the check, even with a fully readable cwd -- the gate is
# tree position, not argv shape alone.
setup_comms_sandbox
fixture_bun_server_sibling_not_child
export PATH="$FAKEBIN:$PATH"
if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
check "comms: T-2651 server.ts sibling of runner (not child) -> not present" "$r" "no"
teardown_comms_sandbox

setup_comms_sandbox
fixture_bare_bun_runner_absolute_server_child
export PATH="$FAKEBIN:$PATH"
if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
check "test_cutover_telegram_poller_present_accepts_bare_bun_runner_with_absolute_bun_server_child" "$r" "ok"
teardown_comms_sandbox

setup_comms_sandbox
fixture_runner_argv0_path "/usr/bin/bun"
export PATH="$FAKEBIN:$PATH"
if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
check "test_cutover_telegram_poller_present_rejects_missing_or_duplicate_plugin_groups (runner-slash-path-not-account-local)" "$r" "no"
teardown_comms_sandbox

setup_comms_sandbox
fixture_bare_bun_server_child
export PATH="$FAKEBIN:$PATH"
if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
check "test_cutover_telegram_poller_present_rejects_missing_or_duplicate_plugin_groups (server-bare-bun-not-absolute)" "$r" "no"
teardown_comms_sandbox

for bad_plugin in "/tmp/not-telegram/0.0.7" "$PLUGIN/nested" "$HOME_DIR/.claude/plugins/cache/claude-plugins-official/telegram/." "$HOME_DIR/.claude/plugins/cache/claude-plugins-official/telegram/.."; do
  setup_comms_sandbox
  fixture_runner_with_plugin_path "$bad_plugin"
  export PATH="$FAKEBIN:$PATH"
  if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
  check "test_cutover_telegram_poller_present_rejects_wrong_plugin_path_version_and_unanchored_server (${bad_plugin##*/})" "$r" "no"
  teardown_comms_sandbox
done

setup_comms_sandbox
fixture_bun_server_sibling_not_child
export PATH="$FAKEBIN:$PATH"
if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
check "test_cutover_telegram_poller_present_rejects_wrong_plugin_path_version_and_unanchored_server (unanchored-server)" "$r" "no"
teardown_comms_sandbox

setup_comms_sandbox
fixture_bun_pair 1 0
export PATH="$FAKEBIN:$PATH"
if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
check "test_cutover_telegram_poller_present_rejects_missing_or_duplicate_plugin_groups (missing-server)" "$r" "no"
teardown_comms_sandbox

setup_comms_sandbox
fixture_duplicate_pair_groups
export PATH="$FAKEBIN:$PATH"
if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
check "test_cutover_telegram_poller_present_rejects_missing_or_duplicate_plugin_groups (duplicate-pairs)" "$r" "no"
teardown_comms_sandbox

setup_comms_sandbox
fixture_duplicate_servers_under_runner
export PATH="$FAKEBIN:$PATH"
if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
check "test_cutover_telegram_poller_present_rejects_missing_or_duplicate_plugin_groups (duplicate-servers)" "$r" "no"
teardown_comms_sandbox

setup_comms_sandbox
fixture_direct_and_pair_groups
export PATH="$FAKEBIN:$PATH"
if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
check "test_cutover_telegram_poller_present_rejects_missing_or_duplicate_plugin_groups (direct-plus-pair)" "$r" "no"
teardown_comms_sandbox

setup_comms_sandbox
fixture_bun_pair 1 1
export PATH="$FAKEBIN:$PATH"
if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
check "test_cutover_telegram_poller_present_preserves_absolute_bun_direct_and_pair_shapes (absolute-pair)" "$r" "ok"
teardown_comms_sandbox

setup_comms_sandbox
fixture_bun_direct
export PATH="$FAKEBIN:$PATH"
if cutover_telegram_poller_present "claude-telegram-bot-fallback:0" "$HOME_DIR" "$PROC_FIXTURE"; then r=ok; else r=no; fi
check "test_cutover_telegram_poller_present_preserves_absolute_bun_direct_and_pair_shapes (absolute-direct)" "$r" "ok"
teardown_comms_sandbox

echo "----------------------------------------"
echo "$pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
