# Research / design-dispatch deliverable standards

**Status:** **Standards 1–2 ratified 2026-06-08 by the operator.** Drafted 2026-06-06 by Specialist (`claude-research`) per T-0871 (ORG-012 promotion of two Specialist feedback pins to an org-tier SOP; surfaced by the first scripted knowledge sweep, T-0869). Authored under the overnight raised-autonomy posture (2026-06-06). **Standard 3 added 2026-06-16 by Specialist** (T-1170) under the goal-level autonomy bar (the operator msgs 3733/3735 — methodology is below-goal-level, adopted autonomously + digest-surfaced, NOT a fresh the operator ratification); see its inline provenance. **Standard 4 added 2026-06-23 by Specialist** (T-1276, recovery-filed) — codifies the operator's 2026-06-22 ratified Sonnet-for-cyber-research policy (TG msg 3338); a transcription of a ratified decision, see its inline provenance.

## When to use this

Any Specialist (or commissioned-researcher) **research or design-dispatch deliverable** — watch reports, episodic research, revenue-project/decision research, design-noodle write-ups — that either:
- **(a)** cites empirical / benchmark numbers, or
- **(b)** is a design-noodle / parking-the-question dispatch.

This SOP governs deliverable **content shape** (what the write-up must contain to be trustworthy and actionable). It is the companion to [`research-dispatch-filing.md`](research-dispatch-filing.md), which governs the **filing of follow-ups** a deliverable surfaces — content shape here, follow-up mechanics there.

## Standard 1 — Empirical numbers carry their setup + generalization scope

A cited benchmark/empirical number is meaningless — and actively misleading — without its harness and its boundary. Any deliverable that cites empirical numbers MUST include:

1. **Setup** — what the agent/model was actually given (retrieval? real-time data? the answer / market price in-prompt?), the task mechanism, and the **baseline** the number is scored against.
2. **Generalization-scope** — an explicit line on what the result does and does *not* say, and which different setup might flip it. Tie the boundary back to the recommendation.

