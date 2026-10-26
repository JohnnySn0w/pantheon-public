#!/usr/bin/env bash
# test_ci_runner_hooks_install.sh - T-4416: ci_runner_hooks/install.sh wires only the runners
# it is asked to, restarts only an idle runner, refuses enforce without observe evidence
# for every runner the global mode reaches, and rolls back safely. Rollback flips to observe
# at once when nothing else is wired, and it never removes the hook while a live listener
# can still call it.
#
# It runs a COPY of install.sh with its paths rewritten to a temp tree: a fake /opt, /etc,
# systemd dir and /proc (exe symlinks plus NUL-separated environ files), with stub
# systemctl, journalctl and logger on PATH. It never touches the real system.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/ci_runner_hooks"
T="$(realpath "$(mktemp -d)")"
# Every path this suite deletes is under $T: never run on without one.
[[ -n "$T" && -d "$T" && "$T" != / ]] || { echo "FAIL: could not create a temp dir"; exit 1; }
trap 'rm -rf "$T"' EXIT
export T_STUB="$T"
KEY=ACTIONS_RUNNER_HOOK_JOB_STARTED
U_PC=actions.runner.example-org-pantheon-config.pve2-pantheon-config.service
U_PL=actions.runner.example-org-pantheon-config.pve2-pantheon-config-light.service
mkdir -p "$T/src" "$T/bin" "$T/systemd" "$T/proc"
cp "$SRC/job-started-fresh-workspace.sh" "$T/src/"

rewrite() { # <uid to require> <out>: install.sh with every system path moved into $T
  sed -e "s#^LIB=/usr/local/lib/pantheon-ci\$#LIB=$T/lib#" \
      -e "s#^CONF_DIR=/etc/pantheon-ci\$#CONF_DIR=$T/etc#" \
      -e "s#^UNIT_DIR=/etc/systemd/system\$#UNIT_DIR=$T/systemd#" \
      -e "s#^OPT=/opt\$#OPT=$T/opt#" \
      -e "s#^PROC=/proc\$#PROC=$T/proc#" \
      -e "s#^OWNER=root:root\$#OWNER=$(id -un):$(id -gn)#" \
      -e "s#^NEED_UID=0\$#NEED_UID=$1#" \
      -e "s#^RESTART_WAIT=60\$#RESTART_WAIT=2#" "$SRC/install.sh" > "$2"
  local n
  n=$(grep -cE "^(LIB|CONF_DIR|UNIT_DIR|OPT|PROC)=$T/|^OWNER=$(id -un):|^NEED_UID=$1\$|^RESTART_WAIT=2\$" "$2")
  if [ "$n" != 8 ]; then
    echo "FAIL: rewrote $n of 8 install.sh constants (did one change shape?); refusing to run it"
    exit 1
  fi
}
rewrite "$(id -u)" "$T/src/install.sh"
rewrite 99999 "$T/src/install-needs-other-uid.sh"

# mkproc <runner> <Listener|Worker> [hook value]: a fake running process for that runner.
cat > "$T/bin/mkproc" <<'EOF'
#!/usr/bin/env bash
case "$1:$2" in pc:Listener) pid=1001 ;; pc:Worker) pid=1002 ;; pc-light:Listener) pid=2001 ;; pc-light:Worker) pid=2002 ;; esac
rm -rf "$T_STUB/proc/$pid"; mkdir -p "$T_STUB/proc/$pid"
ln -s "$T_STUB/opt/actions-runner-$1/bin.2.337.0/Runner.$2" "$T_STUB/proc/$pid/exe"
if [ -n "${3:-}" ]; then printf 'LANG=C\0ACTIONS_RUNNER_HOOK_JOB_STARTED=%s\0' "$3"; else printf 'LANG=C\0'; fi \
  > "$T_STUB/proc/$pid/environ"
