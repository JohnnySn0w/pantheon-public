---
title: Onboarding a mailbox-first seat onto the consume control plane
status: Drafted 2026-08-08 by Orchestrator (HO), written from the real execution that landed T-2677 (metis + atlas moved onto pantheon-persona-consume, operator-directed TG7340/7343). Adopted 2026-08-08 by Orchestrator (HO) — the same below-goal-level methodology-SOP pattern `onboarding-a-new-harness.md` (T-1031) and `migrating-an-existing-persona-onto-the-control-plane.md` (T-2633) were adopted under (autonomous per msg-3733 principles-as-ratification; sop/ is an HO-written surface per D2). The operator override-available. The ONBOARDING ACTION on any specific seat remains a goal-level call; this SOP is the how, not a decision to fire. Documents the CURRENT operator recipe (performed by hand); generalizing it into a one-command op is a tracked follow-up (T-2679).
---

# Onboarding a mailbox-first seat onto the consume control plane

**Rationale:** the two sibling SOPs each cover a case this one does not:

- `migrating-an-existing-persona-onto-the-control-plane.md` moves an EXISTING claude persona that already runs a bespoke telegram-bot supervisor onto `pantheon-persona-consume@<persona>`, via `tools/persona_cutover.sh`. That tool HARD-REQUIRES a telegram channel (`persona_cutover_resolve.py` `CONTROL_PLANE_CHANNELS` check, ~line 327) and a bespoke supervisor to tear down.
- `onboarding-a-new-harness.md` brings up a NEW non-claude harness (codex, etc.).

Neither fits a **from-scratch, mailbox-first claude seat** — no bespoke supervisor to migrate from, and NO telegram channel (all comms via the mailbox). The sub-orchestrator seats **metis** (suborch1) and **atlas** (suborch2) were the first two, moved 2026-08-08 under T-2677 after ~10 days of dormancy caused by their old wake timers being `start`-ed but never `enable`-d (they did not survive a session restart). The consume control plane replaces that: an `enable`-d `pantheon-persona-consume@<persona>.timer` fires every ~2min, and each fire wakes (or first-launches) the seat to work its mailbox.

**Key fact that makes this config-only (no code change):** a mailbox-first seat carries NO `native.channels` in its manifest launch stanza. `tools/harness_adapter/claude.py` `build_interactive_launch` (~line 371) appends `--channels` ONLY when channels is a non-empty string, and `tools/harness_adapter/diagnostics.py` (~line 455) validates channels ONLY when present. So the channel-less launch is simply `claude --model claude-opus-4-8 --permission-mode default` — the same shape as specialist/recovery minus the `--channels` flag.

---

## When to use this

- Bringing a **claude persona that has no bespoke telegram supervisor** (e.g. a sub-orchestrator seat that has only been run as a bare TUI, or was launched by hand) onto `pantheon-persona-consume@<persona>`.
- The seat is **mailbox-first**: it has no operator-facing telegram channel; it is woken to consume its mailbox/inbox.

Do NOT use for:
- A persona already running a bespoke telegram-bot supervisor — use `migrating-an-existing-persona-onto-the-control-plane.md` (and it needs the telegram channel the cutover tool requires).
- A brand-new NON-claude harness — use `onboarding-a-new-harness.md`.
- Giving a claude agent the full ORG-020 self-restart stack — use `adding-restart-capability-to-a-new-agent.md`.
- Recovering an already-onboarded, wedged consume seat — ordinary recovery (`../remediation/`).

---

## Prerequisites

- [ ] The seat's unix account exists and is a member of the `pantheon-mailbox` group (the consume `ExecStart` wraps in `sg pantheon-mailbox`; without membership the fire fails). Verify: `id -Gn <account>`.
- [ ] The seat has a durable mailbox hub dir at `/home/the-ancestor/mailbox/<persona>` with `new/ cur/ tmp/`.
- [ ] The seat has its OWN `pantheon-config` clone it can read/pull.
- [ ] The seat has a live `~/CLAUDE.md` persona brief (or you will author one) — the consume install's `persona.brief` precondition requires a TRACKED `pinned-rules/<persona>.md` (see step 2).
- [ ] You (the operator) can run as the seat account (`sudo -S -k -u <account> env XDG_RUNTIME_DIR=/run/user/<uid> ...`), and the account's user-systemd manager is reachable (linger on).

