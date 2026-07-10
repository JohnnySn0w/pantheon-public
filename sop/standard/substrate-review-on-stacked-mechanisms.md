# Substrate review — when you're stacking mechanisms to make a surface work

**Status:** Adopted 2026-06-16 by Orchestrator (HO) — autonomous per msg-3733, as an SOP-tier promotion (the content is trigger+procedure, not principles-tier). Promoted from `lessons/stacking-mechanisms-may-mean-the-substrate-is-the-problem.md` (T-1195, ORG-012). Promotion bar met: two clean instances (blocked-state ticket substrate, 2026-05-18; interactive-TUI-for-unattended-task, T-0766 veneration first-fire 2026-06-14). **Tier note:** the source lesson self-proposed *directive*; Specialist read it as SOP and lean-surfaced the SOP-vs-directive call to the operator (TG-534). If the operator elevates it, the content promotes into `rulebook/directives.md` (HO curation). The operator override-available.

**When you find yourself stacking multiple mechanisms / amendments / discipline-pins on a single substrate to make it work, the substrate itself may be the problem.** This SOP is the trigger + the review procedure that catches that before you ship the Nth mechanism.

## The trigger (signal threshold)

Three or more amendments on the same surface within a short window (~a week), **especially when each amendment surfaces NEW edge cases requiring NEW amendments**. That cascade — patch reveals the next patch — is the smell.

**Counter-signal (NOT the trigger):** amendments that *eliminate* edge cases (the substrate getting structurally simpler) are healthy maintenance. Stacking is when each amendment adds a sub-rule **without retiring** an existing one. If the chain is getting shorter, you're fine.

## The procedure (when the smell hits)

1. **Pause the next amendment.** Don't ship the next mechanism by reflex.
2. **Inventory the chain.** List every mechanism on the substrate. Count them.
3. **Ask the substrate-level question.** What is the substrate's job? Is anything else already doing that job better? Would *removing* the substrate dissolve the entire amendment chain?
4. **Surface to operator (the operator)** with the inventory + the substrate-level question. Substrate retirement is org-tier — don't decide alone. (Cheap to surface; P-cost-asymmetry applies to the surfacing, not just to filing the next amendment.)
5. **Accept the calibration.** When the operator answers "just retire it" rather than "yes, add the next thing," that's the signal the substrate was the problem N amendments ago.

## The two instances (the evidence this generalizes)

- **Blocked-state ticket substrate (2026-05-18):** +6 tickets, +1 cron, an ADR-amendment proposal, three multi-ticket frontmatter passes (27/20/19), memory pins — all to make `tickets/blocked/` work. Resolution (T-0453): retire the blocked state entirely; express blocks as `not_before`/`depends_on` frontmatter (ORG-019). The whole chain dissolved.
- **Interactive-TUI-for-unattended-task (T-0766, 2026-06-14):** five amendments deep driving an interactive TUI to do an unattended one-shot harvest (resume→write HANDOFF→exit), each defeating another human-in-the-loop affordance (consent screen, readiness sentinel, inherited hooks). Resolution: switch to a single headless `claude --resume <id> --print --safe-mode --permission-mode bypassPermissions` call — dissolved all five at once. The substrate (interactive TUI), not the Nth mechanism, was the problem.

Both generalize the same way: the chain dissolved by changing the *substrate*, not by adding mechanism N+1.

## What this SOP does NOT say

- NOT "fewer mechanisms = better" — some genuinely belong.
- NOT "the operator always wants retirement" — he'll often want the amendment; the procedure surfaces the question, it doesn't presume the answer.
- NOT "stop being thorough" — the thoroughness is correct; the substrate may be wrong.

## Cross-references

- **Source lesson:** `lessons/stacking-mechanisms-may-mean-the-substrate-is-the-problem.md` (full timelines + both instances).
- **Related lesson:** `lessons/structural-enforcement-retires-the-prose-rule.md` (architecture > policy — retire the prose rule when structure absorbs it).
- **First-instance artifacts:** `adr/ORG-019` (blocked-state retirement). **Second-instance artifacts:** `sop/standard/venerating-ancestors.md` + `tools/veneration.py` (commit `524aed73`).
- **Principles + directives:** P1 (architecture over policy — the substrate-level question IS the P1 question); D10 (don't scatter mechanisms where one stronger surface does the job).

## Authoring discipline

Hand-curated per D2 (HO authors). If the operator elevates this to directive-tier (per the pending lean-surface), promote the trigger+procedure into `directives.md` and convert this SOP to a pointer.
