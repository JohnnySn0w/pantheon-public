# Plan-first dispatch

**Status:** Ratified 2026-05-10 (T-0197 #2 of 4; the operator msg_1220 + msg_1223 critique → e5a34ea amendments → msg_1236 "with the amendments, 196 and 197 should be fine"). Authoritative for operator/HO citation.
**ORG-024 gap amendments Ratified 2026-06-02 (the operator, T-0699):** (T-0647, gap-2) §1b External/library round-up + a §Pre-mortem element in the worker plan (§2) and reviewer pass (§3); (T-0652, gap-7) §1c Six-question pre-dispatch checklist.
**T-2528 evidence-contract amendments — Drafted 2026-08-06 (Orchestrator, from the T-2382 corpus); awaiting the operator ratification.** Adds to the plan a §Evidence map (each load-bearing claim → authoritative source + a falsification), a §Deployment sequence (deploy/restart/rollback ordering + a clean-main smoke baseline recorded before code), and the §3 reviewer distinction between an underspecified-evidence-contract planning gap and irreducible live validation. The operator agreed the lesson belongs in the SOPs (T-2528); this drafts the concrete wording for his sign-off. Live-usable as drafted; the ratification promotes it to precedent.

This is the prescriptive standard for plan-first dispatches — what HO and orchestrators DO at each stage of an implementer-tier or team-pattern cycle to keep the plan-tier gate intact. The remediation counterpart (what to do when an implementer bypasses the gate) lives at [`../remediation/plan-first-violation.md`](../remediation/plan-first-violation.md).

## When to use this

Every implementer-tier dispatch (HO-direct or via project-orchestrator) follows this discipline. No exceptions; the discipline applies to:

- Single-shot implementer dispatches (HO → implementer subagent for a focused change).
- Team-pattern (b9-shape) cycles (HO → Agent-spawn project-orchestrator → implementer + reviewer; implicit agent-team, no TeamCreate).
- Cross-account implementer dispatches via ticket-comms (HO files Recovery-target ticket; Recovery implements per the same discipline locally).
- Worker-only redispatches per `../remediation/plan-first-violation.md` Path B1 (the new implementer inherits the same plan-first contract).

The discipline does NOT apply to:

- Routine read-only queries (Read, grep, ls — no plan needed for inspection).
- One-shot tool calls that don't produce durable artifacts (e.g., a Bash command that prints output).
- HO's own direct edits within the D8 ceiling (3 edits / >2 files / ~50 LoC / no design-decision) — HO's audit chain is the ledger entry, not a separate plan-tier surface.

## Procedure

### 1. Brief construction (HO at dispatch time)

Brief includes verbatim hard-stop language:

> **HARD STOP: emit a plan-tier surface as your FIRST artifact. No code edits before plan APPROVE. Pushing code before plan APPROVE forces the cycle to REBOUND at the dispatching orchestrator (project-orchestrator for team-pattern cycles; HO for HO-direct dispatches). HO is reached only via the 3-REBOUND budget exhaustion per D7, not as default.**

Brief also includes:
- §Locked answers — design calls already ratified by HO/the operator; worker MUST honor literally; deviation requires FLAG + RE-CONFIRM (per D6).
- §Plan template pointer — what shape the plan should take (§Plan + §Tests + §DECISIONS-with-justifies-via).
- §Tests prescription — §Brief-named tests (the orchestrator-prescribed test names that bind plan ↔ code ↔ reviewer artifacts).
- §Triple-defense + §Readback-gate when sticky-belief drift surfaced in prior iterations of this same arc (per D8).

### 1b. External / library round-up (before reviewer dispatch)

The model's knowledge has a training cutoff (Jan 2026); anything that touches external reality (a dependency, an external API, a CLI tool's flags/behavior, a vendor changelog) may have moved since. Before the plan is reviewed, run a brief round-up and bake the findings into the brief's §Locked context so the worker plans against current reality, not stale recall:

- **Grep the touched surface's imports / dependency manifests** for external libs or tools in play.
- **For anything version-sensitive, upgraded, or stale-looking**, check the current pin / release notes (or a quick web/changelog look) — does the API/behavior the plan assumes still hold?
- **Bake findings into the brief** as §Locked context ("rg defaults to recursive + respects .gitignore, unlike grep"; "lib X bumped to 3.x, the 2.x call shape in the plan is gone").

