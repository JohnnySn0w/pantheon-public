# ORG-052 host-broker install: the ENABLE RUNBOOK (T-2920)

This runbook takes the DARK ORG-052 host-broker install from "all units merged" to a supervised live first fire. It is the single consolidated checklist for T-2920: every checklist annotation on that ticket is either here or in the carry-forward ledger (section 6). It is orchestrator's to run. Workers never run any live step.

- **Code it drives:**
  - `tools/host_broker_install.py` (U1: the manifest, the plan and the name table; pure);
  - `tools/host_broker_install_apply.py` (U3a: apply, verify and rollback plus the receipt; FIXTURE root only today);
  - `tools/germinate/host_substrate/` (U3b-d: the observe layer, the symlink guard and the ancestor walk);
  - `tools/host_control/` (U2: the daemon entrypoints and config schemas);
  - `tools/host_broker_rehearsal.py` (U4a: the nspawn rehearsal harness and orchestrator's root script);
  - `tools/host_broker_rehearsal_live.py` (U4b: the attested in-machine live layer, nspawn-only).
- **Design (HO Q1, hybrid):** a new tool-owned installer (`tools/host_broker_install.py` plus `tools/host_broker_install_apply.py`) drives the host-substrate families through RootHelper (ORG-082 :134 plus A-4), with additive render-staging and no new family.
- **Dark state:** both `LIVE_ACTUATION_ENABLED` literals (`tools/host_broker_install.py` and `tools/germinate/host_substrate/ports.py`) are False. This runbook never flips either one. Live apply is a separate, reviewed code change (section 2).
- **Scope trims (HO Q12):**
  - gates 37/38 belong to T-2924;
  - plan regeneration on update belongs to T-2922;
  - teardown and account retirement belong to T-2921;
  - healthsig is out of scope (HO Q11, the ORG-050 installer).
- **No real operator-batch value appears in this file.** Batch items are referenced by number only (`MB-1` ... `MB-6`). The rehearsal uses FIXTURE values only (U4a `FIXTURE_LABEL`).

Every phase block in section 3 has the same six fields:
- **Preconditions** (what to verify and how);
- **Commands** (the tools' own CLIs only, never improvised shell);
- **Expect**;
- **HOLD**;
- **Rollback**;
- **Gates** (the ORG-052 gate ids the phase evidences).

---

## 1. Enable-readiness gates

Every box must be ticked before ANY live step in section 3. The order matters: the rehearsals come before any live step.

- [ ] **G-S3: S3 is decided.** This is the GE dark-gate input named in orchestrator's 10-17 ruling.
- [ ] **G-MB: the the operator batch**, relayed by orchestrator as ONE message once U1-U4 are done and a green rehearsal exists (HO ruling). The exception is MB-4, the name ratification, which goes early and alone.
  - [ ] **MB-4 FIRST: name ratification T-5643 is DONE.** This covers the provisional account, bus and path names in `tools/host_broker_install.names.md` (ORG-052 ratification item 12). Renaming after install is a migration.
  - [ ] **MB-1:** a NEW dedicated BotFather bot, with its token handed over through a secure path. Never a persona bot token, because persona bots share the getUpdates queue.
  - [ ] **MB-2:** The operator's Telegram user id as the single allowlisted human, plus /start to the new bot (chat id == user id).
  - [ ] **MB-3:** the grant list plus the approver policy, together:
    - the service accounts, including `pantheon-hostmonitor` (nologin, locked; HO Q5);
    - the bus names and the per-seat executor rights;
    - the mailbox-hub write surface;
    - the ingress egress (AF_INET/AF_INET6 with the residual recorded; HO Q8);
    - who approves which families, seat classes and TTLs, and who revokes.
  - [ ] **MB-5:** the root execution channel for the live enable (sudo, a window, or the operator typing it).
  - [ ] **MB-6 (optional):** MCP adoption confirmation for the two in-house adapters `pantheon-host` and `pantheon-approval`. orchestrator decides whether ORG-052 §7's ratification covers adoption (mason lean: covered; a confirm is cheap insurance). The partner edit to `rulebook/policies/mcp-adoption.md` §2 is orchestrator's, applied when U5 C2 merges (LOW-1 ruling (b)).
- [ ] **G-R1: a GREEN nspawn rehearsal 1 receipt.**
  - `python3 -I tools/host_broker_rehearsal.py --check-evidence <evidence-dir>` exits rc 0.
  - Its receipt carries the gate table (section 5). Every evidenced or partial row is read, and nothing is over-claimed.
  - The rehearsed commit must contain the `_CLAUDE_MCP_ALLOWLIST` rows (U5 C1); without them the real plan refuses on `blocking_preconditions`.
  - CF-3 acl/owner_mode stay PARTIAL in rehearsal 1 (no new verb; Recovery D15 on U4a); G-R2 closes the owner_mode half.
- [ ] **G-R2: rehearsal 2 evidences the owner_mode subject-symlink fault through the REAL binaries** (CF-W8, Recovery D15 on U4a, ticket T-5673). chmod has no -h on Linux and follows a CLI symlink, so this family carries the worst blast radius if the guard regresses. The acl: subject-symlink fault is included too if it is cheap. It needs a narrowly-scoped U4b verb, reviewed by Recovery under D15 when it is built. Rehearsal 1 accepts CF-3 acl/owner_mode as PARTIAL (both pass the same helper symlink_guard, which F1-F3 evidence on the real fs). The enable does NOT proceed on rehearsal 1 alone.
- [ ] **G-LIVE: the operator-bootstrap live change is merged** with every required property (a)-(k) in section 2, including (k), the tool-enforced HOLD stop. **It does not exist yet.**
- [ ] **G-PRE: the pre-enable tickets are closed.**
  - T-5516 (approval/broker import chain), T-5518 (async Present/HumanDecision plus retry), T-5572 (executor imports `tools/runtime_invocation_receipt.py`) and T-5533 (root-registered plan, design (c)) have all landed.
  - T-5644 (the synchronous ApprovalGet on the loop) is decided.
  - T-5597 (framework-digest widening) stays deferred and gated on this ticket.
- [ ] **G-MEMBERSHIP: checked immediately before P10, NOT before P0** (property (j), CF-U5-1). In order: (1) orchestrator's mcp-adoption.md §2 doc row has landed; (2) the ratified membership commit fills the `_CLAUDE_MCP_ALLOWLIST` persona sets; (3) the membership check confirms the intended seats are members. Seats are admitted LAST, after the daemons are healthy (least privilege). P0's `precondition:mcp-allowlist` keeps checking key presence only.
- [ ] **G-FIRE: orchestrator's supervised fire.** The live enable is orchestrator's supervised fire, never a worker's. It is gated on the the operator batch (G-MB), on S3 (G-S3) and on T-5643 (MB-4) on top of the boxes above. orchestrator is present at the terminal for P2-P12, and every HOLD is a stop for his explicit go.

**C5 is NOT a gate** (HO Q3, the C5 decoupling). The operator-run, fixed-argv, receipt-bound root bootstrap is a different trust class from component sandboxing. The T-2920 live enable therefore does not wait on T-3296/T-5106. Engine-composed component paths still keep C5.

---

## 2. Order, and the live change that does not exist yet

1. The rehearsals come BEFORE any live step: rehearsal 1 (G-R1), then rehearsal 2 (G-R2). Both run on a disposable systemd-nspawn machine on this VPS under HO root (HO Q9; the fallback is a free pve2 VM, never a paid one).
2. A rehearsal receipt is NEVER live evidence. Its mode is `rehearsal-nspawn`, and live refuses any non-live receipt (property (e)). The U4b attestation carries `plan_sha256`, and load requires the rebuilt plan's sha to equal it (U4B-Q1).
3. **The rehearsal's one-step P2-P9 is acceptable ONLY because the machine is disposable.** U4a's root script runs P2-P9 as one `run` step, and U3a's apply runs every entry in one `--execute` loop (a `hold` entry is only stamped `hold_acknowledged`). The rehearsal is therefore NOT evidence for property (k); (k) needs its own test in the live change.
4. **Live apply needs the operator-bootstrap live change, which does NOT exist yet.** Today `host_broker_install_apply.py` is fixture-root only (`--root` is required; there is no live mode). The only real-system actuator is U4b, which refuses outside an attested nspawn machine. The live change is a separate unit with its own independent review and its own Recovery D15 review. It MUST have all of the following properties:

| Prop | Required property of the live change | Source |
|---|---|---|
| (a) | A CommandRunner `cwd` parameter that is PASSED THROUGH to subprocess, never swallowed: no `**kwargs` accept-and-ignore, pinned by a test. Today `_organ_lifecycle.SubprocessCommandRunner.run(argv)` has no cwd kwarg, so it fails closed with a TypeError. | Recovery D15 U3a (f), mason 10-27 |
| (b) | An openat walk over the token SOURCE's ancestors (the U3a destination is already an in-process O_EXCL\|O_NOFOLLOW create; the source walk is the live gap). | mason 10-27 live-layer notes |
| (c) | Rollback's chown/install owner and group EQUAL the owner and group recorded for that path in the root-owned receipt's preimage; apply's equal the manifest A/G. The live change keeps the U4b slot binding: `P_SHELL` only at `useradd --shell`, `GUARDED_MODE_BITS` (0o7022) planned-or-preimage only, and setfacl only with the exact planned or preimage ACL. | Recovery D15 U4b code review CF-2 (W2), U4b review F3 |
| (d) | A live `verify` REBUILDS the plan and compares it, rather than trusting a plan file. | mason 10-27 live-layer notes |
| (e) | Live receipts carry mode `"live"`, and the live install, verify and rollback paths REFUSE any receipt whose mode is not `"live"` (a rehearsal or fixture receipt can never serve as live idempotency or rollback evidence). Receipt and journal are pinned (pinned-absent included) and a malformed receipt refuses cleanly, as in U4b. | Recovery D15 U4b design (c) |
| (f) | `LC_ALL=C` on the operator runner (absence detection and the `stat %F` allowlist depend on it). | U3c MED-1, U4 CF-1 |
| (g) | The live chmod argv follows symlinks (no -h on Linux). It is guarded by the U3c/U3d preflight (row-4 no-follow stat, the ancestor walk, and the designated-owner rule), with the residual TOCTOU documented (accepted by Recovery, including intermediate components; same class as the U3c residual). | Recovery D15 U3c, U3d |
| (h) | Built from the `OPERATOR_BOOTSTRAP_ENABLED` / `OperatorBootstrapPort` proposal (U3b), with the U4b `_LiveSubstrateOps` mixin as its base (HostSubstratePort's constructor and dark gate stay byte-identical; exactly two subclasses today). | U3b proposal, Recovery D15 U4b mixin |
| (i) | Recovery D15 review of EVERY new argv, AND of every new external caller of `observe()`, before merge. | D15; Recovery D15 U3b (a new external observe() caller is a D15 change) |
| (j) | The membership check is a SEPARATE gate, G-MEMBERSHIP, run immediately before P10. It is NOT the P0 precondition: U1's `precondition:mcp-allowlist` is a P0 entry gating P4 and keeps checking key presence only, while membership is filled only at P10. G-MEMBERSHIP checks that the INTENDED seats are members of the `pantheon-host` / `pantheon-approval` rows after the doc row and the ratified membership commit. | CF-U5-1 (mason U5 ruling; review MED-2 ruling) |
| (k) | The live verb STOPS at each plan `hold` entry and resumes the SAME receipt only on an explicit operator go (or takes an explicit phase bound). The D3 holds P2, P8 and P9 are tool-enforced, not prose. Today U3a's apply runs every entry in one loop, so without (k) the P8 token would be placed and P9 ingress started before orchestrator reached the P2 HOLD. The explicit phase bound is part of (k): P10's P4-P6 re-run uses it. | review MED-1 ruling (mason), D3 |

---

## 3. Phases P0-P12

The phase ids match U1's plan phases (`host_broker_install.PHASES`: P0, P2-P9) and U4a's harness phases (`host_broker_rehearsal.PHASES`). P1 and P10-P12 are runbook-only. In the rehearsal they are the U4a P1/P10/P11/P12 rows.

Notation:
- `<M>`, `<R>`, `<T>` and `<I>` are the install manifest, seat/manifest registry, tracked-file list and inputs document.
- `<plan>` is P1's plan file.
- `<receipt>` is a FRESH receipt path under `/var/lib/pantheon-install-receipts/` (see the pre-loop note in section 4).
- `LIVE-VERB` marks a step whose live command is the live change's verb (section 2). It does not exist yet, and nothing in this runbook substitutes shell for it. The rehearsal equivalent is the named U4a script step.

### P0: preconditions

- **Preconditions:**
  - G-S3, G-MB, G-R1, G-R2, G-LIVE and G-PRE are all ticked.
  - The checkout is at the reviewed commit.
  - `python3 -I tools/host_broker_install.py names --check` exits 0: the name table matches its generator. It proves generator consistency only; the ratification is T-5643 (still open, G-MB MB-4).
- **Commands:** `python3 -I tools/host_broker_install.py plan --manifest <M> --registry <R> --tracked-files <T> --out <plan>`. The P0 entries are verified by the live verb's observe step (`dpkg-verify-and-record`, `verify-exists-no-symlink-component`, `verify-framework-copy-allowlist-rows`, `verify-schema-digest`).
- **Expect:**
  - `blocking_preconditions == []`.
  - `precondition:mcp-allowlist` is `met` (U5 C1).
  - `packages_verify` (python3-dbus, python3-gi, python3-yaml) are VERIFIED and RECORDED, never installed (HO Q7). The probed PyYAML version is in `PINNED_PYYAML_VERSIONS`, or verify refuses (`verify_time_refusals`).
  - The mailbox hub exists with no symlinked component BEFORE any executor unit is rendered (T-2917 MR-2 carry-in 2, D33).
  - The approval journal SCHEMA_DIGEST equals the v1 baseline (bf169e04...617ff, T-2919 FLAG; never migrated, HO Q10).
  - The broker journal schema digest is recorded.
- **HOLD:** none.
- **Rollback:** **Scope: none.** P0 mutates nothing, so there is nothing to roll back.
- **Gates:** plan: E35; observed: none.

### P1: the dry plan

- **Preconditions:**
  - P0 is green.
  - The plan's closure is from TRACKED files only (U1 MED-2), and the closure is proven by `python3 -I` from the copy.
  - `--tracked-files` comes from the reviewed commit.
- **Commands:**
  - The plan (as in P0), then `python3 -I tools/host_broker_rehearsal.py --plan-digest <plan>` (prints the verified `plan_sha256`).
  - LIVE-VERB dry run. The fixture equivalent is `python3 -I tools/host_broker_install_apply.py apply --plan <plan> --manifest <M> --registry <R> --tracked-files <T> --inputs <I> --root <fixture> --receipt <receipt>` without `--execute`. It prints the ordered actions, mutates nothing and writes no receipt.
- **Expect:**
  - The ordered action list matches the plan.
  - `live_actuation_enabled` is as the live change sets it.
  - P1 takes NO backup: a dry run creates no `<receipt>.d` (the Q10 backups are apply's, P2).
- **HOLD:** none.
- **Rollback:** **Scope: none.** The dry run mutates nothing and writes no receipt.
- **Gates:** plan: none; observed: none (a dry ordered action list cannot evidence gate 35).

### P2: accounts

- **Preconditions:**
  - P1 is green.
  - The MB-3 grant list is ratified.
  - No uid or name collision in `getent passwd` / `group` (U3b observe rows; the shadow is projected to name and expiry only, never a hash).
  - The `operator_uid` collision refusal passes.
- **Commands:** the LIVE-VERB apply with --execute, which STOPS at the P2 hold (property (k)). Rehearsal: U4a script `run`, step P2-P9 in one step, acceptable ONLY because the machine is disposable and not evidence for (k) (section 2).
- **Expect:**
  - The service accounts are created as the manifest names them, with `pantheon-hostmonitor` nologin and locked (HO Q5), and `monitor_uids` hostmonitor-only (U3 carry-forward).
  - The receipt records every account.
  - **The Q10 backups:** at the start of `--execute`, before its first mutation, apply backs up every existing `/var/lib/pantheon-host-*` state dir into `<receipt>.d/backups` (O_EXCL 0600; restore uses tarfile `filter='data'`). They are never migrated (HO Q10; migration is T-2922's update leg). Never take a hand tar.
- **HOLD: HOLD.** D3: userdel orphans state after the daemons write. Tool-enforced: the live verb stops at this hold entry (property (k)). orchestrator confirms the account set against MB-3, then gives the explicit go that resumes the SAME receipt.
- **Rollback:** **Scope: the WHOLE receipt, reverse sequence.** Receipt rollback = the WHOLE receipt, reverse sequence (every mutating entry by descending `seq`), never one phase. **Accounts are sticky** (a residual, per T-2921; rollback never deletes an account once a daemon has written state).
- **Gates:** plan: E35, E19; observed: E32 (account leg).

### P3: the input checkpoint (no plan entries)

U1's `PHASES` lists P3, but no plan entry carries it. The runtime entries sit in P4 after the framework copy, because `build_runtime.py` runs FROM the framework copy (`<fw>`). P3 is therefore a checkpoint with no mutation.

- **Preconditions:**
  - P2 is green.
  - The wheelhouse is lock-pinned (`--require-hashes --no-deps --only-binary=:all: --no-index`; Recovery accepted pinning via the lock hashes as equivalent) and sits OUTSIDE the installer-trusted paths.
  - The root-owned inputs document holds the MB-1/MB-2/MB-3 refs (never in this file).
- **Commands:** `python3 -I tools/host_broker_rehearsal.py --plan-digest <plan>` re-prints the plan sha, which must equal P1's.
- **Expect:**
  - The same `plan_sha256` as P1.
  - The Q10 backups apply took in P2 are present under `<receipt>.d/backups`, one per existing state dir, and the receipt's progress head lists them. Rollback re-backs-up only the dirs apply backed up.
- **HOLD:** none.
- **Rollback:** **Scope: none.** P3 is a checkpoint and mutates nothing.
- **Gates:** plan: none; observed: none.

### P4: the framework copy, then the runtimes

- **Preconditions:** P3 is green. The closure includes:
  - all THREE `FRAMEWORK_CODE_ARTIFACTS` (`tools/persona_installer.py`, `persona_installer_contracts.py`, `persona_installer_activation.py`);
  - `tools/runtime_invocation_receipt.py`;
  - `tools/host_plan_producer.py`;
  - `registry/systems.yaml`;
  - every germinate component dir that `load_checkout_op_registry` reads.
  - The `_CLAUDE_MCP_ALLOWLIST` rows are already in the copied `persona_installer.py` (F3: the allowlist edit precedes the framework copy).
- **Commands:** LIVE-VERB (`dir`, `framework_tree`, `framework_file`, `framework_active_link`, then `runtime`). Each runtime entry runs the Recovery-ruled build line `/usr/bin/env -i LC_ALL=C PATH=/usr/bin:/bin /usr/bin/python3 -I <fw>/tools/host_mcp_adapter/dist/build_runtime.py --root / --wheelhouse <W> --python /usr/bin/python3 --adapter host|approval`, with cwd = the root-owned framework root (recorded in the receipt).
- **Expect:**
  - A versioned root-owned `/usr/local/lib/pantheon/framework/<rev>/`, outside /home (the broker has ProtectHome=yes), serving the daemons and the producer.
  - The `active` symlink is switched atomically, with the previous rev kept (HO Q6). `active` is the installer's pointer only (O_NOFOLLOW loader); daemon configs name the concrete rev.
  - The executor `_source_check` passes against the copy.
  - Both runtimes are root-owned and pass `verify_artifact.py --expect-owner 0`.
  - The runtime hash covers the script, the imported framework modules and the wheelhouse.
  - The root-owned, not g/o-writable stat check (including intermediate dirs) runs right before exec.
  - The venv and pip children run with `-I` (Recovery condition (f)).
  - The receipt records every package, wheel hash and installed executable hash.
- **HOLD:** none.
- **Rollback:** **Scope: the WHOLE receipt, reverse sequence.** Receipt rollback = the WHOLE receipt, reverse sequence (every mutating entry by descending `seq`), never one phase. For P4's entries: `active` goes back to the previous rev, the new rev dir is removed, and a prior runtime's move-aside (recorded before the first mutation) is restored.
- **Gates:** plan: E35, E36, E7; observed: E32a (the copy the startup digest is computed over).

### P5: root configs, registries and CredentialRef

- **Preconditions:** P4 is green. The MB-2 and MB-3 values are in the root-owned inputs document (never in this file, never in a receipt).
- **Commands:** LIVE-VERB (`dir`, `owner_mode`, `launcher`, `config`, `registry`, `registered_manifest`, `credential_ref_registration`).
- **Expect:**
  - The broker and approval configs are rendered from U2's strict schemas (framework_root = the concrete rev), with compensate OFF (HO Q4).
  - The root paths `/etc/pantheon/host-approval/approver-policy.json` and `/etc/pantheon/host-broker/seat-registry.json` are as fixed by the ratified name table. The `/etc/pantheon/*` dirs include the 0700 credentials parent.
  - The ingress config (one user id, chat id == user id, private), a matching `HumanIngressRegistration` and the human-source approver entry.
  - The CredentialRef is registered.
  - The broker host-policy operator-batch ref is resolved.
- **HOLD:** none.
- **Rollback:** **Scope: the WHOLE receipt, reverse sequence.** Receipt rollback = the WHOLE receipt, reverse sequence (every mutating entry by descending `seq`), never one phase. For P5's entries the preimages are saved in `<receipt>.d/saved`.
- **Gates:** plan: E35, E19, E9, E21; observed: E14 (the allowlisted human).

### P6: plan registration (T-5533 steps a-d)

- **Preconditions:**
  - P5 is green.
  - The SETPRIV producer argv is EXACTLY the Recovery-amended template: `--reuid=nobody --regid=nogroup --clear-groups --no-new-privs --inh-caps=-all --bounding-set=-all -- /usr/bin/env -i PATH=/usr/bin:/bin /usr/bin/python3 -I <fw_root>/tools/host_plan_producer.py ...`.
  - The two JSON args equal the plan entry's values.
  - cwd is `producer_cwd` (stat-checked).
- **Commands:** LIVE-VERB (`dir`, `plan_registration`, `registry_post_registration`). The rehearsal probe is the same setpriv prefix reading /proc/self/status (U4a `PRIVDROP_PROBE_ARGV`).
- **Expect:**
  - (a) The unprivileged producer runs from the root-owned framework copy with a clean env. Its output is captured on stdout, with a root digest re-check (U1 MED-1); staging is a fresh per-run nobody:nogroup 0700 dir.
  - (b) Root re-reads the output O_NOFOLLOW (owner, mode, size, and sha against the printed record, plus `validate_digest`), installs the same bytes under a broker trusted root, and appends `{seat_id, manifest_revision, path, sha256}` to the registry's plans list. A swapped output file is refused.
  - (c) The broker loads its registry with `framework_root=<framework copy>` (Plan() refuses without it).
  - (d) This step is re-run on EVERY framework-copy update, not only when `framework_sha256` changes (T-5597).
  - The producer child is observed once with `CapBnd == 0` and `NoNewPrivs == 1`.
- **HOLD:** none.
- **Rollback:** **Scope: the WHOLE receipt, reverse sequence.** Receipt rollback = the WHOLE receipt, reverse sequence (every mutating entry by descending `seq`), never one phase. For P6's entries the registry preimage is restored and the installed plan file is removed.
- **Gates:** plan: E35, E9; observed: E19 (the producer is unprivileged).

### P7: bus policies, units and activation

- **Preconditions:** P6 is green. The mailbox hub exists (P0), so the executor unit's `ReadWritePaths=@MAILBOX_HUB@` resolves; the install reviewer treats `@MAILBOX_HUB@` as a destructive-capable write surface (orchestrator condition).
- **Commands:** LIVE-VERB (`dbus_policy`, `unit`, `activation`). This includes the per-approver bus fragments, the four policies from `tools/host_control/dist/`, the three static units plus the ingress unit, and the MANDATORY executor activation file (the executor unit has no [Install], so activation is its only start path). It ends with a system-bus reload.
- **Expect:**
  - The policies and units are byte-equal to the plan's `content_sha256`.
  - `systemctl show --property=UnitFileState --value` parses as rehearsed (U4 CF-2).
  - There are explicit "none" lines for polkit, sockets, tmpfiles and logrotate.
- **HOLD: none.** P7 is NOT a hold (HO ruling): unlinking the files plus a bus reload is a proven rollback.
- **Rollback:** **Scope: the WHOLE receipt, reverse sequence.** Receipt rollback = the WHOLE receipt, reverse sequence (every mutating entry by descending `seq`), never one phase. For P7's entries: unlink plus a bus reload.
- **Gates:** plan: E35, E10, E19; observed: E32 (unit and bus-policy leg).

### P8: the bot token file

- **Preconditions:**
  - P7 is green.
  - MB-1 is delivered through the secure path.
  - The token source is no-follow stat-checked: a regular file, not a symlink, owned by root or the operator, not g/o-readable, at most 4KiB.
  - The source ancestors pass the openat walk (property (b)).
- **Commands:** LIVE-VERB (`credential`): an in-process create, with no install argv. It does `openat(parent, O_NOFOLLOW|O_DIRECTORY)`, then `open('bot-token', O_CREAT|O_EXCL|O_NOFOLLOW, 0600, dir_fd)`, then fchown and fchmod.
- **Expect:**
  - The token is owned by the ingress uid, mode 0600, with the parent ingress:ingress 0700 (Recovery amendment: the daemon reads it itself).
  - A planted destination symlink is refused.
  - The token never appears in a receipt, evidence, the journal, units or root configs (it is never hashed).
- **HOLD: HOLD.** D3: secret placement. Tool-enforced: the live verb stops at this hold entry (property (k)). orchestrator confirms the file metadata, then gives the explicit go that resumes the SAME receipt.
- **Rollback:** **Scope: the WHOLE receipt, reverse sequence.** Receipt rollback = the WHOLE receipt, reverse sequence (every mutating entry by descending `seq`), never one phase. For P8's entry it removes the token file; the token itself is never saved anywhere.
- **Gates:** plan: E35, E21; observed: E14.

### P9: start approval, then the broker, then ingress

- **Preconditions:**
  - P8 is green.
  - The approval entrypoint calls `ApprovalService.resume_all_human_deliveries` at start: keyset-paginated, looped until it returns fewer than the limit, and resuming the Present retry after a restart. (The original ask called it `resume_pending()`; it does not exist.)
  - The broker entrypoint does all of the following:
    - sets `reconcile=True`, wiring `BrokerLiveStateReconciler(journal, probe=ExecutorReconcileProbe(port=DBusExecutorPort(...)))`;
    - wires `operation_driver_probe` and `BusReachability` into `make_broker_health_provider`;
    - calls `load_daemon_runtime()` and logs `startup_record()`;
    - refuses euid 0.
  - `DriverContext.compensate` is OFF for the first fire (HO Q4).
- **Commands:** LIVE-VERB (`unit_enable`, then `journal_root` verify-after-start), in the order approval, broker, ingress (the plan's `after` chain; ingress also waits on the P8 credential). Restart HostApproval1 on EVERY approver-policy install: there is no hot reload, so revocation takes effect only on restart.
- **Expect:**
  - The broker and approval run as their unprivileged service uids.
  - The launchers are `python3 -I` from inside the framework rev only.
  - MemoryDenyWriteExecute=yes holds with PyGObject/libffi.
  - The broker's logged `startup_record` digest EQUALS the root-side gate-32a digest over the installed copy.
  - The broker boots DEGRADED with zero executors, then becomes healthy once an executor seat is activated (a real D-Bus activation via the activation file).
  - A non-seat uid is denied on the bus.
  - Ingress starts with the egress residual recorded (AF_INET/AF_INET6; IPAddressAllow cannot express a DNS name).
  - The E21 token journal scan is clean.
- **HOLD: HOLD** (the plan marks it on `unit_enable:pantheon-host-approval-telegram-ingress.service`). D3: the ingress start is outward-facing. Tool-enforced: the live verb stops at this hold entry (property (k)). orchestrator confirms the approval and broker health, then gives the explicit go that resumes the SAME receipt and enables ingress.
- **Rollback:** **Scope: the WHOLE receipt, reverse sequence.** Receipt rollback = the WHOLE receipt, reverse sequence (every mutating entry by descending `seq`), never one phase. For P9's entries it stops and disables the units. The Q10 restore uses the backups apply took at the start of `--execute` (P2).
- **Gates:** plan: E35; observed: E19, E28, E32a, E14 (ingress leg; the full proof E38 is T-2924's).

### P10: MCP allowlist membership and registration

- **Preconditions:**
  - P9 is green.
  - The adoption decision is made (MB-6 or orchestrator's ruling that ORG-052 §7 covers it).
  - **The mcp-adoption.md §2 doc row comes before any persona set is filled.** Under orchestrator's LOW-1 ruling (b), orchestrator applies the `pantheon-host` / `pantheon-approval` partner rows (listed, not admitted) to `rulebook/policies/mcp-adoption.md` §2 when U5 C2 merges. No membership commit may fill a `_CLAUDE_MCP_ALLOWLIST` persona set until that row is on main.
  - **Membership is a SEPARATE, orchestrator-ratified commit before registration.** The `_CLAUDE_MCP_ALLOWLIST` rows landed in U5 C1 with EMPTY persona sets ("membership set at enable from the ratified grant list / approver policy; empty = no seat admitted"). The membership commit fills `pantheon-host` with every registered seat and `pantheon-approval` with approver seats only, from MB-3. Seats are admitted LAST, after the daemons are healthy (least privilege).
  - **G-MEMBERSHIP is ticked** (section 1): the doc row, then the membership commit, then the membership check (property (j)).
  - **The membership commit moves the framework rev and the plan sha**, because `tools/persona_installer.py` is a FRAMEWORK_CODE_ARTIFACT. Before registration, P10 therefore runs, in order:
    1. a re-plan at the new commit, recording the new `plan_sha256`;
    2. the P4 framework copy (a new `<rev>`, `active` switched atomically, the previous rev kept) and the P6 plan registration, re-run (P6 (d));
    3. a restart of the broker and approval units onto the new rev (LIVE-VERB);
    4. a fresh gate-32a check: the broker's logged `startup_record` digest equals the root-side digest over the NEW copy.
  - **No re-rehearsal for the membership commit (ACCEPTED, mason ruling).** The membership commit differs from the G-R1-rehearsed rev only in the `_CLAUDE_MCP_ALLOWLIST` persona sets. P10's own gate-32a check and the broker and approval health checks are its evidence.
- **Commands:** the re-plan `python3 -I tools/host_broker_install.py plan --manifest <M> --registry <R> --tracked-files <T> --out <plan-membership>`, then `python3 -I tools/host_broker_rehearsal.py --plan-digest <plan-membership>`; LIVE-VERB for the P4/P6 re-run and the restart. Under (k), the P4/P6 re-run takes an explicit P4-P6 phase bound, so it does not re-stop at the P2, P8 and P9 holds, which are no-op entries at that point (mason ruling); then per seat `python3 -m tools.harness_adapter.host_mcp_registration --seat <seat-id> --approver-policy <root approver policy> --seat-registry <root seat registry>` prints one seat's role-gated fragment to stdout and applies nothing. The fragment is applied via the installer path (persona_installer's claude MCP registration op for claude seats; the codex `config.toml` fragment for codex seats).
- **Expect:**
  - Every registered seat gets `pantheon-host` (seven tools). A seat gets `pantheon-approval` (four tools) iff `is_policy_approver` (the same predicate as the server-side Reviewer gate).
  - No fragment carries a token, a bus address, a uid, bus policy or an executor path (`assert_registration_clean`).
  - G-MEMBERSHIP's check passes: the intended seats are members (property (j)).
  - The fresh gate-32a digest equals the new copy's.
- **HOLD:** none.
- **Rollback:** **Scope: P10's own, HOLD-free.** Remove each seat's registration via the installer op (presentation only, never revocation; authority stays server-side), then revert the membership commit and re-run the same sequence at the reverted commit: re-plan, the P4 copy and P6 re-run, the broker and approval restart, and the gate-32a check. It never rolls back the P2-P9 receipt.
- **Gates:** plan: none; observed: E5, E8 (unsupported rows stay unsupported until T-2924), E18.

### P11: the red/green first fire

- **Preconditions:** P10 is green and orchestrator is present.
- **Commands:** the ORG-052 red/green first fire through the real MCP adapters (orchestrator's supervised fire). The real-client evidence and gates 37/38 are T-2924's (SUPPORT_MATRIX unsupported rows).
- **Expect:**
  - The forwarded approval text is RED; a real Telegram tap is GREEN (gate 38 first live proof).
  - The accepted residual: the Bot API runs on the ingress loop thread (poll <=5s, then <=10s per call), so measure the real Telegram latency.
  - There is no close-card call: a card whose request was withdrawn or expired stays on Telegram, and a tap on it is refused.
  - The gate-26/30 compensate hook is recorded DISABLED (HO Q4). Compensate is turned on only after gate-26/30 fault injection on the rehearsal host.
- **HOLD:** none (orchestrator supervises the whole phase).
- **Rollback:** **Scope: the WHOLE receipt, reverse sequence.** Receipt rollback = the WHOLE receipt, reverse sequence (every mutating entry by descending `seq`), never one phase. Stop the units first; there is no P11-only rollback.
- **Gates:** plan: none; observed: E35 (live), feeding E37 and E38 (T-2924).

### P12: the identical rerun is a no-op

- **Preconditions:** P11 is done. The rerun's apply takes its own Q10 backups at the start of `--execute`, into the fresh `<receipt>.d/backups`.
- **Commands:** the LIVE-VERB apply with --execute, the same plan and a FRESH `<receipt>`, then the LIVE-VERB verify (which rebuilds the plan, property (d)).
- **Expect:** ZERO mutations, and a receipt whose actions are all noop. Rolling back a noop receipt skips the state restore (mason ruling L6).
- **HOLD:** none.
- **Rollback:** **Scope: none.** A noop receipt has nothing to reverse; rolling it back skips the state restore (L6).
- **Gates:** plan: none; observed: E35 (idempotent rerun).

---

## 4. Standing notes

- **The pre-loop failure case.** apply creates `<receipt>.d` (RC.d, root-only) before its entry loop. If the setup between the RC.d mkdir and the loop fails (writing the staged family bytes, or the Q10 backups), the error escapes as rc 2 and leaves a receipt-less RC.d. The O_EXCL check then refuses that `--receipt` path forever (`--receipt ... (or its .d dir) already exists`). **Use a fresh `--receipt` path**, and keep the orphan RC.d for diagnosis. After the loop starts, any failure is always partial, rc 1, with a receipt.
- **The mount allowlist is never self-widened.** If a rehearsal fails closed on an unlisted mount (for example a rw /sys/fs/bpf or /sys/fs/pstore), the U4b mount allowlist is NEVER widened by the operator or a worker. Any widening needs a NEW Recovery D15 ruling, made on the REAL mountinfo line captured from rehearsal 1's P0 snapshot (W-MOUNT-RULE) or a later rehearsal's. The shipped allowlist was built against synthesized mountinfo (U4b review F2).
  - **The first real widening: Recovery's D15 W3 ruling of 2026-11-01** (T-5703, W-T5703), made on rehearsal 1's P0 capture (run 1990856d106c), where real systemd-nspawn 255 created 7 mounts the synthesized allowlist refused. `RULED_MOUNTS` (U4b) and `MOUNT_RULED` (U4a, parity-pinned) accept exactly those mounts, each EXACT on mountpoint, root, fstype, ro/rw and nosuid/nodev/noexec: the `/sys` tmpfs (mode=555, size=4096k); `/run/host` as a read-only bind of the machine's /run tmpfs (not its own tmpfs); `/proc/sys/net` rw from the machine's /proc, at most two lines (parents /proc and /proc/sys), and only while the pinned nspawn argv carries `--private-network`; both `/proc/sys/kernel/random/boot_id` lines and the `/proc/kmsg` line as `//deleted` files on the machine /run tmpfs; and the guest's own `/run/lock` (a separate fresh tmpfs under /run, size=5120k, not masked in the image).
  - Every ruled tmpfs's superblock uid= and gid= must equal the userns root-mapped base that U4b reads from the machine's /proc/self/uid_map and gid_map, never a literal. The host checker (C-MOUNTS) has no uid_map in the P0 evidence, so it compares against the machine /run superblock instead; the uid_map equality is U4b's in-machine factor, recorded by the attested guard-probe (C-ATTESTATION).
  - A mount at a ruled mountpoint and fstype that misses its entry refuses. The earlier synthesized forms (a fresh tmpfs at /run/host, a `/boot_id` ro mask) now refuse. Anything new in rehearsal 2 goes back to Recovery.
- **The tmpfs/cgroup2 whole-fs bind residual** (accepted for the rehearsal): mountinfo cannot tell a fresh virtual filesystem from a host one bound whole. A host tmpfs bound rw at an allowed tmpfs mountpoint (`/tmp`, `/run`, `/run/systemd/*`, `/run/user/*`, and `/dev`, which is tmpfs ONLY now that devtmpfs is dropped, U4b F1), or a host cgroup2 filesystem bound at `/sys/fs/cgroup`, passes as fresh inside the machine. The PRIMARY control is the pinned nspawn argv golden (`-U`, no `--bind=`, only `--bind-ro=/srv/rehearsal/repo`, no `--volatile`). The P0 mountinfo snapshot is kept in the evidence as the secondary record.
- **The attestation factors are never relaxed.** For `nspawn -D /`, the machine-id factor (machine-id != the recorded host id) and the attestation-file factor are the ONLY refusals (U4b review, informational). No change may weaken either one.
- **The inputs file is not attested** (U4b review NIT-1; a follow-up). Today the wheels are lock-hash-checked anyway, so no unpinned byte reaches pip.
- **The forbidden-on-host canary:** `guard-probe` run on the HOST must refuse (rc 3) and is observe-only. U4a's R0 records the refusal.
- **The host-untouched check** watches the plan-derived installer targets, never all of `/var/lib/pantheon`, which is live fleet state (U4a review HIGH-1).
- **Prerequisites (rehearsal):** systemd-container plus debootstrap, the cached noble debootstrap, and the hash-pinned wheelhouse are run by orchestrator from U4a's emitted script, with a rollback per line. After a partial prerequisites failure, run `rollback-prerequisites` first.
  - **PRE-a install** is `host_broker_rehearsal.prea_install_line()`, built from `PRE_A_INSTALL`: `--no-install-recommends --no-remove`, with systemd-container pinned to the INSTALLED systemd version. A simulated install (`PRE_A_SIMULATE`) runs first; it refuses on any upgrade or removal and records the new-package list. The lint classifies every other apt install, upgrade, dist-upgrade or full-upgrade form (and a trailing-dash remove) as UNKNOWN and refuses it.
  - **PRE-a rollback** is `host_broker_rehearsal.prea_rollback_lines()`, the SUBSET guard (mason's ruling, T-2920 annotation `@fee4e14dd`, replacing exact equality). It filters the recorded list to the names dpkg knows (`PRE_A_PURGE_FILTER`), simulates the purge over them (`PRE_A_PURGE_SIMULATE`), and refuses only when a simulated removal falls OUTSIDE the recorded list (never a cascade). It then purges the verified set (`PRE_A_ROLLBACK`, never `--no-remove`, which refuses every purge), and VERIFIES before archiving (`PRE_A_VERIFY_QUERY` with the `PRE_A_VERIFY_SENTINEL` dpkg sentinel). It refuses and keeps the records if any recorded package is still installed or the dpkg database is unreadable. A smaller or empty set (a failed or partial install) proceeds, so the rollback is clean and prerequisites can re-run. The records are archived, never deleted. Residual (`PRE_A_WINDOW_NOTE`): the simulation-to-purge window.
  - The exact lines, GENERATED from those functions (`--regen` rewrites them; the runbook test fails on drift):

<!-- BEGIN GENERATED PRE-A -->
```sh
  timeout 900 apt-get install -y --no-install-recommends --no-remove "systemd-container=$SYSTEMD_VERSION" debootstrap  # PRE-a
  # ROLLBACK PRE-a (begin): simulate the purge, refuse a removal not on the recorded list, purge the rest
  # window: apt state could change between the purge simulation and the purge; it needs root, lasts seconds, and the rollback runs once under orchestrator's supervision
  # simulate over the recorded names dpkg knows; the subset check below stays against the FULL list
  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt; then timeout 60 xargs -a /var/lib/pantheon-rehearsal/prereq-new-packages.txt dpkg-query -W '-f=${Package}\n' > /var/lib/pantheon-rehearsal/prereq-purge-known.txt || test $? = 123; fi
  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt; then : > /var/lib/pantheon-rehearsal/prereq-purge-simulation.txt; fi
  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt && test -s /var/lib/pantheon-rehearsal/prereq-purge-known.txt; then timeout 120 xargs -r -a /var/lib/pantheon-rehearsal/prereq-purge-known.txt apt-get -s purge > /var/lib/pantheon-rehearsal/prereq-purge-simulation.txt; fi
  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt; then grep -E '^(Purg|Remv) ' /var/lib/pantheon-rehearsal/prereq-purge-simulation.txt | cut -d' ' -f2 | sort -u > /var/lib/pantheon-rehearsal/prereq-purge-set.txt || true; fi
  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt; then sort -u /var/lib/pantheon-rehearsal/prereq-new-packages.txt > /var/lib/pantheon-rehearsal/prereq-new-packages.sorted.txt; fi
  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt; then timeout 60 comm -23 /var/lib/pantheon-rehearsal/prereq-purge-set.txt /var/lib/pantheon-rehearsal/prereq-new-packages.sorted.txt > /var/lib/pantheon-rehearsal/prereq-purge-extra.txt; fi
  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt && test -s /var/lib/pantheon-rehearsal/prereq-purge-extra.txt; then timeout 60 xargs -a /var/lib/pantheon-rehearsal/prereq-purge-extra.txt echo 'PRE-a rollback would remove packages not in the recorded list:' >&2; exit 3; fi
  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt && test -s /var/lib/pantheon-rehearsal/prereq-purge-set.txt; then timeout 900 xargs -r -a /var/lib/pantheon-rehearsal/prereq-purge-set.txt apt-get purge -y; fi
  # verify BEFORE archiving: no recorded package may still be present (anything but not-installed); dpkg is the sentinel
  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt; then timeout 60 xargs -a /var/lib/pantheon-rehearsal/prereq-new-packages.txt dpkg-query -W '-f=${Package} ${db:Status-Status}\n' dpkg > /var/lib/pantheon-rehearsal/prereq-verify-status.txt || test $? = 123; fi
  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt && ! grep -qx 'dpkg installed' /var/lib/pantheon-rehearsal/prereq-verify-status.txt; then echo 'PRE-a rollback verify: dpkg-query could not read the dpkg database; refusing' >&2; exit 3; fi
  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt; then grep -v '^dpkg ' /var/lib/pantheon-rehearsal/prereq-verify-status.txt | grep -v ' not-installed$' | cut -d' ' -f1 > /var/lib/pantheon-rehearsal/prereq-verify-left.txt || true; fi
  if test -s /var/lib/pantheon-rehearsal/prereq-new-packages.txt && test -s /var/lib/pantheon-rehearsal/prereq-verify-left.txt; then timeout 60 xargs -a /var/lib/pantheon-rehearsal/prereq-verify-left.txt echo 'PRE-a rollback incomplete: still installed:' >&2; exit 3; fi
  # ROLLBACK PRE-a (end)
```
<!-- END GENERATED PRE-A -->
- **Seat-local residue hand-off.** On the broker path, a claude seat's routed residue depends on BROKER-leg evidence: the timer-enable `after: mailbox-probe`, the timer-observe needing that run's `connective.mailbox-probe` evidence (`tools/persona_installer.py` around :9984-9990; the after-chain at :3976), and the legacy seed from `timer-enablement-absence-check`. A residue-only apply must carry that evidence across the hand-off or re-run those ops. Since T-5473/T-5608 every seat carries residue quarantine, which is admitted (E40 stays reachable).
- **E36 protected roots** include `tools/persona_installer.py` (the InstallOperation enum), `/usr/local/lib/pantheon`, the ingress state dir and the broker install prefix.
- **The ORG-082 adapter payload** is returned as `structuredContent.error`, not bounded text.
- **The daemon literal path skips the T-5477 pin check.** The gate-32a digest equality (P9) is what catches it.
- **The mailbox hub ancestor gap:** the strict leaf-parent rule stays. Option (b), one hub exception keyed on a manifest-declared hub root, is deferred to T-5659, triggered by the first `mailbox_dirs` subject.
- **Re-cite before resolve:** `registry/systems.yaml` cites T-2920 as a gap closer, so re-cite it by PR before T-2920 resolves.
- **No real operator-batch values:** batch values live only in the root-owned inputs document at enable time, never in this file, a ticket, a receipt or evidence.

---

## 5. NOT EVIDENCED by the rehearsal, left to the live supervised fire

Rehearsal 1 evidences only what this table says. Everything in "not_evidenced_left_to_live_fire" is the live supervised fire's (and its arcs': T-2924, T-2921, T-2922), and nothing here may be relabelled as live evidence. Property (k), the tool-enforced HOLD stop, is NOT evidenced by the rehearsal either: its one-step P2-P9 is acceptable only because the machine is disposable. The block below is GENERATED VERBATIM from U4a's pinned constant `tools/host_broker_rehearsal.GATE_TABLE` (regenerate with `python3 -I tools/test_host_broker_enable_runbook.py --regen`). `tools/test_host_broker_enable_runbook.py` fails if the block and the constant differ.

<!-- BEGIN GENERATED GATE_TABLE -->
| Section | Id | Statement (verbatim from host_broker_rehearsal.GATE_TABLE) |
|---|---|---|
| evidenced | E35 | rehearsal tier, FIXTURE inputs: the bootstrap installs every account, executable, unit, bus policy, registry, journal root, approver policy and human allowlist on a disposable nspawn machine with a complete receipt, and the identical rerun is a no-op. The LIVE gate still needs orchestrator's supervised fire. |
| evidenced | E32a | the broker's logged startup_record digest equals the root-side digest over the installed framework copy |
| partial | E32 | account, owner/mode, system-unit and D-Bus-policy actions converge and roll back from a registered manifest revision; groups, mailbox dir, linger and ACL convergence are not in the T-2920 plan |
| partial | E19 | broker and approval run as their unprivileged service uids; the executor runs as the seat uid only when StartServiceByName is admitted |
| partial | E7 | runtime bytes are root-owned and not seat-writable; adapter dependency import is not exercised |
| partial | E21 | the fixture token never appears in the evidence, the journal, receipts, units or root configs |
| not_evidenced_left_to_live_fire | E1 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E2 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E3 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E4 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E5 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E6 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E8 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E9 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E10 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E11 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E12 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E13 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E14 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E15 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E16 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E17 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E18 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E20 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E22 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E23 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E24 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E25 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E27 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E28 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E29 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E31 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E33 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E34 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E36 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E37 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E38 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E39 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E40 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E41 | not exercised by the bootstrap rehearsal; left to the live supervised fire and its arcs |
| not_evidenced_left_to_live_fire | E26 | fault injection over the stuck states: the compensate hook is DISABLED for the first fire (Q4) |
| not_evidenced_left_to_live_fire | E30 | the dispatch-intent fault proof: the compensate hook is DISABLED for the first fire (Q4) |
| carry_forward_partials | CF-5:ingress | the ingress imports dbus/gi and connects the bus, passes the uid check and the token lease, then fails closed at getMe (no network, fixture token; the Bot API base is a constant), so it never owns its bus name or runs a GLib callback. A transient-unit MDWE+PyGObject/libffi probe copies its sandbox properties. The full ingress serve is left to the supervised fire (T-2924, E38) |
| carry_forward_partials | CF-3:acl+owner_mode | the T-2920 plan has no acl.converge subject and every owner_mode path is preceded by its dir entry, so the RootHelper acl/owner_mode leaf and ancestor guards are reachable only with the B6 guard manifest, which U4b's verb set (apply, verify, rollback, guard-probe, observe-probe) does not drive; the dir-leaf, ancestor and token-destination symlinks are evidenced through the real plan |
<!-- END GENERATED GATE_TABLE -->

---

## 6. Carry-forward ledger

Every carry-forward, U5 item and runbook note recorded on T-2920 (and in the reviews and Recovery rulings it cites) has one row here. The coverage test pins the id list. The "Honoured in" column points into this runbook.

| Id | Carry-forward | Source | Honoured in |
|---|---|---|---|
| CF-A1 | Broker `reconcile=True` plus `BrokerLiveStateReconciler(journal, probe=ExecutorReconcileProbe(...))`, with a test | T-2917 5d MR-1 (D25), T-2920 10-13 | P9 |
| CF-A2 | `operation_driver_probe` plus `BusReachability` wired into `make_broker_health_provider` | T-2917 MR-2 (1), 10-13 | P9 |
| CF-A3 | Mailbox hub created BEFORE the executor unit is rendered | T-2917 MR-2 (2), 10-13 | P0, P7 |
| CF-A4 | The install reviewer verifies `@MAILBOX_HUB@` as a destructive-capable write surface | T-2917 MR-2 (3), orchestrator condition | P7 |
| CF-A5 | MCP adoption is RESERVED; allowlist rows; root-owned runtimes; final root paths; per-approver bus fragments; HostApproval1 restart on every policy install; live registration via the installer; real-client evidence is T-2924 | orchestrator 10-26 (T-2916 checklist) | G-MB MB-6, P4, P5, P7, P9, P10 |
| CF-A6 | `load_daemon_runtime()` plus `startup_record()`; broker refuses euid 0; E36 roots; ORG-082 payload as structuredContent.error; daemon literal path skips the T-5477 pin | orchestrator 10-26 (T-2918) | P9, section 4 |
| CF-A7 | Ingress: a NEW BotFather bot; the ingress account; token 0600 / parent 0700 plus CredentialRef; root ingress config plus HumanIngressRegistration; unit plus conf from dist; the egress decision; T-5518 and T-5516 pre-enable; gate 38 first live proof; approval SCHEMA_DIGEST v1 baseline | orchestrator 10-26 (T-2919) | G-MB, G-PRE, P0, P5, P8, P9, P11 |
| CF-A8 | T-5533 decided before enable; framework.source is THREE files | orchestrator 10-26 (T-5516) | G-PRE, P4 |
| CF-A9 | Approval resumes pending deliveries at start; Bot API on the loop thread residual; no close-card | orchestrator 10-26 (T-5518) | P9, P11 |
| CF-A10 | Executor install AND framework copy ship `tools/runtime_invocation_receipt.py` | mason 10-26 (T-5572) | P4 |
| CF-A11 | Seat-local residue depends on broker-leg evidence | mason 10-26 (T-5533 delta review) | section 4 |
| CF-A12 | T-5533 registration steps a-d, re-registered on every framework-copy update | mason 10-26 (T-5533 merged) | P6 |
| CF-A13 | T-5597 digest widening deferred, gated on this ticket | orchestrator/mason 10-27 | G-PRE |
| CF-S1 | `DriverContext.compensate` is an opt-in, OFF for the first fire | design survey (T-4964) | P5, P9 |
| CF-S2 | CredentialRef registration; /etc/pantheon dirs including the 0700 credentials parent; the mandatory executor activation file; "none" lines | design review F6/F8 | P5, P7 |
| CF-S3 | Re-cite `registry/systems.yaml` before resolving T-2920 | design digest | section 4 |
| CF-S4 | The MCP allowlist edit precedes the framework copy (F3) | design review F3 | P4, P10 |
| HO-Q1 | Hybrid installer (ORG-082 :134 plus A-4; additive render-staging) | orchestrator rulings 10-27 | header |
| HO-Q2 | A real-host observe layer plus port protocol, built DARK, Recovery reviews the argv | orchestrator rulings 10-27 | section 2 (h)(i) |
| HO-Q3 | The live enable is DECOUPLED from C5 | orchestrator rulings 10-27 | section 1 |
| HO-Q4 | compensate OFF for the first fire; ON after gate-26/30 fault injection | orchestrator rulings 10-27 | P5, P9, P11 |
| HO-Q5 | pantheon-hostmonitor on the grant list, nologin, locked | orchestrator rulings 10-27 | G-MB MB-3, P2 |
| HO-Q6 | Versioned `/usr/local/lib/pantheon/framework/<rev>/` plus an atomic `active` link, previous rev kept | orchestrator rulings 10-27 | P4 |
| HO-Q7 | Verify and record dpkg packages, never install | orchestrator rulings 10-27 | P0 |
| HO-Q8 | Ingress AF_INET/6 with the residual recorded | orchestrator rulings 10-27 | G-MB MB-3, P9 |
| HO-Q9 | Rehearsal on systemd-nspawn on this VPS (fallback: a free pve2 VM) | orchestrator rulings 10-27 | section 2 |
| HO-Q10 | Back up /var/lib/pantheon-host-* before any rerun or rollback; never migrate | orchestrator rulings 10-27 | P2, P3, P12 |
| HO-Q11 | Healthsig is out of scope here; the ORG-050 installer owns it | orchestrator rulings 10-27 | header |
| HO-Q12 | Scope trims: 37/38 to T-2924, update to T-2922, teardown to T-2921 | orchestrator rulings 10-27 | header |
| HO-HOLDS | D3 holds are P2, P8 and P9; P7 does NOT hold | orchestrator rulings 10-27 | P2, P7, P8, P9 |
| HO-BATCH | The the operator batch is relayed as ONE message after U1-U4 plus a green rehearsal; name ratification early and alone | orchestrator rulings 10-27 | section 1 |
| CF-U1 | Producer output by stdout capture with a root digest re-check (U1 MED-1) | U1 review | P6 |
| CF-U2 | Tracked-file-list input to the closure (U1 MED-2) | U1 review | P1 |
| CF-U3 | Verify the shadow lock; refuse apply while blocking_preconditions is non-empty; hostmonitor-only monitor_uids; configs from U2 schemas | U1/U2 reviews 10-27 | P0, P2, P5 |
| CF-U4 | PyYAML pinned set; an unpinned host is refused at verify | U1 CI fix 10-27 | P0 |
| W-U3B | Observe argv: shadow row projected to name plus expiry (no hash anywhere); full shadow enumeration dropped; getfacl --physical | Recovery D15 U3b | P2 |
| W-U3A-ARGV | dpkg-query from the fixed tuple; build_runtime as root with covered-set hash, pre-exec ownership stat, clean env, wheelhouse-only, literal outputs, 2-value --adapter | Recovery D15 U3a | P0, P4 |
| W-U3A-TOKEN | Token via in-process openat plus O_CREAT\|O_EXCL\|O_NOFOLLOW; parent ingress:ingress 0700; no install argv; source no-follow stat | Recovery D15 U3a amendment | P8 |
| W-U3A-F | `-I` on the venv and pip children; explicit root-owned cwd recorded in the receipt | Recovery D15 U3a condition (f) | P4, section 2 (a) |
| W-TAR | Tarfile restore uses the data filter | Recovery D15 U3a FYI | P2 |
| W-U3C | Symlink guard on acl/owner_mode/mailbox_dir; setfacl --physical; chown -h; chmod TOCTOU residual accepted | Recovery D15 U3c | section 2 (g) |
| W-U3D | Ancestor walk with designated-owner rule; '/' operand walk-only; T-5658 base-entry rule; T-5665 one entry per key; hub gap T-5659 | Recovery D15 U3d | section 2 (g), section 4 |
| W-U4B-DESIGN | Attested rehearsal context; mount factor; full-argv gate for env/python3/setpriv; receipt separation; host canary; module out of the closure | Recovery D15 U4b design | section 2 (e)(h), section 4 |
| W-SETPRIV | Amended producer template (+ --no-new-privs --inh-caps=-all --bounding-set=-all); observe CapBnd 0 and NoNewPrivs 1 once; swapped output refused | Recovery D15 SETPRIV | P6 |
| W-U4B-TMPFS | (W1) The tmpfs/cgroup2 whole-fs bind residual only (`/dev` is tmpfs only; devtmpfs dropped, F1 done): a host tmpfs or cgroup2 fs bound at an allowed virtual mountpoint passes as fresh (accepted for the rehearsal); the pinned nspawn argv is the primary control | Recovery D15 U4b code CF-1, U4b review F1 | section 4 |
| W-U4B-OWNER | (W2) The owner-slot binding before live: rollback owner = receipt preimage; apply owner = manifest A/G | Recovery D15 U4b code CF-2 | section 2 (c) |
| W-MOUNT-RULE | (W3) Never self-widen the U4b mount allowlist; a widening needs a new Recovery ruling on the real mountinfo captured from rehearsal 1 | Recovery D15 U4b standing rule | section 4 |
| W-T5703 | Recovery's D15 W3 ruling (2026-11-01), the first real widening: the 7 nspawn 255 mounts refused in rehearsal 1 (run 1990856d106c) are accepted EXACTLY (`RULED_MOUNTS` / `MOUNT_RULED`); every ruled tmpfs uid/gid equals the userns base read from /proc/self/uid_map and gid_map; /proc/sys/net only with `--private-network`; anything new goes back to Recovery | Recovery D15 W3 ruling 2026-11-01, T-5703 | section 4 |
| CF-W8 | (W4) Rehearsal 2 evidences the owner_mode subject-symlink fault through the real binaries (acl too if cheap), via a narrowly-scoped U4b verb reviewed under D15 | Recovery D15 on U4a CF-3, T-5673 | G-R2 |
| CF-L1 | CommandRunner cwd passed through, never swallowed | mason 10-27 live-layer notes | section 2 (a) |
| CF-L2 | Openat walk for the token source ancestors | mason 10-27 live-layer notes | section 2 (b), P8 |
| CF-L3 | Live chmod follows symlinks (preflight-guarded) | mason 10-27 live-layer notes | section 2 (g) |
| CF-L4 | A live verify rebuilds the plan | mason 10-27 live-layer notes | section 2 (d), P12 |
| CF-L5 | A receipt-less RC.d on a pre-loop failure (rc 2) blocks that --receipt path; use a fresh path | mason 10-27 live-layer notes | section 4 |
| CF-LC | LC_ALL=C on the operator runner | U3c review, U4 CF-1 | section 2 (f) |
| CF-NOOP | Rolling back a noop receipt skips the state restore (L6) | mason U3a ruling | P12 |
| U4-B4 | Ingress CF-5 PARTIAL in the rehearsal (no network or real token); the full serve is the supervised fire's | mason U4 rulings | section 5, P9, P11 |
| U4-B5 | Machine-only activation move-aside for the degraded-then-ready check | mason U4 rulings | P9 |
| U4-B6 | An acl rehearsal mini-manifest (guard manifest), not the install manifest | mason U4 rulings | G-R2 |
| U4-B7 | StartServiceByName denial maps to partial | mason U4 rulings | section 5 |
| U4-CF7 | Executor activated or activatable only | mason U4 rulings | P9 |
| U4-PREREQ | orchestrator runs the rehearsal prerequisites from the emitted script with a rollback per line; the wheelhouse stays out of the trusted paths | orchestrator U4 ruling | section 4, P3 |
| U4B-Q1 | The attestation carries plan_sha256; load requires the rebuilt plan sha to equal it | mason U4b ruling | section 2 |
| W-OBSERVE-CALLER | A new external caller of `observe()` is a D15 change (Recovery reviews it like a new argv) | Recovery D15 U3b (05:27:59) | section 2 (i) |
| CF-HOLD-K | The live verb stops at each plan hold entry and resumes the SAME receipt only on an explicit go; the D3 holds are tool-enforced; the rehearsal's one-step P2-P9 is not evidence for it | review MED-1 ruling (mason) | G-LIVE, section 2 (k), section 5 |
| U4B-Q2 | No extra binds; everything copied into the machine root fs; real mountinfo captured in P0 | mason U4b ruling | section 4 |
| U4A-CF3 | CF-3 acl and owner_mode PARTIAL in rehearsal 1 (no new verb now) | mason 10-27, Recovery D15 | G-R1, G-R2 |
| CF-U5-1 | The mcp-allowlist precondition checks key presence only (P0); membership is a separate gate, G-MEMBERSHIP, checked immediately before P10 (doc row, then the membership commit, then the check), never at P0 | mason U5 ruling, review MED-2 ruling | G-MEMBERSHIP, section 2 (j), P10 |
| T-5644 | The synchronous ApprovalGet on the loop (U2 LOW-2) is decided before enable | U2 review | G-PRE |
| T-5659 | Mailbox hub exception option (b), gated on the first mailbox_dirs subject | Recovery D15 U3d | section 4 |
| U4B-F3 | The rehearsal owner/path slots: P split into observe/mutate slots, N bound to manifest names (or the preimage on rollback), setuid/setgid/sticky modes refused unless planned; the live change keeps the same binding | U4b review F3 | section 2 (c) |
| U4B-NIT1 | The inputs file is not attested (a follow-up; the wheels are lock-hash-checked) | U4b review NIT-1 | section 4 |
| U4B-FACTORS | The machine-id and attestation-file factors are the only refusals for nspawn -D /, so they are never relaxed | U4b review (informational) | section 4 |
| U4A-WATCH | The host-untouched check watches the plan-derived installer targets, not all of /var/lib/pantheon | U4a review HIGH-1 | section 4 |
| U4A-PREA | PRE-a: `prea_install_line()` (no-recommends, --no-remove, version-pinned); `prea_rollback_lines()` = the SUBSET guard (refuse only a simulated removal outside the recorded list), simulating over the names dpkg knows, purging the verified set without --no-remove; residual: the simulation-to-purge window | U4a review MED-2, delta MEDs 1-2, mason subset ruling `@fee4e14dd` | section 4 (generated block) |
| U4A-VERIFY-ARCHIVE | The PRE-a rollback verifies before archiving: the dpkg sentinel; refuse and keep the records if a recorded package is still installed or the DB is unreadable; a failed or partial install rolls back cleanly and prerequisites can re-run | U4a final nits (0f317a26f) | section 4 (generated block) |
| U4A-LINT | The emitted-script lint: known/unknown classification; every apt install/upgrade/dist-upgrade/full-upgrade form (and a trailing-dash remove) other than the pinned PRE-a install, and every package-removal form other than the pinned rollback block, is UNKNOWN and refused; the block sits contiguously, exactly once, inside rollback_prerequisites | U4a delta reviews 2-3 | section 4 |
| U4B-PSHELL | `P_SHELL`: an account's login shell is its own slot, rendered only into useradd's `--shell` position, never a mutated path | U4b delta L1, Recovery D15 | section 2 (c) |
| W-MODE-7022 | `GUARDED_MODE_BITS = 0o7022`: a MODE with setuid/setgid/sticky or group/other WRITE on a P_MUTATE path needs that exact mode planned for the path, or in its receipt preimage on rollback; required before LIVE | Recovery D15 on U4b F3 | section 2 (c) |
| W-ACL-EXACT | A setfacl on a P_MUTATE path is accepted ONLY with exactly the planned ACL (apply) or the receipt-preimage ACL (rollback), compared after the T-5665 canonicalisation; no fallback; a template without exactly one P_MUTATE path refuses setfacl; any new setfacl form goes to Recovery, never a widening | Recovery D15 ACL-slot ruling | section 2 (c) |
| U4B-PIN | Receipt and journal pinning: O_NOFOLLOW, fstat root:root with no g/o-write, byte sha re-checked on every read; an absent journal is pinned ABSENT and refuses if it appears | U4b delta L2, L5 | section 2 (e) |
| U4B-MALFORMED | A malformed or unowned receipt refuses cleanly (rc 2, `RECEIPT_ERRORS`), never a crash | U4b delta L3 | section 2 (e) |
| HO-LOW1B | orchestrator's C1 LOW-1 ruling (b): orchestrator applies the mcp-adoption.md §2 partner rows (listed, not admitted) when C2 merges; P10 requires the doc row before any persona set is filled | orchestrator ruling, T-2920 14:30 | G-MB MB-6, P10 |
