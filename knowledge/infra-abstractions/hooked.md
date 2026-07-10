---
title: HOOKED — boundary-driven components
status: Redesigned 2026-06-11 by Orchestrator (T-1076); content lifted from Recovery's guards.md (T-0999).
purpose: Components the harness fires at an action/lifecycle boundary — Claude Code hooks (PreToolUse, Stop, StopFailure, SessionStart) and git hooks. Subtypes: Blocker / Injector / Recorder.
---

# HOOKED — the intercepted action is the work signal

**Membership test:** wired as a hook — `settings.json` (PreToolUse, Stop, StopFailure,
SessionStart) or a git hook (pre-commit, pre-push) — around someone else's action.
Hooks are executed by the **harness, not the model**: any "whenever / before / after X,
do Y" behavior MUST be a hook; memories and preferences cannot enforce timing. Wiring +
inventory: [`../../tools/hooks/README.md`](../../tools/hooks/README.md).

**Governing contract:** [default-safe](cross-cutting-patterns.md#default-safe-contract) —
pick the failure side by which failure is worse, and state it in the docstring: a guard
around comms/irreplaceable channels fails **open**; a guard against an
irreversible/dangerous op fails **closed**. **Debounce blocking hooks** (block-once per
distinct unit via a [marker](cross-cutting-patterns.md#marker--sentinel-debounce)) so a
legitimate retry isn't looped.

**Wiring drift caveat:** hooks are wired **per-account** in each agent's `settings.json`
— presence in this map ≠ active on every agent (e.g. `backlog-surface` is not in Recovery's
live SessionStart array). When auditing, read the live settings, not this catalogue.

---

## Subtype: Blocker — refuse/allow the action

### act-vs-ask-gate — PreToolUse on the Telegram reply tool
- `tools/hooks/act-vs-ask-gate.py` · matcher `mcp__plugin_telegram_telegram__reply`.
- Blocks permission-asking replies ("want me to…") **once per distinct message** (hash-marker debounce) to force the 3-prong act-vs-ask test. **Fail-open by design** — can never brick comms; a genuine decision-for-operator re-sends and passes. Structural enforcement of the act-vs-park calibration.

### mcp-list-guard — PreToolUse refusing a bridge-killer
- `tools/hooks/mcp-list-guard.py` (T-0894/T-0996).
- Refuses `claude mcp list` while this account's Telegram poller is live (a second poller's 409 kills the bridge). **Fail-closed**; the safe diagnostic is ps + `curl getMe`.

### HERMES content guards — commit/push byte-sequence scanners
- `tools/hermes_diff_scan.py` (diff scan), `tools/hermes_history_audit.py` (history audit); runtime guard embedded in every auto-committer via `_git_utils.py`.
- Refuse any commit carrying the trigger byte-sequence contiguously. **Fail-closed** content guard; prose references use the escape form.

## Subtype: Injector — add context/warning to the action

### handoff-auto-read — SessionStart orientation inject
- `tools/hooks/handoff-auto-read.sh` (T-0512).
- Injects the ORG-MAP pointer (always) + the instance HANDOFF (if mtime-fresh) as additionalContext — the mechanism that feeds each session its handoff. Memory `handoff_pointer` is the fallback.

### backlog-surface — SessionStart backlog reminder
- `tools/hooks/backlog-surface.sh` (T-0023).
- One-line summary of open `BACKLOG.md` items at session start. **Per-account drift example** — absent from Recovery's live array.

### directive_skill_drift_check — pre-commit drift warn
- `tools/directive_skill_drift_check.py` (T-0796) · wired as a **non-blocking git pre-commit warn**.
- Flags drift between ratified directives and the skills implementing them.
- *Placement note:* previously misfiled as a scheduled Filer ("orphan-trigger" finding); its actual wiring is a pre-commit hook and its effect is a warning into the commit flow — Hooked/Injector.

### pre-push freshness hook — git pre-push *(census addition)*
- Repo-local `pre-push` (pantheon-config): rebases onto origin/main before pushing (the multi-writer freshness gate); echo-prismatic's pre-push re-runs the full smoke. Both intercept the push boundary; the echo one is a Blocker (red smoke refuses the push), the pantheon one an Injector (freshens, then allows).

## Subtype: Recorder — persist state about the action for a downstream consumer

The producer half of a [handoff chain](README.md#handoff-chains--composition-is-expected-not-an-overlap-apology):
the hook fires at the boundary, writes an artifact, exits; a
[Reactive](reactive.md) component consumes it.

### ho_stop_failure — StopFailure → limit-hit state file
- `tools/hooks/ho_stop_failure.py`.
- On a usage/rate-limit Stop failure, writes the limit-hit state file. Consumer: `ho_resumer` (**Resume loop**, README §chains).

### session_reset_log — reset-event recorder
- `tools/session_reset_log.py` (hook-driven event log).
- Records session-reset-class events; consumer: `session_reset_reconcile` ([Scheduled/Reconciler](scheduled.md)) lands them in the ledger. (A Recorder whose consumer is scheduled — the chain pattern is the same.)

---

## Placement notes

- New hook: choose Blocker/Injector/Recorder by what it does to the action (stop it / add to it / record it). Declare the failure side. Document in `tools/hooks/README.md` AND wire per-account deliberately — note which accounts carry it.
- The old "Guards" family maps here 1:1; the name survives colloquially ("the act-vs-ask guard") but the subtype names answer the placement question the old name couldn't.
