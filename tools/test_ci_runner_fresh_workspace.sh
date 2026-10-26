#!/usr/bin/env bash
# test_ci_runner_fresh_workspace.sh - T-4416: the CI job-started hook
# (ci_runner_hooks/job-started-fresh-workspace.sh) empties the job's workspace and nothing
# else, and only in enforce. It refuses anything that is not a canonical
# <ROOT>/actions-runner-*/_work/<repo>/<repo>, and it fails closed: a missing or garbage
# conf means enforce, and in enforce every refusal fails the job.
#
# It runs a COPY of the hook with CONF and ROOT rewritten to a temp tree. Every workspace
# a case points at lives under that tree, so a broken path guard can only ever delete temp
# files, never /opt, /etc or $HOME.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/ci_runner_hooks/job-started-fresh-workspace.sh"
T="$(realpath "$(mktemp -d)")"
# Every path this suite deletes is under $T: never run on without one.
[[ -n "$T" && -d "$T" && "$T" != / ]] || { echo "FAIL: could not create a temp dir"; exit 1; }
trap 'chmod -R u+rwx "$T" 2>/dev/null; rm -rf "$T"' EXIT
ROOT="$T/opt"; CONF="$T/fresh-workspace.conf"; HOOK="$T/hook.sh"; LOG="$T/logger.log"
HROOT="$T/home"
mkdir -p "$ROOT" "$T/bin"
# A stub logger: the suite writes nothing to the journal, and it can see the syslog tag the
# rollout reads (journalctl -t pantheon-fresh-workspace).
printf '#!/bin/sh\necho "$*" >> "%s"\n' "$LOG" > "$T/bin/logger"; chmod +x "$T/bin/logger"
# T-4425 stubs. getent answers "passwd <uid>" with the one line in $T/passwd (or fails when
# $T/getent-fail exists); id -u prints $T/fake-uid when it exists. So a case can give the hook
# any passwd home, or a uid that does not own that home, without touching /etc/passwd.
REAL_ID="$(command -v id)"
printf '#!/bin/sh\n[ "$1" = passwd ] || exit 2\n[ -e "%s/getent-fail" ] && exit 2\ncat "%s/passwd"\n' "$T" "$T" \
  > "$T/bin/getent"
printf '#!/bin/sh\nif [ "$1" = -u ] && [ -e "%s/fake-uid" ]; then cat "%s/fake-uid"; exit 0; fi\nexec "%s" "$@"\n' \
  "$T" "$T" "$REAL_ID" > "$T/bin/id"
chmod +x "$T/bin/getent" "$T/bin/id"

sed -e "s#^CONF=/etc/pantheon-ci/fresh-workspace\.conf\$#CONF=$CONF#" \
    -e "s#^ROOT=/opt\$#ROOT=$ROOT#" -e "s#^HOME_ROOT=/home\$#HOME_ROOT=$HROOT#" "$SRC" > "$HOOK"
if ! grep -qxF "CONF=$CONF" "$HOOK" || ! grep -qxF "ROOT=$ROOT" "$HOOK" || ! grep -qxF "HOME_ROOT=$HROOT" "$HOOK" \
   || grep -v '^[[:space:]]*#' "$HOOK" | grep -qE '/etc/pantheon-ci|=/opt([^a-z]|$)|=/home([^a-z]|$)'; then
  echo "FAIL: could not point the hook copy at the temp tree (did the CONF=, ROOT= or HOME_ROOT= line change shape?)"
  exit 1
fi

