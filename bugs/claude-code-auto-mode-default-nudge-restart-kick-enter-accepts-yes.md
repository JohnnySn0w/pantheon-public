---
symptom: "After a restart, a seat comes up in auto permission mode and its user settings now say permissions.defaultMode=auto. Nobody chose that: the restart kick's Enter landed on Claude Code's 'Make auto mode your default permission mode?' startup prompt, whose highlighted option is 'Yes'."
fix: "Tooling (T-4646): the restart/kick path now refuses to paste or press Enter into that prompt or any pane without a composer footer (send_keys_submit exit 3; restart step 7 exit 7). Config: don't pin permissions.defaultMode in a seat's USER settings.json. If a pin is needed, set it in a non-user layer, or make sure ~/.claude.json has hasSeenAutoDefaultNudge=true."
domain: "pantheon, claude-code, restart, permission-mode, boot-dialog"
---

## Symptom

Claude Code 2.1.289 can boot into this prompt instead of the composer (specialist, 2026-10-05 08:49Z, blank lines dropped):

```
────────────────────────────────────────────────────────────────────────────────
 Make auto mode your default permission mode?
 Auto mode lets Claude handle permission prompts automatically. Claude checks each
 tool call for risky actions and prompt injection before executing, runs the ones it
 assesses as lower-risk, and blocks the rest.
 ❯ Yes, set auto mode as my default permission mode
   No, keep manual
```

It has no `Enter to confirm · Esc to cancel` footer. It has the `─` run and a `❯` option cursor, so the restart step-7 conjunction (frame + `❯` + no footer) read it as an idle prompt. The kick's `send_keys_submit` render wait then timed out (the paste never showed in a composer) and it "pressed Enter anyway" at 08:50:06Z, which chose **Yes**. Specialist's live session switched to auto mode and its user `settings.json` got `defaultMode: "auto"`.

## Cause

From the 2.1.289 binary's gate function: the prompt shows only when **all** of these hold:
- `~/.claude.json` lacks `hasSeenAutoDefaultNudge`;
- the **user** settings layer pins `permissions.defaultMode` to something other than `auto`;
- no project, local, flag (`--settings`) or policy layer sets `defaultMode`.

Choosing "No" sets `hasSeenAutoDefaultNudge`; Esc ("cancelled") does not, so the prompt comes back on the next boot. Only specialist qualified, through its T-4607 user-settings pin. The other seats have no pin, so they were never shown it.

## Fix

Tooling (T-4646, all in `tools/`):
- `submit_verify.AUTO_MODE_NUDGE_MARKER` is in `_READY_DIALOG_PATTERN`. New predicates: `paste_refusal` (positive dialog evidence before the paste) and `enter_anyway_refusal` (after the render wait, a dialog, or a claude-code pane with no composer footer in its bottom rows).
- `send_keys_submit.sh` (render-gated callers only) exits **3** instead of pasting or pressing the fallback Enter. `kick_verify` treats exit 3 as terminal (`KickRefused`): no re-fire.
- The same marker is in `DIALOG_EXCLUDE` (`handoff_and_restart.sh`, `verify_startup.sh`, `recovery_boot_kick.sh`) and in `inbox_consumer._WEDGE_DIALOG_FOOTERS` (`pane_idle_state`, the T-4591 Recovery-pane gate). A restart onto the prompt now exits 7 and the boot kick does not fire. Nothing auto-dismisses it: answer it by hand (pick **No**, not Esc).

Config:
- Don't put `permissions.defaultMode` in a seat's user `~/.claude/settings.json`. That pin is what arms the prompt.
- If a seat needs a pinned mode, set it in a non-user layer. For controller-launched seats, that is the `--settings` file the controller writes on every start (`runtime_controller._statusline_launch_args`). The adapter generates that file, so the pin belongs in the adapter's settings dict, not in a hand edit.
- Or answer **No** once (or set `hasSeenAutoDefaultNudge: true` in `~/.claude.json`), which stops the prompt for that account.

## Verification

Per-seat exposure (exposed iff the first value is not `True` and the second is set and not `auto`):

```bash
sudo -u <account> python3 -c 'import json,os;h=os.path.expanduser("~");c=json.load(open(h+"/.claude.json"));s=json.load(open(h+"/.claude/settings.json"));print(c.get("hasSeenAutoDefaultNudge"),s.get("permissions",{}).get("defaultMode"))'
```

Tooling: `bash tools/test_handoff_and_restart.sh` case `auto_mode_nudge` (exit 7, no dismiss) and `bash tools/test_send_keys_submit.sh` cases `render_gate_refuses_paste_into_a_dialog` / `render_gate_timeout_without_composer_refuses_enter` (exit 3, no Enter). On the pre-T-4646 tree these give exit 0 and exit 0 respectively (the restart "succeeds" and the Enter is pressed).

## Discovered
- T-4644 (specialist, the incident), T-4646 (the fix), 2026-10-05. Evidence: specialist `~/.local/log/pantheon/pane-timed-specialist.log` (08:49:53Z) and specialist's user journal.

## Keywords
auto mode, auto-mode nudge, Make auto mode your default permission mode, Yes set auto mode as my default permission mode, hasSeenAutoDefaultNudge, defaultMode, permissions.defaultMode, permission mode flipped, restart kick, Enter anyway, send_keys_submit exit 3, KickRefused, boot dialog, startup prompt, steady-state false pass, DIALOG_EXCLUDE, Claude Code 2.1.289
