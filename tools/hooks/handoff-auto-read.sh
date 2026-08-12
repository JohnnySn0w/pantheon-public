#!/bin/bash
# SessionStart hook. Runs for EVERY Pantheon persona — six as of ORG-042 + ORG-043:
# orchestrator / specialist / recovery / mnemosyne / metis / atlas. The old "orchestrator / specialist / recovery" here was
# pre-ORG-042 drift and disagreed with the topology this same file inlines below (T-2261).
#
# Injects (as additionalContext on stdout) two orientation pointers:
#   1. ORG MAP — $HOME/pantheon-config/README.md, ALWAYS if present. Its Read-order
#      section indexes principles, directives, ADRs, tickets, ledger — the org context
#      a fresh session needs (T-0512: instance HANDOFF alone was too little context).
#   2. INSTANCE HANDOFF — $HOME/HANDOFF.md, ONLY if fresh (mtime within $THRESHOLD_HOURS).
#      The freshness guard prevents replay loops on a stale handoff.
#      OVERSIZE GUARD (T-2166): a single Read truncates around 50KB and says so only
#      in the tool result's footer. Specialist measured the cost — 108,540 bytes delivered
#      as 158 of 466 lines, and the 66% lost was the tail, where the DURABLE pointers
#      sat; it then rediscovered a documented fact by hitting the error. Specialist received
#      the tool's own "Continue with offset=158" and did not act on it for many turns,
#      which is why this warns BEFORE the read rather than repeating the remedy after:
#      the defect is salience and authority, not missing information. The hook is the
#      surface that told the session to read the file, so the hook is the surface that
#      must say the read will be partial. Threshold is in BYTES (what stat gives us) and
#      deliberately below the cap — the cap's exact semantics are unsettled (one measured
#      case delivered 49,495 chars as 50,079 bytes, which a strict 50,000-BYTE cap does
#      not explain), so we guard conservatively and never call bytes "the cap".
#
# $HOME-relative so ONE canonical script works for every persona account, whatever the
# count is this month (T-0512 generalize) — which is why only the comment drifted.
# Part of pantheon-config T-0008 deliverable 1 + T-0512.
#
# Failure mode: errors stay silent on stderr; never block session start.

set -e

HANDOFF="$HOME/HANDOFF.md"
ORG_README="$HOME/pantheon-config/README.md"
THRESHOLD_HOURS=1
# Bytes above which a single Read is assumed to truncate. ~10% headroom under the
# ~50KB cap. Override for testing; there is no reason to raise it in production.
HANDOFF_WARN_BYTES="${HANDOFF_WARN_BYTES:-45000}"

# Drain stdin (Claude Code passes hook input JSON; we don't use it).
cat >/dev/null 2>&1 || true

out=""

# 1. Org map — INLINE THE CONTENT, don't point at it.
#
# Was: 'Read ${ORG_README} for org orientation'. That spends a NON-VOLITIONAL channel
# (additionalContext goes into context before the session acts) on a VOLITIONAL
# instruction — the session still has to choose to read it, which reintroduces exactly
# the failure the hook exists to prevent. The operator, 2026-07-28 TG 6632: "is there a less
# volitional way to handle new sessions? can we put items into context on start?"
#
# Motivating failure, same evening: this hook fired, printed the Read instruction, and
# the HO read HANDOFF.md but not the README. It then operated from stale memory pins for
# a full session — missing the reviewer SOP and the dispatch-liveness SOP it had itself
# adopted, and holding a WRONG model of the sub-orchestrator seats (pin said Metis/Atlas
# had no unix accounts; README line 25 says six personas, and suborch1/suborch2 exist).
# The information was one Read away the whole time. An instruction in context still needs
# an action; content in context does not.
#
# RULE: if it is small enough to inline, inline it. If it is too big to inline, it should
# not be a session-start dependency at all — it should be pulled just-in-time by the work
# that needs it. "Read this large file at startup" is the worst of both.
#
# Size discipline — MEASURED, not assumed. The first cut of this change inlined whole
# files behind an 8000-byte guard and silently fell back to pointers for every one of
# them, because the guess was wrong: README.md is 8903 bytes, notes/recent-policy-changes.md
# is 21496, sop/standard/README.md is 41227. A guard that silently degrades to the old
# behaviour is the same defect class this hook exists to fix, so: inline bounded EXTRACTS
# of the load-bearing sections, and make the fallback SAY it fell back.
#
# Budget: ~2.3KB (read order + topology) + ~3.3KB (SOP filenames) + ~2KB (policy head)
# ≈ 8KB per session. Cheap against a session's context; the whole point is that it is
# unconditional.
INLINE_MAX_BYTES="${INLINE_MAX_BYTES:-12000}"