pass=0; fail=0
check() { # <name> <got> <want>
  if [ "$2" = "$3" ]; then pass=$((pass + 1)); else fail=$((fail + 1)); echo "FAIL: [$INV] $1 (got '$2', want '$3')"; fi
}
set_mode() { # <conf body> | --none
  if [ "$1" = --none ]; then rm -f "$CONF"; else printf '%s\n' "$1" > "$CONF"; fi
}
hook() { # <workspace> | --unset : runs the hook copy; sets $rc and $out
  if [ "$1" = --unset ]; then
    out="$(env -u GITHUB_WORKSPACE PATH="$T/bin:$PATH" "${HSH[@]}" "$HOOK" 2>&1)"; rc=$?
  else
    out="$(env GITHUB_WORKSPACE="$1" PATH="$T/bin:$PATH" "${HSH[@]}" "$HOOK" 2>&1)"; rc=$?
  fi
}
populate() { # <dir>: 4 top-level entries, including a dotfile, .git and a dangling symlink
  mkdir -p "$1/.git/objects" "$1/tools"
  echo stale > "$1/tools/x.py"; echo stale > "$1/.hidden"; echo ref > "$1/.git/HEAD"
  ln -s "$T/does-not-exist" "$1/dangling"
}
mkws() { # <runner> <repo>: a populated workspace and the runner's siblings; prints its path
  local work="$ROOT/actions-runner-$1/_work"
  mkdir -p "$work/$2/pytest-basetemp/keep" "$work/_tool/keep" "$work/_actions/keep"
  populate "$work/$2/$2"
  echo "$work/$2/$2"
}
count() { find "$1" -mindepth 1 2>/dev/null | wc -l | tr -d ' '; }
has() { grep -cF -e "$2" <<<"$1"; } # <text> <literal>: prints the matching line count
# git for fixtures (never the repo's hooks) and GV for checks (the repo's OWN config and hooks
# apply, so a poison the hook failed to remove fires and leaves $T/pwned behind).
genv() { env GIT_CONFIG_NOSYSTEM=1 HOME="$T/githome" GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t \
  GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t "$@"; }
G() { genv git -c core.hooksPath=/dev/null "$@"; }
GV() { genv git "$@"; }
mkrepo_ws() { # <runner> <repo>: a real checked-out repo whose .git carries the poisons; prints its path
  local ws="$ROOT/actions-runner-$1/_work/$2/$2"
  mkdir -p "$ws" "$T/githome"
  G init -q -b main "$ws"
  echo v1 > "$ws/tracked.txt"; G -C "$ws" add tracked.txt; G -C "$ws" commit -q -m one
  G -C "$ws" remote add origin https://example.invalid/not-the-origin
  G -C "$ws" pack-refs --all
  # Poison last, so no fixture command runs it: an fsmonitor and a hook that mark $T/pwned,
  # and an alternates file pointing at an outside object dir.
  printf '#!/bin/sh\necho fsmonitor >> %s/pwned\n' "$T" > "$T/evil-fsmonitor"; chmod +x "$T/evil-fsmonitor"
  G -C "$ws" config core.fsmonitor "$T/evil-fsmonitor"
  printf '#!/bin/sh\necho post-checkout >> %s/pwned\n' "$T" > "$ws/.git/hooks/post-checkout"
  chmod +x "$ws/.git/hooks/post-checkout"
  mkdir -p "$T/evil-objects"; echo "$T/evil-objects" > "$ws/.git/objects/info/alternates"
  echo junk > "$ws/untracked.txt"; mkdir -p "$ws/.hidden-dir"
  echo "$ws"
}
objects_in() { find "$1/.git/objects" -type f ! -path '*/info/*' | wc -l | tr -d ' '; }
TEMPLATE_URL_BASE=https://github.com/example-org

