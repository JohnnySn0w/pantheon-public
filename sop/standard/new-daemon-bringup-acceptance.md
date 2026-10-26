# SOP: New-daemon bring-up acceptance

**Status:** Ratified 2026-10-07 by the operator (Telegram 11324, ticket walk T-3285). The off-prod suite and procedure (steps 1-4) are practised; **the live-window part (step 5 and the live constituents) is a STUB, not yet run**, per `writing-sops-from-first-use.md`. The first attended live window fills it in. Promoted from `notes/2026-08-05-new-daemon-bringup-acceptance-sop-draft.md` (T-2556 phase 15 / T-2577). Authored by Orchestrator (HO); the suite was built by the T-2556 implementer team.

## Purpose

When a *new* Pantheon daemon is provisioned (a fresh agent seat, a rebuilt account, a recovery clone brought online), "it seems to work" is not evidence. This procedure produces **repeatable, evidence-backed proof** that the daemon's substrate is correctly brought up — each capability proven by a module that isolates itself, records a hash-chained evidence trail, and fails closed on any anomaly. The output is a **closure matrix**: a per-capability GREEN/RED verdict a human can read at a glance.

The operator's framing at commission: *be EXTREMELY thorough*; the suite is multi-session by design.

## The suite

`tools/bringup_acceptance/` — a library of acceptance **modules**, each implementing the `AcceptanceModule` protocol (`declare` → `prove` → `restore` → `report`) from `types.py`, plus the shared machinery:
- `recorder.py` — hash-chained evidence recorder.
- `evidence.py` — run-manifest / inventory / closure persistence.
- `inventory.py` — ground-truth target discovery (units, hooks, mailbox, telegram surface) + the module_id catalog + parity oracles.
- `restore.py` — fail-closed exact-restore/baseline primitive (HardStop on residue).
- `registry.py` — persona-optionals discovery (`optionals/<persona>.py` plug-ins).
- `closure.py` — `build_closure(rows, composites)` + `render_closure_markdown`; a composite is GREEN **iff every constituent is proven**.
- `runner.py` — `run_end_to_end(ctx, target, catalog_modules, composites)`: the e2e runner that ties it all together.

## The procedure

1. **Build the target inventory.** Discover the daemon's real surface into a `TargetInventory` (units, hooks, mailbox hub, telegram surface, persona identity). The runner takes an already-built inventory — live fact-gathering is a separate step and is **never** run against real system state during off-prod acceptance.
2. **Run the e2e runner** (`runner.run_end_to_end`): it runs the core catalog modules + the persona optionals + resolves the ≥1-of wake-trigger, collects one `MatrixRow` per module, then calls `build_closure` with the composites map and renders the matrix.
3. **Read the closure matrix.** Two distinct row types:
   - **Per-module rows** — each capability's own module. Core-tier rows read `proven` (off-prod substrate verified) or `gap` (a real problem). Optional-tier rows read `proven` or `declared_absent` (cleanly skipped — a bare daemon with no plug-in for that persona is not a failure).
   - **Composite rows** — the five real *capabilities*. GREEN only when every constituent (including the live-account constituent) is proven.
4. **Interpret RED composites correctly.** After an off-prod-only run, **every composite is RED**, and the aggregate verdict is RED. This is correct and honest, not a failure: the off-prod modules are all green (substrate proven), but a capability cannot be claimed fully-functional until its **live-account constituent** is proven in a coordinated window. The RED row names exactly the missing live constituent (e.g. `harness-runtime-live`).
5. **Populate the live rows in a coordinated window.** The live "L" legs are `interactive-required` — they touch real accounts / real tmux panes / asymmetric sudo and are **never run unattended**. They run only in an attended window (the operator + HO). As each live constituent is proven, its composite flips toward GREEN. A daemon is *fully brought up* only when the full matrix is GREEN.

## The anti-inferred-green principle (why composites exist)

A per-module green must **never** be read as its capability being proven. "The mailbox substrate delivered in-suite" ≠ "mailbox is fully functional" — the latter additionally needs the runtime to actually land+execute a real kick and a real wake-trigger to fire. Composite rows enforce this structurally: they stay RED, naming the missing constituent, until the whole chain is proven. The suite is built to make an over-claim impossible to render as green.

