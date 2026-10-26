#!/usr/bin/env bash
# install.sh: wire, switch or roll back the T-4416 fresh-workspace hook
# (job-started-fresh-workspace.sh, next to this file) on the pantheon-config runners in CT202.
#
# Run as root INSIDE CT202:
#   install.sh status   [--runner pc|pc-light|all]
#   install.sh observe  [--runner pc|pc-light|all] [--restart]
#   install.sh enforce  [--runner pc|pc-light|all] [--restart] [--skip-observe-check]
#   install.sh rollback [--runner pc|pc-light|all] [--restart]
#   install.sh home-on  --users "<user> [<user>]"
#   install.sh home-off
#   install.sh toolcache-lock   --pins "<name>==<version> ..." [--runner pc|pc-light|all]
#   install.sh toolcache-unlock --owner <user>
#
# toolcache-lock (T-4425) makes $OPT/hostedtoolcache root-owned and not group/other-writable, so
# no job can leave a .pth or a trojaned package in the interpreters every later job starts. It
# refuses unless every interpreter already holds exactly the given pins (ci.yml's pip line) and
# no .pth but setuptools' own, and it reads that from each *.dist-info/METADATA: it never runs
# the toolcache's Python as root, since a planted .pth would run as root. It also writes a
# root-owned drop-in with AGENT_TOOLSDIRECTORY (live at the runner's next restart), and status
# flags a copy in the runner-owned .env. Once locked, ci.yml's "pip install --upgrade pip <pins>"
# finds the pins satisfied and writes nothing. A newer pip goes to the job user's user site,
# which the fresh HOME clears. A pin bump: as root, <x64>/bin/python3 -m pip install
# --only-binary=:all: <new pins> for each interpreter, then toolcache-lock with the new pins.
# toolcache-unlock is the rollback: it hands the tree back to <user>.
#
# home-on / home-off (T-4425) turn the hook's fresh-HOME reset on or off for the listed users
# (the conf's WIPE_HOME / WIPE_HOME_USERS; the MODE governs it like the workspace). home-on
# refuses a user that owns any runner install other than the pantheon-config ones (its home is
# shared with that runner's jobs) or that does not own exactly one pantheon-config runner, so
# it only works once each pc runner has a uid of its own (T-4436).
#
# From swarm, from a clean checkout of the REVIEWED main (deploy the bytes that merged). Use
# nsenter, not pct exec, which hangs on this host:
#   tar -C tools/ci_runner_hooks -cf - install.sh job-started-fresh-workspace.sh \
#     | ssh pve2 'nsenter -t $(lxc-info -n 202 -p -H) -a -- bash -c \
#         "d=\$(mktemp -d) && tar -C \$d -xf - && bash \$d/install.sh status; rc=\$?; rm -rf \$d; exit \$rc"'
#
# What it touches, all root-owned (the runner install and its .env are runner-owned, so
# nothing is put there):
#   /usr/local/lib/pantheon-ci/job-started-fresh-workspace.sh   the hook, 0755
#   /etc/pantheon-ci/fresh-workspace.conf                        MODE=observe|enforce, 0644
#   /etc/systemd/system/<unit>.d/pantheon-fresh-workspace.conf   Environment=ACTIONS_RUNNER_HOOK_JOB_STARTED=<hook>
#
# The MODE is GLOBAL: one conf, read by the hook on every job, so changing it takes effect at
# the next job with no restart. --runner picks which runners are WIRED (have the drop-in).
# Wiring or unwiring takes effect only when that runner restarts. --restart does it, and only
# while the runner is idle: a restart kills a running job, so a busy runner is skipped (exit 3).
#
# Rollout (T-4416): observe --runner pc-light --restart; read one job's hook line
# (journalctl -t pantheon-fresh-workspace) and check the workspace it names; observe --runner
# pc --restart; then enforce. enforce refuses a runner the journal shows no observe run for,
# unless --skip-observe-check.
#
# Rollback is instant when it covers every wired runner. It sets MODE=observe first, so from
# the next job on the hook removes nothing, and then unwires. The hook file is removed only
# once no runner can still call it; a listener that still holds the variable would otherwise
# fail every job on a missing hook.
#
# Residual (documented in the hook too): a runner's .env is runner-owned and read by the
# listener at start, so a job could still override or drop the variable for the next start.
# status flags it.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
HOOK_SRC="$HERE/job-started-fresh-workspace.sh"
LIB=/usr/local/lib/pantheon-ci
HOOK="$LIB/job-started-fresh-workspace.sh"
CONF_DIR=/etc/pantheon-ci
CONF="$CONF_DIR/fresh-workspace.conf"
UNIT_DIR=/etc/systemd/system
OPT=/opt
PROC=/proc
OWNER=root:root
NEED_UID=0
RESTART_WAIT=60
DROPIN_NAME=pantheon-fresh-workspace.conf
ENV_KEY=ACTIONS_RUNNER_HOOK_JOB_STARTED
LOG_TAG=pantheon-fresh-workspace
RUNNERS=(pc pc-light)
NO_LISTENER="<no listener>"
UNREADABLE="<unreadable>"
TOOLCACHE="$OPT/hostedtoolcache"
TC_KEY=AGENT_TOOLSDIRECTORY
TC_DROPIN_NAME=pantheon-toolcache.conf
PTH_OK=distutils-precedence.pth   # setuptools' own; any other .pth in the toolcache is flagged