cases() {
# 1. enforce clears every entry (dotfiles, .git, symlinks) and nothing outside the workspace
set_mode "MODE=enforce"
ws=$(mkws pc pantheon-config)
mkdir -p "$T/outside"; echo keep > "$T/outside/f"; ln -s "$T/outside" "$ws/link-out"
hook "$ws"
check "enforce exits 0" "$rc" "0"
check "enforce leaves the workspace empty" "$(count "$ws")" "0"
check "enforce keeps the workspace dir itself" "$([ -d "$ws" ] && echo yes)" "yes"
check "the sibling pytest-basetemp survives" "$(count "$ROOT/actions-runner-pc/_work/pantheon-config/pytest-basetemp")" "1"
check "_tool and _actions survive" "$(count "$ROOT/actions-runner-pc/_work/_tool")$(count "$ROOT/actions-runner-pc/_work/_actions")" "11"
check "a symlink's target outside the workspace survives" "$(cat "$T/outside/f")" "keep"
check "enforce reports the count it cleared" "$(has "$out" "cleared 5 entries from $ws (no reusable git store, .git removed)")" "1"
check "the report reaches syslog under the rollout's tag" \
  "$(has "$(cat "$LOG")" "-t pantheon-fresh-workspace -- [fresh-workspace T-4416 enforce] cleared 5 entries")" "1"

# 2. observe reports and removes nothing
set_mode "MODE=observe"
ws=$(mkws pco pantheon-config)
hook "$ws"
check "observe exits 0" "$rc" "0"
check "observe removes nothing" "$(count "$ws")" "7"
check "observe says what it would clear" "$(has "$out" "would clear 4 entries from $ws (no reusable git store, .git removed; observe mode")" "1"

# 3. no workspace: a refusal, which fails the job only in enforce
set_mode "MODE=enforce"
hook --unset; check "enforce: unset GITHUB_WORKSPACE exits 1" "$rc" "1"
check "enforce: unset GITHUB_WORKSPACE says REFUSED" "$(has "$out" "REFUSED: GITHUB_WORKSPACE is unset")" "1"
hook ""; check "enforce: empty GITHUB_WORKSPACE exits 1" "$rc" "1"
set_mode "MODE=observe"
hook --unset; check "observe: unset GITHUB_WORKSPACE exits 0" "$rc" "0"
check "observe: unset GITHUB_WORKSPACE still says REFUSED" "$(has "$out" "REFUSED")" "1"

# 4. anything but <ROOT>/actions-runner-*/_work/<repo>/<repo> is refused and left intact
set_mode "MODE=enforce"
ws=$(mkws pcr pantheon-config)
populate "$ROOT/not-a-runner/_work/r/r"
populate "$T/nest$ROOT/actions-runner-pcr/_work/r/r"
populate "$ROOT/actions-runner-pcr/_work/a/b"
for bad in \
  "$ROOT/actions-runner-pcr/_work/pantheon-config" \
  "$ws/tools" \
  "$ROOT/not-a-runner/_work/r/r" \
  "$T/nest$ROOT/actions-runner-pcr/_work/r/r" \
  "$ROOT/actions-runner-pcr/_work/a/b"; do
  before=$(count "$bad")
  hook "$bad"
  check "refused, exit 1: ${bad#"$T"}" "$rc" "1"
  check "refused, says REFUSED: ${bad#"$T"}" "$(has "$out" "REFUSED")" "1"
  check "refused, left intact: ${bad#"$T"}" "$(count "$bad")" "$before"
done
check "the sibling basetemp survives the too-shallow refusal" \
  "$(count "$ROOT/actions-runner-pcr/_work/pantheon-config/pytest-basetemp")" "1"

# 5. a non-canonical path is refused: a symlink in ANY component, or a ".."
victim=$(mkws victim r)
mkdir -p "$ROOT/actions-runner-ln/_work/r"
ln -s "$victim" "$ROOT/actions-runner-ln/_work/r/r"
ln -s "$ROOT/actions-runner-victim" "$ROOT/actions-runner-parentln"
for bad in \
  "$ROOT/actions-runner-ln/_work/r/r" \
  "$ROOT/actions-runner-parentln/_work/r/r" \
  "$ROOT/actions-runner-victim/_work/r/../r/r"; do
  hook "$bad"
  check "non-canonical, exit 1: ${bad#"$T"}" "$rc" "1"
  check "non-canonical, says not canonical: ${bad#"$T"}" "$(has "$out" "is not canonical")" "1"
  check "non-canonical, the real workspace is intact: ${bad#"$T"}" "$(count "$victim")" "7"
done

# 6. mode fails closed: no conf, an empty conf, or a garbage MODE all mean enforce
for conf in --none "" "MODE=obsrve" "MODE=OBSERVE" "MODE=observe
MODE=enforce"; do
  set_mode "$conf"
  ws=$(mkws fc pantheon-config)
  hook "$ws"
  label="$(printf '%s' "$conf" | tr '\n' ';')"
  check "conf '$label' is enforce: exit 0" "$rc" "0"
  check "conf '$label' is enforce: workspace cleared" "$(count "$ws")" "0"
done

# 7. the job's environment cannot turn enforcement off or move the guard
set_mode --none
printf 'MODE=observe\n' > "$T/hostile.conf"
ws=$(mkws envpin pantheon-config)
out="$(env GITHUB_WORKSPACE="$ws" CONF="$T/hostile.conf" MODE=observe ROOT="$T/nowhere" mode=observe \
  PATH="$T/bin:$PATH" "${HSH[@]}" "$HOOK" 2>&1)"; rc=$?
check "hostile env: still enforce, exit 0" "$rc" "0"
check "hostile env: still enforce, workspace cleared" "$(count "$ws")" "0"

# 8. nothing to do is success, in enforce too
set_mode "MODE=enforce"
hook "$ROOT/actions-runner-absent/_work/r/r"
check "absent workspace exits 0" "$rc" "0"
check "absent workspace says so" "$(has "$out" "absent; nothing to clear")" "1"
mkdir -p "$ROOT/actions-runner-empty/_work/r/r"
hook "$ROOT/actions-runner-empty/_work/r/r"
check "already-empty workspace exits 0" "$rc" "0"
check "already-empty workspace says so" "$(has "$out" "already empty")" "1"

# 9. a workspace that is a file, or that cannot be listed, is refused (never "already empty")
mkdir -p "$ROOT/actions-runner-file/_work/r"; echo x > "$ROOT/actions-runner-file/_work/r/r"
hook "$ROOT/actions-runner-file/_work/r/r"
check "a file workspace is refused, exit 1" "$rc" "1"
check "a file workspace is left intact" "$(cat "$ROOT/actions-runner-file/_work/r/r")" "x"
if [ "$(id -u)" != 0 ]; then
  ws=$(mkws unreadable r); chmod 000 "$ws"
  hook "$ws"
  chmod 755 "$ws"
  check "an unreadable workspace is refused, exit 1" "$rc" "1"
  check "an unreadable workspace says it cannot be listed" "$(has "$out" "is not readable")" "1"
  check "an unreadable workspace is left intact" "$(count "$ws")" "7"
fi

# 10. a real repo: enforce keeps git's store so checkout can REUSE it (no 113 MB re-clone),
#     and removes the worktree and every poison (config, hooks, alternates)
set_mode "MODE=enforce"
ws=$(mkrepo_ws keep pantheon-config)
head_before=$(G -C "$ws" rev-parse HEAD); objs_before=$(objects_in "$ws")
hook "$ws"
check "keep: exits 0" "$rc" "0"
check "keep: says the store was kept" "$(has "$out" "entries from $ws (git store kept)")" "1"
check "keep: only .git is left at the top" "$(ls -A "$ws" | tr '\n' ' ')" ".git "
check "keep: .git holds only the store and a fresh config" "$(ls -A "$ws/.git" | sort | tr '\n' ' ')" "HEAD config objects packed-refs refs "
check "keep: the config is exactly the template" "$(cat "$ws/.git/config")" "$(printf '[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n\tlogallrefupdates = true\n[remote "origin"]\n\turl = %s/pantheon-config\n\tfetch = +refs/heads/*:refs/remotes/origin/*' "$TEMPLATE_URL_BASE")"
check "keep: alternates removed" "$([ -e "$ws/.git/objects/info/alternates" ] && echo left)" ""
check "keep: every object kept" "$(objects_in "$ws")" "$objs_before"
check "keep: HEAD still resolves to the same commit" "$(GV -C "$ws" rev-parse HEAD 2>&1)" "$head_before"
check "keep: origin is the URL checkout compares against" "$(GV -C "$ws" config --get remote.origin.url)" "$TEMPLATE_URL_BASE/pantheon-config"
# actions/checkout@v4's reuse path, in its own order (prepareExistingDirectory in
# dist/index.js, read on CT202 2026-09-30): if .git exists and the fetch URL matches, it runs
# detach, delete the local branches, submodule status, clean -ffdx, reset --hard HEAD. ANY
# failing step means "recreated": a full re-clone, the cost this keeps away. So every step
# must exit 0, none may run a poison, and the reset must rebuild the tree from the store alone.
reuse=""
GV -C "$ws" checkout --detach >/dev/null 2>&1; reuse+="$?"
for b in $(GV -C "$ws" rev-parse --symbolic --branches 2>/dev/null); do
  GV -C "$ws" branch --delete --force "$b" >/dev/null 2>&1; reuse+=" $?"
done
GV -C "$ws" submodule status >/dev/null 2>&1; reuse+=" $?"
GV -C "$ws" clean -ffdx >/dev/null 2>&1; reuse+=" $?"
GV -C "$ws" reset --hard HEAD >/dev/null 2>&1; reuse+=" $?"
GV -C "$ws" status --porcelain >/dev/null 2>&1
check "keep: every step of checkout's reuse path exits 0" "$reuse" "0 0 0 0 0"
check "keep: the reset rebuilt the tree from the kept store" "$(cat "$ws/tracked.txt" 2>&1)" "v1"
check "keep: no poison ran (fsmonitor, post-checkout)" "$(cat "$T/pwned" 2>/dev/null)" ""
check "keep: the sibling basetemp survives" "$([ -d "$ROOT/actions-runner-keep/_work/pantheon-config" ] && echo yes)" "yes"
hook "$ws"
check "keep, second run: exits 0" "$rc" "0"
check "keep, second run: still only the store" "$(ls -A "$ws/.git" | sort | tr '\n' ' ')" "HEAD config objects packed-refs refs "

# 10b. a SYMLINKED .git/config is removed, never written through: the template must land in
#      a fresh file, and the link's target (any file the runner can write) stays untouched
ws=$(mkrepo_ws cfglink pantheon-config)
mkdir -p "$T/outside"; echo "precious" > "$T/outside/victim"
rm -f "$ws/.git/config"; ln -s "$T/outside/victim" "$ws/.git/config"
hook "$ws"
check "config symlink: exits 0" "$rc" "0"
check "config symlink: the target is untouched" "$(cat "$T/outside/victim")" "precious"
check "config symlink: config is now a regular file" "$([ -f "$ws/.git/config" ] && [ ! -L "$ws/.git/config" ] && echo file)" "file"

# 11. observe on a real repo removes nothing, poisons included
set_mode "MODE=observe"
ws=$(mkrepo_ws keepobs pantheon-config)
before=$(find "$ws" | sort | md5sum)
hook "$ws"
check "keep, observe: exits 0" "$rc" "0"
check "keep, observe: nothing changed" "$(find "$ws" | sort | md5sum)" "$before"
check "keep, observe: says the store would be kept" "$(has "$out" "(git store kept; observe mode; nothing removed)")" "1"

# 12. a .git that is not a plain store is removed WHOLE (checkout then does a full clone),
#     and nothing it points at outside the workspace is touched
set_mode "MODE=enforce"
mkdir -p "$T/outside/objs"; echo keep > "$T/outside/objs/f"
for shape in objects-symlink symlink-in-refs head-symlink packed-refs-dir git-is-a-file; do
  ws=$(mkrepo_ws "s-$shape" pantheon-config)
  case "$shape" in
    objects-symlink) rm -rf "$ws/.git/objects"; ln -s "$T/outside/objs" "$ws/.git/objects" ;;
    symlink-in-refs) ln -s "$T/outside/objs/f" "$ws/.git/refs/heads/evil" ;;
    head-symlink) mv "$ws/.git/HEAD" "$T/outside/HEAD-$shape"; ln -s "$T/outside/HEAD-$shape" "$ws/.git/HEAD" ;;
    packed-refs-dir) rm -f "$ws/.git/packed-refs"; mkdir "$ws/.git/packed-refs" ;;
    git-is-a-file) rm -rf "$ws/.git"; echo "gitdir: $T/outside/elsewhere" > "$ws/.git" ;;
  esac
  hook "$ws"
  check "$shape: exits 0" "$rc" "0"
  check "$shape: .git removed whole" "$([ -e "$ws/.git" ] || [ -L "$ws/.git" ] && echo left)" ""
  check "$shape: the workspace is empty" "$(count "$ws")" "0"
  check "$shape: says why" "$(has "$out" "(no reusable git store, .git removed)")" "1"
