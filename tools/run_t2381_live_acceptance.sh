#!/usr/bin/env bash
# T-2381 disposable live acceptance — reviewed root/fixture boundary.
#
# EXECUTION IDENTITIES (binding):
#   root        provision synthetic account/mailbox/linger/user manager, capture
#               evidence, validate ownership markers, and perform teardown.
#   fixture uid derive the isolated manifest inside its clone and run the
#               PersonaInstaller plan/apply/verify path against its user manager.
#   codex uid   no mutation; /home/codex/pantheon-config is clone input only.
#
# Commands:
#   sudo tools/run_t2381_live_acceptance.sh all <reviewed-commit>
#   sudo tools/run_t2381_live_acceptance.sh cleanup   # after a failed held run
#
# `all` leaves a failed fixture in place for inspection.  A successful run is
# captured, timer-disabled, and fully removed.  Cleanup refuses unless all three
# root-owned, exact ownership markers agree.  Production instance.yaml is hashed
# before and after and is never edited.  No auth material is copied.
set -euo pipefail

readonly ACCOUNT="t2381-fixture"
readonly PERSONA="t2381-fixture"
readonly GROUP="pantheon-mailbox"
readonly HOME_DIR="/home/t2381-fixture"
readonly REPO_DIR="${HOME_DIR}/pantheon-config"
readonly SOURCE_BUNDLE="${HOME_DIR}/.t2381-reviewed-source.bundle"
readonly SOURCE_REPO="/home/codex/pantheon-config"
readonly SOURCE_MANIFEST="${SOURCE_REPO}/instance.yaml"
readonly FIXTURE_MANIFEST="${REPO_DIR}/.t2381-fixture/instance.yaml"
readonly MAILBOX_DIR="/home/the-ancestor/mailbox/t2381-fixture"
readonly MAILBOX_MARKER="${MAILBOX_DIR}/.t2381-owner.json"
readonly PEER_MAILBOX_DIR="/home/the-ancestor/mailbox/t2381-fixture-peer"
readonly PEER_MAILBOX_MARKER="${PEER_MAILBOX_DIR}/.t2381-owner.json"
readonly CONTROLLER_MARKER="/home/codex/.local/state/pantheon/t2381-live-controller.json"
readonly PROOF_ROOT="/home/codex/.local/state/pantheon/t2381-live-proof"
readonly HELPER="${SOURCE_REPO}/tools/t2381_live_acceptance.py"
readonly TIMER="pantheon-persona-consume@t2381-fixture.timer"
readonly SERVICE="pantheon-persona-consume@t2381-fixture.service"

die() { echo "[T-2381-live] ERROR: $*" >&2; exit 1; }
log() { echo "[T-2381-live] $*"; }

