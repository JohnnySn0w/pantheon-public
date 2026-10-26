"""Orchestrator (Orchestrator) persona optionals plug-in (T-2556 phase 14 / T-2566).

Proves Orchestrator's custom bringup surface named in this ticket's ask: the HO
resumer (``tools/ho_resumer.py`` + its ``pantheon-ho-resumer.timer``/
``.service`` systemd units) and the orchestration hook it depends on
(``tools/hooks/ho_stop_failure.py``, wired on the ``StopFailure`` event).

Deliberately NOT re-firing either script. ``hook_emissions.py`` (T-2562, the
CORE catalog, discovered independently of this optionals/ package) already
live-fires ``ho_stop_failure.py`` inside a sandboxed HOME as part of its own
per-event proof. Re-running that here would be duplicate work, not new
evidence. What this module proves instead -- and what T-2562's per-hook
emission check has no reason to know or care about -- is the PRODUCER/
CONSUMER schema contract between the two independently-authored scripts:
``ho_stop_failure.py`` writes a ``state`` dict to
``$PANTHEON_STATE_DIR/ho_limit_hit.json``; ``ho_resumer.py`` separately reads
that same file and pulls specific keys back out of it. Nothing enforces
those two scripts agree with each other except convention -- this module is
that enforcement, derived from BOTH sources via ``ast.parse`` rather than
asserted from memory. Neither script is ever imported or executed; every
check here is either a file-existence check, a plain-text substring check
(systemd unit directives), or a restricted, non-executing AST walk.

Zero real mutation, by construction. There is no scratch tree, no baseline,
no live process to sandbox -- every prove() here only ever opens files for
reading. This mirrors ``hook_emissions.py``'s own explicit "Zero real
mutation ... no restore.py Baseline needed" carve-out for its purely-sandboxed
legs; ``restore()`` is consequently a documented no-op, not a stubbed-out
requirement.

Path resolution. Two independent env-var overrides, both for test
hermeticity (production honours neither, resolving the real account paths):

* ``PANTHEON_BRINGUP_ACCOUNT_HOME`` -- same NAME as
  ``hook_emissions._ACCOUNT_HOME_ENV`` (deliberate convention reuse, not a
  code import -- one override redirects settings.json-reading modules across
  the whole optionals family). Governs where ``.claude/settings.json`` is
  read from.
* ``PANTHEON_BRINGUP_REPO_ROOT`` -- governs where the pantheon-config
  checkout (``tools/ho_resumer.py``, ``tools/hooks/ho_stop_failure.py``,
  ``tools/systemd/pantheon-ho-resumer.{timer,service}``) is read from.
  Defaults to ``<account_home>/pantheon-config``, matching the REAL deployed
  topology verified straight from ``pantheon-ho-resumer.service``'s own
  ``WorkingDirectory=%h/pantheon-config`` directive -- not assumed.

Neither the basename-extraction helper nor the AST-extraction helpers import
their nearest sibling's private equivalents (``hook_emissions._basename_of``
et al.) -- small, independently-owned duplicates instead, mirroring
``mailbox_roundtrip.py``'s own stated precedent for not sharing private state
across sibling modules.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
from pathlib import Path
from typing import Callable

from tools.bringup_acceptance.evidence import RunPaths, write_module_evidence
from tools.bringup_acceptance.inventory import EVENT_MODULE_ID, discover_hooks
from tools.bringup_acceptance.recorder import TraceRecorder
from tools.bringup_acceptance.types import (
    AcceptanceModule,
    HardStop,
    MatrixRow,
    ModuleDeclaration,
    ModuleEvidence,
    RestoreEvidence,
    RunContext,
    Tier,
    TargetInventory,
)


RESUMER_MODULE_ID = "orchestrator-ho-resumer"
STOP_FAILURE_MODULE_ID = "orchestrator-stop-failure-hook"

_ACCOUNT_HOME_ENV = "PANTHEON_BRINGUP_ACCOUNT_HOME"
_REPO_ROOT_ENV = "PANTHEON_BRINGUP_REPO_ROOT"

_SCRIPT_EXT_RE = re.compile(r"\.(py|sh|js)$")


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _account_home(account: str) -> Path:
    override = os.environ.get(_ACCOUNT_HOME_ENV)
    return Path(override) if override else Path(f"/home/{account}")


def _settings_json_path(account: str) -> Path:
    return _account_home(account) / ".claude" / "settings.json"


def _repo_root(account: str) -> Path:
    override = os.environ.get(_REPO_ROOT_ENV)
    return Path(override) if override else _account_home(account) / "pantheon-config"


def _basename_of(command: str) -> str:
    """The script basename a wired command invokes, tolerant of interpreter
    prefixes (``python3 <path>``). Small independent duplicate of
    ``hook_emissions._basename_of`` -- see module docstring for why this
    module doesn't import that private sibling helper."""
    tokens = shlex.split(command) if command.strip() else []
    for token in tokens:
        if _SCRIPT_EXT_RE.search(token):
            return Path(token).name
    return Path(tokens[-1]).name if tokens else command


