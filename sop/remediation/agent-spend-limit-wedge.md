# Agent monthly-spend-limit wedge

**Default-unblock amendment (the operator, Telegram 2068, 2026-10-12):** once the
printed reset has passed and the live pane is re-verified as this exact modal,
the Escape-only dismissal is default-authorized. It does not require a separate
per-seat or fleet "unblock" ask. Enter, billing changes, pre-reset dismissal,
and any unverified modal remain outside this authorization.

## Failure shape

An agent goes silent while its **process and inbound poller stay up** — it looks
idle, but its tmux pane shows a blocking modal:

```
You've hit your monthly spend limit · your weekly limit resets <DATE>, <TIME> (UTC)
```

This is the Claude Code **`/rate-limit-options` modal** ("Adjust monthly spend limit"
/ "Wait for limit to reset"). An unattended agent **cannot self-dismiss it**, so the
session wedges. First seen 2026-06-26 when the fleet's monthly limit was set to **$10**;
recurred fleet-wide 2026-09-27 when the weekly usage window capped **every seat at once**
(specialist, orchestrator, mason, artisan).

**KEY (T-2290, the operator 2026-07-29):** despite the "monthly spend limit" text, the binding
constraint is the **current usage window**, and the cap **self-clears at the printed reset**
— usually hours, occasionally up to ~2 days if the window is far (T-2387: artisan erred every
turn ~48h). It is a *long-but-finite* wait, **not permanent**, and **raising the limit is NOT
the required fix** — only relevant if the limit is genuinely too low (the 2026-06-26 $10 case).

Why nothing caught it before: unlike the auth-outage 401s, the spend-limit modal is
**not an API error in the session JSONL** — it is pane-only. Every JSONL-based detector
is structurally blind to it. Detection requires a **pane scrape**. (The related *idle-429*
variant — a clean prompt erroring per-turn, no modal — is caught by the JSONL
capability-cap detector and self-resumes when the window rolls; only the MODAL variant
blocks the pane and needs a dismiss.)

## Detection (live)

`context_fill_monitor._maybe_fire_spend_limit` (2026-06-26) — runs per-agent on the
5-min cycle, captures the pane (cross-account via sudo) and matches the
`monthly spend limit` sentinel. On a hit it alerts through `inbox_consumer.route_failure_alert`
(6h per-agent cooldown; ships ON; `PANTHEON_SPEND_LIMIT_ENABLED=0` to disable). Another
seat's modal goes to Recovery, who clears it by hand per step 3. Recovery's own modal goes to orchestrator's
mailbox (orchestrator can Esc Recovery's pane), never Recovery's pane. The operator is reached only if delivery
fails (T-4590, D1). It used to be a direct the operator FYI, but the cap self-clears, the clear is
Recovery's, and his one lever, a raise, is ruled out (T-2290). Related: `INTERACTIVE_MODAL_SENTINELS` now recognises this modal
so the wedge path does **not** auto-restart a capped seat (9d0eaf23f / T-4024) — a restart
just re-hits the window and its send-keys lands in the modal.

## Remediation

The cap self-clears at the printed reset; the un-wedge is Recovery's reversible recovery.

1. **Confirm it's the spend-limit modal + read the printed reset.** Pane shows the
   `monthly spend limit` sentinel (or `verify_startup.sh --agent <name>` reports a
   non-idle pane that is not a boot dialog). Note the printed `resets <DATE> <TIME>`.
   `df` is irrelevant — this is a usage window, not disk.
2. **WAIT for the printed reset, log it (T-2290).** The cap self-clears when the window
   rolls. Do **NOT** restart (re-hits the window; the monitor no longer wedge-loops it).
   Do **NOT** escalate a raise: the operator has ruled twice (T-2290) that no raise is needed.
   Raising the limit (claude.ai → Settings → Usage — the operator's account action) only
   **shortens** a long wait and is reserved for a genuinely-too-low limit.
3. **Un-wedge by default once the window has reset.** The modal does **not** auto-dismiss even
   after the window rolls. Once the printed reset has passed and the pane is re-verified as
   this exact modal, send **Escape** (cancel) to the wedged seat's pane as the agent's user.
   This post-reset Escape-only recovery is default-authorized; it does not need a separate
   per-seat or fleet approval. **NEVER send Enter or → while the modal is up** — those select
   "Adjust monthly spend limit" (the money option). Escape alone returns the seat to a clean
   idle prompt:
   ```bash
   sudo -n -u <agent-unix-user> bash -c \
     'XDG_RUNTIME_DIR=/run/user/$(id -u) tmux send-keys -t <pane> Escape'
   ```
   Re-capture the pane to confirm the modal cleared to a normal prompt, then leave the seat
   for its own kicker/inbound to resume (a manual resume-poke is optional and, in manual
   permission-mode, only queues). Confirm health per `sop/standard/confirm-health-after-touching-a-live-session.md`.
   (Panes: orchestrator `claude-telegram-bot:0`, specialist `claude-research-bot:0`, mason `suborch1-main:0`,
   artisan `suborch2-main:0`, recovery `claude-telegram-bot-fallback:0` — never self-inject to recovery's
   own pane, T-0559.)
4. **Prevent recurrence:** `tools/cap_modal_reaper.py` (`pantheon-cap-modal-reaper.timer`,
   on Recovery, T-4024) is installed but runs **OBSERVE-only**: it reads state and re-verifies the
   pane live, but sends no keystroke until it runs with `--arm`. Arming it is a supervised
   first-fire at a real cap window (T-4064), because it sends keys into live panes. Until
   then, step 3 is done by hand (Recovery cleared orchestrator and artisan this way on 2026-10-04). For the
   genuinely-too-low-limit case, keep the monthly limit above fleet throughput.

## Autonomous-vs-bubble-up D3 placement

- **Default-authorized reversible recovery:** any orchestrator or recovery steward with existing
  cross-account authority may diagnose, **wait for the printed reset**, re-verify the exact modal,
  and perform the post-reset Escape dismissal without a separate ask. Recovery remains the fleet
  recovery steward; this default also lets the orchestrator who discovers the rolled modal clear
  it instead of manufacturing an approval wait. This is NOT operator-must-act for the usage-window
  cap — the cap self-clears and Escape only removes its stale modal.
- **Above the bar (the operator's, and OPTIONAL):** raising the monthly spend limit (his
  billing/account) — only to shorten a long wait or fix a genuinely-too-low limit, never
  the required remediation. Enter, billing changes, pre-reset dismissal, and action on an
  unverified modal remain outside the default authorization.

## Cross-references

- `tools/context_fill_monitor.py` (`_maybe_fire_spend_limit`, `_capture_pane_text`, `INTERACTIVE_MODAL_SENTINELS`)
- T-2290 (window-cap self-clears / no-raise ruling), T-4024 (wedge-restart suppression + reaper), T-4037 (shared mid-session-cap detector)
- `sop/standard/confirm-health-after-touching-a-live-session.md`
