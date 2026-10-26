#!/bin/bash
# T-2693 (ORG-087 sec. 4 step 3): move the live mailbox hub from
# /home/the-ancestor/mailbox to /home/post-office/mailbox. Recovery-executed and
# operator-notified at start and completion. Run as claude-fallback: every
# cross-account step goes through `sudo -n -u <account>`.
#
# Stages (each prints what it would do under --dry-run). CUT ORDER:
#   1. preflight    the new hub root is owned by post-office, sudo works, the new hub is empty
#   2. copy         tar --acls: read the live hub as the-ancestor, extract as post-office.
#                   The "." entry carries the live root's 0710 mode + named ACLs.
#   3. verify-copy  compare the dir tree + ACLs of both hubs (owner lines normalised)
#   4. configs      add the new hub roots to codex writable_roots + suborch1 Read perms (old
#                   kept until phase 7), via the fail-closed tools/t2693_config_roots.py.
#                   THEN restart the long-lived codex sessions (consultant's codex-session, specialist's
#                   seat): codex reads writable_roots at session start. They keep working on
#                   the old hub until the flip.
#   5. (push the flip commit)   instance.yaml hub/hub_owner + fallback + unit templates
#   6. pull         git pull --ff-only in every account's pantheon-config clone
#   7. units        re-install the three hub-bearing unit templates wherever an installed
#                   copy exists; daemon-reload; restart active wake .path units
#   8. (restart import-time holders)   specialist-a2a.service; orchestrator + archivist mailbox MCP
#   9. cut          make the OLD hub NON-maildir (rename each new/ to $MARK) so a stale
#                   sender raises (Shape A, tools/verify_hub_cut.py), then sync post-copy
#                   arrivals across and ack-reconcile post-copy consumes
#  10. verify       verify_hub_cut.py on the old hub, a negative send to the old path,
#                   exists() on every new inbox; then a per-persona round trip
#   rollback        rename $MARK back to new/ and carry the new hub's traffic back (acks and
#                   unread); then git revert the flip, push, and re-run pull + units
#
# The copy is small (the hub is maildir text) and each stage is idempotent, so a
# stage can be re-run after fixing whatever stopped it.
set -uo pipefail

# Overridable only so the rehearsal (tools/test_t2693_hub_cutover.sh) can run the
# real copy/cut/rollback logic on temp hubs owned by the invoking account.
OLD="${T2693_OLD:-/home/the-ancestor/mailbox}"
NEW="${T2693_NEW:-/home/post-office/mailbox}"
OLD_OWNER="${T2693_OLD_OWNER:-the-ancestor}"
NEW_OWNER="${T2693_NEW_OWNER:-post-office}"
# Every live hub entry is group pantheon-mailbox (verified 2026-09-28: 6224/6224). Non-root
# tar cannot restore group ownership, so the copy re-applies it.
GROUP="${T2693_GROUP:-pantheon-mailbox}"
MARK=new.pre-post-office-cut
ACCOUNTS=(claude claude-research claude-fallback codex exocortex suborch1 suborch2 the-ancestor)
HUB_UNITS=(pantheon-mailbox-wake@.path pantheon-persona-consume@.service pantheon-persona-quota@.service)
CODEX_CONFIGS=(codex claude-research)       # accounts whose ~/.codex/config.toml lists hub writable_roots
DRY_RUN=0

log() { echo "[t2693] $*"; }
die() { echo "[t2693] ERROR: $*" >&2; exit 1; }
run() { if (( DRY_RUN )); then echo "  DRY: $*"; else "$@"; fi; }
as()  { local u="$1"; shift; if [[ "$u" == "$(id -un)" ]]; then bash -c "$*"; else sudo -n -u "$u" bash -c "$*"; fi; }
su_do() { local u="$1"; shift; if [[ "$u" == "$(id -un)" ]]; then "$@"; else sudo -n -u "$u" "$@"; fi; }
run_as() { local u="$1"; shift; if (( DRY_RUN )); then echo "  DRY[$u]: $*"; else as "$u" "$*"; fi; }

