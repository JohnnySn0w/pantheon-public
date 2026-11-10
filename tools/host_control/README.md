# Host control

This package implements the harness-neutral host-control boundary ratified in
ORG-052. It is intentionally separate from:

- `tools/harness_adapter/`, which presents harness-specific configuration;
- `tools/persona_installer.py`, which generates and verifies typed installation
  plans;
- `tools/credential_broker.py`, which issues purpose-scoped credential leases;
  and
- mailbox/ticket delivery, which carries opaque handles and work context but is
  never identity, approval, actuation, or durable operation state.

## Authority boundary

The public host surface accepts fixed logical identifiers and opaque handles.
It never accepts a caller-supplied shell, command, executable, path, content,
environment, uid, group, unit or policy text, sudo target, credential value,
serialized plan, or claimed persona authority.

The eventual system D-Bus service derives the caller uid from the live bus
sender. Root-owned registries resolve the seat, target, manifest, harness,
deployment root, policy, templates, and operation families. MCP remains a
privilege-free harness adapter over that service.

The initial package increment does not expose D-Bus or MCP and cannot mutate a
host. It establishes only server-owned planning truth:

- frozen root-owned registry, policy, and manifest input, plus a
  root-registered installer plan per seat and manifest revision (T-5533):
  the unprivileged `tools/host_plan_producer.py` computes it at
  registration, the registry pins its path and sha256 under `plans`, and
  the broker reads it through the root-owned loader. The broker never
  imports `tools.persona_installer`, and `Plan()` refuses unless the
  registry is loaded with a `framework_root` whose digest matches;
- a closed actor-neutral effect digest and actor-bound plan digest;
- structured operation-family and approval-class metadata;
- subject-bound opaque plan handles;
- durable idempotent SQLite plan storage; and
- bounded, canonical, redacted plan summaries.

## Landed increments beyond Unit 1

**Broker operation state machine (Unit 2a, T-2909, PR 294).** `journal.py` v2 +
`operations.py`: the PREPARED → AUTHORIZED → DISPATCH_INTENT → DISPATCHED →
terminal lifecycle with subject-bound random operation handles, exact
plan/receipt binding, strict idempotency, and staged receipts plus write-ahead
rollback intents durably synced before any dispatch step. Execution evidence
and terminal state are appended by the broker only; the executor seam is a
value-returning Protocol that structurally cannot reach the journal.
Operations deliberately never expire.

**Approval journal (Unit 2b, T-2910, PR 297).** `approval/` is a physically
separate authority: its own SQLite file (application_id `PHCA` vs the broker's
`PHCB`; each journal loudly refuses the other's file), its own review
(OPEN → GRANTED/DENIED/EXPIRED) and grant (ISSUED → BOUND/REVOKED/EXPIRED)
state machines, single-use BOUND enforced by SQL constraints, and
ValidateAndBind semantics where an identical-tuple retry replays the original
binding and any differing element denies with a closed reason code. The broker
cannot mint or edit approval records; the approval service cannot edit plans,
operations, or receipts. Shipped defaults deny everything: a deny-all
authorizer and an empty consumer-uid set. Grant payloads (single-use nonce,
authority record id) never cross a public return; an approver-supplied audit
anchor is context and can never populate the provider-authenticated
`source_event` field.

**Per-seat executor, broker seams (Unit 5a, T-2917).** Groundwork for the
target-uid executor; nothing here installs, enables, or dispatches anything.

- *Receipt ownership split.* `HostBackend` in `tools/persona_installer.py` no
  longer persists receipts. A separate `ReceiptSink` does: `FileReceiptStore`
  on the operator-run local path, with its bytes pinned by a golden test, and
  the broker journal on the broker path. The executor never writes the
  journal.
- *Executor envelope* (`envelope.py`). This is one frozen table mapping each
  installer kind the executor implements to an executor capability class.
  The capability classes are code. They are NOT policy families, which stay
  root-owned data.
  - It includes the §8a classification from HO ruling T-4861.
  - Superseded in 5b by the planner partition below: unmapped kinds are no
    longer refused at plan time. A non-admitted kind INSIDE a broker plan
    is still refused, whole-plan, with `family_denied` (the bounded detail
    names the kinds): at plan time, at every prepare and at `OperationSpec`.
  - The broker never applies a harness permission-posture change.
- *Approval consumer client* (`approval_client.py`). This is the real
  `ApprovalBinder` and `ApprovalStatusSource` over `HostApproval1.Consumer`,
  deferred here from T-2913 by its OQ6 disposition.
  - It transcribes the approval package's wire names rather than importing
    them (gate 18), and tests pin the transcription.
  - `ApprovalGet` is bound to the caller's own plan (ORG-052 section 4,
    gate 15; T-4875). The approval service's `ApprovalStatus` payload
    (schema `pantheon-approval-status/v2`) carries the grant's
    `plan_handle` and `plan_owner`. The broker answers only if one of the
    caller's own operations carries the handle, the payload's `plan_owner`
    is the caller's seat, and one of the caller's operations carries the
    handle on that `plan_handle`. Otherwise it answers
    `UnknownHandle("unknown approval handle")`, the same as for a
    nonexistent handle. Attaching someone else's handle to a throwaway
    Apply no longer discloses its state. A v1 or unbound payload fails
    closed. A granted handle that no operation carries yet still reads as
    unknown (the pre-Apply gap).
