# Brief template — canonical structure for dispatch briefs

Authored 2026-05-07 post-T-0113 retrospective. Captures the structural rules HO + project-orchestrators apply when authoring dispatch briefs for implementer cycles. Lean by default; defenses live in process (reset, reviewer pre-flight) not in brief bloat.

> **Amended 2026-06-05 (T-0611, ratified by the operator):** added §Directives in force (item 4 — per-ticket-class D-id map so implementer DECISIONS cite by id; closes the T-0603 gap where D11 was in the skill but never named for the class) + the §Memory composition anti-drift layering. The directive↔skill auto-sync structural fix is deferred to a sibling ticket.
>
> **Amended 2026-06-16 (T-1199, ORG-012 outflow of the already-ratified ORG-034):** briefs now OPEN with an arc-intent line, and §DECISIONS minimums is intent-first (intent/goal-served first, P/D-ids as the how). Propagates `adr/ORG-034` + the D7 amendment into the operational SOP — the ratified decision predates this SOP's prior revision, so a brief built from the template was missing the mandatory intent-first opening.

## When this applies

Multi-step build arcs going via Team + project-orchestrator (per `rulebook/directives.md` D7 — decision flow execution). Single-shot Agent-direct dispatches don't need this — the brief is the prompt.

## Canonical structure

Every brief **OPENS with its arc-intent line** (per `adr/ORG-034` + `rulebook/directives.md` D7 — intent-first justification, ratified 2026-06-12): one sentence stating the outcome this arc/ticket is for + which north star (`rulebook/goals.md`) or parent arc-intent it serves, in outcome language ("installer never ships ahead of the daemon wire shape"). It is the brief's first line, before §Ask. "Goal-neutral maintenance" is the honest cite when the arc is hygiene / tooling / process — name it as such, never dress it up. The arc-intent cites the north star; leaf DECISIONS cite the *arc-intent*, not the north star directly (pasting north stars into leaf DECISIONS is how the middle tier becomes ceremony).

Then these sections, in this order:

1. **§Ask (the contract)** — what the work is, in implementer-actionable terms. References authoritative sources (ratified ADR §sections; ticket §scope) by path + section name, NOT inlined.

2. **§Locked answers** — ratified decisions the implementer must NOT re-litigate. State each as one-liner with a verbatim quote of the source-of-record (e.g. ADR §QN line). Do NOT restate in 3 places. Do NOT inline implementation-level code as a "carry-forward."

   **Multi-ruling enumeration contract** (T-1612 promotion of `lessons/multi-ruling-briefs-require-per-ruling-status-enumeration.md`; T-1530 recurrence): when the brief carries N numbered rulings (locked answers, ratified ADR requirements, or any numbered obligations), the brief MUST end the section with: *"your report must enumerate all rulings 1–N, each with a per-ruling status: built / deferred-with-disclosure (+ where the disclosure lives) / unchanged-from-last-increment / infeasible-stopped."* Reports organized around what-was-done are structurally blind to what-was-NOT-done; the mandatory numbered list makes completeness checkable by diffing two lists (P1) instead of trusting a reader to notice an absence. T-1530's implementer silently dropped an operator-ratified ruling (5 of 7) and its accurate-sounding report had no slot that forced the omission into view — one full rebound cycle lost.

3. **§Brief-named tests** — exact test names + 1-line body-shape per test. Names are the contract; body-shapes are the reviewer's body-fidelity check. Per `rulebook/directives.md` D4 (brief quality bar — coverage cross-check at plan-tier). **Include integration-seam tests** (write→read→route at unit-test level for each boundary) per D4 (e2e smoke for read-paths from bootstrapped daemons) — NOT just isolated-component tests with mocks at every seam. Two integration bugs in T-0113 surfaced because mocks-around-each-seam pattern hid them; integration-seam tests catch this class.

