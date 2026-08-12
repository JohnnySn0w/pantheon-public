---
aliases: [ORG-018]
title: "Codex CLI as MCP tool source: pilot (Option 2 from msg_1640+1641)"
status: Ratified 2026-05-17
date: 2026-05-17
build_disposition: built
---
# ORG-018 — Codex CLI as MCP tool source: pilot (Option 2 from msg_1640+1641)

**Status:** Ratified 2026-05-17 by the operator (T-0415 verdict ~21:37Z via PWA).

**Date authored:** 2026-05-17

**Author:** Orchestrator (Orchestrator) — drafted post-conversation Telegram thread msg_1638→1640→1641. The operator's msg_1638 surfaced OpenAI Codex CLI as the analog harness; HO researched (msg_1640) the cost-of-acquisition shift (ChatGPT Plus *includes* Codex CLI via OAuth — no separate API key); the operator picked Option 2 (wrap Codex CLI capabilities as MCP tools for existing Claude instances) over Option 1 (Codex as 4th Pantheon instance) or Option 3 (stay closed). Two ratify-relevant rationales he gave: "use compute I'm already paying for" + "see if the current policy model works with other harnesses."

**Related:** [[ORG-016]] (multi-model orchestration Shape A pilot — *Ratified-then-cancelled*; this ADR is a re-opening with a different mechanism), msg_1638 + msg_1640 + msg_1641, [[T-0387]] (ORG-016 ratification ticket, closed). Sibling-in-spirit: T-0411 (xAI key custody, closed won't-do) — same cost-of-acquisition problem killed both Grok variants; Codex CLI doesn't have that problem.

**HITL:** Authored

## Context

[[ORG-016]] Shape A (Claude-primary + Grok/GPT as MCP-wrapped APIs) was ratified 2026-05-17 msg_1629 then cancelled ~6 min later msg_1634 once the operator cost-of-acquisition (separate paid xAI account + key custody) surfaced. ORG-016's §Post-ratify cancellation called out the lesson: future multi-substrate ADRs should foreground operator cost-of-acquisition at draft time.

The operator's msg_1638 ("hmm does openapi not have a tui/agent harness?") opened a different shape: instead of wrapping a non-Claude *API*, wrap a non-Claude *agent harness* (Codex CLI) that authenticates against an existing ChatGPT plan. Per HO's research msg_1640:

- **OpenAI Codex CLI** is OpenAI's terminal coding agent (Claude Code analog). Open-source, Rust-based, supports MCP for tool extensions. Sources: [Codex CLI overview](https://developers.openai.com/codex/cli), [Codex auth docs](https://developers.openai.com/codex/auth), [GitHub repo](https://github.com/openai/codex).
- **Authentication via ChatGPT Plus works** — "Sign in with ChatGPT" OAuth flow. ChatGPT Plus tier *includes Codex* at standard rate limits. The operator already pays a paid subscription tier.

Cost-of-acquisition that killed ORG-016 Shape A (separate paid product, separate key custody, separate billing surface) is **N/A** for Codex CLI on a Plus account. The mechanism reopens.

The operator picked Option 2 from msg_1640's surfaced options:
- ~~Option 1: Run Codex CLI as 4th Pantheon instance (Specialist/Recovery/Orchestrator + GPT-instance)~~ — heavier infra; declined as the v1 shape
- **Option 2: Wrap Codex CLI capabilities as MCP tools for existing Claude instances** — chosen
- ~~Option 3: Stay closed~~ — declined

His rationale (msg_1641):
1. "use compute I'm already paying for" — D1-aligned (no new billing surface) + P7-aligned (cheapest cost-asymmetry reopener)
2. "good to see if the current policy model works with other harnesses" — explicit observability ask; the pilot doubles as a policy-transfer test

## Decision

### Architecture — Codex CLI as MCP tool source for existing Claude instances

```
┌─────────────────────────────────────────────────────────────────┐
│ Pantheon Claude instance (Orchestrator | Specialist | Recovery — unchanged)     │
│   - Persona file unchanged                                       │
│   - Discipline (HERMES, narrow scope, audit ledger) unchanged    │
│   - Tool list gains: pantheon-mcp-codex tools                    │
└─────────────────────────────┬───────────────────────────────────┘
                              │ MCP tool call
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ pantheon-mcp-codex (new) — Python MCP server                    │
│   - Authenticated via ChatGPT Plus OAuth (Codex CLI session)    │
│   - Exposes Codex CLI capabilities as discrete MCP tools         │
│   - Spawns codex subprocess per tool call; captures stdout       │
│   - Logs each call to ~/.local/state/pantheon/codex_calls.jsonl │
└─────────────────────────────┬───────────────────────────────────┘
                              │ subprocess
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Codex CLI (OpenAI; ChatGPT Plus OAuth)                          │
│   - Rust binary; sandbox blocks network by default               │
│   - Reads/edits files in current directory                       │
│   - Returns result via stdout                                    │
└─────────────────────────────────────────────────────────────────┘
```

Key design points:

- **Pantheon's policy lives at the Claude-instance tier**, not the Codex CLI tier. Each Claude persona's discipline (HERMES guard, narrow scope, audit ledger, write-scope policy) governs what gets STAGED FOR COMMIT after the Codex call returns. Codex itself is a tool that produces text/edits; whether those edits land is a Pantheon-Claude decision.
- **Codex CLI runs sandbox-by-default** (network blocked, working-dir scoped). The MCP wrapper invokes Codex in a controlled tmp directory per call, then surfaces the result back to the Claude caller.
- **Authentication is the user-owned ChatGPT session**, not a separate Pantheon credential. The MCP server starts Codex with the existing `~/.codex/auth.json` (or equivalent). Key custody = none new.
- **MCP server lives at `~/.claude/mcp/pantheon-mcp-codex/`** following the existing per-MCP-server pattern. Python-implemented (matches existing Pantheon Python tooling stack); future Rust rewrite reasonable if performance matters.

### v1 pilot scope — one MCP tool, one use case

**Tool: `codex_second_opinion(task_brief: str, context_path: str) -> str`**

The pilot's narrowest useful surface: Claude instance asks Codex "given this task brief + this file/directory, return your analysis or suggested approach." Output is text returned to Claude's context. Claude decides whether to act on Codex's input.

**Concrete pilot use case**: HO dispatches an implementer; before approving the implementer's plan, HO calls `codex_second_opinion` with the plan + the relevant source files; gets Codex's read; cross-checks against the Claude implementer's plan; surfaces material divergence as a DECISIONS-section consideration.

Why this scope is right for v1:
- **Tests policy-transfer**: the request goes Claude → MCP → Codex → MCP → Claude. The MCP boundary is where Pantheon's discipline gets enforced (or doesn't). If Codex returns something that, applied as-is, would violate HERMES or scope-discipline, Claude is the gatekeeper — does Claude reliably catch it? Pilot answers this.
- **Tests cost**: ChatGPT Plus rate limits apply. The pilot's call volume is HO-controlled (one call per implementer plan review, optionally); easy to keep within limits.
- **Tests value**: does Codex's second opinion materially improve dispatch quality (catch real issues Claude missed, surface alternatives) or just noise (cost without payoff)?
- **Tests integration substrate**: validates that `pantheon-mcp-<X>` as a wrapping pattern works in our stack before scaling to other harnesses (Grok via xAI Plus when/if pricing shifts; Mistral; etc.).

