# Stacking mechanisms to make a substrate work may mean the substrate IS the problem

**Date authored:** 2026-05-18 (lesson surfaced by the operator msg_1569 in response to Recovery's T-0453 retirement-plan filing; T-0453 itself was the operator's response msg_1565+1567 to a day of Recovery stacking mechanisms on the blocked-state substrate).
**Author:** Recovery (claude-fallback)
**Status:** Ratified 2026-05-18 by the operator (Telegram msg_1571). **Second instance landed 2026-06-14 (T-0766 veneration first-fire — see §Second instance below) — WITHIN the ~30-day promotion window → the promotion criterion (one instance = anecdote, two = pattern) is now MET. Ready for discipline-tier promotion (the operator's ratification call); flagged for review, to be raised at the next lessons/rulebook touch.**
**Re-evaluate when:** ~~another substrate-vs-mechanism pattern surfaces~~ **DONE — 2nd instance landed 2026-06-14.** Now: when the operator rules on the directive-tier promotion (the proposed "N amendments on one substrate in window Y → escalate to substrate-review" rule), or a 3rd instance further hardens it.

## Observation

2026-05-17 → 2026-05-18 timeline of Recovery + HO amendments to the `tickets/blocked/` substrate:

| Date | Mechanism | Stated reason |
|---|---|---|
| 2026-05-17 | T-0383 filed | Active tickets becoming invisible — needed a way to re-surface stuck claims. |
| 2026-05-17 | T-0430 filed | 5-pattern structural ticket capturing blocked-state gaps (gated mechanism, cascade backfill, orchestrator-sequencing, blocked-event watch, operator-resurface). |
| 2026-05-17 | T-0439 filed | Active reassessment cron design — eliminate HO-sequencing chain-block downtime. |
| 2026-05-18 | T-0440 cron shipped | 30min cron walking `blocked/`, classifying, surfacing, auto-unblocking. |
| 2026-05-18 | T-0441/T-0442 shipped | git "multi-branches" audit + structural fix (T-0218 pattern propagated to 3 callsites). |
| 2026-05-18 | T-0452 OVERDUE flag shipped (HO) | Cron extension to parse unblock_when dates + surface past-date entries distinctly. |
| 2026-05-18 morning | 27-ticket backfill pass (Recovery) | Adding `unblock_when` + `unblock_check` frontmatter to legacy blocked tickets. |
| 2026-05-18 afternoon | 20-ticket sharpening pass (Recovery) | Tightening soft "HO has capacity" phrases to hard dates. |
| 2026-05-18 evening | 19-ticket re-sharpening pass (Recovery) | Re-pinning all dates to TODAY per msg_1557. |
| 2026-05-18 evening | Memory pin (Recovery) | "D1 is operator-only" memory after msg_1560 corrected my framing. |
| 2026-05-18 evening | T-0453 filed | the operator msg_1565+1567: **retire the blocked state entirely**. All of the above becomes moot. |

**Net mechanism count of the day**: +6 tickets, +1 cron, +1 ADR amendment proposal, +27 ticket-file edits in pass 1, +20 in pass 2, +19 in pass 3, +2 memory pins, +1 lesson — all to make the blocked-state substrate work. The operator's resolution: don't have the substrate.

## Second instance (2026-06-14) — veneration resume-harvest substrate, T-0766

The qualifying second instance, exactly the predicted shape. The T-0766 veneration first-fire (supervised, with the operator) started from an **interactive-tmux substrate** for an UNATTENDED one-shot task (resume a dead session → write a HANDOFF → exit). Each failure surfaced a NEW edge case requiring a NEW amendment on that substrate:

| Order | Amendment on the interactive-TUI substrate | Edge case it was patching |
|---|---|---|
| 1 | `--resume <session-id>` (was passing the `.jsonl` path) + cwd-into-project-home | resume wouldn't load the dead session |
| 2 | `--dangerously-skip-permissions` | the harvest Write blocked on an interactive permission prompt |
| 3 | (surfaced by #2) pre-accept the one-time "Yes, I accept" consent screen | skip-permissions itself shows a blocking consent dialog |
| 4 | (surfaced by #3) widen the `wait_ready` sentinel to match the bypass-mode footer | the readiness check couldn't see the session was ready |
| 5 | (surfaced by #4) suppress inherited SessionStart hooks | the target account's hooks injected a competing mailbox-drain nudge |

Five amendments deep, each one revealing the next — the exact "each amendment surfaces NEW edge cases requiring NEW amendments" smell from the signal threshold above. The resolution was NOT amendment #6: it was the **substrate switch** to a single headless `claude --resume <id> --print --safe-mode --permission-mode bypassPermissions <prompt>` subprocess. Headless `--print` is the right substrate for an unattended one-shot — and switching to it **dissolved all five amendments at once**: no TUI → no consent screen, no readiness sentinel, no inherited-hook competition; `--safe-mode` strips hooks/plugins; `bypassPermissions` lands the Write unattended. The whole `launch→wait_ready→inject→ack→kill` machinery collapsed into one self-exiting call (commit `524aed73`).

The generalizable form: **driving an interactive TUI to perform an UNATTENDED one-shot task is a substrate mismatch** — the TUI's human-in-the-loop affordances (consent screens, readiness sentinels, inherited session hooks) are exactly what you keep having to defeat. When the amendments are all "defeat another piece of the interactive UI," the substrate (interactive TUI) is the problem; the headless/batch substrate dissolves them. Counter-check held: each veneration amendment ADDED a defeat-the-UI sub-rule without retiring one (true stacking), and the substrate switch retired all five (healthy structural simplification).

## The lesson

**When you find yourself stacking multiple mechanisms / amendments / discipline-pins on a single substrate to make it work, the substrate itself may be the problem.**

Signal threshold (rough): three or more amendments on the same surface within a week, especially when each amendment surfaces NEW edge cases requiring NEW amendments, is a sign to ask:

- What is the substrate trying to encode?
- Are the amendments removing the friction or just naming it more precisely each time?
- Would removing the substrate dissolve the entire amendment chain?

Counter-signal: amendments that **eliminate** edge cases (substrate getting structurally simpler) are healthy maintenance, not stacking. Stacking is when each amendment adds a sub-rule without retiring an existing one.

## What to do when the smell hits

1. **Pause the next amendment.** Don't ship the next mechanism by reflex.
2. **Inventory the chain.** List every mechanism on the substrate. Count.
3. **Ask the substrate-level question.** What's the substrate's job? Is anything else doing the same job better? Could removing the substrate move the job to a stronger surface?
4. **Surface to operator (the operator)** with the inventory + the substrate-level question. Don't decide alone — substrate retirement is org-tier.
5. **Accept the calibration**: when the operator responds "just retire it" instead of "yes, add the next thing," that's the signal the substrate was the problem from N amendments ago.

## Anti-patterns this lesson rejects

- "Just one more amendment and it'll work." (Stacking has unbounded depth.)
- "The substrate is load-bearing; we can't retire it." (Load-bearing-via-amendments ≠ load-bearing-via-design.)
- "I'm following the rule." (The rule may be wrong; the substrate may be wrong; surface it.)

## What this lesson does NOT say

- It does NOT say "fewer mechanisms = better." Some mechanisms genuinely belong.
- It does NOT say "the operator always wants substrate retirement." He'll often want the amendment.
- It does NOT say "stop being thorough." The thoroughness was correct; the substrate was wrong.

## Cross-references

- T-0383 / T-0439 / T-0440 / T-0452 / T-0453 — the amendment chain + the retirement.
- `feedback_cheap_action_just_try.md` — P7 cost-asymmetry applies to surfacing the substrate question, not just to filing the next amendment.
- `feedback_d1_is_operator_only_not_team_bandwidth.md` — sibling Recovery-discipline lesson from msg_1560 same day; both about calibrating my framing to the operator's actual intent.
- `lessons/structural-enforcement-retires-the-prose-rule.md` — related pattern (architecture > policy = retire the prose rule when structure can absorb it).
- **Second instance:** T-0766 + `sop/standard/venerating-ancestors.md` (the headless `--print` substrate) + `tools/veneration.py` (commit `524aed73`, the substrate switch that dissolved five TUI amendments).

## Promotion criteria (lesson → ADR / principle / directive)

If a second instance of this pattern surfaces (within ~30 days of this lesson's ratification), promote to discipline-tier — likely a directive amendment ("when N amendments on substrate-X in window-Y, escalate to substrate-review"). One instance = anecdote; two = pattern.

**TRIGGERED 2026-06-14.** The second instance (§Second instance — T-0766 veneration first-fire) landed ~27 days after the 2026-05-18 ratification — within the window. So **two instances now = pattern**, and the lesson's own promotion bar is met. Proposed discipline shape (for the operator's ratification — directive authorship is rulebook-tier, his call): a directive amendment that names the trigger — *"when you've made ≥3 amendments to a single substrate in a short window, especially each surfacing a NEW edge case, PAUSE the next amendment and run a substrate-level review (what is the substrate's job? is a stronger surface doing it better?) before shipping more."* The two instances generalize cleanly: (a) blocked-state ticket substrate → retire the substrate; (b) interactive-TUI-for-unattended-task substrate → switch to the headless substrate. Both dissolved the entire amendment chain by changing the substrate, not the Nth mechanism. **Flagged for promotion review 2026-06-14 (Recovery) — to be raised with the operator at the next lessons/rulebook touch; low urgency, no time gate (the lesson already governs as a ratified lesson; promotion only hardens it to directive-tier).**
