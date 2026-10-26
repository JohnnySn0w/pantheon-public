# Cross-account ops discipline

**Status:** Ratified 2026-05-11 (T-0197 #3 of 4; the operator msg_1261 batch ratification). Authoritative for operator/HO citation.

> **Note — topology since ratification (2026-10-16, Orchestrator, T-5060).** The "three-account topology" below is historical. Today the agent seats are `claude` (Orchestrator), `claude-fallback` (Recovery), `claude-research` (Specialist), `suborch1` (mason), `suborch2` (artisan), `exocortex` (Archivist), `codex` (Consultant) and `codex-fallback` (Watchman); `the-ancestor` and `post-office` are substrate accounts. Recovery's repair sudo now reaches every one of them (`sudo -l -U claude-fallback`, verified 2026-10-16). Two statements below no longer hold: Orchestrator has had full root sudo since the operator's TG 11543 (deliberately unhardened), and Watchman holds a one-way rescue grant into Recovery (`56-codex-fallback-rescues-recovery`) plus the scoped `codex-recovery` broker. Recovery stays the default owner of cross-account work; the discipline in this SOP applies to whichever seat does it.

This SOP codifies the discipline for cross-unix-account file operations within Pantheon's three-account topology (Orchestrator at `claude`, Specialist at `claude-research`, Recovery at `claude-fallback`). Recovery holds asymmetric repair-sudo capabilities into the other two accounts; this is the structural primitive that makes inter-agent ops possible without breaking each agent's session-isolation. The SOP names what's allowed, by whom, and the audit-chain discipline that holds across sudo boundaries.

## When to use this

This SOP applies whenever an action needs to land in a unix account that is NOT the actor's own. The triggers:

1. **Recovery is dispatched a ticket targeting another agent's account** (most common: HO files Recovery-target ticket whose deliverable lands in `/home/claude/...` or `/home/claude-research/...`).
2. **Recovery proactively repairs another agent's account** (e.g., MCP eviction recovery, settings.json corruption fix, systemd unit re-enable). Often paired with a heads-up ticket post-action.
3. **Cross-account file deploy** (e.g., kino-pwa rsync from `/home/claude/pantheon-pwa/` to `/opt/pantheon-pwa/`; same pattern for any service-installed artifact).
4. **Cross-account systemd / lifecycle ops** (start, stop, restart of services owned by another agent's account; KINO_SVC Cmnd_Alias is the canonical sudoers expansion for kino lifecycle).

This SOP does NOT apply when:

- An agent operates within their own home directory + own systemd units (single-account ops; standard discipline).
- Cross-account *ticket comms* that don't actually touch the other account's filesystem (filing a recovery-target ticket from HO is single-account; Recovery picks it up in their own session). See [`inter-agent-comms-via-tickets.md`](inter-agent-comms-via-tickets.md) for that pattern.
- Read-only cross-account inspection (Recovery grep'ing Orchestrator's transcript JSONL for context-fill monitor — that's read-only via filesystem permissions, no sudo needed).

## Procedure

### 1. Confirm the right actor

Cross-account ops belong to **Recovery by default** because Recovery holds asymmetric repair-sudo into both Orchestrator's and Specialist's accounts. Orchestrator and Specialist do NOT have sudo into each other's accounts (the asymmetry is structural — Recovery bridges, the other two stay isolated). Procedure:

- If the work is in another agent's account: **route to Recovery via a `role:head_system_orchestrator` ticket** (or retarget an existing ticket via `tools/ticket.py retarget T-NNNN --to role:head_system_orchestrator`). Seat names are refused as targets (ORG-088 amendment 2026-10-19).
- If the work is in your own account: standard single-account ops; no SOP needed.
- If you're Recovery: proceed per steps 2-5 below.

### 2. Confirm the sudo capability matches the work

Recovery's sudoers configuration (per `/etc/sudoers.d/pantheon*` fragments — admin-readable only; the operator owns the configuration). Currently codified capabilities:

- **Repair-sudo into `/home/claude`** (Orchestrator's account): file edits, settings repair, MCP recovery, tmux send-keys, systemd ops on Orchestrator's user-scope units.
- **Repair-sudo into `/home/claude-research`** (Specialist's account): same capability shape; per T-0145 / T-0146 lineage.
- **`KINO_SVC` Cmnd_Alias** (per 2026-05-10 sudoers expansion): standing NOPASSWD for kino lifecycle ops (`systemctl restart kino`, `systemctl status kino`, `journalctl -u kino`, etc.).
- **Specific NOPASSWD entries** for routine repair patterns (e.g., `tmux send-keys` for pane recovery, ` cat ~/.local/state/pantheon/*.json` for state inspection).

If the work needs a sudo capability NOT in the codified list, Recovery HALTS and surfaces to the operator — sudo expansion is an operator-tier ratification (cost-or-secret-bearing per D3; expanding sudo trust is policy-touching).

### 3. Execute as the right principal

Two canonical patterns:

- **`sudo -u claude <cmd>`** — Recovery executes a command AS Orchestrator (the command runs with Orchestrator's UID + Orchestrator's environment).
- **`sudo -u claude-research <cmd>`** — Recovery executes a command AS Specialist.
- **`sudo systemctl ...`** — for systemd ops where the unit is system-scope OR where the KINO_SVC alias applies.
- **`sudo cat /etc/sudoers.d/foo`** — for inspection of admin-protected config.

Use the principal-sudo pattern for *file operations + agent-context work*; use system-sudo only for system-scope ops (systemd, /etc, etc.).

### 4. Audit-chain discipline

Cross-account work doesn't bypass the audit chain. Recovery records actions in pantheon-config's ledger as `agent: "head_system_orchestrator"`, `executor: "head-system-orchestrator"` (ORG-001 role ids, 2026-10-08), regardless of which account the action affected. The ledger entry's `rationale` names:
- The affected account(s).
- The sudo capability invoked (per the codified list).
- The structural fix that authorized the action (e.g., "per T-0145 sudoers fragment" or "per KINO_SVC alias").
- The trigger (which ticket, which Telegram msg, which monitor signal).

If the cross-account work touched another agent's pantheon-config audit chain (e.g., ledger entry on Orchestrator's behalf — rare; usually Recovery's own ledger entry suffices), the cross-account ledger entry is co-authored with the affected agent's role-id explicit.

### 5. Heads-up where appropriate

For substantive cross-account changes (deploy, repair, lifecycle), Recovery files a heads-up ticket OR Telegram message to the affected agent so they don't get surprised by drift. Examples:

- T-0184 / T-0186 pattern: HO files Recovery-target ticket; Recovery implements + heads-up files (T-0186) so HO knows the change landed.
- Routine ops (groom-cycle systemd restart, MCP recovery) — heads-up not needed; ledger entry is sufficient.
- One-off invasive changes (settings.json edit, /etc config) — heads-up always; the affected agent should be able to see the change in their next session start.

## Common failure modes

- **Wrong actor (Orchestrator or Specialist tries to do their own cross-account work).** Symptom: `permission denied` on the file write. Mitigation: route the work to Recovery via a `role:head_system_orchestrator` ticket; don't attempt to elevate from a non-Recovery account.
- **Sudo capability not yet codified.** Symptom: `sudo: a password is required` (the action falls through to interactive password). Mitigation: HALT, file a sudoers-expansion ticket targeting the operator, do not proceed via interactive sudo (the operator can't be the human-in-the-loop for routine ops).
- **Cross-account file mode drift.** Symptom: Recovery writes a file as `claude:claude` ownership when the affected agent expected `claude-research:claude-research`. Mitigation: use the principal-sudo pattern (`sudo -u <agent> <cmd>`) so the file is written with the right uid:gid; verify with `ls -l` post-write.
- **Ledger entry attribution drift.** Symptom: Recovery's cross-account work shows up in pantheon-config's ledger under another seat's `agent` (because Recovery forgot to set its own). Mitigation: ledger entries always carry Recovery's own role id (`head_system_orchestrator`) and seat slot (`head-system-orchestrator`); the affected account is named in `rationale`, not in `agent`.
- **Asymmetric capability assumption** (operator assumes Orchestrator or Specialist can also Recovery-style sudo). Symptom: cross-account ticket targeted to Orchestrator or Specialist that needs Recovery's sudo. Mitigation: per-account capabilities are listed in `notes/account-topology` (or per-account memory pins); route to Recovery when the action requires sudo bridging.

## Boundary

- **What this SOP does NOT cover:** intra-account ops (each agent's own home directory + own systemd units — standard discipline; no SOP needed); ticket-comms-without-sudo (see [`inter-agent-comms-via-tickets.md`](inter-agent-comms-via-tickets.md)); read-only cross-account inspection (filesystem permissions handle that).
- **What this SOP does NOT replace:** the sudoers configuration itself (admin-tier; the operator owns); per-account memory pins describing each account's capabilities (those are reference material, not procedure).
- **Future structural work:** the `claim` subcommand on `tools/ticket.py` doesn't currently expose a `--target` flag for retargeting (per `inter-agent-comms-via-tickets.md`); the `tools/ticket.py retarget` command landed via T-0181 to fill that gap. Future expansion: structured cross-account audit trail (e.g., a recovery-actions log under `~/.local/state/pantheon/recovery-cross-account.log`) — not yet authored.

## Cross-references

- **Principles + directives:** P5 (roles don't hop — Recovery's repair-sudo is the structural primitive that lets the role-bridging happen cleanly without role-conflation); D2 (hand-curated docs immutable to workers — sudoers config IS the architecture-vault for this discipline); D8 (HO self-discipline — pre-commit cross-check applies to Recovery's cross-account ops too); D10 (context-frugality — single SOP covers the discipline rather than scattering per-pattern docs).
- **Policies:** `rulebook/policies/trust-posture.md` (auto-approve scope); `rulebook/policies/autonomy.md` (auto-consumer interaction).
- **ADRs:** `architecture/adr/ORG-002` (principles/directives split); future ADR-on-account-topology candidate (the three-account topology itself isn't ADR-tier yet but may warrant one if the topology becomes more elaborate).
- **Sibling SOPs (standard):** [`agent-role-map.md`](agent-role-map.md) (persona ↔ role-id mapping — context for the actor + audit-chain attribution); [`team-orchestrator-brief-construction.md`](team-orchestrator-brief-construction.md) (brief construction; cross-account dispatches use the same brief shape with target=role:head_system_orchestrator / target=role:head_research_orchestrator); [`shared-repo-multi-writer-permissions.md`](shared-repo-multi-writer-permissions.md) (the DIFFERENT axis: many uids → one shared-group repo; credential + umask propagation — distinct from this SOP's Recovery-sudo-bridge).
- **Sibling SOPs (remediation):** none directly; if a cross-account-ops failure pattern recurs, file a remediation SOP.
- **Notes:** [`inter-agent-comms-via-tickets.md`](inter-agent-comms-via-tickets.md) (the ticket-comms pattern that complements this SOP — comms ARE intra-account on each side; sudo bridges only when actual filesystem cross-account write is needed).
- **Tickets:** T-0077 (HANDOFF.md auto-approve via Recovery sudo — early canonical case); T-0145 (Specialist inbox-consumer install via Recovery sudo — multi-step cross-account install precedent); T-0146 (Recovery groom-cycle migration on claude-fallback — same-account but informs the discipline); T-0184 (auto-restart-on-decay — Recovery fires Orchestrator restart via sudo); T-0192 (kino-pwa deploy + KINO_SVC sudoers expansion — most recent precedent); T-0156 (structural prevention — adjacent area); T-0181 (retarget subcommand — supporting tooling); T-0197 (this ticket — 4 standard-bucket SOPs).
- **Telegram refs:** msg_811 + msg_813 (T-0077 routing correction — "when Recovery needs to do something, ticket is assigned to him directly"); msg_1116 (canonical cross-account routing principle); msg_876 (T-0160 ratification of standard-bucket extension); msg_1218 + msg_1220 + msg_1223 (per-SOP ratification cycle establishing the convention this draft follows).
