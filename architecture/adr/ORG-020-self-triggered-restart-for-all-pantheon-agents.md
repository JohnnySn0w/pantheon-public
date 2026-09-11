---
aliases: [ORG-020]
title: Self-triggered restart mechanism for all Pantheon agents
status: Ratified 2026-05-20
date: 2026-05-20
build_disposition: built
---
# ORG-020 — Self-triggered restart mechanism for all Pantheon agents

**Status:** **Ratified 2026-05-20 by the operator (Telegram msg_1739 + msg_1740, via T-0467).** Drafted 2026-05-19 by Orchestrator (Orchestrator).

**Date authored:** 2026-05-19

**Author:** Orchestrator (Orchestrator) — drafting per T-0309's "HO drafts design (likely ADR-shape since structural)" transition (msg_1422, 2026-05-17 retargeting).

**Related:** ORG-011 (stays-working-under-silence — context-fill-monitor is the P1.a substrate this ADR extends); T-0309 (this ADR's authoring ticket); T-0184 + T-0189 (HO-locked decisions ratifying context_fill_monitor.py); T-0008 (handoff_and_restart.sh wrapper this ADR composes); msg_1225 (the operator's originating ask — "I shouldn't have to fire the restart myself").

**HITL:** Ratified 2026-05-20 by the operator (T-0467).

## Context

