#!/usr/bin/env bash
# tools/echo-merge.sh — automate the echo-prismatic agent/T-NNNN PR merge flow.
#
# Turns the reference_echo_agent_branch_merge_flow runbook into one supervised
# command: push (pre-push smoke) -> PR -> poll CI to ci-gate-green -> --admin
# merge -> refresh main + clean up worktree/branch. T-1306.
#
# SAFETY MODEL. The irreversible merge is gated TWICE: (1) dry-run is the DEFAULT
# — without --execute the script stops after CI polling and only PRINTS what it
# would merge; (2) even with --execute it refuses unless the run's overall
# conclusion is `success` AND the `ci-gate` job is `success`. So the first live
# use is inherently supervised. Push/PR-create/poll are safe/idempotent.
#
# TOKEN. GH_TOKEN is read from gh_token.env and exported for `gh` (env, never a
# cmdline arg). Nothing echoes the token; git push relies on the clone's already
# -configured credential helper (same as a manual `git push origin <branch>`).
#
# GOTCHAS baked in (see reference_echo_agent_branch_merge_flow + bugs/):
#  - poll `gh run view <id> --json status,conclusion,jobs`, NOT `gh run watch`
#    (spurious exit-0 under the fine-grained PAT) and NOT `gh pr view
#    --json statusCheckRollup` (GraphQL, PAT-403, returns empty -> loops forever).
#  - `ci-gate` is the ONE required check and reports LAST; --admin is required
#    (PAT author can't self-approve) but ci-gate is NOT admin-bypassable.
#  - repo delete_branch_on_merge=false -> we delete the branch explicitly.
#
# Usage:
#   tools/echo-merge.sh <ticket-id> [options]
#     <ticket-id>            e.g. T-1481  (branch agent/<id>, worktree /tmp/echo-<id>)
#   --execute               perform the merge (default: dry-run, stop before merge)
#   --title <s>             PR title      (required only if no PR exists yet)
#   --body-file <path>      PR body file  (required only if no PR exists yet)
#   --method squash|merge|rebase   merge method (default: squash)
#   --no-push               branch already pushed; skip the push phase
#   --worktree <path>       override worktree (default /tmp/echo-<id>)
#   --repo <owner/name>     override repo   (default example-org/echo-prismatic)
#   --main-checkout <path>  override main clone (default /home/claude/echo-prismatic)
#   --poll-timeout <secs>   CI poll ceiling (default 1500)
#   -h|--help
#
# AUTO-RESUME (T-1631): re-running to catch a timed-out CI poll (the runner-queue
# remedy) is cheap — if the branch is already on origin at the same sha, phase 1
# detects the no-op and SKIPS the pre-push smoke automatically (no --no-push flag
# needed, no wasted cargo run contending with the CI it's waiting on).
#
# NOTE: run under `dangerouslyDisableSandbox: true` — the Bash sandbox SIGTERMs
# git push + the pre-push cargo smoke + gh network calls (exit 144).

set -euo pipefail

# ---- defaults ----------------------------------------------------------------
REPO="example-org/echo-prismatic"
MAIN_CHECKOUT="/home/claude/echo-prismatic"
TOKEN_ENV="/home/claude/gh_token.env"
METHOD="squash"
EXECUTE=0
DO_PUSH=1
POLL_TIMEOUT=1500
POLL_INTERVAL=30
TICKET=""
WORKTREE=""
PR_TITLE=""
PR_BODY_FILE=""

die() { echo "echo-merge: ERROR: $*" >&2; exit 1; }
log() { echo "echo-merge: $*" >&2; }

# ---- arg parse ---------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute) EXECUTE=1; shift ;;
    --no-push) DO_PUSH=0; shift ;;
    --title) PR_TITLE="${2:-}"; shift 2 ;;
    --body-file) PR_BODY_FILE="${2:-}"; shift 2 ;;
    --method) METHOD="${2:-}"; shift 2 ;;
    --worktree) WORKTREE="${2:-}"; shift 2 ;;
    --repo) REPO="${2:-}"; shift 2 ;;
    --main-checkout) MAIN_CHECKOUT="${2:-}"; shift 2 ;;
    --poll-timeout) POLL_TIMEOUT="${2:-}"; shift 2 ;;
    -h|--help) grep -E '^# ' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option: $1" ;;
    *) [[ -z "$TICKET" ]] && TICKET="$1" || die "unexpected arg: $1"; shift ;;
  esac
done

