#!/usr/bin/env python3
"""unknown_dialog_monitor.py (T-2553) — DETECT-ONLY backstop for a claude seat parked on an
UNRECOGNISED blocking confirm dialog, alerting Recovery.

WHY: an unattended claude seat can wedge on a confirm dialog nothing in the fleet handles
(an update / NUX / trust "press to continue" dialog appearing mid-run, the ORG-031 wedge
family) and sit there until the operator notices and resumes it by hand. Known dialogs have
owners — the spend-limit cap modal (cap_modal_reaper, T-4024) and the tool-permission
prompt family (activity_monitor's stuck-seat escalation, T-3555/T-3558) — this covers the
residual: anything else with a confirm-dialog footer.

WHAT IT DOES: every run, for each fleet seat except Recovery's own (T-0559), capture the pane
and classify its BOTTOM (classify_claude_bottom_dialog, pure). A dialog counts only when
its "Enter to confirm" / "Esc to cancel" footer is the LAST non-empty line — a live dialog
draws nothing below itself; a dialog quoted in scrollback has the prompt box + status line
under it. When the SAME seat shows an unknown dialog on >= 2 consecutive runs (state file),
Recovery is alerted via route_failure_alert, cooldown-gated per seat (permission prompts: per seat + prompt, T-4829).

WHAT IT NEVER DOES: send a key. This module has no keystroke-sending code path at all — an unknown
dialog has no known-safe answer (an option may spend, grant, or switch something), so the
recovery stays Recovery's: inspect, or handoff_and_restart. It is deliberately a SEPARATE
module/timer from cap_modal_reaper so the reaper's keystroke surface stays exactly its
T-4037-gated Esc (Recovery, T-2553 rev 2).

SHIP-DARK: a no-op unless PANTHEON_UNKNOWN_DIALOG_MONITOR_ENABLED=1 (set by a drop-in when
Recovery arms it). Fail-open: every error is logged and the exit code is always 0 — a missed
alert is harmless, a wedged or OnFailure-flapping monitor is not. Sibling modules
(cap_modal_reaper for its pure bottom-anchor classifier + read-only pane capture,
activity_monitor for the known-kind classifiers + seat panes) are imported LAZILY so a
fault in either degrades only this monitor's run.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable

DIALOG_CAP_MODAL = "cap_modal"
DIALOG_PERMISSION_MODAL = "permission_modal"
DIALOG_UNKNOWN = "unknown_confirm_dialog"

ENABLE_ENV = "PANTHEON_UNKNOWN_DIALOG_MONITOR_ENABLED"  # "1" enables (default OFF)
DEFAULT_MIN_SAMPLES = 2
DEFAULT_COOLDOWN_SECONDS = 3 * 60 * 60
# T-4183: a tool-permission prompt is a KNOWN kind, so it never alerts as "unknown", but
# one nobody answers parks the seat (archivist 2026-09-28: ~40 min, seen by no monitor).
# Longer debounce than the unknown kind: the same seat on the SAME prompt for this many
# consecutive runs (~15 min on the 5-min timer) before Recovery is told.
DEFAULT_PERMISSION_MIN_SAMPLES = 3
STATE_FILENAME = "unknown_dialog_monitor.json"

# Recovery's own pane is never scanned: a self-targeted alert is noise (T-0559), and a
# dialog-wedged Recovery is covered by Recovery's own liveness monitoring, not by this.
SELF_EXCLUDED: frozenset[str] = frozenset({"recovery"})

# The confirm-dialog footer family (the tokens of submit_verify._READY_DIALOG_PATTERN and
# cap_modal_reaper._MODAL_FOOTER_RE, but either half suffices: the permission prompt's
# footer is "Esc to cancel · Tab to amend", a trust dialog's "Enter to confirm · Esc to exit").
_DIALOG_FOOTER_RE = re.compile(r"enter to confirm|esc to cancel", re.IGNORECASE)
# Every tool-permission prompt (Bash/Read "Do you want to proceed?", Edit/Write "Do you want
# to make this edit…", WebFetch, …) carries this footer.
_PERMISSION_FAMILY_MARKER = "tab to amend"


def _activity_monitor():
    try:
        from tools import activity_monitor as am  # run-as-package / tests
    except ImportError:  # pragma: no cover - bare run with cwd=tools/
        import activity_monitor as am  # type: ignore
    return am


def _reaper():
    try:
        from tools import cap_modal_reaper as reaper  # run-as-package / tests
    except ImportError:  # pragma: no cover - bare run with cwd=tools/
        import cap_modal_reaper as reaper  # type: ignore
    return reaper


def _instance_binding(persona: str) -> dict:
    try:
        from tools.agent_role_map import instance_binding  # run-as-package / tests
    except ImportError:  # pragma: no cover - bare run with cwd=tools/
        from agent_role_map import instance_binding  # type: ignore
    return instance_binding(persona) or {}


def enabled(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(ENABLE_ENV, "").strip().lower() in ("1", "true", "yes")


def classify_claude_bottom_dialog(pane_text: str | None) -> str | None:
    """PURE: classify a blocking confirm dialog at the BOTTOM of a claude pane.

    Returns None (no dialog at the bottom), DIALOG_CAP_MODAL, DIALOG_PERMISSION_MODAL, or
    DIALOG_UNKNOWN. Requires the LAST non-empty line to be a confirm-dialog footer (same
    bottom-anchor rule as cap_modal_reaper.pane_shows_cap_modal_at_bottom). Known kinds are
    judged on the trailing render span only, so an old known modal in scrollback cannot
    mask a new unknown one."""
    if not pane_text:
        return None
    am = _activity_monitor()
    reaper = _reaper()
    lines = [ln for ln in am._strip_ansi(pane_text).splitlines() if ln.strip()]
    if not lines or not _DIALOG_FOOTER_RE.search(lines[-1]):
        return None
    span = "\n".join(lines[-reaper._MODAL_SPAN_LINES:])
    if reaper.pane_shows_cap_modal_at_bottom(span) or am.classify_cap_modal(span) is not None:
        return DIALOG_CAP_MODAL
    if (_PERMISSION_FAMILY_MARKER in span.lower()
            or am.classify_permission_modal(span) is not None):
        return DIALOG_PERMISSION_MODAL
    return DIALOG_UNKNOWN


def unknown_dialog_step(entry: dict | None, kind: str | None, now: int, *,
                        min_samples: int = DEFAULT_MIN_SAMPLES,
                        cooldown: int = DEFAULT_COOLDOWN_SECONDS) -> tuple[dict, bool]:
    """PURE persistence + cooldown decision for one seat. Returns (new_entry, alert).

    `streak` counts CONSECUTIVE samples classified DIALOG_UNKNOWN; any other sample (no
    dialog, a known kind, or an unreadable pane -> None) resets it, so a transient dialog
    seen once never alerts. `last_alert` survives resets, so the per-seat cooldown also
    holds if the dialog flickers."""
    entry = dict(entry or {})
    last_alert = int(entry.get("last_alert", 0) or 0)
    if kind != DIALOG_UNKNOWN:
        return {"streak": 0, "last_alert": last_alert}, False
    streak = int(entry.get("streak", 0) or 0) + 1
    cooled = not last_alert or (now - last_alert) >= cooldown  # never alerted => not gated
    alert = streak >= min_samples and cooled
    return {"streak": streak, "last_alert": now if alert else last_alert}, alert


def _permission_fingerprint(pane_text: str) -> str:
    """Identity of the prompt on screen: its trailing render span, normalised. The same
    unanswered prompt renders identically; a new tool call is a new prompt."""
    lines = [ln.strip() for ln in _activity_monitor()._strip_ansi(pane_text or "").splitlines()
             if ln.strip()]
    return hashlib.sha256("\n".join(lines[-_reaper()._MODAL_SPAN_LINES:]).encode()).hexdigest()[:16]


def permission_prompt_step(entry: dict | None, kind: str | None, fingerprint: str | None,
                           now: int, *, min_samples: int = DEFAULT_PERMISSION_MIN_SAMPLES,
                           cooldown: int = DEFAULT_COOLDOWN_SECONDS) -> tuple[dict, bool]:
    """PURE persistence + cooldown decision for a seat parked on a tool-permission prompt
    (T-4183). Returns (perm_* fields, alert). `perm_streak` counts CONSECUTIVE samples of
    the SAME prompt (by fingerprint); anything else resets it. `perm_alerted` maps each
    alerted fingerprint to its alert time and survives resets: the cooldown is per
    (seat, prompt), so a NEW prompt alerts inside an older prompt's cooldown (T-4829)."""
    entry = dict(entry or {})
    last_alert = int(entry.get("perm_last_alert", 0) or 0)
    alerted = entry.get("perm_alerted")
    if not isinstance(alerted, dict):
        # Pre-T-4829 state: the per-seat alert belonged to the prompt then on screen.
        alerted = {entry["perm_fp"]: last_alert} if last_alert and entry.get("perm_fp") else {}
    alerted = {fp: int(ts) for fp, ts in alerted.items() if (now - int(ts)) < cooldown}
    if kind != DIALOG_PERMISSION_MODAL or not fingerprint:
        return {"perm_streak": 0, "perm_fp": None, "perm_last_alert": last_alert,
                "perm_alerted": alerted}, False
    same = entry.get("perm_fp") == fingerprint
    streak = (int(entry.get("perm_streak", 0) or 0) + 1) if same else 1
    alert = streak >= min_samples and fingerprint not in alerted
    if alert:
        alerted[fingerprint] = now
    return {"perm_streak": streak, "perm_fp": fingerprint,
            "perm_last_alert": now if alert else last_alert, "perm_alerted": alerted}, alert


