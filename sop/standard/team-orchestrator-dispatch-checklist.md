# Team-orchestrator dispatch checklist

**Status:** Ratified per T-0273 (the operator 2026-05-13T03:00:57Z). T-0238 §Pre-merge smoke addition + T-0229 §Merge step addition included in the ratification scope.

**Amended 2026-09-28 (T-3079): default routing for multi-worker arcs moved to peer-orchestrator seats.** The in-session project-orchestrator this checklist spawns sits two layers below HO. The harness routes its subagents' completion notifications to the ROOT session, not to it (`bugs/nested-subagent-completion-notification-routes-to-main-not-spawner.md`, deterministic). So the default for a multi-worker arc is now a **peer orchestrator seat** (mason or artisan), handed off per `sop/standard/retask-dispatch.md`. The seat is its own session root, and its implementer and reviewer land one layer down, where notifications route correctly. The operator ratified the peer-orchestrator shape on 2026-09-07 (TG 9294-9300); it was live-proven 2026-09-27 on T-4040, T-4008, T-4044 and T-4015 via mason. Use this checklist's in-session shape only when an arc needs HO's live conversational context or iteration too tight for mailbox latency, or when neither seat holds the needed repo grant. The body below is unchanged and still governs that residual case.

This is the pre-dispatch checklist for HO when spinning a team-pattern cycle (b9-shape). Captures the operational discipline that's been refined across cycles b9 → b35 → b36, including the workarounds for known team-spawn substrate gotchas (`bugs/teamcreate-orchestrator-restricted-tool-inventory.md`, `bugs/sendmessage-without-spawn-dispatch-failure.md`, `bugs/team-sendmessage-system-name-not-personal-name.md`). The checklist is what HO runs through before spawning the team (Agent-tool named teammates — the session's agent-team is implicit; no TeamCreate, removed CC 2.1.178); following it prevents the dispatch-failure patterns those bugs/ entries catalog.

## When to use this

Whenever HO is about to fire a multi-worker team-pattern cycle. Concretely:

- Multi-unit feature arcs that exceed the HO-direct edit-loop threshold (3+ atomic edits / >2 source files / ~50 LoC / any design-decision).
- Cross-repo cycles needing orchestrator coordination (e.g., pantheon-config + pantheon-pwa Phase A+C cross-repo).
- BACKLOG-tier items needing decomposition into worker-sized units.
- Any cycle where the orchestrator's value-add is decomposition + per-unit dispatch + synthesis (not "more workers in parallel").

