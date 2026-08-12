#!/usr/bin/env bash
# Local git history for orchestrator's two unversioned durable artifacts (T-2424).
#
# THE GAP THIS CLOSES: the memory dir and HANDOFF.md carry everything a session
# knows that is NOT derivable from the repos (D10). Both were single-copy with
# no VCS and no snapshot — a bad sed or a disk event loses them silently, and
# nothing surfaces it until a future session goes looking for a fact that had
# quietly stopped existing. Sister to T-2233 (mnemosyne's seat, same finding).
#
# SHAPE: mirrors exocortex-data-snapshot.service — the proven, tiny mechanism —
# but snapshots COPIES into a dedicated repo rather than git-init'ing the live
# dirs. Two reasons: HANDOFF.md and the memory dir have no common parent short
# of $HOME (which holds root.env and every project clone, so a repo there is a
# footgun), and a HANDOFF copy placed INSIDE the memory dir would pollute the
# harness's memory recall, which reads that directory.
#
# ⛔ STRICTLY LOCAL. No remote, ever. The memory dir holds personal facts about
# the operator; T-2233 rejected folding this into pantheon-config for exactly that
# reason (it has a remote), and that reasoning binds here identically.
# Off-host replication is a separate, open the operator decision — not this script's.
#
# Idempotent and safe to run by hand at any time; the timer is a floor, not the
# only path. Run it manually right after editing HANDOFF.md.
set -Eeuo pipefail

REPO="${PANTHEON_AGENT_STATE_REPO:-$HOME/.local/state/pantheon/agent-state-history}"
MEMORY_DIR="${PANTHEON_MEMORY_DIR:-$HOME/.claude/projects/-home-claude/memory}"
HANDOFF="${PANTHEON_HANDOFF:-$HOME/HANDOFF.md}"
# T-2736: not every memory-bearing seat has a HANDOFF.md. Sub-orchestrators
# atlas/metis carry accumulated memory but no HANDOFF, so a hard "HANDOFF
# MISSING -> exit 3" leaves their single-copy memory UNPROTECTED — the exact
# silent-loss risk this guard exists to close. REQUIRE_HANDOFF makes the HANDOFF
# leg opt-OUT, explicitly per seat (NOT auto-detected from absence): the default
# (=1) keeps the loud loss-detection for the seats that DO have a HANDOFF
# (recovery/orchestrator/specialist) — a configured HANDOFF that vanishes is a real loss and
# still exit-3s — while memory-only seats install with =0 and snapshot memory
# alone. A present HANDOFF is always snapshotted regardless of the flag.
REQUIRE_HANDOFF="${PANTHEON_SNAPSHOT_REQUIRE_HANDOFF:-1}"

[[ -d "$REPO/.git" ]] || { echo "no repo at $REPO — run: git init \"$REPO\"" >&2; exit 2; }

# Fail loudly on a missing SOURCE rather than committing its disappearance as an
# empty snapshot. Losing the file is the event this exists to survive; recording
# the loss as a clean commit would be the guard erasing its own evidence. The
# memory dir is ALWAYS required — it is the thing being protected.
[[ -d "$MEMORY_DIR" ]] || { echo "memory dir MISSING: $MEMORY_DIR" >&2; exit 3; }
if [[ ! -f "$HANDOFF" ]]; then
  # Missing + required (default) is still a loud loss for HANDOFF-bearing seats.
  [[ "$REQUIRE_HANDOFF" == "1" ]] && { echo "HANDOFF MISSING: $HANDOFF" >&2; exit 3; }
  # Missing + opt-out: a memory-only seat by design — warn, snapshot memory alone.
  echo "note: no HANDOFF at $HANDOFF — memory-only snapshot (PANTHEON_SNAPSHOT_REQUIRE_HANDOFF=0)" >&2
fi

mkdir -p "$REPO/memory"
# --delete so a genuinely removed memory file shows as a deletion in history
# rather than lingering forever in the mirror.
rsync -a --delete "$MEMORY_DIR/" "$REPO/memory/"
# Snapshot the HANDOFF whenever one exists (both modes); memory-only seats simply
# have none to copy.
[[ -f "$HANDOFF" ]] && cp -p "$HANDOFF" "$REPO/HANDOFF.md"

cd "$REPO"
git add -A
if git diff --cached --quiet; then
  echo "no change — nothing to snapshot"
  exit 0
fi
files=$(git diff --cached --name-only | wc -l)
git commit --quiet -m "snapshot $(date -u +%Y-%m-%dT%H:%M:%SZ) — ${files} file(s) changed"
echo "snapshot committed: $(git rev-parse --short HEAD) (${files} file(s))"