**Why:** The operator reasons about findings structurally — a benchmark number is only meaningful with its harness (what inputs/tools the agent got, what baseline it's measured against) and its generalization boundary. A headline number without that invites a wrong conclusion.

**Origin / worked example (T-0791, revenue-project prediction-markets research):** the deliverable led with headline numbers (Prophet Arena 0.943 trading return; ForecastBench Brier ~0.10 / market-copy corr 0.994). The operator's immediate probe was "what structure did they have to work with… the numbers refer to a specific setup" + "it probably doesn't generalize." The numbers were setup-bound (identical pre-built static context, scored against the market price itself; the 0.994 is the crowd-consensus-in-prompt condition), so the true finding was narrow ("a one-shot static-context LLM won't beat a liquid market on info-speed"), NOT "LLMs can't forecast." The setup + generalization line is what separates those two conclusions.

## Standard 2 — Design-noodle dispatches carry both filed AND explicitly-NOT-filed

At **design-noodle / parking-the-question** tier, structure the deliverable with two companion sections at resolve-time:

1. **Crystallized + filed** — directions that warrant follow-up tickets, filed with `parent_ticket` linkage (the T-0388 / `research-dispatch-filing.md` "just file" discipline applies *here*).
2. **Explicitly NOT filed** — directions considered and rejected, each with the **load-bearing test that disqualified it**.

**Why:** the "just file, don't withhold" discipline is correct at actuation tier (concrete bugfixes, codification, ratification asks) but at design-noodle tier it grows ticket-count without bounding scope. Explicit-NOT-filed sections (a) make the reasoning visible to the operator and the org, (b) prevent next-session-me or a sibling from re-filing what was already considered-and-rejected, (c) demonstrate deliberate rejection rather than oversight. Validated by the operator (kudos via Recovery-relay msg_1611, on the T-0455 ticket-system noodle: 3 crystallized-and-filed vs 3 explicitly-NOT-filed, each disqualified by a stated load-bearing-properties test).

**This is the scoped exception to "just file":** `research-dispatch-filing.md` says surface-and-file; this standard narrows that — at noodle tier, file the crystallized AND record the rejected-with-rationale.

**When Standard 2 does NOT apply** (revert to plain "just file"):
- Actuation-tier research dispatches (e.g., a skills eval — file all the actionable items).
- Ratification refile chains (each step has a concrete acceptance).
- Bug-investigation dispatches (file the actual fix / audit-correction).

**Signal you're in noodle territory:** the parent ticket says "park this," "noodle," "no urgency," "resolve when crystallized," or similar.

## Standard 3 — Relayed claims carry independent primary-source verification + a CONFLICT section

**Adopted 2026-06-16 by Specialist** under the goal-level autonomy bar (the operator msgs 3733/3735 — methodology is below-goal-level; adopted autonomously, ledgered, digest-surfaced) per T-1170. Drawn from arXiv:2602.00428 (ICLR 2026, "When Agents 'Misremember' Collectively"), **adapted** (see the adaptation note below — do not copy the paper's prompts literally).

Any deliverable that **relays a claim sourced from a prior artifact** — a watch report, a dispatch brief's framing, another agent's finding; i.e. the claim arrives *already-framed* rather than discovered fresh — MUST:

1. **Verify each above-bar claim against its PRIMARY source, independently.** Not against the relaying artifact, and not against the agent's own prior memory. The primary source is the anchor.
2. **Surface CONFLICT explicitly** — a named section listing where the relaying artifact's framing diverged from what the primary source showed (factual errors, overstatements, stale attributions), OR an explicit "no divergence found." Don't let a corrected error stay silent inside the analysis.
3. **Tag residual uncertainty `[UNVERIFIED]` per claim, not bundled** — verify each claim on its own source; a true claim sitting next to a shaky one must not inherit the doubt (nor the shaky one the confidence).

**Why:** multi-agent / multi-artifact context-sharing amplifies false narratives through reinforcement loops — a distorted claim in a watch report propagates into tickets → briefs → the next agent's "established context" without re-challenge (arXiv:2602.00428; MANBENCH; cognitive-anchoring + source-scrutiny defenses cut collective misremembering ~74%). Pantheon runs a slow, *serial* version of that loop (the report→ticket→brief→handoff→ledger chain is the memory-consolidation artifact set the paper identifies as locking in distorted narratives).

**Adaptation (load-bearing):** the paper anchors on *internal-knowledge primacy* as the defense against peer pressure. Pantheon anchors instead on **PRIMARY-SOURCE primacy** — for external-world facts the agent's internal prior is exactly what we must NOT trust (it's stale/confabulatable). The failure runs **both** ways: relaying a confabulated claim *and* over-doubting a TRUE one because a prior said "that seems wrong." Both are corrected only by checking the EXACT primary source for the EXACT entity. (Pin: [[feedback_verify_claims_independently_not_bundled]].)

**Worked examples (2026-06-16):** T-1162 (AHE eval) caught the watch report's "UC Berkeley" attribution (actually Fudan/Peking/Shanghai) and its "top aggregate success" overstatement — surfaced in a CONFLICT section, not buried. The 2026-06-15 claim batch (Fable suspension; opus/sonnet-4-*snapshot* retirement) was over-doubted twice though both were true — the over-skepticism failure mode this standard guards against.

**When Standard 3 does NOT apply:** fresh-discovery deliverables where no prior artifact framed the claim (the agent found it itself, primary-source-first) — there's no relaying artifact to conflict-check against, though per-claim `[UNVERIFIED]` tagging still applies.

## Standard 4 — Cyber/exploit-sensitive research isolates raw detail to Sonnet subagents (never the Opus orchestrator)

**operator-ratified 2026-06-22 (TG msg 3338), standing policy "for now"** — codified here by Specialist per T-1276 (recovery-filed). This is a transcription of a ratified operational decision (principles-as-ratification; the codification itself is below-goal-level), NOT a fresh ratification ask. It is the "for now" guard until Anthropic's Cyber Verification Program whitelist for Pantheon's defensive-monitoring use-case lands (durable fix (b), in motion).

Unlike Standards 1–3 (deliverable **content shape**), Standard 4 governs **dispatch mechanics / operational safety** — *which model touches the raw material, and where it lands in context.*

Any research or claim-verification on a **security/cyber-sensitive topic** — `mcp-supply-chain`, `llm-agent-failure-modes`, the gated NSA-MCP guidance harvest (T-1203), CVE exploit mechanics, malware/worm/supply-chain-attack detail, or anything whose substance is *how an exploit works* — MUST:

1. **Run the web research / claim-verification via a Sonnet 4.6 subagent**, not the Opus orchestrator's own WebSearch. Sonnet handles the research fine and does not trip Anthropic's Opus-specific real-time cyber classifier.
2. **Keep the Opus orchestrator's context free of raw exploit/malware specifics.** The subagent returns a *vetted summary* (what's true, severity, remediation, primary-source link) — not the raw exploit content. The orchestrator reasons over the summary.
3. **Never let the orchestrator pull exploit detail into its own context to "verify it myself"** — that is the exact failure path below. Verify-first (Standard 3) still applies, but the primary-source fetch on cyber-sensitive material is the subagent's job, not the orchestrator's.

**Why:** the Opus-specific cyber safeguard is a real-time classifier on the *orchestrator's own context*. Once raw exploit content enters that context, the refusal (`stop_reason`, category `cyber`) persists and **every subsequent request re-hits the block** because the flagged content stays in context — the session wedges until a human (Recovery) restarts it. Sonnet subagents are outside that blast radius; the orchestrator stays clean by construction.