Scope-proportionate: a one-file internal change needs none; a dependency bump, an external-API integration, or a tool-swap needs a real round-up. Worked example (2026-05-29, this SOP's own arc): a ledger-migration review found `rg` differs from `grep` in defaults/regex — a library-behavior gap a round-up surfaces before code, not after.

### 1c. Six-question pre-dispatch checklist (in the brief)

Before dispatch, the orchestrator articulates intent + risk surface by answering these six at the top of the brief (one sentence each) — forcing the framing to exist *before* execution starts, not be reconstructed after a roadblock:

1. **What do I want?** — the concrete target/outcome.
2. **How many ways could I achieve it?** — alternatives considered (so the chosen path is a choice, not the first idea).
3. **What will slow me down?** — known blockers / risk surface (feeds §1b round-up + the worker's §Pre-mortem; don't re-derive there).
4. **How do I prevent this from happening again?** — the structural lock (hook/lint/test) if this closes a recurring failure.
5. **How do I stay on track?** — the checkpoint / verification cadence for the cycle.
6. **What are the 2nd- and 3rd-order effects?** — cross-agent / downstream / standing-infra impact.

Scope-proportionate: a trivial change answers these in a sentence or two inline; a non-trivial or cross-cutting change warrants explicit per-question answers. The six overlap deliberately with §1b (round-up) and §2 §Pre-mortem — this checklist is the *upfront articulation* at brief-time; those are the *deeper passes* at plan/review-time. Don't duplicate; cross-reference.

### 2. Worker emits plan-tier surface

Plan-tier surface contains:

- **§Locked acknowledgment** — worker reads back the locked answers verbatim, demonstrating absorbed-not-merely-cited.
- **§Plan** — design intent + step-by-step approach + alternatives considered. Decisional, not descriptive.
- **§Tests** — names the §Brief-prescribed tests + describes what each verifies.
- **§DECISIONS** — each non-obvious call justifies-via P/D ids + EX/ED ids; see chain-of-justification rule in `~/CLAUDE.md`.
- **§Pre-mortem** — assume the change shipped and FAILED or missed the target: enumerate the top failure modes (what breaks, what edge case is missed, what assumption is wrong) and the mitigation each is designed around. Decisional, not boilerplate — surfaces the risks the plan actively defends against. Scope-proportionate (skip for trivial changes; required for anything non-trivial or touching shared/irreversible state).
- **§Evidence map** *(T-2528, draft)* — for every **load-bearing** claim the plan relies on, map it to (a) its **authoritative source** and (b) the **observation that would falsify it**. Load-bearing = a claim the design's correctness or safety rests on: a billing/cost provenance ("this path bills at $X"), a live-process's source/version provenance ("the running daemon is built from commit Y"), an external API's behavior, a "this is safe because Z". A claim with no named source, or no falsification, is an **assumption, not a fact** — label it as such in the map so the reviewer knows it is unverified rather than silently trusting it. This is the up-front **evidence contract**; its absence is precisely what turns necessary live validation into review churn — the reviewer *discovers* a trust boundary the plan should have *named* (the T-2382 failure shape: two load-bearing boundaries — provider billing provenance and live-process source provenance — surfaced during review rather than in the plan). Scope-proportionate: a one-file internal change with no external/live claims needs none; anything touching billing, provenance, cross-account state, or external reality requires the map.
- **§Deployment sequence** *(T-2528, draft)* — for any change that **deploys, restarts, or touches live state**: specify the **deploy → restart → rollback ordering** explicitly (what gets restarted, in what order, and the exact rollback path if it goes wrong), and record a **clean-main smoke baseline BEFORE implementation** — run the project smoke loop on unmodified `main` and capture the result, so a post-change smoke delta is attributable to the change rather than to a pre-existing failure. The baseline is the control the change is measured against; without it, a red smoke after the change is ambiguous. Scope-proportionate: pure-analysis or non-deploying changes skip it; anything that restarts a service, migrates data, or alters cross-account/production state requires it.

If the brief asked for a §Readback-gate, worker emits the readback section before §Plan. Verification: orchestrator confirms the readback matches the brief's locked answers literally.

**ADR-creation implementation detail is ADVISORY.** Implementation detail carried over from an ADR's creation — the referenced-note reasoning, relocated build values, or any mechanism sketch that survived the altitude gate in the ADR body — is GUIDANCE for the implementer, not a binding spec. The implementer MAY overrule it in the plan if it seems unfit, or if the technical ground has shifted since the ADR was written — ADRs for stages-away surfaces are often authored long before build, and by build time the landscape may have moved. **The ADR's DECISION still binds; only its implementation detail is refreshable.** When overruling, record the divergence and why in §DECISIONS (justifies-via + the ADR-detail-being-overruled cite + why it's stale/unfit), so the author-time reasoning and the build-time deviation are both legible. Companion: `sop/standard/ratifiable-adr-shape.md` (the ADR-side altitude gate) — this section is the implementer-side refreshability rule. Directed by the operator TG 1792 (2026-09-06).

### 3. Orchestrator review (REBOUND or APPROVE)

Orchestrator (HO for HO-direct dispatches; project-orchestrator for team-pattern cycles) reviews the plan against:

- **Coverage cross-check** — does the plan address every §Locked answer + every brief-named test? Missing items = REBOUND.
- **Justifies-via density** — does each non-obvious DECISION have a P/D citation that load-bears? Inverted-justifies-via = REBOUND (per `../remediation/claimed-prior-approval.md`).
- **Locked-answer deviation handling** — if the plan deviates from a §Locked answer, did the worker FLAG + RE-CONFIRM? Implicit deviation = REBOUND.
- **Framing-language scan** — does the plan claim "approved" or "as agreed" for any path? Verify against the actual brief content + REBOUND chain.
- **Pre-mortem adversarial pass** — the reviewer runs an adversarial pre-mortem ON THE DESIGN (not just checking the worker's §Pre-mortem exists): "assume this shipped and failed — what's the most likely way?" Distinct from the coverage cross-check (which tests completeness vs the brief); the pre-mortem tests robustness vs reality. A real, unmitigated high-severity failure mode the plan ignores = REBOUND with the specific mode named. Worked example (2026-05-29): an adversarial pre-mortem on an acceptance-check design caught a regex word-boundary bug that would have auto-reopened legitimately-done tickets — a failure mode a completeness check alone misses.
- **Evidence-contract completeness** *(T-2528, draft)* — every load-bearing claim in the plan (§Evidence map) carries a source + a falsification; unlabeled assumptions are flagged as such. **The key move:** if a review round surfaces a NEW load-bearing trust boundary the evidence map omitted, that is a **planning gap — an underspecified evidence contract** — REBOUND to add it to the map, and it counts as avoidable churn. This is **distinct from irreducible live validation**: a boundary that can only be *confirmed by running live* (a real cross-process handshake, a real billing event, a real restart under the production launch shape) — no amount of up-front planning removes that, and a review round spent on it is a necessary cost, not churn. Naming which one a given round is prevents two opposite errors: mislabeling avoidable planning churn as "just how live validation goes" (so the evidence contract never tightens), and mislabeling irreducible live validation as a planning failure (so the plan is blamed for reality). REBOUND only the first; expect and budget the second.
- **Deployment-sequence presence** *(T-2528, draft)* — for live-touching changes, the §Deployment sequence (deploy/restart/rollback ordering) and the clean-main smoke baseline are present. Absent on a change that restarts a service or alters cross-account/production state = REBOUND.

Outcomes: APPROVE (worker proceeds to code-tier), REBOUND (worker re-emits plan with gap closed; counts against the 3-REBOUND budget), or escalate (3-REBOUND budget exhausted; bubble up per D7).

### 4. Code-tier emission (post-APPROVE)

Worker proceeds to code only after explicit APPROVE in the dispatch chain. Code-tier deliverables:

- Implementation matching the approved plan.
- Tests with §Brief-prescribed names; tests pass.
- Smoke verification per project-specific smoke-loop (see project's `pantheon-config/sop/standard/` or project-scope SOP).
- Commit message includes DECISIONS section reflecting any implementation-emergent decisions made during code-tier work.

### 5. Reviewer pass (READY-FOR-REVIEW)

Reviewer (orchestrator-pre-staged for team-pattern; HO-on-demand for HO-direct) validates:

- Coverage of approved plan + brief-named tests pass.
- DECISIONS-section present + justifies-via clean.
- Smoke-loop output (literal command-tail in fenced block per D5).
- No D2-protected paths touched without explicit ratification trail.

(HERMES byte-sequence enforcement is structural — pre-commit hook + watch_runner guard. No reviewer-tier check needed; the hook catches at commit-time.)

Outcomes: APPROVE (cycle proceeds to close), REBOUND (worker re-emits code with gap closed), or escalate.

### 6. Close

Orchestrator (or HO for HO-direct cycles) writes the close ledger entry with structured metrics field per T-0193 ratified shape (see [`cycle-stats-augmentation.md`](cycle-stats-augmentation.md) when authored) + DECISIONS curation reflecting the cycle's actual decision-set (not just plan-tier decisions; implementation-emergent decisions belong here too).

## Common failure modes

- **Plan-first skip** (worker emits code before plan) — per `../remediation/plan-first-violation.md`. Most common b23-corpus pattern.
- **Description-as-plan** (plan emitted but is descriptive not decisional) — REBOUND with "rewrite plan to surface design intent + alternatives + DECISIONS, not code-walkthrough."
- **Self-approved plan** (worker emits plan + immediately proceeds without orchestrator/HO APPROVE in chain) — REBOUND; treat as plan-first skip variant.
- **Orchestrator green-lights without coverage cross-check** — orchestrator-tier discipline failure; HO catches at READY review per D8 standing pre-commit discipline.
- **Plan APPROVEd but implementation drifts during code-tier** — implementation-emergent DECISIONS not surfaced to orchestrator; close-time DECISIONS curation gap. REBOUND at READY-for-review if reviewer catches; ledger entry flag if HO catches at close.
- **Evidence discovered in review, not planned** *(T-2528, draft)* — a load-bearing trust boundary (billing provenance, live-process source provenance, external-API behavior) surfaces during a review round because the plan's §Evidence map omitted it, forcing extra correct-but-avoidable rounds. The fix is the evidence map up front (§2) — not more review passes. Distinguish from irreducible live validation per §3; only the omitted-from-the-map case is a failure mode. Corpus: T-2382 (two boundaries discovered in review over several rounds).

## Boundary

- **What plan-first DOES NOT cover:** design-tier ratification (which sits at HO ↔ the operator, not implementer-tier); routine inspection / read-only tool calls; HO-direct edits within the D8 ceiling.
- **What plan-first DOES NOT replace:** the brief itself (this SOP is the procedure; the brief is the per-cycle content); the chain-of-justification rule (which propagates through all dispatch boundaries, not just plan-tier).
- **Cross-account dispatches** inherit this discipline — Recovery implementing on a HO-filed ticket follows the same plan-first contract; the inter-agent SOP at [`cross-account-ops-discipline.md`](cross-account-ops-discipline.md) (when authored — T-0197 #3) details how the contract crosses unix-user boundaries.

## Cross-references

- **Principles + directives:** P3 (decisions justify upward — plan-tier surface is where the chain begins); P5 (roles don't hop — implementer emits plan but doesn't APPROVE it); D6 (plan-first protocol — directly codified here); D7 (decision flow execution — chain-of-justification through tiers); D8 (HO self-discipline — pre-approve scans + sticky-belief N=2 trigger).
- **Policies:** `rulebook/policies/trust-posture.md` (auto-approve scope at orchestrator-tier vs HO-tier); `rulebook/policies/autonomy.md`.
- **ADRs:** `architecture/adr/ORG-002` (principles/directives split); `architecture/adr/ORG-010` (three-tier rulebook framework — this SOP sits at the policy-tier under the framework).
- **Sibling SOPs (standard):** [`team-orchestrator-brief-construction.md`](team-orchestrator-brief-construction.md) (brief-construction primitives that this SOP dispatches with); [`b9-team-brief-reference.md`](b9-team-brief-reference.md) (team-pattern reference); [`team-orchestrator-dispatch-checklist.md`](team-orchestrator-dispatch-checklist.md) (pre-dispatch checklist — pending T-0197 #4).
- **Sibling SOPs (remediation):** [`../remediation/plan-first-violation.md`](../remediation/plan-first-violation.md) (the remediation counterpart — what to do when this discipline is violated); [`../remediation/implementer-self-merge-to-main.md`](../remediation/implementer-self-merge-to-main.md); [`../remediation/claimed-prior-approval.md`](../remediation/claimed-prior-approval.md).
- **Tickets:** T-0156 (structural prevention via branch protection — collapses the bypass surface); T-0160 (parent ratification of standard-bucket extension); T-0197 (this ticket — 4 standard-bucket SOPs); T-0193 (cycle stats — close ledger entry shape this SOP produces); T-2528 (evidence-contract amendments — §Evidence map + §Deployment sequence + the planning-churn vs live-validation distinction, from the T-2382 corpus); T-2382 (the corpus that surfaced the gap — trust boundaries discovered in review rather than planned).
- **Lessons:** [`lessons/instruction-fidelity-collapse-discipline-vs-architecture.md`](../../lessons/instruction-fidelity-collapse-discipline-vs-architecture.md) (broader corpus framing — why this discipline needs both prose AND structural backing).
- **Telegram refs:** msg_876 (T-0160 ratification of standard-bucket extension); msg_945 (T-0197 file request via Recovery); msg_1220 (T-0197 #1 ratification — sibling precedent for shape).
