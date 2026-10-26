#!/usr/bin/env bash
# pantheon_metrics_push.sh — regenerate the PANTHEON metrics JSON and push it to
# CT103, whose caddy file_server fronts pantheon-metrics.your-domain.example (CF-proxied).
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
# T-3739: local canonical dir. NOT the literal /var/lib/pantheon-metrics from
# the ticket -- that is under root:root /var/lib and recovery cannot mkdir there
# (recovery's root sudo is scoped to kino/journalctl only). This uses the existing
# group-writable, setgid /var/lib/pantheon tree (recovery is in pantheon-shared),
# matching the /var/lib/pantheon/health-signals precedent -- no root
# provisioning needed. Emitter path (T-3719/BT1, orchestrator) must match this.
LOCAL_DIR="${METRICS_LOCAL_DIR:-/var/lib/pantheon/metrics}"

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

# 2b. ORG-098 BT2 (T-3739): tee the validated canonical JSON to a local read
#     path for internal consumers (presence emitter T-3719, kino Health Plane)
#     -- same data as the public feed, no public-egress dep for internal reads.
#     NON-FATAL by design: the public push (step 3) is the primary function, so
#     a local-persist failure (dir missing/unwritable, disk full) must NEVER
#     abort it under `set -e`. `install -m 0644` copies with an explicit
#     cross-account-readable mode (mktemp's $tmp is 0600, which cp would have
#     preserved -> unreadable to the orchestrator-run emitter) and leaves $tmp intact
#     for the push (cleanup() still rm's it on exit -- no mv). The dir is
#     provisioned root-owned->recovery 0755 one-time (recovery cannot mkdir under
#     root:root /var/lib); the install -d here is an idempotent self-heal that
#     fails soft if that provisioning is ever absent.
if install -d -m 0755 "$LOCAL_DIR" 2>/dev/null \
   && install -m 0644 "$tmp" "$LOCAL_DIR/data.json" 2>/dev/null; then
    :
else
    echo "metrics-push: warn: local canonical persist to $LOCAL_DIR failed (non-fatal)" >&2
fi

# 3. Push into CT103's docroot. BatchMode=yes: a broken key fails loudly and
#    never hangs waiting on a prompt (a hung push would stall the timer).
ssh -o BatchMode=yes "$SSH_HOST" "pct exec $CT -- tee $REMOTE_PATH >/dev/null" < "$tmp"

echo "metrics-push: pushed $(wc -c <"$tmp")B -> ${SSH_HOST}:ct${CT}:${REMOTE_PATH}"
