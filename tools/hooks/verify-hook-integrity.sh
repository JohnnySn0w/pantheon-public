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

    # REFERENCE POINT (T-2198, specialist): compare against the COMMITTED blob at
    # origin/main, not against the working-tree file.
    #
    # The working tree is the wrong reference in two ways, and the second is fatal
    # for most of our hooks. (a) It cannot see that the tree is ITSELF off-canonical.
    # (b) Deployment is mixed copy-vs-symlink per file, and for a SYMLINKED deploy
    # `$dep` and `$canon` resolve to the SAME INODE — so `cmp` compared the file to
    # itself and could never differ. Three of the four deployed hooks are symlinks,
    # so the check was structurally silent for most of its own surface. It would have
    # reported GREEN throughout the window a stray `cp` wrote through a symlink into
    # a peer's worktree — measured, not hypothetical (T-2195).
    #
    # Not "any blob in history": an attacker who can write the file can write an
    # OLDER legitimate version, and a botched rollback produces that state with no
    # attacker at all. Current-commit-at-origin is the only reference that is both
    # CURRENT and NON-LOCAL.
    #
    # ACCEPTED COST, deliberately: an in-flight hook change now reads as drift on
    # every account that has not pulled, so benign hits go up. Taken because a check
    # that is structurally silent on 3 of 4 hooks is not a check, and because step 0
    # already makes the benign case a one-command clear. Purely local (`git show`, no
    # fetch), so it stays fast and offline-safe; if the ref is unreadable we fall back
    # to the worktree file and SAY SO, rather than silently changing what was verified.
    # BYTE-EXACT, via a temp file rather than a $(...) capture (specialist, T-2198 nit):
    # command substitution strips trailing newlines from BOTH sides, so a file
    # differing only in trailing whitespace compared EQUAL. Small, but this is a
    # byte-integrity check — "almost all the bytes" is not the property it claims.
    ref="origin/main blob"
    blob="$(mktemp)" || continue
    if ! git -C "$CANON_DIR" show "origin/main:tools/hooks/${base}" > "$blob" 2>/dev/null; then
      ref="WORKING TREE (origin/main blob unreadable — weaker check)"
      cp "$canon" "$blob" 2>/dev/null || { rm -f "$blob"; continue; }
    fi
    differs=0
    cmp -s "$blob" "$dep" || differs=1
    rm -f "$blob"
    if (( differs )); then
      # Name the DIRECTION (T-2195 suggestion 3). Weak evidence — an attacker controls
      # mtime — so it only softens wording and must never suppress the emission.
      dep_mt=$(stat -c %Y "$dep" 2>/dev/null || echo 0)
      canon_mt=$(stat -c %Y "$canon" 2>/dev/null || echo 0)
      if (( dep_mt > canon_mt )); then
        dir="deployed is NEWER than canonical — consistent with an in-flight deploy landing before its push, OR with a swap"
      else
        dir="canonical is NEWER than deployed — consistent with a missed re-provision after a repo change, OR with a swap"
      fi
      drift+="  ✗ ${base} — deployed bytes DIFFER from canonical [ref: ${ref}]; ${dir}"$'\n'
    fi
  done
fi

out=""
if [[ -n "$drift" ]]; then
  out="⚠️ Hook-integrity check (T-1284): DEPLOYED hook(s) DIFFER from their version-controlled canonical."$'\n'
  out+="Drift is REAL — these bytes differ and this session should not trust the hook until you know why. But drift has a BENIGN cause as well as a malicious one, and they are byte-indistinguishable: a correct fleet-wide hook change deploys to every account BEFORE (or after) its canonical reaches this clone, so an in-flight landing looks exactly like a swap. Do not start from tamper:"$'\n'
  out+="${drift}"
  out+="Remediate, IN THIS ORDER (T-2195 — the previous text put the revert first, which in the in-flight case undoes a correct change someone else deployed and opens an incident on a non-event):"$'\n'
  out+="  0a. git -C ${HOME}/pantheon-config status --short tools/hooks/ FIRST. If the drifted file shows as ' M', the cause is a LOCAL UNCOMMITTED EDIT — not a swap and not a stale clone. Do NOT pull (it aborts on a dirty tree) and do NOT discard it: a deployed hook is often a SYMLINK into this clone, so a fleet-wide deploy writes THROUGH it into your worktree. Find out whose it is before touching it."$'\n'
  out+="  0b. Otherwise: git -C ${HOME}/pantheon-config pull --ff-only, then RE-RUN this check. A clone behind origin is the single most common cause. Only if drift SURVIVES the pull is any of the following warranted."$'\n'
  out+="  1. Read the deployed file and diff it against ${CANON_DIR}/. Judge the CONTENT: a benign change is legible and matches some open ticket; a tamper is not."$'\n'
  out+="  2. Check whether a peer landed it — ask on the mailbox before reverting. Reverting someone's in-flight deploy is itself a fleet-wide action."$'\n'
  out+="  3. ONLY if the content is unexplained after 0-2: restore the canonical and investigate the persistence vector (Miasma signature — allowlisted path, swapped bytes)."$'\n'
fi

printf '%s' "$out"
exit 0
