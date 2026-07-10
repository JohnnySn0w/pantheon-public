# Protocol-variant enumeration — discriminated-union deserialization needs all-variants-in-fixtures-before-parser

**Date authored:** 2026-05-18 (lesson distilled from T-0381 retrospective; surfaced by Specialist T-0401 superpowers-TDD-skill-eval research arc; promoted to lesson per the operator 2026-05-18 msg_1663 picking lessons/-substrate over CLAUDE.md amendment)
**Author:** Orchestrator (Orchestrator)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015; the older "awaiting ratification" convention predated it)
**Re-evaluate when:** another deserializer-class bug surfaces in echo-prismatic or a sibling codebase (a 2nd recurrence point promotes this to "pattern, not anecdote"); or when a structural fix becomes available (e.g., a Rust macro / serde feature that enumerates variants from the protocol spec automatically).

## Observation

T-0381 (echo-prismatic Speechmatics adapter, 2026-05-17) shipped a non-discriminated deserialization bug that broke partway through a live transcription. The bug class:

- The source protocol (Speechmatics v2 WebSocket messages) is a discriminated union with **8 documented variants**: `RecognitionStarted`, `AudioAdded`, `AddTranscript`, `AddPartialTranscript`, `EndOfTranscript`, `Info`, `Warning`, `Error`.
- The test fixtures covered only the **happy-path subset**: `RecognitionStarted` + `AddTranscript`.
- The deserializer was written to those fixtures.
- It shipped passing tests + reviewer-APPROVE + smoke-loop-green — but **refused** at runtime as soon as the live stream produced the first `AudioAdded` envelope (or any of the other 5 variants the fixtures hadn't exercised).
- The failure mode was silent: the deserializer caught the parse error + dropped the message; no log line surfaced the dropped variant; the adapter chain appeared healthy from the outside while the transcript was being silently truncated.

The class is **non-discriminated deserialization shipped because the test fixture only exercised the variants the implementer happened to think of, not the variants the protocol actually documents.**

This matches `obra/superpowers` TDD anti-patterns 1 ("Testing mock behavior instead of real code") and 3 ("Mocking without understanding dependencies") — the fixtures tested the deserializer's expectations of what the protocol looks like, not the protocol's documented shape.

## The lesson

**When writing a deserializer for a protocol with multiple message variants, enumerate ALL documented variants in test fixtures BEFORE writing the deserializer.**

Practically:

1. **Read the protocol spec.** Find the canonical list of variants — every message type the source can emit. For Speechmatics v2, this is in the protocol docs; for many JSON protocols, it's in a `#[serde(tag = "type")]` enum definition upstream or in a JSON Schema.
2. **Build a fixture per variant.** Each fixture should be a real captured payload where possible (recorded from a live session), or a hand-built sample that matches the documented shape exactly. Hand-built samples that mirror the deserializer's expectations rather than the source's actual shape do not count.
3. **Write the deserializer to pass all variant fixtures.** If a variant is genuinely out-of-scope for the current adapter, that's a design decision — capture it in a DECISIONS entry citing P5 (decisions-driven) + name the variant explicitly + commit to either: (a) the deserializer surfaces an explicit unrecognized-variant error (not silent drop), or (b) future iteration adds the missing fixture.
4. **Non-discriminated union → enumeration is mandatory; missing-variant fixtures must surface as test failures, not silent refusals.** This is the structural test: if a variant the protocol documents is absent from the fixture set, the test suite should be red.

For Rust + serde specifically: `#[serde(tag = "...")]` discriminated enums fail closed by default (unknown tags return an error). The bug is when the deserializer's enum doesn't include all the protocol variants → unknown tags surface as parse errors → if the parse error is swallowed instead of surfaced, the bug is silent. Two fixes apply jointly: (i) all protocol variants present in the Rust enum (even ones the adapter doesn't act on — they can be `Ignored` cases); (ii) parse errors that ARE encountered logged loudly, not swallowed.

## Grounding

- **T-0381** — Speechmatics AddTranscript schema-mismatch bug; echo-prismatic adapter; shipped via cycle that passed all gates because the fixtures didn't cover the variants. Forensic detail in the T-0381 resolution log + the subsequent Speechmatics arc T-0389/T-0392 that re-shaped the test infrastructure toward multi-clip fuzzy-match validation.
- **`obra/superpowers` TDD skill — anti-patterns 1 + 3.** Cross-reference in Specialist's `knowledge/watch/reports/2026-05-17/superpowers-tdd-skill-eval.md` §4 ("anti-patterns Pantheon code has hit").
- **Pattern recurrence is currently N=1** (only T-0381 in the documented corpus). This lesson is captured proactively because the cost of one such bug is high (silent data loss) + the discipline is cheap (read the spec; one fixture per variant).

## Why this is lessons/ and not directives.md / CLAUDE.md

- **It's not yet a pattern.** Per the b-pattern recurrence threshold informally tracked in `lessons/instruction-fidelity-collapse-discipline-vs-architecture.md`, a class typically warrants directive-tier codification at 5-6 recurrences. T-0381 is N=1. Promote if a 2nd instance lands.
- **It's narrow.** The discipline applies to deserializer code with discriminated-union shape; broadening to "all NEW crate work + bugfixes" (the original T-0422 framing) overreached the actual finding.
- **CLAUDE.md is for the discipline an agent should always have loaded.** Variant-enumeration is the kind of advice you read once + remember when the situation arises; lessons/-tier is the right shelf.

## Related

- [[T-0381]] — the shipped bug; motivating retrospective.
- [[T-0389]] / [[T-0392]] — the Speechmatics arc that re-shaped test infrastructure post-incident.
- [[T-0401]] — Specialist's superpowers-framework research arc that surfaced this + 5 sibling findings.
- [[T-0422]] — the original CLAUDE.md amendment proposal the operator withdrew (msg_1663); this lesson is the substituted artifact.
- `obra/superpowers` TDD skill SKILL.md — external reference; if a vendor-fork of any superpowers skill lands per T-0405-T-0408 disposition, this lesson cross-references the forked TDD skill at that point.
- `lessons/instruction-fidelity-collapse-discipline-vs-architecture.md` — sibling lesson capturing the discipline-by-prose-ceiling pattern + recurrence-threshold framing.
