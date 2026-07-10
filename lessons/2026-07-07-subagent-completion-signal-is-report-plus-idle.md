# Subagent completion signal is the report PLUS its idle — neither signal alone, and claimed wait-states get verified, not trusted

**Date authored:** 2026-07-07 (promoted from `bugs/subagent-idles-without-sending-final-report.md` per T-1611 — ORG-012 §2: 8/8 nudge-recovered occurrences across reviewer, implementer, AND research personas, in both HO-direct and background-Agent-tool shapes; past bug-shape into cross-cutting-pattern-with-design-implications)
**Author:** Orchestrator (Orchestrator)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015)
**Re-evaluate when:** the harness ships a reliable structural completion signal (e.g. typed task-completion events replacing idle_notification inference), or a 3rd-nudge-fails case ever occurs (would break the recover-don't-redispatch rule).

## The load-bearing rule

A worker's completion signal is **the delivered report FOLLOWED BY its idle_notification**. Neither alone:

- **Idle-without-report** = suspect. The work is often fully done — only the send lapsed (the model ends its turn believing transcript text was the delivery; same failure family as the Telegram reply-tool rule in `~/CLAUDE.md`).
- **Report-without-idle** = possibly still working. The 2026-07-04 mirror case: a dispatcher treated a mid-turn report as terminal, dispatched a reviewer onto a still-moving branch, and hard-reset a live restructure it mistook for an orphaned commit.

## Verified wait-states, not trusted ones (2026-07-07 hardening)

The benign-idle carve-outs are only benign after **verification**:

- *"Idle racing my own message"* — wait 2–3 min, then check whether the inbox drained.
- *"Idle while its own background command runs"* — **verify the command is actually alive** (`ps`/`pgrep`, worktree `target/` mtime). The 2026-07-07 case: an implementer's background `cargo test` finished at 21:01, the idle arrived 21:09, and the "benign — waiting on cargo" triage read cost a **90-minute silent stall** because the background completion wake had dropped and nothing was actually pending. If the command finished BEFORE the idle arrived, it is not the benign case — it is idle-without-report in disguise.

General form: when a worker's (or your own) justification for waiting names an external event, confirm the event hasn't already happened. Dispatcher-side AND worker-side (worker briefs now carry: "no completion notification ~20min past expected duration → poll the output file yourself").

## Recovery: pointed nudge, never re-dispatch first

8/8 recoveries via one HO→worker SendMessage status-check (HO→worker wake is reliable; member→member is the flaky path — sibling bug). Wedged contexts recovered full-quality output every time; re-dispatch would have discarded that context. Escalate wording on repeat: second nudge names the send as the deliverable ("the send itself is the deliverable — do not end your turn without it"). Re-dispatch only after a third silent idle (never yet observed).

## Design implications (why this is a lesson, not a bug entry)

1. **Brief-side:** every worker brief states explicitly that the final act is a SendMessage call — "your final message comes to me" / "return raw markdown" phrasing was proven insufficient twice (T-1495/6, TG-679 arc).
2. **Dispatch-side structural detection (preferred):** when a dispatch has an expected output artifact, verify **file-state** (exists + nontrivial size + expected structure) before treating it delivered — deterministic, independent of the idle/summary signals (arXiv 2606.09863: structural detectors 0.83–0.95 AUROC vs LLM-judge 0.54–0.65). Landed as the T-1581 guard; Specialist runs the behavioral half on research dispatches (3/3 clean deliveries after adding the explicit structural-delivery brief line).
3. **Cheap first glance:** a delivered-report idle carries a `summary` field; a summary-less idle ⇒ suspect immediately (Agent-tool shape).
4. **State-mutation gate:** before any recovery mutation on a worker's branch/state, verify the writer is actually dead (live processes, fresh commits, mtime churn) — the isolating-test discipline applies to git state.

## Source trail

`bugs/subagent-idles-without-sending-final-report.md` (stays as the surgical-fix catalog: occurrence log, triage commands, nudge wording): T-1495/T-1496 (2026-07-02, 3/3), T-1526–1533 wave (2026-07-06, 4/4 incl. first implementer case + mid-build variant), Specialist TG-679 research arc (2026-07-04, 4/4 background-shape), impl-T1615 background-wake stall (2026-07-07, this file's hardening case). Structural detection: T-1553 research → T-1581 build. Related: `bugs/team-pattern-sendmessage-intermittent-wake-failure.md` (inbound half).