### Policy-transfer evaluation (the operator's msg_1641 ask)

Explicit observability questions the pilot answers:

| Question | How the pilot answers |
|---|---|
| Does HERMES byte-sequence discipline survive across the MCP boundary? | Pre-commit hook is on the Claude side; runs against staged content. Codex output that contains the trigger gets caught at the Claude-stage-and-commit boundary, not at the Codex-return boundary. Pilot test: deliberately ask Codex something that returns trigger bytes; verify Claude's hook refuses. |
| Does narrow-scope discipline (one file scope per dispatch) survive? | Codex output is text/suggestions, not direct commits. Claude is the one editing files; Claude's persona enforces scope. The MCP wrapper's per-call workdir scoping is belt-and-suspenders. |
| Does audit-ledger discipline survive? | Codex calls log to `~/.local/state/pantheon/codex_calls.jsonl` (input, output, cost, timestamp). Claude's DECISIONS section references Codex-output material; chain-of-justification carries through. |
| Does write-scope discipline (Specialist only writes `knowledge/watch/reports/<date>/`) survive when Specialist calls Codex? | Same shape — Codex returns text; Specialist's persona is the writer. The MCP boundary doesn't expand write surface. |
| Does Codex's OWN model (different from Claude Opus 4.7) actually improve outputs? | Empirical question for the pilot. 30-day observation window; compare implementer plan reviews with and without `codex_second_opinion` consultation. |

