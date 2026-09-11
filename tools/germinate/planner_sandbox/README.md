# planner_sandbox — germination-engine sandbox+admission reference-implementation

**Intent.** Prove — mechanically, against a real Linux host — that the sandbox+admission subsystem of the germination engine can satisfy commitments C1–C5 of [[adr/ORG-080-germination-engine.md]]. Where the ADR names specific implementation mechanisms (`systemd-run --user` invocations, mode-0644 gate leaves at specific paths, tmpfs marker byte contents, seccomp closure sequences), those are documentary reference; this reference-implementation is free to demonstrate an equally-valid mechanism against real host semantics. Where documentary prose conflicts with what this ref-impl proves, the ref-impl wins on mechanism; where either conflicts with C1–C5, the ADR §Architectural-tier scope carve wins on outcome.

**Why a ref-impl and not more ADR prose.** The T-3273→T-3274 pivot fired because prose-only iteration kept surfacing POSIX/systemd/state-machine facts one round too late (v8 seat-home traverse + system↔user-manager ordering; v9 flock cross-process lifecycle + N-gate cardinality + systemd unit-tier vs command-tier enforcement + state-machine reconciliation). ADR iteration cannot mechanically discover the next-depth-level provider fact in advance; a minimal running implementation exercising real primitives can.

## What this ref-impl must prove

Each subsection maps a commitment to the mechanism this ref-impl demonstrates and the acceptance signal that confirms it. Acceptance is red-before-green: every mechanism ships with a test that fails before the mechanism exists and passes only when it does. Tests exercise the real host (real `flock(2)`, real `systemd-run --user`, real seccomp install), not mocks — the whole point of the pivot is that mocks are what let the provider facts slip through.

### C1 — Component-wide single-run serialization

**Outcome required.** For any `(instance, component)` pair and any admission-eligible verb, exactly one run holds admission at any moment across ALL scopes/uids that can hold or read the coordination primitive, across process-lifecycle boundaries (caller-exit while run persists), fail-closed if the primitive is unreachable.

**Mechanism this ref-impl demonstrates.** A single-holder coordination primitive whose hold survives the caller process's exit and is visible to every uid that can invoke the engine on the component. Candidate primitives to trial in red-before-green order: (a) `flock(2)` on a root-owned world-readable leaf held by a persistent helper (long-lived holder, not the caller — Cairn v9 BLOCK-1's finding is that the caller's process-exit release breaks single-run when the writer becomes the holder), (b) a broker-daemon-mediated lease with the broker as sole holder, (c) a filesystem-atomic rename claim with fsync-boundary lifecycle. The ref-impl picks one and PROVES it; alternatives are documented as considered-and-rejected with the failure signature.

**Red-before-green tests.**
1. **Cross-uid concurrent-claim.** Two processes under distinct uids attempt admission on the same `(instance, component)`; exactly one succeeds, the other refuses at the admission tier BEFORE any mutation-tier code runs. Instrument: strace both callers to prove the losing caller never opens a mutation-tier path.
2. **Caller-exit persistence (Cairn v9 BLOCK-1 witness).** Caller A claims admission, exec's a plan phase, then exits (SIGKILL from outside — no chance to clean up). Caller B under distinct uid attempts admission on the same `(instance, component)` — MUST still refuse until the run's mechanism-of-record itself transitions out of the admitting-state, regardless of caller-A's process lifecycle.
3. **Primitive-unreachable fail-closed.** Remove/corrupt the primitive underneath a live engine invocation; the next admission attempt refuses with a clearly-typed error, not a permit.
4. **N-scope enumeration.** With `system-scope + seat-<uid1> + seat-<uid2>` all having gates in the tree, admission for `(instance, component)` is single-run across all three; the "two-gate admission" prose in ADR §Base engine mechanics is documentary — this test proves the actual invariant regardless of scope count.

### C2 — Parked-resume state-machine reachability + closure

**Outcome required.** Every non-initial persisted state has ≥1 admissible incoming transition; every non-terminal persisted state has ≥1 admissible outgoing transition (including `PARKING_FAILED`, whose only admissible outgoing is operator-invoked rollback); genuinely terminal states are absorbing with defined entry conditions; reboot recovery from `HALTED reason: system-reboot` has a deterministic reconciliation path back through `PARKED` to `ACTIVE` (or a documented terminal); no reconciliation path admits an unenumerated state.

**Mechanism this ref-impl demonstrates.** A machine-checkable state-machine spec (source: `state_machine.py` — a closed enum + a transition table + an executable transition function) plus a reconciliation function that walks a real record on disk and returns exactly one of `{next_state, HALTED(reason), REFUSE_MALFORMED}`. Reboot-recovery path is EXERCISED: a fake `system-reboot` op mutates `boot_id` on a persisted record's context, the reconciler runs, the record transitions through `PARKED` back to `ACTIVE` without prose-tier ambiguity.

