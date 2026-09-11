# SOP — Shaping and viability pass

**Ratified 2026-09-06 by the operator via Telegram 9047** as part of the ORG-089 landing bundle. Emanates from [[ORG-089]] (deference heuristic + appetite circuit breaker); the how-to companion the drafter opens when the pass fires. Cairn's ORG-041 planning-phase review checklist points at this SOP.

## When it fires

Every non-vault ADR (agent-authored, i.e. ORG-NNN + ADR-NNN 200+) carries the pass in RECORD-mode unconditionally — populated `appetite:` frontmatter integer + §Appetite body section + §Riskiest assumption body section. Runs at draft-time; results land on the artifact regardless of whether they surface to the operator.

**SURFACE-mode** additionally fires when the ADR transcribes a directive touching the D3 reserved / goal-level set (org shape, principles, standing roles, irreversible external / real-world / identity acts, money, access). In that case, surface any pass-result that would materially change the decision to the operator as a reasoned lean, before transcribing.

## The three viability questions (RECORD content)

1. **Riskiest assumption + cheap test.** What is the one unknown whose failure would kill the bet? What is the cheapest way to probe it before commit? A named riskiest assumption + a stated cheap-test-shape is the floor against under-viability.

2. **Appetite / is-this-worth-it-now.** What depth and mechanism is warranted right now, measured against outcome value + strategic alignment + reversibility per the 3-sub-score 1-10 rubric at `knowledge/episodic/2026-09-05-deference-heuristic-proposal/appetite-scale.md`. Average rounds to the appetite integer. An explicit ceiling — "not worth X, halt if scope grows to Y" — is the ceiling against over-specification.

3. **Strongest genuine alternative + why-not.** The best other option a serious adversarial reader would raise, and the specific reason not to take it. If you can't name a real alternative, the design isn't shaped yet — go back to the shaping-and-viability pass, not the transcription.

## The material-change filter (SURFACE-mode gating)

Any pass-result whose surfacing would materially change the outcome gets surfaced as a reasoned lean. Reasoned lean = concrete alternative + rationale + your recommendation, not a bare question, not ritual contrarianism. Pass-results that don't change the outcome stay on the artifact record but do NOT surface to the operator. This suppresses ritual-contrarianism drift while preserving the record for lookback.

## Field shape (what lands on the artifact)

**Frontmatter:**
```yaml
appetite: <int 1-10>
```

**Body sections (both required, positioned near the top after §Status/§Related):**

```markdown
## Appetite

**Score: N/10** (value X / strategic Y / reversibility Z, average → N). <One-paragraph reasoning: what this is worth now, cost/benefit summary>.

Explicit ceiling: <what triggers an appetite-overrun halt>. **Not-in-scope:** <bounds honestly named>.

## Riskiest assumption

**<The assumption named in one sentence>.** Cheap test / mitigation: <how it gets probed cheaply>. Re-evaluate at <trigger>: if <observation>, <re-evaluate action>.
```

See [[ORG-089]] itself as the worked example — it dogfoods both fields.

## Anti-patterns

- **Ritual contrarianism.** Surfacing every pass-result regardless of material-change. This is the failure mode ORG-089's §Riskiest assumption names; the material-change filter exists to prevent it. If in doubt: would surfacing this result change the outcome? If no, stay on the artifact record; do not surface.
- **Boilerplate fields.** "TODO", "placeholder", empty. Grooming (ORG-089 §Surface 4a-completeness) flags these on ratified ADRs; a boilerplate field is decorative and the pass didn't run.
- **Appetite score without reasoning.** The number without the value/strategic/reversibility sub-scores + the reasoning paragraph is not the pass, it's an assertion. Reviewers will REBOUND on this.
- **Riskiest assumption without a cheap test.** Named-but-unprobeable isn't a viability check, it's a hedge.
- **Under-scored appetite to avoid the ceiling.** Scoring 3/10 to keep the artifact "small" when the real scope is 7/10 defeats the ceiling function. Score against the real scope; if the score is too high for what you want to build, shape the scope down honestly.

## Composition with existing surfaces

- **Trust-posture §Act-vs-park** carries the SURFACE-mode trigger rule (this SOP amends into the policy at ORG-089 landing).
- **ORG-041 §Amendment 2026-09-06** carries Cairn's review checklist for the RECORD fields (populated + not boilerplate + coherent with the ADR's actual claims).
- **ORG-021 §Substates** carries the ticket-tier field shape (`appetite:` + `bet_ref:` + `substate: appetite-overrun` + `substate: dropped`).
- **`tools/ticket_grooming.py`** carries the two grooming checks (completeness + overrun); overrun is calibration-gated per ORG-089 §Decision.4d.

## Re-evaluate when

- 90 days post-landing: if the pass has added digest volume without changing outcomes on ≥3 material-change surfaces, revisit trigger or retire per [[ORG-089]] §Riskiest assumption mitigation.
- Post-T-3430 calibration (~2026-12-05 + ≥20-bet-outcome precondition): the appetite→time-budget mapping activates the overrun grooming check; observe for false-fire noise on legitimate overruns vs actionable flags.
- Any operator-flagged instance of "the pass turned into ritual contrarianism" — immediate re-eval regardless of calendar cadence.