# Recipient dirs of the OLD hub (listed AS the owner: the hub is traverse-only to the group).
old_recipients() { as "$OLD_OWNER" "find $OLD -mindepth 1 -maxdepth 1 -type d -printf '%f\n'" | sort; }

stage_preflight() {
  local owner; owner=$(stat -c %U "$NEW") || die "cannot stat $NEW"
  [[ "$owner" == "$NEW_OWNER" ]] || die "$NEW is owned by $owner, not $NEW_OWNER (root chown pending)"
  su_do "$OLD_OWNER" true || die "no sudo to $OLD_OWNER"
  su_do "$NEW_OWNER" true || die "no sudo to $NEW_OWNER"
  local n; n=$(as "$NEW_OWNER" "find $NEW -mindepth 1 | wc -l")
  log "new hub entries: $n (0 expected before copy)"
  log "old recipients: $(old_recipients | tr '\n' ' ')"
  log "preflight OK"
}

stage_copy() {
  stage_preflight
  local n; n=$(as "$NEW_OWNER" "find $NEW -mindepth 1 | wc -l")
  (( n == 0 )) || die "new hub is not empty ($n entries); refusing to copy over it"
  # A file the hub owner cannot read (live: one 0600 codex-owned .delivery receipt
  # sidecar in recovery's inbox) is left out of the bulk tar and carried AS ITS OWN
  # OWNER. An unreadable DIRECTORY is not handled: stop and look.
  local bad_dirs; bad_dirs=$(as "$OLD_OWNER" "cd $OLD && find . ! -readable -type d" | wc -l)
  (( bad_dirs == 0 )) || die "$bad_dirs dir(s) unreadable by $OLD_OWNER; resolve by hand"
  local -a skip_owner=() skip_path=() excludes=()
  local owner path
  while IFS=$'\t' read -r owner path; do
    [[ -n "$path" ]] || continue
    skip_owner+=("$owner"); skip_path+=("$path"); excludes+=("--exclude=$path")
  done < <(as "$OLD_OWNER" "cd $OLD && find . ! -readable -type f -printf '%u\t%p\n'")
  log "copy: $OLD -> $NEW (tar --acls, read as $OLD_OWNER, extract as $NEW_OWNER); ${#skip_path[@]} file(s) carried by their owners"
  if (( DRY_RUN )); then
    echo "  DRY: sudo -u $OLD_OWNER tar --acls ${excludes[*]} -C $OLD -cpf - . | sudo -u $NEW_OWNER tar --acls -C $NEW -xpf -"
    local i; for i in "${!skip_path[@]}"; do echo "  DRY: carry ${skip_path[$i]} as ${skip_owner[$i]}"; done
    return 0
  fi
  su_do "$OLD_OWNER" tar --acls "${excludes[@]}" -C "$OLD" -cpf - . \
    | su_do "$NEW_OWNER" tar --acls -C "$NEW" -xpf - \
    || die "tar copy failed"
  local i
  for i in "${!skip_path[@]}"; do
    su_do "${skip_owner[$i]}" tar --acls -C "$OLD" -cpf - -- "${skip_path[$i]}" \
      | su_do "$NEW_OWNER" tar --acls -C "$NEW" -xpf - \
      || die "carrying ${skip_path[$i]} as ${skip_owner[$i]} failed"
    log "carried ${skip_path[$i]} as ${skip_owner[$i]} (now owned by $NEW_OWNER)"
  done
  su_do "$NEW_OWNER" chgrp -R -h "$GROUP" "$NEW" || die "chgrp -R $GROUP on $NEW failed"
  log "copy done (group $GROUP re-applied)"
}

# Dir-level ACL listing with the hub prefix and owner lines normalised away.
acl_dump() { local u="$1" hub="$2"; as "$u" "cd $hub && find . -type d -print0 | sort -z | xargs -0 getfacl -p 2>/dev/null" | grep -v '^# owner:'; }