unit_of() {
  case "$1" in
    pc) echo actions.runner.example-org-pantheon-config.pve2-pantheon-config.service ;;
    pc-light) echo actions.runner.example-org-pantheon-config.pve2-pantheon-config-light.service ;;
  esac
}
dir_of() { echo "$OPT/actions-runner-$1"; }
dropin_of() { echo "$UNIT_DIR/$(unit_of "$1").d/$DROPIN_NAME"; }
die() { echo "install.sh: $*" >&2; exit 1; }
usage() { sed -n '5,13p' "$0" | sed 's/^# \{0,3\}//'; }

pids_of() { # <runner> <Listener|Worker>: pids whose executable is that runner's binary
  local dir p e
  dir="$(dir_of "$1")"
  for p in "$PROC"/[0-9]*; do
    e="$(readlink "$p/exe" 2>/dev/null)" || continue
    # bin is bin.<version> after a self-update; the /bin anchor keeps pc from matching pc-light.
    [[ "$e" =~ ^${dir}/bin[^/]*/Runner\.$2$ ]] && echo "${p##*/}"
  done
}
live_value() { # <runner>: the hook variable its running listener holds ("" = none)
  local pids pid
  pids="$(pids_of "$1" Listener)"
  [[ -n "$pids" ]] || { echo "$NO_LISTENER"; return; }
  pid="${pids%%$'\n'*}"
  [[ -r "$PROC/$pid/environ" ]] || { echo "$UNREADABLE"; return; }
  tr '\0' '\n' < "$PROC/$pid/environ" | sed -n "s/^$ENV_KEY=//p" | tail -n 1
}
env_override() { # <runner>: prints the runner's own .env line for the variable, if any
  grep -E "^[[:space:]]*$ENV_KEY=" "$(dir_of "$1")/.env" 2>/dev/null
}
effective_mode() { # the mode the hook applies, by the hook's own rule
  local m
  m="$(sed -n 's/^MODE=//p' "$CONF" 2>/dev/null | tail -n 1)"
  [[ "$m" == observe ]] && echo observe || echo enforce
}
wired_elsewhere() { # runners outside the selection that are wired or still hold the variable
  local r
  for r in "${RUNNERS[@]}"; do
    [[ " ${SEL[*]} " == *" $r "* ]] && continue
    if [[ -e "$(dropin_of "$r")" || "$(live_value "$r")" == "$HOOK" ]]; then echo "$r"; fi
  done
}

