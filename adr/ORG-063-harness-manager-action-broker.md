---
aliases: [ORG-063]
title: Full-perms harness posture — the UID/OS boundary is the security cordon
status: "DRAFT (rewrite 2026-08-11) — awaiting the operator ratification. Rewrites the 2026-08-07 bundled draft down to its one load-bearing decision (the security posture); the components it bundled already have homes (ORG-052 / ORG-031 / ORG-050 / ORG-064)."
date: 2026-08-07
rewritten: 2026-08-11
author: Orchestrator (Orchestrator)
relates: ORG-052, ORG-031, ORG-050, ORG-051, ORG-064, ORG-049
build_disposition: needs-build
scoping_ticket: T-2653
---
# ORG-063 — Full-perms harness posture: the UID/OS boundary is the security cordon

**Status:** **DRAFT (rewrite 2026-08-11) — awaiting the operator's ratification.** This rewrites the prior bundled draft (Autonomous, 2026-08-07) down to its one genuinely-new decision — the security posture — per the operator's steer (TG 7608–7618, 2026-08-11) that the bundle was really *two-to-three already-homed components plus one new posture*, so bundling them fought its own modular ruling. Direction: the operator TG 7226–7255 (the posture + rulings) + TG 7608–7618 (the de-bundling + the repair-tier and codex-sandbox refinements). **HITL: Autonomous draft — the write-up is agent-authored; the decision is the operator's, awaiting his ratify.**

## Intent

Canonicalize **one** security posture for running persona seats: stop gating power at the harness permission-prompt, make the UNIX **UID/OS** the real boundary, and route brokered sensitive host actions through the Action Broker — so seats never wedge on a permission prompt, and the security boundary is explicit, deterministic, and auditable. This serves P1 (the boundary is a deliberate OS artifact, not a per-harness allowlist kept in sync by hand) and the D1-adjacent operational goal of unwedging the fleet.

## Context

Surfaced by the T-2633 specialist control-plane cutover: specialist reached full bringup but **wedged on a Telegram-reply permission prompt** — the harness launched under `--permission-mode default` and its allow-list path diverged from what the bespoke launch loaded. The operator's framing (TG 7226): each harness handles perms differently (config file vs CLI flags), and a harness wedges on any perm not granted via its **pre-daemon-start** method. The operator then named the cleaner path (TG 7227/7230 + the loginasroot.net root-login FAQ he circulated): **flip full-perms flags on all units and rely on user-level (UID) restrictions to cordon actual power.** The load-bearing idea: in-harness permission prompts are the cargo-culted layer; the real boundary is the UNIX user — what powerful resource sits behind a UID — and it must be built deliberately.

## The decision — one posture, four layers

**1. Harness runs unblocked (per-harness).** Every seat launches with its harness's no-prompt/full-auto setting so it never wedges. This is a **per-harness mapping, not a single flag** — a thin adapter (the ORG-031 `harness_adapter`) maps a neutral "full-auto" intent onto each harness's actual controls:
  - **Claude:** `--permission-mode bypassPermissions`.
  - **Codex:** `approval_policy: never` — **and note codex additionally runs its own OS sandbox** (`sandbox_mode: workspace-write`, bubblewrap-enforced) that restricts writes to enumerated `writable_roots` and gates network **regardless of the approval setting**. That sandbox is an **added layer we keep**, not something the unblock removes; codex separates approval (when it prompts) from sandbox (what the OS allows), plus per-MCP-tool approval and hook-trust — so "unblock codex" is several settings, not one switch. (Verified vs `tools/harness_adapter/codex.py` + `knowledge/watch/reports/2026-07-07/codex-cli-sandbox-profiles-spec.md` + `codex doctor`.)

  **Load-bearing distinction (the operator, TG 7614–7615):** unblocking the harness drops the harness **prompt** layer ONLY. It does **not** grant OS power. A fully-unblocked seat can still only do what its UID is granted — the flag stops the prompting; the UID is still the wall.

**2. The UID/OS grant is the real cordon.** Once the harness stops gating, the UNIX user is the boundary: files, group membership, sudo, reachable sockets. This must be built deliberately and audited (see §UID-cordon audit) — it is the ONLY boundary once the prompts are gone.

**3. The broker gates brokered sensitive ops (normal seats).** Normal seats hold minimal OS power and route real host actions through the **Action Broker (ORG-052)** — a single deterministic privileged UID behind a privilege-free MCP adapter over system D-Bus (no model session, no per-seat bearer secret, no network listener). Sensitive host capability lives in the broker's auditable code, not spread across daemon-user grants.

**4. The watch/repair tier is the exception — standalone, broker-independent privilege (the operator, TG 7614).** Repair personas (Recovery) hold **asymmetric direct sudo that does NOT route through the broker.** Rationale: **the recovery tier cannot depend on the thing it recovers** — a broker-as-repair-dependency makes the broker a single point of failure for recovery. The repair tier sits **above** the broker, not behind it. This is the concrete content of the posture's "few exceptions": the exception set is the watch/repair tier, deliberately broker-independent, not a vague carve-out. (ORG-052 already leans this way — it calls host-broker ops an "exceptional recovery surface" — but the sharper point is that the *meta*-recovery tier can't sit behind the broker at all.)

## The UID-cordon audit — the load-bearing prerequisite

