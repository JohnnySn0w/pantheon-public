---
aliases: [ORG-066]
id: ORG-066
title: "pantheon-config CI: provision the existing CT202 runner to a fleet-mirror (simplest-first)"
status: Ratified — 2026-08-09 (the operator fire-on-green; codex GREEN)
date: 2026-08-09
build_disposition: built
---

# ORG-066 — pantheon-config CI fleet-mirror on the existing pve2 CT202 runner

**Status:** **RATIFIED 2026-08-09** (the operator fire-on-green, TG 7481/7490; codex second-opinion GREEN on Rev 2 after a first-pass YELLOW). Drafted 2026-08-09 by Metis
(sub-orchestrator-1) per T-2723 (operator-greenlit, TG 7435). **Revised 2026-08-09**
after the operator's simplest-first challenge (TG 7450 / 7453, relayed via Orchestrator): the
first draft proposed a net-new pve2 VM + self-hosted runner; this revision
re-baselines on the runner that **already exists** (CT202) and recommends the
minimal delta that makes CI honest. **Rev 2 (2026-08-09)** tightened three codex
caveats (see next section), which cleared codex to GREEN. This record scopes the design; it does not authorize a build order that skips the D-5 hard gate (T-2443 isolation precedes any full-suite run on CT202). Division: Metis drafts → HO (Orchestrator) relays → the operator
ratifies → **Recovery owns the pve2 build (T-2219)** → **Codex isolates the one
mutating test (T-2443)**.

## Revision 2 — codex-YELLOW tightenings (2026-08-09)

Codex reviewed Revision 1 (the simplest-first re-baseline) and returned YELLOW —
no design hole, three concrete caveats to tighten before green. DECISIONS taken in
response, each `justifies-via` P/D:

- **C1 — Rung 1 forbids `systemd --user` capability for the runner account** (D-2
  Rung-1 boundary + D-3 opening). Revision 1's "reconcile … and, where a test
  needs it, `loginctl enable-linger`" was self-undermining: enabling linger / a
  user bus for the runner account would satisfy skip-guard clause 3
  (`systemctl --user is-system-running`), **un-skip** the real-systemd legs, and
  turn them into new failures — breaking the central claim. Struck the linger
  clause; Rung 1 provisions passwd entries + `tmux` only, with an explicit
  no-linger/no-user-manager boundary for the runner account. Systemd capability is
  now a stated Rung-2/3 transition, never a Rung-1 side effect.
  `justifies-via` **D11** (don't let the provisioning quietly defeat the real
  surface the guard reads) + **P1** + **D12** (smallest correct footprint).
- **C2 — hard build-sequencing gate on the mutating test** (D-5 + Consequences +
  Division). Elevated the T-2443 mutating test from "flagged residual" to a hard
  precondition: **no full-suite run on the provisioned CT202 until T-2443 is
  isolated or that test is deselected.** Rung-1 provisioning removes the
  `getpwnam`-KeyError accidental circuit-breaker, making the live
  `systemctl --user start` path *more* reachable, so isolation must precede the
  first real run. `justifies-via` **D11** (a real mirror makes a real mutation path
  reachable — close it before the real run) + **P1** + **P5** (codex owns the fix;
  Metis gates the order) + **D5**.
- **C3 — sharpened D-3 provisioning boundaries** (D-3 boundaries a–e): reconcile
  only the manifest's account set (never touch unrelated/local/runner/root
  accounts); fail-loud on UID/GID conflict (no clobber); minimal inert account
  posture (locked, nologin, scoped home, no CI-path ownership); pre-job drift mode
  is strictly read-only (never silently mutates); reconciliation stays
  target-agnostic/portable so Rung 2/3 reuse it. `justifies-via` **P4**
  (traceable, bounded, manifest-derived state) + **ORG-049** (Layer-0 provisioning
  discipline — convergent, idempotent, verifiable).

