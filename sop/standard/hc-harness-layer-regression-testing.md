# HC + harness-layer regression testing

**Status:** HO-drafted 2026-08-22 by orchestrator (T-3024) per the operator TG msg 8151 ("we'll want to formalize this series of tests in an sop as regression testing in the HC+the harness layers"). Captures the test series proven on specialist during the 2026-08-21 arc (T-2953/T-2984 series). Not ratify-gated — SOPs are HO-drafted per convention; steer edits welcome.

This is the runbook for regression-testing the harness-controller (HC) + harness-layer mechanisms after: (a) a persona cutover from bespoke supervisor to HC, (b) a controller-code change, (c) a fleet-wide upgrade touching either layer. Runs against a **single seat at a time**.

## When to use this

- **After any persona cutover** to `pantheon-runtime-controller@<persona>.service` (per T-2953 arc procedure).
- **After any controller-code change** landing in `tools/runtime_controller.py` or `tools/persona_bringup.py`.
- **After any harness-layer change** touching `tools/persona_installer.py`, `tools/notify_policy.py`, or the systemd unit templates under `tools/systemd/pantheon-runtime-controller*` + `tools/systemd/pantheon-persona-consume*`.
- **On any fleet-wide upgrade** that touches acp-bridge / claude-code-acp / mailbox delivery paths.