[[ -n "$TICKET" ]] || die "missing <ticket-id> (e.g. T-1481)"
[[ "$TICKET" =~ ^T-[0-9]+$ ]] || die "ticket-id must look like T-NNNN, got: $TICKET"
[[ "$METHOD" =~ ^(squash|merge|rebase)$ ]] || die "--method must be squash|merge|rebase"
BRANCH="agent/${TICKET}"
[[ -n "$WORKTREE" ]] || WORKTREE="/tmp/echo-${TICKET}"

# ---- token (exported for gh, never printed) ----------------------------------
[[ -f "$TOKEN_ENV" ]] || die "token file not found: $TOKEN_ENV"
GH_TOKEN="$(tr -d '\n\r ' < "$TOKEN_ENV" | sed 's/^GH_TOKEN=//')"
[[ -n "$GH_TOKEN" ]] || die "GH_TOKEN empty after reading $TOKEN_ENV"
export GH_TOKEN

# ---- preflight ---------------------------------------------------------------
command -v gh >/dev/null || die "gh not on PATH"
if [[ "$DO_PUSH" == "1" ]]; then
  [[ -d "$WORKTREE" ]] || die "worktree not found: $WORKTREE (use --no-push if already pushed)"
  cur="$(git -C "$WORKTREE" branch --show-current 2>/dev/null || true)"
  [[ "$cur" == "$BRANCH" ]] || die "worktree $WORKTREE is on '$cur', expected '$BRANCH'"
fi
log "ticket=$TICKET branch=$BRANCH repo=$REPO method=$METHOD execute=$EXECUTE"

# ---- phase 1: push -----------------------------------------------------------
if [[ "$DO_PUSH" == "1" ]]; then
  # Auto-resume: if the branch is already on origin at the SAME sha, this is a
  # re-run to catch a timed-out CI poll (the documented remedy for runner-queue
  # delays). Re-firing the pre-push cargo smoke on a no-op push wastes minutes
  # AND contends with the very CI run we're waiting on (one VPS) — T-1631.
  # ls-remote reads origin's sha without mutating any local ref.
  local_sha="$(git -C "$WORKTREE" rev-parse "$BRANCH" 2>/dev/null || true)"
  origin_sha="$(git -C "$WORKTREE" ls-remote origin "refs/heads/$BRANCH" 2>/dev/null | awk 'NR==1{print $1}')"
  if [[ -n "$origin_sha" && "$local_sha" == "$origin_sha" ]]; then
    log "[1/5] branch already on origin at ${local_sha:0:8} — nothing to push; skipping pre-push smoke (auto-resume)."
  else
    log "[1/5] push $BRANCH (pre-push smoke runs; may take minutes) ..."
    git -C "$WORKTREE" push -u origin "$BRANCH" 2>&1 | sed -E 's#https://[^@]*@#https://REDACTED@#g' >&2
    log "[1/5] pushed."
  fi
else
  log "[1/5] --no-push: skipping push."
fi

# ---- phase 2: PR (reuse existing, else create) -------------------------------
PR_NUM="$(gh pr list --repo "$REPO" --head "$BRANCH" --state open --json number --jq '.[0].number // empty' 2>/dev/null || true)"
if [[ -n "$PR_NUM" ]]; then
  log "[2/5] reusing open PR #$PR_NUM for $BRANCH."
else
  [[ -n "$PR_TITLE" && -n "$PR_BODY_FILE" ]] || die "no open PR for $BRANCH; provide --title and --body-file to create one"
  [[ -f "$PR_BODY_FILE" ]] || die "body file not found: $PR_BODY_FILE"
  log "[2/5] creating PR ..."
  PR_URL="$(gh pr create --repo "$REPO" --base main --head "$BRANCH" --title "$PR_TITLE" --body-file "$PR_BODY_FILE")"
  PR_NUM="$(echo "$PR_URL" | grep -oE '[0-9]+$')"
  log "[2/5] created PR #$PR_NUM ($PR_URL)."
fi

# ---- phase 3: poll CI to terminal --------------------------------------------
log "[3/5] locating CI run for $BRANCH ..."
RUN_ID=""
for _ in $(seq 1 10); do
  RUN_ID="$(gh run list --repo "$REPO" --branch "$BRANCH" --json databaseId,status --limit 1 --jq '.[0].databaseId // empty' 2>/dev/null || true)"
  [[ -n "$RUN_ID" ]] && break
  sleep 6
