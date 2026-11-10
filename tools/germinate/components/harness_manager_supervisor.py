"""harness-manager configure sub-op (a), the supervisor cutover: shared pure data (T-5830, T-5091 U4).

UNTESTED-LIVE. Built dark (HO ruling 2026-10-27): nothing here actuates. The
operative path stays ``tools/persona_cutover.sh`` / ``tools/persona_rollback.sh`` with
``tools/persona_cutover_resolve.py`` (all three untouched; T-3444 is not retired).
The first live fire is supervised. If any op here ever needs root it is
reclassified (a host-substrate family, ORG-080 A-4), never widened in place
(Recovery D15 (T-5830)).

This leaf module is imported by BOTH the planner (harness-manager.py, to write each
op's postconditions into the op as data) and the simulated handler
(harness-manager_handlers.py, to evaluate them). It is pure: no I/O.

Contents:

* the constants persona_rollback.sh hardcodes (runtime scope, codex receiver, consume
  slice) and the urgent-env assignment persona_cutover.sh writes, re-stated here as
  literals; a test greps them back out of the scripts so they cannot drift;
* EXACT tmux targets (Recovery D15 (T-5830) (j)): every session an op names is
  ``=<session>``. The scripts' bare ``-t <session>`` prefix-matches
  (persona_cutover.sh:247-248, persona_rollback.sh:329-330; T-5804), which these ops
  must not inherit. ``tmux_resolve`` models tmux's own target rule so a test can show
  the hazard and that ``=`` avoids it;
* the user-manager pin (Recovery D15 (T-5830) (i)): every systemd effect is in the seat's
  OWN user manager (``systemctl --user``), at ``/run/user/<seat uid>``;
* the typed CHECK table: each op carries its postconditions as
  ``[{"check": id, "params": {...}, "advisory"?: true}]`` (canonical JSON in
  ``desired["postconditions"]``); ``evaluate`` runs them over an observation view.
  Missing or malformed facts never pass;
* ``INVENTORY``: every step of both scripts, by line, mapped to its op kind or to an
  explicit out-of-scope note (the parity test reads it).
"""

from __future__ import annotations

import json
import re

#: persona_rollback.sh:68 (== tools/runtime_controller.py TMUX_RUNTIME_SCOPE_UNIT).
RUNTIME_SCOPE_UNIT = "pantheon-runtime-tmux.scope"
#: persona_rollback.sh:69. The seat's ONE codex receiver unit, in its own user manager.
CODEX_RECEIVER_UNIT = "codex-telegram-poller.service"
#: persona_rollback.sh:78 (defensive only there; recorded, never asserted here).
RUNTIME_CONSUME_SLICE = "app-pantheon-persona-consume.slice"
#: persona_cutover.sh:179 / :230.
URGENT_ENV = "PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED"
URGENT_DROPIN_CONTENT = f"[Service]\nEnvironment={URGENT_ENV}=1\n"
#: tools/t2611_recovery_persona_apply.py STAMP_RE (parity-tested).
STAMP_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
#: persona_cutover.sh:408.
PERMISSION_MODE = "bypassPermissions"
#: persona_cutover.sh:365 (the codex branch's expected profile).
CODEX_PROFILE = "interactive-chatgpt"
#: persona_cutover.sh:435 (sentinels reused from recovery_boot_kick.sh by the script).
COMPOSER_READY_RE = re.compile("(╭(─)+╮|(─){20,})")
CHANNELS_DIALOG_RE = re.compile("Enter to confirm|Esc to cancel")

#: Recovery D15 (T-5830) (i): the only systemd manager these ops address.
SYSTEMD_MANAGER = "user"
#: Recovery D15 (T-5830) (n): who is paged when the composite sv-retire undo stops with the
#: seat left unsupervised. A component fact (ORG-080 A-4): the engine only carries it.
ESCALATION_RECIPIENT = "recovery"


def backup_path(path: str, stamp: str) -> str:
    """persona_cutover.sh:31-35 backup convention: ``<file>.pre-cutover-<TS>``."""

    return f"{path}.pre-cutover-{stamp}"


