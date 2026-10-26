# The harness itself can block an unattended session — and no model-level logic will clear it

**Date:** 2026-08-06 · **Author:** Orchestrator (HO) · **Status:** Authored (`lessons/` is a
non-ratifiable class per ORG-015)
**Origin:** codex's (Consultant's) live session wedged mid-T-2382 on a harness *update* prompt and
needed the operator to manually resume it; same family as the T-2382 first-fire/readiness bug and
the T-2599 model-migration-modal gap. ORG-031 named the class at bringup time; this generalises
it to mid-run and records why a config fix is not enough.
**Re-evaluate when:** a wedge appears that is neither bringup-time nor a known-frame modal —
i.e. a harness interaction that the pane-predicate + keystroke-dismissal shape cannot catch.

## The rule

> **A persona session can be blocked by the *harness*, not the model — a login screen, a
> trust dialog, a NUX "press enter", an update picker, a model-migration modal. None of it is
> reachable by the model's own reasoning: an unattended session cannot think its way past a
> frame that is waiting on a keystroke the model does not control. Treat harness-interaction
> wedges as a first-class failure class with its own prevent / detect / recover machinery.**

The trap is that everything *below* the modal is healthy. The service is `active`, the process
tree is intact, the token budget is fine — and the session does nothing, because a dialog the
model never chose is holding the input. From inside the session there is often no signal at all.

## Why it needed writing

The class kept re-manifesting in different costumes, and each was handled as a one-off:

| Manifestation | When it fires | Ticket |
|---|---|---|
| Fresh/updated TUI doesn't consume injected input (paste-readiness race) | bringup / first-fire | T-2382, `bugs/telegram-inbound-injects-input-but-turn-doesnt-fire.md` |
| Login / device-code, hook-trust, trust-dir, sandbox/bubblewrap, NUX press-enter | bringup | ORG-031 §Decision (scripted-bypass onboarder step) |
| Harness **update** prompt fires *mid-run* → session wedges | any time after bringup | T-2553 (the Consultant incident) |
| **Model-migration** modal fires, unsuppressable by config in this binary | any time after bringup | T-2599 / T-2597 |

Seen apart, each looks like a distinct bug. Seen together they are one class with one shape:
*a harness-level frame is waiting on a keystroke, and the unattended session has no way to
supply it.* ORG-031 already carried the bringup half (a "scripted-bypass step" in the
onboarder). What was missing was the recognition that the same wedge fires **after** bringup —
an update or migration dialog mid-session — where no onboarder runs, and that mid-run case is
what actually cost the operator a manual resume.

## The three layers, and where each fails

- **PREVENT** — pre-launch, suppress/pre-accept the NUX so it cannot fire (config keys like
  `[notices] hide_*_migration_prompt`, pre-seeded `auth.json`, pre-accepted trust dirs). Best
  when it works, but **version-fragile**: T-2599 found that a pinned binary's live upgrade
  presets (gpt-5.4…5.6) had *no hideable config key at all*, so the migration modal was
  unsuppressable by config. A prevent layer keyed on the config is only as complete as the
  build's hide-keys, and a new build can add a modal with none.
- **DETECT** — a watcher predicate over the rendered pane: a non-idle frame that is *not* the
  normal prompt (a confirm-modal "Enter to confirm | Esc to cancel", an update notice, a
  migration heading + "Press enter to continue"). Reuse the `recovery_boot_kick` idle/modal
  predicate shapes. **Elapsed time is not a detection signal** — a wedged TUI increments its
  own clock (see the 2026-07-28 lesson); detection must key on the *frame content*, not on
  "it's been quiet a while."
- **RECOVER** — fail-safe: either a scripted keystroke-dismissal of the known-safe frame
  (`tools/codex_modal.py` already does this for the update picker; T-2599 extends it to the
  migration modal) or a handoff-and-restart past the wedge. The keystroke-dismissal keys off
  the *rendered needle*, not the config — which is exactly why it survives the version-fragility
  that breaks the prevent layer.

## Is a structural guard warranted?

**Yes — and specifically at the pane layer, not the config layer.** The load-bearing insight
is that config-suppression and pane-dismissal fail *independently*: config-suppression breaks
when a build ships a modal with no hide-key; pane-dismissal breaks only if the frame is
unrecognised. For a *known, deterministic, known-safe* prompt (update picker, migration modal),
a rendered-frame detect+dismiss is the robust guard because it does not depend on the binary
exposing a knob. Prevent-by-config is the cheap first line where a key exists; the pane guard
is the backstop that holds when it doesn't. The general live-session detector for the whole
class is T-2553 (proposed, hosted naturally by T-2039 codex-as-fleet-watcher); the per-modal
dismissers (codex_modal.py) are the concrete instances that exist today.

## The self-check

When an unattended session "does nothing" and everything below it is healthy:

> "Is a harness frame holding the input?" — look at the *pane*, not the logs, not the uptime.

If yes, it is this class: it will not clear itself, config may not be able to suppress it, and
the durable fix is a rendered-frame detect+dismiss, not a longer wait or a smarter prompt.

## Related

- ORG-031 §Decision — the wedge class + scripted-bypass onboarder step (bringup half).
- T-2553 — live-session detect+recover for mid-run wedges (the general guard).
- T-2599 / T-2597 — `tools/codex_modal.py` keystroke-dismissal; the config-unsuppressable
  migration modal that proves the version-fragility of the prevent layer.
- T-2382 — the first-fire/readiness bug; same family, distinct manifestation.
- `lessons/2026-07-28-name-the-other-hypothesis-before-asserting-a-state.md` — "a service is
  `active` vs. a session blocked on a modal behind it" is one of its named same-observation
  traps; this lesson is that trap's dedicated write-up.
- `bugs/telegram-inbound-injects-input-but-turn-doesnt-fire.md` — the paste-readiness instance.