def permission_prompt_message(persona: str, pane_text: str, samples: int) -> str:
    """Recovery-facing alert: the seat, how long, and the prompt itself, so triage is one look.
    Approving is a permission decision, so it states what to weigh, not what to press."""
    text = _activity_monitor()._strip_ansi(pane_text or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    excerpt = " | ".join(lines[-_reaper()._MODAL_SPAN_LINES:])[:700]
    return (
        f"🔐 [permission-prompt] {persona}'s pane has been parked on the SAME tool-permission "
        f"prompt for {samples} consecutive checks (~{samples * 5} min; T-4183). The seat is "
        f"stalled until someone answers; nothing was sent. Read the command: a one-time 'Yes' "
        f"only if it is benign and in scope (never 'don't ask again' from here), else No/Esc so "
        f"the agent adapts; FYI the seat either way. Prompt: {excerpt}"
    )


def _state_path() -> Path:
    d = os.environ.get("PANTHEON_STATE_DIR")
    base = Path(d) if d else Path.home() / ".local" / "state" / "pantheon"
    return base / STATE_FILENAME


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_json(path: Path, data: dict) -> None:
    """Atomic write (tmp + os.replace): a kill mid-write can't leave a torn state file."""
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[unknown_dialog_monitor] state write failed: {exc!r}", file=sys.stderr)
        try:
            tmp.unlink()
        except OSError:
            pass


def seat_pane_target(persona: str) -> tuple[str | None, str | None]:
    """(account, pane) for a seat: the instance binding's restart_pane, else the pane
    activity_monitor already polls for that seat (mason/artisan are scope-managed and carry
    no restart_pane in their binding). (None, None) when unresolvable."""
    try:
        binding = _instance_binding(persona)
    except Exception:
        binding = {}
    am_cfg = _activity_monitor().AGENT_CONFIG.get(persona, {})
    account = binding.get("account") or am_cfg.get("sudo_user")
    pane = binding.get("restart_pane") or am_cfg.get("tmux_target_pane")
    return account, pane


def _capture_pane(persona: str, account: str, target: str) -> str | None:
    """READ-ONLY pane capture, reusing cap_modal_reaper._capture_pane (cross-account
    `tmux capture-pane`; None on any failure)."""
    return _reaper()._capture_pane(persona, account, target)


def unknown_dialog_message(persona: str, pane_text: str, samples: int) -> str:
    """Recovery-facing alert: names the seat and quotes the dialog's trailing lines. Gives NO
    'answer it' advice: an unrecognised dialog's options are unknown."""
    text = _activity_monitor()._strip_ansi(pane_text or "")
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    excerpt = " | ".join(ln.strip() for ln in lines[-6:])[:600]
    return (
        f"🧱 [unknown-dialog] {persona}'s pane has been parked on an UNRECOGNISED confirm "
        f"dialog at the bottom for {samples} consecutive checks (T-2553). Not a cap modal or a "
        f"tool-permission prompt, so no automation will answer it — nothing was sent. "
        f"Inspect the pane and READ the dialog's options before choosing any (one may spend, "
        f"grant, or switch something — do not answer blindly); if unsure, handoff_and_restart "
        f"past it. Bottom of pane: {excerpt}"
    )


def _route_alert(persona: str, msg: str) -> None:
    try:
        from tools.inbox_consumer import route_failure_alert  # run-as-package / tests
    except ImportError:  # pragma: no cover - run-as-script (prod)
        from inbox_consumer import route_failure_alert  # type: ignore
    route_failure_alert(persona, msg, vocab="unknown-dialog")


def _route_permission_alert(persona: str, msg: str) -> None:
    try:
        from tools.inbox_consumer import route_failure_alert  # run-as-package / tests
    except ImportError:  # pragma: no cover - run-as-script (prod)
        from inbox_consumer import route_failure_alert  # type: ignore
    route_failure_alert(persona, msg, vocab="permission-prompt")


def _fleet_personas() -> tuple[str, ...]:
    return tuple(_reaper().FLEET_PERSONAS)


def run(personas: tuple[str, ...] | None = None, *,
        now: int | None = None,
        capture_pane: Callable[[str, str, str], "str | None"] = _capture_pane,
        resolve_target: Callable[[str], tuple] = seat_pane_target,
        do_alert: Callable[[str, str], None] = _route_alert,
        do_permission_alert: Callable[[str, str], None] = _route_permission_alert,
        permission_min_samples: int = DEFAULT_PERMISSION_MIN_SAMPLES,
        state_path: Path | None = None,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        cooldown: int = DEFAULT_COOLDOWN_SECONDS,
        log=print) -> dict[str, str]:
    """One DETECT-ONLY pass over the fleet. Returns {persona: disposition}. Never raises
    (per-seat fail-open; an unreadable pane just resets that seat's streak). Sends NO keys."""
    now = int(time.time()) if now is None else now
    if personas is None:
        personas = _fleet_personas()
    sp = state_path or _state_path()
    state = _load_json(sp)
    me = getpass.getuser()
    results: dict[str, str] = {}
    for persona in personas:
        try:
            if persona in SELF_EXCLUDED:
                results[persona] = "skip:self-excluded"  # T-0559
                continue
            account, pane_target = resolve_target(persona)
            if not account or not pane_target:
                results[persona] = "skip:no-pane-target"
                continue
            if account == me:
                results[persona] = "skip:self-account"  # defence in depth for T-0559
                continue
            pane = capture_pane(persona, account, pane_target)
            kind = classify_claude_bottom_dialog(pane) if pane else None
            prior = state.get(persona)
            entry, alert = unknown_dialog_step(prior, kind, now,
                                               min_samples=min_samples, cooldown=cooldown)
            fp = _permission_fingerprint(pane) if kind == DIALOG_PERMISSION_MODAL else None
            perm, perm_alert = permission_prompt_step(prior, kind, fp, now,
                                                      min_samples=permission_min_samples,
                                                      cooldown=cooldown)
            state[persona] = {**entry, **perm}
            disp = kind or ("none" if pane else "unreadable")
            if kind == DIALOG_UNKNOWN:
                disp = f"{kind}:streak={entry['streak']}"
            if kind == DIALOG_PERMISSION_MODAL:
                disp = f"{kind}:streak={perm['perm_streak']}"
            for fire, send, message in (
                    (alert, do_alert, lambda: unknown_dialog_message(persona, pane or "", entry["streak"])),
                    (perm_alert, do_permission_alert,
                     lambda: permission_prompt_message(persona, pane or "", perm["perm_streak"]))):
                if not fire:
                    continue
                try:
                    send(persona, message())
                    disp += ":alerted"
                except Exception as exc:  # alert routing must never wedge the monitor
                    disp += f":alert-failed({exc!r})"
            results[persona] = disp
        except Exception as exc:  # per-seat fail-open
            results[persona] = f"error:{exc!r}"
    _save_json(sp, state)
    flagged = {p: d for p, d in results.items()
               if d.startswith(DIALOG_UNKNOWN) or ":alerted" in d}
    log(f"[unknown_dialog_monitor] {len(personas)} seats; unknown={flagged or 'none'}; "
        f"all={results}")
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Detect a claude seat parked on an unrecognised confirm dialog; alert "
                    "Recovery. Never sends keys (T-2553).")
    p.add_argument("--persona", help="one persona (default: whole fleet)")
    args = p.parse_args(argv)
    try:
        if not enabled():
            print(f"[unknown_dialog_monitor] disabled ({ENABLE_ENV} unset) — no-op")
            return 0
        min_samples = max(1, int(os.environ.get("PANTHEON_UNKNOWN_DIALOG_MIN_SAMPLES",
                                                str(DEFAULT_MIN_SAMPLES))))
        cooldown = int(os.environ.get("PANTHEON_UNKNOWN_DIALOG_COOLDOWN_SECONDS",
                                      str(DEFAULT_COOLDOWN_SECONDS)))
        permission_min = max(1, int(os.environ.get("PANTHEON_UNKNOWN_DIALOG_PERMISSION_MIN_SAMPLES",
                                                   str(DEFAULT_PERMISSION_MIN_SAMPLES))))
        personas = (args.persona,) if args.persona else None
        run(personas, min_samples=min_samples, cooldown=cooldown,
            permission_min_samples=permission_min)
    except Exception as exc:  # fail-open: never OnFailure-flap on a monitor fault
        print(f"[unknown_dialog_monitor] run failed: {exc!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
