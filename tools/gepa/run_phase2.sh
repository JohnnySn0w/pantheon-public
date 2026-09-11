#!/usr/bin/env bash
# T-2809 Phase 2 — GEPA reviewer-persona optimization entrypoint.
#
# Thin wrapper: runs tools/gepa/run_phase2.py under the dedicated venv
# (/home/claude/.venv-gepa), never system python3 (gepa is not installed
# there — see tools/gepa/__init__.py). Run from the repo root:
#
#     tools/gepa/run_phase2.sh --max-rollouts 150 --wall-clock-seconds 21600
#
# Does not run automatically and is not installed as a timer/unit — per
# T-2809 Phase-2 scope, scheduling is an HO enable-leg decision.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

exec /home/claude/.venv-gepa/bin/python -m tools.gepa.run_phase2 "$@"