---

## The recipe (per seat)

### 1. Fill the manifest launch stanza (channel-less)

In `instance.yaml` `harness_runtime.roles.<role>`, fill the stub to carry the control-plane launch shape, mirroring the `specialist` (specialist) / `recovery` (recovery) stanzas but WITHOUT a `channels` key:

```yaml
    <role>:
      tmux:
        session: <the seat's live tmux session name>   # VERIFY it first-hand; do not guess
      post_start:
        - command: ["{account_home}/pantheon-config/tools/arm_pane_log.sh", "{tmux_target}", "{persona}"]
        - command: ["{account_home}/pantheon-config/tools/recovery_boot_kick.sh", "{tmux_target}", "{persona}"]
          background: true
      claude:
        profile: interactive-subscription
        model: claude-opus-4-8            # fleet policy (T-2643)
        native:
          permission_mode: default
          # NO channels key — this is the mailbox-first difference
```

Commit + push to `main`, then `git pull` the seat's OWN clone to that commit (the install reads the seat's clone, not HO's authoring clone). Validate before pushing: `python3 -m pytest tools/test_harness_runtime.py tools/test_instance_role_grants.py tools/test_persona_installer.py -q`.

### 2. Version-control the persona brief + symlink

The `persona.brief` precondition (`persona_installer.py` ~line 798, `persona_bringup.py` ~line 496) requires a tracked `pinned-rules/<persona>.md`. Adopt the seat's EXISTING live brief verbatim rather than inventing one:

- Copy `/home/<account>/CLAUDE.md` content into `pinned-rules/<persona>.md`, commit + push, pull the seat clone.
- As the seat account, back up and symlink the live path to the tracked file (the specialist/recovery/mnemosyne pattern):
  `cp ~/CLAUDE.md ~/CLAUDE.md.pre-symlink-<date>; ln -sf ~/pantheon-config/pinned-rules/<persona>.md ~/CLAUDE.md`

### 3. Install the consume units as operator, then fire

`persona_installer` deliberately leaves the privileged/systemd operations (unit-install, daemon-reload, enable, fire) to an **operator** — its plan marks them `privileged-helper` / `operator-required`. The `t2611_recovery_persona_apply.py apply` driver is Recovery-flavored and does NOT reliably materialize the units for a fresh persona in a cross-account invocation. So perform the operator steps directly, mirroring an already-live consume seat's materialized units (e.g. `/home/claude-research/.config/systemd/user/pantheon-persona-consume@.service` — it is fully `%i`/`%h`-parameterized and account-agnostic; only its provenance header comment differs per seat):

1. **KILL any stale pre-existing pane/claude for the seat FIRST.** This is load-bearing — see the gotcha below.
2. As the seat account: `mkdir -p ~/.config/systemd/user ~/.local/state/pantheon/persona-installer/<persona>/receipts ~/.local/state/pantheon/personas`
3. Write `~/.config/systemd/user/pantheon-persona-consume@.service` and `@.timer` from `tools/systemd/pantheon-persona-consume@.{service,timer}` (optionally prepend the `# instance=... role=... persona=... harness=claude revision=<sha>` provenance header).
4. `systemctl --user daemon-reload && systemctl --user enable --now pantheon-persona-consume@<persona>.timer`

The timer fires the consume service, which runs `persona_bringup.py consume --persona <persona>` → `runtime_controller.ensure_runtime` first-launches a fresh `claude` in the seat's tmux session with the manifest launch config, then wakes it via a mailbox kick.

### GOTCHA — kill the stale pane FIRST (T-2678)