**Worked incident (root cause, T-1220 → T-1276):** during the T-1220 watch dispatch, the report-writing subagents were *already* Sonnet — but the **Opus orchestrator** ran its *own* WebSearch verification on the `mcp-supply-chain` topic (a real, publicly-reported NPM supply-chain worm). The exploit detail entered the Opus session context and tripped the classifier; the session wedged and required a Recovery restart. The leak was not the subagents — it was the orchestrator pulling raw exploit content into its own context via verification searches. Standard 4 closes exactly that path. (Signature + recovery: recovery memory `reference_cyber_safeguard_block_on_security_research`.)

**When Standard 4 does NOT apply:** non-cyber research (model cards, benchmarks, orchestration/agent-framework surveys, general capability findings) — the orchestrator may run its own WebSearch normally. The trigger is *exploit/malware/attack-mechanics substance*, not merely the word "security."

> **Note (axis scope):** this carve-out is scoped to the **cyber-classifier-wedge** axis only — there is no classifier to trip on a model card. It does **not** waive *indirect-prompt-injection* isolation for untrusted targets; that orthogonal axis is governed by [`web-research-isolation.md`](web-research-isolation.md) (this SOP's general-web sibling). Non-cyber + *untrusted* (arbitrary crawl/social mirrors) → still isolate per that SOP; non-cyber + *trusted* (arxiv/docs/official sources) → orchestrator-direct OK.

## Standard 5 — Dispatcher-side deliverable verification (ad-hoc dispatches)

*(Added 2026-07-14 by Orchestrator, T-1771 sweep S3 promotion — Specialist-crystallized from `knowledge/episodic/2026-07-13-silent-failure-vs-pantheon-posture/notes.md`; below-goal methodology adopted under msgs 3733/3735, digest-surfaced.)*

Every **ad-hoc Agent-tool dispatch whose deliverable is a file** gets a **files-on-disk check** (exists, non-empty, expected shape/frontmatter) before its result is used or relayed; every **decision-changing claim** in a dispatch report gets **one primary-source spot-check**. This generalizes what `tools/watch_runner.py` already enforces structurally for cron'd dispatches (file-exists + frontmatter) to the dispatches nothing enforces. Role-defining for any orchestrator-tier agent (HO, Specialist, PO tier).

**Why:** LLM self-reports are optimized to sound complete — fluency is an anti-signal, not evidence (arXiv 2606.09863 false-success finding; same family as the standing `feedback_dispatch_brief_claims_are_unverified` + `feedback_no_fabricated_tool_results` pins). The report saying "written to X" is a claim, not an artifact.

## Common failure modes

- **Headline-number-without-harness** — citing a benchmark figure with no setup/baseline/generalization line (Standard 1 anti-pattern; the wrong-conclusion risk).
- **Blanket "just file" at noodle tier** — over-applying the actuation-tier filing discipline, producing ticket-count growth + no record of what was rejected (Standard 2 anti-pattern).
- **Generalization line that doesn't loop back to the recommendation** — stating the boundary abstractly without saying which recommendation it supports or undercuts.
- **Silent error-correction** — verifying a relayed claim, finding the source artifact was wrong, and fixing it inside the analysis without a CONFLICT section (Standard 3 anti-pattern; the error keeps propagating in every artifact that doesn't read the fine print).
- **Bundled doubt / bundled confidence** — judging a batch of relayed claims as one block instead of each against its own primary source (Standard 3 anti-pattern; the over-skepticism *and* the over-trust direction).
- **Orchestrator-verifies-the-exploit-itself** — the Opus orchestrator running its own WebSearch on cyber-sensitive material to "verify it myself," pulling raw exploit detail into its context and wedging the session on the persistent cyber-classifier block (Standard 4 anti-pattern; the cyber-research must land in a Sonnet subagent, not the orchestrator).

## Cross-references

- [`research-dispatch-filing.md`](research-dispatch-filing.md) — follow-up filing mechanics; the "just file" discipline Standard 2 scopes at noodle tier.
- `adr/ORG-012-information-inflow-bubble-up-methodology.md` — the deliverable→promotion pathway; this SOP is itself an ORG-012 §Promotion #6 memory-pin→SOP lift.
- **T-0791** — Standard 1 origin (prediction-markets research). **T-0455** — Standard 2 origin (ticket-system noodle). **T-0871 / T-0869** — this SOP's promotion (first scripted ORG-012 sweep). **T-1170** — Standard 3 origin (Mandela-effect mitigation eval, arXiv:2602.00428); deliverable `knowledge/episodic/2026-06-16-mandela-effect-mitigation/notes.md`. **T-1276 / T-1220** — Standard 4 origin (cyber-classifier orchestrator-wedge incident; operator-ratified Sonnet-for-cyber policy TG msg 3338, 2026-06-22); recovery memory `reference_cyber_safeguard_block_on_security_research`.
- On ratification, the two source pins (`feedback_research_surface_setup_and_generalization`, `feedback_design_noodle_explicit_not_filed`) convert to reference-pointers to this SOP per the ORG-012 memory-vs-SOP layering rule.