- *Per-uid rate limiting* (`ratelimit.py`). In-memory buckets keyed by the
  bus-derived uid, with separate mutating and read budgets. The check order
  is identity, then budget, then domain.
- *`ExecutionSource.OperationSpec`* (`execution_source.py`). A broker-only
  method that returns a deterministic, digest-bound execution spec.
  - Only the registry uid of the operation's target seat may call it.
  - It answers only after DISPATCH_INTENT.
  - The executor envelope is re-checked at the execution point.

**Per-seat executor (Unit 5b, T-2917).** The target-uid executor, its
two-sided identity checks, read-only diagnostics, seat-root file
materialization and a real Verify. Nothing here installs, enables or
starts anything; the staged files under `dist/` are T-2920's to install.

- *Executor identity* (`executor/identity.py`). The seat comes from
  `getuid()` through the root-owned registry, and nothing else: argv,
  environment and the unit instance name are ignored. An unregistered uid
  refuses to start (exit 78) before any bus connection. The bus name is
  `org.pantheon.HostExecutor1.U<uid>`, derived from the registry uid.
- *Two-sided identity.* The broker resolves the executor name's owner and
  requires its bus-attested uid to be the target seat's registry uid
  before every `Execute`/`ExecutionGet`, then calls that unique name. The
  executor requires the caller to be the configured broker uid and the
  owner of `org.pantheon.HostBroker1` to be the broker uid, and fetches the
  spec from that owner's unique name. The spec's target must resolve to
  the executor's own uid.
- *Execution.* `Execute` returns an execution handle at once and the work
  runs on a worker thread. At most one execution runs at a time: a
  duplicate `(handle, digest)` gets the same handle, a different digest is
  `Denied`, a different handle while busy is `Busy`. The executor returns
  only typed mutation and evidence records (bounded, 1 MiB) and never
  imports the broker journal; this is tested at the AST and `sys.modules`
  levels, which is why the package `__init__` re-exports lazily.
- *Engine* (`executor/engine.py`). In this revision it serves the
  `diagnostics.read` and `files.seat_root` classes for apply and verify
  (rollback since 5d.2; see Increment 5d.1/5d.2). Nothing shells out: no `systemctl --user`, no
  `loginctl`, no `git`. The user manager is reached only through
  `UserManagerPort`; 5b ships its read side as a protocol with a
  fail-closed default, and the real user-bus adapter lands in 5c.
  - Repo-sourced checks read only the root-owned framework copy named in
    the executor config, never a seat checkout. `source-check` proves the
    artifact digest over that copy; revision equality stays with the
    planner's effect digest. The codex standing-instructions source check
    drops the installer's `git cat-file` commit proof (ruling B6).
  - A brief under the seat root is checked for readability only and
    derives no trust (ruling B7).
  - A present timer-enablement link fails closed until the 5c port can
    judge legacy convergence (ruling B9). Superseded in 5c: see the B9
    legacy branch below.
