# Weekly ticket-summary generation — agent brief

**Status:** Authored 2026-05-29 by Recovery per the operator msg 2087-2094. Sample ratified at `reports/2026-W21.md`. Generator + cron landing under T-0688.

This SOP is the brief consumed by the agent that the weekly cron spins up. The cron's job is orchestration (pick the target ISO week, walk done tickets, build the input bundle, dispatch the agent, commit the result). The agent's job is generation — turning the bundle into an executive-summary report in the shape the operator approved.

## When fired

- **Cron cadence:** Monday 09:00 UTC, by `pantheon-weekly-ticket-summary.timer` on Recovery's user-mode systemd. Targets the prior ISO week (Mon→Sun resolutions).
- **Backlog mode:** the same script run with `--week <YYYY>-Wnn>` produces a single named week (used to backfill history).
- **Idempotency:** if `reports/<YYYY>-W<NN>.md` already exists with non-DRAFT status, the cron skips. Manual re-generation requires `--force`.

## Input contract (what you receive)

The orchestrator hands the agent:

1. **This SOP** as system context.
2. **A reference exemplar:** `reports/2026-W21.md` — the canonical shape the operator approved. Read it first to calibrate tone before writing the new one.
3. **An input bundle** containing, for every ticket resolved in the target week:
    - `id`, `title`, `target`, `requester`, `labels`, resolution timestamp + actor
    - The full transition log (concatenated, chronological)
    - Commit hashes referenced in the transition log (the close-commit + any intermediate work commits)
    - For ratification tickets (`intent=ratify`): the ratified-files list and link to the artifact (ADR, SOP, etc.)
4. **The target file path:** `reports/<YYYY>-W<NN>.md`.
5. **The week range** in YYYY-MM-DD form (Monday → Sunday).

## Output contract (what you produce)

Write exactly one file at the target path with this frontmatter + body shape:

```markdown
---
week: <YYYY>-W<NN>
range: <Mon-date> to <Sun-date>
tickets_resolved: <count>
status: GENERATED   # not DRAFT-SAMPLE — the cron produces final reports
---

# Week of <Mon-date>

<lede — 2-3 sentences naming the dominant arc(s) of the week, in plain English. Read like a newsletter opener, not a status report.>

## What changed

<2-5 paragraphs covering this week's STRUCTURAL or GOVERNANCE changes — new capabilities, policy/SOP/ADR ratifications, mechanisms that changed how the system works. Each paragraph: one bold lede sentence stating the outcome, followed by 1-3 sentences of plain-English context. NO ticket IDs in body prose. NO jargon (`with_worktree`, `ORG-019`, `posture C`, etc.). Talk about what changed for operation, not what changed in the code.>

## What got faster, safer, or cleaner

<Bullet list, 3-6 items, of operational improvements — infra robustness, automation extensions, bug fixes that close a class of recurring issue. One sentence per bullet. Frame each item as the OUTCOME (`Recurring crons stopped racing each other`), not the mechanism.>

## External signals to watch

<Bullet list, 2-5 items, of EXTERNAL findings worth the operator's attention — security developments (supply-chain attacks, vendor policy changes), research findings that reframe ongoing work, billing/account changes with action deadlines. One sentence per item. Mention specific dates if action-relevant.>

## By the numbers

<One paragraph. Total tickets resolved + the four-way split (new capabilities / improvements / fixes / batch closures + follow-ups). Notable counts: ADRs ratified, SOPs ratified, ratification-tier closes. Be concise — this is the footer, not the headline.>

<Then the `### Pantheon at scale` block. The generation prompt supplies it PRECOMPUTED under the "PRECOMPUTED SCALE METRICS" delimiter — reproduce it VERBATIM at the end of this section, numbers exact; do NOT recompute, round, or reword. These cumulative fun-metrics (the operator request 2026-08-12: "tuck those metrics into the weekly pr report, I think they're fun") are computed deterministically by `tools/weekly_ticket_summary.py::compute_scale_metrics()`. If the block arrives empty or as an HTML comment (a git/FS hiccup), omit the subsection — never fabricate the numbers.>

---