[[ "$(id -u)" -eq 0 ]] || die "run as root; the wrapper has explicit root legs"
[[ $# -ge 1 && $# -le 2 ]] || \
  die "usage: $0 all <reviewed-commit> | $0 cleanup"
readonly COMMAND="$1"
[[ "$COMMAND" == "all" || "$COMMAND" == "cleanup" ]] || die "unknown command: $COMMAND"
if [[ "$COMMAND" == "all" ]]; then
  [[ $# -eq 2 ]] || die "all requires the reviewer-approved commit"
  readonly REVIEWED_REVISION="$2"
  [[ "$REVIEWED_REVISION" =~ ^[0-9a-f]{40}$ ]] || \
    die "reviewed commit must be a full lowercase SHA-1"
else
  [[ $# -eq 1 ]] || die "cleanup does not accept a reviewed commit"
  readonly REVIEWED_REVISION=""
fi
[[ -x /usr/bin/python3 ]] || die "python3 missing"
[[ -x /usr/bin/setfacl ]] || die "setfacl missing"
[[ -x /usr/bin/getfacl ]] || die "getfacl missing"
[[ -f "$HELPER" ]] || die "helper missing: $HELPER"

fixture_uid() { id -u "$ACCOUNT"; }

run_codex_git() {
  runuser -u codex -- env \
    HOME=/home/codex USER=codex LOGNAME=codex \
    git -C "$SOURCE_REPO" "$@"
}

run_fixture() {
  local uid
  uid="$(fixture_uid)"
  runuser -u "$ACCOUNT" -- env \
    "HOME=$HOME_DIR" \
    "USER=$ACCOUNT" \
    "LOGNAME=$ACCOUNT" \
    "XDG_RUNTIME_DIR=/run/user/$uid" \
    "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus" \
    "$@"
}

install_fixture_mailbox_tree() {
  local path="$1" leaf_owner="$2" leaf_mode="$3"
  install -d -o root -g "$GROUP" -m "$leaf_mode" "$path"
  install -d -o "$leaf_owner" -g "$GROUP" -m "$leaf_mode" \
    "$path/tmp" "$path/new" "$path/cur"
}

grant_fixture_peer_acl() {
  local path
  for path in \
    "$PEER_MAILBOX_DIR" \
    "$PEER_MAILBOX_DIR/tmp" \
    "$PEER_MAILBOX_DIR/new" \
    "$PEER_MAILBOX_DIR/cur"; do
    /usr/bin/setfacl -m \
      "u:${ACCOUNT}:rwx,g::---,m::rwx,o::---" "$path"
    /usr/bin/setfacl -d -m \
      "u::rwx,u:${ACCOUNT}:rwx,g::---,m::rwx,o::---" "$path"
  done
}

probe_fixture_mailbox_write() {
  local direction="$1" directory="$2"
  run_fixture /usr/bin/python3 "${REPO_DIR}/tools/t2381_live_acceptance.py" \
    probe-mailbox-write --direction "$direction" --directory "$directory"
}

assert_fixture_mailbox_writes() {
  local proof negative_output negative_rc
  proof="$(proof_dir)"
  negative_output="$proof/peer-negative-write-probe.txt"
  set +e
  probe_fixture_mailbox_write sibling-send "$PEER_MAILBOX_DIR/tmp" \
    >"$negative_output" 2>&1
  negative_rc=$?
  set -e
  [[ "$negative_rc" -ne 0 ]] || \
    die "negative control unexpectedly wrote the ACL-free peer mailbox"
  grep -Fq "direction=sibling-send" "$negative_output" || \
    die "negative control did not identify the sibling-send direction"
  grep -Fq "path=$PEER_MAILBOX_DIR/tmp" "$negative_output" || \
    die "negative control did not identify the peer mailbox path"

  grant_fixture_peer_acl
  {
    probe_fixture_mailbox_write receive "$MAILBOX_DIR/tmp"
    probe_fixture_mailbox_write sibling-send "$PEER_MAILBOX_DIR/tmp"
  } >"$proof/positive-write-probes.jsonl"
}

proof_dir() {
  local run_id
  run_id="$(/usr/bin/python3 "$HELPER" marker-field \
    --path "$CONTROLLER_MARKER" --field run_id)"
  printf '%s/%s\n' "$PROOF_ROOT" "$run_id"
}

assert_production_manifest_unchanged() {
  local proof expected current
  proof="$(proof_dir)"
  expected="$(sed -n 's/^production_instance_sha256=//p' "$proof/preflight.txt")"
  current="$(sha256sum "$SOURCE_MANIFEST" | awk '{print $1}')"
  [[ -n "$expected" && "$expected" == "$current" ]] || \
    die "production instance.yaml changed during the fixture run"
}

provision_fixture() {
  [[ ! -e "$CONTROLLER_MARKER" ]] || die "controller marker already exists; inspect/cleanup first"
  getent passwd "$ACCOUNT" >/dev/null && die "refusing pre-existing Unix account $ACCOUNT"
  [[ ! -e "$HOME_DIR" ]] || die "refusing pre-existing home $HOME_DIR"
  [[ ! -e "$MAILBOX_DIR" ]] || die "refusing pre-existing mailbox $MAILBOX_DIR"
  [[ ! -e "$PEER_MAILBOX_DIR" ]] || die "refusing pre-existing peer mailbox $PEER_MAILBOX_DIR"
  getent group "$GROUP" >/dev/null || die "required group missing: $GROUP"

  local source_branch source_head source_status
  source_branch="$(run_codex_git branch --show-current)" || \
    die "codex could not evaluate the source checkout branch"
  [[ "$source_branch" == "agent/T-2381-live" ]] || \
    die "source branch mismatch: expected agent/T-2381-live, observed ${source_branch:-<detached>}"
  source_head="$(run_codex_git rev-parse --verify HEAD)" || \
    die "codex could not evaluate the source checkout HEAD"
  [[ "$source_head" == "$REVIEWED_REVISION" ]] || \
    die "source HEAD mismatch: reviewed $REVIEWED_REVISION, observed $source_head"
  source_status="$(run_codex_git status --porcelain=v1 --untracked-files=all)" || \
    die "codex could not evaluate source checkout cleanliness"
  [[ -z "$source_status" ]] || die "source repo is not clean"

  local run_id proof uid
  run_id="$(date -u +%Y%m%dT%H%M%SZ)"
  proof="${PROOF_ROOT}/${run_id}"
  install -d -o root -g codex -m 0750 "$PROOF_ROOT"
  install -d -o root -g root -m 0750 "$proof"
  /usr/bin/python3 "$HELPER" write-marker \
    --path "$CONTROLLER_MARKER" --account "$ACCOUNT" \
    --persona "$PERSONA" --run-id "$run_id"
  cp -- "$CONTROLLER_MARKER" "$proof/controller-marker.json"
  {
    echo "production_instance_sha256=$(sha256sum "$SOURCE_MANIFEST" | awk '{print $1}')"
    echo "source_revision=$source_head"
    echo "source_branch=$source_branch"
    echo "reviewed_revision=$REVIEWED_REVISION"
  } >"$proof/preflight.txt"

  # Establish all root-owned deletion authorities before creating the Unix
  # account or any account-owned mailbox leaf.  An interrupted provision can
  # therefore always use the same exact-marker cleanup path.
  install -d -o root -g "$GROUP" -m 3770 "$MAILBOX_DIR"
  /usr/bin/python3 "$HELPER" write-marker \
    --path "$MAILBOX_MARKER" --account "$ACCOUNT" \
    --persona "$PERSONA" --run-id "$run_id"
  install_fixture_mailbox_tree "$PEER_MAILBOX_DIR" root 0700
  /usr/bin/python3 "$HELPER" write-marker \
    --path "$PEER_MAILBOX_MARKER" --account "$ACCOUNT" \
    --persona "$PERSONA" --run-id "$run_id"

  useradd --create-home --user-group --shell /bin/bash \
    --groups "$GROUP" "$ACCOUNT"
  uid="$(fixture_uid)"
  id -nG "$ACCOUNT" | tr ' ' '\n' | grep -Fxq "$GROUP" || \
    die "fixture account lacks $GROUP"
  if id -nG "$ACCOUNT" | tr ' ' '\n' | grep -Fxq "pantheon-shared"; then
    die "fixture account must never join pantheon-shared"
  fi

  install -d -o "$ACCOUNT" -g "$GROUP" -m 2770 \
    "$MAILBOX_DIR/tmp" "$MAILBOX_DIR/new" "$MAILBOX_DIR/cur"
  install -o "$ACCOUNT" -g "$GROUP" -m 0660 /dev/null "$MAILBOX_DIR/.consumer.lock"

  loginctl enable-linger "$ACCOUNT"
  systemctl start "user@${uid}.service"
  systemctl is-active --quiet "user@${uid}.service" || die "fixture user manager is not active"

  # The fixture uid must not open codex's repository directly. Export the
  # already-pinned HEAD as the repository owner, then clone the inert bundle.
  run_codex_git bundle create - HEAD >"$SOURCE_BUNDLE"
  chown "$ACCOUNT:$ACCOUNT" "$SOURCE_BUNDLE"
  chmod 0400 "$SOURCE_BUNDLE"
  run_fixture git clone --no-hardlinks "$SOURCE_BUNDLE" "$REPO_DIR"
  run_fixture git -C "$REPO_DIR" remote remove origin
  run_fixture git -C "$REPO_DIR" config --local --unset-all credential.helper || true
  rm -f -- "$SOURCE_BUNDLE"
  run_fixture /usr/bin/python3 "${REPO_DIR}/tools/t2381_live_acceptance.py" prepare \
    --repo-root "$REPO_DIR" \
    --source-manifest "${REPO_DIR}/instance.yaml" \
    --fixture-manifest "$FIXTURE_MANIFEST" \
    --account "$ACCOUNT" --persona "$PERSONA" \
    --evidence "${HOME_DIR}/prepare-evidence.json"
  assert_fixture_mailbox_writes
  log "fixture provisioned: uid=$uid run_id=$run_id"
}

run_acceptance() {
  run_fixture /usr/bin/python3 "${REPO_DIR}/tools/t2381_live_acceptance.py" run \
    --repo-root "$REPO_DIR" --manifest "$FIXTURE_MANIFEST" \
    --account "$ACCOUNT" --persona "$PERSONA" \
    --report "${HOME_DIR}/live-report.json"
}

capture_proof() {
  local proof receipt_dir
  proof="$(proof_dir)"
  receipt_dir="${HOME_DIR}/.local/state/pantheon/persona-installer/${PERSONA}/receipts"
  cp -- "$MAILBOX_MARKER" "$proof/mailbox-marker.json"
  cp -- "$PEER_MAILBOX_MARKER" "$proof/peer-mailbox-marker.json"
  [[ ! -f "${HOME_DIR}/prepare-evidence.json" ]] || \
    cp -- "${HOME_DIR}/prepare-evidence.json" "$proof/"
  [[ ! -f "${HOME_DIR}/live-report.json" ]] || \
    cp -- "${HOME_DIR}/live-report.json" "$proof/"
  install -d -o root -g root -m 0750 "$proof/receipts"
  if [[ -d "$receipt_dir" ]]; then
    find "$receipt_dir" -maxdepth 1 -type f -exec cp -- {} "$proof/receipts/" \;
  fi
  run_fixture systemctl --user show "$SERVICE" \
    -p ActiveState -p Result -p ExecMainStatus -p InvocationID --no-pager \
    >"$proof/service.show" 2>&1 || true
  run_fixture systemctl --user show "$TIMER" \
    -p ActiveState -p UnitFileState -p NextElapseUSecRealtime --no-pager \
    >"$proof/timer.show" 2>&1 || true
  run_fixture journalctl --user -u "$SERVICE" --no-pager -n 120 \
    >"$proof/service.journal" 2>&1 || true
  run_fixture git -C "$REPO_DIR" status --short >"$proof/clone-status.txt"
  run_fixture git -C "$REPO_DIR" remote >"$proof/clone-remotes.txt"
  run_fixture git -C "$REPO_DIR" config --show-origin --get-all credential.helper \
    >"$proof/credential-helpers.txt" 2>&1 || true
  id "$ACCOUNT" >"$proof/fixture-id.txt"
  /usr/bin/getfacl -cp \
    "$PEER_MAILBOX_DIR" \
    "$PEER_MAILBOX_DIR/tmp" \
    "$PEER_MAILBOX_DIR/new" \
    "$PEER_MAILBOX_DIR/cur" >"$proof/fixture-peer-acl.txt"
  /usr/bin/getfacl -cp \
    /home/the-ancestor/mailbox/orchestrator \
    /home/the-ancestor/mailbox/orchestrator/tmp \
    /home/the-ancestor/mailbox/orchestrator/new \
    /home/the-ancestor/mailbox/orchestrator/cur >"$proof/durable-peer-acl-reference.txt"
  [[ ! -e "${HOME_DIR}/.codex/auth.json" ]] || die "auth material appeared in fixture home"
  assert_production_manifest_unchanged
  chown -R codex:codex "$proof"
  chmod -R go-rwx "$proof"
  log "proof captured: $proof"
}

validate_cleanup() {
  [[ "$MAILBOX_DIR" == "/home/the-ancestor/mailbox/t2381-fixture" ]] || \
    die "mailbox cleanup target changed"
  [[ "$PEER_MAILBOX_DIR" == "/home/the-ancestor/mailbox/t2381-fixture-peer" ]] || \
    die "peer mailbox cleanup target changed"
  [[ "$HOME_DIR" == "/home/t2381-fixture" ]] || die "home cleanup target changed"
  /usr/bin/python3 "$HELPER" check-cleanup \
    --controller-marker "$CONTROLLER_MARKER" \
    --mailbox-marker "$MAILBOX_MARKER" \
    --account "$ACCOUNT" --persona "$PERSONA" >/dev/null
  /usr/bin/python3 "$HELPER" check-cleanup \
    --controller-marker "$CONTROLLER_MARKER" \
    --mailbox-marker "$PEER_MAILBOX_MARKER" \
    --account "$ACCOUNT" --persona "$PERSONA" >/dev/null
}

cleanup_fixture() {
  validate_cleanup
  local uid proof account_exists
  proof="$(proof_dir)"
  account_exists=false
  if getent passwd "$ACCOUNT" >/dev/null; then
    account_exists=true
    uid="$(fixture_uid)"
    run_fixture systemctl --user disable --now "$TIMER" >/dev/null 2>&1 || true
    run_fixture systemctl --user stop "$SERVICE" >/dev/null 2>&1 || true
    loginctl disable-linger "$ACCOUNT"
    systemctl stop "user@${uid}.service"
    if systemctl is-active --quiet "user@${uid}.service"; then
      die "fixture user manager remained active after stop"
    fi
    userdel --remove "$ACCOUNT"
  fi
  if [[ -e "$HOME_DIR" ]]; then
    rm -rf --one-file-system -- "$HOME_DIR"
  fi
  rm -rf --one-file-system -- "$MAILBOX_DIR"
  rm -rf --one-file-system -- "$PEER_MAILBOX_DIR"
  rm -f -- "$CONTROLLER_MARKER"
  {
    echo "cleanup=complete"
    echo "account_existed=$account_exists"
    echo "account_removed=true"
    echo "mailbox_removed=true"
    echo "peer_mailbox_removed=true"
    echo "linger_disabled_before_manager_stop=true"
  } >"$proof/cleanup.txt"
  chown codex:codex "$proof/cleanup.txt"
  chmod 0600 "$proof/cleanup.txt"
  log "fixture account/home/mailbox removed; only redacted proof remains at $proof"
}

if [[ "$COMMAND" == "cleanup" ]]; then
  cleanup_fixture
  exit 0
fi

provision_fixture
set +e
run_acceptance
run_rc=$?
set -e
capture_proof
if [[ "$run_rc" -ne 0 ]]; then
  log "acceptance failed; fixture intentionally held for inspection"
  log "after inspection run: sudo $0 cleanup"
  exit "$run_rc"
fi
cleanup_fixture
log "T-2381 disposable live acceptance complete"