- *Seat-root files* (`executor/seat_files.py`). Descriptor-anchored, with
  `O_NOFOLLOW` on every component. It refuses targets outside the seat
  root, symlinks anywhere (including the final component), owner
  mismatches, setuid/setgid/sticky modes, a missing parent, and an
  existing file that is not installer-managed. Content is hash-pinned.
- *Planner partition* (HO ruling T-4861, ruling B8). The broker plan holds
  only admitted kinds. Every excluded or unclassified kind becomes a
  `seat-local.<operation_id>` operator action: it is bound into the plan
  and effect digests and listed under `seat_local_residue` in the plan
  summary the approver reads. The plan is refused whole when the
  out-of-envelope residue exceeds 32 entries or when an admitted operation
  is ordered `after` a residue operation.
- *Launch routing* (HO ruling T-5470). When a plan carries a
  must-run-before-launch op that is residue (`seat-claude-settings-env`,
  `seat-runtime-residue-quarantine`, `seat-claude-workspace-trust`), its
  launch ops (direct-fire, unit-fire, timer-enable,
  seat-service-enable-observe) and timer-observe route to the residue in
  plan order. Routed ops do not count toward the 32 cap; they have their
  own bound of one per kind. The producer and `Plan()` both refuse a plan
  whose effective broker order (admitted, then residue) runs a launch
  before a must-run op, or a claude plan that launches without
  containment.
- *Verify* (rulings B2/B3). Verify is policy-authorized: the caller's own
  stored plan for that seat is the authority, and ValidateAndBind is never
  called. The record carries the closed sentinels
  `none:read-only-verify` / `broker:read-only-verify`, which no apply or
  rollback record can carry. It dispatches with an empty intent set. A
  plan with any kind lacking a read-only handler is `Unsupported` before
  any record is written.
- *Apply gate* (ruling B4). When the driver is wired, an Apply whose plan
  holds a class this revision cannot execute is `Unsupported` before
  prepare, so no grant can bind.
- *Operation driver* (`driver.py`, `executor_client.py`). It runs
  authorize, dispatch, `ExecutionGet` collect and `append_result` on one
  dedicated thread. That thread has its own journal handle and its own bus
  connections, so the main loop stays free for the `PlanMetadata` and
  `OperationSpec` call-backs. Recovery at start (ruling B11):
  - prepared and authorized operations resume;
  - a verify is re-dispatched when its execution is unknown;
  - an apply is resumed only after a definitive refusal;
  - a dispatched apply whose execution the executor no longer holds stays
    `dispatched` (visible in OperationGet) for the reconciler and is never
    re-dispatched blind (5d.2: see Increment 5d.1/5d.2);
  - rollback operations are driven since 5d.2;
  - a verify whose execution is unknown is re-dispatched at most 3 times
    per drive, with back-off and the collect deadline applied;
  - each definitive refusal licenses at most one further `Execute`.
- *Spec served once.* `ExecutionSource.OperationSpec` serves a mutating
  (apply/rollback) spec exactly once per operation, recorded atomically as
  a `spec_served` journal event. A later fetch is refused, so a broker-uid
  process cannot re-trigger an apply parked for the reconciler. Verify may
  be re-fetched.