conf_value() { # <key>: its last value in the installed conf, "" if none
  sed -n "s/^$1=//p" "$CONF" 2>/dev/null | tail -n 1
}
write_conf() { # <mode> [<wipe_home> <users>]: without the last two, the current ones are kept
  local wipe users
  if [[ $# -ge 3 ]]; then wipe="$2"; users="$3"; else wipe="$(conf_value WIPE_HOME)"; users="$(conf_value WIPE_HOME_USERS)"; fi
  install -d -o "${OWNER%:*}" -g "${OWNER#*:}" -m 0755 "$CONF_DIR" || die "could not create $CONF_DIR"
  {
    printf '# T-4416: read on every job by %s.\n# observe | enforce. Anything else, or no file, is enforce.\nMODE=%s\n' "$HOOK" "$1"
    if [[ "$wipe" == 1 ]]; then
      printf '# T-4425: fresh HOME per job for these users only (install.sh home-on/home-off).\nWIPE_HOME=1\nWIPE_HOME_USERS=%s\n' "$users"
    fi
  } > "$CONF.new" \
    && chown "$OWNER" "$CONF.new" && chmod 0644 "$CONF.new" && mv -f "$CONF.new" "$CONF" \
    || die "could not write $CONF"
}
home_user_ok() { # <user>: dies unless its home can be reset per job without touching another runner
  local u=$1 d n=0
  [[ "$u" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "home-on: '$u' is not a user name"
  getent passwd "$u" >/dev/null || die "home-on: no such user '$u'"
  for d in "$OPT"/actions-runner-*; do
    [[ -d "$d" ]] || continue
    [[ "$(stat -c %U -- "$d")" == "$u" ]] || continue
    case "${d##*/actions-runner-}" in
      pc|pc-light) n=$((n + 1)) ;;
      *) die "home-on: $u also owns $d, so its home is shared with that runner's jobs (T-4436 first)" ;;
    esac
  done
  [[ $n -eq 1 ]] || die "home-on: $u owns $n pantheon-config runner installs; it must own exactly one (one uid per runner, T-4436)"
}

# ---- T-4425 toolcache lock. Nothing here runs the toolcache's Python. ----
tc_dropin_of() { echo "$UNIT_DIR/$(unit_of "$1").d/$TC_DROPIN_NAME"; }
norm() { sed -E 's/[-_.]+/-/g' <<<"${1,,}"; } # PEP 503 name normalization
tc_sites() { # every interpreter's site-packages, one per line
  local s
  for s in "$TOOLCACHE"/Python/*/x64/lib/python3*/site-packages; do
    if [[ -d "$s" && ! -L "$s" ]]; then echo "$s"; fi
  done
}
tc_dists() { # <site>: "name==version" per distribution, read from its METADATA headers
  local m
  for m in "$1"/*.dist-info/METADATA; do
    [[ -f "$m" ]] || continue
    awk '/^$/ { exit } tolower($1) == "name:" { n = tolower($2); gsub(/[-_.]+/, "-", n) }
         tolower($1) == "version:" { v = $2 } END { if (n != "") print n "==" v }' "$m"
  done | sort
}
tc_pth_extra() { # <site>: every .pth other than setuptools' own
  local p
  for p in "$1"/*.pth; do
    [[ -e "$p" || -L "$p" ]] || continue
    [[ "${p##*/}" == "$PTH_OK" ]] || echo "$p"
  done
}
tc_missing() { # <site> <pin>...: the pins that are not installed at exactly that version
  local have p site=$1
  shift
  have="$(tc_dists "$site")"
  for p in "$@"; do
    grep -qxF -- "$(norm "${p%%==*}")==${p#*==}" <<<"$have" || echo "$p"
  done
}
tc_state() { # locked: every entry owned by the OWNER user and none group/other-writable
  local u="${OWNER%:*}"
  if [[ -z "$(find "$TOOLCACHE" \( ! -user "$u" -o \( -perm /022 ! -type l \) \) -print -quit 2>/dev/null)" ]]; then
    echo locked
  else
    echo "UNLOCKED (entries not owned by $u, or group/other-writable)"
  fi
}
toolcache_status() {
  local s x r
  if [[ ! -d "$TOOLCACHE" ]]; then echo "toolcache: $TOOLCACHE absent"; return; fi
  echo "toolcache: $TOOLCACHE $(tc_state)"
  while IFS= read -r s; do
    echo "  $s: $(tc_dists "$s" | grep -c .) distributions"
    while IFS= read -r x; do [[ -n "$x" ]] && echo "  WARNING: unexpected .pth $x"; done < <(tc_pth_extra "$s")
  done < <(tc_sites)
  for r in "${RUNNERS[@]}"; do
    [[ -e "$(tc_dropin_of "$r")" ]] && echo "  runner $r: $TC_KEY from the root-owned drop-in"
    grep -qE "^[[:space:]]*$TC_KEY=" "$(dir_of "$r")/.env" 2>/dev/null \
      && echo "  runner $r: $TC_KEY is also set in its runner-owned .env (a job can repoint it for the next start)"
  done
  return 0
}
toolcache_lock() { # <pin>...
  local s bad="" m x r d
  [[ -d "$TOOLCACHE" && ! -L "$TOOLCACHE" ]] || die "toolcache-lock: $TOOLCACHE is not a directory"
  [[ -n "$(tc_sites)" ]] || die "toolcache-lock: no interpreter under $TOOLCACHE/Python/*/x64"
  while IFS= read -r s; do
    m="$(tc_missing "$s" "$@" | tr '\n' ' ')"
    [[ -n "$m" ]] && bad+="  $s: MISSING ${m% }"$'\n'
    x="$(tc_pth_extra "$s" | tr '\n' ' ')"
    [[ -n "$x" ]] && bad+="  $s: unexpected .pth ${x% } (review, remove, then lock)"$'\n'
  done < <(tc_sites)
  [[ -z "$bad" ]] || die "toolcache-lock: refusing, nothing changed:"$'\n'"${bad%$'\n'}"
  chown -R -h "$OWNER" "$TOOLCACHE" && chmod -R go-w "$TOOLCACHE" || die "toolcache-lock: chown/chmod failed (run toolcache-unlock to restore)"
  [[ "$(tc_state)" == locked ]] || die "toolcache-lock: still not locked after chown/chmod: $(tc_state)"
  for r in "${SEL[@]}"; do
    d="$(tc_dropin_of "$r")"
    install -d -o "${OWNER%:*}" -g "${OWNER#*:}" -m 0755 "${d%/*}" || die "could not create ${d%/*}"
    printf '# T-4425: pantheon-config tools/ci_runner_hooks/install.sh toolcache-lock\n[Service]\nEnvironment=%s=%s\n' \
      "$TC_KEY" "$TOOLCACHE" > "$d.new" && chown "$OWNER" "$d.new" && chmod 0644 "$d.new" && mv -f "$d.new" "$d" \
      || die "could not write $d"
  done
  systemctl daemon-reload || die "systemctl daemon-reload failed"
  echo "toolcache locked ($OWNER, no group/other write); $TC_KEY drop-in for ${SEL[*]}, live at each runner's next restart"
}
install_hook() {
  [[ -f "$HOOK_SRC" ]] || die "$HOOK_SRC is missing (run install.sh from next to the hook)"
  install -d -o "${OWNER%:*}" -g "${OWNER#*:}" -m 0755 "$LIB" || die "could not create $LIB"
  # Rename into place, so a job starting mid-install never runs a half-written hook.
  install -o "${OWNER%:*}" -g "${OWNER#*:}" -m 0755 "$HOOK_SRC" "$HOOK.new" && mv -f "$HOOK.new" "$HOOK" \
    || die "could not install $HOOK"
}
wire() { # <runner>
  local d
  d="$(dropin_of "$1")"
  install -d -o "${OWNER%:*}" -g "${OWNER#*:}" -m 0755 "${d%/*}" || die "could not create ${d%/*}"
  # The .new name does not end in .conf, so systemd never reads a partial drop-in.
  printf '# T-4416: pantheon-config tools/ci_runner_hooks/install.sh. Undo: install.sh rollback --runner %s\n[Service]\nEnvironment=%s=%s\n' \
    "$1" "$ENV_KEY" "$HOOK" > "$d.new" \
    && chown "$OWNER" "$d.new" && chmod 0644 "$d.new" && mv -f "$d.new" "$d" \
    || die "could not write $d"
}
unwire() { # <runner>
  local d
  d="$(dropin_of "$1")"
  rm -f "$d" || die "could not remove $d"
  rmdir "${d%/*}" 2>/dev/null || true
}
observed() { # <runner>: has the journal seen this runner's hook run in observe on a real workspace?
  journalctl -t "$LOG_TAG" --since "-14 days" -o cat 2>/dev/null \
    | grep -F "[fresh-workspace T-4416 observe]" \
    | grep -E "(would clear [0-9]+ (top-level )?entries from|workspace) $(dir_of "$1")/_work/pantheon-config/pantheon-config( |$)" \
    | grep -q .
}
restart_if_idle() { # <runner> <wired|unwired>; returns 1 when it did not end up that way
  local u busy v _
  u="$(unit_of "$1")"
  busy="$(pids_of "$1" Worker | tr '\n' ' ')"
  if [[ -n "$busy" ]]; then
    echo "runner $1: NOT restarted: a job is running (Runner.Worker ${busy% }). Re-run with --restart once it is idle."
    return 1
  fi
  systemctl restart "$u" || { echo "runner $1: systemctl restart $u FAILED"; return 1; }
  for _ in $(seq "$RESTART_WAIT"); do
    v="$(live_value "$1")"
    if [[ "$2" == wired && "$v" == "$HOOK" ]] || [[ "$2" == unwired && -z "$v" ]]; then
      echo "runner $1: restarted; its listener is now $2"
      return 0
    fi
    sleep 1
  done
  echo "runner $1: restarted, but after ${RESTART_WAIT}s its listener is not $2 (it holds '$v')"
  return 1
}
purge_if_unused() {
  local r v
  for r in "${RUNNERS[@]}"; do
    [[ -e "$(dropin_of "$r")" ]] && { echo "hook kept: runner $r is still wired"; return; }
    [[ -n "$(env_override "$r")" ]] && { echo "hook kept: runner $r sets $ENV_KEY in its .env"; return; }
    v="$(live_value "$r")"
    [[ "$v" == "$UNREADABLE" ]] && { echo "hook kept: cannot read runner $r's listener environment"; return; }
    [[ "$v" == "$HOOK" ]] && { echo "hook kept (MODE=observe): runner $r still holds it until its next restart"; return; }
  done
  rm -f "$HOOK" "$CONF"
  rmdir "$LIB" "$CONF_DIR" 2>/dev/null || true
  echo "hook and conf removed: no runner is wired or still holds the variable"
}

status() { # prints the state of every runner; returns 2 if a live listener points at a missing hook
  local r u v state dropin busy ov rc=0
  if [[ -x "$HOOK" ]]; then
    if [[ -f "$HOOK_SRC" ]] && cmp -s "$HOOK" "$HOOK_SRC"; then echo "hook: $HOOK (matches the bundled copy)"
    elif [[ -f "$HOOK_SRC" ]]; then echo "hook: $HOOK (DIFFERS from the bundled copy)"
    else echo "hook: $HOOK"; fi
  else
    echo "hook: not installed"
  fi
  if [[ -e "$CONF" ]]; then echo "mode: $(effective_mode) ($CONF)"; else echo "mode: no conf (the hook would enforce)"; fi
  if [[ "$(conf_value WIPE_HOME)" == 1 ]]; then echo "home reset: ON for $(conf_value WIPE_HOME_USERS) (under mode $(effective_mode))"
  else echo "home reset: off"; fi
  toolcache_status
  for r in "${RUNNERS[@]}"; do
    u="$(unit_of "$r")"
    dropin=absent; [[ -e "$(dropin_of "$r")" ]] && dropin=present
    v="$(live_value "$r")"
    case "$v" in
      "$NO_LISTENER"|"$UNREADABLE") state="$v" ;;
      "") state="not live" ;;
      "$HOOK") state="LIVE" ;;
      *) state="live with an unexpected value '$v'" ;;
    esac
    busy="$(pids_of "$r" Worker | tr '\n' ' ')"
    job=idle; [[ -n "$busy" ]] && job="running (Runner.Worker ${busy% })"
    echo "runner $r ($u): active=$(systemctl is-active "$u" 2>/dev/null) drop-in=$dropin hook-variable=$state job=$job"
    [[ "$dropin" == present && "$state" != LIVE ]] && echo "  pending: wired on disk, not live until runner $r restarts"
    [[ "$dropin" == absent && "$state" == LIVE ]] && echo "  pending: unwired on disk, the hook stays live until runner $r restarts"
    ov="$(env_override "$r")"
    [[ -n "$ov" ]] && echo "  WARNING: $(dir_of "$r")/.env sets $ov; the listener reads it at start and it overrides the drop-in"
    if [[ "$state" == LIVE && ! -x "$HOOK" ]]; then
      echo "  BROKEN: runner $r's listener calls $HOOK, which is not installed; every job on it fails"
      rc=2
    fi
  done
  return "$rc"
}