# ---------------------------------------------------------------------------
# Restricted, non-executing AST extraction. Every helper here parses source
# text and walks the resulting tree; none of them import or exec the target
# file.
# ---------------------------------------------------------------------------


def _top_level_assignment(tree: ast.Module, name: str) -> ast.AST | None:
    for node in tree.body:
        target: ast.Name | None = None
        value: ast.AST | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target, node.value
        if target is not None and target.id == name:
            return value
    return None


def _function_node(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    return next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name), None
    )


def _has_module_level_function(source: str, *, path: Path, name: str) -> bool:
    tree = ast.parse(source, filename=str(path))
    return _function_node(tree, name) is not None


def _extract_string_constant(source: str, *, path: Path, name: str) -> str:
    tree = ast.parse(source, filename=str(path))
    value_node = _top_level_assignment(tree, name)
    if value_node is None:
        raise HardStop(f"{path}: no module-level assignment to {name!r} found")
    if not isinstance(value_node, ast.Constant) or not isinstance(value_node.value, str):
        raise HardStop(f"{path}: {name!r} is not a string literal: {ast.dump(value_node)}")
    return value_node.value


# Restricted numeric evaluator: number literals, and Add/Mult of number
# literals ONLY (e.g. the real ``24 * 3600``-shape constants in
# ho_resumer.py). Deliberately NOT general expression evaluation, even
# though the source being read is trusted repo content -- anything outside
# this narrow shape is a HardStop naming the assignment, never a silent
# best-effort guess (ratified: team-lead review of this ticket's plan).
_NUMERIC_BINOPS: dict[type, Callable[[float, float], float]] = {
    ast.Add: lambda left, right: left + right,
    ast.Mult: lambda left, right: left * right,
}


def _eval_numeric_literal(node: ast.AST, *, path: Path, name: str) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(
        node.value, bool
    ):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _NUMERIC_BINOPS:
        left = _eval_numeric_literal(node.left, path=path, name=name)
        right = _eval_numeric_literal(node.right, path=path, name=name)
        return _NUMERIC_BINOPS[type(node.op)](left, right)
    raise HardStop(
        f"{path}: {name!r} is not a numeric literal or a numeric-literal Add/Mult "
        f"expression: {ast.dump(node)}"
    )


def _extract_numeric_constant(source: str, *, path: Path, name: str) -> float:
    tree = ast.parse(source, filename=str(path))
    value_node = _top_level_assignment(tree, name)
    if value_node is None:
        raise HardStop(f"{path}: no module-level assignment to {name!r} found")
    return _eval_numeric_literal(value_node, path=path, name=name)


def _extract_dict_literal_keys(
    source: str, *, path: Path, func_name: str, var_name: str
) -> tuple[str, ...]:
    """String keys of the dict literal assigned to ``var_name`` anywhere
    inside ``func_name``'s body -- the PRODUCER side of a state-schema
    contract."""
    tree = ast.parse(source, filename=str(path))
    func = _function_node(tree, func_name)
    if func is None:
        raise HardStop(f"{path}: no module-level function {func_name!r} found")
    for node in ast.walk(func):
        target: ast.Name | None = None
        value: ast.AST | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target, node.value
        if target is not None and target.id == var_name and isinstance(value, ast.Dict):
            keys: list[str] = []
            for key_node in value.keys:
                if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
                    raise HardStop(
                        f"{path}: {func_name}()'s {var_name!r} dict literal has a non-string-"
                        f"constant key: {ast.dump(key_node) if key_node is not None else None}"
                    )
                keys.append(key_node.value)
            return tuple(keys)
    raise HardStop(f"{path}: no dict literal assigned to {var_name!r} found inside {func_name}()")


def _extract_get_call_keys(
    source: str, *, path: Path, func_name: str, var_name: str
) -> tuple[str, ...]:
    """String keys read via ``<var_name>.get("key", ...)`` anywhere inside
    ``func_name``'s body -- the CONSUMER side of a state-schema contract."""
    tree = ast.parse(source, filename=str(path))
    func = _function_node(tree, func_name)
    if func is None:
        raise HardStop(f"{path}: no module-level function {func_name!r} found")
    keys: list[str] = []
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == var_name
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.append(node.args[0].value)
    return tuple(keys)