done
check "symlink targets outside the workspace survive" "$(cat "$T/outside/objs/f")" "keep"

# 13. a workspace with no .git at all says so
ws=$(mkws nogit pantheon-config); rm -rf "$ws/.git"
hook "$ws"
check "no .git: exits 0 and says so" "$rc:$(has "$out" "entries from $ws (no .git)")" "0:1"

# ---- T-4425: fresh HOME, only when the conf enables it for this uid's name ----
ME="$("$REAL_ID" -un)"; MYUID="$("$REAL_ID" -u)"
H="$HROOT/ci"
ON="WIPE_HOME=1
WIPE_HOME_USERS=runner-other $ME"

# 14. off unless enabled: no WIPE_HOME, no conf, or anything but exactly 1 leaves the home alone
mkhome "$H"
for conf in "MODE=enforce" --none "MODE=enforce
WIPE_HOME=yes
WIPE_HOME_USERS=$ME" "MODE=enforce
WIPE_HOME=1
WIPE_HOME=0
WIPE_HOME_USERS=$ME"; do
  set_mode "$conf"; ws=$(mkws hoff pantheon-config)
  hook "$ws"
  check "home reset off [$conf]: home untouched" "$(count "$H")" "9"
  check "home reset off [$conf]: says nothing about home" "$(has "$out" "home:")" "0"
  check "home reset off [$conf]: the workspace is still cleared" "$rc:$(count "$ws")" "0:0"
