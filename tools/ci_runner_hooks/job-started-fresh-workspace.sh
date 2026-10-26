#!/usr/bin/env bash
# job-started-fresh-workspace.sh: the ACTIONS_RUNNER_HOOK_JOB_STARTED hook for the
# pantheon-config runners in CT202 (T-4416).
#
# It EMPTIES the job's workspace before any step runs. A green run can then only have
# tested a tree the job itself checked out. Before this, a workflow that skipped checkout
# would test whatever an earlier job had left behind (T-4385 hole B). Text guards in
# test_ci_integrity cannot close that: on a stale tree they are stale too.
#
# It clears the workspace's CONTENTS and keeps the directory the runner created. It keeps
# everything outside the workspace: the sibling pytest basetemp (T-4391), _tool, _actions
# and /ci-cache.
#
# Inside the workspace it KEEPS git's content store (.git/objects, refs, packed-refs, HEAD,
# shallow) and rewrites .git/config from a clean template whose origin is the URL
# actions/checkout expects. checkout then REUSES the repo and fetches incrementally.
# Wiping .git too meant a full re-clone per job (fetch-depth 0, 113 MB, ~23s). hermes-audit
# runs on every push on the single light runner, ~1,000 jobs/day, so that came to ~110 GB/day
# over pve2's home link (measured 2026-09-30).
# Everything else goes: the worktree (so a job that skips checkout finds no tree), the old
# config (fsmonitor, hooksPath, credential helpers...), hooks/, info/, the index, logs, and
# objects/info/(http-)alternates. A .git that is not a plain store (a file, or a symlink
# anywhere in objects/ or refs/, HEAD, packed-refs or shallow) is removed whole, and checkout
# then does a full clone. This hook never RUNS git: running it in a poisoned repo executes
# that repo's config.
#
# Mode comes ONLY from the root-owned CONF below, never from the environment. A workflow's
# env: reaches this hook, so an env switch would let a job turn enforcement off. The only
# accepted value other than the default is MODE=observe. Anything else, or no file, is
# enforce:
#   observe  report what would be removed and remove nothing (the rollout's first stage)
#   enforce  remove it. Any refusal FAILS the job, because a job must not run on a tree
#            this hook could not clear.
#
# Fresh HOME (T-4425), OFF unless the CONF enables it. With WIPE_HOME=1, and this uid's name
# listed in WIPE_HOME_USERS, it also empties the job user's home before any step runs, so
# nothing a job leaves there reaches the next job: a Python user site (a .pth runs at every
# interpreter start), pip's cache and config, ~/.local/bin, or a ~/.gitconfig. That is safe only
# when the runner has a uid, and so a home, of its own (T-4436): a runner runs one job at a time,
# but a home SHARED with other runners would be wiped under their running jobs. So a uid not in
# the list keeps its home. The home comes from the passwd entry, never from $HOME, which a
# workflow's env: can set.
#
# Residuals (documented, not closed). Both need hostile job code; this closes the
# accidental and lazy cases, which are the ones T-4385 is about.
# - The runner install is owned by the runner user, so a job could still rewrite the
#   runner's .env for the NEXT runner start.
# - A job could plant an object or ref in the kept store for a later checkout to reuse.
#
# Installed by tools/ci_runner_hooks/install.sh to /usr/local/lib/pantheon-ci/, root:root 0755.
# Tests: tools/test_ci_runner_fresh_workspace.sh runs every case under the runner's real
# invocation (bash -e), a stricter bash -e -o pipefail, and plain bash.
set -u

CONF=/etc/pantheon-ci/fresh-workspace.conf
ROOT=/opt
HOME_ROOT=/home
ORIGIN_BASE=https://github.com/example-org

# The runner runs this as `bash -e <hook>` (its Worker _diag log, 2026-09-30). Keep every
# pipeline safe under pipefail too: without the || a missing or unreadable conf (sed exits 2)
# would ABORT the hook under `-e -o pipefail` instead of enforcing.
mode="$(sed -n 's/^MODE=//p' "$CONF" 2>/dev/null | tail -n 1)" || mode=""
[[ "$mode" == observe ]] || mode=enforce
tag="[fresh-workspace T-4416 $mode]"

