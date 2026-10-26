---
aliases: [ORG-073]
title: "Concordance — retrieval-policy and injection layer (decision-corpus PoC)"
status: "RATIFIED 2026-08-21 by the operator (TG msg 8093, via T-3001). Supersedes the 2026-08-12 v1 ratify."
date: 2026-08-20
author: Orchestrator (HO) with Consultant (Codex, ORG-041) spec review; Specialist (claude-research) foundational research (2026-08-10 info-flow study)
relates: ORG-058, ORG-062, ORG-065, ORG-061, ORG-017, ORG-035, ORG-012, ORG-044, ORG-031, T-2107, T-2106, T-2113, T-2425, T-2533, T-2816, T-2998, T-2999, T-3000
supersedes:
  - id: ORG-073-v1
    ratified: 2026-08-12
    disposition: retired-on-ratification
build_disposition: needs-build
scoping_ticket: T-3006
re_evaluate:
  - id: R1
    type: event
    when: "the decision-recall-injection PoC is first built and measured on the decision-corpus"
    fires: surface
    text: "PoC hit/precision numbers may revise the Last_K default, embedding-model choice, corpus-class aggregation rule, or corpus scope"
  - id: R2
    type: event
    when: "any HookInjectionProvider native encoder is added for a new harness"
    fires: surface
    text: "new harness surface may reveal cross-harness contract gaps; re-verify the injection-shape contract remains uniform"
  - id: R3
    type: event
    when: "PoC PMB-1 evaluation returns a blocking regression on any Track (A/B/C/E)"
    fires: surface
    text: "irrelevant-injection harm exceeded acceptable bounds OR another track failed — re-evaluate the top-K unconditional-inject decision + the corpus adaptation choices"
---

# ORG-073 — Concordance: retrieval-policy and injection layer