done

# 15. enforce + listed: the home is emptied (dotfiles, a user site .pth, a symlink), the dir and
#     a symlink's target outside it survive, and the workspace is cleared as before
set_mode "MODE=enforce
$ON"
mkhome "$H"; ws=$(mkws hon pantheon-config)
hook "$ws"
check "home enforce: exits 0" "$rc" "0"
check "home enforce: home emptied" "$(count "$H")" "0"
check "home enforce: the home dir itself is kept" "$([ -d "$H" ] && echo yes)" "yes"
check "home enforce: a symlink's target outside the home survives" "$(cat "$T/outside-home/f")" "keep"
check "home enforce: reports what it cleared" "$(has "$out" "home: cleared 5 entries from $H")" "1"
check "home enforce: the report reaches syslog" "$(has "$(cat "$LOG")" "[fresh-workspace T-4416 enforce] home: cleared 5 entries from $H")" "1"
check "home enforce: the workspace is still cleared" "$(count "$ws")" "0"

# 16. observe: reports, removes nothing, and the workspace report still follows
set_mode "MODE=observe
$ON"
mkhome "$H"; ws=$(mkws hobs pantheon-config)
hook "$ws"
check "home observe: exits 0 and removes nothing" "$rc:$(count "$H")" "0:9"
check "home observe: says what it would clear" "$(has "$out" "home: would clear 5 entries from $H (observe mode; nothing removed)")" "1"
check "home observe: the workspace report still follows" "$(has "$out" "would clear 4 entries from $ws")" "1"

