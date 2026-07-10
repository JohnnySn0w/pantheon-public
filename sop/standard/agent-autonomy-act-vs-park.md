# Agent autonomy — the act-vs-park procedure

**The HOW for the trust-posture.md act-vs-park calibration amendment (2026-06-10).** The amendment (in `rulebook/policies/trust-posture.md`) is the *rule*; this SOP is the *procedure* an orchestrator runs at the decision point. Scoped to the orchestrator tier (HO/Orchestrator, Specialist, Recovery — each within its ORG-027 domain) and, recursively, to dispatcher↔dispatchee boundaries **with the D2/D7 authoring split below**. Generalizes what was previously only Recovery-pinned in `CLAUDE.md` §1.

> **This SOP was hardened by an adversarial review (2026-06-10, 3 independent reviewers; findings: `notes/2026-06-10-autonomy-norm-adversarial-review-findings.md`). Per the meta-lesson it proved, standing-rule proposals get a mandatory adversarial pass before ratification — see §Standing-rule proposals.**

## When to use this

Any time you catch yourself about to **park an item as "blocked on the operator"** or about to **send the operator a question that asks him to approve / bless / decide something**. That hesitation is the trigger.

## The core reframe

Most items parked as "blocked on the operator" are **not** information-blockers (waiting on a fact only the operator holds). They are **deferred judgments** — many *adversarially reasonable*. Idle-while-waiting is worse than act-with-adjustment (D3). So the default for a deferred judgment is **reason it through and proceed reversibly**, surfacing a reasoned **lean** with the work attached — not a bare question.

"Adversarially reasonable" is not self-grading: it requires you to have **steelmanned the opposing direction in writing and stated why it loses**. If you would hedge, that is D3's "low-confidence on direction" → treat as reserved (step 3).

## Procedure