stage_verify_copy() {
  local a b; a=$(mktemp); b=$(mktemp)
  acl_dump "$OLD_OWNER" "$OLD" > "$a"; acl_dump "$NEW_OWNER" "$NEW" > "$b"
  # The hub root's own "user:<owner>" named entry cannot exist on the new side
  # for the-ancestor-as-named-user rows; those are carried verbatim, so a
  # difference here is a real divergence.
  if diff -u "$a" "$b" > "$a.diff"; then log "verify-copy: dir tree + ACLs IDENTICAL ($(grep -c '^# file:' "$a") dirs)"
  else log "verify-copy: DIVERGENCE:"; head -60 "$a.diff"; rm -f "$a" "$b" "$a.diff"; return 1; fi
  local fo fn
  fo=$(as "$OLD_OWNER" "find $OLD -type f | wc -l"); fn=$(as "$NEW_OWNER" "find $NEW -type f | wc -l")
  log "files: old=$fo new=$fn (live traffic can move these between the two counts)"
  rm -f "$a" "$b" "$a.diff"
}

stage_pull() {
  local u
  for u in "${ACCOUNTS[@]}"; do
    run_as "$u" "cd ~/pantheon-config && git pull -q --ff-only && echo \"$u: \$(git log --oneline -1)\"" \
      || log "WARN: pull failed for $u (dirty or diverged clone) -- reconcile by hand"
  done
}

stage_units() {
  local u unit
  for u in "${ACCOUNTS[@]}"; do
    for unit in "${HUB_UNITS[@]}"; do
      # Only accounts that already carry an installed copy; never add new units.
      # A symlinked unit follows the clone, so pull alone updated it.
      run_as "$u" "d=~/.config/systemd/user; f=\$d/$unit; src=~/pantheon-config/tools/systemd/$unit; \
        if [ -L \"\$f\" ]; then echo \"$u: $unit is a symlink (pull updated it)\"; \
        elif [ -f \"\$f\" ]; then install -m 0644 \"\$src\" \"\$f\" && echo \"$u: installed $unit\"; fi"
    done
    run_as "$u" "XDG_RUNTIME_DIR=/run/user/\$(id -u) systemctl --user daemon-reload; \
      for p in \$(XDG_RUNTIME_DIR=/run/user/\$(id -u) systemctl --user list-units --state=active --plain --no-legend 'pantheon-mailbox-wake@*.path' | awk '{print \$1}'); do \
        XDG_RUNTIME_DIR=/run/user/\$(id -u) systemctl --user restart \"\$p\" && echo \"$u: restarted \$p\"; done"
  done
}

