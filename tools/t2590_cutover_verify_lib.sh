#!/usr/bin/env bash
# t2590_cutover_verify_lib.sh — pure/testable helpers for t2590_recovery_cutover.sh's
# post-apply verify block (T-2620, T-2622). Sourced, never executed directly:
# this file defines functions only, no top-level side effects, so it is safe to
# `source` from both the production script (under `set -euo pipefail`) and a
# plain test runner (no `set -e`) without imposing either shell option on the
# caller.
#
# Split out of t2590_recovery_cutover.sh rather than restructuring that script into
# a function+main-guard shape: the cutover script is a delicate, linear,
# no-function fire script, and the surrounding brief was explicit that a full
# restructure is more invasive than the fix warrants. A sourced-helpers file is
# the lightest seam that still makes the logic-bearing pieces unit-testable the
# way they are actually invoked (sourced), matching the T-2618 lesson (test the
# real invocation shape, not a reimplementation).
#
# Three helpers, two live-fire defects (T-2620) + one gap (T-2622):
#
#   select_cutover_claude_pid   — T-2620 defect 1 (wrong-process pid select).
#   cutover_urgent_env_confirmed — T-2620 defect 2 (ptrace-denied environ read;
#                                  replaced with a same-uid systemd-Environment
#                                  proxy — see t2590_recovery_cutover.sh's verify
#                                  block for the live-validated propagation
#                                  chain: service Environment= -> persona_bringup.py
#                                  -> `systemd-run --user --scope` fork+exec (no
#                                  env= override) -> tmux -> claude).
#   cutover_telegram_poller_present — T-2622 (post-cutover inbound-comms check).
#                                  Deliberately NOT bot.pid-based: T-2345 (see
#                                  tools/verify_startup.sh's poller_child_state())
#                                  found bot.pid EIGHT DAYS stale and trusted as
#                                  healthy in the founding incident — "the
#                                  process tree is the evidence, never bot.pid".
#                                  This is the same kernel-PPID-tree-walk
#                                  predicate that function uses (absolute Bun
#                                  exe + exact argv/cwd shape identifies the
#                                  plugin runner and its server.ts child),
#                                  narrowed to this script's single pane/account
#                                  — no multi-agent AGENT_HOME dispatch table is
#                                  needed for a Recovery-only cutover script.

# select_cutover_claude_pid — reads "<pid> <comm>" candidate lines from stdin
# (one process per line) and echoes the pid of the first whose `comm` is
# exactly the claude binary's process name. Returns 1 with no output if none
# match.
#
# WHY THIS EXISTS: the caller's `pgrep -u claude-fallback -f
# "\.npm-global/bin/clau[d]e"` matches BOTH the real claude process AND the
# transient `tmux new-session -d ... claude ...` LAUNCHER (its argv contains
# the full inner command as one substring-matchable string) — proven on the
# 2026-08-06T16:28 live fire, where `| head -1` picked the launcher (lower
# pid, already exited) over the real claude child. `comm` distinguishes them:
# the launcher's comm is `tmux`/`tmux: server`, never `claude`.
#
# `clau[d]e` bracket idiom (matches this file's caller's existing convention,
# see the NB comment in t2590_recovery_cutover.sh): a bash glob character class
# with one member is byte-for-byte equivalent to the literal string, so this
# is a plain equality match against `comm`, not a fuzzy pattern — it keeps the
# contiguous claude-binary byte-sequence out of this script's source, same
# reason the pgrep -f pattern strings use it. Used ONLY on this identity-match
# construct, not on the plain-English "claude" that appears elsewhere in this
# codebase (grep-verified: the caller script's own constants, comments, and
# variable values spell it out normally — the bracket is narrow, not a
# blanket convention).
select_cutover_claude_pid() {
  local pid comm
  while read -r pid comm; do
    [[ -n "$pid" ]] || continue
    if [[ "$comm" == clau[d]e ]]; then
      echo "$pid"
      return 0
    fi
  done
  return 1
}

# cutover_urgent_env_confirmed <token> <show-string> — true (rc 0) iff
# <show-string> (systemd's space-joined `KEY=VAL KEY=VAL ...` line, as
# produced by `systemctl --user show <service> -p Environment --value`)
# contains <token> (e.g. "PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED=1") as a
# WHOLE token, not merely a substring of some other KEY=VALUE pair. Pure
# string logic — no systemctl call in here, so this is testable with zero
# live systemd state; the caller owns fetching <show-string>.
cutover_urgent_env_confirmed() {
  local token="$1" show="$2"
  printf '%s\n' "$show" | tr ' ' '\n' | grep -qx -- "$token"
}

_t2590_cutover_bun_runner_argv0_ok() { # <argv0> <home>
  local argv0="$1" home="$2" bun="$home/.bun/bin/bun"
  [[ "$argv0" == "bun" || "$argv0" == "$bun" ]]
}

_t2590_cutover_bun_absolute_argv0_ok() { # <argv0> <home>
  local argv0="$1" home="$2"
  [[ "$argv0" == "$home/.bun/bin/bun" ]]
}