action="${1:-}"
[[ $# -gt 0 ]] && shift
sel=all; do_restart=0; skip_observe_check=0; users=""; pins=""; tc_owner=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --runner) [[ $# -ge 2 ]] || die "--runner needs pc, pc-light or all"; sel="$2"; shift 2 ;;
    --restart) do_restart=1; shift ;;
    --skip-observe-check) skip_observe_check=1; shift ;;
    --users) [[ $# -ge 2 ]] || die "--users needs a list of user names"; users="$2"; shift 2 ;;
    --pins) [[ $# -ge 2 ]] || die "--pins needs \"<name>==<version> ...\""; pins="$2"; shift 2 ;;
    --owner) [[ $# -ge 2 ]] || die "--owner needs a user name"; tc_owner="$2"; shift 2 ;;
    *) usage >&2; exit 64 ;;
  esac
done
case "$action" in status|observe|enforce|rollback|home-on|home-off|toolcache-lock|toolcache-unlock) ;; *) usage >&2; exit 64 ;; esac
case "$sel" in
  all) SEL=("${RUNNERS[@]}") ;;
  pc|pc-light) SEL=("$sel") ;;
  *) die "--runner must be pc, pc-light or all" ;;
esac
[[ "$(id -u)" == "$NEED_UID" ]] || die "run as root inside CT202"
for r in "${SEL[@]}"; do
  [[ -d "$(dir_of "$r")" ]] || die "runner $r: $(dir_of "$r") does not exist"
  [[ -e "$UNIT_DIR/$(unit_of "$r")" ]] || die "runner $r: unit $(unit_of "$r") is not installed"