def companion_snapshot_name(persona: str, stamp: str) -> str:
    """T-6047: the sim cell name for the companion pre-cutover state.

    Mirrors ``companion_snapshot_path`` in tools/t2590_cutover_verify_lib.sh, whose file is
    ``~/.local/state/pantheon/persona-cutover-<persona>-<stamp>.companions``; this is that
    file's basename, so the key components are (persona, stamp) exactly. The cell lives at
    ``companion_prestate/<name>`` in the simulated seat. The LIVE port MUST map this cell to
    the script's ``.companions`` file (one record, read and written by both the script and
    the engine; parse_companion_snapshot reads the file's format)."""

    return f"persona-cutover-{persona}-{stamp}.companions"


def parse_companion_snapshot(text: str) -> dict:
    """The script's ``companion_snapshot_write`` format: one ``unit=ActiveState`` line per
    companion (an empty state is stored as ``unknown`` by the script)."""

    out = {}
    for line in text.splitlines():
        unit, sep, state = line.partition("=")
        if sep and unit:
            out.setdefault(unit, state or "unknown")
    return out


def companion_should_restore(snapshot, unit: str) -> bool:
    """``companion_should_restore``: restore unless the record PROVES the unit was not
    active. No record, an unrecorded unit, or active / activating / reloading / unknown /
    empty all restore (unknown must not read as was-inactive)."""

    if not snapshot or unit not in snapshot:
        return True
    return snapshot[unit] in ("active", "activating", "reloading", "unknown", "")


def user_runtime_dir(uid: int) -> str:
    """The seat's OWN user-manager runtime dir (tools.persona_apply_path)."""

    return f"/run/user/{uid}"


def exact_target(session: str) -> str:
    """Recovery D15 (T-5830) (j): the exact tmux target form."""

    if not session or session.startswith("="):
        raise ValueError(f"not a bare session name: {session!r}")
    return "=" + session


def session_of(target: str) -> str:
    """The session an EXACT target names; a bare (prefix-matching) target is refused."""

    if not isinstance(target, str) or not target.startswith("=") or len(target) < 2:
        raise ValueError(f"session target {target!r} is not the exact form '=<session>'")
    return target[1:]


def tmux_resolve(target: str, sessions) -> str | None:
    """tmux's own session-target rule, as data: ``=name`` matches exactly; a bare name
    matches exactly, else the first session it is a PREFIX of (the T-5804 hazard)."""

    names = sorted(sessions)
    if target.startswith("="):
        return target[1:] if target[1:] in names else None
    if target in names:
        return target
    return next((name for name in names if name.startswith(target)), None)


# --- postconditions ------------------------------------------------------------


def encode(checks) -> str:
    return json.dumps(list(checks), sort_keys=True, separators=(",", ":"))


def decode(raw: str):
    checks = json.loads(raw)
    if not isinstance(checks, list):
        raise ValueError("postconditions must be a list")
    for check in checks:
        if not (isinstance(check, dict) and check.get("check") in CHECKS and isinstance(check.get("params", {}), dict)):
            raise ValueError(f"malformed postcondition {check!r}")
    return checks


def check(check_id: str, advisory: bool = False, **params: str) -> dict:
    if check_id not in CHECKS:
        raise ValueError(f"unknown check {check_id!r}")
    out = {"check": check_id, "params": {k: str(v) for k, v in params.items()}}
    if advisory:
        out["advisory"] = True
    return out


def _bool(value) -> bool | None:
    return value if isinstance(value, bool) else None


def _unit(view, name):
    unit = (view.get("units") or {}).get(name)
    return unit if isinstance(unit, dict) else None


def _c_unit(view, p):
    unit = _unit(view, p["unit"]) or {"loaded": False, "enabled": False, "active": False}
    for key in ("enabled", "active"):
        if key in p and unit.get(key) is not (p[key] == "true"):
            return False, f"{p['unit']} {key}={unit.get(key)!r}, want {p[key]}"
    return True, f"{p['unit']} state OK"


def _c_unit_not_found_dead(view, p):
    unit = _unit(view, p["unit"])
    if unit is None or (unit.get("loaded") is False and unit.get("active") is False):
        return True, f"{p['unit']} not-found and inactive/dead"
    return False, f"{p['unit']} is still loaded or active"


