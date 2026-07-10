# Diagnosing third-party surface issues

**Status:** Drafted 2026-05-27 by Orchestrator (Orchestrator). **Drafted, NOT yet awaiting ratification** — Phase 1 isn't fully closed; finalize the SOP after the T-0607 ceremony lands so any final-tier surprise gets folded in. Authoring per D2 (HO drafts; the operator ratifies via in-file Status update).

## When to use this

The integration crosses a boundary you don't control: third-party HTTP/WebSocket API, vendor SDK, external daemon (HA, j17), MCP server, LLM API. The integration is misbehaving in some way — wrong wire shape, behavior not matching docs, downstream rejection, silent no-op, performance anomaly. Diagnosing-by-reading-our-code isn't working because the bug straddles the boundary.

Use this SOP when:

- A change to outbound or inbound code surfaces behavior unit tests didn't predict
- The third-party API accepts the request but downstream behavior diverges from expectation
- Errors are generic / opaque / silently swallowed by the integration
- "Worked yesterday, broken today" against an unchanged external service

Don't use this for purely-internal bugs (our IPC, our state machines, our schemas) — those are diagnosable by reading the code; the third-party surface adds spec-discovery overhead this SOP exists to amortize.

## Procedure

Walk the 5 evidence layers in order. Each layer covers a class of bugs the prior layer's verification surface cannot see by construction (see `lessons/verification-tier-blind-spots-each-covers-what-the-previous-cant-see.md` for the recurrence shape).

### Layer 0 — spec source-of-truth

**Action:** open the upstream docs URL. Read the section that covers the surface you're touching, *fresh*, not from memory or internal research notes.

**Verify:** can you produce 2-3 direct field-name / value-range / behavior quotes from the canonical doc? If you can't, you don't actually have the spec yet — keep reading before scoping the fix.

**Escalation criterion:** docs are stale, ambiguous, or silent on the behavior you observe → escalate to provider's support / community forum / changelog before guessing. Don't proceed to layer 1 with incomplete spec.

**Pantheon failure mode this layer catches:** "research-summary != docs" drift (T-0603 implementer used T-0595 internal research notes as source of truth; the summary partially mis-described the schema; tests inherited the wrong shape). The canonical docs are the truth; internal research notes are someone's read, frozen at the moment of reading.

### Layer 1 — live wire (single direction)

**Action:** run the integration code against the real endpoint with debug logging on. Capture the wire-level request + raw response. Don't swallow errors via `?` short-circuits or `_ => {}` catch-alls — log them at debug level.

For Pantheon-Echo specifically: `RUST_LOG=info,echo=debug,echo_adapter_speechmatics=debug,echo_core=debug,echo_j17_bridge=debug,echo_adapter_silero_vad=debug,echo_adapter_claude=debug` (already pinned in `/home/claude/echo-data/run-echo.sh` per msg_2358).

**Verify:** the request payload matches the spec shape from layer 0 byte-for-byte. The response is captured raw (not just parsed-into-our-types).

**Escalation criterion:** payload rejected with explicit error (e.g., Speechmatics `protocol_error`, HA `cannot target all devices`) → the error message is the spec violation; fix the request shape; layer-0 had a gap.

**Pantheon failure mode this layer catches:** wire-shape mismatch invisible to unit tests because the tests mock the boundary using the implementer's own (wrong) struct shape (T-0609). Fake-server tests have zero discriminating power on wire-shape correctness — they mirror your assumptions.

### Layer 2 — full roundtrip (both directions; multi-step workflows)

**Action:** for a workflow that touches the boundary in multiple steps (enrollment → identification; create → read → update; outbound → inbound response), exercise EVERY step the change touches. Not just the immediate call.

**Verify:** evidence from every step the change affects, not just the leg you patched. State that survives across steps (auth tokens, opaque identifiers, session IDs) must be checked at each step's boundary.

**Escalation criterion:** behavior good at step N, fails at step N+1 → step N's verification was insufficient; investigate what step N's success depended on and verify those preconditions still hold at step N+1.

**Pantheon failure mode this layer catches:** model-variant binding between enrollment and identification (T-0612). T-0609 wet-run covered enrollment only; the daemon-identification session start surfaced the model-incompat warning. "Wet-run covers every state transition the change touches" — not just the immediate call.

### Layer 3 — persistent state (sqlite / disk / cache)

**Action:** query the persistent state directly. For Pantheon-Echo: `sqlite3 /home/claude/echo-data/echo.db` (or `python3 -c "import sqlite3..."` until the CLI is installed). Find the event records for the utterance / transaction / object you're investigating.

**Verify:** the persisted payload has the fields you expected, with values matching what the live wire produced. Mismatches here indicate parsing bugs (the daemon received correct data but stored it wrong) OR write-path bugs.

**Escalation criterion:** field empty/missing in persisted state but present on the wire → inbound parsing bug; struct definition doesn't match wire shape.

**Pantheon failure mode this layer catches:** inbound parsing bug where the daemon's `AddTranscript.speaker` field at root-level wasn't where Speechmatics actually puts the field (T-0614). The daemon log was filtered to INFO; the bug was invisible at log-level alone; sqlite preserved the empty `speaker_label`. Query the store, not just the log.

