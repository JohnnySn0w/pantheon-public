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
#  - PROBLEM 2 fallback (T-2714): if the CI-poll background task itself gets
#    killed (a long echo run can outlast the harness's background-task
#    duration cap), don't re-run cold — watch the SAME run read-only via
#    Monitor: `gh run view <run-id> --repo <repo> --json status,conclusion`.
#    On ci-gate green, merge manually:
#    `gh pr merge <pr#> --repo <repo> --admin --squash --delete-branch`, then
#    refresh main + clean the worktree by hand. Or just re-run this script —
#    it's already resumable (see AUTO-RESUME below): it reuses the open PR and
#    relocates the CI run rather than re-pushing.
#
# STAGED MODE (T-2877): the one-shot flow (push + pre-push-smoke + CI-poll +
# merge) reliably OUTLASTS the harness background-task duration cap and gets
# KILLED mid-flight (T-1666: killed during the pre-push smoke, before push
# landed). Auto-resume (T-1631) only helps AFTER remote state exists; a kill in
# the pre-push-smoke phase leaves nothing to resume. Split the flow so each
# invocation fits the cap and persists resumable state at its boundary:
#   1. tools/echo-merge.sh <id> --stage-push [--no-verify-push] --title .. --body-file ..
#        push + PR-create + locate the CI run, PRINT `run-id=<id>` and EXIT.
#        (no CI poll, no merge — the short half that establishes remote state.)
#   2. tools/echo-merge.sh <id> --merge-when-green [<run-id>] --execute
#        skip push; reuse the open PR; ONE bounded CI poll; --admin merge; clean
#        up. Re-invokable across turns: on poll TIMEOUT (exit 3) just re-run the
#        SAME command — it resumes the poll (the run-id is optional; omitted, it
#        relocates the run by branch). Dry-run without --execute, same as before.
# The default (neither flag) is the unchanged one-shot flow — keep it for short
# doc-only merges whose CI fits inside the cap.
#
# Usage:
#   tools/echo-merge.sh <ticket-id> [options]
#     <ticket-id>            e.g. T-1481  (branch agent/<id>, worktree /tmp/echo-<id>)
#   --stage-push            STAGE 1: push + PR + locate run-id, print it, EXIT
#                           (no poll/merge/cleanup). Mutually exclusive with
#                           --merge-when-green and --execute.
#   --merge-when-green [id]  STAGE 2: skip push; poll the CI run (id optional —
#                           relocated by branch if omitted) to green, then merge
#                           (with --execute) + clean up. Re-invokable on exit 3.
#                           Mutually exclusive with --stage-push.
#   --execute               perform the merge (default: dry-run, stop before merge)
#   --title <s>             PR title      (required only if no PR exists yet)
#   --body-file <path>      PR body file  (required only if no PR exists yet)
#   --method squash|merge|rebase   merge method (default: squash)
#   --no-push               branch already pushed; skip the push phase
#   --no-verify-push        push with `git push --no-verify`, skipping the
#                           pre-push hook (full `cargo test --workspace`),
#                           which cold-builds gtk/tauri/webkit via
#                           echo-demo-app and gets KILLED mid-build on
#                           workspace-touching branches (T-2714). NOT a
#                           verification skip: `ci-gate` is a required,
#                           non-admin-bypassable check that re-runs the
#                           identical smoke on the PR before merge, so
#                           verification just moves to CI, the authoritative
#                           gate. OPT-IN — omitting it is byte-identical to
#                           the push behavior before this flag existed.
#   --worktree <path>       override worktree (default /tmp/echo-<id>)
#   --repo <owner/name>     override repo   (default example-org/echo-prismatic)
#   --main-checkout <path>  override main clone (default /home/claude/echo-prismatic)
#   --poll-timeout <secs>   CI poll ceiling (default 4500 — a busy single-runner cycle
#                           is ~50min queued-behind-another; T-1670)
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
# T-2714: env-overridable so a fixture path can drive this same script
# hermetically in tests / on seats without /home/claude/gh_token.env (Recovery,
# codex, CI). Unset TOKEN_ENV -> identical default to before.
TOKEN_ENV="${TOKEN_ENV:-/home/claude/gh_token.env}"
METHOD="squash"
EXECUTE=0
DO_PUSH=1
DO_NO_VERIFY_PUSH=0
STAGE_PUSH=0            # T-2877 stage 1: push + PR + locate run-id, print, exit
MERGE_WHEN_GREEN=0      # T-2877 stage 2: poll + merge + cleanup (no push)
MERGE_RUN_ID=""         # optional explicit run-id for --merge-when-green
POLL_TIMEOUT=4500
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
    --stage-push) STAGE_PUSH=1; shift ;;
    --merge-when-green)
      MERGE_WHEN_GREEN=1
      # Optional run-id: consume the next token ONLY if it's all-digits (a run
      # id). Otherwise leave it for the parser (a flag) or the positional ticket
      # — merge-when-green then relocates the run by branch.
      if [[ "${2:-}" =~ ^[0-9]+$ ]]; then MERGE_RUN_ID="$2"; shift 2; else shift; fi
      ;;
    --no-push) DO_PUSH=0; shift ;;
    --no-verify-push) DO_NO_VERIFY_PUSH=1; shift ;;
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