def _c_session(view, p):
    sessions = view.get("sessions")
    if not isinstance(sessions, dict):
        return False, "no session observation: could not ask"
    name = session_of(p["target"])
    present = sessions.get(name) is True
    want = p["present"] == "true"
    return present is want, f"session {p['target']} present={present}, want {want}"


def _c_file(view, p):
    entry = (view.get("files") or {}).get(p["path"])
    if p["sha256"] == "absent":
        return entry is None, f"{p['path']} absent={entry is None}"
    ok = isinstance(entry, dict) and entry.get("kind") == "file" and entry.get("sha256") == p["sha256"]
    return ok, f"{p['path']} {'matches' if ok else 'does not match'} sha256 {p['sha256'][:12]}"


def _facts(view):
    facts = view.get("facts")
    return facts if isinstance(facts, dict) else {}


def _c_control_plane_session(view, p):
    value = _bool(_facts(view).get("control_plane_session"))
    return value is True, f"control-plane session {p['target']} up={value}"


def _c_scope_owns(view, p):
    facts = _facts(view)
    scope, cgroup = facts.get("runtime_scope_unit"), facts.get("tmux_server_cgroup")
    ok = isinstance(scope, str) and scope and isinstance(cgroup, str) and scope in cgroup
    return bool(ok), f"tmux server under runtime scope {scope!r}: {bool(ok)}"


def _c_scope_clean(view, p):
    cgroup = _facts(view).get("tmux_server_cgroup")
    if not isinstance(cgroup, str):
        return False, "no tmux server cgroup observation: could not ask"
    return p["scope_unit"] not in cgroup, f"tmux server outside {p['scope_unit']}: {p['scope_unit'] not in cgroup}"


def _runtime(view):
    runtime = _facts(view).get("runtime")
    return runtime if isinstance(runtime, dict) else {}


def _c_runtime_declared(view, p):
    harness = _runtime(view).get("harness")
    return harness in ("claude", "codex"), f"selected runtime harness {harness!r}"


def _applies(view, harness):
    return _runtime(view).get("harness") == harness


def _c_claude_argv(view, p):
    if not _applies(view, "claude"):
        return True, "not applicable (runtime is not claude)"
    argv = _facts(view).get("argv")
    if not isinstance(argv, str):
        return False, "no argv observation: could not ask"
    want = (f"--channels {p['channels']}", f"--model {p['model']}", f"--permission-mode {PERMISSION_MODE}")
    missing = [flag for flag in want if flag not in argv]
    return not missing, "launch flags OK" if not missing else f"launch flags missing {missing}"


def _c_codex_viability(view, p):
    if not _applies(view, "codex"):
        return True, "not applicable (runtime is not codex)"
    ok = _runtime(view).get("profile") == CODEX_PROFILE and _facts(view).get("initial_viability") is True
    return ok, f"codex/{_runtime(view).get('profile')!r} viability evidence {ok}"


def _c_urgent_env(view, p):
    env = _facts(view).get("consume_environment")
    if not isinstance(env, str):
        return False, "no consume Environment observation: could not ask"
    return p["assignment"] in env.split(), f"{p['assignment']} in effective Environment: {p['assignment'] in env.split()}"


def _c_composer_ready(view, p):
    if not _applies(view, "claude"):
        return True, "not applicable (runtime is not claude)"
    pane = _facts(view).get("pane_text")
    if not isinstance(pane, str):
        return False, "no pane observation: could not ask"
    ok = bool(COMPOSER_READY_RE.search(pane)) and not CHANNELS_DIALOG_RE.search(pane)
    return ok, "composer ready, no channels dialog" if ok else "composer not ready or channels dialog present"


def _c_telegram_poller(view, p):
    if not _applies(view, "claude"):
        return True, "not applicable (runtime is not claude)"
    value = _bool(_facts(view).get("telegram_poller_in_pane"))
    return value is True, f"telegram inbound poller in the pane's tree: {value}"


def _c_single_receiver(view, p):
    count = _facts(view).get("telegram_pollers")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return False, "no poller count observation: could not ask"
    return count <= 1, f"{count} telegram poller(s) for the seat (no second poller: {count <= 1})"