Findings from the pilot inform whether to expand the surface (more Codex tools, more wrapped harnesses) or contract it (revert if policy-transfer fails or value < cost).

## Alternatives considered

| Alternative | Outcome | Why not |
|---|---|---|
| **Run Codex CLI as 4th Pantheon instance** | Considered, declined msg_1641 | Heavier infra (new account, persona, systemd, etc.); not needed for v1 value-of-non-Claude tool-access. Could revisit if pilot succeeds and surfaces value that wrapper-pattern can't deliver. |
| **Stay closed (msg_1634 line still holds)** | Considered, declined msg_1641 | Cost-of-acquisition objection that killed ORG-016 doesn't apply here ($0 incremental). The operator chose to reopen given the cost-asymmetry shift. |
| **Direct OpenAI API (no Codex CLI middleman)** | Implicitly excluded by Option 2 framing | Same cost-of-acquisition problem ORG-016 hit (separate paid API). Codex CLI's ChatGPT-Plus-OAuth path is what unblocks this — using the API directly would re-introduce the ORG-016 blocker. |
| **Wait for Claude Code → cross-vendor agent interop standard** | Considered, declined | Standards-track too slow; Codex CLI exists *now* + supports MCP *now*. Pilot the existing surface; revisit interop standards if they emerge with better ergonomics. |

## Consequences