**System name:** **Concordance** — umbrella term for the retrieval-inject substrate this ADR builds. The name generalizes across two intended corpora — decision-corpus (Pantheon-org: ADRs + ledger + notes) and memex-corpus (the operator's personal knowledge) — reflecting the traditional "cross-reference tool over a body of writing" meaning. Instances scope-qualified as needed (e.g. `Concordance/decision`, `Concordance/memex`).

**Scope of this ADR:** decision-corpus PoC only. Memex-instance activation, push-at-write mechanism, and post-PoC extensions are separately-scoped follow-up ADRs (see §Post-PoC follow-ups below) with their own scoping tickets, gated on this ADR's PoC passing PMB-1.

**Source record:** `knowledge/episodic/2026-08-10-information-flow-optimization-study/` (the how-it-works-now / SOTA / what-we-can-do research), the the operator Telegram arc 2026-08-10, and 2026-08-20/2026-08-21 rewrite direction (naming, anchor-architecture framing, generalization framing, PoC-scoped-only shape).

**Extends:** ORG-058 (memory classes + envelope + purpose-bounded retrieval), ORG-062 (substrate evaluation methodology + PMB-1 gate). **Composes with:** ORG-012 (policy outflow), ORG-044 (digest-altitude), ORG-031 (harness-abstraction partition-and-document), ORG-061 §1 (harness dev-sequence). **Sibling:** ORG-074 (self-improvement — the *learning* facet; this is the *retrieval* facet).

## Context (the failure this ADR closes)

Operator-observed (the operator, 2026-08-10): personas act without knowledge that *exists in the repo* — an ADR or prior decision they should have applied but never traversed to in-session, and which was not in their handoff. **Retrieval/injection gap, not a storage gap** — the knowledge exists, it just isn't reaching the persona at moment-of-need.

Three mechanical causes:

1. **Decisions rely on active traversal ("index-and-hope").** The existing recall hook (`tools/hooks/recall-feedback.py`, T-1090 — a PreToolUse footgun-guard on mutating tools that surfaces `feedback`/`project` memory pins matched on EXACT tokens: file paths, `T-NNNN`, `ORG-NNN`, basenames; narrow-precision by design) is exact-token-only over memory pins. ADRs/ledger/notes are NOT in any recall path; discovery depends on the agent knowing to look. Literature: LLM agents that can't cheaply resolve an index guess from training priors rather than traverse.
2. **Compaction + handoff are lossy summarization.** Context rot (30-50% accuracy loss well before window fill) forces compaction; both intra-session compaction and cross-session handoff silently drop detail (T-2533 / ACE).
3. **Triggered injections accumulate.** Content injected for one past turn re-sends every subsequent turn, carrying token cost with no ongoing value.

ORG-058 specifies the memory model but not the operational retrieval/injection mechanism. This ADR decides that mechanism.

## Decision

Establish Concordance — a retrieval-policy and injection layer — as the operational realization of ORG-058's purpose-bounded retrieval for the decision-corpus. Git-durable, provenance-carrying (ORG-058 envelope), audit-first. The retrieval-stack selection is settled by prior Specialist work (T-2106 V1 build, T-2113 GPU benchmark, T-2107 eval-protocol); its applicability to the decision-corpus remains a PoC validation obligation.

### 1. Substrate — stack selection SETTLED; per-corpus adaptation is PoC obligation

- **Retrieval stack:** ModernBERT (`nomic-ai/modernbert-embed-base`, mean-pooled, nomic `search_document:`/`search_query:` prefixes) → **FAISS `IndexFlatIP`** (exact cosine on normalised vectors) + **BM25Okapi** → **RRF fusion (k=60)**. Proven via T-2106 on GPU VM 210.
- **Latency (measured V1 path only):** ~25ms GPU encode plus microsecond-scale FAISS search for the measured V1 path. The PoC MUST measure actual hook-to-injection latency including transport, BM25, fusion, filtering, provenance lookup, and rendering; T-2113 does not establish that full-path number. **Target: under the harness hook timeout.** Per-harness verified defaults (T-3006 pre-verify 2026-08-21):
  - **Claude Code `UserPromptSubmit`:** default 30s, per-hook configurable via `timeout` field (Claude Code hook docs).
  - **Codex CLI `UserPromptSubmit`:** default 600s (10 minutes) via `normalize_command_hook` (`codex-rs/hooks/src/engine/discovery.rs:720-729`), per-hook configurable via `timeout_sec`.
  
  Tighter constraint dominates for cross-harness parity → target the Claude 30s default (or bump the Claude hook's `timeout` field to a higher value, which is a supported knob). The PoC MUST still measure actual observed hook-to-injection latency at build time to confirm behavior matches these documented defaults. Those timeouts, not an operator preference, are the actual latency anchor.
- **NO graph tier at PoC.** Defer knowledge-graph (GraphRAG / HippoRAG / typed edges) until multi-hop provenance need is demonstrated on measured data. ORG-065's typed-edge base graph is now built (T-2673 + BT1-5 + T-3328); it is the substrate if/when typed-edge retrieval is needed, but is not this ADR's scope.

**Decision-corpus adaptation obligations (PoC MUST test — NOT presumed from personal-vault performance):**

- (a) YAML frontmatter separated from or field-weighted against body prose rather than embedded as an undifferentiated chunk;
- (b) ledger entries indexed as bounded logical records, not arbitrary file chunks;
- (c) timestamps, hashes, ticket IDs, and ADR IDs preserved as lexical tokens without dominating semantic similarity;
- (d) cross-reference-heavy ADR passages prevented from retrieving primarily because they enumerate many related IDs;
- (e) chunk-to-document max-pooling is the V1 baseline, NOT an immutable choice: the PoC MUST compare it against a corpus-appropriate aggregation rule because one incidental high-scoring cross-reference chunk can dominate an ADR under max-pooling.

**The PoC MUST report retrieval quality separately for ADRs, ledger entries, and designated notes.** Passing on aggregate while one corpus class fails is not sufficient.

**Design constraints from V1 measurements (T-2106):**
- Absolute cosine threshold does NOT separate relevant from irrelevant (garbage ~0.400 vs real ~0.417). **Consequence: retrieval MUST NOT gate on absolute score.** See §3 for the injection semantics + irrelevant-injection harm-measurement obligation.
- Chunker must defend against empty documents (T-2106 §Addendum: empty-chunk artifact surfaced blank docs as identical vectors, ranked as a block).
- **Reranker is a preregistered challenger, not a PoC baseline.** T-2106's reranker result (gte-reranker-modernbert-base strictly dominated cosine at 0.065 margin on n=8) is retained as a measured challenger. It may be evaluated in a paired optional arm, but its thin margin does not authorize a cutoff and it is not part of the settled PoC baseline.

### 2. Scope — decision-corpus only

- **In-scope corpus:** all ADRs (`adr/`), ledger entries (`ledger/`), designated notes (`notes/*.md` marked with a `description` frontmatter field per §5).
- **Explicitly out-of-scope for this ADR:** tickets, memory pins, code, incidents, bugs, lessons, watch reports, code comments, and the memex-corpus. See §Post-PoC follow-ups for how those get scoped separately.

### 3. Retrieval semantics — inject-and-let-model-verify with measured harm bounds

**Candidate-generation policy:** every prompt queries the hybrid semantic/lexical stack. A **Last_K = 3-5 recency lane** is fused as a bounded prior/safety lane; it is NOT an unconditional instruction to inject the newest decisions regardless of prompt relevance. **The PoC MUST compare hybrid-only, recency-only, and fused hybrid+recency variants on identical queries before fixing the default.**

**Injection framing:** top-K results injected as a "recalled decision — verify it still applies" system-reminder-style hook context. Precision-capped (K ≤ 5 by default). Per-session dedupe (don't re-inject an entry already surfaced this session).

**Injection-vs-no-op contract:**

- Every eligible retrieval that produces ranked, current, unseen candidates injects top-K without an absolute similarity-score cutoff.
- Retrieval returns a **model-silent no-op** when: the eligible corpus is empty; all candidates are superseded or already surfaced this session; required provenance cannot be established; or the backend fails.
- **A no-op MUST NOT inject an "explicit nothing relevant" message.**

**Irrelevant-injection harm — measured, not assumed benign:**

Top-K may still be irrelevant because V1 evidence disqualifies score-based abstention. **That risk is accepted only for the PoC and MUST be measured using negative and topically-adjacent queries.** The ship gate MUST compare unconditional top-K injection against a no-injection control for:
- Instruction-following (blocking regression per PMB-1 Track B)
- Task correctness
- Token cost
- False-trigger burden (how often does the model surface irrelevant recalls to the operator?)

**If irrelevant injection causes a blocking PMB-1 regression, the system does not ship; the ADR is re-evaluated rather than introducing an unvalidated threshold.**

**Stale-decision protection:** ORG-058's supersession-lineage envelope IS the staleness signal. A superseded decision never injects as current — the retrieval filter reads supersession status before returning.

### 4. Harness integration — typed `HookInjectionProvider` contract

- **Harness-neutral retrieval contract:** `HookInjectionProvider.retrieve(request: InjectionRequest) → InjectionResult` (new abstract class in `tools/harness_adapter/`).
- **`InjectionRequest` MUST carry at minimum:** prompt text, stable session identifier, harness identifier, hook event name, working directory/repository identity, persona or role-domain, and an extensible native-metadata mapping. Prompt text and session identity are REQUIRED; missing required fields produce a typed no-op/failure result rather than an ambiguous empty string.
- **`InjectionResult` MUST distinguish:** `inject` (carries markdown content plus structured source/provenance records), `no_op` (carries a reason but emits no model-visible "nothing found" message per §3), and `backend_failure`.
- **Harness bindings are separate native encoders.** Each binding parses native hook stdin, constructs `InjectionRequest`, and converts `InjectionResult` into the harness-native response shape. **The provider does not pretend native hook envelopes are identical.** ONE uniform provider implementation serves both harnesses; ONE native hook binding/encoder is REQUIRED per supported harness. Corpus-specific parsing, indexing, chunking, and evaluation belong in configured corpus adapters, NOT separate per-corpus provider implementations.
- **The build MUST verify, for both harnesses:** native stdin fields, `additionalContext`/equivalent output shape, stdout/stderr rules, timeout behavior + actual timeout value, exit-code semantics, fail-open behavior, hook trust/allowlisting, session-ID stability, and whether multiple `UserPromptSubmit` hooks compose or overwrite.
- **`HarnessCapabilities`/`HookCapability` proves only catalog-level availability.** Its current fields do not prove the runtime injection contract above.
- **Deployment:** provider runs as the hook command; retrieval backend runs on GPU VM 210 as a small HTTP service or via a persistent local socket (choice deferred to build-time; both are proven at V1 scale).
- **Wiring:** installed via the existing manifest-driven persona-installer (per ORG-049 install mechanism); adding the hook is additive, does not touch existing controller logic.

### 5. Trigger-description-frontmatter (Skills-pattern-aligned)

- Each ADR / designated note gets a `description` frontmatter field. **`description` is the preferred trigger representation and a separately weighted indexed field; for legacy ADRs, body/title indexing supplies the fallback trigger representation.**
- Ledger entries already carry structured `action` + `summary` fields; those serve as the equivalent trigger surface without a schema change.
- **Rollout: additive.** Existing ADRs without a `description` field participate via body-index (BM25 + vector over their content); adding a `description` field only sharpens their trigger-match precision. Not a mandatory backfill.

### 6. Coexistence with existing recall

Concordance is **additive** to the existing recall hook `tools/hooks/recall-feedback.py` (a narrow PreToolUse footgun-guard that surfaces `feedback`/`project` memory pins on exact-token matches — file paths, `T-NNNN`, `ORG-NNN`, basenames — against mutating tools like Edit/Write/Bash). That hook retains ownership of exact-token memory-pin recall; Concordance owns decision-corpus turn-intent recall. **Neither corpus is migrated into the other.**

The PoC MUST test combined behavior with the existing hook active:
- Hook ordering (which fires first? does either block or supplement the other?)
- Cumulative token cost (dual-injection budget)
- Dedupe boundaries (if the same underlying content ends up in both surfaces)
- Output composition (do the two injection blocks compose sensibly to the model?)

The PoC MUST NOT disable or replace existing recall as an implementation shortcut.

## Anchor architectures (recipe territory, cite the recipes)

Concordance sits in a mostly-solved industry area. **Design work anchors around these known patterns rather than re-deriving them.** Build-ticket briefs cite the specific anchor; deviations require D6 flag + justification.

- **Conditional / contextual injection family** — Anthropic Skills, Cursor `.mdc` rules, Windsurf trigger fields, GitHub Copilot `applyTo` instruction attachment. These share the general shape of "surface additional context conditionally at model interaction" but use different trigger mechanisms (model-selected, glob-selected, always-on, manual, path-scoped attachment). **None establishes the complete external-retrieval → ranking → hook-injection pattern that Concordance implements.** Concordance's specific mechanism — prompt-time external retrieval over a hybrid index, ranked results injected via a lifecycle hook — is a NARROWER member of this broader family, and the recipe status applies to the family-level ideas (conditional injection, description-as-trigger, progressive disclosure), not to any single vendor's implementation.
- **Hybrid dense + sparse retrieval with RRF fusion** (BM25 + vector, RRF k=60) — standard IR technique; V1 stack (T-2106) is the concrete instance we ride.
- **Trigger-description-as-frontmatter** (Skills-pattern-aligned) — Anthropic Skills / Cursor `.mdc` frontmatter / Windsurf's description field all use description-as-trigger in different scoping contexts. §5's `description` field applies the pattern to Concordance's retrieval index.
- **Provenance-carrying RAG** (each result carries source path + git-sha + ORG-058 envelope) — enterprise-RAG standard.
- **Chunk-to-document max-pooling** — a **common aggregation baseline that requires per-corpus comparison**, not a settled default. §1 already obligates the PoC to compare it against corpus-appropriate alternatives (cross-reference-heavy ADRs can defeat max-pool).

**Sources for the family framing:** [Anthropic Skills docs](https://code.claude.com/docs/en/slash-commands), [Cursor rules docs](https://docs.cursor.com/context/rules), [GitHub Copilot instructions docs](https://docs.github.com/en/copilot/concepts/prompting/response-customization?tool=visualstudio). Each vendor's mechanism differs in scoping + trigger + delivery; anchoring on the family, not on any one implementation, keeps the design honest.

## Eval methodology

Adapt T-2107's protocol design (Specialist, `knowledge/episodic/2026-07-28-search-eval-protocol/report.md`) to the decision-corpus:

- **Two-tier pool** (T-2107 §5): deep queries for reranker discrimination + shallow queries for statistical power + random probe for pool-bias estimate.
- **Paired comparisons on identical queries** mandatory (T-2107 §3 statistical-power finding).
- **Fuse + dedupe to document-level** before applying document-level labels (T-2106 §4 chunk-level artifact).
- **Decision-corpus queries derived from real personas' recent turn-intent** (not synthetic) — proves decision-recall precision on actual work-in-progress shapes.
- **Graded 0-3 relevance** per T-2107; no absolute-threshold gating (§3 hard constraint).

**Minimum ratified evaluation:** at least **50 paired real-turn-intent queries**, including at least **15 positive/deep queries, 20 shallow queries, and 15 negative or topically-adjacent probes**; at least **800 graded document judgments** unless a preregistered pooling calculation demonstrates that fewer judgments retain equivalent comparison power. Use at least **two independent labelers for the deep and negative subsets, adjudicating material disagreement**. The build plan may increase these numbers but may not reduce them without reviewer-approved rationale recorded before results are observed.

## Post-PoC follow-ups (deferred content — separately scoped ADRs)

The following work is intentionally outside this ADR's scope and lives in separate follow-up ADR-authoring tickets, each gated on this ADR's PoC passing PMB-1. Each follow-up ADR gets its own scoping ticket, design pass, Consultant ORG-041 review, and operator-ratify.

- **T-2998** — **Concordance push-at-write / retrieval-on-write ADR.** The genuinely-novel non-recipe piece. On commit of an ADR/decision, a hook propagates a pointer into affected personas' surfaces. **Primary design risk = inflation:** the mechanism must model expected volume, per-persona pending-pointer budget, back-pressure caps + digest consolidation, supersession-based dedupe (not commit-sha), authoritative post-receive trigger (not local git hook), and MUST prove non-inflationary at scale before proposing implementation. Gated on T-2816 (PoC) passing PMB-1.
- **T-2999** — **Concordance/memex instance ADR.** Memex-corpus (the operator's `~/notes/` Obsidian vault) as the second scope-qualified instance of the Concordance substrate. Requires per-corpus adaptation obligations validated separately; deployment-shape decision (shared multi-corpus service vs isolated per-instance) MUST be made explicitly with security + operations analysis, not as a build-time preference. Gated on T-2816 (PoC) passing PMB-1 + the operator's separate go.
- **T-3000** — **Concordance post-PoC extensions bundle ADR** (moves 3-6): compaction detail-loss mitigation (ACE-aligned, T-2533-converging), context-editing / stale-injection eviction, modular context / progressive disclosure, provenance + freshness (GraphRAG-style if measured multi-hop need). One bundle-ADR or split into per-move ADRs at drafting time based on design surface. Each move gated on measured evidence favoring it, not on this ADR's ratify.

**None of these ship with this ADR's PoC.** Referenced by ID above for discoverability; content lives in the follow-up ADRs when authored.

## Consequences

- **Closes the operator-observed failure at its mechanism:** relevant decisions surface at point-of-need without the agent knowing to look.
- **Additive + reversible:** every piece is a hook/file/manifest change over existing git-durable substrate; nothing rewrites the storage model.
- **No parallel infrastructure:** rides V1 stack (T-2106) + PMB-1 gate (ORG-062) + policy-outflow (ORG-012) + memory envelope (ORG-058).
- **Cost:** build + maintain a hybrid index on GPU VM 210; author + review `HookInjectionProvider` provider + first harness native encoder; PMB-1 evaluation labeling (≥800 judgments, ≥2 labelers on deep+negative subsets).
- **Priority-stack alignment (the operator TG 8027):** this ADR is the priority-3 mechanism (excellent information traversal) — organizationally sequenced atop stable priority-1 (harness substrate) and stable priority-2 (ticket-system flow). This is layering discipline, not a technical dependency: the PoC's actual technical preconditions are GPU VM 210 network + retrieval-service deployment path + hook installation path.

## Build sequence

1. **Decision-recall-injection PoC** — highest leverage. **Build ticket: T-2816** (existing scoping ticket, PMB-1-gated per ORG-062). Concrete build steps: (a) index decision-corpus on GPU VM 210 using the V1 stack with the corpus-adaptation obligations of §1; (b) implement `HookInjectionProvider` abstract + first harness native encoder with typed contract per §4; (c) wire `UserPromptSubmit` on test-bench first (mirrors T-2953 test-bench-first discipline); (d) eval per §Eval methodology with the ratified minimums.
2. **PMB-1 gate (ORG-062):** no ship without **Track A** (operational/retrieval conformance), **Track B** (blocking capability-regression), **Track C** (governance-envelope conformance, blocking for authority-bearing roles), and **Track E** (workload-realism) at minimum. Track E MUST include irrelevant-injection/no-injection controls and live hook-path latency. Track D applies where ORG-062's adversarial criteria are triggered. ORG-062 owns benchmark definitions; this ADR owns mapping the decision-recall workload and acceptance evidence into those tracks.
3. **Post-PoC:** if PMB-1 passes, the follow-up ADRs (T-2998 push-at-write, T-2999 memex-instance, T-3000 extensions bundle) begin their design passes. None ships automatically; each gets a scoped design + review + ratify cycle.

## Precondition (not the same as priority-alignment)

The PoC depends on verified access to GPU VM 210, a supervised retrieval-service deployment path, corpus/index refresh mechanics, and a test-bench hook-installation path. It reuses the manifest-driven harness configuration/controller surfaces where available, but T-2953 controller completion is not itself a hard prerequisite unless the scoped build demonstrates a concrete dependency. Production fleet rollout remains downstream of stable harness lifecycle management (that IS the priority-1-atop dependency, applied at deploy time, not at PoC-build time).

## Relationship to the unified memory/knowledge program (NOT a silo)

- **ORG-058** (spine) — memory classes + envelope; this realizes its purpose-bounded retrieval application. No change to the model.
- **ORG-062** (substrate + eval) — this decision-recall index rides ORG-062's substrate build + is PMB-1-gated (Tracks A/B/C/E per §Build sequence). Does NOT stand up a parallel index.
- **ORG-061 §1** (dev sequence — harness bit) — this layer sits atop stable harness substrate at PRODUCTION-rollout time; the operator's priority-stack TG 8027 makes this explicit as organizational sequencing, distinct from the PoC's actual technical preconditions.
- **ORG-065** (first-class dependency/lineage graph, base built 2026-08 per T-2673 + BT1-5 + T-3328) — supplies the provenance layer if T-3000's move-6 activates. "Why was this retrieved" becomes a typed-edge query, not a rebuild.
- **ORG-031** (harness-abstraction partition-and-document) — `HookInjectionProvider` provider vs native-encoder split follows ORG-031's "generalized surface + native binding encoders" pattern.
- **`eval-set-t2107`** (retrieval eval infra) — reuse methodology (queries/pooling/judgments) for decision-corpus. No parallel eval harness.
- **ORG-017 / ORG-035** — knowledge-tree unification + memory portability are foundations the corpus + index sit on.
- **T-2425 evidence** — measured that storing *irrelevant* memory costs **22pts of instruction-following**: hard argument for precise, purpose-bounded retrieval (this ADR) over dumping context, AND direct grounding for §3's irrelevant-injection harm-measurement obligation.
- **T-2106 / T-2113** — the V1 build + GPU benchmark that settled the substrate STACK. This ADR consumes those results as inputs for stack selection, does not re-derive, and requires the PoC to validate applicability to the decision-corpus (§1 adaptation obligations).
- **Sibling ADR ORG-074** — self-improvement / optimization (GEPA + ACE) is the *learning* facet; this ADR is the *retrieval* facet. Both spine on ORG-058.

No change to ORG-058's memory classes or envelope. This is the retrieval/injection track of ORG-062's build sequence, decision-corpus-scoped, PoC-first, PMB-1-gated (A/B/C/E), harness-controller-integrated, typed-contract-carrying, follow-ups-scoped-separately.
