---
id: ORG-079
title: Persona-stack L1 install-completeness contract — the harness-generic provision list + adapter-qualified verify-invariants
status: "Ratified 2026-08-26 by the operator via Telegram 8632 — direct ratify after read of rev-2 (ac507d611b). Ratify condition the operator named: \"as long as the installer allows for contractual variation\" — met by the L1 (mechanisms, fleet-wide) vs L2 (values, role-specific-via-roles.<role-key>-bindings) partition rev-2 establishes. Consultant round-2 review (T-3111) not consumed — subsumed by direct-ratify. Content is rev-2 (Consultant T-3091 rev-1 blockers applied 2026-08-23 @ ac507d611b: terminology-collision, contract-completeness, values-vs-mechanisms L2 partition, plan-abort trap + per-dimension V-invariant refinements)."
date: 2026-08-23
deciders: [operator, orchestrator]
supersedes: []
extends: [ORG-049, ORG-068, ORG-063]
labels: [org-design, install-lifecycle, harness-controller, org-053, org-068-install-leg]
build_disposition: needs-build
scoping_ticket: T-3030
---

# ORG-079 — Persona-stack L1 install-completeness contract

> **STATUS: RATIFIED 2026-08-26** by the operator via Telegram 8632 — direct ratify after read of rev-2. Ratify condition: the L1 (mechanisms, fleet-wide) vs L2 (values, role-specific-via-`roles.<role-key>`-bindings) partition allows contractual variation per persona while keeping the install mechanism generic; the operator's TG 8632 ("Yeah then its ratified") confirmed the seam meets the gate. Content is rev-2 (Consultant T-3091 rev-1 blockers applied 2026-08-23 @ ac507d611b: terminology-collision, contract-completeness, values-vs-mechanisms L2 partition, plan-abort trap + per-dimension V-invariant refinements). Consultant round-2 review (T-3111) not consumed — subsumed by direct-ratify. Two open dispositions remain (Q3 chronicle-emit-per-supervisor + Q_new V11 rollout pacing) as optional future v2 refinements; neither blocks the v1 ratification.

## Terminology alignment (rev-2 correction, load-bearing)

This ADR uses **T-3029 persona-stack layer** vocabulary (L1/L2/L3 across persona levels). ORG-049 uses a separate **"Layer N"** scheme for connective substrate (Layer 1 = harness-independent connective substrate, Layer 2 = harness runtime, etc.). **The two are distinct axes.** Read `persona-stack L1` (this ADR) as orthogonal to `ORG-049 Layer 1`.

Every persona-stack L1 provision in §L1 provision list below maps to one of ORG-049's Layers (typically Layer 2 = harness runtime, since L1 is per-harness-installer scope). The mapping is explicit in each provision.

## Context

Three post-HC install-lifecycle gaps surfaced on specialist + recovery during the 2026-08-19 → 2026-08-22 window:

1. **specialist** — `mcpServers` empty post-cutover (T-3027 scope).
2. **recovery** — missing source archive + timer convergence marker on dry-run cutover (T-3026 blockers 1+2).
3. **specialist** — chronicle-emit path dead since HC cutover 2026-08-19T23:33Z (no entries for the day's restart events).

Common shape: **the HC installer does not provision things the bespoke supervisor did.** The gap is not "one missing feature"; it's **absence of a contract** for what the harness-generic install layer MUST materialize before a persona-seat is considered installed.

operator-directed via TG msg 8178 (2026-08-21): file an umbrella ADR that defines what persona-stack L1 must provision to fully install a persona.

The 7/7-persona audit sweep (T-3031 specialist + T-3034 orchestrator + T-3033 recovery + T-3035 artisan + T-3036 mason + T-3037 archivist + T-3038 codex) delivered the cross-referenced aggregation matrix at `knowledge/episodic/2026-08-21-l1-install-completeness-sweep/aggregation-matrix.md` (commit `7622d21661`). This ADR codifies the contract the matrix implies.

**Composes with (not duplicates):**
- **ORG-049** (manifest-driven installation) — defined the contract SHAPE (four modes: plan/apply/verify/rollback + Layer-1/Layer-2 connective-substrate partition). This ADR fills persona-stack L1's provision-list + `verify` invariants.
- **ORG-068** (universal system-lifecycle contract) — this ADR is the **Install-leg completion** for the Harness Manager row of ORG-068's coverage matrix. Once ratified + retrofits land, that row's Install cell moves from ◐ → ✓.
- **ORG-063** (harness manager / UID-cordon) — hard prerequisite. V11 below promotes the cordon audit from doc-level prereq to structural install gate.
- **T-3029** (persona-primitive definition, filed) — foundational; this ADR is the persona-stack L1 concrete filling.

## Persona-stack L1/L2 partition — mechanisms vs values

**Persona-stack L1** = the harness-generic install MECHANISM set (identical shape fleet-wide). **Persona-stack L2** = role-specific VALUES + role-only mechanisms.

**Critical distinction (rev-2, per Consultant T-3091 blocker 3):** mechanisms may be L1 (fleet-wide), but VALUES are role-specific. L1 defines **generic evaluators**; the VALUES those evaluators check against come from role/persona bindings in the manifest.

The manifest carries the seam:
- **L1** = `persona_installer.*` (evaluator mechanisms) + `harness_runtime.controller.*` + `harness_runtime.harnesses.<adapter>.*` (per-adapter mechanism specifics)
- **L2** = `roles.<role-key>.*` (role values incl. UID blast-radius, credential_refs, autoMemoryDirectory) + `harness_runtime.roles.<role-key>.*` (role-specific overlays) + `pinned-rules/<persona>.md` (persona-content)

Example (from Consultant): Recovery's direct-sudo means her declared UID blast-radius is role-specific (ORG-063:34-42). L1 provides the cordon-audit **evaluator**; the **declared envelope value** comes from `roles.recovery.uid_cordon.declared_blast_radius` (L2).

The 7/7 audit confirmed the seam holds empirically: role-specific mechanisms (telegram wiring, supervisor mode, companion units) cluster cleanly under `roles.<role-key>`; harness-generic mechanisms (systemd base units, storage roots, session-continuity, HANDOFF, chronicle-emit) are identical shape across all seven personas.

**This ADR defines persona-stack L1 only.** L2 gets its own contract (T-3029 continuation).

## Decision

### 1. Persona-stack L1 provision list — what harness install MUST materialize

Each entry names (a) the generic evaluator/mechanism, (b) its ORG-049 Layer mapping, (c) where the manifest values live, (d) the applicable adapter(s).

**L1.a — Adapter-config skeleton materialization** (largest currently-missing block):

- **Mechanism**: installer materializes a fresh adapter-config skeleton for the seat's declared adapter.
- **ORG-049 Layer**: Layer 2 (harness runtime).
- **Values from**: `harness_runtime.harnesses.<adapter>.config` — per-adapter skeleton-source + hooks-source + skills-baseline + plugin-baseline paths.
- **Applicable adapters**: all; per-adapter specifics differ. For **adapter=claude**: `~/.claude.json` skeleton + `~/.claude/skills/` dir + `~/.claude/plugins/` + `~/.claude/hooks/` + `settings.json`. For **adapter=codex**: `~/.codex/` skeleton + config.toml. For future adapters: their equivalent per manifest.

**L1.b — Systemd substrate materialization + daemon-reload + role-qualified enablement**:

- **Mechanism**: installer writes base template units, runs daemon-reload, enables units per-role (not blanket).
- **ORG-049 Layer**: Layer 2 (harness runtime).
- **Values from**: `harness_runtime.harnesses.<adapter>.systemd` (base template unit paths) + `roles.<role-key>.enabled_units` (per-role enablement list).
- **Applicable adapters**: all seats with a systemd runtime.

**L1.c — Storage roots** under `~/.local/state/pantheon/`:

- **Mechanism**: create-if-absent + ownership + mode check + no-symlink invariant.
- **ORG-049 Layer**: Layer 2 (harness runtime state).
- **Values from**: `persona_installer.state_roots` (paths + owner/group/mode expectations).
- **Applicable adapters**: all.
- **Concrete paths**: `personas/<persona>.json` (session-continuity per ORG-057), `agent-state-history/`, `chronicle/` (post-T-3039), `persona-installer/`, `personas/`. All present-and-writable by seat UID (post-cordon).

**L1.d — Continuity primitives** (present-and-wired, not re-authored by install):

- **Mechanism**: assert binary presence + wire reachability + create initial state files.
- **ORG-049 Layer**: Layer 2 (harness runtime).
- **Values from**: `harness_runtime.controller.chronicle_binary` + `harness_runtime.controller.handoff_seed_path`.
- **Applicable adapters**: all.
- **Concrete**: `chronicle.py` binary reachable + emit-on-restart wired via `handoff_and_restart.sh`; `HANDOFF.md` initial-seed file at known path (create-if-absent, ownership-checked); session-id capture path (`load_session_id` → `personas/<persona>.json`).

**L1.e — Mailbox path** at `/home/the-ancestor/mailbox/<persona>/{cur,new,tmp}`:

- **Mechanism**: canonical mailbox verifier call (per T-1733/T-1824 deny-model specifics) with exact manifest-derived ACL.
- **ORG-049 Layer**: Layer 1 (connective substrate — mailbox is cross-persona).
- **Values from**: `persona_installer.mailbox_root` + `persona_installer.mailbox_acl` (declared ACL per persona).
- **Applicable adapters**: all.

**L1.f — Source-provenance record** (per-install snapshot bound to operator receipt):

- **Mechanism**: installer archives source snapshot + records revision + path hashes bound to an operator receipt.
- **ORG-049 Layer**: Layer 2 (harness runtime state).
- **Values from**: `persona_installer.source_archive.root` (default `~/.local/state/pantheon/persona-installer/`).
- **Applicable adapters**: all.
- **Bindings**: `RUNTIME_SOURCE_HASH_PATHS` (runtime_controller.py:86-101) records runtime provenance; L1 adds install-time snapshot beside it, hash-bound to an operator receipt (not just seat-owned presence).

**L1.g — UID-cordon audit gate** (ORG-063 hard prereq):

- **Mechanism**: cordon-audit evaluator that compares actual seat UID's power against manifest-declared envelope; install refuses if actual exceeds declared.
- **ORG-049 Layer**: Layer 1 (connective substrate — cordon is a cross-cutting security invariant).
- **Values from**: `roles.<role-key>.uid_cordon.declared_blast_radius` (L2; role-specific — Recovery's direct-sudo makes her envelope explicitly wider).
- **Applicable adapters**: all.
- **Not a posture-check** — runs AS AN INSTALL GATE (see §Fail-behavior).

### 2. Persona-stack L2 (out of L1 scope, reviewer sanity)

For clarity — the following live under persona-stack L2, NOT L1:

- Telegram wiring (own-bot vs plugin-channel vs none).
- Bespoke supervisor scripts (`recovery-bot-supervise.sh`, `exo-bot-supervise.sh`, `claude-bot-supervise.sh`).
- Companion units (`acp-bridge@specialist`, archivist ACP bridge, codex-daemon).
- Persona pinned-rules file at `pinned-rules/<persona>.md` (file-based-via-symlink is the canonical pattern; see §Resolved Q1 below).
- Role-specific `harness_runtime.roles.<role-key>` overlays (model, effort, fallback chain).
- Role-specific credential requirements (declared conditionally per `roles.<role-key>.credential_refs`; not universal — see §L1 credential-refs correction below).
- Role-specific `autoMemoryDirectory` path (per ORG-035; declared under `roles.<role-key>.autoMemoryDirectory`, not universal).
- Role-specific declared UID blast-radius (per ORG-063; declared under `roles.<role-key>.uid_cordon`).

### 3. NOT-in-scope carve-outs (explicit, with rationale)

Three mechanisms surfaced in the audit but are intentionally NOT persona-stack L1:

- **Telegram token install** — NOT L1. ORG-051 reference-only contract + operator-reserved credential custody (T-2869) is by design. L1 asserts `credential_refs` presence CONDITIONALLY (per-role, when the role declares credential requirements); L1 never touches the token itself.
- **tmux pane/session creation** — NOT L1. Correctly `runtime_controller`'s job (ORG-020 / ORG-057 runtime-binding). L1 names `runtime_controller` as the owner of first-pane creation but does not take that responsibility. L1 verify DOES include ORG-049 bounded-boot smoke to prove the wired path reaches a working session.
- **Chronicle emit content** — NOT re-authored by L1. The mechanism is present + wired at runtime. L1's assertions: `chronicle.py` binary presence + emit-hook reachability + role-qualified emit-invariant (L1.d + V7).

### 4. Credential-refs correction (rev-2, per Consultant T-3091 blocker 3)

Rev-1 §Manifest §Already-present said L1 "asserts credential_refs presence" — this contradicted §L2's placement of credential_refs as role-specific. **Rev-2 correction**: L1 validates the credential_refs SCHEMA (manifest-field format is correct IF present); the DECISION of whether a role has credential_refs at all is L2, per-role. A role with no external credential dependencies has no credential_refs and this is not a completeness gap.

## Verification contract — adapter-qualified invariants

**Per-invariant applicability**: each V<n> below names the adapter(s) it applies to. Where a seat's adapter is not in the applicable set, the invariant vacuously holds. This is the "adapter/capability-qualified invariant set" Consultant's T-3091 review required (rev-2 fix for the unconditional-Claude-only V1-V4 trap).

L1 install-completeness = ORG-049 `verify()` mode enforces the following. Each maps to an L1.x row above.

| ID | Invariant | Applies to | Verified via |
|---|---|---|---|
| V1 | Adapter-config-root exists + is a mapping (not just present); `mcpServers`-equivalent key exists per adapter (may be `{}` — value governed by L2 / T-3027) | `adapter=claude` (path `~/.claude.json`); other adapters per manifest | Adapter-conditional; JSON schema validation for claude |
| V2 | Adapter skills directory exists (empty legal; NULL not); ownership + mode match manifest | `adapter=claude` (path `~/.claude/skills/`); other adapters per manifest | Adapter-conditional; ownership/mode check |
| V3 | Adapter plugin config skeleton present + exact-paths present + JSON-wiring schema conformance | `adapter=claude` (paths `~/.claude/plugins/{marketplaces,known_marketplaces.json}`); other adapters per manifest | Adapter-conditional; exact-path + JSON-schema |
| V4 | Adapter hooks directory + settings.json present + exact-paths + JSON-wiring schema; contents governed by L2 role overlay | `adapter=claude` (paths `~/.claude/hooks/settings.json`); other adapters per manifest | Adapter-conditional; exact-path + JSON-schema |
| V5 | `~/.local/state/pantheon/personas/<persona>.json` exists + writable by seat UID + valid legacy session-id-capture schema (per ORG-057 legacy `load_session_id` path) + ownership + mode. **V5 is install-time evidence of the legacy session-id capture file; it is NOT full continuity validation.** Full continuity validation (bundle resolution + checkpoint verification + candidate isolation + verification report + switch receipts) is the discipline of `architecture/rfc/RFC-001-persona-continuity-lifecycle.md`, runs at hydration/handoff/switch time (not install time), and produces a `pantheon-persona-continuity-verification/v1` report — a green V5 does NOT imply a green continuity contract. | all adapters | Schema validation + ownership/mode check (install-time only; NOT continuity-verification-report evidence) |
| V6 | `~/.local/state/pantheon/chronicle/` + `agent-state-history/` dirs exist + writable + ownership | all adapters | ownership/mode check |
| V7 | `chronicle.py` importable + `handoff_and_restart.sh` present + executable + configured restart-hook actually reaches the pinned executable (traceable path binding, not just file existence) | all adapters | Import test + shebang + hook-binding trace |
| V8 | Mailbox path present; canonical mailbox verifier call succeeds with exact manifest-derived ACL (not vague "ACL correct") | all adapters | Canonical mailbox-verifier invocation |
| V9 | Per-install source snapshot archived under `persona-installer/<install-id>/`; source revision + path hashes bound to operator receipt (immutable, not seat-owned snapshot presence) | all adapters | Operator-receipt hash-bind |
| V10 | Apply receipt present at `persona_installer.convergence_marker.path`; immutable, hash-bound, created ONLY after post-apply verify succeeds (not a loose marker) | all adapters | Post-verify-only creation + hash-immutability |
| V11 | UID-cordon audit result present + CURRENT PASS for this seat's declared blast radius (not "result present" from stale evidence); binds current UID grants at verify-time | all adapters | Live-uid-grants comparison against `roles.<role-key>.uid_cordon.declared_blast_radius` |
| V12 | `autoMemoryDirectory` (per ORG-035) present + writable + both native harness config binding present AND target path resolves | all adapters | Config-binding trace + path-resolve check |
| V13 (new) | Owned systemd unit content matches repo (file bytes + owner + mode); provenance-bound to install-time source snapshot from V9 | all adapters with systemd runtime | Bytes-diff against snapshot + ownership/mode |
| V14 (new) | daemon-reload run successfully after unit materialization; last-reload-result recorded | all adapters with systemd runtime | Recorded reload-result + timestamp |
| V15 (new) | HANDOFF.md create-if-absent + ownership check | all adapters | ownership + presence |
| V16 (new) | Role-qualified enablement of systemd units (per-role, not blanket); enabled set matches `roles.<role-key>.enabled_units` | all adapters with systemd runtime | systemctl-show + set-compare |
| V17 (new) | persona-installer root ownership + mode + no-symlink invariant | all adapters | ownership/mode + realpath-check |
| V18 (new) | ORG-049:107-111 disabled-first-fire evidence bound to apply receipt for each enabled unit | all adapters with systemd runtime | Apply-receipt cross-check |
| V19 (new) | Adapter-config schema conformance (V1's schema check extended to fully-typed manifest-derived schema, not just JSON-well-formed) | per-adapter | Adapter-specific schema |

**L1 ↔ V mapping** (Consultant T-3091 blocker 2: every L1 provision has at least one V-invariant covering it; every V-invariant traces to an L1 provision):

| L1 provision | Covered by V-invariants |
|---|---|
| L1.a adapter-config skeleton | V1, V2, V3, V4, V19 |
| L1.b systemd substrate + daemon-reload + role-enablement | V13, V14, V16, V18 |
| L1.c storage roots | V5, V6, V17 |
| L1.d continuity primitives | V7, V15 |
| L1.e mailbox | V8 |
| L1.f source-provenance | V9 |
| L1.g UID-cordon audit gate | V11 |

Additionally: V10 covers ORG-049 apply-receipt semantics (not a single L1 provision but the receipt for all provisions completing); V12 covers ORG-035 autoMemoryDirectory (L2-declared, L1-verified).

## Fail-behavior — plan/apply/verify/rollback split (rev-2, per Consultant T-3091 blocker 4)

Rev-1 conflated the ORG-049 four modes and made plan abort on absent postconditions — which would make a clean-seat plan structurally impossible (nothing installed yet ⇒ V<n> absent ⇒ plan aborts before it can propose the install). Rev-2 splits per ORG-049:

- **`plan`**: validates schema + preconditions + reports drift (what's absent vs manifest-declared). **NEVER aborts on absent postconditions** — a clean seat is the trigger for install, not a failure. Plan reports the delta between current-state and manifest-declared state.
- **`apply`**: executes the approved plan.
- **`verify`**: checks V1–V19 as post-conditions. **This is where invariants gate.** Verify runs post-apply; a failed verify triggers rollback.
- **`rollback`**: restores recorded ownership + state per apply receipt (V10 receipt is the rollback anchor).

**Runtime fail-behavior** (post-install, invariant drift detected on a running seat):

- **Default**: monitor-leg alert (ORG-068 Health), not seat shutdown. A live seat with a missing hooks-dir is degraded, not dead; alert routes to steward for repair; Recovery-side wedge-monitor (ORG-063) catches acute cases.
- **V11 EXCEPTION (security-drift response)**: UID cordon is the sole security wall (per ORG-063). Blanket fail-open is unsafe. On V11 runtime failure: **escalate-to-operator-tier immediately + Recovery-audit + optionally auto-tombstone the seat pending review**. This is a security-drift response, not ordinary drift.

**Full-perms plan gate**: any plan that would enable full-perms (per ORG-063 vocabulary) hard-refuses without a fresh V11 PASS bound to the current session. Prior-session PASS is insufficient.

**No install-time grace-window.** A grace-window would recreate the "assume-hand-present" failure mode this ADR is closing.

## Manifest source-of-truth mapping

L1 items map to `persona_installer` + `harness_runtime.controller`/`harnesses` top-level manifest keys. Extensions needed to realize the contract:

**New / clarified manifest fields:**

- `harness_runtime.harnesses.<adapter>.config` (block): `skeleton_source`, `hooks_source`, `skills_baseline`, `plugin_baseline`. **Per-adapter, not universal-Claude.**
- `persona_installer.source_archive.root` — default `~/.local/state/pantheon/persona-installer/`.
- `persona_installer.convergence_marker.path` — the apply-receipt file location (V10).
- `roles.<role-key>.uid_cordon.declared_blast_radius` — per-role declared powers (files / groups / sudo / sockets); L1's cordon-audit evaluator compares against actuals.
- `roles.<role-key>.autoMemoryDirectory` — ORG-035-mandated path, **declared per-role** (not universal-controller); promoted from a manual bringup step.
- `roles.<role-key>.enabled_units` — per-role enabled systemd unit list (V16).
- `roles.<role-key>.credential_refs` — role-conditional credential dependencies (may be absent for a role with no external credentials).
- `roles.<role-key>.pinned_rules_path` — file-based-via-symlink pattern (see §Resolved Q1).

**Already-present fields (contract asserts, does not add):** `mailbox_root`, `state_roots`, `role`, `harness`, `persona`, per-adapter systemd base template paths.

## Resolved questions (rev-2 in-draft, per Consultant T-3091 dimension 7)

**Resolved Q1 (was Q1 skills baseline)** — Provision an adapter-scoped skills DIRECTORY (mechanism at L1); skill CONTENT stays optional and role-bound (L2). L1 mandates `harness_runtime.harnesses.<adapter>.config.skills_baseline` as a manifest field that names the directory path; L1 verifies directory presence + ownership (V2). Whether a specific persona has any skills installed is L2, per-role, per-adapter.

**Resolved Q1' (was Q2 pinned-personality convention)** — **File-based-via-symlink is canonical.** 5-of-7 audit majority already uses this pattern; orchestrator + recovery (the outliers using `~/CLAUDE.md`-hierarchy) retrofit to the file-based pattern via T-3030 sub-ticket. Justifies-via P1 (structural: one shape, not two) + D10 (traversal cost) + majority-alignment. This is a retrofit, not a novel decision.

**Resolved Q4 (was Q4 convergence-marker)** — Apply-mode writes the convergence receipt; verify-mode reads it. Verify success is the trigger for receipt creation (V10). Receipt is hash-bound + immutable post-write.

**Resolved Q5 (was Q5 UID-cordon gate)** — Hard-refuse per ORG-063's "hard prereq" language. Justifies-via P4 (truthful gate — cordon IS the security wall) + ORG-063 explicit disposition. See §Open questions Q3 below for the rollout-pacing choice that stays the operator's.

**Resolved Q6 (was Q6 T-3027 disposition)** — T-3027 stays standalone as the mcpServers-implementation ADR; ORG-079 cross-references it as the umbrella. This is internal ticket organization, not architecture.

**Removed Q2** — Removed from ADR (was 5-of-7 pinned-personality majority argument). Q1' (Resolved above) replaces it with the file-based-via-symlink ratification; there was no genuine architecture question here, only a retrofit decision.

## Open questions for the operator (rev-2 trimmed set)

**Q3 (retained) — Chronicle-emit invariant per supervisor mode.** HC-driven specialist emit-quiet (regression); bespoke recovery emits; sub-orch artisan / archivist empty. Either L1 requires chronicle-emit on ALL restart-events regardless of supervisor mode (add a V-invariant), or HC-driven's emit-quiet is an accepted trait (document in ADR). If required → T-3030 sub-ticket; if accepted → the ADR carries the rationale + amends V7.

**Q_new — V11 runtime-response rollout pacing choice.** The V11 security-drift response (escalate-to-operator + Recovery-audit + optionally auto-tombstone) is architecturally clear. Rollout choice: (a) auto-tombstone on first V11 runtime failure (strictest), (b) escalate + Recovery-audit + auto-tombstone-only-if-review-times-out (default), (c) escalate + Recovery-audit + never-auto-tombstone (most conservative). The operator's disposition binds default rollout shape.

## Retrofit ticket list (post-ratify)

Child tickets to file post-ratify:

| Mechanism | Severity | Sub-ticket shape | Existing ticket |
|---|---|---|---|
| Claude `mcpServers` provisioning | Blocker | Extend `persona_installer` to write `mcpServers` from manifest | **T-3027** |
| `~/.claude.json` skeleton (per-adapter generalization) | Blocker | Seed adapter-config skeleton on fresh install; adapter-conditional (V1) | none |
| Skills baseline directory provisioning | Blocker | `skills_baseline` manifest field + adapter-scoped directory install (V2) | Resolved Q1 |
| Plugin config baseline | Blocker | Seed adapter-plugin scaffold (V3) | none |
| Hooks/settings.json fresh-seed | Blocker | Replace backup-only path with fresh-seed provisioning (V4) | none |
| Source-archive-per-install + operator-receipt bind | Blocker | Per-install snapshot + hash-bind to operator receipt (V9) | **T-3026 blocker-2** |
| Convergence-marker file (apply receipt, hash-bound) | Blocker | Sentinel-file write on apply-success post-verify (V10) | **T-3026 blocker-1** (T-3087 unblocks) |
| UID-cordon audit as install gate + V11 verify-time live-uid-grants | Blocker | Promote cordon audit to install-refuse gate + live-uid rebind | depends on ORG-063 build |
| verify-continuity smoke (V1–V19) | Blocker | Fill ORG-049 `verify` mode | none |
| systemd unit content provenance-bind (V13) | Blocker | Bytes-diff + ownership/mode + snapshot-bind | none |
| daemon-reload result recording (V14) | Blocker | Record reload-result + timestamp | none |
| Role-qualified systemd enablement (V16) | Blocker | Per-role enabled-units manifest + set-compare | none |
| Disabled-first-fire evidence bind (V18) | Blocker | Bind disabled-first-fire evidence to apply receipt | ORG-049 punch-list |
| `autoMemoryDirectory` seeding (per-role) | Blocker | Promote manual bringup step to per-role manifest | ORG-068 punch-list item 3 |
| pinned_rules file-based-via-symlink retrofit (orchestrator + recovery) | Latent | Retrofit outliers to canonical pattern | Resolved Q1' |
| Chronicle-emit invariant per supervisor mode | Latent | Specify emit-contract per supervisor mode | Open Q3 |
| V11 security-drift response implementation | Blocker | Auto-tombstone plumbing + Recovery-audit routing | pending Q_new disposition |

## DECISIONS (rev-2, per Consultant T-3091 proposed set)

1. **Rename ADR-wide vocabulary to persona-stack L1** + explicit ORG-049 Layer-N cross-reference; V-invariants are adapter/capability-qualified — justifies-via P1 (structural: terminology precision prevents readers conflating the two axes) + P6 (traceability: every reader of ORG-079 sees the two-axis distinction upfront).
2. **Convergence marker is the apply receipt** bound to the full verify result (V10) — justifies-via P4 (truthful gate: presence-of-past-pass ≠ current PASS) + D5 (evidence discipline: hash-bound, immutable, post-verify-only).
3. **Cordon envelopes at role/persona binding + refuse full-perms without fresh V11 PASS** (rev-2 upgrade from generic hard-refuse) — justifies-via P1 (structural consistency: cordon evaluator at L1, values at L2) + ORG-063 (cordon is sole security wall, warrants fresh-PASS gate).
4. **Preserve Scheduler policy isolation in schedules/** — ORG-079 does not extend into scheduler policy; convergence receipt (V10) is not a schedule mechanism — justifies-via P5 (org-scope containment) + ORG-064 (schedules stay exclusively in schedules/, per ORG-064:64-67 explicit forbid).
5. **Fix ORG-079:100-vs-:106 credential_refs internal contradiction (rev-1)**: L1 validates credential_refs SCHEMA (if present); role-conditional presence stays L2 — justifies-via P1 (partition-correctness: role decides whether it has external credential dependencies, not L1).

## Re-evaluate

- On new gap surfacing (an install-lifecycle failure that survives V1–V19) — invariant list grows; ADR amends.
- On T-3029 (persona-primitive definition) ratifying — this ADR's L1/L2 partition may need re-anchoring against T-3029's formal primitive shape.
- On ORG-064 (general scheduler) landing — the auto-currency mechanism per ORG-068 update-leg may extend the convergence-marker semantics.
- On UID-cordon fleet rollout (post-ORG-063 build) — V11 semantics may tighten from "audit result present + current PASS" to specific per-domain evidence attestation.
- On a new adapter joining the fleet (beyond claude + codex) — V1-V4 + V19 gain adapter-conditional branches per that adapter's config surface.

## Related

- ORG-049 (manifest-driven installation) — extended; note: uses ORG-049's own Layer-N vocabulary, distinct from persona-stack L1/L2 here.
- ORG-053 §6 (grand-goal lifecycle statement) — origin principle for ORG-068 install-leg.
- ORG-057 (continuity + harness-independent selfhood) — legacy session-id capture (V5, install-time evidence only). **Full continuity validation lives in `architecture/rfc/RFC-001-persona-continuity-lifecycle.md` (T-3175 D2-writer child of T-2417) and runs at hydration/handoff/switch time, not install time.** V5's install-time schema check is necessary but NOT sufficient for continuity readiness.
- ORG-063 (UID-cordon / harness manager) — hard prereq (V11); rev-2 upgrades cordon values to role-binding.
- ORG-064 (general scheduler) — decisions §4 preserves scheduler policy isolation.
- ORG-068 (universal system-lifecycle contract) — this ADR is its Harness-Manager Install-leg realization.
- ORG-035 (autoMemoryDirectory) — V12 invariant; rev-2 makes path role-declared.
- T-3027 (mcpServers-specific ADR, narrower same-class) — kept standalone per Resolved Q6.
- T-3029 (persona-primitive definition, foundational).
- T-3030 (this scoping-ticket, umbrella arc).
- T-3026 (recovery-cutover, blocked on convergence-marker retrofit).
- T-3039 (graveyard → chronicle rename, merged 2026-08-23) — updates V6 + L1.c reference to `chronicle/`.
- T-3091 (Consultant ORG-041 spec-review) — rev-1 BLOCK-WITH-REASON; rev-2 applies all 4 blockers + per-dimension refinements.
- Audit deliverables: `knowledge/episodic/2026-08-21-l1-install-completeness-sweep/{report.md,aggregation-matrix.md}`.
- Revision-prep notes: `notes/2026-08-23-org-079-revision-prep-from-consultant-review.md`.
