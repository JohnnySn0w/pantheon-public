---
symptom: "A spawned worker (observed: reviewer subagents on sonnet, 2026-07-02, rev-T-1495 twice; rev-T-1496 once in no-start form) emits an idle_notification WITHOUT having sent its final report/verdict via SendMessage. The work may be partially or fully done — transcript/tool evidence shows real activity (smoke runs, file reads) — but the deliverable message never lands at the dispatcher, so the arc silently stalls until the dispatcher notices."
fix: "Recovery that worked 3/3: a pointed HO->worker SendMessage status-check/nudge (HO->worker wake is reliable; the flaky path is member->member per the sibling wake-failure bug). The second nudge should state explicitly: 'the send itself is the deliverable - do not end your turn without it.' Do NOT re-dispatch first; the wedged context recovered full-quality output both times it had partial work. Detection: idle_notification arriving WITHOUT a preceding report message = suspect; check work evidence (target/ mtimes, inbox drain time) to distinguish idle-after-report (normal) from idle-instead-of-report (this bug)."
domain: pantheon, subagents, reviewer, sendmessage, idle, teams, dispatch, liveness
---

## Occurrence update (2026-07-19, orch-T1792 — first PROJECT-ORCHESTRATOR case, running total 12/12)

Orchestrator tier, idle-instead-of-ACT variant: orch-T1792 received the dispatcher's "U1 APPROVE — proceed to U2" relay at 08:42, idled twice (08:43/08:44, no action summary), and had spawned nothing by the 09:00 ground-truth check (no new worker, no lane processes, arc task-list frozen). Pointed status-check nudge at 09:01 with the "the send itself is your deliverable" wording → U2 implementer spawned at 09:02 (tasks-dir symlink mtime) and a full state report delivered. The orchestrator's own framing ("crossed in flight — ground truth was just behind") is contradicted by the symlink timestamp; treat post-nudge "already done" claims as recovery, not refutation of the stall. TRIAGE REFINEMENT from this instance: the reliable spawn-evidence signal is the tasks/ dir transcript symlink (`/tmp/claude-<uid>/<cwd-slug>/<session>/tasks/*.output`, mtime = spawn time), NOT the teams inbox file — inbox files can lag or stay absent for Agent-tool children.

## Occurrence update (2026-07-19, vc-200s..vc-240s — 5× Explore-type, running total 17/17)

All FIVE Explore-type read-only analysts in the T-1793 fan-out idled without sending their final report; one pointed nudge each ("the send itself is the deliverable") recovered all five with complete, high-quality reports within ~2 min. New signal: the failure appears SYSTEMATIC for Explore-type named background spawns (5/5 in one batch) — their final-text does not auto-relay as a teammate-message the way implementer-type spawns' reports did in the same session. Dispatcher posture for Explore fan-outs: expect the nudge round; budget it into the arc.

> **Promoted as load-bearing to `lessons/2026-07-07-subagent-completion-signal-is-report-plus-idle.md`** (T-1611, ORG-012 §2). This file stays as the surgical-fix catalog: occurrence log, triage commands, nudge wording.

## Occurrence update (2026-07-08, impl-T1617 — 2 more background-wake drops, running total 10/10)

Same background-cargo-completion wake-drop as the T-1615 case, twice on one worker during the T-1617 jitter-buffer arc: (1) first-build smoke finished ~00:30, wake dropped, nudged ~00:55 → reported; (2) rebound-fix smoke finished 01:21, wake dropped, went unnoticed ~2h while HO was deep in concurrent contract work, nudged 03:19 → full report immediately. Both nudge-recovered, zero re-dispatch. Confirms the pattern is not worker-specific and recurs whenever a worker backgrounds a long cargo run.

**NEW triage gotcha (liveness-check false-positive):** `pgrep -f 'cargo test'` MATCHES THE DEAD RUN'S LINGERING BASH WRAPPER — the `bash -c '... cargo test ... | tee ...'` process can outlive the actual cargo/rustc children, so a substring `-f` match reports "still running" when the build finished. This fooled the HO liveness check for ~25 min. FIX: check the real toolchain binary, not a command-line substring — `ps -eo args | grep -E '^\S*(cargo|rustc)'` (anchored to the binary path), or check the tee'd output log's mtime/tail for a completed test-result block. The "verify the background cmd is actually alive" carve-out (below) is only sound if the aliveness check itself isn't matching a zombie wrapper.

