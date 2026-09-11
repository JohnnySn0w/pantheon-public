# SOP — tap the latent corpus (before bubbling any design/methodology question)

**Status:** DRAFT — operator-ratify pending (surfaced TG 8809 on 2026-08-29, HO drafted post-T-3341 bubble correction).
**Scope:** fleet-wide — HO, project-orchestrators, Cairn (all roles), reviewers, implementers, research. Any persona/role about to bubble a design or methodology decision upward.
**Justifies-via:** D1 (preserve the operator's bandwidth — corpus-informed proposals absorb variance the fork-to-operator pattern radiates), P3 (decisions justify upward with rationale — citations ARE the rationale), P5 (tier authority — bubble the ratify, not the option-narrowing).

## The rule

Before any of the following, pause and query the corpus first:

1. About to bubble a design or methodology question upward (worker → orchestrator, HO → the operator, Cairn → the operator).
2. About to split a decision into "here are two/three directions, pick one" in a bubble.
3. About to say "this is architectural methodology, above my tier."
4. About to reach for a novel-feeling framing on a problem in a well-trodden domain (testing, distributed systems, concurrency, security, verification, databases, compilers, ML, software engineering practice).

Ask: **"who has written about this exact class of problem, and what did they conclude?"** Then produce the answer with citations, not the fork.

## Why

The operator, 2026-08-29 (TG 8809), after HO's T-3341 Arc B methodology bubble handed him a fork: *"what literature talks about this topic, and can you summon ideas from it, from within yourself? tap into that latent (space) power. It's in your corpus somewhere, almost assuredly. Every senior to staff to principle engineer who has ever written something, is inside of your weights. That's hefty power there!"*

The load-bearing insight: a persona with access to the corpus of every senior/staff/principal engineer who wrote about a domain, and yet still bubbles the framing upward for pick-selection, is wasting the asset. The operator isn't the tie-breaker between two thin options — the operator is the ratify-tier for a corpus-informed proposal. **The corpus does the option-narrowing.** The persona brings a defensible pick.

Backing case: T-3341 (T-3334 Arc B methodology bubble). Cairn's fresh-dispatch R1 hit "impl fabricates observations, tests don't catch it" 3/3 rounds. HO bubbled two candidate methodology directions to the operator as a fork ("adversarial-test-writer split" vs "mutation-testing gate"). Both were re-inventing well-known patterns. The corpus had the answer:

- **Meszaros 2007 (xUnit Test Patterns)** — the pathology has a name: *Overspecified Software* (test verifies impl-internal shape) + *Fragile Test* (breaks on internal shift, misses on behavior shift). ScriptedProbe was textbook Overspecified.
- **Mutation testing** (Lipton 1971 origin; PITest, Stryker, MutMut current tooling) — formalizes the invariant Arc B was missing: "test kills mutants = test depends on code path."
- **Fowler, "Mocks Aren't Stubs" (2007)** — the London/Detroit schism; Arc B was the London-school extreme with impl-controlled mocks.

Corpus-informed answer: "mutation testing + ban the Overspecified test-double, replaced by a fast-teardown real-probe utility (make the correct thing easier)."

## How to apply

1. **Name the problem class.** Not the specific ticket — the general shape. "Test-double substitution that passes shape-match but fails behavior-check" is the class; "ScriptedProbe substitutes for RuntimeControllerProbe" is the instance.
2. **Query the corpus.** Which body of literature covers this class? Testing methodology (Meszaros, Fowler, Beck, Freeman-Pryce). Distributed systems (Lamport, Lynch, Kleppmann, Bailis). Concurrency (Herlihy & Shavit; Lampson). Security (Saltzer-Schroeder, threat-modeling literature). Verification (Dijkstra, Hoare, Lamport TLA+, Alloy). Databases (Stonebraker, Bernstein, Kleppmann). Compilers (Dragon Book, Appel, Pierce type theory). ML (Goodfellow, Sutton-Barto, Bishop). If you don't know which body, name that gap first.
3. **Summon 2–5 specific references.** Authors, canonical works, patterns by name. If a pattern has a name in the literature (Overspecified Software, Fragile Test, Optimistic Concurrency Control, Two-Phase Commit, Byzantine Fault, Adversarial Validation, Metamorphic Testing, MC/DC coverage, Design by Contract, Property-Based Testing), use the name — it triangulates the reader into shared vocabulary.
4. **Compose the answer.** State what the literature says the solution is. Give the specific mechanism (mutation testing + antipattern ban, not "let's design something"). Preserve the option space at the bottom for override, but LEAD with the corpus-informed pick.
5. **Cite.** Even one citation ("per Meszaros's xUnit Test Patterns") signals to the ratifying tier that the answer isn't invented ad-hoc.

## Anti-pattern

- "Two candidate directions, both bubble-worthy for you." (T-3341 shape.)
- "This is architectural methodology, above my tier." (Skips step 2.)
- "I don't know how other teams solve this." (You almost always do — go check.)

## Pattern

- "Per [author/pattern name], the canonical answer is X. Applied here: X specifically means Y. Filing as decision + ready to prototype."

## Domain sampler (checklist when the domain-fit isn't obvious)

- **Testing / test-design:** Meszaros (xUnit Test Patterns), Fowler (Mocks Aren't Stubs, Testing Pyramid), Freeman & Pryce (GOOS), Beck (TDD), van Deursen (test smells), mutation testing (Lipton, PITest/Stryker/MutMut), property-based testing (QuickCheck/Hypothesis), metamorphic testing (Chen 1998), MC/DC (DO-178B), contract testing (Pact, Bertrand Meyer's DBC).
- **Distributed systems:** Lamport (Time/Clocks/Paxos), Lynch (Distributed Algorithms), Kleppmann (DDIA), FLP impossibility, CAP/PACELC, Bailis (linearizability + isolation levels), Vogels (eventual consistency), CRDTs (Shapiro).
- **Concurrency:** Herlihy & Shavit (Art of Multiprocessor Programming), Lampson (Hints for Computer System Design), Lea (Concurrent Programming in Java), lock-free literature (operator-Scott queues, hazard pointers), memory models (Adve & Gharachorloo).
- **Security:** Saltzer & Schroeder 1975 (design principles: least privilege, defense in depth, complete mediation, etc.), Schneier, Anderson (Security Engineering), threat modeling (STRIDE, PASTA), formal-methods security (Bell-LaPadula, Biba, Clark-Wilson).
- **Verification / formal methods:** Dijkstra (guarded commands, weakest preconditions), Hoare (CSP, Hoare logic), Lamport (TLA+, temporal logic), Alloy (Jackson), model checking (Clarke, Emerson, Sifakis), refinement types (Rondon, Vazou/Jhala).
- **Databases:** Stonebraker + Hellerstein readings, Bernstein/Newcomer (Principles of Transaction Processing), Kleppmann (DDIA), Gray + Reuter (Transaction Processing), MVCC literature.
- **Compilers / PL:** Aho/Sethi/Ullman (Dragon), Appel (Modern Compiler Impl), Pierce (Types and PL), Muchnick (Advanced Compiler Design), abstract interpretation (Cousot).
- **ML / stats:** Bishop (PRML), Goodfellow (DL), Sutton & Barto (RL), Murphy (probabilistic ML), Hastie/Tibshirani (ESL), adversarial ML (Goodfellow et al. FGSM/PGD, Madry).
- **Software engineering practice:** Brooks (Mythical Man-Month, No Silver Bullet), Parnas (information hiding, cohesion), Wirth, McConnell (Code Complete), Fowler (Refactoring, Patterns of EA), Evans (DDD), Bertrand Meyer (OOSC).

## When NOT to apply

- The problem is genuinely novel (rare — verify by attempting the query first).
- The domain is Pantheon-specific (persona role design, ledger schema, ticket flow) where the corpus doesn't have direct analogues. Then the corpus supplies principles, not specific patterns — cite the principle-level references (e.g. Parnas information-hiding for a persona-boundary decision).
- The decision is a value/preference question (the operator's tone, business direction, budget). The corpus doesn't have his answer.

## Fleet inclusion

This SOP is fleet-wide (all personas, all roles). The pantheon-persona-orientation skill (T-3301 landed 2026-08-27) already carries the SOP-discovery convention; once ratified, this SOP becomes discoverable via that skill's checklist. T-3339 (fleet persona-orientation parity across Claude + Codex, plan drafted 2026-08-29) will subsume its role-relevant sections into role deltas for HO / project-orchestrator / reviewer / implementer / research where the discipline applies.

## Ratifiable files

- `sop/standard/tap-the-latent-corpus.md` (this file)
- `~/.claude/skills/tap-the-latent-corpus/SKILL.md` (Claude-side skill, personal auto-load, already landed 2026-08-29)

## Related

- `rulebook/policies/autonomy.md` — the autonomy calibration this SOP feeds into (corpus-informed proposal raises the autonomy bar because the pick is defensible)
- `rulebook/directives.md` — D1 (the operator's bandwidth), D3 (autonomy at lowest competent tier)
- `rulebook/principles.md` — P3 (decisions justify upward), P5 (roles don't hop)
- T-3341 (the backing-case ticket)
- T-3339 (fleet persona-orientation parity — SOP-inclusion vehicle)