- *Increment 5d.1 / 5d.2 (T-2917 MR-1).*
  - *5d.1 hardening* changes no capability:
    - identifiers in specs are bounded exactly as the broker decodes records;
    - a committed mutation survives any per-operation error;
    - the codex standing-instructions pin is checked at parse, and at apply
      against the hash the source check observed (D23);
    - every execution has a hard deadline: each user-manager call is
      clamped to the time left, and a watchdog exits the executor at
      budget + 120 s without ever fabricating a result (D11);
    - verify re-dispatches are capped at 3 over the operation's lifetime
      (D18);
    - a refusal before the engine runs is the executor state `refused`,
      which the broker classifies from journal serve ordering (D14).
  - *5d.2 recovery.* A lost mutating result goes to
    `BrokerLiveStateReconciler` (`reconciler.py`):
    - a spec whose arm was never served is re-dispatched;
    - a served apply is probed read-only through the `reconcile` execution
      kind, which is selected by digest (D13). It is re-dispatched only
      through a journaled, capped re-arm (`MAX_SPEC_REARMS = 2`, D17), and
      only when the first mutating operation's target is untouched (D12).
      Otherwise it parks in `manual_recovery` with the probe report.
  - *D27.* Every mutating `Execute` is preceded by a lifetime-counted
    issuance marker. A refusal ends `failed` with no mutations only when
    the operation was issued exactly one `Execute`; any other refusal is
    reconciled.
  - *Reconciliation is opt-in.* It is enabled per driver context with
    `DriverContext.reconcile` (default off, ruling D25). With it off, a
    lost result parks loudly as `awaiting_reconciler`. The production
    broker entrypoint (T-2920) must set it on.
  - *Rollback is executable* (D15/D16). It is grant-bound and works from a
    complete or failed apply receipt:
    - it restores only intent-backed changed records, and a timer only
      with the executor's owned record (D6);
    - rollback specs elide `desired.content`;
    - any drift refuses the whole set, and restores run in reverse order.
    Broker auto-compensation of a failed apply (T-4964): a broker-created rollback inheriting the failed apply's authority, gated by DriverContext.compensate (inert until T-2920).
- *Entrypoint* (ruling B5, PROVISIONAL). `python3 -m
  tools.host_control.executor`, no arguments, with config
  `pantheon-host-executor-config/v1` at
  `/etc/pantheon/host-executor/config.json`. T-2920 owns the final path,
  the schema and the install.
- *Staged files* (not installed): `org.pantheon.HostExecutor1.conf`
  (static default-deny, prefix-scoped; the broker may call only `Execute`
  and `ExecutionGet`), `pantheon-host-executor@.service` (system template,
  `User=@SEAT_ACCOUNT@`, the instance name unused), the activation-file
  template, and the seat fragment's `own` of its own executor name.

**Per-seat executor, user manager and activation (Unit 5c, T-2917).** The
executor reaches its seat's user manager over the seat's own user D-Bus,
never through `systemctl --user` and its private control socket (the
founding incident, ORG-052 gate 39). All evidence here is fixture evidence
(labelled `test-override`); agreement with a real systemd user manager and
real cross-uid runs are T-2924's live checks. Nothing here installs,
enables or starts anything.

- *Typed user-manager port* (`tools/user_systemd.py`, 5c.1). A neutral
  module: typed errors, a typed `UnitSnapshot`, a closed `ReadPurpose`
  table whose property tuples keep the historical request order, pure
  classifiers, and a converter from native typed values. It names no
  transport command, socket or environment variable (an AST guard checks
  it). The operator-run installer path moved behind the same port as
  `SystemctlUserManager` in `tools/persona_installer.py`, a pure refactor:
  identical argv and timeouts per call site, golden legacy parity, zero
  edits to the existing installer tests.
- *`DBusUserManager`* (`executor/user_bus.py`). The address is
  `unix:path=/run/user/<registry uid>/bus`, derived from the registry
  uid, never read from the environment. Before the first call the socket
  must be a socket owned by the seat uid, and the owner of
  `org.freedesktop.systemd1` on that bus must be the seat uid; every call
  then goes to that owner's verified unique name. Reads are `LoadUnit` plus
  per-property `Properties.Get`; writes are the Manager methods, with a
  `GetJob` wait where the operator path blocked (ruling D9 also admits the
  `NeedDaemonReload`, `EnvironmentFiles` and `Version` reads).
  `runtime_root` is the only test override and the entrypoint never passes
  it (ruling D7; a guard flags a test that constructs the manager without
  it). D4: disabled-first's EnvironmentFile gate reads the typed
  `EnvironmentFiles` list, where the operator path matched a substring of
  the unit file, so the two differ when the word appears only in a comment
  or another directive.
