#!/usr/bin/env bash
# tools/gepa/launch_phase2_systemd.sh — HO wrapper to launch run_phase2.sh under
# systemd-run --user with OnFailure alerting (T-3837 item 5, from Recovery's T-3836
# resolution note mailbox 01789889256626732120 2026-09-20).
#
# WITHOUT this wrapper, transient systemd-run units created by hand do NOT get
# the OnFailure=pantheon-unit-failure-alert@%n.service directive that
# repo-tracked templated units carry, so a failed GEPA run stays silent until
# pantheon-failed-unit-reconcile catches it (worst case ~6h post-T-3836 tighten,
# ~24h pre-tighten). This wrapper closes the fail-time alerting gap.
#
# Usage:
#     tools/gepa/launch_phase2_systemd.sh <run-id> [args passed to run_phase2.sh]
#
# Example:
#     tools/gepa/launch_phase2_systemd.sh 20260920T012035Z-fix3 \
#         --max-rollouts 150 --wall-clock-seconds 21600
#
# Sets --unit=gepa-t2809-phase2-<run-id>.service and
# --property=OnFailure=pantheon-unit-failure-alert@<same>.service so a
# failure alerts in-band at failure-time.
#
# %n specifier expansion in --property values is not universally guaranteed
# for transient units (Recovery's caveat, T-3836 mailbox); this wrapper passes the
# LITERAL instance name to sidestep the ambiguity.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  cat >&2 <<EOF
Usage: $0 <run-id> [extra args to run_phase2.sh]

<run-id> is a slug appended to the unit name, e.g. 20260920T012035Z-fix3.
Extra args flow verbatim to run_phase2.sh (e.g. --max-rollouts, --wall-clock-seconds).
EOF
  exit 2
fi

RUN_ID="$1"
shift

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UNIT_BASE="gepa-t2809-phase2-${RUN_ID}"

exec systemd-run --user \
  --unit="${UNIT_BASE}.service" \
  --property="OnFailure=pantheon-unit-failure-alert@${UNIT_BASE}.service" \
  "${REPO_ROOT}/tools/gepa/run_phase2.sh" "$@"
