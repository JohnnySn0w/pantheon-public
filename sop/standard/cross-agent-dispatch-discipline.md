# SOP: Cross-Agent Dispatch Discipline (HO tmux send-keys)

**Status:** active  
**Ratified:** T-0533 (2026-05-22)  
**Root-cause ticket:** T-0284 (9-day mystery — text in pane without Enter)

---

## The rule

**HO's ad-hoc cross-agent dispatch (sending a directive into another agent's TUI
pane) MUST use `tools/send_keys_submit.sh`, not raw `tmux send-keys`.**

```bash
# CORRECT — submit-verify form:
bash /home/claude/pantheon-config/tools/send_keys_submit.sh specialist:0.0 "run T-0320 now"

# WRONG — raw send-keys, no submit-verify:
tmux send-keys -t specialist:0.0 "run T-0320 now" Enter
```

---

## Why

Two back-to-back `tmux send-keys` calls (text then Enter, issued consecutively)
get coalesced by the Claude Code TUI paste-mode debounce into a single paste
event. The trailing Enter folds into the paste as a literal newline instead of
submitting — the directive sits unsubmitted in the input buffer until a human
presses Enter.

This is the T-0284 root cause (diagnosed 2026-05-22 via Recovery):
- All automated senders already had the submit-verify discipline:
  `activity_monitor.py:send_poke()`, `inbox_consumer.py:inject()`,
  `handoff_and_restart.sh:send_keys_submit()`.
- The unguarded path was HO's ad-hoc cross-agent dispatch (typing
  `tmux send-keys` directly in a Bash tool call or via a quick shell one-liner).

---

## How `tools/send_keys_submit.sh` works

1. Sends the literal text (`tmux send-keys -t <pane> -l <text>`).
2. Waits `INJECT_ENTER_DELAY` seconds (default 0.7s) for paste-mode to settle.
3. Sends Enter (`tmux send-keys -t <pane> Enter`).
4. Captures the pane and checks for the pending-paste sentinel (`[Pasted text`).
5. If sentinel is still present, retries steps 2–4 up to `SUBMIT_RETRIES` times
   (default 2).
6. Returns exit 0 on success, exit 1 on send-keys failure or unconfirmed submit
   after all retries.

The submit-VERIFY form is stronger than a blind delay (which only reduces the
race under load). This is P1: structural enforcement rather than "remember the
delay."

---

## Pane spec convention

Pane specs follow `tmux` target syntax. For cross-account dispatch (HO → Specialist
or HO → Recovery), include the session owner prefix if needed for cross-user tmux
access (e.g. via sudo, as used in `activity_monitor.py`). For same-account
dispatch the plain `<session>:<window>.<pane>` form works directly.

Consult `tools/agent_role_map.py` for the canonical pane-name → session mapping.

---

## Automated senders (already compliant)

These senders already use the submit-verify pattern (their own Python inline
forms, pre-dating this helper). They are listed here for audit completeness:

| Sender | Path | Form |
|---|---|---|
| `inbox_consumer.py` | `tools/inbox_consumer.py:inject()` | Python inline with 0.7s delay + sentinel check |
| `inbox_consumer.py` | `tools/inbox_consumer.py:_send_to_recovery_pane()` | Python inline with 0.7s delay + sentinel check |
| `activity_monitor.py` | `tools/activity_monitor.py:send_poke()` | Python inline with 0.7s delay (no retry) |
| `handoff_and_restart.sh` | calls `send_keys_submit()` sourced from `send_keys_submit.sh` | submit-verify (T-0533 DRY refactor) |

Migrating the Python senders to call `send_keys_submit.sh` via subprocess is
tracked in a follow-up ticket (parent T-0533) — they are not the bug source and
the migration crosses a language boundary that warrants its own scope.

---

## Recovery carve-out INVARIANT (T-1826 design, T-1829 landed)

The producer-repoint program (send-keys → `mailbox.send()`, T-1826) migrates **work-turn** producers only. The following stay pane-based (`send_keys_submit.sh`) **permanently — this is a design invariant, not a rollout phase**: `ho_resumer` (post-cap-hit resume), `restart_drain` → `handoff_and_restart.sh` (restart choreography), `context_fill_monitor` / `activity_monitor` / Recovery `verify_startup` de-wedge pokes, and all cross-agent recovery injections per this SOP. Why: the recovery layer must not ride the mechanism it exists to recover — a wedged/dead session consumes no mailbox, and the bridge/waker being the wedged component is exactly the failure these tools break. Pane-level send-keys is the lowest common denominator that works on a sick session. Full rationale: `knowledge/episodic/2026-07-20-producer-repoint-mailbox-design/notes.md` §1.

**Usage-gate scope (T-4510/T-4889).** The fleet usage gate (`tools/fleet_usage_gate.py`) suppresses ONLY periodic work-turn pokes: keep_going's nudge and activity_monitor's P1.a/P1.b queue/idle content pokes. Its provider-specific weekly schedule is: 50% ceiling before the subscription window midpoint; linearly rising 50→95% ceiling from midpoint to Friday 18:00 America/Chicago; then a weekend hold until that provider resets. Claude is a shared pool; Codex pools are seat-specific and read from the app-server quota record. A trip mails Orchestrator and the affected seat: narrow engineering orchestration to one ticket or hold. Every recovery path above stays live at any usage level, including activity_monitor's cap-modal confirm, its cap/cyber/effectiveness escalations, the P1.d retry-inject and the TUI-wedge escalation. That is pinned by `tools/test_activity_monitor.py` (a real-`main()` ordering test) and a structural test that the carve-out modules never reference the gate.

## Test coverage

`tools/test_send_keys_submit.sh` — 5 cases:

1. Success on first try (no sentinel)
2. Success on retry (sentinel on first capture, clear on second)
3. Fail after retries (sentinel persists through all attempts)
4. send-keys text failure (exit 1 immediately)
5. send-keys Enter failure (exit 1 after text succeeds)

Run: `bash tools/test_send_keys_submit.sh`

---

## Justifies-via

- **P1** (architecture over policy) — the helper makes the correct path the
  easy path; raw `tmux send-keys` requires extra deliberate steps to get right
- **P3** (decisions justify upward) — T-0284 root-cause + fix rationale recorded
  in T-0533 + this SOP; not buried in a commit message
- **T-0533** — the ticket that ratified this discipline
