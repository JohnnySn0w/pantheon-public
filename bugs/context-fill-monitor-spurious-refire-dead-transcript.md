---
symptom: "An agent's graceful-restart fires AGAIN within ~30 min of the previous recycle, killing a healthy low-fill session. The context-fill monitor's two readings around the double-fire both name the SAME jsonl, whose mtime is frozen at the previous recycle's service-stop time (i.e. it is the just-killed session's dead transcript, not the live one)."
fix: "Guard the context-fill threshold fire against re-selecting the just-fired-against transcript. Record the fired-against jsonl's path+mtime UNCONDITIONALLY at fire time in a dedicated context_fill_<agent>_last_fire.json; on a later cycle, if find_latest_jsonl re-selects that same path AND it has not grown past a tolerance (default 300s, absorbs the drain/handoff flush gap), it is the frozen dead transcript -> bail. A SAME-path transcript still growing well past the fire is a no-op restart that left the session alive -> NOT suppressed (escalates). Fail-open throughout. T-1328 shipped this keyed off success-only last_restart (commit 05249d86); T-1330 (commit 5ea33e9f) moved the guard source to fire-time last_fire after the hole below recurred on specialist."
domain: pantheon, context-fill-monitor, graceful-restart, cross-account-jsonl-read, recovery-layer
---

> Fixed in T-1328 (commit `05249d86`). This entry records the failure-mode + the generalizable lesson for any cross-account "newest-file" selector.

## Symptom

`pantheon-context-fill-monitor@<agent>` recycles a session at the fill threshold, a fresh session spawns — and then **~30 min later (one cooldown) it recycles AGAIN**, killing the freshly-spawned, low-fill, perfectly healthy session. The user notices the unexplained back-to-back restart ("it's barely been half an hour, why the restart?").