done

rc=0
case "$action" in
  status)
    status; exit $?
    ;;
  observe|enforce)
    if [[ "$action" == enforce && "$skip_observe_check" == 0 ]]; then
      # The mode is global: enforce reaches every wired runner, not only the selected ones.
      for r in "${SEL[@]}" $(wired_elsewhere); do
        observed "$r" || die "runner $r: the journal (-t $LOG_TAG, last 14 days) shows no observe run on its workspace. Run observe first, or pass --skip-observe-check."
      done
    fi
    install_hook
    write_conf "$action"
    for r in "${SEL[@]}"; do wire "$r"; done
    systemctl daemon-reload || die "systemctl daemon-reload failed"
    echo "MODE=$action for every wired runner, from the next job on; wired now: ${SEL[*]}"
    others="$(wired_elsewhere | tr '\n' ' ')"
    [[ -n "$others" ]] && echo "note: the mode is global, so it now applies to ${others% } too"
    if [[ "$do_restart" == 1 ]]; then
      for r in "${SEL[@]}"; do restart_if_idle "$r" wired || rc=3; done
    fi
    ;;
  rollback)
    others="$(wired_elsewhere | tr '\n' ' ')"
    if [[ -z "$others" ]]; then
      if [[ -e "$HOOK" ]]; then
        write_conf observe
        echo "MODE=observe now: from the next job on, the hook removes nothing (no restart needed)"
      fi
    else
      echo "note: ${others% } still wired, so the global mode is left at $(effective_mode); ${SEL[*]} keep running the hook until restarted"
    fi
    for r in "${SEL[@]}"; do unwire "$r"; done
    systemctl daemon-reload || die "systemctl daemon-reload failed"
    if [[ "$do_restart" == 1 ]]; then
      for r in "${SEL[@]}"; do restart_if_idle "$r" unwired || rc=3; done
    fi
    purge_if_unused
    ;;
  home-on)
    [[ -x "$HOOK" && -e "$CONF" ]] || die "home-on: the hook is not installed; run observe first"
    list=()
    read -r -a names <<<"${users//,/ }"   # read, not an unquoted for: no glob expansion
    for u in "${names[@]}"; do home_user_ok "$u"; list+=("$u"); done
    [[ ${#list[@]} -gt 0 ]] || die "home-on needs --users \"<user> [<user>]\""
    write_conf "$(effective_mode)" 1 "${list[*]}"
    echo "home reset ON for ${list[*]} from the next job on (mode $(effective_mode); no restart needed)"
    ;;
  home-off)
    if [[ -e "$CONF" ]]; then write_conf "$(effective_mode)" 0 ""; fi
    echo "home reset off from the next job on"
    ;;
  toolcache-lock)
    read -r -a pinlist <<<"$pins"
    [[ ${#pinlist[@]} -gt 0 ]] || die "toolcache-lock needs --pins \"<name>==<version> ...\" (ci.yml's pip install line)"
    for p in "${pinlist[@]}"; do
      [[ "$p" =~ ^[A-Za-z0-9._-]+==[A-Za-z0-9.+!_-]+$ ]] || die "toolcache-lock: '$p' is not <name>==<version>"
    done
    toolcache_lock "${pinlist[@]}"
    ;;
  toolcache-unlock)
    [[ "$tc_owner" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "toolcache-unlock needs --owner <user>"
    getent passwd "$tc_owner" >/dev/null || die "toolcache-unlock: no such user '$tc_owner'"
    [[ -d "$TOOLCACHE" && ! -L "$TOOLCACHE" ]] || die "toolcache-unlock: $TOOLCACHE is not a directory"
    chown -R -h "$tc_owner:$tc_owner" "$TOOLCACHE" || die "toolcache-unlock: chown failed"
    echo "toolcache handed back to $tc_owner (job-writable again); the $TC_KEY drop-ins are kept"
    ;;
esac
echo "---"
status || rc=2
exit "$rc"
