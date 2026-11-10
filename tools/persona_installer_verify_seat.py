#!/usr/bin/env python3
"""T-5449: ``verify-seat`` -- the read-only post-install check for one seat.

Intent: a post-install check fails if any seat is incomplete (T-5446 item 4).
Run it AS the target account. It reads only: ``systemctl --user show / is-enabled
/ is-active``, ``tmux display-message``, and files. It never starts, stops,
enables or disables anything and never sends keys.

Sections of the JSON report (each a list of ``{section, name, ok, detail}``):

  units        every installer-managed unit/drop-in in the plan is present with the
               managed banner and the planned body (banner revision may differ)
  enablement   every timer / the controller the plan enables is enabled
  ho-resumer   absent or disabled (neither enabled nor active)
  controller   ActiveState=active SubState=running MainPID!=0 (NRestarts reported, not
               judged: a long-lived controller may carry earlier restarts, T-5478)
  viability    initial-viability --expect-selected <primary> --require-source-provenance
  attach       the controller ATTACHED to the pane the consume fire created
  comms        comms seats only: codex poller / release / MCP registration
  containment  Claude seats: CLAUDE_CODE_SHELL_PREFIX wired to the shell-box, the wrapper
               executable, the user manager reachable, no fallback-log entries (T-5454).
               Codex-only seats report not-applicable and pass.
  flip         effective cap-swap, fallback-return and quota support. These read the
               EFFECTIVE binding (persona_bringup.load_binding), so the answer is
               truthful whatever the schema default is.

ATTACH is verified from durable state as a chain of three equal pane pids:
  pre-start pid   recorded in the install receipt by seat-service-enable-observe
                  (a read-only tmux display-message BEFORE the controller start)
  attach-line pid ``controller_start_attached_live_runtime`` in
                  controller-evidence.jsonl, recorded_at >= the receipt's created_at
  live pane pid   ``tmux display-message`` now
A live seat converged then restarted has no attach line (the new controller launches the
runtime itself, T-5479): the check then accepts a controller-launch witness instead -- a
``post_start_command`` line naming the pane plus ``active-runtime.json`` (pid, started_ts),
both at/after the receipt -- and the live-pane pid check; pre-start is not applicable.
The detail names ``shape=attach-line`` or ``shape=controller-launch``.
Pre-start == attach proves the pane was not respawned across the start; attach ==
live proves it has not been replaced since.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import codex_telegram_bridge, persona_bringup, runtime_controller  # noqa: E402
from tools import persona_installer as pim  # noqa: E402
from tools.persona_installer_apply import (  # noqa: E402
    REPO_ROOT,
    _account_uid,
    require_target_account,
)

Run = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
ATTACH_EVENT = "controller_start_attached_live_runtime"
LAUNCH_EVENT = "post_start_command"
CONTROLLER_OP = "service.runtime-controller.enable-observe"
UNIT_INSTALL_KINDS = frozenset({"user-unit-install", "seat-periodic-unit-install"})
ENABLE_KINDS = frozenset({"timer-enable", "seat-periodic-timer-enable", "seat-service-enable-observe"})
SECTIONS = (
    "units", "enablement", "ho-resumer", "controller", "viability", "attach", "comms",
    "containment", "flip",
)
FALLBACK_LOG_RELATIVE = Path(".local") / "state" / "pantheon" / "shell-box-fallback.log"


def _check(section: str, name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"section": section, "name": name, "ok": bool(ok), "detail": detail}


def _default_run(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    try:
        return subprocess.run(list(argv), check=False, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(list(argv), 124, "", str(exc))


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _unit_state(run: Run, verb: str, unit: str) -> str:
    return run(("systemctl", "--user", verb, unit)).stdout.strip()


def unit_checks(plan: Any) -> list[dict[str, Any]]:
    checks = []
    for op in plan.operations:
        if op.kind not in UNIT_INSTALL_KINDS:
            continue
        path = Path(op.target)
        if not path.is_file():
            checks.append(_check("units", op.operation_id, False, f"missing: {path}"))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.startswith(f"# {pim.MANAGED_MARKER}"):
            checks.append(_check("units", op.operation_id, False, f"managed banner absent: {path}"))
        elif pim._strip_unit_banner(text) != pim._strip_unit_banner(pim._desired_content(op).decode("utf-8")):
            checks.append(_check("units", op.operation_id, False, f"body differs from the plan: {path}"))
        else:
            checks.append(_check("units", op.operation_id, True, str(path)))
    return checks


def enablement_checks(plan: Any, run: Run) -> list[dict[str, Any]]:
    checks = []
    for op in plan.operations:
        if op.kind in ENABLE_KINDS:
            state = _unit_state(run, "is-enabled", op.target)
            checks.append(_check("enablement", op.target, state == "enabled", f"is-enabled={state or '?'}"))
    return checks


def resumer_checks(run: Run) -> list[dict[str, Any]]:
    checks = []
    for unit in pim.HO_RESUMER_UNITS:
        enabled = _unit_state(run, "is-enabled", unit)
        active = _unit_state(run, "is-active", unit)
        checks.append(_check("ho-resumer", unit, enabled != "enabled" and active != "active",
                             f"is-enabled={enabled or '?'} is-active={active or '?'}"))
    return checks


def controller_checks(plan: Any, run: Run) -> list[dict[str, Any]]:
    op = next(o for o in plan.operations if o.operation_id == CONTROLLER_OP)
    try:
        snap = pim.SystemctlUserManager(run).read_unit(op.target, pim.ReadPurpose.SERVICE_BRINGUP_OBSERVE)
        values = pim._controller_values_from_snapshot(snap)
    except pim._us.UserManagerCallFailed as exc:
        detail = (exc.transport_evidence.stderr or exc.transport_evidence.stdout or "").strip()
        return [_check("controller", "controller-state", False, detail or "systemctl show failed")]
    except pim._us.UserManagerProtocolError as exc:
        return [_check("controller", "controller-state", False,
                       f"controller show output is not the expected property set: {exc}")]
    except pim.OperationFailed as exc:
        return [_check("controller", "controller-state", False, str(exc))]
    ok = (values["ActiveState"] == "active" and values["SubState"] == "running"
          and values["MainPID"] != "0")
    return [_check("controller", "controller-state", ok, json.dumps(values, sort_keys=True))]


def viability_checks(plan: Any) -> list[dict[str, Any]]:
    op = next(o for o in plan.operations if o.operation_id == CONTROLLER_OP)
    try:
        verdict = runtime_controller.validate_initial_viability_state(
            plan.persona,
            state_root=Path(op.desired["persona_state_root"]),
            expect_selected=op.desired["expect_selected"],
            require_source_provenance=True,
        )
    except Exception as exc:  # fail closed, report
        return [_check("viability", "initial-viability", False, f"{type(exc).__name__}: {exc}")]
    ok = isinstance(verdict, Mapping) and verdict.get("ok") is True
    detail = f"selected={op.desired['expect_selected']}" if ok else f"not ok: {verdict}"
    return [_check("viability", "initial-viability", ok, detail)]


def _latest_complete_receipt(plan: Any) -> Mapping[str, Any] | None:
    best: Mapping[str, Any] | None = None
    for path in sorted(Path(plan.provenance["receipt_dir"]).glob("*.json")):
        if path.name.endswith(".plan.json"):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict) or raw.get("persona") != plan.persona or raw.get("phase") != "complete":
            continue
        if _parse_time(raw.get("created_at")) is None:
            continue
        if best is None or _parse_time(raw["created_at"]) > _parse_time(best["created_at"]):
            best = raw
    return best


def attach_checks(plan: Any, run: Run) -> list[dict[str, Any]]:
    op = next(o for o in plan.operations if o.operation_id == CONTROLLER_OP)
    receipt = _latest_complete_receipt(plan)
    if receipt is None:
        return [_check("attach", "install-receipt", False, "no complete install receipt for this persona")]
    installed_at = _parse_time(receipt["created_at"])
    checks = [_check("attach", "install-receipt", True, f"{receipt['receipt_id']} @ {receipt['created_at']}")]
    evidence_path = Path(op.desired["persona_state_root"]) / plan.persona / "runtime-controller" / "controller-evidence.jsonl"
    attach: Mapping[str, Any] | None = None
    try:
        lines = evidence_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    rows: list[Mapping[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
        when = _parse_time(row.get("recorded_at")) if isinstance(row, dict) else None
        if isinstance(row, dict) and row.get("event") == ATTACH_EVENT and when and when >= installed_at:
            if attach is None or when >= _parse_time(attach["recorded_at"]):
                attach = row
    target = op.desired.get("tmux_target", "")
    launched = False
    if attach is None:
        launch = _controller_launch_witness(rows, Path(op.desired["persona_state_root"]) / plan.persona,
                                            target, installed_at)
        if launch is None:
            checks.append(_check("attach", "attach-line", False,
                                 f"no {ATTACH_EVENT} line and no controller-launch witness ({LAUNCH_EVENT} naming "
                                 f"{target} plus a fresh active-runtime.json) at/after {receipt['created_at']} "
                                 f"in {evidence_path}; the launch shape needs the binding to declare post_start"))
            return checks
        launched = True
        attach = launch
        checks.append(_check("attach", "attach-line", True,
                             f"shape=controller-launch pid={attach['pid']} launch line at {attach['recorded_at']}"))
    else:
        checks.append(_check("attach", "attach-line", True,
                             f"shape=attach-line pid={attach.get('pid')} at {attach['recorded_at']}"))
    attach_pid = str(attach.get("pid", ""))
    live = pim.parse_pane_pid(*_tmux_pid(run, target), target) if target else pim.PANE_PID_ABSENT
    checks.append(_check("attach", "attach-pid-matches-live-pane", live == attach_pid,
                         f"attach-line pid={attach_pid} live pane pid={live} ({target})"))
    pre = ""
    for item in receipt.get("evidence", ()):
        if isinstance(item, dict) and item.get("operation_id") == CONTROLLER_OP:
            pre = str((item.get("evidence") or {}).get("controller_pre_start_pane_pid", ""))
    if launched:
        checks.append(_check("attach", "attach-pid-matches-pre-start", True,
                             "not applicable: the controller launched a new runtime after the receipt"))
    elif not pre:
        checks.append(_check("attach", "attach-pid-matches-pre-start", False,
                             "receipt records no pre-start pane pid (installed before pre-start capture; re-run apply)"))
    elif pre == pim.PANE_PID_ABSENT:
        checks.append(_check("attach", "attach-pid-matches-pre-start", False,
                             "no pane existed before the controller start (pre-start pid absent)"))
    else:
        checks.append(_check("attach", "attach-pid-matches-pre-start", pre == attach_pid,
                             f"pre-start pid={pre} attach-line pid={attach_pid}"))
    return checks


def _controller_launch_witness(
    rows: Sequence[Mapping[str, Any]], persona_dir: Path, target: str, installed_at: datetime,
) -> Mapping[str, Any] | None:
    """T-5479: the second attach witness, for a live seat converged and then restarted.

    The restarted controller LAUNCHES the runtime itself, so no ``ATTACH_EVENT`` line is
    written (that is emitted only on the attach branch). A ``post_start_command`` line is
    written only after a real runtime start (``_run_post_start_commands``), never on an
    attach, and its argv names the pane. It carries no pid, so the pid comes from
    ``active-runtime.json`` (written by ``controller_main`` with the post-launch pane pid,
    ``started_ts`` after the launch). The live-pane pid check stays the authority.
    Limitation: a binding with an empty ``post_start`` writes no such line.
    """
    if not target:
        return None
    launch: Mapping[str, Any] | None = None
    for row in rows:
        when = _parse_time(row.get("recorded_at"))
        if (row.get("event") == LAUNCH_EVENT and when and when >= installed_at
                and target in str(row.get("argv", ""))):
            if launch is None or when >= _parse_time(launch["recorded_at"]):
                launch = row
    if launch is None:
        return None
    try:
        record = json.loads((persona_dir / "active-runtime.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    started = _parse_time(record.get("started_ts")) if isinstance(record, dict) else None
    pid = record.get("pid") if isinstance(record, dict) else None
    if started is None or started < installed_at or not pid:
        return None
    return {"recorded_at": launch["recorded_at"], "pid": pid}


def _tmux_pid(run: Run, target: str) -> tuple[int, str]:
    result = run(pim.pane_pid_argv(target))
    return result.returncode, result.stdout or ""


def _bridge_for(manifest: Any, persona: str, root: Path) -> Any:
    bridge = manifest.installer.get("codex_telegram_bridge")
    personas = bridge.get("personas") if isinstance(bridge, Mapping) else None
    if not isinstance(personas, Mapping) or persona not in personas:
        return None
    return codex_telegram_bridge.binding_from_manifest(manifest, persona, root=root)


def _selected_harness(persona: str, state_root: Path) -> str:
    for name, key in (("active-runtime.json", "runtime"),):
        try:
            raw = json.loads((state_root / persona / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict) and isinstance(raw.get(key), str):
            return raw[key].split("/", 1)[0]
    try:
        raw = json.loads((state_root / persona / "runtime-controller" / "initial-viability.json").read_text("utf-8"))
        return str(raw.get("selected_runtime", "")).split("/", 1)[0]
    except (OSError, ValueError):
        return ""


def comms_checks(manifest_path: Path, persona: str, plan: Any, run: Run, *, root: Path = Path("/")) -> list[dict[str, Any]]:
    manifest = pim.Manifest.load(manifest_path)
    bridge = _bridge_for(manifest, persona, root)
    if bridge is None:
        return [_check("comms", "comms-applicable", True, "no codex_telegram_bridge entry; not a comms seat")]
    unit_dir = Path(next(o.target for o in plan.operations if o.operation_id == "service.runtime-controller.unit")).parent
    mcp_op = next((o for o in plan.operations if o.operation_id == "codex-telegram.mcp-registration"), None)
    # T-5451: ONE readiness definition, shared with the runtime controller's swap gate.
    shared = codex_telegram_bridge.receiver_readiness(
        bridge, managed_marker=pim.MANAGED_MARKER,
        poller_unit=unit_dir / bridge.codex_receiver_unit,
        codex_config=Path(mcp_op.target) if mcp_op is not None else bridge.account_home / ".codex" / "config.toml",
    )
    by_name = {c.name: c for c in shared}
    checks = [_check("comms", "poller-unit-installed", by_name["poller-unit-installed"].ok,
                     by_name["poller-unit-installed"].detail)]
    op = next(o for o in plan.operations if o.operation_id == CONTROLLER_OP)
    selected = _selected_harness(persona, Path(op.desired["persona_state_root"]))
    state = _unit_state(run, "is-active", bridge.codex_receiver_unit)
    if selected == "claude":
        checks.append(_check("comms", "poller-inactive-while-claude", state != "active",
                             f"selected={selected} poller is-active={state or '?'}"))
    elif selected:
        checks.append(_check("comms", "poller-inactive-while-claude", True,
                             f"selected runtime is {selected}; poller state not constrained"))
    else:
        checks.append(_check("comms", "poller-inactive-while-claude", False, "selected runtime unknown"))
    for name in ("release-current-pinned", "codex-mcp-registered"):
        checks.append(_check("comms", name, by_name[name].ok, by_name[name].detail))
    return checks


def containment_checks(
    plan: Any,
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], "str | None"] = shutil.which,
    read_hooks: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """T-5454 / T-5480: read-only. Runs as the seat account, so ``os.access`` is the seat's own
    answer. A seat with a claude runtime is checked on its settings.json prefix; a seat with a
    codex runtime on its config.toml hook block (present, exact, trusted, scripts executable).
    A plan with neither op is not-applicable. ``read_hooks`` (an injected codex hooks/list reader)
    additionally gates on the stored trusted_hash equalling codex's own hash; without it the codex
    trust check is presence-only (verify-seat stays hermetic and spawns no codex)."""
    env = os.environ if env is None else env
    op = next((o for o in plan.operations if o.kind == pim.SETTINGS_ENV_KIND), None)
    codex_op = next((o for o in plan.operations if o.kind == pim.CODEX_HOOK_KIND), None)
    if op is None and codex_op is None:
        note = plan.provenance.get(pim.SHELL_BOX_PROVENANCE_KEY, "not-applicable")
        return [_check("containment", "containment-applicable", True,
                       note if note.startswith("not-applicable") else f"not-applicable: {note}")]
    checks: list[dict[str, Any]] = []
    if op is not None:
        checks.extend(_claude_containment_checks(op))
    if codex_op is not None:
        checks.extend(_codex_containment_checks(codex_op, read_hooks))
    anchor = Path((op or codex_op).target)
    runtime = env.get("XDG_RUNTIME_DIR", "")
    missing = []
    if not runtime:
        missing.append("XDG_RUNTIME_DIR unset")
    elif not (Path(runtime) / "systemd" / "private").exists():
        missing.append(f"{runtime}/systemd/private absent")
    if not which("systemd-run"):
        missing.append("systemd-run not on PATH")
    checks.append(_check("containment", "user-manager", not missing,
                         "user manager reachable" if not missing else "; ".join(missing)))
    log = anchor.parent.parent / FALLBACK_LOG_RELATIVE
    try:
        size = log.stat().st_size
    except FileNotFoundError:
        size = 0
    except OSError as exc:
        size = -1
        checks.append(_check("containment", "fallback-log", False, f"unreadable: {exc}"))
    if size >= 0:
        checks.append(_check("containment", "fallback-log", size == 0,
                             "no unboxed fallbacks logged" if size == 0
                             else f"{log} is non-empty: the seat ran commands unboxed"))
    return checks


def _claude_containment_checks(op: Any) -> list[dict[str, Any]]:
    key, expected = op.desired["env_key"], op.desired["env_value"]
    settings = Path(op.target)
    try:
        doc = pim._settings_env_parse(settings.read_bytes() if settings.exists() else None)
    except (OSError, pim.OperationFailed) as exc:
        doc, problem = None, str(exc)
    else:
        problem = "settings.json absent" if doc is None else ""
    actual = (doc or {}).get("env", {}).get(key) if doc else None
    checks = [_check("containment", "settings-key", actual == expected and os.path.isabs(expected),
                     f"env.{key} == {expected}" if actual == expected
                     else problem or f"env.{key} is {actual!r}, expected {expected}")]
    target = Path(expected)
    runnable = target.is_file() and os.access(target, os.X_OK)
    checks.append(_check("containment", "prefix-executable", runnable,
                         str(target) if runnable else f"missing or not executable: {target}"))
    return checks


def _codex_containment_checks(op: Any, read_hooks: Callable[..., Any] | None) -> list[dict[str, Any]]:
    """T-5480: on-disk verdict for the codex shell-box hook block in the seat's config.toml."""
    from tools.harness_adapter import codex_shell_box as csb

    config = Path(op.target)
    codex_home, account_home = config.parent, Path(op.desired["account_home"])
    try:
        text = config.read_text(encoding="utf-8") if config.exists() else ""
    except (OSError, UnicodeDecodeError) as exc:
        return [_check("containment", "codex-hook-block", False, f"{config} unreadable: {exc}")]
    st = csb.status(text, codex_home, account_home)
    block_ok = st["block_present"] and st["definition_ok"] and op.desired["hook_command"] == st["command"]
    block_problem = next((p for p in st["problems"] if "trusted_hash" not in p
                          and "script" not in p and "shell-box missing" not in p),
                         "shell-box hook block absent or differs from the canonical definition")
    checks = [
        _check("containment", "codex-hook-block", block_ok,
               f"{st['command']} (matcher Bash) in {config}" if block_ok else block_problem),
        _check("containment", "codex-hook-trusted", bool(st["trusted_hash"]),
               f"trusted_hash stored for {st['key']}" if st["trusted_hash"]
               else f"no trusted_hash stored for the shell-box hook in {config}: codex would skip it"),
        _check("containment", "codex-hook-targets", st["hook_executable"] and st["box_executable"],
               "hook script and shell-box executable" if st["hook_executable"] and st["box_executable"]
               else "; ".join(p for p in st["problems"] if "script" in p or "shell-box missing" in p)),
    ]
    if read_hooks is not None and st["trusted_hash"]:
        digest = csb.precomputed_hash(text, codex_home, account_home,
                                      executable=Path(op.desired["codex_executable"]), read_hooks=read_hooks)
        checks.append(_check(
            "containment", "codex-hook-hash", digest is not None and digest == st["trusted_hash"],
            "stored trusted_hash equals codex's own hash" if digest and digest == st["trusted_hash"]
            else f"stored {st['trusted_hash']} vs codex {digest or 'unreadable'}"))
    return checks


