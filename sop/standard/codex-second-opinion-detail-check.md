# Codex second-opinion — the HO pre-merge / pre-adopt detail-check

**Status:** Adopted 2026-06-16 by Orchestrator (HO) — autonomous per msg-3733 (principles-as-ratification; HO-practice methodology, below goal-level). Promoted from `notes/2026-06-13-codex-second-opinion-fit.md` (T-1201, ORG-012 note→SOP). The verdict earned its keep 3-for-3 on real wet-runs (T-1116/T-1117/T-1118, logged to `~/.local/state/pantheon/codex_calls.jsonl`). The operator override-available.

Run `mcp__pantheon-mcp-codex__codex_second_opinion` as a **detail skeptic over a specific artifact** — a one-call, ~tens-of-seconds, advisory check that fact-checks a concrete falsifiable claim before it drives a decision. It is NOT a strategist, decision-maker, ratifier, or reviewer-subagent replacement. The premise (the operator, msg 3803: "codex is better for details than big picture") held in every wet-run: codex materially changed the output every time, always on a checkable detail, never on the big-picture framing. Reach for it when a wrong *detail* would flip a *decision*.

## Use it for

1. **Fact-checking a load-bearing technical claim before it drives a decision.** A checkable assertion that flips the recommendation if wrong. *(Wet-run: caught that "PreToolUse can't inject `additionalContext` on the allow path" was stale against current docs — a wrong API premise that would have driven the wrong mechanism. T-1116.)* This is the strongest use.
2. **Adversarial review of a peer agent's design call before it's built.** The skeptic that catches a plausible-but-wrong implementation detail. *(Wet-run: found a proposed dotted `source_id.tool_name` namespacing would degrade LLM tool-selection / break tool-calling bridges; proposed flat-names-+-refuse-on-collision instead. T-1118.)*
3. **Pressure-testing a "defer / don't-build" judgment for hidden one-way-doors.** Good at "what makes this reversible, and what did you over-claim." *(Wet-run: affirmed a defer call but pinpointed the provenance/freshness frontmatter that must be added NOW or the eventual migration becomes archaeology. T-1117.)*

## Do NOT use it for

- **Big-picture / strategy / taste / org-shape calls** — its weak axis. Ask "is this specific claim true / is this design hole real," never "what should our architecture be."
- **Anything needing repo or live-system access it doesn't have** — it runs in an ephemeral tmpdir with only what you pipe in. Not a substitute for reading the actual code/datastore (ground-truth-over-narrative; `feedback_isolating_test_before_root_cause_claim`).
- **As a decision-maker or ratifier** — advisory only. Goal-level + externals stay the operator's; methodology stays HO's. It informs DECISIONS, it doesn't make or ratify them.
- **Replacing the reviewer-subagent** in the dispatch chain — a second *opinion*, not the DECISIONS-boundary reviewer that gates a merge.
- **Confirming something a cheap local check already settles** — if a grep, a test, or a doc read gives ground truth, spend that, not a codex call (D10).

## How to get value

- **Pass the real artifact as `context_path`** (piped as stdin), not a summary — codex checking the actual claims is the whole point.
- **Sharp brief beats open review:** name the hinge claim(s), ask codex to *overturn or confirm with concrete falsifiable detail*, and tell it explicitly **"don't restate the input — only add, correct, or challenge."** That instruction produced dense, non-redundant output every time.
- **Best ROI when the brief points at 2–4 specific load-bearing claims**, not "review this." Diffuse questions got diffuse answers; pointed ones got the catches.

## Cross-references

- `notes/2026-06-13-codex-second-opinion-fit.md` — the fit-finding evidence (full wet-run detail; this SOP is the distilled procedure).
- Auto-memory pin `reference_codex_second_opinion_fit.md` — the same triptych in HO's user-scope memory.
- `rulebook/directives.md` D10 (context-frugality — don't spend a codex call where a local check settles it).
- `adr/ORG-018` — codex-as-MCP-tool (this is the second-opinion-at-plan-review shape of that decision).

## Authoring discipline

Hand-curated per D2 (HO authors; this is HO-practice). Refresh as further wet-runs sharpen the fit-boundary; if the boundary shifts materially, amend here and note the run that moved it.