4. **§Directives in force** — name the ticket's *class* and the specific D-ids (+ project-extension Ds) that govern this dispatch, each with a one-line operational form for THIS ticket. The implementer's §DECISIONS then cites these by id. This closes the T-0603 gap: a skill carried D11, but the brief never told the implementer D11 *applied to this ticket class*, so it wasn't applied. Select from the per-class default map below; add/drop per the specific ticket. Do NOT restate full directive text — point by id (the `pantheon-org-discipline` skill carries the text; the brief operationalizes).

   **Always in force** (every class, do not restate): **D2** (hand-curated-doc protection), **D6** (plan-first + no self-merge), the HERMES byte-sequence rule, and chain-of-justification propagation.

   Per-ticket-class default D-id map (beyond the always-in-force baseline):

   | Ticket class | Governing D-ids | Class-specific load-bearing note |
   |---|---|---|
   | **Outbound-API / third-party integration** | D4, D5, D7, **D11** + test-fixture corollary | D11 is load-bearing: READ THE SPEC at the authoritative URL; mock fixtures derived from spec or captured from the real API, NEVER hand-invented; real-surface smoke is the gate, not fake-server tests. |
   | **Substrate / cron / daemon** | D4, D5, D7, **D8** + deploy-verification | After daemon code changes, verify the *running binary* is the new build (mtime / version stamp / known new log line) — committed-code-intact ≠ running-process-correct. |
   | **Pure refactor** | D4, D5, D7 | No new external surface; integration-seam tests at unit level still required. |
   | **Doc / SOP / ADR** | D1, D9, D10 | D2 (always-in-force) is the operative gate; D10 frugality (references-not-transclusion); D1 = don't bloat the operator's review surface. |
   | **Ratify** | D9 | `ratifiable_files` MUST point at a committed file diff (the PWA ratifies a diff, not prose); D9 response contract (categorical answer + format + timing). |

   (D3 autonomy-bar and P1–P6 are orientation, not per-dispatch enumerated; they govern HO's dispatch decision, not the implementer's execution.) Drop-in template:

   ~~~
   ## Directives in force
   Ticket class: <class>. Cite these by id in your DECISIONS:
   - D4 (brief quality bar) — <one-line operational form for THIS ticket>
   - D5 (smoke verification) — literal command-tail in a fenced block, never "+N passing"
   - D7 (decision flow) — 3-REBOUND budget; propagate chain-of-justification to anyone you sub-dispatch
   - D11 (third-party surface) — READ THE SPEC; fixtures from real surface, never invented   [outbound-API class only]
   - <project-extension Ds, e.g. EX-1 / ED-2, if the cwd project defines them>
   Always in force: D2 (don't edit hand-curated docs — propose instead), D6 (plan-first; no push/merge), HERMES byte-sequence rule, chain-of-justification.
   ~~~

5. **§DECISIONS minimums** — list of choice points the implementer must justify-via, **intent-first** (per `adr/ORG-034`): each material call names the arc-intent / goal served FIRST (or "goal-neutral maintenance" honestly), with P/D ids as **the how** — methodology compliance, no longer carrying the why. When methodology and intent genuinely conflict mid-arc, intent binds and the deviation is FLAGGED per D6 (the licensed-deviation seam — the flag is mandatory, not an escape hatch). Don't enumerate every possible choice; list the architecturally-load-bearing ones. Implementer adds more as they encounter.

6. **§Acceptance** — smoke loops + test-count delta + byte-sequence-guard + hand-curated-doc-protection + scope-bounds. One bulleted list. Refer to standing discipline in `~/CLAUDE.md` + memory pins instead of restating.

7. **§Plan-first contract** — implementer returns plan inline FULL BODY (per `rulebook/directives.md` D6 — plan-first protocol + locked-answer-deviation handling; D5 — smoke verification discipline / literal output rule). Plan = (a) file paths, (b) test-name→body-shape mapping, (c) DECISIONS list, intent-first (arc-intent/goal served + P/D-as-how, per ORG-034), (d) plan-flags.

8. **§Dispatch pattern** — orchestrator Agent-`name` (NOT `team-lead` — that's the session-lead's own identity), implementer name, reviewer is HO-on-demand. (Agent-spawned teammates; no TeamCreate.)

9. **§3-REBOUND budget** + chain-of-justification propagation reference (one line each).

That's the whole brief. ~1-2 pages typical.

## Anti-patterns (don't do these)

- **File READ lists** ("read these 15 files"). Implementer can grep + Read. Naming the load-bearing files at scope-level is enough.
- **Verbatim code-block inlines** as carry-forward. State the locked answer at high-level + point to where the verified code lives. Inlining-the-code makes the brief read like a draft commit, not a contract.
- **Multi-place ratification warnings** (the b16 brief had Q3 verbatim quote + warning block + scope-section restate + readback-ask = 4 places). One verbatim quote + readback-ask is enough.
- **Kitchen-sink standing-discipline blocks**. Standing discipline lives in `~/CLAUDE.md` + memory pins. Brief refers; doesn't restate.
- **"Explicitly NOT in scope" enumeration of 12 items**. State the 2-3 most-likely-misread items; trust the implementer for the rest.

## Hard rules (load-bearing)

### Detect-and-reset on sticky-belief drift (within 2 REBOUNDs)

If implementer DECISIONS contradict a verbatim ratification across 2 REBOUNDs, the issue is sticky-belief-overriding-source-text (not state-loss). Re-brief won't penetrate. Reset: shutdown agent + spawn fresh implementer in fresh team. Carry-forward set preserves substantive plan progress; fresh context drops the inverted-belief anchor.

Confirmed 2026-05-07: T-0113 b15→b16 redispatch. b15 implementer drifted on ORG-007 §Q3 across 4 plan iterations despite verbatim brief; b16 fresh-implementer first-fire clean. Per `rulebook/directives.md` D8 (HO self-discipline — sticky-belief at N=2 plan iterations triggers fresh-context redispatch).

### Wet-fire validate external-binary interactions BEFORE finalizing brief

When the brief specifies a CLI invocation (ExecStart, command-line tooling, MCP-server protocols), HO **runs that exact command against the actual binary before finalizing the brief**. Cheap (~30s); catches CLI-flag-interaction class of bugs at brief-time, not deploy-time.

Surfaced 2026-05-07: T-0113 brief specified ExecStart `--session-id $UUID $KINO_RESUME` flip-pattern; claude-p CLI rejects `--session-id <uuid> --resume` combo. Would have been caught with one wet-fire smoke. Lesson lands in T-0114 brief (which DOES wet-fire-validate).

**Binary-in-isolation wet-fire is necessary but NOT sufficient.** When the implementation runs through a multi-layer deploy chain (e.g., systemd ExecStart → socat EXEC: → subprocess argv), brief-time wet-fire of the binary in isolation passes while runtime fails on integration-tokenization. T-0114 surfaced this: orchestrator wet-fired ExecStart shapes against actual claude binary 3 times — caught CLI-flag-interaction class but missed systemd's quoted-EXEC: env-var-expansion+socat-tokenization interaction at runtime. **Brief-template addendum**: if the implementation runs through systemd ExecStart with env-var indirection or socat EXEC: with quoted args, wet-fire-validate the FULL deploy chain (systemd-restart + journal-watch + actual socket connect) before declaring done — not just the binary in isolation. Catalog entries: `bugs/systemd-environment-whitespace-split.md` + `bugs/systemd-execstart-env-var-indirection-vs-socat.md`.

Pairs with: `rulebook/directives.md` D5 (smoke verification discipline — wet-run validate IPC patterns at design-time).

### Integration-seam tests at unit-test level

When implementation has multiple modules wired together (write→read→route style), brief MUST require an end-to-end test at unit-test level that exercises the actual integration path, NOT isolated-component tests with mocks at every seam.

Surfaced 2026-05-07: T-0113 had 3 integration bugs (request_id correlation, missing writer wiring, missing record+push) that all bypassed the test suite because each test mocked around the very seam that was broken. Brief discipline going forward: every wire-protocol cycle gets one test that exercises register→enqueue→write→read→route→resolve end-to-end against a fake-but-realistic socket pair, not three isolated mocks.

Pairs with: `rulebook/directives.md` D4 (brief quality bar — e2e smoke for read-paths from bootstrapped daemons).

### Prod build = required smoke for any cycle touching build config or shipping prod assets

When the implementation touches build config (vite, webpack, esbuild, vite-pwa strategy switches, workbox config, tsconfig with build implications, etc) OR compiles to prod assets that go through a separate build pipeline, brief MUST include the prod-build command as an explicit §Acceptance smoke — separate from `<runner> test`. Unit tests + frontend tests pass cleanly while prod build breaks; the production build pipeline is its own integration seam.

Surfaced 2026-05-07: T-0115 (Phase 3 Web Push) implementer landed all 13 brief-named tests green (137 backend / 34 frontend). HO supervised first-fire ran `bun run build:web` and it failed — `runtimeCaching` block carried over from Cycle 3's generateSW config into current injectManifest config (workbox rejects the key in injectManifest mode). The implementer's switch to injectManifest preserved the rule's intent in the wrong layer. Brief §Acceptance did not list the prod build as a required smoke; orchestrator ran `bun test` + `vitest run` per brief but not `bun run build:web`. Cost: REBOUND #1 to fix.

**Concrete addition:** brief §Acceptance gets a line like:

```
- `cd <build-root> && <prod-build-command>` exits 0 (paste literal final 10 lines of output).
```

…whenever the cycle touches build config or compiles to prod assets. Per `rulebook/directives.md` D5 (smoke verification discipline — orchestrators run smokes directly with literal output).

Pairs with: `rulebook/directives.md` D4 (brief quality bar — prod-build is the same class of integration seam as e2e read-path smokes; unit tests don't exercise it).

### Lean structure

Briefs have gotten longer + more defensive over time. The b16 brief was 2x T-0107's. Per the operator (2026-05-07 msg_924/925/928): the over-defense was structurally wrong — reset is the cheap structural fix for sticky-belief drift, not piling defenses into brief. This template is the canonical lean shape; resist accretion.

When considering adding a defense to the brief, ask: *would reset solve this if it surfaces?* Usually yes → keep brief lean, rely on reset. Defenses go into the brief only when they prevent a class of failure that reset doesn't address (e.g., readback-gate prevents the orchestrator from passing a drifted plan downstream).

## Memory composition (anti-drift layering)

Directive content lives in five layers; each has ONE role, and the composition rule is **each layer references the next-more-authoritative one rather than copying it** (D10 — references, not transclusion). This is what keeps brief / skill / pin / directive content from drifting or duplicating. The §Directives in force section above is the *brief* layer — it operationalizes by id, it does not restate.

| Layer | Role | Composition rule (no-drift) |
|---|---|---|
| `rulebook/directives.md` | **Source of truth** — full D-text + project EX/ED; amendable only via ratification | canonical; everything below points here by id, nothing else restates the full text |
| `pantheon-org-discipline` skill | **Quick-ref**, auto-loaded into every worker — D-id summaries + mis-cite traps | summarizes directives.md, does NOT re-derive; MUST refresh when directives change (the T-0603→T-0609 staleness gap; the deferred auto-sync ticket structurally fixes this) |
| dispatch brief **§Directives in force** | **Per-class operationalization** — which Ds apply for THIS class + one-line form | selects + operationalizes from skill/directives by id; does NOT restate full text |
| HO memory pins (`feedback_*`) | **Drift-catches + nuanced patterns** not (yet) directives | capture recurring lapses; promote to directive/SOP if they stabilize (ORG-012 bubble-up); never duplicate a live directive |
| Letta named-memory-blocks | **Hypothetical future** structured-memory layer | NOT adopted; flat MEMORY.md + this layering is current. Revisit at the `notes/2026-05-19-letta-memory-architecture-study.md` adoption threshold |

The one-line principle: directives.md is canonical; the skill summarizes it; the brief selects-and-operationalizes from it; pins catch what isn't in it yet; Letta is a future shape. No layer copies another — they reference by id.

## Cross-references

- `adr/ORG-034` (two-layer goal architecture — arc-intents as the missing justification tier; the brief's intent-first opening + DECISIONS intent-first derive from here)
- `rulebook/goals.md` (north stars — layer 1, the operator's; arc-intents nest under these)
- `rulebook/principles.md` P1 (architecture over policy), P2 (principles-first, re-grounded as intent-first per ORG-034), P3, P4, P5
- `rulebook/directives.md` D1 (the operator's bandwidth), D2 (hand-curated docs), D3 (autonomy bar), D4 (brief quality bar — verify symbols at source; coverage cross-check; e2e smoke for read-paths), D5 (smoke verification discipline — literal output, orchestrator runs smokes directly, IPC wet-run), D6 (plan-first protocol + locked-answer-deviation handling), D7 (decision flow execution — Team-pattern dispatch; D2 dynamic scope-split; 3-REBOUND budget; bubble-up criteria), D8 (HO self-discipline — sticky-belief at N=2 → fresh-context redispatch), D9 (orchestrator response contract + inbox discipline), D10 (context-frugality / minimum-traversal-to-precise-info), D11 (third-party / outbound-API surface — read the authoritative spec; fixtures from the real surface, never hand-invented)
- `~/CLAUDE.md` §Chain of justification (recursive propagation)
- Auto-memory (user-scope `~/.claude/projects/-home-claude/memory/`):
  - `user_no_time_pressure_in_architecture.md` — strip time-pressure framing from architectural messages
  - `feedback_self_reference_drift.md` — say "I'll write" not "HO writes"
  - Other reference / project / user pins (see MEMORY.md index)
- `bugs/` — gotcha catalog (grep-keyword filenames; reference, not inline)

## Authoring discipline

This doc is hand-curated per D2. HO authors; orchestrator-tier suggests via dispatch reports; only the operator or HO with explicit ratification writes here. Updates ride alongside the failure modes that surface them — refresh after every team arc that surfaces meaningful brief-quality lessons. Resist accretion; if this doc grows past 3 pages, that itself is a smell.