done
[[ -n "$RUN_ID" ]] || die "no CI run found for $BRANCH after PR open"
log "[3/5] run id=$RUN_ID; polling (timeout ${POLL_TIMEOUT}s, NOT gh run watch) ..."

elapsed=0
STATUS=""; CONCLUSION=""
while (( elapsed < POLL_TIMEOUT )); do
  read -r STATUS CONCLUSION < <(gh run view "$RUN_ID" --repo "$REPO" --json status,conclusion \
      --jq '"\(.status) \(.conclusion // "null")"' 2>/dev/null || echo "query_err null")
  if [[ "$STATUS" == "completed" ]]; then break; fi
  sleep "$POLL_INTERVAL"; elapsed=$(( elapsed + POLL_INTERVAL ))
done
[[ "$STATUS" == "completed" ]] || die "CI did not complete within ${POLL_TIMEOUT}s (last status=$STATUS)"

# ci-gate is the ONE required check; confirm it (and overall) are green.
CIGATE="$(gh run view "$RUN_ID" --repo "$REPO" --json jobs \
    --jq '.jobs[] | select(.name=="ci-gate") | .conclusion' 2>/dev/null || true)"
log "[3/5] CI completed: conclusion=$CONCLUSION ci-gate=${CIGATE:-<none>}"
if [[ "$CONCLUSION" != "success" || "$CIGATE" != "success" ]]; then
  log "[3/5] failed jobs:"
  gh run view "$RUN_ID" --repo "$REPO" --json jobs \
    --jq '.jobs[] | select(.conclusion!="success") | "  \(.name): \(.status)/\(.conclusion)"' >&2 || true
  die "CI not green (conclusion=$CONCLUSION ci-gate=${CIGATE:-none}) — refusing to merge"
fi

# ---- phase 4: merge (gated) --------------------------------------------------
if [[ "$EXECUTE" != "1" ]]; then
  log "[4/5] DRY-RUN: CI is green. Would merge PR #$PR_NUM with --admin --$METHOD --delete-branch."
  log "[4/5] Re-run with --execute to perform the merge. Stopping (no changes made)."
  exit 0
fi
log "[4/5] merging PR #$PR_NUM (--admin --$METHOD --delete-branch) ..."
gh pr merge "$PR_NUM" --repo "$REPO" --admin --"$METHOD" --delete-branch >&2
log "[4/5] merged."

# ---- phase 5: refresh main + cleanup ----------------------------------------
log "[5/5] refreshing $MAIN_CHECKOUT to origin/main + cleaning up ..."
if [[ -d "$MAIN_CHECKOUT/.git" ]]; then
  git -C "$MAIN_CHECKOUT" checkout main >/dev/null 2>&1 || true
  git -C "$MAIN_CHECKOUT" fetch -q origin main
  git -C "$MAIN_CHECKOUT" merge --ff-only origin/main 2>&1 | tail -1 >&2 || log "  (ff-only merge skipped/failed — check manually)"
  # Remove the worktree unconditionally after a merge — phase 5 only runs post-merge,
  # so reaching here means the PR landed regardless of whether THIS invocation pushed
  # (a two-step `dry-run` then `--no-push --execute` flow merges in the no-push leg).
  # `git -C "$MAIN_CHECKOUT"` runs from the main checkout, so it can remove a worktree
  # even when the caller's cwd is inside it.
  WORKTREE_GONE=1
  if [[ -d "$WORKTREE" ]]; then
    if git -C "$MAIN_CHECKOUT" worktree remove "$WORKTREE" --force 2>&1 | tail -1 >&2; then :; fi
    [[ -d "$WORKTREE" ]] && { WORKTREE_GONE=0; log "  (worktree remove failed — remove manually: git -C $MAIN_CHECKOUT worktree remove $WORKTREE --force)"; }
  fi
  git -C "$MAIN_CHECKOUT" worktree prune >/dev/null 2>&1 || true
  git -C "$MAIN_CHECKOUT" branch -D "$BRANCH" >/dev/null 2>&1 || true
fi
# Belt-and-suspenders: delete the remote ref if --delete-branch didn't.
gh api -X DELETE "repos/${REPO}/git/refs/heads/${BRANCH}" >/dev/null 2>&1 || true

if [[ "${WORKTREE_GONE:-1}" == "1" ]]; then
  log "DONE: PR #$PR_NUM merged; main refreshed; $BRANCH + worktree cleaned up."
else
  log "DONE: PR #$PR_NUM merged; main refreshed; branch deleted. NOTE: worktree $WORKTREE still present — remove manually."
fi
