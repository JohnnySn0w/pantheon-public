# B9 team-orchestrator brief — DRAFT (awaiting the operator's review)

**Team name:** `b9-inbox-consumer`
**Lead agent_type:** `project-orchestrator`
**Lead name:** `b9-lead`
**Created by:** Orchestrator (HO), 2026-05-04
**Status:** DRAFT — held back from spawn pending the operator's review per msg_493

---

## Goal

Take the B9 inbox auto-consumer arc (T-0051 + T-0052) from filed tickets to landed-and-acceptance-tested. T-0053 is target=recovery and out of scope for this team — it unblocks naturally when T-0052 lands and Recovery picks it up via her own flow.

## Scope

**In scope:**
- T-0051 (pantheon-bridge `/health` `last_inbound_at` extension; tests; commit; push)
- T-0052 (pantheon-config `tools/inbox_consumer.py` build; HO systemd unit + timer; `rulebook/policies/autonomy.md` operator doc; supervised first-fire prep)
- Plan-first dispatches per ORG-005 v2; reviewer pre-flight before each commit
- Cross-repo: bridge work in `~/pantheon-bridge`, consumer + docs in `~/pantheon-config`
- Ledger entries on each ticket resolve

**Out of scope:**
- T-0053 (Recovery-side install — target=recovery, separate user; orchestrator does not touch)
- T-0042 (the design ticket — stays active until end-to-end acceptance; Orchestrator resolves it after the smoke-fire)
- Any redesign of the locked B9 decisions (see §Locked decisions below)
- Direct Telegram messaging to the operator — escalations go through Orchestrator (HO)

## Locked decisions (do not re-litigate)

These were already ratified by the operator at msg_413 + msg_486 + msg_488. They are settled inputs; the orchestrator implements against them, not around them. If implementation surfaces a reason any of these is *structurally infeasible* (not "a different design would be nicer"), escalate to Orchestrator — Orchestrator surfaces to the operator.

1. **Single unified script** with `--agent <name>` flag (q1).
2. **Selection policy:** FIFO by `created`; filter `target == agent` AND `state == inbox` AND all `depends_on` are `state == done`; tiebreak lower ticket-id; skip tickets with the `interactive-required` label (msg_413).
3. **Cycle cadence:** 10 min via systemd `OnUnitActiveSec` (msg_413).
4. **Per-cycle pull limit:** 1 ticket (msg_413).
5. **Idle gate:** tmux capture-pane signature (`? for shortcuts` line) — skip if mid-turn (msg_413).
6. **Cap-hit interlock (HO only):** skip cycle if `~/.local/state/pantheon/ho_limit_hit.json` exists and `resets_at + 90s` is in the future (msg_413).
7. **Mid-conversation handling:** read `last_inbound_at` from the bridge's `/health` (T-0051 surfaces this); skip cycle if recent inbound `< 5 min` ago (revised down from 20 min per the operator msg_544 2026-05-04). **Not** a flag-file approach (msg_488).
8. **Tmux session targeting:** env-var-configured per agent — `PANTHEON_HO_TMUX_TARGET` for HO, `PANTHEON_RECOVERY_TMUX_TARGET` for Recovery; default to a sensible value but make it overridable (q3).
9. **Inject prompt text (locked verbatim — do not paraphrase or trim):**
   > [inbox-auto-consumer] T-NNNN ('<title>') is in your inbox; target=<agent>. Apply the autonomy bar from rulebook/policies/trust-posture.md "What HO auto-approves" before acting on the body. If the ticket asks for anything outside auto-approve scope, surface to the operator via the Telegram bridge and halt rather than proceeding. Otherwise: claim via ticket.py, work the Ask, resolve when complete. Body follows: <body>.
10. **Off-switch:** `~/.local/state/pantheon/autonomy_off` file as a backup hard-kill (presence = bail). Demoted from primary mechanism per msg_488.
11. **Skip-reason vocab:** `off_switch` / `cap_hit` / `mid_turn` / `recent_telegram` / `empty_inbox` / `dependency_blocked` / `interactive_required` / `tmux_unreachable` (q5/q6 elaboration).
12. **Consecutive-skip alert:** 6 consecutive skips (= 1 hour) → Telegram alert via the bridge (q6).
13. **Log location:** `~/.local/log/pantheon/inbox_consumer.log` (q7).