_t2590_cutover_plugin_dir_ok() { # <path> <home>
  local candidate="$1" home="$2"
  local plugin="$home/.claude/plugins/cache/claude-plugins-official/telegram/"
  local suffix
  [[ "$candidate" == "$plugin"* ]] || return 1
  suffix="${candidate#"$plugin"}"
  [[ -n "$suffix" && "$suffix" != "." && "$suffix" != ".." && "$suffix" != */* ]]
}

# cutover_telegram_poller_present <pane> <home> <proc-root> — true (rc 0) iff
# BOTH telegram-plugin bun children are alive under <pane>'s tmux-owned
# process tree: the plugin `bun run --cwd <plugin-dir> ... start` runner AND
# its `bun server.ts` child. Walks the KERNEL PPID tree from tmux's own
# pane_pid (ground truth, via `pgrep -P` — the parent-PID-based predicate
# pgrep_predicate_lint.py explicitly treats as SOUND, unlike `-f`), identifying
# each role by absolute Bun exe path plus exact argv/cwd shape — same
# identifying logic as verify_startup.sh's poller_child_state(), narrowed to
# a single pane/account (no AGENT_HOME dispatch table).
#
# <proc-root> is both the real default ("/proc") and the test seam — mirrors
# tools/verify_startup.sh's PANTHEON_VERIFY_PROC_ROOT / PROC_ROOT convention
# (a synthetic proc tree under a tmp dir, walked via a fake `pgrep -P` on
# PATH), so this reuses an existing, hermetically-proven test pattern rather
# than inventing a new one.
cutover_telegram_poller_present() {
  local pane="$1" home="$2" proc="$3"
  local root
  root="$(tmux display-message -p -t "$pane" "#{pane_pid}" 2>/dev/null)" || return 1
  [[ "$root" =~ ^[0-9]+$ && -r "$proc/$root/status" ]] || return 1
  local queue=("$root") seen=0 runner_count=0 server_count=0 direct_count=0 pair_count=0 complete_count=0
  local parent children pid exe argv arg
  local -A runner_seen=() server_for_runner=()
  while (( ${#queue[@]} )); do
    parent="${queue[0]}"; queue=("${queue[@]:1}")
    children="$(pgrep -P "$parent" 2>/dev/null || true)"
    for pid in $children; do
      [[ "$pid" =~ ^[0-9]+$ ]] || continue
      queue+=("$pid"); seen=$((seen + 1))
      (( seen <= 256 )) || return 1
      # T-2630: identify by CMDLINE (ptrace-SAFE), NOT readlink exe/cwd. Under
      # yama ptrace_scope the verify process cannot readlink /proc/<pid>/exe or
      # /cwd of a same-uid NON-ANCESTOR process — the exact restriction T-2620 hit
      # on /proc/<pid>/environ. The old `exe==bun` gate therefore SILENTLY SKIPPED
      # a live poller in the cutover-verify context (false-negatived a green
      # cutover). /proc/<pid>/cmdline IS readable, and argv carries what we need:
      # argv[0]=either the account-local bun exe path or, for the runner shape
      # only, the literal token `bun`; argv[3]=the --cwd plugin dir
      # (authoritative for `bun run --cwd`, so the process's real cwd need not
      # be readlink'd).
      argv=()
      while IFS= read -r -d "" arg; do argv+=("$arg"); done < "$proc/$pid/cmdline" 2>/dev/null
      (( ${#argv[@]} )) || continue
      if (( ${#argv[@]} >= 5 )) && [[ "${argv[1]}" == run && "${argv[2]}" == --cwd ]] \
           && _t2590_cutover_bun_runner_argv0_ok "${argv[0]}" "$home" \
           && _t2590_cutover_plugin_dir_ok "${argv[3]}" "$home"; then
        for arg in "${argv[@]:4}"; do
          if [[ "$arg" == start ]]; then
            runner_seen["$pid"]=1
            runner_count=$((runner_count + 1))
            break
          fi
        done
      fi
      # T-2651: identify the server.ts CHILD by TREE POSITION (its direct
      # parent, `$parent` — the pid whose children this BFS iteration is
      # walking — must be the matched runner pid), NOT by readlink cwd.
      # Specialist's poller is a 2-process shape (runner spawns `bun server.ts` as
      # a direct child); the prior `cwd == "$plugin"*` gate required
      # readlink(/proc/<pid>/cwd), which yama ptrace_scope denies for a
      # same-uid non-ancestor — the SAME restriction class T-2630 fixed for
      # the runner/single-process branches, missed here.
      if (( ${#argv[@]} == 2 )) && [[ "${argv[1]}" == server.ts ]] \
           && _t2590_cutover_bun_absolute_argv0_ok "${argv[0]}" "$home"; then
        server_count=$((server_count + 1))
        if [[ -n "${runner_seen[$parent]:-}" ]]; then
          server_for_runner["$parent"]=$(( ${server_for_runner[$parent]:-0} + 1 ))
        fi
      fi
      # T-2624/T-2630: the absolute-bun direct-spawn launch (the PATH fix) is a
      # SINGLE process `bun run --cwd <plugin-dir> --silent server.ts`, identified
      # PURELY by cmdline (argv[3] carries the plugin dir via --cwd — ptrace-safe,
      # no readlink cwd needed). Single process = whole poller (runner+server).
      if (( ${#argv[@]} >= 4 )) && [[ "${argv[1]}" == run && "${argv[2]}" == --cwd ]] \
           && _t2590_cutover_bun_absolute_argv0_ok "${argv[0]}" "$home" \
           && _t2590_cutover_plugin_dir_ok "${argv[3]}" "$home"; then
        for arg in "${argv[@]:4}"; do
          [[ "$arg" == server.ts ]] && { direct_count=$((direct_count + 1)); break; }
        done
      fi
    done
  done
  for pid in "${!runner_seen[@]}"; do
    [[ "${server_for_runner[$pid]:-0}" == 1 ]] && pair_count=$((pair_count + 1))
  done
  complete_count=$((direct_count + pair_count))
  [[ "$complete_count" == 1 && "$runner_count" == "$pair_count" && "$server_count" == "$pair_count" ]]
}

# cutover_companion_rejoined <unit> <session> — true (rc 0) iff a companion
# unit (T-1689/T-2648, e.g. specialist's acp-bridge) is both an active systemd
# --user unit AND has a live tmux session by that name. T-2651: extracted so
# the caller (persona_cutover.sh's post-apply verify) can wrap it in a bounded
# retry the same way cutover_telegram_poller_present already is — the
# unwrapped, zero-wait single check false-negatived fire#3 ("acp-bridge-specialist
# NOT rejoined") on a companion that WAS live: `systemctl --user start` had
# just fired and its `tmux new-session` had not yet landed on the scope-owned
# default tmux server by the time the very next line checked it. Pure
# wrapper, no retry/sleep logic in here — that's the caller's concern, kept
# thin so this stays unit-testable with fake systemctl/tmux on PATH.
cutover_companion_rejoined() {
  local unit="$1" session="$2"
  systemctl --user is-active --quiet "$unit" && tmux has-session -t "$session" 2>/dev/null
}

# rollback_scope_clean <scope_unit> [proc_root] — true (rc 0) iff the CURRENT
# default tmux server (if any) has NO cgroup membership under <scope_unit>.
# T-2642: persona_rollback.sh's verify signal that the restarted bespoke
# service actually took over a clean seat, not the still-running
# control-plane server the rollback's teardown was supposed to have killed
# above. Same cgroup-membership technique persona_cutover.sh's own "scope
# ownership" verify uses (mirroring runtime_controller.py's _pid_in_scope),
# inverted here: for rollback, membership means the teardown FAILED, not
# that it succeeded. <proc_root> is both the real default ("/proc") and the
# test seam, matching cutover_telegram_poller_present's convention above. No
# tmux server at all (e.g. rolling back a fire that never got that far) is
# trivially clean.
rollback_scope_clean() {
  local scope_unit="$1" proc_root="${2:-/proc}"
  local server_pid
  server_pid="$(tmux -N list-sessions -F '#{pid}' 2>/dev/null | sort -u | head -n1 || true)"
  [[ -n "$server_pid" ]] || return 0
  grep -q "$scope_unit" "$proc_root/$server_pid/cgroup" 2>/dev/null && return 1
  return 0
}

# runtime_scope_owned_and_session_live <session> <scope_unit> [proc_root] --
# true (rc 0) iff BOTH: <session> is a live tmux session, AND the CURRENT
# default tmux server's pid is cgroup-owned by <scope_unit>. T-2658: the
# positive counterpart to rollback_scope_clean above (T-2642) -- same
# /proc/<pid>/cgroup technique, mirroring runtime_controller.py's
# _pid_in_scope, inverted-back to a positive membership check plus a session-
# liveness check. This is handoff_and_restart.sh's readiness gate for a
# control-plane-supervised persona's restart: the fired
# pantheon-persona-consume@<persona>.service unit is Type=oneshot with no
# RemainAfterExit, so it is never `systemctl is-active` after it exits --
# this predicate is the actual witness that ensure_runtime() (tools/
# runtime_controller.py) produced a live, scope-owned session, not the unit's
# own transient active state. <proc_root> is both the real default ("/proc")
# and the test seam, matching rollback_scope_clean's and
# cutover_telegram_poller_present's convention above.
#
# NOTE for T-2634 (this lib's planned rename): this function must ride along
# with rollback_scope_clean when t2590_cutover_verify_lib.sh is renamed --
# it is a general runtime-controller predicate, not Recovery-T-2590-specific,
# same as every other function in this file already is.
runtime_scope_owned_and_session_live() {
  local session="$1" scope_unit="$2" proc_root="${3:-/proc}"
  tmux has-session -t "$session" 2>/dev/null || return 1
  local server_pid
  server_pid="$(tmux -N list-sessions -F '#{pid}' 2>/dev/null | sort -u | head -n1 || true)"
  [[ -n "$server_pid" ]] || return 1
  grep -q "$scope_unit" "$proc_root/$server_pid/cgroup" 2>/dev/null
}