**Red-before-green tests.**
1. **Coverage exhaustion.** Every state in the enum appears in at least one `test_transition_from_<state>_admits_<successor>` case; a `pytest --collect-only`-style check counts states vs. covered states and REFUSES the suite if any state has zero outgoing tests (except explicitly-named terminals).
2. **Reboot-recovery trace (Cairn v9 BLOCK-2 witness).** Persist a record at `ACTIVE`; simulate `system-reboot` (`HALTED reason: system-reboot`, `boot_id` differs from `/proc/sys/kernel/random/boot_id`); run reconciler; assert record ends at `ACTIVE` again via the documented `HALTED → PARKED → ACTIVE` path, with the transitions logged in order.
3. **PARKING_FAILED escape.** Persist a record at `PARKING_FAILED`; the ONLY admissible operator verb is `germinate rollback --run <run_id>`; every other verb refuses at the state-machine gate (not at some later admission tier).
4. **Malformed-record refusal.** Persist a record with an unenumerated `state:` value; reconciler returns `REFUSE_MALFORMED` and does NOT invent a next state.

### C3 — Postboot-completion precondition, universal across invocation surfaces

**Outcome required.** Postboot completion is a precondition of every operation the engine can take on a component — regardless of surface (`germinate` CLI, systemd unit `ExecStart`, library call from another Pantheon component, any other invocation surface). Any surface that can bypass MUST be closed at the enforcement tier below where bypass can occur.

**Mechanism this ref-impl demonstrates.** A single enforcement library entry-point (`postboot.require_or_refuse()`) that every invocation surface calls before any mutation-tier code runs, PLUS a substrate-tier ordering primitive (candidate: a `pantheon-germinate.target` that every engine-consuming systemd unit `After=` + `Requires=`; or a library-level readiness gate refused by every ExecStart wrapper). The ref-impl PROVES the closure: launching a bare `python3 -c 'from pantheon.germinate import verify; verify.run()'` (library-call surface) refuses if postboot has not completed, just as `germinate verify` (CLI surface) refuses, just as a systemd component unit refuses to start until the target is reached.

**Red-before-green tests.**
1. **CLI surface refusal.** Before postboot marker exists, `germinate <verb>` for any admission-eligible verb refuses with a typed "postboot precondition unmet" error.
2. **Systemd unit surface refusal (Cairn v9 BLOCK-3 witness).** A component unit whose `ExecStart` bypasses the CLI (invokes engine internals directly) MUST fail to start until the postboot-completion primitive is reached. Test installs a minimal component unit, verifies it stays in `activating`/`inactive` until the postboot precondition holds.
3. **Library-call surface refusal.** A Python process directly imports and invokes an engine internal; the internal refuses at the postboot gate. The gate lives INSIDE the library, not at the CLI wrapper.
4. **Boot-id staleness.** Marker exists but its body ≠ `/proc/sys/kernel/random/boot_id`; every surface refuses (marker treated as stale, not present).

### C4 — Fail-closed on primitive unavailability

**Outcome required.** Every primitive (coordination lock, state store, postboot indicator) refuses the operation fail-closed when the primitive is unreachable/malformed/reports a compatibility warning at engine startup. Silent degradation to a laxer mode is forbidden. A component contract MAY NOT opt out.

**Mechanism this ref-impl demonstrates.** Each primitive's client library raises a typed `PrimitiveUnavailable(name, reason)` exception; every call site handles it by refusing the operation with the primitive name + reason in the error surface. There is NO catch-and-degrade path anywhere in the ref-impl; a `grep -rn 'except PrimitiveUnavailable'` sweep at CI-time refuses any call site that catches without re-raising or refusing.

**Red-before-green tests.**
1. **Coordination-primitive unreachable.** Rename the gate tree root; next admission attempt refuses with the primitive name in the error.
2. **State-store malformed.** Corrupt a run-record's JSON; next reconciliation refuses that record specifically without progressing others.
3. **Postboot-indicator compat warning.** Write a marker with a valid-but-warning body shape (e.g. reserved-for-future-use marker version); engine refuses at startup with the warning surface, does NOT degrade to "marker present, good enough".
4. **CI-time no-swallow lint.** A repo-wide grep proves no `except PrimitiveUnavailable` clause exists that doesn't either re-raise or produce a typed refusal.

### C5 — Sandbox capability confinement — planner is data-only, non-actuating

**Outcome required.** Any code executed within a component's planning sandbox is capability-confined: MUST NOT spawn external processes; MUST NOT mutate host state outside a defined scratch/return region; MUST NOT open communication channels to other host services or buses; MUST NOT hold or exercise authority beyond planning scratch. Planner returns typed `Operation` records only; actuation is the exclusive responsibility of the typed executor.

**Mechanism this ref-impl demonstrates.** The launcher-driven sandbox from ADR §Component contract (Python `-SEI` interpreter startup + launcher canonical startup order: `sys.path` freeze → env clear → env set → in-process seccomp `execve`/`execveat` closure → import hook → component module import → `plan_<leg>()`), housed in `systemd-run --user` transient service with the enumerated sandbox properties. The ref-impl PROVES capability confinement via a canary suite that runs INSIDE the sandbox and attempts each forbidden capability; every attempt must be denied at the primitive layer (kernel refusal, not Python exception), and the launcher must survive multiple denials in one process to record them (`SystemCallErrorNumber=EPERM`, not SIGSYS-kill).

