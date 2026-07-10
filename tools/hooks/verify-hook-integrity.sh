#!/bin/bash
# SessionStart hook (fleet-generic): content-integrity check of DEPLOYED hook
# scripts against their version-controlled canonicals — automates the manual diff
# Specialist ran in the T-1244 Miasma SessionStart-hook audit. T-1284.
#
# Threat model. The Miasma worm persists across reinstall by tampering the
# SessionStart hook / settings file. There are two tamper vectors:
#   (1) IDENTITY  — settings.json repointed to a NEW/malicious script, or a hook
#       command laced with shell metacharacters. ALREADY COVERED by the runtime
#       allowlist checker (context_fill_monitor.check_hook_allowlist, 5-min
#       cadence): a wired command that is not a clean invocation of exactly one
#       allowlisted script is flagged.
#   (2) CONTENT   — an ALLOWLISTED hook script whose bytes are swapped IN PLACE
#       (same path, same basename, still allowlisted, no metacharacters). This
#       PASSES the allowlist checker. It is exactly what Specialist verified by hand in
#       T-1244 (byte-diff deployed ~/.claude/hooks/* vs tools/hooks/*). This hook
#       closes that gap automatically, per session start.
#
# Mechanism: for every DEPLOYED file under ~/.claude/hooks/ that has a tracked
# canonical at pantheon-config/tools/hooks/<same-basename>, byte-compare the two
# and surface DRIFT on mismatch. Self-discovering — no separate allowlist to keep
# in sync; the set of checked hooks is "deployed ∩ canonical". Hooks with no
# canonical (third-party hook-handler.js, local-only snapshot-settings.sh) are
# not content-checkable here; their IDENTITY is covered by the allowlist checker.
# Symlinked deployments (e.g. backlog-surface.sh) resolve to the canonical and are
# byte-identical by construction, so they pass silently; if a symlink is ever
# replaced by a tampered regular file, cmp catches it.
#
# Scope note (v1): this checks hook SCRIPT content. Detecting REMOVAL of a security
# hook, or a settings.json downgrade, needs a tracked per-persona expected-hooks
# manifest — deliberately deferred as a possible fast-follow (see T-1284 DECISIONS).
#
# FAIL-OPEN by design: ALWAYS exits 0. A boot-health check must NEVER block or slow
# a session start. Mirrors handoff-auto-read.sh / verify-startup-boot.sh. Emits on
# stdout (as additionalContext) ONLY when drift is present; an all-clean run is
# suppressed as noise.

cat >/dev/null 2>&1 || true   # drain the hook-input JSON (unused)

HOOKS_DIR="$HOME/.claude/hooks"
CANON_DIR="$HOME/pantheon-config/tools/hooks"
drift=""

if [[ -d "$HOOKS_DIR" && -d "$CANON_DIR" ]]; then
  for dep in "$HOOKS_DIR"/*; do
    [[ -f "$dep" ]] || continue                 # -f follows symlinks; skips dirs (e.g. .recall_seen)
    base="$(basename "$dep")"
    canon="$CANON_DIR/$base"
    [[ -f "$canon" ]] || continue               # no tracked canonical → not content-checkable here
    if ! cmp -s "$dep" "$canon"; then
      drift+="  ✗ ${base} — deployed bytes DIFFER from canonical (${canon})"$'\n'
    fi
  done
fi

out=""
if [[ -n "$drift" ]]; then
  out="⚠️ Hook-integrity check (T-1284): DEPLOYED hook(s) DIFFER from their version-controlled canonical."$'\n'
  out+="This is the Miasma persistence-tamper signature — allowlisted path, swapped bytes. Verify before trusting this session:"$'\n'
  out+="${drift}"
  out+="Remediate: diff each against ${CANON_DIR}/. If you did not change it, restore the canonical and investigate the persistence vector."$'\n'
fi

printf '%s' "$out"
exit 0