- *Activation kinds* (`user_systemd.activate`, apply and verify):
  `daemon-reload`, `stale-unit-failure-reset`, `direct-fire`, `unit-fire`,
  `timer-enable`, `timer-observe`. `codex-telegram-disabled-first` is
  admitted as `diagnostics.read` (inspection only, T-4861).
  - An apply first runs a read-only *pre-pass* (the installer's preflight
    gates: manager reachable, timer and link states, the receiver checks,
    the already-enabled-timer boundary, a timer that is not loaded) and
    changes nothing unless every gate passes.
  - A *per-kind unit allowlist*: each (kind, field) may name only its unit
    shapes (the persona consume service/timer for the persona kinds; the
    codex receiver only for disabled-first and the daemon-reload unit
    read), and only the unit its own operation names. Every persona consume
    unit in one spec belongs to ONE consume instance, unit-fire's target is
    the service its timer fires, and so is timer-observe's service. Checked
    when the spec is parsed and again at every call.
  - `direct-fire` runs `sys.executable -I` on the root-owned framework
    copy's `persona_bringup.py diagnose`, with an explicit minimal
    environment (PATH, HOME, LANG; nothing inherited); the plan's repo and
    manifest paths are never used.
  - `timer-enable` returns an owned mutation record whenever the enable
    call was issued, whatever happens after it.
  - *B9 legacy branch*: a present enablement link passes the absence check
    only when the port proves the converged legacy shape (canonical link,
    timer active and waiting with a finite next elapse, service inactive
    and successful within its success policy); it then seeds the run's
    unit-fire and timer-observe evidence for THAT timer only (seeds are
    keyed by the timer unit).
  - *Verify* observes live state read-only and records no mutation;
    direct-fire re-runs diagnose (ruling D3).
  - `mailbox.probe` is implemented since 5d.3 (MR-2); see Increment
    5d.3-5d.5 below.
- *Budgets.* One execution is capped at 3300 s, checked between
  operations, with in-operation waits bounded by the remaining time or the
  adapter's 30 s call timeout; the broker driver's collect timeout is
  3600 s. Since 5d.1 there is also a hard deadline: every call is clamped
  and a watchdog exits at budget + 120 s (D11).
- *D6 rollback intent* (`operations.py`). A timer-enable declares no
  inverse, so the broker synthesizes one write-ahead intent for it, and
  only for it. Its basis is the executor's precondition check, and it is
  never cleanup authority on its own: undoing an enable needs the
  executor's owned mutation record.
- *E39 fixture proof* (`tools/test_host_control_executor_e39.py`). The real
  engine runs in a child process against a fake systemd1 on a private bus
  at the registry-derived address. Decoys sit where the founding-incident
  paths would lead: `systemctl`/`loginctl`/`busctl` tripwires alone on
  PATH, listening `XDG_RUNTIME_DIR` bus and `systemd/private` sockets, a
  decoy session bus, and an audit hook on process spawns. GREEN: the target
  receives the exact expected call sequence and no tripwire fires. RED
  controls: an injected `SystemctlUserManager` trips the decoy binary, and
  a session-bus variant trips the decoy session bus.

### Increment 5d.3-5d.5 (T-2917 MR-2): mailbox.probe, Health1 legs, E29 proof

- *The capability flip* (5d.3, ruling D22).
  - `IMPLEMENTED_EXECUTION_CLASSES` gives apply, verify and reconcile the
    `mailbox.probe` class, on top of MR-1's reconciler and rollback. No
    in-envelope installer kind is `Unsupported` any more.
  - **The flip is partial (ruling D31).** A real persona plan that carries
    a named out-of-envelope kind is still refused at plan time with
    `family_denied` (ruling i4, T-4861 section 8b). The kinds are
    `claude-mcp-server-registration`, `codex-telegram-source-check` and
    `codex-telegram-mcp-check`. In this repo's `instance.yaml` only the
    `codex` and `watchman` plans are executable end to
    end. A test pins that set, so any drift fails.
  - The B4 gates, broker side and executor side, stay tested against the
    pre-flip table.
