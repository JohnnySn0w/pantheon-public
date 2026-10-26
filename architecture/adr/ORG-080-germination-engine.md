---
aliases: [ORG-080]
id: ORG-080
title: "Germination engine — apex lifecycle composer over typed component contracts (the SSEED installer/manager)"
status: Ratified 2026-08-26 by the operator via Telegram 8612 as the ORG-080 + ORG-081 + ORG-082 bundle at goal-level authority. Frozen ratify candidate at v15 (commit 845aa519a). Supersedes [[ORG-049]]. Amends [[ORG-068]] (matrix retires to [[ORG-081]] registry pointer; three-part health-leg predicate). Companion [[ORG-082]] amends [[ORG-052]] (gate 32a — same-registry-digest acceptance). Revision history at §Revision history. Amended 2026-10-17 (buildout charter, T-2667; A-3/A-5/A-7 HO-decided by orchestrator 2026-10-17, pending the operator ratification).
date: 2026-08-26
author: Orchestrator (Orchestrator)
steward: orchestrator
supersedes: ORG-049
relates: ORG-042, ORG-043, ORG-046, ORG-050, ORG-051, ORG-052, ORG-053, ORG-055, ORG-057, ORG-063, ORG-064, ORG-065, ORG-066, ORG-068, ORG-076, ORG-079, ORG-082, ORG-084, ORG-089, ORG-099, RFC-004
build_disposition: needs-build
scoping_ticket: T-2667
appetite: 8
riskiest_assumption: "Wrap, don't rewrite: an existing tool adopts the component contract through a thin adapter (probe: T-5081 on repo_reconciler)."
# ADR ↔ registry link (per D-10 of [[ORG-081]]).
# ORG-080 names day-one components (F4) that MUST have registry rows once
# [[ORG-081]]'s validator lands (T-2667 tranche a). The waiver is not admissible
# here; the components are listed. The validator is not live at bundle-ratify;
# the field is honored on landing of the [[ORG-081]] build tranche.
registers_components: [germination-engine, system-registry, host-substrate, harness-manager, action-broker, credential-broker, mailbox, scheduler, pantheon-config, health-collector]
---

# ORG-080 — Germination engine

**Status:** **Ratified 2026-08-26 by the operator via Telegram 8612** as the ORG-080 + ORG-081 + ORG-082 bundle. **Authority: goal-level.** Frozen ratify candidate at v15 (commit 845aa519a). Supersedes [[ORG-049]]. Amends [[ORG-068]] (matrix → [[ORG-081]] registry pointer; three-part health-leg predicate). Companion [[ORG-082]] amends [[ORG-052]] (gate 32a — same-registry-digest acceptance). The sandbox+admission subsystem's implementation-mechanism (kernel/POSIX and systemd behaviors) lives in the reference-implementation at `tools/germinate/planner_sandbox/` (T-3274), not this ADR; this ADR carries the architectural commitments the reference-implementation must satisfy (see §Architectural-tier scope carve). See §Revision history for the fold-chain.