def _declare_orchestrator_only(module_id: str, tier: Tier, target: TargetInventory) -> ModuleDeclaration:
    applies = target.persona == "orchestrator"
    reason = (
        f"target persona is orchestrator -- {module_id} custom surface expected"
        if applies
        else f"target persona {target.persona!r} is not orchestrator -- no {module_id} surface to prove"
    )
    return ModuleDeclaration(module_id=module_id, tier=tier, applies=applies, reason=reason)


# ---------------------------------------------------------------------------
# OrchestratorHoResumerModule -- tools/ho_resumer.py + its systemd units.
# ---------------------------------------------------------------------------


class OrchestratorHoResumerModule:
    id = RESUMER_MODULE_ID
    tier: Tier = "optional"

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        return _declare_orchestrator_only(self.id, self.tier, target)

    def prove(self, ctx: RunContext) -> ModuleEvidence:
        repo = _repo_root(ctx.account)
        resumer_path = repo / "tools" / "ho_resumer.py"
        timer_path = repo / "tools" / "systemd" / "pantheon-ho-resumer.timer"
        service_path = repo / "tools" / "systemd" / "pantheon-ho-resumer.service"
        recorder = TraceRecorder(ctx.evidence_root, module_id=self.id, run_id=ctx.run_id)
        paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)

        for label, file_path in (
            ("ho_resumer.py", resumer_path),
            ("pantheon-ho-resumer.timer", timer_path),
            ("pantheon-ho-resumer.service", service_path),
        ):
            if not file_path.is_file():
                raise HardStop(f"{self.id}: expected {label} missing at {file_path}")
        recorder.append(
            "observe",
            {"resumer_path": str(resumer_path), "timer_path": str(timer_path), "service_path": str(service_path)},
        )

        resumer_source = resumer_path.read_text(encoding="utf-8")
        if not _has_module_level_function(resumer_source, path=resumer_path, name="main"):
            raise HardStop(f"{self.id}: {resumer_path} has no module-level main() function")

        resumer_prompt = _extract_string_constant(resumer_source, path=resumer_path, name="RESUMER_PROMPT")
        if not resumer_prompt.strip():
            raise HardStop(f"{self.id}: RESUMER_PROMPT in {resumer_path} is empty")

        state_file_name = _extract_string_constant(
            resumer_source, path=resumer_path, name="_DEFAULT_STATE_FILE_NAME"
        )
        if state_file_name != "ho_limit_hit.json":
            raise HardStop(
                f"{self.id}: _DEFAULT_STATE_FILE_NAME is {state_file_name!r}, expected 'ho_limit_hit.json'"
            )

        margin = _extract_numeric_constant(resumer_source, path=resumer_path, name="_RESUME_MARGIN_SECONDS")
        if not margin > 0:
            raise HardStop(f"{self.id}: _RESUME_MARGIN_SECONDS is {margin!r}, expected > 0")

        stale = _extract_numeric_constant(resumer_source, path=resumer_path, name="_STALE_MTIME_SECONDS")
        if not stale > 0:
            raise HardStop(f"{self.id}: _STALE_MTIME_SECONDS is {stale!r}, expected > 0")

        timer_text = timer_path.read_text(encoding="utf-8")
        if "OnCalendar=" not in timer_text:
            raise HardStop(f"{self.id}: {timer_path} has no OnCalendar= directive")
        if "Unit=pantheon-ho-resumer.service" not in timer_text:
            raise HardStop(f"{self.id}: {timer_path} does not target Unit=pantheon-ho-resumer.service")

        service_text = service_path.read_text(encoding="utf-8")
        if "Type=oneshot" not in service_text:
            raise HardStop(f"{self.id}: {service_path} is not Type=oneshot")
        exec_start_lines = [line for line in service_text.splitlines() if line.strip().startswith("ExecStart=")]
        if not exec_start_lines or not any("ho_resumer.py" in line for line in exec_start_lines):
            raise HardStop(f"{self.id}: {service_path} has no ExecStart= line invoking ho_resumer.py")

        detail = {
            "resumer_path": str(resumer_path),
            "timer_path": str(timer_path),
            "service_path": str(service_path),
            "resumer_prompt_len": len(resumer_prompt),
            "state_file_name": state_file_name,
            "resume_margin_seconds": margin,
            "stale_mtime_seconds": stale,
        }
        evidence_paths = (write_module_evidence(paths, self.id, "contract", detail),)
        recorder.append("result", {"status": "proven"})
        return ModuleEvidence(
            module_id=self.id, status="proven", trace_path=recorder.path,
            evidence_paths=evidence_paths, detail=detail,
        )

    def restore(self, ctx: RunContext) -> RestoreEvidence:
        return RestoreEvidence(
            module_id=self.id, restored=True,
            detail={"note": "read-only source/unit-file introspection; no mutation performed, nothing to restore"},
        )

    def report(self, evidence: ModuleEvidence) -> MatrixRow:
        summary = (
            f"{self.id}: ho_resumer.py + pantheon-ho-resumer.{{timer,service}} present and "
            f"contract-consistent (state file {evidence.detail.get('state_file_name')!r}, "
            f"margin={evidence.detail.get('resume_margin_seconds')}s, "
            f"stale-window={evidence.detail.get('stale_mtime_seconds')}s)"
        )
        return MatrixRow(
            module_id=self.id, tier=self.tier, status=evidence.status,
            summary=summary, evidence_refs=evidence.evidence_paths,
        )