## The five capabilities (composites)

| Composite | Off-prod constituent(s) | Live constituent(s) — gate on a window |
|---|---|---|
| `mailbox-fully-functional` | `mailbox-roundtrip`, `mailbox-wake-trigger` (≥1-of timer/path) | `harness-runtime-live` |
| `telegram-fully-functional` | `telegram-outbound` | `telegram-inbound`, `telegram-user-visible`, `telegram-failure-modes` |
| `harness-runtime-fully-functional` | `harness-runtime` | `harness-runtime-live` |
| `harness-fallback-swap-fully-functional` | `harness-fallback-swap` | `harness-fallback-swap-live` |
| `forced-cap-return-fully-functional` | `forced-cap-return` | `forced-cap-return-live` |

## Off-prod module coverage (what each proves, disposable/in-suite)

- **hook-emissions** — each wired Claude-Code hook fires + emits, per-hook path_id, per-prover disposable HOME sandbox.
- **mailbox-roundtrip** — send → new/ → drain → cur/ → ack → return-to-idle, correlated reply-route; substrate-only (D11 idempotency contract: post-ack redelivery is *reprocessed*, not suppressed — idempotency is the consumer's job).
- **producer-fires** (timer-cron / watch-producer) — each persona-directed producer trigger fires (accelerated stand-in cadence, not literal-cadence parity — ratified convention); unit-failure-alert into mailbox.
- **harness-runtime** — diagnose + readiness gate (real isolated `tmux -S`) + ensure_runtime contract order; the live kick-lands-executes is deferred to `-live`.
- **telegram-outbound** — send-accept CONTRACT + failure-mode classification via an injected seam (bot-lock / single-poller / mcp-eviction / wrong-chat), zero real network; real user-visible receipt is `-live` (a bot can't read its own outbound).
- **harness-fallback-swap** — the full planned claude→codex transition state machine (preparing → awaiting-successor-read → completed), exactly-one --permission-mode gate, correlated mailbox roundtrip through the post-swap-resolved binding.
- **forced-cap-return** — forced_swap on authoritative cap → chronicle-receipt bootstrap → requeue → quota-reset return-to-primary.
- **keep-going-kicker** — dedupe/rate-limit bounding + a parity oracle vs the known-good per-daemon kicker; records observed per-seat firing state (non-firing seats are named findings, not defects).
- **optionals contract + specialist/orchestrator/recovery plug-ins** — persona-specific surfaces (Specialist watch-runner/auto-commit; Orchestrator ho-resumer + the stop-failure→resumer schema contract; Recovery recovery-watchdog/self-restart/boot-kick, static-only where touching Recovery's real session would be a landmine).

## Isolation discipline (non-negotiable)

Every module: disposable state (env-overridden roots), exact restore with **HardStop on any residue**, and it **never** touches a canonical clone, a real mailbox, a real chronicle, or a live account. Recovery (the SPOF recovery account) gets extra caution: never start/restart/kick it, never invoke asymmetric sudo, never manufacture its real tmux session name — those are the live `-live` ticket's job, in a window.

## Findings surfaced during buildout (open tickets)

The acceptance work itself surfaced real defects (a good acceptance suite finds things):
- **T-2581** — `backlog-surface.sh` hardcodes an absolute BACKLOG.md path (non-portable to a new daemon).
- **T-2582** — no dedicated non-operator Telegram test chat exists; blocks a real automated send-accept round-trip.
- **T-2583** — `keep_going.py` read/write_state has no flock (low-likelihood send-keys same-state race).
- **T-2584** — forced-cap fallback can't auto-return to primary (`primary_recovery_evidence` gates on `trigger==planned-quota`; forced_swap sets `forced-cap`). Needs a product decision.

## Live window: first run, record the actual procedure here (STUB)

*Empty until the first attended live window.* The `-live` legs (`harness-runtime-live`, `telegram-inbound`, `telegram-user-visible`, `telegram-failure-modes`, `harness-fallback-swap-live`, `forced-cap-return-live`) have never run. Whoever runs the first window with the operator records here: the coordination steps actually used, the order, how each live constituent was proven, and what surprised them. Then drop this STUB marker. Label that ticket `sop:new-daemon-bringup-acceptance` (T-4680 gate).
