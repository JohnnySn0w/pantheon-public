---
aliases: [ORG-031]
id: ORG-031
title: Harness-abstraction adapter + generalized daemon-onboarding (codex = first non-claude case)
status: "RATIFIED 2026-06-09 by the operator (via T-0897, resolved 2026-06-09T03:12:58Z). Drafted 2026-06-06 by Orchestrator (HO). Ratifies T-0712 deliverable (2): the adapter interface, the Option-(b) policy decision, and the harness-agnostic onboarding procedure. Codifies decisions; execution of the onboarder generalization is sub-ticketed (T-0898) and reversibility-classified."
date: 2026-06-06
build_disposition: built
---

# ORG-031 — Harness-abstraction adapter + generalized daemon-onboarding

**Status: RATIFIED 2026-06-09 by the operator** (via ratify-ticket T-0897, resolved 2026-06-09T03:12:58Z "Ratified"). Drafted 2026-06-06 by Orchestrator (`claude`, HO) under the raised-autonomy directive (Telegram msg 3454). Per ORG-015, `adr/` is the ratifiable substrate. This ADR is **T-0712 deliverable (2)** — the decision record the comparison report (deliverable 1) was the input for. It ratifies (i) the `PantheonHarnessAdapter` interface, (ii) the Option-(b) policy decision for codex, and (iii) a harness-agnostic generalization of the daemon-onboarding SOP. It does **not** itself stand up a new daemon — codex is already live (ledger 2026-06-02T01:05Z); the onboarder generalization is sub-ticketed below.

**Author:** Orchestrator (HO) — orchestrator-drafted; the operator ratifies.

**Related:** [[ORG-018]] (codex-as-MCP-*tool* pilot — the complementary shape; this ADR is codex-as-peer-*daemon*), [[ORG-016]] (multi-model orchestration), [[ORG-020]] (self-triggered restart — the stack the onboarder extends), [[ORG-027]] (role/domain is the stable axis the adapter keys on), [[ORG-028]]/[[ORG-029]]/[[ORG-030]] (portable-config topology — the role/harness-keying this generalizes against), `sop/standard/adding-restart-capability-to-a-new-agent.md` (the SOP this ADR generalizes into a harness-agnostic onboarder), [[T-0712]] (the epic), `knowledge/episodic/2026-06-04-codex-harness-abstraction-synthesis/notes.md` (deliverable 1 — the evidence base; referenced not re-pasted per D10), [[T-0426]] (the MCP-tool shape).

---

## Context

Per the operator msg_2846/2847/2849 (2026-06-02): drive toward a **harness-AGNOSTIC** framework for (a) bringing up an agent daemon from any CLI harness and (b) wiring it into Pantheon's loops + tooling the way Orchestrator/Specialist/Recovery are wired. Codex is the first non-claude harness to generalize against. Stated priority: the **policy + orchestration layer**; behavioral/executional differences are secondary.

Deliverable (1) — the codex-vs-claude-code capability + policy-layer comparison — is **done** (synthesis 2026-06-04, from Specialist doc-research T-0713 + HO hands-on T-0714/T-0715, live-verified on the running codex daemon). Its findings, load-bearing for this ADR:

- **~70% surface-isomorphic at the config tier.** Codex has native multi-agent (`features.multi_agent`, live spawn round-trip verified), a near-superset clone of claude-code hooks (10 events vs 8, same JSON/stdin/`permissionDecision` model), and `AGENTS.md ≡ CLAUDE.md` standing-prompt loading (live-verified post-restart).
- **Seven surfaces cleanly generalize** behind one adapter with mechanical key/filename translation: standing_prompt, mcp-client, hooks, subagents, session-resume, headless-exec, auth.
- **Two surfaces are genuinely harness-specific** and are the adapter's real engineering: **(a) inbound/turn-injection** (claude's `claude/channel` EXPERIMENTAL MCP push does NOT generalize — codex never surfaced bridge SSE messages as turns; non-claude agents must poll-and-inject via their own turn API), and **(b) hook-trust + approval-mode defaults** (codex gates new hooks + tool calls behind explicit trust/approval unless pre-answered).
- **The orthogonal Pantheon wiring** (inbox consumer, activity monitor, restart stack, ledger, Telegram) is harness-independent — it needs only a long-lived tmux+systemd process that accepts inbox-driven prompts, is tmux-driveable for recovery, and can write ledger/Telegram via MCP/hooks. Codex satisfies all four (verified).
- **Policy:** the §4 recommendation resolved the (a)-vs-(b) question empirically → **Option (b)** (org-tier policy, not in-harness approval gates), live-verified to silence prompts on the actual TUI daemon shape.