stage_cut() {
  local r
  log "cut: making $OLD non-maildir (new/ -> $MARK)"
  for r in $(old_recipients); do
    run_as "$OLD_OWNER" "[ -d $OLD/$r/new ] && mv $OLD/$r/new $OLD/$r/$MARK && echo 'neutralised $r' || echo 'skip $r (no new/)'"
  done
  log "cut: syncing post-copy arrivals + reconciling post-copy acks"
  local tmpd; tmpd=$(mktemp -d)
  for r in $(old_recipients); do
    # One listing per side per dir (cur/ holds thousands of acked messages), then set ops.
    as "$OLD_OWNER" "ls -1 $OLD/$r/$MARK 2>/dev/null" | sort > "$tmpd/old_new"
    as "$OLD_OWNER" "ls -1 $OLD/$r/cur 2>/dev/null" | sort > "$tmpd/old_cur"
    as "$NEW_OWNER" "ls -1 $NEW/$r/new 2>/dev/null" | sort > "$tmpd/new_new"
    as "$NEW_OWNER" "ls -1 $NEW/$r/cur 2>/dev/null" | sort > "$tmpd/new_cur"
    sort -u "$tmpd/new_new" "$tmpd/new_cur" > "$tmpd/new_all"
    # Arrivals: in the old (renamed) new/ but in neither new/ nor cur/ of the new hub.
    mapfile -t todo < <(comm -23 "$tmpd/old_new" "$tmpd/new_all")
    if (( ${#todo[@]} )); then
      log "$r: carrying ${#todo[@]} post-copy arrival(s) to the new hub"
      if (( DRY_RUN )); then echo "  DRY: ${todo[*]}"; else
        su_do "$OLD_OWNER" tar --acls -C "$OLD/$r/$MARK" -cpf - -- "${todo[@]}" \
          | su_do "$NEW_OWNER" tar --acls -C "$NEW/$r/new" -xpf - || log "WARN: arrival sync failed for $r"
      fi
    fi
    # Acks: consumed from the OLD hub after the copy, so still unread in the new one.
    mapfile -t acked < <(comm -12 "$tmpd/old_cur" "$tmpd/new_new")
    if (( ${#acked[@]} )); then
      log "$r: ack-reconciling ${#acked[@]} message(s) consumed on the old hub after the copy"
      printf '%s\n' "${acked[@]}" > "$tmpd/acked"
      if (( DRY_RUN )); then echo "  DRY: ${acked[*]}"; else
        su_do "$NEW_OWNER" bash -c "cd $NEW/$r && while IFS= read -r n; do [ -e \"cur/\$n\" ] || mv -- \"new/\$n\" \"cur/\$n\"; done" < "$tmpd/acked"
      fi
    fi
    # History: arrived AND acked on the old hub inside the copy->cut window, so it is
    # in neither side of the new hub. Carry it into the new cur/ so the record (and
    # kino's mailbox stats, which count it) stays complete.
    sort -u "$tmpd/new_new" "$tmpd/new_cur" > "$tmpd/new_all"
    mapfile -t hist < <(comm -23 "$tmpd/old_cur" "$tmpd/new_all")
    if (( ${#hist[@]} )); then
      log "$r: carrying ${#hist[@]} message(s) acked on the old hub in the copy->cut window into the new cur/"
      if (( DRY_RUN )); then echo "  DRY: ${hist[*]}"; else
        su_do "$OLD_OWNER" tar --acls -C "$OLD/$r/cur" -cpf - -- "${hist[@]}" \
          | su_do "$NEW_OWNER" tar --acls -C "$NEW/$r/cur" -xpf - || log "WARN: history carry failed for $r"
        su_do "$NEW_OWNER" chgrp -h "$GROUP" -- "${hist[@]/#/$NEW/$r/cur/}" 2>/dev/null || true
      fi
    fi
  done
  rm -rf "$tmpd"
  log "cut done"
}

stage_configs() {
  # Fail-closed helper (tools/t2693_config_roots.py): run AS each account from its own
  # clone (so run after `pull`), so the writable-root access check is that account's.
  local u
  for u in "${CODEX_CONFIGS[@]}"; do
    run_as "$u" "python3 ~/pantheon-config/tools/t2693_config_roots.py codex-toml ~/.codex/config.toml" \
      || die "codex config for $u NOT updated (left unchanged) -- fix before relying on codex mail"
  done
  run_as suborch1 "python3 ~/pantheon-config/tools/t2693_config_roots.py claude-settings ~/.claude/settings.local.json" \
    || log "WARN: suborch1 settings.local.json not updated"
}

stage_verify() {
  local repo rc=0; repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  python3 "$repo/tools/verify_hub_cut.py" --old-hub "$OLD" || { log "FAIL: old hub still maildir-shaped"; rc=1; }
  python3 - "$repo" "$OLD" "$NEW" <<'PY' || rc=1
import sys
sys.path.insert(0, sys.argv[1] + '/tools')
import mailbox
old, new = sys.argv[2], sys.argv[3]
try:
    mailbox.send(old + '/recovery', 'T-2693 negative probe', 'recovery', subject='t2693-negative-probe')
    print('[t2693] FAIL: send to the OLD hub returned an id (Shape B)'); sys.exit(1)
except mailbox.MailboxNotFound:
    print('[t2693] negative send to the old hub RAISED (Shape A) -- OK')
bad = [p for p in ('orchestrator', 'specialist', 'specialist-a2a', 'recovery', 'codex', 'archivist', 'mason', 'artisan', 'the-ancestor')
       if not mailbox.exists(new + '/' + p)]
print('[t2693] new-hub inboxes missing:', bad or 'none')
sys.exit(1 if bad else 0)
PY
  return $rc
}

stage_rollback() {
  # Undo the cut without losing or replaying mail (consultant review): restore the old new/,
  # then carry the new hub's traffic back. For each recipient:
  #   acked on the new hub, and still unread in the old new/  -> ack it there (move to cur/)
  #   acked on the new hub, and never on the old hub          -> copy into the old cur/
  #   unread on the new hub, and on neither old side          -> copy into the old new/
  local r tmpd; tmpd=$(mktemp -d)
  for r in $(old_recipients); do
    run_as "$OLD_OWNER" "[ -d $OLD/$r/$MARK ] && [ ! -e $OLD/$r/new ] && mv $OLD/$r/$MARK $OLD/$r/new && echo 'restored $r' || true"
    as "$OLD_OWNER" "ls -1 $OLD/$r/new 2>/dev/null; ls -1 $OLD/$r/$MARK 2>/dev/null" | sort -u > "$tmpd/old_new"
    as "$OLD_OWNER" "ls -1 $OLD/$r/cur 2>/dev/null" | sort > "$tmpd/old_cur"
    as "$NEW_OWNER" "ls -1 $NEW/$r/new 2>/dev/null" | sort > "$tmpd/new_new"
    as "$NEW_OWNER" "ls -1 $NEW/$r/cur 2>/dev/null" | sort > "$tmpd/new_cur"
    sort -u "$tmpd/old_new" "$tmpd/old_cur" > "$tmpd/old_all"
    comm -23 "$tmpd/new_cur" "$tmpd/old_cur" > "$tmpd/acked_new"
    mapfile -t ack_there < <(comm -12 "$tmpd/acked_new" "$tmpd/old_new")
    mapfile -t cur_back < <(comm -23 "$tmpd/acked_new" "$tmpd/old_new")
    mapfile -t new_back < <(comm -23 "$tmpd/new_new" "$tmpd/old_all")
    log "$r: ack ${#ack_there[@]} in old new/, copy ${#cur_back[@]} to old cur/, ${#new_back[@]} to old new/"
    (( DRY_RUN )) && continue
    if (( ${#ack_there[@]} )); then
      printf '%s\n' "${ack_there[@]}" | su_do "$OLD_OWNER" bash -c "cd $OLD/$r && while IFS= read -r n; do [ -e \"new/\$n\" ] && [ ! -e \"cur/\$n\" ] && mv -- \"new/\$n\" \"cur/\$n\"; done; true"
    fi
    if (( ${#cur_back[@]} )); then
      su_do "$NEW_OWNER" tar --acls -C "$NEW/$r/cur" -cpf - -- "${cur_back[@]}" \
        | su_do "$OLD_OWNER" tar --acls -C "$OLD/$r/cur" -xpf - || log "WARN: cur carry-back failed for $r"
    fi
    if (( ${#new_back[@]} )); then
      su_do "$NEW_OWNER" tar --acls -C "$NEW/$r/new" -cpf - -- "${new_back[@]}" \
        | su_do "$OLD_OWNER" tar --acls -C "$OLD/$r/new" -xpf - || log "WARN: new carry-back failed for $r"
    fi
  done
  rm -rf "$tmpd"
  log "rollback of the hub dirs done; now git revert the cut commit, push, then re-run: pull, units"
}

[[ $# -ge 1 ]] || { sed -n '2,25p' "$0"; exit 64; }
stage="$1"; shift
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1
case "$stage" in
  preflight|copy|pull|units|cut|configs|verify|rollback) "stage_$stage" ;;
  verify-copy) stage_verify_copy ;;
  *) echo "unknown stage: $stage" >&2; exit 64 ;;
esac