# ---- staged-mode validation (T-2877) -----------------------------------------
if [[ "$STAGE_PUSH" == "1" && "$MERGE_WHEN_GREEN" == "1" ]]; then
  die "--stage-push and --merge-when-green are mutually exclusive (they are the two halves of the staged flow — run them in separate invocations)"
fi
if [[ "$STAGE_PUSH" == "1" && "$EXECUTE" == "1" ]]; then
  die "--stage-push never merges; drop --execute (merge happens in the --merge-when-green stage)"
fi
# The merge stage never pushes — it resumes from remote state the push stage left.
if [[ "$MERGE_WHEN_GREEN" == "1" ]]; then DO_PUSH=0; fi

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
    push_args=(-C "$WORKTREE" push -u origin "$BRANCH")
    if [[ "$DO_NO_VERIFY_PUSH" == "1" ]]; then
      push_args+=(--no-verify)
      log "[1/5] --no-verify-push: skipping the pre-push hook (ci-gate is the authoritative gate) ..."
    else
      log "[1/5] push $BRANCH (pre-push smoke runs; may take minutes) ..."
    fi
    git "${push_args[@]}" 2>&1 | sed -E 's#https://[^@]*@#https://REDACTED@#g' >&2
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
  # The merge stage resumes an already-opened PR — it must not create one (that
  # is the push stage's job). A missing PR here means stage 1 never ran (or the
  # PR closed), so fail loudly rather than silently opening a fresh PR.
  [[ "$MERGE_WHEN_GREEN" != "1" ]] || die "no open PR for $BRANCH — run --stage-push first (--merge-when-green resumes an existing PR, it does not create one)"
  [[ -n "$PR_TITLE" && -n "$PR_BODY_FILE" ]] || die "no open PR for $BRANCH; provide --title and --body-file to create one"
  [[ -f "$PR_BODY_FILE" ]] || die "body file not found: $PR_BODY_FILE"
  log "[2/5] creating PR ..."
  PR_URL="$(gh pr create --repo "$REPO" --base main --head "$BRANCH" --title "$PR_TITLE" --body-file "$PR_BODY_FILE")"
  PR_NUM="$(echo "$PR_URL" | grep -oE '[0-9]+$')"
  log "[2/5] created PR #$PR_NUM ($PR_URL)."
fi

# ---- phase 3: locate CI run --------------------------------------------------
if [[ -n "$MERGE_RUN_ID" ]]; then
  # --merge-when-green <id>: trust the id stage 1 printed; skip the locate loop.
  RUN_ID="$MERGE_RUN_ID"
  log "[3/5] using provided run id=$RUN_ID for $BRANCH."
else
  log "[3/5] locating CI run for $BRANCH ..."
  RUN_ID=""
  for _ in $(seq 1 10); do
    RUN_ID="$(gh run list --repo "$REPO" --branch "$BRANCH" --json databaseId,status --limit 1 --jq '.[0].databaseId // empty' 2>/dev/null || true)"
    [[ -n "$RUN_ID" ]] && break
    sleep 6
  done
  [[ -n "$RUN_ID" ]] || die "no CI run found for $BRANCH after PR open"
fi

# STAGE 1 boundary (T-2877): push + PR + run-id located = the short half is done
# and remote state now exists (resumable). Print the run-id machine-greppably and
# hand off to the merge stage. Do NOT poll — that is what outlasts the cap.
if [[ "$STAGE_PUSH" == "1" ]]; then
  log "[3/5] STAGE-PUSH COMPLETE: PR #$PR_NUM open, CI run located."
  log "Resume when convenient (re-invokable across turns):"
  log "  tools/echo-merge.sh $TICKET --merge-when-green $RUN_ID --execute"
  echo "run-id=$RUN_ID pr=$PR_NUM"   # stdout, machine-greppable for the next turn
  exit 0
fi

log "[3/5] run id=$RUN_ID; polling (timeout ${POLL_TIMEOUT}s, NOT gh run watch) ..."

elapsed=0
STATUS=""; CONCLUSION=""
while (( elapsed < POLL_TIMEOUT )); do
  read -r STATUS CONCLUSION < <(gh run view "$RUN_ID" --repo "$REPO" --json status,conclusion \
      --jq '"\(.status) \(.conclusion // "null")"' 2>/dev/null || echo "query_err null")
  if [[ "$STATUS" == "completed" ]]; then break; fi
  sleep "$POLL_INTERVAL"; elapsed=$(( elapsed + POLL_INTERVAL ))
done
if [[ "$STATUS" != "completed" ]]; then
  # T-1670: timeout is NOT failure — push+PR succeeded and CI is still running.
  # Distinct exit code (3) + explicit resume guidance so orchestrators don't
  # burn a diagnosis round conflating this with a red build.
  log "TIMEOUT (not failure): PR #$PR_NUM is open and CI run $RUN_ID is still ${STATUS}."
  log "Resume: re-run this same command (auto-resumes into polling), or poll manually:"
  log "  gh run view $RUN_ID --repo $REPO --json status,conclusion"
  exit 3
fi

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