**Related:** [[ORG-049]] (manifest-driven installation + convergent bringup — the
provisioning here is its Layer-0/Layer-1 applied to a CI target), [[ORG-039]]
(host-provisioning contract), [[ORG-031]] (harness abstraction), [[ORG-027]]
(orchestrator-tier domains — why the mutating-test fix stays codex's),
`instance.yaml` (the manifest the mirror reconciles against). Tickets: T-2723
(this design), T-2219 (the build + full runner-migration history), T-2443 (the
residual mutating-test isolation, codex-domain), T-2249 (prior host-coupled-test
class, resolved), T-2250 (VPS-runner-retirement access, operator-tier),
T-2714/PR #262 (the getpwnam red-line that surfaced this). Grounding research:
`knowledge/watch/reports/2026-08-09/pve2-ci-staging-runner-grounding-T-2723.md`
(commit 2ce74c41e5).

## Arc-intent

pantheon-config CI goes green by validating the host-coupled tests against a
**real fleet-mirror**, restoring a passable required check instead of the chronic
`--admin`-merge-over-red — with the **smallest structural change that works**. The
tests exist to prove cross-account (`getpwnam`) and real-`tmux` integration
against a genuine OS surface; the design preserves that signal rather than mocking
it away (the operator, TG 7432/7433: "real system, real checking, mocks aren't
useful"), while not standing up infrastructure the failing tests don't require.

## Context

pantheon-config CI is chronically red and merged past with admin bypass:

- **26 failures/run, all `getpwnam` KeyError** (`tools/persona_cutover_resolve.py`
  + the `bringup_acceptance` suite call `pwd.getpwnam('claude-research' /
  'claude-fallback' / 'specialist' / …)`, which `KeyError`s on a runner that carries
  none of the fleet daemon accounts), plus **real-tmux** failures
  (`FileNotFoundError: 'tmux'`). `origin/main` is red the same way (run
  31319001565), so every code PR merges via `--admin` over red (T-2443/T-2714).
  **A gate nobody can pass is worse than no gate** (T-2220).
- **CI is ALREADY self-hosted on pve2.** Recovery migrated pantheon-config's runner
  off the VPS in T-2219: `pve2-pantheon-config` (registration **id 22**, install
  `/opt/actions-runner-pc` on **CT202**, an **unprivileged Debian-13 LXC**); both
  pantheon-config workflows are pinned to the `pve2` label; the VPS runner is
  retired. So the question is **not** GH-hosted-vs-self-hosted, and **not** "stand
  up a runner" — it is the minimal provisioning delta on the runner that exists so
  the host-coupled tests pass **for real**. The getpwnam class went red precisely
  because CT202 is a bare CI container lacking the fleet accounts the VPS happened
  to have (a *false green* on the VPS — T-2249's lesson).
- **the operator's simplest-first steer (TG 7450 / 7453):** don't over-build. Evaluate
  the smallest fix first; keep a VM only if a test genuinely forces it.

## Empirical finding — what the 26 failures actually need (the crux)

The whole design collapses onto one empirical question: do the failing tests need
only **(a) daemon accounts + (b) a `tmux` binary** — both trivial on the existing
CT202, no systemd — or does any of them genuinely need **real `systemd --user`**
(worse: `systemctl --user --machine=<account>@.host`, which needs systemd +
machined)? Verified test-by-test in the source:

- **The failing set needs only accounts + tmux.** Two classes fail on a bare
  runner and neither touches real systemd:
  - **getpwnam / accounts** — `persona_cutover_resolve` and the `load_binding`
    path in `bringup_acceptance` resolve fleet accounts via `pwd.getpwnam`;
    absent-account ⇒ `KeyError`. Provisioning the accounts fixes these.
  - **real tmux, private socket** — the `bringup_acceptance` `readiness_gate` leg
    proves the real `submit_verify.ready_for_submission` predicate against real
    `tmux capture-pane` output using, verbatim, "a fully private
    `tmux -S <socket file>` server. **No systemd scope**, no default tmux server
    interaction of any kind — total isolation" (`tools/bringup_acceptance/harness_runtime.py:18`).
    Absent-`tmux` ⇒ `FileNotFoundError`. Provisioning the `tmux` binary fixes
    these.
- **The real-`systemd --user` tests are NOT in the failing set — they self-skip,
  and stay skipped even after accounts+tmux are provisioned.** Every real-systemd
  leg (`harness_codex_ready`, `harness_codex_rc_ready`, `harness_fallback_swap`,
  `producer_fires`, `restore`) is guarded by a **three-part probe** before it runs
  (`_skip_unless_disposable_tmux_available`):
  1. `shutil.which("tmux")` present, **and**
  2. `shutil.which("systemd-run")` present, **and**
  3. `systemctl --user is-system-running` returns `running` or `degraded` — i.e. a
     **genuinely usable `systemd --user` session for the runner account**.

  A GitHub Actions job runs as the runner account (`ci-runner`/`runner`) with no
  linger and no login session, so clause (3) does not hold in the CI job context —
  the tests skip. Crucially, **provisioning accounts + `tmux` does not un-skip
  them**: clause (3) still fails without a real per-account user manager, so they
  remain skips rather than becoming new failures. `producer_fires.py:258` labels
  its real-systemd wiring "Not exercised by the [default suite]"; `harness_runtime`
  leg 3 (`ensure_runtime_chain`) **mocks** the systemd/tmux-touching internals via
  `test_runtime_controller.py`'s established seam. The fixture-based files that
  merely *mention* tmux/systemctl in comments (`forced_cap_return`,
  `urgent_mail_preempt`, `inventory`, `optionals_recovery`) inject observations and
  shell out to neither.

**The tmux-on-logout regression does not apply.** The grounding report's
load-bearing argument for a VM was the Debian-12-LXC regression where user
processes die on logout. That bug is a **logind session-teardown** artifact — it
fires when a *login session* ends. The tests spawn tmux **in-process on a private
`tmux -S <socket>`** inside a short-lived test process; there is no login-then-
logout cycle for the regression to act on. So the concrete landmine that pushed
the first draft toward a VM **dissolves** for this test pattern. (Confirmed on-box
is still cheaper than certain — see Open Questions.)

**Basis of this finding:** a test-by-test read of the source (the three-part skip
guard, the private-`tmux -S` isolation, and the fixture-only files that shell to
neither tmux nor systemd) — not a claim about a specific reproduced failure count.
The definitive confirmation is the on-box re-run after provisioning (Open Question
(a)), which only CT202 can give; the source read establishes what that re-run
should show and why (accounts+tmux pass, real-systemd legs stay skipped).

## Decisions

Each decision carries `justifies-via` P/D ids per the chain-of-justification
discipline (P3, P4).

### D-1 — Validate against a real fleet-mirror, not mock/skip (unchanged)

Fix the environment so the getpwnam/tmux tests pass by executing against genuine
OS state (real accounts, real `tmux`), not by skipping on `KeyError` or injecting
a fake `pwd` resolver.

`justifies-via` **D11** (validate against the real surface; the precondition-
fidelity corollary names exactly the "synthetic state past the precondition"
anti-pattern a `getpwnam` mock would reintroduce) + **P1** (architecture over
policy). operator-ruled TG 7432/7433.

### D-2 — Simplest-first ladder; recommend Rung 1 (minimal provisioning on the existing CT202)

Re-baselined per the operator TG 7450/7453. Provision the **minimum** the failing tests
need onto the runner that already exists, and climb only if an on-box check forces
it. **Recommendation: Rung 1.**

- **Rung 1 (recommended) — provision the fleet daemon accounts (`/etc/passwd`
  entries) + the `tmux` binary directly on CT202. Nothing more — and specifically
  NO `systemd --user` capability for the runner account (see the boundary below).**
  No new VM, no inner container, no new runner. The empirical finding says this
  covers all 26: the accounts satisfy `getpwnam`, the `tmux` binary satisfies the
  private-socket real-tmux tests, and the real-systemd legs keep skipping. The
  Debian-LXC tmux-on-logout regression does not touch the private-socket pattern,
  so the unprivileged LXC is not disqualified. **Net change = account/tmux
  provisioning + drift-sync (D-3); zero new infrastructure.**
  - **Load-bearing Rung-1 boundary (codex C1) — do NOT enable linger / a user bus
    / a user-manager for the GitHub Actions runner account.** The skip guard's
    clause 3 is `systemctl --user is-system-running ∈ {running,degraded}` evaluated
    for the account the suite runs as (the runner account). Enabling linger or a
    user manager for that account would satisfy clause 3, **un-skip** the
    real-`systemd --user` legs, and turn them into NEW failures — destroying the
    "26 need only accounts+tmux, systemd legs stay skipped" property this whole
    rung rests on. Rung 1 therefore provisions passwd entries + `tmux` only, and
    must leave the runner account with **no usable `systemd --user` session**.
    Granting real systemd capability is a deliberate **Rung-2/3 transition** (where
    the systemd legs are *meant* to run), stated as such — never a Rung-1 side
    effect. This applies to the mirrored fleet accounts too under Rung 1: they are
    `getpwnam` targets, not accounts the suite starts user managers for.
- **Rung 2 — nested rootless-podman / privileged container on CT202** — only if
  Open-Question (a) shows the tmux tests fail on CT202 despite Rung 1 (e.g. an
  unforeseen LXC interaction with the private-socket pattern). Contained to CT202.
- **Rung 3 — a net-new VM on pve2** (the first draft's proposal) — only if both
  lower rungs are shown insufficient on-box. Carries the VM's capacity cost
  (Open-Question (b)).

`justifies-via` **D12** (structural correctness at the *smallest* footprint that
is correct — a VM the failing tests provably do not require is surplus structure,
not extra safety; the ladder makes the escalation evidence-gated) + **P1** +
**D1** (least standing maintenance surface — one provisioning script vs a VM to
patch and keep alive) + **P6** (the rungs preserve the VM option rather than
discarding it; escalation is gated on evidence, not asserted away).

### D-3 — Topology mirror: manifest-driven idempotent reconciliation from `instance.yaml` (load-bearing, retargeted to CT202)

Provision the daemon accounts from `instance.yaml` as the single source of truth,
via an **idempotent, manifest-driven reconciliation script** that reconciles
CT202's `/etc/passwd` to match the manifest. Under Rung 1 it provisions **passwd
entries only — it does NOT enable linger, a user bus, or a user-manager** for any
account (see D-2's Rung-1 boundary / codex C1); systemd-user capability is a
deliberate Rung-2/3 transition, never emitted by the Rung-1 reconciler.
Re-running produces no diff when already in sync. Running it in `--check`
(dry-run) mode is the **drift detector** — it surfaces divergence between
`instance.yaml` and the CI environment without mutating anything, and doubles as a
"the fleet topology changed and CI forgot to catch up" alarm. Run it as a
**pre-job hook** so account drift cannot silently invalidate what the tests
validate against.

**Accounts to mirror** (from `instance.yaml`, role→persona→account): `claude`
(orchestrator), `claude-research` (specialist), `claude-fallback` (recovery), `codex` (cairn),
`the-ancestor` (the-ancestor), `suborch1` (metis), `suborch2` (atlas),
`exocortex` (mnemosyne). Fixed UIDs/usernames created **at provision time**, not
test time. `tmux` installed on `$PATH`.

**Provisioning boundaries (codex C3) — the reconciler must:**
- **(a) Reconcile only the manifest's account set.** It must never create, modify,
  lock, or delete a local account outside the `instance.yaml`-derived set — no
  touching the runner account, `root`, or any pre-existing unrelated user.
- **(b) Fail loudly on UID/GID conflict, never clobber.** Provision fixed UIDs/GIDs
  from a reserved range; if a target UID/GID or name already belongs to an
  unrelated existing account, the reconciler **errors and stops** rather than
  reassigning or overwriting it. No silent reuse of an occupied id.
- **(c) Provision a minimal, inert account posture:** created **locked** (no
  password / `!` in shadow), **login-shell disabled** (`/usr/sbin/nologin` or
  equivalent), a home dir only if a test reads `account_home` (created, owned by
  the account, mode-scoped), group membership per the manifest, and **no ownership
  of the runner workspace or any CI path**. These are `getpwnam` fixtures, not
  interactive or service accounts.
- **(d) Pre-job drift mode never mutates.** In its pre-job/`--check` role the
  detector is strictly read-only — it reports drift (and may fail the job on
  drift) but performs **no** account mutation in that mode. Mutation is a separate,
  explicit provisioning invocation, never a silent side effect of the drift check.
- **(e) Keep it target-agnostic / portable.** The reconciliation is manifest-driven
  and host-neutral (a script reading `instance.yaml`, e.g. Ansible `user` /
  idempotent shell), **not** CT202-only hand-provisioned shell state — so a later
  climb to Rung 2/3 reuses it unchanged against a container image or VM.

This is the design's **real and durable tension** (the operator's own framing in
T-2723) and it is **rung-independent** — whether Rung 1, 2, or 3, the account set
must track the manifest. It is the one piece of the first draft that survives the
simplification intact; it just targets CT202 instead of a new VM.

`justifies-via` **ORG-049** (extends its Layer-0 host-substrate + Layer-1
convergent-bringup discipline to a CI mirror; repeated provisioning yields one
known state and verification proves it) + **P4** (the mirror's account state is
derived from and checkable against the manifest, never hand-curated) + **P1**.
No pantheon-specific prior art for "regenerate CI accounts from a fleet manifest"
(grounding §2.3); standard idempotent-IaC applied to our manifest — recorded as
reasonable-default.

### D-4 — The self-hosted-runner setup + most of the security-hardening dissolve

The first draft's runner-standup + threat-model + hardening sections are **largely
struck**: the runner already exists on CT202 (id 22), already ran the suite
(T-2219), and this ADR does not create a new one. Net change is account/tmux
provisioning + drift-sync — **not** a new runner posture.

What remains recorded (not re-litigated — properties of the existing runner):

- **Threat model is unchanged and already accepted:** single-org, private repo,
  contributors are the operator + the fleet, no fork-based external contribution — so
  the GitHub "don't use self-hosted with untrusted contributors" fork-PR warning
  does not apply (grounding §1.1). The residual risk (dependency supply-chain,
  state-leak across jobs) is a property of the runner that already exists and is
  out of scope for this provisioning change; if the fleet later hardens the runner
  (runner-group scoping, outbound-only-to-GitHub egress, per-job workspace
  cleanup), that is a separate T-2219-lane item, not a blocker for making CI green.
- **Standing constraint carried forward (T-2219):** `vibecamp-scheduler` is the
  only PUBLIC repo, runs `ubuntu-latest` with zero self-hosted runners, and **must
  never gain one** — untrusted-fork-PR RCE. This ADR touches pantheon-config's
  existing runner only and does not relax that line.

`justifies-via` **P7** (don't deliberate/build past the cheap correct action — the
runner exists; provisioning it is the near-zero-surface move, and re-deriving a
whole runner threat model for an unchanged runner is surplus) + **D12** (footprint
matched to the actual change) + **P4** (the accepted-risk boundary stays
documented, just not re-opened).

### D-5 — Read-vs-mutate test split, and a HARD build-sequencing gate on the mutating test (codex C2)

- **Read-real-state class (handled by Rung 1):** the getpwnam + private-socket-
  tmux tests pass for real once CT202 carries the accounts + `tmux`.
- **The ONE mutating test (NOT handled here — must be isolated regardless):**
  `tools/test_persona_installer.py`
  `test_local_backend_refuses_cross_account_before_mutation`. Where the target
  persona resolves to the running euid, the cross-account guard does not fire and
  `LocalHostBackend.apply` reaches a **real `systemctl --user start`**, installing/
  starting/failing a real unit and paging via `OnFailure` (T-2443). The fix is
  proper test isolation (fake/simulated backend, or a provably-not-current-euid
  account, or stub `pwd.getpwnam`/`os.geteuid`) so the `LocalHostBackend` path can
  never reach a real `systemctl start` from a unit test — **independent of the
  environment.** `persona_installer.py` is host-control's ORG-052 module; the fix
  is codex-domain (T-2443 already targets codex). This ADR does **not** fix it.

- **⚠ HARD BUILD-SEQUENCING GATE (codex C2) — provisioning must precede isolation
  in DESIGN but MUST NOT precede it in EXECUTION.** The two fixes are separable as
  design but **not** safely separable in run-order. Today, the `getpwnam` KeyError
  is an *accidental circuit-breaker*: the mutating test dies at account resolution,
  before the `LocalHostBackend` mutation path is ever reached. **Rung-1
  provisioning removes that accidental safety net** — with the accounts present,
  the test proceeds deeper and the live `systemctl --user start` path becomes
  *more* reachable on the runner, not less. Therefore, as a precondition of the
  build (not merely a flagged residual): **NO full-suite run happens on CT202 after
  Rung-1 provisioning until T-2443 is isolated, or that single test is explicitly
  excluded/deselected from the run.** Recovery and Codex sequence accordingly —
  T-2443's isolation lands (or the test is deselected) *before* the first real
  suite run on the provisioned mirror. This is a gate on the T-2219 build order,
  surfaced here so it cannot be missed.

`justifies-via` **P5** (roles don't hop — the persona_installer fix is codex's
domain; Metis flags + gates, does not fix) + **P6** (two fixes with opposite
meanings kept distinct rather than collapsed into "the container fixes CI") +
**D11** (the real mirror makes a real mutation path reachable — validate-against-
real cuts both ways, so the dangerous path must be closed before the real run) +
**D5** (the done-bar suite run must not itself mutate production-adjacent state).

### D-6 — Migration / cutover / rollback (simplified — no runner cutover needed)

Because the runner is unchanged, there is **no `runs-on` cutover and no new
registration**. Migration is just: land the D-3 provisioning + pre-job hook on
CT202; re-run the suite; confirm green. **Rollback** is trivial — the provisioning
is additive and reversible (remove the accounts/hook), and CI returns to its
current (honestly-red) state with no runner surgery. Carry Recovery's T-2219
Debian-container gotcha: `actions/setup-python` has no Debian prebuilt CPython, so
the tool cache is seeded from `astral-sh/python-build-standalone` with
`AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache` — already solved on CT202, noted so a
Rung-2/3 escalation re-applies it.

`justifies-via` **P1** + **D5** (prove green on CT202 before declaring the fix
landed; never call it done on an unverified re-run).

## Open questions — on-box verification on CT202, NOT blocking this design

These need an **on-box check on CT202** (Recovery), not more desk research; they gate
which *rung* is built, not whether the design is sound:

- **(a) The Rung-1 confirmation (first on-box check):** after adding the fleet
  accounts + `tmux` to CT202 **and with the D-5 gate satisfied** (T-2443 isolated
  or that test deselected — see below), run the 26 and confirm **(i)** the getpwnam
  + private-socket-tmux tests pass, and **(ii)** the real-`systemd --user` legs
  still **skip** (clause-(3) `is-system-running` remains unsatisfied for the runner
  account — which requires that D-2's no-linger-for-the-runner boundary held)
  rather than un-skipping into new failures. If both hold → Rung 1 is the entire
  fix, zero new infra. If the tmux tests fail on CT202 despite Rung 1 → climb to
  Rung 2. **Do not run the full suite on the provisioned mirror before the D-5
  gate is met** (codex C2).
- **(b) VM capacity/headroom on pve2** — relevant **only** if forced to Rung 3;
  moot for Rung 1/2.

**Sourcing caveat (carried from the research):** the "ephemeral mode is now
GitHub-default-recommended as of March 2026" claim rests on a single unverified
blog source and is **not** relied on anywhere here.

## Consequences

- **CI becomes a passable required check again** at the smallest footprint: the
  read class goes honestly green on the existing runner, ending the
  `--admin`-over-red posture once D-5's mutating test is also isolated.
- **A new standing invariant:** the CI mirror must track `instance.yaml`. D-3's
  dry-run drift detector is the guard.
- **Reversible.** The provisioning is additive; no runner registration or
  `runs-on` change to unwind.
- **Hard build-sequencing gate (not just a residual):** the mutating test (T-2443,
  codex) must be isolated — or that single test deselected — **before any
  full-suite run on the provisioned CT202** (D-5 / codex C2). Rung-1 provisioning
  removes the `getpwnam`-KeyError circuit-breaker that currently keeps the mutation
  path unreachable, so the isolation must land first, not "eventually."
- **Escalation stays open, evidence-gated:** if Open-Question (a) surprises,
  Rungs 2/3 are pre-reasoned and available — the design does not have to be
  re-opened, only the rung re-selected.

## Division of labor

| Step | Owner | Ticket |
|---|---|---|
| Draft this ADR | Metis (sub-orch-1) | T-2723 |
| Relay to the operator + re-run codex | Orchestrator (HO) | — |
| Ratify (gated on codex-green) | the operator | — |
| **Isolate/deselect the mutating test — BEFORE any full-suite run on CT202 (D-5 gate)** | Codex | T-2443 |
| Build (provision CT202 passwd+tmux, NO runner linger; drift-sync; on-box check (a)) | Recovery | T-2219 |

**Build-order note:** the T-2443 row is sequenced *before* the first real suite run
in the T-2219 build, per the D-5 hard gate — they are separate owners but a fixed
order, not parallel.