# 17. a uid not in the list keeps its home (a uid shared with other runners), even in enforce.
# The list is built from $ME so it can never contain the user running this suite: CI runs it
# AS runner-pc / runner-pcl since T-4436, so a literal "runner-pc runner-pcl" listed the job
# user and case 17 wiped its own fixture home.
set_mode "MODE=enforce
WIPE_HOME=1
WIPE_HOME_USERS=not-$ME also-not-$ME"
mkhome "$H"; ws=$(mkws hnot pantheon-config)
hook "$ws"
check "home unlisted: exits 0 and keeps the home" "$rc:$(count "$H")" "0:9"
check "home unlisted: says why" "$(has "$out" "home: NOT reset: $ME is not in WIPE_HOME_USERS")" "1"

# 18. a comma-separated list works too
set_mode "MODE=enforce
WIPE_HOME=1
WIPE_HOME_USERS=runner-other,$ME"
mkhome "$H"; ws=$(mkws hcomma pantheon-config)
hook "$ws"
check "home comma list: emptied" "$rc:$(count "$H")" "0:0"

# 19. the home comes from passwd, NEVER from $HOME: a workflow's env: HOME must not aim the rm.
#     The decoy is a home every path guard would accept (ours, directly under HOME_ROOT), so
#     only the passwd lookup keeps it safe.
set_mode "MODE=enforce
$ON"
mkhome "$H"; mkdir -p "$HROOT/decoy"; echo mine > "$HROOT/decoy/keep"; ws=$(mkws hdecoy pantheon-config)
out="$(env GITHUB_WORKSPACE="$ws" HOME="$HROOT/decoy" PATH="$T/bin:$PATH" "${HSH[@]}" "$HOOK" 2>&1)"; rc=$?
check "HOME=decoy: the decoy survives" "$(cat "$HROOT/decoy/keep" 2>/dev/null)" "mine"
check "HOME=decoy: the passwd home is the one cleared" "$rc:$(count "$H")" "0:0"

# 20. a passwd home that is not a plain dir directly under HOME_ROOT, or is not this uid's, is
#     refused: the job fails in enforce, and nothing is removed
for shape in outside dotdot dot symlink nested missing foreign; do
  set_mode "MODE=enforce