Once the harness stops gating, the UID cordon is the ONLY boundary, so it must actually be tight. Today it is **not audited**: daemon users share groups (`pantheon-shared` / `pantheon-mailbox` / `veneration`) and some hold sudo. Flipping full-perms without this audit = a full-auto agent on a loose boundary. The work: audit each daemon user's actual power, define the intended per-seat blast radius, tighten each UID to match. **This is where "cordon off actual power" actually happens, and it is a HARD prerequisite** — not a follow-up — before full-perms becomes fleet-wide.

Sequenced (the operator, TG 7237): **specialist is the pilot** cordon-audit (the seat mid-cutover). Flag home tracks cordon maturity — **per-role flag NOW** (conservative; only flip seats already cordoned) → **harness-manager global-default** (full-auto everywhere, per-seat opt-out) once the cordon is comprehensive.

## What this ADR does NOT specify — the already-homed components

This posture is *implemented by* subsystems that each already have their own ADR. This ADR **references** them; it does not re-specify them (that re-bundling was the prior draft's flaw):

- **Action Broker** → **ORG-052** (harness-neutral host-capability broker with MCP adapters over system D-Bus). Layer 3's "brokers gate sensitive stuff."
- **Harness Manager** — the "how a seat runs" umbrella (`persona_installer`, `runtime_controller`, `persona_bringup`, `harness_adapter`, `instance.yaml`) — is itself an umbrella over subsystems already covered by **ORG-031** (harness-abstraction adapter + generalized daemon-onboarding), **ORG-050** (security/identity contract for installer + bringup), and **ORG-051** (secret & authentication lifecycle). Layer 1's full-auto-flag mapping is a thin addition to the ORG-031 `harness_adapter`; it is not a new subsystem.
- **Scheduler** — the "when things run" isolate — → **ORG-064**. Peeled out of the prior draft's §Scheduler; unaffected by this rewrite.

## the operator's rulings (carried forward — TG 7234 + 7255 + 7614)

1. **specialist: HOLD** the `bypassPermissions` flip until the UID-cordon (specialist pilot) lands. The reply perm currently goes through via interactive approval, which is not durable across a session restart; the durable fix is the cordon-gated flip.
2. **Modular isolates, confirmed** (TG 7255): the Action Broker (ORG-052) and the Harness Manager subsystems are independently-supervised isolates, neither enmeshed in the other — enmeshing = one subsystem holding both seat-lifecycle power and host-capability power = the blast radius ORG-052 exists to collapse.
3. **Full-perms flag home: sequenced** — per-role now → harness-manager global-default once the cordon is comprehensive (ruling 3 above).
4. **Repair tier is broker-independent** (TG 7614): the watch/repair tier keeps standalone asymmetric sudo so recovery never depends on the broker.
5. **Codex sandbox confirmed** (TG 7616–7618): codex's OS sandbox holds regardless of the approval flags; "remove the harness blocks" is the prompt layer only, and is per-harness.

## Consequences

- **Positive:** no harness wedges; one boundary model regardless of harness; directionally the ORG-052 end-state, not a throwaway.
- **Cost/risk:** full-auto agents on the current (un-audited) UID boundary until the cordon audit lands — so the **UID-cordon audit is a hard prerequisite**, not a follow-up, before this becomes fleet-wide policy.
- Recovery gains a runtime **wedge-monitor** backstop (detect a seat stuck on a perm prompt, surface/handle) for anything the pre-start flag misses.

## Alternatives considered

- **Harness-neutral GRANTS rendering** (a manifest allowlist the harness manager renders into each harness's config with parity): retired — it rebuilds the per-harness parity machinery that full-perms + UID-cordon makes unnecessary.
- **Enmeshed broker** (host authority inside the harness manager): rejected (ruling 2) — collapses the two power classes into one blast radius.
- **Keeping this as a 2–3-component bundle** (the prior draft): retired per the operator TG 7608–7618 — the components already have homes; only the posture is new.

## OPEN for the operator — flagged, not assumed

1. **Where do the modular-subsystem invariants + health interface land?** The prior draft's §4b (every modular subsystem must be **health-legible + restart-fixable + stateless-across-sessions**) and §7 (a **uniform D-Bus health-property surface** — `Get` = pull, `PropertiesChanged` = push — feeding a kino "Health" page, T-2660) are real design you co-authored (TG 7236–7244), but they are a **separate "how modules are built + report health" concern**, not the security posture. Proposed disposition: **relocate** them rather than lose them or re-bundle here — candidate home is **ORG-068** (universal system-lifecycle contract), or their own ADR. Your call on where they land. *(Until you decide, they are preserved verbatim in git history at the prior ORG-063 revision.)*
2. **Forward-looking control component** (TG 7255): the future higher-tier "who orchestrates the orchestrators" seat that monitors + manipulates the isolates was acknowledged-and-deferred. It stays deferred; noting it here only so it isn't lost.

## Implementation ticket chain (the operator TG 7237)

- **T-2652** — build the Action Broker (ORG-052 end-state). Head of the chain.
- **T-2653** — unblock all harnesses (full-perms) + standardize user perms. `depends_on T-2652`. *(This ADR's `scoping_ticket`.)*
- **T-2654** — UID-cordon audit + tightening, **specialist pilot** — the near-term stopgap track (not broker-gated); gates specialist's `bypassPermissions` flip.

## Re-evaluate when

- The UID-cordon audit completes — its findings may reshape the posture's stopgap→end-state sequencing.
- The Action Broker (ORG-052) reaches implementation — this posture's stopgap retires into it.
- The operator dispositions the §OPEN-1 modular-invariants/health-interface relocation.
