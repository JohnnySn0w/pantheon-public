# rulebook/policies/

Operational calibrations of `rulebook/directives.md`. One policy = one file. HO authors under D2; the operator ratifies at surface time. Per-policy file shape per `architecture/adr/ORG-010-three-tier-policies-extension.md` §Implementation §2.

## When to add a policy

Add a policy file when a directive's operational interpretation has stabilized enough to write down — and especially when:

- Multiple incidents / lessons would have been prevented by the rule.
- HO has been making the same call repeatedly + the cost of re-deriving it each turn is non-trivial.
- A worker-tier dispatch needs the rule to scope itself correctly.

Don't add a policy speculatively. Don't add a policy for something a single comment in the source can carry. Policies are durable enough to be referenced by name; ephemeral guidance lives in CLAUDE.md or persona briefs.

## When NOT to add a policy

- The rule is org-identity (→ principle).
- The rule has multi-month project scope but no ops-tier specifics (→ directive).
- The rule is one-shot ticket-scope guidance (→ ticket body or CLAUDE.md note).
- The rule needs the operator ratification per *event*, not per *standing rule* (→ ticket).

## Founding policies

| File | Operationalizes | Ratified |
|---|---|---|
| `autonomy.md` | D1 (bandwidth) + D3 (autonomy bar) — inbox-consumer gating (activity-digest cadence retired 2026-09-29, T-4289) | 2026-05-04 (BACKLOG §B9) |
| `trust-posture.md` | D3 (autonomy bar) — HO direct-action calibration | 2026-05-03 (msg_377) |

(Migrated from `notes/` on 2026-05-10 per T-0191. History preserved via `git log --follow`.)

## Auto-index