## Occurrence update (2026-07-07, impl-T1615 — background-wake variant, running total 8/8)

Implementer idled at 22:40 having launched `cargo test --workspace` in background; the test had FINISHED at 21:01 but the completion wake dropped — the "benign: waiting on its own background cmd" triage read held for 90 minutes because nothing verified the cmd was still alive. Pointed nudge → woke <1min, full report. Hardening (now in the lesson): the background-cmd carve-out requires verifying the process is actually running; a cmd that finished BEFORE the idle arrived = idle-without-report in disguise. Worker briefs now carry the self-help line ("no notification ~20min past expected duration → poll your output file yourself").

## Occurrence update (2026-07-06, T-1526–T-1533 wave)

Four more occurrences, all nudge-recovered (running total 7/7): `impl-1527` (first IMPLEMENTER case — retires the reviewer-only hypothesis below), `rev-1530`, `rev-1531` (idle-instead-of-report; pointed nudge → full deliverable within minutes each), and a variant: `impl-1531` idled MID-BUILD with a dirty tree and no commits (not done, not blocked — just stopped; nudge resumed it). Also observed benign look-alikes worth distinguishing before nudging: (a) idle stamped seconds BEFORE a dispatcher message lands (race — the message itself wakes them; wait 2–3 min), (b) idle while the worker's own background bash command (long clippy/test) runs — the harness re-wakes them on command exit; check for live processes cwd'd in their worktree first.

## Symptom

During the T-1495/T-1496 HO-direct arc (2026-07-02), reviewer subagents twice ended their turn and went idle (`idle_notification`, `idleReason: available`) without sending the verdict they were dispatched to produce:

