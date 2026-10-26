#!/bin/bash
# SessionStart hook (fleet-generic): auto-repair pantheon-config git-hook
# activation, so the checked-in .githooks guard cannot silently lapse.
#
# Why. The 2026-06-09 episode (T-1198 / T-1216 re-eval) found core.hooksPath
# EMPTY in a clone: the committed .githooks (byte-sequence commit guard +
# commit-msg + pre-push freshness) were present but NOT running, because the
# per-clone activation had drifted. tools/install_hooks.sh is run-ONCE and
# drifts silently; this hook re-asserts activation every session start so the
# guard self-heals instead of failing open unnoticed.
#
# Ratified: the operator 2026-07-01 (Telegram 4512) — HERMES-tier re-eval T-1216
# option B ("harden core.hooksPath across all three agent clones"), prioritized
# over the server-side audit Action (option A). Sibling defense to
# verify-hook-integrity.sh: that checks hook-script CONTENT (Miasma tamper);
# this checks hook ACTIVATION (silent drift).
#
# Mechanism: locate $HOME/pantheon-config; read core.hooksPath. Unset -> SET it
# to .githooks (auto-repair) and surface the repair. Already .githooks -> no-op,
# silent. Set to some OTHER value -> WARN but do NOT clobber (could be
# intentional) — mirrors install_hooks.sh semantics. Also re-asserts the hook
# files are executable (defensive for fresh clones).
#
# FAIL-OPEN by design: ALWAYS exits 0 — a boot-health check must never block or
# slow a session start. Emits on stdout (as additionalContext) ONLY when it
# repaired or found a problem; a clean run is suppressed as noise. Mirrors
# verify-hook-integrity.sh / verify-startup-boot.sh.

cat >/dev/null 2>&1 || true   # drain the hook-input JSON (unused)

SUBDIR=".githooks"
out=""

# DISCOVERED, not hardcoded (2026-08-03, T-2392 sweep). This was pinned to
# $HOME/pantheon-config, which left the exact 2026-06-09 failure reachable in
# any OTHER repo carrying the same committed guard — and one was already in it:
# pantheon-public shipped all three hooks (commit-msg, pre-commit, pre-push)
# with core.hooksPath UNSET and nothing in .git/hooks, so a guard that refuses
# the trigger correctly when invoked was never invoked at all. Verified by
# direct-invoking it: exit 1 on a trigger message, exit 0 on a safe one.
#
# A repo that COMMITS a .githooks/ dir is asserting it wants those hooks run.
# So cover every such repo under $HOME rather than a list someone has to
# remember to extend — the list-you-must-remember is the same failure class
# this hook exists to close. Per-repo semantics below are unchanged: SET only
# when unset, WARN but never clobber a different value.
shopt -s nullglob
for REPO in "$HOME"/*/; do
  REPO="${REPO%/}"
  [[ -d "$REPO/$SUBDIR" ]] || continue

# .git is a dir in the canonical clone, a file in linked worktrees; accept both.
if [[ -d "$REPO/.git" || -f "$REPO/.git" ]]; then
  current="$(git -C "$REPO" config --local core.hooksPath 2>/dev/null || true)"
  # A linked worktree reads its main clone's SHARED config, where the absolute form names
  # the MAIN worktree's hooks. git runs those, so that value is active too (T-4427). Only an
  # ordinary <main>/.git common dir counts; anything else leaves main_root empty.
  common="$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  main_root=""
  [[ "$common" == /*/.git ]] && main_root="${common%/.git}"
  if [[ -z "$current" ]]; then
    if git -C "$REPO" config core.hooksPath "$SUBDIR" 2>/dev/null; then
      out+="🔧 ensure-hooks-active: core.hooksPath was UNSET in ${REPO} — auto-repaired to ${SUBDIR}."$'\n'
      out+="The commit-guard + commit-msg + pre-push hooks were silently OFF and are now active again (T-1216 option B)."$'\n'
    else
      out+="⚠️ ensure-hooks-active: core.hooksPath is UNSET in ${REPO} and auto-repair FAILED — hooks are NOT running."$'\n'
      out+="Fix manually: git -C ${REPO} config core.hooksPath ${SUBDIR}"$'\n'
    fi
  elif [[ "$current" != "$SUBDIR" && "$current" != "$REPO/$SUBDIR" \
          && ( -z "$main_root" || "$current" != "$main_root/$SUBDIR" ) ]]; then
    # The absolute form resolves to the same hooks. Claude Code's
    # isolation-worktree creation rewrites core.hooksPath to it on every
    # dispatch, so warning on it is noise, and resetting it just churns.
    out+="⚠️ ensure-hooks-active: core.hooksPath in ${REPO} is '${current}', not '${SUBDIR}'. Not clobbering."$'\n'
    out+="If unintended, run: git -C ${REPO} config core.hooksPath ${SUBDIR}"$'\n'
  fi
  # Re-assert hooks executable (silent unless something is missing).
  for h in pre-commit commit-msg pre-push; do
    f="$REPO/$SUBDIR/$h"
    if [[ -f "$f" && ! -x "$f" ]]; then
      chmod +x "$f" 2>/dev/null || true
    fi
  done
fi

done
shopt -u nullglob

printf '%s' "$out"
exit 0