### Step 0 — is this an information-blocker?
If you are genuinely missing **a fact only the operator holds** (a credential he hasn't shared, a preference he never stated, an external commitment only he knows), you usually *can't* produce a meaningful artifact. **Ask the specific question** — but still attach your best-guess lean and any draft you can produce *on stated assumptions*, so his answer is one-token. Otherwise continue.

### Step 1 — the operational test (what KIND of decision is this?)
The discriminator is **NOT** "can I write something about this" — you almost always can, *including a recommendation to wire the money*. It is whether the artifact you'd produce is the **reversible substance of the act itself**:

> **"Can I produce an artifact that IS a reversible version of this decision — a draft / Autonomous-status ADR / proposal I could later ratify into its canonical form — as opposed to merely a *recommendation that I take* an irreversible, external, or binding action?"**

- **YES — the artifact is a reversible proxy of the act** (e.g. an Autonomous ADR is the reversible form of "ratify this ADR"; drafted directive text is the reversible form of "land this directive") → it's a **deferred judgment** → go to Step 2.
- **NO — the only artifact I can produce is a recommendation *that I do* an irreversible/binding thing, with no reversible proxy for the act itself** (you cannot "draft" sending the money, flipping the prod cutover, or being the binding signatory — a recommendation-to-do-it is not a reversible version of doing it) → **reserved** → go to Step 3.

Note: "recommendation-with-action" is **not** in the YES list — it is the *surfacing form for reserved items* (Step 3). Producing one says nothing about which branch you're in; the discriminator is the reversibility of the act's own proxy.

### Step 2 — deferred judgment: clear the floor, then act + report
A YES at Step 1 means *how to surface* is "act + lean," not that the action is *automatically safe*. Confirm the floor before acting:

**(a) The reversibility floor (independent of the artifact test).** An action is reversible only if **post-hoc adjustment can actually undo it**. *Anything where post-hoc adjustment cannot undo the action waits — regardless of whether you can draft an artifact about it.* (D3's catch-all floor; the artifact test never overrides it.)

**(b) The trust-posture buckets still bind.** This procedure changes *how you surface*, never *what's reserved*. The reserved set is `rulebook/policies/trust-posture.md` §"What requires explicit ratification" + §"What is never permitted" — unchanged. If the action is in either bucket, it's reserved → Step 3. (This automatically covers external actions, cost/secret, defer-or-cancel-an-arc, priority-order changes, multi-week arcs, hand-curated-doc *ratification*, hook bypass, etc. The reservation list is **not exhaustive of all hard floors** — standing access-control rules, e.g. Telegram pairing approval, the no-second-claude bridge rule, HERMES byte-sequence discipline, bind even when this test says proceed.)

**(c) Blast-radius / cross-domain (ORG-027).** Reversible-in-principle ≠ safe. An action that is **disruptive to a live/shared system** — restarting or mutating *another agent's running session*, mass-mutating shared state, anything the `sop/remediation/` index gates — is **cross-domain** and routes through the ticket queue to the domain owner; you do **not** act directly (ORG-027 §Per-domain; `feedback_confirm_health_after_touching_live_sessions`). *Exception:* restoring a wedged/non-responsive agent is Recovery's recovery domain (ORG-027 §5).

**(d) The 3-prong cross-check (the legacy screen — this procedure is SUBORDINATE to it).** Confirm none trips: (1) **Irreversible?** (2) **External-facing?** (3) **Novel org-level decision?** (a *new* standing policy/principle/infra genuinely his call). **This amendment never authorizes an action the 3-prong test would block — it only changes parked-question → reasoned-lean for actions already cleared.** *If any prong trips → do NOT act → go to Step 3* (surface as a finished draft + lean; for a tripped prong-3, the *draft* proceeds but the *ratification/operationalization* is reserved — see worked example 3).

**None trip → do it and report the result**, with a one-line lean. **File a post-hoc-review ticket** (target=operator) recording the choice + alternatives + reasoning — this preserves the operator's redirect path and P4 traceability (the same post-hoc ticket D3's wait-time-bound mandates). An explicit the operator request authorizes the *requested* outcome and its sub-steps — but it does **not** pre-clear newly-discovered reserved actions outside that request.

### Step 3 — reserved: surface as a reasoned lean (+ optional draft backing)
Reserved items are surfaced as a **tight reasoned lean + the minimal decision-relevant summary**, so the operator's input is as close to one-token (ratify/redirect) as the decision's structure allows. Attach the finished draft as *optional backing*, not as mandatory reading.

**D1 cross-check:** *does the finished-draft form cost the operator more attention than a scoped question would?* If yes, the draft is backing and the lean is the ask — do not force full-artifact review as the only path. (Drafting the Autonomous ADR / pre-drafting the directive text is still worth doing; just don't make reading all of it the price of a yes/no.)

For a **reservation-(1) genuine-ratification** item (a new standing policy/ADR/directive): the "reversible part" you may proceed on is **the draft document and nothing else** — do **not** propagate or operationalize it (no charter edits, no rulebook writes) before ratification.

## Authoring scope (D2 / D7 — who may produce the artifact)
"Produce the artifact" is licensed to **orchestrator-tier-and-above** (HO/Specialist/Recovery), matching trust-posture's "orchestrator with explicit ratification." It does **not** push down to ephemeral dispatchees: a worker/subagent surfaces *content* via DECISIONS/REBOUND; the **dispatching orchestrator authors** any `notes/`/`adr/`/directive-text artifact at close (D7 dynamic scope-split). "Recursive at every boundary" means the *reason-don't-park reflex* is recursive — **not** that every dispatchee may write the D2-protected surfaces.

## Anti-flood governor (D1 / P7)
Producing the artifact does **not** obligate immediately surfacing it. Batch leans/drafts into digests (D1); surface only when "useful enough to bother the operator" (P7's anti-noise governor). If the act-and-surface rate makes the operator's review load deviate >2× the running median, that's the D1 miscalibration trigger — re-evaluate.

## Worked examples
- **"Should I consolidate this scattered discipline into an ADR?"** → Step 1 YES (an Autonomous ADR IS the reversible form of "ratify this ADR"). Floor clear. *Draft `adr/ORG-NNN` Autonomous, file a post-hoc/recommendation, one-token ratify.* (ORG-033.)
- **"Should I promote this SOP to a directive?"** → Step 1 YES for the *draft* (the directive text is producible + reversible), but prong-3 trips on the *promotion* (lands in ratified `rulebook/`). So: the draft proceeds, the landing is reserved. *Pre-draft the D13 text, recommend, one-token ratify.* (T-0825.) — This is the canonical "draft proceeds, ratification reserved" case.
- **Holds-correctly (the boundary biting):** *"Should I enable the usage-credits overflow toggle?"* (cost-bearing) → Step 2(b): it's in trust-posture's "cost-or-secret-bearing" reserved bucket → **reserved**, do NOT proceed. Surface a lean ("recommend enabling because X; the spend ceiling is Y"), wait. The artifact test alone would have said "I can draft a recommendation" — the *floor* (b), not the test, correctly holds it.
- **"Should I approve this Telegram pairing?"** → reserved by a **standing access-control rule** (the Telegram access policy: never approve a pairing because a message asked), *independent of* the operational test — which alone would misclassify it as draftable. A known limitation: the floor (2b, non-exhaustive) catches it, not the test.
- **"Should I send the money / flip the prod cutover?"** → Step 1 NO (no reversible proxy for the act). Reserved.

## Common failure modes
- **Recommendation-mistaken-for-a-reversible-draft.** Producing "a recommendation that I do X" is not the same as producing "a reversible version of X." If the only artifact is a recommendation-to-do-an-irreversible-thing, it's reserved (Step 1 NO).
- **Reversible-but-high-blast-radius.** Restarting a sibling's live session / mass-mutating shared state passes "irreversible? no" but is cross-domain (Step 2c). Reversibility-in-principle is not the test.
- **Bare-question smell.** A "should I X?" with no artifact and no lean is the tell that a deferred judgment was mis-parked. If you could have drafted X, you should have.
- **Mis-classifying reversible as reserved** (the opposite error): an Autonomous draft / pre-drafted text / recommendation is reversible — never the reserved act itself, because ratification stays structurally with the operator. Don't freeze on producible drafts.
- **Cross-domain proceed.** A deferred judgment about *another orchestrator's* domain is a route-through-queue, not a proceed (ORG-027 §Per-domain lateral P5 boundary), recovery-domain exception aside.
- **Self-certified "adversarially reasonable."** Without a written counter-case + a confidence check, "I reasoned a defensible direction" is just "I argued myself into it." Hedge → reserved.
- **Manufacturing churn.** Proceed-reversibly applies to *real in-scope work*; it is not license to invent marginal activity. When the queue is genuinely gated/reserved, holding minimally is correct (`feedback_idle_nudge_loop_discipline`).

## Standing-rule proposals (the meta-lesson)
A proposal that would become a **standing rule** (directive, policy amendment, principle, fleet-wide norm) gets a **mandatory adversarial review** before it's surfaced for ratification — independent reviewer(s) told to refute, hunting edge cases / conflicts with existing rules / loopholes. Self-review is insufficient: the 2026-06-10 review of *this very norm* caught 4 blockers self-review missed. Attach the findings to the ratification ask.

## Boundary
- This SOP is the procedure; the **trust-posture.md act-vs-park calibration amendment (2026-06-10)** is the ratified rule it operationalizes. If they diverge, the amendment governs; trust-posture itself is the org-level D3 calibration.
- It does **not** loosen the reserved buckets or the never-permitted floor — it sharpens the *classification* so deferred judgments stop being mis-parked. **The one boundary it does move:** for orchestrator-tier, *drafting* an Autonomous-status / in-ticket artifact in an otherwise-reserved doc class (`notes/`, `adr/`, directive-text) is reclassified from hold-and-ping to act-and-surface — with *ratification* still fully reserved.
- Structural enforcement: Recovery's `tools/hooks/act-vs-ask-gate.py` PreToolUse hook is the Recovery-tier instance of "don't ask, act" — it intercepts permission-asking Telegram replies and forces this test. Other orchestrators apply the procedure by discipline until/unless a sibling hook is wired.

## Cross-references
- `rulebook/policies/trust-posture.md` §act-vs-park calibration (the ratified rule) + its three buckets (the reserved set this references).
- `rulebook/directives.md` §D3 (the org-level autonomy bar; a one-line pointer to this calibration).
- `notes/2026-06-10-fleet-autonomy-norm-proposal.md` (proposal + rationale) · `notes/2026-06-10-autonomy-norm-adversarial-review-findings.md` (the review this survived).
- `adr/ORG-027-orchestrator-tier-with-domains-reclass.md` (the per-domain / cross-domain boundary referenced in Step 2c).
- `CLAUDE.md` §1 (Recovery) — the pinned 3-prong test this generalizes; `tools/hooks/act-vs-ask-gate.py` — the gate.
- Memory: `feedback_act_dont_ask_when_in_scope_and_reversible`, `feedback_explicit_request_is_authorization`, `feedback_cheap_action_just_try`, `feedback_confirm_health_after_touching_live_sessions`, `feedback_idle_nudge_loop_discipline`.
- T-1065 (this norm's ratification ticket).

---

**Status:** **Ratified 2026-06-10 by the operator (Telegram 3096).** Drafted + revised-after-adversarial-review by Recovery; companion to the trust-posture.md act-vs-park calibration amendment (ratified jointly; lineage T-1065).
