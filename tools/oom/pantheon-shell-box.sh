#!/bin/bash
# pantheon-shell-box.sh -- CLAUDE_CODE_SHELL_PREFIX wrapper: run one tool command in a
# memory-bounded transient scope, with the session's OOM shield taken off (T-5222).
#
# WHY. On 2026-10-19 a reviewer subagent's probe generator grew to ~9.8 GiB with a timeout
# but no memory bound, exhausted RAM+swap host-wide, and the global OOM took fleet user
# managers with it. Tool commands inherit the session's oom_score_adj (-850 from the ORG-104
# session shield), so the runaway child was shielded too. This wrapper is the non-inheritance
# rule: the command runs with adj 0, inside its own scope with MemoryMax, so a runaway is
# killed inside its scope (scope result oom-kill, rc 137) and the session survives.
#
# CONTRACT (claude.exe, function lce): Claude Code runs `'<prefix>' '<command string>'`
# through the shell, so the whole command arrives as ONE argument and is run with bash -c.
# The prefix must be an absolute path. Hooks and MCP servers are prefixed too.
#
# FAIL-OPEN. With no reachable user manager (no $XDG_RUNTIME_DIR/systemd/private), or with
# PANTHEON_SHELL_BOX=off, the command runs unboxed. If systemd-run itself fails to start the
# scope (bus hiccup, task limits, a user manager mid-restart), the command also runs unboxed:
# a marker file that the boxed command deletes as its first act tells "the scope never
# started" apart from "the command ran and returned non-zero". Each fallback logs one line to
# stderr and to ~/.local/state/pantheon/shell-box-fallback.log, so an unbounded seat is
# visible. A broken wrapper would break every Bash call on the seat; a missing box only loses
# the bound.
#
# Knobs: PANTHEON_SHELL_BOX_MEMORY_MAX (default 2G), PANTHEON_SHELL_BOX_SWAP_MAX (default
# 512M), PANTHEON_SHELL_BOX=off.

set -u

if [ "$#" -eq 0 ]; then
    echo "pantheon-shell-box: no command" >&2
    exit 2
fi
if [ "$#" -eq 1 ]; then
    set -- /bin/bash -c "$1"
fi

# Take the session shield off this command and its children (an unprivileged process may
# raise its own adj; proven on recovery 2026-10-19). Never fatal.
echo 0 > /proc/self/oom_score_adj 2>/dev/null || true

fallback() {
    local why="pantheon-shell-box: $1; running this command UNBOXED (no memory cap)"
    echo "$why" >&2
    { mkdir -p "$HOME/.local/state/pantheon" \
        && printf '%s %s\n' "$(date -u +%FT%TZ)" "$why" >> "$HOME/.local/state/pantheon/shell-box-fallback.log"; } \
        2>/dev/null || true
}

if [ "${PANTHEON_SHELL_BOX:-on}" != off ] && [ -n "${XDG_RUNTIME_DIR:-}" ] \
        && [ -S "$XDG_RUNTIME_DIR/systemd/private" ] && command -v systemd-run >/dev/null 2>&1; then
    if marker=$(mktemp "$XDG_RUNTIME_DIR/pantheon-shell-box.XXXXXX" 2>/dev/null); then
        systemd-run --user --scope --quiet --collect \
            -p MemoryMax="${PANTHEON_SHELL_BOX_MEMORY_MAX:-2G}" \
            -p MemorySwapMax="${PANTHEON_SHELL_BOX_SWAP_MAX:-512M}" \
            -- /bin/bash -c 'rm -f -- "$0"; exec "$@"' "$marker" "$@"
        rc=$?
        if [ ! -e "$marker" ]; then
            exit "$rc"   # the command started inside the scope: its rc is the answer
        fi
        rm -f -- "$marker"
        fallback "scope start failed (systemd-run rc $rc)"
    else
        fallback "could not create a start marker in \$XDG_RUNTIME_DIR"
    fi
fi
exec "$@"