The operator msg_1225 (2026-05-13): context elevation required him to run `systemctl --user restart claude-telegram-fallback.service` on the swarm VPS to refresh Recovery. Per D1 (the operator's bandwidth), the operator should not have to fire restarts manually. Plus the bus-error footgun: `systemctl --user` from a non-interactive shell requires `XDG_RUNTIME_DIR=/run/user/$(id -u)` — even the operator step has a substrate-tier gotcha.

**What already works (T-0309 body sketch undervalues this):**

`tools/context_fill_monitor.py` (per T-0184 + T-0189, ratified 2026-04-30) runs as a 5-min systemd-user timer on Recovery (`claude-fallback`). It polls each named agent's session JSONL transcript, computes context-fill %, and fires `tools/handoff_and_restart.sh` against that agent when the 60% threshold crosses. Cross-account access via NOPASSWD-sudo. `AGENT_CONFIG` dict at module top covers `orchestrator` + `specialist`. **Phase 2 ("Orchestrator/Specialist self-restart") is structurally already shipped** for the context-fill trigger.

**Two gaps remain:**

1. **Recovery self-restart.** Recovery isn't in `AGENT_CONFIG` because Recovery is the process running the monitor. A circular-trigger isn't viable (Recovery firing its own systemctl restart from within itself would interrupt the running monitor before the restart fires). Recovery needs a different mechanism: an out-of-process tombstone drained by a peer account or a root timer.
2. **Operator-trigger surface for any agent.** `context_fill_monitor.py` only fires on the 60% threshold; it doesn't expose a "the operator says restart Recovery now" surface. Today msg_1225's "operator wants to fire restart" lacks an operator-friendly path — the operator's option is `ssh swarm && systemctl --user restart ...` with the XDG_RUNTIME_DIR footgun, OR ask HO to do it on his behalf.

This ADR covers both gaps + the bus-error footgun fix.

## Decision

Three-phase rollout. Each phase is structurally independent; phases land as separate implementer cycles.

### Phase 1 — Recovery self-restart via tombstone + peer-drained timer

**Mechanism (Option 2+4 from T-0309 body, refined):**

1. Recovery's `context_fill_monitor.py` extends `AGENT_CONFIG` to include a `"recovery"` entry with the appropriate jsonl_dir + restart_service. The context-fill threshold check runs for Recovery like for Orchestrator/Specialist.
2. When Recovery's context-fill threshold crosses, instead of directly invoking `handoff_and_restart.sh` (which would kill the running monitor process before the restart completes), Recovery writes a **tombstone file** at `~/.local/state/pantheon/restart_requested_recovery.json` containing:
   ```json
   {"requested_at": "<ISO ts>", "agent": "recovery", "reason": "context_fill_threshold|operator|other", "operator_request_id": "<optional>"}
   ```
3. A separate **drain timer** runs on **each agent's own account** (`pantheon-restart-drain.timer`, ~30s cadence) — installed independently on Recovery, Orchestrator, and Specialist. The drain timer is a distinct systemd-user unit from the agent's main service unit, so it survives the restart fire-and-forget. When a tombstone exists locally, the drain timer:
   - Reads the tombstone
   - Fires `handoff_and_restart.sh --service <agent's own service unit>` locally (no cross-account sudo)
   - Removes the tombstone on success

Per-agent-local-drain is the right shape because: (a) each agent runs its drain timer as a separate systemd-user unit from its service unit, so the restart fire doesn't kill the drain process (sidesteps Alternative (A)'s race differently than originally considered — same account, *different unit*); (b) no new sudoers entries are needed; (c) the existing Recovery→Orchestrator + Recovery→Specialist sudo paths (per `context_fill_monitor.py`) are reused for cross-account *tombstone writes* (when Recovery's monitor detects Orchestrator/Specialist threshold-crossing, it sudo-writes a tombstone into the target's state dir; the target's local drain picks it up). The reverse direction (Orchestrator → Recovery, Specialist → Recovery) is not configured in `/etc/sudoers.d/` and is not required.

### Phase 2 — Operator-trigger surface

Operator wants to say "graceful restart Recovery" via Telegram. The shape:

1. New CLI `tools/restart_request.py --agent <orchestrator|specialist|recovery>` drops a tombstone at the same path the Phase 1 drain timer reads.
2. Telegram-side: the existing bridge (`pantheon-bridge.service` on Orchestrator's account, per ORG-004) gains a command handler for `/restart <agent>`. The handler shells out to `tools/restart_request.py --agent <X>`.
3. Drain timer (Phase 1's mechanism) handles the actual restart — the tombstone shape is agnostic to whether it was written by context_fill_monitor or restart_request.py.

The cross-product is clean: 3 agents × 2 trigger types (context-fill / operator) all flow through one tombstone → drain-timer → handoff_and_restart.sh path. No new substrate primitives.

### Phase 3 — Bus-error footgun mitigation

Independent quick-win, lands with Phase 1:

- All Pantheon-tier restart documentation (`sop/standard/auto-resume-operator-runbook.md`, Recovery's HANDOFF.md template, any operator-facing instructions) bundles `XDG_RUNTIME_DIR=/run/user/$(id -u)` inline with `systemctl --user` invocations.
- `handoff_and_restart.sh` already handles this for its own invocation; this phase covers the operator-facing docs that recommend manual systemctl as fallback.

## Forces

- **P1 (architecture over policy)** — tombstone + drain timer is structural; alternatives (signals, sudoers, Recovery-self-systemctl from within the running monitor) all have substrate-tier fragility. The tombstone file is the structural witness; same-account-different-unit drain avoids the in-process-self-restart race without expanding the sudoers surface.
- **D1 (the operator's bandwidth)** — eliminates the manual-restart step the operator named in msg_1225 + the XDG_RUNTIME_DIR footgun for both operator and HO.
- **D7 (decision-flow-execution)** — the drain timer is one new primitive that composes existing pieces (`handoff_and_restart.sh` + sudo + tombstone-as-state). No re-architecture.
- **D10 (context-frugality)** — one mechanism covers all 3 agents × 2 trigger types. Single drain timer, single tombstone shape, single docs surface.
- **P5 (decisions-driven)** — Phase 2 ratified by T-0184 + T-0189 (context_fill_monitor itself); this ADR extends that ratified surface rather than redesigning it.

## Alternatives considered

- **(A) Recovery-self-systemctl from within `context_fill_monitor.py`** — the monitor process itself fires `systemctl --user restart`. **Rejected:** the monitor process is part of the unit being restarted, so the restart kills the firing process before completion → race between SIGTERM delivery and the monitor's exec. (Same-account-*different-unit* drain — option E below — sidesteps this race because the drain timer lives in a distinct systemd-user unit.)
- **(B) Signal-based (SIGUSR1 → ExecStopPost handler in systemd unit)** — Recovery signals itself, systemd unit's ExecStopPost handles graceful shutdown + restart. **Rejected:** ExecStopPost runs after the main process exits, not as a restart driver. Would require Restart=on-failure semantics plus signal-on-success workaround; fragile.
- **(C) Root cron polling all three agents' jsonl + tombstones** — single uniform path, no per-account asymmetry. **Rejected:** introduces a root-tier process where current substrate is per-account systemd-user. Permission-creep risk; harder to audit.
- **(D) Considered originally — tombstone + Orchestrator-drained timer (cross-account drain).** Single drain timer on Orchestrator's account, sudo'ing into Recovery/Specialist to fire their restarts. **Rejected during implementation (T-0309 plan-stage):** the assumed sudoers path (Orchestrator → Recovery) does NOT exist in `/etc/sudoers.d/`; only Recovery → Orchestrator + Recovery → Specialist directions are configured (`fallback-rescues-prime`, `50-claude-fallback-research`). Adding the reverse would expand the sudoers surface for marginal benefit over option (E).
- **(E) Selected — tombstone + per-agent-local-drain.** Each agent runs its own `pantheon-restart-drain.timer` as a separate systemd-user unit from its service unit. Drain reads the agent's local tombstone, fires `handoff_and_restart.sh` locally. No cross-account sudo needed for the drain step. Recovery's existing sudo paths handle cross-account *tombstone writes* (when Recovery's monitor detects Orchestrator/Specialist threshold-crossings). Same-account-different-unit avoids option (A)'s race because the drain unit isn't the unit being restarted. **Composes existing primitives; preserves per-account substrate boundary; one new drain timer (installed per account) + one CLI for Phase 2; no new root processes; no new sudoers entries.**

## Consequences

### New code

- `tools/context_fill_monitor.py`: extend `AGENT_CONFIG` with `"recovery"` entry; replace the in-process `handoff_and_restart.sh` invocation with a tombstone write when `agent == "recovery"`.
- `tools/restart_request.py`: new ~30 LoC CLI that writes the tombstone with explicit `reason: operator|context_fill_threshold|other`.
- `tools/restart_drain.py`: new ~60 LoC drain script. Reads its own account's tombstones from `~/.local/state/pantheon/restart_requested_*.json`, fires `handoff_and_restart.sh --service <its-own-service>` locally per tombstone, removes tombstone on success. No cross-account sudo step in the drain path.
- `tools/systemd/pantheon-restart-drain.{service,timer}`: new systemd-user unit installed independently on EACH agent's account (Recovery, Orchestrator, Specialist), ~30s cadence. The drain unit is distinct from each agent's main service unit (so the restart fire-and-forget survives).

### Tests

Per `sop/standard/testing-depth-and-scope.md`:

- **Cron-script tier REQUIRED**: `tools/test_restart_drain.py` against the T-0259 Layer-3 fixture (tmp tombstone state-dir + mocked sudo subprocess + assertion that handoff_and_restart.sh receives expected args).
- **Integ-same-process REQUIRED**: tombstone write → drain read → mock-restart-fire round-trip.
- **Cross-process RECOMMENDED**: real end-to-end (Recovery tombstone → Orchestrator drain → real Recovery restart) in a supervised-first-fire smoke window.

### Sudoers

Verified via `/etc/sudoers.d/` audit (T-0309 plan-stage): only **Recovery → {Orchestrator, Specialist}** directions are configured (`fallback-rescues-prime` + `50-claude-fallback-research`). The reverse direction (Orchestrator → Recovery, Specialist → Recovery) is NOT configured.

The per-agent-local-drain architecture (option E) requires **no new sudoers entries**. Each agent's drain runs locally and fires `systemctl --user restart <its-own-service>` without sudo. Cross-account tombstone *writes* (Recovery's monitor detecting Orchestrator/Specialist threshold-crossings) reuse the existing Recovery → {Orchestrator, Specialist} paths.

### Telegram bridge

Phase 2's `/restart <agent>` command handler is a small addition to `pantheon-bridge.service`. Optional command-allowlist (`/restart` only from authorized Telegram user_id matching the operator's known ID — already gated for other admin commands per ORG-004).

### Supervised-first-fire

Phase 1 lands behind a feature flag (`PANTHEON_RESTART_DRAIN_ENABLED=1`). First fire happens with the operator watching; verify Recovery tombstone → drain → Recovery restart end-to-end in <60s. If clean, flag flips to default-on; if not, rollback is `unset PANTHEON_RESTART_DRAIN_ENABLED` + remove timer.

## Implementation phases

**Phase 1 (Recovery self-restart via tombstone):**
1. Add `"recovery"` entry to `AGENT_CONFIG` in `context_fill_monitor.py`.
2. Modify monitor to write tombstone instead of directly firing restart when `agent == "recovery"`.
3. Write `tools/restart_drain.py` + systemd-user unit on Orchestrator.
4. Install + supervised-first-fire test.

**Phase 2 (Operator-trigger surface):**
1. Write `tools/restart_request.py` CLI.
2. Add `/restart <agent>` Telegram command handler to bridge.
3. Test with the operator's authorized user_id.

**Phase 3 (Bus-error footgun mitigation):**
1. Sweep `sop/standard/auto-resume-operator-runbook.md` + Recovery's HANDOFF.md template + any operator-facing systemctl recommendations.
2. Add `XDG_RUNTIME_DIR=/run/user/$(id -u)` to every documented systemctl invocation.

Phase 3 can land alongside Phase 1 (~10 LoC of docs sweep).

## Cross-references

- ORG-011 — stays-working-under-silence (the P1.a substrate this extends)
- ORG-015 — ratifiable-document-class allowlist (this ADR's ratification path)
- T-0008 — handoff_and_restart.sh authoring
- T-0184 + T-0189 — context_fill_monitor.py + threshold ratification
- T-0309 — this ADR's authoring ticket
- T-0467 — this ADR's amendment ticket (drain-account correction)
- msg_1225 — the operator's originating ask

---

**Status footer:** Drafted 2026-05-19. Ratified 2026-05-19 by the operator (T-0461). Implementation shipped 2026-05-19 (T-0309, commit `6f6a465`); supervised-first-fire deferred to T-0469.

**Amended 2026-05-20 by Orchestrator (HO) per T-0467:** drain-account claim corrected. The ratified ADR specified "drain timer running on Orchestrator's account" with cross-account sudo into `claude-fallback`. During T-0309 implementation (plan-stage Q2), the implementer surfaced that `/etc/sudoers.d/` does not configure that direction — only Recovery → {Orchestrator, Specialist} is configured. HO autonomously corrected the implementation to **per-agent-local-drain** (each agent runs its own drain timer as a separate systemd-user unit from its service unit; same-account-different-unit sidesteps Alternative (A)'s race without requiring new sudoers entries; see updated §Decision Phase 1, §Forces, §Alternatives §E, §Consequences §Sudoers). The architectural intent of the ratified ADR (tombstone-decoupled restart with no new substrate primitives, no operator-routed sudo work) is preserved; the implementation detail of *where the drain runs* is the substantive change.

Justifies-via: P1 (architecture over policy — same-account-different-unit is structurally clean, no race) + D1 (the operator's bandwidth — no new sudoers entries) + D7 (compose existing primitives — reuses Recovery → {Orchestrator, Specialist} sudo paths for cross-account tombstone writes). Audit trail: ledger entry `2026-05-19T22:27:24Z-001` records the autonomous correction; T-0309 commit `6f6a465` body's DECISIONS section captures the implementation rationale.

**Addendum — race-correction series (ADOPTED). Drafted by Recovery per T-0545 / the operator msg_1857; reviewed + finalized by Orchestrator (HO) 2026-06-16. All three corrections (T-0534, T-0535, T-0763) landed + tested; T-0763 validated live 2026-06-12 (Recovery's own context-fill graceful restart wrote a fresh HANDOFF.md and the fresh session booted from it). Adopted autonomously per msg-3733 — below-goal-level reliability corrections to an already-ratified ADR, all shipped + validated; the operator override-available. DRAFT label dropped (the corrections are reality, not a proposal):**

The 2026-05-20 amendment above claimed same-account-**different-unit** "sidesteps Alternative (A)'s race because the drain unit isn't the unit being restarted." That covered the case it named — the agent's *service* restart doesn't kill the separately-unit'd drain — but the T-0469 supervised first-fire (2026-05-22) surfaced a **second race it did not anticipate**: `restart_drain` fired `handoff_and_restart.sh` as a **child of `drain.service`'s own cgroup**, so stopping/disabling the drain — or the drain `oneshot` simply completing — SIGTERM-killed the in-flight restart before it cycled the service (the 2026-05-22T00:25Z incident left Orchestrator half-restarted: handoff written, service not cycled).

- **Correction (T-0535):** the drain now launches `handoff_and_restart.sh` from a **detached `systemd-run --user --collect --unit=drain-restart-<stem>` transient unit** (its own cgroup), mirroring the on-demand `self_restart.sh` mechanism (T-0531). The restart survives the drain being stopped/disabled or the timer ticking; the tombstone is removed on launch-success (closes a double-fire race). So "different unit" was **necessary but not sufficient** — the *fired restart itself* must run in a detached cgroup, not just the drain timer be a distinct unit.
- **Correction (T-0534):** every drain-fired restart now passes `--skip-ack`, not only `reason=api_health_force`. Every drain restart is a same-account self-restart, so the target can never answer `handoff_and_restart.sh`'s in-session handoff ack-wait (steps 2–4); the `operator` + `context_fill_threshold` reasons were blocking on it (the other half of the T-0469 first-fire failure). `is_force` is retained only for a log-audit suffix.
- **Correction (T-0763, 2026-06-12) — supersedes T-0534's blanket skip-ack:** `--skip-ack` is now gated on the tombstone REASON, not applied to all. The forced path (`reason=api_health_force`) keeps `--skip-ack` (a 400ing session genuinely cannot write a handoff). The GRACEFUL path (`context_fill_threshold` / `operator` / other) runs WITHOUT `--skip-ack`, so `handoff_and_restart.sh` steps 2–4 prompt the *healthy* agent to refresh `HANDOFF.md` (mtime-ack) before the kill. T-0534 over-generalized from the T-0469 ack-wait hang: the real bug it should have fixed was a forced session that *can't* write a handoff, NOT a healthy context-fill restart — and blanket skip-ack reintroduced the original failure (a context-fill restart cutting off the handoff-write, so the fresh session boots from a STALE handoff and is lost; Recovery, 2026-06-03). The agent writes its handoff autonomously via send-keys, so "no interactive operator" never meant "cannot write a handoff." Code landed `restart_drain.py` (commit `ac55f461`); **validated live 2026-06-12** — Recovery's own context-fill graceful restart wrote a fresh `HANDOFF.md` and the fresh session booted from it. Justifies-via: P1 (reason-correct gating; one skip-ack policy was the wrong altitude) + reliability. SOP §capability 3–4 amended to match (same change).

Architectural intent unchanged (tombstone-as-witness + same-account drain, no new substrate primitives); the refinement is *how* the fired restart is isolated. Justifies-via: P1 (the detached transient unit is the structurally-correct isolation; "different unit" under-specified it). Operational doc consequence captured in `sop/standard/adding-restart-capability-to-a-new-agent.md` §capability 3–4 (T-0546). Both code fixes landed + tested (T-0534, T-0535 done 2026-05-22).
