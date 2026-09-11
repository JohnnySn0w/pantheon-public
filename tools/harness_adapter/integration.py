"""Harness integration registry for runtime-controller orchestration."""

from __future__ import annotations

import contextlib
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping, Protocol, TYPE_CHECKING

from tools.harness_adapter.base import PantheonHarnessAdapter
from tools.harness_adapter.claude import ClaudeHarnessAdapter
from tools.harness_adapter.codex import CodexHarnessAdapter
from tools.harness_adapter.types import (
    HarnessLaunchConfig,
    ReceiverHookReceipt,
    ReceiverStateProof,
    RuntimeReadinessProof,
    SuccessorEvidenceContract,
)
from tools.codex_telegram_actions import receiver_handoff_actions as _telegram_receiver_handoff_actions

if TYPE_CHECKING:
    from tools.runtime_controller import RuntimeHandle, TmuxRuntimeContext


class HarnessIntegration(Protocol):
    name: str
    communications_managed: bool

    def build_adapter(self, binding: Any, *, harness_home: Path) -> PantheonHarnessAdapter:
        ...

    def validate_launch_config(self, launch: HarnessLaunchConfig) -> None:
        ...

    def prove_runtime_readiness(
        self,
        binding: Any,
        handle: "RuntimeHandle",
        *,
        state_root: Path,
        context: "TmuxRuntimeContext | None",
    ) -> RuntimeReadinessProof:
        ...

    def successor_evidence_contract(
        self,
        persona: str,
        transition_id: str,
        evidence: Path,
    ) -> SuccessorEvidenceContract:
        ...

    def validate_successor_ack(self, state: Mapping[str, Any], evidence: str) -> None:
        ...

    def receiver_direction(self, current: HarnessLaunchConfig, target: HarnessLaunchConfig) -> str | None:
        ...

    def prove_receiver_state(
        self,
        binding: Any,
        expected: Literal["present", "absent"],
        *,
        backend: Any | None = None,
    ) -> ReceiverStateProof:
        ...

    def run_receiver_handoff(
        self,
        binding: Any,
        direction: str,
        phase: Literal["pre", "post"],
        *,
        backend: Any | None = None,
        reason: str,
    ) -> ReceiverHookReceipt:
        ...

    def compensate_receiver_handoff(
        self,
        previous: Any,
        target: Any,
        direction: str,
        *,
        backend: Any | None = None,
        reason: str,
    ) -> ReceiverHookReceipt:
        ...


_INTEGRATIONS: dict[str, HarnessIntegration] = {}
_ALIASES = {"claude-code": "claude"}


def normalize_harness(value: str) -> str:
    key = value.strip().lower()
    normalized = _ALIASES.get(key, key)
    if normalized not in _INTEGRATIONS:
        raise ValueError(f"unsupported harness {value!r}")
    return normalized


def configured_harness_names() -> frozenset[str]:
    return frozenset(_INTEGRATIONS)


def register_integration(item: HarnessIntegration) -> None:
    if not item.name:
        raise ValueError("harness integration name is required")
    _INTEGRATIONS[item.name] = item


@contextlib.contextmanager
def registered_integration(item: HarnessIntegration) -> Iterator[None]:
    previous = _INTEGRATIONS.get(item.name)
    register_integration(item)
    try:
        yield
    finally:
        if previous is None:
            _INTEGRATIONS.pop(item.name, None)
        else:
            _INTEGRATIONS[item.name] = previous


def integration_for(name: str) -> HarnessIntegration:
    return _INTEGRATIONS[normalize_harness(name)]