- *`executor/mailbox_probe.py`* (rulings D19, D20).
  - It runs the installer's round-trip probe (receive and send `tmp`) and
    its writable-root probe (the root plus `tmp`, `new` and `cur`) with the
    installer's evidence keys, plus `mailbox_hub_source: executor-config`.
  - Probes are confined to the hub named in the root-owned executor config.
    Round-trip targets must be exactly `hub/<persona>/tmp`; a writable root
    must be exactly `hub/<persona>`. A plan that names another hub is
    refused.
  - Every component is opened descriptor-anchored with `O_NOFOLLOW`.
    Probe files are created `O_CREAT|O_EXCL|O_NOFOLLOW`, fsynced, read back
    and sha-compared.
  - Probe names are deterministic: `.installer-probe-<32 hex>` and
    `.pantheon-rw-probe-<32 hex>`, the hex being
    sha256(handle|operation_id|direction).
  - Unlinking is check-then-unlink by name. A swap in between needs write
    access to the directory, which gains nothing: without the sticky bit it
    already allows deletion, and with it the kernel blocks the rename.
  - A leftover file is swept once, and only when it is the executor's own
    residue (its uid, a regular file, at most 4 KiB). Anything else is
    refused and never unlinked.
  - The deadline is checked before every write.
  - On apply the probe records a `changed=False` mutation (never
    restorable). Verify re-runs the live probe: it makes no restorable
    change but does write transient dot-files (ADR section 4 allows active
    probes).
  - The reconcile probe reports leftover residue (`absent`, `own` or
    `foreign`) and never deletes it. With no hub configured, every mailbox
    operation fails closed.
- *Executor config and unit* (PROVISIONAL until T-2920).
  - `pantheon-host-executor-config/v1` gains the required key
    `mailbox_hub`.
  - D32: the hub may not equal, contain or sit inside any of: a trusted
    root, the framework root, the registry path, the seat root, or a system
    tree (`/etc /usr /bin /sbin /lib /lib64 /boot /var/lib/systemd /proc
    /sys /dev /run/systemd`). `/` itself is refused.
    - The comparison is per path component, so `/etc2` is fine.
    - Every normalized-path check in host_control and the executor refuses
      a raw path starting with `//`. POSIX `normpath` keeps exactly two
      leading slashes, while the kernel resolves `//etc` to `/etc`.
  - D33: before any bus connection, the entrypoint lstats every existing
    component of the hub and refuses a symlink, because systemd follows
    symlinks when it applies `ReadWritePaths`.
  - The staged unit has `ReadWritePaths=@SEAT_ROOT@ @MAILBOX_HUB@`. The HO
    accepted this write surface; the T-2920 install reviewer verifies it.
    The install must create the hub before rendering the unit, because a
    missing `ReadWritePaths` path stops the unit from starting.
- *5d.2 follow-ups.*
  - D28: each reconcile probe spec carries `probe_nonce`, the operation's
    journaled `reconcile_probe` count, so a second probe never deduplicates
    to a retained finished probe.
  - D29: a definitive `Busy` reply to `Execute` journals `execute_voided`
    against that issuance marker. Busy is raised before any execution
    exists, and the D27 lifetime count excludes the voided issuance.
    `Denied` keeps counting. Only the broker writes the void
    (`OperationService._execute_or_void`).
- *Health1 legs* (5d.4, ruling D21, HO roster ruling).
  - `pantheon-health/v1`'s broker roster appends `operation_driver`. Legs
    are only ever appended, so readers key by name and must tolerate legs
    they don't know.
  - The driver thread keeps a lock-guarded cache, refreshed after each
    drive and every `HEALTH_REFRESH_SECONDS` (30) while idle, through
    `DriverContext.reachability` (`BusReachability`). Probes only read the
    cache, so a Health1 `Get` never blocks on the bus.
  - Leg states:
    - `operation_driver`: unhealthy when the driver failed or its thread
      died, degraded until it has recovered, ready otherwise.
    - `executor_reachability`: per registered seat.
    - `approval_reachability`: owner-uid checked.
    - A reachability observation older than 3 x the refresh interval
      reports degraded, "stale"; it never reports an old ready.
    - An unwired source stays `not_configured`.
  - Executors get no Health1 object. T-2920 wires these legs into the
    broker's health provider.