### Layer 4 — raw protocol frames (debug logging)

**Action:** bump debug logging on the relevant first-party crates (NOT full `RUST_LOG=debug` — third-party deps drown the signal). Restart the daemon. Reproduce. Read the raw frames the third-party is actually sending.

**Verify:** the third-party's response shape matches what your inbound parsing expects. Field paths, nesting, types, optional-vs-required.

**Escalation criterion:** the third-party's response has the field you wanted but at a different nesting level than your struct expects → fix the struct + add a captured-from-real-API JSON test fixture pinning the boundary contract (D11 corollary).

**Pantheon failure mode this layer catches:** discovery that Speechmatics nests `speaker` at `results[*].alternatives[*].speaker`, not at AddTranscript root (T-0614). The raw frame is the truth; the implementer's struct assumption was the wrong derivation.

### Layer 5 — provider-side state

**Action:** if layers 0-4 all agree but the integration still misbehaves, the provider may have side-effects you can't see from the client. Possibilities: rate limits, quotas, model versioning, account flags, A/B routing, expiring tokens, regional binding.

**Verify:** check the provider's documentation for any provider-side state that could affect your specific account / endpoint / model variant. Probe for visible state (concurrent-session usage, quota counters) — Speechmatics includes these in `Info` messages, for instance.

**Escalation criterion:** the docs are silent on provider-side state and the behavior still doesn't match → the only remaining diagnostic surface is the provider's support channel. Escalate to the operator for vendor outreach.

**Pantheon failure mode this layer catches:** Speechmatics' `speaker_sensitivity` default favored generic clustering over enrolled matching (T-0615); no client-side debugging would have surfaced the default-value behavior — required reading the docs + experimenting with the value range.

## Common failure modes (distracters during diagnosis)

| Distractor | Symptom | Fix |
|---|---|---|
| **Symptomatic ticket framing** | The ticket says "X is the problem" but real cause is deeper | Don't trust the ticket's diagnosis without spec-probe. Re-read layer 0; the framing may be wrong. |
| **Debug logging too narrow OR too broad** | Either you can't see the bug, or the signal drowns | Scope to first-party crates at debug; third-party deps at info. |
| **Mock-server-test passes; live-API fails** | Tests give green; integration broken | Mocks reflect implementer assumptions, not external party behavior. Treat mock-tests as necessary-but-insufficient. (D11) |
| **Single-leg wet-run** | Direct call works; subsequent step in same workflow fails | Wet-run must cover full roundtrip, not just immediate call. (Layer 2) |
| **Default-value silent acceptance** | Server accepts payload, behavior diverges from expectation | Provider-side defaults are part of the contract; query/document them. (Layer 5) |
| **Errors swallowed in catch-alls** | The fix doesn't work; no error in logs | Audit the integration for `?` short-circuits, `_ => {}` matches, unhandled-variant fall-throughs. Surface raw error frames at debug. |
| **Deploy-verification gap** | Smoke green; rebuild succeeded; running daemon still misbehaves | The cargo-build command succeeding ≠ the running process executing new code. Verify mtime + a known-new log line on startup. (D11 deploy-verification corollary) |

## Boundary

This SOP does NOT cover:

- **Internal-only bugs.** If both sides of the bug are our code (our IPC, our state, our schemas), this SOP is overkill — read the code.
- **Pure performance issues.** Latency / throughput / memory anomalies have their own diagnostic shape (profiling, tracing, load patterns) that this SOP doesn't touch.
- **Multi-provider failures.** If the bug spans two third-party surfaces (e.g., Anthropic API + Home Assistant), apply this SOP to each surface independently before correlating.
- **Provider outage triage.** When the third-party is genuinely down, the answer is "wait + monitor + update users," not diagnose-by-evidence-layer.
- **Security incidents.** Compromised credentials / unauthorized access / data exfiltration → escalate immediately to the operator, don't grind through layers.

## Cross-references

- **D11** (third-party surface verification) — `rulebook/directives.md`, ratified 2026-05-22. The directive this SOP operationalizes.
- **`lessons/verification-tier-blind-spots-each-covers-what-the-previous-cant-see.md`** — the meta-pattern this SOP procedurally addresses.
- **`skills/verification-before-completion/SKILL.md`** (T-0620) — discipline-tier complement: don't claim work done without fresh verification evidence.
- **`skills/dispatching-parallel-agents/SKILL.md`** (T-0621) — when to fan out diagnostic work across multiple parallel investigations vs serial.
- **`skills/subagent-driven-development/SKILL.md`** (T-0622) — how to brief a researcher / implementer for boundary-spec work.
- **`notes/2026-05-26-echo-speaker-id-diagnostics.md`** — the working-tier diagnostic notes from the Echo arc that seeded this SOP.
- **T-0603 → T-0616 ticket arc** — `tickets/done/T-060{3,9,12,14,16}.md` — the concrete case studies the layer-recurrence is drawn from.
- **Speechmatics realtime speaker-id docs** — https://docs.speechmatics.com/speech-to-text/realtime/speaker-identification — example of a docs URL that's authoritative for layer 0; structurally similar surfaces apply to j17, HA-MCP, Anthropic Claude API.