class _BaseIntegration:
    communications_managed = True

    def validate_launch_config(self, launch: HarnessLaunchConfig) -> None:
        if launch.harness != self.name:
            raise ValueError(f"{launch.harness!r} does not match {self.name!r}")

    def prove_runtime_readiness(self, binding: Any, handle: "RuntimeHandle", *, state_root: Path, context: "TmuxRuntimeContext | None") -> RuntimeReadinessProof:
        del context
        state_path = state_root / str(getattr(binding, "persona")) / "runtime-controller" / "runtime.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return RuntimeReadinessProof(False, {"reason": f"runtime state unavailable: {type(exc).__name__}"})
        expected_target = f"{handle.tmux.session}:{handle.tmux.window}.{handle.tmux.pane}"
        matches = (
            state.get("harness") == handle.profile.harness
            and state.get("profile") == handle.profile.profile
            and state.get("tmux_target") == expected_target
            and state.get("last_structural_event") == "runtime_started"
        )
        return RuntimeReadinessProof(
            ready=matches,
            evidence={
                "harness": str(state.get("harness", "")),
                "profile": str(state.get("profile", "")),
                "tmux_target": str(state.get("tmux_target", "")),
                "last_structural_event": str(state.get("last_structural_event", "")),
                "state_path": str(state_path),
            },
        )

    def successor_evidence_contract(self, persona: str, transition_id: str, evidence: Path) -> SuccessorEvidenceContract:
        return SuccessorEvidenceContract(
            expected=f"hook:runtime-transfer:{transition_id}:{evidence.name}",
            target=str(evidence),
            evidence_kind="runtime-transfer",
        )

    def validate_successor_ack(self, state: Mapping[str, Any], evidence: str) -> None:
        expected = state.get("expected_successor_read_evidence")
        if isinstance(expected, str) and expected and evidence != expected:
            raise ValueError("successor read acknowledgement does not match transition target")
        if state.get("trigger") == "operator":
            target = state.get("chronicle_evidence")
            expected_sha256 = state.get("operator_handoff_sha256")
            if not isinstance(target, str) or not target:
                raise ValueError("operator handoff target is missing")
            if not isinstance(expected_sha256, str) or not expected_sha256:
                raise ValueError("operator handoff hash is missing")
            try:
                actual = hashlib.sha256(Path(target).read_bytes()).hexdigest()
            except OSError as exc:
                raise ValueError("operator handoff target is unavailable") from exc
            if actual != expected_sha256:
                raise ValueError("operator handoff target hash mismatch")

    def prove_receiver_state(self, binding: Any, expected: Literal["present", "absent"], *, backend: Any | None = None) -> ReceiverStateProof:
        if backend is not None and hasattr(backend, "prove_receiver_state"):
            proof = backend.prove_receiver_state(binding, expected)
            if isinstance(proof, ReceiverStateProof):
                return proof
        raise RuntimeError("receiver state proof requires an integration-specific probe")


class ClaudeIntegration(_BaseIntegration):
    name = "claude"

    def build_adapter(self, binding: Any, *, harness_home: Path) -> PantheonHarnessAdapter:
        return ClaudeHarnessAdapter(
            binding.role,
            claude_home=harness_home,
            mailbox_path=binding.mailbox,
            launch=binding.launch,
        )

    def receiver_direction(self, current: HarnessLaunchConfig, target: HarnessLaunchConfig) -> str | None:
        if current.harness == target.harness:
            return None
        return f"{current.harness}-to-{target.harness}"

    def run_receiver_handoff(self, binding: Any, direction: str, phase: Literal["pre", "post"], *, backend: Any | None = None, reason: str) -> ReceiverHookReceipt:
        return _run_telegram_receiver_handoff(binding, direction, phase, backend=backend, reason=reason)

    def compensate_receiver_handoff(self, previous: Any, target: Any, direction: str, *, backend: Any | None = None, reason: str) -> ReceiverHookReceipt:
        del target
        return _run_telegram_receiver_handoff(previous, direction, "post", backend=backend, reason=reason)

    def prove_receiver_state(self, binding: Any, expected: Literal["present", "absent"], *, backend: Any | None = None) -> ReceiverStateProof:
        return _prove_telegram_receiver_state(binding, expected, backend=backend)


class CodexIntegration(_BaseIntegration):
    name = "codex"

    def build_adapter(self, binding: Any, *, harness_home: Path) -> PantheonHarnessAdapter:
        return CodexHarnessAdapter(
            binding.role,
            codex_home=harness_home,
            mailbox_path=binding.mailbox,
            launch=binding.launch,
        )

    def receiver_direction(self, current: HarnessLaunchConfig, target: HarnessLaunchConfig) -> str | None:
        if current.harness == target.harness:
            return None
        return f"{current.harness}-to-{target.harness}"

    def run_receiver_handoff(self, binding: Any, direction: str, phase: Literal["pre", "post"], *, backend: Any | None = None, reason: str) -> ReceiverHookReceipt:
        return _run_telegram_receiver_handoff(binding, direction, phase, backend=backend, reason=reason)

    def compensate_receiver_handoff(self, previous: Any, target: Any, direction: str, *, backend: Any | None = None, reason: str) -> ReceiverHookReceipt:
        del target
        return _run_telegram_receiver_handoff(previous, direction, "post", backend=backend, reason=reason)

    def prove_receiver_state(self, binding: Any, expected: Literal["present", "absent"], *, backend: Any | None = None) -> ReceiverStateProof:
        return _prove_telegram_receiver_state(binding, expected, backend=backend)


