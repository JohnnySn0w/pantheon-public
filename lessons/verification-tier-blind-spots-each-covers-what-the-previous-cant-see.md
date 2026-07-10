# Verification-tier blind-spots — each tier covers what the previous can't see

**Date authored:** 2026-05-27
**Author:** Orchestrator (Orchestrator)
**Status:** Drafted (HO-authored; lessons are not ratifiable per ORG-015 — they inform but don't bind)
**Re-evaluate when:** another integration arc surfaces a 5th blind-spot tier this lesson doesn't name; OR when the verification trilogy skills (verification-before-completion / dispatching-parallel-agents / subagent-driven-development) ship a structural fix that retires the lesson's load-bearing claim; OR when a structural primitive (typed schema validation, recorded-and-replayed wire fixtures, etc.) collapses two of the named tiers into one.

## Observation

When integrating with a third-party surface (API, library, daemon, protocol), bugs hide at verification tiers the previous round didn't cover. Each tier surfaces a blind spot the structurally-prior tier *cannot see by construction*. The implication: you cannot pick one verification tier and declare it sufficient; tiers compose.

Concrete shape across the 2026-05-26 Echo Speechmatics speaker-identification arc — four rounds of bugs, each invisible at the prior round's verification surface:

| Round | Bug | Why invisible at prior tier | Tier that exposed it |
|---|---|---|---|
| T-0609 | Wire schema rejected by live API (outbound) | Unit tests passed because they mocked the WS server using the implementer's own (wrong) struct shape — fixtures mirrored the bug | Live wet-run against real Speechmatics |
| T-0612 | Model-variant binding mismatch (`operating_point` enrollment ≠ daemon) | Wet-run probe covered enrollment ONLY; the daemon's identification session (which is the second leg of the same workflow) wasn't probed | Daemon startup log — "incompatible model" warning at session start |
| T-0614 | Inbound parsing — `speaker` field nested at `results[].alternatives[].speaker`, not at root | No test ever ran `serde_json::from_str::<AddTranscript>` on a captured-from-API frame. Tests assumed root-level. | Sqlite event-store query (`speaker_label` empty across all stt_final post-T-0612) |
| T-0616 | Claude classifier hallucinated `entity_id` because adapter discarded HA-MCP tool schemas | Tests passed (mocked HA call); live API accepted (Claude API doesn't validate downstream HA semantics); only HA's response surfaced "Service handler cannot target all devices" | HA action_results error captured in utterance terminal payload |

Each tier has a blind-spot the next tier covers. The blind-spots are STRUCTURAL — not "the implementer forgot to check" but "the verification surface used at that tier cannot detect this class of bug by construction."

## Grounding

- **T-0609** through **T-0616** — full ticket arc in `pantheon-config/tickets/done/T-060{9,12,14,16}.md`
- **D11** (Third-party surface verification) — `rulebook/directives.md`, ratified 2026-05-22 with the test-fixture corollary added 2026-05-23
- **Phase 1 diagnostic notes** — `pantheon-config/notes/2026-05-26-echo-speaker-id-diagnostics.md` (the 5-evidence-layer SOP-draft material)
- **Verification trilogy skills** (HO-drafted 2026-05-27) — verification-before-completion (T-0620), dispatching-parallel-agents (T-0621), subagent-driven-development (T-0622)
- **Ledger entries** for the Phase 1 arc closeout (forthcoming once ceremony passes)

## Design implications

### Implication 1 — verification surfaces compose; the brief should enumerate ALL surfaces the change touches

A brief that says "wet-run probe against the API" is necessary but not sufficient for a change that affects both outbound and inbound paths. The brief must enumerate every verification surface the change traverses:

- Wire shape (outbound) → captured-payload test fixture + live wet-run
- Wire shape (inbound) → `serde_json::from_str` test against a real captured frame
- Behavior across the boundary (semantic, e.g., model-variant compat) → full-roundtrip wet-run, not just the leg the change touches
- Persistence (sqlite, disk, cache) → query the persisted state, not just the in-flight messages
- Downstream consumer behavior (e.g., HA accepting/rejecting) → capture action_results errors in the utterance payload

The `subagent-driven-development` skill's 10-item brief-design checklist already includes "acceptance bar (D11 if applicable) with concrete evidence format." This lesson sharpens it: for outbound-API integrations, enumerate the verification surfaces explicitly.

### Implication 2 — debug logging needs to be on long enough to capture multi-tier evidence

T-0614's diagnosis required raw `AddTranscript` JSON frames from Speechmatics, which only became visible after we bumped `RUST_LOG=info,echo=debug,echo_adapter_speechmatics=debug,...`. Default INFO-level logging hid the load-bearing diagnostic.

Pinned 2026-05-26 per msg_2358: scoped debug logging for the duration of Echo Phase 1 development. The lesson: **for any integration phase, scope debug logging to the relevant first-party crates and KEEP IT ON until that phase closes.** Don't pre-optimize for log volume; pre-optimize for diagnostic surface.

### Implication 3 — diagnostic notes file structure should mirror the evidence layers

The Phase 1 diagnostic notes file structures findings by 5 evidence layers (spec / live wire / full roundtrip / persistent state / debug-logging / provider-side state). This shape generalizes: any future outbound-API integration's diagnostic notes should organize by the layers, not by chronological order. The layer structure is the recurrence — chronological is incidental.

Crystallization target post-Phase-1: lift the 5-layer structure into `sop/remediation/diagnosing-third-party-surface-issues.md`.

### Implication 4 — symptom-level ticket framing is itself a verification blind-spot

T-0616's ticket framed the bug as "Claude classifier produces too-generic entity_id." The live HA-MCP probe revealed the bug was deeper: HA-MCP tools don't accept `entity_id` AT ALL. The classifier was hallucinating the parameter because the adapter discarded the tool schemas.

The blind-spot here is at the ticket-framing tier — the brief inherited the symptom-level diagnosis from the calling context. The implementer's D11 spec-probe corrected the framing. **Discipline implication:** ticket framings for outbound-API issues should encourage / require a live spec-probe in plan-tier, not after implementation begins.

This composes with the brief-design checklist in `subagent-driven-development` skill — item #2 ("Why this work exists") should include "ticket framing may be symptom-level; verify via live spec-probe before scoping the fix."

### Implication 5 — the ratified directive (D11) is necessary but not sufficient

D11 was effective 2026-05-22. T-0603 was implemented 2026-05-26 — four days after D11. T-0603 implementer didn't apply D11, despite the directive being live. The auto-loaded `pantheon-org-discipline` skill at the time of T-0603 carried D1-D3 only, not D4-D11.

**Discipline implication:** ratified directives propagate via the skill mechanism, not via osmosis. The skill must be refreshed when new directives ratify. This led to msg_2336+2340 — the skill refresh + dispatch-brief D-enumeration ticket T-0611. Both are structural responses to this lesson.

Composes with `feedback_implementer_methodology_signatures_first.md` (msg_2371) — the 5-phase sequence's "tests" phase is where D11's test-fixture corollary lives. Without the corollary explicitly named, implementers may write hand-invented fixtures even with the right phase structure.

## Open questions

- **Can we structurally collapse adjacent tiers?** For example: a typed schema validator that catches inbound-parsing bugs at compile time would collapse the "wire shape (inbound)" tier into the "wire shape (outbound)" tier. Cost: structural complexity + per-surface schema modeling. Benefit: one less tier where bugs hide. Not pursued today; revisit when the codebase has more outbound-API integrations than humans can keep track of.

- **What's the right tier-count?** This lesson names 5 evidence layers; the Echo arc surfaced 4 bug rounds. Is the layer enumeration complete, or are there blind-spots at tiers I haven't yet named? Re-evaluate the lesson's enumeration if a 5th outbound-API arc surfaces a bug at a tier outside the current list.

- **Generalize beyond outbound-API?** The blind-spot recursion shape may apply to any non-trivial integration boundary — internal IPC, database schema changes, file-format evolution. Not yet evidenced; defer until a non-API arc surfaces a similar pattern.

- **Cost of every-tier-discipline?** Enumerating all verification surfaces in every brief adds brief-bloat. The trade-off between brief-completeness and brief-readability is real. Possible structural fix: brief templates that include the enumeration by ticket-class (outbound-API, substrate, refactor, etc.) — which is exactly what T-0611 (dispatch-brief D-enumeration) covers. Watch for the right shape there.

## Cross-links

- D11 (Third-party surface verification) — `rulebook/directives.md`
- Phase 1 diagnostic notes — `notes/2026-05-26-echo-speaker-id-diagnostics.md`
- Verification trilogy skills (T-0620 / T-0621 / T-0622) — `skills/verification-before-completion/` + `skills/dispatching-parallel-agents/` + `skills/subagent-driven-development/`
- T-0611 (dispatch-brief D-enumeration follow-up) — `tickets/inbox/T-0611.md`
- T-0603 → T-0616 ticket arc — `tickets/done/`
- `lessons/instruction-fidelity-collapse-discipline-vs-architecture.md` — adjacent pattern; this lesson is the third-party-surface variant of the same broader "structural enforcement is necessary but discipline is also necessary; neither replaces the other" insight
