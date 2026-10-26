#!/usr/bin/env bash
# test_confined_load.sh - T-4349: confined_load.sh pins its burners AND the command to
# one CPU, bounds the burners, cleans them up on exit and on TERM, and passes the
# command's exit status through. Keeps the load tiny (one burner, a few seconds).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
CL="$HERE/confined_load.sh"
pass=0; fail=0
check() { # <name> <got> <want>
  if [ "$2" = "$3" ]; then pass=$((pass + 1)); else fail=$((fail + 1)); echo "FAIL: $1 (got '$2', want '$3')"; fi
}
burners_for() { # <helper pid> -> live burners carrying that run's tag
  pgrep -u "$(id -u)" -fc "^python3 [^ ]*confined_load_burner\.py [0-9]+ [0-9]+ confined-load-$1\$" 2>/dev/null || true
}
all_burners() { pgrep -u "$(id -u)" -fc "^python3 [^ ]*confined_load_burner\.py " 2>/dev/null || true; }
wait_count() { # <helper pid> <want> <max secs>: poll until the burner count is <want>; print the last count
  local end=$(( $(date +%s%N) + $3 * 1000000000 )) c
  while :; do
    c=$(burners_for "$1")
    [ "$c" = "$2" ] && break
    [ "$(date +%s%N)" -ge "$end" ] && break
    sleep 0.1
  done
  echo "$c"
}
# The CPUs this shell may run on (T-4693: not 0..nproc-1 inside a cpuset/LXC).
allowed=$(sed -n 's/^Cpus_allowed_list:[[:space:]]*//p' /proc/self/status)
cpu=$(echo "$allowed" | tr ',' '\n' | tail -1 | sed 's/.*-//')

# 1. usage errors exit 64 and start nothing
bash "$CL" >/dev/null 2>&1; check "no command is a usage error" "$?" "64"
bash "$CL" -n 0 -- true >/dev/null 2>&1; check "-n 0 is refused" "$?" "64"
bash "$CL" -n 9 -- true >/dev/null 2>&1; check "-n 9 is refused" "$?" "64"
bash "$CL" -t 1801 -- true >/dev/null 2>&1; check "-t 1801 is refused" "$?" "64"
bash "$CL" -c "$((cpu + 1))" -- true >/dev/null 2>&1; check "-c outside the allowed cpus is refused" "$?" "64"

# 1b. T-4693: inside a cpuset whose ids don't start at 0, the default cpu is the allowed one.
# Restricting this run to a single non-zero cpu makes nproc=1, so the old 0..nproc-1
# logic picked cpu 0, which taskset then refused (the CI-runner failure after a reboot).
if [ "$cpu" -gt 0 ]; then
  aff=$(taskset -c "$cpu" bash "$CL" -n 1 -t 30 -- bash -c 'taskset -pc $$' 2>/dev/null | sed 's/.*: //')
  check "default cpu is the allowed one under a non-zero cpuset" "$aff" "$cpu"
fi

# 2. the command's exit status passes through
bash "$CL" -n 1 -t 30 -- bash -c 'exit 7' 2>/dev/null; check "exit status passes through" "$?" "7"

# 3. the command runs pinned to the chosen cpu
aff=$(bash "$CL" -c "$cpu" -n 1 -t 30 -- bash -c 'taskset -pc $$' 2>/dev/null | sed 's/.*: //')
check "command is pinned to cpu $cpu" "$aff" "$cpu"

# 4. while the command runs, each burner is alive and pinned to the same cpu
out=$(bash "$CL" -c "$cpu" -n 2 -t 30 -- bash -c '
  pat="^python3 [^ ]*confined_load_burner\.py [0-9]+ [0-9]+ confined-load-$PPID\$"
  for _ in $(seq 50); do [ "$(pgrep -u "$(id -u)" -fc "$pat")" = 2 ] && break; sleep 0.1; done
  for p in $(pgrep -u "$(id -u)" -f "$pat"); do taskset -pc "$p" | sed "s/.*: //"; done
' 2>/dev/null | sort -u | tr '\n' ' ')
check "both burners run pinned to cpu $cpu" "$out" "$cpu "

# 5. burners are gone once the command exits
bash "$CL" -n 2 -t 30 -- sleep 2 2>/dev/null &
h=$!
check "burners are live mid-run" "$(wait_count "$h" 2 5)" "2"
wait "$h"
# synchronous: the EXIT trap waits for its burners, so none may remain at all
check "no burners right after a normal exit" "$(burners_for "$h")" "0"

# 6. burners are gone when the helper is TERMed mid-run
bash "$CL" -n 2 -t 30 -- sleep 20 2>/dev/null &
h=$!
wait_count "$h" 2 5 >/dev/null
kill -TERM "$h"; wait "$h" 2>/dev/null; rc=$?
check "TERM exits 143" "$rc" "143"
check "no burners after TERM" "$(wait_count "$h" 0 5)" "0"
pkill -u "$(id -u)" -f "[s]leep 20$" 2>/dev/null || true

# 7. the -t cap ends the burners even while the command keeps running
bash "$CL" -n 1 -t 1 -- sleep 8 2>/dev/null &
h=$!
wait_count "$h" 1 5 >/dev/null
check "burners are capped by -t" "$(wait_count "$h" 0 6)" "0"
kill -TERM "$h" 2>/dev/null; wait "$h" 2>/dev/null

# 8. a helper killed uncleanly (SIGKILL: no trap runs) leaves no burner behind and
#    no burner holding the caller's output pipe: orphans exit on their own
tmp=$(mktemp -d); mkfifo "$tmp/p"
bash "$CL" -n 2 -t 60 -- sleep 30 >"$tmp/p" 2>/dev/null &
h=$!
exec 3<"$tmp/p"; wait_count "$h" 2 5 >/dev/null
pkill -KILL -P "$h" -x sleep 2>/dev/null; kill -KILL "$h" 2>/dev/null; wait "$h" 2>/dev/null
# T-4592: cat returns the moment the last orphaned burner closes the pipe; 15s only bounds a
# loaded runner (a 3s bound flaked on shared CT202 with three PR runs at once).
timeout 15 cat <&3 >/dev/null; check "no burner holds the caller's pipe after SIGKILL" "$?" "0"
exec 3<&-
check "orphaned burners exit on their own after SIGKILL" "$(wait_count "$h" 0 15)" "0"
pkill -u "$(id -u)" -f "^python3 [^ ]*confined_load_burner\.py [0-9]+ [0-9]+ confined-load-$h\$" 2>/dev/null || true
rm -rf "$tmp"

# 9. fast-exit runs leave nothing behind (a wrapper-based cap leaked 2 of 40 uncapped)
before=$(all_burners)
for _ in $(seq 30); do bash "$CL" -n 1 -t 60 -- true 2>/dev/null; done
end=$(( $(date +%s) + 3 )); while [ "$(all_burners)" != "$before" ] && [ "$(date +%s)" -lt "$end" ]; do sleep 0.1; done
check "30 fast-exit runs leave no burner behind" "$(all_burners)" "$before"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