def _c_process(view, p):
    value = _bool(_facts(view).get({"claude": "claude_child", "bun": "bun_poller"}[p["which"]]))
    return value is True, f"{p['which']} child present: {value}"


CHECKS = {
    "unit": _c_unit,
    "unit-not-found-dead": _c_unit_not_found_dead,
    "session": _c_session,
    "file": _c_file,
    "control-plane-session": _c_control_plane_session,
    "scope-owns-tmux-server": _c_scope_owns,
    "scope-clean": _c_scope_clean,
    "runtime-harness-declared": _c_runtime_declared,
    "claude-launch-argv": _c_claude_argv,
    "codex-profile-viability": _c_codex_viability,
    "urgent-env": _c_urgent_env,
    "composer-ready": _c_composer_ready,
    "telegram-poller": _c_telegram_poller,
    "single-receiver": _c_single_receiver,
    "process-present": _c_process,
}


def evaluate(checks, view) -> list[dict]:
    """Every check over ``view``; a check that raises is a failure (never a pass)."""

    out = []
    for item in checks:
        try:
            ok, detail = CHECKS[item["check"]](view, item.get("params", {}))
        except Exception as exc:  # noqa: BLE001 -- a broken check is a failure, never a pass
            ok, detail = False, f"check raised {type(exc).__name__}: {exc}"
        out.append({"check": item["check"], "ok": bool(ok), "advisory": bool(item.get("advisory")), "detail": detail})
    return out


def verdict(results) -> tuple[bool, str]:
    failed = [r for r in results if not r["ok"] and not r["advisory"]]
    if failed:
        return False, "; ".join(f"{r['check']}: {r['detail']}" for r in failed)
    warns = [r for r in results if not r["ok"]]
    return True, "postconditions OK" + (f" (advisory: {'; '.join(r['detail'] for r in warns)})" if warns else "")


# --- the invariant inventory (parity-tested against both scripts) ---------------

#: Which script lines are STEPS: every mutating command, every guard (``|| die``, a
#: has-session / is-active / show probe) and every verify condition, outside the
#: dry-run echo blocks and persona_rollback.sh's helper-function bodies (their call
#: sites are the steps). A guard and its action are separate lines, so separate rows.
STEP_RE = re.compile(
    r"^(cp -p |mkdir -p |printf '\[Service\]|systemctl --user (daemon-reload|disable|stop|start|enable|restart|reset-failed)\b"
    r"|tmux kill-session |rm -f |sleep 6$|consume_timer_service_teardown_before_staged_rollback$"
    r"|consume_units_final_proof_after_staged_rollback$|DIAG=)"
    r"|\|\| die |^if tmux has-session |^if systemctl --user is-active |^if \[\[ \"\$\(systemctl --user "
    r"|python3 \"\$RESOLVER\"|launch-check|(activate|check|rollback)-staged --| precondition --manifest"
    r"|^if cutover_|grep -q \"\$SCOPE_UNIT\"|^(el)?if \[\[ \"\$SELECTED_(HARNESS|PROFILE)\" == |initial-viability"
    r"|grep -q -- \"--|grep -qE |pgrep -u |^if rollback_scope_clean "
)
#: Inclusive line ranges that hold no steps of their own (dry-run echoes, helper bodies).
STEP_EXCLUDED = {"persona_cutover.sh": ((181, 220),), "persona_rollback.sh": ((136, 232), (241, 280))}


def step_lines(name: str, text: str) -> list[int]:
    """The exact step lines of one script (pure: the caller reads the file)."""

    out = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("echo "):
            continue
        if any(lo <= number <= hi for lo, hi in STEP_EXCLUDED[name]):
            continue
        if STEP_RE.search(stripped):
            out.append(number)
    return out


