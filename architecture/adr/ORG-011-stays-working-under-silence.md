---
aliases: [ORG-011]
title: Structural enforcement for HO-stays-working-under-silence
status: "Ratified 2026-05-11 by the operator (msg_1305, msg_1307); predicate P1.c withdrawn 2026-05-12 (the operator msg_1326, T-0228, 6e55b9f536); a narrower form is reopened by ORG-037 (supersedes-partial)"
date: 2026-05-11
build_disposition: built
---
# ORG-011 — Structural enforcement for HO-stays-working-under-silence

**Status:** **Ratified 2026-05-11** per the operator msg_1305 + msg_1307 (all open questions closed); predicate P1.c withdrawn 2026-05-12 (the operator msg_1326, T-0228, 6e55b9f536); a narrower form is reopened by ORG-037 (supersedes-partial). Prior draft trail: drafted 2026-05-11; revised per msg_1296 + msg_1298 (15min-absent-keep-working operational rule + SOP-vs-memory layering correction); refined per msg_1305 (1hr P1.a threshold, context-fill conditional removed, P4 retires at Phase B); final ratification msg_1307 (check cadence 15min + bundling-friendly, activity signals OK, await-state phrases OK but Recovery's judgement supersedes, orchestrator-only initially with multi-agent rollout after empirical confirmation).

**Date authored:** 2026-05-11

**Author:** Orchestrator (Orchestrator)

## Context

Multi-day pattern surfaced by the operator msg_1282 (2026-05-11): "it has become painful to try and have you continue on work. I thought we had processes in place to help you stay working." Today's instance: HO went idle for 8h after msg_1274 despite having queued HO-direct work (T-0196 #5+, T-0197 #4, T-0214 sync-bug fix, the b35 dispatch silently failing); failed to status-check the b35 team-lead-2 dispatch which had wedged on a SendMessage-without-spawn bug.

Existing processes target adjacent failure modes but NOT the drift-to-passive surface:

- **T-0184 auto-restart-on-decay** — fires when context-fill crosses 60%; doesn't target idle-with-queue.
- **D3 autonomy bar + trust-posture.md** — calibrates *what HO escalates vs absorbs*; doesn't target *whether HO is acting at all*.
- **T-0157/T-0196 SOPs** — codify discipline against worker bypass; don't target HO inactivity.
- **D8 HO self-discipline** — names cross-check + sticky-belief drift; doesn't include "HO went silent while queue is non-empty."
- **Inbox auto-consumer** — fires on cadence; surfaces ONE ticket per fire; if HO acks-and-doesn't-act, doesn't re-poke.
- **`feedback_drift_to_passive_smell_test` memory pin** (just landed) — prose-tier discipline; per `lessons/structural-enforcement-retires-the-prose-rule.md` + `lessons/instruction-fidelity-collapse-discipline-vs-architecture.md`, prose-without-architectural-backing degrades.

**The architectural gap**: no structural mechanism prevents OR catches HO drifting to idle when queued work + context bandwidth + autonomy-scope all exist simultaneously.

### Revision 2026-05-11 (per msg_1296 + msg_1298)

The operator surfaced a stronger operational rule alongside this ADR's revision: **when the operator is absent >15min, HO does not sit idle — for any task hitting a blocker needing his ack, HO records progress, shelves the blocker, and swaps to the next completable task; repeats until backlog is empty or every remaining item genuinely needs the operator's explicit input.** Caught in msg_1294 ("were you sitting idle this whole time?") + HO admission that the ~3h gap between msg_1289 → msg_1290 was effectively idle between auto-consumer drops despite b36 (T-0206) being dispatchable the whole time.

Per msg_1298: this rule is **SOP-tier, not memory-tier** (operational role behavior, not HO-self-discipline preference); **needs architectural shoring** (structural enforcement, not just prose); **folds into ORG-011** (same gap, different surface). The original draft's Path 1+4 framing addresses the activity-detection surface (queue-has-actionable-while-idle); the msg_1296 rule extends it to the absence-detection surface (operator-not-heard-from-while-HO-could-be-doing-other-work).

The combined rule: HO is idle-eligible only when EITHER (a) queue is empty AND no operator-absent shelving is pending, OR (b) every remaining item is genuinely operator-gated. Neither is the state HO was in at msg_1289.

## Forces

- **D1 (the operator's bandwidth)** — every "what have you been doing?" the operator has to ask is a withdrawal.
- **P1 (architecture over policy)** — structural enforcement scales; prose discipline degrades.
- **D10 (context-frugality)** — too-aggressive poking burns HO context budget on no-op cycles.
- **D8 (HO self-discipline)** — HO should detect own drift via smell test, but the smell test fires actively only if invoked actively (which is itself the failure mode).
- **Eventual posture** (`user_eventual_decision_maker_target.md`) — HO is the decision-maker; idle-HO blocks the entire org.

The tension: structural enforcement ↔ context-cost. Too-frequent pokes burn token budget; too-infrequent leaves the gap. The right cadence is empirical.

## Candidate mechanisms

### Path 1 — Activity-monitor (sister to T-0184 context-fill-monitor)

Recovery's monitoring infrastructure adds a `pantheon-activity-monitor@orchestrator.timer` (mirrors the existing `pantheon-context-fill-monitor@.timer` template per T-0189). Periodic check (~15min cadence) evaluates **two predicates**:

**Predicate P1.a — "idle-with-actionable-queue":** "Has HO produced an outbound message OR commit OR ledger entry in the last 1 hour (per msg_1305 ratification)? If no AND inbox/active queue has ≥1 HO-actionable item: tmux-poke HO with a 'queue-check' inject." (Context-fill conditional removed per msg_1305 — the poke is benign at high context-fill; T-0184 auto-restart fires its own action at 60%; the dual-condition was over-engineered. Simpler: poke fires on idle-with-queue regardless of fill state. If T-0184 fires concurrently, the auto-restart wins and the poke becomes moot in the new session.)

**Predicate P1.b — "operator-absent-with-pullable-work" (per msg_1296 revision):** "Has the operator's last inbound to the Telegram bridge been >15 minutes ago AND HO's last outbound includes await-state language ("waiting on", "pausing on", "ready when you", etc.) AND the queue has ≥1 HO-actionable item not blocked-on-operator? If yes: tmux-poke HO with an 'shelve + swap' inject (different prompt than P1.a's queue-check; explicitly references SOP `sop/standard/ho-absent-operator-keep-working.md`)."

**Predicate P1.c — "worker-silent-while-cycle-open" (per msg_1313 revision):** "For each active team-pattern cycle (each team in `~/.claude/teams/`), iterate the non-lead members. For each member: has the inbox file mtime advanced in the last N minutes? OR has the team's task list updated in the last N minutes? OR has the agent emitted output (a turn in the parent session JSONL) in the last N minutes? If NO to all three AND the cycle is still open (team config exists; no shutdown_request fired on the member; no terminal-resolution event): poke HO with a worker-silence flag naming the silent member + last-known activity timestamp."

Default threshold per worker role:
- **orchestrator**: 30 min (project-orchestrators should be relaying often; longer silences signal genuine stall).
- **implementer**: 15 min (smaller individual turns expected; coding cycles surface within ~10-15 min typically).
- **reviewer**: 5 min (review cycles are short; >5min silence is usually a stuck reviewer).

Tunable per role; configurable per-team if a cycle has unusual cadence (e.g., long-running researcher).

All three predicates fire independently; combined pokes when multiple fire in the same cycle. They target structurally distinct drift surfaces:
- **P1.a**: HO went idle while HO-side queue has work.
- **P1.b**: HO is in self-imposed wait-state while the operator is away.
- **P1.c**: a dispatched worker went silent while HO expected their surface — the failure mode b36 exhibited 2026-05-11 (orchestrator silent 5h between implementer READY and pre-relay surface).

- **Pro**: Sister primitive; reuses existing systemd template + Recovery's monitor pattern; structural-tier per P1.
- **Pro**: Cheapest implementation (mirror T-0189 template).
- **Pro (P1.b)**: Encodes msg_1296 operationally — the 15min-absent + await-language signature is detectable from outside HO's context without scanning HO's tokens.
- **Con**: Adds Recovery's monitoring surface; one more thing to maintain.
- **Con**: Cadence tuning needed (15min = could be too aggressive or too lax; needs empirical calibration).
- **Con**: "Queue has work" is non-trivial to compute cleanly (must distinguish actionable from blocked/awaiting-operator items — the same distinction the SOP encodes).
- **Con (P1.b)**: "Await-state language" regex is heuristic; false positives possible. Mitigation: paired with the actionable-queue check, so a clean inbox state suppresses fires regardless of language.

### Path 2 — Auto-consumer aggressiveness tuning

The existing `pantheon-inbox-consumer@orchestrator.timer` fires on cadence regardless of HO state. Currently surfaces ONE ticket per fire. Tuning: when HO is detected idle (via the same activity-detection logic Path 1 uses), the consumer fires harder — surfaces multiple queued items in batch OR re-fires more aggressively until HO produces output.

- **Pro**: Reuses existing consumer infrastructure.
- **Con**: Couples activity-detection into the consumer loop (entangles two concerns).
- **Con**: "Fire harder" risks dumping queue on HO; not all queued items are HO-direct actionable.

### Path 3 — ScheduleWakeup self-paced cycles

HO uses `/loop` skill or its `ScheduleWakeup` primitive to schedule periodic re-engagement. After each turn, HO calls `ScheduleWakeup(delaySeconds=1200, prompt="check queue + work next item if available")`. Self-paced; no external monitor needed.

- **Pro**: HO-internal; no Recovery dependency; doesn't add monitor overhead.
- **Con**: Requires HO to remember to schedule wake-ups (which is the same discipline-by-prose failure mode); falls over if HO forgets.
- **Con**: Wake-ups burn token budget per fire even when nothing actionable exists.

### Path 4 — End-of-turn queue-check + auto-pick

Modify HO's end-of-turn discipline (via prompt-tier instruction in CLAUDE.md or a hook) to ALWAYS run a queue-check before declaring idle. If queue has actionable HO-direct work, pick it up + work it. Only declare idle when queue is genuinely empty OR all items are blocked-on-operator.

- **Pro**: Prevents drift at the moment-of-drift.
- **Con**: Discipline-by-prose (which the HERMES-arc lesson critiques as degradation-prone).
- **Con**: Lacks structural backing — same failure mode as the existing autonomy-bar prose.

## Recommendation

**Three-layer combined (per msg_1298 architecture-shoring requirement):**

1. **SOP layer** — `sop/standard/ho-absent-operator-keep-working.md` (NEW, authored as Phase A'). Operational sequence: when blocker hit → record progress → shelve → swap to next completable → repeat → until backlog empty or all-remaining-operator-gated. SOP is the org-level operational contract HO operates under; cited by name in the Path 1.b poke prompt so the inject lands HO directly into the SOP.

2. **Architectural layer** — Path 1 activity-monitor with both predicates (P1.a idle-with-queue + P1.b operator-absent-with-pullable-work). Structural enforcement at ~15min latency; Recovery-side; doesn't depend on HO discipline.

3. **Prose layer (interim only, per msg_1305 revision)** — Path 4 end-of-turn queue-check + smell test (memory pin at `feedback_drift_to_passive_smell_test.md` already landed). Load-bearing ONLY until Path 1 lands; retires at Phase B completion (the 15-min structural cadence + per-turn prose check is redundant + wasteful per the operator msg_1305). Phase D in the original arc → restructured: prose retires when Phase B lands, not after a 4-week calibration observation window.

Path 2 (auto-consumer tuning) is rejected — entangles concerns. Path 3 (ScheduleWakeup) is rejected as primary mechanism — depends on HO remembering, which is the failure mode being fixed.

**Why two layers post-Phase-B (SOP + architecture), three layers interim (with prose):** msg_1298 explicitly called out that memory-tier (prose discipline) without architectural shoring degrades; msg_1305 closed the open question of "when does prose retire" — answer is "immediately on Path 1 land, not after observation window." The prose layer is a bridging discipline for the gap between today and Phase B completion; once Path 1 is structurally enforcing the same behavior at 15-min cadence, the per-turn prose check adds nothing and burns HO context.

## Implementation arc (post-ratification)

1. **Phase A — Path 4 prose immediate (today, already done):** end-of-turn queue-check smell test pinned at `feedback_drift_to_passive_smell_test.md`; audit firing every turn until Phase B lands. **Bridging discipline only per msg_1305** — retires immediately on Phase B completion.
2. **Phase A' — SOP authored (today, this revision):** `sop/standard/ho-absent-operator-keep-working.md` codifies the operational sequence per msg_1296. Linked from `~/CLAUDE.md` §HO-absent-operator-keep-working so it's load-bearing per turn. SOP is org-level operational contract; permanent (does NOT retire post-Phase-B).
3. **Phase B — Path 1 structural** (Recovery-target ticket): mirror `tools/context_fill_monitor.py` + `pantheon-context-fill-monitor@.timer` to author `tools/activity_monitor.py` + `pantheon-activity-monitor@.timer`. Activity-detection: latest commit OR Telegram outbound OR ledger entry in last 1 hour. **Two predicates per the revision:**
   - **P1.a (idle-with-actionable-queue):** no-activity-1hr + queue has ≥1 actionable HO-direct item. Poke prompt: generic "check queue + work next item" mirroring T-0184. (No context-fill conditional per msg_1305 — T-0184 fires its own action at 60%; if both fire concurrently auto-restart wins and the poke is moot in the new session.)
   - **P1.b (operator-absent-with-pullable-work):** The operator's last inbound Telegram >15min ago + HO's last outbound contains await-state language ("waiting on", "pausing on", "ready when you", "no further chain-dispatch until", etc.) + queue has ≥1 actionable non-operator-gated item. Poke prompt: explicitly references the SOP — "you appear to be in self-imposed wait-state while the operator is away; per `sop/standard/ho-absent-operator-keep-working.md` — record progress on the awaited item, shelve it, swap to next actionable. Pull queue now."
   - Queue-detection: count `inbox/` + `active/` tickets where target=<agent> AND no unmet `depends_on` AND not blocked-on-operator (the SOP's pull-criteria). Threshold: configurable per-agent. Initial cadence: 15min check cadence.
4. **Phase C (renamed from "Phase D" in prior arc) — Retire Path 4 prose** at Phase B completion. Per msg_1305: the per-turn prose check is redundant with the 15-min structural cadence + wasteful of HO context budget; retire immediately on Path 1 land, not after the 4-week observation window the prior draft proposed. Remove `feedback_drift_to_passive_smell_test.md` memory pin and the `~/CLAUDE.md` §HO-absent-operator-keep-working pointer's "end-of-turn smell test" prose paragraph (the SOP pointer itself stays; just the prose-discipline paragraph retires).
5. **Phase D (renamed from "Phase C" in prior arc) — Calibrate** (1-2 week observation window post-Phase-B): tune cadence + thresholds based on empirical data. Goals:
   - ≥80% of P1.a fires result in HO actually picking up work (low false-positive rate).
   - ≥80% of P1.b fires result in HO shelving the awaited item + picking up something else (the SOP's swap behavior).
   - ≤2% of weekly the operator messages are "what have you been doing?" / "where did I tell you to wait?" (target: zero).
   - SOP stays through calibration — only the prose layer (Phase C) retires. SOP is permanent regardless of calibration outcomes (per msg_1298 framing: role-defining, not discipline-tier).

## DECISIONS

- **Three-layer (SOP + architecture + prose) over original two-layer (architecture + prose)** — justifies via msg_1298 explicit framing ("more an sop than memory … architectural shoring up vs policy") + the org-tier-vs-self-discipline distinction (the rule is role-defining, not preference, so it belongs in `sop/`, not `feedback_*.md`).
- **Path 1 predicate split (P1.a idle-with-queue + P1.b operator-absent-with-pullable-work)** — justifies via D5 (the two drift surfaces are detectably distinct from outside HO; conflating them would hide one or generate noise on the other) + msg_1296 (the 15min-absent rule is operationally specific — not just "HO went quiet" but "HO is in self-imposed wait-state while work is dispatchable").
- **Path 1+4 combined over single-mechanism** — justifies via P1 (structural enforcement) + the HERMES-arc lesson (prose stays load-bearing until structural is reliable).
- **SOP cited by name in P1.b poke prompt** — justifies via D7 (don't rely on inheritance-by-reference; spell the SOP path operationally into the poke so the poked HO lands directly into the operational document rather than having to remember it exists).
- **Path 1 (activity-monitor) over Path 2 (auto-consumer tuning)** — justifies via P5 (separate concerns: monitor monitors, consumer consumes) + D4 (named-symbol verification: existing T-0184/T-0189 templates make activity-monitor a near-mirror, low-risk implementation).
- **Path 1 over Path 3 (ScheduleWakeup)** — justifies via P1 (Recovery-monitored is structural; HO-self-scheduled is prose) + D8 (the discipline being fixed is HO's own drift; HO can't self-cure).
- **15min check cadence as initial; 1hr idle threshold (P1.a, ratified msg_1305)** — justifies via D10 (avoid token-budget burn from too-frequent fires) + P6 (preserve the tension between "catch drift fast" vs "don't burn context"; calibrate empirically). The 1hr P1.a threshold is more lax than the prior 30min draft default — recognizes that legitimate worker-cycle gaps (single implementer turn can run 5-10min; multi-unit team cycles 30-60min) shouldn't trip the idle predicate.
- **Context-fill conditional removed from P1.a (ratified msg_1305)** — justifies via P1 (simplest correct thing): T-0184 fires its own auto-restart action at 60% context-fill; the dual-condition in the original P1.a was over-engineered guard against a redundancy that resolves naturally (auto-restart wins if both conditions trigger; the activity-poke is moot in the post-restart fresh session).
- **Phase 4 prose retires at Phase B completion, not after a 4-week observation window (ratified msg_1305)** — justifies via D10 (per-turn prose check is wasteful when 15-min structural cadence catches the same drift surface); HERMES-arc lesson holds (structural retires prose); msg_1305 explicit framing ("path 4 prose feels like it's taken care of by the 15 minute architectural check? overly redundant + wasteful?").
- **Check cadence 15min, bundling-friendly (ratified msg_1307)** — bundle into existing Recovery periodic-check infrastructure if cadence aligns (e.g., if `pantheon-context-fill-monitor` also fires at ~15min, share the timer). Justifies via D10 (minimize systemd surface) + P5 (consistency with existing monitor pattern).
- **Activity-detection signals: commit + Telegram outbound + ledger entry (ratified msg_1307)** — canonical set; no others added. Justifies via D5 (literal observable signals; file writes + bash invocations are too noisy + don't represent material progress).
- **Await-state phrases: regex heuristic with Recovery judgement override (ratified msg_1307)** — initial phrases ("waiting on", "pausing on", "ready when you", "no further chain-dispatch until", "deferring to you", "awaiting your") are the regex backbone, but Recovery's judgement supersedes: if Recovery's monitor reasons that HO's state warrants the swap-poke even without exact phrase match (or doesn't despite a match), Recovery acts on the judgement. Justifies via D8 (Recovery's tier is the structural observer; the regex is a hint, not a hard predicate) + P6 (preserve tension between mechanical heuristic + intelligent judgement).
- **Multi-agent rollout: orchestrator-only initially, extend to Specialist + Recovery after empirical confirmation (ratified msg_1307)** — first cycle locks the cadence/predicate parameters against real Orchestrator behavior; Specialist + Recovery have distinct activity rhythms (cron-fired research / one-shot grooming) that may want different thresholds. Justifies via P6 (calibrate empirically before extending) + D7 (multi-tier rollout absorbs variance before scale).
- **Predicate P1.c (worker-silent-while-cycle-open) added per msg_1313** — justifies via P5 (structurally distinct surface: P1.a/b target HO-state drift, P1.c targets worker-state drift; conflating them would lose the per-role threshold tuning that orchestrator-vs-implementer-vs-reviewer needs) + observed b36 2026-05-11 failure mode (orchestrator silent 5h between implementer READY and pre-relay surface). Per-role thresholds (orchestrator 30min, implementer 15min, reviewer 5min) calibrated against expected cadence; tunable empirically.
- **Queue-detection requires actionable distinction** — justifies via D9 (depends_on items go in blocked/, not inbox/; activity-monitor counts only inbox/ items where target matches AND no unmet depends_on AND blocked-on-operator reason excluded).
- **SOP stays after Phase D prose-retirement; prose retires** — justifies via P5 (SOP defines the role; prose is the bridging discipline until structural is reliable. They serve different purposes; SOP is permanent vocab, prose is a calibration crutch).

## Open questions for the operator ratification

**All closed (ratified):**
- ~~P1.b absence threshold~~ — 15min (msg_1303).
- ~~P1.a idle threshold~~ — 1hr (msg_1305).
- ~~Context-fill conditional in P1.a~~ — removed (msg_1305).
- ~~Path 4 retirement criterion~~ — retires immediately on Phase B completion (msg_1305).
- ~~Check cadence~~ — 15min, bundling-friendly (msg_1307).
- ~~Activity-detection signals~~ — commit + Telegram outbound + ledger entry (msg_1307).
- ~~Await-state phrase list~~ — initial 6 phrases + Recovery's judgement supersedes (msg_1307).
- ~~Multi-agent rollout~~ — orchestrator-only initially; extend to Specialist + Recovery after empirical confirmation (msg_1307).

Per msg_1308: "Ratified when those changes hit." This revision marks ADR ORG-011 as fully ratified.

## Cross-references

- **Principles + directives:** P1 (architecture over policy — load-bearing), D1 (the operator's bandwidth — the WHY), D8 (HO self-discipline — what fails without this), D10 (context-frugality — calibration constraint).
- **Lessons:** [`lessons/structural-enforcement-retires-the-prose-rule.md`](../lessons/structural-enforcement-retires-the-prose-rule.md) (the framework this ADR instantiates); [`lessons/instruction-fidelity-collapse-discipline-vs-architecture.md`](../lessons/instruction-fidelity-collapse-discipline-vs-architecture.md) (the antecedent — discipline-by-prose degrades).
- **Memory pins:** `feedback_drift_to_passive_smell_test.md` (Phase A immediate prose discipline); `feedback_calibrate_autonomy_higher_per_msg1271.md` (the calibration this drift fired against); `user_eventual_decision_maker_target.md` (the eventual posture).
- **Tickets:** T-0184 (context-fill-monitor — sister primitive); T-0189 (per-agent monitor template — implementation reference); T-0179 (multi-agent extension precedent).
- **Telegram refs:** msg_1271 (calibration that drift fired against); msg_1278 / msg_1279 (the operator's catch); msg_1282 (bubble-up that triggered this ADR); msg_1283 (HO commitment to file).