# ---------------------------------------------------------------------------
# OrchestratorStopFailureHookModule -- wiring + producer/consumer schema contract.
# ---------------------------------------------------------------------------


class OrchestratorStopFailureHookModule:
    id = STOP_FAILURE_MODULE_ID
    tier: Tier = "optional"

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        return _declare_orchestrator_only(self.id, self.tier, target)

    def prove(self, ctx: RunContext) -> ModuleEvidence:
        repo = _repo_root(ctx.account)
        hook_path = repo / "tools" / "hooks" / "ho_stop_failure.py"
        resumer_path = repo / "tools" / "ho_resumer.py"
        recorder = TraceRecorder(ctx.evidence_root, module_id=self.id, run_id=ctx.run_id)
        paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)

        for label, file_path in (("ho_stop_failure.py", hook_path), ("ho_resumer.py", resumer_path)):
            if not file_path.is_file():
                raise HardStop(f"{self.id}: expected {label} missing at {file_path}")

        settings_path = _settings_json_path(ctx.account)
        settings_text = settings_path.read_text(encoding="utf-8") if settings_path.is_file() else None
        entries = discover_hooks(settings_text, ctx.account)
        stop_failure_module_id = EVENT_MODULE_ID["StopFailure"]
        wired = [
            entry for entry in entries
            if entry.module_id == stop_failure_module_id and entry.status == "applies"
        ]
        matching = [entry for entry in wired if _basename_of(str(entry.path)) == "ho_stop_failure.py"]
        recorder.append(
            "observe", {"settings_path": str(settings_path), "wired_count": len(wired)}
        )
        if not matching:
            raise HardStop(
                f"{self.id}: ho_stop_failure.py is not wired to StopFailure in {settings_path} "
                f"(wired commands: {[str(entry.path) for entry in wired]})"
            )

        producer_source = hook_path.read_text(encoding="utf-8")
        consumer_source = resumer_path.read_text(encoding="utf-8")
        producer_keys = _extract_dict_literal_keys(
            producer_source, path=hook_path, func_name="main", var_name="state"
        )
        consumer_keys = _extract_get_call_keys(
            consumer_source, path=resumer_path, func_name="main", var_name="state"
        )

        if not producer_keys:
            raise HardStop(
                f"{self.id}: extracted zero state keys from {hook_path}'s main() -- "
                "contract could not be derived"
            )
        if not consumer_keys:
            raise HardStop(
                f"{self.id}: extracted zero state.get() keys from {resumer_path}'s main() -- "
                "contract could not be derived"
            )

        missing = set(consumer_keys) - set(producer_keys)
        if missing:
            raise HardStop(
                f"{self.id}: {resumer_path} reads state key(s) {sorted(missing)} that "
                f"{hook_path}'s written schema {sorted(producer_keys)} does not produce"
            )

        detail = {
            "settings_path": str(settings_path),
            "wired_command": str(matching[0].path),
            "producer_keys": sorted(producer_keys),
            "consumer_keys": sorted(consumer_keys),
        }
        evidence_paths = (write_module_evidence(paths, self.id, "contract", detail),)
        recorder.append("result", {"status": "proven"})
        return ModuleEvidence(
            module_id=self.id, status="proven", trace_path=recorder.path,
            evidence_paths=evidence_paths, detail=detail,
        )

    def restore(self, ctx: RunContext) -> RestoreEvidence:
        return RestoreEvidence(
            module_id=self.id, restored=True,
            detail={"note": "read-only settings.json/source introspection; no mutation performed, nothing to restore"},
        )

    def report(self, evidence: ModuleEvidence) -> MatrixRow:
        summary = (
            f"{self.id}: ho_stop_failure.py wired to StopFailure and its written state schema "
            f"{evidence.detail.get('producer_keys')} covers every key ho_resumer.py reads "
            f"{evidence.detail.get('consumer_keys')}"
        )
        return MatrixRow(
            module_id=self.id, tier=self.tier, status=evidence.status,
            summary=summary, evidence_refs=evidence.evidence_paths,
        )


def build_optional_modules() -> tuple[AcceptanceModule, ...]:
    return (OrchestratorHoResumerModule(), OrchestratorStopFailureHookModule())
