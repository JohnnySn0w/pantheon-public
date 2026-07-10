#!/usr/bin/env bash
# Stage a fresh /tmp/echo-<task> worktree off origin/main with vendor symlink.
#
# Why: `git worktree add` creates a new working tree with the gitignored
# `vendor/` directory absent. Echo's silero-vad adapter dlopens libonnxruntime
# via `ORT_DYLIB_PATH` (workspace-relative), so missing `vendor/onnxruntime-*`
# causes 9/9 silero_vad tests to fail with `libonnxruntime.so: No such file
# or directory`. T-0658 traced this across the T-0500 / T-0642 / T-0643 cycles.
#
# Fix: after `git worktree add`, symlink the worktree's `vendor/` entries to
# the canonical /home/claude/echo-prismatic/vendor/ entries. Vendor binaries
# are identical across worktrees by design (same ORT version pinned in
# tools/setup-onnxruntime.sh) so the symlink is structurally correct.
#
# Usage:
#   bash tools/stage-echo-worktree.sh <ticket-id>
# Example:
#   bash tools/stage-echo-worktree.sh T-0700
# Produces:
#   /tmp/echo-T-0700/ worktree on branch agent/T-0700 off origin/main
#   /tmp/echo-T-0700/vendor/onnxruntime-linux-x64-1.22.0 -> canonical vendor

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <ticket-id>  (e.g. T-0700)" >&2
  exit 1
fi

TICKET="$1"
ECHO_CANONICAL="/home/claude/echo-prismatic"
WORKTREE_PATH="/tmp/echo-${TICKET}"
BRANCH_NAME="agent/${TICKET}"

if [[ ! -d "${ECHO_CANONICAL}" ]]; then
  echo "error: ${ECHO_CANONICAL} not found; expected the canonical Echo clone" >&2
  exit 1
fi

if [[ -e "${WORKTREE_PATH}" ]]; then
  echo "error: ${WORKTREE_PATH} already exists; remove it first or use a different ticket id" >&2
  exit 1
fi

cd "${ECHO_CANONICAL}"
git fetch origin >&2
git worktree add "${WORKTREE_PATH}" -b "${BRANCH_NAME}" origin/main >&2

# Vendor symlink: discover each entry under canonical vendor/, mirror into
# worktree vendor/. Handles future per-platform additions without hardcoding.
mkdir -p "${WORKTREE_PATH}/vendor"
for entry in "${ECHO_CANONICAL}"/vendor/*; do
  [[ -e "${entry}" ]] || continue
  name="$(basename "${entry}")"
  ln -s "${entry}" "${WORKTREE_PATH}/vendor/${name}"
done

# Activate the pre-push smoke gate for this worktree (T-0683). Belt-and-suspenders:
# once tools/install-hooks.sh has been run on the canonical clone the shared config
# already covers worktrees, but this makes a freshly-staged worktree self-sufficient.
git -C "${WORKTREE_PATH}" config core.hooksPath .githooks

echo "${WORKTREE_PATH} (branch ${BRANCH_NAME})"