#: (script, line, mapping): EXACTLY one row per step line (a bijection, enforced by
#: tests/test_harness_manager_supervisor.py, which also proves that deleting any
#: single row fails). The mapping is an op kind / plan refusal / structural rule /
#: commit check, or ``out-of-scope: <reason>``.
INVENTORY = (
    # persona_cutover.sh -- preconditions
    ("persona_cutover.sh", 106, "plan refusal: the persona is the seat scope's (desired.supervisor)"),
    ("persona_cutover.sh", 107, "plan refusal: desired.stamp required, STAMP_RE"),
    ("persona_cutover.sh", 119, "plan refusal: resolve_persona manifest precondition + target-clone digest (Q2)"),
    ("persona_cutover.sh", 132, "structural: one seat-scope sub-run as the seat's own account (A-3 / HO Q3)"),
    ("persona_cutover.sh", 133, "precondition: user_bus reachable on every supervisor op (XDG_RUNTIME_DIR)"),
    ("persona_cutover.sh", 134, "precondition: user_bus reachable on every supervisor op (DBUS address)"),
    ("persona_cutover.sh", 140, "planner: observed seat_state launch-check"),
    ("persona_cutover.sh", 141, "plan refusal: seat_state deep-sleep"),
    ("persona_cutover.sh", 152, "plan refusal: observed install-done precondition"),
    ("persona_cutover.sh", 158, "plan refusal: staged checkpoint digest == installer plan digest, receipt valid"),
    # persona_cutover.sh -- mutations, each guard its own row
    ("persona_cutover.sh", 224, "harness.supervisor-file (snapshot)"),
    ("persona_cutover.sh", 229, "mkdir (urgent drop-in dir; desired.urgent_env=1)"),
    ("persona_cutover.sh", 230, "atomic-write (urgent drop-in; desired.urgent_env=1)"),
    ("persona_cutover.sh", 234, "daemon-reload"),
    ("persona_cutover.sh", 239, "harness.supervisor-unit (retire: disable --now, gate supervisor-cutover)"),
    ("persona_cutover.sh", 247, "harness.supervisor-unit (retire: exact '=<session>' kill, Recovery D15 (j))"),
    ("persona_cutover.sh", 248, "harness.supervisor-unit (retire postcondition session absent + require-quiesced gate, Recovery D15 (m))"),
    ("persona_cutover.sh", 273, "harness.companion-unit (stop)"),
    ("persona_cutover.sh", 275, "harness.companion-unit (stop postcondition session absent + require-quiesced gate)"),
    ("persona_cutover.sh", 281, "installer activation suffix (plan.operations[activation_index:], U1 identity mapping)"),
    ("persona_cutover.sh", 294, "harness.companion-unit (start)"),
    # persona_cutover.sh -- verify (soak-crit-4) -> harness.supervisor-commit postconditions
    ("persona_cutover.sh", 305, "harness.supervisor-commit check control-plane-session"),
    ("persona_cutover.sh", 326, "commit checks unit + session per companion (window 10x1s)"),
    ("persona_cutover.sh", 351, "commit check scope-owns-tmux-server"),
    ("persona_cutover.sh", 364, "commit check runtime-harness-declared (codex branch)"),
    ("persona_cutover.sh", 365, "commit check codex-profile-viability (profile)"),
    ("persona_cutover.sh", 370, "commit check codex-profile-viability (initial viability)"),
    ("persona_cutover.sh", 375, "commit check runtime-harness-declared (claude branch)"),
    ("persona_cutover.sh", 388, "commit check claude-launch-argv (the observer selects the real claude pid)"),
    ("persona_cutover.sh", 398, "commit check claude-launch-argv (--channels)"),
    ("persona_cutover.sh", 403, "commit check claude-launch-argv (--model)"),
    ("persona_cutover.sh", 408, "commit check claude-launch-argv (--permission-mode)"),
    ("persona_cutover.sh", 423, "commit check urgent-env (desired.urgent_env=1)"),
    ("persona_cutover.sh", 435, "commit check composer-ready"),
    ("persona_cutover.sh", 450, "commit check telegram-poller (window 30x3s)"),
    ("persona_cutover.sh", 462, "out-of-scope: comms-dark diagnostic capture is live-handler evidence, not an assertion"),
    ("persona_cutover.sh", 471, "out-of-scope: comms-dark diagnostic capture (bun listing)"),
    # persona_rollback.sh -- preconditions
    ("persona_rollback.sh", 83, "plan refusal: the persona is the seat scope's (desired.supervisor)"),
    ("persona_rollback.sh", 84, "plan refusal: desired.stamp required, STAMP_RE"),
    ("persona_rollback.sh", 102, "planner: resolve_bespoke_identity (no manifest precondition)"),
    ("persona_rollback.sh", 110, "structural: one seat-scope sub-run as the seat's own account (A-3 / HO Q3)"),
    ("persona_rollback.sh", 111, "precondition: user_bus reachable on every supervisor op (XDG_RUNTIME_DIR)"),
    ("persona_rollback.sh", 112, "precondition: user_bus reachable on every supervisor op (DBUS address)"),
    ("persona_rollback.sh", 119, "planner: observed seat_state launch-check"),
    ("persona_rollback.sh", 120, "plan refusal: seat_state deep-sleep"),
    ("persona_rollback.sh", 131, "plan refusal: every backup present (never a partial restore)"),
    # persona_rollback.sh -- mutations, each guard its own row
    ("persona_rollback.sh", 284, "harness.supervisor-file (restore)"),
    ("persona_rollback.sh", 288, "daemon-reload"),
    ("persona_rollback.sh", 305, "harness.control-plane-teardown (runtime scope probe; HARD here, Recovery D15 (m))"),
    ("persona_rollback.sh", 306, "harness.control-plane-teardown (runtime scope stop; postcondition inactive, HARD)"),
    ("persona_rollback.sh", 309, "harness.control-plane-teardown (runtime scope reset-failed)"),
    ("persona_rollback.sh", 311, "harness.control-plane-teardown (codex receiver stop)"),
    ("persona_rollback.sh", 312, "harness.control-plane-teardown (postcondition codex receiver inactive, hard)"),
    ("persona_rollback.sh", 322, "harness.control-plane-teardown (consume slice stop: soft, recorded)"),
    ("persona_rollback.sh", 323, "harness.control-plane-teardown (consume slice reset-failed: soft)"),
    ("persona_rollback.sh", 329, "harness.control-plane-teardown (exact '=<session>' kill, Recovery D15 (j))"),
    ("persona_rollback.sh", 330, "harness.control-plane-teardown (postcondition session absent, hard)"),
    ("persona_rollback.sh", 335, "harness.consume-retire (timer/service stop, proven inactive/dead)"),
    ("persona_rollback.sh", 351, "harness.consume-retire (staged-receipt rollback, manifest pin or break-glass)"),
    ("persona_rollback.sh", 361, "harness.consume-retire (postconditions link absent, units not-found dead)"),
    ("persona_rollback.sh", 366, "harness.supervisor-unit (reinstate: enable, gate supervisor-cutover)"),
    ("persona_rollback.sh", 369, "harness.supervisor-unit (reinstate: restart)"),
    ("persona_rollback.sh", 370, "out-of-scope: the 6s settle is a live-handler observe window"),
    ("persona_rollback.sh", 376, "harness.consume-retire (urgent drop-in absent; moved before reinstate, DECISION)"),
    ("persona_rollback.sh", 377, "harness.consume-retire (its daemon-reload)"),
    ("persona_rollback.sh", 398, "harness.companion-unit (start)"),
    # persona_rollback.sh -- verify -> harness.supervisor-commit (bespoke) postconditions
    ("persona_rollback.sh", 404, "harness.supervisor-commit check unit active"),
    ("persona_rollback.sh", 411, "commit check unit enabled"),
    ("persona_rollback.sh", 416, "commit check session present"),
    ("persona_rollback.sh", 427, "commit check scope-clean"),
    ("persona_rollback.sh", 436, "commit check process-present claude"),
    ("persona_rollback.sh", 441, "commit check process-present bun (ADVISORY: the script only warns)"),
    ("persona_rollback.sh", 453, "commit check unit active per companion"),
)


def inventory_parity(name: str, text: str, rows=INVENTORY) -> tuple[list[int], list[int]]:
    """(step lines with no row, rows that are not a step line) for one script. Exact."""

    steps = set(step_lines(name, text))
    mapped = [line for script, line, _m in rows if script == name]
    return sorted(steps - set(mapped)), sorted(set(mapped) - steps)