*Detailed ticket trail available in `tickets/done/` under the date range above; commits referenced by individual ticket transition logs.*
```

## Tone + voice rules

- **Plain English over jargon.** "The agents recover themselves" over "self-triggered restart pipeline self-drains on context-fill." If you must reference a system component, name it the way an executive would refer to it (`the queue`, `the cron jobs`, `the PWA`), not by the internal identifier.
- **Outcomes over mechanics.** "Triage gets faster" over "ticket states collapsed from four to three." The mechanism is the path; the outcome is the news.
- **Single dominant arc per week.** Lead with what mattered most. If the week was scattered, name that explicitly ("This week was scattered — three small improvements landed without a single dominant arc.").
- **Don't list every ticket.** 90% of weekly closures are routine (batch closures of sub-tickets, watch-finding actuations, reminder fires). Roll those up in "By the numbers." Surface only the items where a reader would want to know what happened.
- **~400 words target.** Phone-readable. If you're past 500, cut.
- **No ticket IDs, no commit hashes, no ADR numbers in body prose.** The ADR + SOP ratification COUNTS are fine in "By the numbers." The trail-of-evidence pointer in the footer covers anyone who needs the IDs.

## Bucketing guidance (for your own classification, hidden from the reader)

When walking the input bundle, mentally bucket each ticket:

- **New capability** (Features): something the system can now do that it couldn't last week. New tools, new automation paths, ratified ADRs introducing new mechanisms, ratified SOPs introducing new discipline. → surface in **What changed**.
- **Improvement** (Upgrades): something existing got faster, safer, more robust, more covered. Sentinel additions, cron retrofits, schema extensions, refactors. → surface in **What got faster, safer, or cleaner** (the load-bearing 3-6 items).
- **Fix** (Bugs): something broken got unbroken. Bug/regression labels, "from-incident" labels. → surface in **What got faster, safer, or cleaner** if narratively useful, otherwise roll up.
- **Other** (Batch / Reminder / Follow-up): everything else. Roll up in **By the numbers**.

When multiple tickets share an arc (a parent ticket + 3-5 follow-ups), summarize them as one item, not many.

## Edge cases

- **Empty week (zero tickets resolved).** Land the report with a brief honest "Quiet week — zero closures" lede and `By the numbers` only. Don't pad.
- **All-ratifications week.** Lead with the ratifications; "What got faster" may legitimately be empty.
- **All-bugs week (no features).** Lead with the dominant fix arc; reframe "What changed" as "What's working again."
- **Cross-week parent ticket.** If the parent landed THIS week but follow-ups land LATER, treat THIS week's narrative as the arc-anchor and the follow-up weeks will reference back.
- **Conflicting framings.** If two arcs both deserve the lede, name both in the headline sentence ("Two arcs: the ticket taxonomy settled, and the restart pipeline went self-driving.").

## Boundary

This SOP does NOT cover:

- Monthly / quarterly rollups (separate cadence).
- Per-agent breakdowns ("Orchestrator did X, Specialist did Y") — strategic framing is system-wide.
- Trend analysis across weeks (deferred until 4+ weeks of going-forward data exist).
- Reverts / reopens (rare; if it happens, name it explicitly in the next week's lede).
- Echo-prismatic / pantheon-pwa / revenue-project ticket trails — those projects manage their own narrative spines if/when they get one.

## Cross-references

- **Approved shape exemplar:** `reports/2026-W21.md`.
- **Source ticket:** T-0688 (weekly summaries + reports/ folder + cron).
- **Sibling cron disciplines:** `tools/ticket_grooming.py` (daily ticket-grooming cron, same orchestrator shape), `tools/spine_index_groom.py` (daily spine-index cron).
- **Principles:** P1 (structural answers over ad-hoc), P4 (audit trail — reports/ is the durable narrative), P7 (cheap action — generate weekly, don't backfill from memory later).
- **Directives:** D2 (HO curation of structural docs — applies to reports/ too; manual edits to landed reports stay welcome).

## Cron flow (post-T-0847, 2026-06-13 billing change)

The cron no longer calls `claude -p`. After the 2026-06-13 billing change, headless `claude -p` is API-billed; interactive in-session generation stays subscription-covered.

**New flow:**
1. `pantheon-weekly-ticket-summary.timer` fires Monday 09:00 UTC → runs `weekly_ticket_summary.py --file-request`.
2. `--file-request` builds the full generation prompt and writes it to `reports/.pending/<YYYY>-W<NN>-prompt.md`.
3. A generation-request ticket (`target=orchestrator`, `labels=weekly-summary,in-session-gen`) is filed pointing at that prompt file.
4. An interactive Orchestrator session picks up the ticket, reads the prompt file, generates the report in-session, writes `reports/<YYYY>-W<NN>.md`, refreshes the README spine-index, commits+pushes, removes the pending prompt, and resolves the ticket.

**Backfill / manual:** `python3 tools/weekly_ticket_summary.py --file-request --week 2026-W21` files a request for any named week. `--dry-run` shows what would be filed without writing or filing.