def flip_checks(manifest_path: Path, persona: str) -> list[dict[str, Any]]:
    binding = persona_bringup.load_binding(manifest_path, persona)
    if not binding.controller.policy.fallbacks:
        return [_check("flip", "flip-applicable", True, "no manifest fallback; flip does not apply")]
    controller = binding.controller
    supported, reason = persona_bringup.quota_check_supported(binding, manifest_path=manifest_path)
    return [
        _check("flip", "mid_session_cap_swap_enabled", controller.mid_session_cap_swap_enabled,
               f"effective={controller.mid_session_cap_swap_enabled}"),
        _check("flip", "fallback_return_enabled", controller.fallback_return_enabled,
               f"effective={controller.fallback_return_enabled}"),
        _check("flip", "quota_check_supported", supported, reason),
    ]


def verify_seat(
    manifest_path: Path,
    persona: str,
    *,
    root: Path = Path("/"),
    run: Run = _default_run,
    repo_root: Path = REPO_ROOT,
    euid: int | None = None,
    uid_of: Callable[[str], int | None] = _account_uid,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], "str | None"] = shutil.which,
    read_hooks: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    manifest = pim.Manifest.load(manifest_path)
    plan = pim.PersonaInstaller(repo_root=repo_root).plan(manifest, persona, root=root)
    require_target_account(plan, euid=euid, uid_of=uid_of)
    checks: list[dict[str, Any]] = [
        *unit_checks(plan),
        *enablement_checks(plan, run),
        *resumer_checks(run),
        *controller_checks(plan, run),
        *viability_checks(plan),
        *attach_checks(plan, run),
        *comms_checks(manifest_path, persona, plan, run, root=root),
        *containment_checks(plan, env=env, which=which, read_hooks=read_hooks),
        *flip_checks(manifest_path, persona),
    ]
    failed = [c for c in checks if not c["ok"]]
    return {
        "schema": "pantheon-persona-verify-seat/v1",
        "persona": persona,
        "account": plan.account,
        "ok": not failed,
        "failed": failed,
        "sections": {name: [c for c in checks if c["section"] == name] for name in SECTIONS},
    }