If a stale claude pane already exists for the seat when the consume timer first fires, `runtime_controller.ensure_runtime` hits the mismatch branch (`runtime_controller.py:1066`): the pane EXISTS but its `runtime.json` is absent or does not match the binding → the service FAILS every fire (~every 2min) and each failure fires `OnFailure=pantheon-unit-failure-alert@` (alert-spam). The fix is to kill the stale pane BEFORE enabling the timer, so the consume fire creates a fresh pane AND writes a consistent `runtime.json`. (In T-2677, two aborted `t2611 apply` attempts fired the timer against the pre-existing orphan pane and produced exactly this failure; the clean reinstall + `systemctl --user reset-failed` resolved it.)

---

## Verification checklist

Run after enabling the timer, before declaring the seat onboarded:

- [ ] Service fires clean: `journalctl --user -u pantheon-persona-consume@<persona>.service` shows `Finished` (or SuccessExitStatus 3/4/5), not `Failed`, across at least 2-3 consecutive fires.
- [ ] Timer armed with a finite next-elapse: `systemctl --user list-timers pantheon-persona-consume@<persona>.timer`.
- [ ] The launched `claude` carries the correct channel-less argv: `pgrep -u <account> -a -f "claude --"` shows `claude --model claude-opus-4-8 --permission-mode default` (NO `--channels`).
- [ ] `runtime.json` present under `~/.local/state/pantheon/...` (so the mismatch branch will not fire).
- [ ] Mailbox round-trip: the seat wakes on a real inbound and (for a seat whose job is to report upward) mailboxes its lead. In T-2677, metis and atlas each self-woke, loaded their brief, and mailboxed orchestrator — the definitive end-to-end proof.
- [ ] `systemctl --user reset-failed pantheon-persona-consume@<persona>.service` if any transient failure was logged during setup.

If the first fires are not clean, roll back (below), diagnose, re-attempt only after the root cause is understood — same disarm discipline as the sibling SOPs.

---

## Rollback

Per seat, fully reverts to the seat's prior (dormant) state:

```bash
systemctl --user disable --now pantheon-persona-consume@<persona>.timer
systemctl --user reset-failed pantheon-persona-consume@<persona>.service   # if it logged a failure
```

Optionally restore the pre-symlink brief (`~/CLAUDE.md.pre-symlink-<date>`) if reverting the version-control step too. The manifest stanza edit is a plain git revert.

---

## Boundary

Covers: bringing a from-scratch, mailbox-first (channel-less) claude persona onto `pantheon-persona-consume@<persona>` — the manifest stanza, brief-tracking, operator install, the kill-stale-pane gotcha, verification, and rollback.

Out of scope: personas with a bespoke telegram supervisor (`migrating-an-existing-persona-onto-the-control-plane.md`); non-claude harnesses (`onboarding-a-new-harness.md`); the full ORG-020 self-restart stack (`adding-restart-capability-to-a-new-agent.md`); ordinary recovery of an already-onboarded seat (`../remediation/`). Generalizing the step-3 operator steps into a one-command persona-parameterized op is tracked as a follow-up (T-2679).

---

## Cross-references

- `sop/standard/migrating-an-existing-persona-onto-the-control-plane.md` — the sibling for a persona WITH a bespoke telegram supervisor.
- `sop/standard/onboarding-a-new-harness.md` — the sibling for a new non-claude harness.
- `tools/systemd/pantheon-persona-consume@.service` / `@.timer` — the unit templates.
- `tools/persona_bringup.py` (`consume`) + `tools/runtime_controller.py` (`ensure_runtime`) — the consume runtime.
- `tools/harness_adapter/claude.py` `build_interactive_launch` — where `--channels` is conditionally added (the channel-less fact).
- `adr/ORG-031-harness-abstraction-and-daemon-onboarding.md` / `adr/ORG-050-security-and-identity-contract.md` — the control-plane ADRs.
- T-2677 (the metis/atlas execution this SOP was written from), T-2678 (the kill-stale-pane failure), T-2679 (generalize step 3 + this SOP).