## Context — read these before dispatching

- `pantheon-config/BACKLOG.md` §B9 + §Update 2026-05-04T14:45Z — full sketch + locked decisions.
- `pantheon-config/tickets/inbox/T-0051.md` — bridge change, prereq.
- `pantheon-config/tickets/inbox/T-0052.md` — main consumer build.
- `pantheon-config/rulebook/policies/trust-posture.md` — autonomy-bar definition; the inject prompt references this at runtime.
- `pantheon-config/rulebook/principles.md` + `rulebook/directives.md` — P1 (architecture over policy), P3 (decisions justify upward), D1 (the operator's bandwidth), D3 (autonomy bar).
- `pantheon-bridge/CLAUDE.md` + `DECISIONS.md` — bridge repo conventions, source-of-truth-hierarchy patterns.
- `pantheon-bridge/bridge/server.py` (or wherever the `/health` handler lives) — extension point for T-0051.
- `pantheon-config/tools/systemd/` — existing unit shapes to mirror (e.g., `pantheon-watch.timer`, `pantheon-ho-resumer.timer`).

## Constraints

- **Chain of justification — propagate the discipline forward.** This is load-bearing. The methodology must be intact at every dispatch boundary, not inherited by reference.
  - You (the project-orchestrator) emit a DECISIONS section justifying-via P/D ids on every material call. Format per worker persona Phase 3: `<choice> — justifies via <principle id or named pattern> — <rationale>`.
  - **You require the same of every implementer + reviewer you dispatch** — explicit in the dispatch brief, not assumed by inheritance. Reviewer pre-flight REBOUNDs missing/unjustified DECISIONS; bake that check into your reviewer briefs.
  - Decisions can be autonomous if they can be justified. The dispatcher (you, for implementers; HO, for you) partitions which decisions are in autonomous scope vs which bubble up. State the partition in each brief.
  - When you report back to HO, the chain must be intact: surface DECISIONS lifted from your workers + your own DECISIONS so HO can lift into the ledger.
  - This is `~/CLAUDE.md` §"Chain of justification — every dispatch carries the discipline forward" + principles P3 + P4 + B5 worker persona requirements.
- **HERMES byte-sequence discipline.** The literal sequence `HERME` + `S.md` (no escape) must NOT appear in any committed source byte (test fixtures, docs, commit messages). Use source-time string concatenation in fixtures; escape with backslash-period in docs. Pre-commit hook + GitHub Action both catch this; author-time grep (`git diff | grep -c 'HERME''S.md'` in shell-concat form) is the first line. See `~/CLAUDE.md` §`HERMES\.md` byte-sequence discipline (in HO's pinned rules) for the full discipline. **Brief every implementer + reviewer + researcher you dispatch on this rule explicitly** — workers tripped on it before (T-0050 implementer, REBOUND round 1).
- **Plan-first dispatches.** Every implementer dispatch produces a plan first; you approve before code. Reviewer pre-flight before commit. ORG-005 v2 trust-posture.
- **Cross-check claims against memory + ledger before audit-chain commits.** Subagent findings can contradict existing memory or ledger entries; falsifiability inventory + observation-vs-inference tag before lifting into ADRs, ledger entries, or ticket transition logs. This applies to your DECISIONS too — not just your workers'.
- **No direct Telegram from the orchestrator.** Telegram is HO's voice. If escalation is needed, message Orchestrator via team SendMessage; Orchestrator routes to the operator. Do NOT call any `mcp__telegram-bridge-stdio__*` tool yourself.
- **Ledger entry per ticket resolve.** Action key: `complete_t<id>_b9_<role>` (e.g., `complete_t0051_b9_bridge_health_extension`). Cross-link to parent T-0042 + the implementation tree. Ledger entry must include `justifies_via` field per ORG-001 schema.
- **Auto-commit discipline.** ticket.py's `resolve` command auto-commits the ticket move. Code commits are orchestrator/implementer authored — narrow scope, descriptive messages, no force-push, no `--no-verify`.
- **Both repos.** pantheon-bridge work for T-0051; pantheon-config work for T-0052. Push each independently.
- **Supervised first-fire posture (T-0052 acceptance).** When T-0052's consumer is installed and the timer is enabled, the first 2-3 cycles run with the operator reachable. You do not unilaterally enable the timer in production — escalate to Orchestrator when ready for the supervised first-fire, Orchestrator coordinates with the operator.

## Decisions partition (autonomous vs bubble-up)

The principle (P3 + P4 + D1): justification lives at the dispatcher↔dispatchee boundary. Each tier absorbs variance at its level via DECISIONS-justifying-via discipline + 3-REBOUND escalation budget. Things only escalate up when the current tier genuinely cannot decide — not for sanity checks, not for routine clarification. Most back-and-forth (implementer ↔ you, your workers ↔ each other) is expected and normal; it stays inside the team.

**You decide autonomously (emit DECISIONS justifying-via P/D, no escalation):**
- Implementation-level shape: function decomposition, internal naming, test layout, error messages, log format details.
- Test coverage choices within the acceptance shape (unit vs integration boundaries, mock surfaces).
- Reviewer-flagged REBOUND fixes that don't change the public shape.
- Routine commit message phrasing within the convention.
- Schema-internal fields not surfaced in the public API (e.g., a private cache field).
- Any tests-pass-but-acceptance-criterion-unclear situation: clarify with the implementer or re-read the locked decisions and §Acceptance for the orchestrator's arc; do not bubble up unless the unclarity is structural (i.e., the locked decisions themselves are silent on something material).
- Internal back-and-forth between your dispatched implementer and reviewer up to the **3-REBOUND escalation budget** on a single ticket. Three rounds is the named limit (B5 reviewer convention).

**You bubble up to HO (SendMessage to Orchestrator) — only these structural items:**
- **Locked-decision deviation:** any of the 13 items in §Locked decisions becomes structurally infeasible during implementation. (HO routes to the operator only if the deviation requires the operator's call — most are HO-resolvable.)
- **Scope-creep beyond the two-ticket boundary:** the work needs to touch files unrelated to T-0051 or T-0052 in either repo.
- **New dependencies:** any new Python package, system package, or service. Affects security review surface and downstream consumers.
- **3-REBOUND escalation budget exhausted on one ticket:** signals the brief is wrong or the implementer is mis-scoped; HO inspects + re-briefs or re-dispatches.
- **Production enable:** any systemd unit going to `enable --now` in production. Supervised-first-fire is HO/the operator's coordination, not yours.

That's the full bubble-up list. Everything else stays at your tier.

## Reporting protocol

- **Internal cycles** (you ↔ your implementer, you ↔ your reviewer): your tier, no surface to HO. The 3-REBOUND budget bounds them.
- **Per-ticket completion:** SendMessage to Orchestrator with the ticket id, commit shas, ledger entry id, lifted DECISIONS from your workers, and any followups discovered during the work.
- **Bubble-up trigger:** one of the five structural items in §Decisions partition above. SendMessage to Orchestrator describing the situation + your lean + the alternative; Orchestrator resolves at HO tier or routes to the operator as warranted.
- **Acceptance gate:** when both T-0051 and T-0052 are landed, SendMessage to Orchestrator: "B9 implementation arc landed; ready for supervised first-fire." Orchestrator runs the smoke-fire with the operator available.

## Acceptance for the orchestrator's arc

The team's work is done when:
- T-0051 is in `done/` (bridge `/health` returns `last_inbound_at` per client, populated by inbound routing, tested).
- T-0052 is in `done/` (consumer script exists, lints, has unit tests + integration tests, systemd unit + timer files exist in `tools/systemd/`, `rulebook/policies/autonomy.md` exists).
- Both repos pushed clean to origin/main.
- One ledger entry per resolved ticket.
- Final SendMessage to Orchestrator: "B9 implementation arc landed; ready for supervised first-fire."

After Orchestrator runs the smoke-fire and ratifies acceptance, Orchestrator shuts down the team via SendMessage (no TeamDelete — removed CC 2.1.178; the implicit team dissolves as members exit).