<!-- AUTOGEN-START spine-index -->
| [`access-by-operation-risk.md`](access-by-operation-risk.md) | **Status:** Withdrawn 2026-10-31. The operator rejected or redirected the surviving decision rows in TG 12648, and TG 12650 identified the missed T-4554 stale-record preflight. T-4133 and its implementation child were dropped. This file is retained as historical design evidence only; it is not authoritative and none of its reconciliation rows may be executed without fresh grounding and scope. **Author:** Orchestrator (Orchestrator), under T-2893. **Operationalizes:** ORG-075 decision D3: access is gated by the operation's blast radius and reversibility, not by the actor — least privilege, answered the same way for every actor including the HO. **Stays inside:** `trust-posture.md`'s reserved set. New credentials, wider scopes, access grants, anything that mints or reveals a secret, money, and outward-facing acts remain the operator's. This policy does not move any of them to the HO; where it proposes a carve-out, the row says **decide**. **Yields to the broker:** when the ORG-052 host broker lands, its approval tiers govern wherever they are stricter than this file (its §5 puts cross-seat repair and credential install/rotation behind the operator's approval, which then overrides rule 5's "no gate" and row 15). **Re-evaluate when:** a new operation class appears; the ORG-052 host broker or ORG-051 credential references land (they replace several manual controls below); a credential is exposed; a seat or service account is added or retired. |
| [`autonomy.md`](autonomy.md) | **Status:** Ratified 2026-05-04 by the operator (BACKLOG §B9 design + Update 2026-05-04T14:45Z); migrated to rulebook/policies/ on 2026-05-10 per T-0191 (ORG-010 three-tier extension). **Author:** Orchestrator (Orchestrator) **Date:** 2026-05-04 **Re-evaluate when:** consumer cron mechanism changes; `autonomy_off` off-switch retired; new agent added to consumer coverage; bridge `/health.last_inbound_at` semantics change. **Justifies via:** D1 (bandwidth preservation), D3 (autonomy bar), P3 (decisions justify upward). *(Citation corrected from drifted "P5 (decisions-driven)" per T-1587, ratified the operator 2026-07-06 msg 4664.)* |
| [`code-size-norms.md`](code-size-norms.md) | **Status:** **Ratified 2026-06-04 by the operator.** PROPOSED 2026-06-02 (T-0723, spawned from noodle T-0646 / ORG-024). **Author:** Orchestrator (Orchestrator). **Re-evaluate when:** LLM edit-fidelity problems start clustering around specific large files; the model's reliable-attention window materially changes; ORG-024 (project-is-the-eval) is revisited. **Justifies via:** P1 (code structured for reliable LLM editing is structural, not "remember to be careful"), D7 (reviewer scope), ORG-024 (the project is the eval). |
| [`cost-monitoring-thresholds.md`](cost-monitoring-thresholds.md) | **Status:** Ratified 2026-05-17 by the operator (T-0409 verdict 17:34Z) — Specialist row + catalog format both ratified. Orchestrator row was previously ratified via Telegram-verbatim per T-0010 (msg_412, 2026-05-03) before this catalog existed; reflected here for canonical reference. Recovery row ratified 2026-05-11 by the operator (msg_989 via T-0069, p99/172M — Orchestrator-consistency shape). |
| [`goal-mapping.md`](goal-mapping.md) | **Status:** Adopted 2026-06-11 by Orchestrator (HO) — autonomous under the operator msg 3733 (principles-as-ratification; below-goal-level policy adopted under P/D justification + ledger, no ratify-ask). Originally drafted per the operator msg 3737/3738 ("many things may not have a defined goal level. I think that might be our gap in structure policy-wise"); in normative force since. Operationalizes the amended D3 bar's goal-level test. **Author:** Orchestrator (Orchestrator) **Re-evaluate when:** the goals registry (`rulebook/goals.md`) changes shape; OR a mapping dispute with the operator shows the reversibility test miscalibrated; OR ≥2 incidents where work proceeded under a proposed mapping he then rejected. **Justifies via:** D3 (amended 2026-06-11 — bar at goal-level), D1 (digest surface, not asks), P4 (traceability), P6 (the ambiguity is a real tension; this preserves it as a named mechanism instead of collapsing to always-ask or always-adopt). |
| [`mcp-adoption.md`](mcp-adoption.md) | **Status:** Ratified 2026-10-07 by the operator (Telegram 11297/11299, T-3276 ticket walk: "looks stringent. good for mcp, external servers aren't to be blindly trusted" / "it reads well enough, i say it'll work"). It was ratified together with the `sticker` allowlist row (§2) and yes to both §8 questions: build the tripwire as a follow-up, and the policy covers the codex harness (§0). Previously: PROPOSED, HO-landed 2026-08-26 from Specialist's draft at commit `9637847cc` (T-3137). **Author:** Specialist (draft) → Orchestrator (Orchestrator, landed under D2 policy-authorship). **Date:** 2026-08-26 **Re-evaluate when:** the next `mcp-supply-chain` watch report flags a new malware namespace; OR any seat gains a network-transport MCP server; OR any allowlisted server changes version; OR the codex harness's inactive plugin-marketplace templates (T-3171) are activated; OR the follow-up tripwire proposed in §8 lands, shifting §2's enforcement from procedural to structural. **Justifies via:** P1 (architecture over policy — the gate is structural: an unlisted server cannot be adopted, not "remember to audit"), D3 (autonomy bar — MCP *adoption* is goal-level/reserved; this policy is the below-goal methodology that governs the reserved decision when it arises), D11 (third-party surface verification — read the source, validate against the real transport), P4 (traceability — every allowlist entry carries its audit trail). |
| [`trust-posture.md`](trust-posture.md) | **Status:** Ratified 2026-05-03 by the operator (Telegram, msg_377 — "very solid" with one wording fix on the worker→orchestrator routing chain to include project-orchestrators explicitly); migrated to rulebook/policies/ on 2026-05-10 per T-0191 (ORG-010 three-tier extension). **Per-domain generalization RATIFIED 2026-06-05 by the operator (ORG-027, [[T-0789]]):** the calibration now applies to all three always-on orchestrators (Orchestrator/Specialist/Recovery), each scoped by its domain — see "§Per-domain calibration (ORG-027, in force)" at the foot of this doc, which is authoritative. The "What HO auto-approves / holds / never permits" buckets in the body below read as the per-domain bar applied **within each orchestrator's domain** (HO's domain = org/delivery/infra — the broadest; see ORG-027 §1 for the full domain map). **Author:** Orchestrator (Orchestrator) — orchestrator-drafted under T-0022; operator-ratified. **Date:** 2026-05-03 **Re-evaluate when:** see "Re-evaluate when" section below — substrate changes, escalation-rate deviation, new principle/directive, cost/usage threshold breach, HO migration. **Justifies via:** D3 (autonomy bar — the org-level rule this calibrates), D1 (bandwidth preservation), P3 (decisions justify upward), P5 (roles don't hop). *(Both cites corrected from renumber drift per T-1587 fork (b), ratified the operator 2026-07-06.)* |
<!-- AUTOGEN-END spine-index -->

## How to amend

- Open a ticket: subject `policy(<name>): <change>`, target HO, ask = the proposed amendment text.
- HO drafts the diff in-ticket; the operator ratifies via Telegram or ticket reply.
- Land on resolve (one-commit shape): file diff + ledger append + ticket move to `done/`.

## Related

- `rulebook/README.md` — the three-tier spine + cross-tier discipline.
- `rulebook/directives.md` — the directives these policies calibrate.
- `architecture/adr/ORG-010-three-tier-policies-extension.md` — framework extension that introduced this folder.
- T-0173 (parent), T-0191 (migration).