**Related:** [[ORG-049]] (**Supersession, ratified 2026-08-26** — its layers become the op taxonomy, its four modes become the engine verbs), [[ORG-052]] (§8 + Unit 6 — **amended 2026-08-26** via companion [[ORG-082]], removing substrate op families from broker daemon exposed surface), [[ORG-051]] (credential broker verbs — called), [[ORG-057]] (six-step harness switch — consumed as configure sub-op b), [[ORG-063]] (isolates ruling 2 — constraint on the composer), [[ORG-064]] (scheduler render + apply — called), [[ORG-068]] (five lifecycle legs + subsystem invariants + health interface — **amended 2026-08-26** alongside), [[ORG-076]] (fleet health plane — adjacent, seam at §Health-plane seam; rev-3 restarts inside this arc per F7), [[ORG-079]] (persona-stack L1 invariants — consumed as the harness-manager component's verify block), [[ORG-053]] (SSEED north star — served). [[ORG-081]] (System Registry — companion ADR; the engine reads it), [[ORG-082]] (ORG-052 substrate-off-daemon amendment — companion for F2 reconciliation).

## Intent

Give Pantheon **one apex composer** that reads the manifest and system registry, composes every component into one plan, runs each leg (install / configure / update / remove, plus verify and repair) under the right identity per layer, gates only at declared irreversible transitions, and writes one transcript. Replaces the runbook-plus-one-off-script pattern with typed operations, staged apply, receipts, and rollback. Serves [[ORG-053]] §6 (every organ carries a full lifecycle) by giving the five lifecycle legs of [[ORG-068]] one executor instead of runbooks; serves T-2667 ("one coherent apex controller that installs/configures/monitors/removes any organ or seat") by naming what runs each leg.

The apex is **a command you run, not a service that runs.** The only ongoing processes in this design are each component's health emitter, the health collector ([[ORG-076]] plane consumer), and the scheduler's timers ([[ORG-064]]).

## Context — the gap the composer closes

The ADR tier already specifies an installer several times over: [[ORG-049]] (layers + plan/apply/verify/rollback), [[ORG-050]] (security gates), [[ORG-051]] (credential verbs), [[ORG-052]] §8 (privileged op families), [[ORG-057]] (six-step harness switch), [[ORG-064]] (timers), [[ORG-068]] (five lifecycle legs + prose coverage matrix), [[ORG-079]] (nineteen persona-stack verify invariants). **Nothing executes any of it end-to-end.** `tools/persona_installer.py` — the thing [[ORG-049]] marks `built` — exposes only `plan` and `simulate` on its CLI; its one live backend refuses any account but the caller's and "never changes Unix users, groups, ACLs, credentials, or policy"; its Layer-0 operations are `OperatorAction` tokens whose "done" is self-attested. Layer 0 itself lives only as prose in an 841-line SOP a human or persona executes §1→§9 by hand. Every live transition since (Recovery T-2590, Specialist T-2633, T-2950) was a bespoke script plus a runbook, executed command-by-command with a persona as the checkpoint monitor. Two of T-2950's three halts were preflight-catchable; none were caught because there is no preflight — there is a sheet and a reader (T-3191 log). Full prior-art map at `knowledge/episodic/2026-08-24-installer-prior-art-survey/report.md`.

The operator named the fix on 2026-08-08 (T-2667): a general seed installer/manager that **is** the main controller. The composing pieces have landed since — typed plan/receipt/rollback (`persona_installer`), two proven organ installers (`mailbox_onboard`, `consume_onboard`) plus their shared engine (`_organ_lifecycle.py`, T-2696), the scheduler renderer (T-2896), the broker's registry/journal units (`host_control/`, Units 1–2b), the two D-Bus health emitters (`org.pantheon.Health1`, `org.pantheon.PersonaController1`), and the broker arc's own root-bootstrap + executor + update-leg tickets (T-2920 / T-2917 / T-2922). What is missing is the apex.

### ADR layer map — which decisions this ADR touches, and how

| ADR | Layer | Relationship to this engine |
|---|---|---|
| [[ORG-049]] | contract — installer shape | **Superseded** by this ADR (F1) — ratified 2026-08-26 with the bundle; its four modes become the engine verbs, its layers become the op taxonomy |
| [[ORG-068]] | contract — the five lifecycle legs + system invariants | **Amended** (companion) — ratified 2026-08-26 with the bundle; principle, legs, invariants and health interface kept; the prose coverage matrix becomes a pointer to the [[ORG-081]] system registry (F3) |
| [[ORG-079]] | contract — L1 install-completeness (persona stack) | **Consumed as data**: its V1–V19 are the `harness-manager` component's invariant list; rides with this ADR rather than a separate ratification |
| [[ORG-052]] | service — the Action Broker (privileged host ops) | **Called**: engine's root path shares its typed-operation registry; Units 5/6/8 are the build that makes it real (F2) |
| [[ORG-051]] | service — credential broker | **Called**: credential verbs; installed early in the order so secrets have a home before seats need them |
| [[ORG-057]] | procedure — six-step harness switch | **Consumed**: the step shape of configure sub-op (b) |
| [[ORG-064]] | service — general scheduler | **Called**: timer render + apply; its reconciler is the timers' repair/update leg |
| [[ORG-065]] | contract — evidence / ledger obligations | **Consumes** run and operation receipts and registry status changes as evidence (§Base mechanics; D-D) |
| [[ORG-066]] | constraint — CI has no real accounts | **Shapes** the two test tiers: simulated backend in CI; disposable container/VM for the real footprint |
| [[ORG-063]] | ruling — capability isolates | **Constraint**: the composer holds no capability of its own (D-B) |
| [[ORG-050]] | contract — security gates, no secret values in tooling | **Constraint** on every leg |
| [[ORG-076]] | plane — fleet health and repair | **Adjacent**: the engine installs each component's emitter and shares the invariant vocabulary; rev-3 restarts inside this arc after ratify (F7) |
| [[ORG-042]] · [[ORG-043]] · [[ORG-046]] · [[ORG-055]] | constraints — reserved flips · seats key on account · manifest integrity · assembly lock | **Honoured** as preflight checks and design limits; [[ORG-042]]'s four reserved flips are the `reserved_authority` gate class |
| [[ORG-053]] (SSEED) | north star | **Served** — one coherent apex controller for every organ and seat |

## Architectural-tier scope carve

The sandbox+admission subsystem's architectural commitments live here (C1-C5 below). The mechanism that satisfies them — kernel/POSIX-tier and systemd-substrate-tier behaviors that must be verified against a real host to be trusted — lives in the reference-implementation at `tools/germinate/planner_sandbox/` (T-3274), not this ADR. Where later subsections in `## Decision` (§Component contract, §Base engine mechanics, §Parked-resume operator surface, §Interaction model) name specific implementation mechanisms (mode-0644 gate leaves at specific paths, `systemd-run --user` invocations, `pantheon-germinate.target` ordering, tmpfs marker byte contents, etc.), those specifics are DOCUMENTARY reference — a reader can see one worked mechanism — but the reference-implementation is free to demonstrate a different one that satisfies C1-C5 equally well against real host semantics. Inline `[Documentary — informs Cn]` markers anchor the affected passages to the relevant commitment. Where documentary prose conflicts with the reference-implementation, the reference-implementation wins on mechanism; where it conflicts with C1-C5, this section wins on outcome.

Preserved architectural outcomes elsewhere in the ADR — the component contract, `planner_kind` schema, `planner_kind: python`'s data-only-return semantics, sole-typed-executor authority, D-AC — remain NORMATIVE and are consumed by C5.

### Commitments

The sandbox+admission subsystem MUST satisfy the following architectural-tier commitments. Each names an outcome; the mechanism is the reference-implementation's responsibility.

- **C1. Component-wide single-run serialization.** For any (instance, component) pair and any admission-eligible engine verb (`plan`, `apply`, `resume`, `rollback`), exactly one run may hold admission at any moment across ALL scopes that can hold or read the coordination primitive, across process lifecycle boundaries (including caller-process exit while the run's mechanism-of-record persists), and across every uid that can invoke the engine on that component. A second admission attempt from any scope while a run holds admission MUST refuse before mutation, and MUST refuse fail-closed if the coordination primitive is unreachable.

- **C2. Parked-resume state-machine reachability + closure.** The parked-resume state machine MUST guarantee: (a) every non-initial persisted state has at least one admissible transition INTO it from a defined predecessor; (b) every non-terminal persisted state has at least one admissible transition OUT of it to a defined successor — this includes `PARKING_FAILED` and other halted-with-reason states, whose only admissible outgoing transition is the operator-invoked rollback (`germinate rollback --run <run_id>` per §Mid-leg failure) admitting the run into the rollback flow (ROLLING_BACK → ROLLED_BACK); (c) genuinely terminal persisted states (SATISFIED, ROLLED_BACK — natural completions of the forward and rollback flows respectively; and any additional terminals the state enum defines and the ADR explicitly names as having no admissible outgoing operator verb) have defined entry conditions and are absorbing; (d) reboot recovery from `HALTED` with `reason: system-reboot` has a defined path through `PARKED` back to `ACTIVE` (or to any documented terminal state) that reconciliation code can trace deterministically without prose-only ambiguity; (e) no reconciliation path admits a state the enumerated set does not name.

- **C3. Postboot-completion precondition — universal across invocation surfaces.** Postboot completion MUST be a precondition of every operation the engine can take on a component, regardless of whether the operation is invoked via a `germinate` command directly, via a systemd unit's `ExecStart`, via a library call from another Pantheon component that consumes engine internals, or via any other invocation surface. Any invocation surface that can bypass the postboot check MUST be closed at the enforcement tier below where bypass can occur.

- **C4. Fail-closed on primitive unavailability.** Every primitive named by the reference-implementation (coordination lock, state store, postboot indicator) MUST refuse the operation fail-closed when the primitive is unreachable, malformed, or reports a compatibility warning at engine startup. Silent degradation to a laxer mode is forbidden. A component contract MAY NOT opt out of any commitment in this section.

- **C5. Sandbox capability confinement — planner is data-only, non-actuating.** Any code executed within a component's planning sandbox MUST be capability-confined to not-actuate: it MUST NOT spawn processes external to the sandbox, MUST NOT mutate host state outside a defined scratch/return region, MUST NOT open communication channels to other host services or buses, MUST NOT hold or exercise authority beyond the planning scratch. The planner MUST return typed operation data only; actuation of those typed operations is the exclusive responsibility of the typed executor (§Component contract sole-typed-executor + decision D-AC).

---

## Decision

### 1. `germinate` — one command, verb × leg × scope

The apex is a command named `germinate` (alias `germ`; canonical name in receipts and transcripts is `germinate`). Not a daemon — nothing about it runs continuously (D-P). Verbs are [[ORG-049]]'s four; legs are [[ORG-068]]'s five plus repair; scope is component-scoped or seat-scoped:

```
germinate plan     {install|configure|update|remove|repair} [--all | --system <id> | --seat <account>] [--preflight]
germinate apply    {install|configure|update|remove|repair} <same scope> [--accept-gates[=class,…]] [--interactive <component>]
germinate verify   [--all | --system <id> | --seat <account>]   # read-only; self-scoped by default (see §Interaction model)
germinate rollback --run <run-id>                                # receipt-anchored undo of one attempt
germinate resume   <handle>                                      # CLI arg is the resume_handle; handle looked up via the handle-to-record index (§Base engine mechanics)
germinate status                                                 # coverage from [[ORG-081]] joined with run-state store; self-scoped by default
germinate registry {show|render}                                 # the [[ORG-068]] matrix, rendered from the [[ORG-081]] registry
germinate preflight [scope]                                      # GO/NO-GO, never mutates
```

`germinate resume` is normative here, not only in §Parked-resume prose. `germinate status` joins two distinct authorities — coverage from [[ORG-081]] (git-tracked, contract adoption + per-leg evidence) and live run-state from the run-state store (§Base engine mechanics — parked runs, `rollback_incomplete` markers, HALTED markers). The join is display-only; each side has one canonical writer.

Component-specific desired state enters a generic leg through the component's declared, typed configure schema — `germinate configure --system harness-manager --seat <account> --harness codex` maps `--harness` onto the harness-manager component's `desired.harness` field, which its schema declares with an enum; there is no free-form key/value surface, and a flag a component does not declare is a `plan` error.

Every op declares `privilege: none | seat | root`: `verify`, `status`, `plan` and most seat-scoped configure/update legs run as the seat uid; only declared root ops go through the root helper / broker.

### 2. Component contract

The component contract below is the **single authority** for `id`, `steward`, `planner_kind`, `depends_on`, `bindings`, `scopes`, `desired_schema`, `privilege`, `legs`, `gates`, `interactions`, `reserved_authority`, `rollback`, `receipts`, `emitter`, `invariant_list_id`, `invariant_list_digest`, and `contract_digest`. The [[ORG-081]] system registry stores per-instance coverage/evidence only; it does not redeclare any of these fields and its rows reference the contract by `contract_digest` (mismatch is a `plan` refusal, not a runtime error). External-repo components declare their contract in `pantheon-component.yaml` at the repo root; the central registry pins the external repo revision + contract_digest. No two documents may write the same field.

The component's `plan_*` methods run in a **sandboxed, unprivileged planning process** with a concrete structural confinement profile — not a behavioral denylist. The planner returns typed `Operation` records only (data). The engine's **sole typed executor** (in-process root for bootstrap / operator-run legs, broker daemon for runtime seats — one shared registry per F2 / [[ORG-082]]) is the only code that actuates an op, resolving the op's `id` against the root-owned typed-op registry (`tools/host_control/contracts.py`). This is the structural realization of [[ORG-063]] ruling 2 for the composer; the boundary is capability-shaped, not lexical.

**Planner-launch profile — Python planners only.** External-executable planners are refused at engine load structurally via the contract-schema `planner_kind` closed enum. The launcher uses a **transient service** (the default when neither `--scope` nor `--service` is passed to `systemd-run --user`, per `systemd-run(1)` and `systemd.scope(5)` on systemd 255) so the unit carries the full `systemd.exec(5)` sandbox context; a scope unit would only inherit the caller's execution environment. The launcher form is:

The engine launches each `plan_*` invocation via `systemd-run --user --wait --pipe --collect --unit=germinate-planner-<run_id>-<component>-<leg> --property=<K>=<V> ...` (transient service, not scope; `--wait --pipe --collect` binds stdio and auto-cleans the unit on exit). On a system without systemd-user available, an in-process **nsjail** fallback with `--seccomp_syscall_errno=EPERM` + `--seccomp_policy=<inline>` + explicit `--user_ns_uid_map`/`--mount ro` bind-mounts + a locked-down `--rlimit_*` set — the exact nsjail equivalence table is checked in at `tools/germinate/planner_sandbox/nsjail_profile.md`, red-before-green in the same acceptance fixtures below. Launcher startup MUST refuse (fail-closed, no silent degradation) when any listed property is unavailable, rejected, or reports a compatibility warning; a component contract may not opt into a laxer profile.

**Enforced properties (each named against systemd 255 semantics):**

- **No privilege elevation:** `NoNewPrivileges=yes`, `CapabilityBoundingSet=`, `AmbientCapabilities=` (empty), `RestrictSUIDSGID=yes`, `LockPersonality=yes`, `MemoryDenyWriteExecute=yes`. (These bound privilege; `NoNewPrivileges` alone does NOT block subprocess creation — that is `SystemCallFilter`'s job below.)
- **No network + no local sockets:** `PrivateNetwork=yes`, **`RestrictAddressFamilies=none`** (`none` denies all address families per `systemd.exec(5)`; an empty value would *undo all restrictions*, the opposite outcome). AF_UNIX is denied by this filter — this is what blocks D-Bus (`PrivateIPC=yes` covers only System V IPC and POSIX message queues, not AF_UNIX sockets, and cannot be relied on to block D-Bus).
- **No subprocess creation; systemcall-filter returns EPERM (not SIGSYS-kill); execve closed in-process by the planner Python code:** **`SystemCallFilter=~@process ~@privileged ~@resources ~@mount ~@debug ~@reboot ~@raw-io ~@swap ~@cpu-emulation ~@clock ~@module ~@obsolete`** + **`SystemCallErrorNumber=EPERM`**. `~execve` is NOT in the systemd-level filter — per `systemd.exec(5)` "access to the `execve()` system call is required for the execution of the service binary: if it is blocked service invocation will necessarily fail." The `execve` closure lives **in-process, inside the planner Python code**: the launcher (`tools/germinate/planner_sandbox/launcher.py`) is the very first Python code to run under `ExecStart`; before any component-supplied module is imported, it calls `seccomp.SyscallFilter(defaction=ALLOW).add_rule(ERRNO(EPERM), 'execve').add_rule(ERRNO(EPERM), 'execveat').load()`. From that point onward the process can no longer `execve` or `execveat` — the two paths that would let a compromised planner escape the import hook by re-launching itself — while systemd's initial `execve` of the launcher itself has already completed. `systemd-analyze syscall-filter @process` on this host names `clone`, `clone3`, `execveat`, `fork`, `vfork`; the systemd-level `~@process` blocks subprocess creation and `execveat`, and the in-process seccomp rule adds `execve` closure post-launch. `SystemCallErrorNumber=EPERM` makes every denied syscall at the systemd layer return `EPERM` rather than terminating the process with SIGSYS — load-bearing for the canary suite (below), which needs to survive multiple denials in one process to record them.
- **Kernel-surface protection (system-service path only; user-service path drops `ProtectControlGroups`):** `ProtectKernelTunables=yes`, `ProtectKernelModules=yes`, `ProtectKernelLogs=yes`, `ProtectHostname=yes`, `ProtectClock=yes`, `ProtectProc=invisible`, `ProcSubset=pid`. `ProtectControlGroups=yes` is retained ONLY on the system-scope launch profile (bootstrap in-process root path). Per `systemd.exec(5)` on systemd 255, `ProtectControlGroups=` "is only available for system services and is not supported for services running in per-user instances of the service manager" — its presence on `systemd-run --user` transient units causes a compatibility rejection at unit load, which the fail-closed startup refusal (below) would then convert into a permanent engine refusal. `ProtectControlGroups=` is therefore explicitly OMITTED from the user-service launcher form. cgroup-namespace escape from within the planner is bounded by `RestrictNamespaces=yes` + `PrivateUsers=yes` + the empty visibility root (below), not by `ProtectControlGroups=`.
- **Filesystem visibility root:** **`TemporaryFileSystem=/:mode=755` + explicit `BindReadOnlyPaths=` for {manifest dir, registry dir, component module dir (absolute path passed to the launcher at invocation), **`/opt/pantheon/germinate/` (the absolute-path launcher script `launcher.py` + its vendored `seccomp/` closure, root-owned mode 0755 dir + 0755/0644 leaves, installed by `germinate install --bootstrap`; this MUST be bind-mounted read-only into the planner's visibility root because the launcher is what `ExecStart=/usr/bin/python3 -SEI /opt/pantheon/germinate/launcher.py ...` executes — an absent path is a systemd unit-load failure and a partial-sandbox refusal)**, `/usr/bin/python3` (the specific interpreter binary — NOT `/usr` wholesale), `/usr/lib/python3.<minor>/` (stdlib), `/lib64/ld-linux-x86-64.so.2` (ELF interpreter), `/lib/x86_64-linux-gnu/` (multiarch shared-library dir, containing `libc.so.6`, `libm.so.6`, `libz.so.1`, `libexpat.so.1` and every other shared object `ldd /usr/bin/python3` names — binding the loader without the multiarch dir would fail `python3` at first-symbol-resolve; the multiarch dirs DO contain executable ELFs (`libc.so.6`, `ld-linux-x86-64.so.2` are executable per their file modes), but the in-process `execve` closure inside the planner Python process prevents any `execve` into these files regardless of visibility, so the design relies on the seccomp closure, not on absence-of-executables reasoning), `/usr/lib/x86_64-linux-gnu/` (secondary multiarch dir for architecture-specific stdlib companions like `_ssl` compiled extensions), `/etc/ld.so.conf.d/` for linker config} + `BindPaths=` for the single per-invocation scratch dir under `/run/germinate-planner-<run_id>/`** (`TemporaryFileSystem=/` mounts an empty tmpfs over `/`, then explicit bind-mounts define exactly what is visible; other paths are absent from the planner's mount view, not just read-only). See §"Environment + Python startup boundary" below for the environment cleanup mechanism (`python3 -SEI` at ExecStart level plus the launcher's canonical startup sequence).
- **Namespaces:** `RestrictNamespaces=yes`, `PrivateTmp=yes`, `PrivateDevices=yes`, `PrivateMounts=yes`, `PrivateUsers=yes`, `PrivateIPC=yes` (kept for System V IPC / POSIX MQ; no longer relied on for D-Bus blocking).
- **Inherited fds:** exactly `stdin` (typed operations JSON schema request), `stdout` (typed `Operation[]` response), `stderr` (diagnostics — not part of the contract; parsed only on non-zero exit). No other fd is inherited (the launcher closes 3+ explicitly before `execve`).
- **Environment + Python startup boundary — `python3 -SEI` disables `site.py` + isolates from PYTHON* + user site:** on the user-service path, `systemd.exec(5)` on systemd 255 states manager-inherited environment variables are propagated to executed processes and `PassEnvironment=` "has no effect" for services managed by a per-user instance. `os.environ.clear()` inside the launcher Python code is not by itself sufficient — per `python3.12(1)` + `site.py` source (`/usr/lib/python3.12/site.py:571-643`), the interpreter runs `import site` DURING interpreter initialization, which invokes `sitecustomize` if importable via `PYTHONPATH` or user site; that occurs BEFORE the launcher script executes. A component-controlled `sitecustomize.py` under a manager-injected `PYTHONPATH` would therefore run before the launcher's `os.environ.clear()` or seccomp install — the attack would land before the defense. The launcher runs under **`python3 -SEI`** on ExecStart to close that window:

    - **`-S`** — disables the implicit `import site` at startup. No `site.py`, no `sitecustomize`, no user site-packages, no `.pth` files processed.
    - **`-E`** — ignore all `PYTHON*` environment variables (`PYTHONPATH`, `PYTHONSTARTUP`, `PYTHONHOME`, ...) at interpreter startup.
    - **`-I`** — isolated mode; implies `-E` + `-P` (don't prepend a potentially unsafe path to `sys.path`) + `-s` (don't add user site-packages). Belt-and-braces with `-S` + `-E`.

    Combined, `python3 -SEI /opt/pantheon/germinate/launcher.py plan_<leg> <component_module_absolute_path>` gives an interpreter startup that is impervious to `PYTHONPATH`-injected `sitecustomize`, ambient `PYTHONSTARTUP`, and every documented ambient Python control that runs before the launcher script executes. **After** interpreter startup — but STILL before any component-supplied module is imported — the launcher's `main()` runs the canonical fixed startup sequence:

    **Canonical launcher startup order.** Every step below runs in this exact order in `/opt/pantheon/germinate/launcher.py` `main()`. **Launcher location:** `/opt/pantheon/germinate/launcher.py` (root-owned, mode 0755, installed by `germinate install --bootstrap`, bind-mounted read-only into the sandbox visibility root at the same path). **Launcher deps:** the `libseccomp` Python binding lives at `/opt/pantheon/germinate/vendor/seccomp/` (root-owned, mode 0755 dir + 0644 files, installed by `germinate install --bootstrap` from a pinned wheel; also bind-mounted read-only into the sandbox visibility root at the same path). Component module dirs are bind-mounted at their canonical repo paths (e.g. `/home/claude/pantheon-config/tools/germinate/components/`) and the absolute path is passed as `<component_module>` at launcher invocation time. The launcher is invoked as an absolute-path script (not `-m <package>`): under `python3 -SEI` the `tools` package is not on `sys.path`, so `-m` resolution would fail before `main()` gets a chance to install the sandbox defenses.

    1. `sys.path[:] = ['/usr/lib/python312.zip', '/usr/lib/python3.12', '/usr/lib/python3.12/lib-dynload']` — freeze `sys.path` to the stdlib-only closure the `-SEI` interpreter startup already left it at. Belt-and-braces against any later mutation.
    2. `os.environ.clear()` — hardcoded reset of the process environment (belt-and-braces with `-SEI` in case any manager-level env leaked past interpreter isolation).
    3. `os.environ.update({'LC_ALL':'C.UTF-8','TZ':'UTC','PATH':'/usr/bin:/bin'})` — the three allowed values, hardcoded constants. `PYTHONHASHSEED` is deliberately NOT set: `-E` at interpreter startup ignores `PYTHONHASHSEED` before `main()` runs, and Python initializes its hash secret from the OS RNG at startup regardless of what `os.environ` says later. Setting it here would have no effect on the current process's hash secret and would falsely imply a determinism guarantee the ADR does not make. The sandbox does not require dict-iteration determinism for its correctness properties.
    4. Insert `/opt/pantheon/germinate/vendor/seccomp` into `sys.path[0]` and `import seccomp`; install in-process seccomp filter denying `execve` + `execveat` via `libseccomp` (`seccomp.SyscallFilter(defaction=ALLOW).add_rule(ERRNO(EPERM), 'execve').add_rule(ERRNO(EPERM), 'execveat').load()`). From this point onward the process cannot re-exec.
    5. Install the module import hook restricting `sys.path` to the launcher's own dir + the declared component module dir passed as `<component_module>` at invocation, refusing dynamic import beyond the component's allowlist.
    6. Insert the component module dir (absolute path passed at launcher invocation) into `sys.path[0]`, then import the component's declared module (the first exposure to component-supplied code).
    7. Call `plan_<leg>(...)` on the component.

    The interpreter startup itself is guarded by `-SEI`; steps 1-5 are the launcher's belt-and-braces on top of that. Step 6 is the earliest point at which any component-controlled code can execute — after `sys.path` freeze, after environment reset, after seccomp closure, after import restriction.

    **On systemd-run at unit-launch time:** `systemd-run --user --wait --pipe --collect --unit=germinate-planner-<run_id>-<component>-<leg> --setenv=LC_ALL=C.UTF-8 --setenv=TZ=UTC --setenv=PATH=/usr/bin:/bin --property=<sandbox properties> /usr/bin/python3 -SEI /opt/pantheon/germinate/launcher.py plan_<leg> <component_module_absolute_path>`. `--setenv` provides the initial values; `-SEI` isolates interpreter startup; the launcher's `main()` runs the canonical sequence above. Invoking the launcher by absolute path (rather than `-m tools.germinate.planner_sandbox.launcher`) is required because under `-I -P` `sys.path[0]` is not prepended and the tools package would not be resolvable — but Python still executes a script named by path, and the launcher's step 1 re-freezes `sys.path` to the stdlib-only closure with every subsequent path insertion pointing to an absolute-path bind-mounted directory that the sandbox visibility root exposes.
- **Working directory:** the scratch bind-mount above. **Module search path:** the component contract's declared module path, resolved by absolute path at launch — no `$PYTHONPATH`, no ambient `sys.path` addition. Dynamic import beyond the declared module is refused by an import hook that mirrors the module allowlist. This hook is load-bearing only because the in-process seccomp rule (above) closes `execve`/`execveat` post-launch — the two together prevent both re-exec into a fresh Python interpreter that skips the hook AND in-process arbitrary import.
- **Runtime bound:** **`RuntimeMaxSec=`** (default 30s, per-component override in the contract). `RuntimeMaxSec=` is the actual runtime cap; on expiry the unit is terminated with `SIGKILL`. `TimeoutStopSec=` retains its normal role (how long systemd waits for `ExecStop=`) and is not the runtime bound.

**External-executable planners — DEFERRED to a follow-on ADR revision.** The ADR supports ONLY Python planners at bundle adoption. The component contract schema (§Component contract below) carries a normative `planner_kind` field with a **closed enum whose only accepted value is `python`**. A component contract declaring `planner_kind: external` (or any other value, or omitting the field) is refused at engine load with a clear error message ("component `<id>` declares `planner_kind: <value>`; only `python` is accepted. External-executable planners are deferred to a follow-on ADR revision; contract-schema field is closed to prevent silent admission."). The schema-mandatory closed enum makes the refusal structural at load time, not a runtime-prose obligation. No day-one component in the F4 list requires an external planner (harness-manager, mailbox, action-broker, credential-broker, scheduler, host-substrate, health-collector, system-registry, germination-engine, pantheon-config all declare `planner_kind: python` or carry no planner). The Python-only closure is load-bearing on the in-process `execve` seccomp rule inside the launcher, which cannot cover an external planner whose entrypoint IS the component-supplied binary: routing an external binary through the Python launcher would fail because the launcher's post-install `execve` is denied EPERM. Follow-on ADR work will design an external-planner sandbox mechanism — candidates include (a) a supervisor shim written in a compiled language that installs seccomp before `execve`ing the target binary using state-tracking BPF; (b) a distinct nsjail-based profile for the external-planner path; (c) a `SECCOMP_RET_TRAP`+SIGSYS-supervisor design. None of these are in-scope for this bundle. When the follow-on ADR lands, it will extend the closed enum to include `external` and pair that admission with the enforcement mechanism.

**Launcher-startup refusal (fail-closed, no silent degradation).** `systemd-analyze verify` per its man page (systemd 255) loads unit files and detects unknown directives, dependencies, documentation errors, and executable-path errors; it does NOT prove that the running kernel + user-manager enforces each sandbox feature. `systemd.exec(5)` explicitly warns that many sandboxing features are "silently and gracefully turned off" when the mechanism is unavailable. A syntax-only probe therefore CANNOT establish the promised no-silent-degradation result. The ADR requires a **suite of isolated enforcement canaries** run at engine start, each canary invocation routed through the launcher (`/opt/pantheon/germinate/launcher.py --canary=<test-name>`) so the launcher's steps 1-5 (sys.path freeze, environ reset, seccomp install, import-hook install) run before the canary probe fires. The probe is a canary-branch inside `main()` that runs after step 5 and before step 6 (which is skipped for canary invocations — no component module is imported when `--canary=...` is passed).

The mechanism uses two roles distinctly — the **OUTER germinate command** runs unsandboxed as root or seat uid, retains normal syscalls and D-Bus, and is what invokes `systemd-run` to create units; the **INNER sandboxed launcher** (`/opt/pantheon/germinate/launcher.py`) runs INSIDE each unit and has execve/D-Bus/spawn denied by seccomp+SystemCallFilter, so the inner launcher cannot spawn siblings — orchestration is exclusively the outer command's job.

1. **The OUTER germinate command** writes N transient probe units at engine startup (one per denial the sandbox promises), each with the exact profile above and `SystemCallErrorNumber=EPERM` so denied syscalls return errno rather than SIGSYS-killing the process. `ExecStart=` for each unit is `/usr/bin/python3 -SEI /opt/pantheon/germinate/launcher.py --canary=<test-name>` — the same launcher binary as the planner path, invoked in canary mode (`--canary` argument selects which single denied operation the canary branch attempts). The canary branch inside `main()` runs steps 1-5 (sys.path freeze, environ reset, seccomp install, import-hook install) exactly as the planner path does, then attempts exactly ONE denied operation and writes the observed outcome (EPERM / ENOENT / EACCES / success) to a scratch file inside its bind-mounted scratch dir before exiting. The inner launcher never itself invokes `systemd-run` or any subprocess — only reads/writes the scratch file.
2. **One denial per unit invocation.** A single canary running all probes in sequence would be fatal: the FIRST successfully-denied call under a SIGSYS-terminating filter kills the process before it can write subsequent probe outcomes. Splitting into N units (subprocess-clone, subprocess-fork, self-replace-execve, subprocess-execveat, AF_INET-connect, AF_UNIX-connect, shadow-open, out-of-scratch-write, dynamic-import, ambient-env-check, pythonpath-sitecustomize-injection) means each canary either exits cleanly having recorded the denial (via EPERM return, courtesy `SystemCallErrorNumber=EPERM`), OR is killed / succeeds — either of which the launcher reads as a probe FAILURE (fail-closed).
3. **The OUTER germinate command** waits for each canary unit's completion (via `systemd-run --wait`), reads each canary's scratch file, and refuses to start the germination engine if ANY probe reports success or a missing scratch file (indeterminate → refuse). The inner sandboxed launcher plays no orchestration role here — it exited after step 2's write.
4. Each canary unit is cleaned up (`--collect`). The engine STARTS only when every promised denial has been proven at engine boot on the actual running kernel + user manager.

Refusal is loud (stderr + non-zero exit + no `germinate` command available); a partial-sandbox engine is not admissible. The canary sources are branch cases inside `/opt/pantheon/germinate/launcher.py`'s `main()` (selected by `--canary=<name>`); the acceptance table lands at `tools/germinate/planner_sandbox/enforcement_probe.md`, red-before-green in CI. **A component contract may not opt into a laxer profile.** On the nsjail fallback, the launcher runs the equivalence-table self-test AND the same isolated per-denial canaries (invoked via nsjail on the same absolute-path launcher form) at start and refuses on any red.

**Acceptance covers** (each malicious fixture is required in CI, run against the *real* launcher on a systemd-user host; the CI job that lacks systemd-user runs the nsjail-fallback fixtures instead):
- **Subprocess/fork** → REFUSE with EPERM (blocked by `SystemCallFilter=~@process` + `SystemCallErrorNumber=EPERM` denying `clone`/`clone3`/`fork`/`vfork`/`execveat`).
- **Self-replace via execve** → REFUSE (closed by the **in-process seccomp filter** installed by `tools/germinate/planner_sandbox/launcher.py` before the component-supplied module loads. `execve` cannot be closed by systemd's `SystemCallFilter` because per `systemd.exec(5)` execve is required for systemd's own launch of `ExecStart` — the filter would prevent the service from starting. In-process seccomp installs the filter AFTER systemd's exec of the launcher completes, so systemd's exec succeeds and every subsequent execve/execveat attempt inside the process returns EPERM).
- **D-Bus (`org.freedesktop.DBus` connect)** → REFUSE (blocked by `RestrictAddressFamilies=none` denying AF_UNIX).
- **Network (any AF_INET connect)** → REFUSE (blocked by `PrivateNetwork=yes` + address-family filter).
- **Filesystem escape (open `/etc/shadow`, write outside scratch)** → REFUSE (`/etc/shadow` returns ENOENT because `/etc` is not bind-mounted into the planner's visibility root; write outside `/run/germinate-planner-<run_id>/` is EROFS/EACCES on the tmpfs mount).
- **Interpreter presence + library closure** → PASS (`/lib64/ld-linux-x86-64.so.2` binds the ELF loader AND `/lib/x86_64-linux-gnu/` + `/usr/lib/x86_64-linux-gnu/` bind the shared-library closure `ldd /usr/bin/python3` names. Without the library dirs the loader would find its interpreter but fail at first-symbol-resolve on libc/libm/libz/libexpat).
- **Ambient environment isolation** → PASS (`python3 -SEI` isolates interpreter startup from `PYTHONPATH`/`PYTHONSTARTUP`/user site/`sitecustomize`; `systemd-run --setenv=...` pins the **three** allowed values at unit-launch time (`LC_ALL`, `TZ`, `PATH`); the launcher's canonical startup order runs `os.environ.clear()` + fixed re-set to the same three names before any component code executes. A canary reading its ambient env sees exactly the three allowlisted names, and no `PYTHONPATH`-injected `sitecustomize` can execute before the launcher's cleanup + seccomp install).
- **PYTHONPATH-injected sitecustomize** → REFUSE (`-SEI` disables `site.py` at interpreter startup, so `import site` doesn't run and `sitecustomize` isn't invoked regardless of `PYTHONPATH`; a hostile PYTHONPATH sitecustomize.py canary confirms no execution before launcher `main()`).
- **Dynamic import of an undeclared module** → REFUSE (import hook, load-bearing only because in-process seccomp closes execve/execveat).
- **Read from inherited fd >2** → REFUSE (fd not present).
- **Actuate typed-op registry directly (bypass executor)** → REFUSE (registry loader path not in the visibility root).
- **External-binary planner exec** → REFUSED at engine load (a component contract declaring `planner_kind: external`, or any value outside the closed enum `{python}`, or a contract omitting `planner_kind` entirely, is refused at engine load by the schema validator with the "external planners deferred" error. External-planner sandboxing is a follow-on ADR; the acceptance table proves the STRUCTURAL refusal rather than the substrate-level enforcement that follow-on ADR will design).

Each fixture ships as a component variant in the CI acceptance suite (`tools/germinate/acceptance/planner_sandbox/`), one per denied capability, red-before-green with the launcher property assertion first (the fixture's `plan()` receives a fresh sandbox each invocation).

Every registered component is a Python module (in `pantheon-config` or an external repo speaking the same YAML contract) declaring `planner_kind: python` — the closed enum accepts only that value. External-executable planners are structurally refused at engine load until the follow-on ADR extends the enum. The component implements one contract that the engine calls through the sandboxed planning boundary above (an execution seam like `_organ_lifecycle.py` may exist underneath the sole executor for op-implementation reuse — never inside a planner):

```yaml
component:
  id: harness-manager                 # registry key == [[ORG-068]] row; authority: THIS document
  steward: orchestrator                      # authority: THIS document
  version: 1
  planner_kind: python                # closed enum, only `python` accepted; contract-schema validator refuses any other value or an omitted field. External-executable planners deferred to a follow-on ADR.
  contract_digest: sha256:<hex>       # SHA-256 of the contract module's canonical serialization (see §Component contract for the exact procedure); registry rows reference this and validation refuses mismatch at plan
  depends_on: [host-substrate, mailbox, action-broker, credential-broker]   # authority: THIS document
  bindings: [scheduler, action-broker, credential-broker]                   # engine synthesizes <target>-seat-binding plan nodes; authority: THIS document
  desired_schema: {harness: {enum: [claude, codex]}, supervisor: {enum: [consume, bespoke]}}
  scopes: [system, seat]
  invariant_list_id:    org.pantheon.persona-stack/v1   # stable id for the one invariant list this component ships
  invariant_list_digest: sha256:<hex>                    # both `verify` and emitter MUST report against this exact list; validator refuses drift
  privilege:                          # THREE DISJOINT LISTS; authority: THIS document
    bootstrap_root: [account.ensure, group.member, linger.enable, unit.system.install, sudoers_fragment.ensure, dbus_policy.install, acl.converge]   # in-process root only ([[ORG-082]]); never on broker daemon exposed surface
    seat:           [unit.user.install, daemon-reload, unit-fire, timer-enable, mailbox-probe, file.atomic-write]
    broker_runtime: [unit.user.install, daemon-reload, unit-fire, timer-enable, mailbox-probe]                                                      # must be subset of the runtime enum in tools/host_control/contracts.py AND disjoint from every substrate/self-amendment family per [[ORG-082]] (which amends [[ORG-052]] acceptance 32)
  legs:
    install:   plan_install(manifest, registry, scope)     -> [Operation]   # persona_installer shape; runs sandboxed (BLOCK-4)
    configure: plan_configure(...)                         # sub-ops: supervisor-cutover | harness-swap ([[ORG-057]] §3 each)
    update:    plan_update(source_rev_from, source_rev_to) # state-preserving; auto-stage vs auto-apply declared
    remove:    plan_remove(manifest, registry, scope)     -> [Operation]    # decommission ONLY; undoing an attempt is rollback(run_receipt)
    verify:    invariant_list_ref: org.pantheon.persona-stack/v1@<digest>   # the SAME list the emitter reports; adapter/capability-qualified per [[ORG-079]]; read-only
    repair:    plan_repair(verify_result)                  # converge the diff, nothing else
  gates:                              # OPERATION gates: typed transition hazards with typed approval contracts
    - {id: authority-transfer, irreversible: true, rollback: receiver-restore,
       approval: {class: independent-agent, grant_source: action-broker-approval-registry, binds: &binds [actor, performer, target, operation, plan_digest, scope, policy_revision, expiry]}}
    - {id: decommission, irreversible: true, approval: {class: independent-agent, grant_source: action-broker-approval-registry, binds: *binds}}
    - {id: credential-revoke, irreversible: true, note: "OAuth refresh-token cliff", approval: {class: independent-agent, grant_source: credential-broker, binds: *binds}}
    - {id: group-restart-on-live-seat, irreversible: false, approval: {class: operator}}
    - {id: sudoers-self-lockout, irreversible: true, note: "visudo -cf + sudo -l evidence before commit", approval: {class: independent-agent, grant_source: action-broker-approval-registry, binds: *binds}}
  interactions:                       # NECESSARY EXTERNAL interactions: a human must operate an external system.
                                      # Not plan-approval grants; may create external credential or authority state. Never suppressible.
                                      # resume_check / abandon_check / revoke_evidence are check_ids referencing the closed interaction-check registry
                                      # (interaction_check_specs/, schema pantheon-interaction-check/v1) — each spec has typed params, privilege, output schema, timeout, error mapping.
                                      # English prose descriptions are illustrative; the check_id + params tuple is what the engine executes.
    - {id: oauth-onboarding,       external_effect: remote_authorization,
       resume_check:    {check_id: claude-auth-status-loggedin,           params: {seat: harness-manager.seat}},
       abandon_check:   {check_id: seat-home-credential-present,          params: {seat: harness-manager.seat, artifacts: [".config/anthropic/session"]}},
       rollback: external-interaction-required,
       revoke_evidence: {check_id: claude-auth-logout-verified,           params: {seat: harness-manager.seat}}}
    - {id: bot-token-issuance,     external_effect: credential,
       resume_check:    {check_id: credential-broker-ref-bound,           params: {ref_name: "telegram.bot_token"}},
       abandon_check:   {check_id: botfather-mybots-lists-ref,            params: {ref_name: "telegram.bot_token"}},
       rollback: external-interaction-required,
       revoke_evidence: {check_id: botfather-revoke-and-broker-invalid,   params: {ref_name: "telegram.bot_token"}}}
    - {id: deploy-key-upload,      external_effect: remote_authorization,
       resume_check:    {check_id: forge-accepts-key-readonly,            params: {repo: "<manifest.forge_repo>"}},
       abandon_check:   {check_id: forge-lists-key-fingerprint,           params: {repo: "<manifest.forge_repo>"}},
       rollback: external-interaction-required,
       revoke_evidence: {check_id: forge-fingerprint-absent,              params: {repo: "<manifest.forge_repo>"}}}
    - {id: sudo-password-placement, external_effect: none,
       resume_check:    {check_id: file-mode-and-owner,                   params: {path: "/home/claude/root.env", mode: "0600", owner: "root"}},
       abandon_check:   {check_id: file-present,                          params: {path: "/home/claude/root.env"}},
       rollback:        {check_id: shred-and-remove-file,                 params: {path: "/home/claude/root.env"}, privilege: root},
       revoke_evidence: {check_id: file-absent,                           params: {path: "/home/claude/root.env"}}}
  reserved_authority:                 # [[ORG-042]] / operator-reserved flips; class operator-reserved, grant_source operator; never pre-acceptable.
    - {id: seat-plan-flip}
    - {id: bot-token-rebind}
    - {id: exocortex-agents-enabled}
    - {id: off-host-replication}
  rollback:  rollback(run_receipt)                       # receipt-scoped undo of ONE attempt, engine-internal, any leg; a different verb from remove (D-E)
  receipts:  schema: pantheon-receipt/v1
  emitter:                            # what the engine INSTALLS for the health plane
    bus: system | session
    interface: org.pantheon.Health1 | org.pantheon.PersonaController1
    invariant_list_ref: org.pantheon.persona-stack/v1@<digest>   # SAME as legs.verify.invariant_list_ref
    reports: {states: [ready, degraded, unhealthy, not_configured, suppressed]}
    collector_attachment: required                        # emitter alone does not satisfy E9.C3; the health-collector must be subscribed AND the alert path must deliver, verified at leg-close
```

**Canonical `contract_digest` computed by a single named oracle.** The digest is not self-referential AND is not implementation-portable — the earlier approach ("emit canonical YAML by any conforming implementation") is retired because YAML canonicalization is under-specified: `PyYAML 6.0.1` produces different bytes for the same mapping under `allow_unicode=False` vs `True`, and neither is "wrong"; scalar quoting/escaping, Unicode normalization/escaping, sequence indentation, explicit document markers, aliases/tags, and final newline are all implementation-choice surfaces the v3 rules did not fix. Rather than close every canonicalization surface (a multi-week grammar-writing exercise for a value nothing else consumes), v4 names one authoritative computation and bounds it:

**The `contract_digest` computation is defined solely by `tools/germinate/registry/contract_digest.py::compute_contract_digest(module_path: Path) -> str`.** This function is the sole oracle. Concretely:

1. `compute_contract_digest` loads the contract module (YAML) using PyYAML's `safe_load`.
2. It walks the mapping to the schema-located `contract_digest` field (per §Component contract: exactly one `contract_digest` field at the top-level `component:` mapping — validator refuses a contract module carrying more than one occurrence at any nesting level, so "walk the mapping" is unambiguous).
3. It REPLACES that one field's value with the fixed sentinel string `"SHA256_DIGEST_PLACEHOLDER"` (exactly 25 chars: `len("SHA256_DIGEST_PLACEHOLDER") == 25`).
4. It re-emits the mapping using an in-repo canonicalizer (`_canonical_yaml_emit`) that is byte-deterministic by construction: sorted keys top-to-leaf via `sort_keys=True`, block style forced (`default_flow_style=False`), `allow_unicode=True` (Unicode passes through as UTF-8 rather than being ASCII-escaped — the choice is pinned, not left to callers), `default_style=None`, `line_break='\n'` (LF), `explicit_end=False`, `explicit_start=False`, `indent=2`, `width=float('inf')` (no line wrapping), and a post-emit strip of trailing whitespace on every line + a single trailing `\n`. The exact PyYAML/`ruamel.yaml` version and parameter set is pinned in the helper.
5. It returns `sha256:<hex>` of the resulting bytes.

Verification (at `plan`) invokes the same function; a component whose written `contract_digest` differs from the recomputed value is a `plan` refusal. **Cross-implementation portability is explicitly not claimed, and there is no equivalent-implementation carveout** — a reviewer or auditor recomputing the digest MUST invoke this exact Python function. A second implementation is not the sole oracle, and one golden fixture cannot prove byte equality over the full accepted YAML input domain. If a non-Python auditor needs to reproduce the digest, they run this Python function via the reference container image the CI job publishes (SHA-pinned in `tools/germinate/registry/contract_digest.py`). A future revision may replace this with a formal grammar; the rule for now is: one implementation, one oracle, no ports.

**Golden vectors.** `tools/germinate/registry/contract_digest_test.py` ships a checked-in golden fixture with a sample contract and its expected `sha256:<hex>` value, red-before-green under CI. Any change to `_canonical_yaml_emit` that produces different bytes on the golden fixture fails CI; the digest is stable across pinned dependency versions or the CI job refuses the dependency bump.

**ORG-082 §2a's expected-runtime-subset digest inherits this exact procedure** (same helper, same sole-oracle contract) — see [[ORG-082]] §2a for how it applies to the `broker_runtime` op-family list union.

**Operation-level fields** (returned by each `plan_*` leg method). Every operation extends `persona_installer`'s `InstallOperation` shape (`layer, kind, target, precondition, desired, inverse, privilege, owner, provenance`) with two additional required fields validated at `plan`:

- **`crash_contract: stageable | write-ahead-inverse-durable`** — how the op survives a crash mid-execution. `stageable` records inverse intent, stages disabled/non-authoritative state, verifies the stage, then activates. `write-ahead-inverse-durable` records preimage + inverse intent durably in the run manifest **before the first mutation**, commits the intent only after the postcondition passes. Host-substrate ops (`account.ensure`, `group.member`, `acl.converge`, `linger.enable`, sudoers replacement) take the write-ahead path. An op that can declare neither is refused at `plan`. Both contracts require a **kill-injection test** between intent-persist / mutate / receipt-commit; a component whose crash contract cannot pass that test is inadmissible.
- **`side_effect_free: true`** (required on every `verify` op) — the probe changes nothing on the seat it inspects. Checked structurally at `plan` alongside `llm_free`. Rationale: the 2026-08-24 T-3192 incident — `claude mcp list` as a probe spawned a second Telegram poller on a live seat and 409-killed the live one.

**Components never call each other.** A component returns typed operations plus the capability/output requirements those operations have; it does not call another component's `apply`. The engine alone expands a request into component + binding plan nodes, orders them by the DAG, executes them under the right identity, handles their gates, and writes their receipts. The import-lint that would refuse `plan` on a component module importing another's apply surface is a hygiene check, NOT the enforcement. The load-bearing enforcement is the sandboxed planner boundary (above): a planner without subprocess/network/D-Bus/dynamic-import capability cannot call another component regardless of whether it tried to import it. External executables enforce the same boundary at the process/interface seam.

### 3. System registry (see [[ORG-081]])

The [[ORG-068]] prose coverage matrix retires. A validated `registry/systems.yaml` — one record per component: `id`, `steward`, `contract_digest` (reference to the component contract's authoritative fields per §Component contract), per-leg **coverage** status with evidence refs / gap+ticket / waivers, and adoption fields — is the composer's coverage input and the matrix's rendering source. [[ORG-081]] is the companion ADR that fixes the schema, closed status enum (`covered` + evidence; `partial`/`missing` + gap + ticket; `partial_by_design`/`not_applicable` + waiver), the `contract_adoption` vs `legacy_coverage` distinction, discriminated union between component records and external-plane records, and the render + validator tools.

The coverage registry is **static contract-coverage only** — it does not hold live run state (parked runs, rollback markers, HALTED markers, active-run manifests all live in the run-state store per §Base engine mechanics). The registry rows also do not redeclare DAG / privilege / emitter / gate / interaction fields — those are authoritative in the component contract module, and the registry references the contract by `contract_digest`. External-repo components declare their contract in `pantheon-component.yaml` at the repo root and the registry pins the repo revision + `contract_digest`; any digest mismatch is a `plan` refusal.

### 4. Base engine mechanics

**Two stores, one writer each:**

The engine reads and writes two structurally distinct stores. Neither overlaps the other; a display surface may join them read-only. Each has one canonical writer.

| Store | Home | Writer | Reader | Holds |
|---|---|---|---|---|
| **Coverage registry** ([[ORG-081]]) | `registry/systems.yaml` in `pantheon-config` | git commits (reviewed) | engine at `plan` + `germinate registry show/render` + [[ORG-065]] closure validation | static contract-adoption + per-leg coverage evidence + waivers |
| **Run-state store** (this ADR) | RUN-RECORDS at `/var/lib/pantheon/germinate/run-state/` (system-scope) or `~/.local/state/pantheon/germinate/run-state/` (seat-scope), mode 0700 subtree per §File modes below. GATES live at the system-shared gate tree `/var/lib/pantheon/germinate/gates/{system\|seat-<uid>}/<instance>/<component>/_component_gate.json` (per §Gate location) — separated from run records because gates need cross-scope read authority and the seat-scope run-state store's `/home/<seat>/` prefix defaults to 0750 (not cross-seat traversable), whereas run records need only same-scope read. Gates and run records are two related but structurally distinct filesystem locations | the executor for the run that opens the record (in-process root for bootstrap; per-seat executor for seat legs) | the engine at `plan`, `apply`, `resume`, `rollback`, `status` | live run manifests (correlation-id, participating ops, receipts written), `HALTED` markers, `rollback_incomplete: <run-id>` mutation gates, `PARKED` interaction records (with only the non-secret resume_handle), operational per-component per-leg state |

Records are keyed by the tuple `(instance, component, scope, leg, run_id)` and the store is atomic-file-per-record (temp-file + rename) with **two layered locks: a per-record `record.lock` protecting the one record's atomic write, AND a cross-scope component tmpfs lock at `/run/pantheon/germinate/component-locks/<instance>-<component>.lock` protecting the component-wide mutation gate below.** Receipts update the run-state record; the coverage registry updates only when contract adoption changes and only via git commit. A `germinate status` join surfaces both for display; joins are read-only.

**Versioned run-record schema + bijective tuple-to-path mapping + component-wide mutation gate + crash-safe two-file transaction.** The store is not a free-form file tree; every run record conforms to schema `pantheon-run-state/v1` normatively defined at `tools/germinate/run_state/schema.py`. The schema is JSON, UTF-8, LF-terminated, and enforces these top-level fields on every record (unknown-field rule: extra fields are ignored on read but a `plan` refuses to write a record containing unknown fields; schema-discriminator drift is a fail-closed refusal):

| Field | Type | Semantics |
|---|---|---|
| `schema` | const string `pantheon-run-state/v1` | schema discriminator; a record whose value differs refuses reads until reconciled |
| `tuple` | object `{instance, component, scope, leg, run_id}` | the record's key, embedded so a stray-file check is single-file |
| `state` | enum `PENDING`\|`ACTIVE`\|`PENDING_PARK`\|`PARKING`\|`PARKED`\|`VERIFYING`\|`PARKING_FAILED`\|`HALTED`\|`SATISFIED`\|`ROLLING_BACK`\|`ROLLED_BACK`\|`ROLLBACK_INCOMPLETE` | canonical state — closed enum covering every persisted state used in this ADR: `PENDING_PARK` (parking transaction step 1), `PARKING`/`VERIFYING` (§Parked-resume operator surface), `PARKING_FAILED` (non-terminal reconciliation-failed halted state — its only admissible outgoing transition is operator-invoked `germinate rollback --run <run_id>` per C2, admitting the run into the rollback flow ROLLING_BACK → ROLLED_BACK). A record whose `state` is outside this enum is a validator refusal; transitions are typed (§Parked-resume + §Mid-leg failure); no free-form status |
| `operations` | list of `{op_id, crash_contract, precondition_check, postcondition_check, staged_receipt?}` | the plan's ops in DAG order; each carries its committed crash contract |
| `receipts` | list of `{op_id, receipt_ref, committed_at}` | already-written receipts; a resume replays from the last entry |
| `halted` | nullable `{op_id, reason, at}` where `op_id` is `<string \| "system-reboot">` | non-null when `state == HALTED`; the op that failed and why. `op_id` is the failing op's identifier for normal HALTED transitions; the sentinel value `"system-reboot"` is the only accepted non-op value, used exclusively by reboot recovery when a run was interrupted by reboot before any op committed (see §Reboot recovery). Any other non-op string is a validator refusal. |
| `rollback_incomplete` | nullable `{failed_run_id, outstanding_inverses}` | non-null when `state == ROLLBACK_INCOMPLETE`; list of unreversed ops from an earlier rollback attempt |
| `parking` | nullable `{resume_handle, check_id, check_params, parking_authority_uid, at, instruction}` | non-null when `state == PARKED`; secret `resume_token` lives at `resume/token.secret`, never in the record |
| `created_at` / `updated_at` | ISO-8601 UTC | audit timestamps; monotonically increasing per record |

The tuple-to-path mapping is bijective and URL-safe encoded — no implementation choice about which file carries which marker:

- **Root:** `<scope_root>/schema-v1/<instance>/<component>/<scope>/<leg>/<run_id>/record.json` where `<scope_root>` is `/var/lib/pantheon/germinate/run-state` for system-scope legs and `~/.local/state/pantheon/germinate/run-state` for seat-scope legs (seat home resolved from the seat-registry, not `$HOME`).
- **Canonical segment encoding + rejecting decoder:** each tuple segment (except `run_id`) is RFC-3986 percent-encoded using the unreserved set (`ALPHA / DIGIT / "-" / "." / "_" / "~"`) as no-encoding, all other bytes percent-encoded as **`%XX` with lowercase hex only**. The canonical decoder REJECTS: (a) uppercase or mixed-case hex (only `%2f` is accepted; `%2F`, `%2f%2F`-mixed, `%FF` are rejected); (b) percent-encoding of any unreserved byte (`%2d` for `-` is refused; the unreserved form is normative); (c) malformed UTF-8 in the decoded byte sequence; (d) segment length >64 bytes post-encoding; (e) empty segments. Percent-decoding is exactly one pass — a segment containing `%25xx` decodes to `%xx`, not to the byte `xx` would encode. Two path strings cannot decode to the same tuple.
- **Canonical `run_id` (ULID):** 128-bit ULID rendered as **Crockford base32, 26 chars, uppercase only, alphabet `0123456789ABCDEFGHJKMNPQRSTVWXYZ`** (I/L/O/U excluded per Crockford spec). The canonical decoder rejects lowercase, any non-alphabet character, or length ≠ 26; **ULID timestamp bytes must decode to a UTC ms epoch ≤ `now + 60s`** (a ULID from the future is refused; a small forward-clock-skew allowance is preserved). Monotonicity within a single run is enforced by the ULID library's monotonic-generator contract; cross-run lower-bound plausibility is not a safety property this schema owns. The mapping is bijective because both encode/decode directions are total pure functions on the canonical form.
- **File modes.** The mode responsibilities split cleanly by location:

    - **Run-record subtree** (`<scope_root>/schema-v1/<instance>/<component>/<scope>/<leg>/<run_id>/`): mode `0700`, owned by the writing uid (root for system-scope, seat-uid for seat-scope). `record.json` inside is mode `0600`, owned by the writing uid. Run records stay in the scope_root; only the calling scope needs to read its own run records, and the reconciler (root) can traverse any seat home. The scope_root ancestor chain (`<scope_root>/`, `<scope_root>/schema-v1/`, `<scope_root>/schema-v1/<instance>/`, `<scope_root>/schema-v1/<instance>/<component>/`) is mode `0700`, owned by the writing scope's principal — cross-scope traversal here is NOT required (the gates that need cross-scope read live at their own location per §Gate location).
    - **Gate tree** (`/var/lib/pantheon/germinate/gates/{system|seat-<uid>}/<instance>/<component>/_component_gate.json`): per §Gate location, gate ancestor dirs mode `0755` root-owned (with seat-scope's leaf `<component>/` dir owned by seat-uid group root mode `0755` so the seat has write on the gate leaf); gate leaf file mode `0644`, owned by writing-scope's principal (root for system-scope; seat-uid for seat-scope). Any uid can `open(O_RDONLY)` any gate file via the full absolute path — the 0755 ancestor chain is world-traversable AND world-readable (safe because the tree contains only gate files, not run records), and the 0644 leaf is world-readable. **[Documentary — informs C1, per §Architectural-tier scope carve. The specific mode/ownership scheme is an implementation-mechanism; the C1 outcome (cross-scope reachability of the gate under a coordination primitive) is what's normative. Reference-implementation per T-3274 may realize C1 with a different primitive.]**

    A record whose file mode or ownership is wrong is treated as tampered and refuses reads until repaired.
- **Per-record lock:** sibling `record.lock` (single-writer `flock(LOCK_EX)`) protects the temp-file + rename atomic-write for that one record.
- **Component-wide mutation gate.** A root-owned, non-replaceable, run-lifetime advisory lock with world-readable gates. Three constraints are load-bearing: (a) group-membership escalation must not be able to unlink or replace the lock file (Linux `flock(2)` locks the open file description, not the pathname, so a replacement inode is a fresh lock domain — group-writable directories break the "single lock" claim); (b) the lock must be held through the entire run, not just admission (a lock released before mutation begins does not serialize mutation, and a fresh admission after the release could observe an outdated gate and admit a concurrent run); (c) an unprivileged seat must be able to read the sibling scope's gate (`active_run_scope_root` cross-visibility is required for cross-scope serialization).

    - **Path + permissions (root-owned, non-replaceable).** `/run/pantheon/germinate/component-locks/<instance>-<component>.lock` — tmpfs. The DIRECTORY is created at `germinate install` with owner root, group root, mode `0555` (no write for any user, including root — only root operating as `germinate install` may create/unlink lock files, and only during bootstrap). Lock files inside are created at `germinate install` per component (registered under F4) with owner root, group root, mode `0444`. **A group-membership escalation cannot unlink or replace a lock file because no seat uid has write on the directory.** Any uid can `open(O_RDONLY)` the lock file and call `flock(fd, LOCK_EX)`; per Linux `flock(2)` the advisory lock does not require the fd to be writable — only that the file be openable, which the 0444 mode allows universally. This gives a single, replacement-proof lock domain.
    - **Lock lifetime — held for the ENTIRE RUN, not just admission.** The executor holds the tmpfs lock from admission through the run's terminal state (SATISFIED / ROLLED_BACK / PARKED — parking releases the lock, since a PARKED run is not actively mutating and a subsequent `germinate resume <handle>` re-acquires it for the resume phase). Long-running runs therefore continuously hold the lock; concurrent admission from either scope will block on `flock(LOCK_EX)` until the run terminates. If the executor process crashes without releasing, the kernel releases the flock on process exit, which the reconciliation matrix then classifies as an in-flight-crash prefix (with the durable gate record + `boot_id` distinguishing pre-boot vs same-boot).
    - **Gate location — system-shared gate tree, world-readable (0644) beneath a world-traversable ancestor chain.** BOTH scope_roots' `_component_gate.json` live under a system-shared world-traversable tree, decoupled from seat home (a seat-home-anchored gate cannot be cross-scope-readable on the deployed 0750-home default; POSIX pathname access rules stop `namei -l` at another seat's `~/.local` with `Permission denied` regardless of what leaf mode the gate carries):

        - **System-scope gate:** `/var/lib/pantheon/germinate/gates/system/<instance>/<component>/_component_gate.json`
        - **Seat-scope gate:** `/var/lib/pantheon/germinate/gates/seat-<uid>/<instance>/<component>/_component_gate.json` (one subtree per seat uid, keyed by numeric uid resolved from the seat-registry; a seat uid resolves to exactly one seat identity so `seat-<uid>` is bijective).

        **[Documentary — informs C1, per §Architectural-tier scope carve. Specific paths/modes are an implementation-mechanism; C1 requires only that the coordination primitive be reachable across ALL scopes/uids that hold or read it.]** The ancestor tree `/var/lib/pantheon/germinate/gates/`, `.../gates/system/`, `.../gates/seat-<uid>/`, `.../<instance>/`, `.../<component>/` is installed at bootstrap with owner `root`, group `root`, mode `0755` (world-executable + world-readable — read is safe because the tree contains gate files only, no run records; a directory listing exposes only which `<instance>` and `<component>` names have gates, which is non-sensitive by design — the gate contents themselves carry no authority-bearing tokens). The `_component_gate.json` leaf under `/var/lib/pantheon/germinate/gates/system/...` is root-owned mode `0644`; the leaf under `/var/lib/pantheon/germinate/gates/seat-<uid>/...` is owned by the seat uid mode `0644` (writable-by-owner-only; the seat's parent dir `seat-<uid>/<instance>/<component>/` is created at bootstrap with owner `seat-<uid>`, group `root`, mode `0755` so the seat has write permission on the gate file but the ancestor tree stays root-owned to prevent replacement/unlink attacks). Any uid can `open(O_RDONLY)` either scope's gate via the full absolute path, on the deployed 0750-home shape, without depending on any seat-home mode. Only the GATE files live under this shared tree; run records stay at their scope_root locations (`<scope_root>/schema-v1/...` under system scope_root and seat home respectively).

        The bootstrap that creates `/var/lib/pantheon/germinate/gates/seat-<uid>/` per registered seat (system-scope: root; seat-scope: seat uid ownership on the leaf dir) is a `germinate install --bootstrap` step, verified at bootstrap-complete by real-uid `namei -l` probes against a canary gate written by each seat. Gate contents (`active_run_id`, `active_run_scope`, `active_run_leg`, `active_run_scope_root`, `boot_id`, `rollback_incomplete`, `updated_at`, `updater_uid`) are non-sensitive; the run's authority-bearing tokens (resume tokens, secrets) live elsewhere per §Interaction model.
    - **Acquire order.** Every `plan`, `apply`, and `resume` for that `(instance, component)`, on either scope path, `open(O_RDONLY)`s the tmpfs lock file (fail-closed refusal if the lock file is missing — bootstrap did not register this component), calls `flock(LOCK_EX)` (blocking, or `LOCK_NB` with a bounded wait per callsite policy), THEN reads BOTH scope_roots' `_component_gate.json` files. The admission table below distinguishes the CALLING scope's gate ("this-scope gate") from the OTHER scope's gate ("other-scope gate"). `plan` checks BOTH gates because it establishes cross-scope serialization; `apply` and `resume` check ONLY the this-scope gate for `active_run_id == this run`, because the admission transaction writes only the this-scope gate (§Gate + record admission transaction below) — after a normal single-gate plan write, the other-scope gate remains at `active_run_id: null`, so an "apply requires both gates" predicate would be unreachable by every normal plan/apply sequence. The other-scope gate remains readable during apply/resume for reconciliation and audit; it is not part of the admission predicate at those stages. Refuses per the following normative admission table:

        | Verb | Gate state observed | Admission decision |
        |---|---|---|
        | `plan` | BOTH gates `active_run_id == null` and `rollback_incomplete == null` | ADMIT (proceed to write record + this-scope gate per admission transaction; other-scope gate not written) |
        | `plan` | Either gate `active_run_id != null` (some OTHER run active) | REFUSE (fail-closed; only rollback-retry or gated `remove` on the SAME run admissible below) |
        | `plan` | Either gate `rollback_incomplete != null` | REFUSE unless the plan is a rollback-retry of the referenced run |
        | `apply` | **THIS-scope gate** `active_run_id == this run` and `state == ACTIVE`/`PENDING`; other-scope gate not part of predicate | ADMIT (continue in-flight run under the held lock) |
        | `resume` (of a `PARKED` handle) | **THIS-scope gate** `active_run_id == the handle's run_id`, gate.boot_id == current boot_id, record.state == `PARKED` | ADMIT — same-run identity check on this-scope gate's `active_run_id` MUST match the handle's run_id byte-for-byte; if this-scope gate shows a DIFFERENT `active_run_id`, another run has claimed the gate since this run parked, and resume is REFUSED (the operator must consult the reconciler / operator surface before retrying) |
        | `resume` (of a `PARKED` handle) | **THIS-scope gate** `active_run_id == null` and this-scope gate.boot_id != current boot_id (the boot_id mismatch proves the null came from reboot recovery, NOT from a concurrent unrelated clear) and `rollback_incomplete == null`, AND the run's `record.json` still exists at its schema-v1 path with `state: PARKED` and `parking_authority_uid` matches the invoking uid | ADMIT — the gate was cleared by reboot recovery; re-claim under the resume verb (writes a fresh admission transaction with `active_run_id: <the run's ULID>`). The boot_id-mismatch and record-check gate together prove this is a legitimate post-reboot resume and not a race with another admission |
        | rollback-retry (of `ROLLBACK_INCOMPLETE`) | Either gate `rollback_incomplete.failed_run_id == this run` | ADMIT (rollback-retry is the ONLY verb that clears `rollback_incomplete`) |
        | gated `remove` | Both gates any state | ADMIT (component removal is gated at the operation level; the admission gate observes the component going away, not the reverse) |

        Parking releases the tmpfs lock atomically as part of the parking transaction step 7 (§Handle-to-record parking transaction below — unchanged from v7); resume re-acquires it via the `resume` row above. The tmpfs lock is held for the entire run lifetime described above, EXCEPT while a run is `PARKED`. A crash while `PARKED` leaves the gate as-is (kernel-released flock; gate.boot_id lets reconciliation classify pre-reboot vs same-boot); the reboot classifier below handles the re-entry.
    - **Gate record schema.** `{active_run_id: <ULID | null>, active_run_scope: <string | null>, active_run_leg: <string | null>, active_run_scope_root: <path | null>, boot_id: <36-char-dashed-uuid>, rollback_incomplete: {failed_run_id, outstanding_inverses} | null, updated_at, updater_uid}`. `boot_id` is a mandatory schema field in the canonical `/proc/sys/kernel/random/boot_id` form: 36 characters, four dashes, lowercase hex (e.g. `4a5b6c7d-89ab-cdef-0123-456789abcdef`), read verbatim (no dash removal, no re-formatting). **Every gate write includes `boot_id`** in the atomic-rewrite step (per admission transaction step 3 below); a gate record whose `boot_id` differs from the current boot's id is a stale-pre-boot record (see reboot recovery below).
    - **Reboot recovery — runtime postboot marker at the germinate-command tier.** Because tmpfs is empty on boot, the lock files must be re-created by `germinate install --reboot-recover` (an idempotent bootstrap step run by a `germinate-postboot.service` systemd system unit). Enforcing "postboot before components" via cross-manager systemd-target ordering (a system-manager target with `After=` edges in user-manager component units) does not work: `systemctl --system show <unit>` inspects the SYSTEM manager, not the user manager that owns each component unit, and a user-manager `After=pantheon-germinate.target` resolves against a target of the same name in the SAME manager, not the system-manager one. The gate therefore lives at the germinate-command tier via a runtime completion marker:

        - **Postboot marker. [Documentary — informs C3, per §Architectural-tier scope carve. The tmpfs marker file is an implementation-mechanism; C3 requires only that postboot completion be a precondition of every operation across every invocation surface. Reference-implementation per T-3274 may satisfy C3 with unit ordering, a library-level readiness gate, or another substitute.]** `germinate-postboot.service` (system-manager unit ordered `After=local-fs.target Before=multi-user.target`) runs the idempotent bootstrap step and, on success, atomically writes a marker file at `/run/pantheon/germinate/postboot-complete.marker` (tmpfs, root-owned mode `0644` world-readable, temp+rename). The marker's body is the current-boot's 36-char boot_id, so every consumer verifies `cat marker == /proc/sys/kernel/random/boot_id` before trusting it (a marker left over from a prior boot's success is distinguishable from a fresh one). If the postboot step fails, the marker is NOT written.
        - **Runtime check at germinate-command startup.** Every `germinate` command (`plan`, `apply`, `resume`, `rollback`, `status`, `verify`, `repair`) reads `/run/pantheon/germinate/postboot-complete.marker` at startup as its first action. If the marker is absent, or its body doesn't match `/proc/sys/kernel/random/boot_id`, the command refuses fail-closed with a clear error: `germination-engine postboot has not completed on this boot; retry after germinate-postboot.service succeeds. Marker: <absent|stale (marker_boot=<x>, current_boot=<y>)>`. The check is at the germinate-command tier, not at the systemd-unit tier — component units start as usual under their own user managers, but the germinate command they invoke refuses until postboot marks itself complete. Cross-manager boundaries are no longer a concern because the enforcement lives inside the germinate command, which every scope invokes.
        - **Why the runtime marker is sufficient.** No `germinate plan/apply/resume` runs against a component unless postboot has completed on the current boot. That is stronger than an activation-order edge (which could be raced on a fast reboot) and portable across the system/user manager boundary. Component units may still start pre-postboot (their own systemd-unit metadata governs that), but the FIRST germinate command they invoke will refuse — the postboot bootstrap step must complete on the same boot before any germinate op takes effect.

        On the same boot pass, engine startup reads `/proc/sys/kernel/random/boot_id` (36-char dashed form, byte-identical to what the schema stores), then walks both scope_roots' gate records (at their §Gate location paths); for each gate whose `active_run_id != null`, compares `gate.boot_id` against the current boot id. **Mismatch (stale) → the previously-active run was interrupted by reboot**; routed to the reconciliation matrix as a pre-boot terminal state (transitioned to `HALTED` with `halted.op_id: "system-reboot"`, `halted.reason: "interrupted-by-reboot"`, `halted.at: <boot-time-approx from /proc/uptime + wall clock>`). **Match (same boot, tmpfs lost the lock, engine restart after crash)** → the previously-active run was interrupted by an executor crash within this boot; routed to the reconciliation matrix as an in-boot crash (any receipts committed apply; incomplete transaction rolled back via the parking/admission matrices below).

**Cross-scope lock rationale. [Documentary — informs C1 + C4, per §Architectural-tier scope carve. The specific 0755 / 0700 mode split and same-filesystem gate/record scheme is an implementation-mechanism artifact; C1 requires only that admission serialize component-wide across scopes/lifecycles, and C4 requires fail-closed on primitive unreachability. Reference-implementation per T-3274 may retire, replace, or redesign these mechanism specifics.]** Root-owned + non-replaceable + world-readable-gates + world-traversable-ancestor-chain is the minimum trust base sufficient to enforce single-lock-domain semantics across the F2 tri-context isolate model. A group-write scheme is insufficient — group members can replace the lock inode, which breaks the "single lock" claim because Linux `flock(2)` binds to the open file description; a root-owned dir + 0444 lock file removes that surface without adding a runtime broker dependency. Holding the lock for the whole run (not just admission) is the invariant Consultant's finding requires — a lock released before mutation begins does not serialize mutation. Gate world-readability is the minimum information exposure sufficient to close the seat-cannot-read-sibling observation gap; the gate contents carry no authority-bearing tokens (per §Interaction model). Locating the gate tree on a shared world-traversable path (not under a seat's home) makes world-readability actually reachable on the deployed 0750-home shape — the run-record subtree stays at 0700 to preserve run-record confidentiality.
- **Gate + record admission transaction.** Gate admission and run-record creation are two files on the same filesystem; POSIX gives no cross-file atomic rename. The transaction defines an **ordered pair of atomic writes** so every crash prefix is reconciler-classifiable:

    1. **Acquire cross-scope tmpfs lock** at `/run/pantheon/germinate/component-locks/<instance>-<component>.lock` (`flock LOCK_EX`). Read BOTH scope_roots' `_component_gate.json` — the 0755 gate-tree ancestor-chain above lets any uid traverse to either scope's gate, and the 0644 gate leaf lets any uid read it, so BOTH gates are readable from EITHER scope's admission path (seat-scope admission reads system gate; system-scope admission reads seat gate as root). Refuse per the admission table above (§Acquire order).
    2. **Write record.** Atomically write the run's `record.json` at `<scope_root>/schema-v1/<instance>/<component>/<scope>/<leg>/<run_id>/record.json` with `state: PENDING` (temp+rename per §Atomic replacement rule). The run is admitted-in-store but not yet gated.
    3. **Write THIS-scope gate.** Atomically rewrite the calling scope's `_component_gate.json` at `/var/lib/pantheon/germinate/gates/{system|seat-<uid>}/<instance>/<component>/_component_gate.json` (scope-selector picks `system` if calling scope is system, `seat-<uid>` with the calling seat's numeric uid if calling scope is seat) with `{active_run_id: <new_run_id>, active_run_scope, active_run_leg, active_run_scope_root, boot_id: <current /proc/sys/kernel/random/boot_id verbatim, 36 chars with dashes>, rollback_incomplete: null, updated_at: <ISO-8601 Z>, updater_uid: <writing uid>}`. **Only the this-scope gate is written**; the other-scope gate is unchanged (the admission-predicate table above checks only the this-scope gate at apply/resume; cross-scope serialization is established by the plan-time read of BOTH gates in step 1). Once this rename returns, the run holds the mutation gate AND the gate carries the current boot_id required by the reboot-recovery classifier.
    4. **Optional flip.** Rewrite `record.json` with `state: ACTIVE`. This is a same-file rewrite the executor performs before firing the first mutating op; a crash here leaves `record.json` at `PENDING` with the gate already claimed, which the reconciler treats as ACTIVE-with-no-ops-yet (no receipts to replay).
    5. **Retain the tmpfs lock.** The executor holds `flock(LOCK_EX)` on the tmpfs lock file continuously from step 1 through the run's terminal state (SATISFIED / ROLLED_BACK / PARKED). A crash between step 5 and the first mutating op leaves the record at `PENDING`/`ACTIVE` with the gate claimed and the flock kernel-released on process exit; the reboot classifier + reconciliation matrix (below) recover.

    **Terminal-state release.** On terminal state:

    - **SATISFIED / ROLLED_BACK** — the executor performs the completion sequence: (a) atomically rewrite `record.json` to `state: SATISFIED` (or `ROLLED_BACK`); (b) atomically rewrite the THIS-scope `_component_gate.json` at its gate-tree path (`/var/lib/pantheon/germinate/gates/{system|seat-<uid>}/<instance>/<component>/_component_gate.json`) clearing `active_run_id`, `active_run_scope`, `active_run_leg`, `active_run_scope_root` (leaving `boot_id`, `updated_at`, `updater_uid` set to the current values; other-scope gate unchanged); (c) release the tmpfs lock (`flock LOCK_UN` on the held fd, then close fd). Subsequent admissions on this `(instance, component)` acquire the lock afresh.
    - **PARKED** — the parking transaction (§Handle-to-record parking transaction below) already sequences the record write, handle write, index write, and gate remains unchanged (`active_run_id` still names this run so a resume can identity-check). Parking releases the tmpfs lock as its terminal step (release is atomic w.r.t. the parking commit — the flock is released only after the record reaches `PARKED`). Subsequent admissions on this `(instance, component)` may acquire the lock and read the gate showing `active_run_id == <this run's ULID>`; per the admission table above, only a `resume` of THIS run's handle (or a rollback-retry) is admissible while `active_run_id` names a live parked run.

    **Reconciliation matrix for the admission transaction** (executor start, holding the cross-scope tmpfs lock, before any new plan is admitted; complements the handle/index matrix below):

    | Prefix state on disk | Cause | Reconciler action |
    |---|---|---|
    | `_component_gate.json.active_run_id == null` + no record.json for the run | pre-step-2 crash (no state written) or plan refused | nothing to reconcile |
    | `active_run_id == null` + record.json exists for run X at `state: PENDING` | step-2 succeeded, step-3 crashed | unlink orphan record (`<run_X>/record.json` + parent dir) — orphan admission never got the gate; a fresh `plan` may re-admit |
    | `active_run_id == run_X` + record.json for X at `state: PENDING` | step-3 succeeded, step-4 crashed (or executor died before firing first op) | rewrite record.json to `state: ACTIVE`; the run may resume from operation 0 (no receipts yet) |
    | `active_run_id == run_X` + record.json for X at `state: ACTIVE` and receipts | normal in-flight run | no reconciliation; resume the run per §Mid-leg failure |
    | `active_run_id == run_X` + record.json for X at terminal state (SATISFIED / ROLLED_BACK) | step-5 crashed after final record write but before gate clear | atomically clear the gate (idempotent completion of run close) |
    | `active_run_id == run_X` + no record.json for X on disk | tampering signal (gate claims a run whose record vanished) | fail-closed alert; refuse further admission until operator investigation |

    Plan completion (SATISFIED / ROLLED_BACK) executes the terminal-state release above: atomically clears `active_run_id`, `active_run_scope`, `active_run_leg`, `active_run_scope_root` in the gate (leaving `boot_id`, `updated_at`, `updater_uid` current) and releases the tmpfs `flock`. Run cleanup (record retention per policy) happens after the lock is released. Two runs of the same component serialize on the cross-scope tmpfs lock regardless of which scope_root either lives in, and every crash prefix of the admission transaction has one defined reconciler outcome.

- **Atomic replacement rule:** writer writes `record.json.tmp.<pid>` (or `_component_gate.json.tmp.<pid>`) in the same directory (same filesystem), `fsync` the file, then `rename()` over the target, then `fsync` the parent directory. Any partial `*.tmp.<pid>` file is unlinked on next reconciliation scan.
- **Parking slot** (§Parked-resume) is `<...>/<run_id>/resume/` (a subdirectory of the run record, same mode as parent): `resume/handle.json` (public — `resume_handle`, `check_id`, `check_params`, `parking_authority_uid`, `at`, `instruction`) mode `0600`; `resume/token.secret` (secret — `resume_token` bytes only; may be absent if the check needs no secret) mode `0600`. Parking is thus a defined child of the run record; the resume `<handle>` looks up the record via the handle index below.

**Handle-to-record parking transaction.** `resume/handle.json` (in the run's dir) and `schema-v1/handles/<handle>.json` (the index pointer, in a sibling top-level dir) cannot be one atomic POSIX rename because they are on different directories, and the run's `record.json` is a third file. The transaction:

1. **Snapshot.** Writer holds the cross-scope tmpfs lock, records the current record state in `<...>/<run_id>/parking/_prior_state.json` (temp+rename+fsync; contents `{prior_state: <state>, at: <ts>}`). This is the reconciler's rollback target if the transaction is abandoned mid-flight.
2. **Prepare record.** Writer atomically rewrites `record.json` to `state: PENDING_PARK` (temp+rename+fsync). After this rename returns, the record durably reflects that a park attempt is in flight.
3. **Prepare handle (immutable authority).** Writer atomically writes `<...>/<run_id>/resume/handle.json.tmp.<pid>` → fsync → `rename` to `resume/handle.json` → fsync dir. **`resume/handle.json` is immutable once written** — a subsequent parking re-attempt writes a fresh directory under a fresh `run_id`, never overwrites.
4. **Commit index.** Writer writes `schema-v1/handles/<handle>.json.tmp.<pid>` → fsync → `rename` to `schema-v1/handles/<handle>.json` → fsync dir. Duplicate handle at insert-time is a fail-closed replay guard (rename fails with `EEXIST` because we use `RENAME_NOREPLACE` where available, and re-check `openat(O_EXCL)` where not).
5. **Flip state.** Writer atomically rewrites `record.json` with `state: PARKED` (parent dir fsync).
6. **Cleanup snapshot.** Unlink `parking/_prior_state.json` (best-effort; a stale snapshot on a `PARKED` record is inert — the reconciler ignores it).
7. **Release** cross-scope tmpfs lock.

**Consume-to-tombstone (mirror of the above):**

1. Writer holds the cross-scope tmpfs lock, atomically rewrites `record.json` with `state: SATISFIED` (or `ROLLED_BACK`).
2. Writer writes `<handle>.tombstone.json.tmp.<pid>` → fsync → `rename` to `schema-v1/handles/<handle>.tombstone.json` → fsync dir. Tombstone is `{consumed_at, run_root, consumed_reason: satisfied|rolled_back}`, mode `0600`, bounded TTL 30 days.
3. Writer unlinks `schema-v1/handles/<handle>.json` → fsync dir.
4. Release cross-scope tmpfs lock.

**Reconciliation for every prefix state.** On executor start (before any new plan is admitted), the reconciler holds the cross-scope tmpfs lock per (instance, component) and enumerates, using the durable `record.json.state` × `resume/handle.json` × `handles/index` × `parking/_prior_state.json` product to classify:

| Prefix state on disk | Which step crashed (parking transaction above) | Reconciler action |
|---|---|---|
| `record.json` at any non-`PENDING_PARK` state, `_prior_state.json` present, no handle.json, no index | step-1 succeeded, step-2 crashed. Snapshot is durable but the record was never rewritten from its prior state; nothing external changed. | unlink snapshot; no state change (record already reflects the "prior" state the snapshot would restore, so restoration is a no-op) |
| `record.json` at `PENDING_PARK`, no handle.json, no index, `_prior_state.json` present | step-2 succeeded, step-3 crashed | roll back record to `_prior_state.json.prior_state`; unlink snapshot |
| `record.json` at `PENDING_PARK`, handle.json present, no index, snapshot present | step-3 succeeded, step-4 crashed | unlink `resume/handle.json` (immutable-authority rule allows unlink on abandonment); roll back record to prior state; unlink snapshot |
| `record.json` at `PENDING_PARK`, handle.json present, index entry present, snapshot present | step-4 succeeded, step-5 crashed | complete step-5: rewrite `record.json` to `state: PARKED`; unlink snapshot (idempotent completion — the run parked successfully) |
| `record.json` at `PARKED`, handle.json present, index entry present | normal parked state | no reconciliation |
| `record.json` at `PARKED`, handle.json present, no index entry | index vanished after `PARKED` (tampering signal — post-transaction unlink) | fail-closed alert; refuse resume; leave for operator |
| `record.json` at any state, no handle.json, index entry present | index-only orphan (theoretically impossible given write order — treated as tampering) | unlink index + log ALERT |
| Any run has stale `*.tmp.<pid>` files | crash between temp-write and rename | unlink |

**Consume-to-tombstone reconciliation** (mirror of parking):

| Prefix state on disk | Cause | Reconciler action |
|---|---|---|
| `record.json` at `SATISFIED`/`ROLLED_BACK`, live index, no tombstone | consume step 2 crashed | write tombstone then unlink index (idempotent completion) |
| `record.json` at `SATISFIED`/`ROLLED_BACK`, tombstone present, live index | consume step 4 crashed after tombstone but before index unlink | unlink index (idempotent completion) |

**Reconciliation-failed halted escalation.** If reconciliation cannot classify a prefix (e.g. `record.json` at `PENDING_PARK` with no snapshot AND no handle — a corruption of the snapshot itself before step-2 completed, or an operator's manual edit), the reconciler rewrites `record.json` to `state: PARKING_FAILED` (a non-terminal reconciliation-failed halted state in the closed enum, per C2 §Architectural-tier scope carve) and logs the escalation; operator invokes `germinate rollback --run <run_id>` per §Mid-leg failure, which admits the run into the rollback flow (ROLLING_BACK → ROLLED_BACK) as C2 requires. The rollback is a same-record transition, not a new record with independent authority.

**Reconciliation policy:** encountering an `unknown` schema version refuses (does not silently skip); encountering a record whose `tuple` embedded value disagrees with its filesystem path refuses (tampering signal). The reconciliation is idempotent — running it twice produces the same store state.

- **Verbs** unchanged from [[ORG-049]]; `plan` is privilege-free and reports drift; `apply` executes only the approved digest; `verify` is read-only; `rollback` is receipt-anchored.
- **Identity per layer**: root for L0 + cross-account; `sudo -u` / `systemd-run --user` / `machinectl shell` to the seat uid for per-account legs (the "run as the target account with its own user bus" rule the SOP repeats nine times becomes structural).
- **Gates**: only operation gates a component declares (with their approval contract) and reserved-authority gates from policy; a gate is a typed prompt on the transcript with the rollback pre-armed and the required grant checked before any mutation. Necessary external interactions park the run, they do not gate it. Preflight + staging never gate.
- **Failure routing has a bootstrap-safe floor**: durable run record + stderr + non-zero exit unconditionally; emitter flip + page only when those routes already exist and answer; `notification: unavailable (<prerequisite>)` otherwise. A fresh install can fail before the very emitter it is installing exists; the degraded reporting path is a fresh-box acceptance case.
- **Transcript + receipts**: one correlation-id run manifest listing every mutation, written to the **run-state store**; per-op schema-versioned receipts; the run manifest is the rollback anchor; receipts feed [[ORG-065]] as evidence pointers. The **coverage registry** is not on this path.
- **Idempotent re-entry**: plan-from-current-state; already-satisfied preconditions verify-not-mutate (persona_installer's managed-marker convergence, generalized); resumable from the last clean receipt in the run-state store after an abort.
- **Testability, two tiers**: (a) every component runs against `SimulatedHostBackend`/fake root in CI ([[ORG-066]] has no real accounts) — zero privilege, unit-level; the sandboxed planner boundary is part of every CI run (a planner that tries to subprocess/network/D-Bus/dynamic-import returns REFUSE, verified per component); (b) a disposable systemd container or VM where accounts, units, ACLs and the whole footprint are real but throwaway — that is where `germinate install --all` acceptance runs, and where the root backend is proven ([[ORG-052]] gate 32 shape + the [[ORG-082]] same-registry-digest gate) before any live `--execute`.
- **A command, not a daemon** (D-P): the engine has no resident process; state lives in the run-state store + the coverage registry + receipts. Anything that must observe continuously belongs to the health plane (§Health-plane seam) or the scheduler ([[ORG-064]]).

### 5. Harness Manager first (the paradigm component)

The `harness-manager` component is the first organ the engine composes. Its engine: `persona_installer` (plan/receipt/rollback/staged-apply), `consume_onboard` (fresh-seat path), `persona_cutover` (configure sub-op a — bespoke-supervisor → `pantheon-persona-consume@`), `runtime_controller` + `proactive_swap_init` (configure sub-op b — claude ↔ codex; `runtime_controller` also owns tmux/session and remains **NOT** installer work per [[ORG-079]] carve-out; the engine names it as owner), `codex_telegram_bridge` primitives (the codex receiver sub-component). [[ORG-079]] V1–V19 are its verify block.

A harness-runtime swap becomes `germinate configure --system harness-manager --seat <account> --harness codex`: preflight → stage (unattended, disabled-first) → gate: authority-transfer → swap → verify (text/doc/photo canaries via `telegram-unattended-evidence`, mailbox roundtrip, receiver-state, restart-offset — as invariants, not operator steps; a human sender only for the one real-inbound canary, and that is the gate's prompt) → receipt. Restore is the same sub-op in reverse with its own gate.

Absorbs T-2889 (real-host apply + full-seat decommission) and T-3191 (T-2950 wrapper). Recovery's nine co-review criteria land as: preflight sudo-scope vs declared privilege + typed cross-account handoff op; `source-hold` op or per-op re-verify (chosen by declared window length); canaries as invariants; run transcript; attempt-scoped rollback via run manifest; plan-from-current-state; component-declared gates; `--preflight`; `--seat` scope.

### 6. Health-plane seam

**Health is a separate plane, not an installer stage** (D-J).

| | Installer (germination engine) | Health plane ([[ORG-068]] §health interface + [[ORG-076]]) |
|---|---|---|
| Verb | mutates state toward the manifest; `verify` is invoked, point-in-time | observes and emits, continuously |
| Unit of work | component leg → plan → apply → receipt | emitter property set → `PropertiesChanged` → collector |
| Where | one CLI, operator-run | D-Bus: system bus for privileged services (`org.pantheon.Health1`), session bus per persona (`org.pantheon.PersonaController1`) |
| Centralizer | run manifest in the run-state store (per run) + coverage registry (per leg) | [[ORG-076]] §7 operator plane — per-persona rollup + mute registry (T-2978 → recovery, T-2979 → orchestrator); Recovery alerting subscribes to `PropertiesChanged` |
| Failure → action | `verify` FAIL → `repair` (converge) | detector verdict → episode store (dedup) → **repair ticket** ([[ORG-076]] §3) → repair persona runs `germinate repair --system <id>` **or** an installer-independent break-glass path (R10) |

**Centralization.** The health plane has one **collector** — a registered component (`health-collector`, steward recovery) the engine installs like any other — that subscribes to every emitter's `PropertiesChanged` on both buses and re-exposes the union as one subscription: the [[ORG-076]] detector's input, and what alerting, the repair-ticket emitter and any operator surface consume. kino's Health Plane section (T-2979) is an operator legibility affordance on top of that subscription, not a core plane element.

**Three touch-points, and only three:** (1) the engine **installs** each component's emitter (unit + `OnFailure=` leaf + D-Bus exporter registration) — a provision like any other, verified and repaired like any other; (2) **one invariant list per component**, identified by a stable `invariant_list_id` and pinned by an `invariant_list_digest` (per §Component contract), feeds both `verify` (invoked) and the emitter's `Status`/`Reasons` (continuous) — the validator refuses drift between them, so the one-vocabulary rule is machine-enforced, not a prose pointer; (3) the health plane's repair-ticket names the component id and the failing invariant, which is exactly `germinate repair --system <id>`'s input. Recovery's monitor-contract adds belong on the emitter side: report `owner ≠ observed manager` as a distinct state; a `suppressed`/maintenance state that is not `healthy` and not `unhealthy` ([[ORG-076]] §7.1 mute ≠ blind); hook symlink + allowlist parity as an invariant on both surfaces.

**Emitter-install alone does NOT satisfy E9.C3.** The health-leg coverage cell (in the coverage registry) requires **three** pieces of evidence, not one: (a) the emitter is installed and reporting; (b) the emitter's `invariant_list_ref` and the component's `verify` `invariant_list_ref` resolve to the same `<id>@<digest>` (validator-checked); (c) the health-collector is subscribed AND at least one alert-path attempt has proven delivery (a plumbed-through unhealthy transition observed at the operator surface, OR an explicit `waiver:` keeping the cell non-`covered` with the ratified E9.C3 obligation acknowledged). An orphan emitter with no subscription cannot promote the cell to `covered`; the amendment to [[ORG-068]] (companion in this bundle) makes this the health-leg predicate.

**Downstream ADR amendments this ADR implies** (folded into [[ORG-076]] rev-3 per F7):

- **[[ORG-068]] §health interface** — sub-amendment: a component's obligation is *installing its emitter, pinning one invariant list shared by `verify` and the emitter (via `invariant_list_id` + `invariant_list_digest`), and having that emitter attached to the health-collector with a proven alert route* — all three are required to promote the health-leg cell; E9.C3's ratified "failure is observable, alerting wired" is preserved unchanged. The health-leg matrix cell is authored against those three predicates, not a single install-leg receipt.
- **[[ORG-076]] §1 (detector contract)** — detectors consume the collector's unified subscription, not per-service D-Bus subscriptions.
- **[[ORG-076]] §2 (durable episode store)** — the store's ingest is the collector's `PropertiesChanged` stream, one schema, one dedup boundary.
- **[[ORG-076]] §3 (repair-ticket emission)** — a repair ticket's `action` field is `germinate repair --system <id> [--seat <account>]`; a break-glass path (R10) may substitute a named `sop/remediation/` runbook.
- **[[ORG-076]] §4 (constrained repair authority)** — the constraint applies to the persona's *invocation* of `germinate`; the engine's typed operations are the constraint boundary.
- **[[ORG-076]] §5 (fault-domain matrix)** — the unit of the matrix is the component invariant list; the same list feeds `verify`, the emitter and the detector.
- **[[ORG-076]] §6 (acceptance)** — proves the new collector boundary: initial `Get` plus subscription, collector restart and re-`Get`, missed-signal reconciliation, per-user-bus loss, detector behavior while the collector is unavailable (fail-closed, not silent-clear).
- **[[ORG-076]] §7 (operator plane)** — kino's Health Plane section is an operator legibility affordance on top of the collector's subscription.
- **[[ORG-076]] §Steward / Lifecycle / Buildout** — the collector is added explicitly to install / update / remove buildout as a registered component, and its liveness watcher lives in a **different failure domain** from the collector itself.

### 7. Interaction model — three classes, unattended by default

Three interaction classes prompt the operator; a fourth surface — fiddling — is an opt-in variance modifier, not a class.

| Class | What | Default | Examples |
|---|---|---|---|
| **necessary external interaction** | an external system requires a human and no agent can substitute; **not a plan-approval grant, but may create external credential or authority state** (see §8 abandon/revoke) | always prompts, resumable | Anthropic OAuth in the seat's pane; BotFather token issuance; deploy-key upload to the forge; sudo-password placement |
| **operation gate** | a declared transition hazard, each with a typed approval contract (`operator` / `independent-agent`) | prompts once, rollback pre-armed; pre-acceptable per its class | authority transfer in a harness swap; decommission; credential revoke; sudoers commit; group restart on a live seat |
| **reserved-authority gate** | an [[ORG-042]] / operator-reserved decision | always prompts the operator; never pre-acceptable, whoever started the run | seat/plan flip; bot-token rebind; `EXOCORTEX_AGENTS_ENABLED`; off-host replication |

**Fiddling is an opt-in modifier, not a class.** Each component may declare *tweak points* (model/effort per role beyond the manifest default; timer offsets; per-persona schedule exceptions; allowlist widening; optional units). An unattended run takes the manifest's values and never prompts on tweak points. Fiddling surfaces only when the operator explicitly opts in via `--interactive <component>` or a plan-level `tweak:` block.

**`--accept-gates[=<class>,…]` is consume-not-mint.** The flag answers declared operation gates without prompting. It records intent; it does not mint authority. For **operator-class** gates the server-derived invoking identity is the approver under policy and the flag accepts. For **independent-agent-class** gates the flag may only **consume a matching pre-issued grant** — bound to actor, performer, target, operation, plan digest, scope, policy revision and expiry ([[ORG-052]] acceptance 9–16), issued by the named `grant_source`, never by the same actor — and a missing, mismatched, stale or same-actor grant fails closed *before* any mutation. It never touches necessary external interactions or reserved-authority gates.

**The interaction protocol is machine-readable.** Every necessary interaction, gate and tweak point is emitted as a typed JSON event on the transcript stream with a public **`resume_handle`** (non-secret, safe to log/receipt); the console `--interactive` experience is one front-end to that protocol, a future TUI/GUI is another, and neither adds a second code path through the engine.

**Resume-token storage authority split.** An earlier draft said resume tokens both ride the transcript stream and are stored in the parking receipt, then said they never enter transcripts/logs/receipts. Those are mutually exclusive contracts for an authority-bearing value. Split resolves it:

- **`resume_handle`** — non-secret, opaque identifier for a parked run. Safe to appear in transcripts, receipts, `germinate status` output, operator messages. What the operator quotes when running `germinate resume <handle>`.
- **`resume_token`** — the actual secret an interaction's `resume_check` needs to prove completion (where such a secret exists — e.g., a nonce the check compares against a stored value). Lives ONLY in the run-state store's authority-protected slot at `<scope_root>/schema-v1/<instance>/<component>/<scope>/<leg>/<run_id>/resume/token.secret` (mode 0600, owned by the writing uid — root for system-scope, seat-uid for seat-scope); NEVER in transcripts, receipts, logs, or argv. `germinate resume <handle>` looks up the run-state record via the handle-to-record index, retrieves the token internally, and passes it to the check executor. Operator uid must match the record's persisted `parking_authority_uid`; a mismatch fails closed *before* any check runs.

Not every interaction needs a `resume_token` — most `resume_check`s are stateless probes (`claude auth status`, forge deploy-key list). The split is normative for cases where the check itself needs a secret to run; the storage rule applies uniformly regardless.

**Read-only is not world-readable.** `verify`, `status`, `plan` and `registry show|render` are self-scoped by default. A separately-defined redacted registry summary may be world-readable. Secret values, resume tokens and grant tokens never enter argv, registry renderings, logs or receipts ([[ORG-050]]; [[ORG-052]] acceptance 21).

### 8. Parked-resume state machine (§3.9a of the design draft)

A run that hits a necessary external interaction *parks* rather than fails. The state machine has **three operator-visible parking-phase states** (`PARKING`, `PARKED`, `VERIFYING`) — plus the parking transaction's intermediate `PENDING_PARK` and the reconciliation-failed halted `PARKING_FAILED`, both defined in the run-record schema (§Base engine mechanics) and reachable through the parking transaction / reconciliation matrix — one operator verb (`germinate resume`), and **three typed verify outcomes** that decide the transition out of `VERIFYING`.

- **`PARKING`.** The engine writes a parking record to the **run-state store** at the exact path specified by the schema-v1 bijection (§Base engine mechanics): `<scope_root>/schema-v1/<instance>/<component>/<scope>/<leg>/<run_id>/resume/handle.json` (mode 0600). The record persists: (a) `resume_handle` — a 128-bit ULID (Crockford base32, 26 chars), collision-resistant, non-secret, safe to log/transcript/receipt; (b) the exact **check_id + params** tuple that will decide completion (the check_id references the closed **interaction-check registry** — `interaction_check_specs/`, schema `pantheon-interaction-check/v1` — where each spec has typed parameters, privilege, output schema, timeout, error mapping; there is no free-form prose check); (c) the precise instruction the operator needs to satisfy (typed JSON event, machine-readable, and rendered to the console); (d) `parking_authority_uid` — the server-derived uid of the caller who parked the run (looked up from D-Bus credentials or `getuid()`, never from a request parameter). VERIFYING compares against this exact field, not against a re-derived value. (e) If the check needs a secret to run, a `resume_token` stored in `resume/token.secret` in the same directory (mode 0600), NEVER emitted on transcripts/logs/receipts/argv. Alongside the record write, an atomic handle-to-record index entry is created at `<scope_root>/schema-v1/handles/<handle>.json` — a duplicate handle at insert-time is a fail-closed replay guard.
- **`PARKED`.** Nothing runs. The parking record lives in the run-state store; no polling, no background timer.
- **`VERIFYING`.** Triggered by `germinate resume <handle>` (the normative CLI verb, per §1). The engine looks up the parking record via the handle-to-record index, verifies the invoking operator's server-derived uid equals the record's persisted `parking_authority_uid` byte-for-byte (mismatch → fail-closed before any check runs), and executes the parked `check_id` once against the recorded params + secret (if any). The executor is fixed code registered in the interaction-check registry, not English prose. On `satisfied`, the handle entry is unlinked and a `<handle>.tombstone.json` written; subsequent `germinate resume <handle>` returns a clear replay error rather than a not-found ambiguity.

Verify outcomes (three typed cases; `CONTINUING` is a transition out of `VERIFYING`, not a persisted state):

- **`satisfied`** — the check ran and passed. Transition to the active run and continue from the next operation. **No leg mutation resumes before `satisfied`.** The resume attempt is durably recorded; a subsequent `germinate resume <handle>` on a satisfied run is idempotent-refused (replay guard).
- **`unsatisfied(reason)`** — the check ran and reported known-incomplete. Transition back to `PARKED` with an updated instruction; `resume_handle` unchanged; `resume_token` unchanged.
- **`check_error(reason)`** — the check could not be evaluated (timeout, malformed response, missing checker, permission error, broker outage). Transition back to `PARKED`; the failed resume attempt is recorded loudly per R6 (durable run record + stderr + non-zero exit); the operator is instructed to repair/retry the check or `germinate rollback --run <run-id>`. **A resume-check error never creates a second halted leg** — the `PARKED → VERIFYING → PARKED` loop is idempotent.

**"Operator marks done without doing it" is always a fail-closed on the verify step.** The verify-check is the substrate that decides completion, not the operator's assertion. There is no path from `germinate resume` to "run continues" that does not pass through the verify-check the parking receipt named.

**Abandonment with external state.** Rollback of a parked run first runs each in-flight interaction's `abandon_check`. If external credential or authority state exists — a BotFather token, a forge deploy key, an OAuth grant — the engine executes the declared typed inverse **or** parks on the named human revoke interaction; only after `revoke_evidence` verifies may rollback claim pre-run restoration. An interaction whose `rollback` is `external-interaction-required` cannot be silently unwound. An install leg is fully reversible only when every interaction it created either reports `not-applicable` or produced verified `revoke_evidence`; otherwise the leg is partial-with-external-authority-remaining, and the transcript names the specific residual state.

### 9. No LLM on the execution path

Every leg of every component is deterministic code. The execution path from `plan` through `receipt` contains no model turn. Personas appear in exactly two roles: *operator* (chooses verb, scope, desired state, timing; reads the transcript; presents authority they already hold) and *subject* (the thing being installed). The persona never *executes*, *decides* or *verifies* a step. Deciding *whether and when* to invoke a verb is the model's; deciding *how* the verb executes is the engine's. **Personas are the default operators** (R12b) — ideally a human only ever optionally runs the tool. No acceptance criterion may depend on a target model cooperating.

**Enforcement.** The component contract carries `llm_free: true` as an invariant checked structurally at `plan`: no leg may include an op of kind `harness-turn` / `send-keys-prompt` / `model-reply-wait`; those kinds exist in the enum only for the *runtime* controller. A plan that would need one is a design bug surfaced at `plan`, not at `apply`.

**The one unavoidable case.** When the component being installed *is* an LLM harness, proving it is installed means starting it. The boot smoke is a set of deterministic, no-model-turn checks: (1) `claude auth status → loggedIn: true` — this proves the OAuth *interaction's completion check*, not the boot smoke itself; (2) the `claude` pid is alive under the unit's tmux pane through the settle window (not `systemctl is-active`, which the oneshot+tmux unit shape reports as active while wedged); (3) the plugin poller is present as a `bun … server.ts` child of that pid; (4) **pane-frame check, bring-up only** — split into (4a) authoritative for known-wedge signatures (each a named reason like `wedged: trust-dialog` that parks a *necessary* interaction) and (4b) advisory for the ready frame (a mismatch is `unknown`, never `fail`; the captured frame is logged for the signature catalog). Signatures live in a versioned data file (`pantheon-harness-frames/v1`), not inline regex, so TUI-chrome drift is a data update, never a false-fail on a healthy seat. Post-install `verify` and the health-plane emitter use (1)–(3) only.

**Probe invariant** (from T-3192, 2026-08-24). Every verify probe declares `side_effect_free: true`, checked at `plan`. `claude mcp list` is disqualified as a probe on any seat with a live poller.

### 10. Mid-leg failure and rollback

Any leg — install, configure, update, remove, repair — can hit an unplanned failure mid-execution. The engine keeps the box's state coherent via the per-operation crash contract (§2, `stageable` vs `write-ahead-inverse-durable`) composed with repair-as-plan-from-drift and idempotent re-entry via postcondition-verified skip.

**When a leg halts**, the failing operation writes a durable run record to the **run-state store** (transcript entry + failure classification + receipt-so-far); the failure surfaces loudly per R6 (stderr + non-zero exit + emitter flip + page when routes exist); the component's run-state record carries a `HALTED: <run-id>` marker with the failed operation named. The coverage registry ([[ORG-081]]) is **not** written on this path — a live halt does not rewrite the git-tracked contract-adoption row.

**Operator's three options after a halted run:** (a) `germinate rollback --run <run-id>` (abandon this attempt, restart clean); (b) `germinate apply <same scope>` re-run (plan-from-current-state, convergence skips completed ops, retries the failed one); (c) fix the underlying cause manually then either of the above (transcript records the out-of-band event; R10 break-glass).

**Rollback failure.** If `germinate rollback` itself fails partway, the run-state store carries the unreversed operations forward with a **`rollback_incomplete: <run-id>`** marker and the run's `record.json` transitions to `state: ROLLBACK_INCOMPLETE` (a run-record state in the closed enum per §Base engine mechanics). That marker is a **structural mutation gate**; `ROLLBACK_INCOMPLETE` is a run-record state, but not a new [[ORG-068]] lifecycle leg and not a new engine verb. Until the outstanding-inverse list is empty, read-only `status`/`verify` may inspect, exact `germinate rollback --run <run-id>` may retry, and a separately-approved `germinate remove --system <id>` (gated per D-E) may decommission — **every other mutating apply refuses before plan approval**. The refusal is enforced by the run-state store's authority (root-owned or seat-uid-owned), not by the coverage registry; a restart cannot lose the refusal because the marker is durable and mode-protected. Successful retry clears the marker only after post-rollback verification proves the recorded preimage. Two admissible recovery paths remain: retry this exact rollback after fixing its cause, or invoke separately-gated decommission.

**Where the mutation gate lives.** The `rollback_incomplete` marker lives on the run-state store, NOT on the coverage registry. [[ORG-081]] defines the coverage registry's `partial` field as static contract-adoption evidence, written only by git commit; a live rollback failure needs an operational authority the executor can write and read at run time. The run-state store is that authority. Placing the marker on the coverage registry would either silently rewrite contract-coverage evidence at run-time or leave the failure with nowhere schema-valid to persist.

## Consequences

**Positive:**
- One executor replaces the runbook-plus-one-off-script pattern; the two of three T-2950 halts that were preflight-catchable become uncatchable-only-if-plan-fails.
- Typed operations plus staged apply plus receipts give every mutation a rollback anchor; a rollback failure structurally refuses further mutation instead of silently compounding.
- The system registry becomes the [[ORG-068]] matrix's rendering source; the "who is stewarding what leg" question becomes a data query.
- Health obligations become a three-part predicate: the installer proves (a) emitter installed + reporting, (b) invariant-list id/digest bound between `verify` and the emitter, (c) collector attachment + proven alert delivery (or explicit E9.C3-non-cover waiver). The health plane owns continuous observation; the two vocabularies merge into one. Orphan-emitter builds do not promote coverage.
- Necessary human interactions become a first-class, resumable, revocable class — not a mid-run pause that leaks external authority.
- Every existing tool (persona_installer, mailbox_onboard, consume_onboard, cutover, bridge, scheduler_reconcile, host_control) becomes a component engine behind one contract (D-I). Nothing is rewritten from scratch.

**Negative:**
- The engine is a substantive new codebase to build, even given the wrap-don't-rewrite discipline. §Build plan (below) sequences it; T-2667 is the umbrella scoping ticket.
- Every component needs its declared privilege lists and crash contracts curated; the `plan`-time refusal for `crash_contract`-undeclared ops is a real ergonomic cost paid up front.
- The [[ORG-076]] design must revise to rev-3 to consume the collector cleanly; that arc restarts inside this one (F7).
- kino's Health Plane section repositions from "core operator plane" to "operator legibility affordance"; T-2979 rewrites.

**Deferred:**
- **Broker runtime enablement per operation family** stays [[ORG-052]] Unit 8's per-capability decision. The engine does not shortcut it.
- **Op-enum home** is `tools/host_control/contracts.py` (per §Open Q1 of the draft).
- **External-repo component registration**: `pantheon-component.yaml` at repo root + `repos.yaml` entry ([[ORG-046]]).
- **Codex receiver as a sub-component of harness-manager** (lean) vs its own component — decided during Phase 4 of the build.
- **Emitter contract versioning** shared with [[ORG-076]] rev-3 (`pantheon-health/v1` already exists; `PersonaController1` mirrors it).

**Open questions:**
- Whether `plan --preflight` drops to each seat uid for per-account checks (lean: yes).
- Human-reserved gate mechanics (lean: park with resumable receipt; resume re-verifies the postcondition; no `--yes` for the `operator` class).

## Build plan

**Sequence.** This ADR → [[ORG-081]] System Registry ADR → [[ORG-068]] amendment → [[ORG-076]] rev-3 (F7) → engine ADR revision only if the health design moves it.

**Build (children under T-2667):**
- (a) registry schema + validator + render tool (closed status enum, evidence rules, `contract_adoption` vs `legacy_coverage`, `kind: external-plane`);
- (b) engine skeleton over `SimulatedHostBackend` — DAG, plan merge, transcript, gates; CI-testable, zero privilege;
- (c) host-substrate component + root backend = T-2920 generalized, proven on a disposable host ([[ORG-052]] gate 32 shape);
- (d) harness-manager component = `persona_installer` full CLI + root-context backend (T-2917 seam) + two configure sub-ops ([[ORG-057]]) + [[ORG-079]] invariants + `PersonaController1` emitter wiring — absorbs T-2889 + T-3191; Recovery co-authors the invariant/gate list + preflight schema;
- (e) mailbox / scheduler (T-2662) / credential components wrapped;
- (f) update leg per D-H (T-2922 for the broker);
- (g) **acceptance = the SOP §1–§9 procedure re-expressed as `germinate install --all` on a throwaway box** (T-2556 / T-3109 are this test).

Health-plane build (T-2941 / T-2978 / T-2979) proceeds in parallel on its own tickets; the engine's obligation to it is the three-part predicate: emitter install + reporting AND invariant-list-id/digest bound between `verify` and emitter AND collector attachment + proven alert delivery, OR an explicit `partial_by_design` waiver acknowledging [[ORG-068]] E9.C3 non-cover (see D-AB / D-J / §Health-plane seam).

## DECISIONS

*Letters (D-A, D-B, …) are this ADR's local decisions; the rulebook's D1, D2, … in `rulebook/directives.md` do not collide.*

- **D-A. One apex executor; components as the unit of modularity — P1 (structure over procedure), [[ORG-068]], [[ORG-053]] §6, T-2667.** A runbook asks a reader to be consistent; an executor is consistent.
- **D-B. Thin composer, not Harness Manager renamed — [[ORG-063]] ruling 2 (isolates), P1.** The engine holds no capability of its own; it executes components' declared typed ops.
- **D-C. In-process root for bootstrap / operator-run legs, broker daemon for seat-runtime, ONE shared typed-op registry, and the daemon exposes only the runtime subset — F2, [[ORG-052]] gate 35 / Unit 6, [[ORG-063]] ruling 4, P1.** No generic shell path in either context; substrate ops never on the daemon's exposed list.
- **D-D. Registry is data; the matrix is rendered; status values are a closed enum whose evidence rules the schema validates — F3, P1, P4.**
- **D-E. `rollback` is the receipt-scoped undo of one attempt; `remove` is decommission — two verbs, no mode flag — P1, [[ORG-052]] §8 (retire ≠ delete).**
- **D-F. Gates only at declared operation gates (with their approval contract) and reserved-authority gates; necessary external interactions park, they do not gate — D1 (operator bandwidth), [[ORG-050]] apply-after-gates + authority identity, [[ORG-042]] reserved flips, [[ORG-052]] acceptance 9–16, R6.**
- **D-G. T-0753's no-`bringup.sh` ruling is superseded (ratified 2026-08-26 with the bundle).** [[ORG-068]] ratified after it; its concern (substrate-tier paste-and-run) is answered by typed ops + preflight + gates + receipts, not by keeping the procedure in prose. Before ratification the original T-0753 verdict stood; at ratify all status surfaces (this D-G, the T-0753 banner, the [[ORG-049]] supersession, the [[ORG-068]] amendment, the [[ORG-081]]/[[ORG-082]] PROPOSED banners) flip together.
- **D-H. Update leg per component: auto-apply for unprivileged, auto-stage + supervised cutover for privileged — [[ORG-068]] §1 + T-2922's rationale.**
- **D-I. Wrap, don't rewrite — P1 (one structural contract over the existing tools, not a second implementation of each).** `persona_installer` / `mailbox_onboard` / `consume_onboard` / `persona_cutover` / `codex_telegram_bridge` / `scheduler_reconcile` / `host_control` become component engines behind one contract, extending `_organ_lifecycle.py`.
- **D-J. Health is not an installer stage; the installer's obligation is the three-part predicate — [[ORG-068]] E9.C3 (unchanged), [[ORG-076]] §6, P1 (one boundary: mutate vs observe).** The installer proves (a) emitter installed + reporting, (b) the emitter's `invariant_list_ref` and the component's `verify` `invariant_list_ref` resolve to the same `<id>@<digest>`, (c) collector attachment + proven alert delivery (or explicit `partial_by_design` waiver acknowledging E9.C3 non-cover per D-AB). Emitter-install alone does NOT satisfy E9.C3.
- **D-K. Repair tier stays above the installer; break-glass is installer-independent — [[ORG-063]] ruling 4 applied one level up, Recovery co-review, P1.**
- **D-L. Supervisor-model cutover and harness-runtime swap are two composable configure sub-ops, each with its own gate — Recovery co-review (T-2950 evidence), [[ORG-057]] §3, P6.**
- **D-M. Three interaction classes — necessary external interaction / operation gate / reserved-authority gate — with unattended as the default; fiddling is an opt-in modifier, not a fourth class — D1, R6/R7, R11.** A prompt outside the three classes is a design bug.
- **D-N. No LLM on the execution path; `llm_free` is a structural invariant the engine enforces at `plan`; canaries and reply-wakes move off the critical path — P1 (structural not procedural), [[ORG-049]] ("durable delivery ≠ a model turn ran"), the codex-bridge "fixed operations, not target LLM cooperation" principle made engine-wide.**
- **D-O. `rollback` (undo one attempt, receipt-scoped, engine-internal) and `remove` (decommission a component or seat, gated) are different verbs — [[ORG-049]] modes, P6.** Remove-as-undo is retired.
- **D-P. The engine is a command, not a daemon — P1.** Continuous observation belongs to the health plane and the scheduler; the engine holds state only in **receipts, the coverage registry, and the run-state store** — the run-state store is durable authority for parked runs, HALTED markers, and `rollback_incomplete` markers.
- **D-Q. Declared gates are pre-acceptable by flag and by class; necessary interactions and the reserved-authority class are not gates and cannot be suppressed — [[ORG-042]], D1.**
- **D-R. The interaction protocol is typed JSON events on the transcript stream; console `--interactive` is one front-end — P1 (one code path).**
- **D-S. Role counts and account names are manifest data resolved at `plan` — [[ORG-043]], R13.**
- **D-T. Two test tiers: simulated backend in CI + a disposable systemd container/VM for the real footprint — [[ORG-066]], [[ORG-052]] gate 32.**
- **D-U. Update is automatic unless opted out (`update.policy`, default `auto`), with D-H's privileged-cutover exception — [[ORG-068]] §1, T-1996 (manifest-resolved remotes).**
- **D-W. Necessary external interactions carry a lifecycle contract (`external_effect`, `abandon_check`, typed `rollback`, `revoke_evidence`); rollback of a parked run runs abandon_check first and refuses to claim pre-run restoration until `revoke_evidence` verifies — P1, [[ORG-050]] minimum lifecycle controls, [[ORG-051]] required properties.**
- **D-X. Every mutating operation declares one of two crash contracts (`stageable` or `write-ahead-inverse-durable`); an op that can declare neither is refused at `plan`, and a component whose crash contract cannot pass a kill-injection test is refused at registration — P1 (structural, not inferred), [[ORG-050]] required rollback controls.**
- **D-Y. `partial` with `rollback_incomplete: <run-id>` is a structural mutation gate: every mutating apply refuses until the outstanding-inverse list is empty; only rollback-retry or gated decommission is admissible — P1, P4 (one run remains the auditable rollback owner), [[ORG-050]] rollback preserves only known managed state.** The marker lives in the run-state store, not the coverage registry.
- **D-Z. Coverage and run state are separate stores with separate writers.** The coverage registry ([[ORG-081]], git-tracked, one writer: git commits) is authoritative for contract adoption + per-leg coverage evidence; the run-state store (root/seat-uid-owned, mode 0700/0600, one writer per component: the executor for that run) is authoritative for live parked runs, HALTED markers, `rollback_incomplete` markers, active-run manifests. A join is display-only. **justified-via P1, P4, [[ORG-050]] rollback controls.**
- **D-AA. One authority per field; digest-bound mirrors; contract-digest is not self-referential.** The component contract module is authoritative for every schema field (id/steward/DAG/privilege/legs/gates/interactions/reserved_authority/emitter/invariant_list_id + digest). The registry references the contract by `contract_digest` (SHA-256 of the contract module's canonical serialization — the `contract_digest` field itself is replaced with the fixed sentinel `"SHA256_DIGEST_PLACEHOLDER"` before hashing, so the digest is not self-referential and any implementation computing it gets the same bytes; see §Component contract for the exact procedure). External-repo `pantheon-component.yaml` is the authoritative source for external components, pinned by revision. Any digest mismatch is a `plan` refusal. Registry-side mirror fields (currently `steward:` per [[ORG-081]] §1) are digest-bound: their value MUST equal the contract's field exactly, refused at `plan` on mismatch, no transition-window carve-out. **justified-via P1, P4, [[ORG-050]] §§46-69, [[ORG-052]] acceptance 17.**
- **D-AB. E9.C3's ratified obligation is preserved unchanged; the health-leg coverage predicate names three predicates (emitter installed + reporting; invariant-list-id/digest bound between `verify` and emitter; collector attachment + proven alert delivery, or an explicit waiver acknowledging E9.C3 non-cover).** An orphan emitter cannot promote the cell. **justified-via P1, [[ORG-068]] E9.C3 unchanged, [[ORG-076]] §6.**
- **D-AC. Component isolation is capability-shaped, not lexical.** A component's `plan_*` methods run in a sandboxed unprivileged planning process (no fs-write / subprocess / network / D-Bus / dynamic-import) and return typed `Operation` records only. The engine's sole typed executor is the only code that actuates ops, resolving each op's `id` against the root-owned typed-op registry. Any import-lint that refuses cross-component imports is a hygiene check, not the enforcement. **justified-via P1, [[ORG-063]] ruling 2, [[ORG-052]] acceptance 17-19.**
- **D-AD. F2's daemon subset is a genuine tightening of [[ORG-052]]; the amendment lives in the companion [[ORG-082]].** The broker daemon's exposed surface excludes account / group / ACL / sudoers / root-unit families; those move to the bootstrap in-process root path only. Post-bootstrap convergence of those families runs through the bootstrap path (a supervised operator-run leg), not the daemon. Same-registry-digest acceptance gate is added by [[ORG-082]]. **justified-via P1, P4 (audit-chain honesty), [[ORG-052]] acceptance 17,19,32-36.**
- **D-AE. Resume-token contract is split; interaction checks are typed executable specs, not prose.** Public `resume_handle` is safe in transcripts/receipts/logs; secret `resume_token` lives only in the run-state store's authority-protected slot. Every `resume_check` / `abandon_check` / `revoke_evidence` field is a `check_id` referencing the closed **interaction-check registry** (`interaction_check_specs/`, schema `pantheon-interaction-check/v1`) with typed params. `germinate resume <handle>` is a normative CLI verb. Operator uid must match the parking-authority record. Idempotency + replay refusal are structural. **justified-via P1, [[ORG-050]] §§46-82, [[ORG-051]] §§52-65,85-92.**

**Naming / packaging / sequencing** (F-series, referenced by the design draft):

- **F1. Supersede [[ORG-049]].** Its layers become the op taxonomy and its four modes survive as the engine verbs.
- **F2. Shared privileged code surface, two invocation contexts.** One typed-op registry, one backend, one receipt schema. Bootstrap + operator-run: in-process as root, executing every op family including substrate (account/group/ACL/sudoers/root-unit). Runtime seats: broker daemon exposing only a **strict runtime subset** — never substrate ops. This is a genuine tightening of ratified [[ORG-052]] acceptance 32; the amendment lives in the companion [[ORG-082]] and adds a same-registry-digest acceptance gate that verifies both invocation contexts loaded the same registry revision and the daemon-visible ops are exactly the reviewed runtime subset.
- **F3. System registry is a system.** Own ADR ([[ORG-081]]), own `registry/` directory, own schema and validator. [[ORG-068]]'s matrix becomes a pointer to the rendered view.
- **F4. Substrate first.** Day-one components: host-substrate, mailbox, harness-manager, scheduler, action-broker, credential-broker, pantheon-config self-update (plus the registry and the health collector). Project organs (echo, kino, watch, memory, presence, passport) appear in the registry with `contract_adoption: not_adopted` + `legacy_coverage:` until each adopts the contract on its own ticket.
- **F5. Lives in pantheon-config.** `tools/germinate/` package + `germinate` command alias; extraction to its own repo only if it ever needs its own release cadence ([[ORG-055]] procedure).
- **F6. Name is `germinate`.** ADR title "Germination engine"; command `germinate`; short alias `germ` installed alongside. "Pantheon" stays as the internal name; "germinate" is the outward one.
- **F7. [[ORG-076]] rev-3 restarts inside this arc, after structure confirmation.** Engine structure confirmed 2026-08-26 → [[ORG-076]] rev-3 restarts respecting §Health-plane seam → engine ADR revision only if the health design moves it.
- **R12b. Personas are the default operators; a human only ever optionally.** Roles are operator or subject only — never executor, decider or verifier of a step.

## Re-evaluate when

- **R1** — the first non-day-one component (an external project organ) adopts the contract; discover where the yaml-only registration cracks.
- **R2** — a live cross-account leg fails halfway on a real host and the crash-contract kill-injection tests catch (or miss) the exact failure mode; if a class of failures reproducibly slips both crash contracts, add a third contract or refactor.
- **R3** — [[ORG-076]] rev-3 lands and either fits the §Health-plane seam or forces this ADR into a revision.
- **R4** — a necessary interaction the engine cannot classify as one of the four current ids (OAuth, bot-token, deploy-key, sudo-password) arrives; extend the interaction schema and re-check the abandon/revoke contract for the new class.
- **R5** — the operator plane (kino) needs interactions the machine-readable protocol does not express; add to the protocol, not to the console front-end.

## Revision history

Fold arc across 14 rounds, all 2026-08-26. Each round appears as a git commit on `main`; the sequence is preserved there for audit. High-level shape:

- **v1–v9** — Nine prose fold rounds through Consultant's ORG-041 spec-review, each closing the previous round's findings surgically. Substrate work: state-enum + parking transaction (v5), cross-scope tmpfs lock (v6), root-owned lock + world-readable gates + boot_id (v7), ancestor-mode / uuid-shape / postboot-target (v8), gate relocation + admission-predicate correction + runtime postboot marker + planner_kind schema (v9). Convergence pattern shifted from "you missed the wire" (v3–v7) to "you missed the provider fact" (v8–v9): kernel/POSIX and systemd behaviors the ADR-prose tier could not mechanically discover in advance.
- **v10 — Architectural-tier CARVE.** Per the operator TG 8579/8585/8586/8590/8594, the sandbox+admission subsystem's implementation-mechanism authority moved out of the ADR body into a reference-implementation deliverable at `tools/germinate/planner_sandbox/` (T-3274). The ADR retains the architectural commitments (C1–C4 at v10, extended to C5 at v11); the reference-implementation demonstrates the mechanism against real host semantics. Layer-anchor critique discipline (TG 8590) governs the review contract from here forward — REBOUND is reserved for architectural-prose-tier findings the ADR can actually carry.
- **v11 — Consultant v10-REBOUND fold (2 BLOCK + 2 FIX-SOON, all architectural-prose-tier).** Added C5 (sandbox capability/no-actuation outcome), C2 quantifier split (initial-vs-non-initial for incoming, terminal-vs-non-terminal for outgoing), inline `[Documentary — informs Cn]` markers at the four sandbox+admission passages in §Decision.
- **v12 — Consultant v11-REBOUND fold (1 BLOCK, architectural-prose-tier).** C2 reclassified `PARKING_FAILED` as non-terminal with operator-invoked rollback as its only admissible outgoing transition (option (i) of Consultant's disposition, chosen over option (ii) because ORG-080:574 reads as a same-record transition).
- **v13 — Consultant v12 FIX-SOON landing (0 BLOCK + 2 FIX-SOON).** Consultant v12 verdict architecturally APPROVED. Aligned the two remaining PARKING_FAILED wording sites (§Component contract enum row + §Reconciliation-failed halted escalation paragraph) to the C2 reclassification; aligned ORG-081 v12 frontmatter identity.
- **v14 — the operator editorial pass** (TG 8604): stripped revision-history/process scaffolding from all three bundle ADRs. Frontmatter status collapsed to one sentence; §Architectural-tier scope carve reduced to commitments-only (Consultant-verdict-disposition subsections retired); parenthetical process refs stripped from C1–C5 titles; per-section "**vN (T-NNNN fold):**" callouts in §Decision retired; fold signatures consolidated into this section. No semantic change to any commitment.
- **v15 — the operator's external-reviewer final-review pass** (TG 8609). Two ORG-080 fixes: (a) §8 parked-resume no longer undercounts persisted states — reworded to "three operator-visible parking-phase states, plus PENDING_PARK / PARKING_FAILED defined in the run-record schema"; (b) §Mid-leg failure `rollback_incomplete` paragraph now names `ROLLBACK_INCOMPLETE` explicitly as a run-record state in the closed enum (matches the schema table) while preserving the "not a new lifecycle leg / not a new verb" distinction. No architectural change; both edits align prose to the already-normative enum.

Convergence trajectory across the T-3273/T-3284 review arc: v9 3-BLOCK → v10 2-BLOCK → v11 1-BLOCK → v12 0-BLOCK-2-FIX-SOON → v13 landing → v14 editorial. Zero implementation-tier findings routed as REBOUND across v10/v11/v12 — R/D carve held structurally throughout.


## Amendment (2026-10-17, T-2667): buildout charter

**Status.** Drafted under T-2667 on 2026-10-17. Calls A-3, A-5 and A-7 (marked *HO-decided*) were decided at HO tier by orchestrator on 2026-10-17. All of it is pending the operator's ratification. It changes no ratified decision above. It adds decisions the 2026-08-26 bundle left open, plus the [[ORG-089]] viability fields the bundle predates. Until the operator ratifies, the HO-decided items govern how the build is sequenced. They don't change what the ratified text requires.

**Intent.** One coherent apex controller that installs, configures, monitors and removes any organ or seat (the operator TG 7285). The bundle ratified *what* that controller is. This amendment decides *how it gets built without ballooning*:
- what the installable unit is called and what pins it;
- what fires the engine when no operator does;
- which slice proves the bet first;
- how the bespoke-automation backlog ([[T-3433]]) maps onto the build.

**Context.** Seven weeks after ratification, `registry/` and the engine skeleton did not exist. The de-risking reference implementations did: the planner sandbox (T-3274), the WAL (T-3336), the runtime-effect adapter (T-3340) and the broker executor (T-2917). Four gaps sat at decision altitude:
1. "Seed" was defined only in a design-draft note.
2. D-U makes updates automatic and D-P makes the engine a command, but nothing named what fires the command unattended, or with what privilege.
3. The build plan had no viability gate. The bundle took 15 fold rounds, and its closest probe (T-3334 Arc B) hit the 3-REBOUND cap.
4. [[ORG-099]]'s HARD axes created a second verify vocabulary beside [[ORG-079]] V1–V19.

### A-1. Seed: the installable unit of an instance

A **seed** is inert data. It consists of:
- an instance manifest (`instance.yaml`);
- the [[ORG-081]] system registry;
- the framework revision;
- the set of component contracts;
- secret *references* (never values).

The [[ORG-055]] generated assembly lock pins all of it.

A **component** is the installable unit for one organ or seat. A seed is the set of components plus the bindings that make them one instance.
- **Identity:** a seed is its instance id plus its assembly-lock digest. A component is its registry `id` plus its `contract_digest` ([[ORG-081]] D-9).
- **Versioning** is the lock: a new seed version is a new lock digest.

**Germination** is this engine applying legs to a seed. The engine never edits a seed; it converges a host toward one. Source: design draft §3.1 (`notes/2026-08-24-seed-installer-system-design-draft.md`), lifted into the ratified record.

### A-2. Lifecycle: no new legs, no new verbs

Seed → running organ or seat is §1 of this ADR, unchanged: preflight → install → configure → health → update → remove.
- **Health:** the installer's duty is the three-part obligation in [[ORG-068]] §Amendment item 2. Continuous observation belongs to [[ORG-076]].
- Verify, repair and rollback remain engine verbs.

### A-3. Cadence seam: the Scheduler fires the engine; scheduled runs never run as root (*HO-decided*)

The engine calls the Scheduler to install timers (§ADR layer map). In the other direction, **Scheduler timers are the only unattended trigger for `germinate`**. That covers auto-update under `update.policy: auto`, and periodic verify.

Limits on a scheduled run:
- It may execute only legs whose ops are `privilege: none | seat`.
- For a privileged component it may auto-*stage* an update but never cut over; cutover stays supervised (D-H).
- The bootstrap in-process root path ([[ORG-082]]) remains operator-run only.

The repair seam is where [[ORG-076]] / [[RFC-004]] put it:
- A repair ticket's action is `germinate repair` ([[RFC-004]] §3).
- Who may invoke it is bounded by the repair grant ([[RFC-004]] §4).
- Repair stays above the installer (D-K).

### A-4. A new module, not the Harness Manager renamed: D-B confirmed

The engine is a new thin composer. The Harness Manager is one component it composes. "Main controller" means the apex *executor*. The apex *decider* remains an operator persona (R12b; the future Bridge head seat, [[ORG-063]] OPEN item 2).

Alternatives rejected:
- **Harness Manager renamed at the top:** it would hold seat-lifecycle and host-capability power together, which [[ORG-063]] ruling 2 forbids.
- **A resident controller daemon:** contradicts D-P, and duplicates the health plane and the Scheduler.
- **Inside the Action Broker:** the broker daemon may not expose substrate ops ([[ORG-082]]).
- **Scheduler-driven:** the Scheduler owns *when*, never *what* ([[ORG-064]] 1b).

### A-5. Build order: walking skeleton first, then one go/no-go (*HO-decided*)

The build starts with three pieces:
1. the registry (T-5073);
2. the engine skeleton over the simulated backend (T-5074);
3. **one real, unprivileged component adopting the contract** (T-5081).

The root-path and harness-manager tranches (T-5084, T-5090, T-5091, T-5092) start only after T-5081 passes a go/no-go against §Riskiest assumption below. Tranche asks, acceptance and sequencing live in those tickets, not here.

### A-6. One verify list per seat

The harness-manager component's `verify` invariant list is a single `invariant_list_ref`. It resolves [[ORG-079]] V1–V19 together with [[ORG-099]]'s HARD axes. Neither list keeps a parallel verifier.

### A-7. Absorbing the bespoke-automation backlog (*HO-decided* for the publish exclusion)

A [[T-3433]] child is an engine-absorption candidate only if its op is a leg of a registered component. A candidate retires in one of two ways:
- when its absorbing tranche lands; or
- when another durable mechanism replaces it, recorded as the component's `legacy_coverage` (the T-3434 precedent).

**Cross-repo publishing (echo merge, release-mirror refresh) is out of the engine, and no new verb is added for it.** It is not a lifecycle leg of a host component. Those ops keep their scripts and SOPs.

### Appetite

**8.**
- **Value-if-it-works 9:** it ends runbook-driven live transitions. Two of T-2950's three halts were preflight-catchable.
- **Strategic alignment 9:** [[ORG-053]] §6 and SSEED.
- **Reversibility 6:** the engine is a command, and legacy tools stay as fallback until each retires. But substrate ops are high-stakes.

**Ceiling:** the build widens past the skeleton only through the A-5 go/no-go. Any of these is an overrun signal (halt and re-scope):
- a tranche that needs a new verb;
- a new interaction class;
- a rewrite of a wrapped tool's own logic.

### Riskiest assumption

**"Wrap, don't rewrite" (D-I).** The assumption: an existing tool can adopt the component contract through a thin adapter, at a cost well below rewriting it. The contract means typed ops, a declared privilege and crash contract, a sandboxed data-only planner, and a contract digest. If it is false, every component becomes a rewrite, and the "thin composer" is thin only on paper.

Evidence for the risk:
- the bundle needed 15 review rounds;
- T-3334 Arc B hit the REBOUND cap;
- `persona_installer` still offers only plan and simulate.

**Cheap test:** T-5081 adopts `tools/repo_reconciler.py` as the `pantheon-config` self-update component. That component is unprivileged, already reconciler-shaped, and a day-one component (F4).

**Kill or re-scope signal:** the adoption needs changes to the reconciler's own logic beyond an adapter, or any of its ops cannot declare a crash contract.

**Secondary:** C5 sandbox confinement holds on the production host. If it doesn't, D-AC's enforcement weakens to a lint. T-3296 is the test.

### Consequences

- T-2667 becomes a pure scoping ticket. It closes now that T-5073, T-5074 and T-5081–T-5092 exist with the `org-080` + `build` labels, and its dependents (T-3382, T-2156) are repointed to the tranches they actually need.
- [[ORG-064]]'s `system:` field joins [[ORG-081]] registry ids. The Scheduler gains a stated invocation role ([[ORG-064]] 2d).
- [[ORG-084]]'s build (T-3382) gates on the registry tranche (T-5073), not on the whole engine.
- Two [[T-3433]] children retire as out of engine scope (A-7): T-3443 and T-3446. T-3445 waits for the pantheon-pwa organ's own adoption (F4).

### Re-evaluate when

- The A-5 go/no-go returns "no".
- A scheduled `germinate` run is found to need root.
- A third verify vocabulary appears beside [[ORG-079]] and [[ORG-099]].
- A cross-repo op class reaches the volume where a verb is cheaper than its scripts.

### DECISIONS (this amendment)

- **D-AF. Seed defined; component stays the unit of modularity.** Intent: one apex controller needs one named installable unit. Justifies via P4 (lifts the draft definition into the ratified record) and P1; uses [[ORG-055]] and [[ORG-081]] D-9.
- **D-AG. The Scheduler is the sole unattended trigger; scheduled runs never run as root.** Intent: auto-update without a resident controller. Justifies via P1 (privilege bounded by structure, not operator care), D-H, D-P and [[ORG-082]].
- **D-AH. D-B confirmed; executor and decider stay distinct.** Intent: the "main controller" without collapsing the isolates. Justifies via P5, P1 and [[ORG-063]] ruling 2.
- **D-AI. Walking skeleton plus go/no-go before the privileged tranches.** Intent: prove the controller cheaply before investing deep. Justifies via P7, [[ORG-089]] §Riskiest assumption, and D12 (structural correctness over momentum).
- **D-AJ. One verify list.** Intent: one source of truth for seat health. Justifies via P1 and [[ORG-081]] D-9.
- **D-AK. Absorption limited to lifecycle legs; publishing out; retire-by-other-mechanism allowed.** Intent: keep the apex engine a lifecycle composer. Justifies via P1, P6 (the publish tension is preserved, with a named re-evaluate trigger) and P4 (retirements recorded).

### A-8. Build readings (T-5074)

**Note (2026-10-17, T-5074 build readings; HO-accepted, pending the operator's ratification with the T-2667 Amendment).** "A-8" is the next label after the Amendment's A-1..A-7. These readings record how the engine skeleton (T-5074, #1030) interprets text above that a builder had to resolve. They change no decision.

- **A-8.1 plan admits nothing.** `germinate plan` is read-only and non-admitting. It reports drift and a plan digest, refuses while another run holds the mutation claim, and writes nothing under the host root. Admission (the claim plus a run record) starts at `apply`. §4's admission table lists `plan` as admitting; read that as documentary.
- **A-8.2 op keys are op-registry kinds.** A contract's privilege lists name kinds from the typed-op registry. The registry is built from the Action Broker envelope tables, the ORG-082 substrate families and the engine's own table. The op names in §2's example (`file.atomic-write`, `unit.user.install`) are illustrative.
- **A-8.3 privilege vocabulary.** §2's `none | seat | root` maps onto the installer's levels: `none` and `seat` are unprivileged; `root` is privileged-helper.
- **A-8.4 privilege-list disjointness.** `bootstrap_root` is disjoint from `seat` ∪ `broker_runtime`, and `broker_runtime` contains no ORG-082 substrate family. The §2 example's overlap of `seat` and `broker_runtime` is allowed.
- **A-8.5 component op kinds stay under the engine (D-AC).** A component may declare its own op kinds, as literal data the engine reads without importing component code. Those kinds are seat privilege only. They may not take a reserved capability class (broker or envelope classes, substrate, read-only diagnostics), shadow an engine kind, or be an LLM-path kind (D-N). Root and read-only kinds are defined only by the engine's own registry, so the registry stays engine-owned.
- **A-8.6 `germinate sim` is a harness verb.** `germinate sim init` prepares a simulated host root for CI and operator smoke tests. It is a simulation-harness verb, not a lifecycle verb or leg, so it does not count against A-5's "no new verb" ceiling.
- **A-8.7 the CI tier is not confinement.** The in-process planner runner, with its audit-hook guard, catches accidents at CI tier only. It does not cover reads, native code, inherited file descriptors, `os.mkfifo`, direct `fork_exec`, or a planner that disables the guard. C5's sandboxed runner (T-3296, T-5106) is the confinement. Component handler modules are executor-trust code; revisit that trust when a real backend lands (T-5084).
- **A-8.8 a wrapping planner's imports must be admitted (T-5081, S3).** A wrapping planner's transitive imports must be admitted by the C5 import hook. Either the component contract declares a planner import allowlist, or the launcher admits the repo's tools/ import closure read-only. The planner's declarative inputs (for the pantheon-config component, checkouts/) must sit inside the visibility root as manifest data. This is launcher policy for T-5106/T-3296, not a technical impossibility.

**Note (2026-10-17, Orchestrator): A-5 go/no-go result (T-5081, #1037).** GO at the CI tier for "wrap, don't rewrite". repo_reconciler was adopted as the pantheon-config component through the GB seam, with no change to the reconciler or the engine; planning and verify reuse its public API, and apply is designed but not yet built (T-5133). The engine may rewind only its own receipted, clean fast-forward advance (HEAD equals the receipted sha and the tree is clean; otherwise it refuses as drift); this is a component-contract rule, not reconciler behaviour. CONDITION: the root and harness tranches (T-5084, T-5090, T-5091) may be built and land dark, but no live-host actuation happens until S3 (A-8.8) is decided and C5 holds on the production host (T-3296, T-5106).
