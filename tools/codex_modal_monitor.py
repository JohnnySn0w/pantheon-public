"""tools/codex_modal_monitor.py — detect codex wedged on a blocking TUI modal, ALERT
Recovery, and (only when armed) recover the known-safe UPDATE picker
(T-1366; T-2553 residual detect+recover layer).

Three modal kinds are classified from a live pane scrape (codex TUI state is NOT in
any jsonl, so only a PANE scrape sees it):

* ``model_switch_modal`` — the rate-limit MODEL-SWITCH modal (T-1366):

      Approaching rate limits
      Switch to gpt-5.4-mini for lower credit usage?
      › 1. Switch to gpt-5.4-mini …
        2. Keep current model
        3. Keep current model (never show again) …
      Press enter to confirm or esc to go back

  ALERT-ONLY, ALWAYS — armed or not. Recovery dismisses with **Esc** (keeps the current
  model); **Enter would SWITCH the model**, a goal-level the operator call
  (reference_spend_limit_modal_wedge, codex variant). This monitor NEVER sends a key
  for this kind.

* ``update_modal`` — codex's update-available picker (tools/codex_modal.py
  ``is_codex_update_modal``). Startup is already protected (harness_adapter/codex.py
  ``ensure_update_check_disabled``); this is the live-session backstop for a shape that
  slips past it (T-2553).

* ``migration_modal`` — codex's model-migration prompt (tools/codex_modal.py
  ``is_codex_model_migration_modal``, T-2599). ALERT-ONLY, ALWAYS — armed or not (Recovery,
  T-2553 rev 3): its Enter accepts the highlighted "Try new model", which would change the
  seat's model mid-run, and model pins are the operator's call (T-4067). Recovery clears it by hand
  with the decline option ("2. Use existing model"). This monitor NEVER sends a key for
  this kind and never calls codex_modal's migration dismisser (bringup still uses it).

RECOVERY (the update picker ONLY) is SHIP-DARK: default OFF, armed by ``--arm`` or
``CODEX_MODAL_RECOVER_ARM=1`` (same arm shape as tools/cap_modal_reaper.py). Armed, the
monitor RE-CAPTURES the live pane immediately before acting, re-classifies, and only if
it is still the update picker calls ``codex_modal.dismiss_codex_update_modal`` — which
itself re-verifies before every keystroke and is bounded (never blind-sends). It then
re-captures to confirm the modal cleared. Unarmed, it detects + alerts Recovery, stating the
dismissal it WOULD have run. Every detection and every recovery outcome (recovered /
aborted / failed) alerts Recovery via route_failure_alert, cooldown-gated per kind; a failed
or aborted attempt is not retried until the cooldown elapses (no keystroke loops).

Guards on the update AND migration kinds (T-2553 review): (1) BOTTOM-ANCHORED — the modal's own
footer must be the last non-empty line and codex's normal prompt-box/status chrome must be
absent, so needle text QUOTED above codex's live prompt box never classifies; (2) PERSISTED
— the same kind must be seen on >= 2 consecutive runs (CODEX_MODAL_RECOVER_MIN_SAMPLES)
before it is alerted or acted on; (3) the cooldown stamp is written BEFORE any keystroke
('in-progress'), and a DISARMED detection also stamps it (one alert per cooldown).

ROUTING (feedback_failure_alert_routing_recovery_vs_self): codex is NOT recovery, so alerts
route to RECOVERY via route_failure_alert (not direct-to-operator).

Runs on Recovery's account; captures/sends to codex-session via ``sudo -n -u codex``.
Fail-open: a scrape/route/dismiss error never crashes the monitor and the exit code is
always 0 (a missed alert is harmless, a wedged monitor isn't).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

try:  # run-as-package / tests
    from tools import codex_modal
except ImportError:  # run-as-script (prod, tools/ is the script dir on sys.path)
    import codex_modal  # type: ignore

CODEX_PANE = os.environ.get("CODEX_PANE", "codex-session")
CODEX_USER = os.environ.get("CODEX_USER", "codex")
COOLDOWN_SECONDS = int(os.environ.get("CODEX_MODAL_COOLDOWN_SECONDS", str(3 * 60 * 60)))
ARM_ENV = "CODEX_MODAL_RECOVER_ARM"
# Seconds to wait after a dismissal before re-capturing to confirm the modal cleared.
# Module-level so tests can zero it.
RECOVER_SETTLE_SECONDS = float(os.environ.get("CODEX_MODAL_RECOVER_SETTLE_SECONDS", "1.5"))
_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
_BOTTOM_REGION_LINES = 14  # the live modal sits at the bottom; ignore older screen text

# Kinds.
MODEL_SWITCH_MODAL = "model_switch_modal"
UPDATE_MODAL = "update_modal"
MIGRATION_MODAL = "migration_modal"
# Kinds that are bottom-anchored + 2-sample debounced before any alert/action.
DEBOUNCED_KINDS = frozenset({UPDATE_MODAL, MIGRATION_MODAL})
# The ONLY kind this monitor may ever send keys for (Recovery, T-2553 rev 3). Rate-limit and
# migration both switch the model on Enter — the operator's call — so both are alert-only.
RECOVERABLE_KINDS = frozenset({UPDATE_MODAL})

# The rate-limit modal's title is the strong, specific signal. Require it PLUS a
# modal-structure marker so a passing mention can't false-fire. Model name
# (gpt-5.4-mini) is deliberately NOT keyed on — it can change.
_MODAL_TITLE = "approaching rate limits"
_MODAL_STRUCTURE = ("keep current model", "esc to go back")

# The only keys this monitor may ever send (the codex_modal dismissers' vocabulary).
_ALLOWED_KEYS = frozenset({"Enter", "Down"})

# Bottom-anchoring for the RECOVERABLE kinds. Footers of the update picker ("Press enter
# to continue") and the migration prompt ("Press enter to continue" / "Use ↑/↓ to move,
# press enter to confirm") — see tools/test_codex_modal.py fixtures.
_RECOVERABLE_FOOTER_RE = re.compile(r"press enter to (continue|confirm)", re.IGNORECASE)
# codex's normal chat-view chrome (prompt box hints + status line). Any of these in the
# bottom region means the chat view, not a full-screen modal, owns the screen.
_CODEX_CHROME_MARKERS = ("context left", "? for shortcuts", "esc to interrupt",
                         " default · ")

# Consecutive samples a RECOVERABLE kind must persist before it is alerted or acted on.
DEFAULT_RECOVER_MIN_SAMPLES = 2


def _bottom_region(pane_text: str) -> str:
    non_blank = [ln for ln in (pane_text or "").splitlines() if ln.strip()]
    return "\n".join(non_blank[-_BOTTOM_REGION_LINES:])


def _modal_owns_bottom(bottom: str) -> bool:
    """True iff a full-screen codex modal (not the normal chat view) owns the bottom:
    the LAST non-empty line is a modal footer AND none of codex's normal prompt-box /
    status chrome is in the bottom region. A modal draws nothing below its footer; codex
    QUOTING a needle (e.g. printing codex_modal.py source, or discussing the migration
    prompt) always has its prompt box + status line under that text (T-2553 review)."""
    lines = bottom.splitlines()
    if not lines or not _RECOVERABLE_FOOTER_RE.search(lines[-1]):
        return False
    low = bottom.lower()
    return not any(marker in low for marker in _CODEX_CHROME_MARKERS)


def classify_codex_pane(pane_text: str) -> str | None:
    """Pure: which blocking modal codex is wedged on, judged on the live BOTTOM region
    only (older on-screen text can't false-fire). Returns MODEL_SWITCH_MODAL,
    UPDATE_MODAL, MIGRATION_MODAL, or None.

    The rate-limit kind is checked FIRST (and unchanged from T-1366) so an ambiguous
    pane can only ever resolve to the alert-only kind. The two RECOVERABLE kinds are
    additionally BOTTOM-ANCHORED (_modal_owns_bottom): needle text quoted above codex's
    live prompt box never classifies, because arming turns this answer into keystrokes.
    Needle matching itself is delegated to tools/codex_modal (single source)."""
    if not pane_text:
        return None
    bottom = _bottom_region(pane_text)
    if not bottom:
        return None
    low = bottom.lower()
    if _MODAL_TITLE in low and any(m in low for m in _MODAL_STRUCTURE):
        return MODEL_SWITCH_MODAL
    if not _modal_owns_bottom(bottom):
        return None
    if codex_modal.is_codex_update_modal(bottom):
        return UPDATE_MODAL
    if codex_modal.is_codex_model_migration_modal(bottom):
        return MIGRATION_MODAL
    return None


def _tmux_argv(inner: str, user: str) -> list[str]:
    if user == getpass.getuser():
        return ["bash", "-lc", inner]
    return ["sudo", "-n", "-u", user, "bash", "-lc", inner]


def capture_codex_pane(pane: str = CODEX_PANE, user: str = CODEX_USER) -> str | None:
    """Capture codex's pane. Cross-account (monitor runs as Recovery) → sudo -nu codex;
    same-account path kept for tests/parity. None on any failure (caller fails open)."""
    argv = _tmux_argv(f"tmux capture-pane -p -t {shlex.quote(pane)}", user)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[codex_modal_monitor] capture error: {exc!r}", file=sys.stderr)
        return None
    return r.stdout if r.returncode == 0 else None


def send_codex_key(key: str, pane: str = CODEX_PANE, user: str = CODEX_USER) -> None:
    """Send ONE whitelisted key (Enter/Down) to codex's pane. Raises on a disallowed key
    or a failed send — recover() treats any raise as a recovery FAILURE (alerted)."""
    if key not in _ALLOWED_KEYS:
        raise ValueError(f"codex_modal_monitor refuses to send key {key!r}")
    argv = _tmux_argv(f"tmux send-keys -t {shlex.quote(pane)} {key}", user)
    r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError(f"tmux send-keys {key} exited {r.returncode}: {r.stderr.strip()!r}")


def _state_dir() -> Path:
    d = os.environ.get("PANTHEON_STATE_DIR")
    return Path(d) if d else _DEFAULT_STATE_DIR


def _state_path(kind: str = MODEL_SWITCH_MODAL) -> Path:
    """Per-kind cooldown stamp. The rate-limit kind keeps its original filename so the
    T-1366 cooldown is unchanged across this upgrade."""
    if kind == MODEL_SWITCH_MODAL:
        return _state_dir() / "codex_modal_monitor.json"
    return _state_dir() / f"codex_modal_monitor.{kind}.json"


def _in_cooldown(state_path: Path, cooldown: int, now: int) -> bool:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return (now - int(data.get("ts", 0))) < cooldown
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return False


def _write_json_atomic(path: Path, data: dict) -> None:
    """tmp + os.replace, so a TimeoutStartSec kill mid-write can't tear a state file."""
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[codex_modal_monitor] state write failed: {exc!r}", file=sys.stderr)
        try:
            tmp.unlink()
        except OSError:
            pass


def _stamp(state_path: Path, now: int, reason: str = "codex-modal") -> None:
    _write_json_atomic(state_path, {"ts": now, "reason": reason})


def _streak_path() -> Path:
    return _state_dir() / "codex_modal_monitor.streak.json"


def next_streak(prev: dict | None, kind: str | None) -> dict:
    """PURE: consecutive-sample counter for the DEBOUNCED kinds (update + migration). Same
    kind as last sample => +1; a different debounced kind => 1; anything else => 0."""
    if kind not in DEBOUNCED_KINDS:
        return {"kind": None, "streak": 0}
    prev = prev or {}
    streak = int(prev.get("streak", 0) or 0) + 1 if prev.get("kind") == kind else 1
    return {"kind": kind, "streak": streak}


def _update_streak(kind: str | None) -> int:
    """Persist and return the streak for this sample (fail-open: unreadable => fresh)."""
    try:
        prev = json.loads(_streak_path().read_text(encoding="utf-8"))
        if not isinstance(prev, dict):
            prev = {}
    except (OSError, json.JSONDecodeError, ValueError):
        prev = {}
    cur = next_streak(prev, kind)
    _write_json_atomic(_streak_path(), cur)
    return cur["streak"]


_ALERT_MSG = (
    "🔌 [codex-modal] codex is WEDGED on the rate-limit MODEL-SWITCH modal "
    "(\"Approaching rate limits — Switch to gpt-5.4-mini?\") — a blocking dialog it "
    "can't self-dismiss, so it sits parked behind it. DISMISS WITH **Esc** (keeps the "
    "current model; Enter would SWITCH the model = the operator's goal-level call). Then the "
    "weekly-limit / model-switch decision is the operator's — escalate only if needed. "
    "(T-1366; codex variant of the spend-limit wedge.)"
)

_MIGRATION_ALERT_MSG = (
    "🔌 [codex-modal] codex is WEDGED on the MODEL-MIGRATION prompt (\"Choose how you'd like "
    "Codex to proceed.\") — a blocking dialog it can't self-dismiss (T-2553). This monitor is "
    "ALERT-ONLY for it, armed or not: Enter accepts the highlighted \"1. Try new model\", which "
    "would SWITCH the seat's model mid-run — model pins are the operator's call (T-4067). Clear it by "
    "hand with the DECLINE option: move to \"2. Use existing model\" (Down) and confirm "
    "(Enter). Escalate the migration itself to the operator only if the old model is retiring."
)

_UPDATE_DISMISS = ("codex_modal.dismiss_codex_update_modal (navigate to 'Skip until next "
                   "version' + Enter; NEVER 'Update now')")


def recover_message(kind: str, outcome: str, detail: str = "") -> str:
    """Pure: Recovery-facing message for an UPDATE-picker detection or recovery outcome
    (outcome in disarmed / recovered / aborted / failed)."""
    if kind != UPDATE_MODAL:
        raise ValueError(f"recover_message is for the update picker only, not {kind!r}")
    how = _UPDATE_DISMISS
    head = "🔌 [codex-modal] codex is WEDGED on the UPDATE-AVAILABLE picker (T-2553). "
    if outcome == "disarmed":
        body = (f"Auto-recovery is DISARMED ({ARM_ENV} unset) — it WOULD have dismissed via "
                f"{how}. Clear it by hand the same way, or arm the monitor.")
    elif outcome == "recovered":
        body = f"AUTO-RECOVERED: dismissed via {how}; live re-capture confirms the modal cleared."
    elif outcome == "aborted":
        body = f"Auto-recovery ABORTED before any keystroke: {detail}. Please check codex-session."
    else:  # failed
        body = (f"Auto-recovery FAILED: {detail}. codex is likely still wedged — clear it by "
                f"hand via {how}, or handoff_and_restart past it.")
    return head + body


def _alert(do_alert, msg: str) -> None:
    try:
        do_alert(msg)
    except Exception as exc:  # alert routing must never wedge the monitor
        print(f"[codex_modal_monitor] alert routing failed {exc!r}", file=sys.stderr)


def recover(kind: str, *, do_capture, do_send_key, do_sleep) -> tuple[str, str]:
    """Armed recovery for the UPDATE picker (the only RECOVERABLE kind). Returns (outcome,
    detail) with outcome in {'recovered', 'aborted', 'failed'}. Never raises.

    Order (fail-CLOSED before any keystroke): live re-capture -> re-classify (must still
    be the update picker) -> codex_modal.dismiss_codex_update_modal (itself re-verifies
    before each key, bounded) -> settle -> re-capture to confirm cleared."""
    if kind not in RECOVERABLE_KINDS:  # defence in depth: rate-limit + migration never are
        return "aborted", f"kind {kind!r} is not auto-recoverable"
    try:
        live = do_capture()
    except Exception as exc:
        return "aborted", f"live re-capture raised {exc!r}"
    if not live:
        return "aborted", "live re-capture failed (pane unreadable)"
    now_kind = classify_codex_pane(live)
    if now_kind != kind:
        return "aborted", f"pane changed between detect and act (now {now_kind!r})"
    try:
        codex_modal.dismiss_codex_update_modal(
            capture=lambda: do_capture() or "", send_keys=do_send_key, sleep=do_sleep)
    except codex_modal.ModalNavigationFailed as exc:
        return "failed", f"dismissal could not verify its target ({exc})"
    except Exception as exc:
        return "failed", f"dismissal raised {exc!r}"
    if RECOVER_SETTLE_SECONDS > 0:
        do_sleep(RECOVER_SETTLE_SECONDS)
    try:
        after = do_capture()
    except Exception:
        after = None
    if after is None:
        return "failed", "keystrokes sent but post-dismiss re-capture failed (unverified)"
    if classify_codex_pane(after) == kind:
        return "failed", "keystrokes sent but the modal is still on screen"
    return "recovered", ""


def run(*, do_capture=capture_codex_pane, now: int | None = None,
        cooldown: int = COOLDOWN_SECONDS, do_alert=None, arm: bool = False,
        do_send_key: Callable[[str], None] = send_codex_key,
        do_sleep: Callable[[float], None] = time.sleep,
        min_samples: int = DEFAULT_RECOVER_MIN_SAMPLES) -> int:
    """Capture codex's pane; alert Recovery on a blocking modal (cooldown-gated per kind);
    when ``arm`` is True, also recover the UPDATE picker. Returns 0 always. Rate-limit and
    migration are ALERT-ONLY regardless of ``arm``.

    Update/migration are acted on (alerted, and update recovered if armed) only once the SAME
    kind has been seen on >= ``min_samples`` consecutive runs (persisted streak), so a
    one-sample blip never produces keystrokes. Every acted-on detection STAMPS the per-kind
    cooldown — including a DISARMED detection (so an unarmed monitor alerts once per
    cooldown, not every 5 min) — and an armed attempt stamps 'in-progress' BEFORE any
    keystroke, so a TimeoutStartSec kill mid-recovery can't cause a retry 5 min later."""
    now = int(time.time()) if now is None else now
    try:
        pane = do_capture()
    except Exception as exc:
        print(f"[codex_modal_monitor] capture raised {exc!r}", file=sys.stderr)
        return 0
    kind = classify_codex_pane(pane or "")
    streak = _update_streak(kind)
    if kind is None:
        return 0
    if kind in DEBOUNCED_KINDS and streak < min_samples:
        print(f"[codex_modal_monitor] {kind} seen ({streak}/{min_samples} samples) — "
              "waiting for persistence")
        return 0
    sp = _state_path(kind)
    if _in_cooldown(sp, cooldown, now):
        print(f"[codex_modal_monitor] {kind} present but in cooldown — skip")
        return 0
    if do_alert is None:
        do_alert = _route_alert

    if kind == MODEL_SWITCH_MODAL:
        # ALERT-ONLY regardless of arm: Enter here switches the model (the operator's call).
        _alert(do_alert, _ALERT_MSG)
        print("[codex_modal_monitor] codex model-switch modal detected — alerted Recovery")
        _stamp(sp, now)  # stamp on detection regardless of delivery (don't spam on flaky send)
        return 0

    if kind == MIGRATION_MODAL:
        # ALERT-ONLY regardless of arm (Recovery, T-2553 rev 3): Enter accepts "Try new model"
        # = a mid-run model switch = the operator's call (T-4067). No keys, no dismisser call.
        _alert(do_alert, _MIGRATION_ALERT_MSG)
        print("[codex_modal_monitor] codex migration prompt detected — alerted Recovery (alert-only)")
        _stamp(sp, now, reason=f"{kind}:alert-only")
        return 0

    if not arm:
        _alert(do_alert, recover_message(kind, "disarmed"))
        print(f"[codex_modal_monitor] {kind} detected (DISARMED) — alerted Recovery, no keys sent")
        _stamp(sp, now, reason=f"{kind}:disarmed")
        return 0

    _stamp(sp, now, reason=f"{kind}:in-progress")  # before ANY keystroke (kill-safe)
    try:
        outcome, detail = recover(kind, do_capture=do_capture,
                                  do_send_key=do_send_key, do_sleep=do_sleep)
    except Exception as exc:  # recover() is already total; belt and braces
        outcome, detail = "failed", f"recover raised {exc!r}"
    _alert(do_alert, recover_message(kind, outcome, detail))
    print(f"[codex_modal_monitor] {kind} ARMED recovery -> {outcome} {detail}".rstrip())
    # Stamp every outcome: a failed/aborted attempt is not retried (no keystroke loop)
    # until the cooldown elapses; Recovery has been told.
    _stamp(sp, now, reason=f"{kind}:{outcome}")
    return 0


def _route_alert(msg: str) -> None:
    """Route the codex failure to Recovery (others→Recovery per the failure-alert routing)."""
    try:
        from tools.inbox_consumer import route_failure_alert  # run-as-package / tests
    except ImportError:
        from inbox_consumer import route_failure_alert  # run-as-script (prod)
    route_failure_alert("codex", msg, vocab="codex-modal")


def _env_armed() -> bool:
    return os.environ.get(ARM_ENV, "").strip().lower() in ("1", "true", "yes")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Detect codex wedged on a blocking modal; alert Recovery; when armed, "
                    "recover the update picker (rate-limit + migration stay alert-only).")
    p.add_argument("--arm", action="store_true", default=_env_armed(),
                   help=f"actually dismiss the update picker (default OFF; or {ARM_ENV}=1)")
    args = p.parse_args(argv)
    try:
        min_samples = max(1, int(os.environ.get("CODEX_MODAL_RECOVER_MIN_SAMPLES",
                                                str(DEFAULT_RECOVER_MIN_SAMPLES))))
    except ValueError:
        min_samples = DEFAULT_RECOVER_MIN_SAMPLES
    return run(arm=args.arm, min_samples=min_samples)


if __name__ == "__main__":
    raise SystemExit(main())