$ON"
  mkhome "$H"; rm -rf "$T/elsewhere" "$HROOT/link" "$T/fake-uid"; home="$H"
  case "$shape" in
    outside) mkhome "$T/elsewhere"; home="$T/elsewhere" ;;
    dotdot) home="$HROOT/.." ;;
    dot) home="$HROOT/." ;;
    symlink) ln -s "$H" "$HROOT/link"; home="$HROOT/link" ;;
    nested) mkdir -p "$H/sub"; home="$H/sub" ;;
    missing) home="$HROOT/nobody" ;;
    foreign) echo 424242 > "$T/fake-uid" ;;
  esac
  printf '%s:x:%s:%s::%s:/bin/bash\n' "$ME" "$MYUID" "$MYUID" "$home" > "$T/passwd"
  [ "$shape" = foreign ] && printf '%s:x:424242:424242::%s:/bin/bash\n' "$ME" "$home" > "$T/passwd"
  ws=$(mkws "hbad$shape" pantheon-config); hook "$ws"
  check "bad home [$shape]: enforce fails the job" "$rc" "1"
  check "bad home [$shape]: says REFUSED: home" "$(has "$out" "REFUSED: home:")" "1"
  check "bad home [$shape]: the real home is untouched" "$(count "$H")" "$([ "$shape" = nested ] && echo 10 || echo 9)"
  [ "$shape" = outside ] && check "bad home [outside]: that dir is untouched" "$(count "$T/elsewhere")" "9"
  set_mode "MODE=observe
$ON"
  ws=$(mkws "hbado$shape" pantheon-config); hook "$ws"
  check "bad home [$shape] observe: exits 0, still reports the workspace" \
    "$rc:$(has "$out" "REFUSED: home:"):$(has "$out" "would clear 4 entries from $ws")" "0:1:1"
done
rm -f "$T/fake-uid"
printf '%s:x:%s:%s::%s:/bin/bash\n' "$ME" "$MYUID" "$MYUID" "$H" > "$T/passwd"

# 21. no passwd entry: refused (enforce fails), and an empty home is "already empty"
set_mode "MODE=enforce
$ON"
mkhome "$H"; touch "$T/getent-fail"; ws=$(mkws hnoent pantheon-config); hook "$ws"
check "no passwd entry: enforce fails, home untouched" "$rc:$(has "$out" "REFUSED: home: no passwd entry"):$(count "$H")" "1:1:9"
rm -f "$T/getent-fail"
rm -rf "$H"; mkdir -p "$H"; ws=$(mkws hempty pantheon-config); hook "$ws"
check "empty home: exits 0 and says already empty" "$rc:$(has "$out" "home: $H already empty")" "0:1"

}

mkhome() { # <dir>: a home with 5 top-level entries (9 paths), incl. a user-site .pth and a symlink out
  rm -rf "$1"; mkdir -p "$1/.local/lib/python3.12/site-packages" "$1/.cache" "$T/outside-home"
  echo 'import os' > "$1/.local/lib/python3.12/site-packages/evil.pth"
  echo "[core] fsmonitor = x" > "$1/.gitconfig"; echo rc > "$1/.bashrc"
  echo keep > "$T/outside-home/f"; ln -sfn "$T/outside-home" "$1/link-out"
  [ "$1" = "$H" ] && printf '%s:x:%s:%s::%s:/bin/bash\n' "$ME" "$MYUID" "$MYUID" "$1" > "$T/passwd"
  return 0
}

# Every case runs three times. "runner" is how the runner really calls a job hook: `bash -e
# <hook>`, from its Worker _diag log (ProcessInvokerWrapper Arguments, CT202, 2026-09-30).
# "strict" adds pipefail, the template for `shell: bash` steps in Runner.Worker.dll, so a
# failing pipeline cannot abort the hook if the runner ever tightens. "plain" is a person
# running it by hand.
for INV in runner strict plain; do
  case "$INV" in
    runner) HSH=(bash -e) ;;
    strict) HSH=(bash --noprofile --norc -e -o pipefail) ;;
    plain) HSH=(bash) ;;
  esac
  rm -rf "$ROOT" "$T/outside" "$T/nest" "$LOG" "$T/hostile.conf" "$T/githome" "$T/pwned" \
    "$T/evil-fsmonitor" "$T/evil-objects" "$HROOT" "$T/decoy" "$T/outside-home" "$T/elsewhere" \
    "$T/passwd" "$T/fake-uid" "$T/getent-fail"; mkdir -p "$ROOT"
  cases
done

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
