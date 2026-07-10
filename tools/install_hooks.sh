#!/usr/bin/env bash
# tools/install_hooks.sh — idempotent bootstrap for pantheon-config git hooks.
#
# Sets core.hooksPath to .githooks so git picks up the pre-commit,
# commit-msg, and pre-push hooks checked into this repo.  Safe to run
# multiple times:
#   - already configured to .githooks  → no-op (prints confirmation)
#   - configured to a different path   → warns, does NOT overwrite
#   - not configured                   → sets it
#
# Also ensures the hook files are executable (defensive for platforms where
# file mode may not survive a fresh clone cleanly).
#
# Usage:
#   bash tools/install_hooks.sh
#
# Run once per clone after `git clone`.

set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
HOOKS_SUBDIR=".githooks"
HOOKS_PATH="${REPO_ROOT}/${HOOKS_SUBDIR}"

# Resolve current hooksPath setting (empty string if unset).
CURRENT="$(git -C "${REPO_ROOT}" config --local core.hooksPath 2>/dev/null || true)"

if [[ "${CURRENT}" == "${HOOKS_SUBDIR}" ]]; then
    echo "install_hooks.sh: already configured (core.hooksPath = ${HOOKS_SUBDIR}). No changes made."
elif [[ -n "${CURRENT}" ]]; then
    echo "install_hooks.sh: WARNING — core.hooksPath is already set to '${CURRENT}'." >&2
    echo "install_hooks.sh: Not overwriting. If you want to use ${HOOKS_SUBDIR}," >&2
    echo "  run: git config core.hooksPath ${HOOKS_SUBDIR}" >&2
    exit 1
else
    git -C "${REPO_ROOT}" config core.hooksPath "${HOOKS_SUBDIR}"
    echo "install_hooks.sh: set core.hooksPath = ${HOOKS_SUBDIR}"
fi

# Ensure hook files are executable (belt-and-suspenders for fresh clones).
for HOOK in "${HOOKS_PATH}/pre-commit" "${HOOKS_PATH}/commit-msg" "${HOOKS_PATH}/pre-push"; do
    if [[ -f "${HOOK}" ]]; then
        chmod +x "${HOOK}"
        echo "install_hooks.sh: confirmed executable: $(basename "${HOOK}")"
    else
        echo "install_hooks.sh: WARNING — expected hook not found: ${HOOK}" >&2
    fi
done

echo "install_hooks.sh: done. HERMES discipline + pre-push freshness hooks are active for this clone."