This ADR converts those findings into ratified decisions.

---

## Decision

### (i) The `PantheonHarnessAdapter` interface

Adopt the adapter/strategy philosophy the operator named (msg_2849) — *generalize tooling into abstract calls that dispatch to harness-specific wrapped functions* — applied to agent harnesses (the same shape as Echo's adapter crates). The adapter surface, partitioned by how it generalizes:

```
PantheonHarnessAdapter
  ├── standing_prompt   CLAUDE.md ↔ AGENTS.md (+ size cap + fallback list)
  ├── mcp_servers       per-server config (transport, auth, per-tool gating)
  ├── hooks             per-event handler registration (normalized event names)
  ├── subagents         spawn-by-role (description, per-role config layer, depth/concurrency/runtime bounds)
  ├── session           resume-by-id (fork = codex extension, not in the abstract surface)
  ├── exec              non-interactive prompt → result
  ├── auth              delegated to harness CLI
  ├── inbound  *bridge* poll-and-inject via harness turn API (NOT a generalizable MCP push)
  └── policy   *org*    approval/sandbox left at org-tier (Option b) — NOT abstracted per-harness
```

**Amendment 2026-07-06 (T-1579 — Paperclip pattern harvest, Specialist comparison; adopted by HO under amended D3 as spec-surface refinement, no ratified semantics changed):** three refinements to the generalized surfaces, harvested from Paperclip's independently-designed AdapterExecutionContext/Result contract:
1. **`exec` results carry structured cost fields** — `{tokens_in, tokens_out, cost, model}` on the result object, not recovered by transcript-scanning. Retires the out-of-band cost-inference ambiguity that dogged the T-0409/T-1478 threshold work, and a structured result field doubles as a structural completion signal (T-1553 structural-over-LLM-judge theme).
2. **Streaming progress callbacks** (`on_log` / `on_meta` / `on_spawn` class) join `hooks` as an inward-facing surface during a run — terminal-result-only reporting is how the idle-without-report bug family stays invisible until the end (bugs/subagent-idles-without-sending-final-report.md); a log stream that stops IS the early stall signal, and `on_spawn` gives subagent-of-subagent visibility (dispatch-routing bug family).
3. **`session` handle round-trips in the `exec` result** rather than requiring a separate resume-by-id lookup (Paperclip's eval rated this shape first-class among peer harnesses).

External corroboration, same harvest: Paperclip independently converged on approval-at-org-tier — the same shape as this ADR's Option (b) — which is independent-design evidence the policy-partition choice is correct. Source: knowledge/episodic/2026-06-29-paperclip-orchestration-tier-eval/notes.md; harvest ticket T-1579. These bind the §Ratify-trigger item-2 skeleton implementation when it lands; nothing already-built changes.

**Addendum 2026-07-15 (T-1740 — code-level verification against the live ACP adapter):** the 2026-07-06 amendment's three fields are confirmed unimplemented as of this date. Concretely, for the skeleton-implementation ratify-trigger item to close: (1) `tools/harness_adapter/base.py` needs a post-execution result type — none exists today; `exec_headless()` currently returns a pre-execution `argv` list, not a result — the three fields attach to this new type, not to an existing one. (2) The live ACP bridge (`tools/acp_bridge.py`) is independent of `PantheonHarnessAdapter` (no import relationship) and is the nearest real analogue to validate the result-type shape against: `AcpTransport._recv_until()` currently discards all non-matching wire messages (the `on_log`-equivalent gap, concretely) and `AcpBridge.handle_message()` reads only `stopReason` from the `session/prompt` result (the cost/usage-equivalent gap). (3) Whether `costUsd`/`usage` can be sourced from the ACP wire protocol itself, versus only from the harness's own transcript (as T-1730's billing check did), is unverified and should be checked empirically before finalizing the field's semantics — do not assume wire-native availability. `model` rides the already-ratified field set (it varies per-role within a harness). Full comparison: knowledge/episodic/2026-06-29-paperclip-orchestration-tier-eval/adapter-contract-harvest-T-1740.md (also records the (e) npm-package/hot-register DEFER, revisit trigger: third non-claude harness or out-of-monorepo adapter author).

**Note (T-1740):** `provider` (as distinct from `model`) is deliberately not part of the adapter result contract — Pantheon's adapter classes are provider-exclusive (`ClaudeHarnessAdapter` ≡ Anthropic, `CodexHarnessAdapter` ≡ OpenAI), so the field would duplicate information already available from which adapter executed. Revisit only if a future harness binding is itself provider-polymorphic (none is, as of this writing).

**Three partitions, each with a rule:**
1. **Generalized (the seven):** one abstract call, mechanical per-harness key/filename translation. These form the adapter's body.
2. **Bridge surfaces (the two harness-specific):** `inbound` and hook-trust/approval-defaults get a **real per-harness implementation**, not a thin mapping. `inbound` for non-claude agents is poll-and-inject through the harness's own turn API (tmux send-keys or per-inbound `exec`), explicitly NOT the `claude/channel` MCP capability.
3. **Explicitly NOT abstracted (configure-per-harness, do not paper over):** sandbox/approval, profiles, app-server/remote-control, doctor/health. These are first-party harness surfaces; forcing them behind a common call would hide semantics that differ in load-bearing ways. The adapter *documents* them per-harness; it does not unify them.

The adapter keys on the **role/domain axis** (ORG-027), not on persona identity — consistent with the portable-config topology (ORG-028/029/030).

### (ii) Policy layer for codex — Option (b), org-tier

Run codex under `approval_policy="never"` + `sandbox_mode="danger-full-access"` *(amended 2026-10-12, the operator TG 11733/11737; originally `sandbox_mode="workspace-write"` + `[sandbox_workspace_write] network_access=true`)*, relying on Pantheon's **existing org-tier policy** (Unix-asymmetric sudo + file-ownership write-scope + ratification gates + autonomy bar) — mirroring how Orchestrator/Specialist/Recovery already operate. This is **live-verified** on the running TUI daemon (zero approval prompts including MCP + `spawn_agent`; `trust_level` alone does NOT silence prompts, `approval_policy` does; prior config backed up at `~codex/.codex/config.toml.bak.preunattended`).

Rationale: (1) one policy model across all agents is what makes the abstraction's policy layer org-tier — the whole point of the epic; (2) it is the **same risk profile already accepted** for the bypass-permissions claude agents on this single-tenant, HO-monitored VPS, which is exactly the "thoroughly isolated, trusted, monitored" environment OpenAI's docs scope this config to; (3) Option (a) (in-harness `on-request` approvals plumbed to operator via Telegram) is a *different* operating model from the claude instances and adds orchestration to plumb for no consistency gain.

**Scope of this decision:** ratifies the policy *posture* for codex-as-peer-daemon. The test config is already live on the daemon (a reversible config write); no further org-tier policy mechanism changes.

**Amendment (2026-10-12, the operator TG 11733/11737):** codex runs `danger-full-access` + `approval_policy="never"` by design. "Permissions operate at the user level not at the harness level": the security boundary is the unix account (ORG-063 §2, "the UID is still the wall"), never a harness sandbox. The live codex config already runs this way.

### (iii) Generalized daemon-onboarding procedure

Generalize `sop/standard/adding-restart-capability-to-a-new-agent.md` into a **harness-agnostic onboarder**: a per-harness adapter binding (the §i interface) + the unchanged orthogonal Pantheon wiring (§3 of the report) + a **scripted bypass step** for the unattended-bring-up wedge class (login/device-code, hook-trust, trust-dir, sandbox/bubblewrap, NUX "press enter" notices — full catalog in the investigation doc). Onboarding a new harness = implement the seven-surface adapter + the two bridges + run the wedge-bypass script; the loops attach unchanged.

This is a **sub-ticketed** deliverable (below), not executed by this ADR.

**Fulfilled by:** `sop/standard/onboarding-a-new-harness.md` (sub-ticket 1 below; drafted T-1031, 2026-06-09) — the harness-agnostic onboarder this section calls for. (Back-reference added T-1202 to make the ADR↔SOP link bidirectional.)

**Migration companion (T-2633, 2026-08-07 — back-reference per the operator TG 7170):** the onboarder above stands up a NEW agent/harness. Migrating an EXISTING claude persona off its bespoke telegram-bot supervisor onto this ADR's role-keyed control plane is the complementary case, documented in `sop/standard/migrating-an-existing-persona-onto-the-control-plane.md` (generic tool `tools/persona_cutover.sh` / `persona_rollback.sh`, extracted from the Recovery T-2590 cutover; Specialist was the second migration and the extraction point). It resolves persona→role and leans on this ADR's role-keyed control plane + ORG-050's installer contract; it is a deliberately SEPARATE transitional migration tool, not a `persona_installer` verb — the legacy-bespoke-teardown it wraps around the installer call is a one-time concern that does not belong in the steady-state installer.

---

## Alternatives considered

- **Abstract everything (force all 12 surfaces behind common calls).** Rejected: the five first-party surfaces (sandbox/approval, profiles, app-server, cloud, doctor) differ in load-bearing semantics; a common call hides what an operator needs to see. Partition-and-document beats false unification (P1 — structure honest about its seams).
- **Option (a) in-harness approval policy.** Rejected per §ii: different operating model, more to plumb, no consistency gain; org-tier policy already exists and is proven.
- **Treat codex only as an MCP tool (ORG-018) and not as a peer daemon.** Not an either/or — both shapes stand. ORG-018 is codex-as-tool (second-opinion at plan-review); this ADR is codex-as-peer-daemon. Which to use is per-use-case, not a fork.
- **Defer the ADR until a second non-claude harness exists (avoid n=1 generalization).** Rejected: the comparison already separates harness-specific from generalizable on evidence; codifying the seam now makes the *next* harness cheap, and the partition is falsifiable/amendable when n=2 arrives.

---

## Consequences

- **Positive:** the policy layer lives at the org tier (one model, all agents); onboarding a new harness becomes a bounded checklist (adapter + two bridges + wedge-bypass) rather than an investigation; the codex daemon's live config is now decision-backed, not provisional.
- **Cost / risk:** Option (b) removes human-review gates and carries prompt-injection risk — accepted as the *same* posture the existing agents run under, on a single-tenant monitored VPS, and reversible via config. The adapter is n=1-derived; the partition may shift when a second non-claude harness onboards (named, not hidden — P6).
- **Reversibility:** the decisions are a spec; the live config write is reversible (backup exists); the onboarder generalization is sub-ticketed and reversibility-classified there.

---

## Justifies via

- **P1 (architecture over policy):** the seam between generalizable and harness-specific surfaces is enforced by the adapter's structure (partition-and-document), and policy lives at the org tier where structure (Unix asymmetry + ownership) already enforces it — not new per-harness policy.
- **D4 (read related specs at source):** drafted against the live-verified synthesis + both prong docs + the existing restart SOP, reconciled with ORG-027/028/029/030 (role axis + portable topology), not in isolation.
- **P6 (tensions preserved):** the n=1 generalization risk and the Option-(b) risk profile are named and reserved-for-revisit, not collapsed away.
- **feedback_fluid_not_default_deny / overnight raised-autonomy (msg 3454):** drafting the decision is autonomous and reversible; the live config is the same posture already accepted; anything beyond a reversible config write stays ratify-gated.

## DECISIONS

- **Adapter = partition into generalized(7) / bridge(2) / not-abstracted(5), not unify-all — justifies via P1 + the live comparison** — false unification hides load-bearing first-party semantics; the evidence cleanly separates the surfaces, so the adapter codifies the seam the data shows.
- **Policy = Option (b), org-tier, for codex-as-peer-daemon — justifies via P1 + the §4 live verification** — one policy model across agents is the precondition for an org-tier (not per-harness) policy layer, which is the epic's stated #1 priority; verified to work on the actual daemon shape; same accepted risk profile.
- **Onboarder generalization sub-ticketed, not executed here — justifies via ORG-028 sequencing + D7** — the ADR ratifies the design; the SOP-generalization + adapter-skeleton implementation is worker-tier code with its own reversibility classification, filed as children, not folded into the decision record.
- **n=1 generalization accepted-with-revisit-reserved, not deferred-to-n=2 — justifies via P6** — codifying the seam now makes the next harness cheap; the partition is falsifiable and this ADR is amendable when a second non-claude harness onboards.

## Ratify trigger

Surface to the operator at next engagement, bundled with the deliverable-1 report and the Option-(b) recommendation (which was already pending surfacing). On ratification: flip status to `Ratified: YYYY-MM-DD by the operator (<channel>)`, then the sub-tickets below become actionable.

**Sub-tickets to file on ratification** (not before — they implement a ratified design):
1. Generalize `sop/standard/adding-restart-capability-to-a-new-agent.md` → harness-agnostic onboarder (the seven-surface adapter binding + wedge-bypass script).
2. Implement the `PantheonHarnessAdapter` skeleton (codex + claude bindings) — the strategy layer §i names.
3. `[agents.<role>]` codex worker-config proof (mirror Pantheon implementer/reviewer personas) — the §7 follow-up the report flagged as needing a quick the operator steer on orchestration posture.

**Open question carried for the operator** (does not block ratification): headless `codex exec` billing model (usage-based vs ChatGPT-Plus quota, msg_2852) — gates ever leaning on the headless path; TUI is primary so it's deprioritized.
