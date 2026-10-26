# Retask dispatch — handing operator-interjected work to a sub-orchestrator seat

**Status:** Drafted 2026-07-21 by Orchestrator (HO) per T-1896; operationalizes ORG-043 §Decision 4. Autonomous under msg-3733 (below-goal-level; the org shape is operator-ratified TG 6245). First-fire is supervised (T-1896).

**When:** any delegable arc that Orchestrator would otherwise run itself while holding another arc, or would otherwise hand to an in-session project-orchestrator. That covers operator-interjected work (the original scope) and Orchestrator's own multi-worker arcs, since the team checklist's in-session shape is now the exception (T-3079; see its 2026-09-28 amendment). Not delegable: reserved-to-operator classes, Orchestrator-identity work, or anything the seats' bubble-up criteria would immediately return.

## The flow (ORG-043 §4, made operational)

1. **Capture verbatim.** File a ticket with `--requester operator --target <mason|artisan>`: the Ask carries the operator's words near-verbatim plus Orchestrator-added context/pointers. Pick the seat that is FREE (check both seats' active tickets via `ticket.py`; the WIP cap serializes per persona).
2. **Dispatch brief via mailbox** (`mailbox.send` to the seat, subject `RETASK <ticket-id>: <title>`). The brief MUST carry, explicitly (D7 — never by reference):
   - Arc-intent line first (what the outcome serves).
   - The ticket id + where to read it.
   - Scope partition: what the seat decides autonomously (with DECISIONS-justifies-via) vs the 5 bubble-up criteria (locked-decision deviation, scope-creep, new deps, 3-REBOUND exhaustion, production-enable coordination) — bubbles go to ORCHESTRATOR, never the operator.
   - Chain-of-justification propagation: the seat imposes the same discipline on every worker it dispatches.
   - Repo conventions for the work (agent-branch flow, smoke commands, byte-sequence discipline where applicable).
   - Reporting cadence: mailbox progress note at meaningful milestones; completion = ticket resolve with evidence + mailbox summary to Orchestrator.
3. **The seat drives.** Own session, own subagent teams, own worktrees. The seat's wake/keep-going timers keep it moving; Orchestrator's worker-silence smell-test applies at the orchestrator threshold (30 min).
4. **Orchestrator lifts.** On completion: verify the evidence trail (read the resolution, spot-check the diff/smoke like any reviewer relay), append the ledger entry, surface to the operator per D1 digest discipline. Orchestrator remains the only tier that bubbles to the operator.

## Push/merge boundary

Seats follow each repo's existing conventions. For pantheon-config: seats commit on `agent/<task>` branches, **push the branch themselves** (`git push origin agent/<task>` — branch only, never main; the ruleset guards only main, so a branch push widens nothing), then request PR+merge from Orchestrator via mailbox; Orchestrator verifies in a detached review worktree and squash-merges (settled 2026-07-21 via T-1922/T-1929, extending the Archivist ORG-044 precedent). Repos where a seat becomes operator-of-record get per-repo deploy keys and follow that repo's own rules.

**Agent branches are CODE-ONLY; ticket-lifecycle ops always go direct to main (T-1944 ruling, 2026-07-23 — the previously-implicit convention made normative).** Every `ticket.py` operation (file/claim/block/retarget/resolve) runs from a SYNCED MAIN checkout and commits/pushes to main immediately — never on an `agent/` branch, never `--no-push` as part of a branch workflow. Why structural, not stylistic: ticket state is the org's live database — main is a high-frequency ticket writer, `tickets/README.md` regenerates on every op, and IDs allocate against the local tree, so a branch that carries lifecycle commits ALWAYS conflicts on merge (Artisan ×2, 2026-07-22: the T-1926 README conflict + the T-1939 ID collision) and a stale/branched tree is an ID-collision window. Shapes (b) union merge-driver and (c) timer-regenerated table were rejected: (b) fixes only the README half and adds per-clone git-config surface that hookless clones silently drop (the T-1946 class); (c) trades index freshness the auto-consumer and ticket-UI read. Under this rule T-1939's collision window narrows to simultaneous same-tree filings (serialized by push contention); its origin-consulting allocator hardening stays queued as defense-in-depth for the `--no-push`/offline edges.

## Field lessons (2026-09-27, T-3079 live run: T-4040 / T-4008 / T-4044 / T-4015 via mason)

- **Check the seat's repo grants before routing.** `repos.yaml` grants decide what a seat can push. mason holds no `echo-prismatic` or `claude-config` write grant, so T-4055 bounced back and T-3823 went to artisan. A seat that can't push hands back turnkey content, which is useful but costs a round trip.
- **Target a ticket at the seat; don't rely on mail alone.** A mailbox wake is deferred while the seat is mid-turn: specialist left a 00:09Z request unread for 15+ minutes (T-4062), and mason noticed several handed tickets only on its next queue pull. Deferral is by design, never send-keys into a generating pane (T-2729). Recovery's 2026-09-28 diagnosis: specialist's pane showed "esc to interrupt" on every ~2-min wake fire, so it was busy, not missed. The ticket inbox-consumer is the reliable wake. Use mail for the brief, and the ticket's `target` for the wake.
- **Seats without GitHub tokens push the branch; Orchestrator opens the PR and merges.** For pantheon-config the seat pushes `mason/<ticket>` or `agent/<ticket>`, and Orchestrator creates the PR, gates on CI, and merges. The seat resolves its own ticket after Orchestrator confirms the merge SHA.
- **Queue more than one arc.** A seat that finishes and reports "arc-quiet" wastes a wake cycle. Hand the next arc in the same message that confirms the merge.

## Anti-patterns

- Retasking work that needs Orchestrator's live conversational context with the operator (the retask should be self-contained from the ticket + brief).
- Splitting one coherent ask across both seats to parallelize — a retask is one arc, one owner.
- The seat surfacing to the operator "just this once" — the single-interface property is the entire point (ORG-043 consequence 4).
- Orchestrator silently absorbing the item anyway because writing the brief feels slower than doing the work — the brief cost is the price of arc integrity, and it amortizes (D12: structural over wall-clock).

## Re-evaluate when

The T-1896 supervised first-retask retro lands (fold its lessons in); or a retask bounces on brief-insufficiency twice (the template above is missing something); or seats gain any operator-facing surface (would invalidate the lift step).