**Do NOT use the team pattern for** single-worker focused tasks (HO-direct dispatch of an implementer + on-demand reviewer is lighter); routine bug fixes within the HO-direct threshold; or work that already has a project-orchestrator persona configured elsewhere (don't double-stack orchestrators).

## Pre-dispatch checklist

Run through ALL items before spawning the first teammate via the Agent tool. Skipping items causes the dispatch-failure patterns catalogued in bugs/.

### 1. Brief construction prerequisites

- [ ] **Ticket exists + state=active**, claimed by HO. If not, `python3 tools/ticket.py claim T-NNNN` first.
- [ ] **Spec is canonical-clean** — every named symbol (file paths, schema fields, route names, env vars) has been verified at source per D4. Substrate assumptions verified per `lessons/orchestrator-tier-substrate-verification.md` (file paths exist, wire shapes match the assumed transport, persistence shapes match the assumed substrate).
- [ ] **§Locked answers drafted** — the 5-9 hard locks that the orchestrator must readback verbatim in their plan + carry into per-unit briefs. Typical locks: branch name, decomposition shape, smoke command + baseline, brief-named test list, DECISIONS chain propagation requirement, self-merge-or-PR-default, any unit-specific structural locks.
- [ ] **Brief-named tests pre-enumerated** — exact test names locked at brief-tier so plan↔code↔reviewer artifact binding holds per D6. The implementer cannot rename without REBOUND.
- [ ] **Multi-ruling enumeration clause included** — if the brief carries N numbered rulings, it ends with the mandatory report contract: enumerate all rulings 1–N with per-ruling status (built / deferred-with-disclosure / unchanged-from-last-increment / infeasible-stopped). Per `sop/standard/team-orchestrator-brief-construction.md` §Locked-answers + `lessons/multi-ruling-briefs-require-per-ruling-status-enumeration.md` (T-1530: a silently-dropped ratified ruling cost a rebound cycle).
- [ ] **Bubble-up criteria spelled out** — copy the 5 criteria from CLAUDE.md §Chain of justification verbatim into the orchestrator's brief (not by reference). Restart-fresh orchestrators may not load the file unless the brief operationalizes it.
- [ ] **Delegation invariants explicit** — every worker/subagent brief states (a) the local authority boundary, (b) sensitive operations that require stop/escalation unless explicitly authorized, and (c) independent-verification requirements for material claims or externally visible effects.
- [ ] **HERMES byte-sequence rule** mentioned if the cycle touches anything that might contain the trigger bytes (research, docs, anything referencing the original issue).
- [ ] **Arc sub-ticket gating briefed** — instruct the orchestrator that any build sub-tickets it files for the arc carry a suppression gate **at file-time**: `depends_on:[T-prev]` for sequenced units (the honest gate — also cascade-unblocks per ORG-013 when the predecessor resolves), `interactive-required` for judgment/data-gated ones the auto-consumer must never fire on. The orchestrator then claims each to `active/` when its implementer picks it up. Without the gate, the inbox auto-consumer can't tell an orchestrator-driven sub-ticket from fresh work and **races the orchestrator's in-flight implementer onto the same branch** (`lessons/inbox-auto-consumer-races-orchestrator-arc-subtickets.md`).

### 2. Team scaffolding

- [ ] **Project-orchestrator spawned** via the Agent tool with a descriptive `name` (e.g. `b<NN>-<ticket-or-shape>`) + agentType (`project-orchestrator`) + the brief. (No TeamCreate — the session's agent-team is implicit; the Agent `name` makes the teammate addressable via SendMessage.)
- [ ] **Confirm `team-lead@<team-name>` slot pre-allocated to HO** — `cat ~/.claude/teams/<team-name>/config.json | jq '.leadAgentId'` should show `team-lead@<team-name>`. (This is structural; no action needed, just verify.)
- [ ] **Agent-spawn the project-orchestrator** with `subagent_type=project-orchestrator`, `team_name=<team-name>`, `name=orchestrator`, full brief in `prompt`, `run_in_background=true`. The `name=orchestrator` (NOT `team-lead`) avoids the slot-pre-allocation collision (per `bugs/teamcreate-team-lead-slot-pre-allocation-collision.md`).
- [ ] **DEFAULT — self-spawn (ORG-036): do NOT pre-stage.** Spawn the orchestrator with the brief and let it `Agent`-spawn its own implementer/reviewer + stage its own `agent/<task>` worktrees. The premise that forced HO-pre-staging — "the orchestrator does not have the `Agent` tool in team-context" — was FALSE as of T-1291 (probe-verified: orchestrator HAS `Agent`). The `bugs/teamcreate-orchestrator-restricted-tool-inventory.md` entry is superseded for this purpose. Under self-spawn, skip the pre-stage + member-verify steps below.
- [ ] **EXCEPTION — pre-stage workers** (only when the brief invokes an ORG-036 exception: a worktree/resource only HO can stage; a sensitive/live-service arc needing HO spawn-visibility; a deep arc near the 5-level spawn-depth limit) — Agent-spawn the implementer (and any other long-running workers) into the same team via `team_name`. Names: `implementer`, plus role-suffixed variants if multiple. State explicitly in the brief that the worker is pre-staged (address via `SendMessage`), never "spawn the implementer."
- [ ] **Verify members in team config** — `cat ~/.claude/teams/<team-name>/config.json | jq '.members[].name'` should show team-lead + orchestrator + implementer(s). This is the smell-test check from `bugs/sendmessage-without-spawn-dispatch-failure.md` — SendMessage to an unspawned name silently writes to an orphan inbox; verify recipients exist before any send.

### 3. Reviewer pattern

- [ ] **DEFAULT — orchestrator self-spawns the reviewer (ORG-036).** When an implementer's diff is READY, the orchestrator `Agent`-spawns a one-shot `reviewer` (subagent_type: `reviewer`) itself, one-shot per code-tier READY — it has `Agent` (T-1291), so the old "must ping HO because no Agent tool" round-trip is gone. The reviewer reports to the orchestrator; the orchestrator lifts the verdict into its arc audit.
- [ ] **EXCEPTION — HO-spawned reviewer** (only under the pre-stage exception, or if HO wants direct review-visibility for a sensitive arc): orchestrator pings HO via SendMessage at code-READY; HO Agent-spawns the one-shot `reviewer` and relays the verdict. Document whichever applies in the orchestrator's brief operationally (not by reference).
- [ ] **Per-ruling enumeration verified (multi-ruling briefs)** — the reviewer verifies the report enumerates EVERY ruling 1–N and each status is one of the four legal values (built / deferred-with-disclosure / unchanged-from-last-increment / infeasible-stopped); a missing or partial enumeration is itself REBOUND-able, before any content review. (T-1612 promotion; `lessons/multi-ruling-briefs-require-per-ruling-status-enumeration.md`.)
- [ ] **Review diff base = `origin/main`, not local `main`.** Brief the reviewer to diff `git diff origin/main..<feat-branch>` (and HO uses the same base prepping the merge). The worktree's local `main` goes stale when PRs merge mid-cycle, so a local-`main` base renders already-merged commits as phantom deletions → spurious REBOUNDs / false "extra changes" flags (bit T-0533/T-0538/T-0557). If `main` advanced during the cycle, merge `origin/main` into the feat-branch (or diff the merge-base) first for a clean review surface. (HO memory `feedback_merge_surface_check_against_origin_main.md` + `feedback_merge_main_into_agent_before_review.md`.)

### 4. SendMessage routing

- [ ] **Confirm SendMessage `to:` field uses SYSTEM identifiers**, not personal names. `orchestrator`, `implementer`, `team-lead` — NOT `orchestrator`, `specialist`, `recovery`. Personal-name addressing lands in separate inbox buckets and breaks auto-delivery to lead's conversation pipe (per `bugs/team-sendmessage-system-name-not-personal-name.md`).
- [ ] **SendMessage body shape**: substantive content goes in `message`, summary goes in `summary` (the 5-10 word preview). Recipients see `message` body in their conversation; `summary` is the UI preview only (per `bugs/sendmessage-body-delivers-summary-preview-only.md`). Don't put brief content in `summary` thinking it'll be delivered.

### 5. Chain-of-justification propagation (load-bearing per CLAUDE.md)

- [ ] **Every dispatch brief explicitly requires** the dispatchee to (a) emit DECISIONS justifying-via P/D ids on every material call, (b) recursively require the same of anyone they dispatch, (c) report back with the chain intact. Spell this out operationally; don't rely on inheritance-by-reference.
- [ ] **Smell test** before firing the dispatch: "is the chain-of-justification rule explicit in this brief, or am I assuming the dispatchee will infer it?" If the latter, fix it before firing.

### 6. Smoke + acceptance contract

- [ ] **Smoke command + expected baseline locked in the brief.** Per D5 literal-command-tail discipline. Orchestrator pre-relay runs the smoke before relaying upward (operationally a literal `bash` command paste, not delta-claims).
- [ ] **Acceptance criteria stated as testable assertions**, not vague aspirations. Each criterion maps to a brief-named test or an explicit verification step.

### 7. Post-dispatch monitoring

- [ ] **Acknowledge orchestrator's plan-tier surface within reasonable time** (~5-10min after expected first surface; depends on cycle complexity). Don't let orchestrators sit blocked on team-lead approval.
- [ ] **Apply the 3-REBOUND budget per ticket** — if I'm REBOUNDing the same orchestrator on the same unit three times, the brief is wrong or the unit is mis-scoped; re-brief or escalate per CLAUDE.md bubble-up criterion #4.
- [ ] **End-of-cycle teardown** — when the last unit lands + the ticket resolves, request shutdown of orchestrator + implementer(s) via SendMessage(`{"type":"shutdown_request"}`); confirm members terminated. (No TeamDelete — removed CC 2.1.178; the implicit team dissolves as its members exit.)

## Pre-merge smoke (Playwright e2e + visual — T-0238 extension)

Added as part of the b9-pattern close-scope discipline. Run after implementer READY + reviewer APPROVE, before HO performs the merge.

- [ ] **`bun run test:e2e`** in `pantheon-pwa/` — must exit 0. Runs Layer 1 lifecycle smoke (7 tests) + Layer 2 visual regression (7 tests). If any visual test fails due to intentional UI change, run `bun run test:e2e:update-snapshots` and commit the new baselines on the feat branch before merging.
- [ ] **Build freshness** — if the feat branch has frontend changes, run `bun run build:web` first. The e2e daemon serves static assets from `build/web`; stale builds cause tests to run against the wrong UI. globalSetup warns if `build/web/index.html` is older than `web/src/**/*.ts`.
- [ ] **Sweep findings** — if the suite surfaces new failures (beyond the known `playwright-sweep-finding` tickets), file a new `playwright-sweep-finding` fix-ticket parent=T-0238 before merging. Do not merge code that introduces new e2e failures.

**Scope**: `pantheon-pwa` only. CI integration is a separate Pantheon-wide infra ticket. Recovery-side pre-deploy gate is v1 nice-to-have.

## Merge step (superseded 2026-10-08, T-4548)

The merge step that used to live here (direct `git push origin main`, a branch delete chained onto the merge, a halt for the operator on vault diffs, manual PWA rsync) is retired. Every point was overtaken: push and merge go through a PR, the HO merges, a delete never chains onto the merge, the D3 amendment retired the routine vault-diff halt, and kino deploys itself via T-0316. Follow:

- [`merging-an-agent-branch-pr.md`](merging-an-agent-branch-pr.md) for push, PR, CI, the same-turn mailbox HOLD check, the merge and cleanup;
- [`redeploy-pantheon-pwa.md`](redeploy-pantheon-pwa.md) for getting a merged pantheon-pwa change live.

The reviewer-is-the-gate invariant (T-0156) still holds: no merge without a reviewer APPROVE, and the implementer never self-merges.

## Common dispatch pitfalls (anti-patterns this checklist prevents)

- **Forgetting to pre-stage workers** — orchestrator hits the no-Agent gap mid-cycle, wastes turns + asks HO. Items 2.5 + 2.6 cover this.
- **Naming orchestrator `team-lead`** instead of `orchestrator` — slot collision; HO's own slot is `team-lead`. Item 2.3 covers this.
- **Personal-name SendMessage** — silent orphan inbox. Item 4.1 covers this.
- **`success: true` on SendMessage assumed-equal-to-delivered** — items 2.6 (verify recipients exist) + 4.1 (correct routing) together catch this.
- **DECISIONS-by-reference** instead of operational embedding — item 5.1 catches this.
- **Implicit delegation boundaries** — item 1.7 catches worker briefs that leave authority, sensitive-stop conditions, or verification burden to inference.
- **No 3-REBOUND escape hatch named** — orchestrator drifts on unsolvable unit. Item 7.2 covers this.
- **Worker report trusted without a deliverable check** — for file-deliverable dispatches, verify files-on-disk before using the result; spot-check decision-changing claims against a primary source. Rule lives at [`research-dispatch-deliverable-standards.md`](research-dispatch-deliverable-standards.md) §Standard 5 (T-1771).

## DECISIONS (SOP-tier)

- **Checklist format over prose narrative** — justifies via D5 (literal items, not paraphrase) + D10 (operator scan-and-tick is faster than reading prose); SOPs are operator-facing surfaces.
- **Bugs/ catalog cross-references inline** — justifies via D7 (don't rely on inheritance-by-reference; spell out which gotcha each item prevents so the reader doesn't have to chase). The bugs/ entries themselves are the source-of-truth; this SOP is the dispatch-time aggregator.
- **Reviewer pattern explicit as HO-on-demand-outside-team** — justifies via P5 (orchestrator's role is decomposition + per-unit dispatch + synthesis; spawning + judging code-tier output is HO's role per the team-context tool restriction) + the validated b35/b36 pattern.

## Cross-references

- **`bugs/teamcreate-orchestrator-restricted-tool-inventory.md`** — the structural restriction this SOP works around.
- **`bugs/teamcreate-team-lead-slot-pre-allocation-collision.md`** — naming pitfall (item 2.3).
- **`bugs/team-sendmessage-system-name-not-personal-name.md`** — routing pitfall (item 4.1).
- **`bugs/sendmessage-without-spawn-dispatch-failure.md`** — orphan-inbox pitfall (items 2.5–2.6).
- **`bugs/sendmessage-body-delivers-summary-preview-only.md`** — parameter-shape pitfall (item 4.2).
- **`lessons/orchestrator-tier-substrate-verification.md`** — pre-dispatch substrate verification (item 1.2).
- **`CLAUDE.md §Chain of justification`** — the bubble-up criteria + DECISIONS propagation discipline (items 1.5 + 5).
- **`sop/standard/plan-first-dispatch.md`** — the orthogonal SOP for what happens after this checklist fires (each implementer's dispatch follows plan-first).
- **`sop/standard/freeze-at-ratification-surface.md`** — mid-arc discipline: when a worker's artifact is surfaced upward for ratification, freeze its branch + cite HEAD-at-package-send (prevents the HEAD-desync class; T-1196).
- **`sop/standard/handoff-restart-runbook.md`** — operationally orthogonal; restart mechanics are session-tier, dispatch is cycle-tier.
- **T-0157 / T-0160 / T-0197** — the SOP-authoring ticket chain this entry closes.
- **b9 / b35 / b36 cycle retrospectives** — empirical lineage; the checklist incorporates lessons from each.
