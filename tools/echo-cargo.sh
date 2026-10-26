#!/usr/bin/env bash
# echo-cargo.sh — run cargo for echo-prismatic against the SHARED target, safely (T-4822).
#
# Usage: ~/pantheon-config/tools/echo-cargo.sh <cargo args...>
#   e.g. ~/pantheon-config/tools/echo-cargo.sh test -p echo-core
#
# Every seat that builds echo shares /var/lib/pantheon/echo-target (T-3777). Three
# things must hold on EVERY build there, and ad-hoc `cargo` runs kept missing one:
#   1. CARGO_TARGET_DIR points at the shared target (an inherited value wins).
#   2. umask 002, so peers can overwrite this seat's artifacts.
#   3. echo_target_adopt.py runs first: a re-running build script fs::copy()s into
#      an OUT file another seat owns, and fs::copy's fchmod needs ownership, so
#      group-write is never enough (T-4071, bugs/shared-cargo-target-fs-copy-eperm-
#      foreign-owned-out-dir.md). The echo pre-push hook already does all three;
#      this wrapper gives development builds (implementers, ad-hoc tests) the same.
#   4. echo_worktree_switch.py runs next (T-5152): every echo worktree shares unit
#      hashes for the workspace crates, so when the target was last built by a
#      DIFFERENT worktree, it touches this worktree's crate roots and cargo rebuilds
#      them from this worktree's own source instead of reusing the other branch's.
#   5. Steps 3, 4 and cargo run as ONE critical section under a wrapper flock on
#      <target>/.echo-wrapper-lock (T-5296). Without it, a build that switched and
#      then queued on cargo's lock behind a peer's build would see its touched
#      sources as OLDER than the peer's fresh outputs and reuse them anyway. The
#      wrapper re-execs itself under `flock -o`, so cargo's children never inherit
#      the lock fd, and ECHO_CARGO_LOCK_HELD stops a nested wrapper from deadlocking.
#      Same-profile builds already serialize on cargo's own lock; the added cost is
#      that wrapped builds of different profiles now serialize too.
#
# Adopt is fail-open (it never blocks the build). Extra adopt scope, if a crate
# outside the known copiers fails: ECHO_ADOPT_PACKAGES=ring,openssl-sys.
set -u

SHARED_TARGET="${PANTHEON_ECHO_TARGET_DIR:-/var/lib/pantheon/echo-target}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "${CARGO_TARGET_DIR:-}" ]]; then
    export CARGO_TARGET_DIR="$SHARED_TARGET"
fi
umask 002

if [[ "$CARGO_TARGET_DIR" == "$SHARED_TARGET" && -d "$SHARED_TARGET" && -z "${ECHO_CARGO_LOCK_HELD:-}" ]]; then
    lock="$CARGO_TARGET_DIR/.echo-wrapper-lock"
    if command -v flock >/dev/null 2>&1 && { : >>"$lock"; } 2>/dev/null; then
        export ECHO_CARGO_LOCK_HELD=1
        if ! flock -n "$lock" true; then
            echo "echo-cargo: another wrapped echo build holds $lock; waiting for it (T-5296)" >&2
        fi
        exec flock -o "$lock" bash "${BASH_SOURCE[0]}" "$@"
    fi
    echo "echo-cargo: cannot take $lock; building without the wrapper lock (T-5296)" >&2
fi

if [[ "$CARGO_TARGET_DIR" == "$SHARED_TARGET" && -d "$SHARED_TARGET" ]]; then
    adopt_args=(--target "$CARGO_TARGET_DIR")
    if [[ -n "${ECHO_ADOPT_PACKAGES:-}" ]]; then
        adopt_args+=(--packages "$ECHO_ADOPT_PACKAGES")
    fi
    python3 "$HERE/echo_target_adopt.py" "${adopt_args[@]}" >&2 || true
fi

CARGO_BIN="$(command -v cargo || true)"
if [[ -z "$CARGO_BIN" && -x "$HOME/.cargo/bin/cargo" ]]; then
    CARGO_BIN="$HOME/.cargo/bin/cargo"
fi
if [[ -z "$CARGO_BIN" ]]; then
    echo "echo-cargo: cargo not found on PATH or in ~/.cargo/bin" >&2
    exit 127
fi

if [[ "$CARGO_TARGET_DIR" == "$SHARED_TARGET" && -d "$SHARED_TARGET" ]]; then
    python3 "$HERE/echo_worktree_switch.py" --target "$CARGO_TARGET_DIR" --cargo "$CARGO_BIN" >&2 || true
fi
exec "$CARGO_BIN" "$@"