Observed concretely (Recovery, 2026-06-26):
- 16:30 — monitor read `a630f538` at 50.5% → legit recycle. Fresh session spawned 16:32, ~5% full.
- 16:35–16:55 — monitor in cooldown.
- 17:00 — cooldown expired; monitor read **50.93% on `a630f538` AGAIN** → fired → killed the healthy fresh session.
- `a630f538` mtime was frozen at **16:32:04** (the first recycle's service-stop) = the **dead** transcript. The live post-16:32 session (`627307a6`, ~5% full) was the casualty.

## Cause

`find_latest_jsonl()` returns the **most-recently-modified** jsonl in the agent's projects dir, with no guard that it is the **live** session. Right after a recycle, the just-killed session's transcript (large, at/above threshold, mtime frozen at service-stop) can still **out-rank the freshly-spawned IDLE session by mtime** — because the fresh session hasn't written much yet. So once the cooldown lifts, the monitor re-measures the DEAD transcript at ≥threshold and fires the SAME restart again.

This is structurally the same class as the [stale-jsonl risks elsewhere]: **a "pick the newest file" selector has no notion of which file is *alive*.** A frozen transcript looks identical to a live one by name and content; only its *non-advancing mtime* distinguishes it.

The pane-modal detectors (spend-limit) and JSONL-error detectors (api-health/auth/model-outage) were all blind to this because the dead transcript IS a real, parseable, above-threshold jsonl — nothing about it reads as "wrong" except that it stopped growing.

## Fix

Record the fired-against transcript's mtime (`jsonl_mtime`) in `context_fill_<agent>_last_restart.json` at fire time. Add `selected_jsonl_is_dead_restarted()`: if the selected jsonl path == the last-restarted path AND its current mtime ≤ recorded-mtime + `DEAD_TRANSCRIPT_MTIME_TOLERANCE_SECONDS` (default 300s), it is the frozen dead transcript → bail the whole poll cycle (suppresses the threshold fire, staleness, and the alert-only detectors — none should act on a dead transcript).

- **Tolerance, not zero:** the dying session flushes its handoff for up to ~2 min *after* the tombstone (the incident's final flush landed ~103s past the fire), so a zero-tolerance compare would miss it.
- **Still-growing same-path → NOT suppressed:** a transcript that keeps advancing well past the fire is a *no-op restart that left the inner session alive* (the `handoff_and_restart` systemctl-no-op failure mode) — that genuinely needs escalation, so the guard deliberately does not suppress it.
- **Fail-open:** missing/legacy state without `jsonl_mtime`, or an unreadable mtime, returns "not dead" (do not suppress) — the worst case degrades to the pre-fix behavior (one spurious restart), never worse.

Also fixed in passing: the unit `Description` said "60% fill" but the live `CONTEXT_FILL_THRESHOLD` default is 50%.

### Recurrence + completion — T-1330 (commit `5ea33e9f`)

The T-1328 fix above had a **hole**: it recorded the guard's `jsonl_mtime` in `context_fill_<agent>_last_restart.json`, which `write_last_restart_state` writes **only after a SUCCESSFUL restart**. But `handoff_and_restart` *issues the service restart* (freezing the transcript) **before** its post-restart steady-state verification. If that verify times out (e.g. a fresh session that boots slower than the 60s steady-state window, or a boot-dialog blocking the prompt-frame sentinel), the monitor records the restart as **`failed`** and does NOT write `last_restart` — so the now-frozen transcript is **unguarded**, and the next 5-min cycle re-selects it and re-fires. The original bug, recurring.

**Observed (specialist, 2026-06-26 ~20:05):** 50% recycle → ack OK → service restart issued 20:06:31 (froze `f3b040fe`) → steady-state verify timed out at 60s → recorded `failed` → `last_restart` not updated → 20:10 re-fired against the frozen `f3b040fe` → 600s ack-timeout → 3rd-consecutive-failure alert. specialist itself auto-recovered fine (fresh session healthy).

**Completion:** the guard now reads a **separate `context_fill_<agent>_last_fire.json`, written UNCONDITIONALLY at fire time** (both the threshold and staleness fire paths), *before* the restart's success/failure is known. `last_restart` keeps its success-only cooldown semantics; the dead-transcript guard no longer depends on it. The mtime-advance branch is unchanged (frozen → suppress; still-growing → escalate). Secondary observation (not actioned): the 60s steady-state timeout mislabeled a successful-but-slow restart as `failed`, which is benign noise once the re-fire is guarded.

**Stale-high failure-counter aftercare (2026-06-27, Recovery).** The spurious run left `restart_failure_count_specialist.json` at `count=3`. `reset_restart_failure_count()` is called **only on a monitor-driven successful restart**, so a *bug-driven* stale-high count stays **primed** until specialist's next recycle — meaning the very next single transient restart failure would bump to 4 and immediately re-fire the "wedged: N consecutive failures" alert (it re-alerts every cycle while `count >= RESTART_FAILURE_ALERT_THRESHOLD` by design). Manually reset via the tool's own `reset_restart_failure_count('specialist')` after confirming specialist healthy (`verify_startup.sh --agent specialist` rc=0). **Future hardening (defense-in-depth, low priority — the T-1330 fix already closes the re-fire path that created this):** consider clearing the consecutive-failure counter whenever the agent is observed healthy at a normal sub-threshold, non-wedged poll, so a stale-high count auto-heals instead of staying primed until the next recycle. Not urgent because, post-fix, a genuine `count>=3` now means a *real* wedge where the periodic re-alert is wanted.

## Generalizable lesson

**Any cross-account "select the newest/largest file" heuristic over session transcripts needs a liveness check, not just a recency sort.** After a process restart, the just-killed artifact can transiently out-rank the fresh one. Before taking a *destructive* action (restart) on the selected file, confirm it is the *live* one — the cheapest signal is "its mtime is still advancing" (it belongs to a running writer). A *non-destructive* action (e.g. activity_monitor's poke, which lands in the live pane) does not carry the same harm, so this guard is specific to the destructive context-fill restart path.

## Cross-references

- **Fix:** T-1328 / commit `05249d86`; tests `TestJsonlMtime`, `TestSelectedJsonlIsDeadRestarted`, `TestDeadTranscriptGuardMainFlow` in `tools/test_context_fill_monitor.py` (incl. the incident reproduction).
- **Sibling no-op-restart failure mode:** the `handoff_and_restart.sh` systemctl-restart-is-a-no-op-for-inner-claude pattern (verify a real restart by PID change, not exit-0/is-active) — the guard's "still-growing same-path" branch deliberately preserves escalation for that case.
- **ADR:** ORG-020 (self-triggered restart mechanism — the context-fill monitor is its sensor).

## Discovered

- **2026-06-26 (Recovery / `claude-fallback`).** Surfaced by the operator ("barely half an hour, why the restart?"). Diagnosed from the `pantheon-context-fill-monitor@recovery` journal (two fires against the same frozen `a630f538`) + the projects-dir jsonl mtimes (fresh `627307a6`/`e2a7d668` vs frozen `a630f538`). Fixed same session.

## Keywords

pantheon, context-fill-monitor, spurious-restart, double-fire, dead-transcript, frozen-jsonl, find_latest_jsonl, newest-file-selector, liveness-check, mtime-advance, graceful-restart-loop, cross-account-jsonl, t1328, recovery-layer