- *E29 proof* (5d.5, `tools/test_host_control_e29_matrix.py`; fixture
  evidence under D1, never relabelled live).
  - Tier A (CI) covers (apply/verify/rollback) x P0-P9 as journal cuts. A
    SIGKILLed child is recovered through the real driver, service and
    reconciler; P9 goes through the real watchdog exit (70). `verify` x P1
    is not applicable.
  - Tier B (private bus, `/usr/bin/python3`; CI skips it, T-4931) covers
    six cells: an executor kill, a broker kill, an executor name change, a
    client disconnect, a transport disconnect, and a hard-deadline exit.
    Each prints one JSON evidence line labelled `fixture`.
  - The MCP-adapter legs are cited from T-2915 (D24).

### Provisional names pending ratification

ORG-052 ratifies a closed, versioned namespace and the internal signatures
but not these strings. They were minted under the ticket shown and are
surfaced for ratification together.

| Name | Kind | Minted in |
|---|---|---|
| `org.pantheon.HostBroker1.Error.*` (the 12 T-2912 names) | broker error namespace | T-2912 OQ4 |
| `org.pantheon.HostBroker1.Error.RateLimited` | broker error | T-2917 5a (ruling i1) |
| `broker_rate_limited` | host MCP adapter error code for `…Error.RateLimited`, distinct from the adapter-local `rate_limited` | T-2917 5a (ruling i1) |
| `org.pantheon.HostBroker1.ExecutionSource` | internal interface (`OperationSpec` `"ss"→"ssss"`) | T-2917 5a (ruling i3) |
| `pantheon-host-execution-spec/v1` | execution-spec schema | T-2917 5a |
| `org.pantheon.HostExecutor1.U<uid>` | per-seat executor bus name, derived from the registry uid | T-2917 5b (ruling i5) |
| `org.pantheon.HostExecutor1.Executor` (`Execute` `"ss"→"s"`, `ExecutionGet` `"s"→"ss"`) at `/org/pantheon/HostExecutor1` | executor interface | T-2917 5b (ruling i3) |
| `org.pantheon.HostExecutor1.Error.{Denied,Busy,UnknownExecution,InvalidSpec,Unsupported,Failed,Internal}` | executor error namespace | T-2917 5b (ruling i3) |
| `pantheon-host-execution-result/v1` | executor result schema | T-2917 5b |
| `pantheon-host-executor-config/v1`, `/etc/pantheon/host-executor/config.json`, `/usr/local/libexec/pantheon-host-executor` | executor config schema, path and entrypoint (PROVISIONAL; T-2920 owns them) | T-2917 5b (ruling B5) |
| `none:read-only-verify` / `broker:read-only-verify` | verify approval/authority sentinels | T-2917 5b (ruling B2) |
| `seat-local.<operation_id>` / `pending-seat-local` / `seat_local_residue` | planner residue action ids, status and summary field | T-2917 5b (ruling B8) |
| `typed-canonical-json` / `systemctl-show-stdout` | user-manager snapshot source encodings (evidence `*_snapshot_encoding`; text keeps the historical `*_stdout_*` keys) | T-2917 5c.1 |
| `user_manager_transport` (`dbus` / `systemctl`), `user_manager_address_source` (`registry-uid` / `test-override`) | activation evidence labels | T-2917 5c.2 (ruling D7) |
| `timer-enable-rollback` / `executor-precondition` / `requires-owned-mutation-record` (with `synthesized_by: broker`) | broker-synthesized timer-enable rollback intent inverse | T-2917 5c.2 (ruling D6) |
| `rerun-diagnose` / `live-state` / `need-daemon-reload` / `manager-environment` | Verify `observation` values | T-2917 5c.2 (ruling D3) |
| `refused` | ExecutionGet state | T-2917 5d.1 (ruling D14) |
| `HARD_DEADLINE_GRACE_SECONDS = 120`, `EXIT_HARD_DEADLINE = 70`, `LIFETIME_VERIFY_REDISPATCH_CAP = 3`, `verify_redispatch_cap` | executor deadline constants, exit code, verify cap and its journal event | T-2917 5d.1 (rulings D11/D18) |
| `reconcile` | read-only execution kind, digest-selected | T-2917 5d.2 (ruling D13) |
| `spec_rearmed` / `execute_retry` / `redispatch_intent` / `reconcile_probe`, `MAX_SPEC_REARMS = 2` | journal events and re-arm cap | T-2917 5d.2 (rulings D17/D27) |
| `reconcile-probe-clean` / `rollback-redispatch` (re-arm reasons); `journal-clean` / `probe-clean` / `lost-result-mutating` / `d6-no-owned-record` / `rearm-cap` (reconcile detail codes); `reconcile_pending`, `awaiting_reconciler` (driver outcomes) | reconciler vocabulary | T-2917 5d.2 |
| `restore-from-receipt` / `receipt-mutation` with `source_receipt_handle` | rollback restore intent | T-2917 5d.2 (rulings D15/D16) |
| `mailbox_hub` (executor config v1 key), `@MAILBOX_HUB@` (staged unit substitution) | executor mailbox hub source and write surface (PROVISIONAL; T-2920) | T-2917 5d.3 (rulings D19/D32/D33) |
| `mailbox_hub_source: executor-config`; reconcile `observation: mailbox-residue` with `residue` = `absent` / `own` / `foreign` | mailbox evidence labels | T-2917 5d.3 |
| `.installer-probe-<32 hex>` / `.pantheon-rw-probe-<32 hex>` | deterministic executor probe file names | T-2917 5d.3 (ruling D20) |
| `probe_nonce` | reconcile-spec key (journal-derived) | T-2917 5d MR-2 (ruling D28) |
| `execute_voided` (refusal `Busy`, `marker_seq`) | journal event | T-2917 5d MR-2 (ruling D29) |
| `operation_driver` | `pantheon-health/v1` broker roster leg (HO ruling) | T-2917 5d.4 (ruling D21) |
| `HEALTH_REFRESH_SECONDS = 30`, staleness bound 3x, leg revision `T-2917-5d` | driver health cache constants | T-2917 5d.4 |
| `{"gate":"E29","cell":…,"fault":…,"outcome":…,"evidence_label":"fixture","transport":"private-bus"}` | E29 tier-B evidence line | T-2917 5d.5 |
| `broker:apply-compensation` | compensation approval sentinel (rollback records only; authority inherited from the failed apply, no second grant) | T-4964 |
| `broker compensation <operation_handle>` | reserved compensation idempotency-key namespace (contains a space; no caller key can reach it) | T-4964 |
| `compensation_authorized` (`source_operation_handle`, `authority_record_id`) / `compensation_refused` (`reason_code: compensation-source-mismatch`); `compensation_operation_handle` in the apply `result` payload; `compensation_error` on a build failure | journal events and payload keys | T-4964 |
| `compensation_operation_handle` | OperationGet JSON field on an apply, present only when a compensation exists (read-time, outside the record digest) | T-4964 |

## Implementation authority

T-2383 contains the reviewed Unit-1 implementation plan, exact signatures,
twenty mandatory tests, smoke loop, pre-mortem, and decision record. Its
reviewed plan SHA-256 is
`4aa4c20335684e3966bf234aaaa732957c477869ebe2ca223375d376d334288f`.
Units 2a/2b landed under the same discipline via T-2909 and T-2910 (plans,
dispositions, reviews, and gap registers on those tickets' transition logs).

Later increments require separate tickets, plans, reviews, and explicit gates
for journals/state machines, D-Bus services, MCP packaging, executors, the
privileged helper, Telegram human approval, whole-system acceptance, and
production enablement.

Passing Unit 1 does not create a capability, authorize production use, or allow
the broker to amend its own authority boundary.