**Red-before-green tests.**
1. **Subprocess-creation canary.** A component planner calls `os.system('true')` / `subprocess.Popen(['true'])` / `os.execv('/bin/true', ['true'])`; each returns `EPERM` from the kernel; the launcher records the denial; the plan phase overall succeeds with the typed `Operation[]` return intact.
2. **Network canary.** Planner attempts `socket.socket(AF_INET, ...).connect(...)` and `socket.socket(AF_UNIX, ...).connect(...)` (D-Bus surface); both refused at the address-family filter.
3. **Host-mutation canary.** Planner attempts to write outside the scratch dir; refused at the filesystem-visibility root.
4. **Authority-holding canary.** Planner attempts to open the coordination primitive itself; the sandbox visibility root does not expose it, so the open refuses at the OS layer.
5. **Data-only return.** Planner returns a `list[Operation]`; the typed executor validates each op against the root-owned typed-op registry; an op with an unknown `id` is refused by the executor (not by the planner). The ref-impl demonstrates the boundary is capability-shaped, not lexical — a well-typed op the planner emits doesn't actuate until the executor decides.

## Build plan — four tranches

Each tranche is a bounded sub-arc with its own ticket, its own red-before-green acceptance, and its own dispatch. The tranches are independent enough to parallelize once the shared harness (§Shared harness below) is in place; the ordering below is the reading order, not a strict serial dependency.

- **Tranche A — Admission subsystem.** `admission/` module + `flock`-based (or broker-based, or rename-claim-based; ref-impl picks and proves) coordination primitive + admission-tier refusal path + fail-closed on primitive unavailability. Covers C1 + partial C4. Acceptance: C1 tests 1–4 + C4 test 1.
- **Tranche B — State-machine subsystem.** `state_machine/` module + closed enum + machine-checkable transition table + reconciler + reboot-recovery path. Covers C2 + partial C4. Acceptance: C2 tests 1–4 + C4 test 2.
- **Tranche C — Postboot subsystem.** `postboot/` module + universal enforcement library entry-point + substrate-tier ordering primitive (target-unit or readiness-gate) + boot-id staleness handling. Covers C3 + partial C4. Acceptance: C3 tests 1–4 + C4 test 3.
- **Tranche D — Sandbox launcher.** `launcher/` module + `launcher.py` (canonical startup order) + seccomp vendoring + `systemd-run --user` invocation shape + canary suite + `nsjail_profile.md` equivalence for the systemd-user-absent fallback path. Covers C5 + the CI-time no-swallow lint (C4 test 4). Acceptance: C5 tests 1–5 + C4 test 4.

**Shared harness (implicit prerequisite for all tranches).** A `harness/` module with: (a) real-host fixture helpers (temp gate tree, temp scratch dir, cleanup); (b) a `run_in_sandbox()` helper for the canary tests; (c) a fixture that installs and tears down a minimal `pantheon-germinate.target` for the postboot tests. Harness lands as the earliest tranche work (before or alongside tranche A).

## Dispatch shape decision

Per T-3274 ask (c), the dispatch shape is a **project-orchestrator arc**, not HO-direct implementer dispatch. Justification: four subsystems + a shared harness + red-before-green tests + a canary suite exceeds the D7 single-bounded threshold; the arc benefits from an orchestrator maintaining cross-tranche coherence (all four tranches must reach C4 fail-closed consistently, the sandbox visibility root must not expose the coordination primitive, etc.). The project-orchestrator dispatches implementer + reviewer per tranche, synthesizing at tranche boundaries.

Per F5 chain-of-justification, the project-orchestrator brief carries C1–C5 forward explicitly (not by ADR reference) and requires each implementer's DECISIONS section to cite the specific C-commitment its mechanism satisfies.

## Non-goals for this ref-impl

- **Not a shippable engine.** This is a demonstration that C1–C5 are satisfiable against real host semantics. The production engine consumes what this ref-impl proves; where the ref-impl picks a mechanism the production engine has reason to substitute, the substitute must satisfy the same C-commitments and re-prove against the same acceptance suite.
- **Not a component registry, not a coverage registry, not a health plane.** Those are separate build tranches (T-2941 / T-2978 / T-2979 for health-plane per ADR §Build plan; ORG-081 for registry). This ref-impl consumes minimal fixtures.
- **Not multi-host.** Single-host semantics only. Cross-host coordination is out of scope for the germination engine per ORG-080 §Decision.

## Refs

- **[[adr/ORG-080-germination-engine.md]]** — the ratified §Architectural-tier scope carve is the source of truth for C1–C5.
- **[[adr/ORG-081-system-registry.md]]** — coverage registry the engine reads.
- **[[adr/ORG-082-broker-daemon-exposed-surface-amendment.md]]** — the substrate broker the engine's typed executor uses for runtime-seat actuation.
- **T-3273** — the v9 predecessor whose findings triggered the pivot.
- **T-3274** — this ref-impl's umbrella ticket (retargeted orchestrator 2026-08-26).