say() {
  echo "$tag $*"
  logger -t pantheon-fresh-workspace -- "$tag $*" 2>/dev/null || true
}
refuse() {
  say "REFUSED: $*"
  [[ "$mode" == enforce ]] && exit 1
  exit 0
}
conf_value() { # <key>: its last value in CONF, "" if none (pipefail-safe for the caller's ||)
  sed -n "s/^$1=//p" "$CONF" 2>/dev/null | tail -n 1
}

# T-4425 fresh HOME (see the header). A refusal fails the job in enforce. In observe it is
# reported and the workspace part below still runs.
home_refuse() {
  [[ "$mode" == enforce ]] && refuse "home: $*"
  say "REFUSED: home: $*"
  return 1
}
reset_home() {
  local want users me uid entry home owner e
  want="$(conf_value WIPE_HOME)" || want=""
  [[ "$want" == 1 ]] || return 0
  users="$(conf_value WIPE_HOME_USERS)" || users=""
  uid="$(id -u)" && me="$(id -un)" || { home_refuse "cannot tell which user this is"; return; }
  if [[ " ${users//,/ } " != *" $me "* ]]; then
    say "home: NOT reset: $me is not in WIPE_HOME_USERS (a uid shared with other runners keeps its home)"
    return 0
  fi
  entry="$(getent passwd "$uid")" || { home_refuse "no passwd entry for uid $uid"; return; }
  IFS=: read -r _ _ _ _ _ home _ <<<"$entry"
  # Only a direct child of HOME_ROOT, canonical, a real directory owned by this uid.
  [[ "$home" =~ ^${HOME_ROOT}/([A-Za-z0-9._-]+)$ && "${BASH_REMATCH[1]}" != . && "${BASH_REMATCH[1]}" != .. ]] \
    || { home_refuse "passwd home '$home' of $me is not a directory directly under $HOME_ROOT"; return; }
  [[ "$(realpath -m -- "$home")" == "$home" ]] || { home_refuse "$home is not canonical"; return; }
  [[ -d "$home" && ! -L "$home" ]] || { home_refuse "$home is not a directory"; return; }
  owner="$(stat -c %u -- "$home")" || owner=""
  [[ "$owner" == "$uid" ]] || { home_refuse "$home is owned by uid '$owner', not $me ($uid)"; return; }
  [[ -r "$home" && -x "$home" ]] || { home_refuse "$home is not readable, so its contents cannot be listed"; return; }
  local gone=()
  while IFS= read -r -d '' e; do gone+=("$e"); done < <(find "$home" -mindepth 1 -maxdepth 1 -print0)
  if (( ${#gone[@]} == 0 )); then say "home: $home already empty"; return 0; fi
  if [[ "$mode" != enforce ]]; then
    say "home: would clear ${#gone[@]} entries from $home (observe mode; nothing removed)"
    return 0
  fi
  rm -rf --one-file-system -- "${gone[@]}" || { home_refuse "rm failed in $home"; return; }
  [[ -z "$(find "$home" -mindepth 1 -maxdepth 1 -print -quit)" ]] \
    || { home_refuse "$home is not empty after clearing"; return; }
  say "home: cleared ${#gone[@]} entries from $home"
}
reset_home || true

ws="${GITHUB_WORKSPACE:-}"
[[ -n "$ws" ]] || refuse "GITHUB_WORKSPACE is unset"
real="$(realpath -m -- "$ws")"
# The runner hands over a canonical path (checked on CT202). Anything else (a symlink in
# ANY component, a "..") could point this rm at a tree that is not the job's workspace.
[[ "$real" == "$ws" ]] || refuse "$ws is not canonical (resolves to $real)"
# Only a runner job workspace, <ROOT>/actions-runner-<name>/_work/<repo>/<repo>.
if [[ ! "$real" =~ ^${ROOT}/actions-runner-[A-Za-z0-9._-]+/_work/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)$ ]]; then
  refuse "$real is not a runner job workspace"
fi
[[ "${BASH_REMATCH[1]}" == "${BASH_REMATCH[2]}" ]] || refuse "$real is not <repo>/<repo>"
repo="${BASH_REMATCH[1]}"
if [[ ! -e "$real" ]]; then
  say "workspace $real absent; nothing to clear"
  exit 0
fi
[[ -d "$real" ]] || refuse "$real is not a directory"
# find on an unreadable dir lists nothing, which would read as "already empty".
[[ -r "$real" && -x "$real" ]] || refuse "$real is not readable, so its contents cannot be listed"

git_dir="$real/.git"
kept_in_git() { case "$1" in objects|refs|packed-refs|HEAD|shallow) return 0 ;; esac; return 1; }
reusable_store() { # is $git_dir a plain git store worth keeping? (no symlink anywhere in it)
  local f
  [[ -d "$git_dir" && ! -L "$git_dir" ]] || return 1
  [[ -d "$git_dir/objects" && ! -L "$git_dir/objects" && -d "$git_dir/refs" && ! -L "$git_dir/refs" ]] || return 1
  [[ -f "$git_dir/HEAD" && ! -L "$git_dir/HEAD" ]] || return 1
  for f in packed-refs shallow; do
    [[ -L "$git_dir/$f" ]] && return 1
    [[ ! -e "$git_dir/$f" || -f "$git_dir/$f" ]] || return 1
  done
  [[ -z "$(find "$git_dir/objects" "$git_dir/refs" -type l -print -quit 2>/dev/null)" ]]
}
store="no .git"
[[ -e "$git_dir" || -L "$git_dir" ]] && store="no reusable git store, .git removed"
keep_git=no
if reusable_store; then keep_git=yes; store="git store kept"; fi

remove=()
while IFS= read -r -d '' e; do
  [[ "$keep_git" == yes && "$e" == "$git_dir" ]] || remove+=("$e")
done < <(find "$real" -mindepth 1 -maxdepth 1 -print0)
if [[ "$keep_git" == yes ]]; then
  while IFS= read -r -d '' e; do
    kept_in_git "${e##*/}" || remove+=("$e")
  done < <(find "$git_dir" -mindepth 1 -maxdepth 1 -print0)
  for e in "$git_dir/objects/info/alternates" "$git_dir/objects/info/http-alternates"; do
    if [[ -e "$e" || -L "$e" ]]; then remove+=("$e"); fi
  done
fi
if (( ${#remove[@]} == 0 )) && [[ "$keep_git" == no ]]; then
  say "workspace $real already empty"
  exit 0
fi
if [[ "$mode" != enforce ]]; then
  say "would clear ${#remove[@]} entries from $real ($store; observe mode; nothing removed)"
  exit 0
fi
if (( ${#remove[@]} )); then
  rm -rf --one-file-system -- "${remove[@]}" || refuse "rm failed in $real"
fi
if [[ "$keep_git" == yes ]]; then
  printf '[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n\tlogallrefupdates = true\n[remote "origin"]\n\turl = %s/%s\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n' \
    "$ORIGIN_BASE" "$repo" > "$git_dir/config" || refuse "could not write $git_dir/config"
fi
# Check the result, not the plan: only the kept store (and its fresh config) may remain.
while IFS= read -r -d '' e; do
  [[ "$keep_git" == yes && "$e" == "$git_dir" ]] || refuse "$real still holds $e after clearing"
done < <(find "$real" -mindepth 1 -maxdepth 1 -print0)
if [[ "$keep_git" == yes ]]; then
  while IFS= read -r -d '' e; do
    kept_in_git "${e##*/}" || [[ "${e##*/}" == config ]] || refuse "$git_dir still holds $e after clearing"
  done < <(find "$git_dir" -mindepth 1 -maxdepth 1 -print0)
  [[ ! -e "$git_dir/objects/info/alternates" ]] || refuse "$git_dir still has objects/info/alternates"
fi
say "cleared ${#remove[@]} entries from $real ($store)"
