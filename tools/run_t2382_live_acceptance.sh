#!/usr/bin/env bash
# T-2382 real-path live acceptance wrapper for named tests 55-59.
#
# Operators must first append live observations with:
#   tools/run_t2382_live_acceptance.sh init <leg> <run-id>
#   tools/run_t2382_live_acceptance.sh observe <leg> <run-id> --phase ...
# This wrapper finalizes exactly one gated leg and never starts services, sends
# tmux input, deploys code, or invokes a provider.
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SOURCE_REPO="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly HELPER="${SOURCE_REPO}/tools/t2382_live_acceptance.py"
readonly EVIDENCE_ROOT="/home/codex/.local/state/pantheon/t2382-live-proof"

usage() {
  cat >&2 <<'USAGE'
usage:
  tools/run_t2382_live_acceptance.sh init <leg> <run-id>
  tools/run_t2382_live_acceptance.sh observe <leg> <run-id> --phase <phase> [phase arguments]
  tools/run_t2382_live_acceptance.sh finalize <leg> <reviewed-commit> <run-id>

legs:
  test_live_codex_interactive_tmux_quota_and_ack
  test_live_claude_interactive_tmux_quota_and_ack
  test_live_same_unit_planned_harness_swap_preserves_persona_and_tmux
  test_live_forced_cap_requeues_and_bootstraps_from_chronicle_receipt
  test_live_explicit_claude_batch_sdk_is_nondefault_and_separately_metered

required gates for finalize:
  every leg requires T2382_LIVE_ACCEPTANCE=1
  codex requires T2382_LIVE_CODEX=1
  claude requires T2382_LIVE_CLAUDE=1
  swap requires T2382_LIVE_SWAP=1
  forced cap requires T2382_LIVE_FORCED_CAP=1
  claude batch SDK requires T2382_LIVE_CLAUDE_BATCH_SDK=1
USAGE
}

die() {
  echo "[T-2382-live] ERROR: $*" >&2
  exit 1
}

log() {
  echo "[T-2382-live] $*"
}

run_codex_git() {
  git -C "$SOURCE_REPO" "$@"
}

require_reviewed_source() {
  local reviewed="$1"
  [[ "$reviewed" =~ ^[0-9a-f]{40}$ ]] || \
    die "reviewed commit must be a full lowercase SHA-1"

  local top source_head status
  top="$(run_codex_git rev-parse --show-toplevel)" || \
    die "cannot inspect source checkout"
  [[ "$top" == "$SOURCE_REPO" ]] || \
    die "source checkout mismatch: expected $SOURCE_REPO, got $top"

  source_head="$(run_codex_git rev-parse --verify HEAD)" || \
    die "cannot inspect source HEAD"
  [[ "$source_head" == "$reviewed" ]] || \
    die "source HEAD mismatch: reviewed $reviewed, observed $source_head"

  status="$(run_codex_git status --porcelain=v1 --untracked-files=all)" || \
    die "cannot inspect source cleanliness"
  [[ -z "$status" ]] || die "source repo is not clean"
}

observe_leg() {
  local leg="$1" run_id="$2"
  shift 2
  require_run_id "$run_id"
  [[ $# -gt 0 ]] || die "observe requires a phase and its arguments"
  [[ -f "$HELPER" ]] || die "helper missing: $HELPER"
  /usr/bin/python3 "$HELPER" observe \
    --leg "$leg" \
    --run-id "$run_id" \
    --evidence-root "$EVIDENCE_ROOT" \
    "$@"
}

require_live_gate_env() {
  local leg="$1"
  [[ "${T2382_LIVE_ACCEPTANCE:-}" == "1" ]] || \
    die "missing T2382_LIVE_ACCEPTANCE=1"
  case "$leg" in
    test_live_codex_interactive_tmux_quota_and_ack)
      [[ "${T2382_LIVE_CODEX:-}" == "1" ]] || die "missing T2382_LIVE_CODEX=1"
      ;;
    test_live_claude_interactive_tmux_quota_and_ack)
      [[ "${T2382_LIVE_CLAUDE:-}" == "1" ]] || die "missing T2382_LIVE_CLAUDE=1"
      ;;
    test_live_same_unit_planned_harness_swap_preserves_persona_and_tmux)
      [[ "${T2382_LIVE_SWAP:-}" == "1" ]] || die "missing T2382_LIVE_SWAP=1"
      ;;
    test_live_forced_cap_requeues_and_bootstraps_from_chronicle_receipt)
      [[ "${T2382_LIVE_FORCED_CAP:-}" == "1" ]] || die "missing T2382_LIVE_FORCED_CAP=1"
      ;;
    test_live_explicit_claude_batch_sdk_is_nondefault_and_separately_metered)
      [[ "${T2382_LIVE_CLAUDE_BATCH_SDK:-}" == "1" ]] || \
        die "missing T2382_LIVE_CLAUDE_BATCH_SDK=1"
      ;;
    *)
      die "unknown T-2382 live acceptance leg: $leg"
      ;;
  esac
}

require_run_id() {
  local run_id="$1"
  [[ "$run_id" =~ ^[0-9TZ-]+$ ]] || die "run-id must be a bounded UTC timestamp token"
}

init_leg() {
  local leg="$1" run_id="$2"
  require_run_id "$run_id"
  [[ -f "$HELPER" ]] || die "helper missing: $HELPER"
  log "initializing append-only trace for one leg: $leg run $run_id"
  /usr/bin/python3 "$HELPER" init \
    --leg "$leg" \
    --run-id "$run_id" \
    --evidence-root "$EVIDENCE_ROOT" \
    --operator "${USER:-unknown}" \
    --purpose "T-2382 live acceptance"
}

finalize_leg() {
  local leg="$1" reviewed="$2" run_id="$3"
  require_run_id "$run_id"
  [[ -f "$HELPER" ]] || die "helper missing: $HELPER"
  require_reviewed_source "$reviewed"
  require_live_gate_env "$leg"
  log "finalizing one gated leg: $leg at reviewed $reviewed run $run_id"
  /usr/bin/python3 "$HELPER" finalize \
    --leg "$leg" \
    --reviewed-revision "$reviewed" \
    --run-id "$run_id" \
    --evidence-root "$EVIDENCE_ROOT"
}

[[ $# -ge 1 ]] || {
  usage
  exit 1
}

case "$1" in
  init)
    [[ $# -eq 3 ]] || {
      usage
      exit 1
    }
    init_leg "$2" "$3"
    ;;
  observe)
    [[ $# -ge 5 ]] || {
      usage
      exit 1
    }
    leg="$2"
    run_id="$3"
    shift 3
    observe_leg "$leg" "$run_id" "$@"
    ;;
  finalize)
    [[ $# -eq 4 ]] || {
      usage
      exit 1
    }
    finalize_leg "$2" "$3" "$4"
    ;;
  *)
    usage
    exit 1
    ;;
esac
