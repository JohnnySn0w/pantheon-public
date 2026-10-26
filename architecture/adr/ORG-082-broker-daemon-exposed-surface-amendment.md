---
aliases: [ORG-082]
id: ORG-082
title: "Broker daemon exposed-surface amendment — substrate ops off the daemon, same-registry-digest acceptance gate"
status: Ratified 2026-08-26 by the operator via Telegram 8612 as the ORG-080 + ORG-081 + ORG-082 bundle at goal-level authority. Frozen ratify candidate at v15 (commit 845aa519a). Companion to [[ORG-080]] + [[ORG-081]]; amends [[ORG-052]] gate 32 to move substrate operations off the broker daemon (bootstrap in-process root only) and adds gate 32a (same-registry-digest acceptance). Revision history at §Revision history.
date: 2026-08-26
author: Orchestrator (Orchestrator)
steward: orchestrator
supersedes: []
relates: ORG-052, ORG-063, ORG-080, ORG-081
build_disposition: needs-build
scoping_ticket: T-2667
no_registry_row_waiver: "Decision-level ADR amending ORG-052's acceptance surface. Registers no component; the ratified components affected are host-substrate, action-broker, and pantheon-config's bootstrap in-process root path, already registered by ORG-080 F4 and ORG-081 §Home + shape."
---

# ORG-082 — Broker daemon exposed-surface amendment

**Status:** **Ratified 2026-08-26 by the operator via Telegram 8612** as the ORG-080 + ORG-081 + ORG-082 bundle companion ADR. **Authority: goal-level** (per the operator TG 8553 2026-08-26). Frozen ratify candidate at v15 (commit 845aa519a). Amends [[ORG-052]] acceptance gate 32 to move substrate operations off the broker daemon's exposed surface (they run only through the bootstrap in-process root path) and adds a same-registry-digest acceptance gate (32a) that verifies both invocation contexts loaded the same operation-registry revision and the daemon-visible ops are exactly the reviewed runtime subset. See §Revision history for the fold-chain.