**Positive:**
- $0 incremental cost (uses the operator's existing ChatGPT Plus).
- Adds non-Claude capability access to existing Pantheon Claude instances without persona drift, without new account, without new audit chain.
- Pilot doubles as a policy-transfer experiment — the operator's msg_1641 ask gets answered empirically.
- Sets a pattern (`pantheon-mcp-<vendor>`) for future low-cost vendor integrations (Mistral La Plateforme, Together AI, etc.) where the wrap-as-MCP-tool shape applies.
- Codex CLI's MCP-server-mode support means future `pantheon-mcp-codex` could expose richer surfaces (file ops, shell commands) if narrow `codex_second_opinion` proves valuable.

**Negative:**
- ChatGPT Plus rate limits become a Pantheon-side constraint. Plus tier is ~lower than Pro; HO must monitor + degrade gracefully on rate-limit-hit (don't crash; just skip the second-opinion call).
- Adds a new MCP server to maintain (Python code, MCP protocol updates, Codex CLI version compatibility).
- Codex CLI is a moving target — OpenAI may change auth flow, MCP support, or behavior. Pilot needs version-pinning + an update cadence.
- New code path for Pantheon to test against (the MCP wrapper itself is testable; the cross-process flow needs integration testing).

**Deferred:**
- Whether to add more Codex tools beyond `codex_second_opinion` (file-edit, shell-execution, etc.) — gates on pilot results.
- Whether to add `pantheon-mcp-mistral` / `pantheon-mcp-together` / etc. for other vendors — pattern established here; expansion gates on pilot policy-transfer findings.
- Codex CLI 4th-instance shape (Option 1 from msg_1640) — explicitly NOT chosen; defer until v1 pilot demonstrates the cheaper shape doesn't suffice.

## Implementation path (post-ratification)

On the operator ratify:
1. HO files sub-ticket (`T-XXXX`) for `pantheon-mcp-codex` MCP server scaffold. Plan-first dispatch via single-implementer; worktree pre-staged per T-0357 on `agent/<sub-ticket-id>` per T-0176 convention.
2. Scope: one tool (`codex_second_opinion`), thin Python wrapper, subprocess-spawn-and-capture pattern, call-log to `~/.local/state/pantheon/codex_calls.jsonl`, integration test against mocked Codex CLI subprocess.
3. Persona-brief paragraph in `claude-config/agents/implementer.md` and `project-orchestrator.md` naming when to use `codex_second_opinion` (HO-discretion at plan-review time; not autonomous).
4. Cross-link to ORG-018 in the MCP server's README.
5. First-fire smoke: HO dispatches an implementer for a small task; uses `codex_second_opinion` on the plan; manually compares Codex's read vs Claude's plan; logs observation in ticket close.
6. 30-day observation window: aggregate call-log data; assess policy-transfer findings + value vs cost.

After 30d:
- **Value confirmed**: file follow-up to expand toward v2 path (see §v2 path below) — exact shape depends on v1 findings.
- **Policy-transfer issue surfaces**: file the gap as an incident or directive amendment; v2 path likely sunsets.
- **No material value**: sunset the wrapper; document the negative finding as an ADR-amendment; v2 path retired.

## v2 path (named per msg_1649+1651)

The operator msg_1649 surfaced two operational goals beyond v1's narrow scope:

1. **Rate-limit absorption** — Codex CLI handles actual Pantheon work during Claude 5h-rolling-session cap-hit windows. Pantheon's effective uptime increases because work-flow continues on Codex when Claude instances cap.
2. **Model-agnostic validation** — Pantheon-as-system runs across substrates with discipline preserved. The empirical version of "is Pantheon's architecture model-bound or substrate-portable?"

Per the operator msg_1651, Path B chosen: **keep v1 narrow + name explicit v2 path here**, rather than expand v1 scope upward. Rationale: v1's policy-transfer experiment is cleaner when the MCP-wrap mechanism is observed in isolation; stacking work-shifting on top before v1 closes risks confounding the policy-transfer findings (was HERMES survived because the substrate works, or because Codex output was too narrow to exercise it?).

### v2 scope (sketch — concrete shape gates on v1 findings)

**Capability expansion** (from v1's `codex_second_opinion` single tool):
- Codex-CLI-via-MCP can EXECUTE work classes that Pantheon Claude instances currently own (specific work types TBD; candidates: research-tier surveys, code-review-tier reviews of implementer diffs, design-doc-tier ADR draft proposals).
- Each work class has explicit Pantheon-Claude-tier policy enforcement (the gatekeeper Claude instance reviews + approves what Codex produced before it lands in audit chain).

**Rate-limit absorption mechanism**:
- HO (or whichever Claude instance) detects Claude 5h-rolling-session cap approach (via `usage_monitor.py` window data).
- Pre-cap-hit, HO migrates eligible in-flight work to Codex CLI dispatch via the MCP tool surface.
- Post-cap-hit window expires, eligible work-products from Codex are audited by Pantheon-Claude tier + integrated into normal flow.
- v2-tier implementation: detection logic in HO persona + Codex-callable work shapes + audit pattern for Codex-produced artifacts.

**Model-agnostic validation criteria** (operationalized):
- HERMES guard catches Codex-produced trigger bytes at the Pantheon-Claude staging tier (verified by adversarial test in v1 already; v2 stress-tests at higher volume).
- Narrow-scope discipline holds when Codex output covers multiple file domains — Pantheon-Claude must successfully narrow + stage only the scoped subset.
- Audit ledger entries reference Codex-produced material with chain-of-justification intact (justifies-via cleanly through Codex-step).
- Write-scope discipline (e.g., Specialist only writes `knowledge/watch/reports/<date>/`) holds when Specialist uses Codex to draft research — Codex's text returns to Specialist; Specialist's persona is the writer.

**v2 target = 3-layer hierarchy mirror (per msg_1653)**: v2 IS closer to Option 1 4th-instance territory than first-amendment framing suggested. The operator msg_1653: *"for v2, will want to figure out what subagent/team equivalents gpt has and try to implement 3 layer hierachy. Essentially, have second HO persona if possible. This nets a full test of policy layer, plus throughput optimization."*

The architectural shape:
- **L1 — the operator (human)**: unchanged.
- **L2 — Head Orchestrators**: HO-Claude (Orchestrator, existing) + HO-Codex (new, if Codex CLI supports it). Two orchestrator personas running in parallel, each owning their substrate's orchestration tier.
- **L3 — Workers (implementers / reviewers / researchers / project-orchestrators)**: spawned by their respective HO; Claude-tier workers spawn via Agent tool; Codex-tier workers spawn via whatever subagent primitive Codex CLI exposes (TBD — v2 prerequisite research).

Cross-substrate boundary: HO-Claude and HO-Codex communicate via shared substrate (ticket queue + ledger in pantheon-config repo + Telegram bridge). They share the operator's read surface; they share the audit chain; they differ only in which model runs the agent loop.

**Prerequisite research (v2 phase 1, post-v1)**: figure out what Codex CLI exposes for recursive orchestration:
- Does Codex CLI have an Agent-tool equivalent (programmatic sub-agent dispatch)?
- Does Codex CLI support team-pattern (multiple workers coordinating via shared state)?
- Does Codex CLI's MCP support extend to "agent-as-MCP-server" (where Codex acts as the agent another agent calls into)?
- What's the Codex CLI persona/system-prompt equivalent for an HO-tier role (custom instructions, prompt-engineering surface)?

If Codex CLI supports primitives sufficient for L2/L3 hierarchy → mirror Pantheon's 3-layer structure on Codex side; second HO persona becomes "HO-Codex" with project-orchestrator + implementer + reviewer Codex-tier personas underneath.

If Codex CLI's primitives are thinner than Pantheon's Claude-side stack → degrade v2 scope gracefully:
- Thinner hierarchy on Codex side (e.g., HO-Codex with flat-worker pool instead of recursive project-orch tier).
- Document the gap as a Codex CLI capability ask (file upstream issue, observe roadmap).
- Pantheon-policy enforcement migrates to the Claude-tier review boundary where Codex output crosses into shared audit chain.

**Full policy-layer test (per msg_1653)**: v2 doesn't just test "policy survives at the MCP boundary" (v1's experiment). v2 tests **policy survives across a parallel cross-substrate hierarchy** — HERMES guard on Codex-tier commits; narrow-scope discipline on Codex-tier dispatch briefs; audit ledger entries from HO-Codex justifying-via P/D ids same as HO-Claude; write-scope policy applied to Codex personas (research-tier writes to knowledge/episodic/; etc.). If policy is genuinely model-agnostic, both HOs converge on consistent behavior across substrates.

**Throughput optimization (per msg_1653)**: with two HOs running in parallel, work fanout doubles. HO-Codex picks up work classes Codex is strong at (per-vendor differentiator analysis lives in ORG-016 Specialist research §2); HO-Claude continues its existing scope; the operator's Telegram surface sees both, routes work by class or just lets each pull from a shared queue. Rate-limit absorption becomes a property of the system (when HO-Claude caps, HO-Codex continues; vice versa).

### v2 trigger conditions

Three conditions ALL must hold to file v2 follow-up:
1. v1 30-day observation closes with value-confirmed disposition (Codex's second-opinion materially improved at least 30% of plan reviews where it was consulted — threshold tunable on v1 findings).
2. Policy-transfer evaluation §Policy-transfer evaluation table above shows GREEN on all four substantive questions (HERMES / scope / ledger / write-scope discipline survive).
3. Claude cap-hit frequency justifies the absorption work — i.e., ≥1 cap-hit per week in the v1 window AND demonstrated wall-clock recoverable-work-time the absorption would have captured.

If any of the three fails: v2 is shelved + the ORG-018 amendment records the gap. No automatic escalation; HO surfaces to the operator at v1 close.

### v2 NOT in scope until v1 closes

This section is a named-future-path commitment, not a current dispatch. v1's `codex_second_opinion` ships + observes for 30 days; v2 work doesn't begin until that observation window closes + the three trigger conditions evaluated. Avoids stacking ambitious-v2 on a v1 substrate that hasn't yet proven the mechanism works.

### v2 phases (post-trigger)

Three sequenced phases inside v2:
- **v2-α — Codex CLI primitive research**: HO dispatches Specialist on a focused survey (mirrors T-0416's shape): what subagent/team primitives does Codex CLI expose; what's the persona/system-prompt surface; what's MCP-bidirectional vs MCP-one-way; what's the closest analog to Claude Code's Agent tool. Deliverable: capability matrix + degrade-gracefully recommendation.
- **v2-β — HO-Codex persona + minimum-viable hierarchy**: build the HO-Codex persona (mirrors Orchestrator's structure adjusted for Codex CLI's actual primitives discovered in v2-α). **Substrate shape per msg_1655**: same as Pantheon's existing Claude instances — Codex CLI running inside a tmux pane wrapped by a systemd-user daemon (mirrors `claude-telegram.service` pattern for HO-Claude). I/O interface: reuse the existing Telegram bridge via its MCP surface (Codex CLI has MCP client support → can call `mcp__telegram-bridge-stdio__*` tools directly to read/reply the operator's chat). Validates the Telegram bridge's MCP surface as model-agnostic I/O. Smoke: HO-Codex picks one work item from shared ticket queue, dispatches a Codex-tier worker per its hierarchy capability, ships output through the same audit chain HO-Claude uses, and replies to the operator via Telegram bridge MCP same as HO-Claude does.
- **v2-γ — Parallel-HO operation + policy-cross-substrate validation**: both HOs running, work routed by class or pull-from-shared-queue; policy-cross-substrate metrics gathered (HERMES survival rate, narrow-scope adherence, audit-chain coherence, write-scope drift). 30-day observation feeds back into ORG-018 amendment or fresh-ADR depending on findings.

Each phase produces its own follow-up ticket(s); ratification needed before v2-β begins (committing to a second long-lived persona is operator-scope decision per trust-posture).

## Relationship to ORG-016

ORG-016 Shape A and ORG-018 share a **goal** (non-Claude capability access for Pantheon Claude instances via MCP tools) but use **different mechanisms**:

| Aspect | ORG-016 Shape A (cancelled) | ORG-018 (this ADR) |
|---|---|---|
| Underlying engine | xAI Grok API directly | OpenAI Codex CLI (which uses OpenAI API under the hood) |
| Authentication | Separate xAI API key | ChatGPT Plus OAuth (existing) |
| Cost-of-acquisition | $$ (new account + billing surface) | $0 (uses existing Plus sub) |
| Capability shape | Grok X Search (live X firehose) | Codex coding-agent capabilities (file analysis, code understanding) |
| Pilot scope | One tool (X Search) for watch-loop policy-monitoring | One tool (second-opinion) for HO implementer plan-review |
| Killed by | Operator cost-of-acquisition not foregrounded | (Not killed; this ADR is the alternative) |

ORG-016's lesson (foreground operator cost-of-acquisition as first-class ADR consequence) is applied in this ADR's §Context + §Consequences. The cost-asymmetry shift (Plus includes Codex; xAI separate) is the load-bearing change that makes this ADR viable where ORG-016 wasn't.

## DECISIONS

- Wrap Codex CLI as MCP tool source for existing Claude instances (Option 2) over running Codex CLI as 4th Pantheon instance (Option 1) — justifies via the operator msg_1641 explicit pick + P7 (cheap-action: minimum infrastructure, maximum reversibility — wrapper can sunset cleanly; 4th instance commits ongoing ops surface).
- v1 pilot scope = single tool `codex_second_opinion` not full Codex capability surface — justifies via P7 + D6 (minimum-viable pilot; expand on confirmed value; ChatGPT Plus rate limits make broad surface risky at v1).
- MCP server in Python not Rust for v1 — justifies via P1 (matches existing Pantheon Python tooling stack; Rust rewrite later if performance matters; reversibility favors the easier-to-evolve language).
- Pilot doubles as explicit policy-transfer experiment per the operator msg_1641 — justifies via P3 + P4 (decisions justify upward; the empirical findings inform future cross-harness ADRs).
- Call-log to `~/.local/state/pantheon/codex_calls.jsonl` (not pantheon-config audit chain directly) — justifies via P5 + ORG-001 (per-call ephemera lives in agent state; ledger absorbs summary observations at observation-window close, not per-call detail).
- Codex CLI version-pinning + update cadence required — justifies via ORG-005 (MCP channel reliability — vendor moving targets need explicit version-tracking discipline; bake into the MCP server README).
- §Relationship to ORG-016 explicit comparison table — justifies via ORG-012 + ORG-015 (information-inflow methodology; this is a sibling-not-supersession; both ADRs stay in catalog with clear differentiation).

## Ratify trigger

Filed as a fresh ratification ticket on completion of this ADR (T-XXXX, target=operator, `ratifiable_files: [pantheon-config:adr/ORG-018-codex-cli-mcp-wrap-pilot.md]`). On the operator ratify verdict via Telegram:
- Status flips Authored → Ratified inline + adr/README.md row updated.
- HO files the `pantheon-mcp-codex` implementation sub-ticket per §Implementation path.
- 30-day pilot observation window opens; calendar-tracked for post-pilot ADR amendment.

If the operator modifies the pilot scope / mechanism / observation period → HO edits this ADR in-place + re-files for ratification.
