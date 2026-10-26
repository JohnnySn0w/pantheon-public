#!/usr/bin/env bash
# confined_load.sh - reproduce a load-dependent flake WITHOUT starving the shared host (T-4349).
#
# Starts N busy-loop burners and runs COMMAND, all pinned to ONE CPU. COMMAND
# competes for that core exactly as it would on a saturated box, while the other
# cores stay free for every other seat. Each burner stops at its time cap or as soon
# as this helper dies (even by SIGKILL), and cleanup kills them when COMMAND exits.
#
# Usage: confined_load.sh [-c CPU] [-n BURNERS] [-t SECONDS] -- COMMAND [ARGS...]
#   -c CPU      CPU to pin to (default: the highest-numbered CPU this process may use)
#   -n BURNERS  burners on that CPU (default 3, range 1-8)
#   -t SECONDS  hard cap on each burner's lifetime (default 600, range 1-1800)
# Exit status: COMMAND's exit status, or 64 on a usage error.
#
# Why: on 2026-09-30, full-host burners (16 unpinned `timeout 3600` busy loops for a
# flake repro) drove the 4-core fleet VPS to load 35 and CPU PSI 96%, slowing every
# seat. Pinning gives the test the same contention on one core and leaves the rest free.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"

usage() {
  sed -n '9,13p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 64
}

is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }

# The CPUs this process may actually run on. Not 0..nproc-1: inside an LXC/cgroup
# cpuset (e.g. a CI runner container after a host reboot) the allowed ids can be any
# subset of the host's CPUs, so `nproc` counts them but says nothing about which ids
# they are (T-4693).
allowed_cpus() {
  local list part a b
  list=$(sed -n 's/^Cpus_allowed_list:[[:space:]]*//p' /proc/self/status)
  for part in ${list//,/ }; do
    if [[ "$part" == *-* ]]; then a=${part%-*}; b=${part#*-}; seq "$a" "$b"; else echo "$part"; fi
  done
}
mapfile -t allowed < <(allowed_cpus)
[ ${#allowed[@]} -ge 1 ] || { echo "confined_load: cannot read the allowed CPU list" >&2; exit 64; }
cpu=${allowed[-1]}
n=3
t=600
while getopts ":c:n:t:h" opt; do
  case "$opt" in
    c) cpu=$OPTARG ;;
    n) n=$OPTARG ;;
    t) t=$OPTARG ;;
    *) usage ;;
  esac
done
shift $((OPTIND - 1))
[ "${1:-}" = "--" ] && shift
[ $# -ge 1 ] || usage
is_allowed() { local c; for c in "${allowed[@]}"; do [ "$c" = "$1" ] && return 0; done; return 1; }
is_uint "$cpu" && is_allowed "$cpu" || { echo "confined_load: -c must be an allowed CPU (${allowed[*]})" >&2; exit 64; }
is_uint "$n" && [ "$n" -ge 1 ] && [ "$n" -le 8 ] || { echo "confined_load: -n must be 1..8" >&2; exit 64; }
is_uint "$t" && [ "$t" -ge 1 ] && [ "$t" -le 1800 ] || { echo "confined_load: -t must be 1..1800" >&2; exit 64; }

# Every burner carries this tag in its argv, so tests (and a human) can find exactly
# this run's burners.
tag="confined-load-$$"
pids=()
cleanup() {
  [ ${#pids[@]} -gt 0 ] || return 0
  kill "${pids[@]}" 2>/dev/null
  wait "${pids[@]}" 2>/dev/null
}
# bash runs the EXIT trap on INT/TERM too, so one trap covers both (exit 130/143).
trap cleanup EXIT

for _ in $(seq "$n"); do
  # taskset execs python, so $! IS the burner (no wrapper: see confined_load_burner.py).
  taskset -c "$cpu" python3 "$HERE/confined_load_burner.py" "$t" "$$" "$tag" &
  pids+=("$!")
done
echo "confined_load: $n burner(s) on cpu $cpu (tag $tag, cap ${t}s); running: $*" >&2

taskset -c "$cpu" "$@"