def _telegram_actions(binding: Any, direction: str, phase: str) -> tuple[str, ...]:
    communications = getattr(binding, "communications", None)
    if isinstance(communications, Mapping):
        watchdog_units = tuple(communications.get("watchdog_units", ()))
        topology = str(communications.get("claude_receiver_topology", "systemd-user"))
    else:
        watchdog_units = tuple(getattr(binding, "watchdog_units", ()))
        topology = str(getattr(binding, "claude_receiver_topology", "systemd-user"))
    return _telegram_receiver_handoff_actions(
        claude_receiver_topology=topology,
        watchdog_units=watchdog_units,
        direction=direction,
        phase=phase,
    )


def _restore_watchdog_after_failed_pre(binding: Any, actions: tuple[str, ...], completed: list[str], backend: Any) -> None:
    del actions
    restored = False
    if "snapshot-claude-receiver" in completed:
        backend.execute("restore-claude-receiver", binding)
        restored = True
    if "snapshot-watchdog-units" in completed:
        backend.execute("restore-watchdog-units", binding)
        restored = True
    if restored:
        backend.execute("complete-receiver-topology-snapshot", binding)


def receiver_handoff_actions(binding: Any, direction: str, phase: str) -> tuple[str, ...]:
    return _telegram_actions(binding, direction, phase)


def run_receiver_handoff_actions(
    binding: Any,
    direction: str,
    phase: Literal["pre", "post"],
    *,
    backend: Any,
) -> ReceiverHookReceipt:
    operations = []
    actions = _telegram_actions(binding, direction, phase)
    try:
        for action in actions:
            backend.execute(action, binding)
            operations.append(action)
    except Exception:
        if phase == "pre":
            _restore_watchdog_after_failed_pre(binding, actions, operations, backend)
        raise
    return ReceiverHookReceipt(True, tuple(operations), {"direction": direction, "phase": phase})


def _run_telegram_receiver_handoff(
    binding: Any,
    direction: str,
    phase: Literal["pre", "post"],
    *,
    backend: Any | None,
    reason: str,
) -> ReceiverHookReceipt:
    if backend is None:
        return _telegram_handoff_subprocess(binding, direction, phase, reason=reason)
    return run_receiver_handoff_actions(binding, direction, phase, backend=backend)


def _telegram_bridge_tool() -> Path:
    return Path(__file__).resolve().parents[1] / "codex_telegram_bridge.py"


def _telegram_subprocess(binding: Any, *args: str, timeout_seconds: int = 90) -> Mapping[str, Any]:
    result = subprocess.run(
        (sys.executable, str(_telegram_bridge_tool()), *args),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    try:
        payload = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Telegram bridge returned invalid JSON") from exc
    if result.returncode != 0 or payload.get("ok") is False:
        detail = payload.get("error") or result.stderr.strip() or "Telegram bridge command failed"
        raise RuntimeError(str(detail))
    del binding
    return payload


def _prove_telegram_receiver_state(
    binding: Any,
    expected: Literal["present", "absent"],
    *,
    backend: Any | None,
) -> ReceiverStateProof:
    if backend is not None and hasattr(backend, "prove_receiver_state"):
        proof = backend.prove_receiver_state(binding, expected)
        if isinstance(proof, ReceiverStateProof):
            return proof
    kind = str(getattr(getattr(binding, "launch", None), "harness", ""))
    if backend is not None:
        method = getattr(backend, "_require_present" if expected == "present" else "_require_absent", None)
        if method is not None:
            method(binding, kind=kind)
            return ReceiverStateProof(expected, {"kind": kind, "backend": type(backend).__name__})
    payload = _telegram_subprocess(
        binding,
        "receiver-state",
        "--persona",
        str(getattr(binding, "persona")),
        "--kind",
        kind,
        "--expected",
        expected,
        "--json",
    )
    state = str(payload.get("state", "ambiguous"))
    evidence_raw = payload.get("evidence")
    evidence = {str(key): str(value) for key, value in evidence_raw.items()} if isinstance(evidence_raw, Mapping) else {}
    return ReceiverStateProof(state, evidence)  # type: ignore[arg-type]


def _telegram_handoff_subprocess(
    binding: Any,
    direction: str,
    phase: Literal["pre", "post"],
    *,
    reason: str,
) -> ReceiverHookReceipt:
    payload = _telegram_subprocess(
        binding,
        "handoff",
        "--persona",
        str(getattr(binding, "persona")),
        "--direction",
        direction,
        "--phase",
        phase,
        "--reason",
        reason,
        "--json",
    )
    operations = tuple(str(item) for item in payload.get("operations", ()))
    return ReceiverHookReceipt(True, operations, {"direction": direction, "phase": phase})


register_integration(ClaudeIntegration())
register_integration(CodexIntegration())
