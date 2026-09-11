#!/usr/bin/env bash
# pantheon_metrics_push.sh — regenerate the PANTHEON metrics JSON and push it to
# CT103, whose caddy file_server fronts pantheon-metrics.snowis.me (CF-proxied).
#
# Runs as a systemd --user oneshot on recovery (pantheon-metrics-push.timer, ~90s),
# deliberately UNDER specialist's 2min stale-fallback threshold (T-3625 / T-3626).
#
# Architecture: the swarm GENERATES the doc (reads /proc + agent accounts) and
# PUSHES it to CT103. CT103's caddy cannot reach the swarm tailnet, so it is a
# push (ssh + tee), not a reverse_proxy. The generator's leak_scan() is the
# safety gate: it raises (writing nothing) on any output outside the coarse
# allowlist, so a violation aborts here before anything reaches the public edge.
set -euo pipefail

REPO="${PANTHEON_REPO:-$HOME/pantheon-config}"
GEN="$REPO/tools/pantheon_metrics_gen.py"
SSH_HOST="${METRICS_SSH_HOST:-boxon}"
CT="${METRICS_CT:-103}"
REMOTE_PATH="${METRICS_REMOTE_PATH:-/srv/pantheon-metrics/data.json}"

tmp="$(mktemp)"
cleanup() { rm -f "$tmp"; }
trap cleanup EXIT

# 1. Generate. leak_scan() inside the generator raises on any allowlist
#    violation -> non-zero -> set -e aborts here, and nothing is pushed.
python3 "$GEN" --out "$tmp" >/dev/null

# 2. Defensive re-validate before the doc reaches a public endpoint: it must be
#    non-empty and parseable JSON. The generator already writes atomically, but
#    the push target is externally visible, so the cheap paranoia is warranted.
[ -s "$tmp" ] || { echo "metrics-push: generated file is empty" >&2; exit 3; }
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$tmp"

# 3. Push into CT103's docroot. BatchMode=yes: a broken key fails loudly and
#    never hangs waiting on a prompt (a hung push would stall the timer).
ssh -o BatchMode=yes "$SSH_HOST" "pct exec $CT -- tee $REMOTE_PATH >/dev/null" < "$tmp"

echo "metrics-push: pushed $(wc -c <"$tmp")B -> ${SSH_HOST}:ct${CT}:${REMOTE_PATH}"