Do NOT use for: routine session restarts (just fire the wrapper), Max-window cap-hit auto-resume (separate mechanism, `tools/ho_resumer.py`), or bespoke-supervisor regressions (that's the pre-HC path — separate `handoff-restart-runbook.md`).

## Preflight (mandatory before ANY class)

1. **Confirm target seat is HC-live**, not bespoke: `systemctl --user is-active pantheon-runtime-controller@<persona>.service` must return `active`. If bespoke still: this SOP does not apply — use bespoke-path runbooks.
2. **Arm T-2984 seat-maintenance-mode** — suppresses false-fires from context_fill / channel_liveness / any other seat-scoped monitor during the test window:
   ```
   python3 /home/<user>/pantheon-config/tools/notify_policy.py enter-maintenance <persona> --minutes 30
   ```
   Extend if a class takes longer. Exit maintenance at the end of each session: `notify_policy.py exit-maintenance <persona>`.
3. **Baseline snapshot** — capture pre-test state so post-test comparisons are grounded:
   ```
   systemctl --user list-timers --all > /tmp/pretest-timers-<persona>-$(date -u +%s).txt
   systemctl --user status pantheon-runtime-controller@<persona>.service > /tmp/pretest-controller-<persona>-$(date -u +%s).txt
   ```

## Class A — HC-mechanism lifecycle proofs

**Purpose:** prove the controller's own state-machine (start / restart / respawn / bounded-backoff) still works.

### A1. Controller restart cycle with maintenance-mode armed

**Command:**
```
systemctl --user restart pantheon-runtime-controller@<persona>.service
```

**Expected signals:**
- New PID for `pantheon-runtime-controller@<persona>.service` within 3s (`systemctl --user status`).
- Post-restart monitor cycle observed clean in journalctl (no false-fires from context_fill / channel_liveness — T-2984 suppresses them).
- Interactive persistent pane (claude session in tmux) stays alive across the restart (`tmux capture-pane` still shows the session prompt).

**Rollback:** none needed (restart is idempotent). If pane died: manual `tmux new-session` + investigate.

### A2. Mid-life restart-fixable respawn (ORG-068 §88)

**Command:**
```
# Find + kill the claude PID under this seat's tmux session
pgrep -u <user> -f "/usr/bin/claude|claude-code" | head -1 | xargs -r kill -KILL
```

**Expected signals:**
- Controller detects claude-exit within 10s (`journalctl --user -u pantheon-runtime-controller@<persona>` shows `runtime exited`).
- Controller exits per ORG-068 §88 stateless-invariant.
- systemd `Restart=always` fires, new controller PID within 5s of the exit.
- New controller re-selects runtime (interactive-subscription / interactive-chatgpt / etc. per manifest priority) + respawns claude in the tmux pane.
- Pane recovers cleanly, prompts visible via `tmux capture-pane`.

**Rollback:** if respawn stalls > 60s or wrong runtime selected: `systemctl --user restart pantheon-runtime-controller@<persona>` to force a clean re-selection.

### A3. Both-exhausted bounded backoff

**Command:**
```
# Hold BOTH runtime slots (adjust manifest paths per persona)
python3 tools/harness_control.py hold --persona <persona> --runtime interactive-subscription
python3 tools/harness_control.py hold --persona <persona> --runtime interactive-chatgpt
# Then kill claude to force controller re-selection
pgrep -u <user> -f "/usr/bin/claude|claude-code" | head -1 | xargs -r kill -KILL
```

**Expected signals:**
- Controller detects no-viable-runtime.
- Controller exits `1` / `FAILURE` (not `Restart=always` retry — this is a hard-fail).
- `OnFailure=pantheon-unit-failure-alert@%n.service` fires.
- Recovery's mailbox receives an urgent alert (check `/home/the-ancestor/mailbox/recovery/new/`).

**Rollback (mandatory):**
```
python3 tools/harness_control.py clear-hold --persona <persona> --runtime interactive-subscription
python3 tools/harness_control.py clear-hold --persona <persona> --runtime interactive-chatgpt
python3 tools/harness_control.py clear-cooldown --persona <persona>
systemctl --user restart pantheon-runtime-controller@<persona>.service
```

### A4. Subscription-cap classifier — opportunistic

**Trigger:** natural Max-window cap event (event-gated per T-2985, not artificially triggerable without cap spend). On next cap-hit, verify controller correctly classifies + routes to auto-resume path.

## Class B — Periodic-coverage audit

**Purpose:** prove every periodic (weekly / monthly / cron-scheduled) that ran under bespoke still fires under HC. **Governed by the verification-window discipline** (see [`feedback_verification_window_must_span_mechanism_lifetime`](../../../memory/) memory pin): a "last-fired within period" observation only proves the periodic works IF the substrate has been in place ≥ that period. Below the substrate age threshold, absence of last-fire proves nothing.

### B1. Timer inventory sweep

**Command:**
```
sudo -u <user> systemctl --user list-timers --all
```

**Expected signals:** every timer's `LAST` timestamp within its period AND within the HC-substrate-lifetime window. Timers whose period exceeds the substrate age get a follow-up ticket (see B2).

### B2. Weekly + monthly follow-up verifiers

**File one ticket per periodic** whose period exceeds current HC-substrate age, with `not_before` at the next expected fire:

- Template: T-3022 (weekly periodics fired under HC, post-2026-08-24)
- Template: T-3023 (monthly soak-filer fired under HC, post-2026-09-01)

These fire post-cadence + verify last-fire timestamps are BOTH within period AND within the HC-substrate-managed window. Target the seat's owner persona (usually orchestrator for HO-visible verify; recovery for recovery-side observation).

## Class C — Feature-parity synthetic probes

**Purpose:** prove the mailbox-intake + interactive-pane pathways still deliver + respond after cutover.

### C1. Mailbox-intake path probe (mailbox-mode seats only)

**Applies to:** seats in `DELIVERY_MODE 'mailbox'` per T-1827 (currently: specialist, the-ancestor). Skips for pane-only seats.

**Command:**
```
python3 tools/ticket.py file --requester orchestrator --target <persona> \
  --title "SYNTHETIC probe: mailbox-intake regression check" \
  --labels 'synthetic,probe,transient' \
  --ask "Echo this ticket-id + your latest jsonl mtime + mailbox delivery mode. Resolve immediately."
```

**Expected signals within 10 min:**
- `inbox_consumer` picks it up (log: `[inbox_consumer] <persona>: delivered ...`)
- Seat processes + responds via mailbox
- Seat resolves the ticket

**Exercises:** mailbox → acp-bridge → claude-code-acp → fresh claude spawn.

### C2. HC-persistent-pane direct probe

**Command:**
```
bash /home/<user>/pantheon-config/tools/send_keys_submit.sh "<persona-tmux-target>" \
  "SYNTHETIC probe: reply with (a) MCP-server count from ~/.claude.json, (b) skills count from ~/.claude/skills/, (c) your session JSONL path, (d) full MCP-server-list. Reply via mailbox to orchestrator."
```

**Expected signals within 10 min:**
- Claude session responds via mailbox to `/home/the-ancestor/mailbox/orchestrator/`
- Reply includes MCP-server count + skills-count + session JSONL path + MCP-server-list

**Exercises:** interactive persistent pane. HC-driven seats keep the user-configured MCP surface (unless `~/.claude.json` is empty — surface as config-side gap per Class D).

## Class D — MCP-surface audit

**Purpose:** validate MCP-server + skills provisioning per-persona. Feeds ORG-079 L1-install-completeness umbrella (T-3030; ORG-079 landed as rev-2 at ac507d611b — status: Autonomous draft awaiting the operator ratification per T-3091).

**Command:** implicit — C1 + C2 responses ARE the audit signal. Missing user-configured MCP servers surface as config-side gaps (fix via `~/.claude.json` edit, orthogonal to HC).

## Report artifact

At end of test session, capture:
- All 4 preflight + baseline artifacts
- Per-class pass/fail with signal-evidence quotes
- Any class marked FAIL → file a bug + tag `hc-regression`

**Recommended annotation format:** annotate the arc-driving ticket (typically T-2953 or a cutover ticket like T-3026) with a `[HC-regression-test session <UTC>]` entry naming which classes ran + verdict.

## Integration with the ticket-file discipline

For classes whose verification-window exceeds current session (Class B weekly/monthly, Class A4 opportunistic):
- File a `not_before`-gated ticket at expected trigger
- Target = the persona that will observe (usually the seat's own persona or orchestrator for HO-visible)
- On fire: run the specific class-probe + annotate the arc ticket

## Related

- **Arc source:** T-2953 (HC cutover arc — the source of this test series)
- **Preflight mechanism:** T-2984 (seat-maintenance-mode)
- **Verifier templates:** T-3022 (weekly), T-3023 (monthly)
- **Verification-window rule:** memory pin `feedback_verification_window_must_span_mechanism_lifetime`
- **Feeds:** T-3030 (L1 install-completeness umbrella)
- **Controller design:** ORG-068 §85-88 (stateless controller + mid-life restart-fixable)
- **Manifest / bringup:** `sop/standard/adding-restart-capability-to-a-new-agent.md` (bespoke-path; HC-path variant TBD)
