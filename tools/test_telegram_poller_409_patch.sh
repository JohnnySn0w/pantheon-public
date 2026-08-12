#!/usr/bin/env bash
# Regression test for tools/telegram_poller_409_patch.sh (T-2629).
#
# Exercises the durable 409-resilience re-assert against FIXTURES (via the
# T2629_TARGETS override) so the live plugin copies are never touched. Asserts:
#   1. pristine copy (old give-up block, no marker) -> re-patched to the new block
#   2. already-patched copy (marker present)        -> untouched (idempotent)
#   3. unrelated copy (neither block)               -> untouched (upstream-changed no-op)
#   4. helper exits 0 (fail-open) in all cases
#
# Self-reports "N passed, 0 failed" and exits non-zero on any failure.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$HERE/telegram_poller_409_patch.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass=0; fail=0
ok(){ echo "ok: $1"; pass=$((pass+1)); }
bad(){ echo "FAIL: $1"; fail=$((fail+1)); }

# Single source of truth: pull the exact OLD/NEW blocks out of the helper itself,
# so this test can never drift from what the helper actually substitutes.
read_block(){ python3 - "$HELPER" "$1" <<'PY'
import re,sys
h=open(sys.argv[1]).read()
m=re.search(r"read -r -d '' %s <<'EOF'\n(.*?)\nEOF"%re.escape(sys.argv[2]),h,re.S)
sys.stdout.write(m.group(1) if m else "")
PY
}
OLD="$(read_block OLD_BLOCK)"; NEW="$(read_block NEW_BLOCK)"
[ -n "$OLD" ] && [ -n "$NEW" ] || { echo "FAIL: could not extract OLD/NEW blocks from helper"; exit 1; }

# Fixtures wrapped in minimal surrounding context (the helper matches the block
# exactly regardless of surroundings).
pristine="$TMP/pristine_server.ts"
patched="$TMP/patched_server.ts"
unrelated="$TMP/unrelated_server.ts"
printf 'const before = 1\n%s\nconst after = 2\n' "$OLD" > "$pristine"
printf 'const before = 1\n%s\nconst after = 2\n' "$NEW" > "$patched"
printf 'const wholly = "different"\nfunction f(){ return 42 }\n' > "$unrelated"

patched_before="$(cat "$patched")"
unrelated_before="$(cat "$unrelated")"

T2629_TARGETS="$pristine
$patched
$unrelated" bash "$HELPER"
rc=$?
[ "$rc" -eq 0 ] && ok "helper exits 0 (fail-open)" || bad "helper exit $rc (want 0)"

# 1. pristine re-patched
if grep -q 'T-2629: never give up' "$pristine" && ! grep -q 'attempt >= 8) {' "$pristine"; then
  ok "pristine copy re-patched (marker present, old give-up gone)"
else
  bad "pristine copy NOT re-patched"
fi
# the re-patched pristine body must now equal the patched fixture body byte-for-byte
if [ "$(cat "$pristine")" = "$patched_before" ]; then
  ok "re-patched pristine is byte-identical to the expected patched form"
else
  bad "re-patched pristine differs from expected patched form"
fi
# 2. idempotent
if [ "$(cat "$patched")" = "$patched_before" ]; then
  ok "already-patched copy untouched (idempotent)"
else
  bad "already-patched copy was modified"
fi
# 3. upstream-changed no-op
if [ "$(cat "$unrelated")" = "$unrelated_before" ]; then
  ok "unrelated copy untouched (no old block, no-op)"
else
  bad "unrelated copy was modified"
fi

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