# $1 = label, $2 = fallback sentence, $3 = content.
#
# Content is an ARGUMENT, not stdin, and that is load-bearing: the first cut piped into
# this function, which runs it in a SUBSHELL, so every `out+=` was discarded and the hook
# emitted nothing at all — no inline, and no fallback either, because the fallback branch
# was also inside the subshell. Silent total failure that looked exactly like a hook that
# had not been edited. Caught only by running it; the edit itself "succeeded".
_emit() {
  local label="$1" fallback="$2" content="$3" bytes
  bytes=${#content}
  if (( bytes > 0 && bytes <= INLINE_MAX_BYTES )); then
    out+="--- BEGIN ${label} (inlined — this IS the content, no read needed) ---"$'\n'
    out+="${content}"$'\n'
    out+="--- END ${label} ---"$'\n'
  else
    out+="${fallback} [inline skipped: ${bytes}B > ${INLINE_MAX_BYTES}B budget — this is a DEGRADED path, the content is NOT in your context]"$'\n'
  fi
}

# 1a. Org map — read order + topology only (the orientation), not the whole README.
if [[ -f "$ORG_README" ]]; then
  _readorder="$(sed -n '/^## Read order/,/^## Repository convention/p' "$ORG_README" 2>/dev/null || true)"
  _emit "pantheon-config README — read order + topology" \
        "Read ${ORG_README} for org orientation." "$_readorder"
fi

# 1b. Which SOPs exist. sop/standard/README.md is 41KB — far too big to inline, but the
# FILENAMES are the discovery surface and cost ~3KB. Skipping this index is what let a
# session miss pre-implementation-reviewer-discipline and dispatch-and-wait-liveness on
# 2026-07-28 while believing it was following the SOPs.
if [[ -d "$HOME/pantheon-config/sop" ]]; then
  _sops="$( { echo "SOPs available (read the specific file when relevant; full index at sop/standard/README.md):"
              ls "$HOME"/pantheon-config/sop/standard/*.md "$HOME"/pantheon-config/sop/remediation/*.md 2>/dev/null \
                | sed "s|$HOME/pantheon-config/||"; } || true )"
  _emit "SOP index (filenames)" \
        "Scan ${HOME}/pantheon-config/sop/standard/README.md before any non-trivial action." "$_sops"

fi

# 1b2. THE P&Ds — the durable core, and the thing every DECISIONS section justifies via.
# the operator, 2026-07-28 TG 6640: "might inject the p&ds for good measure."
#
# Asymmetric on purpose, because the two files are asymmetric. principles.md is 8552B and
# goes in WHOLE — P1-P7 are short, load-bearing, and cited constantly. directives.md is
# 43736B and cannot; but its D1-D12 HEADINGS are self-describing ("Preserve the operator's
# bandwidth", "Autonomy bar", "Smoke verification discipline") and cost 683B, so the index
# alone carries most of the recall value. Knowing D5 exists and is about smoke discipline
# is what makes you go read D5; not knowing it exists is the whole failure.
if [[ -f "$HOME/pantheon-config/rulebook/principles.md" ]]; then
  _principles="$(cat "$HOME/pantheon-config/rulebook/principles.md" 2>/dev/null || true)"
  _emit "rulebook/principles.md (P1-P7, FULL)" \
        "Read ${HOME}/pantheon-config/rulebook/principles.md." "$_principles"
fi

if [[ -f "$HOME/pantheon-config/rulebook/directives.md" ]]; then
  _directives="$( { echo "Directives D1-D12 (titles only — read rulebook/directives.md for any you are about to rely on):"
                    grep '^## D[0-9]' "$HOME/pantheon-config/rulebook/directives.md" 2>/dev/null; } || true )"
  _emit "rulebook/directives.md (D1-D12 index)" \
        "Read ${HOME}/pantheon-config/rulebook/directives.md." "$_directives"
fi

# 1c. What changed recently — head only; the file is reverse-chronological, so the head
# IS the recency signal and the tail is history that does not need re-reading each session.
if [[ -f "$HOME/pantheon-config/notes/recent-policy-changes.md" ]]; then
  _policy="$(head -25 "$HOME/pantheon-config/notes/recent-policy-changes.md" 2>/dev/null || true)"
  _emit "recently-ratified policy (head)" \
        "Skim ${HOME}/pantheon-config/notes/recent-policy-changes.md." "$_policy"
fi

# 2. Instance handoff — inject only if fresh (mtime within threshold).
if [[ -f "$HANDOFF" ]]; then
  mtime=$(stat -c %Y "$HANDOFF" 2>/dev/null || echo 0)
  now=$(date +%s)
  if (( mtime > 0 && now - mtime <= THRESHOLD_HOURS * 3600 )); then
    out+="Read ${HANDOFF} and follow it. (Auto-injected by handoff-auto-read.sh — file's mtime is within ${THRESHOLD_HOURS}h, indicating a fresh handoff from the previous session.)"$'\n'

    # Oversize guard: state the incompleteness BEFORE the read, so a full read is the
    # default rather than a recovery. Every step is fail-open — a hook that cannot size
    # the file must still inject the read instruction above.
    bytes=$(stat -c %s "$HANDOFF" 2>/dev/null || echo 0)
    if (( bytes > HANDOFF_WARN_BYTES )); then
      lines=$(wc -l < "$HANDOFF" 2>/dev/null || echo 0)
      passes=$(( (bytes + 44999) / 45000 ))
      # T-2317: this used to say "past the ~50KB single-Read limit: ONE Read will
      # NOT deliver it" at a threshold of 45,000 — a FALSE claim for every size
      # between the safe line and real truncation, where one Read does deliver the
      # whole file. State both numbers and their relationship instead. Note the
      # header's standing constraint: never call the byte figure "the cap" (the
      # cap's char-vs-byte semantics are unsettled — 49,495 chars measured as
      # 50,079 bytes), which is why this says "safe line" and hedges "~50KB".
      out+="WARNING — ${HANDOFF} is ${lines} lines / ${bytes} bytes, past the 45,000 B safe line. Real Read truncation is ~50KB (exact semantics unsettled): ABOVE that, one Read silently drops the TAIL — where the durable pointers live — leaving you confidently misinformed rather than obviously short; between the safe line and there you are inside the margin and one Read still delivers the whole file. If this file is over ~50KB, read it in at least ${passes} passes (offset=0, then the offset each truncated result reports) BEFORE acting on it. Either way, prune toward 45,000 per sop/standard/end-of-work-notes-consolidation.md item 3 — durable-first ordering, archive at the bottom."$'\n'
    fi
  fi
fi

printf '%s' "$out"
exit 0
