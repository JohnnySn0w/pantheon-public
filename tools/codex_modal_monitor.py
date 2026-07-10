"""tools/codex_modal_monitor.py — detect codex wedged on a rate-limit model-switch
modal and ALERT Recovery (T-1366).

codex's rate-limit MODEL-SWITCH modal blocks the session:

    Approaching rate limits
    Switch to gpt-5.4-mini for lower credit usage?
    › 1. Switch to gpt-5.4-mini …
      2. Keep current model
      3. Keep current model (never show again) …
    Press enter to confirm or esc to go back

An unattended codex cannot self-dismiss it, so it sits parked behind the dialog —
and it is NOT in any jsonl (it's codex TUI state), so only a PANE scrape sees it.
The Claude fleet's spend-limit scrape (context_fill_monitor) does NOT cover codex:
codex is a separate substrate (its own tmux session + sentinels), so this is its
own Recovery-run, cross-account monitor. Found manually TWICE on 2026-06-28 (the T-1365
deploy + the weekly-reset check) — this closes that "Recovery eyeballs it" gap.

ROUTING (feedback_failure_alert_routing_recovery_vs_self): codex is NOT recovery, so the
alert routes to RECOVERY via route_failure_alert (not direct-to-operator). Recovery then
dismisses with **Esc** — keeping the current model; **Enter would SWITCH the model**,
a goal-level the operator call (reference_spend_limit_modal_wedge, codex variant). The
monitor itself NEVER sends keys into codex: detection + alert only. The goal-level
part (raise the weekly limit / switch model) Recovery escalates to the operator if needed.

Runs on Recovery's account; captures codex-session via `sudo -u codex`. Alert-only,
cooldown-gated, fail-open (a scrape/route error never wedges the monitor).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CODEX_PANE = os.environ.get("CODEX_PANE", "codex-session")
CODEX_USER = os.environ.get("CODEX_USER", "codex")
COOLDOWN_SECONDS = int(os.environ.get("CODEX_MODAL_COOLDOWN_SECONDS", str(3 * 60 * 60)))
_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
_BOTTOM_REGION_LINES = 14  # the live modal sits at the bottom; ignore old scrollback

# The modal's title is the strong, specific signal. Require it PLUS a modal-structure
# marker so a passing mention in scrollback can't false-fire. Model name (gpt-5.4-mini)
# is deliberately NOT keyed on — it can change; the title + structure are stable.
_MODAL_TITLE = "approaching rate limits"
_MODAL_STRUCTURE = ("keep current model", "esc to go back")


def classify_codex_pane(pane_text: str) -> str | None:
    """Pure: 'model_switch_modal' iff codex is wedged on the rate-limit model-switch
    dialog (title + a modal-structure marker, in the live bottom region), else None."""
    if not pane_text:
        return None
    non_blank = [ln for ln in pane_text.splitlines() if ln.strip()]
    if not non_blank:
        return None
    bottom = "\n".join(non_blank[-_BOTTOM_REGION_LINES:]).lower()
    if _MODAL_TITLE in bottom and any(m in bottom for m in _MODAL_STRUCTURE):
        return "model_switch_modal"
    return None


def capture_codex_pane(pane: str = CODEX_PANE, user: str = CODEX_USER) -> str | None:
    """Capture codex's pane. Cross-account (monitor runs as Recovery) → sudo -nu codex;
    same-account path kept for tests/parity. None on any failure (caller fails open)."""
    inner = f"tmux capture-pane -p -t {pane}"
    if user == getpass.getuser():
        argv = ["bash", "-lc", inner]
    else:
        argv = ["sudo", "-n", "-u", user, "bash", "-lc", inner]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[codex_modal_monitor] capture error: {exc!r}", file=sys.stderr)
        return None
    return r.stdout if r.returncode == 0 else None


def _state_path() -> Path:
    d = os.environ.get("PANTHEON_STATE_DIR")
    base = Path(d) if d else _DEFAULT_STATE_DIR
    return base / "codex_modal_monitor.json"


def _in_cooldown(state_path: Path, cooldown: int, now: int) -> bool:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return (now - int(data.get("ts", 0))) < cooldown
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return False


def _stamp(state_path: Path, now: int) -> None:
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"ts": now, "reason": "codex-modal"}), encoding="utf-8")
    except OSError as exc:
        print(f"[codex_modal_monitor] state write failed: {exc!r}", file=sys.stderr)


_ALERT_MSG = (
    "🔌 [codex-modal] codex is WEDGED on the rate-limit MODEL-SWITCH modal "
    "(\"Approaching rate limits — Switch to gpt-5.4-mini?\") — a blocking dialog it "
    "can't self-dismiss, so it sits parked behind it. DISMISS WITH **Esc** (keeps the "
    "current model; Enter would SWITCH the model = the operator's goal-level call). Then the "
    "weekly-limit / model-switch decision is the operator's — escalate only if needed. "
    "(T-1366; codex variant of the spend-limit wedge.)"
)


def run(*, do_capture=capture_codex_pane, now: int | None = None,
        cooldown: int = COOLDOWN_SECONDS, do_alert=None) -> int:
    """Capture codex's pane; on the model-switch modal, alert Recovery (cooldown-gated).
    Returns 0 always (alert-only; a missed alert is harmless, a wedged monitor isn't)."""
    now = int(time.time()) if now is None else now
    pane = do_capture()
    if classify_codex_pane(pane or "") != "model_switch_modal":
        return 0
    sp = _state_path()
    if _in_cooldown(sp, cooldown, now):
        print("[codex_modal_monitor] modal present but in cooldown — skip")
        return 0
    if do_alert is None:
        do_alert = _route_alert
    try:
        do_alert(_ALERT_MSG)
        print("[codex_modal_monitor] codex model-switch modal detected — alerted Recovery")
    except Exception as exc:  # alert routing must never wedge the monitor
        print(f"[codex_modal_monitor] alert routing failed {exc!r}", file=sys.stderr)
    _stamp(sp, now)  # stamp on detection regardless of delivery (don't spam on flaky send)
    return 0


def _route_alert(msg: str) -> None:
    """Route the codex failure to Recovery (others→Recovery per the failure-alert routing)."""
    try:
        from tools.inbox_consumer import route_failure_alert  # run-as-package / tests
    except ImportError:
        from inbox_consumer import route_failure_alert  # run-as-script (prod)
    route_failure_alert("codex", msg, vocab="codex-modal")


def main(argv=None) -> int:
    argparse.ArgumentParser(description="Detect codex wedged on a rate-limit model-switch modal; alert Recovery.").parse_args(argv)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