**Related:** [[ORG-052]] (harness-neutral host-capability broker — the ADR being amended), [[ORG-063]] (harness-manager / action-broker naming — F2's tri-context isolate model), [[ORG-080]] (germination engine — F2 v2 + D-AD name this amendment), [[ORG-081]] (system registry — the `contract_digest` mechanism the same-registry-digest gate leans on).

## Intent

Preserve the ratified [[ORG-052]] safety posture while making its acceptance surface consistent with the germination-engine's tri-context isolate model. [[ORG-052]] acceptance 32 currently reads as "a disposable host converges and rolls back account/group/ACL/mailbox-directory/system-unit/D-Bus-policy actions exclusively from a registered manifest revision" — with the natural reading that those substrate operations run *through the broker daemon*. [[ORG-080]] F2 tightens that: substrate ops execute only through the bootstrap in-process root path (a supervised operator-run leg), never through the broker daemon's exposed runtime surface. This ADR files that tightening explicitly rather than allowing [[ORG-080]] to call [[ORG-052]] "merely called, not amended" — which Consultant's T-3261 BLOCK-5 review correctly flagged as false.

## Context

[[ORG-052]] ratified 2026-07-31 (the operator TG 450) with gate 32 in the "Privileged bringup slice" section:

> **Gate 32.** A disposable host/VM converges and rolls back account, allowlisted group, mailbox directory/ACL, owner/mode/ACL, linger, system-unit, and D-Bus-policy actions exclusively from a registered manifest revision.
>
> **Gate 36.** Broker plans attempting to modify broker/approval/helper bytes or units, bus ownership rules, root registries/templates, approver policy, human allowlists, or operation-enum definitions are refused.

Gate 36 refuses broker self-amendment; it does NOT refuse substrate ops (account/group/ACL/sudoers/system-unit families) through the broker's runtime surface. The ratified text at [[ORG-052]] §Bootstrap and self-amendment boundary reinforces the ambiguity:

> After bootstrap, typed operations may converge registered seat accounts, seat-specific units, mailbox ACLs, and allowlisted policy fragments.

"Typed operations may converge" left the invocation-context boundary implicit. [[ORG-080]] F2 v2 makes it explicit: those "typed operations" run through the bootstrap in-process root path (a supervised operator-run leg the germination engine composes), not through the broker daemon's runtime exposed methods. The one shared typed-op registry loads in both contexts, but the daemon exposes only a strict runtime subset — substrate ops are structurally absent from its method table.

The genuine tightening: an attacker who compromises the broker daemon's runtime surface cannot converge new seat accounts, drop new sudoers fragments, mutate system-unit policy, or extend allowlisted-group membership. Those capabilities live only in the bootstrap path, which requires a supervised operator-run leg and cannot be triggered by a runtime client.

## Decision

### 1. Amend gate 32's runtime-context clause; gate 32's substrate scope moves to the bootstrap in-process root path

The gate's *evidence* (a disposable host converges and rolls back substrate families exclusively from a registered manifest revision) stands unchanged. The *invocation context* is amended: the disposable host proves convergence through the bootstrap in-process root path, not through the broker daemon's runtime exposed methods. The broker daemon's **loaded operation-family projection** (see §2) structurally excludes every substrate operation family listed below.

**Substrate operation families off the daemon's exposed surface** (covering the full ratified [[ORG-052]] gate 32 family set):

- Unix account creation / retirement / uid mutation (`account.ensure`, `account.retire`). `account.retire` is **retirement, not destructive deletion**: per ratified [[ORG-052]] gate 34 and [[ORG-080]] D-E, retire produces an authoritative retired-account record (uid released for possible re-issuance under policy, home archived per the credential lifecycle) — it does not remove the account entity from the audit chain. The op family is still substrate-class and off the daemon.
- Unix group creation / deletion / membership mutation (`group.ensure`, `group.member`).
- POSIX ACL and owner/mode changes on system-owned paths (`acl.converge`, `owner_mode.set`).
- Sudoers fragment installation / removal / mutation (`sudoers_fragment.ensure`, `sudoers_fragment.retire`).
- systemd system-unit (not user-unit) install / enable / start-policy mutation (`unit.system.install`, `unit.system.enable`).
- D-Bus bus-policy fragment install / removal (`dbus_policy.install`, `dbus_policy.retire`).
- Mailbox-directory creation / removal / mode change (`mailbox_dir.ensure`, `mailbox_dir.retire`) — named in ratified [[ORG-052]] gate 32.
- Linger enable / disable (`linger.enable`, `linger.disable`) — named in ratified [[ORG-052]] gate 32.

These families remain fully realized by the shared typed-op registry; the amendment restricts *which invocation context* can call them. Runtime seat clients cannot; the bootstrap in-process root path can. Any op family that is later added to the substrate class MUST be added to this list in the same commit that adds it to the registry, refused by a CI lint that reads the registry's substrate-class tag.

### 2. New gate — same-registry-digest acceptance

Add [[ORG-052]] acceptance gate **32a** (numbered as an addendum to gate 32 so no existing gate id renumbers, matching [[ORG-068]]'s no-renumber discipline):

> **Gate 32a.** Both invocation contexts (the bootstrap in-process root path and the broker daemon) load the same operation-registry revision. The disposable-host acceptance run captures the SHA-256 digest of the loaded operation-registry blob at each context's load time and refuses if the two digests differ. The **daemon's loaded operation-family dispatch table** (the set of op-family ids the daemon's `Plan` composer will admit into a plan — NOT the public seven-method D-Bus contract from [[ORG-052]] §Decision 4, which is unchanged) is proven to equal the **authoritative expected runtime subset** (see §2a) and every substrate family listed in §1 is proven absent from the projection. A `Plan(seat_id, manifest_revision, idempotency_key)` call against a manifest whose ops include any substrate family is refused by the daemon's plan composer **before any privileged code runs**, with a D-Bus error from the closed versioned namespace (§2 finalized shape below).

**Three corrections against ratified [[ORG-052]]:**

- **The public seven D-Bus methods.** The ratified list per [[ORG-052]] §Decision 4 (lines 280-286) is: **`Capabilities`, `Plan`, `Apply`, `Verify`, `Rollback`, `OperationGet`, `ApprovalGet`**. There is no `Status`, no `Cancel`, no generic `Get`. This ADR does not change the method surface — it inspects a strictly different table (the family dispatch below).
- **The refusal fixture path.** Per [[ORG-052]] §Decision 4 line 282, `Apply` accepts exactly `(plan_handle, plan_digest, idempotency_key, approval_handle)`; per line 281, `Plan(seat_id, manifest_revision, idempotency_key)` accepts no operation list and explicitly resolves the manifest server-side. Substrate refusals therefore surface at **`Plan`** time, not at `Apply`: a client calls `Plan` against an installed/allowlisted manifest revision whose resolved ops include a substrate family, and the daemon's plan composer refuses because the family is not in its loaded runtime dispatch table. The disposable-host acceptance fixture is: install a manifest revision containing a substrate op (e.g. an `account.ensure` op on a fresh seat) into the allowlisted set, invoke `Plan(seat_id=<fresh-seat>, manifest_revision=<that-rev>, idempotency_key=<nonce>)` against the daemon, assert the daemon returns the substrate-refusal D-Bus error below **without** having invoked any typed executor. The bootstrap in-process root path admits the same op successfully (proving the substrate family exists in the shared registry but is filtered out of the daemon's dispatch table).
- **Structured error → D-Bus wire mapping.** Per ratified [[ORG-052]] lines 331-335 ("Policy, precondition, validation, and execution failures use a closed, versioned D-Bus error namespace which the adapter maps to MCP tool execution errors"), refusals MUST use the D-Bus error mechanism. This ADR's three new error names live in the ratified versioned namespace `org.pantheon.HostBroker1.*` (matching the ratified interface's versioning discipline); the JSON payload's `pantheon-broker-error/v1` schema-name is retained as an on-wire schema identifier (that versioning applies to the payload shape, not to the D-Bus error name). Finalized:
  - **Reverse-domain D-Bus error names** (closed set added by this amendment to the ratified `org.pantheon.HostBroker1.*` versioned namespace):
    - `org.pantheon.HostBroker1.SubstrateFamilyNotExposed`
    - `org.pantheon.HostBroker1.RegistryDigestMismatch`
    - `org.pantheon.HostBroker1.RuntimeSubsetMismatch`
  - **D-Bus error payload signature: `s`** (single string, canonical JSON per the closed schema below). The payload's JSON conforms to `pantheon-broker-error/v1` — kept as the on-wire schema name so the adapter mapping can identify + validate it — with fields:
    - `code` (enum, string): `"SUBSTRATE_FAMILY_NOT_EXPOSED"` \| `"REGISTRY_DIGEST_MISMATCH"` \| `"RUNTIME_SUBSET_MISMATCH"` — MUST equal the tail component of the D-Bus error name (`SubstrateFamilyNotExposed` ↔ `SUBSTRATE_FAMILY_NOT_EXPOSED`); adapter validates the correspondence.
    - `family` (string): op-family id. **Required for `SUBSTRATE_FAMILY_NOT_EXPOSED`; forbidden for `REGISTRY_DIGEST_MISMATCH` and `RUNTIME_SUBSET_MISMATCH`.**
    - `amendment` (constant string `"ORG-082"`): always present; identifies the amendment authority.
    - `retry_context` (enum, string): `"bootstrap-in-process-root"` (for substrate refusals — points the operator at the correct invocation path) \| `"not-retryable"` (for registry/subset mismatches — operator must repair the load-time inconsistency, not retry). Always present.
    - `manifest_revision` (string, optional): the manifest revision that produced the refused op, when available. Adapter is permitted to omit if the info is not present at refusal time (e.g. a subset-mismatch caught at daemon startup before any plan runs).
  - **MCP adapter mapping:** the adapter receives the D-Bus error name + payload, maps to an MCP tool execution error whose `code` field is the D-Bus error name's tail component (`SUBSTRATE_FAMILY_NOT_EXPOSED` etc.), and passes the JSON payload verbatim under `structuredContent.error` — no re-encoding, no lossy translation. The adapter refuses to forward a D-Bus error whose payload violates the presence rules above (defense-in-depth: a malformed payload from a compromised daemon is caught at the adapter boundary).
  - Wire schema fixture (`tests/host_control/test_broker_wire_errors.py`) verifies: (a) code ↔ error-name correspondence; (b) `family` presence-rule per code; (c) `retry_context` domain; (d) `amendment` constant; (e) adapter round-trip preserves structured JSON.

The compared surface is the daemon's family dispatch table, not the [[ORG-052]] §Decision 4 seven-method contract; that contract is unchanged.

The gate is CI-testable against the germination-engine's simulated backend ([[ORG-080]] §Build plan (b)) and again on the disposable-host acceptance ([[ORG-052]] gate 32 shape). The digest-equality check is the machine form of "F2's one-registry claim is enforced, not asserted."

### 2a. Authoritative expected runtime subset

The "runtime subset" gate 32a compares equality against is not implicit; it is the **union of `broker_runtime` op-family lists declared by every registered component**, digest-bound and reviewer-approvable at plan time.

**Digest input shape.** [[ORG-080]]'s `compute_contract_digest(module_path: Path) -> str` sole-oracle helper accepts a YAML module whose **top-level `component:` mapping** contains exactly one `contract_digest` field. The wrapper this ADR uses must conform:

```yaml
component:                                    # MUST be `component:` — the exact top-level mapping the sole-oracle helper accepts
  id: expected-runtime-subset                 # a synthetic component id; the wrapper is authored as a well-typed component module
  steward: broker-daemon                      # the broker daemon owns this synthetic component
  version: 1
  contract_digest: "sha256:<hex>"             # sentinel-replaceable per the sole-oracle helper's contract
  broker_runtime: [<sorted-strings>]          # deduplicated, lexicographically sorted union of every registered component's privilege.broker_runtime[]
```

That is the ENTIRE wrapper. The manifest revision that produced the union is recorded separately in the run's transcript (`transcript.expected_runtime_subset.manifest_revision`, alongside the computed digest), NOT inside the digested wrapper. Two contexts with the same broker_runtime union but different manifest revisions therefore compute the SAME digest, matching the projection-comparison semantics gate 32a requires.

The helper walks to the schema-located `contract_digest` field (unambiguous — exactly one occurrence at the top-level `component:` mapping; validator refuses drift), substitutes the fixed sentinel `"SHA256_DIGEST_PLACEHOLDER"`, canonicalizes the entire remaining mapping (including `broker_runtime`), and returns `sha256:<hex>` — the same procedure [[ORG-080]] applies to component contracts. **The `id`, `steward`, `version`, and `broker_runtime` fields are inside the digest bytes**; a change to any of them yields a different digest, which is the correct semantic (an amendment changing the synthetic component's identity is a distinguished input).

**Computation and gate check.**

1. For every registered component whose contract module declares a `privilege.broker_runtime: [<op_families>]` list, that list is collected.
2. The union is deduplicated, sorted lexicographically, written into the `component:` wrapper module above (with the fixed `id`, `steward`, `version` bytes and `contract_digest: "sha256:<placeholder>"`), and the digest is computed by invoking `compute_contract_digest` on that wrapper — call the result the **expected-runtime-subset digest**.
3. Gate 32a requires: (a) the daemon's loaded operation-family projection equals the union set (each family present in the projection and no extras); (b) the digest computed from a wrapper module built from the daemon's projection equals the expected-runtime-subset digest; (c) every family in §1's substrate list is absent from both the projection and the union (a substrate op family appearing in any component's `broker_runtime` is itself a `plan` refusal at component-load time — the amendment applies transitively).

The wrapper module is authored by the germination engine at plan time (deterministic from the manifest-resolved component set), NOT hand-maintained. The engine emits the wrapper into the run's transcript for the auditor and to a per-run scratch path the sole-oracle helper reads; auditors can recompute by regenerating the wrapper from the reviewed manifest revision and invoking the helper.

### 3. Post-bootstrap convergence path is renamed for clarity

[[ORG-052]] §Bootstrap and self-amendment boundary's sentence "After bootstrap, typed operations may converge registered seat accounts, seat-specific units, mailbox ACLs, and allowlisted policy fragments" is amended to name the invocation context:

> After bootstrap, typed operations may converge registered seat accounts, seat-specific units, mailbox ACLs, and allowlisted policy fragments **through the bootstrap in-process root path** (a supervised operator-run leg the germination engine composes; [[ORG-080]] F2). These families do NOT appear on the broker daemon's runtime exposed method table; the daemon exposes only the runtime subset per [[ORG-082]] gate 32a.

The rest of §Bootstrap and self-amendment boundary — including the self-amendment refusal (gate 36's shape) — stands unchanged.

## Consequences

**Positive:**
- The ratified [[ORG-052]] safety posture is preserved (substrate ops still converge from a registered manifest; still roll back; still leave receipts) while the invocation-context boundary becomes explicit and gate-enforced.
- [[ORG-080]] F2's "one shared typed-op registry, two invocation contexts, daemon exposes runtime subset only" claim graduates from prose to acceptance gate.
- An attacker who compromises the broker daemon's runtime surface cannot converge new substrate identities.
- The audit chain becomes honest: [[ORG-080]] no longer needs to claim "[[ORG-052]] merely called, not amended"; the amendment is filed here for explicit ratification.

**Negative:**
- One new acceptance gate must land as part of the [[ORG-052]] test surface. The digest-equality machinery is small (SHA-256 of the loaded op-registry blob at each context) but must be wired into both the simulated backend and the disposable-host acceptance.
- The bootstrap in-process root path becomes the ONLY route for substrate convergence — a legitimate operator emergency that needs a substrate change at runtime now requires the operator-run leg, not a daemon call. This is the intended posture; the trade is deliberate.

**Deferred:**
- Whether individual substrate operations gain their own per-family enable-flags on the bootstrap path (per [[ORG-052]] Unit 8's per-capability decision pattern) or converge as one family bundle. Lean: per-family, matching Unit 8.
- Whether the same-registry-digest gate extends to the credential broker's operation registry ([[ORG-051]]) or stays scoped to the host-capability broker. Not for this ADR — flagged when [[ORG-051]] adopts the germination-engine contract.

## Build plan

Under [[T-2667]] tranche (b)-(c), sequenced with [[ORG-080]]'s engine skeleton + host-substrate component. The step sequence (digest capture in both load paths, disposable-host acceptance extension, structural method-table exclusion) is in `notes/2026-10-08-org-082-build-plan-relocated.md`.

## DECISIONS

*Letters (D-A, D-B, …) are this ADR's local decisions; the rulebook's D1, D2, … in `rulebook/directives.md` do not collide; letters do not collide with [[ORG-080]] / [[ORG-081]]'s local D-* series.*

- **D-A. File the amendment as a companion ADR rather than in-lining it in [[ORG-080]] F2.** [[ORG-080]] F2's claim that [[ORG-052]] is "merely called, not amended" was false; Consultant T-3261 BLOCK-5 correctly refused. Filing an explicit amendment ADR is the honest audit-chain move: the operator ratifies the tightening in its own line item rather than approving a bundle whose F2 prose quietly restricts a ratified surface. **justified-via P4 (audit-chain traceability), [[ORG-052]] ratified surface, Consultant T-3261 BLOCK-5.**
- **D-B. Substrate families are structurally absent from the daemon's method table, not filtered at call time.** Structural method-table exclusion removes substrate capability from the daemon's declared invocation surface and eliminates that dispatcher route; a call-time filter would be evadable by any code path that skips the filter. It does not by itself prove a fully-compromised daemon cannot reach shared privileged implementation through another internal channel — a shared library, a helper's method table — see R3 for the follow-on gate that would close that surface. **justified-via P1 (structural over procedural), [[ORG-063]] ruling 2 (isolates), [[ORG-052]] gate 36's shape (self-amendment refusal is structural).**
- **D-C. Gate 32a is a new gate id (not a renumber of gate 32); gate 32's evidence stands, its invocation-context clause is amended.** No existing [[ORG-052]] gate id changes text or renumbers, matching [[ORG-068]]'s no-renumber discipline for E-ids. **justified-via P4, [[ORG-068]] element-id discipline as precedent.**
- **D-D. Same-registry-digest is SHA-256 of the loaded op-registry blob, captured at each context's load time.** Not a per-op-family digest, not a per-method digest — one digest of the whole registry, equality-checked between contexts. Simpler to test, harder to spoof, matches the "one registry" claim's coarseness. **justified-via P1, [[ORG-081]] D-9's `contract_digest` pattern as precedent.**

## Re-evaluate when

- **R1** — a legitimate runtime need arises for a substrate family the daemon does NOT expose (an emergency seat re-provision that cannot wait for the bootstrap in-process root path). If this recurs, reconsider whether a narrowly-scoped runtime family (e.g. seat-account-only, no group/sudoers) merits daemon exposure with its own approval class per [[ORG-050]].
- **R2** — the SHA-256 digest-equality check surfaces a legitimate case where the two contexts must load different registry revisions (e.g. a rolling upgrade where the daemon carries the new revision before the bootstrap path). Extend the gate with a bounded skew window OR retire the equality check in favor of a per-op-family compatibility manifest.
- **R3** — a compromised daemon successfully invokes a substrate family through a channel this ADR did not consider (a shared library, a helper's method table). Add a family-refusal gate at the point the family attempts execution, not just at the daemon's method-table exclusion.
- **R4** — [[ORG-051]] adopts the germination-engine contract and needs the same digest-equality gate for its operation registry. Extend the gate; do NOT copy it (per [[ORG-081]] D-9's authority-per-field discipline).

## Revision history

The v1-v15 fold arc (all 2026-08-26; Consultant ORG-041 rounds, the operator editorial pass TG 8604, final-review pass TG 8609) is in `notes/2026-10-08-org-082-review-history.md`; each round is a commit on `main`.
