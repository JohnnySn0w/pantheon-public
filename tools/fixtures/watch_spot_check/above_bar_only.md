---
surface: above-bar
headline: Behavioral Contracts II's finding (same-model handoff co-fails on 90% of missions where either fails; different-model/vendor decorrelates it) directly validates Pantheon's existing review-boundary heterogeneity — codex_second_opinion (different vendor) + reviewer→sonnet (different tier) are the exact BC-II mitigation, but Pantheon has never quantified its OWN co-failure rate; a full 18K-mission replication is disproportionate — the actionable move is a lightweight decorrelation-check on the ONE mechanism that most relies on it
---

# Compositional independence vs Pantheon — 2026-08-24

**Ticket:** T-3129 (from watch topic `llm-agent-failure-modes-research`, parent T-3127).
**Ask:** Replicate Behavioral Contracts II methodology on Pantheon's agent compositions; quantify co-failure rates.

**The finding being applied** (Bhardwaj et al., *Agent Behavioral Contracts II*, arXiv:2608.12895v1, 2026-08-13; preregistered, 18,000 missions, deterministic scoring, no LLM judge):
- Two instances of ONE model in two-agent handoff **co-fail on 90.0% of missions where either fails** (log OR 6.66, 95% CI [6.38, 7.00]; φ 0.916). The conditional-independence assumption that lets you multiply component reliabilities is badly violated — joint failure is far above the independence product.
- **Substituting a different MODEL reduces the association in 6/6 contrasts.** Substituting a different *vendor* (model already different) does not further reduce it.
- Implication: **model heterogeneity is the decorrelator.** Two Opus agents reviewing each other are near-useless as an independent check; an Opus→(different-model) check recovers real independence.

---

## Applicability to Pantheon

**Scope honesty first.** A literal replication — 18K preregistered deterministic-scored missions — is a research-harness build Pantheon does not have and that would be disproportionate to the question (D12 resource-mindset cuts *against* it: the finding is already high-confidence at n=18K; re-deriving φ on Pantheon adds little). What transfers is the **structural implication**, and it lands sharply.

**Pantheon's compositions are overwhelmingly same-model handoffs.** All six seats (Orchestrator, Specialist, Recovery, Archivist, Mason, Artisan) run Claude, currently Opus-tier. By BC-II, any Opus→Opus handoff where one agent's output is checked by another Opus instance has a **co-failure floor far above the independence assumption** — if the implementer got it wrong for a model-shaped reason, the same-model reviewer is disproportionately likely to miss it the same way.

**Pantheon already instantiates BC-II's mitigation at the load-bearing boundary — the review gate:**

| BC-II mitigation | Pantheon mechanism | Status |
|---|---|---|
| Different **vendor** (strongest decorrelator) | `codex_second_opinion` (ORG-018, T-0426; promoted to standing HO pre-merge detail-check SOP T-1201 / `codex-second-opinion-detail-check.md`) — a **non-Claude** model as second-opinion reviewer | **LIVE — this is exactly the BC-II decorrelator** |
| Different **model/tier** | reviewer→sonnet while implementer is opus (T-0006/T-0014) + per-tier assignment opus-plan/sonnet-execute (ORG-016/018) | **LIVE (partial — see below)** |

**So the headline is not a gap — it's a validation.** Pantheon's two most important cross-check mechanisms (codex second-opinion; sonnet-reviewer-of-opus-implementer) are *precisely* the heterogeneity BC-II says you need. The DECISIONS-with-justifies-via chain and the reviewer-runs-smoke-directly discipline add a further decorrelation axis BC-II doesn't even model: the reviewer re-derives from *source* (runs the smoke, reads the spec) rather than from the implementer's *claim*, which breaks the shared-context path that drives same-model co-failure.

**BC-II gives these mechanisms a NEW justification axis.** They were adopted for cost (per-tier) and detail-catching (codex). BC-II says they are *also* the independence-recovery mechanism — which strengthens the case for operationalizing per-tier assignment (the T-0710 §3.1 open item) and for keeping codex_second_opinion healthy (it had reliability wobbles: T-1775 unresponsive, T-2143 orphan-child). A decorrelator that's silently down is worse than one you know is down.

---

## Where the residual actually is

The exposure is **same-model self-review WITHOUT the codex/tier heterogeneity** — i.e. any place a decision is checked only by another Opus instance sharing the same context:
- Specialist's own watch-report evals (like this one) are single-Opus with no second-model check. Acceptable — these are below-bar research notes, not merge-gated code; the cost of a co-failed research note is a later correction, not a shipped defect.
- The genuinely load-bearing case is **merge-gated implementation**, and there the codex_second_opinion SOP already interposes a different vendor. So the residual is bounded to non-merge-gated same-model handoffs, which are low-stakes by construction.

**Actionable (lightweight, in-proportion):** rather than replicate BC-II, the proportionate move is a **liveness/coverage check on codex_second_opinion** — confirm the different-vendor decorrelator is actually invoked on merge-gated work and is healthy (given T-1775/T-2143 history). That's an HO-domain operational check, not an Specialist research build. Surfacing as a lean to orchestrator, NOT filing a replication ticket.

---

## Verdict

- **Do NOT replicate BC-II** (18K-mission harness is disproportionate; finding is already high-confidence). D12 cuts against re-deriving a settled φ.
- **BC-II validates Pantheon's review-boundary heterogeneity**: codex_second_opinion (different vendor) + reviewer→sonnet (different tier) + reviewer-re-derives-from-source are exactly the decorrelation the finding prescribes. This is a confirmation, aligned with the T-0710/T-3128 through-line.
- **New justification axis**: BC-II re-motivates (a) operationalizing per-tier model assignment (T-0710 §3.1 standing item) and (b) codex_second_opinion liveness (T-1775/T-2143 history) — a decorrelator that's down silently defeats the purpose.
- **Bounded residual**: same-model self-review only bites where there's no codex/tier heterogeneity, which is non-merge-gated low-stakes handoffs by construction. Specialist's own single-Opus research notes are in this acceptable set.
- **Routed, not self-filed**: codex-liveness-on-merge-gated-work coverage check → HO-domain lean (below). No Specialist replication artifact beyond this note.

**Justifies-via:** P1 (heterogeneity-as-structural-decorrelator), P5 (the review-boundary is where roles-don't-hop meets independence), D5 (reviewer-runs-smoke-from-source = the extra decorrelation axis), D12 (in-proportion: no disproportionate replication), ORG-016/018 (per-tier + codex the load-bearing mechanisms).

## HO-domain lean (not a filed ticket — surfaced for orchestrator)
Consider a coverage/liveness check that codex_second_opinion (the different-vendor BC-II decorrelator) is actually invoked on merge-gated implementer work and is healthy — given its T-1775 unresponsive + T-2143 orphan-child history, a silently-down decorrelator collapses the independence it's there to provide. Cheap operational check, HO-domain. If orchestrator wants it as a ticket, this note is the justification.

## Cross-references
- arXiv:2608.12895 (Behavioral Contracts II) — the finding
- T-0426 / ORG-018 — codex_second_opinion (the different-vendor decorrelator)
- T-1201 / `sop/standard/codex-second-opinion-detail-check.md` — standing pre-merge SOP
- T-1775 / T-2143 — codex_second_opinion reliability history (why liveness matters)
- T-0006 / T-0014 — reviewer→sonnet (the different-tier decorrelator)
- ORG-016 / ORG-018 + T-0710 §3.1 — per-tier model assignment (the standing operationalization item)
- T-3128 — MAST eval (sibling; this is the compositional-dependence mode from the same report)
- T-0710 — self-built-substrate-exceeds through-line
