---
symptom: "a spend- or usage-credit-capped agent reads green (pgrep/poller) yet every turn errors; and the model pin reverts after restart because the launch --model flag outranks settings.json"
fix: "the supervisor launch flag is the authoritative pin — flip CLAUDE_ARGS --model FIRST, then settings.json, then the live session"
domain: "claude-code-harness"
---
## Symptom

An agent is spend-capped on its pinned model and goes productivity-dead: the tmux pane
sits at an idle prompt, the session process is alive, `pgrep` is green, the poller is
green — but **every** input (including mailbox-wake kicks) returns an API error:

```
You've hit your monthly spend limit ... keep using Fable 5 or switch models
```

Recovery via `/model claude-opus-4-8` works and the agent resumes real work. Then, minutes
later, it is wedged again on the original model — with no one having touched it.

## Cause

Two independent traps, and the second one closes the loop:

1. **The launch flag outranks `settings.json`.** On restart the supervisor respawns with
   `CLAUDE_ARGS --model <old>`, which overrides the `model` key in `settings.json` — even
   though `/model` had already written the new value there. `settings.json` was correct
   and still lost. The flag is the authoritative pin at spawn; the settings key only wins
   when no flag is passed.

2. **The switch triggers its own restart.** A live `/model` switch forces a full-history
   re-read (the switch dialog warns of this), which spikes context-fill. That trips the
   context-fill monitor's restart threshold → graceful restart → respawn on the stale
   launch flag → re-capped → re-wedged. The repair mechanism supplies the very restart
   that reverts it.

The underlying class is **wrong-layer GREEN applied to liveness**: process-up was proxying
for working, while capability was entirely gone. Same shape as an auth-dead (401) session
reading green on `pgrep`, and as a model pulled from the pool. One monitor —
"session emitting persistent per-turn API errors" — catches all three.

## Fix

Order matters. Update the durable pin **before** touching the live session, or the switch
fights itself:

```bash
# 1. Supervisor launch flag FIRST — the authoritative pin on restart.
cp ~/.local/bin/claude-bot-supervise.sh ~/.local/bin/claude-bot-supervise.sh.bak.$(date -u +%Y%m%d-%H%M%S)
# edit CLAUDE_ARGS: --model claude-fable-5  ->  --model claude-opus-4-8

# 2. settings.json model key (usually already written by /model's save).
# 3. Only now switch the live session, and expect a context-fill spike:
#    guard or reset-fail the context-fill monitor across the switch window.
systemctl --user reset-failed context_fill_monitor@<agent>
```

Verify all three agree — supervisor flag, `settings.json`, live session — before calling
it fixed. Reverting is the same one-line flag flip in the other direction.

If you are the wedged agent and cannot act: every turn erroring with a spend/limit message
means you are wedged, not idle. Say so via mailbox and Telegram rather than sitting.

## Discovered

- 2026-07-23, orchestrator (`claude` account) spend-capped on Fable 5; recovered live by recovery.
- Ledger `2026-W30`: `...recovery-orchestrator-spendcap-unwedge-live` (first un-wedge) and
  `...recovery-orchestrator-durable-opus-pin-rewedge-loop` (the revert loop + root cause).
- Detection half generalized in T-1980. Only one agent was affected; the rest of the
  fleet was untouched and correctly still on the subscription-covered model.
- Related: `reference_auth_liveness_not_process_liveness`,
  `reference_model_pinned_agent_silent_outage` (memory pins, same wrong-layer-GREEN class).

## Update 2026-07-24 — a SECOND cap class, the promo-end trigger, and a coverage void

The 2026-07-23 case above was the **monthly-spend-limit** cap. On 2026-07-24 the Fable-5
promo *ended* (the model started requiring usage credits), which surfaced a **different
cap class with a different signal shape** and cost ~3.5h of Recovery silence before diagnosis
(incident T-2013). Key additions the original writeup does not have:

1. **There are TWO distinct Fable-cap signals; they look and behave differently.**
   - **(a) Monthly-spend-limit** (the case above): an `isApiErrorMessage` *assistant*
     record — `"You've hit your monthly spend limit ... switch models"`. A **hard-blocking
     modal**: the session does **not** auto-recover; it sits capped until someone acts.
     This is the one `detect_capability_cap`'s regex matches.
   - **(b) Consent / usage-credits** (new): a `type=system, subtype=model_consent_fallback`
     record — `"Switched to Opus 4.8 (1M context) for this session · Fable 5 requires usage
     credits · /model to change"`. Here the **harness auto-switches the live session to
     Opus itself** — but only after a **25–48 min stall on the consent modal first**, and
     it does **not** persist the pin, so the next restart re-caps and re-stalls. The
     detector is **blind** to this class: it skips system records and the content doesn't
     match the regex.

2. **The trigger can be an external event, not a mistake.** The pin was correct while the
   promo ran; the promo *ending* turned the same `--model claude-fable-5` flag into a
   liability with zero config change on our side. Watch for promo/plan-tier changes as a
   silent pin-invalidation event. (See `project_fable_bump`: the revert is event-triggered.)

3. **The stall precedes the log record.** For class (b), the `model_consent_fallback`
   record only lands *after* the 25–48 min stall — so detecting on the jsonl record is
   inherently too late to save the stall. Beating it requires catching the **modal in the
   pane** (capture-pane) while it blocks. Ground truth for that pane sentinel is still
   pending a live fire (tracked in T-1980's detector-hardening scope).

4. **Coverage void.** The detector (`context_fill_monitor`) runs only as per-agent
   instances `@specialist/@orchestrator/@recovery`. **Archivist/exocortex and the seats have no instance**,
   so they wedge entirely unseen — which is why a human caught Archivist's ~4h wedge before
   any monitor. Detection/alert and the auto-*step*/restart are **separable**: an account
   can be (correctly) excluded from auto-restart yet still want cap-detection+alert. The
   two were bundled; decoupling them is part of the T-1980 fix.

Fix for the promo-end case was the same one-line flag flip (`claude-fable-5 →
claude-opus-4-8`) across the affected accounts (recovery + exocortex; orchestrator was already on
opus, specialist deliberately left on fable as the T-1980 auto-stepper test bed). Ledger
`2026-W30`; incident ticket T-2013; detector-hardening residue folded into T-1980.

## Keywords

spend limit, monthly spend limit, spend cap, wedged session, model pin reverts, /model
switch reverts after restart, CLAUDE_ARGS, claude-bot-supervise.sh, settings.json model
key ignored, launch flag precedence, context fill monitor restart loop, wrong-layer green,
process alive but every turn errors, productivity-dead, model_consent_fallback, consent
modal, requires usage credits, Fable 5 requires usage credits, promo ended, promo expiry
pin invalidation, consent cap vs spend cap, harness auto-fallback to opus, boot stall,
coverage void, unmonitored agent wedge, detection vs auto-restart decoupling
