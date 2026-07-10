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

REPO="$HOME/pantheon-config"
SUBDIR=".githooks"
out=""

# .git is a dir in the canonical clone, a file in linked worktrees; accept both.
if [[ -d "$REPO/.git" || -f "$REPO/.git" ]]; then
  current="$(git -C "$REPO" config --local core.hooksPath 2>/dev/null || true)"
  if [[ -z "$current" ]]; then
    if git -C "$REPO" config core.hooksPath "$SUBDIR" 2>/dev/null; then
      out+="🔧 ensure-hooks-active: core.hooksPath was UNSET in ${REPO} — auto-repaired to ${SUBDIR}."$'\n'
      out+="The commit-guard + commit-msg + pre-push hooks were silently OFF and are now active again (T-1216 option B)."$'\n'
    else
      out+="⚠️ ensure-hooks-active: core.hooksPath is UNSET in ${REPO} and auto-repair FAILED — hooks are NOT running."$'\n'
      out+="Fix manually: git -C ${REPO} config core.hooksPath ${SUBDIR}"$'\n'
    fi
  elif [[ "$current" != "$SUBDIR" ]]; then
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

printf '%s' "$out"
exit 0