- `rev-T-1496`: spawned, sat ~40 min with zero observable activity (no smoke in the worktree's `target/`, no messages) until an HO status-check woke it; it then produced a complete APPROVE report.
- `rev-T-1495` (round 1): drained its brief, worked ~10 min (fmt check evidenced in `target/`), idled without a verdict. Nudged → full REBOUND verdict delivered.
- `rev-T-1495` (round 2): drained the re-verify request, ran ~3 min of cargo, idled again without the verdict. Second, more pointed nudge ("the send itself is the deliverable — do not end your turn without it") → complete APPROVE within a minute (work had evidently been done; only the send was dropped).

Implementer subagents in the same arc (same model tier) did NOT exhibit this — all four implementer turns ended with their report sent. Sample too small to pin on the reviewer persona, but reviewers were the only repeat offenders.

## How to distinguish from normal idle

`idle_notification` immediately after a delivered report = normal terminal standby (implementers/reviewers idle after reporting — fine, no action).

Suspect THIS bug when idle arrives and no report preceded it. Confirm cheaply before nudging:

```
ls -lat ~/.claude/teams/<session>/inboxes/   # their inbox drain mtime = they received the brief
find /tmp/<worktree>/target -mmin -10        # work evidence (or absence)
ps aux | grep -E 'cargo|rustc'               # mid-smoke right now?
```

- Inbox drained + work evidence + no report → idled-instead-of-reporting → nudge.
- Inbox drained + active processes → still working; the idle was a race with your own message — wait.
- Inbox never drained → the spawn-time wake dropped — nudge serves as the wake.

## Recovery

HO→worker `SendMessage` status-check. Escalate wording on repeat: first nudge = "status? resume and carry to verdict"; second = explicitly name the send as the deliverable. Both recovered full-quality output; no re-dispatch was ever needed. Re-dispatch only if a pointed nudge produces a THIRD silent idle (never observed yet).

## Relation to sibling bugs

- `team-pattern-sendmessage-intermittent-wake-failure.md` — inbound wake drops (member→member). This bug is the outbound half: the worker never *sends*. HO→worker nudges are reliable against both.
- `teammate-orchestrator-agent-dispatch-completion-routes-to-main.md` — mis-routed completions in the sub-spawning-orchestrator shape. Not this: in the HO-direct shape (ORG-036) completions route correctly when sent at all; this bug is the send not happening.

## Open question

Whether this is a harness turn-termination race (model produced the report text but the turn ended before the SendMessage tool call) or a model-side lapse (ended turn believing the transcript text WAS the delivery — the same failure-mode family as the Telegram reply-tool rule in `~/CLAUDE.md`). The round-2 recovery (verdict arrived near-instantly on nudge, work already complete) leans toward the latter. If it recurs on non-reviewer personas, consider adding an explicit "your final message must be a SendMessage call" line to all worker briefs — the T-1495/T-1496 briefs said "your final message comes to me" and it was insufficient.

## Mirror mode (2026-07-04): report-without-idle — the send is NOT proof the turn ended

The inverse failure, observed same arc (impl-T-1504): the worker SENT a complete-looking final report mid-turn and KEPT WORKING (finishing a plan item omitted from the report). The dispatcher treated the report as terminal and (a) dispatched a reviewer onto the still-moving branch — hashes rewrote under the review; (b) "recovered" an apparently-orphaned commit with a hard reset, trampling the worker's live mid-restructure.

**Rule:** a worker's completion signal is the report FOLLOWED BY its idle_notification. Report-without-idle = possibly still working. Before dispatching a reviewer onto a worker's branch (or performing ANY recovery mutation on it): (1) confirm the post-report idle arrived, or (2) check for live processes + fresh commits/mtime churn in its worktree. Before a state-mutating "recovery" on shared state, verify the state's writer is actually dead — an orphaned-looking commit may be a live restructure (the isolating-test discipline applies to git state too).

**Nudge-crossing corollary** (same arc, impl-T-1496): a status-nudge can cross the worker's report in flight; the worker-side defense that proved itself is verify-actual-state-before-redoing. Dispatcher-side: re-check for a just-arrived report before acting on nudge-triggered assumptions.

## Non-reviewer confirmation (2026-07-04, Specialist): 4/4 on research personas, background Agent-tool shape

The open question's condition is met — this is NOT reviewer-specific. During the voice-landscape research arc (the operator TG-679), all FOUR spawned subagents (3× general-purpose sonnet web-researchers + 1× Explore repo-explorer, background Agent-tool dispatch, not team-pattern) emitted `idleReason: available` idle_notifications without a preceding report. One pointed nudge each ("if complete, send the full report to main via SendMessage now... do not idle without delivering") recovered all four — full-quality complete reports, no re-dispatch, matching the 3/3 HO pattern above (now 7/7 single-nudge recovery overall).

Two notes for the fix direction:
- The briefs already said "Return: raw markdown (this is data for the orchestrator)" — like the T-1495/1496 "your final message comes to me" phrasing, it was insufficient. Supports the proposed explicit "your final message must be a SendMessage call / the send itself is the deliverable" line in ALL worker briefs, regardless of persona.
- Detection heuristic that worked at-a-glance: a delivered-report idle carries a `summary` field ("[to main] ..."); the bugged idles arrived with NO summary. summary-less idle ⇒ suspect this bug immediately, no filesystem forensics needed (in the Agent-tool shape at least).

## Structural detection (2026-07-06, Specialist, T-1553): prefer file-state over the idle signal

Per arXiv 2606.09863 (structural completion detectors 0.83-0.95 AUROC vs LLM-judge 0.54-0.65) and the watch loop's own T-0662 partial-stage guard, the ROBUST detector for "did this worker deliver" is **file-state, not the idle_notification**: when the dispatch has an expected output artifact (watch reports write to a known `output_path`; researcher briefs can be told to), check `output_path exists AND size >= ~500B AND has the expected frontmatter/headline` — that deterministically distinguishes delivered / stub-or-error / absent, independent of whether the idle carried a summary or the SendMessage fired at all. The summary-field heuristic (above) is the cheap first glance; the file-state check is the ground truth (ground-truth-over-narrative). Behavioral rule adopted: after dispatching workers with an expected output file, structurally verify each file before treating the topic delivered / nudging / landing — don't rely solely on the idle/summary signal. Tooling enforcement (strengthen the T-0662 guard from exists() to exists+nontrivial+structured; add a post-dispatch file-state gate) proposed to orchestrator as T-1581.