EOF
# systemctl: logs every call; restart respawns the listener with whatever the drop-in says.
cat > "$T/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "$T_STUB/systemctl.log"
case "$1" in
  is-active) echo active ;;
  daemon-reload) [ ! -e "$T_STUB/fail-reload" ] ;;
  restart)
    [ -e "$T_STUB/fail-restart" ] && exit 1
    case "$2" in *pantheon-config-light.service) r=pc-light ;; *) r=pc ;; esac
    v=$(sed -n 's/^Environment=ACTIONS_RUNNER_HOOK_JOB_STARTED=//p' "$T_STUB/systemd/$2.d/pantheon-fresh-workspace.conf" 2>/dev/null)
    [ -e "$T_STUB/restart-noenv" ] && v=
    mkproc "$r" Listener "$v" ;;
esac
EOF
# logger/journalctl: the hook's syslog lines under its tag, and only that tag, read back.
cat > "$T/bin/logger" <<'EOF'
#!/usr/bin/env bash
tag=
while [ $# -gt 0 ]; do case "$1" in -t) tag=$2; shift 2 ;; --) shift; break ;; *) break ;; esac; done
echo "$tag|$*" >> "$T_STUB/syslog.txt"
EOF
cat > "$T/bin/journalctl" <<'EOF'
#!/usr/bin/env bash
tag=
while [ $# -gt 0 ]; do case "$1" in -t) tag=$2; shift 2 ;; *) shift ;; esac; done
[ -n "$tag" ] && sed -n "s/^$tag|//p" "$T_STUB/syslog.txt" 2>/dev/null
exit 0
EOF
# T-4425 stubs: `stat -c %U -- <path>` answers from $T/owners ("<path> <user>" lines) and falls
# through to the real stat otherwise; `getent passwd <name>` succeeds for names in $T/users.
REAL_STAT="$(command -v stat)"
cat > "$T/bin/stat" <<EOF
#!/usr/bin/env bash
if [ "\$1" = -c ] && [ "\$2" = %U ]; then
  p="\${@: -1}"; u=\$(awk -v p="\$p" '\$1 == p { print \$2 }' "\$T_STUB/owners" 2>/dev/null)
  [ -n "\$u" ] && { echo "\$u"; exit 0; }
fi
exec "$REAL_STAT" "\$@"
EOF
cat > "$T/bin/getent" <<'EOF'
#!/usr/bin/env bash
[ "$1" = passwd ] && grep -qxF -e "$2" "$T_STUB/users" 2>/dev/null && { echo "$2:x:1:1::/home/$2:/bin/bash"; exit 0; }
exit 2
EOF
chmod +x "$T/bin/"*
export PATH="$T/bin:$PATH"

pass=0; fail=0
check() { # <name> <got> <want>
  if [ "$2" = "$3" ]; then pass=$((pass + 1)); else fail=$((fail + 1)); echo "FAIL: $1 (got '$2', want '$3')"; fi
}
has() { grep -cF -e "$2" <<<"$1"; } # <text> <literal>: prints the matching line count
inst() { out="$(bash "$T/src/install.sh" "$@" 2>&1)"; rc=$?; } # sets $out and $rc
reset() { # a clean CT202: both runners installed, running and idle, nothing wired
  rm -rf "${T:?}/lib" "${T:?}/etc" "${T:?}/systemd" "${T:?}/proc" "${T:?}/opt" "${T:?}/syslog.txt" \
    "${T:?}/systemctl.log" "${T:?}/fail-reload" "${T:?}/fail-restart" "${T:?}/restart-noenv" \
    "${T:?}/owners" "${T:?}/users"
  mkdir -p "$T/systemd" "$T/proc"
  for r in pc pc-light; do
    mkdir -p "$T/opt/actions-runner-$r"; printf 'LANG=C\nAGENT_TOOLSDIRECTORY=/opt/hostedtoolcache\n' > "$T/opt/actions-runner-$r/.env"
    mkproc "$r" Listener
  done
  : > "$T/systemd/$U_PC"; : > "$T/systemd/$U_PL"
}
observe_run() { # <runner>: the REAL hook, in observe, on that runner's workspace -> syslog
  local ws="$T/opt/actions-runner-$1/_work/pantheon-config/pantheon-config"
  mkdir -p "$ws/.git"
  printf 'MODE=observe\n' > "$T/obs.conf"
  sed -e "s#^CONF=/etc/pantheon-ci/fresh-workspace\.conf\$#CONF=$T/obs.conf#" \
      -e "s#^ROOT=/opt\$#ROOT=$T/opt#" "$SRC/job-started-fresh-workspace.sh" > "$T/hookrun.sh"
  GITHUB_WORKSPACE="$ws" bash -e "$T/hookrun.sh" >/dev/null 2>&1
}
calls() { grep -cxF -e "$1" "$T/systemctl.log" 2>/dev/null; } # count of an exact systemctl call
HOOK="$T/lib/job-started-fresh-workspace.sh"; CONF="$T/etc/fresh-workspace.conf"
DROP_PC="$T/systemd/$U_PC.d/pantheon-fresh-workspace.conf"; DROP_PL="$T/systemd/$U_PL.d/pantheon-fresh-workspace.conf"

# 1. usage and preflight refuse before touching anything
reset
inst; check "no action is a usage error" "$rc" "64"
inst frobnicate; check "an unknown action is a usage error" "$rc" "64"
inst observe --bogus; check "an unknown flag is a usage error" "$rc" "64"
inst observe --runner echo; check "an unknown runner is refused" "$rc" "1"
inst observe --runner; check "--runner without a value is refused" "$rc" "1"
out="$(bash "$T/src/install-needs-other-uid.sh" observe 2>&1)"; rc=$?
check "the wrong uid is refused" "$rc" "1"; check "the wrong uid says root" "$(has "$out" "run as root")" "1"
rm -rf "$T/opt/actions-runner-pc"
inst observe --runner pc; check "a missing runner dir is refused" "$rc" "1"
rm -f "$T/systemd/$U_PL"
inst observe --runner pc-light; check "a missing unit is refused" "$rc" "1"
check "nothing was installed by the refusals" "$([ -e "$T/lib" ] || [ -e "$T/etc" ] || [ -e "$T/systemd/$U_PL.d" ] && echo touched)" ""

# 2. observe --runner pc-light: installs, wires pc-light only, no restart without --restart
reset
inst observe --runner pc-light
check "observe exits 0" "$rc" "0"
check "the hook is the bundled bytes" "$(cmp -s "$HOOK" "$SRC/job-started-fresh-workspace.sh" && echo same)" "same"
check "the hook is 0755" "$(stat -c %a "$HOOK")" "755"
check "the conf says observe" "$(grep -x 'MODE=observe' "$CONF")" "MODE=observe"
check "the conf is 0644" "$(stat -c %a "$CONF")" "644"
check "pc-light's drop-in sets the variable to the hook" \
  "$(grep -xF "Environment=$KEY=$HOOK" "$DROP_PL" && grep -cx '\[Service\]' "$DROP_PL")" "Environment=$KEY=$HOOK
1"
check "the drop-in is 0644" "$(stat -c %a "$DROP_PL")" "644"
check "pc is not wired" "$([ -e "$DROP_PC" ] && echo wired)" ""
check "no temp .new file is left" "$(find "$T/lib" "$T/etc" "$T/systemd" -name '*.new' | wc -l | tr -d ' ')" "0"
check "daemon-reload ran" "$(calls daemon-reload)" "1"
check "no restart without --restart" "$(grep -c '^restart' "$T/systemctl.log")" "0"
check "status says pc-light is pending a restart" "$(has "$out" "pending: wired on disk, not live until runner pc-light restarts")" "1"

# 3. --restart on an idle runner makes the listener live; a busy one is left alone (exit 3)
inst observe --runner pc-light --restart
check "idle restart exits 0" "$rc" "0"
check "idle restart restarted pc-light" "$(calls "restart $U_PL")" "1"
check "idle restart says wired" "$(has "$out" "runner pc-light: restarted; its listener is now wired")" "1"
check "status shows pc-light LIVE" "$(has "$out" "drop-in=present hook-variable=LIVE job=idle")" "1"
mkproc pc-light Worker
inst observe --runner pc-light --restart
check "busy restart exits 3" "$rc" "3"
check "busy restart did not restart" "$(calls "restart $U_PL")" "1"
check "busy restart says why" "$(has "$out" "runner pc-light: NOT restarted: a job is running (Runner.Worker 2002)")" "1"
check "status shows the running job" "$(has "$out" "job=running (Runner.Worker 2002)")" "1"
# pc's own restart is not blocked by pc-light's job (the /bin anchor: pc must not match pc-light)
inst observe --runner pc --restart
check "pc restarts while pc-light is busy" "$(calls "restart $U_PC")" "1"
check "that pc restart exits 0" "$rc" "0"
check "wiring pc names pc-light in the global note" "$(has "$out" "note: the mode is global, so it now applies to pc-light too")" "1"

# 4. a restart that does not end wired is exit 3, not a claimed success
reset
touch "$T/restart-noenv"
inst observe --runner pc --restart
check "unconfirmed restart exits 3" "$rc" "3"
check "unconfirmed restart says so" "$(has "$out" "runner pc: restarted, but after 2s its listener is not wired")" "1"
rm -f "$T/restart-noenv"; touch "$T/fail-restart"
inst observe --runner pc --restart
check "a failed restart exits 3" "$rc" "3"
check "a failed restart says so" "$(has "$out" "systemctl restart $U_PC FAILED")" "1"

# 5. enforce needs observe evidence from the journal for EVERY runner the global mode reaches
reset
inst observe --runner all --restart
inst enforce --runner pc-light
check "enforce with no evidence is refused" "$rc" "1"
check "the refusal names the runner" "$(has "$out" "runner pc-light: the journal")" "1"
check "the refused enforce left the mode at observe" "$(grep -x 'MODE=observe' "$CONF")" "MODE=observe"
observe_run pc-light
inst enforce --runner pc-light
check "evidence for pc-light only: enforce --runner pc-light still refused (pc is wired)" "$rc" "1"
check "that refusal names pc" "$(has "$out" "runner pc: the journal")" "1"
inst enforce --runner pc
check "pc-light's evidence does not count for pc" "$rc" "1"
observe_run pc
inst enforce
check "evidence for both: enforce exits 0" "$rc" "0"
check "the conf says enforce" "$(grep -x 'MODE=enforce' "$CONF")" "MODE=enforce"
reset
inst enforce --runner pc --skip-observe-check
check "--skip-observe-check bypasses the evidence check" "$rc" "0"
check "and the conf says enforce" "$(grep -x 'MODE=enforce' "$CONF")" "MODE=enforce"

# 6. the mode is global: a change on one runner is announced for the others it reaches
reset
inst observe --runner all
inst enforce --runner pc --skip-observe-check
check "the global note names the other wired runner" "$(has "$out" "note: the mode is global, so it now applies to pc-light too")" "1"

# 7. rollback of everything: observe at once, unwire, and keep the hook while a listener holds it
reset
inst enforce --runner all --restart --skip-observe-check
inst rollback
check "rollback exits 0" "$rc" "0"
check "rollback flips the mode to observe at once" "$(grep -x 'MODE=observe' "$CONF")" "MODE=observe"
check "rollback unwires both" "$([ -e "$DROP_PC" ] || [ -e "$DROP_PL" ] && echo wired)" ""
check "rollback removes the empty drop-in dirs" "$([ -d "$T/systemd/$U_PC.d" ] && echo left)" ""
check "the hook is kept while listeners still hold it" "$([ -x "$HOOK" ] && echo kept)" "kept"
check "rollback says why the hook is kept" "$(has "$out" "hook kept (MODE=observe): runner pc still holds it")" "1"
check "status says the hook stays live until restart" "$(has "$out" "pending: unwired on disk, the hook stays live until runner pc restarts")" "1"
inst rollback --restart
check "rollback --restart exits 0" "$rc" "0"
check "both listeners restarted unwired" "$(has "$out" "its listener is now unwired")" "2"
check "the hook is removed once nothing holds it" "$([ -e "$HOOK" ] && echo kept)" ""
check "the conf is removed too" "$([ -e "$CONF" ] && echo kept)" ""
check "status after purge" "$(has "$out" "hook: not installed")" "1"

# 8. rollback of one runner while the other is wired leaves the global mode alone
reset
inst enforce --runner all --restart --skip-observe-check
inst rollback --runner pc
check "partial rollback exits 0" "$rc" "0"
check "partial rollback keeps enforce for the still-wired runner" "$(grep -x 'MODE=enforce' "$CONF")" "MODE=enforce"
check "partial rollback says so" "$(has "$out" "note: pc-light still wired, so the global mode is left at enforce")" "1"
check "partial rollback unwires pc only" "$([ -e "$DROP_PC" ] && echo pc)$([ -e "$DROP_PL" ] && echo pl)" "pl"
check "partial rollback keeps the hook" "$([ -x "$HOOK" ] && echo kept)" "kept"

# 9. a .env override: flagged by status, and it keeps the hook from being purged
reset
inst observe --runner pc --restart
echo "$KEY=$HOOK" >> "$T/opt/actions-runner-pc/.env"
inst status
check "status flags the .env override" "$(has "$out" "WARNING: $T/opt/actions-runner-pc/.env sets $KEY=")" "1"
inst rollback --restart
check "the .env override keeps the hook" "$(has "$out" "hook kept: runner pc sets $KEY in its .env")" "1"
check "and the hook is still there" "$([ -x "$HOOK" ] && echo kept)" "kept"

# 10. a live listener pointing at a missing hook is BROKEN (exit 2)
reset
mkproc pc Listener "$HOOK"
inst status
check "status exits 2 on a missing live hook" "$rc" "2"
check "status says BROKEN" "$(has "$out" "BROKEN: runner pc's listener calls")" "1"

# 11. status does not claim "not live" when it cannot see a listener
reset
rm -rf "$T/proc/1001"
inst status
check "no listener is reported as such" "$(has "$out" "runner pc ($U_PC): active=active drop-in=absent hook-variable=<no listener>")" "1"
if [ "$(id -u)" != 0 ]; then
  chmod 000 "$T/proc/2001/environ"
  inst status
  check "an unreadable environ is reported as such" "$(has "$out" "hook-variable=<unreadable>")" "1"
  chmod 644 "$T/proc/2001/environ"
fi

# 12. home-on (T-4425) refuses unless each user owns exactly one pc runner and no other runner
reset
inst home-on --users runner-pc; check "home-on before the hook is installed is refused" "$rc:$(has "$out" "run observe first")" "1:1"
inst observe --runner all
inst home-on; check "home-on without --users is refused" "$rc:$(has "$out" "home-on needs --users")" "1:1"
mkdir -p "$T/opt/actions-runner-echo-prismatic"
printf 'runner\nrunner-pc\nrunner-pcl\n' > "$T/users"
printf '%s runner\n%s runner\n%s runner\n' "$T/opt/actions-runner-pc" "$T/opt/actions-runner-pc-light" \
  "$T/opt/actions-runner-echo-prismatic" > "$T/owners"
inst home-on --users runner
check "the shared uid (it owns the echo runner too) is refused" \
  "$rc:$(has "$out" "runner also owns $T/opt/actions-runner-echo-prismatic")" "1:1"
check "a refused home-on leaves the conf without WIPE_HOME" "$(grep -c '^WIPE_HOME' "$CONF")" "0"
printf '%s runner-pc\n%s runner-pc\n%s runner\n' "$T/opt/actions-runner-pc" "$T/opt/actions-runner-pc-light" \
  "$T/opt/actions-runner-echo-prismatic" > "$T/owners"
inst home-on --users runner-pc
check "a uid owning BOTH pc runners is refused" "$rc:$(has "$out" "runner-pc owns 2 pantheon-config runner installs")" "1:1"
printf '%s runner-pc\n%s runner-pcl\n%s runner\n' "$T/opt/actions-runner-pc" "$T/opt/actions-runner-pc-light" \
  "$T/opt/actions-runner-echo-prismatic" > "$T/owners"
inst home-on --users "runner-pc nobody-here"; check "an unknown user is refused" "$rc:$(has "$out" "no such user 'nobody-here'")" "1:1"
inst home-on --users "runner-pc;x"; check "a malformed name is refused" "$rc:$(has "$out" "'runner-pc;x' is not a user name")" "1:1"
( cd "$T/opt" && inst home-on --users "*" && echo "$rc:$(has "$out" "'*' is not a user name")" ) > "$T/glob.out"
check "a glob is not expanded (it is refused as a name)" "$(cat "$T/glob.out")" "1:1"
check "no refusal wrote WIPE_HOME" "$(grep -c '^WIPE_HOME' "$CONF")" "0"

# 13. home-on with one uid per runner: written, reported, kept across mode changes, then off
inst home-on --users "runner-pc,runner-pcl"
check "home-on exits 0" "$rc" "0"
check "home-on writes the flag and the list, keeping the mode" \
  "$(grep -xE 'MODE=observe|WIPE_HOME=1|WIPE_HOME_USERS=runner-pc runner-pcl' "$CONF" | wc -l | tr -d ' ')" "3"
inst status; check "status reports it" "$(has "$out" "home reset: ON for runner-pc runner-pcl (under mode observe)")" "1"
observe_run pc; observe_run pc-light
inst enforce
check "enforce keeps the home settings" "$(grep -xE 'MODE=enforce|WIPE_HOME=1|WIPE_HOME_USERS=runner-pc runner-pcl' "$CONF" | wc -l | tr -d ' ')" "3"
inst rollback --runner pc
check "a partial rollback keeps them too" "$(grep -cxE 'WIPE_HOME=1|WIPE_HOME_USERS=runner-pc runner-pcl' "$CONF")" "2"
inst home-off
check "home-off exits 0 and removes them" "$rc:$(grep -c '^WIPE_HOME' "$CONF")" "0:0"
check "home-off keeps the mode" "$(grep -cx 'MODE=enforce' "$CONF")" "1"
inst status; check "status says off" "$(has "$out" "home reset: off")" "1"

# 14. toolcache-lock (T-4425): refuses on a missing pin or an unexpected .pth and changes
#     nothing; otherwise strips group/other write, writes the AGENT_TOOLSDIRECTORY drop-ins, and
#     status tells locked from unlocked. It reads METADATA and never runs the toolcache's Python.
TC="$T/opt/hostedtoolcache"
mktc() { # two interpreters holding ci.yml's pins (names spelled as their METADATA spells them)
  local v s d
  for v in 3.11.15:3.11 3.12.13:3.12; do
    s="$TC/Python/${v%%:*}/x64/lib/python${v#*:}/site-packages"
    for d in PyYAML:6.0.3:pyyaml pytest:9.1.1:pytest pytest-xdist:3.8.0:pytest_xdist pip:26.2.1:pip; do
      IFS=: read -r name ver dir <<<"$d"
      mkdir -p "$s/$dir-$ver.dist-info"
      printf 'Metadata-Version: 2.1\nName: %s\nVersion: %s\n\nName: not-a-header\n' "$name" "$ver" > "$s/$dir-$ver.dist-info/METADATA"
    done
    mkdir -p "$TC/Python/${v%%:*}/x64/bin"
    printf '#!/bin/sh\ntouch "%s/python-ran"\n' "$T" > "$TC/Python/${v%%:*}/x64/bin/python3"
    chmod 0775 "$TC/Python/${v%%:*}/x64/bin/python3" "$s"
  done
  : > "$TC/Python/3.11.15/x64/lib/python3.11/site-packages/distutils-precedence.pth"
}
PINS="pyyaml==6.0.3 pytest==9.1.1 pytest-xdist==3.8.0"
reset; rm -f "$T/python-ran"; mktc
inst status; check "status: a group-writable toolcache is UNLOCKED" "$(has "$out" "toolcache: $TC UNLOCKED")" "1"
check "status: counts each interpreter's distributions" "$(has "$out" "site-packages: 4 distributions")" "2"
check "status flags the .env copy of AGENT_TOOLSDIRECTORY" "$(has "$out" "runner pc: AGENT_TOOLSDIRECTORY is also set in its runner-owned .env")" "1"
inst toolcache-lock; check "lock without --pins is refused" "$rc:$(has "$out" "needs --pins")" "1:1"
inst toolcache-lock --pins "pytest"; check "a malformed pin is refused" "$rc:$(has "$out" "'pytest' is not <name>==<version>")" "1:1"
writable_before="$(find "$TC" -perm /022 ! -type l | grep -c .)"
inst toolcache-lock --pins "$PINS pytest==9.9.9"
check "a pin at another version is refused, per interpreter" "$rc:$(has "$out" "MISSING pytest==9.9.9")" "1:2"
check "a refused lock changes nothing" "$(find "$TC" -perm /022 ! -type l | grep -c .)" "$writable_before"
check "(and the fixture really was writable)" "$([ "$writable_before" -gt 0 ] && echo yes)" "yes"
echo 'import os' > "$TC/Python/3.12.13/x64/lib/python3.12/site-packages/evil.pth"
inst toolcache-lock --pins "$PINS"
check "an unexpected .pth is refused and named" "$rc:$(has "$out" "unexpected .pth $TC/Python/3.12.13/x64/lib/python3.12/site-packages/evil.pth")" "1:1"
check "setuptools' own .pth is not flagged" "$(has "$out" "distutils-precedence.pth")" "0"
inst status; check "status warns about it" "$(has "$out" "WARNING: unexpected .pth $TC/Python/3.12.13")" "1"
rm -f "$TC/Python/3.12.13/x64/lib/python3.12/site-packages/evil.pth"
inst toolcache-lock --pins "PyYAML==6.0.3 pytest==9.1.1 pytest_xdist==3.8.0"
check "lock (pins in any PEP 503 spelling) exits 0" "$rc" "0"
check "nothing in the toolcache is group/other-writable" "$(find "$TC" -perm /022 ! -type l | grep -c .)" "0"
check "the AGENT_TOOLSDIRECTORY drop-ins are written for both runners" \
  "$(cat "$T/systemd/$U_PC.d/pantheon-toolcache.conf" "$T/systemd/$U_PL.d/pantheon-toolcache.conf" 2>/dev/null | grep -cxF "Environment=AGENT_TOOLSDIRECTORY=$TC")" "2"
check "lock reloads systemd" "$(calls daemon-reload)" "1"
inst status; check "status says locked" "$(has "$out" "toolcache: $TC locked")" "1"
check "status names the drop-in source" "$(has "$out" "runner pc-light: AGENT_TOOLSDIRECTORY from the root-owned drop-in")" "1"
check "the toolcache's Python never ran" "$([ -e "$T/python-ran" ] && echo ran)" ""
inst toolcache-unlock; check "unlock without --owner is refused" "$rc" "1"
inst toolcache-unlock --owner nobody-here; check "unlock to an unknown user is refused" "$rc:$(has "$out" "no such user")" "1:1"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
